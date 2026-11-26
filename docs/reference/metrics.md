# Metrics: application numbers and token usage

[Overview](../how-it-works.md) · [Reference index](README.md)

The Metrics page (`/metrics`) and the definitions behind its numbers: application stats from
`prep/stats.py` and Claude Code token usage and cost from `prep/usage.py` and `prep/prices.py`.

## Contents

- [The Metrics page](#the-metrics-page)
- [Usage tab](#usage-tab)
- [Applications tab](#applications-tab)
- [Definitions](#definitions)
- [Token usage: reading transcripts](#token-usage-reading-transcripts)
- [Places and roles](#places-and-roles)
- [Where to save](#where-to-save)
- [Dollars](#dollars)

## The Metrics page

- `/metrics` has three tabs, as pill links next to the title (`web.metrics_filters`):
  - `tab=usage`, the default: [Usage tab](#usage-tab);
  - `tab=applications`: [Applications tab](#applications-tab);
  - `/metrics/runs`: Run history, see
    [assistants.md](assistants.md#workers-sidebar-and-run-history).
- `/dashboard` and `/progress` redirect to `/metrics`.
- The page never reads pay and shows no message content.
- The nav shows the new offers count next to Applications on every page.

## Usage tab

`web.usage_tab` runs `usage.sync` first (reads appended transcript lines, refreshes dev3 task details
when needed), then `usage.report` for the filters:

| Filter | Values |
| --- | --- |
| `project` | A project name or `all`; default this repo's project (`usage.home_project`). |
| `days` | 7, 14 or 30. |

Top to bottom:

1. **Dollars** (`web.money_section`): the API-price estimate note; Today / Last 7 days / Last 30 days
   tiles; cost per day by role for 30 days; where the money goes.
2. **Who spends what** and **top tasks by cost**, sorted by dollars, following `days`.
3. **Tiles**: tokens, turns, sessions; cache-read share; how many assistants are over
   `context_warn_tokens`.
4. **Tokens per day** (`charts.stacked_columns`, full card width).
5. **Top tasks by cost**.
6. **Who spends what** (`charts.share_bar` and a table).
7. **Context size per turn**: each day's last turn (`charts.trend`).
8. **Where to save**: the rules in `usage.report` ([below](#where-to-save)).

## Applications tab

`web.applications_tab`:

- tiles: sent, response rate (with a warning), interviews, offers;
- applications per week: 12 weeks, with a target line (`weekly_target`), full width;
- the four-step funnel (`stats.stage_funnel`);
- job boards (`web.boards_table`: `getjo.source_stats` over the read-only getjo database).

Charts are server-rendered SVG from `prep/charts.py` ([dashboard.md](dashboard.md)).

## Definitions

Used everywhere the numbers appear (`prep/stats.py`).

- **Submitted**: the application was sent. It has a `date_applied`, or its status (now or in its
  history) is a funnel stage, `rejected` or `no_response`.
- **Funnel**: `applied → recruiter_call → technical → system_design → final → offer`.
  - The furthest stage is the latest of these in the status history plus the current status.
  - A submitted application with none of them (for example only `rejected`) counts as `applied`.
- **Replied**: the furthest stage is `recruiter_call` or later, or the application was `rejected`.
  `applied`, `no_response` and a withdrawal straight from `applied` are not replies.
- **Reply rate**: replied / submitted. A warning shows when it is below 10% after 25 or more
  submitted applications.
- **Stage conversion**: for each funnel stage, the number of submitted applications that reached it
  or a later stage (cumulative, so a skipped stage still counts) and their share of all submitted
  applications.
- **Weeks** start on Monday. "Last 7 days" is today and the six days before, by `date_applied`.
- **Practice**: every `sessions` row (mock and real). Each area's 1–5 scores are in session order
  (date, then id); areas are grouped ignoring case, with the first spelling shown
  ([practice.md](practice.md)).
- **Open gaps** counts every open gap, including those from CV fit checks.
- **Skipped** is closed: never submitted, never a reply ([applications.md](applications.md)).

## Token usage: reading transcripts

The Usage tab reads Claude Code transcripts read-only: every `*.jsonl` below `claude_projects_dir`
(default `~/.claude/projects`).

What is read:

- A line is decoded only when it contains the text `"usage"`.
- Of it, only these are copied out: the timestamp, `cwd`, `sessionId`, `message.id`,
  `message.model`, the four token counts, `cache_creation.ephemeral_1h_input_tokens`
  (`cache_write_1h`, NULL when the line has no split), `usage.speed` and `usage.inference_geo`.
- Message content is never stored, logged or shown.

How it stays fast and exact (tables: [database.md](database.md#token-usage)):

- `usage_files` keeps each file's byte offset after its last complete line, so a page load reads
  only appended bytes. A file that shrank is read again.
- `usage_messages` has one row per `message.id`. A streamed response spans several lines with the
  same id, and each count keeps its largest value, so lines read twice never count twice.
- Adding the `cache_write_1h` column empties `usage_files` once, so transcripts still on disk are
  read again (rows merge by message id).
- Days are local dates.

Terms:

- A **turn** is one API response.
- **Context size** is input + cache read + cache write.

## Places and roles

`usage.place` maps a working directory to (project, 8-character task id):

1. a dev3 worktree: `~/.dev3.0/worktrees/<folder>/<id>/…` or `<checkout>/.dev3/worktrees/<id>/…`;
2. else the dev3 project whose checkout holds it;
3. else the folder name.

`usage.refresh_tasks` fills `usage_projects` and `usage_tasks` from:

- `dev3 projects list`;
- `dev3 tasks list --project P` (only tasks with usage);
- `dev3 task show` (PR merged; open tasks only).

Only ids go on the command line. It runs when a task is unknown or the cache is older than 10 minutes,
and not again within a minute of a failure.

Roles (`usage.ROLES`), checked in this order:

| Role | Which sessions |
| --- | --- |
| Applications Assistant | `assistant_task` seq in this repo's project (or `assistant_project`). |
| Learn Assistant | `prep_assistant_task` seq. |
| Research worker | The `[workers] research` seqs ([configuration.md](configuration.md)). A comms worker counts as a build task. |
| Coordinator | `coordinator_task` seq, or a dev3 task of type coordinator. |
| Build tasks | Other tasks. |
| Main checkouts | Sessions with no dev3 task id (not in a worktree). |

## Where to save

Rules in `usage.report`; each match is a line in the Where to save card:

- an assistant or coordinator whose latest turn is over `context_warn_tokens` (default 300,000);
- an open task whose PR merged (not the assistants or the coordinator, which stay open);
- a session whose cache writes are at least 25% of its tokens and at least 1M tokens.

## Dollars

An estimate at Anthropic's API list prices, not a bill.

### Picking a price

`usage.price` picks a price for one group of responses (same day, place, session, model, speed, region
and long-prompt flag):

1. a `[prices."<model>"]` settings prefix ([configuration.md](configuration.md));
2. else the built-in table in `prep/prices.py`: the exact id, or the id with a date suffix or a
   `[...]` tag. These are Anthropic's list prices, with the source URL and date in the file;
3. else None: "price unknown", counted as $0 and shown separately, never guessed.

Modifiers:

- **Fast mode** scales every price by the fast input price over the standard one.
- **US-only inference** is 1.1x.
- **Claude Haiku 5.5** uses its long-prompt row when input + cache read + cache write is over
  100,000.

### Splitting the cost

`usage.cost` splits the dollars into:

- cache read;
- cache write: the 5-minute part at the 5-minute rate, `cache_write_1h` at the 1-hour rate, an
  unknown split at the 1-hour rate;
- output;
- input.

### What `usage.report` returns

- per task and per role, for the `days` filter;
- under `money`: fixed windows (today, 7 and 30 days), dollars per day per role for 30 days, the
  split, and the unpriced models with their tokens.
