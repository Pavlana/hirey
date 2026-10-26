"""Command-line entry points used by the Claude Code skills.

    python -m prep.cli today [--threshold 7] [--limit 15]
    python -m prep.cli applied (--job-id ID | --url URL | --company C --role R) [options]
    python -m prep.cli apps [--status STATUS]
    python -m prep.cli update APP_ID [--status S] [--next-action TEXT] [--next-date DATE] ...
    python -m prep.cli task add TITLE [--kind K] [--due DATE] [--app ID]
    python -m prep.cli task list [--all | --done]
    python -m prep.cli task done TASK_ID

Every command is non-interactive: the skills ask the questions, then pass flags.
Dates accept YYYY-MM-DD, "today", "tomorrow" or "+N" (N days from today).
"""

import argparse
import logging
import sqlite3
import sys
from contextlib import closing
from datetime import date, timedelta

from prep import db, getjo
from prep.config import ConfigError, Settings, load_settings

log = logging.getLogger("prep")


def parse_day(value: str) -> str:
    """Turn a date argument into YYYY-MM-DD."""
    text = value.strip().lower()
    today = date.today()
    if text == "today":
        return today.isoformat()
    if text == "tomorrow":
        return (today + timedelta(days=1)).isoformat()
    if text.startswith("+") and text[1:].rstrip("d").isdigit():
        return (today + timedelta(days=int(text[1:].rstrip("d")))).isoformat()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a date: {value!r} (use YYYY-MM-DD, today, tomorrow or +N)")


# --- output helpers -----------------------------------------------------------

def _app_line(app: dict) -> str:
    line = f"#{app['id']} {app['company']} — {app['role']} [{app['status']}]"
    if app.get("next_action"):
        line += f" next: {app['next_action']}"
        if app.get("next_action_date"):
            line += f" ({app['next_action_date']})"
    return line


def _task_line(task: dict) -> str:
    due = f" due {task['due_date']}" if task.get("due_date") else ""
    done = " ✓" if task["status"] == "done" else ""
    return f"#{task['id']} [{task['kind']}] {task['title']}{due}{done}"


def _job_line(job: dict) -> str:
    seen = (job.get("first_seen") or "")[:10]
    location = f", {job['location']}" if job.get("location") else ""
    return f"[{job['score']}] {job['company']} — {job['title']}{location} (seen {seen})\n    {job['url']}\n    id: {job['id']}"


# --- commands -------------------------------------------------------------------

