"""Load settings from a TOML file, with environment variable overrides.

The settings file is the first of these that applies:
    1. $INTERPREP_SETTINGS, if set (used as-is, even if missing: tests rely on this)
    2. config/settings.toml in this checkout, if it exists
    3. ~/.config/inter_prep/settings.toml, if it exists

The fixed per-user file (3) is what dev3 worktrees, launchd and the Claude app share:
none of them needs a copy of the file or a shell environment.

Keys:
    getjo_db     absolute path to getjo's data/jobs.db (read-only)
    getjo_repo   optional absolute path to getjo's git working copy, read only, for the Learn explainers'
                 staleness check (default: the folder two levels above getjo_db when it holds .git)
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
    assistant_task  optional dev3 task seq of the Applications Assistant (a standing agent): newly queued research
                    and CV fit requests are sent to it with `dev3 message` (unset: no message)
    prep_assistant_task  optional dev3 task seq of the Learn Assistant (a second standing agent), for the Learn page's
                    Learn Assistant: each new message there is sent to it as `/learn-ask` (unset: no message)
    assistant_project  optional dev3 project id, passed with --project when set (both assistants)
    dev3_bin        optional absolute path to the dev3 CLI (default: found on PATH)
    pdf_python      optional absolute path to a Python that has WeasyPrint (a venv outside any
                    worktree), used for tailored CV PDFs when the running Python lacks it
    facts_file      optional absolute path to the facts bank: true facts about the user not in cv.md,
                    which tailored CV claims may trace to (default private_dir/cv-facts.md)
    claude_projects_dir  absolute path to Claude Code's transcripts, read only for token counts on the
                    Metrics page's Usage tab (default ~/.claude/projects)
    coordinator_task  optional dev3 task seq of the coordinator (else tasks of dev3 type coordinator)
    context_warn_tokens  context size per turn above which the Usage tab says "time to /compact"
                    (default 300000)
    [workers]       optional routing of queued work to worker tasks: `research = <dev3 task seq>` sends the
                    pings for company research, CV fit, tailored CVs, AI CV checks and job briefs to that
                    standing task instead of assistant_task (env PREP_WORKER_RESEARCH; unset: assistant_task);
                    `comms = <seq>` sends "Draft a reply" requests (/reply) to a Comms worker (env
                    PREP_WORKER_COMMS; unset: assistant_task)
    [prices."<model id or prefix>"]  optional API prices in dollars per million tokens (input, output,
                    cache_read, cache_write, cache_write_1h) for the Usage tab's cost estimate; they
                    override the built-in list prices (prep/prices.py) for matching models

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
DEFAULT_CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
DEFAULT_CONTEXT_WARN_TOKENS = 300_000
PRICE_KINDS = ("input", "output", "cache_read", "cache_write", "cache_write_1h")  # 1h defaults to cache_write

# Environment variables that override the file. INTERPREP_SETTINGS points at another settings file.
ENV_SETTINGS = "INTERPREP_SETTINGS"
ENV_KEYS = {
    "getjo_db": "PREP_GETJO_DB",
    "getjo_repo": "PREP_GETJO_REPO",
    "prep_db": "PREP_DB",
    "private_dir": "PREP_PRIVATE_DIR",
    "dashboard_port": "PREP_DASHBOARD_PORT",
    "weekly_target": "PREP_WEEKLY_TARGET",
    "no_response_days": "PREP_NO_RESPONSE_DAYS",
    "research_max_age_days": "PREP_RESEARCH_MAX_AGE_DAYS",
    "stale_days": "PREP_STALE_DAYS",
    "brief_auto_min_score": "PREP_BRIEF_AUTO_MIN_SCORE",
    "assistant_task": "PREP_ASSISTANT_TASK",
    "prep_assistant_task": "PREP_PREP_ASSISTANT_TASK",
    "assistant_project": "PREP_ASSISTANT_PROJECT",
    "dev3_bin": "PREP_DEV3_BIN",
    "pdf_python": "PREP_PDF_PYTHON",
    "facts_file": "PREP_FACTS_FILE",
    "claude_projects_dir": "PREP_CLAUDE_PROJECTS_DIR",
    "coordinator_task": "PREP_COORDINATOR_TASK",
    "context_warn_tokens": "PREP_CONTEXT_WARN_TOKENS",
}
# Route keys of the [workers] table (prep/assistant.py ROUTES maps work kinds to them) and their env vars.
WORKER_ROUTES = ("research", "comms")
WORKER_ENV = {route: f"PREP_WORKER_{route.upper()}" for route in WORKER_ROUTES}
# Keys an env var set to an empty string clears, so `PREP_ASSISTANT_TASK= python -m prep.web` (a demo
# on example data, with PREP_PREP_ASSISTANT_TASK= too) never pings the Applications and Learn Assistants named in the settings file. Path and number
# keys ignore an empty value: an empty PREP_DB must never mean "the default database".
CLEARABLE = ("assistant_task", "prep_assistant_task", "assistant_project", "dev3_bin", "coordinator_task")


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
    prep_assistant_task: int | None = None
    assistant_project: str | None = None
    dev3_bin: Path | None = None
    pdf_python: Path | None = None
    facts_file: Path | None = None
    claude_projects_dir: Path = DEFAULT_CLAUDE_PROJECTS_DIR
    coordinator_task: int | None = None
    context_warn_tokens: int = DEFAULT_CONTEXT_WARN_TOKENS
    # (model id or prefix, (input, output, cache_read, cache_write) dollars per million tokens)
    getjo_repo: Path | None = None
    prices: tuple[tuple[str, tuple[float, float, float, float, float]], ...] = ()
    workers: tuple[tuple[str, int], ...] = ()  # (route, dev3 task seq) from [workers]


def worker_task(settings: Settings, route: str) -> int | None:
    """The dev3 task seq set for a [workers] route, or None."""
    return dict(settings.workers).get(route)


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

    for key in ("getjo_db", "getjo_repo", "private_dir", "dev3_bin", "pdf_python", "facts_file", "claude_projects_dir"):
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
        prep_assistant_task=(_int(data, "prep_assistant_task", 0, 1, 10**9)
                             if data.get("prep_assistant_task") else None),
        assistant_project=_project(data.get("assistant_project")),
        dev3_bin=Path(data["dev3_bin"]).expanduser() if data.get("dev3_bin") else None,
        pdf_python=Path(data["pdf_python"]).expanduser() if data.get("pdf_python") else None,
        facts_file=Path(data["facts_file"]).expanduser() if data.get("facts_file") else None,
        claude_projects_dir=(Path(data["claude_projects_dir"]).expanduser() if data.get("claude_projects_dir")
                             else DEFAULT_CLAUDE_PROJECTS_DIR),
        coordinator_task=_int(data, "coordinator_task", 0, 1, 10**9) if data.get("coordinator_task") else None,
        context_warn_tokens=_int(data, "context_warn_tokens", DEFAULT_CONTEXT_WARN_TOKENS, 1000, 10**8),
        getjo_repo=_getjo_repo(data),
        prices=_prices(data.get("prices")),
        workers=_workers(data.get("workers")),
    )


def _workers(value: object) -> tuple[tuple[str, int], ...]:
    """[workers] route = seq, with PREP_WORKER_<ROUTE> overriding (empty clears it)."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ConfigError("workers must be a table: [workers] research = <dev3 task seq>")
    table = dict(value)
    for route, env in WORKER_ENV.items():
        if env in os.environ:
            table[route] = os.environ[env]
    out = []
    for route, seq in table.items():
        if route not in WORKER_ROUTES:
            raise ConfigError(f"workers.{route} is not a known route (known: {', '.join(WORKER_ROUTES)})")
        if seq in ("", None):
            continue
        key = f"workers.{route}"
        out.append((route, _int({key: seq}, key, 0, 1, 10**9)))
    return tuple(sorted(out))


