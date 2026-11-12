# inter_prep

A personal job-search and interview-prep workspace. It tracks applications, tasks and
interview preparation in one SQLite database (`prep.db`). It is driven through Claude Code
skills in this repo and a local web dashboard (`python -m prep.web`, `localhost:8765`: Applications,
with Pipeline and New offers views, at `/`; Dashboard at `/dashboard`).
Both read and write the same database.

It sits next to **getjo** (job-radar), a separate project that polls public job boards,
scores each job against a CV with Claude, stores jobs in its own SQLite database
(`data/jobs.db`) and sends strong matches to Telegram. inter_prep reads getjo's database
**read-only** to surface matches that are not yet tracked as applications (and, when getjo has
them, its `expires_at` / `last_seen` / `closed_at` columns to hide closed jobs). It never writes
to it and never imports getjo code.

The repository is public. Personal data (CV, notes, sessions, databases, pay) never goes into
a committed file.

## How to work

1. Propose a short plan first: files to touch, functions, data flow, one or two trade-offs.
2. Implement the smallest change that meets the acceptance criteria in `docs/ROADMAP.md`.
3. Add or update tests, then run `python -m unittest`.
4. Keep `docs/how-it-works.md` current when components or data flow change.
5. Finish each task with a 3–5 line summary and a suggested commit message; finish each phase
   with a 5–10 line summary (what was built, where it lives, how to try it).
6. Never commit or push unless asked. Ask before anything outward-facing.

## Rules

- Python 3.11+, standard library only. No frameworks, ORMs or pydantic.
- Type hints on public functions. Small functions. No abstractions before two real uses.
- Settings are read with `tomllib` from `$INTERPREP_SETTINGS`, else `config/settings.toml`
  (gitignored, per-checkout override), else `~/.config/inter_prep/settings.toml` (the normal
  place: shared by worktrees, launchd and the app, no env vars needed). Env vars override keys.
  Paths to getjo and private data are absolute. Never use relative paths such as
  `../getjo`: the code runs from dev3 worktrees, where relative paths point elsewhere.
- getjo's database is opened with SQLite URI `mode=ro`. Never write to it.
- Logging with `logging` to stdout. Never log CV text, personal notes, pay, agent-thread messages
  or request bodies.
- The dashboard never renders `applications.pay`, escapes every database value, and binds to
  loopback only. Agent-thread messages are shown verbatim, so they may contain pay the user pasted.
- Pay/salary is stored only in the local database: `applications.pay`, and agent-thread messages
  (`application_messages`) as the user pasted them. Never in committed files, fixtures, examples,
  logs, commit messages, the dev3 ping, or anything an agent writes (comments, tasks, replies,
  research, fit or tailored CV files) unless the user asks.
- Nothing here applies to jobs, sends email or posts anywhere.
- Committed docs are impersonal (no "you/your") and say nothing about private files' contents.
- Allowed values for status/kind/tier columns are validated in Python (`prep/db.py`), not by
  SQLite CHECK constraints, so later phases can add values without a table rebuild.
- Schema changes: new tables via `CREATE TABLE IF NOT EXISTS`; new columns on existing tables
  via `MIGRATIONS` in `prep/db.py`.
- Tests use `unittest`, never touch the network, the real getjo database or private files.
  getjo tests build a fixture database from `tests/fixtures/example.json`.

## Private files (gitignored, never committed)

`cv.md`, `notes/`, `sessions/`, `companies/`, `fit/`, `cv-tailored/`, `cv-history/`, `mock-interview-kit.md`, `stories.md`, `gaps.md`,
`design-cases.md`, `*.docx`, `*.xlsx`, `*.pdf`, `data/*`, `*.db`, `config/settings.toml`, `.env`,
`.claude/skills/board/`.
They live in the folder named by `private_dir`. Committed templates use the `.example` suffix
(`cv.example.md`, `config/settings.example.toml`). `prep/privacy.py` enforces the list.

## Layout

