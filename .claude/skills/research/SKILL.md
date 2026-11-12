---
name: research
description: Do the queued company research, CV fit checks, tailored CVs and job briefs in inter_prep — research what a company does, its size, stack, culture, interview process and recent news, check how cv.md fits the job, set the tier, write a copy of the CV tailored to one job (questions go to the Agent box), check a tailored copy in a separate pass (claims traced to cv.md or the facts bank, fit, six-second skim), write the short job brief shown at the top of a job's panel, and save the results locally. Use when the user types /research [application id] or /research briefs, when /today or the session-start note says agent work or job briefs are pending, or when the user asks to research a company or check the CV against a job.
---

# /research

Interested applications, and the Research company / CV fit / Tailor CV buttons in the dashboard's
Applications panel, queue requests. This skill does the work and saves it. Nothing here applies to a job
or contacts anyone, and nothing leaves the machine except web searches about the company.
Run every command from the repo root.

## Loop

1. `python -m prep.cli research next` claims the oldest waiting application. With an id
   (`/research 12`), use `python -m prep.cli research next 12`. If nothing is queued for that
   id, first run `python -m prep.cli research queue 12`. Add `--kind research` or
   `--kind cv_fit` for only one of them, and `--redo` to research again a profile that is
   still fresh.
2. Follow the printed packet: company research, CV fit, tailored CV and/or AI check of the tailored
   CV, in that order. Each part names its file and the save command.
3. Repeat until it prints "Nothing pending". Then reply in a few lines per application:
   - company: one line on what it does and the tier set
   - CV fit: the score and the top gaps
   - tailored CV: the version saved and how many questions went to the thread
   - AI check: the verdict and how many lines are flagged
   - when a save printed "both results in": the Interested application now has a "Review
     research and apply" task; say it is ready to review (its panel has Mark applied and Skip)

If another session has already claimed the work, leave it alone.

## Job briefs (`/research briefs`)

The dashboard queues a brief for every new offer scored at least `brief_auto_min_score` (8), for
any other offer when its panel is first opened, and when the Write brief / Redo brief button is
pressed. The standing assistant gets `/research briefs` (a count, no ids).

1. `python -m prep.cli brief next` claims the oldest waiting brief and prints the job, the format
   and the description from getjo.
2. Write the brief from the description only (no web search needed) and save it:

   ```bash
   python -m prep.cli brief save --job JOB_ID <<'BRIEF'
   hybrid 2 days · Python · LLM APIs · Senior · London
   ## Summary
   …
   BRIEF
   ```

3. Repeat until it prints "Nothing pending". Reply with one line: how many briefs were written.

The brief is what the user reads to decide Interested or Skip, so it is short and exact:
- The first line is the **key facts**: office days or remote, the 2–3 main skills, seniority,
  location, separated by " · ". No heading before it.
- Then `## Summary` (one line), `## Must-haves`, `## Nice-to-haves`, `## Where & terms`
  (location, office days, seniority, contract), `## Team & product`, `## Interview process` (only
  if the posting states it) and `## Red flags / notable` (only if there are any). Bullets of a few
  words each, in the posting's terms; nothing the posting does not say.
- At most 300 words. **No pay**: no salary, bonus, equity or currency, even when the posting
  states them; `brief save` refuses them.

## Company research → `companies/<slug>.md` (path printed by `research next`)

The packet decides whether to research:
- "reuse it": do not research again. Run the save command as printed. A second role at the same
  company shares the profile.
- Otherwise: research from public sources (the company site, careers and engineering pages,
  news, Companies House or funding databases). Cover:
  - what the company does
  - size
  - tech stack
  - culture
  - interview process (public candidate write-ups)
  - recent news (the last 12 months)
  - London presence and remote policy

  When the packet has a "Job brief" step (a tracked job with no brief yet, or a Redo), write the
  brief from the job description as in "Job briefs" above and save it with the printed
  `brief save` command.

Write the file like this. The `+++` block is TOML and becomes the summary in prep.db; the
Markdown after it is free text:

```
+++
company = "Example Ltd"
what_it_does = "One or two sentences."
sector = "Fintech"
employees = 350
employees_as_of = 2026-01-15
size_band = "251-1000"          # 1-50, 51-250, 251-1000, 1001-5000, 5000+
london_remote = "London office, hybrid 2 days"
recognition = "niche"           # well_known, niche, little_known
recognition_note = "One line on why."
funding = "Series C, 2025"      # or "listed (LSE: EXM)", "bootstrapped", "unknown"
sources = ["https://...", "https://..."]
suggested_tier = "B"
tier_reason = "One line."
researched_at = 2026-01-20
+++

## What it does
## Size and funding
## Stack
## Culture
## Interview process
## Recent news
## Questions worth asking
```

Then run `python -m prep.cli research save <app id>`. It saves the summary, closes the
research request of every application at that company and **sets the tier** of that application
to `suggested_tier`. The panel shows it with `tier_reason`, so write the reason for this role.
No confirm step: the user changes it through the agent if needed.

### Tier rubric (the research sets it; one tier per application)

- **A**: strong fit (CV fit 4–5), and a recognised company or a good employer (stable,
  well-regarded engineering, fair process).
