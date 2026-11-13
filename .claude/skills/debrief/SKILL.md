---
name: debrief
description: Turn a pasted or dictated transcript or notes from a real interview, or from a mock interview done elsewhere, into Hirey records — the interview's questions, what went well and what to improve, a practice session with 1–5 scores per area, and a gap with a drill task for each weakness named. The transcript is saved under private_dir/sessions/. It also completes the "Debrief — what to improve" task. Use when the user types /debrief, pastes or dictates how an interview or a mock went, or says "let's debrief the Initech interview".
---

# /debrief

The user pastes a transcript, notes or a spoken summary. This skill records it in one command,
then replies in a few lines. Ask only when the answer would change what gets recorded, one short
question at a time.

Nothing here sends email or replies to anyone. Run commands from the repo root.

## 1. Real or mock, and which application

- **Real** (`--kind real`): an interview with a company. It needs the application:
  `python -m prep.cli apps`, then match the company as /stage does (ignore Ltd, Inc, plc and case;
  two roles at one company → the one the text names, else ask). No match → offer /applied first.
- **Mock** (`--kind mock`): a practice interview done elsewhere (a friend, a platform). Add `--app ID`
  only when it was practice for that application. A mock gets no interview row; its notes stay in
  the transcript file.
- Stage (`--stage`): `recruiter_call`, `technical` (coding), `system_design` or `final` (company /
  hiring manager / values). Omit it for a real interview when the application is already at that
  stage: the command uses the application's. After a rejection, pass the stage the interview was.
- Date (`--date`): the day it took place, `YYYY-MM-DD` (`today` by default). When /stage stored that
  interview with this date, its row is filled instead of a new one; no task dates move.

## 2. Read the transcript and pull out

- **Questions**: the questions asked, one per line, short ("Design a rate limiter", "Tell me about a
  conflict"). At most about 15.
- **Went well** and **To improve**: two to five short lines each, in plain words.
- **Scores** (`--score AREA=N`, N from 1 to 5): only for areas the text lets you judge — for example
  coding, problem solving, system design, communication, behavioural, domain. Use the interviewer's
  feedback when there is any; otherwise judge from the answers and say the scores are your estimate.
- **Gaps** (`--gap`): each concrete weakness worth drilling, as a short topic ("Consistent hashing",
  "STAR stories: shorter", "Big-O of heap operations"). One gap per topic, not per question. Reuse the
  wording of an open gap when it is the same weakness (`python -m prep.cli gap list`): it is matched
  instead of duplicated. Each new gap gets a "Drill: …" task.

Never put pay, salary or anything the user asked to keep out into any field.

## 3. Record it — one command

```bash
python -m prep.cli debrief --kind real --app ID [--stage S] --date YYYY-MM-DD \
  --questions 'Q1
Q2' --went-well '…' --to-improve '…' \
  --score coding=3 --score communication=4 --gap 'Consistent hashing' --gap 'STAR stories: shorter' \
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

## 4. The outcome is /stage's job — order matters

If the text also says what happens next, record that as /stage does (`python -m prep.cli update ID
--status S [--interview-date D] --comment '…'`). Do not repeat the debrief in the comment.

- **Next round or an offer**: run `debrief` first, then `update`. The application is still at the
  interview's stage, so the debrief fills the interview row /stage stored and takes its stage.
- **Rejected or withdrawn**: run `update` first, then `debrief` with an explicit `--stage` (the
  application is no longer at an interview stage). The rejection adds the "Debrief — what to
  improve" task, and the debrief then completes it.

## 5. Reply

A few lines from the command output. For example:

> Initech coding interview (10 Nov) recorded: 6 questions, scores coding 3/5, communication 4/5.
> New drills: Consistent hashing, STAR stories: shorter. Transcript saved in sessions/.

If something failed, say so plainly with the error line.
