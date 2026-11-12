"""Load settings from a TOML file, with environment variable overrides.

The settings file is the first of these that applies:
    1. $INTERPREP_SETTINGS, if set (used as-is, even if missing: tests rely on this)
    2. config/settings.toml in this checkout, if it exists
    3. ~/.config/inter_prep/settings.toml, if it exists

The fixed per-user file (3) is what dev3 worktrees, launchd and the Claude app share:
none of them needs a copy of the file or a shell environment.

Keys:
    getjo_db     absolute path to getjo's data/jobs.db (read-only)
    prep_db      path to this project's database (default data/prep.db)
    private_dir  absolute path to the folder holding cv.md, notes/, sessions/
    dashboard_port  port for the local dashboard (default 8765)
    weekly_target   applications per week shown on the dashboard (default 10)
    no_response_days  days in Applied with no stage change before an application moves to
                      No response automatically (default 14)
    research_max_age_days  a company profile older than this is researched again (default 90)
    stale_days      a getjo job not listed for more than this many days reads "may be closed" (default 3)
    brief_auto_min_score  new offers scored at least this get a job brief queued in the background;
                    others when their panel is first opened (default 8, 1-10)
    assistant_task  optional dev3 task seq of a standing assistant agent: newly queued research
                    and CV fit requests are sent to it with `dev3 message` (unset: no message)
    assistant_project  optional dev3 project id, passed with --project when set
    dev3_bin        optional absolute path to the dev3 CLI (default: found on PATH)
    pdf_python      optional absolute path to a Python that has WeasyPrint (a venv outside any
                    worktree), used for tailored CV PDFs when the running Python lacks it

An env var set to an empty string clears the assistant keys (CLEARABLE); for every other key an
empty value is ignored. Relative paths are resolved against the repo root, never the current directory,
because the code also runs from dev3 worktrees.
"""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = REPO_ROOT / "config" / "settings.toml"
USER_SETTINGS_PATH = Path.home() / ".config" / "inter_prep" / "settings.toml"
DEFAULT_PREP_DB = "data/prep.db"
DEFAULT_DASHBOARD_PORT = 8765
DEFAULT_WEEKLY_TARGET = 10
DEFAULT_NO_RESPONSE_DAYS = 14
DEFAULT_RESEARCH_MAX_AGE_DAYS = 90
DEFAULT_STALE_DAYS = 3
DEFAULT_BRIEF_AUTO_MIN_SCORE = 8

# Environment variables that override the file. INTERPREP_SETTINGS points at another settings file.
ENV_SETTINGS = "INTERPREP_SETTINGS"
ENV_KEYS = {
    "getjo_db": "PREP_GETJO_DB",
    "prep_db": "PREP_DB",
    "private_dir": "PREP_PRIVATE_DIR",
    "dashboard_port": "PREP_DASHBOARD_PORT",
    "weekly_target": "PREP_WEEKLY_TARGET",
    "no_response_days": "PREP_NO_RESPONSE_DAYS",
    "research_max_age_days": "PREP_RESEARCH_MAX_AGE_DAYS",
    "stale_days": "PREP_STALE_DAYS",
    "brief_auto_min_score": "PREP_BRIEF_AUTO_MIN_SCORE",
    "assistant_task": "PREP_ASSISTANT_TASK",
    "assistant_project": "PREP_ASSISTANT_PROJECT",
    "dev3_bin": "PREP_DEV3_BIN",
    "pdf_python": "PREP_PDF_PYTHON",
}
# Keys an env var set to an empty string clears, so `PREP_ASSISTANT_TASK= python -m prep.web` (a demo
# on example data) never pings the standing assistant named in the settings file. Path and number
# keys ignore an empty value: an empty PREP_DB must never mean "the default database".
CLEARABLE = ("assistant_task", "assistant_project", "dev3_bin")


class ConfigError(Exception):
    """Raised when settings are invalid. The message says how to fix it."""


