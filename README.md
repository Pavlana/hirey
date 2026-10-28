# inter_prep

A local, single-user workspace for a job search: tracking applications, follow-ups and
tasks, and (in later phases) interview practice. Data lives in one SQLite database on the
local machine. It is driven by a set of Claude Code skills in this repo and a local web
dashboard at `http://localhost:8765`; both read and write the same database.

It is designed to sit next to **getjo** (job-radar), a separate project that polls job
boards, scores jobs against a CV and stores them in SQLite. inter_prep reads getjo's database
read-only to show strong matches not yet tracked. getjo is optional: without it, everything
except the match list works.

Standard library only, Python 3.11+.

## Setup

```bash
git clone <this repo> inter_prep && cd inter_prep
mkdir -p ~/.config/inter_prep
cp config/settings.example.toml ~/.config/inter_prep/settings.toml
# edit it: absolute paths for getjo_db, prep_db, private_dir
python -m unittest
```

The settings file is looked up in this order:

1. `$INTERPREP_SETTINGS`, if set
2. `config/settings.toml` in the checkout (gitignored), as a per-checkout override
3. `~/.config/inter_prep/settings.toml`

The per-user file is the normal place: every checkout and git worktree, a launchd job and
the Claude app all find it without environment variables. Single keys can be overridden with
`PREP_GETJO_DB`, `PREP_DB`, `PREP_PRIVATE_DIR`, `PREP_DASHBOARD_PORT` and
`PREP_WEEKLY_TARGET`. Use absolute paths, with `prep_db`
outside any worktree: relative paths resolve differently in each worktree.

## Try it with example data

```bash
python -m prep.seed_example
PREP_DB=data/example.db PREP_GETJO_DB=$PWD/data/example-getjo.db python -m prep.cli today
PREP_DB=data/example.db PREP_GETJO_DB=$PWD/data/example-getjo.db python -m prep.web --port 8766
# then open http://localhost:8766
```

The seed shifts the example dates so the most recent ones fall around today. The example databases are built from made-up fixtures in `tests/fixtures/example.json`
(fictional companies only) and are gitignored.

## Commands

| Skill      | CLI                                                                 | What it does |
|------------|---------------------------------------------------------------------|--------------|
| `/today`   | `python -m prep.cli today [--threshold 7] [--limit 15]`              | Overdue next actions, tasks due, new getjo matches |
| `/applied` | `python -m prep.cli applied --job-id ID \| --url URL \| --company C --role R [--next-action T --next-date D]` | Record an application, linked to getjo when it matches |
|            | `python -m prep.cli apps [--status S]`                               | List applications |
|            | `python -m prep.cli update ID [--status S] [--next-action T] [--next-date D]` | Update an application |
| `/task`    | `python -m prep.cli task add "Title" [--kind K] [--due D]` / `task list` / `task done ID` | Tasks |

Dates accept `YYYY-MM-DD`, `today`, `tomorrow` or `+N` (days from today).

## Dashboard

`python -m prep.web [--port 8765]` serves a local website over `prep.db`, built with the
standard library's `http.server` and plain HTML/CSS (no JavaScript, no external requests):

- **Pipeline** (`/`): applied in the last 7 days against `weekly_target`, total applications,
  reply rate (with a warning below 10% after 25 applications), overdue next actions, counts by
  status and tier.
- **Matches** (`/matches`): getjo jobs scored 7 or more not yet tracked, each with a
  "Mark applied" form (same logic as `/applied --job-id`).
- **Tasks** (`/tasks`): add tasks, tick them done, see recently done ones.
- **Progress** (`/progress`): applications per week, stage conversion, tasks done per week.

It binds to `127.0.0.1` only and refuses any other address; requests with a Host header
other than `localhost`/`127.0.0.1` and cross-origin form posts are rejected. Pay is never
shown. Each request opens the database afresh, so changes made through the skills appear on
the next page load. Optional settings: `dashboard_port` (default 8765) and `weekly_target`
(applications per week, default 10).

### Keep it running with launchd (macOS)

The template `launchd/uk.inter-prep.dashboard.plist.example` starts the dashboard at login
and restarts it if it stops. launchd does not read shell profiles, so the dashboard takes its
settings from `~/.config/inter_prep/settings.toml` (set `prep_db` and `getjo_db` there).
Use the main checkout, not a dev3 worktree, which is deleted when its task completes.

Install, from the main checkout:

```bash
sed -e "s|/ABSOLUTE/PATH/TO/python3|$(command -v python3)|" \
    -e "s|/ABSOLUTE/PATH/TO/inter_prep|$PWD|g" \
    launchd/uk.inter-prep.dashboard.plist.example > ~/Library/LaunchAgents/uk.inter-prep.dashboard.plist
plutil -lint ~/Library/LaunchAgents/uk.inter-prep.dashboard.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/uk.inter-prep.dashboard.plist
open http://localhost:8765
```

(`cp` the template and edit the two placeholders by hand if preferred.) The interpreter must
be a real Python 3.11+ binary such as `/opt/homebrew/bin/python3`: if `command -v python3`
prints a pyenv or asdf shim, put the real path in instead, because a shim depends on shell
state that launchd does not have. Status and logs: `launchctl print gui/$(id -u)/uk.inter-prep.dashboard`
and `data/dashboard.log`. After a code update, restart it with
`launchctl kickstart -k gui/$(id -u)/uk.inter-prep.dashboard`.

Uninstall:

```bash
launchctl bootout gui/$(id -u)/uk.inter-prep.dashboard
rm ~/Library/LaunchAgents/uk.inter-prep.dashboard.plist
```

## Privacy model

The repository is public; personal data stays local.

- Personal files (`cv.md`, `notes/`, `sessions/`, `*.docx`, `*.xlsx`, `*.pdf`, databases,
  `config/settings.toml`, `.env`, prep documents) are gitignored. Committed templates use the
  `.example` suffix.
- Pay is stored only in the local database, never in committed files, fixtures, logs or
  commit messages.
- getjo's database is opened read-only (SQLite `mode=ro`); a write attempt fails.
- The dashboard listens on `127.0.0.1` only, makes no external requests and never shows pay.
- Nothing in this project applies to jobs, sends email or posts anywhere.
- `tests/test_privacy.py` fails if `git ls-files` lists any personal file. The same check runs
  as a pre-commit hook on staged files.

### Pre-commit hook

From the repo root, either point git at the `scripts/` folder:

```bash
git config core.hooksPath scripts
```

or copy the hook (works in worktrees too, where `.git` is a file):

```bash
cp scripts/pre-commit "$(git rev-parse --git-path hooks)/pre-commit"
chmod +x "$(git rev-parse --git-path hooks)/pre-commit"
```

Run the check by hand with `python -m prep.privacy` (tracked files) or
`python -m prep.privacy --staged`.

## Docs

- `docs/ROADMAP.md` — phases and acceptance criteria
- `docs/how-it-works.md` — components and data flow
- `CLAUDE.md` — conventions for the Claude Code agent working in this repo
