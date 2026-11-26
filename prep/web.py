"""Local dashboard over prep.db: Applications (the landing page, whose left column switches
between the pipeline and New offers from getjo), Learn, Evals (getjo's eval catalogue and results files,
read only, prep/evals.py), Usage & cost (Claude Code tokens, cost and response time from its
transcripts, read only, prep/usage.py, with cost and time per worker job), Pipeline stats (the search's
numbers) and Run history (/runs, from the Workers sidebar), plus a
read-only view of the agent's research and CV fit files.

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
requests (db.agent_requests) for the /research skill, and the Applications Assistant box only stores the user's
message (db.agent_messages) for the /ask skill. When assistant_task is set,
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
import time
from collections.abc import Callable, Mapping
from contextlib import closing
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit

from prep import (assistant, charts, cvcheck, cveval, cvfile, cvpdf, db, evals, explainers, getjo, learn, learnnotes,
                  research, stats, usage, workers)
from prep.md import render_markdown
from prep.config import ConfigError, Settings, load_settings

log = logging.getLogger("prep.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/vim.js": ("vim.js", "text/javascript; charset=utf-8"),
    "/static/explainer.css": ("explainer.css", "text/css; charset=utf-8"),
    "/static/explainer.js": ("explainer.js", "text/javascript; charset=utf-8"),
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
    ("/", "Applications", '<rect x="3" y="3" width="5" height="18" rx="1.5"/><rect x="10" y="3" width="5" height="12" rx="1.5"/>'
                          '<rect x="17" y="3" width="4" height="8" rx="1.5"/>'),
    ("/learn", "Learn", '<path d="M4 5.5A1.5 1.5 0 0 1 5.5 4H11v15H5.5A1.5 1.5 0 0 0 4 20.5z"/>'
                        '<path d="M20 5.5A1.5 1.5 0 0 0 18.5 4H13v15h5.5a1.5 1.5 0 0 1 1.5 1.5z"/>'),
    ("/evals", "Evals", '<path d="M9 3h6"/><path d="M10 3v6l-5.6 9.4A1.7 1.7 0 0 0 5.9 21h12.2a1.7 1.7 0 0 0 1.5-2.6L14 9V3"/>'
                        '<path d="M7.5 15h9"/>'),
    ("/usage", "Usage & cost", '<circle cx="12" cy="12" r="9"/><path d="M12 6.5v11"/>'
                               '<path d="M15 9.3c-.5-.9-1.6-1.5-3-1.5-1.7 0-3 .9-3 2.1s1.3 1.8 3 2.1 3 .9 3 2.1-1.3 2.1-3 2.1'
                               'c-1.4 0-2.5-.6-3-1.5"/>'),
    ("/stats", "Pipeline stats", '<path d="M3 21h18"/><rect x="4" y="11" width="4" height="7" rx="1"/>'
                                 '<rect x="10" y="5" width="4" height="13" rx="1"/><rect x="16" y="14" width="4" height="4" rx="1"/>'),
)
EMPTY_ICON = ('<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.5 5.1 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6'
              'l-3.5-6.9A2 2 0 0 0 16.8 4H7.2a2 2 0 0 0-1.7 1.1z"/>')

# Fixed messages shown after an action; the URL carries only the key, never free text.
MESSAGES = {
    "applied": "Recorded as applied. Its panel shows the first tasks.",
    "interested": "Interested · research queued. It is in Pipeline → Interested; the assistant researches the company "
                  "and checks the CV fit.",
    "uninterested": "Interested undone; the job is back in New offers.",
    "applied_today": "Marked as applied today. Chase if no reply is due in 7 days.",
    "unapplied": "Back to Interested; the Applied tasks are gone.",
    "app_skipped": "Skipped and closed. New offers lists it under Show skipped.",
    "skipped": "Skipped. It no longer appears in New offers.",
    "unskipped": "Skip undone; the job is back in New offers.",
    "task_done": "Task marked done.",
    "task_reopened": "Task reopened.",
    "research_queued": "Queued for the Applications Assistant. It runs in the next Claude session (/research).",
    "message_sent": "Sent to the Applications Assistant. Its reply shows in the thread.",
    "mock_started": "Mock interview asked for. The assistant's first question shows in the Applications Assistant box.",
    "draft_requested": "Draft asked for. It shows in the Applications Assistant box with Copy; nothing is sent.",
    "notes_saved": "Notes saved.",
    "employer_saved": "Employer kind saved.",
    "cv_restored": "Restored. The CV it replaced is kept as a version.",
    "brief_queued": "Brief queued. The assistant writes it from the job description; it shows here.",
    "sel_limit": f"Up to {db.MAX_TARGETS} at a time: untick one first.",
    "card_done": "Card done. Here is the next one to do.",
    "card_todo": "Kept as to do.",
    "track_done": "Every card in that track is done.",
    "source_done": "Source ticked done.",
    "source_todo": "Source unticked.",
    "learn_notes_saved": "Note saved.",
    "learn_sent": "Sent to the Learn Assistant. Its reply shows here.",
    "learn_check_sent": "Your answer went to the Learn Assistant. Its feedback shows in the Learn Assistant box.",
    "learn_card_asked": "Asked for a card. The Learn Assistant drafts it; say yes to save it.",
    "learn_note_added": "Added to the notes.",
    "resent": "Sent to the Applications Assistant again.",
    "cancelled": "Cancelled. The assistant skips it; a reply already on its way shows as after cancel.",
    "cancel_late": "It was no longer waiting: answered or cancelled meanwhile.",
    "stopped": "Stopped. Waiting messages and this job's queued work are cancelled; ask or queue them again any time.",
    "learn_resent": "Sent to the Learn Assistant again.",
    "learn_stopped": "Stopped. Waiting Learn Assistant messages are cancelled; ask again any time.",
    "learn_cancelled": "Cancelled. The assistant skips it; a reply already on its way shows as after cancel.",
    "learn_quote_noted": "Added to this card's notes.",
    "learn_explain_asked": "Asked the Learn Assistant for an explainer. It shows under the card when it is written.",
    "learn_refresh_asked": "Asked the Learn Assistant to refresh the explainer.",
    "worker_stopped": "Stopped. That worker's item is cancelled; queue it again any time.",
    "worker_requeued": "Stopped. The item is back in the queue for the next free worker.",
    "worker_idle": "That worker was not holding anything (it finished meanwhile).",
}

SECURITY_HEADERS = [
    ("Content-Security-Policy",
     "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self'; "
     "frame-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"),
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
# One theme per page (defined in style.css). Spare themes for new pages: dusty-blue,
# slate-indigo, dusty-violet, mauve-rose, terracotta.
PAGE_THEMES = {"/": "ochre", "/usage": "sage", "/stats": "dusty-blue", "/learn": "dusty-violet", "/evals": "eucalyptus"}
# Closed outcomes take s1-s3, then to_apply and the funnel
# from dark to light (offer is the lightest).
STATUS_SHADES = {
    "rejected": "s1", "withdrawn": "s2", "no_response": "s3", "skipped": "s2", "to_apply": "s4", "applied": "s5",
    "recruiter_call": "s6", "technical": "s7", "system_design": "s8", "final": "s9", "offer": "s10",
}
TIER_SHADES = {"A": "s9", "B": "s7", "C": "s5", "unset": "s3"}
KIND_SHADES = {"apply": "s9", "prep": "s7", "drill": "s5", "admin": "s3"}


class BadRequest(Exception):
    """A form value is missing or invalid; the message is shown on a 400 page."""


class NotesStale(BadRequest):
    """A Notes save over notes that changed meanwhile. `html` is the Notes card again, editing the
    user's text against the current notes (the JSON reply's "html" and "stamp"; the 400 page's form)."""

    def __init__(self, message: str, html: str, stamp: str, extra: dict | None = None) -> None:
        super().__init__(message)
        self.html, self.stamp, self.extra = html, stamp, extra or {}


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
        stale = e if isinstance(e, NotesStale) else None
        if method == "POST" and wants_json(headers):
            return _json(400, {"ok": False, "message": str(e), **({"html": stale.html, "stamp": stale.stamp, **stale.extra} if stale else {})})
        content = (_banner(str(e), "warning") + (stale.html if stale else "")
                   + '<p><a class="btn btn-secondary" href="/">Back to Applications</a></p>')
        return _page(400, "Not saved", "The form was not saved.", content, "")


def _get(settings: Settings, path: str, query: dict, today: str) -> Response:
    if path in STATIC_FILES:
        name, content_type = STATIC_FILES[path]
        return 200, [("Content-Type", content_type), *SECURITY_HEADERS], (STATIC_DIR / name).read_bytes()
    moved = _moved(path, query)
    if moved:  # old bookmarks (and their query) keep working
        return 303, [("Location", moved)], b""
    if path == "/applications/thread":
        return thread_json(settings, query, today)
    if path == "/research/pdf":
        return tailored_pdf(settings, query, today)
    if path == "/learn/notes":
        return learn_notes_json(settings, query, today)
    if path == "/learn/thread":
        return learn_thread_json(settings, query, today)
    if path == "/learn/explainer":
        return explainer_page(settings, query)
    if path == "/workers.json":
        return workers_json(settings, query, today)
    if path.startswith("/evals/report/"):
        return eval_report(settings, path)
    pages = {
        "/": lambda s, c, t: home_page(s, c, t, query),
        "/usage": lambda s, c, t: usage_page(s, c, t, query),
        "/stats": lambda s, c, t: stats_page(s, c, t, query),
        "/offers/skip": lambda s, c, t: skip_page(s, c, t, query),
        "/applications/skip": lambda s, c, t: app_skip_page(s, c, t, query),
        "/research/file": lambda s, c, t: research_file_page(s, c, t, query),
        "/learn": lambda s, c, t: learn_page(s, c, t, query),
        "/runs": lambda s, c, t: runs_page(s, c, t, query),
        "/evals": lambda s, c, t: evals_page(s, c, t, query),
        "/evals/run": lambda s, c, t: eval_run_page(s, c, t, query),
    }
    if path not in pages:
        return _text(404, "Not found.")
    message = MESSAGES.get((query.get("msg") or [""])[0])
    with closing(_connect(settings, today)) as conn:
        try:
            title, subtitle, content, *head = pages[path](settings, conn, today)
        except NotFound:
            return _text(404, "Not found.")
        offers = new_offer_count(settings, conn, _now(today), offer_score(query))
        cards_todo = learn.counts(conn)["todo"]
        side = workers_section(settings, conn, path)
    notice = f'<div class="notice" role="status" data-flash>{h(message)}</div>' if message else ""
    active = ("/evals" if path.startswith("/evals") else path if path in ("/learn", "/usage", "/stats")
              else "" if path == "/runs" else "/")
    return _page(200, title, subtitle, notice + content, active, offers, head[0] if head else "", cards_todo, side)


OLD_OFFERS = ("/offers", "/matches")  # the Job Offers page, now the New offers view of Applications


def _moved(path: str, query: dict[str, list[str]]) -> str | None:
    """Where an old page lives now, or None: Metrics was split into /usage (its Usage tab, with project and
    days) and /stats (its Applications tab, earlier the Dashboard and /progress), its Run history moved to
    /runs, Applications and /tasks moved to /, and Job Offers to /?view=offers with its filters."""
    if path in ("/progress", "/dashboard"):
        return "/stats"
    if path == "/metrics":
        if (query.get("tab") or [""])[0] == "applications":
            return "/stats"
        return usage_url(usage_filters(query))
    if path == "/metrics/runs":
        return "/runs" + (f"?{urlencode(query, doseq=True)}" if query else "")
    if path in ("/applications", "/tasks"):
        return "/" + (f"?{urlencode(query, doseq=True)}" if query else "")
    if path in OLD_OFFERS:
        pairs = _old_offers_query(query)
        return "/" + (f"?{urlencode(pairs)}" if pairs else "")
    return None


def _old_offers_query(query: dict[str, list[str]]) -> list[tuple[str, str]]:
    """An old Job Offers query as the New offers view's: score and loc kept, company or role as the
    search, show=skipped as Show skipped. show=interested|applied lists those jobs in the pipeline."""
    def first(key: str) -> str:
        return (query.get(key) or [""])[0].strip()

    show = first("show")
    if show in ("interested", "applied"):
        return [("stage", show)]
    pairs = [("view", "offers")]
    score = first("score")
    if score.isdigit() and 1 <= int(score) <= 10:
        pairs.append(("score", score))
    pairs += [("loc", loc) for loc in getjo.LOCATIONS if loc in query.get("loc", [])]
    search = first("q") or first("company") or first("role")
    if search:
        pairs.append(("q", search))
    if show == "skipped":
        pairs.append(("skipped", "1"))
    return pairs


def _post(settings: Settings, path: str, form: dict[str, str], today: str, as_json: bool) -> Response:
    if path not in ACTIONS:
        return _text(404, "Not found.")
    action, page, msg = ACTIONS[path]
    with closing(_connect(settings, today)) as conn:
        mark = db.last_request_id(conn)
        try:
            extra = action(settings, conn, form, today) or {}
            msg = extra.pop("msg", msg)  # an action may pick another of MESSAGES (Learn: track done)
        except (ValueError, LookupError) as e:
            raise BadRequest(str(e)) from e
        # Interested, Move to Interested or a Research / CV fit click may have queued agent work.
        assistant.notify_in_background(settings, assistant.route_requests(settings, conn, db.requests_since(conn, mark)))
    if "redirect" in extra:  # the ticked jobs (sel in the form's return) stay ticked where it goes next
        extra["redirect"] = _with_sel(extra["redirect"], form.get("return"))
    if as_json:
        return _json(200, {"ok": True, "message": MESSAGES[msg], **extra})
    if "redirect" in extra:  # built by the action from database ids, never from the form
        target = urlsplit(extra["redirect"])
        back = urlsplit(_return_to(form.get("return"), target.path))  # an old /applications form counts too
        if form.get("return") and parse_qs(back.query).get("app") == parse_qs(target.query).get("app"):
            # the side panel's own URL with its view and stage filter, re-encoded by _return_to
            return 303, [("Location", _return_to(form["return"], target.path, msg))], b""
        sep = "&" if "?" in extra["redirect"] else "?"
        return 303, [("Location", f"{extra['redirect']}{sep}{urlencode([('msg', msg)])}")], b""
    return 303, [("Location", _return_to(form.get("return"), page, msg))], b""


def _with_sel(url: str, back: str | None) -> str:
    """`url` (a local URL built from database ids) with the `sel` values of the `return` URL added."""
    sel = [v for k, v in parse_qsl(urlsplit(back or "").query) if k == "sel"]
    if not sel:
        return url
    parts = urlsplit(url)
    pairs = [(k, v) for k, v in parse_qsl(parts.query) if k != "sel"] + [("sel", v) for v in sel]
    return f"{parts.path}?{urlencode(pairs)}"


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
    elif not url.scheme and not url.netloc and page == "/" and url.path in (*OLD_OFFERS, "/applications", "/tasks"):
        moved = urlsplit(_moved(url.path, parse_qs(url.query)) or "")  # a form from before the merge
        pairs = [(k, v) for k, v in parse_qsl(moved.query) if k != "msg"]
    query = urlencode([*pairs, *([("msg", msg)] if msg else [])])
    return f"{page}?{query}" if query else page


class Form(dict):
    """A posted form: the first value of each field (stripped), plus `all`, every value of each field
    (the Applications Assistant box's repeated `about`)."""
    all: dict[str, list[str]]


def _form(body: bytes) -> Form:
    if len(body) > MAX_BODY:
        raise BadRequest("Form too large.")
    try:
        fields = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError as e:
        raise BadRequest("Form is not valid UTF-8.") from e
    form = Form({k: v[0].strip() for k, v in fields.items()})
    form.all = {k: [x.strip() for x in v] for k, v in fields.items()}
    return form


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
    """Interested: track the getjo job as to_apply (which queues the agent's research) and open its
    application panel in the pipeline. The JSON reply says whether Undo may delete it (`created`)."""
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
    return {"redirect": _here(app_id), "app_id": app_id, "created": created}


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
        raise BadRequest("It is no longer Interested; ask the assistant to change the stage.")
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
    its getjo job skipped, as an offer's Skip does (db.skip_application)."""
    app_id = _app_id(conn, form)
    reason = form.get("reason")
    if not reason:
        raise BadRequest("Pick a reason for skipping.")
    if len(form.get("comment", "")) > MAX_COMMENT:
        raise BadRequest(f"The comment is longer than {MAX_COMMENT} characters.")
    db.skip_application(conn, app_id, reason, _optional(form, "comment"), today)
    log.info("application %s skipped from the panel (reason %s)", app_id, reason)  # never the comment
    return {"redirect": _here(app_id)}


def send_message(settings: Settings, conn: sqlite3.Connection, form: Form, today: str) -> dict:
    """The Applications Assistant box: store the user's message (author is always user, whatever the form says), about
    the ticked targets (repeated `about`: "a:<app id>", "j:<getjo job id>", "cv"; none = the whole
    search, one = that one, 2-4 = a comparison), and ping the Applications Assistant with
    `/ask <app | job | cv | compare | general> <message>`, ids only, in the background. An older form's
    `app_id` / `job_id` / `topic` still work. The message stays pending until /ask replies through the
    CLI. The JSON reply is the thread."""
    if "about_set" in form:
        return _send_about(settings, conn, form)
    app_id = _int_field(form, "app_id")
    if app_id is not None and db.get_application(conn, app_id) is None:
        raise BadRequest(f"No application with id {app_id}.")
    job_id = None if app_id is not None else _optional(form, "job_id")
    topic = None if app_id is not None or job_id else _optional(form, "topic")  # "cv": the My CV panel
    if topic is not None and topic not in db.MESSAGE_TOPICS:
        raise BadRequest("Unknown message topic.")
    body = form.get("body", "")
    if not body:
        raise BadRequest("Type a question or paste an email first.")
    if job_id and db.find_by_job(conn, job_id) is None and job_id not in db.job_decisions(conn):
        _getjo_job(settings, {"job_id": job_id})  # an unknown job is refused
    msg_id = db.add_message(conn, app_id, body, job_id, topic)  # checks the length after normalising line breaks
    about, job = db.message_subject(conn, msg_id)
    log.info("message %s added (application %s, job %s, topic %s) from the dashboard", msg_id, about, bool(job),
             topic)  # never the text
    assistant.notify_message_in_background(settings, about, msg_id, job=about is None and job is not None, topic=topic)
    return {"html": _thread(conn, settings, *_panel_ctx(conn, form))}


def _about_target(settings: Settings, conn: sqlite3.Connection, key: str) -> tuple:
    """(application_id, getjo_job_id, topic) for a ticked key; an unknown application or job is refused."""
    if key == "cv":
        return None, None, "cv"
    if key.startswith("a:") and key[2:].isdigit():
        if db.get_application(conn, int(key[2:])) is None:
            raise BadRequest(f"No application with id {key[2:]}.")
        return int(key[2:]), None, None
    if key.startswith("j:") and len(key) > 2:
        job_id = key[2:]
        tracked = db.find_by_job(conn, job_id)
        if tracked:
            return tracked["id"], None, None
        if job_id not in db.job_decisions(conn):
            _getjo_job(settings, {"job_id": job_id})  # an unknown job is refused
        return None, job_id, None
    raise BadRequest("Unknown target.")


def _send_about(settings: Settings, conn: sqlite3.Connection, form: Form) -> dict:
    keys = list(dict.fromkeys(k for k in form.all.get("about", []) if k))
    if len(keys) > db.MAX_TARGETS:
        raise BadRequest(f"Ask about at most {db.MAX_TARGETS} at a time.")
    body = form.get("body", "")
    if not body:
        raise BadRequest("Type a question or paste an email first.")
    targets = list(dict.fromkeys(_about_target(settings, conn, k) for k in keys))
    if len(targets) > 1:
        msg_id = db.add_compare(conn, body, targets)
        log.info("message %s added comparing %d targets from the dashboard", msg_id, len(targets))  # never the text
        assistant.notify_message_in_background(settings, None, msg_id, topic=db.COMPARE)
        return {"html": _thread(conn, settings, *_panel_ctx(conn, form))}
    app_id, job_id, topic = targets[0] if targets else (None, None, None)
    msg_id = db.add_message(conn, app_id, body, job_id, topic)
    about, job = db.message_subject(conn, msg_id)
    log.info("message %s added (application %s, job %s, topic %s) from the dashboard", msg_id, about, bool(job),
             topic)  # never the text
    assistant.notify_message_in_background(settings, about, msg_id, job=about is None and job is not None, topic=topic)
    return {"html": _thread(conn, settings, *_panel_ctx(conn, form))}


def start_mock(settings: Settings, conn: sqlite3.Connection, form: Form, today: str) -> dict:
    """The panel's Mock interview: store the fixed start message (db.MOCK_START, built here from the
    stage, never free text) about the application and ping `/ask <app> <message>` like any message.
    The agent runs the mock in the Applications Assistant box (/mock). The JSON reply is the thread."""
    app_id = _app_id(conn, form)
    if db.get_application(conn, app_id)["status"] in db.CLOSED_STATUSES:
        raise BadRequest("This application is closed; a mock is for an open one.")
    stage = form.get("stage") or ""
    if stage not in db.INTERVIEW_STAGES:
        raise BadRequest("Pick an interview stage for the mock.")
    msg_id = db.add_message(conn, app_id, db.MOCK_START.format(label=db.status_label(stage), stage=stage))
    log.info("mock interview (%s) asked for on application %s, message %s", stage, app_id, msg_id)
    assistant.notify_message_in_background(settings, app_id, msg_id)
    return {"redirect": _here(app_id), "html": _thread(conn, settings, *_panel_ctx(conn, form))}


def request_draft(settings: Settings, conn: sqlite3.Connection, form: Form, today: str) -> dict:
    """Draft a reply: store a draft request (db.add_draft_request: what to write, the channel, the pasted
    message and the user's notes) about the application (`app_id`), the job offer not yet tracked
    (`job_id`) or the whole search (neither), and ping `/reply …` with its id in the background: the comms
    worker when one is set, else the Applications Assistant. Nothing is ever sent to the recruiter; the
    draft comes back in the box with Copy. The JSON reply is the thread."""
    app_id = _int_field(form, "app_id")
    if app_id is not None and db.get_application(conn, app_id) is None:
        raise BadRequest(f"No application with id {app_id}.")
    job_id = None if app_id is not None else _optional(form, "job_id")
    if job_id and db.find_by_job(conn, job_id) is None and job_id not in db.job_decisions(conn):
        _getjo_job(settings, {"job_id": job_id})  # an unknown job is refused
    kind, channel = form.get("comms") or "reply", form.get("channel") or ""
    if kind not in db.COMMS_KINDS or channel not in db.CHANNELS:
        raise BadRequest("Pick what to write and the channel (LinkedIn or Email).")
    if kind == "reply" and not form.get("pasted", "").strip():
        raise BadRequest("Paste the recruiter's message to reply to.")
    msg_id = db.add_draft_request(conn, app_id, kind, channel, form.get("pasted", ""), form.get("notes", ""), job_id)
    about, job = db.message_subject(conn, msg_id)
    log.info("draft request %s (%s, %s) added (application %s, job %s)", msg_id, kind, channel, about,
             bool(job))  # never the text
    seq = assistant.pick(settings, conn, "comms")
    if seq:
        db.assign_message(conn, msg_id, seq)
    assistant.notify_message_in_background(settings, about, msg_id, job=about is None and job is not None,
                                           kind="comms", seq=seq)
    return {"html": _thread(conn, settings, *_panel_ctx(conn, form))}


def _msg_id(form: dict[str, str]) -> int:
    msg_id = _int_field(form, "msg_id")
    if msg_id is None:
        raise BadRequest("Which message? (msg_id)")
    return msg_id


def _handler_name(settings: Settings, kind: str) -> str:
    """Who answers a message: the Comms worker for a draft request when one is set, else the Applications Assistant."""
    return assistant.target(settings, "comms")[1] if kind == "comms" else assistant.APPLICATIONS_ASSISTANT


def _unreachable(name: str, seq: int | None) -> BadRequest:
    return BadRequest(f"Couldn't reach the {name} (seq {seq}). Is its task running?")


