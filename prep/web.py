"""Local dashboard over prep.db: Dashboard, Matches and Tasks pages.

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
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from prep import charts, db, getjo, stats
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

Response = tuple[int, list[tuple[str, str]], bytes]

# (href, name, icon) — icons are inline SVG bodies, so nothing is fetched.
PAGES = (
    ("/", "Dashboard", '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/>'
                       '<rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>'),
    ("/matches", "Matches", '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>'),
    ("/tasks", "Tasks", '<rect x="3" y="3" width="18" height="18" rx="4"/><path d="m8 12 3 3 5-6"/>'),
)
EMPTY_ICON = ('<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.5 5.1 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6'
              'l-3.5-6.9A2 2 0 0 0 16.8 4H7.2a2 2 0 0 0-1.7 1.1z"/>')

# Fixed messages shown after an action; the URL carries only the key, never free text.
MESSAGES = {
    "applied": "Recorded as applied. It no longer appears in the match list.",
    "task_added": "Task added.",
    "task_done": "Task marked done.",
    "task_reopened": "Task reopened.",
}

SECURITY_HEADERS = [
    ("Content-Security-Policy",
     "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self'; "
     "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
]

# Badge tones: a few semantic ones plus the shade steps of the page's hue (charts.SHADES,
# s1 dark to s10 light); anything else falls back to "neutral". Each page has one hue.
TONES = ("neutral", "accent", "info", "success", "warning", "danger", *charts.SHADES)
# One theme per page (defined in style.css). Spare themes for new pages: eucalyptus,
# slate-indigo, dusty-violet, mauve-rose, ochre.
PAGE_THEMES = {"/": "sage", "/matches": "dusty-blue", "/tasks": "terracotta"}
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
    pages = {"/": dashboard_page, "/matches": matches_page, "/tasks": tasks_page}
    if path not in pages:
        return _text(404, "Not found.")
    message = MESSAGES.get((query.get("msg") or [""])[0])
    with closing(db.connect(settings.prep_db)) as conn:
        title, subtitle, content = pages[path](settings, conn, today)
    notice = f'<div class="notice" role="status" data-flash>{h(message)}</div>' if message else ""
    return _page(200, title, subtitle, notice + content, path)


def _post(settings: Settings, path: str, form: dict[str, str], today: str, as_json: bool) -> Response:
    if path not in ACTIONS:
        return _text(404, "Not found.")
    action, page, msg = ACTIONS[path]
    with closing(db.connect(settings.prep_db)) as conn:
        try:
            fragment = action(settings, conn, form, today)
        except (ValueError, LookupError) as e:
            raise BadRequest(str(e)) from e
    if as_json:
        payload = {"ok": True, "message": MESSAGES[msg]}
        if fragment is not None:
            payload["html"] = fragment
        return _json(200, payload)
    return 303, [("Location", f"{page}?msg={msg}")], b""


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
# Each returns an HTML fragment for the page script (a new table row) or None.

def mark_applied(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> None:
    """Record a getjo match as applied (same logic as /applied --job-id)."""
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
    app_id, _ = db.record_application(
        conn, job["company"], job["title"],
        getjo_job_id=job["id"], link=job["url"], source=job["source"],
        tier=_optional(form, "tier"),
        next_action=_optional(form, "next_action"),
        next_action_date=_optional(form, "next_date"),
    )
    log.info("application %s recorded from the dashboard", app_id)


def add_task(settings: Settings, conn: sqlite3.Connection, form: dict[str, str], today: str) -> str:
    title = form.get("title")
    if not title:
        raise BadRequest("A task needs a title.")
    app_id = _int_field(form, "application_id")
    app = None if app_id is None else db.get_application(conn, app_id)
    if app_id is not None and app is None:
        raise BadRequest(f"No application with id {app_id}.")
    task_id = db.add_task(conn, title, form.get("kind") or "admin", _optional(form, "due_date"), app_id)
    log.info("task %s added from the dashboard", task_id)
    label = f'{app["company"]} — {app["role"]}' if app else None
    return _open_task_row(db.get_task(conn, task_id), label, today)


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
ACTIONS: dict[str, tuple[Callable[..., str | None], str, str]] = {
    "/matches/apply": (mark_applied, "/matches", "applied"),
    "/tasks/add": (add_task, "/tasks", "task_added"),
    "/tasks/done": (complete_task, "/tasks", "task_done"),
    "/tasks/reopen": (reopen_task, "/tasks", "task_reopened"),
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

    overdue = db.overdue_actions(conn, today)
    overdue_rows = [
        (_due(a["next_action_date"], today), h(a["company"]), h(a["role"]),
         _status_badge(a["status"]), h(a["next_action"]))
        for a in overdue
    ]
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
    tasks_week = charts.line(_weeks(stats.tasks_done_per_week(conn, today)))
    content = (
        f'<div class="tiles">{tiles}</div>{warning}'
        + _card("Overdue next actions", _table(
            (("Due", "date"), "Company", "Role", "Status", "Next action"), overdue_rows,
            "Nothing overdue.", hint="Next actions past their date show up here."), count=len(overdue))
        + '<div class="columns">'
        + _card("Applications by status", status_chart or _empty(no_apps), count=len(apps))
        + _card("Tiers by status", (tier_chart + _note("Column width: applications per tier. "
                                                       "Blocks: their current status.")) if tier_chart
                else _empty(no_apps))
        + "</div>"
        + _card("Stage conversion", (funnel + _note("Sent applications that reached each stage or a later "
                                                    "one, with their share.")) if funnel else _empty(no_apps),
                cls="card-funnel")
        + '<div class="columns">'
        + _card("Applications per week", per_week + _note(f"Week starting Monday. Target {target} per week."))
        + _card("Tasks done per week", tasks_week + _note("Week starting Monday."))
        + "</div>"
    )
    return "Dashboard", f"Pipeline and progress · {today}", content


def matches_page(settings: Settings, conn: sqlite3.Connection, today: str) -> tuple[str, str, str]:
    threshold = getjo.DEFAULT_THRESHOLD
    subtitle = f"getjo jobs scored {threshold} or more that are not yet tracked"
    try:
        with closing(getjo.connect_ro(settings.getjo_db)) as gconn:
            found = getjo.matches(gconn, db.linked_job_ids(conn), threshold)
    except getjo.GetjoUnavailable as e:
        return "Matches", subtitle, _banner(f"getjo matches are not available: {e}.", "warning")
    except sqlite3.Error as e:
        return "Matches", subtitle, _banner(f"Could not read the getjo database: {e}.", "warning")

    next_date = (date.fromisoformat(today) + timedelta(days=7)).isoformat()
    tiers = "".join(f'<option value="{h(t)}">{h(t)}</option>' for t in db.TIERS)
    rows = []
    for job in found:
        form = (
            '<form method="post" action="/matches/apply" class="inline" data-ajax="remove-row">'
            f'<input type="hidden" name="job_id" value="{h(job["id"])}">'
            f'<select name="tier" aria-label="Tier"><option value="">tier</option>{tiers}</select>'
            '<input name="next_action" value="Follow up" aria-label="Next action">'
            f'<input type="date" name="next_date" value="{next_date}" aria-label="Next action date">'
            + _button("Mark applied", size="sm") + "</form>"
        )
        rows.append((
            _score_badge(job["score"]),
            _link(job["url"], job["company"]),
            h(job["title"]), h(job["location"]), _mono(h((job["first_seen"] or "")[:10])), form,
        ))
    table = _table((("Score", "num"), "Company", "Role", "Location", ("Seen", "date"), ""), rows,
                   "No new matches.", table_id="matches", hint="New getjo matches appear here after each poll.")
    content = _card("New matches", table, count=len(found), count_for="matches",
                    action=_filter("matches", "Filter matches"))
    return "Matches", f"{subtitle} ({len(found)})", content


def tasks_page(settings: Settings, conn: sqlite3.Connection, today: str) -> tuple[str, str, str]:
    all_apps = db.list_applications(conn)
    apps = {a["id"]: f'{a["company"]} — {a["role"]}' for a in all_apps}
    open_apps = [a for a in all_apps if a["status"] not in db.CLOSED_STATUSES]

    open_tasks = db.list_tasks(conn, "todo")
    kinds = "".join(f'<option value="{h(k)}">{h(k)}</option>' for k in db.TASK_KINDS)
    app_options = "".join(
        f'<option value="{h(a["id"])}">{h(a["company"])} — {h(a["role"])}</option>' for a in open_apps
    )
    add_form = (
        '<form method="post" action="/tasks/add" class="add" data-ajax="add-row" data-target="open-tasks">'
        '<input name="title" placeholder="New task…" required aria-label="Title">'
        f'<select name="kind" aria-label="Kind">{kinds}</select>'
        '<input type="date" name="due_date" aria-label="Due date">'
        f'<select name="application_id" aria-label="Application"><option value="">no application</option>{app_options}</select>'
        + _button("Add task") + "</form>"
    )
    open_table = _table(
        (("Due", "date"), "Task", "Kind", "Application", ""),
        [_open_task_cells(t, apps.get(t["application_id"]), today) for t in open_tasks],
        "No open tasks.", table_id="open-tasks", hint="Add one above, or with /task in Claude Code.",
    )
    done_rows = []
    for t in db.recent_done_tasks(conn, RECENT_DONE):
        reopen = (
            '<form method="post" action="/tasks/reopen" class="inline">'
            f'<input type="hidden" name="task_id" value="{h(t["id"])}">'
            + _button("Reopen", "ghost", "sm") + "</form>"
        )
        done_rows.append((_mono(h((t["done_at"] or "")[:10])), h(t["title"]), _kind_badge(t["kind"]), reopen))
    content = (
        _card("Add a task", add_form, cls="card-form")
        + _card("Open", open_table, count=len(open_tasks), count_for="open-tasks",
                action=_filter("open-tasks", "Filter tasks"))
        + _card("Recently done", _table((("Done", "date"), "Task", "Kind", ""), done_rows, "Nothing done yet."))
    )
    return "Tasks", "Applications, prep, drills and admin", content


def _open_task_cells(task: dict, app_label: str | None, today: str) -> tuple[str, ...]:
    done = (
        '<form method="post" action="/tasks/done" class="inline" data-ajax="done">'
        f'<input type="hidden" name="task_id" value="{h(task["id"])}">'
        + _button("Done", "secondary", "sm") + "</form>"
    )
    return (_due(task["due_date"], today), h(task["title"]), _kind_badge(task["kind"]),
            f'<span class="muted">{h(app_label)}</span>' if app_label else "", done)


def _open_task_row(task: dict, app_label: str | None, today: str) -> str:
    """One <tr> for the open-tasks table, sent to the page script after an add."""
    return _row(_open_task_cells(task, app_label, today))


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


def _row(cells: tuple[str, ...]) -> str:
    return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"


def _table(head: tuple[str | tuple[str, str], ...], rows: list[tuple[str, ...]], empty: str,
           table_id: str = "", hint: str = "") -> str:
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
    trs = "".join(_row(row) for row in rows)
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
    return _badge(status.replace("_", " "), STATUS_SHADES.get(status, "neutral"))


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
    return status.replace("_", " ").capitalize()


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