- **B**: decent fit (3), or a good fit at a less known company.
- **C**: weak fit (1–2), or risky (unclear funding, layoffs, poor reviews, unclear role).

When the CV fit is done in the same run, use its score. Otherwise judge the fit from the job
description. The profile is shared by every role at the company, but the tier belongs to one
application: when a reused profile ("reuse it") or a later CV fit points to a different tier for
this role, set it with `python -m prep.cli update <app id> --tier X` and give the reason in your
reply.

## CV fit → `fit/<app-id>-<slug>.md`

Read `cv.md` from the path printed in the packet. If it is missing, stop and ask the user for
it. Compare it with the job description in the packet; if getjo has none, use the job link.

```
+++
score = 4                        # 1 (poor) to 5 (excellent)
gaps = ["Most important gap", "Second", "Third"]   # most important first; first 3 show in the panel
checked_at = 2026-01-20
+++

## Fit summary
## Matching skills
## Missing skills and gaps
## How to adapt the CV
```

Then run `python -m prep.cli fit save <app id>`. Its gaps also go into the gaps list (session
"cv fit"); a gap already open with the same text is not added twice. Name each gap as a short
skill or topic, for example "Kubernetes operators", not a sentence.

## Tailored CV → `cv-tailored/CV_<Company>_<RoleShort>_<DDMMYY>_<n>.md`

A copy of `cv.md` for this one job. The master `cv.md` is never edited here: it changes only when
the user asks for that in the My CV panel (/ask, `cv save`). Read `cv.md` from the path in the packet (missing: stop and
ask the user for it), the CV fit file when there is one (its gaps and "How to adapt the CV"), and
the job description.

**Never invent experience.** Rephrase, reorder, emphasise and cut what `cv.md` says, using the
job's words where they truly apply. Do not add a skill, tool, employer, title, date, number or
achievement that `cv.md` does not state. When the job asks for something `cv.md` is silent or
vague about ("Have you used Terraform?"), leave it out of the copy and ask about it in the note.

### Tailoring rules (here and in /ask revisions)

- **Length**: two pages at most; one is fine. `tailor save` prints the word count and warns above
  1,000 words, then makes the PDF beside the file and prints its page count, with a WARNING above
  two pages. Keep the Markdown plain (headings, paragraphs, `-` bullets, bold, `[text](https://…)`
  links): that is all the PDF template lays out. UK conventions: no photo, date of birth or marital status.
- **Structure**: a 2–3 line summary written for this role; within each role, bullets ordered by
  relevance to the job; older or unrelated roles shrunk to one line; skills grouped by theme with
  the job's must-haves first.
- **Impact**: action + result bullets. When a strong, relevant bullet has no number, ask in the
  thread for an honest figure; never make one up.
- **Same facts in every version**: only selection, order and wording change between versions.
- **Keyword coverage**: the note lists the job's top 5–6 requirements as covered / not covered.
  Covered means stated in the summary or a recent bullet, in the job's own wording when the skill
  is true. The same terms go into the front matter (`keywords`, `gaps`, below).
- **Title**: the job's title (without seniority) appears in the name line or the summary.
- **Plain wording**: write like the user, not like a model. No stock words (spearheaded,
  leveraged, passionate, results-driven, robust, seamless, innovative, …), no dashes inside
  sentences, vary the opening verbs, and keep the figures. The check flags these in rewritten lines.
- **Agreed wording**: `cv-tailored/_style.md` (path in the packet) holds wording agreed in earlier
  threads, such as a one-line explanation of a career gap. Reuse it in every copy. On the first
  tailoring, if `cv.md` has an unexplained gap and the file has no wording for it, ask once in the
  note for the preferred line; when the answer comes (/ask), add it to `_style.md`.

Get the file name first. The first time, pick a short role name for this application (FDE,
AIEng, PlatEng: letters and digits, at most 12) and pass it once; it stays the same for every
version:

```bash
python -m prep.cli tailor path APP --role-short FDE    # later: python -m prep.cli tailor path APP
```

It prints the path of the next version, e.g. `cv-tailored/CV_Globex_FDE_101126_1.md` (DDMMYY is
today, the last number the version). If the save runs on a later day, pass that path with
`tailor save APP --file PATH`. Every version is its own file and is kept; never overwrite
an earlier one. Write the file with the same `+++` front matter as the other files, then the CV
in Markdown:

```
+++
job = "Role at Company"
tailored_at = 2026-01-20
based_on = "cv.md"
keywords = ["Kubernetes", "Terraform", "CI/CD", "SOC 2"]   # the job's must-have terms, as the posting writes them
gaps = ["SOC 2"]                                           # must-haves cv.md cannot truthfully claim
+++

<the tailored CV>
```

`keywords` are copied from the job's must-haves (the brief's `## Must-haves`, else the
description) in the posting's exact wording: applicant tracking systems match words literally,
so "K8s" does not count for "Kubernetes". The check flags every keyword missing from the body
unless it is in `gaps`; never move a term into the body that is not true, list it under gaps.