def cmd_today(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    today = db.today_iso()
    print(f"Today: {today}\n")

    overdue = db.overdue_actions(conn, today)
    print(f"Next actions due ({len(overdue)}):")
    print("\n".join(f"  {_app_line(a)}" for a in overdue) or "  none")

    due = db.tasks_due(conn, today)
    print(f"\nTasks due today or overdue ({len(due)}):")
    print("\n".join(f"  {_task_line(t)}" for t in due) or "  none")

    try:
        with closing(getjo.connect_ro(settings.getjo_db)) as gconn:
            found = getjo.matches(gconn, db.linked_job_ids(conn), args.threshold)
    except getjo.GetjoUnavailable as e:
        print(f"\nNew getjo matches: skipped ({e})")
        return 0
    shown = found[: args.limit]
    print(f"\nNew getjo matches ≥{args.threshold} not yet in applications ({len(found)}):")
    print("\n".join(f"  {_job_line(j)}" for j in shown) or "  none")
    if len(found) > len(shown):
        print(f"  … and {len(found) - len(shown)} more (use --limit)")
    return 0


def _resolve_job(args: argparse.Namespace, settings: Settings) -> tuple[dict | None, str | None]:
    """Find the getjo job this application refers to. Returns (job, notice)."""
    try:
        gconn = getjo.connect_ro(settings.getjo_db)
    except getjo.GetjoUnavailable as e:
        if args.job_id:
            raise SystemExit(f"error: cannot look up --job-id: {e}")
        return None, f"getjo not checked ({e})"
    with closing(gconn):
        if args.job_id:
            job = getjo.get_job(gconn, args.job_id)
            if job is None:
                raise SystemExit(f"error: no getjo job with id {args.job_id!r}")
            return job, None
        if args.url:
            job = getjo.find_by_url(gconn, args.url)
            if job:
                return job, None
        if args.company and args.role:
            candidates = getjo.find_by_company_role(gconn, args.company, args.role)
            if len(candidates) == 1:
                return candidates[0], None
            if len(candidates) > 1:
                ids = ", ".join(c["id"] for c in candidates)
                return None, f"several getjo jobs match, not linked (pass --job-id): {ids}"
    return None, "no matching getjo job; stored without a link"


def cmd_applied(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    if not (args.job_id or args.url or (args.company and args.role)):
        raise SystemExit("error: give --job-id, --url, or both --company and --role")

    job, notice = _resolve_job(args, settings)
    fields = {
        "getjo_job_id": job["id"] if job else None,
        "link": args.url or (job["url"] if job else None),
        "tier": args.tier,
        "type": args.type,
        "source": args.source or (job["source"] if job else None),
        "next_action": args.next_action,
        "next_action_date": args.next_date,
        "puzzle_round": args.puzzle_round,
        "pay": args.pay,
        "contact": args.contact,
        "notes": args.notes,
    }
    company = args.company or (job["company"] if job else None)
    role = args.role or (job["title"] if job else None)
    if not (company and role):
        raise SystemExit("error: URL not found in getjo; pass --company and --role too")

    existing = None
    if job:
        existing = db.find_by_job(conn, job["id"])
    if existing is None and fields["link"]:
        existing = db.find_by_link(conn, fields["link"])

    fields.update(_status_fields(args, existing))
    if existing:
        db.update_application(conn, existing["id"], **{k: v for k, v in fields.items() if v is not None})
        app_id, verb = existing["id"], "Updated"
    else:
        app_id, verb = db.add_application(conn, company, role, **fields), "Recorded"
    app = db.get_application(conn, app_id)
    log.info("application %s saved", app_id)

    print(f"{verb} application {_app_line(app)}")
    if job:
        print(f"  linked to getjo job {job['id']} (score {job['score']})")
    if notice:
        print(f"  {notice}")
    if not app["next_action"]:
        print("  no next action set — add one with: python -m prep.cli update "
              f"{app_id} --next-action TEXT --next-date DATE")
    return 0


def _status_fields(args: argparse.Namespace, existing: dict | None) -> dict:
    """Status and dates for /applied. Never moves an application backwards: a later stage
    (technical, offer, ...) is kept unless --status is passed explicitly."""
    current = existing["status"] if existing else None
    if args.status:
        status = args.status
    elif current in (None, "to_apply"):
        status = "applied"
    else:
        status = current
    fields: dict = {"status": status}
    if status != "to_apply" and (args.date or not (existing and existing["date_applied"])):
        fields["date_applied"] = args.date or db.today_iso()
        if current in (None, "to_apply"):
            fields["last_contact"] = fields["date_applied"]
    return fields


def cmd_apps(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    apps = db.list_applications(conn, args.status)
    print("\n".join(_app_line(a) for a in apps) or "no applications")
    return 0


def cmd_update(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    fields = {
        "status": args.status,
        "next_action": args.next_action,
        "next_action_date": args.next_date,
        "last_contact": args.last_contact,
        "tier": args.tier,
        "puzzle_round": args.puzzle_round,
        "contact": args.contact,
        "notes": args.notes,
        "pay": args.pay,
    }
    fields = {k: v for k, v in fields.items() if v is not None}
    if args.clear_next:
        fields.update(next_action=None, next_action_date=None)
    if not fields:
        raise SystemExit("error: nothing to update")
    db.update_application(conn, args.app_id, **fields)
    print(f"Updated {_app_line(db.get_application(conn, args.app_id))}")
    return 0


def cmd_task(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    if args.task_cmd == "add":
        task_id = db.add_task(conn, args.title, args.kind, args.due, args.app)
        print(f"Added task {_task_line(_get_task(conn, task_id))}")
    elif args.task_cmd == "list":
        status = None if args.all else ("done" if args.done else "todo")
        tasks = db.list_tasks(conn, status)
        print("\n".join(_task_line(t) for t in tasks) or "no tasks")
    elif args.task_cmd == "done":
        db.complete_task(conn, args.task_id)
        print(f"Done: {_task_line(_get_task(conn, args.task_id))}")
    return 0


def _get_task(conn: sqlite3.Connection, task_id: int) -> dict:
    return dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())


# --- argument parsing -----------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m prep.cli", description="inter_prep commands")
    parser.add_argument("-v", "--verbose", action="store_true", help="log progress to stdout")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("today", help="overdue actions, tasks due, new getjo matches")
    p.add_argument("--threshold", type=int, default=getjo.DEFAULT_THRESHOLD)
    p.add_argument("--limit", type=int, default=15, help="max matches to show")
    p.set_defaults(func=cmd_today)

    p = sub.add_parser("applied", help="record an application")
    p.add_argument("--job-id", help="getjo job id (source:company:job_id)")
    p.add_argument("--url")
    p.add_argument("--company")
    p.add_argument("--role")
    p.add_argument("--status", choices=db.APPLICATION_STATUSES, help="default: applied (later stages are kept)")
    p.add_argument("--date", type=parse_day, help="date applied (default today)")
    p.add_argument("--tier", choices=db.TIERS)
    p.add_argument("--type", choices=db.APPLICATION_TYPES)
    p.add_argument("--source")
    p.add_argument("--next-action")
    p.add_argument("--next-date", type=parse_day)
    p.add_argument("--puzzle-round", choices=db.PUZZLE_ROUND)
    p.add_argument("--pay", help="free text; stored only in the local database")
    p.add_argument("--contact")
    p.add_argument("--notes")
    p.set_defaults(func=cmd_applied)

    p = sub.add_parser("apps", help="list applications")
    p.add_argument("--status", choices=db.APPLICATION_STATUSES)
    p.set_defaults(func=cmd_apps)

    p = sub.add_parser("update", help="update an application")
    p.add_argument("app_id", type=int)
    p.add_argument("--status", choices=db.APPLICATION_STATUSES)
    p.add_argument("--next-action")
    p.add_argument("--next-date", type=parse_day)
    p.add_argument("--clear-next", action="store_true", help="clear next action and date")
    p.add_argument("--last-contact", type=parse_day)
    p.add_argument("--tier", choices=db.TIERS)
    p.add_argument("--puzzle-round", choices=db.PUZZLE_ROUND)
    p.add_argument("--contact")
    p.add_argument("--notes")
    p.add_argument("--pay", help="free text; stored only in the local database")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("task", help="add, list, complete tasks")
    tsub = p.add_subparsers(dest="task_cmd", required=True)
    t = tsub.add_parser("add")
    t.add_argument("title")
    t.add_argument("--kind", default="admin", choices=db.TASK_KINDS)
    t.add_argument("--due", type=parse_day)
    t.add_argument("--app", type=int, help="related application id")
    t = tsub.add_parser("list")
    group = t.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true")
    group.add_argument("--done", action="store_true")
    t = tsub.add_parser("done")
    t.add_argument("task_id", type=int)
    p.set_defaults(func=cmd_task)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        settings = load_settings()
        log.info("prep.db at %s", settings.prep_db)
        conn = db.connect(settings.prep_db)
        try:
            return args.func(args, settings, conn)
        finally:
            conn.close()
    except (ConfigError, ValueError, LookupError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
