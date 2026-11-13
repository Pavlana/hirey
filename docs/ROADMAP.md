# Roadmap

Four phases as first planned, then agent-first work built outside them. Each phase ends when its
acceptance criteria hold and `python -m unittest` passes in a fresh clone with no private files
and no getjo present.

## Phase 1 — Foundation

Status: done.

Database, config, read-only getjo view and the first three skills.

- `prep.db` with `applications`, `interviews`, `tasks`, `gaps`; created on first use; simple
  column migrations.
- Settings file with absolute `getjo_db`, `prep_db`, `private_dir`: `$INTERPREP_SETTINGS`,
  else `config/settings.toml` (gitignored), else `~/.config/inter_prep/settings.toml`;
  env var overrides per key.
- `prep/getjo.py`: read-only access (URI `mode=ro`) to getjo's `jobs` table; matches at or
  above a score threshold that are not yet applications.
- Skills `/today`, `/applied`, `/task` backed by `python -m prep.cli`.
- Example data built by `python -m prep.seed_example` from committed made-up fixtures.
- Privacy test + `scripts/pre-commit` hook.

Acceptance:
- `python -m unittest` passes in a fresh clone (getjo tests use a fixture database).
- With real settings, `/today` lists real getjo matches ≥7 and reads nothing outside the
  configured paths.
- A write through the getjo connection fails.
- The privacy test passes; no personal file is tracked.

## Phase 2 — Dashboard

Status: done. The Matches, Tasks and Progress pages became the Applications page (Pipeline and
New offers views, `/`) and the Dashboard (`/dashboard`, with practice score trends).

A local website over the same database: standard library `http.server` plus plain
HTML/CSS/JS, no build step, at `http://localhost:8765`.

- Pages:
  - **Pipeline**: applied in the last 7 days vs a weekly target, total applications, reply
    rate (with a warning when it stays below ~10% after 25 applications), overdue next
    actions, counts by status and by tier.
  - **Matches**: getjo matches ≥7 not yet tracked, with a "mark applied" action.
  - **Tasks**: add, tick done.
  - **Progress**: applications per week, stage conversion, tasks done.
- Binds to `127.0.0.1` only. No authentication, no external requests.
- A `launchd` agent (macOS) keeps it running; plist template committed, install steps in
  README. launchd does not read shell profiles, so settings come from
  `~/.config/inter_prep/settings.toml`.

Acceptance:
- Each page renders from a fixture database in tests (handler called directly, no sockets
  needed for unit tests).
- Writes from the dashboard and from the skills are visible to each other without restart.
- The server refuses to bind to a non-loopback address.
- `launchctl` install/uninstall steps work as documented; `localhost:8765` is up after a
  reboot.

## Phase 3 — Prep loop

Status: done, except `/brief` (P3.4). It is not the job brief of a job offer (below).

Practice sessions that turn weak spots into drill tasks.

- `/brief <application>`: a one-page prep brief for an upcoming interview (role, stage,
  likely topics, relevant stories), saved under `private_dir/sessions/`.
- `/mock`: a mock interview run by the agent for a chosen stage; the transcript and a 1–5
  score per area are stored.
- `/debrief`: after a real interview or a mock done elsewhere (a pasted or dictated
  transcript), records an `interviews` row (questions, went well, to improve).
- New `sessions` table (date, kind mock/real, application, stage, scores, file path).
- Gaps found in mocks and debriefs go to `gaps`; each open gap gets a `drill` task.
- Progress shows score trends per area.

Acceptance:
- A mock or debrief creates a session row, at least one gap when weaknesses are named, and a
  linked drill task per new gap.
- Closing a gap closes or completes its drill tasks.
- Session files are written only under `private_dir`; nothing new is tracked by git.
- Tests cover session/gap/task creation with fixture data.

## Phase 4 — Cards and weekly plan

Status: not started.

- New `cards` table: spaced-repetition flashcards (front, back, topic, ease, interval, due
  date, source gap).
- `/cards`: review due cards in the terminal session; grade 0–5; scheduling follows SM-2.
- Gaps can generate cards.
- `/plan-week`: run at the Friday review; proposes next week's tasks from open applications, upcoming interviews, open
  gaps and due cards, within a stated time budget; creates the tasks after confirmation.
- The dashboard gains a Cards page (due count, review history).

Acceptance:
- SM-2 scheduling is unit-tested against known sequences.
- `/plan-week` never creates tasks without confirmation and never exceeds the time budget.
- Cards due today appear in `/today`.

## Built outside the phases

Agent-first work added after the first plan, with no acceptance criteria here. The
[README](../README.md) describes each one; [how-it-works.md](how-it-works.md) has the details.

- Company research and CV fit: queued when a job is marked Interested, done by `/research`.
- Agent box: one conversation with the standing assistant, about one job, several (a
  comparison), the master CV or the whole search (`/ask`).
- Tailored CV: one copy of the CV per job, saved as versions, with a PDF, rule checks and an AI
  check (grounding, fit, skim).
- Job briefs: a short summary of a job description at the top of its panel.
- Master CV history: changes through the agent only, saved versions, diff and restore.
