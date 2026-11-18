"""Claude Code token usage for the Metrics page's Usage tab.

Source: Claude Code's transcripts (`claude_projects_dir`, default ~/.claude/projects), every `*.jsonl`
below it, read only. Of each line only these are used: `timestamp`, `cwd`, `sessionId` and, when the
line has `message.usage`, `message.id`, `message.model` and the four token counts (`input_tokens`,
`output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`). Message content is never
read into prep.db, logged or shown: a line is decoded with json.loads, those fields are copied out and
the rest is dropped at once. Lines without the text `"usage"` are skipped before decoding.

Incremental: usage_files keeps each file's byte offset after its last complete line, so a page load
reads only what was appended since. A file that shrank is read again from the start. A response
streamed over several lines shares one message.id; usage_messages keeps one row per id with the
largest value of each count, so reading a line twice never counts it twice.

A session's working directory names its dev3 task: `~/.dev3.0/worktrees/<folder>/<8-char task id>/...`
(or `<checkout>/.dev3/worktrees/<id>/...`). Seq, title, type, status and whether its PR merged come from
`dev3 projects list`, `dev3 tasks list` and `dev3 task show` (cached in usage_projects / usage_tasks,
refreshed at most every REFRESH_SECONDS). Without dev3 a task shows as its id. Any other directory
shows as its project (the dev3 project whose checkout holds it), else the folder name.

Definitions: a turn is one API response (one message.id). Tokens are all four counts added up. Context
size of a turn is input + cache read + cache write: what the model read for that response.
"""

import json
import logging
import sqlite3
import subprocess
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from prep import assistant
from prep.config import REPO_ROOT, Settings

log = logging.getLogger("prep.usage")

WORKTREES = Path.home() / ".dev3.0" / "worktrees"
REFRESH_SECONDS = 600  # dev3 task details are fetched again after this
RETRY_SECONDS = 60     # after a failed dev3 call, wait this long before trying again
DEV3_TIMEOUT = 10
CHUNK = 1 << 20
DAYS = (7, 14, 30)
CONTEXT_DAYS = 14      # days in the context trend (each day's last turn)
CACHE_WRITE_SHARE = .25   # "many cache writes": a session whose cache writes are at least this share
CACHE_WRITE_MIN = 1_000_000  # ... and at least this many tokens
DONE = ("completed", "cancelled")
COUNTS = ("input", "output", "cache_read", "cache_write")
ROLES = ("Applications Assistant", "Learn Assistant", "Coordinator", "Build tasks", "Main checkouts")
STANDING = ROLES[:3]

_LOCK = threading.Lock()
_LAST_TRY = [0.0]


# --- reading transcripts ------------------------------------------------------------------------

