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
requests (db.agent_requests) for the /research skill. When assistant_task is set, newly queued
requests are also sent to that dev3 agent (prep.assistant), in the background.
"""

import argparse
import html
import ipaddress
import json
import logging
import sqlite3
import sys
from collections.abc import Callable, Mapping
from contextlib import closing
from datetime import date
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
MAX_BODY = 64 * 1024
RECENT_DONE = 10
MAX_COMMENT = 1000
# Applications board: one column per open stage, then a collapsed lane for the closed ones.
BOARD_STAGES = ("to_apply", "applied", "recruiter_call", "technical", "system_design", "final", "offer")
CLOSED_LANE = ("rejected", "no_response", "withdrawn")

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
    "applied": "Recorded as applied. Its card and first tasks are highlighted.",
    "interested": "Saved as interested. Its card and first tasks are highlighted.",
    "moved": "Moved. The stage's tasks are on the card.",
    "commented": "Comment added.",
    "skipped": "Skipped. It no longer appears in new job offers.",
    "unskipped": "Skip undone; the job is new again.",
    "task_added": "Task added.",
    "task_done": "Task marked done.",
    "task_reopened": "Task reopened.",
    "research_queued": "Queued for the agent. It runs in the next Claude session (/research).",
    "tier_set": "Tier saved.",
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
    """Interested: track the getjo job as to_apply, then go to its card."""
    return _track_offer(settings, conn, form, today, "interested")


def _track_offer(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str,
                 decision: str) -> dict:
    job = _getjo_job(settings, form)
    decided = db.job_decisions(conn).get(job["id"])
    if decided and decided["decision"] == "skipped":
        raise BadRequest("This job is skipped; undo the skip first.")
    if decision == "applied":
        app_id, _ = db.record_application(
            conn, job["company"], job["title"], getjo_job_id=job["id"], link=job["url"], source=job["source"],
            date_applied=None if decided and decided["decision"] == "applied" else today, today=today)
    else:
        app_id, _ = db.record_application(
            conn, job["company"], job["title"], getjo_job_id=job["id"], link=job["url"], source=job["source"],
            status="to_apply", today=today)
    status = db.get_application(conn, app_id)["status"]
    db.record_decision(conn, job, "interested" if status == "to_apply" else "applied")
    log.info("application %s recorded from the dashboard as %s", app_id, decision)
    return {"redirect": f"/applications?app={app_id}"}


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
    "/matches/apply": (mark_applied, "/offers", "applied"),  # old form action, before the rename
    "/applications/move": (move_application, "/applications", "moved"),
    "/applications/comment": (comment_application, "/applications", "commented"),
    "/applications/research": (queue_research, "/applications", "research_queued"),
    "/applications/tier": (set_tier, "/applications", "tier_set"),
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
        cells += (_offer_actions(job, decision, f["show"], here),)
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


def _offer_actions(job: dict, decision: dict | None, show: str, here: str) -> str:
    """Applied · Interested · Skipped for a new job, Applied for an interested one, Undo for a
    skipped one. Applied and Interested post and land on the job's tasks. Skipped links to the
    skip form page; the page script opens the skip dialog instead and removes the row."""
    keep = f'<input type="hidden" name="job_id" value="{h(job["id"])}"><input type="hidden" name="return" value="{h(here)}">'
    applied = (f'<form method="post" action="/offers/apply" class="inline">{keep}'
               + _button("Applied", size="sm") + "</form>")
    if decision is not None and decision["decision"] == "interested":
        return applied  # already tracked: skipping it would mean withdrawing the application
    if decision is None:
        interested = (f'<form method="post" action="/offers/interested" class="inline">{keep}'
                      + _button("Interested", "secondary", size="sm") + "</form>")
        skip_url = "/offers/skip?" + urlencode([("job_id", job["id"]), ("return", here)])
        skipped = (f'<a class="btn btn-ghost btn-sm" href="{h(skip_url)}" data-skip="{h(job["id"])}"'
                   f'{" data-remove" if show == "new" else ""}>Skipped</a>')
        return f'<div class="offer-actions">{applied}{interested}{skipped}</div>'
    if decision["decision"] == "skipped":
        ajax = ' data-ajax="remove-row"' if show == "skipped" else ""
        return (f'<form method="post" action="/offers/unskip" class="inline"{ajax}>{keep}'
                + _button("Undo skip", "ghost", "sm") + "</form>")
    return ""


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


def applications_page(settings: Settings, conn: sqlite3.Connection, today: str,
                      query: dict[str, list[str]]) -> tuple[str, str, str]:
    """The board: a column per open stage, a collapsed lane for closed ones, general tasks.
    ?app=ID opens and highlights that card (an unknown id is ignored)."""
    apps = db.board_applications(conn)
    app_param = (query.get("app") or [""])[0]
    focus = int(app_param) if app_param.isdigit() else None
    tasks = db.app_tasks(conn)
    counts = db.comment_counts(conn)
    found = db.research_view(conn)

    def lane(stage: str) -> str:
        cards = [_app_card(conn, a, tasks.get(a["id"], []), counts.get(a["id"], 0), today, a["id"] == focus,
                           found.get(a["id"]))
                 for a in apps if a["status"] == stage]
        body = "".join(cards) or '<p class="lane-empty">–</p>'
        return (f'<section class="lane" aria-label="{h(_status_label(stage))}"><h2>{h(_status_label(stage))}'
                f'<span class="count">{len(cards)}</span></h2>{body}</section>')

    closed = [a for a in apps if a["status"] in CLOSED_LANE]
    summary = " · ".join(f"{_status_label(s)} {n}" for s in CLOSED_LANE
                         if (n := sum(1 for a in closed if a["status"] == s)))
    open_closed = " open" if any(a["id"] == focus for a in closed) else ""
    board = (
        f'<div class="board">{"".join(lane(s) for s in BOARD_STAGES)}</div>'
        f'<details class="closed-lane"{open_closed}><summary>Closed <span class="count">{len(closed)}</span>'
        f'<span class="muted">{h(summary)}</span></summary>'
        f'<div class="board board-closed">{"".join(lane(s) for s in CLOSED_LANE)}</div></details>'
    )
    if not apps:
        board = _empty("No applications yet.", "Mark a job Applied or Interested on Job Offers, or use /applied.")
    open_count = len(apps) - len(closed)
    content = _card("Pipeline", board, count=open_count, cls="card-board") + _general_tasks(conn, tasks.get(None, []), today)
    return "Applications", f"{open_count} open · {len(closed)} closed · {today}", content


def _app_card(conn: sqlite3.Connection, app: dict, tasks: list[dict], comments: int, today: str,
              focus: bool, found: dict | None = None) -> str:
    """One application: a <details> whose summary shows company, role, days in stage, next task,
    comment count and research state; opened, it holds the tasks, research and CV fit, Move to…,
    a comment box and the history."""
    found = found or {"profile": None, "fit": None, "open": set()}
    days = (date.fromisoformat(today) - date.fromisoformat(app["since"])).days
    age = "new today" if days <= 0 else f"{days} day{'s' if days != 1 else ''} in stage"
    nxt = tasks[0] if tasks else None
    next_line = (f'<span class="app-next">{_due(nxt["due_date"], today)}'
                 f'<span class="app-next-title" title="{h(nxt["title"])}">{h(nxt["title"])}</span></span>' if nxt
                 else '<span class="app-next muted">No open tasks</span>')
    chip = (f'<span class="app-comments" title="comments">{_icon(COMMENT_ICON)}{comments}</span>'
            if comments else "")
    tier = f'<span class="app-tier">Tier {h(app["tier"])}</span> · ' if app["tier"] else ""
    summary = (
        f'<summary><strong class="app-head" title="{h(app["company"])}">{h(app["company"])}</strong>'
        f'<span class="app-role" title="{h(app["role"])}">{h(app["role"])}</span>'
        f'<span class="app-meta"><span>{tier}{h(age)}</span>{chip}</span>{next_line}'
        f'{_research_chips(found)}</summary>'
    )
    here = _here(app["id"])
    keep = f'<input type="hidden" name="app_id" value="{h(app["id"])}">'
    rows = "".join(_task_row(t, today, here, kind=False) for t in tasks)
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
    stages = "".join(
        f'<option value="{s}"{" disabled" if s == app["status"] else ""}>{h(_status_label(s))}</option>'
        for s in (*BOARD_STAGES, *CLOSED_LANE))
    move = (
        f'<form method="post" action="/applications/move" class="move" '
        f'data-interview-stages="{" ".join(db.INTERVIEW_STAGES)}">{keep}'
        f'<select name="status" required aria-label="Move to"><option value="">Move to…</option>{stages}</select>'
        '<label class="interview-date">Interview date <input type="date" name="interview_date"></label>'
        f'<input name="comment" maxlength="{MAX_COMMENT}" placeholder="Comment (optional)" aria-label="Comment with the move">'
        + _button("Move", size="sm") + "</form>"
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
    link = (f'<p class="app-link">{_link(app["link"], "Job posting ↗")}</p>'
            if app["link"] and urlsplit(app["link"]).scheme in ("http", "https") else "")
    body = (
        f'<div class="app-body">{link}<h3>Tasks</h3>{task_list}{add}'
        f'<h3>Research and CV fit</h3>{_research_section(app, found)}<h3>Move</h3>{move}'
        f'<h3>Comments and history</h3>{comment}'
        + (f'<ul class="timeline">{history}</ul>' if history else "")
        + "</div>"
    )
    css = "app-card is-focus" if focus else "app-card"
    return (f'<article class="{css}" id="app-{h(app["id"])}"><details{" open" if focus else ""}>'
            f"{summary}{body}</details></article>")


def _research_chips(found: dict) -> str:
    """Small state chips for the collapsed card: pending requests and the fit score."""
    chips = [f'<span class="chip chip-pending">{h(db.REQUEST_LABELS[k])} pending</span>'
             for k in db.REQUEST_KINDS if k in found["open"]]
    if found["fit"] and "cv_fit" not in found["open"]:
        chips.append(f'<span class="chip">CV fit {h(found["fit"]["score"])}/5</span>')
    return f'<span class="app-chips">{"".join(chips)}</span>' if chips else ""


def _research_button(app_id: int, kind: str, has_result: bool, pending: bool) -> str:
    """Research / CV fit: queues the request; "… pending" (disabled) while queued; Redo once done."""
    if pending:
        return f'<button class="btn btn-ghost btn-sm" disabled>{h(db.REQUEST_LABELS[kind])} pending</button>'
    label = "Redo" if has_result else {"research": "Research", "cv_fit": "CV fit"}[kind]
    return ('<form method="post" action="/applications/research" class="inline">'
            f'<input type="hidden" name="app_id" value="{h(app_id)}"><input type="hidden" name="kind" value="{h(kind)}">'
            + _button(label, "secondary", "sm") + "</form>")


def _open_link(app_id: int, kind: str) -> str:
    href = "/research/file?" + urlencode([("app", app_id), ("kind", kind)])
    return f'<a class="btn btn-ghost btn-sm" href="{h(href)}">Open</a>'


def _research_section(app: dict, found: dict) -> str:
    """Company research and CV fit summaries with their buttons, and the tier form."""
    profile, fit = found["profile"], found["fit"]
    head = '<div class="research-head"><strong>Company</strong>'
    if profile:
        head += f' <span class="mono muted">{h(profile["researched_at"])}</span>'
    head += ('<span class="research-actions">' + (_open_link(app["id"], "research") if profile else "")
             + _research_button(app["id"], "research", bool(profile), "research" in found["open"]) + "</span></div>")
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
    elif "research" not in found["open"]:
        company += '<p class="muted">Not researched yet.</p>'

    fit_head = '<div class="research-head"><strong>CV fit</strong>'
    if fit:
        tone = "s9" if fit["score"] >= 4 else ("s6" if fit["score"] == 3 else "s3")
        fit_head += " " + _badge(f'{fit["score"]}/5', tone)
        fit_head += f' <span class="mono muted">{h(fit["checked_at"])}</span>'
    fit_head += ('<span class="research-actions">' + (_open_link(app["id"], "cv_fit") if fit else "")
                 + _research_button(app["id"], "cv_fit", bool(fit), "cv_fit" in found["open"]) + "</span></div>")
    fit_html = fit_head
    if fit and fit["top_gaps"]:
        fit_html += '<ul class="gaps">' + "".join(f"<li>{h(g)}</li>" for g in fit["top_gaps"]) + "</ul>"
    elif not fit and "cv_fit" not in found["open"]:
        fit_html += '<p class="muted">Not checked yet.</p>'

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


def _task_row(task: dict, today: str, here: str, kind: bool = True) -> str:
    """An open task in General tasks (due, title, kind, Done) or on a card (no kind, to fit).
    A plain post returns to `here` (a URL built from database ids)."""
    done = (
        '<form method="post" action="/tasks/done" class="inline" data-ajax="done">'
        f'<input type="hidden" name="task_id" value="{h(task["id"])}">'
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
