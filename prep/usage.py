"""Claude Code token usage, cost and response time for the Usage & cost page (/usage).

Source: Claude Code's transcripts (`claude_projects_dir`, default ~/.claude/projects), every `*.jsonl`
below it, read only. Of each line only these are used: `timestamp`, `cwd`, `sessionId` and, when the
line has `message.usage`, `message.id`, `message.model`, the four token counts (`input_tokens`,
`output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`), the 1-hour part of the cache
write (`cache_creation.ephemeral_1h_input_tokens`), `speed` and `inference_geo`. Message content is never
read into prep.db, logged or shown: a line is decoded with json.loads, those fields are copied out and
the rest is dropped at once. Lines without the text `"usage"` are skipped before decoding.

Response time (latency_ms): the time from the request to the response's last line. Claude Code writes an
`api-request` line when it sends a request; older transcripts without those lines use the last `user`
line instead. Only `type` and `timestamp` of those lines are copied out. A response whose start is not in
the part of the file being read, or that took over MAX_LATENCY_MS, keeps latency_ms NULL. It includes the
time the response took to stream, so it is an approximation of the model's speed.

Cost per job: a run (db.runs: a worker's item, started_at to finished_at) is charged with the responses
of its worker's dev3 task in that window (run_costs). Responses inside several runs of one worker are
split evenly between them.

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

Cost: an estimate at Claude API list prices (prep/prices.py, overridden by settings `prices`), worked
out per response group so the per-response modifiers apply (Haiku 5.5 long prompts, fast mode, US-only
inference). Tokens of a model without a price are counted as "price unknown", never priced.
"""

import json
import logging
import sqlite3
import subprocess
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from prep import assistant, prices as price_list
from prep.config import REPO_ROOT, Settings, worker_tasks

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
COST_KINDS = ("Cache read", "Cache write", "Output", "Input")  # how the money splits, as shown
MONEY_DAYS = 30        # the cost chart and the "last 30 days" tile
ROLES = ("Applications Assistant", "Learn Assistant", "Research worker", "Comms worker", "Coordinator", "Build tasks",
         "Main checkouts")
STANDING = ROLES[:5]
MAX_LATENCY_MS = 30 * 60 * 1000  # longer than this: not one response (a pause, a resumed session)

_LOCK = threading.Lock()
_LAST_TRY = [0.0]


# --- reading transcripts ------------------------------------------------------------------------

