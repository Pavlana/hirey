# Hirey

A personal job-search and interview-prep workspace. It tracks applications, tasks and
interview preparation in one SQLite database (`prep.db`). It is driven through Claude Code
skills in this repo and a local web dashboard (`python -m prep.web`, `localhost:8765`: Applications,
with Pipeline and New offers views, at `/`; Learn at `/learn`;
Evals at `/evals`; Pipeline stats at `/stats`).
Both read and write the same database.

It sits next to **getjo** (job-radar), a separate project that polls public job boards,
scores each job against a CV with Claude, stores jobs in its own SQLite database
(`data/jobs.db`) and sends strong matches to Telegram. Hirey reads getjo's database
**read-only** to surface matches that are not yet tracked as applications (and, when getjo has
them, its `expires_at` / `last_seen` / `closed_at` columns to hide closed jobs). It never writes
to it and never imports getjo code.

The repository is public on GitHub as Hirey. Personal data (CV, notes, sessions, databases, pay)
never goes into a committed file, including in history and in PR bodies and comments.

Internal names keep the original name: the Python package `prep`, the settings folder
`~/.config/inter_prep/`, the launchd label `uk.inter-prep.dashboard`, the local checkout folder
`inter_prep` and the dev3 project.

## How to work

1. Propose a short plan first: files to touch, functions, data flow, one or two trade-offs.
2. Implement the smallest change that meets the acceptance criteria in `docs/ROADMAP.md`.
3. Add or update tests, then run `python -m unittest`.
4. Keep the docs current when components or data flow change: update the flow's diagram in
   `docs/how-it-works.md` and the one page in `docs/reference/` that owns the area (see "Writing the docs").
5. Finish each task with a 3–5 line summary and a suggested commit message; finish each phase
   with a 5–10 line summary (what was built, where it lives, how to try it).
6. Never commit or push unless asked. Ask before anything outward-facing.

## Rules

- Python 3.11+. Standard library by default. A third-party library is allowed only when it does
  something hard to do well without it, is widely used and maintained, brings few dependencies,
  is pinned to an exact version in `requirements.txt`, and the PR that adds it says why.
  Frameworks, ORMs and pydantic stay out. Today: WeasyPrint, for the tailored CV PDF only
  (`prep/cvpdf.py` imports it lazily, so everything else runs without it).
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

`cv.md`, `cv-facts.md`, `notes/`, `sessions/`, `companies/`, `fit/`, `cv-tailored/`, `cv-history/`, `learn/`, `mock-interview-kit.md`, `stories.md`, `gaps.md`,
`design-cases.md`, `*.docx`, `*.xlsx`, `*.pdf`, `data/*`, `*.db`, `config/settings.toml`, `.env`,
`.claude/skills/board/`.
They live in the folder named by `private_dir`. Committed templates use the `.example` suffix
(`cv.example.md`, `cv-facts.example.md`, `learn.example.md`, `config/settings.example.toml`). `prep/privacy.py` enforces the list.

## Layout

