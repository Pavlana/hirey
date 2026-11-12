"""Command-line entry points used by the Claude Code skills.

    python -m prep.cli today [--threshold 7] [--limit 15]
    python -m prep.cli applied (--job-id ID | --url URL | --company C --role R) [options]
    python -m prep.cli apps [--status STATUS]
    python -m prep.cli update APP_ID [--status S [--comment TEXT] [--interview-date DATE]]
                                     [--next-action TEXT [--next-date DATE]] ...
    python -m prep.cli comment APP_ID TEXT
    python -m prep.cli comments list APP_ID | amend REF TEXT | remove REF   (REF: c12 or h34)
    python -m prep.cli task add TITLE [--kind K] [--due DATE] [--app ID]
    python -m prep.cli task list [--all | --done]
    python -m prep.cli task done TASK_ID
    python -m prep.cli task edit TASK_ID [--title T] [--due DATE | --no-due]
    python -m prep.cli task delete TASK_ID
    python -m prep.cli session add --kind mock|real [--app ID] [--stage S] [--date DATE] [--score AREA=N ...]
                                   [--file PATH] [--gap TEXT ...] [--due DATE]   (one drill task per gap)
    python -m prep.cli session list
    python -m prep.cli debrief --kind real|mock [--app ID] [--stage S] [--date DATE] [--questions T]
        [--went-well T] [--to-improve T] [--score AREA=N ...] [--gap TEXT ...] [--transcript < FILE]
    python -m prep.cli mock path APP --stage S [--date DATE]   (transcript file for a new mock, under sessions/)
    python -m prep.cli gap add TEXT ... [--session ID] [--app ID] [--due DATE]   (one drill task per gap)
    python -m prep.cli gap list [--all | --closed]
    python -m prep.cli gap close GAP_ID                    (its open drill tasks are marked done)
    python -m prep.cli skip APP_ID --reason R [--comment TEXT]   (Interested only, as the panel's Skip)
    python -m prep.cli cv sent APP_ID FILE_NAME           (which tailored CV version was sent)
    python -m prep.cli cv show | skills | history         (the master CV, its skills, its saved versions)
    python -m prep.cli cv draft                            (copy cv.md to the draft the agent edits)
    python -m prep.cli cv save --note TEXT [--file PATH] [--to MSG]   (backup first, then cv.md replaced)
    python -m prep.cli cv restore VERSION [--to MSG]      (the replaced text stays a version: undo = restore it)
    python -m prep.cli cv diff VERSION [--with VERSION]   (a version against cv.md, or two versions)
    python -m prep.cli feedback export [--out PATH]
    python -m prep.cli queue                          (read-only: counts and application ids)
    python -m prep.cli research next [APP_ID]          (claims the work, prints what to do)
    python -m prep.cli research queue APP_ID [--kind research|cv_fit|tailor|cv_eval] [--redo]
    python -m prep.cli research save APP_ID [--file PATH]
    python -m prep.cli fit save APP_ID [--file PATH]
    python -m prep.cli tailor path APP_ID [--role-short X] [--copy]   (the next version's file)
    python -m prep.cli tailor save APP_ID [--to MSG] [--file PATH]   (the thread note on stdin; makes the PDF, checks it)
    python -m prep.cli tailor pdf APP_ID [--version N]   (the PDF next to a version's file, default the latest; checks it again)
    python -m prep.cli tailor check APP_ID [--version N]   (checks a version again; pages from its PDF, no WeasyPrint)
    python -m prep.cli tailor eval APP_ID [--version N] [--file PATH]   (stores the AI check from <version>.eval.md)
    python -m prep.cli thread show [APP_ID | --job ID | --cv | --message MSG] [--claim]
                                                       (messages and context; no id: the whole search)
    python -m prep.cli thread reply [APP_ID] --to MSG_ID [--file PATH]   (reply text from the file or stdin)
    python -m prep.cli review [--all]                  (read-only: the top 3 open tasks in review order)
    python -m prep.cli thread pending
    python -m prep.cli brief next | queue (--job ID | APP_ID) [--redo]   (job briefs for the panel)
    python -m prep.cli brief save (--job ID | APP_ID) [--file PATH] | show (--job ID | APP_ID)   (text on stdin)

Every command is non-interactive: the skills ask the questions, then pass flags.
A status change runs the stage's tasks (db.move_application); --next-action adds a task, and
the application's next action always shows its next open task.
Dates accept YYYY-MM-DD, "today", "tomorrow" or "+N" (N days from today).
"""

import argparse
import json
import logging
import os
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

from prep import assistant, config, cvcheck, cveval, cvfile, cvpdf, db, getjo, research
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


def _job_line(job: dict, now: str | None = None, stale_days: int = 3) -> str:
    seen = (job.get("first_seen") or "")[:10]
    location = f", {job['location']}" if job.get("location") else ""
    return (f"[{job['score']}] {job['company']} — {job['title']}{location} (seen {seen}){_job_flags(job, now, stale_days)}"
            f"\n    {job['url']}\n    id: {job['id']}")


def _job_flags(job: dict, now: str | None, stale_days: int) -> str:
    """" · may be closed", " · expires in N days" or " · closed", from getjo's open/closed columns."""
    state = getjo.job_state(job, now or db.now_iso(), stale_days)
    if state["closed"]:
        return " · closed"
    flags = [" · may be closed"] if state["may_be_closed"] else []
    if state["expires_in"] is not None:
        days = state["expires_in"]
        flags.append(" · expires today" if days <= 0 else f" · expires in {days} day{'s' if days != 1 else ''}")
    return "".join(flags)


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
    line = _thread_line(db.pending_messages(conn))
    if line:
        print(line + "\n")
    line = _brief_line(db.open_briefs(conn))
    if line:
        print(line + "\n")

    due = db.tasks_due(conn, today)
    print(f"Tasks due today or overdue ({len(due)}):")
    print("\n".join(f"  {_task_line(t)}" for t in due) or "  none")

    now = db.now_iso()
    try:
        with closing(getjo.connect_ro(settings.getjo_db)) as gconn:
            found = getjo.matches(gconn, db.excluded_job_ids(conn), args.threshold, now=now)
    except getjo.GetjoUnavailable as e:
        print(f"\nNew getjo matches: skipped ({e})")
        return 0
    shown = found[: args.limit]
    print(f"\nNew getjo matches ≥{args.threshold} not yet applied or skipped, closed and expired left out ({len(found)}):")
    print("\n".join(f"  {_job_line(j, now, settings.stale_days)}" for j in shown) or "  none")
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
    if app["status"] == "to_apply":
        print(f"  a \"{db.REVIEW_TASK[0]}\" task follows once company research and CV fit are both saved")
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


