"""What each standing agent (worker) is doing, for the dashboard's Workers sidebar, its popover and the
Run history page (/runs).

A worker is a dev3 task set in the settings: each `[workers] research` seq ("Research"), assistant_task
("Applications"), each `[workers] comms` seq ("Communication") and prep_assistant_task ("Learn"). A seq
listed twice shows once, under its first role. Its state comes from prep.db only (db.runs and the queues):

- working: it holds a claim (an open run); idle: it holds none.
- stuck: a claim past its lease (a message with no reply in db.MESSAGE_CLAIM_MINUTES, a request or brief
  after db.CLAIM_HOURS), or its latest run, within BLOCKED_HOURS, ended blocked.

Every item is described by its kind, the application's company (or "job offer", "My CV", "all jobs",
"card N") and times: never a message's text, a reason is the worker's own short line, and pay is never read.
"""

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from prep import db
from prep.config import Settings, worker_tasks

KIND_LABELS = {**db.REQUEST_LABELS, "brief": "Job brief", "comms": "Draft", "ask": "Question",
               "learn": "Learn question"}
# Long items: a toast says when one finishes while the page is open.
TOAST_KINDS = ("research", "cv_fit", "tailor", "cv_eval", "brief", "comms")
RESULT_LABELS = {"done": "done", "blocked": "blocked", "stopped": "stopped", "lapsed": "lapsed"}
BLOCKED_HOURS = 12  # a blocked last run keeps the worker amber this long (until it runs something else)
LAST_RUNS = 5
RESEARCH_KINDS = (*db.REQUEST_KINDS, "brief")


def configured(settings: Settings) -> list[dict]:
    """The workers to show, in sidebar order: key, name, seq, role (research | applications | comms | learn)."""
    research = worker_tasks(settings, "research")
    comms = worker_tasks(settings, "comms")
    found: list[dict] = []
    seen: set[int] = set()

    def add(seq: int | None, name: str, role: str) -> None:
        if seq and seq not in seen:
            seen.add(seq)
            found.append({"key": f"{role}-{seq}", "name": name, "seq": int(seq), "role": role})

    for i, seq in enumerate(research):
        add(seq, "Research" if len(research) == 1 else f"Research {i + 1}", "research")
    add(settings.assistant_task, "Applications", "applications")
    for i, seq in enumerate(comms):
        add(seq, "Communication" if len(comms) == 1 else f"Communication {i + 1}", "comms")
    add(settings.prep_assistant_task, "Learn", "learn")
    return found


def owner(settings: Settings, item: dict) -> int | None:
    """The worker seq whose queue a waiting item is in: its assigned worker when that one is on its route,
    else the route's first worker, else the Applications Assistant (Learn: the Learn Assistant)."""
    kind = item["kind"]
    if kind == "learn":
        return settings.prep_assistant_task
    route = "research" if kind in RESEARCH_KINDS else "comms" if kind == "comms" else None
    seqs = worker_tasks(settings, route) if route else ()
    if seqs:
        return item.get("worker") if item.get("worker") in seqs else seqs[0]
    return settings.assistant_task


