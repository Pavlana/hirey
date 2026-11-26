# Configuration

[Overview](../how-it-works.md) · [Reference index](README.md)

Every setting, where the settings file is found, environment overrides, and the launchd job that
keeps the dashboard running. Setup steps are in [usage.md](../usage.md).

## Contents

- [Where the settings file is](#where-the-settings-file-is)
- [Settings](#settings)
- [Workers table](#workers-table)
- [Prices table](#prices-table)
- [Environment overrides](#environment-overrides)
- [Paths](#paths)
- [launchd](#launchd)

## Where the settings file is

One TOML file, read with `tomllib` (`prep/config.py`). The first of these that applies:

1. `$INTERPREP_SETTINGS`, if set (used as-is, even if missing: tests rely on this);
2. `config/settings.toml` in the checkout, if it exists (gitignored, a per-checkout override);
3. `~/.config/inter_prep/settings.toml`, if it exists.

The per-user file (3) is the normal place, because dev3 worktrees, launchd and the Claude app all find
it without a shell environment. The committed template is `config/settings.example.toml`.

## Settings

| Key | Purpose | Default |
| --- | --- | --- |
| `getjo_db` | getjo's `data/jobs.db`, opened read-only | none: New offers shows a message |
| `prep_db` | Hirey's database | `data/prep.db` in the repo root |
| `private_dir` | folder of private files (CV, notes, sessions, research, …) | none: features that need it say so |
| `dashboard_port` | dashboard port, 1-65535 | 8765 |
| `weekly_target` | applications per week (Metrics target line), 1-1000 | 10 |
| `no_response_days` | days in Applied before No response, 1-365 | 14 |
| `research_max_age_days` | a company profile older than this is researched again, 1-3650 | 90 |
| `stale_days` | a getjo job not listed for longer reads "may be closed", 1-365 | 3 |
| `brief_auto_min_score` | new offers scored at least this get a brief queued in the background, 1-10 | 8 |
| `assistant_task` | dev3 task seq of the Applications Assistant | unset: no ping |
| `prep_assistant_task` | dev3 task seq of the Learn Assistant | unset: no ping |
| `assistant_project` | dev3 project id passed as `--project` (both assistants and workers) | unset |
| `dev3_bin` | absolute path to the dev3 CLI | PATH, then `~/.dev3.0/bin/dev3` |
| `pdf_python` | a Python with WeasyPrint, for tailored CV PDFs | the running Python |
| `facts_file` | the facts bank tailored CV claims may trace to | `private_dir/cv-facts.md` |
| `getjo_repo` | getjo's git checkout, read only: the Learn explainers' check and the Evals page | getjo_db's grandparent when it holds `.git` |
| `claude_projects_dir` | Claude Code transcripts, read only, for the Usage tab | `~/.claude/projects` |
| `coordinator_task` | dev3 task seq of the coordinator (Usage roles) | tasks of dev3 type coordinator |
| `context_warn_tokens` | context per turn above which the Usage tab warns | 300000 |
| `[workers]` | routes queued work to worker tasks | unset: everything to `assistant_task` |
| `[prices."<model>"]` | API prices for the Usage tab's cost estimate | built-in list prices |

## Workers table

```toml
[workers]
research = 71          # or a list: research = [71, 72]
comms = 75
```

- `research`: company research, CV fit, tailored CVs, AI CV checks and job briefs.
- `comms`: Draft a reply requests (`/reply`).
- Each value must be a whole number or a list of them. An unknown route key is a settings error
  (`config.WORKER_ROUTES`).
- How routing and picking work: [Routing](assistants.md#routing) and
  [Several workers on one route](assistants.md#several-workers-on-one-route).

## Prices table

`[prices."<model id or prefix>"]` gives dollars per million tokens for `input`, `output`,
`cache_read`, `cache_write` and `cache_write_1h` (defaults to `cache_write`). They override the
built-in list prices (`prep/prices.py`) for matching models. See [metrics.md](metrics.md).

## Environment overrides

| Env var | Key |
| --- | --- |
| `PREP_GETJO_DB`, `PREP_GETJO_REPO`, `PREP_DB`, `PREP_PRIVATE_DIR` | paths |
| `PREP_DASHBOARD_PORT`, `PREP_WEEKLY_TARGET`, `PREP_NO_RESPONSE_DAYS` | numbers |
| `PREP_RESEARCH_MAX_AGE_DAYS`, `PREP_STALE_DAYS`, `PREP_BRIEF_AUTO_MIN_SCORE` | numbers |
| `PREP_ASSISTANT_TASK`, `PREP_PREP_ASSISTANT_TASK`, `PREP_ASSISTANT_PROJECT`, `PREP_DEV3_BIN` | assistants |
| `PREP_PDF_PYTHON`, `PREP_FACTS_FILE` | CV |
| `PREP_CLAUDE_PROJECTS_DIR`, `PREP_COORDINATOR_TASK`, `PREP_CONTEXT_WARN_TOKENS` | Usage tab |
| `PREP_WORKER_RESEARCH`, `PREP_WORKER_COMMS` | `[workers]` routes; a list as `"71,72"` |

- Each env var overrides one key.
- Set to an empty string, the clearable keys are cleared (`config.CLEARABLE`: `assistant_task`,
  `prep_assistant_task`, `assistant_project`, `dev3_bin`, `coordinator_task`; an empty worker variable
  clears its route too). A demo on example data runs with `PREP_ASSISTANT_TASK= PREP_PREP_ASSISTANT_TASK=`
  and never messages the assistants named in the file.
- An empty path or number override is ignored: an empty `PREP_DB` never means the default.

## Paths

- Paths to getjo and private data are absolute.
- A relative `prep_db` is resolved against the repo root of the running checkout, never the current
  directory, so `prep_db` should be an absolute path outside any worktree.
- Never use relative paths such as `../getjo`: the code runs from dev3 worktrees, where relative paths
  point elsewhere.

## launchd

`launchd/uk.inter-prep.dashboard.plist.example` keeps the dashboard running on macOS.

- It runs `.venv/bin/python -m prep.web` (the main checkout's venv, so a Homebrew `python3` update
  leaves it alone) from the main checkout, with `RunAtLoad` and `KeepAlive`, so the dashboard starts at
  login and restarts if it exits.
- Output goes to the main checkout's `data/dashboard.log` (gitignored).
- launchd starts processes without a shell profile, so the server finds `prep_db` and `getjo_db`
  through `~/.config/inter_prep/settings.toml`.
- `http://localhost:8765/` opens Applications; Metrics is at `/metrics`.
- Install and uninstall steps are in [usage.md](../usage.md).