def _same_stage(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    """Already at --status: keep the comment as a free comment and, for an interview stage, the
    interview date (a rescheduled call or a second round), so nothing the user said is lost."""
    saved = []
    if args.comment:
        db.add_comment(conn, args.app_id, args.comment)
        saved.append("comment saved")
    if args.interview_date and args.status in db.INTERVIEW_STAGES:
        moved, due = db.set_interview_date(conn, args.app_id, args.status, args.interview_date)
        saved.append(f"interview date {args.interview_date} saved")
        if moved:
            saved.append(f"{moved} prep task{'s' if moved != 1 else ''} moved to {due}")
    elif args.interview_date:
        saved.append("--interview-date not saved (not an interview stage)")
    print(f"Already {db.status_label(args.status)}: nothing moved" + "".join(f"; {s}" for s in saved))


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
        _same_stage(conn, args)
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


def cmd_comments(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    """List, amend or remove comments; a ref is c<id> (free comment) or h<id> (comment on a stage change)."""
    if args.comments_cmd == "list":
        app = db.get_application(conn, args.app_id)
        if app is None:
            raise LookupError(f"no application with id {args.app_id}")
        found = db.comments(conn, args.app_id)
        print(f"Comments on {_app_line(app)}, newest first:")
        print("\n".join(_comment_line(c) for c in found) or "  none")
    elif args.comments_cmd == "amend":
        db.amend_comment(conn, args.ref, args.text)
        print(f"Comment {args.ref} amended.")
    else:
        db.remove_comment(conn, args.ref)
        print(f"Comment {args.ref} removed" + (" (the stage change stays)." if args.ref.startswith("h") else "."))
    log.info("comment %s: %s", args.comments_cmd, getattr(args, "ref", ""))  # never the text
    return 0


def _comment_line(c: dict) -> str:
    stage = f" [with move to {db.status_label(c['status'])}]" if c["status"] else ""
    return f"  {c['ref']} {c['at'][:10]}{stage}: {c['body']}"


def cmd_skip(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    db.skip_application(conn, args.app_id, args.reason, args.comment)
    log.info("application %s skipped (reason %s)", args.app_id, args.reason)  # never the comment
    print(f"Skipped {_app_line(db.get_application(conn, args.app_id))}; open tasks and agent requests closed"
          + ("; Job Offers shows it under Skipped." if db.get_application(conn, args.app_id)["getjo_job_id"] else "."))
    return 0


def cmd_cv(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    """cv sent: which tailored version went out. The rest is the master CV: show, skills, history,
    draft, save, restore and diff. Every write goes through cvfile.write_cv (backup first, atomic)."""
    if args.cv_cmd == "sent":
        return _cv_sent(args, conn)
    private_dir = _private_dir(settings)
    if getattr(args, "to", None) is not None and db.message_topic(conn, args.to) != "cv":
        raise LookupError(f"message {args.to} is not about the master CV")  # checked before anything is written
    if args.cv_cmd == "show":
        text = cvfile.read_cv(private_dir)
        if text is None:
            raise LookupError(f"no cv.md at {cvfile.cv_path(private_dir)}")
        print(f"{cvfile.cv_path(private_dir)} ({research.word_count(text)} words; {_cv_state(conn, private_dir)})\n")
        print(text)
    elif args.cv_cmd == "skills":
        text = cvfile.read_cv(private_dir)
        if text is None:
            raise LookupError(f"no cv.md at {cvfile.cv_path(private_dir)}")
        groups = cvfile.skills(text)
        print("\n".join(f"{name}: {', '.join(items)}" for name, items in groups)
              or "No skills section found (a heading with \"Skills\" in it).")
    elif args.cv_cmd == "history":
        _print_cv_history(conn, private_dir)
    elif args.cv_cmd == "draft":
        path = cvfile.make_draft(private_dir)
        print(path)
        print("  a copy of cv.md: edit it (never cv.md itself), then: python -m prep.cli cv save "
              "--note \"what changed\" [--to MSG]")
    elif args.cv_cmd == "save":
        result = cvfile.save_draft(conn, private_dir, args.note, Path(args.file).expanduser() if args.file else None)
        _cv_saved(conn, args, result, "Saved")
    elif args.cv_cmd == "restore":
        result = cvfile.restore(conn, private_dir, args.version)
        _cv_saved(conn, args, result, f"Restored v{args.version} as")
    elif args.cv_cmd == "diff":
        old = db.cv_version(conn, args.version)
        if old is None:
            raise LookupError(f"no CV version v{args.version}")
        if args.other is not None:
            new = db.cv_version(conn, args.other)
            if new is None:
                raise LookupError(f"no CV version v{args.other}")
            new_text, new_name = cvfile.read_version(private_dir, new), f"v{new['id']}"
        else:
            new_text, new_name = cvfile.read_cv(private_dir) or "", "cv.md"
        print(cvfile.unified(cvfile.read_version(private_dir, old), new_text, f"v{old['id']}", new_name)
              or "No differences.")
    return 0


def _cv_state(conn: sqlite3.Connection, private_dir: Path) -> str:
    versions, current = cvfile.history(conn, private_dir)
    if current is not None:
        return f"saved as v{current}"
    if versions:
        return "changed since the last saved version: the next save keeps this text as a version first"
    return "no saved versions yet: the first save keeps this text as v1"


def _print_cv_history(conn: sqlite3.Connection, private_dir: Path, limit: int | None = None) -> None:
    versions, current = cvfile.history(conn, private_dir)
    shown = versions[:limit] if limit else versions
    print(f"Saved versions of cv.md ({len(versions)}, newest first; {cvfile.history_dir(private_dir)}):")
    for v in shown:
        mark = " · CURRENT" if v["id"] == current else ""
        print(f"  v{v['id']} {v['created_at'][:16].replace('T', ' ')} UTC {v['file_name']} — {v['note']}{mark}")
    if not versions:
        print("  none")
    elif len(shown) < len(versions):
        print(f"  … and {len(versions) - len(shown)} older (python -m prep.cli cv history)")


def _cv_saved(conn: sqlite3.Connection, args: argparse.Namespace, result: dict, verb: str) -> None:
    """Print what a save or restore did; with --to, post the note as the reply to that message."""
    version = db.cv_version(conn, result["version"])
    log.info("master cv v%s written", version["id"])  # never the text or the note
    before = f"; the text it replaced is v{result['previous']} (undo: cv restore {result['previous']})" \
        if result["previous"] else ""
    print(f"{verb} v{version['id']} ({version['file_name']}){before}.")
    if args.to is not None:
        reply = args.reply or f"Done (v{version['id']}): {version['note']}"
        reply_id = db.reply_message(conn, None, args.to, reply)
        print(f"Reply {reply_id} saved; message {args.to} answered. The dashboard's My CV panel and Agent box show it.")


def _cv_sent(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    version = db.record_cv_sent(conn, args.app_id, args.name)
    log.info("cv sent recorded for application %s (v%s)", args.app_id, version["version"])
    print(f"Recorded: {Path(version['file_path']).name} (v{version['version']}) was sent for "
          f"{_app_line(db.get_application(conn, args.app_id))}")
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
    elif args.task_cmd == "edit":
        if args.title is None and args.due is None and not args.no_due:
            raise SystemExit("error: nothing to change; give --title, --due or --no-due")
        db.edit_task(conn, args.task_id, args.title, args.due, args.no_due)
        print(f"Changed: {_task_line(_get_task(conn, args.task_id))}")
    elif args.task_cmd == "delete":
        task = db.delete_task(conn, args.task_id)
        print(f"Deleted: {_task_line(task)}")
    return 0


def _score(value: str) -> tuple[str, int]:
    """AREA=N from --score, N a whole number from 1 to 5."""
    area, sep, score = value.rpartition("=")
    if not sep or not area.strip() or not score.strip().isdigit() or not 1 <= int(score) <= 5:
        raise argparse.ArgumentTypeError(f"not a score: {value!r} (use AREA=N, N from 1 to 5)")
    return area.strip(), int(score)


def _session_file(settings: Settings, value: str) -> str:
    """A session file must be under private_dir: transcripts and notes never go into the repo."""
    private = _private_dir(settings).resolve()
    path = Path(value).expanduser()
    path = (path if path.is_absolute() else private / "sessions" / path).resolve()
    if not path.is_relative_to(private):
        raise ValueError(f"the session file must be under private_dir ({private}); got {path}")
    return str(path)


def _print_drills(conn: sqlite3.Connection, found: dict) -> None:
    print(f"  gaps: {len(found['gaps'])} ({len(found['new_gaps'])} new)")
    for task_id in found["tasks"]:
        print(f"  added {_task_line(_get_task(conn, task_id))}")


def cmd_session(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    if args.session_cmd == "add":
        file_path = _session_file(settings, args.file) if args.file else None
        found = db.record_session(conn, args.kind, args.date, args.app, args.stage, dict(args.score),
                                  file_path, args.gap, args.due)
        log.info("session %s recorded, %s new gaps", found["session_id"], len(found["new_gaps"]))  # never the gaps
        print(f"Recorded session {_session_line(db.get_session(conn, found['session_id']))}")
        _print_drills(conn, found)
    elif args.session_cmd == "list":
        print("\n".join(_session_line(s) for s in db.list_sessions(conn)) or "no sessions")
    return 0


def _debrief_file(settings: Settings, conn: sqlite3.Connection, args: argparse.Namespace) -> Path:
    """A new file under private_dir/sessions/ for the transcript: DATE-COMPANY-STAGE-debrief.md, with
    -2, -3, ... when the name is taken, so nothing is overwritten."""
    app = db.get_application(conn, args.app) if args.app is not None else None
    who = db.company_slug(app["company"]) if app else args.kind
    stage = args.stage or (app["status"] if app and app["status"] in db.INTERVIEW_STAGES else args.kind)
    stem = f"{args.date or db.today_iso()}-{who}-{stage.replace('_', '-')}-debrief"
    folder = Path(_session_file(settings, "x")).parent
    path, n = folder / f"{stem}.md", 1
    while path.exists():
        n += 1
        path = folder / f"{stem}-{n}.md"
    return path


def cmd_debrief(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    path = None
    if args.transcript:
        text = sys.stdin.read()
        if not text.strip():
            raise ValueError("--transcript: nothing on stdin")
        path = _debrief_file(settings, conn, args)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as f:
            f.write(text)
    try:
        found = db.record_debrief(conn, args.kind, args.app, args.date, args.stage, args.questions, args.went_well,
                                  args.to_improve, dict(args.score), str(path) if path else None, args.gap, args.due)
    except Exception:
        if path:
            path.unlink(missing_ok=True)  # nothing recorded, so no orphan transcript
        raise
    log.info("debrief: session %s, %s new gaps", found["session_id"], len(found["new_gaps"]))  # never the text
    if found["interview_id"]:
        print(f"Interview #{found['interview_id']} notes saved")
    print(f"Recorded session {_session_line(db.get_session(conn, found['session_id']))}")
    if path:
        print(f"  transcript: {path}")
    _print_drills(conn, found)
    if found["debrief_tasks"]:
        print(f"  done: {db.DEBRIEF_TASK}")
    return 0


def cmd_mock(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    """mock path: the transcript file for a new mock, the next free name under private_dir/sessions/
    (the folder is created, the file is not). The /mock skill writes the mock's questions and answers there,
    then records it with `session add --file`."""
    if db.get_application(conn, args.app_id) is None:
        raise LookupError(f"no application with id {args.app_id}")
    path = mock_file(_private_dir(settings), args.app_id, args.stage, args.date or db.today_iso())
    path.parent.mkdir(parents=True, exist_ok=True)
    print(path)
    return 0


def mock_file(private_dir: Path, app_id: int, stage: str, day: str) -> Path:
    """private_dir/sessions/mock-<app>-<stage>-<date>.md, or -2, -3 … when that name is taken."""
    if stage not in db.INTERVIEW_STAGES:
        raise ValueError(f"stage must be one of {', '.join(db.INTERVIEW_STAGES)}; got {stage!r}")
    folder = private_dir / "sessions"
    stem = f"mock-{int(app_id)}-{stage}-{day}"
    n = 1
    while (folder / (stem + (f"-{n}" if n > 1 else "") + ".md")).exists():
        n += 1
    return folder / (stem + (f"-{n}" if n > 1 else "") + ".md")


def _session_line(s: dict) -> str:
    app = f" — #{s['application_id']}" + (f" {s['company']}" if s.get("company") else "") if s["application_id"] else ""
    stage = f" {db.status_label(s['stage'])}" if s["stage"] else ""
    scores = ", ".join(f"{area} {n}/5" for area, n in s["scores"].items())
    return f"#{s['id']} {s['date']} {s['kind']}{stage}{app}" + (f" ({scores})" if scores else "")


def cmd_gap(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    if args.gap_cmd == "add":
        found = db.add_gap_drills(conn, args.gaps, args.session, args.app, args.due)
        log.info("%s gaps named, %s new", len(found["gaps"]), len(found["new_gaps"]))
        print(f"Gaps: {', '.join(f'#{g}' for g in found['gaps']) or 'none'}")
        _print_drills(conn, found)
    elif args.gap_cmd == "list":
        status = None if args.all else ("closed" if args.closed else "open")
        print("\n".join(_gap_line(g) for g in db.list_gaps(conn, status)) or "no gaps")
    elif args.gap_cmd == "close":
        tasks = db.close_gap(conn, args.gap_id)
        print(f"Closed gap #{args.gap_id}; {len(tasks)} drill task{'s' if len(tasks) != 1 else ''} done")
    return 0


def _gap_line(g: dict) -> str:
    where = f" ({g['session']})" if g["session"] else ""
    links = "".join(f" {label}#{g[col]}" for label, col in (("app ", "application_id"), ("session ", "session_id"))
                    if g[col] is not None)
    closed = " (closed)" if g["status"] == "closed" else ""
    return f"#{g['id']} {g['date']} {g['gap']}{where}{links} — {g['drill_open']} open drill{closed}"


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


def _thread_line(messages: list[dict]) -> str:
    """One line: thread messages waiting for the agent, with application ids only (never text): a
    message about a job offer not yet tracked counts as "job", one about the whole search as
    "general"."""
    if not messages:
        return ""
    ids = {m["application_id"] for m in messages if m["application_id"] is not None}
    jobs = any(m["application_id"] is None and m.get("getjo_job_id") for m in messages)
    cv = any(m["application_id"] is None and not m.get("getjo_job_id") and m.get("topic") == "cv" for m in messages)
    compare = any(m["application_id"] is None and m.get("topic") == db.COMPARE for m in messages)
    general = any(m["application_id"] is None and not m.get("getjo_job_id") and not m.get("topic") for m in messages)
    apps = ", ".join([str(a) for a in sorted(ids)] + (["job offers"] if jobs else []) + (["my CV"] if cv else [])
                     + (["comparisons"] if compare else []) + (["general"] if general else []))
    claimed = sum(1 for m in messages if m.get("claimed"))
    busy = f"; {claimed} already being answered by another session" if claimed else ""
    noun = "message" if len(messages) == 1 else "messages"
    return f"Agent thread: {len(messages)} {noun} waiting (applications {apps}){busy}. Run /ask to answer."


def _brief_line(requests: list[dict]) -> str:
    """One line: job briefs waiting for the agent (a count only)."""
    if not requests:
        return ""
    claimed = sum(1 for r in requests if r.get("claimed"))
    busy = f"; {claimed} already being written by another session" if claimed else ""
    noun = "brief" if len(requests) == 1 else "briefs"
    return f"Job briefs: {len(requests)} {noun} waiting{busy}. Run /research briefs to write them."


def _messages_query(conn: sqlite3.Connection) -> str:
    """Waiting user messages: agent_messages, or the older per-application table in a database no
    newer version has opened yet (queue never migrates: it is read-only)."""
    found = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'agent_messages'").fetchone()
    table = "agent_messages" if found else "application_messages"
    columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    job = "getjo_job_id" if "getjo_job_id" in columns else "NULL"  # added later: an older database has none
    topic = "topic" if "topic" in columns else "NULL"
    return (f"SELECT application_id, 'message', status, claimed_at, {job}, {topic} FROM {table} "
            "WHERE author = 'user' AND status IN ('pending', 'claimed') ORDER BY id")


def cmd_queue(args: argparse.Namespace, settings: Settings) -> int:
    """Read-only (the SessionStart hook runs it in every session): prep.db is opened with
    mode=ro and nothing is created; a missing database or table prints nothing. Each table is
    read on its own, so an older database without the thread still reports research."""
    path = settings.prep_db
    if not path.is_file():
        return 0
    cutoff = db._claim_cutoff()
    lines = []
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            for query, line in (
                ("SELECT application_id, kind, status, claimed_at FROM agent_requests "
                 "WHERE status IN ('pending', 'claimed') ORDER BY id", _queue_line),
                (_messages_query(conn), _thread_line),
                ("SELECT NULL, 'brief', status, claimed_at FROM brief_requests "
                 "WHERE status IN ('pending', 'claimed') ORDER BY requested_at", _brief_line),
            ):
                try:
                    rows = conn.execute(query).fetchall()
                except sqlite3.Error:
                    continue
                found = [{"application_id": r[0], "kind": r[1], "claimed": r[2] == "claimed" and (r[3] or "") >= cutoff,
                          "getjo_job_id": r[4] if len(r) > 4 else None, "topic": r[5] if len(r) > 5 else None}
                         for r in rows if r[1] in (*db.REQUEST_KINDS, "message", "brief")]
                lines.append(line(found))
    except sqlite3.Error:
        return 0
    text = " ".join(line for line in lines if line)
    if text:
        print("inter_prep: " + text + " Offer it to the user, or run it at once if this is the "
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
        kinds = [args.kind] if args.kind else list(db.RESEARCH_KINDS)  # tailoring only when asked for
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
    requests = db.open_requests(conn, args.app_id)
    tailoring = {r["application_id"] for r in requests if r["kind"] == "tailor"}
    # The AI check waits until the new version is saved (and never blocks other applications' work).
    pending = [r for r in requests if not r["claimed"] and not (r["kind"] == "cv_eval" and r["application_id"] in tailoring)]
    if not pending:
        others = [r for r in requests if r["claimed"] or r["kind"] == "cv_eval"]
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
    for r in sorted(claimed, key=lambda r: db.REQUEST_KINDS.index(r["kind"])):  # the fit before tailoring
        if r["kind"] == "research":
            _print_research_step(conn, settings, private_dir, app_id, slug, bool(r["force"]))
            if app.get("getjo_job_id") and (r["force"] or not db.job_briefs(conn, {app["getjo_job_id"]})):
                _print_brief_step(settings, app["getjo_job_id"], f"#{app_id} (from the job description)")
                if not any(c["kind"] in ("cv_fit", "tailor") for c in claimed):  # those steps print it
                    _print_job_description(settings, app)
        elif r["kind"] == "cv_fit":
            _print_fit_step(settings, private_dir, app, slug)
        elif r["kind"] == "tailor":
            _print_tailor_step(conn, settings, private_dir, app, slug,
                               job=not any(c["kind"] == "cv_fit" for c in claimed))  # the fit step printed it
        elif r["kind"] == "cv_eval":
            _print_eval_step(conn, settings, private_dir, app,
                             job=not any(c["kind"] in ("cv_fit", "tailor") for c in claimed))
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
    _print_job_description(settings, app)


def _print_tailor_step(conn: sqlite3.Connection, settings: Settings, private_dir: Path, app: dict,
                       slug: str, job: bool = True) -> None:
    cv = private_dir / research.CV_FILE
    fit = db.cv_fit(conn, app["id"])
    current = db.tailored_cv(conn, app["id"])
    print("\nTailored CV (a copy for this job only; never edit cv.md):")
    if current:
        print(f"  Redo requested: write a new version from the current cv.md (latest is version {current['version']}, "
              f"{Path(current['file_path']).name}; it is kept)")
    print(f"  CV: {cv} ({'exists' if cv.is_file() else 'MISSING: ask the user for cv.md before tailoring'})")
    if fit:
        print(f"  CV fit: {fit['file_path']} (score {fit['score']}/5; use its gaps and advice)")
    if app.get("role_short"):
        print(f"  file: run python -m prep.cli tailor path {app['id']} (role short name {app['role_short']}); "
              "it prints the new version's file")
    else:
        print(f"  file: pick a short role name (e.g. FDE, AIEng, PlatEng), then run python -m prep.cli tailor path "
              f"{app['id']} --role-short X; it prints the file (CV_<Company>_<RoleShort>_<DDMMYY>_<n>.md)")
    style = research.style_file(private_dir)
    print(f"  agreed wording: {style} ({'exists: reuse it' if style.is_file() else 'missing'})")
    _print_facts(settings, "header from its ## Contact section, cv.md's header for empty fields")
    print("  rules: only rephrase, reorder and cut what cv.md says; never invent experience. Anything the")
    print("    job needs that cv.md does not answer becomes a question in the note. No pay.")
    print(f"  then: python -m prep.cli tailor save {app['id']} with the thread note on stdin "
          "(what was tailored, then the questions)")
    if job:
        _print_job_description(settings, app)


def _print_eval_step(conn: sqlite3.Connection, settings: Settings, private_dir: Path, app: dict,
                     job: bool = True) -> None:
    """The AI check of the latest tailored version: only the files it needs, never the tailor note or
    the thread, so the check is a separate pass from the one that wrote the CV."""
    current = db.tailored_cv(conn, app["id"])
    print("\nAI check of the tailored CV (a separate pass: run it in a fresh subagent given only this part):")
    if current is None:
        print("  no tailored CV yet: nothing to check")
        return
    version = Path(current["file_path"])
    facts = config.facts_path(settings)
    fit = db.cv_fit(conn, app["id"])
    print(f"  version: {current['version']} ({version})")
    print(f"  CV: {private_dir / research.CV_FILE}")
    print(f"  facts bank: {facts} ({'exists' if facts and facts.is_file() else 'missing'})")
    style = research.style_file(private_dir)
    print(f"  agreed wording: {style} ({'exists' if style.is_file() else 'missing'})")
    print(f"  master CV fit: {fit['score']}/5 ({fit['file_path']})" if fit else "  master CV fit: none yet")
    print(f"  file: {cveval.eval_file(version)}")
    print("  rubric (the AI check section of the /research skill): grounding of every changed or added claim,")
    print("    the CV fit rubric on this version, and the six-second skim of the top third of page one.")
    print(f"  then: python -m prep.cli tailor eval {app['id']}")
    if job:
        _print_job_description(settings, app)


def _print_facts(settings: Settings, use: str) -> None:
    facts = config.facts_path(settings)
    print(f"  facts bank: {facts} ({'exists: ' + use if facts and facts.is_file() else 'missing'})")


def _print_job_description(settings: Settings, app: dict) -> None:
    """The job description from getjo (read-only), indented, or why there is none."""
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
    mark = _last_task_id(conn)
    app_ids = db.save_company_profile(conn, slug, profile)
    if profile["suggested_tier"]:  # whoever researches sets the tier on this application (per role, not per company)
        db.update_application(conn, app["id"], tier=profile["suggested_tier"])
    log.info("company profile %s saved", slug)
    tier = f", tier {profile['suggested_tier']} set on #{app['id']}" if profile["suggested_tier"] else ""
    print(f"Saved company profile {slug} ({profile['researched_at']}{tier}); "
          f"research done for applications {', '.join(map(str, app_ids))}")
    _print_review_tasks(conn, mark)
    return 0


def _last_task_id(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM tasks").fetchone()[0]


def _print_review_tasks(conn: sqlite3.Connection, after_id: int) -> None:
    """Review tasks a save just added (both results are in for an Interested application)."""
    rows = conn.execute("SELECT * FROM tasks WHERE id > ? AND title = ? ORDER BY id",
                        (after_id, db.REVIEW_TASK[0]))
    for t in rows:
        print(f"  both results in for #{t['application_id']}: added {_task_line(dict(t))}")


BRIEF_FORMAT = """\
  format (Markdown, at most {words} words, plain and precise; no pay, salary, bonus or currency at all):
    <key facts: one line, e.g. hybrid 2 days · Python · LLM APIs · Senior · London>
    ## Summary
    <one line: what the role is>
    ## Must-haves
    - <requirement> (the posting's words, shortest form)
    ## Nice-to-haves
    - ...
    ## Where & terms
    - location, office days or remote, seniority, contract (permanent / fixed-term / contractor)
    ## Team & product
    - ...
    ## Interview process
    - only if the posting states it; else leave the section out
    ## Red flags / notable
    - anything unusual (vague role, huge scope, on-call, travel, visa); leave out if none"""


def _print_brief_step(settings: Settings, job_id: str, label: str) -> None:
    print(f"\nJob brief for {label}:")
    print(BRIEF_FORMAT.format(words=db.MAX_BRIEF_WORDS))
    print(f"  then: python -m prep.cli brief save --job {job_id} <<'BRIEF' … BRIEF")


def cmd_brief(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    """Job briefs: `next` claims the oldest waiting one and prints the packet; `save` stores the text
    from stdin (or --file); `show` prints it; `queue` asks for one (--redo replaces an existing one)."""
    if args.brief_cmd == "next":
        return _brief_next(args, settings, conn)
    job_id = _brief_job(args, conn)
    if args.brief_cmd == "save":
        text = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
        db.save_brief(conn, job_id, text)
        log.info("job brief saved")  # never the text
        print(f"Saved the brief of job {job_id}; the panel shows it.")
        return 0
    if args.brief_cmd == "queue":
        created = db.queue_brief(conn, job_id, force=args.redo)
        print(f"Job brief for {job_id}: {'queued' if created else 'already queued'}")
        return 0
    found = db.job_briefs(conn, {job_id}).get(job_id)
    print(found["brief"] if found else f"No brief for job {job_id} yet.")
    return 0


def _brief_job(args: argparse.Namespace, conn: sqlite3.Connection) -> str:
    """The getjo job id from --job or an application (which must be linked to a getjo job)."""
    if (args.job is None) == (args.app_id is None):
        raise ValueError("give an application id or --job, one of them")
    if args.job:
        return args.job
    app = db.get_application(conn, args.app_id)
    if app is None:
        raise LookupError(f"no application with id {args.app_id}")
    if not app["getjo_job_id"]:
        raise ValueError(f"application {args.app_id} is not linked to a getjo job, so it has no brief")
    return app["getjo_job_id"]


def _brief_next(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    """Claim the oldest waiting brief and print the job, its description and the format."""
    waiting = [r for r in db.open_briefs(conn) if not r["claimed"]]
    job_id = next((r["getjo_job_id"] for r in waiting if db.claim_brief(conn, r["getjo_job_id"])), None)
    if job_id is None:
        busy = any(r["claimed"] for r in db.open_briefs(conn))
        print("Nothing pending" + (" (some briefs are being written by another session)" if busy else "") + ".")
        return 0
    try:
        with closing(getjo.connect_ro(settings.getjo_db)) as gconn:
            job = getjo.get_job(gconn, job_id)
    except (getjo.GetjoUnavailable, sqlite3.Error):
        job = None
    shown = job or db.job_decisions(conn).get(job_id) or {"company": "?", "title": "?", "location": None, "url": None}
    tracked = db.find_by_job(conn, job_id)
    print(f"Job {job_id}: {shown['company']} — {shown['title']}"
          + (f" (application #{tracked['id']})" if tracked else " (new offer, not tracked)"))
    print(f"  location: {shown['location'] or '–'}" + (f" · link: {shown['url']}" if shown["url"] else ""))
    if db.job_briefs(conn, {job_id}):
        print("  Redo requested: write the brief again from the description")
    _print_brief_step(settings, job_id, "this job")
    _print_job_description(settings, {"getjo_job_id": job_id})
    left = len([r for r in db.open_briefs(conn) if not r["claimed"]])
    print(f"\nBriefs still waiting after this one: {left}")
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
    mark = _last_task_id(conn)
    new_gaps = db.save_cv_fit(conn, args.app_id, fit["score"], fit["gaps"], str(path), fit["checked_at"])
    log.info("cv fit saved for application %s", args.app_id)  # never the gaps or the CV
    print(f"Saved CV fit for #{args.app_id}: {fit['score']}/5, {len(fit['gaps'])} gaps "
          f"({len(new_gaps)} new in the gaps list)")
    _print_review_tasks(conn, mark)
    return 0


def _next_tailored_file(settings: Settings, conn: sqlite3.Connection, app: dict) -> Path:
    """Where the next version of an application's tailored CV goes (today's date, version + 1)."""
    if not app.get("role_short"):
        raise SystemExit(f"error: no role short name yet: run python -m prep.cli tailor path {app['id']} "
                         "--role-short X (e.g. FDE, AIEng) first")
    current = db.tailored_cv(conn, app["id"])
    return research.tailored_file(_private_dir(settings), app["company"], app["role_short"], db.today_iso(),
                                  (current["version"] if current else 0) + 1)


def cmd_tailor(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    """tailor path: the next version's file (storing the role short name the first time; --copy starts
    it from the latest version, for a revision). tailor save: record that file as a new version and
    post the note from stdin in the thread; with --to it answers that user message."""
    app = db.get_application(conn, args.app_id)
    if app is None:
        raise LookupError(f"no application with id {args.app_id}")
    if args.tailor_cmd == "path":
        return _tailor_path(args, settings, conn, app)
    if args.tailor_cmd == "pdf":
        return _tailor_pdf(args, settings, conn, app)
    if args.tailor_cmd == "check":
        return _tailor_check(args, settings, conn, app)
    if args.tailor_cmd == "eval":
        return _tailor_eval(args, settings, conn, app)
    path = Path(args.file).expanduser() if args.file else _next_tailored_file(settings, conn, app)
    if not path.is_file():
        raise SystemExit(f"error: no file at {path}")
    _, body = research.split_front_matter(path.read_text(encoding="utf-8"))  # the same +++ block as the other files
    note = sys.stdin.read()
    mark = db.last_request_id(conn)
    version = db.save_tailored_cv(conn, args.app_id, str(path), note, args.to)
    log.info("tailored cv v%s saved for application %s", version, args.app_id)  # never the CV or the note
    print(f"Saved tailored CV v{version} ({path.name}) for #{args.app_id}; the note is in the thread"
          + (f" as the reply to message {args.to}." if args.to else "."))
    words = research.word_count(body)
    print(f"  {words} words" + (f": WARNING, over {research.TAILOR_MAX_WORDS:,} (about two pages); cut older "
                                "or unrelated roles to one line" if words > research.TAILOR_MAX_WORDS else ""))
    pages = _print_pdf(settings, path, required=False)
    _print_check(settings, conn, args.app_id, version, pages)
    _notify_new(settings, conn, mark)  # the AI check queued by the save
    return 0


def _print_check(settings: Settings, conn: sqlite3.Connection, app_id: int, version: int, pages: int | None) -> None:
    """Run the rule checks on a saved version (cvcheck), store them and print the verdict, the flagged
    lines and the changes against cv.md. A failed check never undoes the save."""
    try:
        found = cvcheck.run(conn, settings.private_dir, app_id, version, pages, config.facts_path(settings))
    except Exception as e:  # the version is saved; the check can be redone with tailor pdf
        log.warning("tailored cv check failed: %s", type(e).__name__)
        print(f"  no check: {type(e).__name__}; retry with tailor check")
        return
    log.info("tailored cv v%s checked: %s, %s findings", version, found["verdict"], len(found["findings"]))  # never the CV
    _print_found(conn, app_id, version, found)
    for line in found["changes"]:
        print(f"    - {line}")


def _print_found(conn: sqlite3.Connection, app_id: int, version: int, found: dict) -> None:
    """The verdict, whether the AI check is in (with its scores), and the flagged lines."""
    latest = db.tailored_cv(conn, app_id)
    state = db.eval_state(conn, app_id, found) if latest and latest["version"] == version else (
        "done" if found.get("summary") else "none")
    ai = {"done": "AI check: " + cveval.summary_line(found),
          "pending": f"AI check pending: run python -m prep.cli research next {app_id} in a separate pass",
          "none": "no AI check"}[state]
    print(f"  Check: {cvcheck.LABELS[found['verdict']]} ({ai})")
    for f in found["findings"]:
        print(f"    [{f['severity']}] {f['message']}" + (f": {f['text']}" if f["text"] else ""))


def _print_pdf(settings: Settings, path: Path, required: bool) -> int | None:
    """Make the PDF next to a tailored file and print its path and page count. Without WeasyPrint it
    says so; `tailor save` (not required) still succeeds, `tailor pdf` fails."""
    try:
        pdf, pages = cvpdf.write_pdf(path, settings.pdf_python)
    except cvpdf.PdfUnavailable as e:
        if required:
            raise SystemExit(f"error: no PDF: {e}") from None
        print(f"  no PDF: {e}; make it later with tailor pdf")
        return None
    except Exception as e:  # the version is saved already; a failed render must not look like a failed save
        if required:
            raise
        log.warning("tailored cv pdf failed: %s", type(e).__name__)
        print(f"  no PDF: the render failed ({type(e).__name__}); retry with tailor pdf")
        return None
    log.info("tailored cv pdf made (%s pages)", pages)  # never the CV
    print(f"  PDF: {pdf} ({cvpdf.page_note(pages)})")
    return pages


def _chosen_version(conn: sqlite3.Connection, app: dict, version: int | None) -> tuple[dict, Path]:
    """The version asked for (default the latest) and its file, which must exist."""
    versions = db.tailored_versions(conn, app["id"])
    if not versions:
        raise SystemExit(f"error: #{app['id']} has no tailored CV yet")
    chosen = [v for v in versions if version in (None, v["version"])]
    if not chosen:
        raise SystemExit(f"error: #{app['id']} has no tailored CV v{version} "
                         f"(versions 1 to {versions[-1]['version']})")
    path = Path(chosen[-1]["file_path"])
    if not path.is_file():
        raise SystemExit(f"error: no file at {path}")
    return chosen[-1], path


def _tailor_check(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection, app: dict) -> int:
    """Check a version again without WeasyPrint: the page count comes from its existing PDF."""
    version, path = _chosen_version(conn, app, args.version)
    pages = cvpdf.page_count(cvpdf.pdf_path(path))
    print(f"Tailored CV v{version['version']} ({path.name}) for #{app['id']}: "
          + (f"PDF {cvpdf.page_note(pages)}" if pages else "no PDF"))
    _print_check(settings, conn, app["id"], version["version"], pages)
    return 0


def _tailor_eval(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection, app: dict) -> int:
    """Store the AI check of a version (default the latest) from its .eval.md file (or --file)."""
    version, path = _chosen_version(conn, app, args.version)
    file = Path(args.file).expanduser() if args.file else cveval.eval_file(path)
    if not file.is_file():
        raise SystemExit(f"error: no file at {file}")
    found = cveval.save(conn, app["id"], version["version"], file)
    log.info("tailored cv v%s ai check saved: %s", version["version"], found["verdict"])  # never the CV or the findings
    print(f"Saved the AI check of tailored CV v{version['version']} ({path.name}) for #{app['id']}")
    _print_found(conn, app["id"], version["version"], found)
    return 0


def _tailor_pdf(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection, app: dict) -> int:
    version, path = _chosen_version(conn, app, args.version)
    print(f"Tailored CV v{version['version']} ({path.name}) for #{app['id']}")
    pages = _print_pdf(settings, path, required=True)
    _print_check(settings, conn, app["id"], version["version"], pages)
    return 0


def _tailor_path(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection, app: dict) -> int:
    if args.role_short:
        word = research.role_short_word(args.role_short)
        if app.get("role_short") and app["role_short"] != word:
            raise SystemExit(f"error: this application's role short name is {app['role_short']}; it stays "
                             "the same for every version")
        if not app.get("role_short"):
            db.update_application(conn, app["id"], role_short=word)
            app = db.get_application(conn, app["id"])
    path = _next_tailored_file(settings, conn, app)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = db.tailored_cv(conn, app["id"])
    if args.copy:
        if current is None:
            raise SystemExit("error: no tailored CV yet to copy; write the first version from cv.md")
        if path.exists():
            raise SystemExit(f"error: {path} already exists")
        shutil.copyfile(current["file_path"], path)
    print(path)
    print(f"  version {(current['version'] if current else 0) + 1}"
          + (f", copied from v{current['version']} ({Path(current['file_path']).name}): edit it" if args.copy else "")
          + f"; then: python -m prep.cli tailor save {app['id']} [--to MSG] with the note on stdin")
    return 0


def cmd_thread(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    if args.thread_cmd == "pending":
        waiting = db.pending_messages(conn)
        for m in waiting:
            busy = ", being answered by another session" if m["claimed"] else ""
            if m["application_id"] is not None:
                about = f"on #{m['application_id']} {m['company']} — {m['role']}"
            elif m["getjo_job_id"]:
                about = f"about job offer {m['getjo_job_id']} (/ask job {m['id']})"
            elif m["topic"] == "cv":
                about = f"about the master CV (/ask cv {m['id']})"
            elif m["topic"] == db.COMPARE:
                about = f"comparing several jobs (/ask compare {m['id']})"
            else:
                about = "about the whole search (/ask general)"
            print(f"message {m['id']} {about} (sent {m['created_at'][:16].replace('T', ' ')} UTC{busy})")
        if not waiting:
            print("No thread messages waiting.")
        return 0
    if args.thread_cmd == "reply":
        return _thread_reply(args, conn)
    if args.message is not None:  # /ask job MSG: whatever the message is about
        if args.app_id is not None or args.job or args.cv:
            raise ValueError("give an application, --job, --cv or --message, not more than one")
        if db.message_topic(conn, args.message) == db.COMPARE:
            return _compare_show(args, settings, conn)
        args.app_id, job_id = db.message_subject(conn, args.message)
        if args.app_id is None and job_id:
            args.job = job_id
        args.cv = args.app_id is None and not job_id and db.message_topic(conn, args.message) == "cv"
    if args.cv:
        if args.app_id is not None or args.job:
            raise ValueError("give an application, --job or --cv, not more than one")
        return _cv_show(args, settings, conn)
    if args.job:
        if args.app_id is not None:
            raise ValueError("give an application or --job, not both")
        tracked = db.find_by_job(conn, args.job)
        if tracked is None:
            return _job_show(args, settings, conn)
        args.app_id = tracked["id"]  # tracked since: its application holds the messages
    if args.app_id is None:
        return _general_show(args, conn)
    app = db.get_application(conn, args.app_id)
    if app is None:
        raise LookupError(f"no application with id {args.app_id}")
    return _thread_show(args, settings, conn, app)


def _thread_show(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection, app: dict) -> int:
    """The application (never pay), the files and commands /ask may need, and the thread."""
    claimed = db.claim_messages(conn, app["id"]) if args.claim else []
    slug = db.company_slug(app["company"])
    print(f"Application #{app['id']}: {app['company']} — {app['role']} [{db.status_label(app['status'])}]")
    if app["link"]:
        print(f"  link: {app['link']}")
    if app["contact"]:
        print(f"  contact: {app['contact']}")

    print("\nContext (read what the message needs):")
    private_dir = settings.private_dir
    profile, fit = db.company_profile(conn, slug), db.cv_fit(conn, app["id"])
    if private_dir:
        company = Path(profile["file_path"]) if profile else research.company_file(private_dir, slug)
        note = f", researched {profile['researched_at']}, research tier {profile['suggested_tier'] or '–'}" if profile else ""
        print(f"  company research: {company} ({'exists' if company.is_file() else 'missing'}{note})")
        print(f"    after updating it: python -m prep.cli research save {app['id']}")
        fit_path = Path(fit["file_path"]) if fit else research.fit_file(private_dir, app["id"], slug)
        note = f", score {fit['score']}/5" if fit else ""
        print(f"  CV fit: {fit_path} ({'exists' if fit_path.is_file() else 'missing'}{note})")
        cv = private_dir / research.CV_FILE
        print(f"  CV: {cv} ({'exists' if cv.is_file() else 'missing'}; the master CV: never edit it here)")
        versions = db.tailored_versions(conn, app["id"])
        if versions:
            print("  tailored CV versions (each its own file, all kept; the latest last):")
            for v in versions:
                path = Path(v["file_path"])
                sent = " · SENT" if app.get("cv_sent_version") == v["version"] else ""
                print(f"    v{v['version']} {path} ({'exists' if path.is_file() else 'missing'}){sent}")
            print(f"    to revise: python -m prep.cli tailor path {app['id']} --copy (new version file), edit it, then "
                  f"python -m prep.cli tailor save {app['id']} --to MSG with what changed on stdin (this is the reply)")
            print(f"    the user sent one: python -m prep.cli cv sent {app['id']} FILE_NAME")
            style = research.style_file(private_dir)
            print(f"  agreed wording: {style} ({'exists' if style.is_file() else 'missing'})")
            _print_facts(settings, "header from its ## Contact section; add facts the user confirms")
    else:
        print("  private_dir is not set: no research, CV fit or CV files")
    _print_brief(conn, app.get("getjo_job_id"))
    _print_job_description(settings, app)
    print(f"\nStage: {db.status_label(app['status'])} · tier {app['tier'] or '–'}")
    tasks = db.app_tasks(conn).get(app["id"], [])
    print(f"Open tasks ({len(tasks)}):")
    print("\n".join(f"  {_task_line(t)}" for t in tasks) or "  none")
    found = db.comments(conn, app["id"])
    print(f"Comments ({len(found)}, newest first; refs for comments amend/remove):")
    print("\n".join(_comment_line(c) for c in found) or "  none")

    _print_messages(db.thread(conn, app["id"]), f"Messages about #{app['id']}", conn)
    _print_claim(args, claimed, db.pending_messages(conn, app["id"]))
    return 0


def _general_show(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    """`thread show` with no application: messages about the whole search ("which tasks need me?").
    Prints the open applications, the review order and the latest of the conversation."""
    claimed = db.claim_messages(conn, None) if args.claim else []
    print("Messages about the whole search (no application). Context:")
    tasks = db.app_tasks(conn)
    latest = db.latest_comments(conn)
    print("\nOpen applications:")
    for a in db.list_applications(conn):
        if a["status"] in db.CLOSED_STATUSES and a["status"] != "offer":
            continue
        nxt = tasks.get(a["id"], [None])[0]
        comment = f" · latest comment: {latest[a['id']][0]}" if a["id"] in latest else ""
        print(f"  #{a['id']} {a['company']} — {a['role']} [{db.status_label(a['status'])}] tier {a['tier'] or '–'}"
              + (f" · next: {nxt['title']}" if nxt else "") + comment)
    print()
    _print_review(conn)
    _print_messages(db.thread(conn, limit=GENERAL_CONTEXT), f"The conversation (latest {GENERAL_CONTEXT})", conn)
    _print_claim(args, claimed, [m for m in db.pending_messages(conn)
                                 if m["application_id"] is None and not m["getjo_job_id"] and not m["topic"]])
    return 0


def _job_show(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    """`thread show --job ID`: a getjo job not yet tracked (questions before deciding: "is this remote?",
    "how does my CV fit?"). Prints the job, its open/closed state, the company research file if one
    exists, cv.md, the job description and the job's messages. No application, no tasks."""
    claimed = db.claim_messages(conn, None, args.job) if args.claim else []
    try:
        with closing(getjo.connect_ro(settings.getjo_db)) as gconn:
            job = getjo.get_job(gconn, args.job)
    except (getjo.GetjoUnavailable, sqlite3.Error) as e:
        job, error = None, str(e)
    else:
        error = "getjo no longer has this job"
    decision = db.job_decisions(conn).get(args.job)
    if job is None and decision is None:
        raise LookupError(f"no getjo job {args.job} ({error})")
    shown = job or {"company": decision["company"], "title": decision["title"], "location": decision["location"],
                    "score": decision["score"], "url": decision["url"]}
    print(f"Job offer {args.job}: {shown['company']} — {shown['title']} (not tracked: no application yet)")
    print(f"  score {shown['score'] if shown['score'] is not None else '–'} · {shown['location'] or 'no location'}")
    if shown["url"]:
        print(f"  link: {shown['url']}")
    if job:
        print(f"  state:{_job_flags(job, None, settings.stale_days) or ' open'}")
    else:
        print(f"  state: {error}; details from the skip's stored copy")
    if decision:
        print(f"  decision: {decision['decision']}" + (f" ({decision['reason']})" if decision["reason"] else ""))
    print("\nContext (read what the message needs):")
    slug = db.company_slug(shown["company"])
    profile = db.company_profile(conn, slug)
    if settings.private_dir:
        company = Path(profile["file_path"]) if profile else research.company_file(settings.private_dir, slug)
        note = f", researched {profile['researched_at']}" if profile else ""
        print(f"  company research: {company} ({'exists' if company.is_file() else 'missing'}{note})")
        cv = settings.private_dir / research.CV_FILE
        print(f"  CV: {cv} ({'exists' if cv.is_file() else 'missing'}; the master CV: never edit it here)")
    else:
        print("  private_dir is not set: no research or CV files")
    print("  no CV fit or tailored CV: those come once the job is marked Interested")
    _print_brief(conn, args.job)
    _print_job_description(settings, {"getjo_job_id": args.job})
    _print_messages(db.thread(conn, job_id=args.job), f"Messages about job {args.job}", conn)
    _print_claim(args, claimed, db.pending_messages(conn, job_id=args.job))
    return 0


def _cv_show(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    """`thread show --cv`: a request about the master CV ("add Terraform to my skills", "copy the summary
    from CV_…"). Prints cv.md's path and state, how to change it, its skills, the saved versions, the
    tailored CV files (to copy wording from) and the CV messages."""
    claimed = db.claim_messages(conn, None, topic="cv") if args.claim else []
    private_dir = _private_dir(settings)
    cv = cvfile.cv_path(private_dir)
    text = cvfile.read_cv(private_dir)
    print("Messages about the master CV (the My CV panel). Context:")
    if text is None:
        print(f"  CV: {cv} (missing)")
    else:
        print(f"  CV: {cv} ({research.word_count(text)} words; {_cv_state(conn, private_dir)})")
    print("  to change it: python -m prep.cli cv draft (prints the draft path), edit the draft only, then")
    print("    python -m prep.cli cv save --note \"what changed, one line\" --to MSG [--reply \"…\"]")
    print("    (backs up the current text first; the reply defaults to \"Done (vN): <note>\")")
    print("  undo or go back: python -m prep.cli cv restore VERSION --to MSG; compare: cv diff VERSION")
    if text is not None:
        groups = cvfile.skills(text)
        print("  skills: " + (" | ".join(f"{name}: {', '.join(items)}" for name, items in groups) or "no skills section"))
    print()
    _print_cv_history(conn, private_dir, limit=10)
    files = db.all_tailored_versions(conn)
    print(f"\nTailored CV files ({len(files)}, newest first; read one to copy wording from it):")
    for f in files[:20]:
        path = Path(f["file_path"])
        print(f"  {path.stem} · #{f['application_id']} {f['company']} v{f['version']} · {path} "
              f"({'exists' if path.is_file() else 'missing'})")
    if not files:
        print("  none")
    _print_messages(db.thread(conn, topic="cv"), "Messages about the master CV", conn)
    _print_claim(args, claimed, db.pending_messages(conn, topic="cv"))
    return 0


def _compare_show(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    """`thread show --message MSG` for a comparison (2-4 ticked jobs, applications or My CV): each
    target's context (stage, tier, brief, file paths, CV fit score, job description), then the
    messages about any of them. --claim takes this one message only."""
    claimed = [args.message] if args.claim and db.claim_message(conn, args.message) else []
    targets = db.message_targets(conn, [args.message]).get(args.message, [])
    print(f"Message {args.message} compares {len(targets)} targets. Answer per target, then the differences, "
          "pros and cons and the fit of each. Context:")
    private_dir = settings.private_dir
    for n, t in enumerate(targets, 1):
        print(f"\n=== {n}. ", end="")
        if t["application_id"] is not None:
            app = db.get_application(conn, t["application_id"])
            print(f"Application #{app['id']}: {app['company']} — {app['role']} [{db.status_label(app['status'])}] "
                  f"tier {app['tier'] or '–'}")
            if app["link"]:
                print(f"  link: {app['link']}")
            slug = db.company_slug(app["company"])
            profile, fit = db.company_profile(conn, slug), db.cv_fit(conn, app["id"])
            if private_dir:
                company = Path(profile["file_path"]) if profile else research.company_file(private_dir, slug)
                print(f"  company research: {company} ({'exists' if company.is_file() else 'missing'})")
                fit_path = Path(fit["file_path"]) if fit else research.fit_file(private_dir, app["id"], slug)
                note = f", score {fit['score']}/5, top gaps: {'; '.join(fit['top_gaps'])}" if fit else ""
                print(f"  CV fit: {fit_path} ({'exists' if fit_path.is_file() else 'missing'}{note})")
            _print_brief(conn, app["getjo_job_id"])
            _print_job_description(settings, app)
        elif t["getjo_job_id"]:
            job_id = t["getjo_job_id"]
            try:
                with closing(getjo.connect_ro(settings.getjo_db)) as gconn:
                    job = getjo.get_job(gconn, job_id)
            except (getjo.GetjoUnavailable, sqlite3.Error):
                job = None
            shown = job or db.job_decisions(conn).get(job_id) or {"company": "?", "title": "?", "location": None,
                                                                    "score": None, "url": None}
            print(f"Job offer {job_id}: {shown['company']} — {shown['title']} (not tracked)")
            print(f"  score {shown['score'] if shown['score'] is not None else '–'} · {shown['location'] or 'no location'}"
                  + (f" · state:{_job_flags(job, None, settings.stale_days) or ' open'}" if job else ""))
            if shown["url"]:
                print(f"  link: {shown['url']}")
            if private_dir:
                company = research.company_file(private_dir, db.company_slug(shown["company"]))
                if company.is_file():
                    print(f"  company research: {company} (exists)")
            _print_brief(conn, job_id)
            _print_job_description(settings, {"getjo_job_id": job_id})
        else:
            print("My CV (the master CV)")
            if private_dir:
                cv = cvfile.cv_path(private_dir)
                text = cvfile.read_cv(private_dir)
                print(f"  CV: {cv} ({'missing' if text is None else f'{research.word_count(text)} words'}; never edit it here)")
    if private_dir:
        cv = private_dir / research.CV_FILE
        print(f"\nCV for the fit of each role: {cv} ({'exists' if cv.is_file() else 'missing'})")
    seen: dict[int, dict] = {}
    for t in targets:
        found = (db.thread(conn, t["application_id"]) if t["application_id"] is not None
                 else db.thread(conn, job_id=t["getjo_job_id"]) if t["getjo_job_id"] else db.thread(conn, topic=t["topic"]))
        seen.update({m["id"]: m for m in found})
    seen.update({m["id"]: m for m in db.thread(conn) if m["id"] == args.message})
    messages = [seen[k] for k in sorted(seen)][-GENERAL_CONTEXT:]
    _print_messages(messages, f"Messages about these targets (latest {GENERAL_CONTEXT})", conn)
    _print_claim(args, claimed, [m for m in db.pending_messages(conn) if m["id"] == args.message])
    return 0


def _print_brief(conn: sqlite3.Connection, job_id: str | None) -> None:
    """The job's brief (the agent's summary of the description), or how to write one."""
    if not job_id:
        return
    found = db.job_briefs(conn, {job_id}).get(job_id)
    if found is None:
        print(f"  job brief: none yet (write one: python -m prep.cli brief save --job {job_id}, text on stdin)")
        return
    print(f"  job brief ({found['updated_at'][:10]}):")
    print("\n".join(f"    {line}" for line in found["brief"].splitlines()))


GENERAL_CONTEXT = 30  # messages of the whole conversation shown with a general question


def _target_name(t: dict) -> str:
    return (f"#{t['application_id']} {t['company']}" if t["application_id"] is not None
            else f"job {t['getjo_job_id']}" if t["getjo_job_id"] else "my CV" if t["topic"] == "cv" else "?")


def _print_messages(messages: list[dict], title: str, conn: sqlite3.Connection | None = None) -> None:
    print(f"\n{title} ({len(messages)} message{'' if len(messages) == 1 else 's'}, oldest first):")
    compared = (db.message_targets(conn, [m["id"] for m in messages if m.get("topic") == db.COMPARE])
                if conn is not None else {})
    for m in messages:
        who = f"agent, reply to {m['reply_to']}" if m["author"] == "agent" else f"user, {m['status']}"
        about = (f"#{m['application_id']} {m['company']}" if m["application_id"] is not None
                 else f"job {m['getjo_job_id']}" if m.get("getjo_job_id")
                 else "my CV" if m.get("topic") == "cv"
                 else "comparing " + " · ".join(_target_name(t) for t in compared.get(m["id"], []))
                 if m.get("topic") == db.COMPARE else "whole search")
        print(f"\n  [message {m['id']} · {who} · {about} · {m['created_at'][:16].replace('T', ' ')} UTC]")
        print("\n".join(f"    {line}" for line in m["body"].splitlines()))
    if not messages:
        print("  none")


def _print_claim(args: argparse.Namespace, claimed: list[int], waiting: list[dict]) -> None:
    print()
    if args.claim:
        if claimed:
            print(f"Claimed for this session: message{'s' if len(claimed) > 1 else ''} {', '.join(map(str, claimed))}. "
                  "Answer each, oldest first.")
        elif waiting:
            print("Nothing claimed: another session is already answering this thread. Leave it alone.")
        else:
            print("Nothing waiting on this thread.")
    for msg_id in claimed or [m["id"] for m in waiting]:
        print(f"To answer message {msg_id}: python -m prep.cli thread reply --to {msg_id} "
              "with the text on stdin (or --file PATH)")


def _thread_reply(args: argparse.Namespace, conn: sqlite3.Connection) -> int:
    """The reply goes with the message's application (or job offer, or the whole search); an APP given
    must match."""
    app_id = db.message_application(conn, args.to)
    if args.app_id is not None and args.app_id != app_id:
        raise LookupError(f"message {args.to} is not about application {args.app_id}")
    text = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    reply_id = db.reply_message(conn, app_id, args.to, text)
    job = db.message_subject(conn, args.to)[1]
    topic = db.message_topic(conn, args.to)
    about = (f"about #{app_id}" if app_id is not None else f"about job offer {job}" if job
             else "about the master CV" if topic == "cv" else "comparing several jobs" if topic == db.COMPARE
             else "about the whole search")
    print(f"Reply {reply_id} saved {about}; message {args.to} answered. The dashboard's Agent box shows it.")
    return 0


REVIEW_TOP = 3  # "which tasks need me?" answers with the top three; --all for the rest


def cmd_review(args: argparse.Namespace, settings: Settings, conn: sqlite3.Connection) -> int:
    """Read-only: the most urgent open tasks (all with --all), in the order the user should look at
    them, and the agent's own work."""
    _print_review(conn, None if args.all else REVIEW_TOP)
    return 0


def _print_review(conn: sqlite3.Connection, limit: int | None = REVIEW_TOP) -> None:
    tasks = db.review_tasks(conn)
    shown = tasks[:limit] if limit else tasks
    print(f"Tasks that need you ({len(tasks)}), " + (f"the top {len(shown)} in order:" if len(shown) < len(tasks)
                                                      else "in order:"))
    for n, t in enumerate(shown, 1):
        due = f" · due {t['due_date']}" if t["due_date"] else ""
        interview = f" · interview {t['interview_date']}" if t["interview_date"] else ""
        app = (f" — #{t['application_id']} {t['company']}, {t['role']} [{db.status_label(t['app_status'])}]"
               if t["application_id"] is not None else " — no application")
        print(f"  {n}. [{t['group']}{due}{interview}] {t['title']}{app} · task #{t['id']}")
    if not tasks:
        print("  none")
    if len(shown) < len(tasks):
        print(f"  … and {len(tasks) - len(shown)} more (python -m prep.cli review --all)")
    work = db.open_requests(conn)
    if work:
        print("The agent is working on (not waiting on you): " + "; ".join(
            f"{db.REQUEST_LABELS[r['kind']].lower()} for #{r['application_id']} {r['company']}" for r in work))


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

    p = sub.add_parser("comments", help="list, amend or remove an application's comments")
    csub = p.add_subparsers(dest="comments_cmd", required=True)
    c = csub.add_parser("list", help="comments with their refs (c12: free comment, h34: with a stage change)")
    c.add_argument("app_id", type=int)
    c = csub.add_parser("amend", help="replace a comment's text")
    c.add_argument("ref")
    c.add_argument("text")
    c = csub.add_parser("remove", help="delete a comment (a stage change keeps its move)")
    c.add_argument("ref")
    p.set_defaults(func=cmd_comments)

    p = sub.add_parser("skip", help="skip an Interested application (closes it; New offers lists it under Show skipped)")
    p.add_argument("app_id", type=int)
    p.add_argument("--reason", required=True, choices=db.SKIP_REASONS)
    p.add_argument("--comment")
    p.set_defaults(func=cmd_skip)

    p = sub.add_parser("cv", help="the master CV (show, skills, history, draft, save, restore, diff) and which tailored CV was sent")
    vsub = p.add_subparsers(dest="cv_cmd", required=True)
    v = vsub.add_parser("sent", help="the file name of the tailored version the user sent")
    v.add_argument("app_id", type=int)
    v.add_argument("name", help="e.g. CV_Globex_FDE_210926_2 (with or without .md)")
    vsub.add_parser("show", help="print cv.md and whether it matches a saved version")
    vsub.add_parser("skills", help="the skills section, grouped by theme")
    vsub.add_parser("history", help="the saved versions, newest first")
    vsub.add_parser("draft", help="copy cv.md to the draft the agent edits; prints its path")
    v = vsub.add_parser("save", help="write the draft (or --file) to cv.md: the current text is saved as a version first")
    v.add_argument("--note", required=True, help="one line: what changed")
    v.add_argument("--file", help="the edited text (default: the draft from cv draft, removed once saved)")
    v.add_argument("--to", type=int, help="a CV message this answers (the reply is posted in the thread)")
    v.add_argument("--reply", help="the reply text with --to (default: \"Done (vN): <note>\")")
    v = vsub.add_parser("restore", help="make a saved version cv.md again (the current text is kept as a version)")
    v.add_argument("version", type=int)
    v.add_argument("--to", type=int, help="a CV message this answers")
    v.add_argument("--reply", help="the reply text with --to")
    v = vsub.add_parser("diff", help="a saved version against cv.md (or --with another version)")
    v.add_argument("version", type=int)
    v.add_argument("--with", dest="other", type=int, help="compare with this version instead of cv.md")
    p.set_defaults(func=cmd_cv)

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
    t = tsub.add_parser("edit", help="change a task's title or due date")
    t.add_argument("task_id", type=int)
    t.add_argument("--title")
    group = t.add_mutually_exclusive_group()
    group.add_argument("--due", type=parse_day)
    group.add_argument("--no-due", action="store_true", help="remove the due date")
    t = tsub.add_parser("delete", help="delete a task")
    t.add_argument("task_id", type=int)
    p.set_defaults(func=cmd_task)

    p = sub.add_parser("session", help="practice sessions: mocks and real interviews, with their gaps")
    ssub = p.add_subparsers(dest="session_cmd", required=True)
    t = ssub.add_parser("add", help="record a session; each gap named gets a drill task")
    t.add_argument("--kind", required=True, choices=db.SESSION_KINDS)
    t.add_argument("--app", type=int, help="related application id")
    t.add_argument("--stage", choices=db.INTERVIEW_STAGES)
    t.add_argument("--date", type=parse_day, help="the session's date (default today)")
    t.add_argument("--score", type=_score, action="append", default=[], metavar="AREA=N",
                   help="a 1-5 score for one area; repeat per area")
    t.add_argument("--file", help="transcript or notes, under private_dir (relative: private_dir/sessions/)")
    t.add_argument("--gap", action="append", default=[], help="a weakness found; repeat per gap")
    t.add_argument("--due", type=parse_day, help="due date of the drill tasks (default none)")
    ssub.add_parser("list", help="every session, newest first")
    p.set_defaults(func=cmd_session)

    p = sub.add_parser("debrief", help="record a real interview or a mock done elsewhere: notes, session, gaps")
    p.add_argument("--kind", required=True, choices=db.SESSION_KINDS)
    p.add_argument("--app", type=int, help="related application id (required for --kind real)")
    p.add_argument("--stage", choices=db.INTERVIEW_STAGES, help="default: the application's, if an interview stage")
    p.add_argument("--date", type=parse_day, help="the interview's date (default today)")
    p.add_argument("--questions", help="questions asked (real: stored in interviews)")
    p.add_argument("--went-well", help="what went well (real: stored in interviews)")
    p.add_argument("--to-improve", help="what to improve (real: stored in interviews)")
    p.add_argument("--score", type=_score, action="append", default=[], metavar="AREA=N",
                   help="a 1-5 score for one area; repeat per area")
    p.add_argument("--gap", action="append", default=[], help="a weakness named; repeat per gap")
    p.add_argument("--due", type=parse_day, help="due date of the drill tasks (default none)")
    p.add_argument("--transcript", action="store_true",
                   help="read the transcript or notes on stdin; saved under private_dir/sessions/")
    p.set_defaults(func=cmd_debrief)
    p = sub.add_parser("mock", help="mock interviews run in the Agent box (/mock)")
    msub = p.add_subparsers(dest="mock_cmd", required=True)
    t = msub.add_parser("path", help="print the transcript file for a new mock (under private_dir/sessions/)")
    t.add_argument("app_id", type=int)
    t.add_argument("--stage", required=True, choices=db.INTERVIEW_STAGES)
    t.add_argument("--date", type=parse_day, help="the mock's date (default today)")
    p.set_defaults(func=cmd_mock)

    p = sub.add_parser("gap", help="gaps and their drill tasks")
    gsub = p.add_subparsers(dest="gap_cmd", required=True)
    t = gsub.add_parser("add", help="add gaps (or match open ones); each gets a drill task")
    t.add_argument("gaps", nargs="+")
    t.add_argument("--session", type=int, help="the session that found them")
    t.add_argument("--app", type=int, help="related application id (default: the session's)")
    t.add_argument("--due", type=parse_day, help="due date of the drill tasks (default none)")
    t = gsub.add_parser("list")
    group = t.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true")
    group.add_argument("--closed", action="store_true")
    t = gsub.add_parser("close", help="close a gap; its open drill tasks are marked done")
    t.add_argument("gap_id", type=int)
    p.set_defaults(func=cmd_gap)

    p = sub.add_parser("feedback", help="export interested/applied/skipped labels for getjo")
    fsub = p.add_subparsers(dest="feedback_cmd", required=True)
    f = fsub.add_parser("export", help="write private_dir/feedback/job_decisions.jsonl")
    f.add_argument("--out", help="output path (default: private_dir/feedback/job_decisions.jsonl)")
    p.set_defaults(func=cmd_feedback)

    p = sub.add_parser("queue", help="read-only: open research / CV fit / tailoring requests (counts and ids)")
    p.set_defaults(func=cmd_queue, read_only=True)

    p = sub.add_parser("research", help="company research and CV fit queue (used by /research)")
    rsub = p.add_subparsers(dest="research_cmd", required=True)
    r = rsub.add_parser("next", help="claim the next application's requests and print what to do")
    r.add_argument("app_id", type=int, nargs="?")
    r = rsub.add_parser("queue", help="queue research and a CV fit (or one --kind, tailor too) for an application")
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

    p = sub.add_parser("tailor", help="tailored CV copies (used by /research and /ask)")
    tsub = p.add_subparsers(dest="tailor_cmd", required=True)
    t = tsub.add_parser("path", help="print the next version's file (CV_<Company>_<RoleShort>_<DDMMYY>_<n>.md)")
    t.add_argument("app_id", type=int)
    t.add_argument("--role-short", help="short role name for file names, set once (e.g. FDE, AIEng)")
    t.add_argument("--copy", action="store_true", help="start it as a copy of the latest version (revisions)")
    t = tsub.add_parser("save", help="record the next version's file (or --file) as a new version; the thread note comes on stdin")
    t.add_argument("app_id", type=int)
    t.add_argument("--to", type=int, help="the user message this revision answers")
    t.add_argument("--file", help="default: the path `tailor path` prints")
    t = tsub.add_parser("pdf", help="make the PDF next to a version's file (same name, .pdf) and check it again; needs WeasyPrint")
    t.add_argument("app_id", type=int)
    t.add_argument("--version", type=int, help="default: the latest version")
    t = tsub.add_parser("eval", help="store the AI check (grounding, fit, skim) of a version from its .eval.md file")
    t.add_argument("app_id", type=int)
    t.add_argument("--version", type=int, help="default: the latest")
    t.add_argument("--file", help="default: the version's file with .eval.md")
    t = tsub.add_parser("check", help="check a version again (pages from its existing PDF; no WeasyPrint needed)")
    t.add_argument("app_id", type=int)
    t.add_argument("--version", type=int, help="default: the latest version")
    p.set_defaults(func=cmd_tailor)

    p = sub.add_parser("thread", help="the Agent thread on an application's panel (used by /ask)")
    hsub = p.add_subparsers(dest="thread_cmd", required=True)
    t = hsub.add_parser("show", help="an application's context and messages, a job offer's (--job), or (no id) the whole search")
    t.add_argument("app_id", type=int, nargs="?")
    t.add_argument("--job", help="a getjo job id not yet tracked: its description and messages")
    t.add_argument("--message", type=int, help="show whatever this message is about (for /ask job MSG, /ask cv MSG)")
    t.add_argument("--cv", action="store_true", help="the master CV: its state, versions and messages")
    t.add_argument("--claim", action="store_true", help="take the waiting messages for this session first")
    t = hsub.add_parser("reply", help="reply to a user message (text from --file or stdin)")
    t.add_argument("app_id", type=int, nargs="?", help="optional: checked against the message's application")
    t.add_argument("--to", type=int, required=True, help="the user message id being answered")
    t.add_argument("--file", help="a file holding the reply (default: stdin)")
    hsub.add_parser("pending", help="user messages waiting for the agent")
    p.set_defaults(func=cmd_thread)

    p = sub.add_parser("brief", help="job briefs: the agent's short summary of a job description, shown in the panel")
    bsub = p.add_subparsers(dest="brief_cmd", required=True)
    bsub.add_parser("next", help="claim the oldest waiting brief and print the job, its description and the format")
    for name, text in (("save", "store the brief (text on stdin or --file)"), ("show", "print the brief"),
                       ("queue", "ask for a brief (--redo: write it again)")):
        t = bsub.add_parser(name, help=text)
        t.add_argument("app_id", type=int, nargs="?", help="an application linked to a getjo job")
        t.add_argument("--job", help="a getjo job id")
        if name == "save":
            t.add_argument("--file", help="a file holding the brief (default: stdin)")
        if name == "queue":
            t.add_argument("--redo", action="store_true")
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser("review", help="read-only: the top 3 open tasks in the order to look at them, and the agent's work")
    p.add_argument("--all", action="store_true", help="every open task, not only the top 3")
    p.set_defaults(func=cmd_review)
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
