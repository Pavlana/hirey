"""Read-only view of getjo's jobs database.

getjo (job-radar) owns jobs.db. This module only reads it: the connection is opened
with SQLite's URI mode=ro, so any write fails. getjo code is never imported.

getjo's table:
    jobs(id TEXT PK "source:company:job_id", source, company, title, location, url,
         description, first_seen ISO, score INTEGER nullable, score_attempts)
"""

import sqlite3
from pathlib import Path

DEFAULT_THRESHOLD = 7

# Columns returned to callers. The description is left out: it is long and not needed.
COLUMNS = "id, source, company, title, location, url, first_seen, score"


class GetjoUnavailable(Exception):
    """getjo_db is not configured or the file does not exist."""


def connect_ro(path: Path | None) -> sqlite3.Connection:
    """Open getjo's database read-only. Raises GetjoUnavailable if it is missing."""
    if path is None:
        raise GetjoUnavailable("getjo_db is not set in config/settings.toml (or PREP_GETJO_DB)")
    if not path.is_file():
        raise GetjoUnavailable(f"getjo database not found at {path}")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def get_job(conn: sqlite3.Connection, job_id: str) -> dict | None:
    row = conn.execute(f"SELECT {COLUMNS} FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def find_by_url(conn: sqlite3.Connection, url: str) -> dict | None:
    """The job with this exact URL (ignoring a trailing slash), if getjo has it."""
    stripped = url.rstrip("/")
    row = conn.execute(
        f"SELECT {COLUMNS} FROM jobs WHERE url IN (?, ?) ORDER BY first_seen DESC LIMIT 1",
        (stripped, stripped + "/"),
    ).fetchone()
    return dict(row) if row else None


def find_by_company_role(conn: sqlite3.Connection, company: str, role: str) -> list[dict]:
    """Jobs whose company and title match case-insensitively, newest first."""
    rows = conn.execute(
        f"""
        SELECT {COLUMNS} FROM jobs
        WHERE lower(company) = lower(?) AND lower(title) = lower(?)
        ORDER BY first_seen DESC
        """,
        (company, role),
    )
    return [dict(r) for r in rows]


def matches(
    conn: sqlite3.Connection,
    exclude_ids: set[str] = frozenset(),
    threshold: int = DEFAULT_THRESHOLD,
    limit: int | None = None,
) -> list[dict]:
    """Jobs scored at or above `threshold` and not in `exclude_ids`, newest first.

    `exclude_ids` is the set of getjo ids already linked to an application in prep.db.
    """
    rows = conn.execute(
        f"SELECT {COLUMNS} FROM jobs WHERE score >= ? ORDER BY first_seen DESC, id",
        (threshold,),
    )
    found = [dict(r) for r in rows if r["id"] not in exclude_ids]
    return found[:limit] if limit is not None else found
