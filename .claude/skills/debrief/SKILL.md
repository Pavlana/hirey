---
name: debrief
description: Turn a pasted or dictated transcript or notes from a real interview into Hirey records — the interview's questions, what went well and what to improve, and a gap with a drill task for each weakness named. The transcript is saved under private_dir/sessions/. It also completes the "Debrief — what to improve" task. Use when the user types /debrief, pastes or dictates how an interview went, or says "let's debrief the Initech interview".
---

# /debrief

The user pastes a transcript, notes or a spoken summary. This skill records it in one command,
then replies in a few lines. Ask only when the answer would change what gets recorded, one short
question at a time.

Nothing here sends email or replies to anyone. Run commands from the repo root.

## 1. Which application

- A debrief is for a real interview with a company, so it needs the application (`--app ID`):
  `python -m prep.cli apps`, then match the company as /stage does (ignore Ltd, Inc, plc and case;
  two roles at one company → the one the text names, else ask). No match → offer /applied first.
- A practice interview (a friend, a platform) is not recorded: Hirey keeps no practice sessions or
  scores. Say so in one line; its weaknesses can still become gaps with `gap add` if the user wants.
- Stage (`--stage`): `recruiter_call`, `technical` (coding), `system_design` or `final` (company /
  hiring manager / values). Omit it when the application is already at that stage: the command
  uses the application's. After a rejection, pass the stage the interview was.
- Date (`--date`): the day it took place, `YYYY-MM-DD` (`today` by default). When /stage stored that
  interview with this date, its row is filled instead of a new one; no task dates move.

## 2. Read the transcript and pull out

- **Questions**: the questions asked, one per line, short ("Design a rate limiter", "Tell me about a
  conflict"). At most about 15.
- **Went well** and **To improve**: two to five short lines each, in plain words.
- **Gaps** (`--gap`): each concrete weakness worth drilling, as a short topic ("Consistent hashing",
  "STAR stories: shorter", "Big-O of heap operations"). One gap per topic, not per question. Reuse the
  wording of an open gap when it is the same weakness (`python -m prep.cli gap list`): it is matched
  instead of duplicated. Each new gap gets a "Drill: …" task.

Never put pay, salary or anything the user asked to keep out into any field.

## 3. Record it — one command

```bash
python -m prep.cli debrief --app ID [--stage S] --date YYYY-MM-DD \
  --questions 'Q1
Q2' --went-well '…' --to-improve '…' \
  --gap 'Consistent hashing' --gap 'STAR stories: shorter' \
  [--due YYYY-MM-DD] --transcript <<'TRANSCRIPT_END'
<the transcript or notes, as given>
TRANSCRIPT_END
```

- Quote every text value in single quotes: pasted text can hold `$` or backticks, which double
  quotes would expand. Write a `'` inside as `'\''`.
- `--transcript` saves stdin to `private_dir/sessions/DATE-COMPANY-STAGE-debrief.md` (never
  overwriting; the output prints the path). Pass the text as the user gave it; leave out pay.
  With only a short spoken summary, `--transcript` is still worth it: it keeps the notes.
- `--due` sets the drill tasks' due date. Before a next round, the day before it is a good choice;
  otherwise leave it out.
- The application's open "Debrief — what to improve" task is completed by the command.

## 3b. Gap cards — drafted, saved only after a yes

`python -m prep.cli cards gaps --gap ID --gap ID …` (the gap ids the command printed after
`gaps:`) gives a draft per gap with no Learn card yet: JSON with track, chapter, front, an empty back and
`source_gap_id`. For each, write the front as the question to drill and the back as 3 to 6 short
key points on the topic, plus one line on how to answer from cv.md when cv.md backs it. Never
invent the user's experience or numbers.

Show the drafts in the reply (`**<front>** (gap #ID)`, then its key points) and ask: "Save these as Learn cards?"
Save only after a yes, with the user's changes, in one call:

```bash
python -m prep.cli cards add <<'CARDS'
[{"track": "gaps-to-drill", "chapter": "Initech", "front": "…", "back": "- …", "tags": ["gap"],
  "source_gap_id": 12}]
CARDS
```

All or none; a gap that already has a card is refused, so there are no duplicates. The "Gaps to
drill" track is created with its first card.

## 4. The outcome is /stage's job — order matters

If the text also says what happens next, record that as /stage does (`python -m prep.cli update ID
--status S [--interview-date D] --comment '…'`). Do not repeat the debrief in the comment.

- **Next round or an offer**: run `debrief` first, then `update`. The application is still at the
  interview's stage, so the debrief fills the interview row /stage stored and takes its stage.
- **Rejected or withdrawn**: run `update` first, then `debrief` with an explicit `--stage` (the
  application is no longer at an interview stage). The rejection adds the "Debrief — what to
  improve" task, and the debrief then completes it.

## 5. Notes

Add to the application's Notes what helps the next round, as short
`-` bullets: the interview format, whether AI tools were allowed, names and roles, next steps,
questions to ask next time. Skip what the notes already say (`python -m prep.cli notes ID`).

```bash
python -m prep.cli notes ID --append <<'NOTES'
- Format: 45 min, one coding task in a shared editor
- Next: system design round with the team lead
NOTES
```

The command adds a dated "Agent" block and never rewrites the user's text. Never pay.

## 6. Reply

A few lines from the command output. For example:

> Initech coding interview (10 Nov) recorded: 6 questions, 2 to improve.
> New drills: Consistent hashing, STAR stories: shorter. Transcript saved in sessions/.
> Cards to drill (not saved yet): **How does consistent hashing spread keys?** (gap #12) … — save them?

If something failed, say so plainly with the error line.
