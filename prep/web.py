"""Local dashboard over prep.db: Dashboard, Job Offers and Applications pages, plus a read-only
view of the agent's research and CV fit files.

    python -m prep.web [--port 8765]

Standard library only (http.server): server-rendered HTML, one stylesheet and one small
script from prep/static/, no external requests. It binds to 127.0.0.1 only and refuses any
other address. The script is an enhancement (sorting, filtering, forms without a reload,
toasts with undo); every page and form also works without it.

Every request opens a fresh prep.db connection (and a fresh read-only getjo connection
where needed), so writes made by the skills show up on the next page load and writes made
here are seen by the skills at once.

`handle()` does routing and rendering without sockets and returns (status, headers, body),
so tests call it directly. Every value from a database is HTML-escaped. The pay column is
never read for display.

The dashboard never runs the agent: Research / CV fit buttons and Interested only queue
requests (db.agent_requests) for the /research skill, and the panel's Agent thread only stores
the user's message (db.application_messages) for the /ask skill. When assistant_task is set,
new work is also sent to that dev3 agent (prep.assistant), in the background, by id only.
"""

import argparse
import html
import ipaddress
import json
import logging
import re
import sqlite3
import sys
from collections.abc import Callable, Mapping
from contextlib import closing
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit

from prep import assistant, charts, db, getjo, research, stats
from prep.config import ConfigError, Settings, load_settings

