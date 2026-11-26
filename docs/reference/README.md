# Reference

[Overview](../how-it-works.md)

Detailed pages behind the [overview](../how-it-works.md). Each page owns one area: its rules,
commands, endpoints and edge cases. Start from the overview's diagrams, then come here for detail.

## Pages

| Page | Covers |
| --- | --- |
| [applications.md](applications.md) | getjo matches, open and closed jobs, triage, stages, stage tasks, no response, comments, notes, review order, `/today` |
| [assistants.md](assistants.md) | standing agents, routing, the `dev3 message` ping, claims and leases, the Applications Assistant box, Resend / Cancel / Stop, drafts, Workers sidebar, run history |
| [research.md](research.md) | the research queue, company profiles, CV fit, tier, blocked work, job briefs, the research file page |
| [cv.md](cv.md) | master CV versions, My CV panel, tailored CV versions, PDF layout, rule check, AI check, verdicts |
| [practice.md](practice.md) | practice sessions, gaps and drill tasks, debriefs, mock interviews |
| [learn.md](learn.md) | tracks and cards, import, gap cards, notes files, the Learn page, Learn Assistant, explainers |
| [dashboard.md](dashboard.md) | server, look, hardening, what `app.js` adds, polling, each page and panel |
| [routes.md](routes.md) | every endpoint, one line each, and old URL redirects |
| [database.md](database.md) | every table, allowed values, migrations and backfills |
| [metrics.md](metrics.md) | application number definitions, token usage, prices |
| [configuration.md](configuration.md) | settings file lookup, every key and env var, launchd |

Setup, demo data and day-to-day commands are in [usage.md](../usage.md). The Markdown format of
`cards import` is in [learn-format.md](../learn-format.md).

## Writing these pages

- One area per page; a fact lives on the page that owns it, and other pages link to it.
- When a component or flow changes, update its diagram in the overview and the one page that owns it.
- Every page starts with the nav line, a one or two sentence intro and a Contents list.
- Diagrams are Mermaid: one per flow, labelled arrows, quoted labels, no exotic syntax.
- No table cell longer than about 200 characters. Long detail becomes a subsection or a list.
- A heading every screenful. Plain, impersonal sentences. Identifiers in backticks.
- Facts come from the code. Nothing about private files' contents.