def parse(stamp: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")) if stamp else None
    except ValueError:
        return None


def span(start: str | None, end: str | None = None, now: datetime | None = None) -> str:
    """A short duration: "45s", "12m", "1h 5m", "2d 3h"; "" when unknown."""
    a = parse(start)
    b = parse(end) if end else (now or datetime.now(timezone.utc))
    if a is None or b is None:
        return ""
    secs = max(0, int((b - a).total_seconds()))
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hours, mins = divmod(mins, 60)
    if hours < 24:
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def about(item: dict, job_names: dict[str, str]) -> str:
    """What an item is about, in a few words (database values, escaped by the caller)."""
    if item.get("company"):
        return item["company"]
    if item.get("getjo_job_id"):
        return job_names.get(item["getjo_job_id"]) or "job offer"
    if item.get("kind") == "learn":
        return f"card {item['card_id']}" if item.get("card_id") else "Learn"
    if item.get("topic") == "cv":
        return "My CV"
    if item.get("topic") == db.COMPARE:
        return "several jobs"
    return "all jobs"


def href(item: dict) -> str:
    """Where Open job goes: the application's panel, the job offer, the Learn card, My CV or the box."""
    if item.get("application_id"):
        return f"/?app={int(item['application_id'])}"
    if item.get("getjo_job_id"):
        return "/?" + urlencode([("view", "offers"), ("job", item["getjo_job_id"])])
    if item.get("kind") == "learn":
        return f"/learn?card={int(item['card_id'])}" if item.get("card_id") else "/learn"
    if item.get("topic") == "cv":
        return "/?cv=1"
    return "/"


def _lease_lapsed(run: dict, now: datetime) -> bool:
    started = parse(run["started_at"])
    if started is None:
        return False
    lease = (timedelta(minutes=db.MESSAGE_CLAIM_MINUTES) if run["source"] == "message"
             else timedelta(hours=db.CLAIM_HOURS))
    return now - started > lease


def _describe(item: dict, job_names: dict[str, str], now: datetime) -> dict:
    """The fields the sidebar and the runs page show for a run or a waiting item."""
    out = {"kind": item["kind"], "label": KIND_LABELS.get(item["kind"], item["kind"]),
           "about": about(item, job_names), "role": item.get("role"), "href": href(item),
           "queued_at": item.get("queued_at")}
    if "started_at" in item:
        out.update(id=item.get("id"), started_at=item["started_at"], finished_at=item.get("finished_at"),
                   result=item.get("result"), reason=item.get("reason"), worker=item.get("worker"),
                   elapsed=span(item["started_at"], item.get("finished_at"), now))
    return out


def job_ids(conn: sqlite3.Connection) -> set[str]:
    """getjo job ids of untracked jobs that runs or the queue mention (their names come from getjo)."""
    rows = conn.execute("SELECT getjo_job_id FROM runs WHERE application_id IS NULL AND getjo_job_id IS NOT NULL "
                        "UNION SELECT getjo_job_id FROM brief_requests WHERE status IN ('pending', 'claimed') "
                        "UNION SELECT getjo_job_id FROM agent_messages WHERE application_id IS NULL "
                        "AND getjo_job_id IS NOT NULL AND status IN ('pending', 'claimed')")
    return {r[0] for r in rows}


def state(settings: Settings, conn: sqlite3.Connection, job_names: Callable[[set[str]], dict[str, str]] | None = None,
          now: datetime | None = None) -> dict:
    """{"workers": [...], "busy": n, "waiting": n}: per configured worker its key, name, seq, state
    (working | idle | stuck), line (the sidebar's short line), since (how long the current item has run), now (its open runs), queued (its waiting
    items, oldest first) and last (its latest finished runs). Closes finished runs first (db.sync_runs)."""
    now = now or datetime.now(timezone.utc)
    db.sync_runs(conn)
    rows = configured(settings)
    if not rows:
        return {"workers": [], "busy": 0, "waiting": 0}
    names = job_names(job_ids(conn)) if job_names else {}
    open_runs = db.open_runs(conn)
    waiting = db.waiting_items(conn)
    cutoff = (now - timedelta(hours=BLOCKED_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = []
    for w in rows:
        mine = [r for r in open_runs if r["worker"] == w["seq"]]
        queued = [_describe(i, names, now) for i in waiting if owner(settings, i) == w["seq"]]
        last = [_describe(r, names, now) for r in db.runs(conn, worker=w["seq"], limit=LAST_RUNS)]
        current = [_describe(r, names, now) for r in mine]
        lapsed = [r for r in mine if _lease_lapsed(r, now)]
        blocked = not mine and last and last[0]["result"] == "blocked" and (last[0]["finished_at"] or "") >= cutoff
        if lapsed:
            state_ = "stuck"
            line = (f"no reply in {db.MESSAGE_CLAIM_MINUTES} min" if lapsed[0]["source"] == "message"
                    else f"{KIND_LABELS.get(lapsed[0]['kind'], '')} · no result in {db.CLAIM_HOURS} h")
        elif mine:
            state_ = "working"
            first = current[0]
            line = f"{first['label']} · {first['about']}"
        elif blocked:
            state_ = "stuck"
            line = f"blocked: {last[0]['reason'] or 'no reason given'}"
        else:
            state_ = "idle"
            line = f"idle · {len(queued)} waiting" if queued else "idle"
        since = current[0]["elapsed"] if state_ == "working" else ""
        out.append({**w, "state": state_, "line": line, "since": since, "now": current, "queued": queued, "last": last})
    busy = sum(1 for w in out if w["state"] == "working")
    return {"workers": out, "busy": busy, "waiting": sum(len(w["queued"]) for w in out)}


def finished(settings: Settings, conn: sqlite3.Connection, since: str,
             job_names: Callable[[set[str]], dict[str, str]] | None = None) -> list[dict]:
    """Long items done at or after `since` (for the toast): id, text ("Tailored CV for Acme is ready"), href."""
    db.sync_runs(conn)
    names = job_names(job_ids(conn)) if job_names else {}
    found = []
    for r in reversed(db.runs(conn, result="done", finished_since=since, limit=20)):
        if r["kind"] not in TOAST_KINDS:
            continue
        label = "Draft" if r["kind"] == "comms" else KIND_LABELS[r["kind"]]
        found.append({"id": r["id"], "text": f"{label} for {about(r, names)} is ready", "href": href(r)})
    return found