```
prep/        config.py (settings), db.py (prep.db schema + queries), getjo.py (read-only getjo view),
             cli.py (commands used by skills), stats.py (dashboard numbers), charts.py (SVG charts), web.py (dashboard server),
             research.py (agent result files), cvfile.py (master CV: versions, skills, diff),
             assistant.py (optional dev3 message to a standing agent),
             static/style.css + app.js, seed_example.py (example dbs), privacy.py (tracked-file check)
.claude/skills/  today/, applied/, stage/, task/, research/, ask/, debrief/ — each SKILL.md calls python -m prep.cli <command>
.claude/settings.json  SessionStart hook: read-only `prep.cli queue` (pending research and thread messages, ids only)
tests/       unit tests; fixtures/example.json holds made-up data (fictional companies only)
config/      settings.example.toml (committed template), settings.toml (optional, gitignored)
scripts/     pre-commit (privacy check on staged files)
launchd/     uk.inter-prep.dashboard.plist.example (keeps the dashboard running; install steps in README)
docs/        ROADMAP.md (phases + acceptance), how-it-works.md (components and data flow)
data/        local databases (gitignored)
```

## Commands

- Tests: `python -m unittest`
- Daily brief: `python -m prep.cli today`
- Record an application: `python -m prep.cli applied --job-id ID --next-action "..." --next-date +7`
- Applications: `python -m prep.cli apps`, `python -m prep.cli update ID --status ... [--comment ...]`,
  `python -m prep.cli comment ID "..."`
- Tasks: `python -m prep.cli task add|list|done`
- Practice sessions and gaps: `python -m prep.cli session add --kind mock|real [--app ID] [--stage S]
  [--score AREA=N ...] [--file F] [--gap TEXT ...]`, `session list`, `gap add|list|close`
  (each named gap gets one drill task; closing a gap completes it)
- Debrief (used by /debrief): `python -m prep.cli debrief --kind real|mock [--app ID] [--stage S]
  [--questions T] [--went-well T] [--to-improve T] [--score AREA=N ...] [--gap TEXT ...] --transcript`
  (transcript on stdin, saved under `private_dir/sessions/`)
- Dashboard: `python -m prep.web [--port 8765]` (binds 127.0.0.1 only)
- Example data: `python -m prep.seed_example`, then
  `PREP_ASSISTANT_TASK= PREP_DB=data/example.db PREP_GETJO_DB=$PWD/data/example-getjo.db python -m prep.cli today` or
  `... python -m prep.web --port 8766` (use a port other than 8765 in worktrees; the empty
  `PREP_ASSISTANT_TASK=` keeps it from messaging the real standing assistant)
- Feedback for getjo: `python -m prep.cli feedback export [--out PATH]`
- Research queue: `python -m prep.cli queue` (read-only), `research next|queue|save`, `fit save`
- Agent box (used by /ask): `python -m prep.cli thread show [APP | --job ID | --message MSG] [--claim]`
  (no APP: the whole search; `--job`: a job offer not yet tracked),
  `thread reply --to MSG`, `thread pending`, `review [--all]` (top 3 open tasks in review order)
- Tailored CV (used by /research and /ask): `python -m prep.cli tailor path APP [--role-short X] [--copy]`, then
  `tailor save APP [--to MSG]` (note on stdin); `cv sent APP FILE_NAME`
- Agent edits (used by /ask): `task edit|delete`, `comments list|amend|remove`, `skip APP --reason R`
- Master CV (used by /ask, My CV panel): `python -m prep.cli cv show|skills|history|draft`,
  `cv save --note "..." [--to MSG]` (backup first, atomic), `cv restore V`, `cv diff V`;
  `thread show --cv [--claim]`
- Job briefs (used by /research and /ask): `python -m prep.cli brief next`, `brief save (--job ID | APP)` (text on
  stdin), `brief show`, `brief queue [--redo]`; `thread show --message MSG` prints a comparison (`/ask compare MSG`)
- Privacy check: `python -m prep.privacy` (tracked files) or `--staged`
