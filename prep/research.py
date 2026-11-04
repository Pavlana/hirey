"""Files behind the agent's company research and CV fit checks.

The full text lives in private_dir (never committed):
    companies/<slug>.md       one per company (db.company_slug), reused for a second role there
    fit/<app-id>-<slug>.md    one per application

Each file starts with TOML front matter between two "+++" lines, read with tomllib; the
summary stored in prep.db (company_profiles, cv_fits) comes from it, so a hand edit followed
by `research save` / `fit save` updates the database. The Markdown body after it is free text.
"""

from datetime import date, datetime
from pathlib import Path
import tomllib

from prep import db

FENCE = "+++"
COMPANY_DIR = "companies"
FIT_DIR = "fit"
CV_FILE = "cv.md"


def company_file(private_dir: Path, slug: str) -> Path:
    return private_dir / COMPANY_DIR / f"{slug}.md"


def fit_file(private_dir: Path, app_id: int, slug: str) -> Path:
    return private_dir / FIT_DIR / f"{app_id}-{slug}.md"


def split_front_matter(text: str) -> tuple[dict, str]:
    """(front matter, Markdown body). Raises ValueError when the block is missing or not TOML."""
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != FENCE:
        raise ValueError(f'the file must start with a "{FENCE}" line and TOML front matter')
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == FENCE)
    except StopIteration:
        raise ValueError(f'the front matter has no closing "{FENCE}" line') from None
    try:
        data = tomllib.loads("\n".join(lines[1:end]))
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"the front matter is not valid TOML: {e}") from e
    return data, "\n".join(lines[end + 1:])


def _day(value: object, name: str) -> str | None:
    """A TOML date (or a YYYY-MM-DD string) as YYYY-MM-DD."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise ValueError(f"{name} must be a date like 2026-01-31; got {value!r}") from None


def _text(value: object) -> str | None:
    return None if value is None or str(value).strip() == "" else str(value).strip()


def read_profile(path: Path) -> dict:
    """The company profile in a companies/<slug>.md file, ready for db.save_company_profile."""
    data, _ = split_front_matter(path.read_text(encoding="utf-8"))
    employees = data.get("employees")
    if employees is not None and (isinstance(employees, bool) or not isinstance(employees, int)):
        raise ValueError(f"employees must be a whole number; got {employees!r}")
    sources = data.get("sources") or []
    if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
        raise ValueError("sources must be a list of URLs")
    profile = {
        "company": _text(data.get("company")),
        "file_path": str(path),
        "what_it_does": _text(data.get("what_it_does")),
        "sector": _text(data.get("sector")),
        "employees": employees,
        "employees_as_of": _day(data.get("employees_as_of"), "employees_as_of"),
        "size_band": _text(data.get("size_band")),
        "london_remote": _text(data.get("london_remote")),
        "recognition": _text(data.get("recognition")),
        "recognition_note": _text(data.get("recognition_note")),
        "funding": _text(data.get("funding")),
        "sources": [s.strip() for s in sources if s.strip()],
        "suggested_tier": _text(data.get("suggested_tier")),
        "tier_reason": _text(data.get("tier_reason")),
        "researched_at": _day(data.get("researched_at"), "researched_at"),
    }
    if profile["suggested_tier"]:
        profile["suggested_tier"] = profile["suggested_tier"].upper()
    return profile


def read_fit(path: Path) -> dict:
    """The CV fit summary in a fit/<app-id>-<slug>.md file: score, gaps, checked_at."""
    data, _ = split_front_matter(path.read_text(encoding="utf-8"))
    gaps = data.get("gaps") or []
    if not isinstance(gaps, list) or not all(isinstance(g, str) for g in gaps):
        raise ValueError("gaps must be a list of strings, most important first")
    return {"score": data.get("score"), "gaps": gaps, "checked_at": _day(data.get("checked_at"), "checked_at")}


def freshness(profile: dict | None, max_age_days: int, today: str) -> tuple[str, int | None]:
    """("missing", None), ("fresh", age in days) or ("stale", age) for a company profile."""
    if profile is None:
        return "missing", None
    age = (date.fromisoformat(today) - date.fromisoformat(profile["researched_at"][:10])).days
    return ("fresh" if age <= max_age_days else "stale"), age


def inside(private_dir: Path | None, recorded: str | None) -> Path | None:
    """The recorded path if it resolves inside private_dir, else None (never served)."""
    if not private_dir or not recorded:
        return None
    try:
        path = Path(recorded).resolve()
        return path if path.is_relative_to(private_dir.resolve()) else None
    except (OSError, ValueError):
        return None


def result_path(conn, private_dir: Path | None, app_id: int, kind: str) -> tuple[bool, Path | None]:
    """For the dashboard's Open link: (a result is recorded, its path if it is inside private_dir)."""
    app = db.get_application(conn, app_id)
    if app is None:
        return False, None
    if kind == "research":
        record = db.company_profile(conn, db.company_slug(app["company"]))
    else:
        record = db.cv_fit(conn, app_id)
    if record is None:
        return False, None
    return True, inside(private_dir, record["file_path"])
