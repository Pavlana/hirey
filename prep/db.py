"""SQLite storage for applications, their status history and comments, interviews, tasks,
gaps and job decisions (prep.db).

Allowed values for enum-like columns are checked in Python, not with CHECK
constraints: later phases may add values, and SQLite cannot alter a CHECK in place.
Dates are ISO strings: YYYY-MM-DD for days, YYYY-MM-DDTHH:MM:SSZ for timestamps.
"""

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

APPLICATION_STATUSES = (
    "to_apply", "applied", "recruiter_call", "technical", "system_design",
    "final", "offer", "rejected", "withdrawn", "no_response",
)
CLOSED_STATUSES = ("offer", "rejected", "withdrawn", "no_response")
TIERS = ("A", "B", "C")
APPLICATION_TYPES = ("contract", "permanent")
PUZZLE_ROUND = ("yes", "no", "unknown")
TASK_KINDS = ("apply", "prep", "drill", "admin")
# closed: an auto task left open when its application moved to another stage (not work done).
TASK_STATUSES = ("todo", "done", "closed")
GAP_STATUSES = ("open", "closed")
DECISIONS = ("interested", "applied", "skipped")
# Labels read back by job_decisions(): a tracked job reads as "interested" while its application
# is to_apply and as "applied" once it moves on, whatever was stored.
DECISION_LABELS = ("interested", "applied", "skipped")
# How statuses are shown; anything not listed shows as its value with spaces.
STATUS_LABELS = {"to_apply": "interested", "technical": "coding interview", "final": "company interview"}
# Stages with an interview date: their tasks are due the day before it (stage_tasks).
INTERVIEW_STAGES = ("recruiter_call", "technical", "system_design", "final")
# First tasks for an application entering a stage: (title, kind, days until due, or None for undated).
# For INTERVIEW_STAGES the days apply only when no interview date is given.
STAGE_TASKS: dict[str, tuple[tuple[str, str, int | None], ...]] = {
    "to_apply": (("Apply", "apply", 3),),
    "applied": (("Chase if no reply", "admin", 7),),
    "recruiter_call": (("Prepare for the call (questions, pitch, availability)", "prep", 2),),
    "technical": (("Coding practice", "drill", 2), ("Review the role's tech stack", "prep", 2)),
    "system_design": (("System design practice", "drill", 2),),
    "final": (("Prepare stories, company values, questions to ask", "prep", 2),),
    "offer": (("Review the offer", "admin", 2),),
    "rejected": (("Debrief — what to improve", "prep", 3),),
    "no_response": (("Debrief — what to improve", "prep", 3),),
}
# Auto tasks earlier versions created, which the agent now does instead of a to-do (company
# research, CV fit). Kept only so _backfill_task_stages marks old ones as auto tasks.
RETIRED_STAGE_TASKS = {"to_apply": (("Research the company", "prep", 2), ("Check CV fit / tailor CV", "prep", 2))}
AUTO_COMMENT = "auto: no reply in {days} days"
SKIP_REASONS = ("location", "seniority", "role_type", "stack", "company", "pay", "contract_type", "other")

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    getjo_job_id     TEXT,              -- getjo jobs.id ("source:company:job_id"), if any
    company          TEXT NOT NULL,
    role             TEXT NOT NULL,
    link             TEXT,
    tier             TEXT,              -- A / B / C
    type             TEXT,              -- contract / permanent
    source           TEXT,
    date_applied     TEXT,
    status           TEXT NOT NULL DEFAULT 'to_apply',
    last_contact     TEXT,
    next_action      TEXT,
    next_action_date TEXT,
    puzzle_round     TEXT NOT NULL DEFAULT 'unknown',
    pay              TEXT,              -- free text, stays in this local database only
    contact          TEXT,
    notes            TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS interviews (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications(id),
    date           TEXT NOT NULL,
    stage          TEXT,
    questions      TEXT,
    went_well      TEXT,
    to_improve     TEXT
);

