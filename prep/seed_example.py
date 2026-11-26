"""Build example databases from the committed, made-up fixtures.

    python -m prep.seed_example

Writes data/example.db (prep schema) and data/example-getjo.db (a stand-in for getjo's
jobs.db). Both are gitignored. Dates are shifted so the fixtures' _anchor_date becomes
today, so the dashboard shows recent activity. Try the commands against them with:

    PREP_ASSISTANT_TASK= PREP_PREP_ASSISTANT_TASK= PREP_DB=data/example.db PREP_GETJO_DB=$PWD/data/example-getjo.db python -m prep.cli today
    PREP_ASSISTANT_TASK= PREP_PREP_ASSISTANT_TASK= PREP_DB=data/example.db PREP_GETJO_DB=$PWD/data/example-getjo.db python -m prep.web --port 8766

Tests use the fixtures unshifted.
"""

import json
import logging
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from prep import db, learn
from prep.config import REPO_ROOT

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "example.json"
EXAMPLE_PREP_DB = REPO_ROOT / "data" / "example.db"
EXAMPLE_GETJO_DB = REPO_ROOT / "data" / "example-getjo.db"
EXAMPLE_CLAUDE_DIR = REPO_ROOT / "data" / "example-claude-projects"
EVALS_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "evals"
EXAMPLE_GETJO_REPO = REPO_ROOT / "data" / "example-getjo-repo"

log = logging.getLogger("prep.seed")

# Fixture keys holding a date (YYYY-MM-DD) or a timestamp starting with one.
DATE_KEYS = ("date_applied", "last_contact", "next_action_date", "due_date", "done_at", "date", "first_seen",
             "posted_at", "expires_at", "last_seen", "closed_at")

# Same shape as getjo's jobs table (getjo/core/store.py), copied rather than imported.
GETJO_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id             TEXT PRIMARY KEY,
    source         TEXT NOT NULL,
    company        TEXT NOT NULL,
    title          TEXT NOT NULL,
    location       TEXT,
    url            TEXT NOT NULL,
    description    TEXT,
    first_seen     TEXT NOT NULL,
    score          INTEGER,
    score_attempts INTEGER NOT NULL DEFAULT 0
);
"""
# getjo's newer columns (docs/design.md there, "Open and closed jobs"); left out for an older jobs.db.
GETJO_STATE_COLUMNS = ("posted_at", "expires_at", "last_seen", "closed_at")


def load_fixtures(path: Path = FIXTURES) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def shift_dates(data: dict, days: int) -> dict:
    """A copy of the fixtures with every date moved by `days` (time of day kept)."""
    def shift(value: str) -> str:
        return (date.fromisoformat(value[:10]) + timedelta(days=days)).isoformat() + value[10:]

    shifted = {}
    for key, value in data.items():
        if isinstance(value, list):
            value = [{k: shift(v) if k in DATE_KEYS and v else v for k, v in record.items()} for record in value]
        shifted[key] = value
    return shifted


def build_getjo_db(path: Path, jobs: list[dict], old_schema: bool = False) -> None:
    """Create a getjo-shaped jobs.db at `path` holding `jobs` (replaces any existing file). With
    `old_schema` the table has no open/closed columns, as before getjo added them."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    columns = ["id", "source", "company", "title", "location", "url", "description", "first_seen", "score"]
    try:
        conn.executescript(GETJO_SCHEMA)
        if not old_schema:
            for column in GETJO_STATE_COLUMNS:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")
            columns += GETJO_STATE_COLUMNS
        conn.executemany(
            f"INSERT INTO jobs ({', '.join(columns)}) VALUES ({', '.join(':' + c for c in columns)})",
            [{c: job.get(c) for c in columns} for job in jobs],
        )
        conn.commit()
    finally:
        conn.close()