```
prep/        config.py (settings), db.py (prep.db schema + queries), getjo.py (read-only getjo view),
             cli.py (commands used by skills), stats.py (Pipeline stats numbers), charts.py (SVG charts), web.py (dashboard server),
             research.py (agent result files), cvfile.py (master CV: versions, skills, diff),
             md.py (safe Markdown subset), cvpdf.py + cv_template.html (CV as PDF: one column, skills first, WeasyPrint),
             cvcheck.py (tailored CV rule checks, verdict, changes vs cv.md),
             cveval.py (AI check of a tailored CV: grounding, fit, skim, from the agent's .eval.md),
             assistant.py (optional dev3 message to a standing agent: /ask, /research, /reply, /learn-ask; picks among several workers),
             workers.py (Workers sidebar and Run history: each worker's state, queue and runs; no message text or pay),
             learn.py (Learn tracks, chapters, cards: done or to do), learnnotes.py (Learn notes files),
             explainers.py (Learn explainer pages: private files, getjo staleness check, read only),
             evals.py (Evals page: getjo's eval catalogue and results files, read only),
             static/style.css + app.js + vim.js (Learn editor's vim keys) + explainer.css/.js (shared by explainer pages), seed_example.py (example dbs), privacy.py (tracked-file check),
             docs_check.py (docs in step with the code: coverage, links, changed code needs its page)
.claude/skills/  today/, applied/, stage/, task/, research/, ask/, reply/, learn-ask/, debrief/, explainer/, docs-sync/ — each SKILL.md calls python -m prep.cli <command>
.claude/settings.json  SessionStart hook: read-only `prep.cli queue` (pending research, briefs, thread, draft and Learn messages, worker routing; ids only)
tests/       unit tests; fixtures/example.json holds made-up data (fictional companies only)
config/      settings.example.toml (committed template), settings.toml (optional, gitignored)
scripts/     pre-commit (privacy check on staged files), screenshots.py (retake docs screenshots on example data)
.github/workflows/  docs.yml (docs check on every pull request)
launchd/     uk.inter-prep.dashboard.plist.example (keeps the dashboard running; install steps in docs/usage.md)
docs/        ROADMAP.md (phases + acceptance), how-it-works.md (overview: system diagram, key flows, data model),
             reference/ (one page per area: applications, assistants, research, cvs, practice, learn, dashboard,
             routes, database, evals, metrics, configuration), usage.md (setup and reference),
             learn-format.md (Markdown format of `cards import`; example: learn.example.md), screenshots/
data/        local databases (gitignored)
requirements.txt  pinned third-party libraries (WeasyPrint); install into a venv
```

## Writing the docs

- `docs/how-it-works.md` is the entry point: system diagram, components, one diagram per key flow,
  data model, boundaries. Aim for a 10-minute read; detail goes to `docs/reference/`.
- Each fact lives on the one reference page that owns its area; other pages link to it.
- Diagrams in Mermaid (GitHub renders it): one per flow, labelled arrows, quoted labels.
- No table cell longer than about 200 characters; long detail becomes a subsection or a list.
- A heading every screenful; plain, impersonal sentences; define a term once and link to it.
- Every page starts with the nav line, a short intro and a Contents list.
- Before `dev3 pr create`, run `/docs-sync` (`python -m prep.docs_check --changed origin/main` must
  pass). CI runs the same check; a change that needs no doc update says `Docs: not needed, <reason>`
  in the PR. `docs/docs-map.json` says which page owns which code; `scripts/screenshots.py` retakes
  screenshots on example data. Details: docs/reference/README.md, "Keeping the docs in step".

## Commands

- Tests: `python -m unittest` (includes the docs check)
- Docs check: `python -m prep.docs_check [--changed origin/main]`; screenshots: `python scripts/screenshots.py`
- Daily brief: `python -m prep.cli today`
- Record an application: `python -m prep.cli applied --job-id ID --next-action "..." --next-date +7`
- Applications: `python -m prep.cli apps`, `python -m prep.cli update ID --status ... [--comment ...]`,
  `python -m prep.cli update ID --employer-kind product|client|unknown` (in-house product vs client delivery; unknown clears it),
  `python -m prep.cli comment ID "..."`
- Tasks: `python -m prep.cli task add|list|done`
- Gaps: `python -m prep.cli gap add|list|close` (each named gap gets one drill task; closing a gap completes it)
- Debrief of a real interview (used by /debrief): `python -m prep.cli debrief --app ID [--stage S]
  [--questions T] [--went-well T] [--to-improve T] [--gap TEXT ...] --transcript`
  (transcript on stdin, saved under `private_dir/sessions/`)
- Dashboard: `python -m prep.web [--port 8765]` (binds 127.0.0.1 only); Evals at `/evals` reads `getjo_repo`'s
  `evals/` (example: `PREP_GETJO_REPO=$PWD/data/example-getjo-repo` after `seed_example`)
- Example data: `python -m prep.seed_example`, then
  `PREP_ASSISTANT_TASK= PREP_PREP_ASSISTANT_TASK= PREP_WORKER_RESEARCH= PREP_WORKER_COMMS= PREP_DB=data/example.db PREP_GETJO_DB=$PWD/data/example-getjo.db python -m prep.cli today` or
  `... python -m prep.web --port 8766` (use a port other than 8765 in worktrees;
  the empty `PREP_ASSISTANT_TASK=`, `PREP_PREP_ASSISTANT_TASK=` and `PREP_WORKER_*=` keep it from messaging the real assistants and workers)