CREATE TABLE IF NOT EXISTS gaps (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    date    TEXT NOT NULL,
    session TEXT,                       -- where the gap showed up (mock, interview, ...)
    gap     TEXT NOT NULL,
    status  TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS tasks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    title          TEXT NOT NULL,
    kind           TEXT NOT NULL DEFAULT 'admin',
    due_date       TEXT,
    status         TEXT NOT NULL DEFAULT 'todo',
    done_at        TEXT,
    application_id INTEGER REFERENCES applications(id),
    gap_id         INTEGER REFERENCES gaps(id),
    created_at     TEXT NOT NULL
);

-- One row each time an application is created or its status changes. Applications that
-- predate this table have no rows; their current status is the only known point.
CREATE TABLE IF NOT EXISTS status_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications(id),
    status         TEXT NOT NULL,
    changed_at     TEXT NOT NULL
);

-- One triage decision per getjo job (Job Offers page). The job's company/title/location/score
-- are copied at decision time, so the label still makes sense if getjo drops the job.
CREATE TABLE IF NOT EXISTS job_decisions (
    getjo_job_id TEXT PRIMARY KEY,
    decision     TEXT NOT NULL,         -- interested / applied / skipped
    reason       TEXT,                  -- SKIP_REASONS, for skips
    comment      TEXT,
    decided_at   TEXT NOT NULL,
    company      TEXT,
    title        TEXT,
    location     TEXT,
    score        INTEGER,
    url          TEXT,
    source       TEXT
);

-- Free comments on an application ("second interview booked"). Comments made with a stage
-- change live on its status_history row instead.
CREATE TABLE IF NOT EXISTS application_comments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications(id),
    body           TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_applications_job ON applications(getjo_job_id);
CREATE INDEX IF NOT EXISTS idx_status_history_app ON status_history(application_id);
CREATE INDEX IF NOT EXISTS idx_tasks_app ON tasks(application_id);
CREATE INDEX IF NOT EXISTS idx_comments_app ON application_comments(application_id);
"""

# Columns added after a table was first created: {table: {column: definition}}.
# CREATE TABLE IF NOT EXISTS skips existing tables, so these are added explicitly.
MIGRATIONS: dict[str, dict[str, str]] = {
    "status_history": {"comment": "TEXT"},  # the comment given with a stage change
    "tasks": {"stage": "TEXT"},             # the stage whose STAGE_TASKS created it; NULL if hand-made
}

APPLICATION_FIELDS = (
    "getjo_job_id", "company", "role", "link", "tier", "type", "source", "date_applied",
    "status", "last_contact", "next_action", "next_action_date", "puzzle_round", "pay",
    "contact", "notes",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_iso() -> str:
    return date.today().isoformat()


def status_label(status: str) -> str:
    """A status as shown to people: to_apply reads "interested"; the stored value is unchanged."""
    return STATUS_LABELS.get(status, status.replace("_", " "))


def connect(path: Path) -> sqlite3.Connection:
    """Open (and create if needed) prep.db, with the schema applied."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _add_missing_columns(conn)
    return conn


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, columns in MIGRATIONS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, definition in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                if (table, column) == ("tasks", "stage"):
                    _backfill_task_stages(conn)
                    next_actions_to_tasks(conn)
    conn.commit()


def _backfill_task_stages(conn: sqlite3.Connection) -> None:
    """Tasks made by stage_tasks before tasks.stage existed get their stage from the title."""
    for stage, specs in (*STAGE_TASKS.items(), *RETIRED_STAGE_TASKS.items()):
        for title, kind, _ in specs:
            conn.execute(
                "UPDATE tasks SET stage = ? WHERE stage IS NULL AND application_id IS NOT NULL "
                "AND title = ? AND kind = ?",
                (stage, title, kind),
            )


