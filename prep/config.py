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

Relative paths are resolved against the repo root, never the current directory,
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

# Environment variables that override the file. INTERPREP_SETTINGS points at another settings file.
ENV_SETTINGS = "INTERPREP_SETTINGS"
ENV_KEYS = {
    "getjo_db": "PREP_GETJO_DB",
    "prep_db": "PREP_DB",
    "private_dir": "PREP_PRIVATE_DIR",
    "dashboard_port": "PREP_DASHBOARD_PORT",
    "weekly_target": "PREP_WEEKLY_TARGET",
    "no_response_days": "PREP_NO_RESPONSE_DAYS",
}


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
        if os.environ.get(env):
            data[key] = os.environ[env]

    for key in ("getjo_db", "private_dir"):
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
    )