def _getjo_repo(data: dict) -> Path | None:
    """getjo's working copy: getjo_repo, else getjo_db's grandparent (getjo/data/jobs.db) when it is a git
    checkout, else None."""
    if data.get("getjo_repo"):
        return Path(data["getjo_repo"]).expanduser()
    if data.get("getjo_db"):
        guess = Path(data["getjo_db"]).expanduser().parent.parent
        if (guess / ".git").exists():
            return guess
    return None


def facts_path(settings: Settings) -> Path | None:
    """The facts bank: `facts_file` when set, else private_dir/cv-facts.md (None without either)."""
    if settings.facts_file:
        return settings.facts_file
    return settings.private_dir / "cv-facts.md" if settings.private_dir else None


def _prices(value: object) -> tuple[tuple[str, tuple[float, float, float, float, float]], ...]:
    """[prices."model"] tables: input, output, cache_read, cache_write (5-minute) and cache_write_1h in
    dollars per million tokens. A missing cache_write_1h is cache_write; any other missing kind is 0."""
    if not value:
        return ()
    if not isinstance(value, dict):
        raise ConfigError("prices must be a table of [prices.\"<model>\"] tables")
    out = []
    for model, table in value.items():
        if not isinstance(table, dict):
            raise ConfigError(f"prices.{model} must be a table with {', '.join(PRICE_KINDS)}")
        nums = []
        for kind in PRICE_KINDS:
            v = table.get(kind, table.get("cache_write", 0) if kind == "cache_write_1h" else 0)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                raise ConfigError(f"prices.{model}.{kind} must be a number of dollars per million tokens, got {v!r}")
            nums.append(float(v))
        out.append((str(model), tuple(nums)))
    return tuple(sorted(out, key=lambda p: -len(p[0])))  # longest prefix first


def _project(value: object) -> str | None:
    """A dev3 project id: letters, digits and dashes only, since it goes on a command line."""
    if not value:
        return None
    text = str(value).strip()
    if not text.replace("-", "").isalnum() or not text.isascii():
        raise ConfigError(f"assistant_project must be a dev3 project id (letters, digits, dashes), got {value!r}")
    return text