def resend_message(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    """Resend, on a message still waiting in the Applications Assistant box: the same message goes
    back to pending (a stale or stuck claim dropped) and the assistant is pinged again with its id, at
    once (not in the background) so a failed ping is reported. No new message row."""
    msg = db.get_message(conn, _msg_id(form)) or {}
    kind = "comms" if msg.get("comms") else "ask"
    seq = assistant.message_target(settings, kind)
    if not seq:
        raise BadRequest("No Applications Assistant task is set (assistant_task): run /ask in a Claude Code session.")
    msg = db.reopen_message(conn, msg.get("id", 0))
    about, job = db.message_subject(conn, msg["id"])
    log.info("message %s sent to the assistant again", msg["id"])
    chosen = None
    if kind == "comms":  # with several comms workers, another one than the one that stalled when it can
        chosen = assistant.pick(settings, conn, "comms", exclude=(msg["worker"],) if msg.get("worker") else ())
        if chosen:
            db.assign_message(conn, msg["id"], chosen)
    if not assistant.notify_message(settings, about, msg["id"], job=about is None and job is not None,
                                    topic=msg["topic"], kind=kind, seq=chosen):
        raise _unreachable(_handler_name(settings, kind), chosen or seq)
    return {"html": _thread(conn, settings, *_panel_ctx(conn, form))}


def cancel_message(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    """Cancel, on a message still waiting in the Applications Assistant box: marked cancelled, kept. One
    answered (or cancelled) since the page was drawn is not an error: the fresh conversation shows it."""
    msg_id = _msg_id(form)
    try:
        db.cancel_message(conn, msg_id)
    except ValueError:
        log.info("message %s was no longer waiting; nothing cancelled", msg_id)
        return {"msg": "cancel_late", "html": _thread(conn, settings, *_panel_ctx(conn, form))}
    log.info("message %s cancelled", msg_id)
    return {"html": _thread(conn, settings, *_panel_ctx(conn, form))}


def stop_waiting(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    """Stop, on the Applications Assistant box's waiting line: every Applications message still waiting
    is cancelled (never a Learn one), and with the open panel (panel_app / panel_job) its queued agent
    requests and job brief too. Rows are kept, marked cancelled. Nothing is sent to the assistant."""
    app_id, job_id = _panel_ctx(conn, form)
    msgs = db.cancel_waiting_messages(conn)
    reqs = db.cancel_requests(conn, app_id) if app_id is not None else 0
    brief_job = (db.get_application(conn, app_id) or {}).get("getjo_job_id") if app_id is not None else job_id
    briefs = db.cancel_brief(conn, brief_job) if brief_job else 0
    log.info("stop: %d message(s), %d request(s), %d brief(s) cancelled", len(msgs), reqs, briefs)
    return {"html": _thread(conn, settings, app_id, job_id)}


def _panel_ctx(conn: sqlite3.Connection, form: dict[str, str]) -> tuple[int | None, str | None]:
    """The side panel open when a box form was sent (panel_app or panel_job, added by the page script):
    (application id, None) or (None, getjo job id), or (None, None). An unknown application is ignored."""
    raw = form.get("panel_app") or ""
    if raw.isdigit() and db.get_application(conn, int(raw)) is not None:
        return int(raw), None
    return None, (form.get("panel_job") or None)


def queue_brief(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> None:
    """Write brief / Redo brief: queue a job brief for the agent (Redo when one exists) and ping the
    Applications Assistant. A second click while it is open changes nothing."""
    job_id = form.get("job_id")
    if not job_id:
        raise BadRequest("job_id is missing.")
    if db.find_by_job(conn, job_id) is None and job_id not in db.job_decisions(conn):
        _getjo_job(settings, form)  # an unknown job is refused
    created = db.queue_brief(conn, job_id, force=bool(db.job_briefs(conn, {job_id})))
    log.info("job brief %s from the dashboard", "queued" if created else "already queued")
    if created:
        seq = assistant.pick(settings, conn, "research")
        if seq:
            db.assign_briefs(conn, [job_id], seq)
        assistant.notify_briefs_in_background(settings, 1, seq)


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


def set_employer_kind(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    """The panel's employer select: product, client, or "" for unknown (stored as NULL)."""
    app_id = _app_id(conn, form)
    kind = form.get("employer_kind") or ""
    if kind and kind not in db.EMPLOYER_KINDS:
        raise BadRequest("Pick product, client or unknown.")
    db.update_application(conn, app_id, employer_kind=kind or None)
    log.info("employer kind of application %s set to %s from the dashboard", app_id, kind or "unknown")
    return {"redirect": _here(app_id)}


def save_notes(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    """The Notes card's Save: replace the notes (empty clears them) unless they changed since the form
    was opened (its `stamp`); then the user's text comes back in the editor beside the current notes."""
    app_id = _app_id(conn, form)
    text = form.get("notes", "")
    try:
        db.save_notes(conn, app_id, text, form.get("stamp", ""))
    except db.NotesChanged as e:
        current = db.get_notes(conn, app_id)
        log.info("notes save for application %s refused: they changed meanwhile", app_id)  # never the text
        raise NotesStale(str(e), _notes_card(app_id, current, _here(app_id), draft=text, alert=str(e)),
                         current["stamp"]) from e
    log.info("notes %s for application %s from the dashboard", "saved" if text else "cleared", app_id)
    return {"redirect": _here(app_id), "html": _notes_card(app_id, db.get_notes(conn, app_id), _here(app_id))}


def restore_cv(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    """The My CV panel's Restore: make a saved version cv.md again (cvfile.restore: the text it
    replaces is kept as a version, which the toast's Undo restores)."""
    version = _int_field(form, "version")
    if version is None:
        raise BadRequest("version is missing.")
    if not settings.private_dir:
        raise BadRequest("private_dir is not set, so there is no CV.")
    result = cvfile.restore(conn, settings.private_dir, version)
    log.info("master cv v%s restored from the panel as v%s", version, result["version"])
    return {"redirect": "/?cv=1", **result}


def _here(app_id: int | None) -> str:
    """The Applications page with a card open (redirect target built from a database id)."""
    return f"/?app={app_id}" if app_id is not None else "/"


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


def _card_id(form: dict[str, str]) -> int:
    card_id = _int_field(form, "card_id")
    if card_id is None:
        raise BadRequest("card_id is missing.")
    return card_id


def card_done(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    """The card's Done: mark it done, then open the track's next card to do (the grid when none is left)."""
    card_id = _card_id(form)
    learn.set_status(conn, card_id, "done")
    log.info("learn card %s done from the dashboard", card_id)
    nxt = learn.next_todo(conn, card_id)
    return {"redirect": "/learn", "msg": "track_done"} if nxt is None else {"redirect": f"/learn?card={nxt}"}


def card_undo(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    """The card's Not done yet: back to do (or kept so); the card stays open."""
    card_id = _card_id(form)
    learn.set_status(conn, card_id, "todo")
    log.info("learn card %s to do from the dashboard", card_id)
    return {"redirect": f"/learn?card={card_id}"}


def tick_source(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    """A Learn source's tick: done=1 ticks it, done=0 unticks it (the state wanted, so a second click
    or a stale page does no harm). The card's own Done / Not done yet is not touched."""
    card_id = _card_id(form)
    done = form.get("done") == "1"
    card = learn.set_source_done(conn, card_id, form.get("key", ""), done)
    log.info("learn card %s: a source %s from the dashboard", card_id, "ticked" if done else "unticked")
    return {"redirect": f"/learn?card={card_id}", "html": _learn_sources(conn, card),
            "msg": "source_done" if done else "source_todo"}


def _notes_key(conn: sqlite3.Connection, form: dict[str, str]) -> tuple[int, str]:
    """(card id, note key) from a Learn notes form: the card is the one open, the note "card" (its
    own), "all" (the general notebook) or "answer" (the card's Your answer). An unknown card is a
    LookupError."""
    card_id = _card_id(form)
    learn.get_card(conn, card_id)
    which = form.get("note") or "card"
    keys = {"card": str(card_id), learnnotes.ALL: learnnotes.ALL, "answer": learnnotes.answer_key(card_id)}
    if which not in keys:
        raise BadRequest("note must be card, all or answer.")
    return card_id, keys[which]


def save_learn_notes(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    """The Learn Notes editor's or Your answer's save (autosave, :w, Cmd-S, or Save without the
    script): replace the text unless it changed since the page loaded it (its `stamp`); then the box
    comes back with the user's text and the current text beside it."""
    card_id, key = _notes_key(conn, form)
    text = form.get("text", "")
    try:
        stamp = learnnotes.save(settings.private_dir, key, text, form.get("stamp", ""))
    except learnnotes.NotesChanged as e:
        current = learnnotes.read(settings.private_dir, key)
        log.info("learn note %s save refused: it changed meanwhile", learnnotes.label(key))  # never the text
        box = _answer_box if learnnotes.is_answer(key) else _notes_editor
        raise NotesStale(str(e), box(card_id, key, current, draft=text, alert=str(e)), current["stamp"],
                         {"text": current["text"]}) from e
    log.info("learn note %s saved from the dashboard", learnnotes.label(key))
    return {"redirect": _notes_url(card_id, key), "stamp": stamp}


def learn_notes_json(settings: Settings, query: dict[str, list[str]], today: str) -> Response:
    """GET /learn/notes?card=ID[&note=all]: the note's text and stamp, so an idle editor can pick up
    an agent's addition when the page is shown again."""
    raw = (query.get("card") or [""])[0]
    form = {"card_id": raw, "note": (query.get("note") or ["card"])[0]}
    try:
        with closing(db.connect(settings.prep_db)) as conn:
            _, key = _notes_key(conn, form)
        note = learnnotes.read(settings.private_dir, key)
    except (BadRequest, ValueError, LookupError) as e:
        return _json(404 if isinstance(e, LookupError) else 400, {"ok": False, "message": str(e)})
    return _json(200, {"ok": True, "text": note["text"], "stamp": note["stamp"]})


# --- Learn: the Learn Assistant -----------------------------------------------------------------
# Its messages are agent_messages with topic LEARN and the open card's id (db.add_learn_message); the
# Learn Assistant answers them (/learn-ask), pinged with ids only. Fixed messages (Check my answer, Want a
# card for this?) are built from ids here, never from the form's text.

def _learn_back(conn: sqlite3.Connection, form: dict[str, str]) -> tuple[int | None, str]:
    """The open card (form card_id; none: prep in general) and the page to go back to. An unknown card
    is a LookupError."""
    card_id = _int_field(form, "card_id")
    if card_id is not None:
        learn.get_card(conn, card_id)
    return card_id, f"/learn?card={card_id}" if card_id is not None else "/learn"


def _learn_ask(settings: Settings, conn: sqlite3.Connection, card_id: int | None, body: str, back: str) -> dict:
    msg_id = db.add_learn_message(conn, card_id, body)
    log.info("Learn Assistant message %s added on card %s", msg_id, card_id)  # never the text
    assistant.notify_learn_in_background(settings, card_id, msg_id)
    return {"redirect": back, "html": _learn_thread(conn, settings, card_id)}


def send_learn_message(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    """The Learn Assistant box's Send: the user's message, with the open card's id."""
    card_id, back = _learn_back(conn, form)
    body = form.get("body", "")
    if not body.strip():
        raise BadRequest("Type a question first.")
    quote = _quote(form)
    if quote:
        body = f"About this passage ({quote[1]}):\n> {quote[0]}\n\n{body}"
    return _learn_ask(settings, conn, card_id, body, back)


MAX_QUOTE = 1000  # characters of highlighted text sent with a message or added to the notes
QUOTE_SOURCES = {"card": "Card", "explainer": "Explainer"}


def _quote(form: dict[str, str]) -> tuple[str, str] | None:
    """The highlighted passage a Learn form carries (quote, quote_src card|explainer): its text on one
    line, cut to MAX_QUOTE, and where it came from; None without one."""
    text = " ".join(form.get("quote", "").split())
    if not text:
        return None
    return _clip(text, MAX_QUOTE), QUOTE_SOURCES.get(form.get("quote_src", ""), "Card")


def add_quote_to_notes(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    """The highlight box's Add to notes: the quote (as a Markdown quote, with where it came from) and
    the user's words under it, appended to the open card's notes under the day. No assistant."""
    card_id = _card_id(form)
    _, back = _learn_back(conn, form)
    quote = _quote(form)
    words = form.get("body", "").strip()
    if quote is None and not words:
        raise BadRequest("Select some text first.")
    text = (f"> {quote[0]} ({quote[1]})" if quote else "") + (f"\n\n{words}" if quote and words else words)
    key = str(card_id)
    stamp = learnnotes.append(settings.private_dir, key, text, today, author="Highlight", by_user=True)
    log.info("highlight added to %s", learnnotes.label(key))  # never the text
    return {"redirect": back, "stamp": stamp, "note": "card", "note_card": card_id}


def make_explainer(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    """Make an explainer: a fixed message to the Learn Assistant with the card's id (/explainer skill)."""
    card_id = _card_id(form)
    _, back = _learn_back(conn, form)
    if settings.private_dir is None:
        raise BadRequest("Explainers need a private folder (private_dir).")
    return _learn_ask(settings, conn, card_id, db.LEARN_EXPLAIN, back)


def refresh_explainer(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    """Refresh explainer: a fixed message naming what changed in getjo (from the check, not the form)."""
    card_id = _card_id(form)
    _, back = _learn_back(conn, form)
    state = explainers.check(settings, card_id)
    if state["state"] == "none":
        raise BadRequest("This card has no explainer yet: Make an explainer.")
    what = "; ".join(state["missing"] + state["changed"]) or "asked by the user"
    return _learn_ask(settings, conn, card_id, db.LEARN_EXPLAIN_REFRESH.format(what=what), back)


# The explainer page runs its own small scripts and styles, so it gets its own policy: inline allowed,
# but nothing reaches the network (no connect-src), and `sandbox allow-scripts` (here and on the iframe)
# gives it an opaque origin: no cookies, no dashboard, no same-origin requests.
EXPLAINER_HEADERS = [
    ("Content-Security-Policy",
     "default-src 'none'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
     "img-src 'self' data:; font-src 'none'; connect-src 'none'; form-action 'none'; base-uri 'none'; "
     "frame-ancestors 'self'; sandbox allow-scripts"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
]


def explainer_page(settings: Settings, query: dict[str, list[str]]) -> Response:
    """GET /learn/explainer?card=ID: the card's explainer page from private_dir/learn/explainers/, and
    nothing else (the file name is built from the int id). 404 when there is none."""
    raw = (query.get("card") or [""])[0]
    if not raw.isdigit() or len(raw) > 10:
        return _text(404, "Not found.")
    page = explainers.read_html(settings.private_dir, int(raw))
    if page is None:
        return _text(404, "No explainer for this card yet.")
    return 200, [("Content-Type", "text/html; charset=utf-8"), *EXPLAINER_HEADERS], page


def check_answer(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    """Check my answer: a fixed message (db.LEARN_CHECK) with the card's id; the agent reads the saved
    answer itself (learn-notes answer CARD --show). Refused while the answer is empty."""
    card_id = _card_id(form)
    _, back = _learn_back(conn, form)
    if settings.private_dir is None:
        raise BadRequest("Your answer needs a private folder (private_dir).")
    if not learnnotes.read(settings.private_dir, learnnotes.answer_key(card_id))["text"].strip():
        raise BadRequest("Write your answer first (Your answer box), then check it.")
    return _learn_ask(settings, conn, card_id, db.LEARN_CHECK, back)


def _learn_reply(conn: sqlite3.Connection, form: dict[str, str]) -> dict:
    """The Learn Assistant reply an action under it names (reply_id); anything else is refused."""
    reply_id = _int_field(form, "reply_id")
    msg = db.get_message(conn, reply_id) if reply_id is not None else None
    if msg is None or msg["topic"] != db.LEARN or msg["author"] != "agent":
        raise BadRequest("That is not a reply from the Learn Assistant.")
    return msg


def draft_card(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    """Want a card for this?: a fixed message naming the reply; the agent drafts the card in the thread
    and saves it (cards add) only after the user says yes."""
    reply = _learn_reply(conn, form)
    _, back = _learn_back(conn, form)
    return {**_learn_ask(settings, conn, reply["card_id"], db.LEARN_CARD.format(reply=reply["id"]), back),
            "msg": "learn_card_asked"}


NO_SUMMARY = ('This reply has no short summary to add. Ask the Learn Assistant to '
              '"summarise this as Understand / Say it".')


def add_reply_to_notes(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    """Add to notes: the essence of the reply (`learnnotes.essence`: its closing Understand: / Say it:
    block, or an older reply's Better phrasing and Say it in the interview lines), appended to its
    card's note under the card's question and the day (the all-prep notes for a general reply, under
    "Agent" and the day). Never the whole reply: one with neither is refused (NO_SUMMARY). Once per
    reply; never pay."""
    reply = _learn_reply(conn, form)
    _, back = _learn_back(conn, form)
    key = str(reply["card_id"]) if reply["card_id"] is not None else learnnotes.ALL
    text = learnnotes.essence(reply["body"])
    if text is None:
        raise BadRequest(NO_SUMMARY)
    if text.strip() in learnnotes.read(settings.private_dir, key)["text"]:
        raise BadRequest("That reply is already in the notes.")
    heading = "Agent"
    if reply["card_id"] is not None:
        try:
            heading = " ".join(learn.get_card(conn, reply["card_id"])["front"].split()).replace("*", "") or heading
        except LookupError:
            pass  # a removed card: "Agent"
    stamp = learnnotes.append(settings.private_dir, key, text, today, author=heading)
    log.info("Learn Assistant reply %s added to %s", reply["id"], learnnotes.label(key))  # never the text
    return {"redirect": back, "stamp": stamp, "note": "all" if key == learnnotes.ALL else "card",
            "note_card": reply["card_id"]}


def resend_learn_message(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    """Resend in the Learn Assistant box: as resend_message, pinging `/learn-ask` with the same id."""
    if not settings.prep_assistant_task:
        raise BadRequest("No Learn Assistant task is set (prep_assistant_task): run /learn-ask in a Claude Code session.")
    card_id, back = _learn_back(conn, form)
    msg = db.reopen_message(conn, _msg_id(form), learn=True)
    log.info("Learn Assistant message %s sent again", msg["id"])
    if not assistant.notify_learn(settings, msg["card_id"], msg["id"]):
        raise _unreachable("Learn Assistant", settings.prep_assistant_task)
    return {"redirect": back, "html": _learn_thread(conn, settings, card_id)}


def cancel_learn_message(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    """Cancel in the Learn Assistant box: as cancel_message."""
    card_id, back = _learn_back(conn, form)
    msg_id = _msg_id(form)
    db.cancel_message(conn, msg_id, learn=True)
    log.info("Learn Assistant message %s cancelled", msg_id)
    return {"redirect": back, "html": _learn_thread(conn, settings, card_id)}


def stop_learn(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    """Stop, on the Learn Assistant box's waiting line: every Learn message still waiting is cancelled
    (never an Applications one); rows are kept, marked cancelled. Nothing is sent to the assistant."""
    card_id, back = _learn_back(conn, form)
    msgs = db.cancel_waiting_messages(conn, learn=True)
    log.info("learn stop: %d message(s) cancelled", len(msgs))
    return {"redirect": back, "html": _learn_thread(conn, settings, card_id)}


def learn_thread_json(settings: Settings, query: dict[str, list[str]], today: str) -> Response:
    """GET /learn/thread?card=ID for polling: whether a Learn Assistant message is still waiting, the
    message count (a reply arrived) and the conversation's HTML (the open card marked)."""
    raw = (query.get("card") or [""])[0]
    if raw and not raw.isdigit():
        return _json(400, {"ok": False, "message": "card must be a card id."})
    with closing(_connect(settings, today)) as conn:
        waiting = db.pending_messages(conn, topic=db.LEARN)
        payload = {"ok": True, "pending": bool(waiting), "failed": _failed_pings(waiting),
                   "messages": db.message_count(conn, learn=True),
                   "html": _learn_thread(conn, settings, int(raw) if raw else None)}
    return _json(200, payload)


# path -> (action, page to return to, MESSAGES key)
# The offer actions keep their old paths (forms from before the merge still post to them).
ACTIONS: dict[str, tuple[Callable[..., dict | None], str, str]] = {
    "/offers/apply": (mark_applied, "/", "applied"),  # no button any more; kept for old forms
    "/offers/interested": (mark_interested, "/", "interested"),
    "/offers/skip": (skip_offer, "/", "skipped"),
    "/offers/unskip": (unskip_offer, "/", "unskipped"),
    "/offers/uninterested": (unmark_interested, "/", "uninterested"),
    "/matches/apply": (mark_applied, "/", "applied"),  # old form action, before the rename
    "/applications/apply": (apply_application, "/", "applied_today"),
    "/applications/unapply": (unapply_application, "/", "unapplied"),
    "/applications/skip": (skip_application, "/", "app_skipped"),
    "/applications/research": (queue_research, "/", "research_queued"),
    "/applications/message": (send_message, "/", "message_sent"),
    "/applications/mock": (start_mock, "/", "mock_started"),
    "/applications/draft": (request_draft, "/", "draft_requested"),
    "/applications/resend": (resend_message, "/", "resent"),
    "/applications/cancel": (cancel_message, "/", "cancelled"),
    "/applications/stop": (stop_waiting, "/", "stopped"),
    "/applications/notes": (save_notes, "/", "notes_saved"),
    "/applications/employer": (set_employer_kind, "/", "employer_saved"),
    "/cv/restore": (restore_cv, "/", "cv_restored"),
    "/offers/brief": (queue_brief, "/", "brief_queued"),
    "/tasks/done": (complete_task, "/", "task_done"),
    "/tasks/reopen": (reopen_task, "/", "task_reopened"),
    "/learn/done": (card_done, "/learn", "card_done"),
    "/learn/undo": (card_undo, "/learn", "card_todo"),
    "/learn/source": (tick_source, "/learn", "source_done"),
    "/learn/notes": (save_learn_notes, "/learn", "learn_notes_saved"),
    "/learn/message": (send_learn_message, "/learn", "learn_sent"),
    "/learn/check": (check_answer, "/learn", "learn_check_sent"),
    "/learn/card": (draft_card, "/learn", "learn_card_asked"),
    "/learn/add-note": (add_reply_to_notes, "/learn", "learn_note_added"),
    "/learn/resend": (resend_learn_message, "/learn", "learn_resent"),
    "/learn/cancel": (cancel_learn_message, "/learn", "learn_cancelled"),
    "/learn/stop": (stop_learn, "/learn", "learn_stopped"),
    "/learn/quote-note": (add_quote_to_notes, "/learn", "learn_quote_noted"),
    "/learn/explainer/make": (make_explainer, "/learn", "learn_explain_asked"),
    "/learn/explainer/refresh": (refresh_explainer, "/learn", "learn_refresh_asked"),
}


# --- pages (GET) ------------------------------------------------------------------------
# Each returns (title, subtitle, HTML content). Only columns that are shown are read; never pay.

TOKEN_SERIES = (("Cache read", "s4"), ("Cache write", "s6"), ("Output", "s8"), ("Fresh input", "s10"))
COST_SHADES = {"Cache read": "s4", "Cache write": "s6", "Output": "s8", "Input": "s10"}
ESTIMATE_NOTE = "Estimated at API prices. Your Claude subscription is billed differently."
ROLE_SHADES = {"Applications Assistant": "s9", "Learn Assistant": "s7", "Research worker": "s6", "Comms worker": "s4",
               "Coordinator": "s5", "Build tasks": "s3", "Main checkouts": "s1"}


def usage_filters(query: dict[str, list[str]]) -> dict:
    """days (7, 14 or 30) and project (a name, "all" or "" for the default) of /usage."""
    days = (query.get("days") or [""])[0]
    project = (query.get("project") or [""])[0].strip()
    return {"days": int(days) if days.isdigit() and int(days) in usage.DAYS else 14, "project": project[:100]}


def usage_url(f: dict, **changes: object) -> str:
    pairs = {**f, **changes}
    q = {"project": pairs["project"], "days": pairs["days"] if pairs["days"] != 14 else ""}
    q = {k: v for k, v in q.items() if v not in ("", None)}
    return "/usage" + (f"?{urlencode(q)}" if q else "")


def _switch(label: str, options: list[tuple[str, str, bool]]) -> str:
    """Pill links: (href, text, active)."""
    tabs = "".join(f'<a class="view-tab{" is-active" if active else ""}" href="{h(href)}"{CURRENT if active else ""}>'
                   f"{h(text)}</a>" for href, text, active in options)
    return f'<nav class="view-switch" aria-label="{h(label)}">{tabs}</nav>'


def stats_page(settings: Settings, conn: sqlite3.Connection, today: str,
               query: dict[str, list[str]]) -> tuple[str, str, str]:
    """/stats: the search's numbers (applications per week, response rate, funnel, job boards)."""
    return "Pipeline stats", f"Job search · {today}", applications_tab(settings, conn, today)


def usage_page(settings: Settings, conn: sqlite3.Connection, today: str,
               query: dict[str, list[str]]) -> tuple[str, str, str]:
    """/usage: what Claude Code costs, where the money goes and how long work takes (prep/usage.py)."""
    return ("Usage & cost", f"Claude Code tokens, cost and time · {today}",
            usage_content(settings, conn, today, usage_filters(query)))


def _tok(n: float) -> str:
    return usage.short(n)


def _usd(x: float) -> str:
    """$0 · <$0.01 · $3.27 · $41.80 · $1,234."""
    if x <= 0:
        return "$0"
    if x < .01:
        return "<$0.01"
    return f"${x:,.0f}" if round(x, 2) >= 1000 else f"${x:,.2f}"


def _usd_axis(x: float) -> str:
    return f"${x:,.0f}" if x >= 10 or x == 0 else f"${x:,.2f}"


def _secs(x: float | None) -> str:
    """A duration in seconds as "–", "4.2s", "38s", "4m 10s" or "1h 5m"."""
    if x is None:
        return "–"
    if x < 10:
        return f"{x:.1f}s"
    if x < 60:
        return f"{x:.0f}s"
    m, sec = divmod(int(round(x)), 60)
    if m < 60:
        return f"{m}m {sec}s" if sec else f"{m}m"
    hr, m = divmod(m, 60)
    return f"{hr}h {m}m" if m else f"{hr}h"


def _ms(x: float | None) -> str:
    return _secs(None if x is None else x / 1000)


def _share(part: float, whole: float) -> str:
    return f"{part / whole:.0%}" if whole else "–"


def _money_tile(label: str, w: dict) -> str:
    unknown = f" · {h(_tok(w['unpriced']))} price unknown" if w["unpriced"] else ""
    return _tile(label, _usd(w["cost"]), f"<small>{h(_tok(w['tokens']))} tokens{unknown}</small>", "accent")


def _cost_cell(dollars: float, unpriced: int, tokens: int) -> str:
    if unpriced and unpriced == tokens:
        return '<span class="muted">price unknown</span>'
    return h(_usd(dollars)) + ("*" if unpriced else "")


def money_summary(r: dict, j: dict, days: int, jobs_count: bool) -> list[str]:
    """"Where the money goes" in a few plain sentences, biggest first. `jobs_count`: the page shows this
    repo's project (or all), so the workers' jobs are part of its cost."""
    lines = [f"{_usd(r['cost'])} in the last {days} days: {_tok(r['total'])} tokens over {r['turns']:,} responses."]
    if r["roles"] and r["cost"]:
        name, _, c = r["roles"][0]
        lines.append(f"{name} spent the most: {_usd(c)} ({_share(c, r['cost'])}).")
    priced = [m for m in r["models"] if m["cost"]]
    if priced:
        m = priced[0]
        wait = f", typical response {_ms(m['median'])}" if m["median"] is not None else ""
        lines.append(f"{m['model']} took {_share(m['cost'], r['cost'])} of the cost{wait}.")
    kinds = sorted(r["money"]["kinds"], key=lambda k: -k[1])
    month = sum(c for _, c, _ in kinds)
    if month:
        name, c, _ = kinds[0]
        tip = (" Cache writes mean the context was rebuilt: long pauses let the cache expire."
               if name == "Cache write" else "")
        lines.append(f"{name} is the largest part of the bill over {usage.MONEY_DAYS} days ({_share(c, month)}).{tip}")
    done = [k for k in j["kinds"] if k["jobs"] and k["per_job"]]
    if done:
        k = max(done, key=lambda k: k["per_job"])
        lines.append(f"{workers.KIND_LABELS.get(k['kind'], k['kind'])} is the costliest job: {_usd(k['per_job'])} "
                     f"and {_secs(k['time'])} each (median).")
    spent = sum(k["cost"] for k in j["kinds"])
    if jobs_count and spent and r["cost"]:
        lines.append(f"Worker jobs are {_share(spent, r['cost'])} of the cost; the rest is conversation, "
                     "coordination and build tasks.")
    return lines


def usage_content(settings: Settings, conn: sqlite3.Connection, today: str, f: dict) -> str:
    """Headline numbers first (cost, cache hit rate, job time), then where the money goes, per kind of job,
    per model, over time, the most expensive jobs and tasks, and the context size per turn."""
    try:
        usage.sync(conn, settings)
    except (OSError, sqlite3.Error) as e:
        log.warning("usage scan failed (%s)", e.__class__.__name__)
    names = usage.report(conn, settings, today, 1)["projects"]
    home = usage.home_project(usage.projects(conn), settings)
    project = f["project"] or (home if home in names else "all")
    r = usage.report(conn, settings, today, f["days"], None if project == "all" else project)
    filters = ('<div class="metrics-filters">'
               + _switch("Project", [(usage_url(f, project=p), p, project == p) for p in [*names, "all"]])
               + _switch("Days", [(usage_url(f, project=project, days=d), f"{d} days", f["days"] == d)
                                  for d in usage.DAYS])
               + "</div>") if names else ""
    if not r["total"]:
        where = h(str(settings.claude_projects_dir))
        return filters + _card("Usage", _empty(f"No Claude Code usage in the last {f['days']} days.",
                                               f"Transcripts are read from {where} (setting claude_projects_dir)."))
    db.sync_runs(conn)
    j = usage.jobs(conn, settings, today, f["days"])
    m = r["money"]
    hit = "–" if r["cache_hit"] is None else f"{r['cache_hit']:.0%}"
    tiles = (_money_tile("Today", m["today"]) + _money_tile("Last 7 days", m["week"])
             + _money_tile(f"Last {usage.MONEY_DAYS} days", m["month"])
             + _tile("Cache hit rate", hit, "<small>input read from the cache, the cheap part</small>",
                     "success" if r["cache_hit"] and r["cache_hit"] >= .8 else "")
             + _tile("Typical job time", _secs(j["time"]["median"]),
                     f"<small>{j['time']['n']} jobs done · slowest 10% {h(_secs(j['time']['p90']))}</small>"
                     if j["time"]["n"] else "<small>no worker jobs in this period</small>"))
    summary = "".join(f"<li>{h(t)}</li>" for t in money_summary(r, j, f["days"], project in (home, "all")))
    save = ("<h3>Where to save</h3><ul class=\"save-list\">" + "".join(f"<li>{h(t)}</li>" for t in r["save"])
            + "</ul>") if r["save"] else ""
    where = _card("Where the money goes", f'<ul class="save-list">{summary}</ul>{save}', cls="card-wide")
    return (filters + _note(ESTIMATE_NOTE) + f'<div class="tiles tiles-five">{tiles}</div>' + where
            + '<div class="columns">' + _cost_parts_card(m) + _roles_card(r, f["days"]) + "</div>"
            + _jobs_card(j, f["days"]) + _models_card(r, f["days"])
            + _cost_per_day_card(m) + _tokens_per_day_card(r)
            + _top_runs_card(settings, conn, j, f["days"]) + _top_tasks_card(r, f["days"])
            + _context_card(settings, r))


def _cost_parts_card(m: dict) -> str:
    total = sum(c for _, c, _ in m["kinds"])
    kinds = [(name, c, COST_SHADES[name]) for name, c, _ in m["kinds"]]
    rows = [(f'<span class="swatch {COST_SHADES[name]}"></span>{h(name)}', h(_usd(c)), _share(c, total), h(_tok(n)))
            for name, c, n in sorted(m["kinds"], key=lambda k: -k[1])]
    unknown = ""
    if m["unknown_models"]:
        unknown = _note("Price unknown (not counted in $): " + ", ".join(
            f"{model} {_tok(n)} tokens" for model, n in m["unknown_models"]) + ".")
    return _card(f"Cost by part, last {usage.MONEY_DAYS} days",
                 charts.share_bar(kinds) + _table(("Part", ("Cost", "num"), ("Share", "num"), ("Tokens", "num")),
                                                  rows, "Nothing priced.")
                 + _note("Cache write includes 5-minute and 1-hour writes at their own prices.") + unknown)


def _roles_card(r: dict, days: int) -> str:
    total = r["cost"]
    roles = [(name, c if total else n, ROLE_SHADES.get(name, "s3")) for name, n, c in r["roles"]]
    rows = [(f'<span class="swatch {ROLE_SHADES.get(name, "s3")}"></span>{h(name)}', h(_usd(c)), _share(c, total),
             h(_tok(n)), h(_ms(r["role_latency"].get(name, {}).get("median"))))
            for name, n, c in r["roles"]]
    return _card(f"Who spends what, last {days} days", charts.share_bar(roles) + _table(
        ("Who", ("Cost", "num"), ("Share", "num"), ("Tokens", "num"), ("Typical response", "num")), rows, "No usage.")
                 + _note("Typical response: the median time from a request to the end of its response."))


def _jobs_card(j: dict, days: int) -> str:
    rows = [(h(workers.KIND_LABELS.get(k["kind"], k["kind"])), str(k["jobs"]), h(_usd(k["cost"])),
             h(_usd(k["per_job"]) if k["per_job"] is not None else "–"),
             h(_tok(k["tokens_per_job"]) if k["tokens_per_job"] is not None else "–"),
             h(_secs(k["time"])), h(_secs(k["slow"]))) for k in j["kinds"]]
    table = _table(("Kind of job", ("Done", "num"), ("Cost", "num"), ("Per job", "num"), ("Tokens per job", "num"),
                    ("Typical time", "num"), ("Slowest 10%", "num")), rows,
                   "No worker jobs in this period.",
                   hint="Each item a worker takes from the queue is a job: its cost and time show here.")
    return _card(f"Cost and time per kind of job, last {days} days", table + _note(
        "A job's cost: its worker's responses between the job's start and finish. Per job, tokens and times are "
        "medians of done jobs; Cost counts every run (also blocked, stopped or lapsed).")
                 + '<p class="card-note"><a href="/runs">Run history →</a></p>', count=j["runs"], cls="card-wide")


def _models_card(r: dict, days: int) -> str:
    rows = [(_mono(h(m["model"])), _cost_cell(m["cost"], m["unpriced"], m["tokens"]), _share(m["cost"], r["cost"]),
             f"{m['turns']:,}", h(_tok(m["input"])), h(_tok(m["output"])), h(_tok(m["cache_read"])),
             h(_tok(m["cache_write"])), h(_ms(m["median"])), h(_ms(m["p90"]))) for m in r["models"]]
    return _card(f"By model, last {days} days", _table(
        ("Model", ("Cost", "num"), ("Share", "num"), ("Responses", "num"), ("Fresh input", "num"), ("Output", "num"),
         ("Cache read", "num"), ("Cache write", "num"), ("Typical", "num"), ("Slowest 10%", "num")),
        rows, "No usage.") + _note("Typical and Slowest 10%: response time, from the request to the response's last "
                                   "line (streaming included), median and 90th percentile. – : not known (older "
                                   "transcripts)."),
                 cls="card-wide")


def _cost_per_day_card(m: dict) -> str:
    series = [(name, ROLE_SHADES.get(name, "s3")) for name in usage.ROLES]
    days = [(f"{date.fromisoformat(d).day}", v) for d, v in m["days"]]
    used = [s for k, s in enumerate(series) if any(v[k] for _, v in days)]
    keep = [k for k, s in enumerate(series) if s in used]
    legend = "".join(f'<span class="stack-key"><span class="swatch {s}"></span>{h(n)}</span>' for n, s in used)
    chart = charts.stacked_columns([(label, [v[k] for k in keep]) for label, v in days], used, _usd_axis)
    return _card(f"Cost per day by role, last {usage.MONEY_DAYS} days",
                 f'<div class="stack-legend">{legend}</div>' + (chart or _empty("Nothing priced in this period."))
                 + _note(f"Day of the month under each column. {ESTIMATE_NOTE}"), cls="card-wide")


def _tokens_per_day_card(r: dict) -> str:
    days = [(f"{date.fromisoformat(d).day} {date.fromisoformat(d):%b}", [cr, cw, o, i]) for d, i, o, cr, cw in r["days"]]
    legend = "".join(f'<span class="stack-key"><span class="swatch {s}"></span>{h(n)}</span>' for n, s in TOKEN_SERIES)
    return _card("Tokens per day", f'<div class="stack-legend">{legend}</div>'
                 + charts.stacked_columns(days, list(TOKEN_SERIES), _tok)
                 + _note("Stacked from the bottom: cache read, cache write, output, fresh input."), cls="card-wide")


def _top_runs_card(settings: Settings, conn: sqlite3.Connection, j: dict, days: int) -> str:
    names = {w["seq"]: w["name"] for w in workers.configured(settings)}
    job_names = _job_name_lookup(settings, conn)({r["getjo_job_id"] for r in j["top"]
                                                   if not r.get("application_id") and r.get("getjo_job_id")})
    rows = [(h(workers.KIND_LABELS.get(r["kind"], r["kind"])),
             f'<a href="{h(workers.href(r))}">{h(workers.about(r, job_names))}</a>',
             h(names.get(r["worker"], f"seq {r['worker']}" if r["worker"] else "–")),
             _cost_cell(r["cost"], r["unpriced"], r["tokens"]), h(_tok(r["tokens"])), f"{r['turns']:.0f}",
             h(_secs(r["seconds"])), h(r["result"] or ""), _mono(h(_clock(r["finished_at"]))))
            for r in j["top"] if r["tokens"]]
    return _card(f"Most expensive jobs, last {days} days", _table(
        ("Kind", "About", "Worker", ("Cost", "num"), ("Tokens", "num"), ("Responses", "num"), ("Time", "num"),
         "Result", "Finished"), rows, "No priced worker jobs in this period."), cls="card-wide")


def _top_tasks_card(r: dict, days: int) -> str:
    rows = [(_mono(h(t["ref"])), h(t["title"]), h(t["role"]), _cost_cell(t["cost"], t["unpriced"], t["tokens"]),
             f"{t['turns']:,}", h(_tok(t["tokens"])), h(_tok(t["per_turn"]))) for t in r["top"]]
    starred = _note("* part of its tokens has no price") if any(0 < t["unpriced"] < t["tokens"] for t in r["top"]) else ""
    return _card(f"Most expensive tasks, last {days} days", _table(
        ("Task", "Title", "Who", ("Cost", "num"), ("Responses", "num"), ("Tokens", "num"), ("Per response", "num")),
        rows, "No tasks.") + starred, count=len(r["top"]), cls="card-wide")


def _context_card(settings: Settings, r: dict) -> str:
    rows = []
    for c in r["context"]:
        top_v = max(max(v for _, v in c["trend"]), settings.context_warn_tokens)
        warn = '<span class="warn-text">time to /compact</span>' if c["over"] else ""
        rows.append((h(c["role"]), _mono(h(c["ref"])), charts.trend(list(c["trend"]), 0, top_v),
                     h(_tok(c["latest"])) + warn))
    table = _table(("Who", "Task", "By day", ("Latest", "num")), rows,
                   "No assistant, worker or coordinator turns in this period.",
                   hint="Settings assistant_task, prep_assistant_task, [workers] and coordinator_task name them.")
    return _card("Context size per response", table + _note(
        f"Input plus cache read and written per response: each day's last one, up to {usage.CONTEXT_DAYS} active "
        f"days, latest on the right. Warning above {_tok(settings.context_warn_tokens)} (setting "
        "context_warn_tokens)."), cls="card-wide")


def applications_tab(settings: Settings, conn: sqlite3.Connection, today: str) -> str:
    """Applications sent per week, response rate, stage funnel and job boards. Never reads pay."""
    apps = stats.application_stages(conn)
    reply = stats.reply_rate(apps)
    steps = stats.stage_funnel(apps)
    reached = {label: n for label, n, _ in steps}
    rate = "–" if reply["rate"] is None else f"{reply['rate']:.0%}"
    tiles = (
        _tile("Applications sent", str(reply["submitted"]), "<small>all time</small>", "accent")
        + _tile("Response rate", rate, f"<small>{reply['replied']} of {reply['submitted']} got a reply, call or "
                                       f"rejection</small>", "warning" if reply["warning"] else "")
        + _tile("Interviews", str(reached["Interview"]), "<small>reached an interview</small>")
        + _tile("Offers", str(reached["Offer"]), f"<small>from {reply['submitted']} sent</small>",
                "success" if reached["Offer"] else "")
    )
    warning = ""
    if reply["warning"]:
        warning = (f'<div class="warning" role="alert">Response rate is below {stats.REPLY_WARNING_RATE:.0%} after '
                   f'{reply["submitted"]} applications. Review the CV, targeting or tiers.</div>')
    no_apps = "No applications sent yet."
    target = settings.weekly_target
    per_week = charts.columns(_weeks(stats.applications_per_week(conn, today)), target, width=1000, height=230)
    funnel = charts.funnel([(label, n, share, shade) for (label, n, share), shade
                            in zip(steps, ("s5", "s6", "s8", "s10"))]) if reply["submitted"] else ""
    return (
        f'<div class="tiles">{tiles}</div>{warning}'
        + _card("Applications sent per week", per_week + _note(
            f"Week starting Monday, last {stats.WEEKS_SHOWN} weeks. Target {target} per week."), cls="card-wide")
        + '<div class="columns">'
        + _card("Stage funnel", (funnel + _note("Screen: recruiter call. Interview: technical, system design or "
                                                "final. Share of applications sent, counting later stages too."))
                if funnel else _empty(no_apps), cls="card-funnel")
        + _card("Which job boards bring the best matches", boards_table(settings, apps))
        + "</div>"
    )


def boards_table(settings: Settings, apps: list[dict]) -> str:
    """Per getjo source: jobs seen, average score, strong matches, applications sent from them."""
    applied = {a["getjo_job_id"] for a in apps if a["submitted"] and a["getjo_job_id"]}
    try:
        with closing(getjo.connect_ro(settings.getjo_db)) as gconn:
            rows = getjo.source_stats(gconn, applied)
    except (getjo.GetjoUnavailable, sqlite3.Error) as e:
        return _empty("getjo's database is not available.", str(e) if isinstance(e, getjo.GetjoUnavailable) else "")
    table = _table(("Source", ("Jobs seen", "num"), ("Avg score", "num"), (f"Strong ({getjo.DEFAULT_THRESHOLD}+)", "num"),
                    ("Applied", "num")),
                   [(h(r["source"]), str(r["seen"]), "–" if r["avg_score"] is None else f"{r['avg_score']:.1f}",
                     str(r["strong"]), str(r["applied"])) for r in rows], "getjo has no jobs yet.")
    return table + (_note("From getjo (read-only): jobs seen, average CV score of the scored ones, strong matches "
                          "and applications sent from them.") if rows else "")


# --- Learn ------------------------------------------------------------------------------------
# /learn: the tracks grid and, under it, the Learn Assistant box for general messages (no card).
# /learn?card=ID (or ?track=SLUG, its first card to do): the card view: the card at full width, its
# Explainer under it (prep/explainers.py), and a floating window with the Learn Assistant (the Learn
# conversation) and Notes (this card's note, or with &notes=all the general notebook: prep/learnnotes.py).


def learn_page(settings: Settings, conn: sqlite3.Connection, today: str,
               query: dict[str, list[str]]) -> tuple[str, str, str]:
    raw_card = (query.get("card") or [""])[0].strip()
    raw_track = (query.get("track") or [""])[0].strip()
    card_id = None
    if raw_card:
        if not raw_card.isdigit():
            raise NotFound()
        card_id = int(raw_card)
    elif raw_track:
        try:
            card_id = learn.first_card(conn, raw_track)
        except LookupError as e:
            raise NotFound() from e
    count = learn.counts(conn)
    subtitle = f"{count['done']} done · {count['todo']} to do · {_next_interview(conn, today)}"
    if card_id is None:
        return "Learn", subtitle, (_tracks_grid(learn.list_tracks(conn), bool(raw_track))
                                   + _card("Learn Assistant", _learn_agent(conn, settings, None),
                                           cls="learn-agent learn-general"))
    try:
        card = learn.get_card(conn, card_id)
    except LookupError as e:
        raise NotFound() from e
    key = learnnotes.ALL if (query.get("notes") or [""])[0] == learnnotes.ALL else str(card_id)
    return "Learn", subtitle, (_tracks_grid(learn.list_tracks(conn), current=card["track_id"])
                               + _learn_view(settings, conn, card, key))


def _next_interview(conn: sqlite3.Connection, today: str) -> str:
    nxt = db.next_interview(conn, today)
    if nxt is None:
        return "no interview booked"
    day = date.fromisoformat(nxt["date"][:10])
    when = "today" if nxt["date"][:10] == today else f"{day:%a} {day.day} {day:%b}"
    stage = f" · {db.status_label(nxt['stage'])}" if nxt["stage"] else ""
    return f"next interview: {nxt['company']}{stage} · {when}"


def _ring(percent: int) -> str:
    """A progress ring as SVG attributes (no inline style: the CSP forbids it); pathLength makes the
    dash a plain percentage."""
    p = max(0, min(100, int(percent)))
    return (f'<svg class="ring" viewBox="0 0 40 40" width="56" height="56" role="img" aria-label="{p}% done">'
            '<circle class="ring-track" cx="20" cy="20" r="16" pathLength="100"/>'
            f'<circle class="ring-done" cx="20" cy="20" r="16" pathLength="100" stroke-dasharray="{p} 100" '
            'transform="rotate(-90 20 20)"/>'
            f'<text x="20" y="20" text-anchor="middle" dominant-baseline="central">{p}%</text></svg>')


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _tracks_grid(tracks: list[dict], empty_track: bool = False, current: int | None = None) -> str:
    """The tracks as tiles; above the card view (`current`: the open card's track, highlighted) they
    are a compact row."""
    if not tracks:
        return _card("Tracks", _empty("No tracks yet.", "python -m prep.cli tracks add TITLE, then cards add"))
    tiles = []
    for t in tracks:
        cover = t["cover"] if t["cover"] in learn.TRACK_COVERS else "slate"
        sub = f'<span class="track-sub">{h(t["subtitle"])}</span>' if t["subtitle"] else ""
        tiles.append(
            f'<a class="track{" is-current" if t["id"] == current else ""}" '
            f'href="/learn?{h(urlencode([("track", t["slug"])]))}"{CURRENT if t["id"] == current else ""}>'
            f'<span class="track-cover cover-{cover}">{_ring(t["progress"])}</span>'
            f'<span class="track-body"><strong class="track-title">{h(t["title"])}</strong>{sub}'
            f'<span class="track-meta">{h(_plural(t["chapters"], "chapter"))} · {h(_plural(t["cards"], "card"))}</span>'
            f'<span class="track-done">{h(t["done"])} done / {h(t["cards"])}</span></span></a>')
    notice = _banner("That track has no cards yet.", "notice") if empty_track else ""
    row = " tracks-row" if current is not None else ""
    return f'{notice}<nav class="tracks{row}" aria-label="Tracks">{"".join(tiles)}</nav>'


def _learn_view(settings: Settings, conn: sqlite3.Connection, card: dict, key: str) -> str:
    """The card view: the card at full width, its Explainer under it, and the floating "Ask about this
    card" window (Learn Assistant | Notes) with its pill. Without the page script the window is a plain
    section at the end of the page."""
    answer = ""
    if settings.private_dir is not None:
        akey = learnnotes.answer_key(card["id"])
        answer = _answer_box(card["id"], akey, learnnotes.read(settings.private_dir, akey)) + _check_form(card["id"])
    return (f'<div class="learn-view" id="learn-view" data-card="{h(card["id"])}">'
            f'{_learn_card(conn, card, answer)}{_explainer_section(settings, card["id"])}'
            f'{_learn_float(settings, conn, card, key)}</div>')


def _explainer_stamp(state: dict) -> str:
    """"getjo @ dacabe7 (12 Nov) · evals/run.py · up to date"."""
    parts = []
    if state["short"]:
        when = ""
        try:
            day = date.fromisoformat(state["date"])
            when = f" ({day.day} {day:%b})"
        except ValueError:
            pass
        parts.append(f"getjo @ {state['short']}{when}")
    parts += state["files"]
    parts.append({"up_to_date": "up to date", "changed": "changed", "missing": "changed",
                  "unknown": "not checked"}.get(state["state"], ""))
    return " · ".join(p for p in parts if p)


def _explainer_section(settings: Settings, card_id: int) -> str:
    """The Explainer card: its stamp (getjo commit, files, up to date or changed) and Refresh, the amber
    notice when getjo changed, and the page itself in a sandboxed iframe (scripts only, no same origin:
    it cannot reach the dashboard). None yet: Make an explainer."""
    if settings.private_dir is None:
        return ""
    state = explainers.check(settings, card_id)
    hidden = f'<input type="hidden" name="card_id" value="{h(card_id)}">'
    if state["state"] == "none":
        make = (f'<form method="post" action="/learn/explainer/make" class="explainer-make" data-ajax="learn" '
                f'data-open-chat>{hidden}<p class="muted">No explainer for this card yet. The Learn Assistant can '
                'write one: a diagram, the getjo code it is about with notes, and a worked example.</p>'
                + _button("Make an explainer", "secondary", "sm") + "</form>")
        return _card("Explainer", make, cls="learn-explainer")
    refresh = (f'<form method="post" action="/learn/explainer/refresh" class="inline" data-ajax="learn" data-open-chat>'
               f'{hidden}<button class="btn btn-ghost btn-sm">Refresh</button></form>')
    stamp = f'<span class="explainer-stamp">{h(_explainer_stamp(state))}</span>'
    notice = ""
    if state["state"] in ("changed", "missing"):
        what = "; ".join(state["missing"] + state["changed"])
        notice = (f'<div class="warning explainer-stale" role="status">getjo has changed since this explainer: '
                  f'{h(what)}<form method="post" action="/learn/explainer/refresh" class="inline" data-ajax="learn" '
                  f'data-open-chat>{hidden}<button class="btn btn-secondary btn-sm">Refresh explainer</button></form></div>')
    frame = (f'<iframe class="explainer-frame" src="/learn/explainer?card={h(card_id)}" sandbox="allow-scripts" '
             'referrerpolicy="no-referrer" title="Explainer" data-explainer></iframe>')
    return (f'<section class="card learn-explainer" id="learn-explainer"><header class="card-head"><h2>Explainer</h2>'
            f'<div class="explainer-head">{stamp}{refresh}</div></header>'
            f'<div class="card-body">{notice}{frame}</div></section>')


def _learn_float(settings: Settings, conn: sqlite3.Connection, card: dict, key: str) -> str:
    """The floating window (app.js: open, minimise, close, tab; kept per browser) and its pill. Tabs:
    the Learn Assistant box (same forms as ever) and the Notes editor."""
    tab = "notes" if key == learnnotes.ALL else "assistant"
    tabs = "".join(
        f'<button type="button" class="float-tab" role="tab" data-float-tab="{t}" '
        f'aria-selected="{"true" if t == tab else "false"}">{label}</button>'
        for t, label in (("assistant", "Assistant"), ("notes", "Notes")))
    where = h(card["track_title"])
    return ('<button type="button" class="learn-pill" data-float-open hidden>✦ Ask about this card</button>'
            f'<aside class="learn-float" id="learn-float" data-tab="{tab}" aria-label="Ask about this card">'
            '<header class="float-head"><div class="float-title"><strong>Ask about this card</strong>'
            f'<span class="muted">{where} · card {h(card["id"])}</span></div>'
            f'<nav class="float-tabs" role="tablist" aria-label="Assistant or notes">{tabs}</nav>'
            '<div class="float-ctl"><button type="button" class="float-btn" data-float-min aria-label="Minimise" '
            'title="Minimise" hidden>–</button><button type="button" class="float-btn" data-float-close '
            'aria-label="Close" title="Close" hidden>✕</button></div></header>'
            f'<div class="float-pane learn-agent" data-pane="assistant">{_learn_agent(conn, settings, card["id"])}</div>'
            f'<div class="float-pane learn-notes" data-pane="notes">{_notes_body(settings, card["id"], key)}</div>'
            '</aside>')


def _notes_url(card_id: int, key: str) -> str:
    return f"/learn?card={card_id}" + ("&notes=all" if key == learnnotes.ALL else "")


def _notes_body(settings: Settings, card_id: int, key: str) -> str:
    """Notes: tabs "this card" | "all prep" (links, so each works without the script) and the editor
    for the open one."""
    tabs = "".join(
        f'<a class="ed-tab" href="{h(_notes_url(card_id, k))}"{CURRENT if k == key else ""}>{label}</a>'
        for k, label in ((str(card_id), "this card"), (learnnotes.ALL, "all prep")))
    nav = f'<nav class="ed-tabs" aria-label="Which notes">{tabs}</nav>'
    if settings.private_dir is None:
        return nav + _empty("Notes need a private folder.", "Set private_dir in settings.toml; notes go to notes/learn/ there.")
    return nav + _notes_editor(card_id, key, learnnotes.read(settings.private_dir, key))


def _notes_editor(card_id: int, key: str, note: dict, draft: str | None = None, alert: str = "") -> str:
    """The editor: a monospace textarea (soft-wrapped) with a line-number gutter (one number per
    logical line; app.js sizes each to its wrapped height) and a status line
    (vim mode, file, autosave time, line,col, the vim keys toggle). app.js autosaves it; vim.js adds
    the optional vim keys. `draft` (a refused save): the user's text stays in the editor, under the
    current note, and the stamp is the current one, so Save again replaces it knowingly."""
    text = note["text"] if draft is None else draft
    lines = len(text.replace("\r\n", "\n").split("\n"))
    gutter = "\n".join(str(n) for n in range(1, lines + 1))
    which = "all" if key == learnnotes.ALL else "card"
    current = _current_text(note, alert) if draft is not None else ""
    label = "Notes for all prep" if key == learnnotes.ALL else "Notes for this card"
    # A textarea drops one leading newline, so one is always written first.
    return (f'<form method="post" action="/learn/notes" class="ed" id="learn-editor" data-learn-notes '
            f'data-card="{h(card_id)}" data-note="{which}">{current}'
            f'<input type="hidden" name="card_id" value="{h(card_id)}"><input type="hidden" name="note" value="{which}">'
            f'<input type="hidden" name="stamp" value="{h(note["stamp"])}">'
            f'<div class="ed-frame" data-ed-anchor><pre class="ed-gutter" aria-hidden="true">{gutter}</pre>'
            f'<textarea name="text" class="ed-text" spellcheck="false" autocomplete="off" '
            f'maxlength="{learnnotes.MAX_CHARS}" aria-label="{label}">\n{h(text)}</textarea></div>'
            '<div class="ed-status"><span class="ed-mode" data-ed-mode hidden></span>'
            f'<span class="ed-file">{h(note["file"])}</span><span class="ed-saved" data-ed-saved role="status"></span>'
            '<span class="ed-pos" data-ed-pos hidden></span>'
            '<button type="button" class="ed-vim" data-vim-toggle aria-pressed="false" hidden>vim keys off</button>'
            '<button class="btn btn-secondary btn-sm" data-ed-save>Save</button></div></form>')


def _current_text(note: dict, alert: str) -> str:
    """After a refused save: the warning and the text as it is now (above the user's own text)."""
    return (_banner(alert, "warning") + '<details class="ed-current" open><summary>As it is now</summary>'
            f'<pre>{h(note["text"]) or "(empty)"}</pre></details>')


def _answer_box(card_id: int, key: str, note: dict, draft: str | None = None, alert: str = "") -> str:
    """Your answer: a plain textarea (six rows, growing as it is typed; no vim keys) for the user's
    own answer, typed or dictated before Show answer, saved like the notes (app.js autosaving()) to
    notes/learn/answer-<id>.md. `draft`: a refused save, as in _notes_editor."""
    text = note["text"] if draft is None else draft
    current = _current_text(note, alert) if draft is not None else ""
    return (f'<form method="post" action="/learn/notes" class="answer-box" id="learn-answer" data-learn-answer '
            f'data-card="{h(card_id)}" data-note="answer">'
            '<label class="answer-label" for="answer-text">Your answer</label>'
            f'<input type="hidden" name="card_id" value="{h(card_id)}"><input type="hidden" name="note" value="answer">'
            f'<input type="hidden" name="stamp" value="{h(note["stamp"])}">{current}'
            f'<textarea id="answer-text" name="text" rows="6" class="answer-text" data-ed-anchor '
            f'maxlength="{learnnotes.MAX_CHARS}" placeholder="Say it, then type or dictate it here before Show answer.">'
            f'\n{h(text)}</textarea>'
            '<p class="answer-foot"><span class="ed-saved" data-ed-saved role="status"></span>'
            '<button class="btn btn-secondary btn-sm" data-ed-save>Save</button></p></form>')


def _check_form(card_id: int) -> str:
    """Check my answer, under Your answer: the page script saves the answer first, then posts this."""
    return (f'<form method="post" action="/learn/check" class="answer-check" data-ajax="learn" data-save-answer>'
            f'<input type="hidden" name="card_id" value="{h(card_id)}">'
            '<span class="muted">The Learn Assistant says what you covered, what you missed and one better phrasing.</span>'
            + _button("Check my answer", "secondary", "sm") + "</form>")


LEARN_MESSAGES = 60  # the latest Learn Assistant messages shown


def _learn_agent(conn: sqlite3.Connection, settings: Settings, card_id: int | None) -> str:
    """The Learn Assistant box: the Learn conversation and a message form that carries the open card;
    with no card (the tracks overview) the message is general."""
    if card_id is None:
        quote = ""
        hidden, ask = "", ("Ask anything about prep: \u201cstart a mock recruiter call\u201d, "
                           "\u201cquiz me on system design\u201d…")
        foot = "A general message, not about one card. The Learn Assistant replies here."
    else:
        hidden = f'<input type="hidden" name="card_id" value="{h(card_id)}">'
        ask = "Ask about this card, \u201cquiz me on chapter 2\u201d, or \u201cexplain it more simply\u201d…"
        foot = "Sent with this card. The Learn Assistant replies here."
    # A passage highlighted on the card or its explainer, attached by app.js (shown as text, never HTML).
    quote = ('' if card_id is None else
             '<div class="ask-quote" data-ask-quote hidden><span>Attached: \u201c<span data-quote-text></span>\u201d</span>'
             '<button type="button" class="float-btn" data-quote-clear aria-label="Remove the attached text">\u2715</button>'
             '<input type="hidden" name="quote" value=""><input type="hidden" name="quote_src" value=""></div>')
    return (f'<div class="learn-chat" id="learn-chat" data-card="{h(card_id or "")}">'
            f'{_learn_thread(conn, settings, card_id)}'
            f'<form method="post" action="/learn/message" class="ask" data-ajax="learn">{hidden}{quote}'
            f'<textarea name="body" rows="3" maxlength="{db.MAX_MESSAGE}" required placeholder="{h(ask)}" '
            'aria-label="Message to the Learn Assistant"></textarea>'
            f'<div class="ask-foot"><span class="muted">{h(foot)}</span>'
            + _button("Send", size="sm") + "</div></form></div>")


CARD_TAG_CHARS = 50  # a Learn message from another card is tagged with its question, cut to this


def _clip(text: str, limit: int) -> str:
    """`text` cut to `limit` characters at a word break where one is close, with "…" when cut."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit * 0.6 else cut).rstrip(" ,;:-") + "…"


def _learn_thread(conn: sqlite3.Connection, settings: Settings, card_id: int | None) -> str:
    """The Learn Assistant conversation, its latest LEARN_MESSAGES oldest first, each tagged with the card it
    was sent from (a link with the card's question, cut short, the whole question as its title; "this
    card" for the open one; "a removed card", not a link, for a deleted one). Under each agent reply: Want a card for
    this? and Add to notes. data-pending while a message waits for the Learn Assistant."""
    messages = db.thread(conn, topic=db.LEARN, limit=LEARN_MESSAGES)
    back = f'<input type="hidden" name="card_id" value="{h(card_id)}">' if card_id is not None else ""

    questions = learn.fronts(conn, {m["card_id"] for m in messages if m["card_id"] is not None})

    def tag(m: dict) -> str:
        if m["card_id"] is None:
            return '<span class="msg-about">general</span>'
        if m["card_id"] == card_id:
            return f'<a class="msg-about is-here" href="/learn?card={h(card_id)}">this card</a>'
        question = questions.get(m["card_id"])
        if question is None:
            return '<span class="msg-about">a removed card</span>'
        full = " ".join(question.split())
        return (f'<a class="msg-about" href="/learn?card={h(m["card_id"])}" title="{h(full)}">'
                f'{h(_clip(full, CARD_TAG_CHARS))}</a>')

    def actions(m: dict) -> str:
        if m["author"] != "agent":
            return _wait_actions(m, "/learn", "learn", back, bool(settings.prep_assistant_task))
        forms = "".join(
            f'<form method="post" action="{path}" data-ajax="learn"><input type="hidden" name="reply_id" '
            f'value="{h(m["id"])}">{back}<button class="btn btn-ghost btn-sm">{label}</button></form>'
            for path, label in (("/learn/card", "Want a card for this?"), ("/learn/add-note", "Add to notes")))
        return f'<div class="msg-actions">{forms}</div>'

    flags = _msg_flags(conn, messages)
    items = "".join(
        f'<li class="msg msg-{"agent" if m["author"] == "agent" else "user"}{_cancelled_cls(m)}" '
        f'data-card="{h(m["card_id"] or "")}">'
        f'<div class="msg-meta"><span class="msg-who">{"assistant" if m["author"] == "agent" else "you"}</span>'
        + tag(m) + flags.get(m["id"], "")
        + f'<time datetime="{h(m["created_at"])}">{h(_local_time(m["created_at"]))}</time></div>'
        f'<div class="msg-body">{_linkify(m["body"])}</div>{actions(m)}</li>'
        for m in messages)
    pending = bool(db.pending_messages(conn, topic=db.LEARN))
    stop = ('<form method="post" action="/learn/stop" class="inline thread-stop" data-ajax="learn">'
            f'{back}<button class="btn btn-secondary btn-sm" title="Stop waiting: cancel the waiting messages">'
            'Stop</button></form>')
    if not messages:
        state = ('<p class="muted thread-empty">No messages yet. '
                 + ("Ask about this card, or say " if card_id is not None else "Ask anything, or say ")
                 + '\u201cquiz me on chapter 1\u201d.</p>')
    elif pending and settings.prep_assistant_task:
        state = ('<div class="thread-state" role="status"><span class="spinner" aria-hidden="true"></span>'
                 f"<span>Learn Assistant is working…</span>{stop}</div>")
    elif pending:
        state = ('<div class="thread-state" role="status"><span>Waiting for the Learn Assistant: run /learn-ask in a '
                 f"Claude Code session (no Learn Assistant task is set).</span>{stop}</div>")
    else:
        state = ""
    return (f'<div class="thread" data-learn-thread{" data-pending" if pending else ""}>'
            + (f'<ol class="msgs">{items}</ol>' if items else "") + state + "</div>")


def _learn_sources(conn: sqlite3.Connection, card: dict) -> str:
    """The card's Learn box: its sources, each with a tick (a form posting the state wanted, so it works
    without JS); a ticked one is greyed and struck through. The header counts the ticked ones."""
    # The sources are the preparation material, so they stay in view; only the answer is folded.
    if not card["sources"]:
        return ""
    flags, done = learn.source_progress(conn, card)
    rows = []
    for s, ticked in zip(card["sources"], flags):
        label = ("Mark not done: " if ticked else "Mark done: ") + s["title"]
        tick = ('<form method="post" action="/learn/source" class="inline" data-ajax="source">'
                f'<input type="hidden" name="card_id" value="{h(card["id"])}">'
                f'<input type="hidden" name="key" value="{h(learn.source_key(s))}">'
                f'<input type="hidden" name="done" value="{"0" if ticked else "1"}">'
                f'<button class="tick" role="checkbox" aria-checked="{"true" if ticked else "false"}" '
                f'aria-label="{h(label)}" title="{"Mark not done" if ticked else "Mark done"}">'
                f'{"☑" if ticked else "☐"}</button></form>')
        rows.append(
            ('<li class="is-done">' if ticked else "<li>") + f'{tick}<span class="learn-src-text">'
            f'{_badge(s["kind"], "s6")} <span class="learn-src-title">{_link(s.get("url"), s["title"])}</span>'
            + (f' <span class="muted">· {h(s["minutes"])} min</span>' if s.get("minutes") is not None else "")
            + (f' <span class="muted">· {h(s["why"])}</span>' if s.get("why") else "") + "</span></li>")
    return (f'<section class="learn-more" id="learn-sources" aria-labelledby="learn-src"><h3 id="learn-src">Learn'
            f' <span class="learn-src-count">· {done} / {len(flags)} done</span></h3>'
            f'<ul class="learn-sources">{"".join(rows)}</ul></section>')


def _learn_card(conn: sqlite3.Connection, card: dict, answer_box: str = "") -> str:
    cards = learn.list_cards(conn, card["track_id"])
    ids = [c["id"] for c in cards]
    i = ids.index(card["id"])
    nav = "".join(
        f'<a class="btn btn-ghost btn-sm" href="/learn?card={other}" aria-label="{label} card">{arrow}</a>'
        if other is not None else f'<span class="btn btn-ghost btn-sm is-off" aria-hidden="true">{arrow}</span>'
        for other, label, arrow in ((ids[i - 1] if i else None, "Previous", "←"),
                                    (ids[i + 1] if i + 1 < len(ids) else None, "Next", "→")))
    where = h(card["track_title"]) + (f' · {h(card["chapter_title"])}' if card["chapter_title"] else "")
    status = _badge("done", "success") if card["status"] == "done" else _badge("to do", "s5")
    tags = "".join(_badge(t, "s7") for t in card["tags"])
    head = (f'<header class="learn-card-head"><div><p class="learn-where"><a href="/learn">All tracks</a> · {where}</p>'
            f'<p class="learn-sub">Card {i + 1} of {len(ids)} {status}</p></div><nav class="learn-nav" '
            f'aria-label="Cards">{nav}</nav></header>')
    follow = "".join(f"<li>{h(f)}</li>" for f in card["follow_ups"])
    answer = ('<details class="learn-answer"><summary class="btn btn-secondary">Show answer</summary>'
              f'<section><h3>Key points</h3><div class="doc">{render_markdown(card["back"])}</div></section>'
              + (f'<section><h3>Follow-ups</h3><ul>{follow}</ul></section>' if follow else "")
              + "</details>")
    learn_box = _learn_sources(conn, card)
    buttons = "".join(
        f'<form method="post" action="{path}"><input type="hidden" name="card_id" value="{h(card["id"])}">'
        + _button(label, variant) + "</form>"
        for path, label, variant in (("/learn/done", "Done", "primary"), ("/learn/undo", "Not done yet", "secondary")))
    return (f'<section class="learn-card" id="learn-card" aria-labelledby="learn-q">{head}<div class="learn-card-body">'
            + (f'<div class="chips">{tags}</div>' if tags else "")
            + f'<h2 class="learn-q" id="learn-q">{h(card["front"])}</h2>'
            + (f'<div class="learn-prompt doc">{render_markdown(card["prompt"], empty="")}</div>' if card["prompt"] else "")
            + f'{answer_box}{answer}{learn_box}'
            f'<div class="learn-actions">{buttons}</div></div></section>')


LOCATION_LABELS = {"london": "London", "remote": "Remote", "other": "Other"}
SKIP_REASON_LABELS = {
    "location": "Wrong location", "seniority": "Seniority", "role_type": "Role type", "stack": "Stack", "company": "Company",
    "pay": "Pay", "contract_type": "Contract type", "other": "Other",
}
VIEWS = ("pipeline", "offers")


def _now(today: str) -> str:
    """The time getjo stamps are compared with: now, or noon UTC on a `today` given by a test."""
    return db.now_iso() if today == db.today_iso() else f"{today}T12:00:00Z"


def offer_score(query: dict[str, list[str]]) -> int:
    """New offers' minimum score from the query (1-10), else getjo.DEFAULT_THRESHOLD (7)."""
    score = (query.get("score") or [""])[0].strip()
    return int(score) if score.isdigit() and 1 <= int(score) <= 10 else getjo.DEFAULT_THRESHOLD


def new_offer_count(settings: Settings, conn: sqlite3.Connection, now: str, threshold: int) -> int | None:
    """How many open getjo jobs at `threshold` or more are not tracked or decided (the nav badge,
    the switch and /today count the same); None when getjo cannot be read."""
    try:
        with closing(getjo.connect_ro(settings.getjo_db)) as gconn:
            return len(getjo.matches(gconn, db.excluded_job_ids(conn), threshold, now=now))
    except (getjo.GetjoUnavailable, sqlite3.Error):
        return None


def home_filters(query: dict[str, list[str]]) -> dict:
    """Applications page state from the query string. `view` (pipeline, the default, or offers);
    the open panel: `app` (an application id) or else `job` (a getjo job id); the pipeline's `stage`
    (a stage group) and `closed` (1: closed applications listed too); New offers' `score` (minimum,
    default 7), `loc` (repeatable), `q` (company or role) and `skipped` (1: the skipped jobs instead).
    `cv=1` opens the My CV panel instead of `app`/`job`, with `v` (a saved version shown) or `compare`
    (a version compared with cv.md).
    Unknown values fall back to the defaults, so an old bookmark (view=board) never fails."""
    def first(key: str) -> str:
        return (query.get(key) or [""])[0].strip()

    app, stage, view = first("app"), first("stage"), first("view")
    cv = first("cv") == "1"  # the My CV panel; it wins over app and job (one panel at a time)
    version, compare = first("v"), first("compare")
    return {
        "view": view if view in VIEWS else "pipeline",
        "app": int(app) if app.isdigit() and not cv else None,
        "job": (first("job") or None) if not cv else None,
        "cv": cv,
        "cv_v": int(version) if cv and version.isdigit() else None,
        "cv_cmp": int(compare) if cv and compare.isdigit() and not version else None,
        "stage": stage if stage in STAGE_GROUPS else "",
        "closed": first("closed") == "1",
        "employer": first("employer") if first("employer") in db.EMPLOYER_KINDS else "",
        "score": offer_score(query),
        "loc": tuple(loc for loc in getjo.LOCATIONS if loc in query.get("loc", [])),
        "q": first("q"),
        "skipped": first("skipped") == "1",
        "sel": parse_sel(query.get("sel", [])),
    }


def parse_sel(values: list[str]) -> tuple[str, ...]:
    """The ticked targets from the query: "a:<app id>", "j:<getjo job id>" or "cv", in order, without
    repeats, at most db.MAX_TARGETS. Malformed values are dropped (unknown ids later, by home_page)."""
    keep: list[str] = []
    for v in values:
        v = v.strip()
        ok = v == "cv" or (v.startswith("a:") and v[2:].isdigit()) or (v.startswith("j:") and len(v) > 2)
        if ok and v not in keep:
            keep.append(v)
    return tuple(keep[:db.MAX_TARGETS])


def toggle_sel(sel: tuple[str, ...], key: str) -> tuple[str, ...] | None:
    """`sel` with `key` ticked or unticked; None when ticking it would pass the limit."""
    if key in sel:
        return tuple(k for k in sel if k != key)
    return None if len(sel) >= db.MAX_TARGETS else (*sel, key)


def auto_sel(f: dict, key: str) -> tuple[str, ...]:
    """Opening a job ticks it only when nothing is ticked yet."""
    return f["sel"] or (key,)


def home_url(f: dict, **changes: object) -> str:
    """An Applications page URL for state `f` with `changes` (e.g. app=3, job=None, view="offers").
    Only the current view's filters go in, default values are left out, and app wins over job. The
    My CV panel and an application's or job's panel exclude each other: opening one closes the other."""
    s = {**f, **changes}
    if changes.get("app") is not None or changes.get("job"):
        s["cv"] = False
    if changes.get("cv"):
        s["app"], s["job"] = None, None
    if not s["cv"]:
        s["cv_v"] = s["cv_cmp"] = None
    pairs: list[tuple[str, object]] = []
    if s["view"] == "offers":
        pairs.append(("view", "offers"))
        if s["score"] != getjo.DEFAULT_THRESHOLD:
            pairs.append(("score", s["score"]))
        pairs += [("loc", loc) for loc in s["loc"]]
        if s["q"]:
            pairs.append(("q", s["q"]))
        if s["skipped"]:
            pairs.append(("skipped", "1"))
    else:
        if s["stage"]:
            pairs.append(("stage", s["stage"]))
        if s["closed"]:
            pairs.append(("closed", "1"))
        if s.get("employer"):
            pairs.append(("employer", s["employer"]))
    if s["cv"]:
        pairs.append(("cv", "1"))
        if s["cv_v"] is not None:
            pairs.append(("v", s["cv_v"]))
        elif s["cv_cmp"] is not None:
            pairs.append(("compare", s["cv_cmp"]))
    elif s["app"] is not None:
        pairs.append(("app", s["app"]))
    elif s["job"]:
        pairs.append(("job", s["job"]))
    pairs += [("sel", k) for k in s.get("sel", ())]
    return "/" + (f"?{urlencode(pairs)}" if pairs else "")


def _snapshot(job_id: str, decision: dict) -> dict:
    """A decided job getjo no longer has, from the copy stored with the decision."""
    return {"id": job_id, "first_seen": None, "description": None,
            **{k: decision[k] for k in ("company", "title", "location", "score", "url", "source")},
            **{k: None for k in getjo.STATE_COLUMNS}}


def _read_getjo(settings: Settings, conn: sqlite3.Connection, f: dict, now: str) -> dict:
    """Everything the page needs from getjo, in one read-only connection: `new` (open jobs at the
    minimum score, not tracked or decided), `jobs` (the rows of tracked jobs, of skipped ones when
    listed, and of the open panel's job, open or closed), `description` (the panel job's) and
    `error` (why getjo could not be read; the pipeline still works)."""
    found: dict = {"new": [], "jobs": {}, "description": None, "error": None}
    decisions = db.job_decisions(conn)
    wanted = (db.linked_job_ids(conn) | ({f["job"]} if f["job"] else set())
              | {k[2:] for k in f["sel"] if k.startswith("j:")})  # ticked offers need their names
    if f["view"] == "offers" and f["skipped"]:
        wanted |= {k for k, d in decisions.items() if d["decision"] == "skipped"}
    try:
        with closing(getjo.connect_ro(settings.getjo_db)) as gconn:
            found["new"] = getjo.matches(gconn, db.excluded_job_ids(conn), f["score"], now=now)
            found["jobs"] = getjo.jobs_by_id(gconn, wanted)
            if f["job"] in found["jobs"]:
                found["description"] = getjo.job_description(gconn, f["job"])
    except getjo.GetjoUnavailable as e:
        found["error"] = f"getjo jobs are not available: {e}."
    except sqlite3.Error as e:
        found["error"] = f"Could not read the getjo database: {e}."
    found["decisions"] = decisions
    return found


def _matches_filters(job: dict, f: dict) -> bool:
    """New offers' location and search filters (the score filter is applied by getjo.matches)."""
    q = f["q"].lower()
    if q and q not in (job["company"] or "").lower() and q not in (job["title"] or "").lower():
        return False
    return not f["loc"] or bool(getjo.location_tags(job["location"]) & set(f["loc"]))


def offer_rows(found: dict, f: dict) -> list[tuple[dict, dict | None]]:
    """(job, decision) pairs for the list: the new offers (newest day first, then highest score), or
    with `skipped` the skipped jobs at any score, open or closed (latest skip first)."""
    if not f["skipped"]:
        jobs = [j for j in found["new"] if _matches_filters(j, f)]
        jobs.sort(key=lambda j: j["id"])
        jobs.sort(key=lambda j: ((j["first_seen"] or "")[:10], j["score"] or 0), reverse=True)
        return [(j, None) for j in jobs]
    pairs = []
    for job_id, d in found["decisions"].items():
        if d["decision"] != "skipped":
            continue
        job = found["jobs"].get(job_id) or _snapshot(job_id, d)
        if _matches_filters(job, f):
            pairs.append((job, d))
    return sorted(pairs, key=lambda p: p[1]["decided_at"] or "", reverse=True)


def _view_switch(f: dict, pipeline: int, offers: int | None) -> str:
    """Pipeline N · New offers M: links (the script remembers the choice), so it works without it."""
    tabs = []
    for view, label, n in (("pipeline", "Pipeline", pipeline), ("offers", "New offers", offers)):
        active = f["view"] == view
        href = home_url(f, view=view)
        count = f' <span class="count">{n}</span>' if n is not None else ""
        tabs.append(f'<a class="view-tab{" is-active" if active else ""}" href="{h(href)}" data-view="{view}"'
                    f'{CURRENT if active else ""}>{label}{count}</a>')
    return f'<nav class="view-switch" aria-label="List">{"".join(tabs)}</nav>'


def _offer_filters(f: dict) -> str:
    """New offers' filters in the list header: a GET form, so every filtered view is a URL. The
    script reloads on a select or checkbox change and narrows the rows as the search is typed."""
    scores = "".join(f'<option value="{n}"{" selected" if n == f["score"] else ""}>score ≥ {n}</option>'
                     for n in range(10, 0, -1))
    locs = "".join(
        f'<label class="check"><input type="checkbox" name="loc" value="{loc}"'
        f'{" checked" if loc in f["loc"] else ""}> {label}</label>'
        for loc, label in LOCATION_LABELS.items())
    score = ("" if f["skipped"] else
             f'<select name="score" aria-label="Minimum score">{scores}</select>')
    return (
        '<form method="get" action="/" class="offer-filters" data-live-filters>'
        '<input type="hidden" name="view" value="offers">'
        + ('<input type="hidden" name="skipped" value="1">' if f["skipped"] else "")
        + "".join(f'<input type="hidden" name="sel" value="{h(k)}">' for k in f["sel"])  # ticks stay ticked
        + score
        + f'<span class="checks" role="group" aria-label="Location">{locs}</span>'
        f'<input type="search" name="q" value="{h(f["q"])}" placeholder="Company or role…" aria-label="Search company or role">'
        + _button("Filter", "secondary", "sm") + "</form>"
    )


def _skipped_toggle(f: dict) -> str:
    """Show skipped / Hide skipped, in the list header next to the switch (a link)."""
    href = home_url(f, skipped=not f["skipped"], app=None, job=None)
    return (f'<a class="view-btn{" is-active" if f["skipped"] else ""}" href="{h(href)}">'
            f'{"Hide skipped" if f["skipped"] else "Show skipped"}</a>')


def _job_chips(state: dict) -> str:
    """closed · may be closed · expires in N days, from getjo.job_state."""
    if state["closed"]:
        return _badge("closed", "warning")
    chips = []
    if state["may_be_closed"]:
        chips.append('<span class="badge badge-neutral" title="getjo has not listed it for a few days">may be closed</span>')
    if state["expires_in"] is not None:
        days = state["expires_in"]
        chips.append(_badge("expires today" if days <= 0 else f"expires in {days} day{'s' if days != 1 else ''}", "warning"))
    return " ".join(chips)


def _offer_list(rows: list[tuple[dict, dict | None]], f: dict, found: dict, now: str, stale_days: int) -> str:
    """The New offers table: score, company (a link to the panel), role with its chips, location; with
    Show skipped the reason and Undo skip instead of the location."""
    if found["error"]:
        return _banner(found["error"], "warning")
    if not rows:
        if f["q"] or f["loc"]:
            return _empty("Nothing matches these filters.", "Clear the search or the location boxes.")
        if f["skipped"]:
            return _empty("Nothing skipped.")
        return _empty("No new job offers.", f"getjo jobs scored {f['score']} or more appear here after each poll.")
    here = home_url(f)
    trs = []
    for job, d in rows:
        key = f'j:{job["id"]}'
        href = home_url(f, job=job["id"], app=None, sel=auto_sel(f, key))
        chips = _job_chips(getjo.job_state(job, now, stale_days))
        role = (f'<span class="clamp" title="{h(job["title"])}">{h(job["title"])}</span>'
                + (f'<span class="chips">{chips}</span>' if chips else ""))
        if d is None:
            last = (f'<span class="ellip" title="{h(job["location"])}">{h(job["location"])}</span>',)
        else:
            reason = SKIP_REASON_LABELS.get(d["reason"] or "", "")
            comment = f' <span class="muted">— {h(d["comment"])}</span>' if d["comment"] else ""
            last = (f'<span class="ellip">{h(reason)}{comment}</span>',
                    '<form method="post" action="/offers/unskip" class="inline" data-ajax="refresh">'
                    f'<input type="hidden" name="job_id" value="{h(job["id"])}"><input type="hidden" name="return" value="{h(here)}">'
                    + _button("Undo skip", "ghost", "sm") + "</form>")
        cells = (_sel_tick(f, key, f'{job["company"]} — {job["title"]}'), _score_badge(job["score"]),
                 f'<a href="{h(href)}">{h(job["company"])}</a>', role, *last)
        focus = ' class="is-focus"' if job["id"] == f["job"] and f["app"] is None else ""
        trs.append(f'<tr data-href="{h(href)}" data-job="{h(job["id"])}" data-key="{h(key)}" data-company="{h((job["company"] or "").lower())}" '
                   f'data-role="{h((job["title"] or "").lower())}"{focus}>' + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    cols = ("Score", "Company", "Role", "Skipped", "") if f["skipped"] else ("Score", "Company", "Role", "Location")
    head = (SEL_HEAD
            + "".join(f'<th scope="col" class="col-{(c or "undo").lower()}">{c}</th>' for c in cols))
    return (f'<div class="table-wrap app-list offer-list"><table id="offer-list"><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(trs)}</tbody></table></div>')


# The tick column's heading: a visually hidden label (the ticks say what they do on hover).
SEL_HEAD = '<th scope="col" class="col-sel"><span class="sr-only">Ask about</span></th>'


def _offer_panel(job: dict, decision: dict | None, brief: str, f: dict, now: str,
                 stale_days: int, busy: bool = False) -> str:
    """The side panel for a job not yet tracked: header (company, role, score, location, seen date,
    posting link, open/closed chips, the Ask-about tick), Interested · Skip (or Skipped with Undo
    skip) and the job brief (or the tidied description while it is written: `brief`, from
    _brief_section). `busy`: a brief is being written, so the page polls."""
    here = home_url(f, job=job["id"], app=None)
    close = home_url(f, job=None, app=None)
    state = getjo.job_state(job, now, stale_days)
    sub = " ".join(x for x in (
        _score_badge(job["score"]) if job["score"] is not None else "",
        f'<span>{h(job["location"])}</span>' if job["location"] else "",
        f'<span class="muted">seen {h(_short_day(job["first_seen"]))}</span>' if job["first_seen"] else "",
        f'<span>{_link(job["url"], "Job posting ↗")}</span>' if job["url"] and urlsplit(job["url"]).scheme in ("http", "https") else "",
        _job_chips(state),
    ) if x)
    keep = f'<input type="hidden" name="job_id" value="{h(job["id"])}"><input type="hidden" name="return" value="{h(here)}">'
    if decision and decision["decision"] == "skipped":
        reason = SKIP_REASON_LABELS.get(decision["reason"] or "", "")
        buttons = (f'<button class="btn btn-ghost btn-sm is-done" disabled>{h("Skipped · " + reason if reason else "Skipped")}</button>'
                   f'<form method="post" action="/offers/unskip" class="inline" data-ajax="refresh">{keep}'
                   + _button("Undo skip", "secondary", "sm") + "</form>")
    else:
        skip_url = "/offers/skip?" + urlencode([("job_id", job["id"]), ("return", here)])
        buttons = (f'<form method="post" action="/offers/interested" class="inline" data-ajax="interested">{keep}'
                   + _button("Interested", size="sm") + "</form>"
                   f'<a class="btn btn-ghost btn-sm" href="{h(skip_url)}" data-skip="{h(job["id"])}" data-skip-field="job_id" '
                   f'data-skip-action="/offers/skip" data-skip-label="{h(job["company"])} — {h(job["title"])}" '
                   'data-after="refresh">Skip</a>')
    tick = _sel_tick(f, f'j:{job["id"]}', f'{job["company"]} — {job["title"]}', "Ask about this")
    return (
        f'<aside class="panel panel-offer" id="panel" aria-labelledby="panel-title" data-job="{h(job["id"])}"'
        f'{" data-busy" if busy else ""}>'
        f'<header class="panel-head"><div><h2 id="panel-title">{h(job["company"])}</h2>'
        f'<p class="panel-role">{h(job["title"])}</p><p class="panel-sub">{sub}</p></div>'
        f'<span class="panel-head-actions">{tick}<a class="panel-ask btn btn-secondary btn-sm" href="#agent" data-ask-agent>'
        'Ask the assistant</a>'
        f'<a class="panel-close btn btn-ghost btn-sm" href="{h(close)}" aria-label="Close" data-close-panel>✕</a></span></header>'
        '<div class="panel-body">'
        f'<section class="shortcuts" aria-label="Shortcuts"><div class="action-row">{buttons}</div>'
        + _draft_form(here, job_id=job["id"]) + "</section>"
        + brief
        + "</div></aside>"
    )


_RULE = re.compile(r"^\s*[-=_*~·•]{3,}\s*$")  # "----" separators some boards put between parts
_BULLET = re.compile(r"^\s*(?:[-*•·–▪◦●]|\d{1,2}[.)])\s+")


def _is_heading(line: str) -> bool:
    """A short line on its own that names a part: "About the team", "Requirements:", "WHAT YOU'LL DO"."""
    text = line.strip()
    if not text or len(text) > 60 or _BULLET.match(text) or text[-1] in ".,;!?":
        return False
    if text.endswith(":"):
        return True
    if ":" in text:  # "Job Description: R&D" is a sentence, not a heading
        return False
    words = [w for w in text.split() if w[0].isalpha()]
    small = ("a", "an", "and", "of", "the", "to", "in", "for", "with", "on", "at", "or", "we", "you")
    title = len(words) >= 2 and all(w[0].isupper() or w.lower() in small for w in words)
    return len(text.split()) <= 6 and (text.isupper() and len(words) >= 1 or title)


def _description(text: str) -> str:
    """A getjo description made readable, every piece escaped: separators ("----") dropped, short
    lines that name a part become headings, "-" / "•" / "1." lines become lists, the rest paragraphs
    with their line breaks kept (CSS pre-line). Some boards send entities (Workday's "&#xa;" line
    breaks), so the text is unescaped first and escaped again here, never shown as "&amp;#xa;"."""
    text = html.unescape(text).replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    para: list[str] = []
    items: list[str] = []

    def flush() -> None:
        if para:
            out.append(f"<p>{h(chr(10).join(para))}</p>")
            para.clear()
        if items:
            out.append("<ul>" + "".join(f"<li>{h(i)}</li>" for i in items) + "</ul>")
            items.clear()

    lines = text.split("\n")
    for n, line in enumerate(lines):
        rest = [x for x in lines[n + 1:] if x.strip() and not _RULE.match(x)]
        short = line.strip()
        # a short line opening a block, with more text after it, names that part ("About the team")
        opens = (not para and not items and bool(rest) and short[:1].isalpha() and len(short) <= 40
                 and len(short.split()) <= 5 and short[-1:] not in ".,;!?" and ":" not in short[:-1]
                 and not _BULLET.match(line))
        if _RULE.match(line) or not line.strip():
            flush()
        elif opens:
            out.append(f"<h4>{h(line.strip().rstrip(':'))}</h4>")
        elif _BULLET.match(line):
            if para:
                flush()
            items.append(_BULLET.sub("", line, count=1).strip())
        elif _is_heading(line) and not para:
            flush()
            out.append(f"<h4>{h(line.strip().rstrip(':'))}</h4>")
        else:
            if items:
                flush()
            para.append(line.strip())
    flush()
    return "".join(out)


# The divider between the left column and the panel (a window splitter). Rendered on every
# Applications page, since the script may open a panel later; CSS shows it only with the script,
# from 801px wide and while a panel is open. app.js drags it and keeps the width per browser.
def _split_handle(controls: str, label: str, key: str, names: str) -> str:
    """A divider between two columns; app.js keeps its share in localStorage under `key`. `names` is
    "left,right" for the screen reader text ("List 40%, panel 60%")."""
    return (f'<div class="apps-split" role="separator" aria-orientation="vertical" aria-controls="{h(controls)}" '
            f'aria-label="{h(label)}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="50" '
            f'data-split-key="{h(key)}" data-split-names="{h(names)}" '
            'tabindex="0" title="Drag to resize · double-click for half and half"></div>')


SPLIT_HANDLE = _split_handle("apps-main", "Resize the list and the panel", "apps-split", "List,panel")


def _skip_dialog() -> str:
    """The Skip pop-up: one button per reason and an optional comment. The clicked Skip link names
    where it posts and which field it fills (`data-skip-action` and `data-skip-field`: /offers/skip
    and job_id for an offer, /applications/skip and app_id for an application); the script sets
    them and return. Without the script it never opens."""
    reasons = "".join(
        f'<button class="btn btn-secondary btn-sm" name="reason" value="{k}">{h(v)}</button>'
        for k, v in SKIP_REASON_LABELS.items())
    return (
        '<dialog id="skip-dialog" class="dialog" aria-labelledby="skip-title">'
        '<form method="post" action="/applications/skip" class="skip-form">'
        '<input type="hidden" name="app_id" value=""><input type="hidden" name="job_id" value="">'
        '<input type="hidden" name="return" value="">'
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
    back = _return_to((query.get("return") or [""])[0], "/")
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
    cancel = _return_to(back, "/")
    form = (
        '<form method="post" action="/offers/skip" class="add">'
        f'<input type="hidden" name="job_id" value="{h(job["id"])}"><input type="hidden" name="return" value="{h(back)}">'
        f'<select name="reason" required aria-label="Skip reason"><option value="">skip reason</option>{reasons}</select>'
        f'<input name="comment" maxlength="{MAX_COMMENT}" placeholder="comment (optional)" aria-label="Comment">'
        + _button("Skip") + f'<a class="btn btn-ghost" href="{h(cancel)}">Cancel</a></form>'
    )
    return "Skip a job", f'{job["company"]} — {job["title"]}', _card("Why skip it?", form, cls="card-form")


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


EMPLOYER_LABELS = {"product": "Product", "client": "Client"}
EMPLOYER_RANK = {"product": 0, None: 1, "client": 2}  # within a stage: product, unknown, then client


def employer_rows(rows: list[dict], f: dict) -> list[dict]:
    """The pipeline's rows for the employer filter (product, client, or all when unset)."""
    return [r for r in rows if r["app"]["employer_kind"] == f["employer"]] if f["employer"] else rows


def employer_order(rows: list[dict]) -> list[dict]:
    """Rows already in list_order, regrouped product → unknown → client (a stable sort keeps the order inside)."""
    return sorted(rows, key=lambda r: EMPLOYER_RANK.get(r["app"]["employer_kind"], 1))


def _employer_badge(kind: str | None) -> str:
    """A small Product / Client label; nothing while unknown."""
    if kind not in EMPLOYER_LABELS:
        return ""
    title = "In-house product work" if kind == "product" else "Client delivery: consultancy or agency"
    return f'<span class="emp-kind emp-{kind}" title="{title}">{EMPLOYER_LABELS[kind]}</span>'


def _employer_filter(f: dict) -> str:
    """All · Product · Client next to Show closed (links, so they work without the script)."""
    links = []
    for kind, label in (("", "All"), ("product", "Product"), ("client", "Client")):
        on = f["employer"] == kind
        links.append(f'<a class="seg-btn{" is-active" if on else ""}" href="{h(home_url(f, employer=kind))}" '
                     f'data-employer="{kind or "all"}"{CURRENT if on else ""}>{label}</a>')
    return f'<nav class="seg" aria-label="Filter by employer">{"".join(links)}</nav>'


def home_page(settings: Settings, conn: sqlite3.Connection, today: str,
              query: dict[str, list[str]]) -> tuple[str, str, str]:
    """The landing page. Left: a switch between the pipeline (grouped by stage, with a stage strip
    that filters it) and New offers (getjo jobs not yet tracked), with the Applications Assistant box under it.
    Right: the side panel for the application named by ?app=ID or else the job named by ?job=ID (a
    tracked job opens its application; an unknown id is ignored)."""
    f = home_filters(query)
    now = _now(today)
    found = _read_getjo(settings, conn, f, now)
    tasks = db.app_tasks(conn, "todo")
    latest = db.latest_comments(conn)
    research_found = db.research_view(conn)
    rows = []
    for a in db.board_applications(conn):
        own = tasks.get(a["id"], [])
        job = found["jobs"].get(a["getjo_job_id"]) if a["status"] in ("to_apply", "applied") else None
        rows.append({"app": a, "group": group_of(a["status"]), "next": own[0] if own else None,
                     "tasks": own, "comment": latest.get(a["id"]),
                     "days": (date.fromisoformat(today) - date.fromisoformat(a["since"])).days,
                     "closed": bool(job) and getjo.job_state(job, now, settings.stale_days)["closed"],
                     "found": research_found.get(a["id"]) or {"profile": None, "fit": None, "tailored": None,
                                                              "open": set()}})
    if f["app"] is None and f["job"]:
        f["app"] = db.app_ids_by_job(conn).get(f["job"])  # a tracked job opens its application
    focus = next((r for r in rows if r["app"]["id"] == f["app"]), None)
    if focus is None:
        f["app"] = None
    job, decision = None, None
    if focus is None and f["job"]:
        decision = found["decisions"].get(f["job"])
        job = found["jobs"].get(f["job"]) or (_snapshot(f["job"], decision) if decision else None)
    if focus is None and job is None:
        f["job"] = None
    else:
        f["job"] = job["id"] if job else None
    targets = sel_targets(conn, f, rows, found)
    f["sel"] = tuple(t["key"] for t in targets)  # unknown ids dropped, a tracked job named by its application
    briefs = _queue_briefs(settings, conn, found, job if focus is None and decision is None else None)
    open_count = sum(1 for r in rows if r["group"] != "closed")
    closed_count = len(rows) - open_count
    offers = None if found["error"] else len(found["new"])
    if f["view"] == "offers":
        body = _offer_filters(f) + _offer_list(offer_rows(found, f), f, found, now, settings.stale_days)
    elif not rows:
        body = _empty("No applications yet.", "Mark a job Interested in New offers, or use /applied.")
    else:
        mine = employer_rows(rows, f)
        body = _stage_strip(mine, f) + _app_list(mine, f)
    switch = _view_switch(f, open_count, offers) + (_skipped_toggle(f) if f["view"] == "offers" else "")
    list_card = (f'<section class="card card-pipeline card-list" data-view="{f["view"]}"><header class="card-head">{switch}'
                 f'</header><div class="card-body">{body}</div></section>')
    if f["cv"]:
        panel = _cv_panel(conn, settings, f)
    elif focus:
        job_id = focus["app"]["getjo_job_id"]
        brief = _brief_section(job_id, briefs, _read_description(settings, job_id), home_url(f)) if job_id else ""
        panel = _panel(conn, focus, today, f, brief, bool(job_id) and job_id in briefs["open"], settings.private_dir)
    elif job:
        brief = _brief_section(job["id"], briefs, found["description"], home_url(f),
                               gone=not job["first_seen"] and found["description"] is None)
        panel = _offer_panel(job, decision, brief, f, now, settings.stale_days, job["id"] in briefs["open"])
    else:
        panel = ""
    agent = _agent_box(conn, settings, f, targets)
    content = (f'<div class="apps"><div class="apps-main" id="apps-main">{list_card}{agent}</div>{SPLIT_HANDLE}{panel}</div>'
               + _skip_dialog())
    new = f" · {offers} new offer{'s' if offers != 1 else ''}" if offers is not None else ""
    my_cv = (f'<div class="page-head-actions">{_sel_tick(f, "cv", "my CV")}'
             f'<a class="btn btn-secondary btn-sm my-cv" href="{h(home_url(f, cv=True))}" '
             f'data-open-panel{CURRENT if f["cv"] else ""}>My CV</a></div>')
    return "Applications", f"{open_count} open · {closed_count} closed{new} · {today}", content, my_cv


def sel_targets(conn: sqlite3.Connection, f: dict, rows: list[dict], found: dict) -> list[dict]:
    """The ticked targets that exist, in order: dicts with key ("a:3", "j:…", "cv") and label (company —
    role, or "My CV"). A ticked job that is tracked now is named by its application; unknown ids (an
    application deleted by Undo Interested, a job getjo and the decisions no longer have) are dropped."""
    apps = {r["app"]["id"]: r["app"] for r in rows}
    tracked = db.app_ids_by_job(conn)
    out: list[dict] = []
    for key in f["sel"]:
        if key == "cv":
            out.append({"key": "cv", "label": "My CV"})
            continue
        if key.startswith("j:") and tracked.get(key[2:]) in apps:
            key = f"a:{tracked[key[2:]]}"
        if key.startswith("a:"):
            app = apps.get(int(key[2:]))
            if app:
                out.append({"key": key, "label": f'{app["company"]} — {app["role"]}'})
            continue
        decision = found["decisions"].get(key[2:])
        job = found["jobs"].get(key[2:]) or (_snapshot(key[2:], decision) if decision else None)
        if job:
            out.append({"key": key, "label": f'{job["company"]} — {job["title"]}'})
    seen: set[str] = set()
    return [t for t in out if not (t["key"] in seen or seen.add(t["key"]))]


def _sel_tick(f: dict, key: str, label: str, text: str = "") -> str:
    """The ☐ that ticks `key` for the Applications Assistant box (a link changing `sel`, so it works without the script;
    at the limit it only says so). `text` adds a visible label (the panel header's "Ask about this")."""
    on = key in f["sel"]
    nxt = toggle_sel(f["sel"], key)
    href = home_url(f, sel=nxt) if nxt is not None else _return_to(home_url(f), "/", "sel_limit")
    words = f'<span class="sel-text">{h(text)}</span>' if text else ""
    return (f'<a class="sel-tick{" is-on" if on else ""}{" has-text" if text else ""}" href="{h(href)}" role="checkbox" '
            f'aria-checked="{"true" if on else "false"}" data-sel="{h(key)}" aria-label="Ask the assistant about {h(label)}" '
            f'title="Tick to ask the assistant about it"><span class="box" aria-hidden="true"></span>{words}</a>')


def _queue_briefs(settings: Settings, conn: sqlite3.Connection, found: dict, opened: dict | None) -> dict:
    """Queue job briefs the agent has not been asked for: every new offer scored at least
    brief_auto_min_score (in the background), and an untracked offer whose panel is open (`opened`).
    Pings the Applications Assistant once when any were added. Returns {job id: brief…} plus "open" (the
    job ids with a brief request open)."""
    wanted = [] if found["error"] else [j["id"] for j in found["new"] if (j["score"] or 0) >= settings.brief_auto_min_score]
    if opened is not None:
        wanted.append(opened["id"])
    added = db.queue_missing_briefs(conn, wanted)
    if added:
        log.info("%d job brief(s) queued", len(added))
        seq = assistant.pick(settings, conn, "research")
        if seq:
            db.assign_briefs(conn, added, seq)
        assistant.notify_briefs_in_background(settings, len(added), seq)
    return {**db.job_briefs(conn), "open": {r["getjo_job_id"] for r in db.open_briefs(conn)}}


def _read_description(settings: Settings, job_id: str) -> str | None:
    """A getjo job's description (read-only), or None."""
    try:
        with closing(getjo.connect_ro(settings.getjo_db)) as gconn:
            return getjo.job_description(gconn, job_id)
    except (getjo.GetjoUnavailable, sqlite3.Error):
        return None


def _brief_section(job_id: str, briefs: dict, description: str | None, here: str, gone: bool = False) -> str:
    """The top of a job's panel: the agent's brief (key facts line as chips, then its sections), with
    Redo and the original description behind Show original; without a brief, Write brief (or
    "Writing brief…") and the original, tidied and collapsed (Show all)."""
    brief, busy = briefs.get(job_id), job_id in briefs["open"]
    keep = (f'<input type="hidden" name="job_id" value="{h(job_id)}"><input type="hidden" name="return" value="{h(here)}">')
    if busy:
        action = ('<button class="btn btn-ghost btn-sm is-busy" disabled><span class="spinner" aria-hidden="true"></span>'
                  'Writing brief…</button>')
    else:
        action = (f'<form method="post" action="/offers/brief" class="inline" data-ajax="refresh">{keep}'
                  + _button("Redo brief" if brief else "Write brief", "ghost" if brief else "secondary", "sm") + "</form>")
    if description and description.strip():
        original = _description(description)
    elif gone:
        original = '<p class="muted">getjo no longer has this job; use the posting link.</p>'
    else:
        original = '<p class="muted">No description in getjo; use the posting link.</p>'
    if brief:
        facts = "".join(f'<span class="fact">{h(x.strip())}</span>' for x in brief["facts"].split("·") if x.strip())
        rest = brief["brief"].strip()[len(brief["facts"]):]
        body = (f'<p class="brief-facts" aria-label="Key facts">{facts}</p>'
                f'<div class="brief doc">{render_markdown(rest) if rest.strip() else ""}</div>'
                f'<details class="original"><summary>Show original</summary><div class="description">{original}</div></details>')
    else:
        body = f'<div class="description is-clamped" data-clamp>{original}</div>'
    title = "Job brief" if brief else "Job description"
    return (f'<section class="panel-brief" aria-labelledby="brief-title"><h3 id="brief-title">{title}'
            f'<span class="brief-actions">{action}</span></h3>{body}</section>')


def _stage_strip(rows: list[dict], f: dict) -> str:
    """Counts per stage group; each links to that group's filter (the active one clears it). Show /
    Hide closed lists the closed applications too (links, so it works without the script)."""
    pills = []
    for group, label in GROUP_LABELS.items():
        n = sum(1 for r in rows if r["group"] == group)
        active = f["stage"] == group
        href = home_url(f, stage="" if active else group)
        pills.append(f'<a class="stage-pill{" is-active" if active else ""}" href="{h(href)}" data-stage="{group}"'
                     f'{CURRENT if active else ""}>{h(label)} <span class="count">{n}</span></a>')
    toggle = ""
    if f["stage"] != "closed":
        href = home_url(f, closed=not f["closed"])
        toggle = (f'<a class="view-btn{" is-active" if f["closed"] else ""}" href="{h(href)}" data-closed-toggle>'
                  f'{"Hide closed" if f["closed"] else "Show closed"}</a>')
    return (f'<div class="stage-strip"><nav class="stage-pills" aria-label="Filter by stage">{"".join(pills)}</nav>'
            f'<span class="strip-end">{_employer_filter(f)}{toggle}</span></div>')


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
        mine = employer_order(list_order([r for r in shown if r["app"]["status"] == status]))
        if not mine:
            continue
        head = (f'<tr class="group-row"><th colspan="6" scope="rowgroup">{h(_status_label(status).capitalize())}'
                f' <span class="count">{len(mine)}</span></th></tr>')
        bodies.append(f'<tbody data-group="{status}">{head}{"".join(_list_row(r, f) for r in mine)}</tbody>')
    if not bodies:
        return _empty("No applications here.", "Clear the stage filter or show the closed ones.")
    head = SEL_HEAD + "".join(f'<th scope="col" class="col-{c.lower().replace(" ", "-")}">{c}</th>'
                              for c in ("Company", "Role", "Stage", "Next task", "Comment"))
    return (f'<div class="table-wrap app-list"><table id="app-list"><thead><tr>{head}</tr></thead>'
            f'{"".join(bodies)}</table></div>')


def _list_row(r: dict, f: dict) -> str:
    app, nxt, comment = r["app"], r["next"], r["comment"]
    key = f'a:{app["id"]}'
    href = home_url(f, app=app["id"], sel=auto_sel(f, key))
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
    closed = f'<span class="chips">{_job_closed_badge()}</span>' if r["closed"] else ""
    cells = (_sel_tick(f, key, f'{app["company"]} — {app["role"]}'), f'<a href="{h(href)}">{h(app["company"])}</a>{_employer_badge(app["employer_kind"])}',
             f'<span class="clamp" title="{h(app["role"])}">{h(app["role"])}</span>{closed}', stage, next_cell, comment_cell)
    focus = ' class="is-focus"' if app["id"] == f["app"] else ""
    return (f'<tr id="app-{h(app["id"])}" data-href="{h(href)}" data-app="{h(app["id"])}" data-key="{h(key)}"{focus}>'
            + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")


def _job_closed_badge() -> str:
    """An Interested or Applied application whose getjo job has closed or expired stays listed."""
    return '<span class="badge badge-warning" title="getjo says the job is closed or expired">closed</span>'


def _age_text(days: int) -> str:
    return f'{days} day{"s" if days != 1 else ""} in this stage' if days > 0 else "new today"


def _panel(conn: sqlite3.Connection, r: dict, today: str, f: dict, brief: str = "", brief_busy: bool = False,
           private_dir: Path | None = None) -> str:
    """The side panel, top to bottom: header (with the Ask-about tick), the job brief (`brief`, from
    _brief_section; none without a getjo job), the latest comment (read-only), shortcut buttons, tasks
    (tick to mark done), the agent's results (company research, CV fit, tailored CV versions) and
    the stage history. Everything else is asked of the agent in the Applications Assistant box under the list."""
    app, found = r["app"], r["found"]
    here = home_url(f, app=app["id"])
    link = (f'<span>{_link(app["link"], "Job posting ↗")}</span>'
            if app["link"] and urlsplit(app["link"]).scheme in ("http", "https") else "")
    close = home_url(f, app=None, job=None)
    versions = db.tailored_versions(conn, app["id"])
    busy = " data-busy" if found["open"] or brief_busy else ""
    tick = _sel_tick(f, f'a:{app["id"]}', f'{app["company"]} — {app["role"]}', "Ask about this")
    return (
        f'<aside class="panel" id="panel" aria-labelledby="panel-title" data-app="{h(app["id"])}" '
        f'data-status="{h(app["status"])}"{busy}>'
        f'<header class="panel-head"><div><h2 id="panel-title">{h(app["company"])}{_employer_badge(app["employer_kind"])}</h2>'
        f'<p class="panel-role">{h(app["role"])}</p>'
        f'<p class="panel-sub">{_status_badge(app["status"])} {step_dots(app["status"], label=False)}'
        f'{_job_closed_badge() if r["closed"] else ""}'
        f'<span class="muted">{h(_age_text(r["days"]))}</span>{link}</p>{_employer_select(app, here)}</div>'
        f'<span class="panel-head-actions">{tick}<a class="panel-ask btn btn-secondary btn-sm" href="#agent" data-ask-agent>'
        'Ask the assistant</a>'
        f'<a class="panel-close btn btn-ghost btn-sm" href="{h(close)}" aria-label="Close" data-close-panel>✕</a></span></header>'
        f'<div class="panel-body">'
        + brief
        + _comments_section(conn, app["id"])
        + _shortcuts(conn, app, found, versions, here)
        + _panel_tasks(r["tasks"], today, here)
        + _research_cards(app, found, versions, here, private_dir,
                          db.cv_check(conn, app["id"], versions[-1]["version"]) if versions else None,
                          db.get_notes(conn, app["id"]))
        + _history(conn, app["id"])
        + "</div></aside>"
    )


def _employer_select(app: dict, here: str) -> str:
    """Product / Client / Unknown for the application, saved on change (a Save button without the script)."""
    current = app["employer_kind"] or ""
    options = "".join(f'<option value="{v}"{" selected" if v == current else ""}>{label}</option>'
                      for v, label in (("product", "Product"), ("client", "Client"), ("", "Unknown")))
    return ('<form method="post" action="/applications/employer" class="inline employer-form" data-ajax="refresh">'
            f'<input type="hidden" name="app_id" value="{h(app["id"])}"><input type="hidden" name="return" value="{h(here)}">'
            '<label class="muted" for="employer-kind">Employer</label> '
            f'<select id="employer-kind" name="employer_kind" data-autosubmit>{options}</select>'
            '<noscript>' + _button("Save", "ghost", "sm") + '</noscript></form>')


def _comments_section(conn: sqlite3.Connection, app_id: int) -> str:
    """The latest comment with its date, and how many came before it. Read-only: the agent adds,
    lists, amends and removes them (`cli comment`, `cli comments`)."""
    found = db.comments(conn, app_id)
    if found:
        latest = found[0]
        earlier = (f'<p class="muted">{len(found) - 1} earlier comment{"s" if len(found) > 2 else ""}; '
                   'ask the assistant to list them.</p>' if len(found) > 1 else "")
        body = (f'<p class="comment-latest"><span class="mono muted">{h(latest["at"][:10])}</span> '
                f'<span class="comment-body">{h(latest["body"])}</span></p>{earlier}')
    else:
        body = '<p class="muted">No comments yet. Ask the assistant to add one.</p>'
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
                       'data-skip-field="app_id" data-skip-action="/applications/skip" '
                       f'data-skip-label="{h(app["company"])} — {h(app["role"])}" data-after="refresh">Skip</a>')
    elif app["status"] == "skipped":
        buttons.append(state("Skipped"))
    elif app["date_applied"]:
        buttons.append(state(f"Applied · {_short_day(app['date_applied'])}"))
    if app["status"] not in db.CLOSED_STATUSES:
        buttons.append(_mock_form(app, here))
    return (f'<section class="shortcuts" aria-label="Shortcuts"><div class="action-row">{"".join(buttons)}</div>'
            + _draft_form(here, app_id=app["id"]) + "</section>")


def _mock_form(app: dict, here: str) -> str:
    """Mock interview for a chosen stage (the current one when it is an interview stage): a fixed message
    in the Applications Assistant box, where the agent asks the questions and gives the scores (/mock)."""
    current = app["status"] if app["status"] in db.INTERVIEW_STAGES else db.INTERVIEW_STAGES[0]
    options = "".join(f'<option value="{s}"{" selected" if s == current else ""}>{h(db.status_label(s).capitalize())}</option>'
                      for s in db.INTERVIEW_STAGES)
    return ('<form method="post" action="/applications/mock" class="inline mock-start" data-ajax="mock">'
            f'<input type="hidden" name="app_id" value="{h(app["id"])}"><input type="hidden" name="return" value="{h(here)}">'
            f'<select name="stage" aria-label="Mock interview stage">{options}</select>'
            + _button("Mock interview", "secondary", "sm") + "</form>")


def _draft_form(here: str, app_id: int | None = None, job_id: str | None = None) -> str:
    """Draft a reply (folded): what to write (reply, follow-up, thank-you note), the channel, the recruiter's
    message pasted in and the user's notes. Posts a draft request about this application, this job offer
    or (neither) the whole search; the draft comes back in the Applications Assistant box with Copy."""
    about = (f'<input type="hidden" name="app_id" value="{h(app_id)}">' if app_id is not None
             else f'<input type="hidden" name="job_id" value="{h(job_id)}">' if job_id else "")
    kinds = "".join(f'<option value="{k}">{h(db.COMMS_LABELS[k])}</option>' for k in db.COMMS_KINDS)
    channels = "".join(f'<label class="draft-channel"><input type="radio" name="channel" value="{c}"'
                       f'{" checked" if c == "linkedin" else ""}> {h(db.CHANNEL_LABELS[c])}</label>'
                       for c in db.CHANNELS)
    return ('<details class="draft-reply"><summary>Draft a reply</summary>'
            '<form method="post" action="/applications/draft" class="ask draft-form" data-ajax="draft">'
            f'{about}<input type="hidden" name="return" value="{h(here)}">'
            f'<div class="draft-row"><select name="comms" aria-label="What to write">{kinds}</select>'
            f'<span class="draft-channels" role="radiogroup" aria-label="Channel">{channels}</span></div>'
            f'<textarea name="pasted" rows="4" maxlength="{db.MAX_MESSAGE}" aria-label="Their message" '
            'placeholder="Paste the recruiter\'s message (LinkedIn or email). Needed for a reply."></textarea>'
            '<textarea name="notes" rows="2" maxlength="2000" aria-label="What I want to say" '
            'placeholder="What to say, optional: available Tue/Wed after 2, ask about hybrid…"></textarea>'
            '<div class="ask-foot"><span class="muted">A draft to copy. Nothing is sent.</span>'
            + _button("Draft", size="sm") + "</div></form></details>")


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


def _pdf_link(private_dir: Path | None, app_id: int, version: dict) -> str:
    """PDF beside the latest tailored version, once its PDF is made (on `tailor save`, or `tailor pdf`)."""
    pdf = research.inside(private_dir, str(cvpdf.pdf_path(Path(version["file_path"]))))
    if pdf is None or not pdf.is_file():
        return ""
    href = "/research/pdf?" + urlencode([("app", app_id), ("v", version["version"])])
    return f'<a class="btn btn-ghost btn-sm" href="{h(href)}" target="_blank" rel="noopener">PDF</a>'


def _artifact(card_id: str, title: str, meta: str, actions: str, body: str) -> str:
    return (f'<section class="artifact" id="{card_id}"><div class="artifact-head"><strong>{h(title)}</strong>{meta}'
            f'<span class="artifact-actions">{actions}</span></div>{body}</section>')


def _fit_tone(score: int) -> str:
    """The CV fit (1-5) as a shade: 4-5 light, 3 middle, 1-2 dark."""
    return "s9" if score >= 4 else ("s6" if score == 3 else "s3")


def _research_cards(app: dict, found: dict, versions: list[dict], here: str, private_dir: Path | None = None,
                    check: dict | None = None, notes: dict | None = None) -> str:
    """The Notes card (always, when `notes` is given: db.get_notes), then the agent's results, each with
    Open and Redo: company research (with the tier the research set and why), the CV fit with its top
    gaps, and the tailored CV: the latest version (with its PDF, its check's verdict, flagged lines and
    changes vs cv.md: `check`) and the sent one (marked), the rest folded under "Older versions". A result card shows once there is a result;
    queued work shows on its shortcut button."""
    profile, fit, open_kinds = found["profile"], found["fit"], found["open"]
    cards = [_notes_card(app["id"], notes, here)] if notes is not None else []
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
        ai = "done" if check and check.get("summary") else ("pending" if "cv_eval" in open_kinds else "none")

        def item(v: dict) -> str:
            return (f'<li><span class="mono version-name">{h(Path(v["file_path"]).name)}</span>'
                    f'<span class="muted">{h(_short_day(v["created_at"]))}</span>'
                    + (_badge("sent", "s9") if app.get("cv_sent_version") == v["version"] else "")
                    + _open_link(app["id"], "tailor", version=v["version"])
                    + (_pdf_link(private_dir, app["id"], v) + _verdict_badge(check, ai) if v is versions[-1] else "")
                    + "</li>")
        shown = [v for v in reversed(versions) if v is versions[-1] or v["version"] == app.get("cv_sent_version")]
        older = [v for v in reversed(versions) if v not in shown]
        actions = "" if "tailor" in open_kinds else _redo(app["id"], "tailor", here, "New from cv.md")
        body = (f'<ul class="versions">{"".join(map(item, shown))}</ul>'
                + _older_versions("".join(map(item, older)), len(older), "ul", "versions")
                + _check_body(check, versions[-1]["version"], ai) +
                '<p class="muted">Ask the assistant for changes; each one is a new version. Tell it which file you sent.</p>')
        cards.append(_artifact("card-tailor", "Tailored CV", "", actions, body))
    return f'<section class="panel-results"><h3>Results</h3>{"".join(cards)}</section>' if cards else ""


def _older_versions(items: str, count: int, tag: str, cls: str) -> str:
    """Versions that are neither current nor sent, in a closed <details> (nothing when there are none)."""
    if not count:
        return ""
    return (f'<details class="older-versions"><summary>Older versions <span class="count">{count}</span></summary>'
            f'<{tag} class="{cls}">{items}</{tag}></details>')


def _notes_card(app_id: int, notes: dict, here: str, draft: str | None = None, alert: str = "") -> str:
    """The Notes card: the notes as safe Markdown (escaped; line breaks kept; agent additions at the
    end), and Edit, which opens a form with the text and the notes' stamp (db.save_notes refuses a
    stale one). With `draft` (a refused save), the editor is open with that text under the current
    notes, after `alert`."""
    text, stamp = notes["notes"], notes["stamp"]
    body = render_markdown(text, '<p class="muted">No notes yet. Questions to ask, call notes, what the assistant '
                                 'finds: edit here or ask the assistant to note it.</p>', breaks=True)
    when = f' <span class="mono muted">{h(_short_day(stamp))}</span>' if stamp and text else ""
    opened = " open" if draft is not None else ""
    form = (f'<details class="notes-edit"{opened}><summary class="btn btn-ghost btn-sm">Edit</summary>'
            '<form method="post" action="/applications/notes" class="notes-form" data-notes>'
            f'<input type="hidden" name="app_id" value="{h(app_id)}">'
            f'<input type="hidden" name="stamp" value="{h(stamp)}">'
            f'<input type="hidden" name="return" value="{h(here)}">'
            f'<textarea name="notes" rows="10" maxlength="{db.MAX_NOTES}" aria-label="Notes">'
            f'{h(text if draft is None else draft)}</textarea>'
            '<p class="notes-buttons">' + _button("Save", "primary", "sm")
            + f'<a class="btn btn-ghost btn-sm" href="{h(here)}" data-notes-cancel>Cancel</a>'
            '<span class="muted">Markdown: **bold**, - lists. Empty clears the notes.</span></p></form></details>')
    return (f'<section class="artifact notes-card" id="card-notes"><div class="artifact-head"><strong>Notes</strong>'
            f'{when}</div>{_banner(alert, "warning") if alert else ""}<div class="notes-body doc">{body}</div>'
            f'{form}</section>')


CHECK_TONES = {"ready": "success", "look": "warning", "blocked": "danger"}


def _verdict_badge(check: dict | None, ai: str = "none") -> str:
    """The verdict, and "AI check pending" while the AI check of that version is queued (`ai`: done,
    pending or none, as db.eval_state), so a rules-only Ready never reads as the final word."""
    badge = _badge(cvcheck.LABELS[check["verdict"]], CHECK_TONES[check["verdict"]]) if check else ""
    return badge + (_badge("AI check pending") if ai == "pending" else "")


def _check_body(check: dict | None, version: int, ai: str = "none") -> str:
    """The latest version's check: the AI check's scores when in, its flagged lines (in line order, as
    stored; marked by severity) and the changes vs cv.md. Every value escaped; a finding's text never
    holds pay (cvcheck, cveval)."""
    if check is None:
        return f'<p class="muted">v{h(version)} is not checked yet: tailor check checks it.</p>'
    scores = ""
    if ai == "done":
        why = check["summary"].get("skim_why")
        scores = (f'<p class="muted">AI check: {h(cveval.summary_line(check))}</p>'
                  + (f'<p class="muted">Skim: {h(why)}</p>' if why else ""))
    items = "".join(
        f'<li class="finding-{h(f["severity"])}">{h(f["message"])}'
        + (f'<br><span class="mono muted">{h(f["text"])}</span>' if f["text"] else "") + "</li>"
        for f in check["findings"])
    findings = f'<ul class="check-findings">{items}</ul>' if items else ""
    changes = "".join(f"<li>{h(c)}</li>" for c in check["changes"])
    return (f'<div class="cv-check">{scores}{findings}'
            + (f'<p class="muted">Changes vs cv.md</p><ul class="gaps">{changes}</ul>' if changes else "") + "</div>")


def _history(conn: sqlite3.Connection, app_id: int) -> str:
    """Stage changes, newest first, collapsed (their comments are under Comments)."""
    moves = [e for e in reversed(db.timeline(conn, app_id)) if e["status"]]
    if not moves:
        return ""
    items = "".join(f'<li><span class="mono">{h(e["at"][:10])}</span> {_status_badge(e["status"])}</li>' for e in moves)
    return (f'<details class="history"><summary>History <span class="count">{len(moves)}</span></summary>'
            f'<ul class="timeline">{items}</ul></details>')


# --- the My CV panel ----------------------------------------------------------------------
# The master CV (private_dir/cv.md) with its skills and saved versions. Read-only apart from
# Restore: changes are asked of the agent in the Applications Assistant box ("About my CV"), which writes them
# through `cli cv save` (prep.cvfile). Only cv.md and recorded cv-history files are read.

def _cv_panel(conn: sqlite3.Connection, settings: Settings, f: dict) -> str:
    """The side panel for ?cv=1: header (words, which version is current), jump buttons, then the
    skills as chips, the rendered CV and the history; or with `v` one saved version, with `compare`
    that version against cv.md."""
    close = home_url(f, cv=False)
    here = home_url(f, cv=True, cv_v=None, cv_cmp=None)
    private_dir = settings.private_dir
    text = cvfile.read_cv(private_dir) if private_dir else None
    versions, current = cvfile.history(conn, private_dir) if private_dir else ([], None)
    by_id = {v["id"]: v for v in versions}
    if not private_dir:
        sub, body = "", _banner("private_dir is not set, so there is no CV to show.", "warning")
    elif text is None:
        sub = ""
        body = _empty("No cv.md yet.", f"Put the master CV at {cvfile.cv_path(private_dir)}.")
    else:
        if current is not None:
            state = f'<span class="muted">saved as v{h(current)} · {h(_short_day(by_id[current]["created_at"]))}</span>'
        elif versions:
            state = ('<span class="badge badge-warning" title="cv.md was edited outside the assistant; the next change '
                     'or restore keeps this text as a version first">changed since the last version</span>')
        else:
            state = '<span class="muted">no saved versions yet</span>'
        sub = f'<span>cv.md · {research.word_count(text)} words</span>{state}'
        if f["cv_v"] in by_id:
            body = _cv_version_view(private_dir, by_id[f["cv_v"]], current, f, here)
        elif f["cv_cmp"] in by_id:
            body = _cv_compare_view(private_dir, by_id[f["cv_cmp"]], text, current, f, here)
        else:
            groups = cvfile.skills(text)
            jumps = "".join(
                f'<a class="btn btn-secondary btn-sm" href="#{card}" data-jump="{card}">{label}</a>'
                for card, label in (("cv-skills", "Skills"), ("cv-text", "CV"), ("cv-history", f"History {len(versions)}")))
            body = (f'<section class="shortcuts" aria-label="Shortcuts"><div class="action-row">{jumps}</div>'
                    '<p class="muted">To change it, tick "Ask about this" and ask the assistant below; each change is '
                    'saved as a version.</p></section>'
                    + _cv_skills(groups)
                    + f'<section class="cv-doc" id="cv-text"><h3>CV</h3><div class="doc">{render_markdown(text)}</div></section>'
                    + _cv_history(versions, current, f, here))
    return (
        '<aside class="panel panel-cv" id="panel" aria-labelledby="panel-title" data-cv="1">'
        '<header class="panel-head"><div><h2 id="panel-title">My CV</h2>'
        f'<p class="panel-sub">{sub}</p></div>'
        f'<span class="panel-head-actions">{_sel_tick(f, "cv", "my CV", "Ask about this")}'
        '<a class="panel-ask btn btn-secondary btn-sm" href="#agent" data-ask-agent>'
        'Ask the assistant</a>'
        f'<a class="panel-close btn btn-ghost btn-sm" href="{h(close)}" aria-label="Close" data-close-panel>✕</a></span></header>'
        f'<div class="panel-body">{body}</div></aside>'
    )


def _cv_skills(groups: list[tuple[str, list[str]]]) -> str:
    """The skills section as one row of chips per theme."""
    if not groups:
        body = '<p class="muted">No skills section found (a heading with "Skills" in it).</p>'
    else:
        body = '<dl class="skill-groups">' + "".join(
            f'<div class="skill-group"><dt>{h(name)}</dt><dd>'
            + "".join(f'<span class="chip">{h(item)}</span>' for item in items) + "</dd></div>"
            for name, items in groups) + "</dl>"
    return f'<section class="cv-skills" id="cv-skills"><h3>Skills</h3>{body}</section>'


def _cv_restore_form(version_id: int, here: str, variant: str = "ghost") -> str:
    return ('<form method="post" action="/cv/restore" class="inline" data-ajax="restore">'
            f'<input type="hidden" name="version" value="{h(version_id)}"><input type="hidden" name="return" value="{h(here)}">'
            f'<button class="btn btn-{variant} btn-sm" aria-label="Restore v{h(version_id)}">Restore</button></form>')


def _cv_link(f: dict, label: str, aria: str = "", **changes: object) -> str:
    label_attr = f' aria-label="{h(aria)}"' if aria else ""
    return (f'<a class="btn btn-ghost btn-sm" href="{h(home_url(f, cv=True, **changes))}" data-panel-link{label_attr}>'
            f'{h(label)}</a>')


def _cv_history(versions: list[dict], current: int | None, f: dict, here: str) -> str:
    """Saved versions newest first: number, date, the note of what changed, and Open · Compare ·
    Restore (the current one is marked and has Open only). Only the current one shows; the rest are
    folded under "Older versions"."""
    if not versions:
        body = '<p class="muted">No saved versions yet. The first change through the assistant saves this text as v1.</p>'
    else:
        items, older = [], []
        for v in versions:
            n = v["id"]
            is_current = n == current
            actions = _cv_link(f, "Open", f"Open v{n}", cv_v=n, cv_cmp=None)
            if not is_current:
                actions += _cv_link(f, "Compare", f"Compare v{n} with the current CV", cv_cmp=n, cv_v=None)
                actions += _cv_restore_form(n, here)
            mark = ' class="is-current"' if is_current else ""
            (items if is_current else older).append(
                f'<li{mark}><span class="mono version-name">v{h(n)}</span>'
                f'<time class="muted" datetime="{h(v["created_at"])}">{h(_local_time(v["created_at"]))}</time>'
                f'<span class="cv-note">{h(v["note"])}</span>'
                + (_badge("current", "s9") if is_current else "")
                + f'<span class="artifact-actions">{actions}</span></li>')
        body = ((f'<ol class="versions cv-versions">{"".join(items)}</ol>' if items else "")
                + _older_versions("".join(older), len(older), "ol", "versions cv-versions"))
    return (f'<section class="cv-history" id="cv-history"><h3>History <span class="count">{len(versions)}</span></h3>'
            f'{body}</section>')


def _cv_version_head(version: dict, title: str, current: int | None, here: str, actions: str) -> str:
    restore = "" if version["id"] == current else _cv_restore_form(version["id"], here, "secondary")
    return (f'<section class="cv-version-head"><a class="btn btn-ghost btn-sm" href="{h(here)}" data-panel-link>'
            '← Current CV</a><h3>' + h(title) + '</h3>'
            f'<p><span class="muted">{h(_local_time(version["created_at"]))}</span> · {h(version["note"])}'
            + (" " + _badge("current", "s9") if version["id"] == current else "") + "</p>"
            f'<div class="action-row">{actions}{restore}</div></section>')


def _cv_version_view(private_dir: Path, version: dict, current: int | None, f: dict, here: str) -> str:
    """One saved version, rendered, with Compare and Restore."""
    n = version["id"]
    compare = "" if n == current else _cv_link(f, "Compare with current", cv_cmp=n, cv_v=None)
    head = _cv_version_head(version, f"Version v{n}", current, here, compare)
    try:
        text = cvfile.read_version(private_dir, version)
    except (LookupError, OSError, UnicodeDecodeError):
        return head + _banner("The file of this version is missing or unreadable.", "warning")
    return head + f'<section class="cv-doc"><div class="doc">{render_markdown(text)}</div></section>'


def _cv_compare_view(private_dir: Path, version: dict, text: str, current: int | None, f: dict, here: str) -> str:
    """A saved version against cv.md: changed lines with two lines of context, − only in the version,
    + only in the current CV, the rest folded."""
    n = version["id"]
    head = _cv_version_head(version, f"v{n} compared with the current CV", current, here,
                            _cv_link(f, f"Open v{n}", cv_v=n, cv_cmp=None))
    try:
        old = cvfile.read_version(private_dir, version)
    except (LookupError, OSError, UnicodeDecodeError):
        return head + _banner("The file of this version is missing or unreadable.", "warning")
    hunks = cvfile.diff(old, text)
    if not hunks:
        return head + '<p class="muted">Same as the current CV.</p>'
    kinds = {" ": ("ctx", ""), "-": ("del", f"only in v{n}"), "+": ("add", "only in the current CV")}
    rendered = '<div class="diff-gap" aria-hidden="true">⋯</div>'.join(
        '<div class="diff-hunk">' + "".join(_diff_line(op, line, kinds) for op, line in hunk) + "</div>"
        for hunk in hunks)
    legend = (f'<p class="diff-legend"><span class="diff-key diff-del">− only in v{h(n)}</span>'
              '<span class="diff-key diff-add">+ only in the current CV</span></p>')
    return head + f'<section class="cv-diff">{legend}<div class="diff">{rendered}</div></section>'


def _diff_line(op: str, line: str, kinds: dict[str, tuple[str, str]]) -> str:
    css, label = kinds[op]
    title = f' title="{h(label)}"' if label else ""
    return (f'<div class="diff-line diff-{css}"{title}><span class="diff-op" aria-label="{h(label or "same")}">'
            f'{"−" if op == "-" else "+" if op == "+" else ""}</span><span class="diff-text">{h(line) or "&nbsp;"}</span></div>')


AGENT_MESSAGES = 60  # the latest messages shown in the Applications Assistant box


def _agent_box(conn: sqlite3.Connection, settings: Settings, f: dict, targets: list[dict]) -> str:
    """The Applications Assistant box under the list: one conversation for every application and job. A new message is
    about the ticked targets (`targets`, from sel_targets: none = the whole search, one = that job,
    2-4 = a comparison), shown as chips above the input. All | Selected filters the conversation to
    messages about any ticked target while something is ticked."""
    return (
        f'<section class="card card-agent agent" id="agent" aria-labelledby="agent-title">'
        f'<header class="card-head"><h2 id="agent-title">Applications Assistant</h2>'
        '<button type="button" class="btn btn-ghost btn-sm agent-back" data-back-to-panel>← Back to the job</button>'
        f'<div class="agent-filter" role="group" aria-label="Show messages"{"" if targets else " hidden"}>'
        '<button type="button" class="view-btn is-active" data-filter-chat="all">All</button>'
        '<button type="button" class="view-btn" data-filter-chat="sel">Selected</button></div></header>'
        f'<div class="card-body">{_thread(conn, settings, f["app"], f["job"])}'
        '<form method="post" action="/applications/message" class="ask" data-ajax="thread">'
        f'<input type="hidden" name="return" value="{h(home_url(f))}">{_agent_about(f, targets)}'
        f'<textarea name="body" rows="3" maxlength="{db.MAX_MESSAGE}" required '
        'placeholder="Ask anything: which tasks need me, a question about the ticked jobs, a comparison, a change '
        '(tasks, comments, stage, tier, CV), or paste a recruiter email…" aria-label="Message to the Applications Assistant"></textarea>'
        '<div class="ask-foot"><span class="muted">The assistant does the edits and replies here.</span>'
        + _button("Send", size="sm") + "</div></form>" + _draft_form(home_url(f)) + "</div></section>"
    )


def _agent_about(f: dict, targets: list[dict]) -> str:
    """"About:" and a chip per ticked target (each with × to untick it, and a hidden `about` field the
    form sends), then Clear all; nothing ticked is a question about the whole search. The × and Clear
    all are links (they work without the script); the script swaps this line when the ticks change,
    so a half-typed message stays."""
    marker = '<input type="hidden" name="about_set" value="1">'  # tells "nothing ticked" from an older form
    if not targets:
        return (f'<div class="agent-about" data-about="">{marker}<span class="about-label">About:</span>'
                '<span class="muted">the whole search · tick ☐ jobs to ask about them</span></div>')
    chips = "".join(
        f'<span class="about-chip" data-key="{h(t["key"])}"><input type="hidden" name="about" value="{h(t["key"])}">'
        f'<span class="about-name">{h(t["label"])}</span>'
        f'<a href="{h(home_url(f, sel=toggle_sel(f["sel"], t["key"])))}" data-sel="{h(t["key"])}" '
        f'aria-label="Untick {h(t["label"])}" title="Untick">×</a></span>'
        for t in targets)
    note = f'<span class="muted">compare {len(targets)}</span>' if len(targets) > 1 else ""
    return (f'<div class="agent-about" data-about="{h(" ".join(t["key"] for t in targets))}">{marker}'
            f'<span class="about-label">About:</span>{chips}{note}'
            f'<a class="about-clear" href="{h(home_url(f, sel=()))}" data-sel-clear>Clear all</a></div>')


def _job_names(settings: Settings, conn: sqlite3.Connection, ids: set[str]) -> dict[str, str]:
    """Company names for messages about jobs not yet tracked: from getjo (read-only), else from a
    skip's stored copy; a job found in neither reads "job offer"."""
    names = {k: d["company"] for k, d in db.job_decisions(conn).items() if k in ids and d["company"]}
    try:
        with closing(getjo.connect_ro(settings.getjo_db)) as gconn:
            names.update({k: j["company"] for k, j in getjo.jobs_by_id(gconn, ids - set(names)).items()})
    except (getjo.GetjoUnavailable, sqlite3.Error):
        pass
    return names


def _thread(conn: sqlite3.Connection, settings: Settings, app_id: int | None = None,
            job_id: str | None = None) -> str:
    """The conversation, its latest AGENT_MESSAGES oldest first, each tagged with its application or
    job offer (a link to its panel) or "all jobs". Swapped in whole by the page script after Send and
    while polling; data-pending marks a user message still waiting for its reply. While anything waits
    (a message, or the open panel's queued work: `app_id` or `job_id`) the waiting line has Stop."""
    messages = db.thread(conn, limit=AGENT_MESSAGES)
    targets = db.message_targets(conn, [m["id"] for m in messages])
    untracked = {t["getjo_job_id"] for ts in targets.values() for t in ts if t["application_id"] is None and t["getjo_job_id"]}
    untracked |= {m["getjo_job_id"] for m in messages if m["application_id"] is None and m["getjo_job_id"]}
    names = _job_names(settings, conn, untracked) if untracked else {}

    def tag(t: dict) -> str:
        if t["application_id"] is not None:
            return f'<a class="msg-about" href="{h(_here(t["application_id"]))}">{h(t["company"])}</a>'
        if t["getjo_job_id"]:
            href = "/?" + urlencode([("view", "offers"), ("job", t["getjo_job_id"])])
            return (f'<a class="msg-about msg-about-job" href="{h(href)}" title="Job offer">'
                    f'{h(names.get(t["getjo_job_id"], "job offer"))}</a>')
        return '<a class="msg-about msg-about-cv" href="/?cv=1" title="The master CV">My CV</a>'

    def about(m: dict) -> str:
        if m["topic"] == db.COMPARE:
            return "".join(tag(t) for t in targets.get(m["id"], []))
        if m["application_id"] is not None:
            return f'<a class="msg-about" href="{h(_here(m["application_id"]))}">{h(m["company"])}</a>'
        if m["topic"] == "cv":
            return '<a class="msg-about msg-about-cv" href="/?cv=1" title="The master CV">My CV</a>'
        if m["getjo_job_id"]:
            href = "/?" + urlencode([("view", "offers"), ("job", m["getjo_job_id"])])
            return (f'<a class="msg-about msg-about-job" href="{h(href)}" title="Job offer">'
                    f'{h(names.get(m["getjo_job_id"], "job offer"))}</a>')
        return '<span class="msg-about">all jobs</span>'

    flags = _msg_flags(conn, messages)
    items = "".join(
        f'<li class="msg msg-{"agent" if m["author"] == "agent" else "user"}{_cancelled_cls(m)}" '
        f'data-app="{h(m["application_id"] or "")}" '
        f'data-job="{h(m["getjo_job_id"] or "")}" data-topic="{h(m["topic"] or "")}" '
        f'data-targets="{h(" ".join(t["key"] for t in targets.get(m["id"], [])))}">'
        f'<div class="msg-meta"><span class="msg-who">{"assistant" if m["author"] == "agent" else "you"}</span>'
        + about(m) + _channel_tag(m) + flags.get(m["id"], "") + _route_tag(settings, m, m["id"] in flags)
        + f'<time datetime="{h(m["created_at"])}">{h(_local_time(m["created_at"]))}</time></div>'
        f'<div class="msg-body">{_linkify(m["body"])}</div>' + _draft_box(m)
        + (_wait_actions(m, "/applications", "thread", "",
                         bool(assistant.message_target(settings, "comms" if m.get("comms") else "ask")))
           if m["author"] == "user" else "")
        + "</li>"
        for m in messages
    )
    waiting_msgs = db.pending_messages(conn)
    waiting = len(waiting_msgs)
    pending = waiting > 0
    work = _panel_work(settings, conn, app_id, job_id)
    # someone is pinged: the Comms worker when every waiting message is a draft request it handles
    drafts_only = pending and all(m.get("comms") for m in waiting_msgs)
    has_handler = bool(assistant.target(settings, "comms")[0] if drafts_only else settings.assistant_task)
    stop = ""
    if pending or work:
        ctx = (f'<input type="hidden" name="panel_app" value="{h(app_id)}">' if app_id is not None
               else f'<input type="hidden" name="panel_job" value="{h(job_id)}">' if job_id else "")
        stop = ('<form method="post" action="/applications/stop" class="inline thread-stop" data-ajax="thread">'
                f'<input type="hidden" name="return" value="/">{ctx}'
                '<button class="btn btn-secondary btn-sm" title="Stop waiting: cancel the waiting messages'
                + (" and this job's queued work" if work else "") + '">Stop</button></form>')
    # Short tags only: who does what and since when lives in the Workers sidebar.
    tags = ([f"{waiting} waiting"] if waiting else []) + work
    line = h(" · ".join(tags))
    if pending and not has_handler:
        state = ('<div class="thread-state" role="status"><span>Waiting for the Applications Assistant: run /ask in a Claude Code '
                 f'session (no Applications Assistant task is set). {line}</span>{stop}</div>')
    elif pending or work:
        state = ('<div class="thread-state" role="status"><span class="spinner" aria-hidden="true"></span>'
                 f'<span>{line}</span>{stop}</div>')
    elif not messages:
        state = '<p class="muted thread-empty">No messages yet.</p>'
    else:
        state = ""
    return (f'<div class="thread" data-thread{" data-pending" if pending else ""}>'
            + (f'<ol class="msgs">{items}</ol>' if items else "") + state + "</div>")


def _channel_tag(m: dict) -> str:
    """The channel of a draft request or a draft ("LinkedIn" / "Email"), and what it is when not a reply."""
    if not m.get("comms") or m.get("channel") not in db.CHANNELS:
        return ""
    what = "" if m["comms"] == "reply" else f' · {h(db.COMMS_LABELS.get(m["comms"], ""))}'
    return (f'<span class="msg-channel msg-channel-{h(m["channel"])}">{h(db.CHANNEL_LABELS[m["channel"]])}'
            f'{what}</span>')


def _draft_box(m: dict) -> str:
    """The agent's draft under its note: the text as typed (escaped, in a pre-wrap block) with Copy, and for
    an email the subject line with its own Copy subject (Copy body then copies the body alone). Copy takes
    the element's text (data-copy names it), so what is copied is exactly what is shown."""
    if m.get("author") != "agent" or not m.get("draft_body"):
        return ""
    n = h(m["id"])
    subject = ""
    if m.get("draft_subject"):
        subject = (f'<div class="draft-subject"><span class="draft-label">Subject</span>'
                   f'<span class="draft-text" id="draft-subject-{n}">{h(m["draft_subject"])}</span>'
                   f'<button type="button" class="btn btn-ghost btn-sm" data-copy="draft-subject-{n}">Copy subject</button></div>')
    label = "Copy body" if m.get("draft_subject") else "Copy"
    return (f'<div class="draft">{subject}<div class="draft-body" id="draft-body-{n}">{h(m["draft_body"])}</div>'
            f'<div class="draft-actions"><button type="button" class="btn btn-secondary btn-sm" '
            f'data-copy="draft-body-{n}">{label}</button>'
            '<span class="muted">Paste it in LinkedIn or your mail app and send it yourself.</span></div></div>')


def _route_tag(settings: Settings, m: dict, flagged: bool) -> str:
    """"→ Comms worker" on a user message still waiting: where it went (details in the Workers sidebar).
    None on a message already flagged (no reply in 30 min, no reply yet) or one no task handles."""
    if m["author"] != "user" or m["status"] not in db.WAITING or flagged:
        return ""
    kind = "comms" if m.get("comms") else "ask"
    if not assistant.message_target(settings, kind):
        return ""
    return f'<span class="msg-route">→ {h(_handler_name(settings, kind))}</span>'


def _panel_work(settings: Settings, conn: sqlite3.Connection, app_id: int | None, job_id: str | None) -> list[str]:
    """What the open panel waits for: its application's queued agent requests and its job's open brief
    request, each as a short tag with who handles it ("Tailored CV → Research worker"). Empty without a panel."""
    labels = []
    if app_id is not None:
        labels = [_work_label(settings, r["kind"], db.REQUEST_LABELS.get(r["kind"], r["kind"]), r)
                  for r in db.open_requests(conn, app_id)]
        job_id = (db.get_application(conn, app_id) or {}).get("getjo_job_id")
    if job_id:
        labels += [_work_label(settings, "brief", "Job brief", b) for b in db.open_briefs(conn, job_id)]
    return labels


def _work_label(settings: Settings, kind: str, label: str, row: dict) -> str:
    """A queued item and its handler (assistant.target: a worker or the Applications Assistant), e.g.
    "Tailored CV → Research worker". Plain text (escaped by the caller)."""
    seq, name = assistant.target(settings, kind)
    return f"{label} → {name}" if seq else f"{label} (no assistant task set: run /research)"


def _cancelled_cls(m: dict) -> str:
    return " is-cancelled" if m["status"] == "cancelled" else ""


def _msg_flags(conn: sqlite3.Connection, messages: list[dict]) -> dict[int, str]:
    """A "Cancelled" flag on each cancelled user message, an "after cancel" one on a reply to it, and
    "no reply yet" on a message still waiting while a newer one on the same subject was answered."""
    status = {m["id"]: m["status"] for m in messages}
    flags = {}
    answered = {(m["application_id"], m["getjo_job_id"], m["topic"]): m["id"] for m in messages
                if m["author"] == "user" and m["status"] == "answered" and m["topic"] != db.COMPARE
                and not m.get("comms")}  # a draft answered says nothing about an /ask question
    for m in messages:
        if m["status"] == "cancelled":
            flags[m["id"]] = '<span class="msg-flag">Cancelled</span>'
        elif (m["author"] == "user" and m["status"] in db.WAITING and not m.get("comms")
              and answered.get((m["application_id"], m["getjo_job_id"], m["topic"]), 0) > m["id"]):
            flags[m["id"]] = ('<span class="msg-flag msg-flag-warn" title="A newer message on the same job was answered, '
                              'this one was not. Cancel it, or Resend it.">no reply yet</span>')
        elif m["author"] == "user" and db.claim_lapsed(m):
            flags[m["id"]] = (f'<span class="msg-flag msg-flag-warn" title="Taken by the assistant {db.MESSAGE_CLAIM_MINUTES} '
                              'or more minutes ago with no reply. Resend it, or Cancel it.">'
                              f'no reply in {db.MESSAGE_CLAIM_MINUTES} min</span>')
        elif m["author"] == "agent" and m["reply_to"] is not None:
            to = status.get(m["reply_to"]) or (db.get_message(conn, m["reply_to"]) or {}).get("status")
            if to == "cancelled":
                flags[m["id"]] = '<span class="msg-flag">after cancel</span>'
    return flags


def _wait_actions(m: dict, base: str, mode: str, extra: str, can_resend: bool) -> str:
    """Resend and Cancel under a user message still waiting for its reply. Resend shows once the
    message has waited assistant.RESEND_AFTER seconds since its last ping (data-resend-due, epoch ms,
    revealed by the page script), or at once when that ping failed or its claim lapsed (db.claim_lapsed);
    only with an assistant task set."""
    if m["status"] not in db.WAITING:
        return ""
    hidden = f'<input type="hidden" name="msg_id" value="{h(m["id"])}">{extra}'
    resend = ""
    if can_resend:
        ping = assistant.last_ping(m["id"])
        since = ping[0] if ping else _epoch(m["created_at"])
        due = int((since + assistant.RESEND_AFTER) * 1000)
        show = (ping is not None and not ping[1]) or time.time() * 1000 >= due or db.claim_lapsed(m)
        resend = (f'<form method="post" action="{base}/resend" data-ajax="{mode}">{hidden}'
                  f'<button class="btn btn-ghost btn-sm" data-resend-due="{due}"{"" if show else " hidden"}'
                  ' title="Ping the assistant again about this message">Resend</button></form>')
    return (f'<div class="msg-actions msg-wait">{resend}<form method="post" action="{base}/cancel" data-ajax="{mode}">'
            f'{hidden}<button class="btn btn-ghost btn-sm" title="Stop waiting: the assistant skips it">Cancel</button>'
            '</form></div>')


def _failed_pings(waiting: list[dict]) -> list[int]:
    """Ids of waiting messages whose latest ping failed: the polling script shows their Resend at once."""
    return [m["id"] for m in waiting if (assistant.last_ping(m["id"]) or (0, True))[1] is False]


def _epoch(stamp: str) -> float:
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


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
    """GET /applications/thread[?app=ID | ?job=ID] for polling: whether a message is still waiting for the
    agent or (with `app` or `job`, the open panel) an agent request or a job brief for it is open; that
    application's status (the agent may have moved it); the message count (so the script can tell
    a reply from finished work) and the conversation's HTML."""
    raw = (query.get("app") or [""])[0]
    job_id = (query.get("job") or [""])[0]
    if raw and not raw.isdigit():
        return _json(400, {"ok": False, "message": "app must be an application id."})
    with closing(_connect(settings, today)) as conn:
        app = db.get_application(conn, int(raw)) if raw else None
        if raw and app is None:
            return _json(404, {"ok": False, "message": "No such application."})
        job_id = (app["getjo_job_id"] if app else job_id) or None
        working = bool(app and db.open_requests(conn, app["id"])) or bool(job_id and db.open_briefs(conn, job_id))
        pending = working or bool(db.pending_messages(conn))
        payload = {"ok": True, "pending": pending, "failed": _failed_pings(db.pending_messages(conn)),
                   "status": app["status"] if app else None,
                   "messages": db.message_count(conn),
                   "html": _thread(conn, settings, app["id"] if app else None, None if app else job_id)}
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


# The PDF is sent with its own headers: the pages' CSP (default-src 'none') can stop the browser's PDF viewer.
PDF_HEADERS = [("Content-Security-Policy", "frame-ancestors 'none'"), ("X-Content-Type-Options", "nosniff"),
               ("Referrer-Policy", "same-origin"), ("Cache-Control", "no-store")]


def tailored_pdf(settings: Settings, query: dict[str, list[str]], today: str) -> Response:
    """The PDF of a tailored CV version, inline. Only the .pdf beside a version file recorded in prep.db,
    inside private_dir, is read; anything else is a 404."""
    app_param, version = (query.get("app") or [""])[0], (query.get("v") or [""])[0]
    if not app_param.isdigit() or not version.isdigit():
        return _text(404, "Not found.")
    with closing(_connect(settings, today)) as conn:
        recorded, path = research.result_path(conn, settings.private_dir, int(app_param), "tailor", int(version))
    pdf = research.inside(settings.private_dir, str(cvpdf.pdf_path(path))) if recorded and path else None
    if pdf is None or not pdf.is_file():
        return _text(404, "No PDF for this version yet: python -m prep.cli tailor pdf APP --version N makes it.")
    name = re.sub(r"[^\w.-]", "_", pdf.name, flags=re.A)  # outside the f-string: Python 3.11 refuses a backslash there
    return 200, [("Content-Type", "application/pdf"),
                 ("Content-Disposition", f'inline; filename="{name}"'), *PDF_HEADERS], pdf.read_bytes()


# --- HTML helpers ---------------------------------------------------------------------
# Helpers that take plain values escape them; parameters documented as HTML are trusted
# fragments built by other helpers.


# --- Workers sidebar, popover and run history -----------------------------------------------------
# prep/workers.py says what each worker does; this draws it. The rows are <details>: the popover opens
# without the script; app.js anchors it, closes it on Esc / a click outside and polls /workers.json.

STATE_LABELS = {"working": "working", "idle": "idle", "stuck": "needs a look"}
RESULT_MARKS = {"done": ("✓", "done"), "blocked": ("!", "blocked"), "stopped": ("✕", "stopped"),
                "lapsed": ("↺", "lapsed")}


def _clock(stamp: str | None) -> str:
    """A stored UTC time as local "14:05" today, else "Nov 25, 14:05"."""
    when = workers.parse(stamp)
    if when is None:
        return ""
    local = when.astimezone()
    return local.strftime("%H:%M") if local.date() == datetime.now().astimezone().date() else local.strftime("%b %d, %H:%M")


def _job_name_lookup(settings: Settings, conn: sqlite3.Connection) -> Callable[[set[str]], dict[str, str]]:
    return lambda ids: _job_names(settings, conn, ids) if ids else {}


def _worker_summary(ws: dict) -> str:
    return f'{ws["busy"]} busy · {ws["waiting"]} waiting'


def _worker_now(w: dict, here: str) -> str:
    if not w["now"]:
        return '<p class="pop-empty muted">Nothing claimed.</p>'
    items = "".join(
        f'<li><strong>{h(r["label"])}</strong> · {h(r["about"])}{(", " + h(r["role"])) if r.get("role") else ""}'
        f'<span class="pop-time">started {h(_clock(r["started_at"]))} · {h(r["elapsed"])}</span></li>'
        for r in w["now"])
    first = w["now"][0]
    stop = (f'<details class="stop-confirm"><summary class="btn btn-danger btn-sm">Stop</summary>'
            f'<form method="post" action="/workers/stop" class="stop-form" data-ajax="workers">'
            f'<input type="hidden" name="worker" value="{h(w["seq"])}"><input type="hidden" name="return" value="{h(here)}">'
            f'<span class="muted">Stop {"these items" if len(w["now"]) > 1 else "this item"}?</span>'
            '<button class="btn btn-secondary btn-sm" name="then" value="requeue" title="Back to the queue for the next free worker">'
            'Back to queue</button>'
            '<button class="btn btn-danger btn-sm" name="then" value="cancel" title="Cancel it; queue it again any time">'
            'Cancel it</button></form></details>')
    return (f'<ul class="pop-list">{items}</ul><div class="pop-actions">'
            f'<a class="btn btn-secondary btn-sm" href="{h(first["href"])}">Open job</a>{stop}</div>')


def _worker_queue(w: dict) -> str:
    if not w["queued"]:
        return '<p class="pop-empty muted">Nothing waiting.</p>'
    return '<ol class="pop-list">' + "".join(
        f'<li><a href="{h(q["href"])}">{h(q["label"])} · {h(q["about"])}</a>'
        f'<span class="pop-time">{h(_clock(q["queued_at"]))}</span></li>' for q in w["queued"]) + "</ol>"


def _run_mark(result: str | None) -> str:
    mark, label = RESULT_MARKS.get(result or "", ("·", "running"))
    return f'<span class="run-mark run-{h(result or "running")}" title="{h(label)}" aria-label="{h(label)}">{mark}</span>'


def _worker_last(w: dict) -> str:
    if not w["last"]:
        return '<p class="pop-empty muted">No runs yet.</p>'
    rows = []
    for r in w["last"]:
        tail = f'blocked: {r["reason"]}' if r["result"] == "blocked" and r.get("reason") else r["elapsed"]
        rows.append(f'<li>{_run_mark(r["result"])} {h(r["label"])} · {h(r["about"])}'
                    f'<span class="pop-time">{h(tail)}</span></li>')
    return f'<ul class="pop-list">{"".join(rows)}</ul>'


def _worker_rows(ws: dict, here: str) -> str:
    rows = []
    for w in ws["workers"]:
        pop_id = f'pop-{h(w["key"])}'
        rows.append(
            f'<li class="worker" data-worker="{h(w["key"])}"><details class="worker-pop">'
            f'<summary class="worker-row" aria-controls="{pop_id}">'
            f'<span class="wdot wdot-{h(w["state"])}" title="{h(STATE_LABELS[w["state"]])}"></span>'
            f'<span class="worker-name">{h(w["name"])}</span>'
            f'<span class="worker-line"><span class="wl-text">{h(w["line"])}</span>'
            + (f'<span class="wl-time"> · {h(w["since"])}</span>' if w["since"] else "") + '</span>'
            f'<span class="sr-only">, {h(STATE_LABELS[w["state"]])}</span></summary>'
            f'<div class="popover" id="{pop_id}" role="group" aria-label="{h(w["name"])} worker, seq {h(w["seq"])}">'
            f'<div class="pop-head"><span class="wdot wdot-{h(w["state"])}"></span>'
            f'<strong>{h(w["name"])} worker</strong><span class="muted">seq {h(w["seq"])}</span></div>'
            f'<section><h3>Now</h3>{_worker_now(w, here)}</section>'
            f'<section><h3>Queued · {len(w["queued"])}</h3>{_worker_queue(w)}</section>'
            f'<section><h3>Last runs</h3>{_worker_last(w)}'
            f'<a class="pop-more" href="/runs?{urlencode([("worker", w["seq"])])}">All runs →</a></section>'
            '</div></details></li>')
    return "".join(rows)


def workers_section(settings: Settings, conn: sqlite3.Connection, here: str = "/") -> str:
    """The sidebar's WORKERS block (empty when no worker is set): a heading with "N busy · M waiting", one
    row per worker with its popover, and Run history. data-since: when it was drawn (UTC), so the polling
    script toasts only work finished after that."""
    ws = workers.state(settings, conn, _job_name_lookup(settings, conn))
    if not ws["workers"]:
        return ""
    return (f'<section class="workers" data-workers data-since="{h(db.now_iso())}" aria-label="Workers">'
            f'<div class="workers-head"><h2>Workers</h2><span class="workers-sum">{h(_worker_summary(ws))}</span></div>'
            f'<ul class="worker-list">{_worker_rows(ws, here)}</ul>'
            '<a class="workers-runs" href="/runs">Run history →</a></section>')


def workers_json(settings: Settings, query: dict[str, list[str]], today: str) -> Response:
    """GET /workers.json[?since=UTC&here=PATH] for the sidebar's poll: the rows' HTML, the summary, the time now
    (the next poll's since) and the long items finished since `since` (toasts). Kinds, companies and times
    only: never message text or pay."""
    since = (query.get("since") or [""])[0]
    here = (query.get("here") or ["/"])[0]
    here = here if here.startswith("/") and not here.startswith("//") else "/"
    stamp = db.now_iso()
    with closing(_connect(settings, today)) as conn:
        names = _job_name_lookup(settings, conn)
        ws = workers.state(settings, conn, names)
        done = workers.finished(settings, conn, since, names) if workers.parse(since) else []
    rows = [{"key": w["key"], "name": w["name"], "seq": w["seq"], "state": w["state"], "line": w["line"], "since": w["since"],
             "queued": len(w["queued"])} for w in ws["workers"]]
    return _json(200, {"ok": True, "now": stamp, "summary": _worker_summary(ws), "workers": rows,
                       "html": _worker_rows(ws, here), "finished": done})


def stop_worker(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> dict:
    """Stop in a worker's popover: its current items (only that worker's) are cancelled, or with
    then=requeue put back in the queue and sent to the next free worker. The worker's terminal is not
    touched: whatever it saves later for a cancelled item is still stored."""
    seq = _int_field(form, "worker")
    if seq is None or seq not in {w["seq"] for w in workers.configured(settings)}:
        raise BadRequest("Which worker? (worker: a configured worker's seq)")
    requeue = form.get("then") == "requeue"
    stopped = db.stop_worker(conn, seq, requeue=requeue)
    log.info("worker %s stopped: %d item(s) %s", seq, len(stopped), "requeued" if requeue else "cancelled")
    if requeue:
        _ping_requeued(settings, conn, stopped, seq)
    msg = "worker_idle" if not stopped else "worker_requeued" if requeue else "worker_stopped"
    return {"msg": msg, "html": _worker_rows(workers.state(settings, conn, _job_name_lookup(settings, conn)),
                                             form.get("return") or "/")}


ACTIONS["/workers/stop"] = (stop_worker, "/", "worker_stopped")


def _ping_requeued(settings: Settings, conn: sqlite3.Connection, stopped: list[dict], seq: int) -> None:
    """Items put back by Stop go to the next free worker (another one than `seq` when there is one)."""
    reqs = [{"id": int(r["item"]), "application_id": r["application_id"], "kind": r["kind"]}
            for r in stopped if r["source"] == "request"]
    if reqs:
        assistant.notify_in_background(settings, assistant.route_requests(settings, conn, reqs, exclude=(seq,)))
    briefs = [r["item"] for r in stopped if r["source"] == "brief"]
    if briefs:
        chosen = assistant.pick(settings, conn, "research", exclude=(seq,))
        if chosen:
            db.assign_briefs(conn, briefs, chosen)
        assistant.notify_briefs_in_background(settings, len(briefs), chosen)
    for r in stopped:
        if r["source"] != "message":
            continue
        if r["kind"] == "learn":
            assistant.notify_learn_in_background(settings, r["card_id"], int(r["item"]))
            continue
        about, job = db.message_subject(conn, int(r["item"]))
        chosen = assistant.pick(settings, conn, "comms", exclude=(seq,)) if r["kind"] == "comms" else None
        if chosen:
            db.assign_message(conn, int(r["item"]), chosen)
        assistant.notify_message_in_background(settings, about, int(r["item"]), job=about is None and job is not None,
                                               topic=r["topic"], kind="comms" if r["kind"] == "comms" else "ask",
                                               seq=chosen)


def runs_filters(settings: Settings, query: dict[str, list[str]]) -> dict:
    """worker (a configured seq, else all) and result (RUN_RESULTS or "running", else all)."""
    raw = (query.get("worker") or [""])[0]
    seqs = {w["seq"] for w in workers.configured(settings)}
    result = (query.get("result") or [""])[0]
    return {"worker": int(raw) if raw.isdigit() and int(raw) in seqs else None,
            "result": result if result in (*db.RUN_RESULTS, "running") else ""}


def runs_url(f: dict, **changes: object) -> str:
    q = {**f, **changes}
    return "/runs" + (f"?{urlencode({k: v for k, v in q.items() if v not in ('', None)})}"
                              if any(v not in ("", None) for v in q.values()) else "")


def runs_page(settings: Settings, conn: sqlite3.Connection, today: str,
              query: dict[str, list[str]]) -> tuple[str, str, str]:
    """/runs: one row per run (worker, kind, application, queued, started, finished, duration,
    result and the blocked reason), newest first, filtered by worker and result."""
    f = runs_filters(settings, query)
    db.sync_runs(conn)
    configured = workers.configured(settings)
    names_by_seq = {w["seq"]: f'{w["name"]} · seq {w["seq"]}' for w in configured}
    job_names = _job_name_lookup(settings, conn)(workers.job_ids(conn))
    found = db.runs(conn, worker=f["worker"], result=f["result"] or None, limit=500)
    worker_opts = [(runs_url(f, worker=""), "All workers", f["worker"] is None)] + [
        (runs_url(f, worker=w["seq"]), w["name"], f["worker"] == w["seq"]) for w in configured]
    result_opts = [(runs_url(f, result=""), "All results", not f["result"])] + [
        (runs_url(f, result=r), r.capitalize(), f["result"] == r) for r in (*db.RUN_RESULTS, "running")]
    filters = (f'<div class="runs-filters">{_switch("Filter by worker", worker_opts)}'
               f'{_switch("Filter by result", result_opts)}</div>')
    rows = []
    for r in found:
        d = workers._describe(r, job_names, datetime.now().astimezone())
        who = names_by_seq.get(r["worker"], f"seq {r['worker']}" if r["worker"] else "other session")
        result = (f'{_run_mark(r["result"])} {h(r["result"] or "running")}'
                  + (f' <span class="muted">· {h(r["reason"])}</span>' if r.get("reason") else ""))
        rows.append((h(who), h(d["label"]), f'<a href="{h(d["href"])}">{h(d["about"])}</a>',
                     _mono(h(_clock(r["queued_at"]))), _mono(h(_clock(r["started_at"]))),
                     _mono(h(_clock(r["finished_at"]))), _mono(h(d["elapsed"])), result))
    table = _table(("Worker", "Kind", "About", "Queued", "Started", "Finished", "Duration", "Result"), rows,
                   "No runs yet. Each time a worker takes an item from the queue, it shows here.")
    note = ("" if configured else _note("No workers are set: runs by any Claude session still show here "
                                        "(assistant_task, [workers] in the settings)."))
    return ("Run history", f"Workers' runs · {today}",
            note + _card("Runs", filters + table + '<p class="card-note">Cost and time per kind of job: '
                         '<a href="/usage">Usage &amp; cost →</a></p>', count=len(found)))


# --- Evals (getjo's evals: catalogue, run details, history; prep/evals.py, read only) ------------

EVAL_GROUPS = (("dimension", "By dimension"), ("where", "By where it runs"))
EVAL_STATUS_TONES = {"built": "success", "partial": "warning", "gap": "neutral"}
GRADER_LABELS = {"code": "Code", "human labels": "Your labels", "LLM judge": "LLM judge"}
REPORT_HEADERS = [  # a saved promptfoo HTML report: sandboxed, nothing fetched, no same-origin access
    ("Content-Security-Policy",
     "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; font-src 'none'; "
     "connect-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'; sandbox allow-scripts"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
]
EVALS_FLOW = (("Question", "what the eval asks"), ("Cases", "labelled jobs: fictional or real"),
              ("Grader", "code, your labels or an LLM judge"), ("Metric", "agreement, precision, pass rate"),
              ("Decision", "ship, change the prompt, move the threshold"))
EVALS_PLACES = (("Offline", "evals.run and promptfoo, by hand, after a prompt, model or threshold change"),
                ("CI gate", "the fictional set on every push to main that touches scoring; fails below 8 of 10"),
                ("Production", "drift on real jobs and latency: not built yet (gaps below)"))


def _pct(rate: float | None) -> str:
    return "–" if rate is None else f"{rate:.0%}"


def _rate_text(r: dict) -> str:
    """'8 of 10 · 80%', or '–' for an empty rate."""
    if not r or r.get("rate") is None:
        return "–"
    if r.get("total"):
        return f"{r['hits']} of {r['total']} · {r['rate']:.0%}"
    return f"{r['rate']:.0%}" if r["rate"] <= 1 else f"{r['rate']:.2f}"


def _metric_name(name: str | None) -> str:
    return {"pass_rate": "pass rate", None: ""}.get(name, (name or "").replace("_", " "))


def evals_url(**q: object) -> str:
    pairs = {k: v for k, v in q.items() if v not in ("", None)}
    return "/evals" + (f"?{urlencode(pairs)}" if pairs else "")


def _evals_header() -> str:
    flow = '<span class="eval-arrow" aria-hidden="true">→</span>'.join(
        f'<span class="eval-step"><strong>{h(a)}</strong><small>{h(b)}</small></span>' for a, b in EVALS_FLOW)
    places = "".join(f'<li><strong>{h(a)}</strong> {h(b)}</li>' for a, b in EVALS_PLACES)
    return _card("How evals fit together",
                 f'<div class="eval-flow">{flow}</div><ul class="eval-places">{places}</ul>'
                 + _note("Read from getjo's evals/catalog.json and evals/results/, never written. Results hold "
                         "case titles, companies and reasons: shown here only, on this machine."), cls="card-wide")


def _evals_empty(settings: Settings, catalog: dict) -> str:
    if catalog["state"] == "no_repo":
        return _card("Evals", _empty("getjo's checkout is not set.",
                                     "Set getjo_repo in settings.toml (an absolute path to getjo's working copy)."))
    if catalog["state"] == "missing":
        return _card("Evals", _empty("getjo has no eval catalogue here yet.",
                                     f"Expected {catalog['path']}. Pull getjo's main branch."))
    return _card("Evals", _empty("getjo's eval catalogue could not be read.",
                                 f"Check {catalog['path']}: it must be valid JSON with an evals list."))


def _copy_command(command: str, ident: str) -> str:
    if not command:
        return ""
    return (f'<div class="eval-command"><code id="{h(ident)}">{h(command)}</code>'
            f'<button type="button" class="btn btn-ghost btn-sm" data-copy="{h(ident)}">Copy</button></div>')


def _steps(title: str, steps: list[str]) -> str:
    if not steps:
        return ""
    items = "".join(f"<li>{h(s)}</li>" for s in steps)
    return f'<details class="eval-steps"><summary>{h(title)}</summary><ol>{items}</ol></details>'


def _code_refs(code: list[dict]) -> str:
    if not code:
        return ""
    refs = "".join(f'<li>{_mono(h(c["file"]))}{" · " + _mono(h(c["function"])) if c.get("function") else ""}</li>'
                   for c in code)
    return f'<div class="eval-code"><span class="label">Code</span><ul>{refs}</ul></div>'


def _spark(runs: list[dict]) -> str:
    points = [(r["when"], round(r["value"] * 100)) for r in runs if r["value"] is not None]
    return charts.trend(points[-12:], low=0, high=100, width=140, height=32) if points else ""


def _eval_card(e: dict) -> str:
    """One eval: name, status, question, tags, grader, where and when, last result with a sparkline,
    the command (Copy), How to check / If it fails, and the code it lives in. A gap is dashed."""
    tags = "".join(_badge(d, "s4") for d in e["dimensions"])
    where = ", ".join(e["where"]) or "–"
    last = e["runs"][-1] if e["runs"] else None
    if last:
        head = (f'<a href="/evals/run?{h(urlencode({"file": last["name"]}))}">{h(_metric_name(last["metric"]))} '
                f'<strong>{h(_pct(last["value"]))}</strong></a>' if last["metric"] else
                f'<a href="/evals/run?{h(urlencode({"file": last["name"]}))}">open the run</a>')
        result = (f'<div class="eval-last"><span class="label">Last run</span>{head}'
                  f' <span class="muted mono">{h(last["when"])}</span>{_spark(e["runs"])}'
                  f'<a class="eval-history" href="{h(evals_url(eval=e["id"]))}">History · {len(e["runs"])} '
                  f'run{"s" if len(e["runs"]) != 1 else ""} →</a></div>')
    elif e["status"] == "gap":
        result = '<p class="eval-last muted">Not built yet: a known blind spot.</p>'
    elif e["results"]:
        result = ('<div class="eval-last"><span class="label">Last run</span><span class="muted">No runs yet. '
                  'Run the command below in getjo.</span></div>')
    else:
        result = '<p class="eval-last muted">Writes no results file: read its output where it runs.</p>'
    return (f'<article class="eval-card eval-{h(e["status"])}" id="eval-{h(e["id"])}">'
            f'<header class="eval-head"><h3>{h(e["name"])}</h3>{_badge(e["status"], EVAL_STATUS_TONES[e["status"]])}</header>'
            f'<p class="eval-question">{h(e["question"])}</p>'
            f'<div class="chips">{tags}</div>'
            f'<dl class="eval-facts"><dt>Grader</dt><dd>{h(GRADER_LABELS.get(e["grader"], e["grader"]))}</dd>'
            f'<dt>Runs</dt><dd>{h(where)}<span class="muted"> · {h(e["when"])}</span></dd></dl>'
            f'{result}{_copy_command(e["command"], "cmd-" + e["id"])}'
            f'{_steps("How to check it", e["how_to_check"])}{_steps("If it fails", e["if_fails"])}'
            f'{_code_refs(e["code"])}</article>')


def _eval_groups(evals_list: list[dict], group: str) -> list[tuple[str, list[dict]]]:
    """Evals under their first dimension or the first place they run (getjo lists the main one first;
    the rest show as tags), so each card appears once. Gaps go last within a group."""
    key = "dimensions" if group == "dimension" else "where"
    groups: dict[str, list[dict]] = {}
    for e in evals_list:
        groups.setdefault(e[key][0] if e[key] else "other", []).append(e)
    order = {s: i for i, s in enumerate(evals.STATUSES)}
    return [(name, sorted(items, key=lambda e: order[e["status"]])) for name, items in groups.items()]


def evals_page(settings: Settings, conn: sqlite3.Connection, today: str,
               query: dict[str, list[str]]) -> tuple[str, str, str]:
    """/evals: how evals fit together, then one card per eval in getjo's catalogue grouped by dimension
    or by where it runs; ?eval=ID shows that eval's history instead."""
    data = evals.overview(settings.getjo_repo)
    catalog = data["catalog"]
    if catalog["state"] != "ok":
        return "Evals", "getjo's evaluations", _evals_header() + _evals_empty(settings, catalog)
    wanted = (query.get("eval") or [""])[0]
    if wanted:
        entry = next((e for e in data["evals"] if e["id"] == wanted), None)
        if entry is None:
            raise NotFound()
        return entry["name"], "Eval history", _eval_history(entry)
    group = (query.get("group") or [""])[0]
    group = group if group in dict(EVAL_GROUPS) else "dimension"
    switch = _switch("Group evals", [(evals_url(group=g if g != "dimension" else ""), label, g == group)
                                     for g, label in EVAL_GROUPS])
    counts = {s: sum(e["status"] == s for e in data["evals"]) for s in evals.STATUSES}
    tiles = (_tile("Evals", str(len(data["evals"])), f"<small>{counts['built']} built · {counts['partial']} partial · "
                                                    f"{counts['gap']} gaps</small>")
             + _tile("Runs saved", str(len(data["files"])), "<small>results files in getjo's evals/results</small>")
             + _tile("Last run", data["files"][-1]["when"] if data["files"] else "–",
                     f"<small>{h(data['files'][-1]['name'])}</small>" if data["files"] else
                     "<small>no runs yet: python -m evals.run --ci</small>"))
    sections = "".join(
        f'<section class="eval-group"><h2 class="eval-group-title">{h(name)} <span class="count">{len(items)}</span></h2>'
        f'<div class="eval-grid">{"".join(_eval_card(e) for e in items)}</div></section>'
        for name, items in _eval_groups(data["evals"], group))
    return ("Evals", f"getjo's evaluations · {today}",
            _evals_header() + f'<div class="tiles">{tiles}</div>'
            + f'<div class="eval-toolbar">{switch}</div>' + sections)


def _eval_history(entry: dict) -> str:
    """Every run of one eval over time: a chart of its headline metric and a table with what changed
    (model, prompt hash, commit), so a prompt or model change shows its effect."""
    runs = entry["runs"]
    back = f'<p class="back"><a href="/evals#eval-{h(entry["id"])}">← All evals</a></p>'
    card = _eval_card({**entry, "runs": runs})
    if not runs:
        hint = f"Run in getjo: {entry['command']}" if entry["command"] else ""
        return back + card + _card("History", _empty("No runs yet.", hint))
    metric = next((r["metric"] for r in reversed(runs) if r["metric"]), None)
    chart = charts.lines([r["when"][5:10] for r in runs],
                         [(_metric_name(metric), "s7", [r["value"] if r["metric"] == metric else None for r in runs])])
    rows = []
    for r in reversed(runs):
        s = r["summary"]
        cost = "–" if s["cost"] is None else _usd(s["cost"])
        passed = {True: _badge("passed", "success"), False: _badge("failed", "danger")}.get(s["passed"], "")
        rows.append((f'<a href="/evals/run?{h(urlencode({"file": r["name"]}))}">{_mono(h(r["when"]))}</a>',
                     h(r["set"]), h(s["model"]), _mono(h(s["prompt_sha"])), _mono(h(s["commit"][:7])),
                     h(_pct(r["value"])) + (" " + passed if passed else ""), h(cost)))
    table = _table(("When", "Set", "Model", "Prompt", "Commit", (_metric_name(metric) or "Result", "num"),
                    ("Cost", "num")), rows, "No runs yet.")
    chart = f'<div class="eval-chart">{chart}</div>' if chart else ""
    return (back + card + _card(f"History · {_metric_name(metric)}", chart + table
                                + _note("Newest first. Prompt is a hash of the scoring prompt (evals.run only): when "
                                        "it changes, the prompt text changed."), count=len(runs), cls="card-wide"))


def eval_run_page(settings: Settings, conn: sqlite3.Connection, today: str,
                  query: dict[str, list[str]]) -> tuple[str, str, str]:
    """/evals/run?file=NAME: one results file in detail (evals.run or promptfoo)."""
    name = (query.get("file") or [""])[0]
    data = evals.read_json(settings.getjo_repo, name) if name.endswith(".json") else None
    if data is None:
        raise NotFound()
    s = evals.summary(name, data)
    catalog = evals.load_catalog(settings.getjo_repo)
    owners = evals.evals_for_file(catalog, name)
    links = " · ".join(f'<a href="{h(evals_url(eval=e["id"]))}">{h(e["name"])}</a>' for e in owners)
    head = (f'<p class="back"><a href="/evals">← All evals</a>{" · History: " + links if links else ""}</p>')
    if s["runner"] == "promptfoo":
        body = _promptfoo_run(name, s, settings)
    else:
        body = _runpy_run(s)
    when = evals._when(evals.NAME_RE.match(name).group(3))
    return "Eval run", f"{name} · {when}", head + body


def _runpy_run(s: dict) -> str:
    m = s["metrics"]
    gate = ""
    if s["passed"] is not None:
        gate = _badge(f"gate {'passed' if s['passed'] else 'failed'} (min {s['min_agreement']})",
                      "success" if s["passed"] else "danger")
    tiles = (_tile("Agreement", _pct(m["agreement"]["rate"]), f"<small>{h(_rate_text(m['agreement']))}</small> {gate}")
             + _tile("Apply precision", _pct(m["apply_precision"]["rate"]), f"<small>{h(_rate_text(m['apply_precision']))}</small>")
             + _tile("Apply recall", _pct(m["apply_recall"]["rate"]), f"<small>{h(_rate_text(m['apply_recall']))}</small>")
             + _tile("Personal mentions", "–" if m["personal_mentions"]["value"] is None
                     else f"{m['personal_mentions']['value']:.0f}", "<small>target 0</small>",
                     "warning" if m["personal_mentions"]["value"] else "")
             + _tile("Cost", "–" if s["cost"] is None else _usd(s["cost"]), f"<small>{h(s['model'])}</small>"))
    facts = (f'<dl class="eval-facts"><dt>Set</dt><dd>{h(s["set"])}</dd><dt>Threshold</dt><dd>{h(s["threshold"])}</dd>'
             f'<dt>Prompt</dt><dd>{_mono(h(s["prompt_sha"]))}</dd><dt>Commit</dt><dd>{_mono(h(s["commit"][:12]))}</dd>'
             f'<dt>Created</dt><dd>{_mono(h(s["created_at"]))}</dd></dl>')
    labels = ("apply", "maybe", "skip")
    cols = (*labels, "unscored")
    conf_rows = []
    for e in labels:
        row = s["confusion"].get(e) if isinstance(s["confusion"].get(e), dict) else {}
        cells = []
        for p in cols:
            n = row.get(p, 0)
            css = "conf-hit" if p == e and n else "conf-miss" if n else "conf-zero"
            cells.append(f'<span class="{css}">{h(n)}</span>')
        conf_rows.append((f"<strong>{h(e)}</strong>", *cells))
    confusion = _table(("Expected ↓ / predicted →", *[(c, "num") for c in cols]), conf_rows, "No cases.")
    sweep = s["sweep"]
    chart = charts.lines([str(r["threshold"]) for r in sweep],
                         [("precision", "s8", [r["precision"]["rate"] for r in sweep]),
                          ("recall", "s5", [r["recall"]["rate"] for r in sweep])])
    sweep_rows = [((f"<strong>{h(r['threshold'])} ★</strong>" if r["threshold"] == s["threshold"] else h(r["threshold"])),
                   h(r["sent"]), h(_rate_text(r["precision"])), h(_rate_text(r["recall"]))) for r in sweep]
    legend = ('<div class="stack-legend"><span class="stack-key"><span class="swatch s8"></span>precision</span>'
              '<span class="stack-key"><span class="swatch s5"></span>recall</span></div>')
    sweep_body = (f'<div class="eval-chart">{legend}{chart}</div>' + _table(("Threshold", ("Sent", "num"), ("Precision", "num"), ("Recall", "num")),
                                                  sweep_rows, "No sweep in this file.")
                  + _note("★ the threshold in use. Precision: of jobs sent, how many are labelled apply. Recall: "
                          "of jobs labelled apply, how many are sent."))
    dis = []
    for c in s["disagreements"]:
        why = c.get("dealbreakers") or []
        reasons = [f"dealbreaker: {d.get('rule', '')}" + (f" (“{d.get('quote')}”)" if d.get("quote") else "")
                   for d in why if isinstance(d, dict)] + [str(x) for x in (c.get("reasons") or [])[:3]]
        personal = c.get("personal_mentions") or []
        dis.append((h(c.get("title")) + (f'<br><span class="muted">{h(c.get("company"))}</span>' if c.get("company") else ""),
                    h(c.get("expected")), h("–" if c.get("score") is None else c.get("score"))
                    + (f' <span class="muted">(raw {h(c.get("raw_score"))})</span>'
                       if c.get("raw_score") not in (None, c.get("score")) else ""),
                    h(c.get("predicted")),
                    "<ul class=\"eval-reasons\">" + "".join(f"<li>{h(x)}</li>" for x in reasons) + "</ul>"
                    + (f'<p class="muted">label: {h(c.get("label_reason"))}</p>' if c.get("label_reason") else "")
                    + (f'<p class="eval-personal">personal: {h(" | ".join(map(str, personal)))}</p>' if personal else "")))
    disagreements = _table(("Job", "Expected", ("Score", "num"), "Verdict", "Reasons"), dis,
                           "Every case agrees with its label.")
    return (f'<div class="tiles">{tiles}</div>' + _card("Run", facts)
            + _card("Confusion matrix", confusion + _note("Rows: the label. Columns: the verdict from the score."))
            + _card("Threshold sweep", sweep_body)
            + _card("Disagreements", disagreements, count=len(s["disagreements"]), cls="card-wide"))


def _promptfoo_run(name: str, s: dict, settings: Settings) -> str:
    stem = name[:-len(".json")]
    has_html = evals.safe_path(settings.getjo_repo, stem + ".html") is not None
    report = (f'<a class="btn btn-ghost btn-sm" href="/evals/report/{h(stem)}.html" target="_blank" rel="noreferrer">'
              "Open promptfoo report</a>" if has_html else '<span class="muted">No HTML report beside this file.</span>')
    model_rows = []
    named = sorted({k for m in s["per_model"] for k in m["named"]})
    judged = {c["judge"]["metric"] for case in s["cases"] for c in case["cells"].values() if c["judge"]}
    fmt = lambda k, v: "–" if v is None else f"{v:.2f}" if k in judged else _pct(v)  # noqa: E731
    for m in s["per_model"]:
        total = m["pass"] + m["fail"] + m["error"]
        model_rows.append((h(m["label"]), h(f"{m['pass']} of {total}"), h(m["fail"]), h(m["error"]),
                           *[h(fmt(k, m["named"].get(k))) for k in named],
                           h("–" if m["cost"] is None else _usd(m["cost"]))))
    per_model = _table(("Model", ("Passed", "num"), ("Failed", "num"), ("Errors", "num"),
                        *[(k.replace("_", " "), "num") for k in named], ("Cost", "num")), model_rows, "No providers.")
    labels = s["models"]
    grid_rows = []
    for c in s["cases"]:
        cells = []
        for model in labels:
            cell = c["cells"].get(model)
            if not cell:
                cells.append('<span class="muted">–</span>')
                continue
            checks = "".join(
                f'<li class="{"ok" if v["pass"] else "bad"}" title="{h(v["reason"])}">'
                f'{"✓" if v["pass"] else "✗"} {h(k.replace("_", " "))}'
                + ("" if v["pass"] or not v["reason"] else f'<span class="muted"> · {h(v["reason"])}</span>') + "</li>"
                for k, v in cell["checks"].items())
            judge = ""
            if cell["judge"]:
                j = cell["judge"]
                js = "–" if j["score"] is None else f"{j['score']:.2f}"
                judge = (f'<details class="eval-judge"><summary>judge {h(js)}</summary>'
                         f"<p>{h(j['reason'])}</p></details>")
            score = "–" if cell["score"] is None else f"{cell['score']:g}"
            cells.append(f'<div class="pf-cell pf-{"pass" if cell["pass"] else "fail"}">'
                         f'<strong>{"PASS" if cell["pass"] else "FAIL"}</strong> <span class="muted">score {h(score)}</span>'
                         + (f'<p class="eval-personal">{h(cell["error"])}</p>' if cell["error"] else "")
                         + f'<ul class="pf-checks">{checks}</ul>{judge}</div>')
        job = h(c["title"]) + (f'<br><span class="muted">{h(c["company"])}</span>' if c["company"] else "")
        grid_rows.append((job, h(c["expected"]), *cells))
    grid = _table(("Case", "Expected", *labels), grid_rows, "No cases in this file.")
    tiles = (_tile("Pass rate", _pct(s["metrics"]["pass_rate"]["rate"]), f"<small>{h(_rate_text(s['metrics']['pass_rate']))}</small>")
             + _tile("Models", str(len(labels)), f"<small>{h(', '.join(labels))}</small>")
             + _tile("Cost", "–" if s["cost"] is None else _usd(s["cost"]), "<small>all models</small>"))
    return (f'<div class="tiles">{tiles}</div>' + _card("Per model", per_model, action=report)
            + _card("Case × model", grid + _note("Each check is a named promptfoo assertion; the judge (LLM "
                                                  "rubric, weight 0) never fails a case, its 0-1 score is the "
                                                  "information."), count=len(s["cases"]), cls="card-wide"))


def eval_report(settings: Settings, path: str) -> Response:
    """GET /evals/report/NAME.html: a saved promptfoo HTML report from getjo's evals/results only
    (prep/evals.py checks the name and that it resolves inside that folder), sandboxed."""
    page = evals.read_html(settings.getjo_repo, path[len("/evals/report/"):])
    if page is None:
        return _text(404, "Not found.")
    return 200, [("Content-Type", "text/html; charset=utf-8"), *REPORT_HEADERS], page


def _page(status: int, title: str, subtitle: str, content: str, active: str, offers: int | None = None,
          actions: str = "", cards_todo: int | None = None, side: str = "") -> Response:
    """The app shell. `offers` is the number of new offers shown next to Applications and `cards_todo`
    the Learn cards to do next to Learn (none when 0 or unknown); `actions` is trusted HTML next to the
    title (Applications' My CV button)."""
    current = ' aria-current="page"'
    nav = "".join(
        f'<a href="{href}"{current if href == active else ""}>{_icon(icon)}<span>{h(name)}</span>'
        + (f'<span class="nav-count" title="New offers">{h(offers)}</span>' if href == "/" and offers else "")
        + (f'<span class="nav-count" title="Cards to do">{h(cards_todo)}</span>' if href == "/learn" and cards_todo
           else "")
        + "</a>"
        for href, name, icon in PAGES
    )
    sub = f'<p class="subtitle">{h(subtitle)}</p>' if subtitle else ""
    body = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="dark">'
        f"<title>{h(title)} · Hirey</title>"
        '<link rel="stylesheet" href="/static/style.css">'
        '<script src="/static/vim.js" defer></script><script src="/static/app.js" defer></script></head>'
        f'<body class="theme-{PAGE_THEMES.get(active, "sage")}"><div class="app">'
        '<aside class="sidebar"><a class="brand" href="/"><span class="brand-mark">hy</span>'
        '<span class="brand-name">Hirey</span></a>'
        f'<nav aria-label="Pages">{nav}</nav>{side}'
        '<p class="sidebar-foot"><span class="dot"></span>local only · 127.0.0.1</p></aside>'
        f'<main><header class="page-head"><div class="page-head-row"><h1>{h(title)}</h1>{actions}</div>{sub}</header>'
        f'{content}</main>'
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
    server_version = "Hirey"
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
    parser = argparse.ArgumentParser(prog="python -m prep.web", description="Hirey local dashboard")
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