def next_actions_to_tasks(conn: sqlite3.Connection) -> list[int]:
    """Turn hand-typed next actions into tasks (once, when tasks.stage is added; also used by the
    example data), so they keep showing as due: one admin task per still-open application whose
    next_action has no open task with that title. Returns the new task ids."""
    rows = conn.execute(
        """
        SELECT id, next_action, next_action_date FROM applications a
        WHERE next_action IS NOT NULL AND status NOT IN ('rejected', 'withdrawn', 'no_response')
          AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.application_id = a.id AND t.status = 'todo'
                          AND t.title = a.next_action)
        ORDER BY id
        """
    ).fetchall()
    return [add_task(conn, r["next_action"], "admin", r["next_action_date"], r["id"]) for r in rows]


def _check(name: str, value: str | None, allowed: tuple[str, ...]) -> None:
    if value is not None and value not in allowed:
        raise ValueError(f"{name} must be one of {', '.join(allowed)}; got {value!r}")


def _check_date(name: str, value: str | None) -> None:
    if value is None:
        return
    try:
        date.fromisoformat(value)
    except ValueError as e:
        raise ValueError(f"{name} must be a date like 2026-01-31; got {value!r}") from e


def _validate_application(fields: dict) -> None:
    unknown = set(fields) - set(APPLICATION_FIELDS)
    if unknown:
        raise ValueError(f"unknown application fields: {', '.join(sorted(unknown))}")
    _check("status", fields.get("status"), APPLICATION_STATUSES)
    _check("tier", fields.get("tier"), TIERS)
    _check("type", fields.get("type"), APPLICATION_TYPES)
    _check("puzzle_round", fields.get("puzzle_round"), PUZZLE_ROUND)
    for name in ("date_applied", "last_contact", "next_action_date"):
        _check_date(name, fields.get(name))


# --- applications -----------------------------------------------------------

def add_application(conn: sqlite3.Connection, company: str, role: str, **fields: str | None) -> int:
    """Insert an application and return its id. Unset fields keep their defaults."""
    fields = {k: v for k, v in fields.items() if v is not None}
    fields.update(company=company, role=role)
    _validate_application(fields)
    stamp = now_iso()
    fields.update(created_at=stamp, updated_at=stamp)
    names = ", ".join(fields)
    marks = ", ".join(f":{k}" for k in fields)
    cursor = conn.execute(f"INSERT INTO applications ({names}) VALUES ({marks})", fields)
    app_id = cursor.lastrowid
    _add_history(conn, app_id, fields.get("status", "to_apply"), stamp)
    conn.commit()
    return app_id


def update_application(conn: sqlite3.Connection, app_id: int, **fields: str | None) -> None:
    """Set the given fields on an application. Pass None to clear a field.

    A status change here only writes history; move_application also runs the stage's tasks."""
    if not fields:
        return
    _validate_application({k: v for k, v in fields.items() if v is not None})
    if fields.get("status") is None:
        fields.pop("status", None)  # status is NOT NULL; None means "leave it"
    row = conn.execute("SELECT status FROM applications WHERE id = ?", (app_id,)).fetchone()
    if row is None:
        raise LookupError(f"no application with id {app_id}")
    stamp = now_iso()
    fields["updated_at"] = stamp
    assignments = ", ".join(f"{k} = :{k}" for k in fields)
    conn.execute(f"UPDATE applications SET {assignments} WHERE id = :id", {**fields, "id": app_id})
    if "status" in fields and fields["status"] != row["status"]:
        _add_history(conn, app_id, fields["status"], stamp)
    conn.commit()


def _add_history(conn: sqlite3.Connection, app_id: int, status: str, stamp: str,
                 comment: str | None = None) -> None:
    conn.execute(
        "INSERT INTO status_history (application_id, status, changed_at, comment) VALUES (?, ?, ?, ?)",
        (app_id, status, stamp, comment or None),
    )


def status_history(conn: sqlite3.Connection, app_id: int) -> list[str]:
    """Statuses an application has had, oldest first (empty for rows older than the table)."""
    rows = conn.execute(
        "SELECT status FROM status_history WHERE application_id = ? ORDER BY id", (app_id,)
    )
    return [r[0] for r in rows]


