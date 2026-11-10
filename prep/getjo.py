"""Read-only view of getjo's jobs database.

getjo (job-radar) owns jobs.db. This module only reads it: the connection is opened
with SQLite's URI mode=ro, so any write fails. getjo code is never imported.

getjo's table:
    jobs(id TEXT PK "source:company:job_id", source, company, title, location, url,
         description, first_seen ISO, score INTEGER nullable, score_attempts,
         posted_at, expires_at, last_seen, closed_at)

The last four (getjo docs/design.md, "Open and closed jobs") are newer: an older jobs.db lacks
them, so the select list is built per connection and a missing one reads as NULL. All getjo
timestamps are "YYYY-MM-DDTHH:MM:SSZ" (UTC), so they compare as text against a stamp in the
same format, never against a bare date.
"""

import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

DEFAULT_THRESHOLD = 7
EXPIRES_SOON_DAYS = 7  # "expires in N days" shows from this many days before expires_at

# Columns returned to callers. The description is left out: it is long and not needed.
BASE_COLUMNS = ("id", "source", "company", "title", "location", "url", "first_seen", "score")
STATE_COLUMNS = ("posted_at", "expires_at", "last_seen", "closed_at")


LOCATIONS = ("london", "remote", "other")
_LONDON = re.compile(r"\blondon\b", re.IGNORECASE)
_REMOTE = re.compile(r"\b(remote|anywhere|work from home|wfh)\b", re.IGNORECASE)


def location_tags(location: str | None) -> set[str]:
    """Location groups for a getjo `jobs.location` string: "london", "remote", both, or "other".

    "London / Remote" and "Remote (UK) or London" are both; "Hybrid - London" is London;
    "Londonderry" and an empty location are other.
    """
    tags = set()
    if location and _LONDON.search(location):
        tags.add("london")
    if location and _REMOTE.search(location):
        tags.add("remote")
    return tags or {"other"}


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


def _has(conn: sqlite3.Connection) -> set[str]:
    """The newer state columns this jobs table has (none in an older getjo)."""
    return {r[1] for r in conn.execute("PRAGMA table_info(jobs)")} & set(STATE_COLUMNS)


def _columns(conn: sqlite3.Connection) -> str:
    """The select list: the base columns plus the state columns, NULL where the table lacks one."""
    has = _has(conn)
    return ", ".join((*BASE_COLUMNS, *(c if c in has else f"NULL AS {c}" for c in STATE_COLUMNS)))


def get_job(conn: sqlite3.Connection, job_id: str) -> dict | None:
    row = conn.execute(f"SELECT {_columns(conn)} FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def job_description(conn: sqlite3.Connection, job_id: str) -> str | None:
    """The job's description text (left out of the select list: long; read for a CV fit check and the offer panel)."""
    row = conn.execute("SELECT description FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return row["description"] if row else None


def jobs_by_id(conn: sqlite3.Connection, ids: set[str]) -> dict[str, dict]:
    """The jobs getjo still has among `ids`, by id, open or closed."""
    ids = list(ids)
    found: dict[str, dict] = {}
    for start in range(0, len(ids), 500):  # well under SQLite's variable limit
        chunk = ids[start:start + 500]
        marks = ", ".join("?" * len(chunk))
        for row in conn.execute(f"SELECT {_columns(conn)} FROM jobs WHERE id IN ({marks})", chunk):
            found[row["id"]] = dict(row)
    return found


def find_by_url(conn: sqlite3.Connection, url: str) -> dict | None:
    """The job with this exact URL (ignoring a trailing slash), if getjo has it."""
    stripped = url.rstrip("/")
    row = conn.execute(
        f"SELECT {_columns(conn)} FROM jobs WHERE url IN (?, ?) ORDER BY first_seen DESC LIMIT 1",
        (stripped, stripped + "/"),
    ).fetchone()
    return dict(row) if row else None


def find_by_company_role(conn: sqlite3.Connection, company: str, role: str) -> list[dict]:
    """Jobs whose company and title match case-insensitively, newest first."""
    rows = conn.execute(
        f"""
        SELECT {_columns(conn)} FROM jobs
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
    now: str | None = None,
) -> list[dict]:
    """Open jobs scored at or above `threshold` and not in `exclude_ids`, newest first.

    `exclude_ids` is the set of getjo ids already linked to an application in prep.db. A job
    with closed_at set, or whose expires_at is not after `now` (a "YYYY-MM-DDTHH:MM:SSZ" stamp,
    default the current time), is left out; an older jobs.db has neither column.
    """
    has = _has(conn)
    where, params = "score >= ?", [threshold]
    if "closed_at" in has:
        where += " AND closed_at IS NULL"
    if "expires_at" in has:
        where += " AND (expires_at IS NULL OR expires_at = '' OR expires_at > ?)"
        params.append(now or now_stamp())
    rows = conn.execute(f"SELECT {_columns(conn)} FROM jobs WHERE {where} ORDER BY first_seen DESC, id", params)
    found = [dict(r) for r in rows if r["id"] not in exclude_ids]
    return found[:limit] if limit is not None else found


def now_stamp() -> str:
    """The current UTC time in getjo's stamp format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _day(stamp: str | None) -> date | None:
    try:
        return date.fromisoformat(stamp[:10]) if stamp else None
    except ValueError:
        return None


def job_state(job: dict, now: str, stale_days: int) -> dict:
    """Whether a job reads as open: `closed` (closed_at set, or expires_at not after `now`),
    `may_be_closed` (last_seen more than `stale_days` days before `now`) and `expires_in` (days
    left when expires_at is at most EXPIRES_SOON_DAYS away, else None). Missing columns read as
    unknown, so a job from an older getjo is simply open."""
    expires, today = job.get("expires_at"), _day(now)
    closed = bool(job.get("closed_at")) or bool(expires and expires <= now)
    seen = _day(job.get("last_seen"))
    may_be_closed = not closed and seen is not None and today is not None and (today - seen).days > stale_days
    expires_in = None
    if not closed and expires and _day(expires) and today:
        left = (_day(expires) - today).days
        expires_in = left if left <= EXPIRES_SOON_DAYS else None
    return {"closed": closed, "may_be_closed": may_be_closed, "expires_in": expires_in}
