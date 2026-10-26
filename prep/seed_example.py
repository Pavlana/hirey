"""Build example databases from the committed, made-up fixtures.

    python -m prep.seed_example

Writes data/example.db (prep schema) and data/example-getjo.db (a stand-in for getjo's
jobs.db). Both are gitignored. Try the commands against them with:

    PREP_DB=data/example.db PREP_GETJO_DB=$PWD/data/example-getjo.db python -m prep.cli today
"""

import json
import logging
import sqlite3
import sys
from pathlib import Path

from prep import db
from prep.config import REPO_ROOT

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "example.json"
EXAMPLE_PREP_DB = REPO_ROOT / "data" / "example.db"
EXAMPLE_GETJO_DB = REPO_ROOT / "data" / "example-getjo.db"

log = logging.getLogger("prep.seed")

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


def load_fixtures(path: Path = FIXTURES) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def build_getjo_db(path: Path, jobs: list[dict]) -> None:
    """Create a getjo-shaped jobs.db at `path` holding `jobs` (replaces any existing file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(GETJO_SCHEMA)
        conn.executemany(
            """
            INSERT INTO jobs (id, source, company, title, location, url, description, first_seen, score)
            VALUES (:id, :source, :company, :title, :location, :url, :description, :first_seen, :score)
            """,
            jobs,
        )
        conn.commit()
    finally:
        conn.close()


def build_prep_db(path: Path, data: dict) -> None:
    """Create prep.db at `path` from fixture applications, tasks and gaps (replaces any existing file)."""
    path.unlink(missing_ok=True)
    conn = db.connect(path)
    try:
        for app in data.get("applications", []):
            fields = dict(app)
            db.add_application(conn, fields.pop("company"), fields.pop("role"), **fields)
        for gap in data.get("gaps", []):
            db.add_gap(conn, gap["gap"], gap.get("date"), gap.get("session"))
        for task in data.get("tasks", []):
            db.add_task(conn, task["title"], task.get("kind", "admin"), task.get("due_date"))
    finally:
        conn.close()


def main() -> int:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
    data = load_fixtures()
    build_getjo_db(EXAMPLE_GETJO_DB, data["getjo_jobs"])
    build_prep_db(EXAMPLE_PREP_DB, data)
    log.info("wrote %s and %s", EXAMPLE_PREP_DB.relative_to(REPO_ROOT), EXAMPLE_GETJO_DB.relative_to(REPO_ROOT))
    log.info("try: PREP_DB=data/example.db PREP_GETJO_DB=$PWD/data/example-getjo.db python -m prep.cli today")
    return 0


if __name__ == "__main__":
    sys.exit(main())