def _local_day(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().date().isoformat()
    except ValueError:
        return ts[:10]


def _count(v: object) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def parse_line(raw: bytes) -> tuple | None:
    """(message_id, ts, day, session, cwd, model, input, output, cache_read, cache_write, cache_write_1h,
    speed, geo) from one transcript line with message.usage, else None. Only these fields are copied
    out. cache_write_1h is None when the line does not split the cache write by duration."""
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
    counts = [_count(usage.get(key)) for key in
              ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
    if not any(counts):
        return None
    ts = line.get("timestamp") if isinstance(line.get("timestamp"), str) else ""
    if not ts:
        return None
    model = msg.get("model") if isinstance(msg.get("model"), str) else None
    session = line.get("sessionId") if isinstance(line.get("sessionId"), str) else None
    cwd = line.get("cwd") if isinstance(line.get("cwd"), str) else ""
    split = usage.get("cache_creation")
    write_1h = min(_count(split.get("ephemeral_1h_input_tokens")), counts[3]) if isinstance(split, dict) else None
    speed = usage.get("speed") if usage.get("speed") in ("standard", "fast") else None
    geo = usage.get("inference_geo") if isinstance(usage.get("inference_geo"), str) else None
    return (msg["id"], ts, _local_day(ts), session, cwd, model, *counts, write_1h, speed, geo and geo[:20])


UPSERT = """INSERT INTO usage_messages (message_id, ts, day, session, cwd, model, input, output, cache_read, cache_write,
    cache_write_1h, speed, geo, latency_ms)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(message_id) DO UPDATE SET
    input = max(input, excluded.input), output = max(output, excluded.output),
    cache_read = max(cache_read, excluded.cache_read), cache_write = max(cache_write, excluded.cache_write),
    cache_write_1h = max(coalesce(cache_write_1h, 0), coalesce(excluded.cache_write_1h, cache_write_1h, 0)),
    speed = coalesce(excluded.speed, speed), geo = coalesce(excluded.geo, geo),
    latency_ms = CASE WHEN excluded.latency_ms IS NULL THEN latency_ms WHEN latency_ms IS NULL THEN excluded.latency_ms
                      ELSE max(latency_ms, excluded.latency_ms) END"""


def _epoch_ms(ts: str) -> int | None:
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def request_time(raw: bytes) -> tuple[str, str] | None:
    """("api-request" | "user", timestamp) for a line that starts a request, else None. Only those two
    fields are kept from the decoded line."""
    if b'"type":"api-request"' not in raw and b'"type":"user"' not in raw and b'"type": "user"' not in raw \
            and b'"type": "api-request"' not in raw:
        return None
    try:
        line = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(line, dict) or line.get("type") not in ("api-request", "user"):
        return None
    ts = line.get("timestamp")
    return (line["type"], ts) if isinstance(ts, str) and ts else None


class _Latency:
    """Per file being read: when the pending request was sent, and each response's start."""

    def __init__(self) -> None:
        self.pending: int | None = None
        self.api_requests = False  # this file writes api-request lines: user lines no longer start a request
        self.starts: dict[str, int | None] = {}

    def see(self, raw: bytes) -> None:
        found = request_time(raw)
        if not found:
            return
        kind, ts = found
        if kind == "api-request":
            self.api_requests = True
        elif self.api_requests:
            return
        self.pending = _epoch_ms(ts)

    def of(self, message_id: str, ts: str) -> int | None:
        if message_id not in self.starts:
            self.starts[message_id], self.pending = self.pending, None
        start, end = self.starts[message_id], _epoch_ms(ts)
        if start is None or end is None or not 0 <= end - start <= MAX_LATENCY_MS:
            return None
        return end - start


def _read_file(conn: sqlite3.Connection, path: Path, offset: int) -> int:
    """Read complete lines from `offset`; return the new offset (just after the last newline). A last
    line still being written is left for the next scan."""
    rows = []
    latency = _Latency()
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
                    rows.append((*row, latency.of(row[0], row[1])))
                else:
                    latency.see(raw)
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


def _is_home(settings: Settings, proj: str, home: str, info: dict) -> bool:
    """True when a task belongs to this repo's dev3 project (where the assistants and workers live)."""
    if settings.assistant_project:
        return str(info.get("project_id") or "").startswith(settings.assistant_project)
    return proj == home


def _tasks(conn: sqlite3.Connection) -> dict[str, dict]:
    return {r["task_id"][:8]: dict(r) for r in conn.execute("SELECT * FROM usage_tasks")}


def price(settings: Settings, model: str | None, long_prompt: bool = False, speed: str | None = None,
          geo: str | None = None) -> price_list.Price | None:
    """Dollars per million tokens (input, output, cache read, cache write 5m, cache write 1h) for one
    response, or None when the model has no price. Settings win over the built-in list."""
    base = next((p for prefix, p in settings.prices if model and model.startswith(prefix)), None)
    key = price_list.base_id(model)
    if base is None and key:
        base = (price_list.LONG_PROMPT_PRICES.get(key) if long_prompt else None) or price_list.DEFAULTS[key]
    if base is None:
        return None
    p = list(base)
    if speed == "fast" and key in price_list.FAST and base[0]:
        fast_in, fast_out = price_list.FAST[key]
        scale = fast_in / base[0]
        p = [fast_in, fast_out, p[2] * scale, p[3] * scale, p[4] * scale]
    if geo == "us":
        p = [v * price_list.US_ONLY for v in p]
    return tuple(p)


def cost(counts: tuple[int, int, int, int, int], p: price_list.Price) -> tuple[float, float, float, float]:
    """(cache read, cache write, output, input) dollars for (input, output, cache_read, cache_write,
    cache_write_1h) tokens. The 5-minute write is the write minus its 1-hour part."""
    i, o, cr, cw, cw1h = counts
    m = 1_000_000
    return (cr * p[2] / m, ((cw - cw1h) * p[3] + cw1h * p[4]) / m, o * p[1] / m, i * p[0] / m)


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
        mine = seq is not None and _is_home(settings, proj, home, info)
        if mine and seq == settings.assistant_task:
            return "Applications Assistant"
        if mine and seq == settings.prep_assistant_task:
            return "Learn Assistant"
        if mine and seq in worker_tasks(settings, "research"):
            return "Research worker"
        if mine and seq in worker_tasks(settings, "comms"):
            return "Comms worker"
        if info.get("task_type") == "coordinator" or (mine and seq == settings.coordinator_task):
            return "Coordinator"
        return "Build tasks"

    per_day = {(start + timedelta(days=i)).isoformat(): [0, 0, 0, 0] for i in range(days)}
    keys: dict[tuple[str, str | None], dict] = {}
    roles = {r: 0 for r in ROLES}
    role_cost = {r: 0.0 for r in ROLES}
    sessions: dict[str, dict] = {}
    totals = [0, 0, 0, 0]
    turns = 0
    unpriced = 0
    # Money: fixed windows (today, 7 and MONEY_DAYS days) whatever `days` is.
    m_start = end - timedelta(days=MONEY_DAYS - 1)
    week = (end - timedelta(days=6)).isoformat()
    money_day = {(m_start + timedelta(days=i)).isoformat(): {r: 0.0 for r in ROLES} for i in range(MONEY_DAYS)}
    windows = {w: {"cost": 0.0, "tokens": 0, "unpriced": 0} for w in ("today", "week", "month")}
    kinds = [0.0, 0.0, 0.0, 0.0]
    kind_tokens = [0, 0, 0, 0]
    unknown_models: dict[str, int] = {}
    models: dict[str, dict] = {}
    rows = conn.execute(
        "SELECT day, cwd, session, model, speed, geo, input + cache_read + cache_write > ?, COUNT(*), SUM(input), "
        "SUM(output), SUM(cache_read), SUM(cache_write), SUM(coalesce(cache_write_1h, cache_write)) "
        "FROM usage_messages WHERE day BETWEEN ? AND ? GROUP BY 1, 2, 3, 4, 5, 6, 7",
        (price_list.LONG_PROMPT, min(start, m_start).isoformat(), end.isoformat()))
    for day, cwd, session, model, speed, geo, long_prompt, n, i, o, cr, cw, cw1h in rows:
        proj, task = places.get(cwd) or place(cwd, projs)
        if project and proj != project:
            continue
        counts = (i, o, cr, cw)
        tokens = sum(counts)
        p = price(settings, model, bool(long_prompt), speed, geo)
        split = cost((i, o, cr, cw, cw1h), p) if p else None
        dollars = sum(split) if split else 0.0
        r_name = role(proj, task)
        if day >= m_start.isoformat():
            for w, since in (("today", today), ("week", week), ("month", m_start.isoformat())):
                if day >= since:
                    windows[w]["tokens"] += tokens
                    windows[w]["cost"] += dollars
                    windows[w]["unpriced"] += 0 if p else tokens
            money_day[day][r_name] += dollars
            if split:
                kinds = [a + b for a, b in zip(kinds, split)]
                kind_tokens = [a + b for a, b in zip(kind_tokens, (cr, cw, o, i))]
            else:
                unknown_models[model or "unknown"] = unknown_models.get(model or "unknown", 0) + tokens
        if day < start.isoformat():
            continue
        for k, v in enumerate(counts):
            per_day[day][k] += v
            totals[k] += v
        turns += n
        key = (proj, task)
        entry = keys.setdefault(key, {"project": proj, "task": task, "turns": 0, "tokens": 0, "cost": 0.0,
                                      "unpriced": 0, "cwds": set()})
        entry["turns"] += n
        entry["tokens"] += tokens
        entry["cost"] += dollars
        entry["unpriced"] += 0 if p else tokens
        entry["cwds"].add(cwd)
        roles[r_name] += tokens
        role_cost[r_name] += dollars
        mod = models.setdefault(model or "unknown", {"model": model or "unknown", "turns": 0, "cost": 0.0, "unpriced": 0,
                                                     **{c: 0 for c in COUNTS}})
        mod["turns"] += n
        mod["cost"] += dollars
        mod["unpriced"] += 0 if p else tokens
        for c, v in zip(COUNTS, counts):
            mod[c] += v
        unpriced += 0 if p else tokens
        s = sessions.setdefault(session or "", {"key": key, "tokens": 0, "cache_write": 0})
        s["tokens"] += tokens
        s["cache_write"] += cw

    total = sum(totals)

    # Response time per model and per role (responses with a known latency_ms only).
    waits: dict[str, list[int]] = {}
    role_waits: dict[str, list[int]] = {}
    for cwd, model, ms in conn.execute("SELECT cwd, model, latency_ms FROM usage_messages WHERE day BETWEEN ? AND ? "
                                       "AND latency_ms IS NOT NULL", (start.isoformat(), end.isoformat())):
        proj, task = places.get(cwd) or place(cwd, projs)
        if project and proj != project:
            continue
        waits.setdefault(model or "unknown", []).append(ms)
        role_waits.setdefault(role(proj, task), []).append(ms)
    by_model = sorted(({**m, "tokens": sum(m[c] for c in COUNTS), **spread(waits.get(name, []))}
                       for name, m in models.items()), key=lambda m: (-m["cost"], -m["tokens"]))
    fed = totals[0] + totals[2] + totals[3]

    def label(key: tuple[str, str | None]) -> dict:
        proj, task = key
        info = tasks.get(task) if task else None
        if not task:
            return {"ref": proj, "title": "main checkout"}
        if info and info.get("seq") is not None:
            ref = f"seq:{info['seq']}" if proj == home else f"seq:{proj}/{info['seq']}"
            return {"ref": ref, "title": info.get("title") or ""}
        return {"ref": task, "title": f"{proj} task"}

    top = sorted(keys.values(), key=lambda e: (-e["cost"], -e["tokens"]))[:10]
    top_rows = [{**label((e["project"], e["task"])), "turns": e["turns"], "tokens": e["tokens"],
                 "per_turn": e["tokens"] // max(e["turns"], 1), "role": role(e["project"], e["task"]),
                 "cost": e["cost"], "unpriced": e["unpriced"]} for e in top]

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
        "roles": sorted(((r, n, role_cost[r]) for r, n in roles.items() if n), key=lambda x: (-x[2], -x[1])),
        "cost": sum(role_cost.values()),
        "cache_hit": totals[2] / fed if fed else None,
        "models": by_model,
        "role_latency": {r: spread(v) for r, v in role_waits.items()},
        "context": context,
        "save": save,
        "unpriced": unpriced,
        "money": {**windows, "days": [(d, [v[r] for r in ROLES]) for d, v in money_day.items()],
                  "kinds": list(zip(COST_KINDS, kinds, kind_tokens)), "unknown_models": sorted(unknown_models.items())},
        "projects": names, "home": home,
    }


def spread(values: list[float]) -> dict:
    """{"n", "median", "p90"} of a list (None for an empty one): the typical and the slow case."""
    v = sorted(values)
    if not v:
        return {"n": 0, "median": None, "p90": None}
    mid = len(v) // 2
    median = v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2
    return {"n": len(v), "median": median, "p90": v[min(len(v) - 1, int(len(v) * .9))]}


# --- cost per job (runs) ------------------------------------------------------------------------

def worker_cwds(conn: sqlite3.Connection, settings: Settings, seqs: set[int]) -> dict[int, list[str]]:
    """The working directories of each worker seq's dev3 task in this repo's project."""
    projs = projects(conn)
    tasks = _tasks(conn)
    home = home_project(projs, settings)
    out: dict[int, list[str]] = {}
    for cwd, in conn.execute("SELECT DISTINCT cwd FROM usage_messages"):
        proj, task = place(cwd, projs)
        info = tasks.get(task) if task else None
        if info and info.get("seq") in seqs and _is_home(settings, proj, home, info):
            out.setdefault(int(info["seq"]), []).append(cwd)
    return out


def run_costs(conn: sqlite3.Connection, settings: Settings, runs: list[dict]) -> dict[int, dict]:
    """{run id: {"cost", "tokens", "turns", "unpriced"}} for finished runs with a worker: the responses of the
    worker's task between started_at and finished_at (to the second). A response inside several runs of
    one worker is split evenly between them."""
    out = {r["id"]: {"cost": 0.0, "tokens": 0, "turns": 0.0, "unpriced": 0} for r in runs}
    done = [r for r in runs if r.get("worker") and r.get("finished_at") and r.get("started_at")]
    cwds = worker_cwds(conn, settings, {int(r["worker"]) for r in done})
    for seq, dirs in cwds.items():
        mine = [(r["started_at"][:19], r["finished_at"][:19], r["id"]) for r in done if int(r["worker"]) == seq]
        low, high = min(a for a, _, _ in mine), max(b for _, b, _ in mine)
        marks = ",".join("?" * len(dirs))
        rows = conn.execute(
            f"SELECT substr(ts, 1, 19), model, speed, geo, input, output, cache_read, cache_write, "
            f"coalesce(cache_write_1h, cache_write) FROM usage_messages WHERE cwd IN ({marks}) "
            f"AND substr(ts, 1, 19) BETWEEN ? AND ?", (*dirs, low, high))
        for ts, model, speed, geo, i, o, cr, cw, cw1h in rows:
            inside = [rid for a, b, rid in mine if a <= ts <= b]
            if not inside:
                continue
            p = price(settings, model, i + cr + cw > price_list.LONG_PROMPT, speed, geo)
            dollars = sum(cost((i, o, cr, cw, cw1h), p)) if p else 0.0
            share = 1 / len(inside)
            for rid in inside:
                o_ = out[rid]
                o_["cost"] += dollars * share
                o_["tokens"] += round((i + o + cr + cw) * share)
                o_["turns"] += share
                o_["unpriced"] += 0 if p else round((i + o + cr + cw) * share)
    return out


def _seconds(start: str | None, end: str | None) -> float | None:
    a, b = _epoch_ms(start or ""), _epoch_ms(end or "")
    return None if a is None or b is None else max(0, b - a) / 1000


def jobs(conn: sqlite3.Connection, settings: Settings, today: str, days: int, top: int = 10) -> dict:
    """Cost and time per kind of job over the runs finished in the last `days` days: per kind the jobs done,
    the cost of all its runs (any result), and per done job the median and 90th-percentile time, the median
    cost and tokens; the `top` most expensive runs; and the median time of all done jobs."""
    since = (date.fromisoformat(today) - timedelta(days=days - 1)).isoformat()
    rows = [dict(r) for r in conn.execute(
        "SELECT r.*, a.company FROM runs r LEFT JOIN applications a ON a.id = r.application_id "
        "WHERE r.result IS NOT NULL AND substr(r.finished_at, 1, 10) >= ?", (since,))]
    costs = run_costs(conn, settings, rows)
    kinds: dict[str, dict] = {}
    for r in rows:
        c = costs[r["id"]]
        r.update(cost=c["cost"], tokens=c["tokens"], turns=c["turns"], unpriced=c["unpriced"],
                 seconds=_seconds(r["started_at"], r["finished_at"]))
        k = kinds.setdefault(r["kind"], {"kind": r["kind"], "runs": 0, "done": [], "cost": 0.0, "tokens": 0})
        k["runs"] += 1
        k["cost"] += c["cost"]
        k["tokens"] += c["tokens"]
        if r["result"] == "done":
            k["done"].append(r)
    table = []
    for k in kinds.values():
        done = k.pop("done")
        times = spread([r["seconds"] for r in done if r["seconds"] is not None])
        table.append({**k, "jobs": len(done), "time": times["median"], "slow": times["p90"],
                      "per_job": spread([r["cost"] for r in done])["median"],
                      "tokens_per_job": spread([r["tokens"] for r in done])["median"]})
    table.sort(key=lambda k: (-k["cost"], -k["runs"]))
    done_times = [r["seconds"] for r in rows if r["result"] == "done" and r["seconds"] is not None]
    return {"kinds": table, "top": sorted(rows, key=lambda r: (-r["cost"], -r["tokens"]))[:top],
            "time": spread(done_times), "runs": len(rows)}


def _short(n: float) -> str:
    """1234 -> 1.2k, 3_400_000 -> 3.4M, 1.2e9 -> 1.2B."""
    for div, unit in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if n >= div:
            v = n / div
            return f"{v:.1f}".rstrip("0").rstrip(".") + unit if v < 100 else f"{v:.0f}{unit}"
    return str(int(n))


short = _short
