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
MAX_COMMENT = 1000
# Applications: the pipeline order (the list's stage groups follow it) and the closed outcomes.
PIPELINE_STAGES = ("to_apply", "applied", "recruiter_call", "technical", "system_design", "final", "offer")
CLOSED_LANE = ("rejected", "no_response", "withdrawn", "skipped")
# The stage strip's filters: stored statuses stay as they are; the four interview stages share one.
STAGE_GROUPS = {
    "interested": ("to_apply",), "applied": ("applied",), "interviewing": db.INTERVIEW_STAGES,
    "offer": ("offer",), "closed": CLOSED_LANE,
}
GROUP_LABELS = {"interested": "Interested", "applied": "Applied", "interviewing": "Interviewing",
                "offer": "Offer", "closed": "Closed"}
STEP_LABELS = {"recruiter_call": "Recruiter call", "technical": "Coding", "system_design": "System design",
               "final": "Company interview"}
# Stage badges in the pipeline list, short enough for a third of the screen (full label on hover).
LIST_STAGE_LABELS = {"to_apply": "Interested", "recruiter_call": "Recruiter", "technical": "Coding",
                     "system_design": "Sys design", "final": "Company", "no_response": "No reply"}
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
    "unapplied": "Back to Interested; the Applied tasks are gone.",
    "app_skipped": "Skipped and closed. Job Offers lists it under Skipped.",
    "skipped": "Skipped. It no longer appears in new job offers.",
    "unskipped": "Skip undone; the job is new again.",
    "task_done": "Task marked done.",
    "task_reopened": "Task reopened.",
    "research_queued": "Queued for the agent. It runs in the next Claude session (/research).",
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
    "rejected": "s1", "withdrawn": "s2", "no_response": "s3", "skipped": "s2", "to_apply": "s4", "applied": "s5",
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
        "/applications/skip": lambda s, c, t: app_skip_page(s, c, t, query),
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
    active = {"/offers/skip": "/offers", "/research/file": "/applications",
              "/applications/skip": "/applications"}.get(path, path)
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


def _app_id(conn: sqlite3.Connection, form: dict[str, str]) -> int:
    app_id = _int_field(form, "app_id")
    if app_id is None:
        raise BadRequest("app_id is missing.")
    if db.get_application(conn, app_id) is None:
        raise BadRequest(f"No application with id {app_id}.")
    return app_id