- Learn cards: `python -m prep.cli tracks list|add`, `cards add` (JSON on stdin or `--file`),
  `cards import FILE [--dry-run]` (Markdown, `docs/learn-format.md`; real files in `private_dir/learn/`),
  `cards gaps [--gap ID ...] [--app ID]` (read-only drafts for open gaps; saved with `cards add`
  after the user confirms), `cards list [--track T] [--todo|--done]`, `cards show ID`, `cards done ID`, `cards undo ID`, `cards delete ID`;
  `learn-notes CARD_ID|all [--show | --append [TEXT]]` (the Learn page's Notes, private_dir/notes/learn/),
  `learn-notes answer CARD_ID --show` (the card's Your answer, read only)
- Learn Assistant (used by /learn-ask; the Learn page's Learn Assistant box, pinged to `prep_assistant_task`):
  `python -m prep.cli learn-thread show MSG [--claim]`, `learn-thread reply --to MSG` (text on stdin),
  `learn-thread pending`
- Feedback for getjo: `python -m prep.cli feedback export [--out PATH]`
- Research queue: `python -m prep.cli queue` (read-only), `research next|queue|save`, `fit save`,
  `research block APP [--kind K] --reason R` / `brief block --job ID --reason R` (the worker cannot do it; shown in
  the Workers sidebar and Run history, `/runs`)
- Applications Assistant box (used by /ask): `python -m prep.cli thread show [APP | --job ID | --message MSG] [--claim]`
  (no APP: the whole search; `--job`: a job offer not yet tracked),
  `thread reply --to MSG`, `thread close MSG` (a waiting message left without a reply), `thread pending`, `review [--all]` (top 3 open tasks in review order)
- Tailored CV (used by /research and /ask): `python -m prep.cli tailor path APP [--role-short X] [--copy]`, then
  `tailor save APP [--to MSG]` (note on stdin; also makes the PDF and checks it); `tailor pdf APP [--version N]`;
  `tailor check APP [--version N]` (checks a version again without WeasyPrint);
  `tailor eval APP [--version N]` (stores the AI check queued by the panel's AI check button, written in a separate pass);
  `cv sent APP FILE_NAME`
- PDF of any CV Markdown: `.venv/bin/python -m prep.cvpdf cv.example.md data/example-cv.pdf` (`--html` prints the HTML)
- Agent edits (used by /ask): `task edit|delete`, `comments list|amend|remove`, `skip APP --reason R`
- Notes (used by /ask and /research; the panel's Notes card): `python -m prep.cli notes APP [--show | --append [TEXT]]`
  (append text on stdin when TEXT is left out; adds a dated "Agent" block, never rewrites, refuses pay)
- Master CV (used by /ask, My CV panel): `python -m prep.cli cv show|skills|history|draft`,
  `cv save --note "..." [--to MSG]` (backup first, atomic), `cv restore V`, `cv diff V`;
  `thread show --cv [--claim]`
- Job briefs (used by /research and /ask): `python -m prep.cli brief next`, `brief save (--job ID | APP)` (text on
  stdin), `brief show`, `brief queue [--redo]`; `thread show --message MSG` prints a comparison (`/ask compare MSG`)
- Draft a reply (used by /reply; recruiter messages pasted in, nothing sent): `python -m prep.cli reply show MSG [--claim]`,
  `reply save --to MSG [--subject S] [--note N]` (the draft on stdin; email needs a subject); routed to
  `[workers] comms`, else `assistant_task`
- Learn explainers (used by /explainer via /learn-ask; the Learn page's Explainer): `python -m prep.cli explainers path CARD`,
  `explainers save CARD --snippet FILE:QUALNAME ...` (sidecar: getjo commit + hashes, computed by Hirey),
  `explainers check [--card ID]` (read only; exit 1 when any changed); getjo checkout from `getjo_repo`
- Privacy check: `python -m prep.privacy` (tracked files) or `--staged`
