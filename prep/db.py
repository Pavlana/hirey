"""SQLite storage for applications, their status history, interviews, tasks, gaps and job
decisions (prep.db).

Allowed values for enum-like columns are checked in Python, not with CHECK
constraints: later phases may add values, and SQLite cannot alter a CHECK in place.
Dates are ISO strings: YYYY-MM-DD for days, YYYY-MM-DDTHH:MM:SSZ for timestamps.
"""

import sqlite3
from datetime import date, datetime, timezone
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
TASK_STATUSES = ("todo", "done")
GAP_STATUSES = ("open", "closed")
DECISIONS = ("applied", "skipped")
# Labels read back by job_decisions(): an "applied" job whose application is still to_apply
# reads as "interested". It is derived from the live status, never stored.
DECISION_LABELS = ("interested", "applied", "skipped")
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
    decision     TEXT NOT NULL,         -- applied / skipped
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

CREATE INDEX IF NOT EXISTS idx_applications_job ON applications(getjo_job_id);
CREATE INDEX IF NOT EXISTS idx_status_history_app ON status_history(application_id);
"""

# Columns added after a table was first created: {table: {column: definition}}.
# CREATE TABLE IF NOT EXISTS skips existing tables, so these are added explicitly.
MIGRATIONS: dict[str, dict[str, str]] = {}

APPLICATION_FIELDS = (
    "getjo_job_id", "company", "role", "link", "tier", "type", "source", "date_applied",
    "status", "last_contact", "next_action", "next_action_date", "puzzle_round", "pay",
    "contact", "notes",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_iso() -> str:
    return date.today().isoformat()


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
    conn.commit()


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
    """Set the given fields on an application. Pass None to clear a field."""
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


def _add_history(conn: sqlite3.Connection, app_id: int, status: str, stamp: str) -> None:
    conn.execute(
        "INSERT INTO status_history (application_id, status, changed_at) VALUES (?, ?, ?)",
        (app_id, status, stamp),
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
    **fields: str | None,
) -> tuple[int, bool]:
    """Record that an application was made (used by /applied and the dashboard).

    Reuses the application already linked to the same getjo job or link, if any, and
    returns (id, created). `status` defaults to applied, but never moves an existing
    application backwards: a later stage (technical, offer, ...) is kept unless `status`
    is given. `date_applied` defaults to today when not already set.
    """
    existing = None
    if fields.get("getjo_job_id"):
        existing = find_by_job(conn, fields["getjo_job_id"])
    if existing is None and fields.get("link"):
        existing = find_by_link(conn, fields["link"])

    fields.update(_status_fields(existing, status, date_applied))
    if existing:
        update_application(conn, existing["id"], **{k: v for k, v in fields.items() if v is not None})
        return existing["id"], False
    return add_application(conn, company, role, **fields), True


def _status_fields(existing: dict | None, status: str | None, date_applied: str | None) -> dict:
    current = existing["status"] if existing else None
    if status is None:
        status = "applied" if current in (None, "to_apply") else current
    fields: dict = {"status": status}
    if status != "to_apply" and (date_applied or not (existing and existing["date_applied"])):
        fields["date_applied"] = date_applied or today_iso()
        if current in (None, "to_apply"):
            fields["last_contact"] = fields["date_applied"]
    return fields


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


def overdue_actions(conn: sqlite3.Connection, today: str | None = None) -> list[dict]:
    """Open applications whose next action is due today or earlier, oldest first."""
    today = today or today_iso()
    marks = ", ".join("?" for _ in CLOSED_STATUSES)
    rows = conn.execute(
        f"""
        SELECT * FROM applications
        WHERE next_action_date IS NOT NULL AND next_action_date <= ?
          AND status NOT IN ({marks})
        ORDER BY next_action_date, id
        """,
        (today, *CLOSED_STATUSES),
    )
    return [dict(r) for r in rows]


def excluded_job_ids(conn: sqlite3.Connection) -> set[str]:
    """getjo job ids that are no longer new: linked to an application, or decided (applied/skipped)."""
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

    A skip needs a reason from SKIP_REASONS; an applied decision takes none.
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

    Applications linked to a getjo job without a decision row (recorded before the table
    existed) count as applied on their date. An applied job whose application is still
    to_apply reads as "interested", so it changes to applied when the application does.
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
        if r["status"] == "to_apply" and d["decision"] == "applied":
            d["decision"] = "interested"
    return found


# --- tasks ------------------------------------------------------------------

def add_task(
    conn: sqlite3.Connection,
    title: str,
    kind: str = "admin",
    due_date: str | None = None,
    application_id: int | None = None,
    gap_id: int | None = None,
) -> int:
    _check("kind", kind, TASK_KINDS)
    _check_date("due_date", due_date)
    cursor = conn.execute(
        """
        INSERT INTO tasks (title, kind, due_date, application_id, gap_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (title, kind, due_date, application_id, gap_id, now_iso()),
    )
    conn.commit()
    return cursor.lastrowid


def recent_done_tasks(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """Most recently completed tasks, newest first."""
    rows = conn.execute(
        "SELECT * FROM tasks WHERE status = 'done' ORDER BY done_at DESC, id DESC LIMIT ?", (limit,)
    )
    return [dict(r) for r in rows]


def complete_task(conn: sqlite3.Connection, task_id: int) -> None:
    cursor = conn.execute(
        "UPDATE tasks SET status = 'done', done_at = ? WHERE id = ? AND status = 'todo'",
        (now_iso(), task_id),
    )
    conn.commit()
    if cursor.rowcount == 0:
        if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
            raise LookupError(f"no task with id {task_id}")


def reopen_task(conn: sqlite3.Connection, task_id: int) -> None:
    """Undo complete_task: back to todo with no done_at. Reopening an open task is a no-op."""
    cursor = conn.execute(
        "UPDATE tasks SET status = 'todo', done_at = NULL WHERE id = ? AND status = 'done'", (task_id,)
    )
    conn.commit()
    if cursor.rowcount == 0:
        if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
            raise LookupError(f"no task with id {task_id}")


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
    """Open tasks due today or earlier."""
    today = today or today_iso()
    rows = conn.execute(
        """
        SELECT * FROM tasks
        WHERE status = 'todo' AND due_date IS NOT NULL AND due_date <= ?
        ORDER BY due_date, id
        """,
        (today,),
    )
    return [dict(r) for r in rows]


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