def apply_application(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    """The panel's Mark applied: Interested → Applied today, at once (the toast offers Undo)."""
    app_id = _app_id(conn, form)
    if not db.move_application(conn, app_id, "applied", today, only_from="to_apply"):
        raise BadRequest("It is no longer Interested; ask the agent to change the stage.")
    log.info("application %s marked applied from the panel", app_id)
    return {"redirect": _here(app_id), "app_id": app_id}


def unapply_application(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    """Undo Mark applied: back to Interested, its date and Applied tasks reverted (db.undo_applied)."""
    app_id = _app_id(conn, form)
    db.undo_applied(conn, app_id)
    log.info("mark applied undone on application %s", app_id)
    return {"redirect": _here(app_id)}


def skip_application(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    """The panel's Skip: a reason and an optional comment; closes the application as skipped and marks
    its getjo job skipped, as Job Offers does (db.skip_application)."""
    app_id = _app_id(conn, form)
    reason = form.get("reason")
    if not reason:
        raise BadRequest("Pick a reason for skipping.")
    if len(form.get("comment", "")) > MAX_COMMENT:
        raise BadRequest(f"The comment is longer than {MAX_COMMENT} characters.")
    db.skip_application(conn, app_id, reason, _optional(form, "comment"), today)
    log.info("application %s skipped from the panel (reason %s)", app_id, reason)  # never the comment
    return {"redirect": _here(app_id)}


def send_message(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    """The Agent box: store the user's message (author is always user, whatever the form says), about
    the application in `app_id` (the open panel's, when "About" is ticked) or the whole search, and
    ping the standing assistant with `/ask <app or general> <message>`, ids only, in the background.
    The message stays pending until /ask replies through the CLI. The JSON reply is the thread."""
    app_id = _int_field(form, "app_id")
    if app_id is not None and db.get_application(conn, app_id) is None:
        raise BadRequest(f"No application with id {app_id}.")
    body = form.get("body", "")
    if not body:
        raise BadRequest("Type a question or paste an email first.")
    msg_id = db.add_message(conn, app_id, body)  # checks the length after normalising line breaks
    log.info("message %s added (application %s) from the dashboard", msg_id, app_id)  # never the text
    assistant.notify_message_in_background(settings, app_id, msg_id)
    return {"html": _thread(conn, bool(settings.assistant_task))}


def queue_research(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    """Research / CV fit / Tailor CV button: queue the request (Redo when a result exists). A second click
    while it is pending changes nothing. The agent does the work, never the dashboard."""
    app_id = _app_id(conn, form)
    kind = form.get("kind") or ""
    if kind not in db.REQUEST_KINDS:
        raise BadRequest("Pick research, cv_fit or tailor.")
    has_result, _ = research.result_path(conn, settings.private_dir, app_id, kind)
    _, created = db.queue_request(conn, app_id, kind, force=has_result)
    log.info("%s %s for application %s from the dashboard", kind, "queued" if created else "already queued", app_id)
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
    "/applications/apply": (apply_application, "/applications", "applied_today"),
    "/applications/unapply": (unapply_application, "/applications", "unapplied"),
    "/applications/skip": (skip_application, "/applications", "app_skipped"),
    "/applications/research": (queue_research, "/applications", "research_queued"),
    "/applications/message": (send_message, "/applications", "message_sent"),
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
    fits = db.fit_scores(conn)
    here = "/offers?" + urlencode(
        [(k, v) for k, v in (("score", f["score"]), ("company", f["company"]), ("role", f["role"]),
                             *(("loc", loc) for loc in f["loc"]), ("show", f["show"])) if v != ""])
    head: tuple = (("Score", "num"), "Company", "Role", "Location", ("Seen", "date"))
    if f["show"] != "new":
        head += ("Decision",)
    head += ("",)
    rows, attrs = [], []
    for job, decision in found:
        fit = fits.get(app_ids.get(job["id"]))
        cells = (
            _score_badge(job["score"]),
            _link(job["url"], job["company"]) + (f' {_fit_badge(fit)}' if fit else ""),
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
        content += _skip_dialog("/offers/skip", "job_id")
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


def _fit_badge(score: int) -> str:
    """The agent's CV fit (1-5) on a tracked job's row, toned like the panel's."""
    return f'<span class="badge {_fit_tone(score)}" title="CV fit">fit {h(score)}/5</span>'


def _fit_tone(score: int) -> str:
    return "s9" if score >= 4 else ("s6" if score == 3 else "s3")


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



def _skip_dialog(action: str, field: str) -> str:
    """The Skip pop-up: one button per reason and an optional comment, posting to `action`. The page
    script fills in `field` (job_id on Job Offers, app_id on Applications) and return when a Skip
    link is clicked; without the script it never opens."""
    reasons = "".join(
        f'<button class="btn btn-secondary btn-sm" name="reason" value="{k}">{h(v)}</button>'
        for k, v in SKIP_REASON_LABELS.items())
    return (
        '<dialog id="skip-dialog" class="dialog" aria-labelledby="skip-title">'
        f'<form method="post" action="{h(action)}" class="skip-form" data-field="{h(field)}">'
        f'<input type="hidden" name="{h(field)}" value=""><input type="hidden" name="return" value="">'
        '<h2 id="skip-title">Skip this job?</h2><p class="muted" data-skip-job></p>'
        f'<textarea name="comment" maxlength="{MAX_COMMENT}" rows="2" placeholder="Comment (optional)" '
        'aria-label="Comment"></textarea>'
        f'<div class="reasons" role="group" aria-label="Reason">{reasons}</div>'
        '<button type="button" class="btn btn-ghost btn-sm" data-close>Cancel</button>'
        "</form></dialog>"
    )


def app_skip_page(settings: Settings, conn: sqlite3.Connection, today: str,
                  query: dict[str, list[str]]) -> tuple[str, str, str]:
    """The panel's Skip without the page script: a reason select and a comment. Reading it changes
    nothing; the form posts to /applications/skip."""
    raw = (query.get("app") or [""])[0]
    app = db.get_application(conn, int(raw)) if raw.isdigit() else None
    if app is None:
        raise NotFound()
    back = _return_to((query.get("return") or [""])[0], "/applications")
    reasons = "".join(f'<option value="{k}">{h(v)}</option>' for k, v in SKIP_REASON_LABELS.items())
    form = (
        '<form method="post" action="/applications/skip" class="add">'
        f'<input type="hidden" name="app_id" value="{h(app["id"])}"><input type="hidden" name="return" value="{h(back)}">'
        f'<select name="reason" required aria-label="Skip reason"><option value="">skip reason</option>{reasons}</select>'
        f'<input name="comment" maxlength="{MAX_COMMENT}" placeholder="Comment (optional)" aria-label="Comment">'
        + _button("Skip") + f'<a class="btn btn-ghost" href="{h(back)}">Cancel</a></form>'
    )
    return "Skip an application", f'{app["company"]} — {app["role"]}', _card("Why skip it?", form, cls="card-form")


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


def list_filters(query: dict[str, list[str]]) -> dict:
    """Applications page state from the query string: open panel (`app`), `stage` (a stage group)
    and `closed` (1: closed applications listed too). Unknown values fall back to the defaults;
    an old `view=board` is ignored."""
    def first(key: str) -> str:
        return (query.get(key) or [""])[0].strip()

    app, stage = first("app"), first("stage")
    return {
        "app": int(app) if app.isdigit() else None,
        "stage": stage if stage in STAGE_GROUPS else "",
        "closed": first("closed") == "1",
    }


def apps_url(app: int | None = None, stage: str = "", closed: bool = False) -> str:
    """An Applications page URL; default values are left out (built from ids and fixed names)."""
    pairs = [(k, v) for k, v in (("app", app), ("stage", stage or None), ("closed", "1" if closed else None))
             if v is not None]
    return "/applications" + (f"?{urlencode(pairs)}" if pairs else "")


def group_of(status: str) -> str:
    """The stage group of a stored status (to_apply → interested, technical → interviewing…)."""
    return next(g for g, statuses in STAGE_GROUPS.items() if status in statuses)


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


def list_order(rows: list[dict]) -> list[dict]:
    """Order within a stage: a dated next task first (earliest due first), then undated tasks,
    then none; ties by days in stage, longest first."""
    def key(r: dict) -> tuple:
        nxt = r["next"]
        rank = 0 if nxt and nxt["due_date"] else (1 if nxt else 2)
        return rank, (nxt["due_date"] or "") if nxt else "", -r["days"], r["app"]["id"]
    return sorted(rows, key=key)


def applications_page(settings: Settings, conn: sqlite3.Connection, today: str,
                      query: dict[str, list[str]]) -> tuple[str, str, str]:
    """The pipeline list on the left (grouped by stage, with a stage strip that filters it); the side panel for the application named by ?app=ID on the right (an unknown id
    is ignored). The panel holds no forms but the shortcut buttons, task ticks and the Agent thread."""
    f = list_filters(query)
    tasks = db.app_tasks(conn, "todo")
    latest = db.latest_comments(conn)
    found = db.research_view(conn)
    rows = []
    for a in db.board_applications(conn):
        own = tasks.get(a["id"], [])
        rows.append({"app": a, "group": group_of(a["status"]), "next": own[0] if own else None,
                     "tasks": own, "comment": latest.get(a["id"]),
                     "days": (date.fromisoformat(today) - date.fromisoformat(a["since"])).days,
                     "found": found.get(a["id"]) or {"profile": None, "fit": None, "tailored": None,
                                                     "open": set()}})
    focus = next((r for r in rows if r["app"]["id"] == f["app"]), None)
    open_count = sum(1 for r in rows if r["group"] != "closed")
    closed_count = len(rows) - open_count
    if not rows:
        main = _empty("No applications yet.", "Mark a job Interested on Job Offers, or use /applied.")
    else:
        main = _stage_strip(rows, f) + _app_list(rows, f)
    panel = _panel(conn, focus, today, f) if focus else ""
    here = apps_url(f["app"] if focus else None, f["stage"], f["closed"])
    agent = _agent_box(conn, focus, here, bool(settings.assistant_task))
    content = (f'<div class="apps"><div class="apps-main">{_card("Pipeline", main, count=open_count, cls="card-pipeline")}'
               f'{agent}</div>{panel}</div>'
               + _skip_dialog("/applications/skip", "app_id"))
    return "Applications", f"{open_count} open · {closed_count} closed · {today}", content


def _stage_strip(rows: list[dict], f: dict) -> str:
    """Counts per stage group; each links to that group's filter (the active one clears it). Show /
    Hide closed lists the closed applications too (links, so it works without the script)."""
    pills = []
    for group, label in GROUP_LABELS.items():
        n = sum(1 for r in rows if r["group"] == group)
        active = f["stage"] == group
        href = apps_url(f["app"], "" if active else group, f["closed"])
        pills.append(f'<a class="stage-pill{" is-active" if active else ""}" href="{h(href)}" data-stage="{group}"'
                     f'{CURRENT if active else ""}>{h(label)} <span class="count">{n}</span></a>')
    toggle = ""
    if f["stage"] != "closed":
        href = apps_url(f["app"], f["stage"], not f["closed"])
        toggle = (f'<a class="view-btn{" is-active" if f["closed"] else ""}" href="{h(href)}" data-closed-toggle>'
                  f'{"Hide closed" if f["closed"] else "Show closed"}</a>')
    return (f'<div class="stage-strip"><nav class="stage-pills" aria-label="Filter by stage">{"".join(pills)}</nav>'
            f'{toggle}</div>')


def _app_list(rows: list[dict], f: dict) -> str:
    """The pipeline: one table, a <tbody> per stage (Interested → Offer, then the closed outcomes
    when shown), next due task first within a stage. Columns: company (a link to the panel), role,
    stage, next task and the latest comment (one line, with the count when there are more)."""
    if f["stage"]:
        shown = [r for r in rows if r["group"] == f["stage"]]
    else:
        shown = [r for r in rows if r["group"] != "closed" or f["closed"]]
    bodies = []
    for status in (*PIPELINE_STAGES, *CLOSED_LANE):
        mine = list_order([r for r in shown if r["app"]["status"] == status])
        if not mine:
            continue
        head = (f'<tr class="group-row"><th colspan="5" scope="rowgroup">{h(_status_label(status).capitalize())}'
                f' <span class="count">{len(mine)}</span></th></tr>')
        bodies.append(f'<tbody data-group="{status}">{head}{"".join(_list_row(r, f) for r in mine)}</tbody>')
    if not bodies:
        return _empty("No applications here.", "Clear the stage filter or show the closed ones.")
    head = "".join(f'<th scope="col" class="col-{c.lower().replace(" ", "-")}">{c}</th>'
                   for c in ("Company", "Role", "Stage", "Next task", "Comment"))
    return (f'<div class="table-wrap app-list"><table id="app-list"><thead><tr>{head}</tr></thead>'
            f'{"".join(bodies)}</table></div>')


def _list_row(r: dict, f: dict) -> str:
    app, nxt, comment = r["app"], r["next"], r["comment"]
    href = apps_url(app["id"], f["stage"], f["closed"])
    status = app["status"]
    stage = (f'<span class="badge {STATUS_SHADES.get(status, "neutral")}" title="{h(_status_label(status).capitalize())}">'
             f'{h(LIST_STAGE_LABELS.get(status, _status_label(status).capitalize()))}</span>')
    next_cell = (f'<span class="clamp" title="{h(nxt["title"])}">{h(nxt["title"])}</span>' if nxt
                 else '<span class="muted">–</span>')
    comment_cell = ""
    if comment:
        body, n = comment
        more = f' <span class="count" title="{n} comments">+{n - 1}</span>' if n > 1 else ""
        comment_cell = f'<span class="comment-cell"><span class="ellip" title="{h(body)}">{h(body)}</span>{more}</span>'
    cells = (f'<a href="{h(href)}">{h(app["company"])}</a>',
             f'<span class="clamp" title="{h(app["role"])}">{h(app["role"])}</span>', stage, next_cell, comment_cell)
    focus = ' class="is-focus"' if app["id"] == f["app"] else ""
    return (f'<tr id="app-{h(app["id"])}" data-href="{h(href)}" data-app="{h(app["id"])}"{focus}>'
            + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")


def _age_text(days: int) -> str:
    return f'{days} day{"s" if days != 1 else ""} in this stage' if days > 0 else "new today"


def _panel(conn: sqlite3.Connection, r: dict, today: str, f: dict) -> str:
    """The side panel, top to bottom: header, the latest comment (read-only), shortcut buttons, tasks
    (tick to mark done), the agent's results (company research, CV fit, tailored CV versions) and
    the stage history. Everything else is asked of the agent in the Agent box under the list."""
    app, found = r["app"], r["found"]
    here = apps_url(app["id"], f["stage"], f["closed"])
    link = (f'<span>{_link(app["link"], "Job posting ↗")}</span>'
            if app["link"] and urlsplit(app["link"]).scheme in ("http", "https") else "")
    close = apps_url(None, f["stage"], f["closed"])
    versions = db.tailored_versions(conn, app["id"])
    busy = " data-busy" if found["open"] else ""
    return (
        f'<aside class="panel" id="panel" aria-labelledby="panel-title" data-app="{h(app["id"])}" '
        f'data-status="{h(app["status"])}"{busy}>'
        f'<header class="panel-head"><div><h2 id="panel-title">{h(app["company"])}</h2>'
        f'<p class="panel-role">{h(app["role"])}</p>'
        f'<p class="panel-sub">{_status_badge(app["status"])} {step_dots(app["status"], label=False)}'
        f'<span class="muted">{h(_age_text(r["days"]))}</span>{link}</p></div>'
        '<span class="panel-head-actions"><a class="panel-ask btn btn-secondary btn-sm" href="#agent" data-ask-agent>'
        'Ask the agent</a>'
        f'<a class="panel-close btn btn-ghost btn-sm" href="{h(close)}" aria-label="Close" data-close-panel>✕</a></span></header>'
        f'<div class="panel-body">'
        + _comments_section(conn, app["id"])
        + _shortcuts(conn, app, found, versions, here)
        + _panel_tasks(r["tasks"], today, here)
        + _research_cards(app, found, versions, here)
        + _history(conn, app["id"])
        + "</div></aside>"
    )


def _comments_section(conn: sqlite3.Connection, app_id: int) -> str:
    """The latest comment with its date, and how many came before it. Read-only: the agent adds,
    lists, amends and removes them (`cli comment`, `cli comments`)."""
    found = db.comments(conn, app_id)
    if found:
        latest = found[0]
        earlier = (f'<p class="muted">{len(found) - 1} earlier comment{"s" if len(found) > 2 else ""}; '
                   'ask the agent to list them.</p>' if len(found) > 1 else "")
        body = (f'<p class="comment-latest"><span class="mono muted">{h(latest["at"][:10])}</span> '
                f'<span class="comment-body">{h(latest["body"])}</span></p>{earlier}')
    else:
        body = '<p class="muted">No comments yet. Ask the agent to add one.</p>'
    return f'<section class="panel-comments" aria-labelledby="comments-{h(app_id)}"><h3 id="comments-{h(app_id)}">Comments</h3>{body}</section>'


def _short_day(stamp: str | None) -> str:
    """A date or timestamp as "3 Nov"."""
    if not stamp:
        return ""
    try:
        day = date.fromisoformat(stamp[:10])
    except ValueError:
        return stamp
    return f"{day.day} {day:%b}"


def _shortcuts(conn: sqlite3.Connection, app: dict, found: dict, versions: list[dict], here: str) -> str:
    """Research company · CV fit · Tailor CV · Mark applied · Skip, each showing its state: queued work
    is a disabled "…ing" button, a result links to its card below (Open and Redo are there). Mark
    applied and Skip act while the application is Interested, and show the outcome afterwards."""
    profile, fit, open_kinds = found["profile"], found["fit"], found["open"]

    def queue(kind: str, label: str) -> str:
        return ('<form method="post" action="/applications/research" class="inline">'
                f'<input type="hidden" name="app_id" value="{h(app["id"])}"><input type="hidden" name="kind" value="{kind}">'
                f'<input type="hidden" name="return" value="{h(here)}">' + _button(label, "secondary", "sm") + "</form>")

    def busy(label: str) -> str:
        return f'<button class="btn btn-ghost btn-sm is-busy" disabled><span class="spinner" aria-hidden="true"></span>{h(label)}</button>'

    def done(card: str, label: str) -> str:
        return f'<a class="btn btn-secondary btn-sm is-done" href="#{card}" data-jump="{card}">{h(label)}</a>'

    def state(text: str) -> str:
        return f'<button class="btn btn-ghost btn-sm is-done" disabled>{h(text)}</button>'

    buttons = []
    if "research" in open_kinds:
        buttons.append(busy("Researching…"))
    elif profile:
        buttons.append(done("card-research", f"Research ✓ {_short_day(profile['researched_at'])}"))
    else:
        buttons.append(queue("research", "Research company"))
    if "cv_fit" in open_kinds:
        buttons.append(busy("Checking CV fit…"))
    elif fit:
        buttons.append(done("card-fit", f"CV fit {fit['score']}/5"))
    else:
        buttons.append(queue("cv_fit", "CV fit"))
    if "tailor" in open_kinds:
        buttons.append(busy("Tailoring…"))
    elif versions:
        sent = (" · sent" if app.get("cv_sent_version") == versions[-1]["version"]
                else f" · sent v{app['cv_sent_version']}" if app.get("cv_sent_version") else "")
        buttons.append(done("card-tailor", f"CV v{versions[-1]['version']}{sent}"))
    else:
        buttons.append(queue("tailor", "Tailor CV"))
    if app["status"] == "to_apply":
        buttons.append(f'<form method="post" action="/applications/apply" class="inline" data-ajax="applied">'
                       f'<input type="hidden" name="app_id" value="{h(app["id"])}"><input type="hidden" name="return" value="{h(here)}">'
                       + _button("Mark applied", size="sm") + "</form>")
        skip_url = "/applications/skip?" + urlencode([("app", app["id"]), ("return", here)])
        buttons.append(f'<a class="btn btn-ghost btn-sm" href="{h(skip_url)}" data-skip="{h(app["id"])}" '
                       f'data-skip-label="{h(app["company"])} — {h(app["role"])}" data-after="refresh">Skip</a>')
    elif app["status"] == "skipped":
        buttons.append(state("Skipped"))
    elif app["date_applied"]:
        buttons.append(state(f"Applied · {_short_day(app['date_applied'])}"))
    return f'<section class="shortcuts" aria-label="Shortcuts"><div class="action-row">{"".join(buttons)}</div></section>'


def _panel_tasks(tasks: list[dict], today: str, here: str) -> str:
    """Open tasks with a tick that marks them done (a direct action; Undo in the toast)."""
    rows = "".join(
        '<tr><td class="tick-cell"><form method="post" action="/tasks/done" class="inline" data-ajax="tick">'
        f'<input type="hidden" name="task_id" value="{h(t["id"])}"><input type="hidden" name="return" value="{h(here)}">'
        f'<button class="tick" aria-label="Mark done: {h(t["title"])}" title="Mark done">☐</button></form></td>'
        f'<td>{h(t["title"])}</td><td class="due-cell">{_due(t["due_date"], today)}</td></tr>'
        for t in tasks)
    body = f'<table class="mini tasks-list"><tbody>{rows}</tbody></table>' if rows else '<p class="muted">No open tasks.</p>'
    return f'<section class="panel-tasks"><h3>Tasks <span class="count">{len(tasks)}</span></h3>{body}</section>'


def _redo(app_id: int, kind: str, here: str, label: str = "Redo") -> str:
    return ('<form method="post" action="/applications/research" class="inline">'
            f'<input type="hidden" name="app_id" value="{h(app_id)}"><input type="hidden" name="kind" value="{kind}">'
            f'<input type="hidden" name="return" value="{h(here)}">' + _button(label, "ghost", "sm") + "</form>")


def _open_link(app_id: int, kind: str, label: str = "Open", version: int | None = None) -> str:
    pairs = [("app", app_id), ("kind", kind)] + ([("v", version)] if version is not None else [])
    return f'<a class="btn btn-ghost btn-sm" href="{h("/research/file?" + urlencode(pairs))}">{h(label)}</a>'


def _artifact(card_id: str, title: str, meta: str, actions: str, body: str) -> str:
    return (f'<section class="artifact" id="{card_id}"><div class="artifact-head"><strong>{h(title)}</strong>{meta}'
            f'<span class="artifact-actions">{actions}</span></div>{body}</section>')


def _research_cards(app: dict, found: dict, versions: list[dict], here: str) -> str:
    """The agent's results, each with Open and Redo: company research (with the tier the research
    set and why), the CV fit with its top gaps, and every tailored CV version (the sent one marked).
    A card shows once there is a result; queued work shows on its shortcut button."""
    profile, fit, open_kinds = found["profile"], found["fit"], found["open"]
    cards = []
    if profile:
        size = " · ".join(x for x in (
            profile["size_band"] and f'{profile["size_band"]} people',
            profile["sector"],
            profile["recognition"] and db.RECOGNITION_LABELS.get(profile["recognition"], profile["recognition"]),
            profile["london_remote"],
        ) if x)
        body = (f'<p>{h(profile["what_it_does"])}</p>' if profile["what_it_does"] else "")
        body += f'<p class="muted">{h(size)}</p>' if size else ""
        tier = app["tier"] or profile["suggested_tier"]
        if tier:
            why = profile["tier_reason"] if tier == profile["suggested_tier"] else None
            body += f'<p>{_tier_badge(tier)}' + (f' {h(why)}' if why else "") + "</p>"
        actions = _open_link(app["id"], "research") + ("" if "research" in open_kinds else _redo(app["id"], "research", here))
        cards.append(_artifact("card-research", "Company research",
                               f' <span class="mono muted">{h(profile["researched_at"])}</span>', actions, body))
    if fit:
        gaps = ('<ul class="gaps">' + "".join(f"<li>{h(g)}</li>" for g in fit["top_gaps"]) + "</ul>") if fit["top_gaps"] else ""
        actions = _open_link(app["id"], "cv_fit") + ("" if "cv_fit" in open_kinds else _redo(app["id"], "cv_fit", here))
        score = _badge(f'{fit["score"]}/5', _fit_tone(fit["score"]))
        meta = f' {score} <span class="mono muted">{h(fit["checked_at"])}</span>'
        cards.append(_artifact("card-fit", "CV fit", meta, actions, gaps))
    if versions:
        items = "".join(
            f'<li><span class="mono version-name">{h(Path(v["file_path"]).name)}</span>'
            f'<span class="muted">{h(_short_day(v["created_at"]))}</span>'
            + (_badge("sent", "s9") if app.get("cv_sent_version") == v["version"] else "")
            + _open_link(app["id"], "tailor", version=v["version"]) + "</li>"
            for v in reversed(versions))
        actions = "" if "tailor" in open_kinds else _redo(app["id"], "tailor", here, "New from cv.md")
        body = (f'<ul class="versions">{items}</ul>'
                '<p class="muted">Ask the agent for changes; each one is a new version. Tell it which file you sent.</p>')
        cards.append(_artifact("card-tailor", "Tailored CV", "", actions, body))
    return f'<section class="panel-results"><h3>Results</h3>{"".join(cards)}</section>' if cards else ""


def _history(conn: sqlite3.Connection, app_id: int) -> str:
    """Stage changes, newest first, collapsed (their comments are under Comments)."""
    moves = [e for e in reversed(db.timeline(conn, app_id)) if e["status"]]
    if not moves:
        return ""
    items = "".join(f'<li><span class="mono">{h(e["at"][:10])}</span> {_status_badge(e["status"])}</li>' for e in moves)
    return (f'<details class="history"><summary>History <span class="count">{len(moves)}</span></summary>'
            f'<ul class="timeline">{items}</ul></details>')


AGENT_MESSAGES = 60  # the latest messages shown in the Agent box


def _agent_box(conn: sqlite3.Connection, focus: dict | None, here: str, assistant_on: bool) -> str:
    """The Agent box under the pipeline list: one conversation for every application. A new message
    is about the open panel's application while "About" is ticked, else about the whole search
    ("which tasks need me?"). The script adds an All / This job filter while a panel is open."""
    return (
        f'<section class="card card-agent agent" id="agent" aria-labelledby="agent-title">'
        f'<header class="card-head"><h2 id="agent-title">Agent</h2>'
        '<button type="button" class="btn btn-ghost btn-sm agent-back" data-back-to-panel>← Back to the job</button>'
        '<div class="agent-filter" role="group" aria-label="Show messages" hidden>'
        '<button type="button" class="view-btn is-active" data-filter-chat="all">All</button>'
        '<button type="button" class="view-btn" data-filter-chat="job">This job</button></div></header>'
        f'<div class="card-body">{_thread(conn, assistant_on)}'
        '<form method="post" action="/applications/message" class="ask" data-ajax="thread">'
        f'<input type="hidden" name="return" value="{h(here)}">{_agent_about(focus)}'
        f'<textarea name="body" rows="3" maxlength="{db.MAX_MESSAGE}" required '
        'placeholder="Ask anything: which tasks need me, a question about a job, a change (tasks, comments, stage, '
        'tier, CV), or paste a recruiter email…" aria-label="Message to the agent"></textarea>'
        '<div class="ask-foot"><span class="muted">The agent does the edits and replies here.</span>'
        + _button("Send", size="sm") + "</div></form></div></section>"
    )


def _agent_about(focus: dict | None) -> str:
    """With a panel open: a ticked "About <company>" box (untick it to ask about the whole search).
    The script swaps it when another panel opens, so a half-typed message stays."""
    if focus is None:
        return '<p class="agent-about muted" data-about="">About: the whole search</p>'
    app = focus["app"]
    return (f'<label class="agent-about" data-about="{h(app["id"])}"><input type="checkbox" name="app_id" '
            f'value="{h(app["id"])}" checked> About {h(app["company"])} — {h(app["role"])}</label>')


def _thread(conn: sqlite3.Connection, assistant_on: bool) -> str:
    """The conversation, its latest AGENT_MESSAGES oldest first, each tagged with its application (a
    link to its panel) or "all jobs". Swapped in whole by the page script after Send and while
    polling; data-pending marks a user message still waiting for its reply."""
    messages = db.thread(conn, limit=AGENT_MESSAGES)
    items = "".join(
        f'<li class="msg msg-{"agent" if m["author"] == "agent" else "user"}" data-app="{h(m["application_id"] or "")}">'
        f'<div class="msg-meta"><span class="msg-who">{"agent" if m["author"] == "agent" else "you"}</span>'
        + (f'<a class="msg-about" href="{h(_here(m["application_id"]))}">{h(m["company"])}</a>'
           if m["application_id"] is not None else '<span class="msg-about">all jobs</span>')
        + f'<time datetime="{h(m["created_at"])}">{h(_local_time(m["created_at"]))}</time></div>'
        f'<div class="msg-body">{_linkify(m["body"])}</div></li>'
        for m in messages
    )
    pending = bool(db.pending_messages(conn))
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
    """GET /applications/thread[?app=ID] for polling: whether a message is still waiting for the agent
    or (with `app`, the open panel) an agent request for that application is open; that
    application's status (the agent may have moved it); the message count (so the script can tell
    a reply from finished work) and the conversation's HTML."""
    raw = (query.get("app") or [""])[0]
    if raw and not raw.isdigit():
        return _json(400, {"ok": False, "message": "app must be an application id."})
    with closing(_connect(settings, today)) as conn:
        app = db.get_application(conn, int(raw)) if raw else None
        if raw and app is None:
            return _json(404, {"ok": False, "message": "No such application."})
        working = bool(app and db.open_requests(conn, app["id"]))
        pending = working or bool(db.pending_messages(conn))
        payload = {"ok": True, "pending": pending, "status": app["status"] if app else None,
                   "messages": db.message_count(conn), "html": _thread(conn, bool(settings.assistant_task))}
    return _json(200, payload)


FILE_TITLES = dict(db.REQUEST_LABELS)


def research_file_page(settings: Settings, conn: sqlite3.Connection, today: str,
                       query: dict[str, list[str]]) -> tuple[str, str, str]:
    """A research, CV fit or tailored CV file, read-only. Only a path recorded in prep.db that resolves inside
    private_dir is read (anything else is a 404); a missing file says so instead of failing."""
    app_param = (query.get("app") or [""])[0]
    kind = (query.get("kind") or [""])[0]
    version = (query.get("v") or [""])[0]
    if not app_param.isdigit() or kind not in FILE_TITLES or (version and not (kind == "tailor" and version.isdigit())):
        raise NotFound()
    app = db.get_application(conn, int(app_param))
    recorded, path = research.result_path(conn, settings.private_dir, int(app_param), kind,
                                          int(version) if version else None)
    if app is None or not recorded or path is None:
        raise NotFound()
    title = f'{FILE_TITLES[kind]} · {app["company"]}'
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
