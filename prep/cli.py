"""Command-line entry points used by the Claude Code skills.

    python -m prep.cli today [--threshold 7] [--limit 15]
    python -m prep.cli applied (--job-id ID | --url URL | --company C --role R) [options]
    python -m prep.cli apps [--status STATUS]
    python -m prep.cli update APP_ID [--status S [--comment TEXT] [--interview-date DATE]]
                                     [--next-action TEXT [--next-date DATE]] ...
    python -m prep.cli comment APP_ID TEXT
    python -m prep.cli task add TITLE [--kind K] [--due DATE] [--app ID]
    python -m prep.cli task list [--all | --done]
    python -m prep.cli task done TASK_ID
    python -m prep.cli feedback export [--out PATH]
    python -m prep.cli queue                          (read-only: counts and application ids)
    python -m prep.cli research next [APP_ID]          (claims the work, prints what to do)
    python -m prep.cli research queue APP_ID [--kind research|cv_fit] [--redo]
    python -m prep.cli research save APP_ID [--file PATH]
    python -m prep.cli fit save APP_ID [--file PATH]

Every command is non-interactive: the skills ask the questions, then pass flags.
A status change runs the stage's tasks (db.move_application); --next-action adds a task, and
the application's next action always shows its next open task.
Dates accept YYYY-MM-DD, "today", "tomorrow" or "+N" (N days from today).
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

from prep import assistant, db, getjo, research
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
    line = f"#{app['id']} {app['company']} — {app['role']} [{db.status_label(app['status'])}]"
    if app.get("next_action"):
        line += f" next: {app['next_action']}"
        if app.get("next_action_date"):
            line += f" ({app['next_action_date']})"
    return line


def _task_line(task: dict) -> str:
    due = f" due {task['due_date']}" if task.get("due_date") else ""
    done = {"done": " ✓", "closed": " (closed)"}.get(task["status"], "")
    app = f" — {task['company']}" if task.get("company") else ""
    return f"#{task['id']} [{task['kind']}] {task['title']}{due}{done}{app}"


def _job_line(job: dict) -> str:
    seen = (job.get("first_seen") or "")[:10]
    location = f", {job['location']}" if job.get("location") else ""
    return f"[{job['score']}] {job['company']} — {job['title']}{location} (seen {seen})\n    {job['url']}\n    id: {job['id']}"


# --- commands -------------------------------------------------------------------

def cmd_today(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    today = db.today_iso()
    print(f"Today: {today}\n")

    db.auto_no_response(conn, settings.no_response_days, today)
    moved = db.recent_auto_moves(conn, today)
    if moved:
        print(f"Moved to No response (no reply in {settings.no_response_days} days), last 7 days ({len(moved)}):")
        print("\n".join(f"  #{m['id']} {m['company']} — {m['role']} ({m['changed_at'][:10]})" for m in moved))
        print()

    line = _queue_line(db.open_requests(conn))
    if line:
        print(line + "\n")

    due = db.tasks_due(conn, today)
    print(f"Tasks due today or overdue ({len(due)}):")
    print("\n".join(f"  {_task_line(t)}" for t in due) or "  none")

    try:
        with closing(getjo.connect_ro(settings.getjo_db)) as gconn:
            found = getjo.matches(gconn, db.excluded_job_ids(conn), args.threshold)
    except getjo.GetjoUnavailable as e:
        print(f"\nNew getjo matches: skipped ({e})")
        return 0
    shown = found[: args.limit]
    print(f"\nNew getjo matches ≥{args.threshold} not yet applied or skipped ({len(found)}):")
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
        "puzzle_round": args.puzzle_round,
        "pay": args.pay,
        "contact": args.contact,
        "notes": args.notes,
    }
    _check_next(args)
    company = args.company or (job["company"] if job else None)
    role = args.role or (job["title"] if job else None)
    if not (company and role):
        raise SystemExit("error: URL not found in getjo; pass --company and --role too")

    mark = db.last_request_id(conn)
    app_id, created = db.record_application(conn, company, role, status=args.status, date_applied=args.date, **fields)
    _add_next_task(conn, args, app_id)
    queued = _notify_new(settings, conn, mark)
    app = db.get_application(conn, app_id)
    if job:
        db.record_decision(conn, job, "interested" if app["status"] == "to_apply" else "applied")
    verb = "Recorded" if created else "Updated"
    tasks = [t for t in db.list_tasks(conn, "todo") if t["application_id"] == app_id]
    log.info("application %s saved", app_id)

    print(f"{verb} application {_app_line(app)}")
    if job:
        print(f"  linked to getjo job {job['id']} (score {job['score']})")
    if notice:
        print(f"  {notice}")
    if tasks:
        print("  open tasks:")
        print("\n".join(f"    {_task_line(t)}" for t in tasks))
    if queued:
        print(f"  queued for the agent: {_kinds(queued)} (run /research)")
    return 0


def _notify_new(settings: Settings, conn: sqlite3.Connection, mark: int) -> list[dict]:
    """Requests queued since `mark`; sent to the standing assistant when one is configured."""
    new = db.requests_since(conn, mark)
    assistant.notify(settings, new)
    return new


def _kinds(requests: list[dict]) -> str:
    return " and ".join(db.REQUEST_LABELS[k] for k in db.REQUEST_KINDS if any(r["kind"] == k for r in requests))


def cmd_apps(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    apps = db.list_applications(conn, args.status)
    print("\n".join(_app_line(a) for a in apps) or "no applications")
    return 0


def _check_next(args: argparse.Namespace) -> None:
    if args.next_date and not args.next_action:
        raise SystemExit("error: --next-date needs --next-action")


def _add_next_task(conn: sqlite3.Connection, args: argparse.Namespace, app_id: int) -> None:
    """--next-action becomes a task on the application, due --next-date."""
    if args.next_action:
        db.add_task(conn, args.next_action, "admin", args.next_date, app_id)


def cmd_update(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    _check_next(args)
    if (args.comment or args.interview_date) and not args.status:
        raise SystemExit("error: --comment and --interview-date go with --status")
    fields = {
        "last_contact": args.last_contact,
        "tier": args.tier,
        "puzzle_round": args.puzzle_round,
        "contact": args.contact,
        "notes": args.notes,
        "pay": args.pay,
    }
    fields = {k: v for k, v in fields.items() if v is not None}
    if not (fields or args.status or args.next_action):
        raise SystemExit("error: nothing to update")
    if db.get_application(conn, args.app_id) is None:
        raise LookupError(f"no application with id {args.app_id}")
    db.update_application(conn, args.app_id, **fields)
    mark = db.last_request_id(conn)
    if args.status and not db.move_application(conn, args.app_id, args.status, comment=args.comment,
                                                interview_date=args.interview_date):
        print(f"Already {db.status_label(args.status)}: nothing moved"
              + ("; --comment and --interview-date were not saved" if args.comment or args.interview_date else ""))
    _add_next_task(conn, args, args.app_id)
    queued = _notify_new(settings, conn, mark)
    print(f"Updated {_app_line(db.get_application(conn, args.app_id))}")
    tasks = db.app_tasks(conn).get(args.app_id, [])
    if args.status and tasks:
        print("  open tasks:")
        print("\n".join(f"    {_task_line(t)}" for t in tasks))
    if queued:
        print(f"  queued for the agent: {_kinds(queued)} (run /research)")
    return 0


def cmd_comment(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    db.add_comment(conn, args.app_id, args.text)
    log.info("comment added to application %s", args.app_id)  # never the text
    print(f"Comment added to {_app_line(db.get_application(conn, args.app_id))}")
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


FEEDBACK_FIELDS = ("getjo_job_id", "decision", "reason", "comment", "decided_at", "company", "title",
                   "location", "score", "url", "source")


def cmd_feedback(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    """Write every interested/applied/skipped decision as JSONL for getjo's scorer to read as examples."""
    if args.out:
        out = Path(args.out).expanduser()
    elif settings.private_dir:
        out = settings.private_dir / "feedback" / "job_decisions.jsonl"
    else:
        raise SystemExit("error: private_dir is not set (settings or PREP_PRIVATE_DIR); pass --out")
    records = feedback_records(conn, settings)
    write_jsonl(out, records)
    log.info("feedback export written")
    print(f"Wrote {len(records)} labelled decisions to {out}")
    return 0


