"""Build example databases from the committed, made-up fixtures.

    python -m prep.seed_example

Writes data/example.db (prep schema) and data/example-getjo.db (a stand-in for getjo's
jobs.db). Both are gitignored. Dates are shifted so the fixtures' _anchor_date becomes
today, so the dashboard shows recent activity. Try the commands against them with:

    PREP_ASSISTANT_TASK= PREP_DB=data/example.db PREP_GETJO_DB=$PWD/data/example-getjo.db python -m prep.cli today
    PREP_ASSISTANT_TASK= PREP_DB=data/example.db PREP_GETJO_DB=$PWD/data/example-getjo.db python -m prep.web --port 8766

Tests use the fixtures unshifted.
"""

import json
import logging
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

from prep import db
from prep.config import REPO_ROOT

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "example.json"
EXAMPLE_PREP_DB = REPO_ROOT / "data" / "example.db"
EXAMPLE_GETJO_DB = REPO_ROOT / "data" / "example-getjo.db"

log = logging.getLogger("prep.seed")

# Fixture keys holding a date (YYYY-MM-DD) or a timestamp starting with one.
DATE_KEYS = ("date_applied", "last_contact", "next_action_date", "due_date", "done_at", "date", "first_seen",
             "posted_at", "expires_at", "last_seen", "closed_at")

# Same shape as getjo's jobs table (getjo/core/store.py), copied rather than imported.
GETJO_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id             TEXT PRIMARY KEY,
    source         TEXT NOT NULL,
    company        TEXT NOT NULL,
    title          TEXT NOT NULL,
    location       TEXT,
    url            TEXT NOT NULL,
    description    TEXT,
    first_seen     TEXT NOT NULL,
    score          INTEGER,
    score_attempts INTEGER NOT NULL DEFAULT 0
);
"""
# getjo's newer columns (docs/design.md there, "Open and closed jobs"); left out for an older jobs.db.
GETJO_STATE_COLUMNS = ("posted_at", "expires_at", "last_seen", "closed_at")


def load_fixtures(path: Path = FIXTURES) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def shift_dates(data: dict, days: int) -> dict:
    """A copy of the fixtures with every date moved by `days` (time of day kept)."""
    def shift(value: str) -> str:
        return (date.fromisoformat(value[:10]) + timedelta(days=days)).isoformat() + value[10:]

    shifted = {}
    for key, value in data.items():
        if isinstance(value, list):
            value = [{k: shift(v) if k in DATE_KEYS and v else v for k, v in record.items()} for record in value]
        shifted[key] = value
    return shifted


def build_getjo_db(path: Path, jobs: list[dict], old_schema: bool = False) -> None:
    """Create a getjo-shaped jobs.db at `path` holding `jobs` (replaces any existing file). With
    `old_schema` the table has no open/closed columns, as before getjo added them."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    columns = ["id", "source", "company", "title", "location", "url", "description", "first_seen", "score"]
    try:
        conn.executescript(GETJO_SCHEMA)
        if not old_schema:
            for column in GETJO_STATE_COLUMNS:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")
            columns += GETJO_STATE_COLUMNS
        conn.executemany(
            f"INSERT INTO jobs ({', '.join(columns)}) VALUES ({', '.join(':' + c for c in columns)})",
            [{c: job.get(c) for c in columns} for job in jobs],
        )
        conn.commit()
    finally:
        conn.close()


def build_prep_db(path: Path, data: dict) -> None:
    """Create prep.db at `path` from fixture applications, tasks, gaps and Agent thread messages
    (replaces any existing file)."""
    path.unlink(missing_ok=True)
    conn = db.connect(path)
    try:
        for app in data.get("applications", []):
            fields = dict(app)
            # "history" lists earlier statuses: replay them so status_history is filled.
            statuses = [*fields.pop("history", []), fields.pop("status", "to_apply")]
            app_id = db.add_application(conn, fields.pop("company"), fields.pop("role"), status=statuses[0], **fields)
            for status in statuses[1:]:
                db.update_application(conn, app_id, status=status)
        db.next_actions_to_tasks(conn)  # next actions are tasks now, as the migration does for older databases
        for gap in data.get("gaps", []):
            db.add_gap(conn, gap["gap"], gap.get("date"), gap.get("session"))
        for task in data.get("tasks", []):
            task_id = db.add_task(conn, task["title"], task.get("kind", "admin"), task.get("due_date"))
            if task.get("done_at"):
                db.complete_task(conn, task_id)
                conn.execute("UPDATE tasks SET done_at = ? WHERE id = ?", (task["done_at"], task_id))
                conn.commit()
        for m in data.get("messages", []):  # an Agent thread: a question and the agent's reply
            msg_id = db.add_message(conn, m["application"], m["question"])
            db.reply_message(conn, m["application"], msg_id, m["reply"])
    finally:
        conn.close()


def main() -> int:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
    data = load_fixtures()
    data = shift_dates(data, (date.today() - date.fromisoformat(data["_anchor_date"])).days)
    build_getjo_db(EXAMPLE_GETJO_DB, data["getjo_jobs"])
    build_prep_db(EXAMPLE_PREP_DB, data)
    log.info("wrote %s and %s", EXAMPLE_PREP_DB.relative_to(REPO_ROOT), EXAMPLE_GETJO_DB.relative_to(REPO_ROOT))
    log.info("try: PREP_ASSISTANT_TASK= PREP_DB=data/example.db PREP_GETJO_DB=$PWD/data/example-getjo.db python -m prep.cli today")
    log.info("or:  PREP_ASSISTANT_TASK= PREP_DB=data/example.db PREP_GETJO_DB=$PWD/data/example-getjo.db python -m prep.web --port 8766")
    return 0


if __name__ == "__main__":
    sys.exit(main())
