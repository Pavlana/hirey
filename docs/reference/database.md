# Database

[Overview](../how-it-works.md) · [Reference index](README.md)

`prep.db` is one SQLite file (`prep_db` setting), defined in `prep/db.py` (`SCHEMA`, `MIGRATIONS`).
The dashboard, the CLI and every agent read and write it. getjo's `jobs.db` is a separate file, only
ever read ([applications.md](applications.md#matches-from-getjo)).

## Contents

- [Connection](#connection)
- [Schema rules](#schema-rules)
- [Applications](#applications)
- [Research and CV](#research-and-cv)
- [Conversations](#conversations)
- [Runs](#runs)
- [Practice](#practice)
- [Learn](#learn)
- [Allowed values](#allowed-values)
- [Migrations and backfills](#migrations-and-backfills)

## Connection

`db.connect(path)`:

- creates the folder and file when needed;
- sets `PRAGMA busy_timeout = 5000` (`db.BUSY_TIMEOUT_MS`) so writers wait for each other;
- switches the file to WAL once (`PRAGMA journal_mode = WAL`, stored in the file), so readers never
  block; an in-memory database stays as it is;
- turns on `PRAGMA foreign_keys`;
- applies `SCHEMA`, then the [migrations and backfills](#migrations-and-backfills).

The dashboard opens a fresh connection per request. `prep.cli queue` (the SessionStart hook) opens
the file with `mode=ro` and creates nothing. `prep.db-wal` and `prep.db-shm` are gitignored.

## Schema rules

- New tables: `CREATE TABLE IF NOT EXISTS` in `SCHEMA`.
- New columns on existing tables: `MIGRATIONS` (`{table: {column: definition}}`), added with
  `ALTER TABLE` when missing.
- Allowed values for status, kind and tier columns are checked in Python (`prep/db.py`,
  `prep/learn.py`), not by SQLite CHECK constraints, so later work can add values without a table
  rebuild.
- Private text (messages, notes, card content, CV text) lives here and in `private_dir` only, and is
  never logged.

## Applications

| Table | One row per | Key columns |
| --- | --- | --- |
| `applications` | tracked job | `company`, `role`, `getjo_job_id`, `status`, `tier`, `date_applied`, `employer_kind`, `notes`, `pay` |
| `status_history` | creation or status change | `application_id`, `status`, `changed_at`, `comment` |
| `application_comments` | free comment | `application_id`, `body`, `created_at` |
| `interviews` | interview date | `application_id`, `date`, `stage`, `questions`, `went_well`, `to_improve` |
| `tasks` | to-do | `title`, `kind`, `due_date`, `status`, `stage`, `application_id`, `gap_id` |
| `job_decisions` | getjo job triaged | `getjo_job_id` (key), `decision`, `reason`, `comment`, snapshot of company, title, location, score, url, source |

Other `applications` columns: `link`, `type` (contract / permanent), `source`, `last_contact`,
`next_action`, `next_action_date` ([applications.md](applications.md#next-action)), `puzzle_round`
(yes / no / unknown), `contact`, `created_at`, `updated_at`, and from migrations `cv_sent`,
`cv_sent_version`, `cv_sent_at`, `role_short` ([cvs.md](cvs.md)), `notes_updated_at`
([applications.md](applications.md#notes)).

- `pay` is free text that stays in this database only. The dashboard never renders it.
- `tasks.stage` is the stage whose `STAGE_TASKS` created the task; NULL for a hand-made one.

## Research and CV

| Table | One row per | Key columns |
| --- | --- | --- |
| `agent_requests` | queued research, cv_fit, tailor or cv_eval | `application_id`, `kind`, `status`, `force`, `worker`, `reason`, times |
| `company_profiles` | company (`slug`) | `company`, `file_path`, size, recognition, funding, `sources`, `suggested_tier`, `tier_reason`, `researched_at` |
| `cv_fits` | application (latest fit) | `score` 1-5, `top_gaps` (JSON, at most 3), `file_path`, `checked_at` |
| `tailored_cvs` | application (latest version) | `file_path`, `version`, `updated_at` |
| `tailored_cv_versions` | tailored CV version | `application_id`, `version`, `file_path`, `created_at` |
| `tailored_cv_checks` | tailored CV version | `verdict`, `findings` (JSON), `changes` (JSON), `summary` (JSON, AI check), `checked_at` |
| `cv_versions` | master CV version (vN) | `file_name`, `note`, `sha256`, `created_at` |
| `job_briefs` | getjo job | `brief` (Markdown), `updated_at` |
| `brief_requests` | getjo job waiting for a brief | `status`, `force`, `worker`, `reason`, `screen_asked`, times |
| `company_checks` | company (`slug`) for the search gate | `verdict`, `sector_ok`, `interviews`, `kind`, `note`, `source`, `agent_checked_at` |
| `company_aliases` | other name of a company | `alias` (slug), `slug` |
| `job_screens` | getjo job screened for the gate | role family, level, team, location, `pay` (pass / fail / unknown), agents, evals, contract |
| `top_picks` | job in a week's Top 5 | `week` (Monday), `rank`, `why`, copied company, title, score |

- A partial unique index (`idx_agent_requests_open`) allows one open (pending or claimed) request per
  application and kind, even when two requests race.
- `brief_requests` exists because `agent_requests` needs an application, which a new offer does not
  have.
- Allowed values of the search tables are `db.GATE_VALUES`, `db.PAY_GATE`, `db.VERDICTS`,
  `db.COMPANY_KINDS`, `db.CONTRACTS` and `db.CHECK_SOURCES`; pay is never a figure.
- Details: [research.md](research.md), [cvs.md](cvs.md), [search.md](search.md).

## Conversations

| Table | One row per | Key columns |
| --- | --- | --- |
| `agent_messages` | message in either box | `author`, `body`, `status`, `reply_to`, `application_id`, `getjo_job_id`, `topic`, `card_id`, `worker` |
| `agent_message_targets` | target of a message | `message_id`, `application_id`, `getjo_job_id`, `topic` |

- `status` is set on user messages only: pending, claimed, answered, cancelled. Agent replies have NULL.
- `topic`: `cv`, `compare` or `learn`; NULL otherwise.
- Draft requests and drafts also use `comms`, `channel`, `draft_subject`, `draft_body`. A draft request's
  `source_id` is the user's own message holding the recruiter's text (Draft reply on a message, or the
  form left empty); NULL when the text was pasted or none is needed.
- A general message has no target rows; a comparison has 2 to 4.
- Details: [assistants.md](assistants.md), [learn.md](learn.md).

## Runs

`runs`: one row per time a session took a queued item.

| Column | Meaning |
| --- | --- |
| `source` | request, brief or message |
| `item` | the request or message id, or the getjo job id |
| `worker` | the dev3 task seq (`DEV3_TASK_SEQ`), NULL outside dev3 |
| `kind` | a request kind, brief, ask, comms or learn |
| `application_id`, `getjo_job_id`, `card_id`, `topic` | what it was about |
| `queued_at`, `started_at`, `finished_at` | times |
| `result`, `reason` | NULL while running; done, blocked, stopped or lapsed |

Never holds message text or pay. See [assistants.md](assistants.md#workers-sidebar-and-run-history).

## Practice

| Table | One row per | Key columns |
| --- | --- | --- |
| `gaps` | weakness | `date`, `session` (where it came from: `interview`, `cv fit`), `gap`, `status`, `application_id` |

A debrief's notes and transcript path are on its `interviews` row (`questions`, `went_well`,
`to_improve`, `file_path`). A database from before mock sessions were dropped keeps an empty
`sessions` table and `gaps.session_id`; nothing reads or writes them ([practice.md](practice.md)).

Details: [practice.md](practice.md).

## Learn

| Table | One row per | Key columns |
| --- | --- | --- |
| `tracks` | track | `slug`, `title`, `subtitle`, `cover`, `position` |
| `chapters` | chapter of a track | `track_id`, `title`, `position` |
| `cards` | card | `front`, `prompt`, `back`, `tags`, `follow_ups`, `sources` (JSON), `source_gap_id`, `status`, `done_at` |
| `card_source_done` | Learn source ticked done | `card_id`, `source_key`, `done_at` |

Details: [learn.md](learn.md).

## Allowed values

| Constant | Values |
| --- | --- |
| `APPLICATION_STATUSES` | to_apply, applied, recruiter_call, technical, system_design, final, offer, rejected, withdrawn, no_response, skipped |
| `CLOSED_STATUSES` | offer, rejected, withdrawn, no_response, skipped |
| `INTERVIEW_STAGES` | recruiter_call, technical, system_design, final |
| `TIERS` | A, B, C |
| `EMPLOYER_KINDS` | product, client (NULL: unknown) |
| `TASK_KINDS` / `TASK_STATUSES` | apply, prep, drill, admin / todo, done, closed |
| `GAP_STATUSES` | open, closed |
| `DECISIONS` | interested, applied, skipped |
| `SKIP_REASONS` | location, seniority, role_type, stack, company, pay, contract_type, other |
| `REQUEST_KINDS` | research, cv_fit, tailor, cv_eval |
| `REQUEST_STATUSES` | pending, claimed, done, cancelled, blocked |
| `MESSAGE_STATUSES` | pending, claimed, answered, cancelled |
| `COMMS_KINDS` / `CHANNELS` | reply, chase, thanks / linkedin, email |
| `CHECK_VERDICTS` | ready, look, blocked |
| `learn.CARD_STATUSES` | todo, done |

## Migrations and backfills

Columns added by `MIGRATIONS`:

| Table | Columns |
| --- | --- |
| `status_history` | `comment` |
| `tasks` | `stage` |
| `gaps` | `application_id` |
| `interviews` | `file_path` |
| `applications` | `cv_sent`, `cv_sent_version`, `cv_sent_at`, `role_short`, `notes_updated_at`, `employer_kind` |
| `agent_messages` | `getjo_job_id`, `topic`, `card_id`, `comms`, `channel`, `draft_subject`, `draft_body`, `worker`, `source_id` |
| `agent_requests`, `brief_requests` | `worker`, `reason` |
| `brief_requests` | `screen_asked` |
| `tailored_cv_checks` | `summary` |
| `cards` | `prompt` |

One-off steps run when a column is first added or on every connect:

- Adding `tasks.stage` fills it from each auto task's title and turns each open application's typed
  next action into an admin task (`db.next_actions_to_tasks`).
- The token-count tables of the removed Usage & cost page (`usage_files`, `usage_messages`,
  `usage_projects`, `usage_tasks`) are dropped on connect (`db.USAGE_TABLES`).
- `db._retire_application_messages`: old thread rows are copied into `agent_messages` with their ids
  (`INSERT OR IGNORE`), so `reply_to` still holds, then the old table is dropped.