def feedback_records(conn: sqlite3.Connection, settings: Settings) -> list[dict]:
    """One record per decision, oldest first. Rows without a location or score (applications
    recorded before job_decisions existed) take them from getjo when it still has the job."""
    decisions = sorted(db.job_decisions(conn).values(), key=lambda d: (d["decided_at"] or "", d["getjo_job_id"]))
    missing = [d for d in decisions if d["location"] is None or d["score"] is None]
    if missing:
        try:
            with closing(getjo.connect_ro(settings.getjo_db)) as gconn:
                for d in missing:
                    job = getjo.get_job(gconn, d["getjo_job_id"]) or {}
                    d["location"] = d["location"] if d["location"] is not None else job.get("location")
                    d["score"] = d["score"] if d["score"] is not None else job.get("score")
        except (getjo.GetjoUnavailable, sqlite3.Error) as e:
            print(f"  note: getjo not read ({e}); some records have no location or score")
    return [{k: d.get(k) for k in FEEDBACK_FIELDS} for d in decisions]


def write_jsonl(path: Path, records: list[dict]) -> None:
    """Replace `path` in one step (temp file + rename), so a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


# --- agent research queue -------------------------------------------------------------

def _queue_line(requests: list[dict]) -> str:
    """One line: open request counts and application ids only (never company names or text)."""
    if not requests:
        return ""
    counts = ", ".join(f"{sum(1 for r in requests if r['kind'] == k)} {db.REQUEST_LABELS[k].lower()}"
                       for k in db.REQUEST_KINDS if any(r["kind"] == k for r in requests))
    apps = ", ".join(str(a) for a in sorted({r["application_id"] for r in requests}))
    claimed = sum(1 for r in requests if r.get("claimed"))
    busy = f"; {claimed} already being done by another session" if claimed else ""
    return f"Agent work pending: {counts} (applications {apps}){busy}. Run /research to do it."


def cmd_queue(args: argparse.Namespace, settings: Settings) -> int:
    """Read-only (the SessionStart hook runs it in every session): prep.db is opened with
    mode=ro and nothing is created; a missing database or table prints nothing."""
    path = settings.prep_db
    if not path.is_file():
        return 0
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            rows = conn.execute(
                "SELECT application_id, kind, status, claimed_at FROM agent_requests "
                "WHERE status IN ('pending', 'claimed') ORDER BY id").fetchall()
    except sqlite3.Error:
        return 0
    cutoff = db._claim_cutoff()
    requests = [{"application_id": r[0], "kind": r[1], "claimed": r[2] == "claimed" and (r[3] or "") >= cutoff}
                for r in rows if r[1] in db.REQUEST_KINDS]
    line = _queue_line(requests)
    if line:
        print("inter_prep: " + line + " Offer it to the user, or run it at once if this is the "
              "standing assistant session.")
    return 0


def _private_dir(settings: Settings) -> Path:
    if not settings.private_dir:
        raise SystemExit("error: private_dir is not set (settings or PREP_PRIVATE_DIR)")
    return settings.private_dir


def cmd_research(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    if args.research_cmd == "queue":
        if db.get_application(conn, args.app_id) is None:
            raise LookupError(f"no application with id {args.app_id}")
        kinds = [args.kind] if args.kind else list(db.REQUEST_KINDS)
        for kind in kinds:
            _, created = db.queue_request(conn, args.app_id, kind, force=args.redo)
            print(f"{db.REQUEST_LABELS[kind]} for #{args.app_id}: {'queued' if created else 'already queued'}")
        return 0
    if args.research_cmd == "next":
        return _research_next(args, settings, conn)
    if args.research_cmd == "save":
        return _research_save(args, settings, conn)
    return 2


def _research_next(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    """Claim the open requests of one application (the given one, else the oldest unclaimed) and
    print the work packet: what to research, where to write it, and the command that saves it."""
    private_dir = _private_dir(settings)
    pending = [r for r in db.open_requests(conn, args.app_id) if not r["claimed"]]
    if not pending:
        others = [r for r in db.open_requests(conn, args.app_id) if r["claimed"]]
        print("Nothing pending" + (" (some requests are being done by another session)" if others else "") + ".")
        return 0
    app_id = args.app_id or pending[0]["application_id"]
    claimed = [r for r in pending if r["application_id"] == app_id and db.claim_request(conn, r["id"])]
    if not claimed:
        print("Another session claimed this work first; run research next again.")
        return 0
    app = db.get_application(conn, app_id)
    slug = db.company_slug(app["company"])
    remaining = len({r["application_id"] for r in db.open_requests(conn) if not r["claimed"]})
    print(f"Application #{app_id}: {app['company']} — {app['role']} [{db.status_label(app['status'])}]")
    if app.get("link"):
        print(f"  link: {app['link']}")
    print(f"  company slug: {slug}")
    for r in claimed:
        if r["kind"] == "research":
            _print_research_step(conn, settings, private_dir, app_id, slug, bool(r["force"]))
        else:
            _print_fit_step(settings, private_dir, app, slug)
    print(f"\nApplications still waiting after this one: {remaining}")
    return 0


def _print_research_step(conn: sqlite3.Connection, settings: Settings, private_dir: Path, app_id: int,
                         slug: str, force: bool) -> None:
    path = research.company_file(private_dir, slug)
    state, age = research.freshness(db.company_profile(conn, slug), settings.research_max_age_days, db.today_iso())
    if force:
        verdict = "Redo requested: research the company again and rewrite the file"
    elif state == "fresh" and path.is_file():
        verdict = f"profile is {age} days old (limit {settings.research_max_age_days}): reuse it, do not research again"
    elif state == "stale":
        verdict = f"profile is {age} days old (limit {settings.research_max_age_days}): research again and rewrite the file"
    elif path.is_file():
        verdict = "a file exists but is not in prep.db: check it, update it if needed, then save"
    else:
        verdict = "no profile yet: research the company and write the file"
    print("\nCompany research:")
    print(f"  {verdict}")
    print(f"  file: {path} ({'exists' if path.is_file() else 'missing'})")
    print(f"  then: python -m prep.cli research save {app_id}")


def _print_fit_step(settings: Settings, private_dir: Path, app: dict, slug: str) -> None:
    cv = private_dir / research.CV_FILE
    print("\nCV fit:")
    print(f"  CV: {cv} ({'exists' if cv.is_file() else 'MISSING: ask the user for cv.md before the fit check'})")
    print(f"  file: {research.fit_file(private_dir, app['id'], slug)}")
    print(f"  then: python -m prep.cli fit save {app['id']}")
    description = None
    if app.get("getjo_job_id"):
        try:
            with closing(getjo.connect_ro(settings.getjo_db)) as gconn:
                description = getjo.job_description(gconn, app["getjo_job_id"])
        except (getjo.GetjoUnavailable, sqlite3.Error) as e:
            print(f"  job description: not available ({e}); use the job link")
            return
    if description:
        print("  job description (from getjo, read-only):")
        print("\n".join(f"    {line}" for line in description.strip().splitlines()))
    else:
        print("  job description: none in getjo; use the job link")


def _research_save(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    app = db.get_application(conn, args.app_id)
    if app is None:
        raise LookupError(f"no application with id {args.app_id}")
    slug = db.company_slug(app["company"])
    path = Path(args.file).expanduser() if args.file else research.company_file(_private_dir(settings), slug)
    if not path.is_file():
        raise SystemExit(f"error: no file at {path}")
    profile = research.read_profile(path)
    profile["company"] = profile["company"] or app["company"]
    app_ids = db.save_company_profile(conn, slug, profile)
    log.info("company profile %s saved", slug)
    tier = f", suggested tier {profile['suggested_tier']}" if profile["suggested_tier"] else ""
    print(f"Saved company profile {slug} ({profile['researched_at']}{tier}); "
          f"research done for applications {', '.join(map(str, app_ids))}")
    return 0


def cmd_fit(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    app = db.get_application(conn, args.app_id)
    if app is None:
        raise LookupError(f"no application with id {args.app_id}")
    slug = db.company_slug(app["company"])
    path = Path(args.file).expanduser() if args.file else research.fit_file(_private_dir(settings), args.app_id, slug)
    if not path.is_file():
        raise SystemExit(f"error: no file at {path}")
    fit = research.read_fit(path)
    new_gaps = db.save_cv_fit(conn, args.app_id, fit["score"], fit["gaps"], str(path), fit["checked_at"])
    log.info("cv fit saved for application %s", args.app_id)  # never the gaps or the CV
    print(f"Saved CV fit for #{args.app_id}: {fit['score']}/5, {len(fit['gaps'])} gaps "
          f"({len(new_gaps)} new in the gaps list)")
    return 0


def _get_task(conn: sqlite3.Connection, task_id: int) -> dict:
    return dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())


# --- argument parsing -----------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m prep.cli", description="inter_prep commands")
    parser.add_argument("-v", "--verbose", action="store_true", help="log progress to stdout")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("today", help="auto no-response moves, tasks due, new getjo matches")
    p.add_argument("--threshold", type=int, default=getjo.DEFAULT_THRESHOLD)
    p.add_argument("--limit", type=int, default=15, help="max matches to show")
    p.set_defaults(func=cmd_today)

    p = sub.add_parser("applied", help="record an application")
    p.add_argument("--job-id", help="getjo job id (source:company:job_id)")
    p.add_argument("--url")
    p.add_argument("--company")
    p.add_argument("--role")
    p.add_argument("--status", choices=db.APPLICATION_STATUSES,
                   help="default: applied (later stages are kept); to_apply records it as interested")
    p.add_argument("--date", type=parse_day, help="date applied (default today)")
    p.add_argument("--tier", choices=db.TIERS)
    p.add_argument("--type", choices=db.APPLICATION_TYPES)
    p.add_argument("--source")
    p.add_argument("--next-action", help="adds a task on the application")
    p.add_argument("--next-date", type=parse_day, help="due date of the --next-action task")
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
    p.add_argument("--status", choices=db.APPLICATION_STATUSES,
                   help="move to this stage: closes the previous stage's auto tasks, adds the new ones")
    p.add_argument("--comment", help="comment stored with the stage change")
    p.add_argument("--interview-date", type=parse_day,
                   help="for interview stages: stored in interviews; prep tasks are due the day before")
    p.add_argument("--next-action", help="adds a task on the application")
    p.add_argument("--next-date", type=parse_day, help="due date of the --next-action task")
    p.add_argument("--last-contact", type=parse_day)
    p.add_argument("--tier", choices=db.TIERS)
    p.add_argument("--puzzle-round", choices=db.PUZZLE_ROUND)
    p.add_argument("--contact")
    p.add_argument("--notes")
    p.add_argument("--pay", help="free text; stored only in the local database")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("comment", help="add a comment to an application")
    p.add_argument("app_id", type=int)
    p.add_argument("text")
    p.set_defaults(func=cmd_comment)

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

    p = sub.add_parser("feedback", help="export interested/applied/skipped labels for getjo")
    fsub = p.add_subparsers(dest="feedback_cmd", required=True)
    f = fsub.add_parser("export", help="write private_dir/feedback/job_decisions.jsonl")
    f.add_argument("--out", help="output path (default: private_dir/feedback/job_decisions.jsonl)")
    p.set_defaults(func=cmd_feedback)

    p = sub.add_parser("queue", help="read-only: open research / CV fit requests (counts and ids)")
    p.set_defaults(func=cmd_queue, read_only=True)

    p = sub.add_parser("research", help="company research and CV fit queue (used by /research)")
    rsub = p.add_subparsers(dest="research_cmd", required=True)
    r = rsub.add_parser("next", help="claim the next application's requests and print what to do")
    r.add_argument("app_id", type=int, nargs="?")
    r = rsub.add_parser("queue", help="queue research and/or a CV fit for an application")
    r.add_argument("app_id", type=int)
    r.add_argument("--kind", choices=db.REQUEST_KINDS)
    r.add_argument("--redo", action="store_true", help="research again even if the profile is fresh")
    r = rsub.add_parser("save", help="store the company profile from companies/<slug>.md")
    r.add_argument("app_id", type=int)
    r.add_argument("--file", help="default: private_dir/companies/<slug>.md")
    p.set_defaults(func=cmd_research)

    p = sub.add_parser("fit", help="CV fit results (used by /research)")
    fsub = p.add_subparsers(dest="fit_cmd", required=True)
    f = fsub.add_parser("save", help="store the CV fit from fit/<app-id>-<slug>.md")
    f.add_argument("app_id", type=int)
    f.add_argument("--file", help="default: private_dir/fit/<app-id>-<slug>.md")
    p.set_defaults(func=cmd_fit)
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
        if getattr(args, "read_only", False):
            return args.func(args, settings)
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
