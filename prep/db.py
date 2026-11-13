"""SQLite storage for applications, their status history and comments, interviews, tasks,
gaps, job decisions, the agent's research queue and results, and each application's message
thread with the agent (prep.db).

Allowed values for enum-like columns are checked in Python, not with CHECK
constraints: later phases may add values, and SQLite cannot alter a CHECK in place.
Dates are ISO strings: YYYY-MM-DD for days, YYYY-MM-DDTHH:MM:SSZ for timestamps.
"""

import json
import logging
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("prep.db")

APPLICATION_STATUSES = (
    "to_apply", "applied", "recruiter_call", "technical", "system_design",
    "final", "offer", "rejected", "withdrawn", "no_response", "skipped",
)
# skipped: closed from Interested without applying (the panel's Skip, `cli skip`); withdrawn is
# pulling out of a live process.
CLOSED_STATUSES = ("offer", "rejected", "withdrawn", "no_response", "skipped")
TIERS = ("A", "B", "C")
APPLICATION_TYPES = ("contract", "permanent")
PUZZLE_ROUND = ("yes", "no", "unknown")
TASK_KINDS = ("apply", "prep", "drill", "admin")
# closed: an auto task left open when its application moved to another stage (not work done).
TASK_STATUSES = ("todo", "done", "closed")
GAP_STATUSES = ("open", "closed")
# A practice session: a mock interview (run by the agent or elsewhere) or a real one, debriefed.
SESSION_KINDS = ("mock", "real")
# Each gap named in a session gets one open drill task (add_gap_drills); closing the gap completes it.
DRILL_TITLE = "Drill: {gap}"
# The message the panel's Mock interview control stores; /ask hands it to the /mock skill.
MOCK_START = "Start a mock interview: {label} ({stage})"
DECISIONS = ("interested", "applied", "skipped")
# Labels read back by job_decisions(): a tracked job reads as "interested" while its application
# is to_apply and as "applied" once it moves on, whatever was stored.
DECISION_LABELS = ("interested", "applied", "skipped")
# How statuses are shown; anything not listed shows as its value with spaces.
STATUS_LABELS = {"to_apply": "interested", "technical": "coding interview", "final": "company interview"}
# Stages with an interview date: their tasks are due the day before it (stage_tasks).
INTERVIEW_STAGES = ("recruiter_call", "technical", "system_design", "final")
# The task a closed application gets; a recorded debrief completes it (record_debrief).
DEBRIEF_TASK = "Debrief — what to improve"
# First tasks for an application entering a stage: (title, kind, days until due, or None for undated).
# For INTERVIEW_STAGES the days apply only when no interview date is given.
STAGE_TASKS: dict[str, tuple[tuple[str, str, int | None], ...]] = {
    "to_apply": (),  # the agent researches first; REVIEW_TASK follows once both results are in
    "applied": (("Chase if no reply", "admin", 7),),
    "recruiter_call": (("Prepare for the call (questions, pitch, availability)", "prep", 2),),
    "technical": (("Coding practice", "drill", 2), ("Review the role's tech stack", "prep", 2)),
    "system_design": (("System design practice", "drill", 2),),
    "final": (("Prepare stories, company values, questions to ask", "prep", 2),),
    "offer": (("Review the offer", "admin", 2),),
    "rejected": ((DEBRIEF_TASK, "prep", 3),),
    "no_response": ((DEBRIEF_TASK, "prep", 3),),
}
# Auto tasks earlier versions created: company research and the CV fit (the agent's job now) and
# Apply (+3 on entering Interested, replaced by REVIEW_TASK). Kept only so _backfill_task_stages
# marks old ones as auto tasks.
RETIRED_STAGE_TASKS = {"to_apply": (("Research the company", "prep", 2), ("Check CV fit / tailor CV", "prep", 2),
                                    ("Apply", "apply", 3))}
# Added to an Interested application once both its company research and CV fit are saved (review_task).
REVIEW_TASK = ("Review research and apply", "apply", 2)
AUTO_COMMENT = "auto: no reply in {days} days"
# Agent work queued for an application (done by the /research skill, never by the dashboard).
# tailor: a copy of cv.md tailored to this job (cv-tailored/), revised later through the thread (/ask).
# cv_eval: the AI check of the latest tailored version (grounding, fit, skim; prep/cveval.py), queued by
# every tailor save and done in a separate pass from the one that wrote the CV.
REQUEST_KINDS = ("research", "cv_fit", "tailor", "cv_eval")
REQUEST_LABELS = {"research": "Company research", "cv_fit": "CV fit", "tailor": "Tailored CV", "cv_eval": "AI CV check"}
# The two kinds entering Interested queues and the Research / CV fit buttons handle; tailor is asked for on its own.
RESEARCH_KINDS = ("research", "cv_fit")
# claimed: a session took it with claim_request; claims older than CLAIM_HOURS count as pending again.
REQUEST_STATUSES = ("pending", "claimed", "done")
CLAIM_HOURS = 6
RECOGNITION = ("well_known", "niche", "little_known")
RECOGNITION_LABELS = {"well_known": "well known", "niche": "known in its niche", "little_known": "little known"}
SIZE_BANDS = ("1-50", "51-250", "251-1000", "1001-5000", "5000+")
FIT_GAP_SESSION = "cv fit"
# The Agent conversation: the user writes, the standing assistant (/ask) replies. A user message is
# about one application or the whole search (application_id NULL). It is pending until an /ask
# session claims it (claims older than CLAIM_HOURS count as pending again) and answered once a
# reply names it. Agent replies have no status and inherit the message's application.
MESSAGE_AUTHORS = ("user", "agent")
MESSAGE_STATUSES = ("pending", "claimed", "answered")
MAX_MESSAGE = 20_000  # characters: room for a pasted recruiter email
# A message about neither an application nor a job may have a topic: "cv" is the master CV (the
# My CV panel's "About my CV" tick). A message with no application, job or topic is general.
MESSAGE_TOPICS = ("cv",)
# A message about 2 to MAX_TARGETS things at once (ticked rows: applications, job offers, My CV) is a
# comparison: topic COMPARE, no application or job in its own columns; agent_message_targets lists them.
# The topic is set by add_compare only, never taken from a form.
COMPARE = "compare"
MAX_TARGETS = 4
# Job briefs: written by the agent from the getjo description, shown at the top of the panel.
MAX_BRIEF_WORDS = 300
MAX_FACTS = 200  # characters in the key facts line (the brief's first line)
# Pay never goes into anything the agent writes: currency signs, "80k", and pay words are refused.
PAY_PATTERN = re.compile(r"[£$€¥]|\b\d+(?:[.,]\d+)?\s?k\b|\b(?:salary|salaries|pay|paid|compensation|remuneration|ote|"
                         r"bonus|per annum)\b", re.IGNORECASE)
# The automatic check of each tailored CV version (prep/cvcheck.py, `tailor save`): every finding is
# "block" (must be fixed before sending) or "look" (read this line); the verdict follows from them.
# A finding's source says which checker wrote it: "rules" (prep/cvcheck.py) or "ai" (prep/cveval.py).
CHECK_VERDICTS = ("ready", "look", "blocked")
CHECK_SEVERITIES = ("block", "look")
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

-- A practice session (mock or a real interview debriefed): scores per area, 1-5, as a JSON object
-- ({"system design": 3}); the transcript or notes are private_dir/sessions/<file>. Gaps it found
-- point back at it (gaps.session_id).
CREATE TABLE IF NOT EXISTS sessions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    date           TEXT NOT NULL,
    kind           TEXT NOT NULL,       -- mock / real
    application_id INTEGER REFERENCES applications(id),
    stage          TEXT,                -- an interview stage (INTERVIEW_STAGES)
    scores         TEXT NOT NULL DEFAULT '{}',
    file_path      TEXT,
    created_at     TEXT NOT NULL
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

-- Research and CV fit requests. At most one open (pending or claimed) request per application and
-- kind: the partial unique index makes a second one fail even when two requests race.
CREATE TABLE IF NOT EXISTS agent_requests (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications(id),
    kind           TEXT NOT NULL,       -- research / cv_fit / tailor
    status         TEXT NOT NULL DEFAULT 'pending',
    force          INTEGER NOT NULL DEFAULT 0,  -- 1: Redo, research again even if the profile is fresh
    requested_at   TEXT NOT NULL,
    claimed_at     TEXT,
    done_at        TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_requests_open
    ON agent_requests(application_id, kind) WHERE status IN ('pending', 'claimed');

-- One profile per company, keyed by company_slug(); the full text is private_dir/companies/<slug>.md.
CREATE TABLE IF NOT EXISTS company_profiles (
    slug             TEXT PRIMARY KEY,
    company          TEXT NOT NULL,
    file_path        TEXT NOT NULL,
    what_it_does     TEXT,
    sector           TEXT,
    employees        INTEGER,
    employees_as_of  TEXT,
    size_band        TEXT,
    london_remote    TEXT,
    recognition      TEXT,              -- RECOGNITION
    recognition_note TEXT,
    funding          TEXT,
    sources          TEXT,              -- JSON list of URLs
    suggested_tier   TEXT,              -- A / B / C, suggested by the agent; the user sets applications.tier
    tier_reason      TEXT,
    researched_at    TEXT NOT NULL
);

-- The latest CV fit check per application; the full text is private_dir/fit/<app-id>-<slug>.md.
CREATE TABLE IF NOT EXISTS cv_fits (
    application_id INTEGER PRIMARY KEY REFERENCES applications(id),
    score          INTEGER NOT NULL,    -- 1-5
    top_gaps       TEXT NOT NULL,       -- JSON list, at most 3
    file_path      TEXT NOT NULL,
    checked_at     TEXT NOT NULL
);

-- The latest tailored copy of cv.md per application; the text is private_dir/cv-tailored/<app-id>-<slug>.md.
-- version goes up by one on every save (first pass and each revision through the thread).
CREATE TABLE IF NOT EXISTS tailored_cvs (
    application_id INTEGER PRIMARY KEY REFERENCES applications(id),
    file_path      TEXT NOT NULL,
    version        INTEGER NOT NULL,
    updated_at     TEXT NOT NULL
);

-- The automatic check of one tailored CV version: the verdict and its findings (JSON list of
-- {source, check, severity, line, message, text}) from every checker, and the changes vs cv.md at
-- check time (JSON list of short lines). Private: findings may quote CV lines (never pay).
CREATE TABLE IF NOT EXISTS tailored_cv_checks (
    application_id INTEGER NOT NULL REFERENCES applications(id),
    version        INTEGER NOT NULL,
    verdict        TEXT NOT NULL,       -- ready / look / blocked
    findings       TEXT NOT NULL,
    changes        TEXT NOT NULL,
    checked_at     TEXT NOT NULL,
    PRIMARY KEY (application_id, version)
);

-- The first Agent thread, one per application (replaced by agent_messages, which copies its rows
-- on connect; nothing writes here any more). Bodies are private: never logged.
CREATE TABLE IF NOT EXISTS application_messages (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications(id),
    author         TEXT NOT NULL,       -- user / agent
    body           TEXT NOT NULL,
    status         TEXT,                -- user messages: pending / claimed / answered; NULL for agent
    reply_to       INTEGER REFERENCES application_messages(id),  -- agent replies: the user message answered
    created_at     TEXT NOT NULL,
    claimed_at     TEXT,
    answered_at    TEXT
);

-- Every saved version of an application's tailored CV, each its own file (tailored_cvs points at the
-- latest). Versions are never overwritten or deleted.
CREATE TABLE IF NOT EXISTS tailored_cv_versions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications(id),
    version        INTEGER NOT NULL,
    file_path      TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    UNIQUE (application_id, version)
);

