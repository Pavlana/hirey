---
name: stage
description: Update an application's stage in inter_prep from free text — a pasted recruiter email, notes or a transcript from a call, or a one-line message such as "Globex invited me to a coding interview on Tuesday" or "Initech rejected me". It finds the application, moves it to the right stage with the interview date, saves a short comment and adds any extra to-dos, then replies in one or two lines. Use when the user types /stage, pastes an email or call notes about an application, or says an interview was booked, moved, passed or failed, an offer arrived, they were rejected, or they withdrew.
---

# /stage

The user does not fill forms. They paste an email or call notes, or say what happened, and
this skill records it. Keep the experience simple: act, then confirm in one or two lines. Ask
only when the answer would change what gets recorded, and ask one short question at a time.

Nothing here sends email or replies to anyone. Run commands from the repo root.

## 1. Find the application

```bash
python -m prep.cli apps
```

Each line reads `#ID Company — Role [stage] next: …`. Match the company from the text: the
sender's company, the signature, an email domain, or the name the user used. Ignore legal
suffixes and case (Ltd, Inc, plc).

- One match → use it.
- Several matches (two roles at one company) → pick the one the text names (role title, job
  reference). If it is still unclear, ask: "Globex — Platform Engineer (#4) or Data Engineer (#9)?"
- No match → say so and offer to record it first with /applied (company and role from the
  text). If the user agrees, run /applied, then continue here with the new id.

## 2. Work out what happened

Stages (`--status` value → what the user sees):

| What the text says | `--status` |
|---|---|
| recruiter / talent / HR screen, intro call, "quick chat" | `recruiter_call` |
| technical interview, coding, live coding, pairing, tech test, take-home review | `technical` (Coding interview) |
| system design, architecture interview | `system_design` |
| final round, onsite, panel, hiring manager or culture/values interview, "last stage" | `final` (Company interview) |
| offer, "we'd like to make you an offer" | `offer` |
| rejection, "decided not to proceed", "other candidates" | `rejected` |
| the user withdrew or declined | `withdrawn` |
| sent the application | `applied` |

- An invitation to a stage moves the application to that stage, with its date if the text
  gives one. The interview has not happened yet; the stage's prep tasks are due the day before.
- "Passed the coding round, next is system design on the 14th" → move to `system_design`
  with that date.
- A take-home test, a second round of the same kind, or a rescheduled call is not a new stage.
  Use the current stage with the new date (the command keeps the date and the comment), or a
  comment alone. A new date moves the stage's open prep tasks to the day before it (today if
  that has passed); the output says "N prep tasks moved to DATE".
- Moving backwards is allowed, but only when the text clearly says so.
- If the stage is genuinely ambiguous ("next steps soon"), record a comment and do not move it.

Dates: convert to `YYYY-MM-DD` using today's date ("Tuesday" is the next Tuesday; "tomorrow"
also works). Put the time and time zone in the comment, not in the date.

## 3. Record it — one command where possible

```bash
python -m prep.cli update ID --status STAGE [--interview-date YYYY-MM-DD] --comment "SHORT SUMMARY"
```

- The comment is a short summary in plain words, at most about 200 characters: who, format,
  time and anything to prepare. Example: "Tue 14:00 UK, 60 min, Zoom with Sam (eng manager):
  live coding in Python + questions about past projects". Never paste the whole email.
- Comment only (no stage change): `python -m prep.cli comment ID "SHORT SUMMARY"`.
- A named contact the application has none of: add `--contact "Name (role)"` to the update.
- Extra to-dos the text asks for, beyond the stage's own prep tasks (send availability, do the
  take-home by Friday, send references):
  `python -m prep.cli task add "Send availability to Sam" --kind admin --due YYYY-MM-DD --app ID`.
  Use kind `prep` for preparation and `admin` for logistics. Don't duplicate a task the update
  output already lists.
- Pay or salary in the text: do not put it in comments or tasks, and do not repeat it in the
  reply. Store it only if the user asks: `--pay "…"` on the update (local database only).

## 4. Reply

One or two lines, from the command output. For example:

> Globex Labs → Coding interview, Tue 10 Nov. Prep tasks due Mon 9 Nov; comment saved.
> Added: Send availability to Sam (due 5 Nov).

If the command printed "Already …: nothing moved", say what was saved instead (comment,
interview date, prep tasks moved). If something failed, say so plainly with the error line.
