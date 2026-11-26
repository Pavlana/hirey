# Metrics: Pipeline stats

[Overview](../how-it-works.md) · [Reference index](README.md)

**Pipeline stats** (`/stats`) shows the search's application numbers from `prep/stats.py`, and this
page holds the definitions behind them. Hirey has no cost page: the Usage & cost page (Claude Code
tokens and dollars) was removed, and the Evals page shows no cost ([evals.md](evals.md)).

## Contents

- [Pages and old URLs](#pages-and-old-urls)
- [Pipeline stats page](#pipeline-stats-page)
- [Definitions](#definitions)

## Pages and old URLs

- The nav lists Pipeline stats after Evals. The page has no tabs.
- Run history (`/runs`) is off the nav: the Workers sidebar's "Run history →" and a worker popover's
  "All runs →" link to it ([assistants.md](assistants.md#workers-sidebar-and-run-history)).
- Old URLs redirect (303, `web._moved`):

| Old | New |
| --- | --- |
| `/usage?…`, `/metrics?…`, `/dashboard`, `/progress` | `/stats` |
| `/metrics/runs?…` | `/runs?…` (same filters) |

- The page never reads pay or shows message content.

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
