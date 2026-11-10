# inter_prep

A local, single-user workspace for a job search: tracking applications, follow-ups and
tasks, and (in later phases) interview practice. Data lives in one SQLite database on the
local machine. It is driven by a set of Claude Code skills in this repo and a local web
dashboard at `http://localhost:8765` (it opens on Applications); both read and write the same database.

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
`PREP_WEEKLY_TARGET`, `PREP_NO_RESPONSE_DAYS`, `PREP_RESEARCH_MAX_AGE_DAYS`,
`PREP_ASSISTANT_TASK`, `PREP_ASSISTANT_PROJECT` and `PREP_DEV3_BIN`. Use absolute paths, with `prep_db`
outside any worktree: relative paths resolve differently in each worktree. An empty
`PREP_ASSISTANT_TASK=` (or `PREP_ASSISTANT_PROJECT=`, `PREP_DEV3_BIN=`) clears that key, so a run
on example data never messages the standing assistant; empty path and number overrides are
ignored.

## Try it with example data

```bash
python -m prep.seed_example
PREP_ASSISTANT_TASK= PREP_DB=data/example.db PREP_GETJO_DB=$PWD/data/example-getjo.db python -m prep.cli today
PREP_ASSISTANT_TASK= PREP_DB=data/example.db PREP_GETJO_DB=$PWD/data/example-getjo.db python -m prep.web --port 8766
# then open http://localhost:8766
```

`PREP_ASSISTANT_TASK=` (empty) keeps the example run away from the standing assistant set in the
settings file: its messages and queued research would otherwise be sent to it.

The seed shifts the example dates so the most recent ones fall around today. The example databases are built from made-up fixtures in `tests/fixtures/example.json`
(fictional companies only) and are gitignored.

## Commands

