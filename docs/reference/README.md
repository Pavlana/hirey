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
| [cvs.md](cvs.md) | master CV versions, My CV panel, tailored CV versions, PDF layout, rule check, AI check, verdicts |
| [practice.md](practice.md) | debriefs, gaps and drill tasks |
| [learn.md](learn.md) | tracks and cards, import, gap cards, notes files, the Learn page, Learn Assistant, explainers |
| [dashboard.md](dashboard.md) | server, look, hardening, what `app.js` adds, polling, each page and panel |
| [routes.md](routes.md) | every endpoint, one line each, and old URL redirects |
| [database.md](database.md) | every table, allowed values, migrations and backfills |
| [evals.md](evals.md) | the Evals page: getjo's eval catalogue, run details, history, the promptfoo report, file safety |
| [metrics.md](metrics.md) | Pipeline stats: the page and the definitions behind its numbers |
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

## Keeping the docs in step

`prep/docs_check.py` checks the docs against the code. `python -m unittest` runs it, and GitHub runs it
on every pull request (`.github/workflows/docs.yml`).

| Check | Fails when |
| --- | --- |
| Coverage | a route, setting or env var, table or migrated column, CLI command or skill in the code is not named on its page |
| Links | a relative link, anchor or image points nowhere, or a screenshot in `docs/screenshots/` is unused |
| Changed code (PRs) | code changed but no page that owns it did, per `docs/docs-map.json` |
| Map | changed code under `prep/`, `.claude/skills/` or `scripts/` has no rule in the map |

- `python -m prep.docs_check` runs coverage and links; `--changed origin/main` adds the changed-code
  rule, as CI does.
- A change that needs no doc update says so in the pull request on its own line:
  `Docs: not needed, <reason>`.
- The `/docs-sync` skill (`.claude/skills/docs-sync/SKILL.md`) is the routine before `dev3 pr create`:
  run the check, update the owning pages and diagrams, retake screenshots, check again.
- `python scripts/screenshots.py [NAME ...]` retakes the page-level screenshots on example data and
  lists the ones to retake by hand.