-- The Agent conversation under the pipeline list: one stream for every application. A message is
-- about one application (application_id) or about the whole search (NULL). Bodies are private.
CREATE TABLE IF NOT EXISTS agent_messages (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER REFERENCES applications(id),  -- NULL: about the whole search
    author         TEXT NOT NULL,       -- user / agent
    body           TEXT NOT NULL,
    status         TEXT,                -- user messages: pending / claimed / answered; NULL for agent
    reply_to       INTEGER REFERENCES agent_messages(id),  -- agent replies: the user message answered
    created_at     TEXT NOT NULL,
    claimed_at     TEXT,
    answered_at    TEXT
);

-- What each agent message is about, one row per target: an application (application_id, with its getjo
-- job id when it has one, so Undo Interested can hand the row back to the job), a getjo job not yet
-- tracked (getjo_job_id only) or a topic ("cv"). A general message has none; a comparison has 2-4.
-- agent_messages keeps its own columns for single-target messages (filled as before).
CREATE TABLE IF NOT EXISTS agent_message_targets (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id     INTEGER NOT NULL REFERENCES agent_messages(id),
    application_id INTEGER REFERENCES applications(id),
    getjo_job_id   TEXT,
    topic          TEXT
);

-- A short structured brief of a getjo job (Markdown: the key facts line, then sections), written by
-- the agent from the description. Never pay. One per job; Redo replaces it.
CREATE TABLE IF NOT EXISTS job_briefs (
    getjo_job_id TEXT PRIMARY KEY,
    brief        TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- Briefs waiting for the agent, one row per getjo job (agent_requests needs an application, which a
-- new offer does not have). pending -> claimed -> done; force: Redo.
CREATE TABLE IF NOT EXISTS brief_requests (
    getjo_job_id TEXT PRIMARY KEY,
    status       TEXT NOT NULL DEFAULT 'pending',
    force        INTEGER NOT NULL DEFAULT 0,
    requested_at TEXT NOT NULL,
    claimed_at   TEXT,
    done_at      TEXT
);

-- The master CV's saved versions (private_dir/cv-history/<file_name>): each row is a state of cv.md
-- and the note says what changed into it. Files and notes never go into git.
CREATE TABLE IF NOT EXISTS cv_versions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,  -- shown as vN
    file_name  TEXT NOT NULL UNIQUE,               -- cv-YYYYMMDD-HHMMSS[-N].md
    note       TEXT NOT NULL,
    sha256     TEXT NOT NULL,                      -- of the file's text, to tell which version is current
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_applications_job ON applications(getjo_job_id);
CREATE INDEX IF NOT EXISTS idx_status_history_app ON status_history(application_id);
CREATE INDEX IF NOT EXISTS idx_tasks_app ON tasks(application_id);
CREATE INDEX IF NOT EXISTS idx_comments_app ON application_comments(application_id);
CREATE INDEX IF NOT EXISTS idx_messages_app ON application_messages(application_id);
CREATE INDEX IF NOT EXISTS idx_agent_messages_app ON agent_messages(application_id);
CREATE INDEX IF NOT EXISTS idx_targets_message ON agent_message_targets(message_id);
CREATE INDEX IF NOT EXISTS idx_targets_app ON agent_message_targets(application_id);
"""

# Columns added after a table was first created: {table: {column: definition}}.
# CREATE TABLE IF NOT EXISTS skips existing tables, so these are added explicitly.
MIGRATIONS: dict[str, dict[str, str]] = {
    "status_history": {"comment": "TEXT"},  # the comment given with a stage change
    "tasks": {"stage": "TEXT"},             # the stage whose STAGE_TASKS created it; NULL if hand-made
    # application_id: e.g. a gap found by a CV fit; session_id: the practice session that first named it.
    "gaps": {"application_id": "INTEGER REFERENCES applications(id)", "session_id": "INTEGER REFERENCES sessions(id)"},
    # cv_sent: the file name of the tailored CV version the user says was sent (`cli cv sent`), with its
    # version and when that was recorded; NULL until then. Older rows may hold a frozen copy's path.
    # role_short: the short role name in tailored CV file names (FDE, AIEng), chosen once by the agent.
    "applications": {"cv_sent": "TEXT", "cv_sent_version": "INTEGER", "cv_sent_at": "TEXT", "role_short": "TEXT"},
    # A message about a getjo job not yet tracked (an offer panel's "About" tick); the agent's reply
    # carries it too. Interested moves these messages to the new application and keeps the id.
    # topic: "cv" for a message about the master CV (no application or job); the reply carries it.
    "agent_messages": {"getjo_job_id": "TEXT", "topic": "TEXT"},
    # summary: the AI check's scores (JSON: fit, master fit, skim and why, claim and must-have counts);
    # NULL until the AI check of that version is saved.
    "tailored_cv_checks": {"summary": "TEXT"},
}

APPLICATION_FIELDS = (
    "getjo_job_id", "company", "role", "link", "tier", "type", "source", "date_applied",
    "status", "last_contact", "next_action", "next_action_date", "puzzle_round", "pay",
    "contact", "notes", "role_short",
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
    _backfill_tailored_versions(conn)
    _copy_application_messages(conn)
    _backfill_message_targets(conn)
    conn.commit()


def _copy_application_messages(conn: sqlite3.Connection) -> None:
    """Messages from the per-application threads move into agent_messages with the same ids, so
    reply_to still points at the right message. Rows already copied are skipped."""
    conn.execute(
        "INSERT OR IGNORE INTO agent_messages (id, application_id, author, body, status, reply_to, created_at, "
        "claimed_at, answered_at) SELECT id, application_id, author, body, status, reply_to, created_at, claimed_at, "
        "answered_at FROM application_messages"
    )


def _backfill_message_targets(conn: sqlite3.Connection) -> None:
    """A message from before agent_message_targets gets one target row from its own columns (an
    application with its getjo job id, a job, or a topic); general messages get none."""
    conn.execute(
        "INSERT INTO agent_message_targets (message_id, application_id, getjo_job_id, topic) "
        "SELECT m.id, m.application_id, COALESCE(m.getjo_job_id, a.getjo_job_id), m.topic "
        "FROM agent_messages m LEFT JOIN applications a ON a.id = m.application_id "
        "WHERE (m.application_id IS NOT NULL OR m.getjo_job_id IS NOT NULL OR m.topic IS NOT NULL) "
        "AND COALESCE(m.topic, '') != ? "
        "AND NOT EXISTS (SELECT 1 FROM agent_message_targets t WHERE t.message_id = m.id)",
        (COMPARE,))


def _backfill_tailored_versions(conn: sqlite3.Connection) -> None:
    """A tailored CV saved before versions were kept becomes its own version row (file not renamed)."""
    conn.execute(
        "INSERT INTO tailored_cv_versions (application_id, version, file_path, created_at) "
        "SELECT t.application_id, t.version, t.file_path, t.updated_at FROM tailored_cvs t "
        "WHERE NOT EXISTS (SELECT 1 FROM tailored_cv_versions v WHERE v.application_id = t.application_id)"
    )


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
        WHERE next_action IS NOT NULL AND status NOT IN ('rejected', 'withdrawn', 'no_response', 'skipped')
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
        _adopt_job_messages(conn, app_id, fields.get("getjo_job_id"))
        stage_tasks(conn, app_id, fields["status"], today)
        return app_id, True
    new_status = fields.pop("status")
    update_application(conn, existing["id"], **{k: v for k, v in fields.items() if v is not None})
    _adopt_job_messages(conn, existing["id"], existing["getjo_job_id"] or fields.get("getjo_job_id"))
    move_application(conn, existing["id"], new_status, today)
    return existing["id"], False


def _adopt_job_messages(conn: sqlite3.Connection, app_id: int, getjo_job_id: str | None) -> None:
    """Messages asked about the job before it was tracked now belong to its application (they keep
    the job id, so Undo Interested can hand them back)."""
    if getjo_job_id:
        conn.execute("UPDATE agent_messages SET application_id = ? WHERE getjo_job_id = ? AND application_id IS NULL",
                     (app_id, getjo_job_id))
        conn.execute("UPDATE agent_message_targets SET application_id = ? WHERE getjo_job_id = ? AND application_id IS NULL",
                     (app_id, getjo_job_id))
        conn.commit()


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
    sent = status not in ("to_apply", "withdrawn", "skipped") and not row["date_applied"]
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


def undo_applied(conn: sqlite3.Connection, app_id: int) -> None:
    """Undo Mark applied: back to Interested, as if the move never happened. Only right after it
    (status applied, its latest history row applied straight from to_apply): the applied history
    row and the Applied stage's open tasks are deleted, the apply tasks the move marked done reopen,
    and date_applied is cleared unless the application had been sent before. One transaction."""
    app = get_application(conn, app_id)
    if app is None:
        raise LookupError(f"no application with id {app_id}")
    rows = conn.execute("SELECT id, status, changed_at FROM status_history WHERE application_id = ? "
                        "ORDER BY id DESC", (app_id,)).fetchall()
    if app["status"] != "applied" or len(rows) < 2 or rows[0]["status"] != "applied" or rows[1]["status"] != "to_apply":
        raise ValueError("It has moved on since; ask the agent to change the stage.")
    sent_before = any(r["status"] not in ("to_apply", "withdrawn", "skipped") for r in rows[1:])
    moved_at, back_at = rows[0]["changed_at"], rows[1]["changed_at"]
    with conn:
        cursor = conn.execute(
            "UPDATE applications SET status = 'to_apply', updated_at = ?, date_applied = "
            "CASE WHEN ? THEN date_applied END WHERE id = ? AND status = 'applied'",
            (now_iso(), sent_before, app_id))
        if cursor.rowcount != 1:
            raise ValueError("It has moved on since; ask the agent to change the stage.")
        conn.execute("DELETE FROM status_history WHERE id = ?", (rows[0]["id"],))
        conn.execute("DELETE FROM tasks WHERE application_id = ? AND stage = 'applied' AND status = 'todo'", (app_id,))
        conn.execute("UPDATE tasks SET status = 'todo', done_at = NULL WHERE application_id = ? AND stage = 'to_apply' "
                     "AND kind = 'apply' AND status = 'done' AND done_at >= ?", (app_id, moved_at))
        conn.execute("UPDATE tasks SET status = 'todo' WHERE application_id = ? AND stage = 'to_apply' "
                     "AND status = 'closed' AND created_at >= ?", (app_id, back_at))
    sync_next_action(conn, app_id)


def skip_application(conn: sqlite3.Connection, app_id: int, reason: str, comment: str | None = None,
                     today: str | None = None) -> None:
    """Skip an Interested application (the panel's Skip, `cli skip`): move it to skipped with the
    comment "skipped: <reason>[ — comment]", close its open tasks and agent requests, and for a
    getjo job mark its decision skipped with the reason and comment (as Job Offers' Skipped), so
    it shows under Job Offers → Skipped."""
    if not reason:
        raise ValueError("a skip needs a reason")
    _check("reason", reason, SKIP_REASONS)
    comment = (comment or "").strip() or None
    app = get_application(conn, app_id)
    if app is None:
        raise LookupError(f"no application with id {app_id}")
    if app["status"] != "to_apply":
        raise ValueError("Only an Interested application can be skipped; ask the agent to withdraw it.")
    text = f"skipped: {reason.replace('_', ' ')}" + (f" — {comment}" if comment else "")
    if not move_application(conn, app_id, "skipped", today, text, only_from="to_apply"):
        raise ValueError("It has moved on since; reload to see its stage.")
    stamp = now_iso()
    with conn:
        conn.execute("UPDATE tasks SET status = 'closed' WHERE application_id = ? AND status = 'todo'", (app_id,))
        conn.execute("UPDATE agent_requests SET status = 'done', done_at = ? WHERE application_id = ? "
                     "AND status IN ('pending', 'claimed')", (stamp, app_id))
        if app["getjo_job_id"]:
            cursor = conn.execute(
                "UPDATE job_decisions SET decision = 'skipped', reason = ?, comment = ?, decided_at = ? "
                "WHERE getjo_job_id = ?", (reason, comment, stamp, app["getjo_job_id"]))
            if cursor.rowcount == 0:
                conn.execute(
                    "INSERT INTO job_decisions (getjo_job_id, decision, reason, comment, decided_at, company, title, "
                    "url, source) VALUES (?, 'skipped', ?, ?, ?, ?, ?, ?, ?)",
                    (app["getjo_job_id"], reason, comment, stamp, app["company"], app["role"], app["link"], app["source"]))
    sync_next_action(conn, app_id)


def stage_tasks(conn: sqlite3.Connection, app_id: int, stage: str, today: str | None = None,
                interview_date: str | None = None) -> list[int]:
    """Set up the tasks for an application entering `stage` and return the new task ids.

    Entering applied completes the open Interested apply tasks (Apply, Review research and apply). Open auto tasks from
    other stages are closed (status closed, not done). Then the stage's STAGE_TASKS are added,
    skipping a title the application already has open. For INTERVIEW_STAGES with an
    interview date, they are due the day before it (today if that has passed). Entering to_apply
    (Interested) also queues company research and a CV fit for the agent (queue_request).
    """
    today = today or today_iso()
    if stage == "applied":
        conn.execute(
            "UPDATE tasks SET status = 'done', done_at = ? WHERE application_id = ? AND kind = 'apply' "
            "AND stage = 'to_apply' AND status = 'todo'",
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
    if stage == "to_apply":  # the agent researches the company and checks the CV fit (/research)
        queue_request(conn, app_id, "research")  # cheap when the profile is fresh: it is reused
        if cv_fit(conn, app_id) is None:
            queue_request(conn, app_id, "cv_fit")
    return ids


def _stage_due(today: str, days: int | None, stage: str, interview_date: str | None) -> str | None:
    if interview_date and stage in INTERVIEW_STAGES:
        return max(today, (date.fromisoformat(interview_date) - timedelta(days=1)).isoformat())
    return None if days is None else (date.fromisoformat(today) + timedelta(days=days)).isoformat()


def set_interview_date(conn: sqlite3.Connection, app_id: int, stage: str, day: str,
                       today: str | None = None) -> tuple[int, str | None]:
    """Store a (new) interview date for the stage the application is in. When it differs from the
    stage's latest date, its open auto tasks move to the day before it (today if that has passed).
    Returns (tasks moved, their new due date or None)."""
    today = today or today_iso()
    _check("stage", stage, INTERVIEW_STAGES)
    _check_date("interview_date", day)
    latest = conn.execute(
        "SELECT date FROM interviews WHERE application_id = ? AND stage = ? ORDER BY id DESC LIMIT 1",
        (app_id, stage),
    ).fetchone()
    add_interview(conn, app_id, day, stage)
    if latest is not None and latest[0] == day:
        return 0, None
    due = _stage_due(today, None, stage, day)
    moved = conn.execute(
        "UPDATE tasks SET due_date = ? WHERE application_id = ? AND stage = ? AND status = 'todo'",
        (due, app_id, stage),
    ).rowcount
    conn.commit()
    sync_next_action(conn, app_id)
    return moved, due


def review_task(conn: sqlite3.Connection, app_id: int, today: str | None = None) -> int | None:
    """Add REVIEW_TASK to an Interested application once its company research and CV fit are both
    saved, unless one is already open (a Redo adds no second one). Returns the new task id."""
    app = get_application(conn, app_id)
    if app is None or app["status"] != "to_apply":
        return None
    if company_profile(conn, company_slug(app["company"])) is None or cv_fit(conn, app_id) is None:
        return None
    title, kind, days = REVIEW_TASK
    if conn.execute("SELECT 1 FROM tasks WHERE application_id = ? AND title = ? AND status = 'todo'",
                    (app_id, title)).fetchone():
        return None
    return add_task(conn, title, kind, _stage_due(today or today_iso(), days, "to_apply", None), app_id,
                    stage="to_apply")


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


_COMMENT_REF = re.compile(r"^([ch])(\d+)$")


def comments(conn: sqlite3.Connection, app_id: int | None = None) -> list[dict]:
    """Comments, newest first: free comments (ref "c<id>") and comments given with a stage change
    (ref "h<id>", its status_history row). {ref, application_id, at, body, status (None for free)}."""
    where = "WHERE application_id = ?" if app_id is not None else ""
    rows = conn.execute(
        f"""
        SELECT * FROM (
            SELECT 'h' || id AS ref, application_id, changed_at AS at, comment AS body, status, 0 AS kind, id
            FROM status_history WHERE comment IS NOT NULL
            UNION ALL
            SELECT 'c' || id, application_id, created_at, body, NULL, 1, id FROM application_comments
        ) {where} ORDER BY at DESC, kind DESC, id DESC
        """,
        (app_id,) if app_id is not None else (),
    )
    return [{k: r[k] for k in ("ref", "application_id", "at", "body", "status")} for r in rows]


def latest_comments(conn: sqlite3.Connection) -> dict[int, tuple[str, int]]:
    """(latest comment, number of comments) by application id, for the pipeline list."""
    found: dict[int, tuple[str, int]] = {}
    for c in comments(conn):
        body, n = found.get(c["application_id"], (c["body"], 0))
        found[c["application_id"]] = (body, n + 1)
    return found


def _comment_ref(ref: str) -> tuple[str, int]:
    match = _COMMENT_REF.match(ref.strip())
    if not match:
        raise ValueError(f"a comment id looks like c12 or h34 (from `comments list`); got {ref!r}")
    return ("application_comments", "body") if match[1] == "c" else ("status_history", "comment"), int(match[2])


def amend_comment(conn: sqlite3.Connection, ref: str, body: str) -> None:
    """Replace a comment's text (ref from comments())."""
    if not body.strip():
        raise ValueError("a comment needs some text")
    (table, column), row_id = _comment_ref(ref)
    cursor = conn.execute(f"UPDATE {table} SET {column} = ? WHERE id = ? AND {column} IS NOT NULL",
                          (body.strip(), row_id))
    conn.commit()
    if cursor.rowcount != 1:
        raise LookupError(f"no comment {ref}")


def remove_comment(conn: sqlite3.Connection, ref: str) -> None:
    """Delete a free comment, or clear the comment of a stage change (the change itself stays)."""
    (table, column), row_id = _comment_ref(ref)
    if table == "application_comments":
        cursor = conn.execute("DELETE FROM application_comments WHERE id = ?", (row_id,))
    else:
        cursor = conn.execute("UPDATE status_history SET comment = NULL WHERE id = ? AND comment IS NOT NULL", (row_id,))
    conn.commit()
    if cursor.rowcount != 1:
        raise LookupError(f"no comment {ref}")


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


def app_ids_by_job(conn: sqlite3.Connection) -> dict[str, int]:
    """getjo job id -> its application id (the oldest one, as find_by_job)."""
    rows = conn.execute("SELECT getjo_job_id, MIN(id) FROM applications WHERE getjo_job_id IS NOT NULL "
                        "GROUP BY getjo_job_id")
    return {r[0]: r[1] for r in rows}


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


def undo_skip(conn: sqlite3.Connection, getjo_job_id: str, today: str | None = None) -> None:
    """Remove a skip so the job is new again. A job whose application was skipped from its panel
    goes back to Interested instead (in the same transaction; it would read as applied otherwise).
    Applied decisions are not undone here."""
    row = conn.execute(
        "SELECT decision FROM job_decisions WHERE getjo_job_id = ?", (getjo_job_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"no decision for job {getjo_job_id}")
    if row["decision"] != "skipped":
        raise ValueError("only a skip can be undone")
    app = find_by_job(conn, getjo_job_id)
    reopen = app is not None and app["status"] == "skipped"
    stamp = now_iso()
    with conn:
        conn.execute("DELETE FROM job_decisions WHERE getjo_job_id = ?", (getjo_job_id,))
        if reopen:
            conn.execute("UPDATE applications SET status = 'to_apply', updated_at = ? WHERE id = ?", (stamp, app["id"]))
            _add_history(conn, app["id"], "to_apply", stamp, "skip undone")
    if reopen:
        stage_tasks(conn, app["id"], "to_apply", today)  # research is reused while fresh


def undo_interested(conn: sqlite3.Connection, getjo_job_id: str) -> int:
    """Undo Interested on a job: delete the decision and the to_apply application it created, with
    its queued requests, so the job is new again. Only while nothing else has happened to it: still
    to_apply with one history row, no tasks, comments, agent messages, interviews, gaps, sessions, CV fit or tier, and no
    request taken by the agent. Messages asked about the job before Interested (they carry its id) do
    not count: they go back to the job. Raises ValueError otherwise. Returns the deleted application id."""
    app = find_by_job(conn, getjo_job_id)
    if app is None:
        raise LookupError(f"no application for job {getjo_job_id}")
    if app["status"] != "to_apply" or app["tier"]:
        raise ValueError("It has moved on since; withdraw it from its card instead.")
    app_id = app["id"]
    used = conn.execute(
        """
        SELECT (SELECT COUNT(*) FROM status_history WHERE application_id = :id) != 1
            OR EXISTS (SELECT 1 FROM tasks WHERE application_id = :id)
            OR EXISTS (SELECT 1 FROM application_comments WHERE application_id = :id)
            OR EXISTS (SELECT 1 FROM agent_messages WHERE application_id = :id
                       AND (getjo_job_id IS NULL OR getjo_job_id != :job))
            OR EXISTS (SELECT 1 FROM interviews WHERE application_id = :id)
            OR EXISTS (SELECT 1 FROM gaps WHERE application_id = :id)
            OR EXISTS (SELECT 1 FROM sessions WHERE application_id = :id)
            OR EXISTS (SELECT 1 FROM cv_fits WHERE application_id = :id)
            OR EXISTS (SELECT 1 FROM tailored_cvs WHERE application_id = :id)
            OR EXISTS (SELECT 1 FROM agent_requests WHERE application_id = :id AND status != 'pending')
        """,
        {"id": app_id, "job": getjo_job_id},
    ).fetchone()[0]
    if used:
        raise ValueError("It has moved on since; withdraw it from its card instead.")
    with conn:  # one transaction: all or nothing
        conn.execute("UPDATE agent_messages SET application_id = NULL WHERE application_id = ?", (app_id,))
        # every target row of an application with a getjo job carries the job id: back to the job
        conn.execute("UPDATE agent_message_targets SET application_id = NULL, getjo_job_id = :job "
                     "WHERE application_id = :id", {"id": app_id, "job": getjo_job_id})
        for table in ("agent_requests", "status_history"):
            conn.execute(f"DELETE FROM {table} WHERE application_id = ?", (app_id,))
        conn.execute("DELETE FROM applications WHERE id = ?", (app_id,))
        conn.execute("DELETE FROM job_decisions WHERE getjo_job_id = ?", (getjo_job_id,))
    return app_id


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


def edit_task(conn: sqlite3.Connection, task_id: int, title: str | None = None, due_date: str | None = None,
              clear_due: bool = False) -> None:
    """Change a task's title and/or due date (clear_due: no date)."""
    task = get_task(conn, task_id)
    if task is None:
        raise LookupError(f"no task with id {task_id}")
    if title is not None and not title.strip():
        raise ValueError("a task needs a title")
    _check_date("due_date", due_date)
    conn.execute("UPDATE tasks SET title = ?, due_date = ? WHERE id = ?",
                 ((title or task["title"]).strip(), None if clear_due else (due_date or task["due_date"]), task_id))
    conn.commit()
    _after_task_change(conn, task_id)


def delete_task(conn: sqlite3.Connection, task_id: int) -> dict:
    """Delete a task for good; returns it."""
    task = get_task(conn, task_id)
    if task is None:
        raise LookupError(f"no task with id {task_id}")
    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    if task["application_id"] is not None:
        sync_next_action(conn, task["application_id"])
    return task


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


# Why a task is where it is in review_tasks(), in that order.
REVIEW_GROUPS = ("overdue", "today", "interview prep", "ready to apply", "upcoming", "no date")


def review_tasks(conn: sqlite3.Connection, today: str | None = None) -> list[dict]:
    """Every open task in the order the user should look at them, each with its application's
    company, role and status (None for a task with no application) and `group` (REVIEW_GROUPS):
    overdue, due today, then prep for the interview stages (nearest interview first), then tasks
    to apply (Review research and apply, apply kind), then the rest by due date, then undated.
    The same order every time, so the agent's answer is stable."""
    today = today or today_iso()
    rows = conn.execute(
        """
        SELECT t.*, a.company, a.role, a.status AS app_status,
               (SELECT MAX(i.date) FROM interviews i WHERE i.application_id = t.application_id
                AND i.stage = t.stage) AS interview_date
        FROM tasks t LEFT JOIN applications a ON a.id = t.application_id
        WHERE t.status = 'todo'
        """
    ).fetchall()

    def group(t: dict) -> str:
        if t["due_date"] and t["due_date"] < today:
            return "overdue"
        if t["due_date"] == today:
            return "today"
        if t["stage"] in INTERVIEW_STAGES:
            return "interview prep"
        if t["kind"] == "apply":
            return "ready to apply"
        return "upcoming" if t["due_date"] else "no date"

    found = []
    for r in rows:
        t = dict(r)
        t["group"] = group(t)
        found.append(t)
    found.sort(key=lambda t: (REVIEW_GROUPS.index(t["group"]), t["interview_date"] or t["due_date"] or "9999",
                              t["due_date"] or "9999", t["id"]))
    return found


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


def add_gap(conn: sqlite3.Connection, gap: str, day: str | None = None, session: str | None = None,
            application_id: int | None = None, session_id: int | None = None) -> int:
    day = day or today_iso()
    _check_date("date", day)
    cursor = conn.execute("INSERT INTO gaps (date, session, gap, application_id, session_id) VALUES (?, ?, ?, ?, ?)",
                          (day, session, gap, application_id, session_id))
    conn.commit()
    return cursor.lastrowid


def _gap_key(text: str) -> str:
    return " ".join(text.split()).casefold()


def add_open_gaps(conn: sqlite3.Connection, gaps: list[str], session: str, application_id: int | None = None,
                  day: str | None = None) -> list[int]:
    """Add each gap unless an open gap with the same text (ignoring case and spacing) exists.
    Returns the new gap ids."""
    have = {_gap_key(r[0]) for r in conn.execute("SELECT gap FROM gaps WHERE status = 'open'")}
    ids = []
    for gap in gaps:
        key = _gap_key(gap)
        if key and key not in have:
            have.add(key)
            ids.append(add_gap(conn, " ".join(gap.split()), day, session, application_id))
    return ids


def close_gap(conn: sqlite3.Connection, gap_id: int) -> list[int]:
    """Close a gap and mark its open tasks (its drill) done. Closing a closed gap changes nothing.
    Returns the ids of the tasks completed."""
    if conn.execute("SELECT 1 FROM gaps WHERE id = ?", (gap_id,)).fetchone() is None:
        raise LookupError(f"no gap with id {gap_id}")
    tasks = conn.execute("SELECT id, application_id FROM tasks WHERE gap_id = ? AND status = 'todo'",
                         (gap_id,)).fetchall()
    conn.execute("UPDATE gaps SET status = 'closed' WHERE id = ?", (gap_id,))
    conn.execute("UPDATE tasks SET status = 'done', done_at = ? WHERE gap_id = ? AND status = 'todo'",
                 (now_iso(), gap_id))
    conn.commit()
    for app_id in {t["application_id"] for t in tasks if t["application_id"] is not None}:
        sync_next_action(conn, app_id)
    return [t["id"] for t in tasks]


def list_gaps(conn: sqlite3.Connection, status: str | None = "open") -> list[dict]:
    """Gaps with the given status (all when None), oldest first, each with `drill_open`: its open tasks."""
    _check("status", status, GAP_STATUSES)
    where = "WHERE g.status = ?" if status else ""
    rows = conn.execute(
        f"""
        SELECT g.*, (SELECT COUNT(*) FROM tasks t WHERE t.gap_id = g.id AND t.status = 'todo') AS drill_open
        FROM gaps g {where} ORDER BY g.id
        """,
        (status,) if status else (),
    )
    return [dict(r) for r in rows]


def add_gap_drills(conn: sqlite3.Connection, gaps: list[str], session_id: int | None = None,
                   application_id: int | None = None, due_date: str | None = None,
                   day: str | None = None) -> dict:
    """Record the weaknesses a session named: each becomes a new open gap, or matches an open gap with
    the same text (ignoring case and spacing), such as one from a CV fit. A new gap points at the
    session; a matched one gets the session only when it has none. Each named gap without an open task
    gets one drill task (DRILL_TITLE, kind drill, due_date). With a session, the application defaults
    to the session's and the gap's `session` label is its kind.
    Returns {"gaps": ids, one per distinct weakness named, "new_gaps": ids created, "tasks": drill task ids}."""
    day = day or today_iso()
    _check_date("date", day)
    _check_date("due_date", due_date)
    label = None
    if session_id is not None:
        session = get_session(conn, session_id)
        if session is None:
            raise LookupError(f"no session with id {session_id}")
        label = session["kind"]
        application_id = application_id if application_id is not None else session["application_id"]
    if application_id is not None and get_application(conn, application_id) is None:
        raise LookupError(f"no application with id {application_id}")
    open_gaps = {_gap_key(r["gap"]): dict(r) for r in conn.execute(
        "SELECT id, gap, application_id, session_id FROM gaps WHERE status = 'open' ORDER BY id DESC")}
    found = {"gaps": [], "new_gaps": [], "tasks": []}
    for text in gaps:
        key = _gap_key(text)
        if not key:
            continue
        gap = open_gaps.get(key)
        if gap is None:
            gap_id = add_gap(conn, " ".join(text.split()), day, label, application_id, session_id)
            gap = open_gaps[key] = {"id": gap_id, "application_id": application_id, "session_id": session_id}
            found["new_gaps"].append(gap_id)
        elif gap["id"] in found["gaps"]:
            continue
        elif session_id is not None and gap["session_id"] is None:
            conn.execute("UPDATE gaps SET session_id = ? WHERE id = ?", (session_id, gap["id"]))
            conn.commit()
            gap["session_id"] = session_id
        found["gaps"].append(gap["id"])
        if conn.execute("SELECT 1 FROM tasks WHERE gap_id = ? AND status = 'todo'", (gap["id"],)).fetchone() is None:
            found["tasks"].append(add_task(conn, DRILL_TITLE.format(gap=" ".join(text.split())), "drill", due_date,
                                           gap["application_id"] or application_id, gap["id"]))
    return found


# --- practice sessions ---------------------------------------------------------

def _check_scores(scores: dict[str, int] | None) -> dict[str, int]:
    checked = {}
    for area, score in (scores or {}).items():
        area = " ".join(str(area).split())
        if not area:
            raise ValueError("a score needs an area name")
        if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
            raise ValueError(f"the score for {area!r} must be a whole number from 1 to 5; got {score!r}")
        checked[area] = score
    return checked


def record_session(conn: sqlite3.Connection, kind: str, day: str | None = None, application_id: int | None = None,
                   stage: str | None = None, scores: dict[str, int] | None = None, file_path: str | None = None,
                   gaps: list[str] | None = None, due_date: str | None = None) -> dict:
    """Record a mock or a real interview: the session row, then its gaps with one drill task per gap
    without one (add_gap_drills). Everything is checked before the first write, so a bad score or stage
    records nothing. Returns add_gap_drills' result with "session_id"."""
    day = day or today_iso()
    _check("kind", kind, SESSION_KINDS)
    if kind is None:
        raise ValueError("a session needs a kind")
    _check("stage", stage, INTERVIEW_STAGES)
    _check_date("date", day)
    _check_date("due_date", due_date)
    checked = _check_scores(scores)
    if application_id is not None and get_application(conn, application_id) is None:
        raise LookupError(f"no application with id {application_id}")
    cursor = conn.execute(
        "INSERT INTO sessions (date, kind, application_id, stage, scores, file_path, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (day, kind, application_id, stage, json.dumps(checked), file_path, now_iso()),
    )
    conn.commit()
    found = add_gap_drills(conn, gaps or [], cursor.lastrowid, application_id, due_date, day)
    return {"session_id": cursor.lastrowid, **found}



def record_debrief(conn: sqlite3.Connection, kind: str, application_id: int | None = None, day: str | None = None,
                   stage: str | None = None, questions: str | None = None, went_well: str | None = None,
                   to_improve: str | None = None, scores: dict[str, int] | None = None,
                   file_path: str | None = None, gaps: list[str] | None = None,
                   due_date: str | None = None) -> dict:
    """Record a debrief: for a real interview, its interviews row (questions, went well, to improve),
    then the session with its gaps and drill tasks (record_session), then the application's open
    DEBRIEF_TASK is completed. The interviews row /stage stored for the same stage and date is filled
    rather than duplicated; no task dates move. A mock (done elsewhere) has no interviews row: its notes
    stay in the transcript file. The stage defaults to the application's when that is an interview
    stage. Everything is checked before the first write. Returns record_session's result with
    "interview_id" (None for a mock) and "debrief_tasks" (ids completed)."""
    day = day or today_iso()
    _check("kind", kind, SESSION_KINDS)
    _check_date("date", day)
    _check_date("due_date", due_date)
    _check_scores(scores)
    app = None
    if application_id is not None:
        app = get_application(conn, application_id)
        if app is None:
            raise LookupError(f"no application with id {application_id}")
    if kind == "real" and app is None:
        raise ValueError("a real interview debrief needs its application")
    if stage is None and app is not None and app["status"] in INTERVIEW_STAGES:
        stage = app["status"]
    _check("stage", stage, INTERVIEW_STAGES)
    interview_id = None
    if kind == "real":
        row = conn.execute(
            """
            SELECT id FROM interviews WHERE application_id = ? AND stage IS ? AND date = ?
            AND questions IS NULL AND went_well IS NULL AND to_improve IS NULL ORDER BY id DESC LIMIT 1
            """,
            (application_id, stage, day),
        ).fetchone()
        if row is None:
            interview_id = add_interview(conn, application_id, day, stage, questions, went_well, to_improve)
        else:
            interview_id = row["id"]
            conn.execute("UPDATE interviews SET questions = ?, went_well = ?, to_improve = ? WHERE id = ?",
                         (questions, went_well, to_improve, interview_id))
            conn.commit()
    found = record_session(conn, kind, day, application_id, stage, scores, file_path, gaps, due_date)
    debrief_tasks = []
    if application_id is not None:
        debrief_tasks = [r["id"] for r in conn.execute(
            "SELECT id FROM tasks WHERE application_id = ? AND title = ? AND status = 'todo'",
            (application_id, DEBRIEF_TASK))]
        for task_id in debrief_tasks:
            complete_task(conn, task_id)
    return {**found, "interview_id": interview_id, "debrief_tasks": debrief_tasks}


def get_session(conn: sqlite3.Connection, session_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["scores"] = json.loads(d["scores"] or "{}")
    return d


def list_sessions(conn: sqlite3.Connection) -> list[dict]:
    """Every session, newest first, with its application's company (None without one)."""
    rows = conn.execute(
        """
        SELECT s.*, a.company FROM sessions s LEFT JOIN applications a ON a.id = s.application_id
        ORDER BY s.date DESC, s.id DESC
        """
    )
    found = []
    for r in rows:
        d = dict(r)
        d["scores"] = json.loads(d["scores"] or "{}")
        found.append(d)
    return found


# --- agent research queue and results ---------------------------------------------

_COMPANY_SUFFIXES = {"ltd", "limited", "inc", "plc", "llc", "gmbh", "llp", "corp", "co"}


def company_slug(company: str) -> str:
    """A stable key for a company: lowercase words joined by "-", legal suffixes dropped
    ("Acme Rockets Ltd." -> "acme-rockets"). Profile files and company notes use it."""
    words = re.findall(r"[a-z0-9]+", company.lower())
    while len(words) > 1 and words[-1] in _COMPANY_SUFFIXES:
        words.pop()
    return "-".join(words) or "company"


def _claim_cutoff() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=CLAIM_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")


def queue_request(conn: sqlite3.Connection, app_id: int, kind: str, force: bool = False) -> tuple[int, bool]:
    """Queue research or a CV fit for an application; returns (request id, newly queued).

    An open (pending or claimed) request of the same kind is reused, so a second click changes
    nothing; `force` (Redo) is then added to it. The unique index settles two racing inserts."""
    _check("kind", kind, REQUEST_KINDS)
    if get_application(conn, app_id) is None:
        raise LookupError(f"no application with id {app_id}")
    if kind == "cv_eval" and tailored_cv(conn, app_id) is None:
        raise LookupError(f"application {app_id} has no tailored CV to check")
    try:
        cursor = conn.execute(
            "INSERT INTO agent_requests (application_id, kind, force, requested_at) VALUES (?, ?, ?, ?)",
            (app_id, kind, int(force), now_iso()),
        )
        conn.commit()
        return cursor.lastrowid, True
    except sqlite3.IntegrityError:
        conn.rollback()
    row = conn.execute(
        "SELECT id FROM agent_requests WHERE application_id = ? AND kind = ? AND status IN ('pending', 'claimed')",
        (app_id, kind),
    ).fetchone()
    if force:
        conn.execute("UPDATE agent_requests SET force = 1 WHERE id = ?", (row["id"],))
        conn.commit()
    return row["id"], False


def last_request_id(conn: sqlite3.Connection) -> int:
    """The highest agent_requests id (0 if none): callers compare it before and after an action."""
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM agent_requests").fetchone()[0]


def requests_since(conn: sqlite3.Connection, after_id: int) -> list[dict]:
    """Requests queued after `after_id` that are still open, oldest first: id, application_id, kind."""
    rows = conn.execute(
        "SELECT id, application_id, kind FROM agent_requests WHERE id > ? AND status != 'done' ORDER BY id",
        (after_id,),
    )
    return [dict(r) for r in rows]


def open_requests(conn: sqlite3.Connection, app_id: int | None = None) -> list[dict]:
    """Requests waiting for the agent (pending, or claimed longer than CLAIM_HOURS ago), oldest
    first, with the application's company and role. `claimed` is True for a live claim."""
    where = "WHERE r.status IN ('pending', 'claimed')" + (" AND r.application_id = ?" if app_id is not None else "")
    rows = conn.execute(
        f"""
        SELECT r.id, r.application_id, r.kind, r.status, r.force, r.requested_at, r.claimed_at,
               a.company, a.role
        FROM agent_requests r JOIN applications a ON a.id = r.application_id
        {where} ORDER BY r.id
        """,
        (app_id,) if app_id is not None else (),
    )
    cutoff = _claim_cutoff()
    found = []
    for r in rows:
        d = dict(r)
        d["claimed"] = d["status"] == "claimed" and (d["claimed_at"] or "") >= cutoff
        found.append(d)
    return found


def claim_request(conn: sqlite3.Connection, request_id: int) -> bool:
    """Take a request for this session. Only one caller wins: the update applies while the
    request is still pending (or its claim has gone stale)."""
    cursor = conn.execute(
        "UPDATE agent_requests SET status = 'claimed', claimed_at = ? "
        "WHERE id = ? AND (status = 'pending' OR (status = 'claimed' AND claimed_at < ?))",
        (now_iso(), request_id, _claim_cutoff()),
    )
    conn.commit()
    return cursor.rowcount == 1


def finish_requests(conn: sqlite3.Connection, app_ids: list[int], kind: str) -> int:
    """Mark the open requests of `kind` for these applications done; returns how many."""
    _check("kind", kind, REQUEST_KINDS)
    done = 0
    for app_id in app_ids:
        done += conn.execute(
            "UPDATE agent_requests SET status = 'done', done_at = ? WHERE application_id = ? AND kind = ? "
            "AND status IN ('pending', 'claimed')",
            (now_iso(), app_id, kind),
        ).rowcount
    conn.commit()
    return done


def applications_at(conn: sqlite3.Connection, slug: str) -> list[int]:
    """Ids of the applications whose company has this slug."""
    rows = conn.execute("SELECT id, company FROM applications ORDER BY id")
    return [r["id"] for r in rows if company_slug(r["company"]) == slug]


PROFILE_FIELDS = (
    "company", "file_path", "what_it_does", "sector", "employees", "employees_as_of", "size_band",
    "london_remote", "recognition", "recognition_note", "funding", "sources", "suggested_tier",
    "tier_reason", "researched_at",
)


def save_company_profile(conn: sqlite3.Connection, slug: str, profile: dict, today: str | None = None) -> list[int]:
    """Store (or replace) a company's research summary, finish the open research requests of every
    application at that company and add the review task where the CV fit is in too (review_task).
    Returns those application ids."""
    unknown = set(profile) - set(PROFILE_FIELDS)
    if unknown:
        raise ValueError(f"unknown profile fields: {', '.join(sorted(unknown))}")
    for name in ("company", "file_path", "researched_at"):
        if not profile.get(name):
            raise ValueError(f"a company profile needs {name}")
    _check("recognition", profile.get("recognition"), RECOGNITION)
    _check("size_band", profile.get("size_band"), SIZE_BANDS)
    _check("suggested_tier", profile.get("suggested_tier"), TIERS)
    _check_date("researched_at", profile["researched_at"])
    _check_date("employees_as_of", profile.get("employees_as_of"))
    row = {name: profile.get(name) for name in PROFILE_FIELDS}
    row["sources"] = json.dumps(list(profile.get("sources") or []))
    row["slug"] = slug
    names = ", ".join(row)
    conn.execute(f"INSERT OR REPLACE INTO company_profiles ({names}) VALUES ({', '.join(':' + k for k in row)})", row)
    conn.commit()
    app_ids = applications_at(conn, slug)
    finish_requests(conn, app_ids, "research")
    for app_id in app_ids:
        review_task(conn, app_id, today)
    return app_ids


def company_profile(conn: sqlite3.Connection, slug: str) -> dict | None:
    row = conn.execute("SELECT * FROM company_profiles WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["sources"] = json.loads(d["sources"] or "[]")
    return d


def save_cv_fit(conn: sqlite3.Connection, app_id: int, score: int, gaps: list[str], file_path: str,
                day: str | None = None) -> list[int]:
    """Store (or replace) an application's CV fit summary (score 1-5, its first 3 gaps), add its
    gaps to the gaps table (session "cv fit", skipping ones already open), finish its open cv_fit
    request and add the review task when the company research is in too (review_task). Returns
    the new gap ids."""
    day = day or today_iso()
    _check_date("checked_at", day)
    if get_application(conn, app_id) is None:
        raise LookupError(f"no application with id {app_id}")
    if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
        raise ValueError(f"the fit score must be a whole number from 1 to 5; got {score!r}")
    gaps = [g.strip() for g in gaps if isinstance(g, str) and g.strip()]
    conn.execute(
        "INSERT OR REPLACE INTO cv_fits (application_id, score, top_gaps, file_path, checked_at) VALUES (?, ?, ?, ?, ?)",
        (app_id, score, json.dumps(gaps[:3]), file_path, day),
    )
    conn.commit()
    new = add_open_gaps(conn, gaps, FIT_GAP_SESSION, app_id, day)
    finish_requests(conn, [app_id], "cv_fit")
    review_task(conn, app_id)  # due from today, not from the file's checked_at
    return new


def cv_fit(conn: sqlite3.Connection, app_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM cv_fits WHERE application_id = ?", (app_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["top_gaps"] = json.loads(d["top_gaps"] or "[]")
    return d


def save_tailored_cv(conn: sqlite3.Connection, app_id: int, file_path: str, note: str,
                     reply_to: int | None = None) -> int:
    """Record a saved tailored CV for an application: version + 1 (a new tailored_cv_versions row; each
    version is its own file and is kept), its open tailor request done, an AI check (cv_eval) queued
    unless one is open already (it always checks the latest version), and
    `note` (what was done or changed, and any questions) posted in the thread as an agent message,
    all in one transaction. With `reply_to` (a revision asked for in the thread) the note answers
    that user message, as reply_message does. Returns the new version."""
    note = _message_body(note)
    if get_application(conn, app_id) is None:
        raise LookupError(f"no application with id {app_id}")
    if reply_to is not None:
        _check_user_message(conn, app_id, reply_to)
    stamp = now_iso()
    with conn:
        row = conn.execute("SELECT version FROM tailored_cvs WHERE application_id = ?", (app_id,)).fetchone()
        version = (row["version"] if row else 0) + 1
        conn.execute(
            "INSERT OR REPLACE INTO tailored_cvs (application_id, file_path, version, updated_at) VALUES (?, ?, ?, ?)",
            (app_id, file_path, version, stamp),
        )
        conn.execute("INSERT INTO tailored_cv_versions (application_id, version, file_path, created_at) VALUES (?, ?, ?, ?)",
                     (app_id, version, file_path, stamp))
        conn.execute(
            "UPDATE agent_requests SET status = 'done', done_at = ? WHERE application_id = ? AND kind = 'tailor' "
            "AND status IN ('pending', 'claimed')",
            (stamp, app_id),
        )
        conn.execute("INSERT OR IGNORE INTO agent_requests (application_id, kind, requested_at) VALUES (?, 'cv_eval', ?)",
                     (app_id, stamp))
        _agent_message(conn, app_id, note, stamp, reply_to)
    return version


def tailored_cv(conn: sqlite3.Connection, app_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM tailored_cvs WHERE application_id = ?", (app_id,)).fetchone()
    return dict(row) if row else None


def tailored_versions(conn: sqlite3.Connection, app_id: int) -> list[dict]:
    """Every saved version of an application's tailored CV, oldest first: version, file_path, created_at."""
    rows = conn.execute("SELECT version, file_path, created_at FROM tailored_cv_versions WHERE application_id = ? "
                        "ORDER BY version", (app_id,))
    return [dict(r) for r in rows]


def check_verdict(findings: list[dict]) -> str:
    """blocked when any finding blocks, look when any asks for a look, else ready."""
    severities = {f["severity"] for f in findings}
    return "blocked" if "block" in severities else ("look" if "look" in severities else "ready")


def save_cv_check(conn: sqlite3.Connection, app_id: int, version: int, source: str, findings: list[dict],
                  changes: list[str] | None = None, summary: dict | None = None) -> str:
    """Store one checker's findings for a tailored CV version and return the verdict. Only the findings
    of `source` are replaced: those of other checkers stay (the rule checks write "rules"; an AI check
    calls this with its own source), and the verdict is worked out again from all of them. `changes`
    (vs cv.md) and `summary` (the AI check's scores) replace the stored ones when given. Each finding: check, severity, line (or None),
    message, and optionally text (the line, never pay)."""
    if not any(v["version"] == version for v in tailored_versions(conn, app_id)):
        raise LookupError(f"no tailored CV v{version} on application {app_id}")
    clean = []
    for f in findings:
        if f["severity"] not in CHECK_SEVERITIES:
            raise ValueError(f"severity must be one of {', '.join(CHECK_SEVERITIES)}")
        clean.append({"source": source, "check": f["check"], "severity": f["severity"], "line": f.get("line"),
                      "message": f["message"], "text": f.get("text") or ""})
    with conn:
        old = cv_check(conn, app_id, version)
        kept = [f for f in (old["findings"] if old else []) if f["source"] != source]
        merged = sorted(kept + clean, key=lambda f: (f["line"] is not None, f["line"] or 0))
        verdict = check_verdict(merged)
        if summary is None and old:
            summary = old["summary"]
        conn.execute("INSERT OR REPLACE INTO tailored_cv_checks (application_id, version, verdict, findings, changes, "
                     "checked_at, summary) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (app_id, version, verdict, json.dumps(merged),
                      json.dumps(changes if changes is not None else (old["changes"] if old else [])), now_iso(),
                      json.dumps(summary) if summary is not None else None))
    return verdict


def save_cv_eval(conn: sqlite3.Connection, app_id: int, version: int, findings: list[dict], summary: dict) -> str:
    """Store the AI check of a tailored CV version (source "ai", merged into its verdict) and return the
    verdict. Its open cv_eval request is done only when `version` is still the latest: a version saved
    while the check ran keeps the request open, so the newer one is checked too."""
    verdict = save_cv_check(conn, app_id, version, "ai", findings, summary=summary)
    current = tailored_cv(conn, app_id)
    if current and current["version"] == version:
        finish_requests(conn, [app_id], "cv_eval")
    return verdict


def eval_state(conn: sqlite3.Connection, app_id: int, check: dict | None) -> str:
    """The AI check of the latest tailored version, whose stored check is `check`: "done" (its summary
    is in), "pending" (a cv_eval request is open) or "none" (never asked, e.g. saved before AI checks)."""
    if check and check.get("summary") is not None:
        return "done"
    found = conn.execute("SELECT 1 FROM agent_requests WHERE application_id = ? AND kind = 'cv_eval' "
                         "AND status IN ('pending', 'claimed')", (app_id,)).fetchone()
    return "pending" if found else "none"


def cv_check(conn: sqlite3.Connection, app_id: int, version: int) -> dict | None:
    """The stored check of a tailored CV version (findings and changes as lists), or None (not checked yet)."""
    row = conn.execute("SELECT * FROM tailored_cv_checks WHERE application_id = ? AND version = ?",
                       (app_id, version)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["findings"], d["changes"] = json.loads(d["findings"]), json.loads(d["changes"])
    d["summary"] = json.loads(d["summary"]) if d.get("summary") else None
    return d


def record_cv_sent(conn: sqlite3.Connection, app_id: int, name: str) -> dict:
    """The user says which tailored CV was sent: `name` is one of the application's versions, by file
    name with or without .md. Stores the file name, its version and the time; returns the version."""
    if get_application(conn, app_id) is None:
        raise LookupError(f"no application with id {app_id}")
    name = Path(name.strip()).name
    versions = tailored_versions(conn, app_id)
    found = next((v for v in versions if name in (Path(v["file_path"]).name, Path(v["file_path"]).stem)), None)
    if found is None:
        known = ", ".join(Path(v["file_path"]).name for v in versions) or "none yet"
        raise LookupError(f"no tailored CV {name!r} on application {app_id} (versions: {known})")
    conn.execute("UPDATE applications SET cv_sent = ?, cv_sent_version = ?, cv_sent_at = ? WHERE id = ?",
                 (Path(found["file_path"]).name, found["version"], now_iso(), app_id))
    conn.commit()
    return found


def fit_scores(conn: sqlite3.Connection) -> dict[int, int]:
    """CV fit score by application id (Job Offers rows of tracked jobs)."""
    return {r[0]: r[1] for r in conn.execute("SELECT application_id, score FROM cv_fits")}


def research_view(conn: sqlite3.Connection) -> dict[int, dict]:
    """For the board, by application id: {"profile": dict | None, "fit": dict | None,
    "tailored": dict | None, "open": set of kinds with an open request}."""
    profiles = {r["slug"]: dict(r) for r in conn.execute("SELECT * FROM company_profiles")}
    fits = {r["application_id"]: dict(r) for r in conn.execute("SELECT * FROM cv_fits")}
    tailored = {r["application_id"]: dict(r) for r in conn.execute("SELECT * FROM tailored_cvs")}
    open_kinds: dict[int, set[str]] = {}
    for r in conn.execute("SELECT application_id, kind FROM agent_requests WHERE status IN ('pending', 'claimed')"):
        open_kinds.setdefault(r[0], set()).add(r[1])
    view = {}
    for a in conn.execute("SELECT id, company FROM applications"):
        fit = fits.get(a["id"])
        if fit:
            fit["top_gaps"] = json.loads(fit["top_gaps"] or "[]")
        view[a["id"]] = {"profile": profiles.get(company_slug(a["company"])), "fit": fit,
                         "tailored": tailored.get(a["id"]), "open": open_kinds.get(a["id"], set())}
    return view


def board_applications(conn: sqlite3.Connection) -> list[dict]:
    """Every application for the Applications page, oldest first: id, company, role, link,
    tier, status, date_applied, getjo_job_id, the tailored CV sent (file name, version and time) and since (the day of its last stage
    change, as last_change_day). Never pay."""
    rows = conn.execute(
        """
        SELECT a.id, a.company, a.role, a.link, a.tier, a.status, a.date_applied, a.getjo_job_id,
               a.cv_sent, a.cv_sent_version, a.cv_sent_at,
               substr(COALESCE((SELECT MAX(changed_at) FROM status_history WHERE application_id = a.id),
                               a.date_applied, a.created_at), 1, 10) AS since
        FROM applications a ORDER BY a.id
        """
    )
    return [dict(r) for r in rows]


# --- agent thread ---------------------------------------------------------------------

def _message_body(body: str) -> str:
    """Line breaks as \n (a plain form post sends \r\n, which the browser's maxlength counts as
    one character), stripped, within MAX_MESSAGE."""
    body = body.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not body:
        raise ValueError("a message needs some text")
    if len(body) > MAX_MESSAGE:
        raise ValueError(f"a message can be at most {MAX_MESSAGE:,} characters; this one has {len(body):,}")
    return body


def add_message(conn: sqlite3.Connection, app_id: int | None, body: str, job_id: str | None = None,
                topic: str | None = None) -> int:
    """A user message, about application `app_id`, getjo job `job_id` (not yet tracked; a tracked
    job's message goes to its application), a `topic` (MESSAGE_TOPICS: "cv", the master CV) or none
    of them (the whole search), pending until the agent answers it."""
    body = _message_body(body)
    if sum(x is not None for x in (app_id, job_id, topic)) > 1:
        raise ValueError("a message is about an application, a job or a topic, not more than one")
    if topic is not None and topic not in MESSAGE_TOPICS:
        raise ValueError(f"topic must be one of {', '.join(MESSAGE_TOPICS)}")
    if app_id is not None and get_application(conn, app_id) is None:
        raise LookupError(f"no application with id {app_id}")
    if job_id is not None:
        tracked = find_by_job(conn, job_id)
        app_id = tracked["id"] if tracked else None
    with conn:
        cursor = conn.execute(
            "INSERT INTO agent_messages (application_id, getjo_job_id, topic, author, body, status, created_at) "
            "VALUES (?, ?, ?, 'user', ?, 'pending', ?)",
            (app_id, job_id, topic, body, now_iso()),
        )
        if app_id is not None or job_id is not None or topic is not None:
            _add_targets(conn, cursor.lastrowid, [(app_id, job_id, topic)])
    return cursor.lastrowid


def target_key(application_id: int | None, getjo_job_id: str | None, topic: str | None) -> str:
    """A target as the page names it: "a:<app id>", "j:<getjo job id>" (not tracked) or the topic ("cv")."""
    if application_id is not None:
        return f"a:{application_id}"
    if getjo_job_id:
        return f"j:{getjo_job_id}"
    return topic or ""


def _add_targets(conn: sqlite3.Connection, msg_id: int, targets: list[tuple]) -> None:
    """Target rows for a message, inside the caller's transaction. An application's row carries its
    getjo job id (so Undo Interested can hand it back); a tracked job's row gets its application."""
    for app_id, job_id, topic in targets:
        if app_id is not None and not job_id:
            row = conn.execute("SELECT getjo_job_id FROM applications WHERE id = ?", (app_id,)).fetchone()
            job_id = row[0] if row else None
        elif app_id is None and job_id:
            tracked = find_by_job(conn, job_id)
            app_id = tracked["id"] if tracked else None
        conn.execute("INSERT INTO agent_message_targets (message_id, application_id, getjo_job_id, topic) "
                     "VALUES (?, ?, ?, ?)", (msg_id, app_id, job_id, topic))


def add_compare(conn: sqlite3.Connection, body: str, targets: list[tuple]) -> int:
    """A user message about 2 to MAX_TARGETS targets at once ((application_id, getjo_job_id, topic)
    each; a comparison), pending until the agent answers it. Its own columns hold topic COMPARE only."""
    body = _message_body(body)
    keys = []
    for app_id, job_id, topic in targets:
        if sum(x is not None for x in (app_id, job_id, topic)) != 1:
            raise ValueError("each target is one application, job or topic")
        if topic is not None and topic not in MESSAGE_TOPICS:
            raise ValueError(f"topic must be one of {', '.join(MESSAGE_TOPICS)}")
        if app_id is not None and get_application(conn, app_id) is None:
            raise LookupError(f"no application with id {app_id}")
        keys.append(target_key(app_id, job_id, topic))
    if len(set(keys)) != len(keys):
        raise ValueError("a target is named twice")
    if not 2 <= len(targets) <= MAX_TARGETS:
        raise ValueError(f"a comparison is about 2 to {MAX_TARGETS} jobs")
    with conn:
        cursor = conn.execute(
            "INSERT INTO agent_messages (topic, author, body, status, created_at) VALUES (?, 'user', ?, 'pending', ?)",
            (COMPARE, body, now_iso()))
        _add_targets(conn, cursor.lastrowid, targets)
    return cursor.lastrowid


def message_targets(conn: sqlite3.Connection, msg_ids: list[int] | None = None) -> dict[int, list[dict]]:
    """Targets by message id (all messages, or `msg_ids`), in the order they were named: dicts with
    application_id, getjo_job_id, topic, company, role and key ("a:3", "j:…", "cv")."""
    where = f"WHERE t.message_id IN ({','.join('?' * len(msg_ids))})" if msg_ids is not None else ""
    if msg_ids is not None and not msg_ids:
        return {}
    rows = conn.execute(
        f"SELECT t.message_id, t.application_id, t.getjo_job_id, t.topic, a.company, a.role "
        f"FROM agent_message_targets t LEFT JOIN applications a ON a.id = t.application_id {where} ORDER BY t.id",
        tuple(msg_ids or ()))
    found: dict[int, list[dict]] = {}
    for r in rows:
        d = dict(r)
        d["key"] = target_key(d["application_id"], d["getjo_job_id"], d["topic"])
        found.setdefault(d.pop("message_id"), []).append(d)
    return found


def thread(conn: sqlite3.Connection, app_id: int | None = None, limit: int | None = None,
           job_id: str | None = None, topic: str | None = None) -> list[dict]:
    """Messages oldest first: id, application_id, getjo_job_id, topic, company, author, body, status,
    reply_to, created_at. With `app_id` only that application's, with `job_id` only that job's
    (before and after it was tracked), with `topic` only that topic's; otherwise the whole
    conversation (with `limit`, its latest messages). company is None for a message about an
    untracked job or a topic."""
    key = app_id if app_id is not None else job_id if job_id else topic
    column = "application_id" if app_id is not None else "getjo_job_id" if job_id else "topic" if topic else ""
    # its own messages, and comparisons that name it
    where = (f"WHERE m.{column} = :key OR m.id IN (SELECT message_id FROM agent_message_targets WHERE {column} = :key)"
             if column else "")
    rows = conn.execute(
        f"""
        SELECT * FROM (
            SELECT m.id, m.application_id, m.getjo_job_id, m.topic, a.company, m.author, m.body, m.status,
                   m.reply_to, m.created_at
            FROM agent_messages m LEFT JOIN applications a ON a.id = m.application_id
            {where} ORDER BY m.id DESC {'LIMIT :limit' if limit else ''}
        ) ORDER BY id
        """,
        {"key": key, "limit": limit},
    )
    return [dict(r) for r in rows]


def message_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM agent_messages").fetchone()[0]


def pending_messages(conn: sqlite3.Connection, app_id: int | None = None, job_id: str | None = None,
                     topic: str | None = None) -> list[dict]:
    """User messages waiting for the agent (pending, or claimed longer than CLAIM_HOURS ago, or
    claimed now: `claimed` is True for a live claim), oldest first, with getjo_job_id, topic, company
    and role (None for a message about an untracked job, a topic or the whole search). With `app_id`
    only that application's, with `job_id` only that untracked job's, with `topic` only that topic's."""
    where = "WHERE m.author = 'user' AND m.status IN ('pending', 'claimed')"
    params: tuple = ()
    if app_id is not None:
        where, params = where + " AND m.application_id = ?", (app_id,)
    elif job_id is not None:
        where, params = where + " AND m.application_id IS NULL AND m.getjo_job_id = ?", (job_id,)
    elif topic is not None:
        where, params = where + " AND m.application_id IS NULL AND m.getjo_job_id IS NULL AND m.topic = ?", (topic,)
    rows = conn.execute(
        f"""
        SELECT m.id, m.application_id, m.getjo_job_id, m.topic, m.status, m.created_at, m.claimed_at, a.company, a.role
        FROM agent_messages m LEFT JOIN applications a ON a.id = m.application_id
        {where} ORDER BY m.id
        """,
        params,
    )
    cutoff = _claim_cutoff()
    found = []
    for r in rows:
        d = dict(r)
        d["claimed"] = d["status"] == "claimed" and (d["claimed_at"] or "") >= cutoff
        found.append(d)
    return found


def claim_messages(conn: sqlite3.Connection, app_id: int | None, job_id: str | None = None,
                   topic: str | None = None) -> list[int]:
    """Take the waiting user messages about an application (or an untracked job with `job_id`, a
    `topic`, or, with none of them, the whole search) for this session; returns their ids, oldest first. Only one
    caller wins each: the update applies while it is still pending (or its claim has gone stale)."""
    stamp, cutoff = now_iso(), _claim_cutoff()
    if app_id is not None:
        match, params = "application_id = ?", (app_id,)
    elif job_id is not None:
        match, params = "application_id IS NULL AND getjo_job_id = ?", (job_id,)
    elif topic is not None:
        match, params = "application_id IS NULL AND getjo_job_id IS NULL AND topic = ?", (topic,)
    else:
        match, params = "application_id IS NULL AND getjo_job_id IS NULL AND topic IS NULL", ()
    ids = [r[0] for r in conn.execute(
        f"SELECT id FROM agent_messages WHERE {match} AND author = 'user' "
        "AND (status = 'pending' OR (status = 'claimed' AND claimed_at < ?)) ORDER BY id",
        (*params, cutoff))]
    won = []
    for msg_id in ids:
        cursor = conn.execute(
            "UPDATE agent_messages SET status = 'claimed', claimed_at = ? "
            "WHERE id = ? AND (status = 'pending' OR (status = 'claimed' AND claimed_at < ?))",
            (stamp, msg_id, cutoff),
        )
        if cursor.rowcount == 1:
            won.append(msg_id)
    conn.commit()
    return won


def claim_message(conn: sqlite3.Connection, msg_id: int) -> bool:
    """Take one waiting user message for this session (a comparison is claimed on its own); True if
    this caller won it."""
    stamp, cutoff = now_iso(), _claim_cutoff()
    cursor = conn.execute(
        "UPDATE agent_messages SET status = 'claimed', claimed_at = ? WHERE id = ? AND author = 'user' "
        "AND (status = 'pending' OR (status = 'claimed' AND claimed_at < ?))", (stamp, msg_id, cutoff))
    conn.commit()
    return cursor.rowcount == 1


def message_application(conn: sqlite3.Connection, msg_id: int) -> int | None:
    """The application a message is about (None: an untracked job or the whole search). LookupError
    if there is none."""
    return message_subject(conn, msg_id)[0]


def message_subject(conn: sqlite3.Connection, msg_id: int) -> tuple[int | None, str | None]:
    """(application id, getjo job id) of a message: (id, job or None) for an application, (None, job)
    for a job not yet tracked, (None, None) for the whole search. LookupError if there is none."""
    row = conn.execute("SELECT application_id, getjo_job_id FROM agent_messages WHERE id = ?", (msg_id,)).fetchone()
    if row is None:
        raise LookupError(f"no message {msg_id}")
    return row[0], row[1]


def message_topic(conn: sqlite3.Connection, msg_id: int) -> str | None:
    """A message's topic ("cv"), or None. LookupError if there is no such message."""
    row = conn.execute("SELECT topic FROM agent_messages WHERE id = ?", (msg_id,)).fetchone()
    if row is None:
        raise LookupError(f"no message {msg_id}")
    return row[0]


def reply_message(conn: sqlite3.Connection, app_id: int | None, to_id: int, body: str) -> int:
    """The agent's reply to user message `to_id` (about application `app_id`, or None for the whole
    search): stores it and marks that message answered, in one transaction, only while the message
    is still open (pending or claimed). Returns the reply's id."""
    body = _message_body(body)
    _check_user_message(conn, app_id, to_id)
    stamp = now_iso()
    with conn:
        return _agent_message(conn, app_id, body, stamp, to_id, message_subject(conn, to_id)[1],
                              message_topic(conn, to_id))


def _check_user_message(conn: sqlite3.Connection, app_id: int | None, to_id: int) -> None:
    row = conn.execute(
        "SELECT application_id, author, status FROM agent_messages WHERE id = ?", (to_id,)
    ).fetchone()
    if row is None or row["application_id"] != app_id or row["author"] != "user":
        where = f"on application {app_id}" if app_id is not None else "about the whole search"
        raise LookupError(f"no user message {to_id} {where}")


def _agent_message(conn: sqlite3.Connection, app_id: int | None, body: str, stamp: str, to_id: int | None,
                   job_id: str | None = None, topic: str | None = None) -> int:
    """Insert an agent message inside the caller's transaction. With `to_id` it answers that user
    message, only while it is still open (ValueError otherwise, which rolls the transaction back).
    `job_id` tags it with the getjo job its question was about, `topic` with its topic ("cv")."""
    if to_id is not None:
        cursor = conn.execute(
            "UPDATE agent_messages SET status = 'answered', answered_at = ? "
            "WHERE id = ? AND status IN ('pending', 'claimed')",
            (stamp, to_id),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"message {to_id} is already answered")
    cursor = conn.execute(
        "INSERT INTO agent_messages (application_id, getjo_job_id, topic, author, body, reply_to, created_at) "
        "VALUES (?, ?, ?, 'agent', ?, ?, ?)",
        (app_id, job_id, topic, body, to_id, stamp),
    )
    if to_id is not None:  # the reply is about what the question was about
        conn.execute("INSERT INTO agent_message_targets (message_id, application_id, getjo_job_id, topic) "
                     "SELECT ?, application_id, getjo_job_id, topic FROM agent_message_targets WHERE message_id = ? "
                     "ORDER BY id", (cursor.lastrowid, to_id))
    elif app_id is not None or job_id is not None or topic is not None:
        _add_targets(conn, cursor.lastrowid, [(app_id, job_id, topic)])
    return cursor.lastrowid


# --- master CV versions ---------------------------------------------------------------
# The files live in private_dir/cv-history (prep.cvfile writes them); prep.db keeps the notes.

def add_cv_version(conn: sqlite3.Connection, file_name: str, note: str, sha256: str, created_at: str) -> int:
    """Record a saved version of cv.md; returns its number (vN)."""
    note = " ".join(note.split())
    if not note:
        raise ValueError("a CV version needs a note saying what changed")
    with conn:
        cursor = conn.execute("INSERT INTO cv_versions (file_name, note, sha256, created_at) VALUES (?, ?, ?, ?)",
                              (file_name, note, sha256, created_at))
    return cursor.lastrowid


def cv_versions(conn: sqlite3.Connection) -> list[dict]:
    """Every saved version of cv.md, newest first: id, file_name, note, sha256, created_at."""
    return [dict(r) for r in conn.execute("SELECT * FROM cv_versions ORDER BY id DESC")]


def cv_version(conn: sqlite3.Connection, version_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM cv_versions WHERE id = ?", (version_id,)).fetchone()
    return dict(row) if row else None


def all_tailored_versions(conn: sqlite3.Connection) -> list[dict]:
    """Every tailored CV version of every application, newest first: application_id, company, version,
    file_path, created_at (for copying wording from one into the master CV)."""
    rows = conn.execute(
        "SELECT v.application_id, a.company, v.version, v.file_path, v.created_at FROM tailored_cv_versions v "
        "JOIN applications a ON a.id = v.application_id ORDER BY v.created_at DESC, v.id DESC")
    return [dict(r) for r in rows]


# --- job briefs -----------------------------------------------------------------------
# A short structured brief per getjo job, written by the agent (/research) from the description.
# The page shows it at the top of the panel instead of the raw description. Requests live in
# brief_requests (one row per job); claims older than CLAIM_HOURS count as pending again.

def check_no_pay(text: str, what: str) -> None:
    """ValueError if `text` mentions pay (a currency sign, "80k", salary, bonus…): agent-written text never does."""
    found = PAY_PATTERN.search(text)
    if found:
        raise ValueError(f"{what} must not mention pay (found {found.group(0)!r}); leave it out")


def brief_facts(brief: str) -> str:
    """The key facts line: the brief's first non-empty line."""
    return next((line.strip() for line in brief.splitlines() if line.strip()), "")


def save_brief(conn: sqlite3.Connection, job_id: str, text: str) -> None:
    """Store (or replace) the brief of getjo job `job_id` and close its brief request. The text is
    Markdown: the key facts line first (not a heading, at most MAX_FACTS characters), then sections;
    at most MAX_BRIEF_WORDS words and never pay."""
    brief = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not brief:
        raise ValueError("the brief is empty")
    facts = brief_facts(brief)
    if facts.startswith("#"):
        raise ValueError("start with the key facts line (e.g. \"hybrid 2 days · Python · Senior · London\"), not a heading")
    if len(facts) > MAX_FACTS:
        raise ValueError(f"the key facts line is {len(facts)} characters; keep it to {MAX_FACTS}")
    words = len(brief.split())
    if words > MAX_BRIEF_WORDS:
        raise ValueError(f"the brief has {words} words; keep it to {MAX_BRIEF_WORDS}")
    check_no_pay(brief, "the brief")
    stamp = now_iso()
    with conn:
        conn.execute("INSERT INTO job_briefs (getjo_job_id, brief, updated_at) VALUES (?, ?, ?) "
                     "ON CONFLICT(getjo_job_id) DO UPDATE SET brief = excluded.brief, updated_at = excluded.updated_at",
                     (job_id, brief, stamp))
        conn.execute("UPDATE brief_requests SET status = 'done', done_at = ? WHERE getjo_job_id = ? "
                     "AND status IN ('pending', 'claimed')", (stamp, job_id))


def job_briefs(conn: sqlite3.Connection, job_ids: set[str] | None = None) -> dict[str, dict]:
    """Briefs by getjo job id (all, or those in `job_ids`): brief, facts (its first line), updated_at."""
    rows = conn.execute("SELECT getjo_job_id, brief, updated_at FROM job_briefs")
    return {r[0]: {"brief": r[1], "facts": brief_facts(r[1]), "updated_at": r[2]}
            for r in rows if job_ids is None or r[0] in job_ids}


def queue_brief(conn: sqlite3.Connection, job_id: str, force: bool = False) -> bool:
    """Queue a brief for getjo job `job_id` (Write brief, Redo, an opened panel). False when one is
    already open, so a double click never queues twice."""
    stamp = now_iso()
    with conn:
        cursor = conn.execute(
            "INSERT INTO brief_requests (getjo_job_id, status, force, requested_at) VALUES (?, 'pending', ?, ?) "
            "ON CONFLICT(getjo_job_id) DO UPDATE SET status = 'pending', force = excluded.force, "
            "requested_at = excluded.requested_at, claimed_at = NULL, done_at = NULL "
            "WHERE brief_requests.status = 'done' OR (brief_requests.status = 'claimed' AND brief_requests.claimed_at < ?)",
            (job_id, int(force), stamp, _claim_cutoff()))
    return cursor.rowcount == 1


def queue_missing_briefs(conn: sqlite3.Connection, job_ids: list[str]) -> list[str]:
    """Queue a brief for each job in `job_ids` that has neither a brief nor a request (ever): the
    background queue for new offers. Returns the ids queued now."""
    have = set(job_briefs(conn)) | {r[0] for r in conn.execute("SELECT getjo_job_id FROM brief_requests")}
    added = [j for j in dict.fromkeys(job_ids) if j not in have]
    stamp = now_iso()
    with conn:
        conn.executemany("INSERT OR IGNORE INTO brief_requests (getjo_job_id, requested_at) VALUES (?, ?)",
                         [(j, stamp) for j in added])
    return added


def open_briefs(conn: sqlite3.Connection, job_id: str | None = None) -> list[dict]:
    """Brief requests not done, oldest first (or only `job_id`'s): getjo_job_id, force, requested_at and
    `claimed` (True for a live claim; a stale claim counts as pending)."""
    rows = conn.execute(
        "SELECT * FROM brief_requests WHERE status IN ('pending', 'claimed')"
        + (" AND getjo_job_id = ?" if job_id else "") + " ORDER BY requested_at, getjo_job_id",
        (job_id,) if job_id else ())
    cutoff = _claim_cutoff()
    return [{**dict(r), "claimed": r["status"] == "claimed" and (r["claimed_at"] or "") >= cutoff} for r in rows]


def claim_brief(conn: sqlite3.Connection, job_id: str) -> bool:
    """Take one brief request for this session; True if this caller won it."""
    stamp = now_iso()
    cursor = conn.execute(
        "UPDATE brief_requests SET status = 'claimed', claimed_at = ? WHERE getjo_job_id = ? "
        "AND (status = 'pending' OR (status = 'claimed' AND claimed_at < ?))", (stamp, job_id, _claim_cutoff()))
    conn.commit()
    return cursor.rowcount == 1