def build_prep_db(path: Path, data: dict) -> None:
    """Create prep.db at `path` from fixture applications, tasks, gaps, practice sessions, Agent thread messages
    and Learn tracks and cards (replaces any existing file)."""
    path.unlink(missing_ok=True)
    conn = db.connect(path)
    try:
        for app in data.get("applications", []):
            fields = dict(app)
            # "history" lists earlier statuses: replay them so status_history is filled.
            statuses = [*fields.pop("history", []), fields.pop("status", "to_apply")]
            app_id = db.add_application(conn, fields.pop("company"), fields.pop("role"), status=statuses[0], **fields)
            for status in statuses[1:]:
                db.update_application(conn, app_id, status=status)
        db.next_actions_to_tasks(conn)  # next actions are tasks now, as the migration does for older databases
        for gap in data.get("gaps", []):
            db.add_gap(conn, gap["gap"], gap.get("date"), gap.get("session"))
        for s in data.get("sessions", []):  # practice sessions, scores only (no application, no gaps)
            db.record_session(conn, s["kind"], s["date"], stage=s.get("stage"), scores=s.get("scores"))
        for task in data.get("tasks", []):
            task_id = db.add_task(conn, task["title"], task.get("kind", "admin"), task.get("due_date"))
            if task.get("done_at"):
                db.complete_task(conn, task_id)
                conn.execute("UPDATE tasks SET done_at = ? WHERE id = ?", (task["done_at"], task_id))
                conn.commit()
        for m in data.get("messages", []):  # an Agent thread: a question and the agent's reply
            msg_id = db.add_message(conn, m["application"], m["question"])
            db.reply_message(conn, m["application"], msg_id, m["reply"])
        for b in data.get("briefs", []):  # job briefs the agent wrote
            db.save_brief(conn, b["job"], b["brief"])
        for c in data.get("comparisons", []):  # a question about several ticked jobs and its reply
            targets = [(int(k[2:]), None, None) if k.startswith("a:") else (None, k[2:], None) if k.startswith("j:")
                       else (None, None, k) for k in c["targets"]]
            msg_id = db.add_compare(conn, c["question"], targets)
            db.reply_message(conn, None, msg_id, c["reply"])
        for t in data.get("tracks", []):  # Learn tracks and their cards (all to do)
            learn.add_track(conn, t["title"], t.get("subtitle"), t.get("cover"), t.get("slug"))
        learn.add_cards(conn, data.get("cards", []))
        for t in data.get("source_ticks", []):  # Learn sources ticked done (card id, source key)
            learn.set_source_done(conn, t["card"], t["key"], True)
    finally:
        conn.close()


LATENCY = {"claude-opus-5-5": 9, "claude-sonnet-5-5": 4, "claude-fable-5-1": 16}  # made-up seconds per response


