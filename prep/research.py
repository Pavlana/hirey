"""Files behind the agent's company research and CV fit checks.

The full text lives in private_dir (never committed):
    companies/<slug>.md       one per company (db.company_slug), reused for a second role there
    fit/<app-id>-<slug>.md    one per application
    cv-tailored/CV_<Company>_<RoleShort>_<DDMMYY>_<n>.md   a copy of cv.md tailored to one job,
                              one file per version (n), all kept; revised through the thread

Each file starts with TOML front matter between two "+++" lines, read with tomllib; the
summary stored in prep.db (company_profiles, cv_fits) comes from it, so a hand edit followed
by `research save` / `fit save` updates the database. The Markdown body after it is free text.
"""

from datetime import date, datetime
from pathlib import Path
import re
import tomllib

from prep import db

FENCE = "+++"
COMPANY_DIR = "companies"
FIT_DIR = "fit"
TAILORED_DIR = "cv-tailored"
STYLE_FILE = "_style.md"  # agreed wording reused in every tailored copy (e.g. a career gap line)
TAILOR_MAX_WORDS = 1_000  # about two pages; one is fine
CV_FILE = "cv.md"


def company_file(private_dir: Path, slug: str) -> Path:
    return private_dir / COMPANY_DIR / f"{slug}.md"


def fit_file(private_dir: Path, app_id: int, slug: str) -> Path:
    return private_dir / FIT_DIR / f"{app_id}-{slug}.md"


ROLE_SHORT_MAX = 12


def file_word(text: str, limit: int | None = None) -> str:
    """Letters and digits only, words run together keeping their case ("Globex Labs" -> "GlobexLabs")."""
    words = re.findall(r"[A-Za-z0-9]+", text)
    word = "".join(w[:1].upper() + w[1:] for w in words)
    return word[:limit] if limit else word


def company_word(company: str) -> str:
    """The company part of a tailored CV file name: legal suffixes dropped, "Acme Rockets Ltd." -> "AcmeRockets"."""
    words = re.findall(r"[A-Za-z0-9]+", company)
    while len(words) > 1 and words[-1].lower() in db._COMPANY_SUFFIXES:
        words.pop()
    return file_word(" ".join(words)) or "Company"


def role_short_word(value: str) -> str:
    """A role short name for file names: letters and digits, at most ROLE_SHORT_MAX ("AI Eng" -> "AIEng")."""
    word = file_word(value, ROLE_SHORT_MAX)
    if not word:
        raise ValueError("the role short name needs letters or digits, e.g. FDE or AIEng")
    return word


def tailored_file(private_dir: Path, company: str, role_short: str, day: str, version: int) -> Path:
    """cv-tailored/CV_<Company>_<RoleShort>_<DDMMYY>_<n>.md, e.g. CV_OpenAI_FDE_210926_1.md."""
    stamp = date.fromisoformat(day).strftime("%d%m%y")
    return private_dir / TAILORED_DIR / f"CV_{company_word(company)}_{role_short_word(role_short)}_{stamp}_{version}.md"


def style_file(private_dir: Path) -> Path:
    return private_dir / TAILORED_DIR / STYLE_FILE


def word_count(text: str) -> int:
    """Words in a Markdown body (front matter already split off), ignoring Markdown markers."""
    return sum(1 for w in text.split() if any(c.isalnum() for c in w))


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


def result_path(conn, private_dir: Path | None, app_id: int, kind: str,
                version: int | None = None) -> tuple[bool, Path | None]:
    """For the dashboard's Open link: (a result is recorded, its path if it is inside private_dir).
    kind: research, cv_fit or tailor (the latest tailored CV, or `version`)."""
    app = db.get_application(conn, app_id)
    if app is None:
        return False, None
    if kind == "research":
        record = db.company_profile(conn, db.company_slug(app["company"]))
    elif kind == "cv_fit":
        record = db.cv_fit(conn, app_id)
    elif kind == "tailor" and version is not None:
        record = next((v for v in db.tailored_versions(conn, app_id) if v["version"] == version), None)
    elif kind == "tailor":
        record = db.tailored_cv(conn, app_id)
    else:
        return False, None
    if record is None:
        return False, None
    return True, inside(private_dir, record["file_path"])