| Skill      | CLI                                                                 | What it does |
|------------|---------------------------------------------------------------------|--------------|
| `/today`   | `python -m prep.cli today [--threshold 7] [--limit 15]`              | Applications moved to No response, tasks due or overdue, new getjo matches |
| `/applied` | `python -m prep.cli applied --job-id ID \| --url URL \| --company C --role R [--status to_apply] [--date D]` | Record an application (applied today, or Interested with `--status to_apply`), linked to getjo when it matches; adds its first tasks |
|            | `python -m prep.cli apps [--status S]`                               | List applications |
| `/stage`   | `python -m prep.cli update ID [--status S [--comment T] [--interview-date D]] [--next-action T [--next-date D]]` | Move an application to a stage (closes the old stage's tasks, adds the new ones); `--next-action` adds a task. Already at that stage: the comment and an interview date are still saved. `/stage` takes a pasted recruiter email, call notes or a one-line message and runs this (plus `comment` / `task add`) itself |
|            | `python -m prep.cli comment ID "text"`                               | Comment on an application |
| `/task`    | `python -m prep.cli task add "Title" [--kind K] [--due D]` / `task list` / `task done ID` | Tasks |
|            | `python -m prep.cli feedback export [--out PATH]`                    | Write interested/applied/skipped labels as JSONL to `private_dir/feedback/job_decisions.jsonl` for getjo |
| `/research` | `python -m prep.cli research next [ID]` / `research queue ID [--kind K] [--redo]` / `research save ID` / `fit save ID` | Queued company research and CV fit checks: the agent writes `companies/<slug>.md` and `fit/<id>-<slug>.md` in `private_dir` and saves a summary |
|            | `python -m prep.cli queue`                                           | Read-only: counts and application ids of queued research (used by the SessionStart hook) |
| `/ask`     | `python -m prep.cli cv show \| skills \| history \| draft` / `cv save --note T [--to MSG]` / `cv restore V` / `cv diff V` | The master CV: print it, its skills by theme, its saved versions; save the edited draft (the current text is saved as a version first), restore or compare a version |

Dates accept `YYYY-MM-DD`, `today`, `tomorrow` or `+N` (days from today).

## Dashboard

`python -m prep.web [--port 8765]` serves a local website over `prep.db`, built with the
standard library's `http.server`, plain HTML/CSS and one small optional script for sorting,
filtering and saving without a reload (no build step, no external requests; one dark theme;
charts are server-rendered SVG):

- **Applications** (`/`, the landing page; `/applications` and `/tasks` redirect here): the
  left column switches between **Pipeline N** and **New offers M** (`?view=pipeline|offers`,
  remembered per browser; a bare `/` opens the view picked last). The nav shows the new offers
  count next to Applications. The Agent box sits under the list in both views; the panel on the
  right shows the selected application or job offer. Details below.
- **Dashboard** (`/dashboard`): applied in the last 7 days against `weekly_target`, total applications,
  reply rate (with a warning below 10% after 25 applications), and charts:
  applications by status (donut), tiers split by status (treemap), stage conversion (funnel),
  applications per week (columns with the target). It counts applications only; tasks are a
  to-do list on the Applications page.
  `/progress` redirects here.
- **New offers** (`/?view=offers`; the old Job Offers page, `/offers` and `/matches` redirect
  here with their filters): open getjo jobs not yet tracked or skipped, newest day first, then
  highest score. Rows: score, company, role, location, with chips "may be closed" (getjo has not
  listed it for more than `stale_days` days) and "expires in N days" (a week or less). Closed and
  expired jobs are left out. Filters in the list header: minimum score (default 7), London /
  Remote / Other and a search on company or role; they live in the URL. **Show skipped** lists
  the skipped jobs at any score with their reason and **Undo skip**. Clicking a row opens the
  offer panel: company, role, score, location, seen date, the posting link, the chips,
  **Interested · Skip**, and the job description from getjo (read-only). **Interested** records
  the application (status `to_apply`, which queues company research and a CV fit), moves it to
  Pipeline → Interested and opens its panel, with an Undo toast that deletes it again while
  nothing else has happened to it. **Skip** asks for a reason and an optional comment (a pop-up;
  without JavaScript a plain form page) and offers Undo. The Agent box's "About" tick works here
  too: a question about a job not yet tracked ("is this remote?", "how does my CV fit?") goes to
  the agent tagged with that job, and follows the job into its application if it is marked
  Interested. Decided jobs also drop out of `/today`.
- **Pipeline** (the default view of `/`): the pipeline list on the left with
  the Agent box under it (below), and the selected application on the right (halves on a
  laptop, a third and two thirds from 1600px wide, a full-screen sheet on phones). On a laptop the page fits the screen: the list (at most
  45% of the left column) scrolls inside with its stage filters and headings kept, and the Agent
  box's input is always in view. The list is grouped by stage (Interested → Applied →
  Recruiter call → Coding interview → System design → Company interview → Offer) with the
  columns Company, Role, Stage, Next task and the latest comment (one line, with the count when
  there are more). A stage strip with counts filters it; closed applications (Rejected, No
  response, Withdrawn, Skipped) show with **Show closed**. Clicking a row opens the panel:
  company, role, stage with interview steps, days in this stage and the job posting; the
  latest comment (and how many came before it); the shortcut buttons **Research company · CV fit · Tailor CV · Mark
  applied · Skip** (each shows its state: "Researching…", "CV fit 4/5", "CV v2 · sent"; Mark
  applied and Skip only while Interested); then the tasks (tick ☐ to mark
  one done, with Undo), the agent's results (company research with the tier and why, CV fit,
  every tailored CV version with Open and Redo) and the stage history. Mark applied records
  Applied today at once and offers Undo, which moves it back to Interested. Skip asks for a
  reason (the same reasons as an offer's Skip) and an optional comment, closes the application as
  Skipped and lists the job under New offers → Show skipped. An Interested or Applied application
  whose getjo job has since closed or expired keeps its place, with a "closed" badge on the row
  and in the panel. Everything else (tasks, comments, stage,
  interview dates, tier, which CV was sent) is asked of the agent in the Agent box. Esc or ✕
  closes the panel. Every task is added, changed or removed through the agent; tasks with no
  application are listed by `/task` and `/today`.

### Company research and CV fit

Marking a job Interested queues company research and a CV fit check for the agent. The panel's
**Research company** and **CV fit** shortcuts queue them by hand; once a result exists, its card
below has **Redo**. Once both results are saved for an application still in
Interested, it gets a "Review research and apply" task, due in two days. The
dashboard never runs the agent. The `/research` skill does the work in a Claude Code session.
It writes `companies/<slug>.md` and `fit/<id>-<slug>.md` to `private_dir`, sets the
application's tier (A/B/C) from the research, and the panel shows the summary (tier and why,
fit score, top gaps) with an **Open** link to a read-only view of the file.

**Tailor CV** asks the agent for a copy of `cv.md` tailored to the job. Each version is its own
file, `private_dir/cv-tailored/CV_<Company>_<RoleShort>_<DDMMYY>_<n>.md` (for example
`CV_Globex_FDE_210926_2.md`), and every version is kept; `cv.md` is never changed. Changes are
asked for in the Agent box and saved as the next version. Telling the agent which file was sent
("sent CV_Globex_FDE_210926_2") marks that version as sent.