def _turn(message_id: str, ts: datetime, session: str, cwd: str, context: int, cw_share: float,
          model: str = "claude-opus-5-5", ttl: str = "1h", wait: float = 6) -> list[dict]:
    """Transcript lines for one made-up turn: the user's line, the request (api-request), then the
    response written twice (as a streamed response is) with the same message id, the last line `wait`
    seconds after the request. The content is a placeholder."""
    cache_write = int(context * cw_share)
    cache_read = int(context * .97) - cache_write
    one_hour = cache_write if ttl == "1h" else 0
    usage = {"input_tokens": context - cache_read - cache_write, "cache_read_input_tokens": cache_read,
             "cache_creation_input_tokens": cache_write, "speed": "standard", "inference_geo": "global",
             "cache_creation": {"ephemeral_1h_input_tokens": one_hour, "ephemeral_5m_input_tokens": cache_write - one_hour}}

    def at(seconds: float) -> str:
        return (ts + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    base = {"cwd": cwd, "sessionId": session}
    return [
        {**base, "timestamp": at(0), "type": "user", "message": {"role": "user", "content": "example prompt"}},
        {"timestamp": at(.1), "type": "api-request", "sessionId": session},
        {**base, "timestamp": at(.1 + wait / 3), "type": "assistant",
         "message": {"id": message_id, "model": model, "role": "assistant",
                     "content": [{"type": "text", "text": "example"}], "usage": {**usage, "output_tokens": 40}}},
        {**base, "timestamp": at(.1 + wait), "type": "assistant",
         "message": {"id": message_id, "model": model, "role": "assistant",
                     "content": [{"type": "text", "text": "example reply"}], "usage": {**usage, "output_tokens": 1500}}},
    ]


def build_usage_example(root: Path, prep_path: Path, spec: dict, anchor: date) -> None:
    """Made-up Claude Code transcripts under `root` (replaced) for the 30 days up to `anchor`, and the
    dev3 projects and tasks they belong to in prep.db at `prep_path`, so no dev3 is needed. A task with
    "jobs" (run kinds) is a worker: every "job_turns" of its turns are one run in db.runs, kinds in turn."""
    from prep import usage  # here, not at the top: usage imports the assistant module
    import shutil
    shutil.rmtree(root, ignore_errors=True)
    paths = {p["id"]: p["path"] for p in spec["projects"]}
    sources = [(t, f"{usage.WORKTREES}/{paths[t['project']].strip('/').replace('/', '-')}/{t['id'][:8]}/worktree")
               for t in spec["tasks"]]
    sources += [(c, paths[c["project"]]) for c in spec.get("checkouts", [])]
    first = anchor - timedelta(days=13)
    runs = [(src, cwd, src["days"]) for src, cwd in sources]
    runs += [(src, cwd, days) for src, cwd in sources for days in src.get("earlier", [])]
    jobs: list[tuple] = []  # (seq, kind, started, finished)
    for n, (src, cwd, (d0, d1)) in enumerate(runs):
        c0, c1 = src["context"]
        folder = root / ("-" + cwd.strip("/").replace("/", "-").replace("_", "-"))
        folder.mkdir(parents=True, exist_ok=True)
        session = f"00000000-0000-4000-8000-{n:012d}"
        model = src.get("model", "claude-opus-5-5")
        lines = []
        for day in range(d0, d1 + 1):
            when = first + timedelta(days=day)
            turns = src["turns"] // (3 if when.weekday() >= 5 else 1)
            group = src.get("job_turns", 4)
            for k in range(turns):
                progress = ((day - d0) + k / max(turns, 1)) / max(d1 - d0 + 1, 1)
                ts = datetime(when.year, when.month, when.day, 9, tzinfo=timezone.utc) + timedelta(
                    minutes=k * 540 // max(turns, 1))
                if src.get("jobs"):  # a worker's job: its turns follow each other, longer for later kinds
                    kind = src["jobs"][(day * 100 + k // group) % len(src["jobs"])]
                    step = 30 + 25 * src["jobs"].index(kind)
                    ts = datetime(when.year, when.month, when.day, 9, tzinfo=timezone.utc) + timedelta(
                        minutes=(k // group) * group * 540 // max(turns, 1), seconds=(k % group) * step)
                wait = LATENCY.get(model, 6) * (1 + progress) + k % 4
                lines += _turn(f"msg_example_{n}_{day}_{k}", ts, session, cwd, int(c0 + (c1 - c0) * progress),
                               src.get("cache_write_share", .04), model, src.get("cache_ttl", "1h"), wait)
                if src.get("jobs") and k % group == 0:
                    last = min(k + group, turns) - 1
                    end = ts + timedelta(seconds=(last - k) * step + wait + 30)
                    jobs.append((src["seq"], kind, ts - timedelta(seconds=20), end))
        with (folder / f"{session}.jsonl").open("w", encoding="utf-8") as f:
            f.writelines(json.dumps(line) + "\n" for line in lines)
    conn = db.connect(prep_path)
    try:
        stamp = "2999-01-01T00:00:00+00:00"  # never stale: example tasks are not looked up in dev3
        for p in spec["projects"]:
            conn.execute("INSERT OR REPLACE INTO usage_projects VALUES (?, ?, ?)", (p["id"], p["name"], p["path"]))
        for t in spec["tasks"]:
            conn.execute("INSERT OR REPLACE INTO usage_tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (t["id"], t["project"], t["seq"], t["title"], t["type"], t["status"], int(t["pr_merged"]), stamp))
        apps = [r[0] for r in conn.execute("SELECT id FROM applications ORDER BY id LIMIT 5")]
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        for i, (seq, kind, started, finished) in enumerate(jobs):
            source = "brief" if kind == "brief" else "message" if kind in ("ask", "comms", "learn") else "request"
            app = apps[i % len(apps)] if apps and kind != "brief" else None
            conn.execute("INSERT INTO runs (source, item, worker, kind, application_id, getjo_job_id, queued_at, "
                         "started_at, finished_at, result) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (source, f"example-{i}", seq, kind, app, "example-job" if kind == "brief" else None,
                          (started - timedelta(minutes=3)).strftime(fmt), started.strftime(fmt),
                          finished.strftime(fmt), "blocked" if i % 17 == 16 else "done"))
        conn.commit()
    finally:
        conn.close()


def build_evals_example(repo: Path) -> None:
    """A stand-in for getjo's checkout with only evals/catalog.json and evals/results/ (made-up runs),
    for the Evals page: PREP_GETJO_REPO=$PWD/data/example-getjo-repo."""
    target = repo / "evals"
    (target / "results").mkdir(parents=True, exist_ok=True)
    for src in EVALS_FIXTURES.rglob("*"):
        if src.is_file():
            dest = target / src.relative_to(EVALS_FIXTURES)
            dest.write_bytes(src.read_bytes())


def main() -> int:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
    data = load_fixtures()
    data = shift_dates(data, (date.today() - date.fromisoformat(data["_anchor_date"])).days)
    build_getjo_db(EXAMPLE_GETJO_DB, data["getjo_jobs"])
    build_prep_db(EXAMPLE_PREP_DB, data)
    build_usage_example(EXAMPLE_CLAUDE_DIR, EXAMPLE_PREP_DB, data["usage"], date.today())
    build_evals_example(EXAMPLE_GETJO_REPO)
    log.info("wrote %s, %s and %s/", EXAMPLE_PREP_DB.relative_to(REPO_ROOT), EXAMPLE_GETJO_DB.relative_to(REPO_ROOT),
             EXAMPLE_CLAUDE_DIR.relative_to(REPO_ROOT))
    log.info("try: PREP_ASSISTANT_TASK= PREP_PREP_ASSISTANT_TASK= PREP_DB=data/example.db PREP_GETJO_DB=$PWD/data/example-getjo.db python -m prep.cli today")
    log.info("or:  PREP_ASSISTANT_TASK= PREP_PREP_ASSISTANT_TASK= PREP_DB=data/example.db PREP_GETJO_DB=$PWD/data/example-getjo.db python -m prep.web --port 8766")
    log.info("Evals page on made-up runs: add PREP_GETJO_REPO=$PWD/data/example-getjo-repo")
    log.info("Metrics' Usage tab on the example transcripts: see docs/usage.md, \"Example data\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