def _local_day(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().date().isoformat()
    except ValueError:
        return ts[:10]


def parse_line(raw: bytes) -> tuple | None:
    """(message_id, ts, day, session, cwd, model, input, output, cache_read, cache_write) from one
    transcript line with message.usage, else None. Only these fields are copied out."""
    if b'"usage"' not in raw:
        return None
    try:
        line = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    msg = line.get("message") if isinstance(line, dict) else None
    usage = msg.get("usage") if isinstance(msg, dict) else None
    if not isinstance(usage, dict) or not isinstance(msg.get("id"), str):
        return None
    counts = []
    for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        v = usage.get(key) or 0
        counts.append(v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0)
    if not any(counts):
        return None
    ts = line.get("timestamp") if isinstance(line.get("timestamp"), str) else ""
    if not ts:
        return None
    model = msg.get("model") if isinstance(msg.get("model"), str) else None
    session = line.get("sessionId") if isinstance(line.get("sessionId"), str) else None
    cwd = line.get("cwd") if isinstance(line.get("cwd"), str) else ""
    return (msg["id"], ts, _local_day(ts), session, cwd, model, *counts)


UPSERT = """INSERT INTO usage_messages (message_id, ts, day, session, cwd, model, input, output, cache_read, cache_write)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(message_id) DO UPDATE SET
    input = max(input, excluded.input), output = max(output, excluded.output),
    cache_read = max(cache_read, excluded.cache_read), cache_write = max(cache_write, excluded.cache_write)"""


def _read_file(conn: sqlite3.Connection, path: Path, offset: int) -> int:
    """Read complete lines from `offset`; return the new offset (just after the last newline). A last
    line still being written is left for the next scan."""
    rows = []
    with path.open("rb") as f:
        f.seek(offset)
        rest = b""  # bytes after the last newline so far; they start at `offset`
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            data = rest + chunk
            end = data.rfind(b"\n")
            if end < 0:
                rest = data
                continue
            for raw in data[:end].split(b"\n"):
                row = parse_line(raw)
                if row:
                    rows.append(row)
            offset += end + 1
            rest = data[end + 1:]
            if len(rows) >= 5000:
                conn.executemany(UPSERT, rows)
                rows = []
    conn.executemany(UPSERT, rows)
    return offset


def scan(conn: sqlite3.Connection, root: Path) -> dict:
    """Read what was appended to every transcript under `root` since the last scan."""
    stats = {"files": 0, "read": 0}
    if not root.is_dir():
        return stats
    with _LOCK:
        known = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT path, size, offset FROM usage_files")}
        for path in sorted(root.rglob("*.jsonl")):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            stats["files"] += 1
            old_size, offset = known.get(str(path), (0, 0))
            if size == old_size and offset <= size:
                continue
            if size < offset:  # rewritten or truncated: read it again; duplicates merge by message id
                offset = 0
            try:
                new_offset = _read_file(conn, path, offset)
            except OSError as e:
                log.warning("could not read a transcript (%s)", e.__class__.__name__)
                continue
            stats["read"] += 1
            conn.execute("INSERT INTO usage_files (path, size, offset) VALUES (?, ?, ?) ON CONFLICT(path) "
                         "DO UPDATE SET size = excluded.size, offset = excluded.offset", (str(path), size, new_offset))
            conn.commit()
    return stats


# --- dev3 tasks and projects --------------------------------------------------------------------

def _dev3_json(dev3: str, args: list[str]) -> object | None:
    """Run `dev3 <args> --json` (ids only on the command line) and parse it; None on any failure."""
    try:
        out = subprocess.run([dev3, *args, "--json"], capture_output=True, text=True, timeout=DEV3_TIMEOUT,
                             cwd=str(REPO_ROOT))
    except (OSError, subprocess.SubprocessError) as e:
        log.info("dev3 %s failed (%s)", args[0], e.__class__.__name__)
        return None
    if out.returncode != 0:
        log.info("dev3 %s exited %s", args[0], out.returncode)
        return None
    try:
        return json.loads(out.stdout)
    except ValueError:
        return None


def projects(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT project_id, name, path FROM usage_projects")]


def refresh_tasks(conn: sqlite3.Connection, settings: Settings, wanted: set[str], force: bool = False) -> bool:
    """Fetch dev3 projects and tasks when a wanted 8-char task id is unknown or the cache is old.
    Returns True when dev3 was asked. Never raises; without dev3 the cache stays as it is."""
    rows = {r[0][:8]: r[1] for r in conn.execute("SELECT task_id, fetched_at FROM usage_tasks")}
    stale = time.time() - REFRESH_SECONDS
    oldest = min((_epoch(rows[w]) for w in wanted if w in rows), default=time.time())
    if not force and wanted <= set(rows) and oldest > stale:
        return False
    if not force and time.time() - _LAST_TRY[0] < RETRY_SECONDS:
        return False
    _LAST_TRY[0] = time.time()
    dev3 = assistant.dev3_path(settings)
    if not dev3:
        return False
    plist = _dev3_json(dev3, ["projects", "list"])
    if not isinstance(plist, list):
        return False
    now = _stamp()
    conn.execute("DELETE FROM usage_projects")
    for p in plist:
        if isinstance(p, dict) and isinstance(p.get("id"), str) and isinstance(p.get("path"), str):
            conn.execute("INSERT OR REPLACE INTO usage_projects VALUES (?, ?, ?)", (p["id"], p.get("name"), p["path"]))
    for p in projects(conn):
        data = _dev3_json(dev3, ["tasks", "list", "--limit", "1000", "--project", p["project_id"]])
        tasks = data.get("tasks") if isinstance(data, dict) else None
        for t in tasks or []:
            if not isinstance(t, dict) or not isinstance(t.get("id"), str) or t["id"][:8] not in wanted:
                continue
            conn.execute(
                "INSERT INTO usage_tasks (task_id, project_id, seq, title, task_type, status, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(task_id) DO UPDATE SET project_id = excluded.project_id, "
                "seq = excluded.seq, title = excluded.title, task_type = excluded.task_type, "
                "status = excluded.status, fetched_at = excluded.fetched_at",
                (t["id"], p["project_id"], t.get("seq"), t.get("title"), t.get("taskType"), t.get("status"), now))
    # Whether the PR merged is only in `task show`: asked for open tasks that have usage.
    for task_id, project_id in conn.execute(
            "SELECT task_id, project_id FROM usage_tasks WHERE status NOT IN ('completed', 'cancelled')").fetchall():
        if task_id[:8] not in wanted:
            continue
        info = _dev3_json(dev3, ["task", "show", "--task", task_id, "--project", project_id])
        if isinstance(info, dict):
            prs = info.get("pullRequests") or []
            merged = any(isinstance(pr, dict) and pr.get("state") == "MERGED" for pr in prs)
            conn.execute("UPDATE usage_tasks SET pr_merged = ? WHERE task_id = ?", (int(merged), task_id))
    conn.commit()
    return True


def _stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _epoch(stamp: str) -> float:
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        return 0.0


def _worktree_folder(path: str) -> str:
    """dev3's folder name for a project's worktrees: its path with slashes as dashes."""
    return path.strip("/").replace("/", "-")


def place(cwd: str, projs: list[dict]) -> tuple[str, str | None]:
    """(project name, 8-char task id or None) for a session's working directory."""
    path = Path(cwd) if cwd else Path()
    names = {_worktree_folder(p["path"]): Path(p["path"]).name for p in projs}
    try:
        parts = path.relative_to(WORKTREES).parts
    except ValueError:
        parts = ()
    if parts:
        task = parts[1][:8] if len(parts) > 1 else None
        return names.get(parts[0]) or parts[0].rsplit("-", 1)[-1], task
    text = str(path)
    if "/.dev3/worktrees/" in text:
        head, tail = text.split("/.dev3/worktrees/", 1)
        return Path(head).name, tail.split("/")[0][:8] or None
    best = max((p for p in projs if text == p["path"] or text.startswith(p["path"].rstrip("/") + "/")),
               key=lambda p: len(p["path"]), default=None)
    if best:
        return Path(best["path"]).name, None
    return path.name or "other", None


def sync(conn: sqlite3.Connection, settings: Settings) -> None:
    """Scan new transcript lines, then refresh dev3 task details if needed. Never raises on dev3."""
    scan(conn, settings.claude_projects_dir)
    since = (date.today() - timedelta(days=max(DAYS))).isoformat()
    projs = projects(conn)
    wanted = {t for c, in conn.execute("SELECT DISTINCT cwd FROM usage_messages WHERE day >= ?", (since,))
              for _, t in [place(c, projs)] if t}
    if wanted:
        refresh_tasks(conn, settings, wanted)


# --- the report ---------------------------------------------------------------------------------

def home_project(projs: list[dict], settings: Settings) -> str:
    """This repo's project name: the assistant_project's when set and known, else from this checkout."""
    if settings.assistant_project:
        for p in projs:
            if p["project_id"].startswith(settings.assistant_project):
                return Path(p["path"]).name
    return place(str(REPO_ROOT), projs)[0]


def _tasks(conn: sqlite3.Connection) -> dict[str, dict]:
    return {r["task_id"][:8]: dict(r) for r in conn.execute("SELECT * FROM usage_tasks")}


def _price(settings: Settings, model: str | None) -> tuple[float, float, float, float] | None:
    for prefix, prices in settings.prices:
        if model and model.startswith(prefix):
            return prices
    return None


def report(conn: sqlite3.Connection, settings: Settings, today: str, days: int = 14,
           project: str | None = None) -> dict:
    """Everything the Usage tab shows, for the last `days` days (today included) and one project
    name (None: all). Numbers only; titles come from dev3."""
    projs = projects(conn)
    tasks = _tasks(conn)
    end = date.fromisoformat(today)
    start = end - timedelta(days=days - 1)
    places = {c: place(c, projs) for c, in conn.execute("SELECT DISTINCT cwd FROM usage_messages")}
    home = home_project(projs, settings)
    names = sorted({p for p, _ in places.values()})

    def role(proj: str, task: str | None) -> str:
        if not task:
            return "Main checkouts"
        info = tasks.get(task) or {}
        seq = info.get("seq")
        mine = proj == home if not settings.assistant_project else str(info.get("project_id") or "").startswith(
            settings.assistant_project)
        if seq is not None and mine and seq == settings.assistant_task:
            return "Applications Assistant"
        if seq is not None and mine and seq == settings.prep_assistant_task:
            return "Learn Assistant"
        if info.get("task_type") == "coordinator" or (seq is not None and mine and seq == settings.coordinator_task):
            return "Coordinator"
        return "Build tasks"

    per_day = {(start + timedelta(days=i)).isoformat(): [0, 0, 0, 0] for i in range(days)}
    keys: dict[tuple[str, str | None], dict] = {}
    roles = {r: 0 for r in ROLES}
    sessions: dict[str, dict] = {}
    cost, unpriced = 0.0, 0
    totals = [0, 0, 0, 0]
    turns = 0
    rows = conn.execute(
        "SELECT day, cwd, session, model, COUNT(*), SUM(input), SUM(output), SUM(cache_read), SUM(cache_write) "
        "FROM usage_messages WHERE day BETWEEN ? AND ? GROUP BY day, cwd, session, model",
        (start.isoformat(), end.isoformat()))
    for day, cwd, session, model, n, i, o, cr, cw in rows:
        proj, task = places.get(cwd) or place(cwd, projs)
        if project and proj != project:
            continue
        counts = (i, o, cr, cw)
        tokens = sum(counts)
        for k, v in enumerate(counts):
            per_day[day][k] += v
            totals[k] += v
        turns += n
        key = (proj, task)
        entry = keys.setdefault(key, {"project": proj, "task": task, "turns": 0, "tokens": 0, "cwds": set()})
        entry["turns"] += n
        entry["tokens"] += tokens
        entry["cwds"].add(cwd)
        roles[role(proj, task)] += tokens
        s = sessions.setdefault(session or "", {"key": key, "tokens": 0, "cache_write": 0})
        s["tokens"] += tokens
        s["cache_write"] += cw
        price = _price(settings, model)
        if price:
            cost += sum(v * p for v, p in zip(counts, price)) / 1_000_000
        else:
            unpriced += tokens

    total = sum(totals)

    def label(key: tuple[str, str | None]) -> dict:
        proj, task = key
        info = tasks.get(task) if task else None
        if not task:
            return {"ref": proj, "title": "main checkout"}
        if info and info.get("seq") is not None:
            ref = f"seq:{info['seq']}" if proj == home else f"seq:{proj}/{info['seq']}"
            return {"ref": ref, "title": info.get("title") or ""}
        return {"ref": task, "title": f"{proj} task"}

    top = sorted(keys.values(), key=lambda e: -e["tokens"])[:10]
    top_rows = [{**label((e["project"], e["task"])), "turns": e["turns"], "tokens": e["tokens"],
                 "per_turn": e["tokens"] // max(e["turns"], 1), "role": role(e["project"], e["task"])} for e in top]

    # Context per turn: per assistant and coordinator task, its last turn on each of its latest
    # CONTEXT_DAYS active days (any date, so the trend shows even in a short period).
    context = []
    for e in sorted(keys.values(), key=lambda e: -e["tokens"]):
        r = role(e["project"], e["task"])
        if r not in STANDING:
            continue
        marks = ",".join("?" * len(e["cwds"]))
        last: dict[str, int] = {}
        for day, size in conn.execute(
                f"SELECT day, input + cache_read + cache_write FROM usage_messages WHERE cwd IN ({marks}) "
                f"ORDER BY ts", sorted(e["cwds"])):
            last[day] = size
        trend = sorted(last.items())[-CONTEXT_DAYS:]
        if trend:
            context.append({"role": r, **label((e["project"], e["task"])), "latest": trend[-1][1], "trend": trend,
                            "over": trend[-1][1] > settings.context_warn_tokens})
    context.sort(key=lambda c: ROLES.index(c["role"]))

    save = []
    for c in context:
        if c["over"]:
            save.append(f"{c['role']} ({c['ref']}) is at {_short(c['latest'])} context per turn, over "
                        f"{_short(settings.context_warn_tokens)}: time to /compact.")
    for e in top + [k for k in keys.values() if k not in top]:
        info = tasks.get(e["task"]) if e["task"] else None
        # Standing tasks (the assistants, the coordinator) stay open by design.
        if (info and info.get("pr_merged") and info.get("status") not in DONE
                and role(e["project"], e["task"]) not in STANDING):
            lab = label((e["project"], e["task"]))
            save.append(f"{lab['ref']} {lab['title']} is still open after its PR merged: complete it to free "
                        f"its session.")
    heavy = sorted((s for s in sessions.values() if s["cache_write"] >= CACHE_WRITE_MIN
                    and s["cache_write"] >= CACHE_WRITE_SHARE * max(s["tokens"], 1)), key=lambda s: -s["cache_write"])
    for s in heavy[:3]:
        lab = label(s["key"])
        save.append(f"A session of {lab['ref']} wrote {_short(s['cache_write'])} cache tokens "
                    f"({s['cache_write'] / s['tokens']:.0%} of its tokens): the context was rebuilt often. "
                    f"Long pauses let the cache expire; fewer, shorter sessions write less.")

    return {
        "days": [(d, *v) for d, v in per_day.items()],
        "total": total, "turns": turns,
        "sessions": len([s for s in sessions if s]),
        "cache_read_share": totals[2] / total if total else None,
        "totals": dict(zip(COUNTS, totals)),
        "top": top_rows,
        "roles": [(r, n) for r, n in roles.items() if n],
        "context": context,
        "save": save,
        "cost": cost if settings.prices else None,
        "unpriced": unpriced if settings.prices else 0,
        "projects": names, "home": home,
    }


def _short(n: float) -> str:
    """1234 -> 1.2k, 3_400_000 -> 3.4M, 1.2e9 -> 1.2B."""
    for div, unit in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if n >= div:
            v = n / div
            return f"{v:.1f}".rstrip("0").rstrip(".") + unit if v < 100 else f"{v:.0f}{unit}"
    return str(int(n))


short = _short