log = logging.getLogger("prep.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
LOOPBACK_NAMES = ("localhost", "127.0.0.1")
MAX_BODY = 256 * 1024  # a 20,000-character pasted email URL-encodes to up to ~180 KB
RECENT_DONE = 10
MAX_COMMENT = 1000
# Applications: the funnel order (Next stage follows it) and the closed outcomes.
BOARD_STAGES = ("to_apply", "applied", "recruiter_call", "technical", "system_design", "final", "offer")
CLOSED_LANE = ("rejected", "no_response", "withdrawn")
# Board columns: stored statuses stay as they are; the four interview stages share one column.
BOARD_COLUMNS = {
    "interested": ("to_apply",), "applied": ("applied",), "interviewing": db.INTERVIEW_STAGES,
    "offer": ("offer",), "closed": CLOSED_LANE,
}
COLUMN_LABELS = {"interested": "Interested", "applied": "Applied", "interviewing": "Interviewing",
                 "offer": "Offer", "closed": "Closed"}
STEP_LABELS = {"recruiter_call": "Recruiter call", "technical": "Coding", "system_design": "System design",
               "final": "Company interview"}
VIEWS = ("board", "list")
CURRENT = ' aria-current="true"'

Response = tuple[int, list[tuple[str, str]], bytes]

# (href, name, icon) — icons are inline SVG bodies, so nothing is fetched.
PAGES = (
    ("/", "Dashboard", '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/>'
                       '<rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>'),
    ("/offers", "Job Offers", '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>'),
    ("/applications", "Applications", '<rect x="3" y="3" width="5" height="18" rx="1.5"/><rect x="10" y="3" width="5" height="12" rx="1.5"/>'
                                      '<rect x="17" y="3" width="4" height="8" rx="1.5"/>'),
)
EMPTY_ICON = ('<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.5 5.1 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6'
              'l-3.5-6.9A2 2 0 0 0 16.8 4H7.2a2 2 0 0 0-1.7 1.1z"/>')

# Fixed messages shown after an action; the URL carries only the key, never free text.
MESSAGES = {
    "applied": "Recorded as applied. Its panel shows the first tasks.",
    "interested": "Interested · research queued. The agent researches the company and checks the CV fit.",
    "uninterested": "Interested undone; the job is new again.",
    "applied_today": "Marked as applied today. Chase if no reply is due in 7 days.",
    "moved": "Moved. The stage's tasks are in its panel.",
    "commented": "Comment added.",
    "skipped": "Skipped. It no longer appears in new job offers.",
    "unskipped": "Skip undone; the job is new again.",
    "task_added": "Task added.",
    "task_done": "Task marked done.",
    "task_reopened": "Task reopened.",
    "research_queued": "Queued for the agent. It runs in the next Claude session (/research).",
    "tier_set": "Tier saved.",
    "message_sent": "Sent to the agent. Its reply shows in the thread.",
}

SECURITY_HEADERS = [
    ("Content-Security-Policy",
     "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self'; "
     "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"),
    ("X-Content-Type-Options", "nosniff"),
    # same-origin, not no-referrer: under no-referrer browsers send "Origin: null" on plain form
    # posts, which origin_allowed() refuses. External links carry rel="noreferrer" anyway.
    ("Referrer-Policy", "same-origin"),
    ("Cache-Control", "no-store"),
]

COMMENT_ICON = '<path d="M21 12a8 8 0 0 1-11.6 7.1L3 21l1.9-6.4A8 8 0 1 1 21 12z"/>'

# Badge tones: a few semantic ones plus the shade steps of the page's hue (charts.SHADES,
# s1 dark to s10 light); anything else falls back to "neutral". Each page has one hue.
TONES = ("neutral", "accent", "info", "success", "warning", "danger", *charts.SHADES)
# One theme per page (defined in style.css). Spare themes for new pages: eucalyptus,
# slate-indigo, dusty-violet, mauve-rose, terracotta.
PAGE_THEMES = {"/": "sage", "/offers": "dusty-blue", "/applications": "ochre"}
# Closed outcomes take s1-s3 (neutral greys on the Dashboard), then to_apply and the funnel
# from dark to light (offer is the lightest).
STATUS_SHADES = {
    "rejected": "s1", "withdrawn": "s2", "no_response": "s3", "to_apply": "s4", "applied": "s5",
    "recruiter_call": "s6", "technical": "s7", "system_design": "s8", "final": "s9", "offer": "s10",
}
TIER_SHADES = {"A": "s9", "B": "s7", "C": "s5", "unset": "s3"}
KIND_SHADES = {"apply": "s9", "prep": "s7", "drill": "s5", "admin": "s3"}


class BadRequest(Exception):
    """A form value is missing or invalid; the message is shown on a 400 page."""


class NotFound(Exception):
    """A page asked for something that is not there (or must not be served): plain 404."""


def h(value: object) -> str:
    """HTML-escape any value for text or a quoted attribute. None becomes an empty string."""
    return "" if value is None else html.escape(str(value), quote=True)


# --- request checks -------------------------------------------------------------

def host_allowed(host: str | None) -> bool:
    """True for localhost / 127.0.0.1 with an optional port. Blocks DNS rebinding."""
    if not host:
        return False
    name, _, port = host.partition(":")
    return name.lower() in LOOPBACK_NAMES and (port == "" or port.isdigit())


def origin_allowed(headers: Mapping[str, str]) -> bool:
    """For POSTs: reject a cross-origin Origin (including "null") or a cross-site fetch."""
    if headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        return False
    origin = headers.get("origin")
    if origin is None:
        return True  # non-browser clients (curl) send none; browsers always send it on POST
    return origin.lower() == f"http://{headers.get('host', '').lower()}"


# --- routing ------------------------------------------------------------------------

def wants_json(headers: Mapping[str, str]) -> bool:
    """True when the page script sent the form (it asks for JSON); plain form posts get a redirect."""
    return "application/json" in headers.get("accept", "")


def handle(settings: Settings, method: str, target: str, headers: Mapping[str, str],
           body: bytes = b"", today: str | None = None) -> Response:
    """Route one request. `headers` may use any case; `today` is for tests."""
    headers = {k.lower(): v for k, v in headers.items()}
    if not host_allowed(headers.get("host")):
        return _text(400, "Bad Host header: the dashboard only answers to localhost.")
    url = urlsplit(target)
    today = today or db.today_iso()
    try:
        if method == "GET":
            return _get(settings, url.path, parse_qs(url.query), today)
        if method == "POST":
            if not origin_allowed(headers):
                return _text(403, "Cross-origin request refused.")
            return _post(settings, url.path, _form(body), today, wants_json(headers))
        return _text(405, "Method not allowed.")
    except BadRequest as e:
        if method == "POST" and wants_json(headers):
            return _json(400, {"ok": False, "message": str(e)})
        content = _banner(str(e), "warning") + '<p><a class="btn btn-secondary" href="/">Back to the dashboard</a></p>'
        return _page(400, "Not saved", "The form was not saved.", content, "")


def _get(settings: Settings, path: str, query: dict, today: str) -> Response:
    if path in STATIC_FILES:
        name, content_type = STATIC_FILES[path]
        return 200, [("Content-Type", content_type), *SECURITY_HEADERS], (STATIC_DIR / name).read_bytes()
    if path == "/progress":  # merged into the dashboard; keeps old bookmarks working
        return 303, [("Location", "/")], b""
    renamed = {"/matches": "/offers", "/tasks": "/applications"}  # old bookmarks (and their query) keep working
    if path in renamed:
        target = renamed[path] + (f"?{urlencode(query, doseq=True)}" if query else "")
        return 303, [("Location", target)], b""
    if path == "/applications/thread":
        return thread_json(settings, query, today)
    pages = {
        "/": dashboard_page,
        "/offers": lambda s, c, t: offers_page(s, c, t, query),
        "/offers/skip": lambda s, c, t: skip_page(s, c, t, query),
        "/applications": lambda s, c, t: applications_page(s, c, t, query),
        "/research/file": lambda s, c, t: research_file_page(s, c, t, query),
    }
    if path not in pages:
        return _text(404, "Not found.")
    message = MESSAGES.get((query.get("msg") or [""])[0])
    with closing(_connect(settings, today)) as conn:
        try:
            title, subtitle, content = pages[path](settings, conn, today)
        except NotFound:
            return _text(404, "Not found.")
    notice = f'<div class="notice" role="status" data-flash>{h(message)}</div>' if message else ""
    active = {"/offers/skip": "/offers", "/research/file": "/applications"}.get(path, path)
    return _page(200, title, subtitle, notice + content, active)


def _post(settings: Settings, path: str, form: dict[str, str], today: str, as_json: bool) -> Response:
    if path not in ACTIONS:
        return _text(404, "Not found.")
    action, page, msg = ACTIONS[path]
    with closing(_connect(settings, today)) as conn:
        mark = db.last_request_id(conn)
        try:
            extra = action(settings, conn, form, today) or {}
        except (ValueError, LookupError) as e:
            raise BadRequest(str(e)) from e
        # Interested, Move to Interested or a Research / CV fit click may have queued agent work.
        assistant.notify_in_background(settings, db.requests_since(conn, mark))
    if as_json:
        return _json(200, {"ok": True, "message": MESSAGES[msg], **extra})
    if "redirect" in extra:  # built by the action from database ids, never from the form
        back, target = urlsplit(form.get("return") or ""), urlsplit(extra["redirect"])
        if back.path == target.path and parse_qs(back.query).get("app") == parse_qs(target.query).get("app"):
            # the side panel's own URL with its view and stage filter, re-encoded by _return_to
            return 303, [("Location", _return_to(form["return"], back.path, msg))], b""
        sep = "&" if "?" in extra["redirect"] else "?"
        return 303, [("Location", f"{extra['redirect']}{sep}{urlencode([('msg', msg)])}")], b""
    return 303, [("Location", _return_to(form.get("return"), page, msg))], b""


def _connect(settings: Settings, today: str) -> sqlite3.Connection:
    """Open prep.db for one request, after moving silent applications to No response."""
    conn = db.connect(settings.prep_db)
    moved = db.auto_no_response(conn, settings.no_response_days, today)
    if moved:
        log.info("%d application(s) moved to no_response (no reply in %d days)", len(moved), settings.no_response_days)
    return conn


def _return_to(value: str | None, page: str, msg: str = "") -> str:
    """Where a plain form post redirects: `page`, keeping the query of a `return` field that
    points at the same page (so filters survive), plus the message key if any. The query is
    parsed and re-encoded, so nothing raw from the form reaches the Location header."""
    url = urlsplit(value or "")
    pairs = []
    if not url.scheme and not url.netloc and url.path == page:
        pairs = [(k, v) for k, v in parse_qsl(url.query) if k != "msg"]
    query = urlencode([*pairs, *([("msg", msg)] if msg else [])])
    return f"{page}?{query}" if query else page


def _form(body: bytes) -> dict[str, str]:
    if len(body) > MAX_BODY:
        raise BadRequest("Form too large.")
    try:
        fields = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError as e:
        raise BadRequest("Form is not valid UTF-8.") from e
    return {k: v[0].strip() for k, v in fields.items()}


def _optional(form: dict[str, str], key: str) -> str | None:
    return form.get(key) or None


def _int_field(form: dict[str, str], key: str) -> int | None:
    value = form.get(key)
    if not value:
        return None
    if not value.isdigit():
        raise BadRequest(f"{key} must be a number.")
    return int(value)


# --- actions (POST) -------------------------------------------------------------------
# Each returns None or a dict merged into the JSON reply: "html" (a new table row for the page
# script) or "redirect" (a local URL with a query, where both kinds of request go next).

def _getjo_job(settings: Settings, form: dict[str, str]) -> dict:
    """The getjo job named by the form's job_id, read from getjo's database (read-only)."""
    job_id = form.get("job_id")
    if not job_id:
        raise BadRequest("job_id is missing.")
    try:
        with closing(getjo.connect_ro(settings.getjo_db)) as gconn:
            job = getjo.get_job(gconn, job_id)
    except (getjo.GetjoUnavailable, sqlite3.Error) as e:
        raise BadRequest(f"getjo database unavailable: {e}") from e
    if job is None:
        raise BadRequest("That getjo job no longer exists.")
    return job


def mark_applied(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    """Applied: record the getjo job as applied today (same logic as /applied --job-id), then go
    to its card on the Applications board."""
    return _track_offer(settings, conn, form, today, "applied")


def mark_interested(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    """Interested: track the getjo job as to_apply (which queues the agent's research) and stay on
    Job Offers. The JSON reply holds the row's new actions cell and whether Undo may delete it."""
    return _track_offer(settings, conn, form, today, "interested")


def unmark_interested(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> None:
    """Undo Interested: delete the application it created while nothing else has happened to it."""
    job_id = form.get("job_id")
    if not job_id:
        raise BadRequest("job_id is missing.")
    app_id = db.undo_interested(conn, job_id)
    log.info("interested undone from the dashboard; application %s deleted", app_id)


def _track_offer(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str,
                 decision: str) -> dict:
    job = _getjo_job(settings, form)
    decided = db.job_decisions(conn).get(job["id"])
    if decided and decided["decision"] == "skipped":
        raise BadRequest("This job is skipped; undo the skip first.")
    if decision == "applied":
        app_id, created = db.record_application(
            conn, job["company"], job["title"], getjo_job_id=job["id"], link=job["url"], source=job["source"],
            date_applied=None if decided and decided["decision"] == "applied" else today, today=today)
    else:
        app_id, created = db.record_application(
            conn, job["company"], job["title"], getjo_job_id=job["id"], link=job["url"], source=job["source"],
            status="to_apply", today=today)
    status = db.get_application(conn, app_id)["status"]
    db.record_decision(conn, job, "interested" if status == "to_apply" else "applied")
    log.info("application %s recorded from the dashboard as %s", app_id, decision)
    if decision == "applied":
        return {"redirect": f"/applications?app={app_id}"}
    # Interested stays on Job Offers: a plain post returns there with its filters
    queued = bool(db.open_requests(conn, app_id))  # a reused application may have nothing queued
    return {"html": _tracked_cell(app_id, status, queued), "app_id": app_id, "created": created}


def skip_offer(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> None:
    """Skip a getjo job with a reason and an optional comment (kept as a label for search)."""
    job = _getjo_job(settings, form)
    reason = form.get("reason")
    if not reason:
        raise BadRequest("Pick a reason for skipping.")
    if len(form.get("comment", "")) > MAX_COMMENT:
        raise BadRequest(f"The comment is longer than {MAX_COMMENT} characters.")
    decided = db.job_decisions(conn).get(job["id"])
    if decided and decided["decision"] != "skipped":
        raise BadRequest(f"This job is already tracked as {decided['decision']}.")
    db.record_decision(conn, job, "skipped", reason, _optional(form, "comment"))
    log.info("getjo job skipped from the dashboard (reason %s)", reason)  # never the comment


def unskip_offer(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> None:
    job_id = form.get("job_id")
    if not job_id:
        raise BadRequest("job_id is missing.")
    db.undo_skip(conn, job_id)
    log.info("getjo job skip undone from the dashboard")


def add_task(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    title = form.get("title")
    if not title:
        raise BadRequest("A task needs a title.")
    app_id = _int_field(form, "application_id")
    app = None if app_id is None else db.get_application(conn, app_id)
    if app_id is not None and app is None:
        raise BadRequest(f"No application with id {app_id}.")
    task_id = db.add_task(conn, title, form.get("kind") or "admin", _optional(form, "due_date"), app_id)
    log.info("task %s added from the dashboard", task_id)
    return {"html": _task_row(db.get_task(conn, task_id), today, _here(app_id))}


def _app_id(conn: sqlite3.Connection, form: dict[str, str]) -> int:
    app_id = _int_field(form, "app_id")
    if app_id is None:
        raise BadRequest("app_id is missing.")
    if db.get_application(conn, app_id) is None:
        raise BadRequest(f"No application with id {app_id}.")
    return app_id


def move_application(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    """Move to…: any stage, forward or back, with an optional comment and interview date."""
    app_id = _app_id(conn, form)
    status = form.get("status") or ""
    if status not in db.APPLICATION_STATUSES:
        raise BadRequest("Pick a stage to move to.")
    comment = _optional(form, "comment")
    if comment and len(comment) > MAX_COMMENT:
        raise BadRequest(f"The comment is longer than {MAX_COMMENT} characters.")
    interview = _optional(form, "interview_date") if status in db.INTERVIEW_STAGES else None
    db.move_application(conn, app_id, status, today, comment, interview)
    log.info("application %s moved to %s from the dashboard", app_id, status)  # never the comment
    return {"redirect": _here(app_id)}


def apply_application(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    """The panel's Apply (after its confirm): Interested → Applied today, as Next stage: Applied."""
    app_id = _app_id(conn, form)
    if not db.move_application(conn, app_id, "applied", today, only_from="to_apply"):
        raise BadRequest("It is no longer Interested; use Move to… instead.")
    log.info("application %s marked applied from the panel", app_id)
    return {"redirect": _here(app_id)}


def comment_application(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    app_id = _app_id(conn, form)
    body = form.get("body", "")
    if not body:
        raise BadRequest("A comment needs some text.")
    if len(body) > MAX_COMMENT:
        raise BadRequest(f"The comment is longer than {MAX_COMMENT} characters.")
    db.add_comment(conn, app_id, body)
    log.info("comment added to application %s from the dashboard", app_id)  # never the text
    return {"redirect": _here(app_id)}


def send_message(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    """The panel's Agent thread: store the user's message (author is always user, whatever the
    form says) and ping the standing assistant with `/ask <app> <message>`, ids only, in the
    background. The message stays pending until /ask replies through the CLI."""
    app_id = _app_id(conn, form)
    body = form.get("body", "")
    if not body:
        raise BadRequest("Type a question or paste an email first.")
    msg_id = db.add_message(conn, app_id, body)  # checks the length after normalising line breaks
    log.info("message %s added to application %s's thread from the dashboard", msg_id, app_id)  # never the text
    assistant.notify_message_in_background(settings, app_id, msg_id)
    return {"redirect": _here(app_id), "html": _thread(db.thread(conn, app_id), bool(settings.assistant_task))}


def queue_research(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    """Research / CV fit button: queue the request (Redo when a result exists). A second click
    while it is pending changes nothing. The agent does the work, never the dashboard."""
    app_id = _app_id(conn, form)
    kind = form.get("kind") or ""
    if kind not in db.REQUEST_KINDS:
        raise BadRequest("Pick research or cv_fit.")
    has_result, _ = research.result_path(conn, settings.private_dir, app_id, kind)
    _, created = db.queue_request(conn, app_id, kind, force=has_result)
    log.info("%s %s for application %s from the dashboard", kind, "queued" if created else "already queued", app_id)
    return {"redirect": _here(app_id)}


def set_tier(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    """Confirm or change the application's tier (the agent only suggests one)."""
    app_id = _app_id(conn, form)
    tier = form.get("tier") or ""
    if tier not in db.TIERS:
        raise BadRequest("Pick tier A, B or C.")
    db.update_application(conn, app_id, tier=tier)
    log.info("tier set on application %s from the dashboard", app_id)
    return {"redirect": _here(app_id)}


def _here(app_id: int | None) -> str:
    """The Applications page with a card open (redirect target built from a database id)."""
    return f"/applications?app={app_id}" if app_id is not None else "/applications"


def complete_task(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> None:
    task_id = _int_field(form, "task_id")
    if task_id is None:
        raise BadRequest("task_id is missing.")
    db.complete_task(conn, task_id)
    log.info("task %s done from the dashboard", task_id)


def reopen_task(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> None:
    task_id = _int_field(form, "task_id")
    if task_id is None:
        raise BadRequest("task_id is missing.")
    db.reopen_task(conn, task_id)
    log.info("task %s reopened from the dashboard", task_id)


# path -> (action, page to return to, MESSAGES key)
ACTIONS: dict[str, tuple[Callable[..., dict | None], str, str]] = {
    "/offers/apply": (mark_applied, "/offers", "applied"),
    "/offers/interested": (mark_interested, "/offers", "interested"),
    "/offers/skip": (skip_offer, "/offers", "skipped"),
    "/offers/unskip": (unskip_offer, "/offers", "unskipped"),
    "/offers/uninterested": (unmark_interested, "/offers", "uninterested"),
    "/matches/apply": (mark_applied, "/offers", "applied"),  # old form action, before the rename
    "/applications/move": (move_application, "/applications", "moved"),
    "/applications/apply": (apply_application, "/applications", "applied_today"),
    "/applications/comment": (comment_application, "/applications", "commented"),
    "/applications/research": (queue_research, "/applications", "research_queued"),
    "/applications/tier": (set_tier, "/applications", "tier_set"),
    "/applications/message": (send_message, "/applications", "message_sent"),
    "/tasks/add": (add_task, "/applications", "task_added"),
    "/tasks/done": (complete_task, "/applications", "task_done"),
    "/tasks/reopen": (reopen_task, "/applications", "task_reopened"),
}


# --- pages (GET) ------------------------------------------------------------------------
# Each returns (title, subtitle, HTML content). Only columns that are shown are read; never pay.

def dashboard_page(settings: Settings, conn: sqlite3.Connection, today: str) -> tuple[str, str, str]:
    apps = stats.application_stages(conn)
    week = stats.applied_last_7_days(conn, today)
    target = settings.weekly_target
    reply = stats.reply_rate(apps)
    to_apply = sum(1 for a in apps if not a["submitted"])

    rate = "–" if reply["rate"] is None else f"{reply['rate']:.0%}"
    tiles = (
        _tile("Applied in the last 7 days", f"{week} / {target}", _meter(week, target),
              "success" if week >= target else "accent")
        + _tile("Total applications", str(reply["submitted"]), f"<small>plus {to_apply} not yet sent</small>")
        + _tile("Reply rate", rate, f"<small>{reply['replied']} of {reply['submitted']} replied</small>",
                "warning" if reply["warning"] else "")
    )
    warning = ""
    if reply["warning"]:
        warning = (f'<div class="warning" role="alert">Reply rate is below {stats.REPLY_WARNING_RATE:.0%} after '
                   f'{reply["submitted"]} applications. Review the CV, targeting or tiers.</div>')

    no_apps = "No applications yet."
    status_chart = charts.donut(
        [(_status_label(s), n, STATUS_SHADES.get(s, "s5")) for s, n in stats.counts_by_status(apps)],
        "applications")
    tier_chart = charts.treemap([
        (_tier_label(t), TIER_SHADES.get(t, "s3"),
         [(_status_label(s), n, STATUS_SHADES.get(s, "s5")) for s, n in by_status])
        for t, by_status in stats.counts_by_tier_status(apps)
    ])
    funnel = charts.funnel([
        (_status_label(stage), n, share, STATUS_SHADES.get(stage, "s5"))
        for stage, n, share in stats.conversion(apps)
    ])
    per_week = charts.columns(_weeks(stats.applications_per_week(conn, today)), target)
    content = (
        f'<div class="tiles">{tiles}</div>{warning}'
        + '<div class="columns">'
        + _card("Applications by status", status_chart or _empty(no_apps), count=len(apps))
        + _card("Tiers by status", (tier_chart + _note("Column width: applications per tier. "
                                                       "Blocks: their current status.")) if tier_chart
                else _empty(no_apps))
        + "</div>"
        + _card("Stage conversion", (funnel + _note("Sent applications that reached each stage or a later "
                                                    "one, with their share.")) if funnel else _empty(no_apps),
                cls="card-funnel")
        + _card("Applications per week", per_week + _note(f"Week starting Monday. Target {target} per week."),
                cls="card-weeks")
    )
    return "Dashboard", f"Pipeline and progress · {today}", content


SHOW = {"new": "New", "interested": "Interested", "applied": "Applied", "skipped": "Skipped", "all": "All"}
LOCATION_LABELS = {"london": "London", "remote": "Remote", "other": "Other"}
SKIP_REASON_LABELS = {
    "location": "Wrong location", "seniority": "Seniority", "role_type": "Role type", "stack": "Stack", "company": "Company",
    "pay": "Pay", "contract_type": "Contract type", "other": "Other",
}
DECISION_SHADES = {"interested": "s6", "applied": "s8", "skipped": "s3"}


def offer_filters(query: dict[str, list[str]]) -> dict:
    """Job Offers filters from the query string. Unknown or bad values fall back to the defaults,
    so an old bookmark never fails."""
    def first(key: str) -> str:
        return (query.get(key) or [""])[0].strip()

    score = first("score")
    show = first("show")
    return {
        "score": int(score) if score.isdigit() and 1 <= int(score) <= 10 else getjo.DEFAULT_THRESHOLD,
        "company": first("company"),
        "role": first("role"),
        "loc": tuple(loc for loc in getjo.LOCATIONS if loc in query.get("loc", [])),
        "show": show if show in SHOW else "new",
    }


def filter_offers(new_jobs: list[dict], decided_jobs: dict[str, dict], decisions: dict[str, dict],
                  filters: dict) -> list[tuple[dict, dict | None]]:
    """(job, decision or None) pairs that pass the filters, newest first.

    `new_jobs` are getjo jobs at the minimum score (the score filter applies to New only);
    `decided_jobs` are the getjo rows of decided jobs, whatever their score. A decided job
    that getjo has dropped comes from its snapshot.
    """
    pairs = [(job, None) for job in new_jobs if job["id"] not in decisions]
    for job_id, d in decisions.items():
        job = decided_jobs.get(job_id)
        if job is None:
            snapshot = {k: d[k] for k in ("company", "title", "location", "score", "url", "source")}
            job = {"id": job_id, "first_seen": None, **snapshot}
        pairs.append((job, d))

    def keep(job: dict, decision: dict | None) -> bool:
        state = decision["decision"] if decision else "new"
        if filters["show"] != "all" and state != filters["show"]:
            return False
        if filters["company"].lower() not in (job["company"] or "").lower():
            return False
        if filters["role"].lower() not in (job["title"] or "").lower():
            return False
        return not filters["loc"] or bool(getjo.location_tags(job["location"]) & set(filters["loc"]))

    def newest(pair: tuple[dict, dict | None]) -> str:
        job, decision = pair
        return job["first_seen"] or (decision["decided_at"] if decision else "") or ""

    return sorted(((job, d) for job, d in pairs if keep(job, d)), key=newest, reverse=True)


def offers_page(settings: Settings, conn: sqlite3.Connection, today: str,
                query: dict[str, list[str]]) -> tuple[str, str, str]:
    f = offer_filters(query)
    subtitle = f"New: getjo jobs scored {f['score']} or more; decided jobs at any score"
    decisions = db.job_decisions(conn)
    try:
        with closing(getjo.connect_ro(settings.getjo_db)) as gconn:
            new_jobs = getjo.matches(gconn, threshold=f["score"]) if f["show"] in ("new", "all") else []
            decided_jobs = getjo.jobs_by_id(gconn, set(decisions))
    except getjo.GetjoUnavailable as e:
        return "Job Offers", subtitle, _banner(f"getjo jobs are not available: {e}.", "warning")
    except sqlite3.Error as e:
        return "Job Offers", subtitle, _banner(f"Could not read the getjo database: {e}.", "warning")

    found = filter_offers(new_jobs, decided_jobs, decisions, f)
    app_ids = db.app_ids_by_job(conn)
    here = "/offers?" + urlencode(
        [(k, v) for k, v in (("score", f["score"]), ("company", f["company"]), ("role", f["role"]),
                             *(("loc", loc) for loc in f["loc"]), ("show", f["show"])) if v != ""])
    head: tuple = (("Score", "num"), "Company", "Role", "Location", ("Seen", "date"))
    if f["show"] != "new":
        head += ("Decision",)
    head += ("",)
    rows, attrs = [], []
    for job, decision in found:
        cells = (
            _score_badge(job["score"]),
            _link(job["url"], job["company"]),
            h(job["title"]), h(job["location"]), _mono(h((job["first_seen"] or "")[:10])),
        )
        if f["show"] != "new":
            cells += (_decision_cell(decision),)
        cells += (_offer_actions(job, decision, f["show"], here, app_ids.get(job["id"])),)
        rows.append(cells)
        attrs.append(f'data-company="{h((job["company"] or "").lower())}" '
                     f'data-role="{h((job["title"] or "").lower())}"')
    if f["company"] or f["role"] or f["loc"]:
        empty = "Nothing matches these filters."
    else:
        empty = {"new": "No new job offers.", "interested": "No jobs marked interested.",
                 "applied": "Nothing marked applied.",
                 "skipped": "Nothing skipped.", "all": "No job offers."}[f["show"]]
    table = _table(head, rows, empty, table_id="offers", hint="New getjo matches appear here after each poll.",
                   row_attrs=attrs)
    content = _offer_filter_form(f) + _card(SHOW[f["show"]], table, count=len(found), count_for="offers")
    if any(d is None for _, d in found):  # some row has a Skipped link
        content += _skip_dialog()
    return "Job Offers", f"{subtitle} · {SHOW[f['show']].lower()} ({len(found)})", content


def _offer_filter_form(f: dict) -> str:
    """The filter bar: a GET form, so a filtered view is a bookmarkable URL. The page script
    submits it on change and filters company/role as you type."""
    scores = "".join(f'<option value="{n}"{" selected" if n == f["score"] else ""}>score ≥ {n}</option>'
                     for n in range(10, 0, -1))
    locs = "".join(
        f'<label class="check"><input type="checkbox" name="loc" value="{loc}"'
        f'{" checked" if loc in f["loc"] else ""}> {label}</label>'
        for loc, label in LOCATION_LABELS.items()
    )
    show = "".join(f'<option value="{k}"{" selected" if k == f["show"] else ""}>{label}</option>'
                   for k, label in SHOW.items())
    form = (
        '<form method="get" action="/offers" class="add filters" data-live-filters>'
        f'<select name="score" aria-label="Minimum score">{scores}</select>'
        f'<input type="search" name="company" value="{h(f["company"])}" placeholder="Company…" aria-label="Company">'
        f'<input type="search" name="role" value="{h(f["role"])}" placeholder="Role…" aria-label="Role">'
        f'<span class="checks" role="group" aria-label="Location">{locs}</span>'
        f'<select name="show" aria-label="Show">{show}</select>'
        + _button("Filter", "secondary") + "</form>"
    )
    return _card("Filters", form, cls="card-form")


def _decision_cell(decision: dict | None) -> str:
    if not decision:
        return _badge("new", "s6")
    badge = _badge(decision["decision"], DECISION_SHADES.get(decision["decision"], "neutral"))
    reason = SKIP_REASON_LABELS.get(decision["reason"] or "", "")
    comment = f' <span class="muted">— {h(decision["comment"])}</span>' if decision["comment"] else ""
    return f'{badge} {h(reason)}{comment}'


def _offer_actions(job: dict, decision: dict | None, show: str, here: str, app_id: int | None) -> str:
    """Interested · Skipped for a new job, Open card for a tracked one, Undo for a skipped one.
    Applying happens on the Applications board, after the agent's research. Interested stays on
    this page (the script swaps the cell and offers Undo). Skipped links to the skip form page;
    the script opens the skip dialog instead and removes the row."""
    keep = f'<input type="hidden" name="job_id" value="{h(job["id"])}"><input type="hidden" name="return" value="{h(here)}">'
    if decision is None:
        interested = (f'<form method="post" action="/offers/interested" class="inline" data-ajax="interested">{keep}'
                      + _button("Interested", "secondary", size="sm") + "</form>")
        skip_url = "/offers/skip?" + urlencode([("job_id", job["id"]), ("return", here)])
        skipped = (f'<a class="btn btn-ghost btn-sm" href="{h(skip_url)}" data-skip="{h(job["id"])}"'
                   f'{" data-remove" if show == "new" else ""}>Skipped</a>')
        return f'<div class="offer-actions">{interested}{skipped}</div>'
    if decision["decision"] == "skipped":
        ajax = ' data-ajax="remove-row"' if show == "skipped" else ""
        return (f'<form method="post" action="/offers/unskip" class="inline"{ajax}>{keep}'
                + _button("Undo skip", "ghost", "sm") + "</form>")
    return _open_card(app_id)


def _open_card(app_id: int | None) -> str:
    return f'<a class="btn btn-ghost btn-sm" href="{h(_here(app_id))}">Open card</a>' if app_id is not None else ""


def _tracked_cell(app_id: int, status: str, queued: bool = False) -> str:
    """A row's actions cell once its job is tracked (the Interested reply swaps it in)."""
    if status == "to_apply":
        label = "Interested · research queued" if queued else "Interested"
    else:
        label = _status_label(status)
    return f'<div class="offer-actions"><span class="offer-state">{h(label)}</span>{_open_card(app_id)}</div>'



def _skip_dialog() -> str:
    """The Skipped pop-up: one button per reason and an optional comment. The page script fills
    in job_id and return when a row's Skipped link is clicked; without the script it never opens."""
    reasons = "".join(
        f'<button class="btn btn-secondary btn-sm" name="reason" value="{k}">{h(v)}</button>'
        for k, v in SKIP_REASON_LABELS.items())
    return (
        '<dialog id="skip-dialog" class="dialog" aria-labelledby="skip-title">'
        '<form method="post" action="/offers/skip" class="skip-form">'
        '<input type="hidden" name="job_id" value=""><input type="hidden" name="return" value="">'
        '<h2 id="skip-title">Skip this job?</h2><p class="muted" data-skip-job></p>'
        f'<textarea name="comment" maxlength="{MAX_COMMENT}" rows="2" placeholder="Comment (optional)" '
        'aria-label="Comment"></textarea>'
        f'<div class="reasons" role="group" aria-label="Reason">{reasons}</div>'
        '<button type="button" class="btn btn-ghost btn-sm" data-close>Cancel</button>'
        "</form></dialog>"
    )


def skip_page(settings: Settings, conn: sqlite3.Connection, today: str,
              query: dict[str, list[str]]) -> tuple[str, str, str]:
    """The Skipped form without the page script: a reason select and a comment. Reading it
    changes nothing; the form posts to /offers/skip."""
    job_id = (query.get("job_id") or [""])[0]
    back = (query.get("return") or [""])[0]
    job = _getjo_job(settings, {"job_id": job_id})
    reasons = "".join(f'<option value="{k}">{h(v)}</option>' for k, v in SKIP_REASON_LABELS.items())
    cancel = _return_to(back, "/offers")
    form = (
        '<form method="post" action="/offers/skip" class="add">'
        f'<input type="hidden" name="job_id" value="{h(job["id"])}"><input type="hidden" name="return" value="{h(back)}">'
        f'<select name="reason" required aria-label="Skip reason"><option value="">skip reason</option>{reasons}</select>'
        f'<input name="comment" maxlength="{MAX_COMMENT}" placeholder="comment (optional)" aria-label="Comment">'
        + _button("Skip") + f'<a class="btn btn-ghost" href="{h(cancel)}">Cancel</a></form>'
    )
    return "Skip a job", f'{job["company"]} — {job["title"]}', _card("Why skip it?", form, cls="card-form")


def board_filters(query: dict[str, list[str]]) -> dict:
    """Applications page state from the query string: open card (`app`), `view` (board or list)
    and `stage` (a board column). Unknown values fall back to the defaults."""
    def first(key: str) -> str:
        return (query.get(key) or [""])[0].strip()

    app, view, stage = first("app"), first("view"), first("stage")
    return {
        "app": int(app) if app.isdigit() else None,
        "view": view if view in VIEWS else "board",
        "stage": stage if stage in BOARD_COLUMNS else "",
    }


def apps_url(app: int | None = None, view: str = "board", stage: str = "") -> str:
    """An Applications page URL; default values are left out (built from ids and fixed names)."""
    pairs = [(k, v) for k, v in (("app", app), ("view", view if view != "board" else None),
                                  ("stage", stage or None)) if v is not None]
    return "/applications" + (f"?{urlencode(pairs)}" if pairs else "")


def column_of(status: str) -> str:
    """The board column of a stored status (to_apply → interested, technical → interviewing…)."""
    return next(col for col, statuses in BOARD_COLUMNS.items() if status in statuses)


def next_stage(status: str) -> str | None:
    """The stage after `status` in the funnel (Interested … Offer); None for Offer and closed."""
    if status not in BOARD_STAGES or status == BOARD_STAGES[-1]:
        return None
    return BOARD_STAGES[BOARD_STAGES.index(status) + 1]


def step_dots(status: str, label: bool = True) -> str:
    """Interview progress: one dot per interview step (done, current, ahead) and the current
    step's short label (left out next to a stage badge). Empty outside the interview stages."""
    if status not in db.INTERVIEW_STAGES:
        return ""
    current = db.INTERVIEW_STAGES.index(status)
    dots = "".join(
        f'<span class="step {"is-done" if i < current else "is-current" if i == current else "is-ahead"}"></span>'
        for i in range(len(db.INTERVIEW_STAGES)))
    name = STEP_LABELS[status]
    text = f'<span class="step-label">{h(name)}</span>' if label else ""
    return (f'<span class="steps" title="Step {current + 1} of {len(db.INTERVIEW_STAGES)}: {h(name)}">'
            f'<span class="dots" aria-hidden="true">{dots}</span>{text}</span>')


def age_tone(status: str, days: int, no_response_days: int) -> str:
    """Ageing colour for days in stage: Applied turns amber at day 7 and red two days before it
    would move to No response. Other stages stay neutral."""
    if status != "applied":
        return ""
    if days >= max(no_response_days - 2, 1):
        return "late"
    return "ageing" if days >= 7 else ""


def list_order(rows: list[dict]) -> list[dict]:
    """List view order: applications with a dated next task first (earliest due first), then
    undated tasks, then none; ties by days in stage, longest first."""
    def key(r: dict) -> tuple:
        nxt = r["next"]
        rank = 0 if nxt and nxt["due_date"] else (1 if nxt else 2)
        return rank, (nxt["due_date"] or "") if nxt else "", -r["days"], r["app"]["id"]
    return sorted(rows, key=key)


def applications_page(settings: Settings, conn: sqlite3.Connection, today: str,
                      query: dict[str, list[str]]) -> tuple[str, str, str]:
    """A five-column board (or a list) to scan, with a stage strip that filters it, and a side
    panel for the application named by ?app=ID (an unknown id is ignored). General tasks below."""
    f = board_filters(query)
    tasks = db.app_tasks(conn, "todo")
    counts = db.comment_counts(conn)
    found = db.research_view(conn)
    rows = []
    for a in db.board_applications(conn):
        own = tasks.get(a["id"], [])
        rows.append({"app": a, "column": column_of(a["status"]), "next": own[0] if own else None,
                     "tasks": own, "comments": counts.get(a["id"], 0),
                     "days": (date.fromisoformat(today) - date.fromisoformat(a["since"])).days,
                     "found": found.get(a["id"]) or {"profile": None, "fit": None, "open": set()}})
    focus = next((r for r in rows if r["app"]["id"] == f["app"]), None)
    shown = [r for r in rows if not f["stage"] or r["column"] == f["stage"]]

    def card(r: dict) -> str:
        return _app_card(r, today, settings.no_response_days, apps_url(r["app"]["id"], f["view"], f["stage"]),
                         r is focus)

    open_count = sum(1 for r in rows if r["column"] != "closed")
    closed_count = len(rows) - open_count
    if not rows:
        main = _empty("No applications yet.", "Mark a job Applied or Interested on Job Offers, or use /applied.")
    else:
        main = (_stage_strip(rows, f) + _board(shown, f, card)
                + _app_list(shown, f, today) + _move_dialog())
    panel = _panel(conn, focus, today, f, bool(settings.assistant_task)) if focus else ""
    layout = (f'<div class="apps" data-view="{f["view"]}">'
              f'<div class="apps-main">{_card("Pipeline", main, count=open_count, cls="card-board")}</div>'
              f"{panel}</div>")
    content = layout + _general_tasks(conn, tasks.get(None, []), today)
    return "Applications", f"{open_count} open · {closed_count} closed · {today}", content


def _stage_strip(rows: list[dict], f: dict) -> str:
    """Counts per column; each links to that column's filter (the active one clears it), and the
    Board | List toggle (links, so it works without the script)."""
    pills = []
    for col, label in COLUMN_LABELS.items():
        n = sum(1 for r in rows if r["column"] == col)
        active = f["stage"] == col
        href = apps_url(f["app"], f["view"], "" if active else col)
        pills.append(f'<a class="stage-pill{" is-active" if active else ""}" href="{h(href)}" data-stage="{col}"'
                     f'{CURRENT if active else ""}>{h(label)} <span class="count">{n}</span></a>')
    views = "".join(
        f'<a class="view-btn{" is-active" if f["view"] == v else ""}" href="{h(apps_url(f["app"], v, f["stage"]))}" '
        f'data-view="{v}"{CURRENT if f["view"] == v else ""}>{v.capitalize()}</a>'
        for v in VIEWS)
    return (f'<div class="stage-strip"><nav class="stage-pills" aria-label="Filter by stage">{"".join(pills)}</nav>'
            f'<div class="view-toggle" role="group" aria-label="View">{views}</div></div>')


def _board(rows: list[dict], f: dict, card: Callable[[dict], str]) -> str:
    """Five columns; with a stage filter only that column. Closed is a strip that expands, open
    only under ?stage=closed (never because the panel shows a closed application: at laptop
    width it would push the board sideways)."""
    columns = []
    for col, label in COLUMN_LABELS.items():
        if f["stage"] and col != f["stage"]:
            continue
        mine = [r for r in rows if r["column"] == col]
        if col == "closed":
            columns.append(_closed_column(mine, card, bool(f["stage"])))
            continue
        body = "".join(card(r) for r in mine) or '<p class="col-empty">–</p>'
        columns.append(f'<section class="col" aria-label="{h(label)}" data-drop="{col}">'
                       f'<h2>{h(label)}<span class="count">{len(mine)}</span></h2><div class="col-cards">{body}</div></section>')
    filtered = " is-filtered" if f["stage"] else ""
    return f'<div class="board{filtered}" data-board{" hidden" if f["view"] != "board" else ""}>{"".join(columns)}</div>'


def _closed_column(rows: list[dict], card: Callable[[dict], str], expanded: bool) -> str:
    """Closed: a narrow strip with the count per outcome; opened, the cards grouped by outcome."""
    groups = []
    for status in CLOSED_LANE:
        mine = [r for r in rows if r["app"]["status"] == status]
        groups.append(f'<h3>{h(_status_label(status))} <span class="count">{len(mine)}</span></h3>'
                      + "".join(card(r) for r in mine))
    counts = "".join(f'<span>{h(_status_label(s))} {sum(1 for r in rows if r["app"]["status"] == s)}</span>'
                     for s in CLOSED_LANE)
    return (f'<details class="col col-closed" aria-label="Closed" data-drop="closed"{" open" if expanded else ""}>'
            f'<summary><span class="col-closed-title">Closed <span class="count">{len(rows)}</span></span>'
            f'<span class="col-closed-counts">{counts}</span></summary>'
            f'<div class="col-cards">{"".join(groups)}</div></details>')


def _app_card(r: dict, today: str, no_response_days: int, href: str, focus: bool) -> str:
    """A compact card, a link to its side panel (no forms): company and role (they wrap), the
    interview steps, the next task with its due date, tier and fit badges, days in stage with
    an ageing colour, pending research chips and the comment count. Draggable with the script."""
    app, nxt, days = r["app"], r["next"], r["days"]
    next_line = (f'<span class="app-next">{_due(nxt["due_date"], today)}<span>{h(nxt["title"])}</span></span>' if nxt
                 else '<span class="app-next muted">No open tasks</span>')
    tone = age_tone(app["status"], days, no_response_days)
    age_text = _age_text(days)
    age = (f'<span class="age{" age-" + tone if tone else ""}" title="{h(age_text)}">'
           f'{"today" if days <= 0 else f"{days}d"}</span>')
    state = research_state(app, r["found"])
    state_line = ""
    if state:  # Interested: Researching… or Ready (fit and tier) on its own line, not separate chips
        text, ready = state
        state_line = f'<span class="app-state"><span class="chip {"chip-ready" if ready else "chip-pending"}">{h(text)}</span></span>'
        badges = []
    else:
        badges = [_tier_badge(app["tier"]) if app["tier"] else ""]
        fit = r["found"]["fit"]
        if fit and "cv_fit" not in r["found"]["open"]:
            badges.append(f'<span class="chip" title="CV fit">fit {h(fit["score"])}/5</span>')
        badges += [f'<span class="chip chip-pending">{h(db.REQUEST_LABELS[k])} pending</span>'
                   for k in db.REQUEST_KINDS if k in r["found"]["open"]]
    if r["column"] == "closed":
        badges.insert(0, _status_badge(app["status"]))
    comments = (f'<span class="app-comments" title="comments">{_icon(COMMENT_ICON)}{r["comments"]}</span>'
                if r["comments"] else "")
    css = "app-card is-focus" if focus else "app-card"
    return (f'<a class="{css}" id="app-{h(app["id"])}" href="{h(href)}" draggable="true" data-app="{h(app["id"])}" '
            f'data-column="{r["column"]}" data-status="{h(app["status"])}">'
            f'<strong class="app-head">{h(app["company"])}</strong><span class="app-role">{h(app["role"])}</span>'
            f'{step_dots(app["status"])}{next_line}{state_line}'
            f'<span class="app-meta"><span class="app-badges">{"".join(badges)}</span>{comments}{age}</span></a>')


def research_state(app: dict, found: dict) -> tuple[str, bool] | None:
    """An Interested application's research state: ("Researching…", False) while company research
    or the CV fit is queued, ("Ready: fit N/5 · tier X", True) once both results are in (tier: the
    confirmed one, else the suggested one, left out when neither). None otherwise."""
    if app["status"] != "to_apply":
        return None
    if found["open"]:
        return "Researching…", False
    profile, fit = found["profile"], found["fit"]
    if not (profile and fit):
        return None
    tier = app["tier"] or profile["suggested_tier"]
    # no-break spaces keep "fit 4/5" and "tier A" whole when a narrow card wraps the chip
    return f'Ready: fit\u00a0{fit["score"]}/5' + (f" · tier\u00a0{tier}" if tier else ""), True


def _age_text(days: int) -> str:
    return f'{days} day{"s" if days != 1 else ""} in stage' if days > 0 else "new today"


def _app_list(rows: list[dict], f: dict, today: str) -> str:
    """The list view: one table, open applications only (unless filtered to Closed), sorted by
    the next due task. A row's company links to its side panel."""
    if f["stage"] != "closed":
        rows = [r for r in rows if r["column"] != "closed"]
    table_rows, attrs = [], []
    for r in list_order(rows):
        app, nxt = r["app"], r["next"]
        fit = r["found"]["fit"]
        href = apps_url(app["id"], f["view"], f["stage"])
        table_rows.append((
            f'<a href="{h(href)}">{h(app["company"])}</a>', h(app["role"]), _status_badge(app["status"]),
            h(nxt["title"]) if nxt else "", _due(nxt["due_date"], today) if nxt else "",
            _mono(h(r["days"])), h(f'{fit["score"]}/5') if fit else "", h(app["tier"] or ""),
        ))
        attrs.append(f'data-href="{h(href)}" data-app="{h(app["id"])}"' + (' class="is-focus"' if app["id"] == f["app"] else ""))
    head = ("Company", "Role", "Stage", "Next task", ("Due", "date"), ("Days", "num"), ("Fit", "text"), "Tier")
    table = _table(head, table_rows, "No open applications here.", table_id="app-list", row_attrs=attrs)
    return f'<div class="app-list" data-list{" hidden" if f["view"] != "list" else ""}>{table}</div>'


def _panel(conn: sqlite3.Connection, r: dict, today: str, f: dict, assistant_on: bool = False) -> str:
    """The side panel for one application: tasks, Next stage and Move to, company research,
    CV fit and tier, the Agent thread, comments and history. Every form posts and comes back to
    this panel."""
    app = r["app"]
    here = apps_url(app["id"], f["view"], f["stage"])
    keep = (f'<input type="hidden" name="app_id" value="{h(app["id"])}">'
            f'<input type="hidden" name="return" value="{h(here)}">')
    rows = "".join(_task_row(t, today, here, kind=False, ajax=False) for t in r["tasks"])
    task_list = (f'<table class="mini" id="tasks-{h(app["id"])}"><tbody>{rows}</tbody></table>' if rows
                 else '<p class="muted">No open tasks.</p>')
    add = (
        '<form method="post" action="/tasks/add" class="add add-sm">'
        f'<input type="hidden" name="application_id" value="{h(app["id"])}">'
        f'<input type="hidden" name="return" value="{h(here)}">'
        '<input name="title" placeholder="New task…" required aria-label="New task">'
        '<input type="date" name="due_date" aria-label="Due date">'
        + _button("Add", "secondary", "sm") + "</form>"
    )
    interested = app["status"] == "to_apply"
    nxt = next_stage(app["status"])
    next_form = ""
    if nxt and not interested:  # Interested: the action row's Apply replaces Next stage: Applied
        date_field = ('<label class="interview-date">Interview date <input type="date" name="interview_date"></label>'
                      if nxt in db.INTERVIEW_STAGES else "")
        next_form = (f'<form method="post" action="/applications/move" class="next-stage">{keep}'
                     f'<input type="hidden" name="status" value="{nxt}">{date_field}'
                     + _button(f"Next stage: {_status_label(nxt)}", size="sm") + "</form>")
    stages = "".join(
        f'<option value="{s}"{" disabled" if s == app["status"] else ""}>{h(_status_label(s))}</option>'
        for s in (*BOARD_STAGES, *CLOSED_LANE))
    move = (
        f'<form method="post" action="/applications/move" class="move" '
        f'data-interview-stages="{" ".join(db.INTERVIEW_STAGES)}">{keep}'
        f'<select name="status" required aria-label="Move to"><option value="">Move to…</option>{stages}</select>'
        '<label class="interview-date">Interview date <input type="date" name="interview_date"></label>'
        f'<input name="comment" maxlength="{MAX_COMMENT}" placeholder="Comment (optional)" aria-label="Comment with the move">'
        + _button("Move", "secondary", "sm") + "</form>"
    )
    comment = (
        f'<form method="post" action="/applications/comment" class="add add-sm">{keep}'
        f'<input name="body" maxlength="{MAX_COMMENT}" required placeholder="Comment, e.g. take-home test sent" '
        'aria-label="Comment">' + _button("Add comment", "secondary", "sm") + "</form>"
    )
    history = "".join(
        f'<li><span class="mono">{h(e["at"][:10])}</span> '
        + (_status_badge(e["status"]) if e["status"] else "")
        + (f' <span class="{"muted" if e["status"] else ""}">{h(e["comment"])}</span>' if e["comment"] else "")
        + "</li>"
        for e in reversed(db.timeline(conn, app["id"]))
    )
    link = (f' · {_link(app["link"], "Job posting ↗")}'
            if app["link"] and urlsplit(app["link"]).scheme in ("http", "https") else "")
    close = apps_url(None, f["view"], f["stage"])
    return (
        f'<aside class="panel" id="panel" aria-labelledby="panel-title" data-app="{h(app["id"])}" '
        f'data-status="{h(app["status"])}">'
        f'<header class="panel-head"><div><h2 id="panel-title">{h(app["company"])}</h2>'
        f'<p class="panel-sub">{h(app["role"])}</p>'
        f'<p class="panel-sub">{_status_badge(app["status"])} {step_dots(app["status"], label=False)}'
        f'<span class="muted">{h(_age_text(r["days"]))}</span>{link}</p></div>'
        f'<a class="panel-close btn btn-ghost btn-sm" href="{h(close)}" aria-label="Close" data-close-panel>✕</a></header>'
        f'<div class="panel-body">'
        + (_interested_actions(app, r["found"], here) if interested else "")
        + f'<section><h3>Tasks</h3>{task_list}{add}</section>'
        f'<section><h3>Stage</h3>{next_form}{move}</section>'
        f'<section><h3>Company and CV fit</h3>{_research_section(app, r["found"], buttons=not interested)}</section>'
        + _agent_section(app["id"], db.thread(conn, app["id"]), keep, assistant_on) +
        f'<section><h3>Comments and history</h3>{comment}'
        + (f'<ul class="timeline">{history}</ul>' if history else "")
        + "</section></div></aside>"
    )


def _agent_section(app_id: int, messages: list[dict], keep: str, assistant_on: bool) -> str:
    """The Agent thread (newest at the bottom) and a box to ask a question or paste an email.
    `keep` is the panel's trusted hidden app_id/return fields."""
    ask = (
        f'<form method="post" action="/applications/message" class="ask" data-ajax="thread">{keep}'
        f'<textarea name="body" rows="3" maxlength="{db.MAX_MESSAGE}" required '
        'placeholder="Ask about the company or the CV fit, or paste a recruiter email…" '
        'aria-label="Message to the agent"></textarea>'
        '<div class="ask-foot"><span class="muted">Pasted emails update the stage.</span>'
        + _button("Send", size="sm") + "</div></form>"
    )
    return (f'<section class="agent" aria-labelledby="agent-{h(app_id)}"><h3 id="agent-{h(app_id)}">Agent</h3>'
            f'{_thread(messages, assistant_on)}{ask}</section>')


def _thread(messages: list[dict], assistant_on: bool) -> str:
    """The thread itself, swapped in whole by the page script after Send and while polling.
    data-pending marks a user message still waiting for its reply."""
    items = "".join(
        f'<li class="msg msg-{"agent" if m["author"] == "agent" else "user"}">'
        f'<div class="msg-meta"><span class="msg-who">{"agent" if m["author"] == "agent" else "you"}</span>'
        f'<time datetime="{h(m["created_at"])}">{h(_local_time(m["created_at"]))}</time></div>'
        f'<div class="msg-body">{_linkify(m["body"])}</div></li>'
        for m in messages
    )
    pending = any(m["author"] == "user" and m["status"] in ("pending", "claimed") for m in messages)
    if not messages:
        state = '<p class="muted thread-empty">No messages yet.</p>'
    elif pending and assistant_on:
        state = '<p class="thread-state" role="status"><span class="spinner" aria-hidden="true"></span>Agent is working…</p>'
    elif pending:
        state = ('<p class="thread-state" role="status">Waiting for the agent: run /ask in a Claude Code session '
                 "(no standing assistant is set).</p>")
    else:
        state = ""
    return (f'<div class="thread" data-thread{" data-pending" if pending else ""}>'
            + (f'<ol class="msgs">{items}</ol>' if items else "") + state + "</div>")


_URL = re.compile(r"(https?://[^\s<>\"']+)")


def _linkify(text: str) -> str:
    """Plain text with its http(s) URLs as links. The raw text is split first, then every piece is
    escaped, so nothing in a message can add markup."""
    out = []
    for i, part in enumerate(_URL.split(text)):
        if i % 2 == 0:
            out.append(h(part))
            continue
        url = part.rstrip(".,;:!?)]")
        out.append(_link(url, url) + h(part[len(url):]))
    return "".join(out)


def _local_time(stamp: str) -> str:
    """A stored UTC timestamp as the server's local "Jan 20, 14:05"."""
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().strftime("%b %d, %H:%M")
    except ValueError:
        return stamp


def thread_json(settings: Settings, query: dict[str, list[str]], today: str) -> Response:
    """GET /applications/thread?app=ID for the panel's polling: whether a message is still
    pending, the application's status (the agent may have moved it) and the thread's HTML."""
    raw = (query.get("app") or [""])[0]
    if not raw.isdigit():
        return _json(400, {"ok": False, "message": "app must be an application id."})
    with closing(_connect(settings, today)) as conn:
        app = db.get_application(conn, int(raw))
        if app is None:
            return _json(404, {"ok": False, "message": "No such application."})
        messages = db.thread(conn, app["id"])
    pending = any(m["author"] == "user" and m["status"] in ("pending", "claimed") for m in messages)
    return _json(200, {"ok": True, "pending": pending, "status": app["status"],
                       "html": _thread(messages, bool(settings.assistant_task))})


def _move_dialog() -> str:
    """The drop dialog: an interview step with a date (dropped on Interviewing) or an outcome
    (dropped on Closed). The page script fills in app_id and return, shows one group and
    disables the other; without the script it never opens."""
    steps = "".join(f'<option value="{s}">{h(STEP_LABELS[s])}</option>' for s in db.INTERVIEW_STAGES)
    outcomes = "".join(f'<button class="btn btn-secondary btn-sm" name="status" value="{s}">{h(_status_label(s))}</button>'
                       for s in CLOSED_LANE)
    return (
        '<dialog id="move-dialog" class="dialog" aria-labelledby="move-title">'
        '<form method="post" action="/applications/move" class="skip-form">'
        '<input type="hidden" name="app_id" value=""><input type="hidden" name="return" value="">'
        '<h2 id="move-title" data-move-title>Move</h2><p class="muted" data-move-app></p>'
        '<fieldset class="move-group" data-group="interviewing"><label>Interview step '
        f'<select name="status" aria-label="Interview step">{steps}</select></label>'
        '<label>Interview date <input type="date" name="interview_date"></label></fieldset>'
        f'<textarea name="comment" maxlength="{MAX_COMMENT}" rows="2" placeholder="Comment (optional)" '
        'aria-label="Comment"></textarea>'
        f'<div class="reasons" role="group" aria-label="Outcome" data-group="closed">{outcomes}</div>'
        '<div class="dialog-actions"><button type="button" class="btn btn-ghost btn-sm" data-close>Cancel</button>'
        '<button class="btn btn-primary btn-sm" data-group="interviewing">Move</button></div>'
        "</form></dialog>"
    )


def _research_button(app_id: int, kind: str, has_result: bool, pending: bool, here: str = "") -> str:
    """Research / CV fit: queues the request; "… pending" (disabled) while queued; Redo once done.
    With `here` (the action row) the labels name the kind and the post returns to that panel URL."""
    if pending:
        return f'<button class="btn btn-ghost btn-sm" disabled>{h(db.REQUEST_LABELS[kind])} pending</button>'
    if here:
        label = {"research": "Redo research", "cv_fit": "Redo CV fit"}[kind] if has_result else db.REQUEST_LABELS[kind]
    else:
        label = "Redo" if has_result else {"research": "Research", "cv_fit": "CV fit"}[kind]
    back = f'<input type="hidden" name="return" value="{h(here)}">' if here else ""
    return ('<form method="post" action="/applications/research" class="inline">'
            f'<input type="hidden" name="app_id" value="{h(app_id)}"><input type="hidden" name="kind" value="{h(kind)}">'
            + back + _button(label, "secondary", "sm") + "</form>")


def _interested_actions(app: dict, found: dict, here: str) -> str:
    """The Interested panel's action row: Company research · CV fit · Apply, with the research state.
    Apply opens a short confirm (a <details>, so it needs no script) that moves it to Applied today."""
    state = research_state(app, found)
    line = ""
    if state:
        text, ready = state
        line = f'<p class="research-state{" is-ready" if ready else ""}" role="status">{h(text)}</p>'
    buttons = "".join(_research_button(app["id"], kind, bool(found["profile" if kind == "research" else "fit"]),
                                       kind in found["open"], here) for kind in db.REQUEST_KINDS)
    apply = (
        '<details class="confirm"><summary class="btn btn-primary btn-sm">Apply</summary>'
        '<form method="post" action="/applications/apply" class="confirm-form">'
        f'<input type="hidden" name="app_id" value="{h(app["id"])}"><input type="hidden" name="return" value="{h(here)}">'
        '<span>Mark as applied today?</span>' + _button("Yes, applied", size="sm") + "</form></details>"
    )
    return (f'<section class="panel-actions" aria-label="Next steps">{line}'
            f'<div class="action-row">{buttons}{apply}</div></section>')


def _open_link(app_id: int, kind: str) -> str:
    href = "/research/file?" + urlencode([("app", app_id), ("kind", kind)])
    return f'<a class="btn btn-ghost btn-sm" href="{h(href)}">Open</a>'


def _research_section(app: dict, found: dict, buttons: bool = True) -> str:
    """Company research and CV fit summaries with their buttons (left out for Interested, whose
    action row holds them), and the tier form."""
    profile, fit = found["profile"], found["fit"]

    def button(kind: str, has_result: bool) -> str:
        return _research_button(app["id"], kind, has_result, kind in found["open"]) if buttons else ""

    head = '<div class="research-head"><strong>Company</strong>'
    if profile:
        head += f' <span class="mono muted">{h(profile["researched_at"])}</span>'
    head += ('<span class="research-actions">' + (_open_link(app["id"], "research") if profile else "")
             + button("research", bool(profile)) + "</span></div>")
    company = head
    if profile:
        size = " · ".join(x for x in (
            profile["size_band"] and f'{profile["size_band"]} people',
            profile["employees"] and f'~{profile["employees"]}'
            + (f' (as of {profile["employees_as_of"]})' if profile["employees_as_of"] else ""),
            profile["sector"],
            profile["recognition"] and db.RECOGNITION_LABELS.get(profile["recognition"], profile["recognition"]),
            profile["london_remote"],
        ) if x)
        company += (f'<p>{h(profile["what_it_does"])}</p>' if profile["what_it_does"] else "")
        company += f'<p class="muted">{h(size)}</p>' if size else ""
        if profile["suggested_tier"]:
            company += (f'<p>Suggested tier <strong>{h(profile["suggested_tier"])}</strong>'
                        + (f' — {h(profile["tier_reason"])}' if profile["tier_reason"] else "") + "</p>")
    else:
        company += f'<p class="muted">{"Queued for the agent." if "research" in found["open"] else "Not researched yet."}</p>'

    fit_head = '<div class="research-head"><strong>CV fit</strong>'
    if fit:
        tone = "s9" if fit["score"] >= 4 else ("s6" if fit["score"] == 3 else "s3")
        fit_head += " " + _badge(f'{fit["score"]}/5', tone)
        fit_head += f' <span class="mono muted">{h(fit["checked_at"])}</span>'
    fit_head += ('<span class="research-actions">' + (_open_link(app["id"], "cv_fit") if fit else "")
                 + button("cv_fit", bool(fit)) + "</span></div>")
    fit_html = fit_head
    if fit and fit["top_gaps"]:
        fit_html += '<ul class="gaps">' + "".join(f"<li>{h(g)}</li>" for g in fit["top_gaps"]) + "</ul>"
    elif not fit:
        fit_html += f'<p class="muted">{"Queued for the agent." if "cv_fit" in found["open"] else "Not checked yet."}</p>'

    suggested = profile["suggested_tier"] if profile else None
    current = app["tier"] or suggested
    options = "".join(f'<option value="{t}"{" selected" if t == current else ""}>Tier {t}</option>' for t in db.TIERS)
    label = "Confirm" if not app["tier"] and suggested else "Set tier"
    placeholder = "" if current else '<option value="">Tier…</option>'
    tier_form = (
        '<form method="post" action="/applications/tier" class="add add-sm">'
        f'<input type="hidden" name="app_id" value="{h(app["id"])}">'
        f'<select name="tier" required aria-label="Tier">{placeholder}{options}</select>'
        + _button(label, "secondary", "sm") + "</form>"
    )
    return f'<div class="research">{company}</div><div class="research">{fit_html}</div>{tier_form}'


def research_file_page(settings: Settings, conn: sqlite3.Connection, today: str,
                       query: dict[str, list[str]]) -> tuple[str, str, str]:
    """A research or CV fit file, read-only. Only a path recorded in prep.db that resolves inside
    private_dir is read (anything else is a 404); a missing file says so instead of failing."""
    app_param = (query.get("app") or [""])[0]
    kind = (query.get("kind") or [""])[0]
    if not app_param.isdigit() or kind not in db.REQUEST_KINDS:
        raise NotFound()
    app = db.get_application(conn, int(app_param))
    recorded, path = research.result_path(conn, settings.private_dir, int(app_param), kind)
    if app is None or not recorded or path is None:
        raise NotFound()
    title = f'{db.REQUEST_LABELS[kind]} · {app["company"]}'
    back = f'<p><a class="btn btn-ghost btn-sm" href="{h(_here(app["id"]))}">Back to the card</a></p>'
    if not path.is_file():
        return title, app["role"], back + _banner("File missing: it was moved or deleted. Redo it from the card.", "warning")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return title, app["role"], back + _banner("The file could not be read.", "warning")
    try:
        meta, body = research.split_front_matter(text)
    except ValueError:
        meta, body = {}, text
    rows = [(h(k.replace("_", " ")), h(", ".join(map(str, v)) if isinstance(v, list) else v))
            for k, v in meta.items() if k.lower() not in ("pay", "salary")]
    content = back
    if rows:
        content += _card("Summary", _table(("Field", "Value"), rows, ""))
    content += _card("Notes", f'<div class="doc">{render_markdown(body)}</div>')
    return title, f'{app["role"]} · {path.name}', content


def render_markdown(text: str) -> str:
    """A small, safe subset of Markdown: headings, bullet lists and paragraphs. Every piece of
    text is escaped; no links, images or raw HTML are produced."""
    out: list[str] = []
    para: list[str] = []
    items: list[str] = []

    def flush() -> None:
        if para:
            out.append(f"<p>{h(' '.join(para))}</p>")
            para.clear()
        if items:
            out.append("<ul>" + "".join(f"<li>{h(i)}</li>" for i in items) + "</ul>")
            items.clear()

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            flush()
        elif stripped.startswith("#"):
            flush()
            level = min(len(stripped) - len(stripped.lstrip("#")) + 1, 4)
            out.append(f"<h{level}>{h(stripped.lstrip('#').strip())}</h{level}>")
        elif stripped[:2] in ("- ", "* "):
            if para:
                flush()
            items.append(stripped[2:].strip())
        else:
            if items:
                flush()
            para.append(stripped)
    flush()
    return "".join(out) or '<p class="muted">No notes.</p>'


def _task_row(task: dict, today: str, here: str, kind: bool = True, ajax: bool = True) -> str:
    """An open task in General tasks (due, title, kind, Done) or in the side panel (no kind, to
    fit; no ajax, so the board's next task refreshes too). A plain post returns to `here` (a URL
    built from database ids)."""
    done = (
        '<form method="post" action="/tasks/done" class="inline"' + (' data-ajax="done">' if ajax else ">")
        + f'<input type="hidden" name="task_id" value="{h(task["id"])}">'
        f'<input type="hidden" name="return" value="{h(here)}">'
        + _button("Done", "secondary", "sm") + "</form>"
    )
    cells = (_due(task["due_date"], today), h(task["title"]), _kind_badge(task["kind"]), done)
    return _row(cells if kind else cells[:2] + cells[3:])


def _general_tasks(conn: sqlite3.Connection, tasks: list[dict], today: str) -> str:
    """Tasks with no application: add form, open list, and the recently done (all tasks) with Reopen."""
    kinds = "".join(f'<option value="{h(k)}"{" selected" if k == "admin" else ""}>{h(k)}</option>'
                    for k in db.TASK_KINDS)
    add = (
        '<form method="post" action="/tasks/add" class="add" data-ajax="add-row" data-target="general-tasks">'
        '<input name="title" placeholder="New task…" required aria-label="Title">'
        f'<select name="kind" aria-label="Kind">{kinds}</select>'
        '<input type="date" name="due_date" aria-label="Due date">'
        + _button("Add task") + "</form>"
    )
    table = _table((("Due", "date"), "Task", "Kind", ""), [], "No general tasks.",
                   hint="Tasks that belong to no application, e.g. drills or admin.")
    if tasks:
        rows = "".join(_task_row(t, today, "/applications") for t in tasks)
        table = (f'<div class="table-wrap"><table id="general-tasks"><thead><tr><th scope="col" data-sort="date">Due</th>'
                 f'<th scope="col" data-sort="text">Task</th><th scope="col" data-sort="text">Kind</th><th scope="col"></th>'
                 f'</tr></thead><tbody>{rows}</tbody></table></div>')
    done_rows = []
    for t in db.recent_done_tasks(conn, RECENT_DONE):
        reopen = (
            '<form method="post" action="/tasks/reopen" class="inline">'
            f'<input type="hidden" name="task_id" value="{h(t["id"])}">'
            + _button("Reopen", "ghost", "sm") + "</form>"
        )
        done_rows.append((_mono(h((t["done_at"] or "")[:10])), h(t["title"]), _kind_badge(t["kind"]), reopen))
    recent = ""
    if done_rows:
        recent = (f'<details class="recent"><summary>Recently done <span class="count">{len(done_rows)}</span></summary>'
                  + _table((("Done", "date"), "Task", "Kind", ""), done_rows, "") + "</details>")
    return _card("General tasks", add + table + recent, count=len(tasks), count_for="general-tasks", cls="card-general")


# --- HTML helpers ---------------------------------------------------------------------
# Helpers that take plain values escape them; parameters documented as HTML are trusted
# fragments built by other helpers.

def _page(status: int, title: str, subtitle: str, content: str, active: str) -> Response:
    current = ' aria-current="page"'
    nav = "".join(
        f'<a href="{href}"{current if href == active else ""}>{_icon(icon)}<span>{name}</span></a>'
        for href, name, icon in PAGES
    )
    sub = f'<p class="subtitle">{h(subtitle)}</p>' if subtitle else ""
    body = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="dark">'
        f"<title>{h(title)} · getjo</title>"
        '<link rel="stylesheet" href="/static/style.css">'
        '<script src="/static/app.js" defer></script></head>'
        f'<body class="theme-{PAGE_THEMES.get(active, "sage")}"><div class="app">'
        '<aside class="sidebar"><a class="brand" href="/"><span class="brand-mark">gj</span>'
        '<span class="brand-name">getjo</span></a>'
        f'<nav aria-label="Pages">{nav}</nav>'
        '<p class="sidebar-foot"><span class="dot"></span>local only · 127.0.0.1</p></aside>'
        f'<main><header class="page-head"><h1>{h(title)}</h1>{sub}</header>{content}</main>'
        '</div><div class="toasts" aria-live="polite"></div></body></html>'
    )
    return status, [("Content-Type", "text/html; charset=utf-8"), *SECURITY_HEADERS], body.encode("utf-8")


def _text(status: int, message: str) -> Response:
    return status, [("Content-Type", "text/plain; charset=utf-8"), *SECURITY_HEADERS], message.encode("utf-8")


def _json(status: int, payload: dict) -> Response:
    body = json.dumps(payload).encode("utf-8")
    return status, [("Content-Type", "application/json; charset=utf-8"), *SECURITY_HEADERS], body


def _icon(paths: str) -> str:
    """An inline stroke icon; `paths` is trusted SVG from this module. No xmlns: inline SVG needs none."""
    return (f'<svg class="icon" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" '
            f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{paths}</svg>')


def _row(cells: tuple[str, ...], attrs: str = "") -> str:
    """A table row; `attrs` is trusted, already-escaped attribute text."""
    return f"<tr{' ' + attrs if attrs else ''}>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"


def _table(head: tuple[str | tuple[str, str], ...], rows: list[tuple[str, ...]], empty: str,
           table_id: str = "", hint: str = "", row_attrs: list[str] | None = None) -> str:
    """A table from already-escaped cells, or an empty state when there are no rows.

    A head item is a label (sorted as text) or (label, "text" | "num" | "date"); an empty label
    is not sortable. The page script adds the sort buttons.
    """
    if not rows:
        return _empty(empty, hint)
    th = ""
    for col in head:
        label, kind = (col, "text") if isinstance(col, str) else col
        sort = f' data-sort="{h(kind)}"' if label else ""
        th += f'<th scope="col"{sort}>{h(label)}</th>'
    ident = f' id="{h(table_id)}"' if table_id else ""
    attrs = row_attrs or [""] * len(rows)
    trs = "".join(_row(row, a) for row, a in zip(rows, attrs))
    return f'<div class="table-wrap"><table{ident}><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table></div>'


def _card(title: str, body: str, count: int | None = None, action: str = "", cls: str = "",
          count_for: str = "") -> str:
    """A panel with a heading, an optional count chip and an optional HTML action (e.g. a filter)."""
    chip = ""
    if count is not None:
        target = f' data-count-for="{h(count_for)}"' if count_for else ""
        chip = f'<span class="count"{target}>{h(count)}</span>'
    extra = f" {h(cls)}" if cls else ""
    return (f'<section class="card{extra}"><header class="card-head"><h2>{h(title)}{chip}</h2>{action}</header>'
            f'<div class="card-body">{body}</div></section>')


def _empty(text: str, hint: str = "") -> str:
    tip = f'<p class="hint">{h(hint)}</p>' if hint else ""
    return f'<div class="empty">{_icon(EMPTY_ICON)}<p>{h(text)}</p>{tip}</div>'


def _banner(text: str, tone: str) -> str:
    """A warning or notice banner; the class is "warning" or "notice" only."""
    css = "warning" if tone == "warning" else "notice"
    return f'<div class="{css}" role="{"alert" if css == "warning" else "status"}">{h(text)}</div>'


def _badge(text: object, tone: str = "neutral") -> str:
    """A pill; `tone` is a semantic tone or a shade step of the page's hue (solid fill)."""
    tone = tone if tone in TONES else "neutral"
    css = f"badge {tone}" if tone in charts.SHADES else f"badge badge-{tone}"
    return f'<span class="{css}">{h(text)}</span>'


def _status_badge(status: str | None) -> str:
    if not status:
        return ""
    return _badge(db.status_label(status), STATUS_SHADES.get(status, "neutral"))


def _tier_badge(tier: str | None) -> str:
    return _badge(f"Tier {tier}", TIER_SHADES.get(tier, "neutral")) if tier else _badge("no tier", "s3")


def _kind_badge(kind: str | None) -> str:
    return _badge(kind, KIND_SHADES.get(kind or "", "neutral")) if kind else ""


def _score_badge(score: object) -> str:
    try:
        n = float(score)
    except (TypeError, ValueError):
        return _badge(score)
    tone = "s10" if n >= 9 else ("s8" if n >= 8 else "s6")
    return f'<span class="score">{_badge(score, tone)}</span>'


def _button(label: str, variant: str = "primary", size: str = "") -> str:
    css = f"btn btn-{h(variant)}" + (f" btn-{h(size)}" if size else "")
    return f'<button class="{css}">{h(label)}</button>'


def _filter(table_id: str, label: str) -> str:
    """A filter box for one table. Hidden until the page script shows it (useless without JS)."""
    return (f'<input type="search" class="filter" data-filter="{h(table_id)}" placeholder="{h(label)}…" '
            f'aria-label="{h(label)}" hidden>')


def _mono(escaped: str) -> str:
    return f'<span class="mono">{escaped}</span>' if escaped else ""


def _tile(label: str, value: str, extra: str = "", tone: str = "") -> str:
    """A stat tile; `extra` is trusted HTML (a meter or a small note)."""
    css = f" tile-{tone}" if tone in TONES else ""
    return (f'<div class="tile{css}"><span class="label">{h(label)}</span>'
            f'<span class="value">{h(value)}</span>{extra}</div>')


def _meter(value: int, total: int) -> str:
    # <progress> needs no inline style, which the Content-Security-Policy forbids.
    return f'<progress value="{min(value, total)}" max="{max(total, 1)}"></progress>'


def _note(text: str) -> str:
    return f'<p class="card-note">{h(text)}</p>'


def _status_label(status: str) -> str:
    return db.status_label(status).capitalize()


def _tier_label(tier: str) -> str:
    return "No tier" if tier == "unset" else f"Tier {tier}"


def _weeks(weeks: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """ISO week starts as short labels such as "7 Sep"."""
    return [(f"{date.fromisoformat(d).day} {date.fromisoformat(d):%b}", n) for d, n in weeks]


def _due(day: str | None, today: str) -> str:
    """A due date: dark badge with a red dot when overdue, light badge when due today, plain otherwise."""
    if not day:
        return ""
    if day < today:
        return f'<span class="badge s4 overdue">{h(day)}</span>'
    if day == today:
        return f'<span class="badge s8 due-today">{h(day)}</span>'
    return _mono(h(day))


def _link(url: str | None, text: str) -> str:
    """An external link only for http(s) URLs (never javascript: and the like)."""
    if url and urlsplit(url).scheme in ("http", "https"):
        return f'<a href="{h(url)}" rel="noreferrer noopener" target="_blank">{h(text)}</a>'
    return h(text)


# --- server -----------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "inter_prep"
    sys_version = ""

    def do_GET(self) -> None:
        self._respond("GET", b"")

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            self._send(*_text(413, "Request body too large."))
            return
        self._respond("POST", self.rfile.read(length))

    def _respond(self, method: str, body: bytes) -> None:
        try:
            response = handle(self.server.settings, method, self.path, dict(self.headers.items()), body)
        except Exception:
            log.exception("error handling %s %s", method, urlsplit(self.path).path)
            response = _text(500, "Something went wrong; see the server log.")
        self._send(*response)

    def _send(self, status: int, headers: list[tuple[str, str]], body: bytes) -> None:
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 (base-class name)
        # Request line and status only; bodies (which can hold notes) are never logged.
        log.info("%s %s", self.address_string(), format % args)


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], settings: Settings) -> None:
        self.settings = settings
        super().__init__(address, Handler)


def check_loopback(host: str) -> None:
    """Raise ValueError unless `host` is an IPv4 loopback address such as 127.0.0.1."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError(f"refusing to bind to {host!r}: use a loopback address such as 127.0.0.1") from None
    if ip.version != 4 or not ip.is_loopback:
        raise ValueError(f"refusing to bind to {host!r}: the dashboard binds to loopback (127.0.0.1) only")


def serve(settings: Settings, host: str = "127.0.0.1", port: int | None = None) -> None:
    """Run the dashboard until interrupted. Refuses any non-loopback `host`."""
    check_loopback(host)
    port = settings.dashboard_port if port is None else port
    with DashboardServer((host, port), settings) as server:
        log.info("dashboard at http://localhost:%s (prep.db %s)", port, settings.prep_db)
        server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m prep.web", description="inter_prep local dashboard")
    parser.add_argument("--port", type=int, help="default: dashboard_port from settings (8765)")
    args = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        serve(settings, port=args.port)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
