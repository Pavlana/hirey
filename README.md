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
`PREP_GETJO_DB`, `PREP_DB`, `PREP_PRIVATE_DIR`, `PREP_DASHBOARD_PORT`,
`PREP_WEEKLY_TARGET` and `PREP_NO_RESPONSE_DAYS`. Use absolute paths, with `prep_db`
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
| `/today`   | `python -m prep.cli today [--threshold 7] [--limit 15]`              | Applications moved to No response, tasks due or overdue, new getjo matches |
| `/applied` | `python -m prep.cli applied --job-id ID \| --url URL \| --company C --role R [--status to_apply] [--date D]` | Record an application (applied today, or Interested with `--status to_apply`), linked to getjo when it matches; adds its first tasks |
|            | `python -m prep.cli apps [--status S]`                               | List applications |
|            | `python -m prep.cli update ID [--status S [--comment T] [--interview-date D]] [--next-action T [--next-date D]]` | Move an application to a stage (closes the old stage's tasks, adds the new ones); `--next-action` adds a task |
|            | `python -m prep.cli comment ID "text"`                               | Comment on an application |
| `/task`    | `python -m prep.cli task add "Title" [--kind K] [--due D]` / `task list` / `task done ID` | Tasks |
|            | `python -m prep.cli feedback export [--out PATH]`                    | Write interested/applied/skipped labels as JSONL to `private_dir/feedback/job_decisions.jsonl` for getjo |

Dates accept `YYYY-MM-DD`, `today`, `tomorrow` or `+N` (days from today).

## Dashboard

`python -m prep.web [--port 8765]` serves a local website over `prep.db`, built with the
standard library's `http.server`, plain HTML/CSS and one small optional script for sorting,
filtering and saving without a reload (no build step, no external requests; one dark theme;
charts are server-rendered SVG):

- **Dashboard** (`/`): applied in the last 7 days against `weekly_target`, total applications,
  reply rate (with a warning below 10% after 25 applications), and charts:
  applications by status (donut), tiers split by status (treemap), stage conversion (funnel),
  applications per week (columns with the target). It counts applications only; tasks are a
  to-do list on the Applications page.
  `/progress` redirects here.
- **Job Offers** (`/offers`; `/matches` redirects): every getjo match, filtered by minimum
  score (New only), company, role, location (London / Remote / Other) and New / Interested
  / Applied / Skipped / All;
  filters live in the URL, so a view can be bookmarked. Each new job has three actions:
  **Applied** (application recorded as applied today, same logic as `/applied --job-id`),
  **Interested** (application recorded with status `to_apply`, shown as "Interested") and
  **Skipped** (a pop-up with the skip reasons and an optional comment; without JavaScript a
  plain form page). Applied and Interested open the Applications board with that card
  open and highlighted; an Interested job can later be marked Applied. A skip can be undone.
  Decided jobs also drop out of `/today`.
- **Applications** (`/applications`; `/tasks` redirects): a board with one column per stage
  (Interested, Applied, Recruiter call, Coding interview, System design, Company interview,
  Offer) and a collapsed Closed lane (Rejected, No response, Withdrawn). Each card shows
  days in stage, its next task and a comment count; opened, it lists the tasks (tick done,
  add one), **Move to…** any stage (with an interview date for interview stages and an
  optional comment), a comment box and the history. Moving creates the new stage's tasks and
  closes the old stage's open ones. General tasks (no application) and recently done tasks
  sit below the board.

It binds to `127.0.0.1` only and refuses any other address; requests with a Host header
other than `localhost`/`127.0.0.1` and cross-origin form posts are rejected. Pay is never
shown. Each request opens the database afresh, so changes made through the skills appear on
the next page load. Optional settings: `dashboard_port` (default 8765), `weekly_target`
(applications per week, default 10) and `no_response_days` (default 14: an application
still in Applied with no stage change for that long moves to No response; checked on every
dashboard request and by `/today`).

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
state that launchd does not have.

Everyday commands (run from anywhere):

```bash
# Is it running? (look for "state = running" and the pid)
launchctl print gui/$(id -u)/uk.inter-prep.dashboard | grep -E "state =|pid =|last exit"

# Update to the latest main and restart, so the new code is served
git -C /path/to/inter_prep pull --ff-only
launchctl kickstart -k gui/$(id -u)/uk.inter-prep.dashboard

# Follow the log (request lines and errors; never request bodies)
tail -f /path/to/inter_prep/data/dashboard.log

# Stop until the next login, then start again
launchctl bootout gui/$(id -u)/uk.inter-prep.dashboard
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/uk.inter-prep.dashboard.plist

# Something else holds port 8765?
lsof -nP -iTCP:8765 -sTCP:LISTEN
```

`/path/to/inter_prep` is the main checkout. `kickstart -k` restarts a running job; after
`bootout` the job is unloaded, so use `bootstrap` to start it again (it also starts at the
next login on its own).

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