Then save it, with a short note for the Agent thread on stdin: two or three lines on what was
tailored, then the questions as `-` bullets (plain text; Markdown is not rendered). The note is
posted in the panel's thread, and the user answers there (`/ask` handles the answers):

```bash
python -m prep.cli tailor save APP <<'NOTE'
Tailored for the role: summary leads with cloud security; Azure work moved up; older roles cut to one line each.
Requirements:
- covered: Azure, Kubernetes, incident response, Python
- not covered: Terraform, SOC 2
Questions (the CV does not say):
- Have you used Terraform? If so, where and for how long?
- The migration bullet has no figure: roughly how many services or how much time saved?
NOTE
```

Each save records the file `tailor path` printed as the next version (v1, v2, …) and makes its
PDF (`CV_….pdf` beside it, shown as PDF in the panel). Over two pages: save a shorter version
right away. "no PDF: …" (WeasyPrint not reachable) leaves the save valid: say so in one line;
`python -m prep.cli tailor pdf APP` makes it later. `python -m prep.cli tailor check APP` checks a
version again without WeasyPrint. Every save also queues the AI check of the new version (next
section); never run it in the pass that wrote the CV. The save also prints the automatic check
("Check: Ready", "Look at these lines" or "Blocked", with the flagged lines; the panel shows it
too). Blocked: fix those lines in a new version right away (`tailor path APP --copy`, edit,
`tailor save`). Look at these lines: fix what is wrong; leave what is right (a date from the
facts bank, a true fact) for the user. A Redo ("New
from cv.md" in the panel) writes a new version from the current `cv.md`. Nothing is recorded as
sent until the user says which file they sent (`python -m prep.cli cv sent APP FILE_NAME`, done
by /ask).

## AI check of a tailored CV → `cv-tailored/<version file>.eval.md`

Every tailor save (here or a revision through /ask) queues it; `research next` prints its packet
after the new version is saved, never while a new version is still being written. **Run it as a
separate pass**: hand only that packet to a fresh subagent (Agent tool) with this section, and do
not tell it what was tailored or why. A model that just wrote the CV does not judge its own claims
well. The subagent reads the version file, `cv.md`, the facts bank (true facts not in cv.md;
missing is fine), `_style.md` (wording agreed in the thread) and the job description, writes the
file, and runs `python -m prep.cli tailor eval APP`.

```
+++
version = 2                    # the version in the packet
checked_at = 2026-01-21
fit_score = 4                  # the CV fit rubric (1 poor to 5 excellent) applied to this version
skim_score = 3                 # the six-second skim, 1 to 5
skim_why = "Title and the two most relevant roles are on top; skills come too late"

[[claims]]                     # every claim changed or added against cv.md, one table each
quote = "Built a deployment pipeline used by 12 teams"   # words copied exactly from the version
source = "cv.md"               # cv.md, facts, style or none
note = "Experience, second role, first bullet"           # where it comes from, or what is missing

[[must_haves]]                 # each must-have requirement of the job
term = "Kubernetes"            # in the posting's wording
covered = true                 # stated truthfully in the summary or a recent bullet
+++

Free notes (optional).
```

- **Grounding**: compare the version with `cv.md` line by line. Every reworded, moved-up or added
  statement of fact (a skill, tool, number, date, title, scope, employer, achievement) is a claim.
  Its source is `cv.md` when cv.md says the same or more, `facts` when only the facts bank says it,
  `style` when it is wording agreed in `_style.md`, and `none` otherwise. A changed number, a
  stronger title ("led" for "worked on", "Lead" for "Senior"), a wider scope or a skill that is only
  in the job description is `none`. Each `none` blocks the verdict, so be exact but not petty:
  pure rephrasing of the same fact is sourced. Quote the version's words exactly (they locate the
  line). Agreed facts that are not in cv.md belong in the facts bank: tell the user so when one
  is missing there.
- **Fit**: the CV fit rubric (the CV fit section above) applied to this version against the job,
  scored 1 to 5 without looking at the master CV's score; the code compares the two. List the
  job's must-have requirements as covered or not.
- **Skim**: look only at the top third of page one (name line, title, summary, first role). Does
  a recruiter see in six seconds that this person fits this role? 5: the job's title and two or
  three must-haves are there; 3: the right field but the role is not obvious; 1: it reads as a
  different job. One line of why.
- No pay anywhere in the file.

`tailor eval` maps the file to findings with fixed rules (an unsourced claim blocks; a must-have
not covered, unless the version's front matter already lists it under `keywords` or `gaps`, a fit
below the master CV's, or a skim of 2 or less asks for a look), merges them into the version's
verdict with the rule checks and prints it. A later version saved while the check ran is checked
next. Blocked: write a fixed version (`tailor path APP --copy`) in the main pass and say which
claims were removed or need a fact in the facts bank.

## Rules

- CV text, tailored copies, fit results and research stay in private_dir and prep.db. Never quote the CV in a
  reply beyond a few words. Never write these into the repo, logs or commit messages.
- Do not mention pay, in replies, notes or any file (tailored CVs included).
- Files can be edited by hand. Re-running the save command re-reads the file.
- The dashboard shows each file read-only through the panel's Open links (every tailored version).
