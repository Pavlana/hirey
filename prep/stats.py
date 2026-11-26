"""Numbers for the dashboard, computed from prep.db. Read-only, plain SQL and Python.

Definitions (used everywhere, documented in docs/reference/metrics.md):

- Submitted: an application that was actually sent: it has a date_applied, or its status
  (now or in its history) is a funnel stage, rejected or no_response. to_apply rows and
  rows withdrawn before applying are not submitted.
- Furthest stage: the latest FUNNEL stage among the application's status history plus its
  current status. Applications older than the status_history table only have their
  current status. A submitted application with no funnel stage (e.g. only rejected) has
  reached "applied".
- Replied (the response rate): furthest stage is recruiter_call or later, the application was
  rejected, or an interview is recorded for it. applied, no_response and a withdrawal straight
  from applied are not replies.
- Reply rate: replied / submitted. A warning shows when it is below 10% after 25 or more
  submitted applications.
- Stage funnel: four steps, each counting submitted applications whose furthest stage reached it
  or a later one (cumulative, so skipped stages still count), with their share of submitted:
  Applied (every submitted one), Screen (recruiter_call), Interview (technical, system_design or
  final), Offer (offer).
- Weeks start on Monday, counted by date_applied.
"""

import sqlite3
from datetime import date, timedelta

from prep import db

FUNNEL = ("applied", "recruiter_call", "technical", "system_design", "final", "offer")
REPLY_WARNING_MIN_APPS = 25
REPLY_WARNING_RATE = 0.10
WEEKS_SHOWN = 12
# (label, first FUNNEL stage that counts) for the Pipeline stats page's stage funnel.
STEPS = (("Applied", "applied"), ("Screen", "recruiter_call"), ("Interview", "technical"), ("Offer", "offer"))


def _today(today: str | None) -> date:
    return date.fromisoformat(today) if today else date.today()


def furthest_stage(statuses: set[str], submitted: bool) -> str | None:
    """The latest FUNNEL stage reached, or None if never submitted."""
    reached = [s for s in FUNNEL if s in statuses]
    if reached:
        return reached[-1]
    return "applied" if submitted else None


def is_submitted(statuses: set[str], date_applied: str | None) -> bool:
    return bool(date_applied) or bool(statuses & (set(FUNNEL) | {"rejected", "no_response"}))


def is_replied(statuses: set[str], stage: str | None, interviewed: bool = False) -> bool:
    return interviewed or "rejected" in statuses or (stage is not None and FUNNEL.index(stage) >= 1)


def application_stages(conn: sqlite3.Connection) -> list[dict]:
    """One dict per application: id, status, tier, date_applied, getjo_job_id, submitted, stage, replied."""
    history: dict[int, set[str]] = {}
    for row in conn.execute("SELECT application_id, status FROM status_history"):
        history.setdefault(row[0], set()).add(row[1])
    interviewed = {r[0] for r in conn.execute("SELECT DISTINCT application_id FROM interviews")}
    result = []
    for row in conn.execute("SELECT id, status, tier, date_applied, getjo_job_id FROM applications ORDER BY id"):
        statuses = history.get(row["id"], set()) | {row["status"]}
        submitted = is_submitted(statuses, row["date_applied"])
        stage = furthest_stage(statuses, submitted)
        result.append({
            "id": row["id"], "status": row["status"], "tier": row["tier"],
            "date_applied": row["date_applied"], "getjo_job_id": row["getjo_job_id"], "submitted": submitted,
            "stage": stage, "replied": submitted and is_replied(statuses, stage, row["id"] in interviewed),
        })
    return result


def reply_rate(apps: list[dict]) -> dict:
    """submitted, replied, rate (0..1 or None) and warning (bool)."""
    submitted = [a for a in apps if a["submitted"]]
    replied = sum(1 for a in submitted if a["replied"])
    rate = replied / len(submitted) if submitted else None
    warning = len(submitted) >= REPLY_WARNING_MIN_APPS and rate is not None and rate < REPLY_WARNING_RATE
    return {"submitted": len(submitted), "replied": replied, "rate": rate, "warning": warning}


def stage_funnel(apps: list[dict]) -> list[tuple[str, int, float | None]]:
    """(step label, reached, share of submitted) for each of STEPS."""
    submitted = [a for a in apps if a["submitted"]]
    result = []
    for label, first in STEPS:
        reached = sum(1 for a in submitted if FUNNEL.index(a["stage"]) >= FUNNEL.index(first))
        result.append((label, reached, reached / len(submitted) if submitted else None))
    return result


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _per_week(days: list[str], today: str | None, weeks: int) -> list[tuple[str, int]]:
    """(Monday as YYYY-MM-DD, count) for the last `weeks` weeks, oldest first, zeros included."""
    current = _week_start(_today(today))
    starts = [current - timedelta(weeks=n) for n in range(weeks - 1, -1, -1)]
    counts = {s: 0 for s in starts}
    for text in days:
        try:
            start = _week_start(date.fromisoformat(text[:10]))
        except ValueError:
            continue
        if start in counts:
            counts[start] += 1
    return [(s.isoformat(), counts[s]) for s in starts]


def applications_per_week(conn: sqlite3.Connection, today: str | None = None,
                          weeks: int = WEEKS_SHOWN) -> list[tuple[str, int]]:
    rows = conn.execute("SELECT date_applied FROM applications WHERE date_applied IS NOT NULL")
    return _per_week([r[0] for r in rows], today, weeks)