@dataclass(frozen=True)
class Settings:
    getjo_db: Path | None
    prep_db: Path
    private_dir: Path | None
    dashboard_port: int = DEFAULT_DASHBOARD_PORT
    weekly_target: int = DEFAULT_WEEKLY_TARGET
    no_response_days: int = DEFAULT_NO_RESPONSE_DAYS
    research_max_age_days: int = DEFAULT_RESEARCH_MAX_AGE_DAYS
    stale_days: int = DEFAULT_STALE_DAYS
    brief_auto_min_score: int = DEFAULT_BRIEF_AUTO_MIN_SCORE
    assistant_task: int | None = None
    assistant_project: str | None = None
    dev3_bin: Path | None = None
    pdf_python: Path | None = None


def _resolve(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path


def _read_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} is not valid TOML: {e}") from e


def settings_path() -> Path:
    """The settings file to read (it may not exist)."""
    if os.environ.get(ENV_SETTINGS):
        return Path(os.environ[ENV_SETTINGS]).expanduser()
    if SETTINGS_PATH.exists():
        return SETTINGS_PATH
    return USER_SETTINGS_PATH


def _int(data: dict, key: str, default: int, low: int, high: int) -> int:
    value = data.get(key, default)
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = None
    if isinstance(value, bool) or number is None or not low <= number <= high:
        raise ConfigError(f"{key} must be a whole number from {low} to {high}, got {value!r}")
    return number


def load_settings(path: Path | None = None) -> Settings:
    """Return settings from the file (if present) with env vars taking precedence.

    A missing settings file is not an error: getjo_db and private_dir are then None
    and prep_db falls back to data/prep.db in the repo.
    """
    if path is None:
        path = settings_path()
    data = _read_file(path)
    for key, env in ENV_KEYS.items():
        if os.environ.get(env) or (key in CLEARABLE and env in os.environ):
            data[key] = os.environ[env]

    for key in ("getjo_db", "private_dir", "dev3_bin", "pdf_python"):
        value = data.get(key)
        if value and not Path(value).expanduser().is_absolute():
            raise ConfigError(f"{key} must be an absolute path, got {value!r}")

    return Settings(
        getjo_db=_resolve(data["getjo_db"]) if data.get("getjo_db") else None,
        prep_db=_resolve(data.get("prep_db") or DEFAULT_PREP_DB),
        private_dir=_resolve(data["private_dir"]) if data.get("private_dir") else None,
        dashboard_port=_int(data, "dashboard_port", DEFAULT_DASHBOARD_PORT, 1, 65535),
        weekly_target=_int(data, "weekly_target", DEFAULT_WEEKLY_TARGET, 1, 1000),
        no_response_days=_int(data, "no_response_days", DEFAULT_NO_RESPONSE_DAYS, 1, 365),
        research_max_age_days=_int(data, "research_max_age_days", DEFAULT_RESEARCH_MAX_AGE_DAYS, 1, 3650),
        stale_days=_int(data, "stale_days", DEFAULT_STALE_DAYS, 1, 365),
        brief_auto_min_score=_int(data, "brief_auto_min_score", DEFAULT_BRIEF_AUTO_MIN_SCORE, 1, 10),
        assistant_task=_int(data, "assistant_task", 0, 1, 10**9) if data.get("assistant_task") else None,
        assistant_project=_project(data.get("assistant_project")),
        dev3_bin=Path(data["dev3_bin"]).expanduser() if data.get("dev3_bin") else None,
        pdf_python=Path(data["pdf_python"]).expanduser() if data.get("pdf_python") else None,
    )


def _project(value: object) -> str | None:
    """A dev3 project id: letters, digits and dashes only, since it goes on a command line."""
    if not value:
        return None
    text = str(value).strip()
    if not text.replace("-", "").isalnum() or not text.isascii():
        raise ConfigError(f"assistant_project must be a dev3 project id (letters, digits, dashes), got {value!r}")
    return text
