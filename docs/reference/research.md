# Research, CV fit and job briefs

[Overview](../how-it-works.md) · [Reference index](README.md)

The agent, not the dashboard, researches companies, checks the CV against a job and writes job
briefs. The dashboard and the CLI only queue requests; the `/research` skill claims them, writes
private Markdown files and saves a summary into prep.db.

## Contents

- [Requests and the queue](#requests-and-the-queue)
- [Claiming work and the packet](#claiming-work-and-the-packet)
- [Company profiles](#company-profiles)
- [CV fit](#cv-fit)
  - [The recommendation](#the-recommendation)
  - [A job offer's CV fit](#a-job-offers-cv-fit)
- [Tier](#tier)
- [Blocked work](#blocked-work)
- [Job briefs](#job-briefs)
- [Research file page](#research-file-page)

## Requests and the queue

Requests live in `agent_requests`, one row per application and kind.

| Kind | What it asks for | Owner page |
| --- | --- | --- |
| `research` | Company research | this page |
| `cv_fit` | How cv.md fits the job | this page |
| `plan` | The tailoring plan, before a tailored CV | [cvs.md](cvs.md#tailoring-plan) |
| `tailor` | A tailored copy of cv.md | [cvs.md](cvs.md#tailored-cv) |
| `cv_eval` | AI check of a tailored copy | [cvs.md](cvs.md#ai-check) |

- Status: `pending` → `claimed` → `done`; also `cancelled` (Stop) and `blocked` (see
  [Blocked work](#blocked-work)). `force` is set for Redo.
- A partial unique index allows one open (pending or claimed) request per application and kind,
  so a double click never queues twice, even when two requests race.
- `db.RESEARCH_KINDS` (`research`, `cv_fit`) are the kinds that entering Interested and
  `research queue` without `--kind` queue. Tailoring is only asked for.

Requests come from:

- Opening an open application's panel (`web._research_on_open`): research and a CV fit, once each when
  missing (never when one was ever asked for the application, `db.requested_kinds`; no research when the
  company has a profile or another application at it has research queued). See
  [dashboard.md](dashboard.md#the-application-panel).
- Entering Interested (`db.stage_tasks`): an offer's Interested button, `/applied --status to_apply`
  and any move to Interested queue research when the company has no profile and a CV fit when the
  application has none and its job has no offer fit to take over (`db.adopt_job_fit`). See
  [applications.md](applications.md).
- The side panel's Redo buttons and Tailor CV (`POST /applications/research`).
- `python -m prep.cli research queue`.

Each new request pings whoever handles research (the Research worker when set, else the
Applications Assistant) with `/research <app id>`; see
[assistants.md](assistants.md#routing).

## Claiming work and the packet

The `/research` skill loops on `python -m prep.cli research next`.

- It claims one application's requests atomically. A claim older than 6 hours
  (`db.CLAIM_HOURS`) counts as pending again; see
  [assistants.md](assistants.md#claims-and-leases).
- The claiming worker's own or unassigned items come first, then another worker's waiting ones.
- It prints a work packet: what to do, where to write it, and the command that saves it.
- Research: a verdict, either reuse a profile younger than `research_max_age_days` (90) or research
  again when the profile is stale, missing or forced.
- CV fit: the cv.md path and the job description, read from getjo with `getjo.job_description`.
- An application recorded from a recruiter's invitation (source "LinkedIn (recruiter)") has no getjo job:
  the packet prints the recruiter's pasted message instead, marked as data (`cli._print_invitation_text`).
  With a criteria file, its research step also asks for the company check with `--job linkedin:<id>`
  when the company has none (`cli._print_invite_check`); see
  [search.md](search.md#recruiters-invitations).
- Job brief: a "Job brief" step when a tracked job has no brief, or on Redo
  (see [Job briefs](#job-briefs)).
- Notes: the application's notes are printed as context ([applications.md](applications.md)). The questions
  for the recruiter call are the panel's, not the notes'
  ([dashboard.md](dashboard.md#red-flags-and-questions)).

## Company profiles

- One file per company: `private_dir/companies/<slug>.md`.
- `db.company_slug`: lowercase words, legal suffixes dropped; a stable key for later company notes.
- Each file starts with TOML front matter between `+++` lines (`prep/research.py`).
- `python -m prep.cli research save` reads it into `company_profiles` (what it does, sector, size,
  London/remote, recognition, funding, sources, suggested tier and reason). A hand edit followed by a
  save updates the summary.
- For the job's panel, all optional and never pay (`db.check_no_pay`): `employer_kind` (product or
  client), one line each on the `engineering_team` and the `interview_style`, `red_flags` and
  `call_questions` (lists of at most 6; the questions are recruiter-call level, never technical, and the
  panel uses at most 2). `red_flags` are concerns only (a team of one, a wide scope, layoffs), never a
  restated criteria check: a failed check shows as "Outside your criteria" ([dashboard.md](dashboard.md)).
- A company save closes the research requests of every application at that company and sets
  `applications.employer_kind` where it is unknown (a kind already set is kept). The panel has no
  employer control; `update ID --employer-kind` still changes it.
- Something new about the company found while answering in the Applications Assistant box also goes
  into the file, then `research save`.

## CV fit

- One file per application: `private_dir/fit/<app-id>-<slug>.md`, with the same `+++` TOML front
  matter.
- `python -m prep.cli fit save` reads it into `cv_fits` (score 1-5, top gaps, at most 3).
- The fit save adds its gaps to `gaps` (session "cv fit", `gaps.application_id`), skipping any gap
  already open with the same text (ignoring case and spacing). These gaps get no drill task until a
  session or `gap add` names them; see [practice.md](practice.md).
- The CV fit only scores and recommends. Nothing is edited there; tailoring is a separate request.
- No task follows the results (the "Review research and apply" task was retired on 27 Nov 2026): the
  panel shows the next step itself.

### The recommendation

The fit file's `## Recommendation` section, written by the Research worker with the criteria file open,
is parsed by `research.read_recommendation` into `cv_fits.recommendation` (JSON) and shown at the top of
the job's panel ([dashboard.md](dashboard.md#the-application-panel)):

```
## Recommendation
Verdict: Stretch: apply only if cheap — one sentence why.
### Gaps that matter less
- a gap: why it is soft
### Gaps that are real
- a gap: why it matters, how it may show up in the interview
### In its favour (by the criteria)
### What would decide it
### If you apply
### What the role needs
- ✓ a must-have, a few plain words   (~ partly, ✗ not yet)
```

- The verdict is Apply, Stretch (shown "Stretch: apply only if cheap") or Skip (`research.VERDICTS`); a
  section without one is refused. Parts are matched by their first words, at most 5 bullets each.
- The skill weighs the gaps against the criteria's ranking; the gap count alone never decides it. Claims
  trace to cv.md or the facts bank, as in the fit; pay is refused.
- "What the role needs" bullets start with a mark (`research.read_need`: ✓ have, ~ partly, ✗ not yet; a
  bullet without one has no mark). The panel shows them as section 1's checklist, not in the recommendation.
- A fit file without the section saves as before (no recommendation).
- The panel shows every fit out of 10, as the lists do (`search.fit_on_ten`: the 1-5 doubled); the 1-5 stays
  in the fit file and `cv_fits`.

### A job offer's CV fit

- A job offer not tracked yet gets its CV fit, recommendation included, when its panel first opens: the
  brief packet prints "CV fit of this job offer" (`cli._print_job_fit_step`) with
  `private_dir/fit/job-<job>-<slug>.md` (`research.job_fit_file`) and `fit save --job ID`.
- `db.save_job_fit` stores it in `job_fits`, keyed by the job id; no application is made and its gaps
  wait. An offer briefed before job fits gets its brief request queued again once for the fit
  (`db.queue_job_fit`, `brief_requests.fit_asked`).
- Interested, or the first opening of the application's panel, copies it to `cv_fits` with its gaps
  (`db.adopt_job_fit`): no second fit.

## Tier

The research sets the tier; the rubric is in the `/research` skill.

| Tier | Meaning |
| --- | --- |
| A | Strong fit at a recognised or good employer |
| B | Decent fit, or a less known employer |
| C | Weak fit or risky |

- `research save APP` copies the profile's `suggested_tier` into `applications.tier` of that
  application only. A profile is shared by every role at the company; the tier depends on each
  role's fit.
- The panel's company card shows the tier with the profile's `tier_reason`. The reason shows only
  while the tier still matches the profile.
- There is no confirm step. The user changes the tier through the agent
  (`python -m prep.cli update APP --tier X`).

## Blocked work

A worker that cannot do a queued item marks it blocked instead of leaving it claimed.

| Command | Effect |
| --- | --- |
| `research block APP [--kind K] --reason R` | Marks the application's open requests (all, or one kind) `blocked` with the reason |
| `brief block --job ID --reason R` | Marks the job's open brief request `blocked` with the reason |

- The reason is a few words, required, length-capped, and refused when it mentions pay
  (`db.check_no_pay`).
- Blocked rows are kept; queueing again adds a new request.
- The blocked reason shows in the Workers sidebar and Run history; see
  [assistants.md](assistants.md#workers-sidebar-and-run-history).

## Job briefs

A short structured summary of a getjo job, written by the agent from the description and shown at
the top of the job's panel (offers and applications linked to a getjo job), so Interested or Skip is
decided quickly. With a criteria file, the same request also carries the job's screen and, when needed,
its company check and profile ([search.md](search.md#agent-work)).

### Format

Stored per getjo job in `job_briefs` as Markdown:

1. The key facts line first, e.g. "hybrid 2 days · Python · LLM APIs · Senior · London".
2. `## Summary`
3. `## Must-haves`
4. `## Nice-to-haves`
5. `## Where & terms`
6. `## Team & product`
7. `## Interview process`, when the posting states it.
8. `## Red flags / notable`: concerns, not the user's criteria (level, sector, location, interview
   style, pay are checks, not red flags).

`db.save_brief` refuses:

- a heading before the facts line;
- a facts line over `db.MAX_FACTS` (200) characters;
- more than `db.MAX_BRIEF_WORDS` (300) words;
- anything that mentions pay (`db.PAY_PATTERN`: currency signs, "80k", salary, pay, bonus,
  compensation…).

### Requests

Requests live in `brief_requests`, one row per getjo job, since `agent_requests` needs an
application. Status `pending` → `claimed` → `done`; `force` for Redo; claims go stale after 6 hours;
also `cancelled` (Stop) and `blocked`.

Who asks:

- The Applications page queues one for each new offer scored at least `brief_auto_min_score` (8)
  that has neither a brief nor a request (`db.queue_missing_briefs`).
- An untracked offer queues one the first time its panel is opened.
- The Write brief / Redo brief button posts `/offers/brief` (`db.queue_brief`).
- `/research` writes the brief of a tracked job during company research.
- `/ask` writes one when asked ("key facts").

When the page queued any, it sends `/research briefs` once to whoever handles research (a count, no
ids; `assistant.notify_briefs_in_background`).

### Commands

| Command | What it does |
| --- | --- |
| `brief next` | Claims the oldest request; prints the job, the format and the description |
| `brief save (--job ID \| APP)` | Saves the brief (text on stdin) |
| `brief show` | Prints one brief |
| `brief queue [--redo]` | Queues a request |
| `brief block --job ID --reason R` | See [Blocked work](#blocked-work) |

`queue` and `today` add a "Job briefs: N waiting" line. `thread show` prints the brief for an
application, a job offer and each target of a comparison.

### In the panel

- While a brief is being written the panel shows "Writing brief…" and is busy (`data-busy`), so the
  page polls `/applications/thread?job=ID` (or `?app=ID`) until it is in.
- With a brief: the facts line shows as chips, the sections go through `render_markdown` (escaped),
  and the original description sits behind Show original.
- Without a brief: the original description made readable (`web._description`):
  - "----" separators dropped;
  - a short line opening a part, or ending in ":" or in capitals, becomes a heading;
  - "-", "•" and "1." lines become lists;
  - other lines become paragraphs;
  - everything escaped;
  - folded to a few lines with Show all (the script; in full without it).

## Research file page

`GET /research/file?app=ID&kind=research|cv_fit|tailor[&v=N]` shows the application's company
profile, CV fit file or tailored CV (the latest, or version N), read only.

- Front matter as a table, any pay/salary key left out.
- The body as minimal Markdown (headings, lists, paragraphs, all escaped).
- Only a path recorded in prep.db that resolves inside `private_dir` is read. Anything else (no
  record, no `private_dir`, a path outside it) is a 404.
- A missing file shows "File missing".
- It writes nothing.
