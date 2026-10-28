"""Local dashboard over prep.db: Pipeline, Matches, Tasks and Progress pages.

    python -m prep.web [--port 8765]

Standard library only (http.server), plain HTML and one CSS file, no JavaScript and no
external requests. It binds to 127.0.0.1 only and refuses any other address.

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
import logging
import sqlite3
import sys
from collections.abc import Mapping
from contextlib import closing
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from prep import db, getjo, stats
from prep.config import ConfigError, Settings, load_settings

log = logging.getLogger("prep.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"
LOOPBACK_NAMES = ("localhost", "127.0.0.1")
MAX_BODY = 64 * 1024
RECENT_DONE = 10

Response = tuple[int, list[tuple[str, str]], bytes]

PAGES = (("/", "Pipeline"), ("/matches", "Matches"), ("/tasks", "Tasks"), ("/progress", "Progress"))

# Fixed messages shown after a redirect; the URL carries only the key, never free text.
MESSAGES = {
    "applied": "Recorded as applied. It no longer appears in the match list.",
    "task_added": "Task added.",
    "task_done": "Task marked done.",
}

SECURITY_HEADERS = [
    ("Content-Security-Policy",
     "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; "
     "frame-ancestors 'none'; base-uri 'none'"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
]


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
            return _post(settings, url.path, _form(body))
        return _text(405, "Method not allowed.")
    except BadRequest as e:
        return _page(400, "Not saved", f'<p class="warning">{h(e)}</p><p><a href="/">Back to the dashboard</a></p>', "")


def _get(settings: Settings, path: str, query: dict, today: str) -> Response:
    if path == "/static/style.css":
        return 200, [("Content-Type", "text/css; charset=utf-8")], (STATIC_DIR / "style.css").read_bytes()
    pages = {"/": pipeline_page, "/matches": matches_page, "/tasks": tasks_page, "/progress": progress_page}
    if path not in pages:
        return _text(404, "Not found.")
    message = MESSAGES.get((query.get("msg") or [""])[0])
    with closing(db.connect(settings.prep_db)) as conn:
        title, content = pages[path](settings, conn, today)
    notice = f'<p class="notice">{h(message)}</p>' if message else ""
    return _page(200, title, notice + content, path)


def _post(settings: Settings, path: str, form: dict[str, str]) -> Response:
    actions = {"/matches/apply": mark_applied, "/tasks/add": add_task, "/tasks/done": complete_task}
    if path not in actions:
        return _text(404, "Not found.")
    with closing(db.connect(settings.prep_db)) as conn:
        try:
            location = actions[path](settings, conn, form)
        except (ValueError, LookupError) as e:
            raise BadRequest(str(e)) from e
    return 303, [("Location", location)], b""


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

def mark_applied(settings: Settings, conn: sqlite3.Connection, form: dict[str, str]) -> str:
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
    return "/matches?msg=applied"


def add_task(settings: Settings, conn: sqlite3.Connection, form: dict[str, str]) -> str:
    title = form.get("title")
    if not title:
        raise BadRequest("A task needs a title.")
    app_id = _int_field(form, "application_id")
    if app_id is not None and db.get_application(conn, app_id) is None:
        raise BadRequest(f"No application with id {app_id}.")
    task_id = db.add_task(conn, title, form.get("kind") or "admin", _optional(form, "due_date"), app_id)
    log.info("task %s added from the dashboard", task_id)
    return "/tasks?msg=task_added"


def complete_task(settings: Settings, conn: sqlite3.Connection, form: dict[str, str]) -> str:
    task_id = _int_field(form, "task_id")
    if task_id is None:
        raise BadRequest("task_id is missing.")
    db.complete_task(conn, task_id)
    log.info("task %s done from the dashboard", task_id)
    return "/tasks?msg=task_done"


# --- pages (GET) ------------------------------------------------------------------------
# Each returns (title, HTML content). Only columns that are shown are read; never pay.

def pipeline_page(settings: Settings, conn: sqlite3.Connection, today: str) -> tuple[str, str]:
    apps = stats.application_stages(conn)
    week = stats.applied_last_7_days(conn, today)
    target = settings.weekly_target
    reply = stats.reply_rate(apps)
    to_apply = sum(1 for a in apps if not a["submitted"])

    rate = "–" if reply["rate"] is None else f"{reply['rate']:.0%}"
    tiles = (
        _tile("Applied in the last 7 days", f"{week} / {target}", _meter(week, target))
        + _tile("Total applications", str(reply["submitted"]), f"<small>plus {to_apply} not yet sent</small>")
        + _tile("Reply rate", rate, f"<small>{reply['replied']} of {reply['submitted']} replied</small>")
    )
    warning = ""
    if reply["warning"]:
        warning = (f'<p class="warning">Reply rate is below {stats.REPLY_WARNING_RATE:.0%} after '
                   f'{reply["submitted"]} applications. Review the CV, targeting or tiers.</p>')

    overdue = db.overdue_actions(conn, today)
    overdue_rows = [
        (_date_cell(a["next_action_date"], today), h(a["company"]), h(a["role"]),
         h(a["status"]), h(a["next_action"]))
        for a in overdue
    ]
    content = (
        f'<div class="tiles">{tiles}</div>{warning}'
        f"<h2>Overdue next actions ({len(overdue)})</h2>"
        + _table(("Due", "Company", "Role", "Status", "Next action"), overdue_rows, "Nothing overdue.")
        + '<div class="columns"><section><h2>By status</h2>'
        + _table(("Status", "Count"), [(h(s), str(n)) for s, n in stats.counts_by_status(apps)], "No applications yet.")
        + "</section><section><h2>By tier</h2>"
        + _table(("Tier", "Count"), [(h(t), str(n)) for t, n in stats.counts_by_tier(apps)], "No applications yet.")
        + "</section></div>"
    )
    return "Pipeline", content


def matches_page(settings: Settings, conn: sqlite3.Connection, today: str) -> tuple[str, str]:
    threshold = getjo.DEFAULT_THRESHOLD
    try:
        with closing(getjo.connect_ro(settings.getjo_db)) as gconn:
            found = getjo.matches(gconn, db.linked_job_ids(conn), threshold)
    except getjo.GetjoUnavailable as e:
        return "Matches", f'<p class="warning">getjo matches are not available: {h(e)}.</p>'
    except sqlite3.Error as e:
        return "Matches", f'<p class="warning">Could not read the getjo database: {h(e)}.</p>'

    next_date = (date.fromisoformat(today) + timedelta(days=7)).isoformat()
    tiers = "".join(f'<option value="{h(t)}">{h(t)}</option>' for t in db.TIERS)
    rows = []
    for job in found:
        form = (
            '<form method="post" action="/matches/apply" class="inline">'
            f'<input type="hidden" name="job_id" value="{h(job["id"])}">'
            f'<select name="tier" aria-label="Tier"><option value="">tier</option>{tiers}</select>'
            '<input name="next_action" value="Follow up" aria-label="Next action">'
            f'<input type="date" name="next_date" value="{next_date}" aria-label="Next action date">'
            "<button>Mark applied</button></form>"
        )
        rows.append((
            f'<span class="score">{h(job["score"])}</span>',
            _link(job["url"], job["company"]),
            h(job["title"]), h(job["location"]), h((job["first_seen"] or "")[:10]), form,
        ))
    content = (
        f"<p>getjo jobs scored {threshold} or more that are not yet tracked ({len(found)}).</p>"
        + _table(("Score", "Company", "Role", "Location", "Seen", ""), rows, "No new matches.")
    )
    return "Matches", content


def tasks_page(settings: Settings, conn: sqlite3.Connection, today: str) -> tuple[str, str]:
    all_apps = db.list_applications(conn)
    apps = {a["id"]: f'{a["company"]} — {a["role"]}' for a in all_apps}
    open_apps = [a for a in all_apps if a["status"] not in db.CLOSED_STATUSES]

    rows = []
    for t in db.list_tasks(conn, "todo"):
        done = (
            '<form method="post" action="/tasks/done" class="inline">'
            f'<input type="hidden" name="task_id" value="{h(t["id"])}"><button>Done</button></form>'
        )
        rows.append((_date_cell(t["due_date"], today), h(t["title"]), h(t["kind"]),
                     h(apps.get(t["application_id"])), done))

    kinds = "".join(f'<option value="{h(k)}">{h(k)}</option>' for k in db.TASK_KINDS)
    app_options = "".join(
        f'<option value="{h(a["id"])}">{h(a["company"])} — {h(a["role"])}</option>' for a in open_apps
    )
    add_form = (
        '<form method="post" action="/tasks/add" class="add">'
        '<input name="title" placeholder="New task" required aria-label="Title">'
        f'<select name="kind" aria-label="Kind">{kinds}</select>'
        '<input type="date" name="due_date" aria-label="Due date">'
        f'<select name="application_id" aria-label="Application"><option value="">no application</option>{app_options}</select>'
        "<button>Add task</button></form>"
    )
    done_rows = [
        (h((t["done_at"] or "")[:10]), h(t["title"]), h(t["kind"]))
        for t in db.recent_done_tasks(conn, RECENT_DONE)
    ]
    content = (
        add_form
        + f"<h2>Open ({len(rows)})</h2>"
        + _table(("Due", "Task", "Kind", "Application", ""), rows, "No open tasks.")
        + "<h2>Recently done</h2>"
        + _table(("Done", "Task", "Kind"), done_rows, "Nothing done yet.")
    )
    return "Tasks", content


def progress_page(settings: Settings, conn: sqlite3.Connection, today: str) -> tuple[str, str]:
    apps = stats.application_stages(conn)
    submitted = sum(1 for a in apps if a["submitted"])
    conversion_rows = [
        (h(stage), str(n), "–" if share is None else f"{share:.0%}", _meter(n, submitted))
        for stage, n, share in stats.conversion(apps)
    ]
    content = (
        "<h2>Applications per week</h2>"
        + _bars(stats.applications_per_week(conn, today), settings.weekly_target)
        + "<h2>Stage conversion</h2>"
        + "<p><small>Share of sent applications that reached each stage or a later one.</small></p>"
        + _table(("Stage", "Reached", "Share", ""), conversion_rows, "No applications yet.")
        + "<h2>Tasks done per week</h2>"
        + _bars(stats.tasks_done_per_week(conn, today))
    )
    return "Progress", content


# --- HTML helpers ---------------------------------------------------------------------

def _page(status: int, title: str, content: str, active: str) -> Response:
    current = ' aria-current="page"'
    nav = "".join(f'<a href="{href}"{current if href == active else ""}>{name}</a>' for href, name in PAGES)
    body = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        f"<title>{h(title)} · inter_prep</title>"
        '<link rel="stylesheet" href="/static/style.css"></head>'
        f'<body><header><span class="brand">inter_prep</span><nav>{nav}</nav></header>'
        f"<main><h1>{h(title)}</h1>{content}</main></body></html>"
    )
    return status, [("Content-Type", "text/html; charset=utf-8"), *SECURITY_HEADERS], body.encode("utf-8")


def _text(status: int, message: str) -> Response:
    return status, [("Content-Type", "text/plain; charset=utf-8"), *SECURITY_HEADERS], message.encode("utf-8")


def _table(head: tuple[str, ...], rows: list[tuple[str, ...]], empty: str) -> str:
    """A table from already-escaped cells, or `empty` when there are no rows."""
    if not rows:
        return f'<p class="empty">{h(empty)}</p>'
    th = "".join(f"<th>{h(c)}</th>" for c in head)
    trs = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>"


def _tile(label: str, value: str, extra: str = "") -> str:
    return f'<div class="tile"><span class="label">{h(label)}</span><span class="value">{h(value)}</span>{extra}</div>'


def _meter(value: int, total: int) -> str:
    # <progress> needs no inline style, which the Content-Security-Policy forbids.
    return f'<progress value="{min(value, total)}" max="{max(total, 1)}"></progress>'


def _bars(weeks: list[tuple[str, int]], target: int | None = None) -> str:
    """One bar per week; an optional target sets the full width."""
    top = max([n for _, n in weeks] + [target or 0, 1])
    rows = "".join(
        f'<div class="bar"><span class="week">{h(start)}</span>{_meter(n, top)}<span class="n">{n}</span></div>'
        for start, n in weeks
    )
    note = f'<p><small>Week starting Monday. Target {target} per week.</small></p>' if target else \
        "<p><small>Week starting Monday.</small></p>"
    return f'<div class="bars">{rows}</div>{note}'


def _date_cell(day: str | None, today: str) -> str:
    if not day:
        return ""
    css = "overdue" if day < today else ("due-today" if day == today else "")
    return f'<span class="{css}">{h(day)}</span>' if css else h(day)


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