Queued work is picked up three ways:

- The committed SessionStart hook (`.claude/settings.json`) lists pending work at the start of
  a session.
- `/today` runs it.
- With `assistant_task = <seq>` (and optionally `assistant_project`, `dev3_bin`) in the
  settings, new requests are sent at once to that long-running dev3 task as
  `/research <id>`.

### Agent box

Under the pipeline list is one **Agent** box: a conversation with the standing assistant for the
whole search. With an application open, a message is about that job ("About Globex — Platform
Engineer", ticked); untick it, or close the panel, to ask about everything. Each message shows
its job as a tag (click it to open the job) or "all jobs"; with a panel open, **This job** shows
only that job's messages. Typical messages:

- "Which tasks need my review?" The agent answers with the 3 most urgent open tasks in a fixed
  order (overdue, due today, interview prep for the nearest interview, ready to apply, upcoming,
  no date), numbered, with a few words on why, how many more there are ("show the rest" lists
  them), and separately what it is still working on. "Done 1 and 3" or "move 2
  to Friday" then does it. `python -m prep.cli review [--all]` prints the same order.
- A question about the company or the fit ("good employer?", "their stack?", "competitors?").
  The agent answers from the research and fit files and the job description, searches the web
  if needed, cites sources, and updates the company file when it learns something new.
- A pasted recruiter email or call notes. The agent does what `/stage` does (stage, interview
  date, short comment, extra to-dos) and replies in a line or two.
- A change: add, edit or remove a task or a comment, move the stage, set the tier, record which
  CV was sent, skip the job, or revise the tailored CV. The agent runs the command and replies in
  one line; the list, the panel and the conversation refresh on their own.

The dashboard only stores the message and pings the assistant with the application (or
"general") and message ids, never the text. It shows "Agent is working…" and refreshes when the
reply arrives. Without an assistant the message waits. The SessionStart hook and `/today`
report it, and `/ask` answers it in any Claude Code session. Pasted emails are treated as data:
the agent never sends email, applies or contacts anyone.

### My CV

**My CV** in the Applications header opens the master CV (`private_dir/cv.md`) in the side panel
(`/?cv=1`; Esc and ✕ close it like a job panel): the skills as chips grouped by theme (one group
per `- **Theme:** a, b; c` line of the Skills section), the CV itself, and its **History**: every
saved version with its date and a one-line note of what changed, each with **Open**, **Compare**
(a line diff against the current CV) and **Restore**. Restore keeps the text it replaces as a
version, so the toast's **Undo** brings it back.

There is no edit box. With the panel open the Agent box reads "About my CV": ask for the change
("add Terraform to my skills", "shorten my summary", "copy the summary from
CV_OpenAI_FDE_210926_2 into my main CV"). The agent edits a draft and saves it with
`cv save --note "…"`, which first saves the current text as a version, then replaces `cv.md`
atomically; it replies with what changed and the panel refreshes. It never invents experience.
Versions live in `private_dir/cv-history/cv-<YYYYMMDD-HHMMSS>.md` with their notes in prep.db,
never in git. A hand edit of `cv.md` shows as "changed since the last version" and is saved as a
version before the next change.

### Standing assistant (optional)

A long-running dev3 task that answers thread messages and does queued research as soon as
they arrive:

1. On the inter_prep project's dev3 board, create a task, for example titled "Standing
   assistant", with this description: "Standing research assistant for inter_prep. When
   messaged `/ask <app> <msg>` run /ask; when messaged `/research <id>` run /research. Between
   messages, wait. Never commit, push, apply to jobs or contact anyone." Start it with Claude
   Code and leave it running.
2. Note its seq number (shown on the card), and the project id if the dashboard runs outside
   that project (`dev3 current` inside the task prints both).
3. Add to `~/.config/inter_prep/settings.toml`:

   ```toml
   assistant_task = 23                  # the task's seq
   # assistant_project = "<project id>" # only when needed
   # dev3_bin = "/absolute/path/to/dev3" # if launchd cannot find dev3 on PATH
   ```

4. Restart the dashboard (`launchctl kickstart -k gui/$(id -u)/uk.inter-prep.dashboard`, or
   stop and start `python -m prep.web`). A message sent from a panel now shows up in that
   task's terminal as `/ask <app> <msg>`.

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
open http://localhost:8765    # Applications; the Dashboard is at /dashboard
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

- Personal files (`cv.md`, `cv-history/`, `notes/`, `sessions/`, `*.docx`, `*.xlsx`, `*.pdf`, databases,
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
