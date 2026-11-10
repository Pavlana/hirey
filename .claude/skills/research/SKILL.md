---
name: research
description: Do the queued company research, CV fit checks and tailored CVs in inter_prep — research what a company does, its size, stack, culture, interview process and recent news, check how cv.md fits the job, suggest a tier, write a copy of the CV tailored to one job (questions go to the Agent thread), and save the results locally. Use when the user types /research [application id], when /today or the session-start note says agent work is pending, or when the user asks to research a company or check the CV against a job.
---

# /research

Interested applications, and the Research / CV fit / Tailor CV buttons on the dashboard's
Applications board, queue requests. This skill does the work and saves it. Nothing here applies to a job
or contacts anyone, and nothing leaves the machine except web searches about the company.
Run every command from the repo root.

## Loop

1. `python -m prep.cli research next` claims the oldest waiting application. With an id
   (`/research 12`), use `python -m prep.cli research next 12`. If nothing is queued for that
   id, first run `python -m prep.cli research queue 12`. Add `--kind research` or
   `--kind cv_fit` for only one of them, and `--redo` to research again a profile that is
   still fresh.
2. Follow the printed packet: company research, CV fit and/or tailored CV, in that order. Each
   part names its file and the save command.
3. Repeat until it prints "Nothing pending". Then reply in a few lines per application:
   - company: one line on what it does and the suggested tier
   - CV fit: the score and the top gaps
   - tailored CV: the version saved and how many questions went to the thread
   - when a save printed "both results in": the Interested application now has a "Review
     research and apply" task; say it is ready to review (the card reads "Ready: fit N/5 ·
     tier X" and its panel has Apply)

If another session has already claimed the work, leave it alone.

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

Then run `python -m prep.cli research save <app id>`. It saves the summary and closes the
research request of every application at that company.

### Tier rubric (suggest only; the user confirms it on the card)

- **A**: strong fit (CV fit 4–5), and a recognised company or a good employer (stable,
  well-regarded engineering, fair process).
- **B**: decent fit (3), or a good fit at a less known company.
- **C**: weak fit (1–2), or risky (unclear funding, layoffs, poor reviews, unclear role).

When the CV fit is done in the same run, use its score. Otherwise judge the fit from the job
description.

## CV fit → `fit/<app-id>-<slug>.md`

Read `cv.md` from the path printed in the packet. If it is missing, stop and ask the user for
it. Compare it with the job description in the packet; if getjo has none, use the job link.

```
+++
score = 4                        # 1 (poor) to 5 (excellent)
gaps = ["Most important gap", "Second", "Third"]   # most important first; first 3 show on the card
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

## Tailored CV → `cv-tailored/<app-id>-<slug>.md`

A copy of `cv.md` for this one job. The master `cv.md` is never edited here: it changes only when
the user asks for that on the CV page. Read `cv.md` from the path in the packet (missing: stop and
ask the user for it), the CV fit file when there is one (its gaps and "How to adapt the CV"), and
the job description.

**Never invent experience.** Rephrase, reorder, emphasise and cut what `cv.md` says, using the
job's words where they truly apply. Do not add a skill, tool, employer, title, date, number or
achievement that `cv.md` does not state. When the job asks for something `cv.md` is silent or
vague about ("Have you used Terraform?"), leave it out of the copy and ask about it in the note.

### Tailoring rules (here and in /ask revisions)

- **Length**: two pages at most; one is fine. `tailor save` prints the word count and warns above
  1,000 words. UK conventions: no photo, date of birth or marital status.
- **Structure**: a 2–3 line summary written for this role; within each role, bullets ordered by
  relevance to the job; older or unrelated roles shrunk to one line; skills grouped by theme with
  the job's must-haves first.
- **Impact**: action + result bullets. When a strong, relevant bullet has no number, ask in the
  thread for an honest figure; never make one up.
- **Same facts in every version**: only selection, order and wording change between versions.
- **Keyword coverage**: the note lists the job's top 5–6 requirements as covered / not covered.
  Covered means stated in the summary or a recent bullet, in the job's own wording when the skill
  is true.
- **Agreed wording**: `cv-tailored/_style.md` (path in the packet) holds wording agreed in earlier
  threads, such as a one-line explanation of a career gap. Reuse it in every copy. On the first
  tailoring, if `cv.md` has an unexplained gap and the file has no wording for it, ask once in the
  note for the preferred line; when the answer comes (/ask), add it to `_style.md`.

Write the file with the same `+++` front matter as the other files, then the CV in Markdown:

```
+++
job = "Role at Company"
tailored_at = 2026-01-20
based_on = "cv.md"
+++

<the tailored CV>
```

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

Each save adds a version (v1, v2, …). A Redo rewrites the copy from the current `cv.md`. When the
application is sent, the dashboard freezes the current copy as `<app-id>-<slug>.sent-v<N>.md`;
never edit that file.

## Rules

- CV text, tailored copies, fit results and research stay in private_dir and prep.db. Never quote the CV in a
  reply beyond a few words. Never write these into the repo, logs or commit messages.
- Do not mention pay, in replies, notes or any file (tailored CVs included).
- Files can be edited by hand. Re-running the save command re-reads the file.
- The dashboard shows each file read-only through the card's Open link.