def record_application(
    conn: sqlite3.Connection,
    company: str,
    role: str,
    *,
    status: str | None = None,
    date_applied: str | None = None,
    today: str | None = None,
    **fields: str | None,
) -> tuple[int, bool]:
    """Record an application from a job (used by /applied and the dashboard).

    Reuses the application already linked to the same getjo job or link, if any, and
    returns (id, created). `status` defaults to applied, but never moves an existing
    application backwards: a later stage (technical, offer, ...) is kept unless `status`
    is given, and an explicit to_apply never replaces a later status. `date_applied`
    defaults to today when not already set. A new application gets its stage's first tasks;
    a status change goes through move_application.
    """
    today = today or today_iso()
    existing = None
    if fields.get("getjo_job_id"):
        existing = find_by_job(conn, fields["getjo_job_id"])
    if existing is None and fields.get("link"):
        existing = find_by_link(conn, fields["link"])

    fields.update(_status_fields(existing, status, date_applied, today))
    if existing is None:
        app_id = add_application(conn, company, role, **fields)
        stage_tasks(conn, app_id, fields["status"], today)
        return app_id, True
    new_status = fields.pop("status")
    update_application(conn, existing["id"], **{k: v for k, v in fields.items() if v is not None})
    move_application(conn, existing["id"], new_status, today)
    return existing["id"], False


def _status_fields(existing: dict | None, status: str | None, date_applied: str | None, today: str) -> dict:
    current = existing["status"] if existing else None
    if status is None or (status == "to_apply" and current not in (None, "to_apply")):
        status = "applied" if current in (None, "to_apply") else current
    fields: dict = {"status": status}
    if status != "to_apply" and (date_applied or not (existing and existing["date_applied"])):
        fields["date_applied"] = date_applied or today
        if current in (None, "to_apply"):
            fields["last_contact"] = fields["date_applied"]
    return fields


