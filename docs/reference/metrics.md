# Metrics: Usage & cost and Pipeline stats

[Overview](../how-it-works.md) · [Reference index](README.md)

Two pages with one purpose each, and the definitions behind their numbers: **Usage & cost** (`/usage`,
Claude Code tokens, cost and response time from `prep/usage.py` and `prep/prices.py`, with the cost and
time of each worker job) and **Pipeline stats** (`/stats`, application numbers from `prep/stats.py`).

## Contents

- [Pages and old URLs](#pages-and-old-urls)
- [Usage & cost page](#usage--cost-page)
- [Pipeline stats page](#pipeline-stats-page)
- [Definitions](#definitions)
- [Token usage: reading transcripts](#token-usage-reading-transcripts)
- [Response time](#response-time)
- [Cost and time per job](#cost-and-time-per-job)
- [Places and roles](#places-and-roles)
- [Where to save](#where-to-save)
- [Dollars](#dollars)

## Pages and old URLs

- The nav lists Usage & cost and Pipeline stats after Evals. Neither page has tabs.
- Run history (`/runs`) is off the nav: the Workers sidebar's "Run history →", a worker popover's
  "All runs →" and the Usage & cost page's jobs card link to it
  ([assistants.md](assistants.md#workers-sidebar-and-run-history)).
- Old URLs redirect (303, `web._moved`):

| Old | New |
| --- | --- |
| `/metrics`, `/metrics?tab=usage&project=P&days=D` | `/usage` with the same `project` and `days` |
| `/metrics?tab=applications`, `/dashboard`, `/progress` | `/stats` |
| `/metrics/runs?…` | `/runs?…` (same filters) |

- Neither page reads pay or shows message content.

## Usage & cost page

`web.usage_page` runs `usage.sync` (reads appended transcript lines, refreshes dev3 task details when
needed), `db.sync_runs`, then `usage.report` and `usage.jobs` for the filters (`web.usage_filters`):

| Filter | Values |
| --- | --- |
| `project` | A project name or `all`; default this repo's project (`usage.home_project`). |
| `days` | 7, 14 or 30 (default 14). |

Headline numbers first, detail below:

1. **Tiles**: cost today, last 7 days, last 30 days (fixed windows); cache hit rate; typical job time
   (median of done jobs, with the slowest 10%).
2. **Where the money goes** (`web.money_summary`): a few sentences from the numbers (total, the role that
   spent most, the costliest model and its typical response, the largest part of the bill, the
   costliest kind of job, the workers' share), then [Where to save](#where-to-save).
3. **Cost by part** (30 days: cache read, cache write, output, input) and **Who spends what** (per role:
   cost, share, tokens, typical response).
4. **Cost and time per kind of job** ([below](#cost-and-time-per-job)), with a link to Run history.
5. **By model**: cost, share, responses, the four token counts, typical and slowest-10% response time.
6. **Cost per day by role** (30 days) and **Tokens per day** (`charts.stacked_columns`).
7. **Most expensive jobs** (top 10 runs) and **Most expensive tasks** (top 10 dev3 tasks).
8. **Context size per response**: each day's last response per assistant, worker or coordinator
   (`charts.trend`).

Cache hit rate is cache read / (input + cache read + cache write): the share of the input read from
the cache.

## Pipeline stats page

`web.stats_page` (`web.applications_tab`):

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
- **Open gaps** counts every open gap, including those from CV fit checks.
- **Skipped** is closed: never submitted, never a reply ([applications.md](applications.md)).

## Token usage: reading transcripts

The Usage & cost page reads Claude Code transcripts read-only: every `*.jsonl` below `claude_projects_dir`
(default `~/.claude/projects`).

What is read:

- A line is decoded only when it contains the text `"usage"`, or (for [response time](#response-time))
  the text `"type":"api-request"` or `"type":"user"`.
- Of a usage line, only these are copied out: the timestamp, `cwd`, `sessionId`, `message.id`,
  `message.model`, the four token counts, `cache_creation.ephemeral_1h_input_tokens`
  (`cache_write_1h`, NULL when the line has no split), `usage.speed` and `usage.inference_geo`.
- Of a request line, only `type` and `timestamp`.
- Message content is never stored, logged or shown.

How it stays fast and exact (tables: [database.md](database.md#token-usage)):

- `usage_files` keeps each file's byte offset after its last complete line, so a page load reads
  only appended bytes. A file that shrank is read again.
- `usage_messages` has one row per `message.id`. A streamed response spans several lines with the
  same id, and each count keeps its largest value, so lines read twice never count twice.
- Adding the `cache_write_1h` or `latency_ms` column empties `usage_files` once, so transcripts still
  on disk are read again (rows merge by message id).
- Days are local dates.

Terms:

- A **turn** is one API response.
- **Context size** is input + cache read + cache write.

## Response time

`usage_messages.latency_ms` (`usage._Latency`, while a file is read):

- Claude Code writes an `api-request` line when it sends a request. A response's start is the latest
  request line before its first line; its response time is its last line's timestamp minus that start.
- A file with no `api-request` lines (older transcripts) uses the latest `user` line instead.
- Unknown (NULL, shown as "–"): no request line before it in the part being read, a negative time, or
  more than 30 minutes (`MAX_LATENCY_MS`).
- It includes streaming, so it measures how long a response takes to finish, not time to first token.
- The page shows the median ("Typical") and the 90th percentile ("Slowest 10%") per model and per role
  (`usage.spread`).

## Cost and time per job

A **job** is a run (`db.runs`): a worker's item from `started_at` to `finished_at`
([assistants.md](assistants.md#workers-sidebar-and-run-history)).

- `usage.run_costs` charges a run with the responses of its worker's dev3 task (the seq in this repo's
  project, `usage.worker_cwds`) whose timestamp, to the second, is inside the run. A response inside
  several runs of one worker is split evenly.
- `usage.jobs` covers the runs finished in the last `days` days, per kind (`workers.KIND_LABELS`):
  - **Done**: runs with result `done`;
  - **Cost**: all its runs, also blocked, stopped and lapsed;
  - **Per job**, **Tokens per job**, **Typical time**: medians over done runs; **Slowest 10%**: their
    90th percentile time.
- Job time is wall-clock time from claim to result, so it includes waiting on tools.
- Runs are recorded since the Workers sidebar shipped; older work has no jobs.

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
| Research worker | The `[workers] research` seqs ([configuration.md](configuration.md)). |
| Comms worker | The `[workers] comms` seqs. |
| Coordinator | `coordinator_task` seq, or a dev3 task of type coordinator. |
| Build tasks | Other tasks. |
| Main checkouts | Sessions with no dev3 task id (not in a worktree). |

## Where to save

Rules in `usage.report`; each match is a line under Where to save in the Where the money goes card:

- an assistant, worker or coordinator whose latest turn is over `context_warn_tokens` (default 300,000);
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

- per task, per role and per model (with response times), the cache hit rate and the total cost, for
  the `days` filter;
- under `money`: fixed windows (today, 7 and 30 days), dollars per day per role for 30 days, the
  split, and the unpriced models with their tokens.