def move_application(
    conn: sqlite3.Connection,
    app_id: int,
    status: str,
    today: str | None = None,
    comment: str | None = None,
    interview_date: str | None = None,
    only_from: str | None = None,
) -> bool:
    """Move an application to `status` (any stage, forward or back) and return True if it moved.

    Every status change goes through here (dashboard board, CLI, record_application, the
    no-response check): it writes the history row with the comment, sets date_applied the
    first time the application is sent, stores the interview date (INTERVIEW_STAGES) and runs
    stage_tasks. Moving to the current status changes nothing; with `only_from`, neither does
    moving from any status but that one. The update only applies while the status is still the
    one read here, so two concurrent moves never both happen.
    """
    today = today or today_iso()
    _check("status", status, APPLICATION_STATUSES)
    _check_date("interview_date", interview_date)
    row = conn.execute("SELECT status, date_applied FROM applications WHERE id = ?", (app_id,)).fetchone()
    if row is None:
        raise LookupError(f"no application with id {app_id}")
    if row["status"] == status or (only_from is not None and row["status"] != only_from):
        return False
    stamp = now_iso()
    sent = status not in ("to_apply", "withdrawn") and not row["date_applied"]
    cursor = conn.execute(
        "UPDATE applications SET status = ?, updated_at = ?, date_applied = COALESCE(date_applied, ?) "
        "WHERE id = ? AND status = ?",
        (status, stamp, today if sent else None, app_id, row["status"]),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        return False
    _add_history(conn, app_id, status, stamp, comment)
    if interview_date and status in INTERVIEW_STAGES:
        conn.execute("INSERT INTO interviews (application_id, date, stage) VALUES (?, ?, ?)",
                     (app_id, interview_date, status))
    conn.commit()
    stage_tasks(conn, app_id, status, today, interview_date)
    return True


def stage_tasks(conn: sqlite3.Connection, app_id: int, stage: str, today: str | None = None,
                interview_date: str | None = None) -> list[int]:
    """Set up the tasks for an application entering `stage` and return the new task ids.

    Entering applied completes the open Apply task (applying is done). Open auto tasks from
    other stages are closed (status closed, not done). Then the stage's STAGE_TASKS are added,
    skipping a title the application already has open. For INTERVIEW_STAGES with an
    interview date, they are due the day before it (today if that has passed).
    """
    today = today or today_iso()
    if stage == "applied":
        conn.execute(
            "UPDATE tasks SET status = 'done', done_at = ? WHERE application_id = ? AND title = 'Apply' "
            "AND kind = 'apply' AND status = 'todo'",
            (now_iso(), app_id),
        )
    conn.execute(
        "UPDATE tasks SET status = 'closed' WHERE application_id = ? AND status = 'todo' "
        "AND stage IS NOT NULL AND stage != ?",
        (app_id, stage),
    )
    conn.commit()
    have = {r[0] for r in conn.execute(
        "SELECT title FROM tasks WHERE application_id = ? AND status = 'todo'", (app_id,))}
    ids = []
    for title, kind, days in STAGE_TASKS.get(stage, ()):
        if title in have:
            continue
        ids.append(add_task(conn, title, kind, _stage_due(today, days, stage, interview_date), app_id,
                            stage=stage))
    sync_next_action(conn, app_id)
    return ids


def _stage_due(today: str, days: int | None, stage: str, interview_date: str | None) -> str | None:
    if interview_date and stage in INTERVIEW_STAGES:
        return max(today, (date.fromisoformat(interview_date) - timedelta(days=1)).isoformat())
    return None if days is None else (date.fromisoformat(today) + timedelta(days=days)).isoformat()


def sync_next_action(conn: sqlite3.Connection, app_id: int) -> None:
    """Set next_action / next_action_date from the application's next open task (dated ones
    first, earliest first), or clear them when it has none. Kept for older readers."""
    task = conn.execute(
        "SELECT title, due_date FROM tasks WHERE application_id = ? AND status = 'todo' "
        "ORDER BY due_date IS NULL, due_date, id LIMIT 1",
        (app_id,),
    ).fetchone()
    conn.execute(
        "UPDATE applications SET next_action = ?, next_action_date = ? WHERE id = ?",
        (task["title"] if task else None, task["due_date"] if task else None, app_id),
    )
    conn.commit()


def last_change_day(conn: sqlite3.Connection, app_id: int) -> str:
    """The day (YYYY-MM-DD) of the application's last stage change: its latest status_history
    row, else date_applied, else created_at."""
    row = conn.execute(
        """
        SELECT COALESCE((SELECT MAX(changed_at) FROM status_history WHERE application_id = a.id),
                        a.date_applied, a.created_at)
        FROM applications a WHERE a.id = ?
        """,
        (app_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"no application with id {app_id}")
    return row[0][:10]


def auto_no_response(conn: sqlite3.Connection, days: int, today: str | None = None) -> list[dict]:
    """Move every application that has sat in applied for `days` days or more since its last
    stage change to no_response (comment AUTO_COMMENT). Returns the moved ones (id, company, role)."""
    today = today or today_iso()
    cutoff = (date.fromisoformat(today) - timedelta(days=days)).isoformat()
    rows = conn.execute("SELECT id, company, role FROM applications WHERE status = 'applied' ORDER BY id").fetchall()
    moved = []
    for r in rows:
        if last_change_day(conn, r["id"]) <= cutoff and move_application(
                conn, r["id"], "no_response", today, AUTO_COMMENT.format(days=days), only_from="applied"):
            moved.append(dict(r))
    return moved


def recent_auto_moves(conn: sqlite3.Connection, today: str | None = None, window: int = 7) -> list[dict]:
    """Applications moved to no_response by auto_no_response in the last `window` days, newest
    first: id, company, role, changed_at."""
    today = today or today_iso()
    since = (date.fromisoformat(today) - timedelta(days=window)).isoformat()
    rows = conn.execute(
        """
        SELECT a.id, a.company, a.role, h.changed_at FROM status_history h
        JOIN applications a ON a.id = h.application_id
        WHERE h.status = 'no_response' AND h.comment LIKE 'auto:%' AND h.changed_at >= ?
        ORDER BY h.changed_at DESC, h.id DESC
        """,
        (since,),
    )
    return [dict(r) for r in rows]


# --- comments ---------------------------------------------------------------------

def add_comment(conn: sqlite3.Connection, app_id: int, body: str) -> int:
    """A free comment on an application (e.g. "take-home test sent")."""
    if not body.strip():
        raise ValueError("a comment needs some text")
    if get_application(conn, app_id) is None:
        raise LookupError(f"no application with id {app_id}")
    cursor = conn.execute(
        "INSERT INTO application_comments (application_id, body, created_at) VALUES (?, ?, ?)",
        (app_id, body.strip(), now_iso()),
    )
    conn.commit()
    return cursor.lastrowid


def timeline(conn: sqlite3.Connection, app_id: int) -> list[dict]:
    """Stage changes and free comments, oldest first: {at, status (None for a comment), comment}."""
    rows = conn.execute(
        """
        SELECT changed_at AS at, status, comment, 0 AS kind, id FROM status_history WHERE application_id = ?
        UNION ALL
        SELECT created_at, NULL, body, 1, id FROM application_comments WHERE application_id = ?
        ORDER BY at, kind, id
        """,
        (app_id, app_id),
    )
    return [{"at": r["at"], "status": r["status"], "comment": r["comment"]} for r in rows]


def comment_counts(conn: sqlite3.Connection) -> dict[int, int]:
    """Comments per application: free comments plus stage changes that carry a comment."""
    rows = conn.execute(
        """
        SELECT application_id, COUNT(*) FROM (
            SELECT application_id FROM application_comments
            UNION ALL
            SELECT application_id FROM status_history WHERE comment IS NOT NULL
        ) GROUP BY application_id
        """
    )
    return {r[0]: r[1] for r in rows}


def get_application(conn: sqlite3.Connection, app_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    return dict(row) if row else None


def find_by_job(conn: sqlite3.Connection, getjo_job_id: str) -> dict | None:
    """The application linked to a getjo job, if one exists."""
    row = conn.execute(
        "SELECT * FROM applications WHERE getjo_job_id = ? ORDER BY id LIMIT 1", (getjo_job_id,)
    ).fetchone()
    return dict(row) if row else None


def find_by_link(conn: sqlite3.Connection, link: str) -> dict | None:
    row = conn.execute("SELECT * FROM applications WHERE link = ? ORDER BY id LIMIT 1", (link,)).fetchone()
    return dict(row) if row else None


def list_applications(conn: sqlite3.Connection, status: str | None = None) -> list[dict]:
    _check("status", status, APPLICATION_STATUSES)
    if status:
        rows = conn.execute("SELECT * FROM applications WHERE status = ? ORDER BY id", (status,))
    else:
        rows = conn.execute("SELECT * FROM applications ORDER BY id")
    return [dict(r) for r in rows]


def linked_job_ids(conn: sqlite3.Connection) -> set[str]:
    """getjo job ids that already have an application."""
    rows = conn.execute("SELECT getjo_job_id FROM applications WHERE getjo_job_id IS NOT NULL")
    return {r[0] for r in rows}


def excluded_job_ids(conn: sqlite3.Connection) -> set[str]:
    """getjo job ids that are no longer new: linked to an application, or decided."""
    rows = conn.execute("SELECT getjo_job_id FROM job_decisions")
    return linked_job_ids(conn) | {r[0] for r in rows}


# --- job decisions ------------------------------------------------------------

def record_decision(
    conn: sqlite3.Connection,
    job: dict,
    decision: str,
    reason: str | None = None,
    comment: str | None = None,
) -> None:
    """Store (or replace) the decision for a getjo job. `job` is a getjo row (id, company, ...).

    A skip needs a reason from SKIP_REASONS; interested and applied take none.
    """
    _check("decision", decision, DECISIONS)
    if decision == "skipped":
        if not reason:
            raise ValueError("a skip needs a reason")
        _check("reason", reason, SKIP_REASONS)
    else:
        reason = None
    conn.execute(
        """
        INSERT OR REPLACE INTO job_decisions
            (getjo_job_id, decision, reason, comment, decided_at, company, title, location, score, url, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (job["id"], decision, reason, comment or None, now_iso(), job.get("company"), job.get("title"),
         job.get("location"), job.get("score"), job.get("url"), job.get("source")),
    )
    conn.commit()


def undo_skip(conn: sqlite3.Connection, getjo_job_id: str) -> None:
    """Remove a skip so the job is new again. Applied decisions are not undone here."""
    row = conn.execute(
        "SELECT decision FROM job_decisions WHERE getjo_job_id = ?", (getjo_job_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"no decision for job {getjo_job_id}")
    if row["decision"] != "skipped":
        raise ValueError("only a skip can be undone")
    conn.execute("DELETE FROM job_decisions WHERE getjo_job_id = ?", (getjo_job_id,))
    conn.commit()


def job_decisions(conn: sqlite3.Connection) -> dict[str, dict]:
    """Every decision by getjo job id, labelled with DECISION_LABELS.

    A job with a linked application reads from its live status: interested while it is
    to_apply, applied once it moves on (a skip stays a skip). Applications linked to a getjo
    job without a decision row (recorded before the table existed) count on their date.
    """
    found = {r["getjo_job_id"]: dict(r) for r in conn.execute("SELECT * FROM job_decisions")}
    rows = conn.execute(
        """
        SELECT getjo_job_id, company, role, link, source, status, date_applied, created_at FROM applications
        WHERE getjo_job_id IS NOT NULL ORDER BY id
        """
    )
    linked = set()
    for r in rows:
        if r["getjo_job_id"] in linked:
            continue  # the oldest application for a job is the one find_by_job uses
        linked.add(r["getjo_job_id"])
        d = found.setdefault(r["getjo_job_id"], {
            "getjo_job_id": r["getjo_job_id"], "decision": "applied", "reason": None, "comment": None,
            "decided_at": r["date_applied"] or r["created_at"], "company": r["company"], "title": r["role"],
            "location": None, "score": None, "url": r["link"], "source": r["source"],
        })
        if d["decision"] != "skipped":
            d["decision"] = "interested" if r["status"] == "to_apply" else "applied"
    return found


# --- tasks ------------------------------------------------------------------

def add_task(
    conn: sqlite3.Connection,
    title: str,
    kind: str = "admin",
    due_date: str | None = None,
    application_id: int | None = None,
    gap_id: int | None = None,
    stage: str | None = None,
) -> int:
    """Add a task; `stage` marks one made by stage_tasks. The application's next action follows."""
    _check("kind", kind, TASK_KINDS)
    _check_date("due_date", due_date)
    if application_id is not None and get_application(conn, application_id) is None:
        raise LookupError(f"no application with id {application_id}")
    cursor = conn.execute(
        """
        INSERT INTO tasks (title, kind, due_date, application_id, gap_id, stage, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (title, kind, due_date, application_id, gap_id, stage, now_iso()),
    )
    conn.commit()
    if application_id is not None:
        sync_next_action(conn, application_id)
    return cursor.lastrowid


def recent_done_tasks(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """Most recently completed tasks, newest first."""
    rows = conn.execute(
        "SELECT * FROM tasks WHERE status = 'done' ORDER BY done_at DESC, id DESC LIMIT ?", (limit,)
    )
    return [dict(r) for r in rows]


def complete_task(conn: sqlite3.Connection, task_id: int) -> None:
    conn.execute(
        "UPDATE tasks SET status = 'done', done_at = ? WHERE id = ? AND status = 'todo'",
        (now_iso(), task_id),
    )
    conn.commit()
    _after_task_change(conn, task_id)


def reopen_task(conn: sqlite3.Connection, task_id: int) -> None:
    """Undo complete_task (or an auto close): back to todo with no done_at. Reopening an open
    task is a no-op."""
    conn.execute(
        "UPDATE tasks SET status = 'todo', done_at = NULL WHERE id = ? AND status IN ('done', 'closed')",
        (task_id,),
    )
    conn.commit()
    _after_task_change(conn, task_id)


def _after_task_change(conn: sqlite3.Connection, task_id: int) -> None:
    row = conn.execute("SELECT application_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise LookupError(f"no task with id {task_id}")
    if row[0] is not None:
        sync_next_action(conn, row[0])


def get_task(conn: sqlite3.Connection, task_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(row) if row else None


def list_tasks(conn: sqlite3.Connection, status: str | None = "todo") -> list[dict]:
    """Tasks with the given status (all when None), by due date then id; undated last."""
    _check("status", status, TASK_STATUSES)
    where = "WHERE status = ?" if status else ""
    params = (status,) if status else ()
    rows = conn.execute(
        f"SELECT * FROM tasks {where} ORDER BY due_date IS NULL, due_date, id", params
    )
    return [dict(r) for r in rows]


def tasks_due(conn: sqlite3.Connection, today: str | None = None) -> list[dict]:
    """Open tasks due today or earlier, oldest first, with their application's company, role
    and status (None for general tasks). Tasks on any application count, offers included."""
    today = today or today_iso()
    rows = conn.execute(
        """
        SELECT t.*, a.company, a.role, a.status AS app_status FROM tasks t
        LEFT JOIN applications a ON a.id = t.application_id
        WHERE t.status = 'todo' AND t.due_date IS NOT NULL AND t.due_date <= ?
        ORDER BY t.due_date, t.id
        """,
        (today,),
    )
    return [dict(r) for r in rows]


def app_tasks(conn: sqlite3.Connection, status: str = "todo") -> dict[int | None, list[dict]]:
    """Tasks with `status` grouped by application id (None: general tasks), each list by due
    date then id, undated last."""
    grouped: dict[int | None, list[dict]] = {}
    for t in list_tasks(conn, status):
        grouped.setdefault(t["application_id"], []).append(t)
    return grouped


# --- interviews and gaps ----------------------------------------------------

def add_interview(conn: sqlite3.Connection, application_id: int, day: str, stage: str | None = None,
                  questions: str | None = None, went_well: str | None = None,
                  to_improve: str | None = None) -> int:
    _check_date("date", day)
    cursor = conn.execute(
        """
        INSERT INTO interviews (application_id, date, stage, questions, went_well, to_improve)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (application_id, day, stage, questions, went_well, to_improve),
    )
    conn.commit()
    return cursor.lastrowid


def add_gap(conn: sqlite3.Connection, gap: str, day: str | None = None, session: str | None = None) -> int:
    day = day or today_iso()
    _check_date("date", day)
    cursor = conn.execute("INSERT INTO gaps (date, session, gap) VALUES (?, ?, ?)", (day, session, gap))
    conn.commit()
    return cursor.lastrowid


def close_gap(conn: sqlite3.Connection, gap_id: int) -> None:
    conn.execute("UPDATE gaps SET status = 'closed' WHERE id = ?", (gap_id,))
    conn.commit()


def board_applications(conn: sqlite3.Connection) -> list[dict]:
    """Every application for the Applications board, oldest first: id, company, role, link,
    tier, status and since (the day of its last stage change, as last_change_day). Never pay."""
    rows = conn.execute(
        """
        SELECT a.id, a.company, a.role, a.link, a.tier, a.status,
               substr(COALESCE((SELECT MAX(changed_at) FROM status_history WHERE application_id = a.id),
                               a.date_applied, a.created_at), 1, 10) AS since
        FROM applications a ORDER BY a.id
        """
    )
    return [dict(r) for r in rows]
