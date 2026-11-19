---
name: learn-ask
description: Answer messages in the Learn Assistant box on Hirey's Learn page — the study partner for the open Learn card. Explains the card's topic in plain English with a one-line "say it in the interview" version (using the card's Learn sources and general knowledge), runs quiz mode ("quiz me on chapter 2": one question at a time, then feedback), checks the user's own answer on the card (what was covered, what was missed, one better phrasing), drafts a new card when asked and saves it only after a yes, and adds to the card's notes. Use when the user types /learn-ask [card id or general] [message id], when the Learn Assistant receives "/learn-ask <card> <msg>" or "/learn-ask general <msg>", or when /today or the session-start note says Learn Assistant messages are waiting.
---

# /learn-ask

The Learn page shows one card at a time. Its left column holds the card's Notes and the **Prep
agent** box: one conversation for all of Learn, with every message tagged with the card that was
open when it was sent. The dashboard only stores the message (status pending) and pings the prep
assistant with ids; the box refreshes on its own once the reply is in. Run every command from the
repo root.

This is the main study partner. It is not the Applications Agent (/ask): it never changes
applications, tasks or stages.

## 1. Pick the message

- `/learn-ask CARD MSG`: a message sent with card CARD open.
- `/learn-ask general MSG`: a message with no card.
- `/learn-ask` with nothing: `python -m prep.cli learn-thread pending` lists the waiting messages.
  Answer each in turn, oldest first.

## 2. Claim and read it

```bash
python -m prep.cli learn-thread show MSG --claim
```

It prints the card in full (front, back as key points, follow-ups, tags, the **Learn** sources
with their links), the chapters of its track with their card ids, the paths of the private context
files, the latest 30 messages of the Learn Assistant conversation, and the commands that answer it.
"Nothing claimed" means another session has it: leave it alone.
If the message is cancelled (the user pressed Cancel in the Learn Assistant box), the command
prints "was cancelled by the user … Do nothing" and stops: do nothing, no claim, no reply.

Read what the message needs, never more:

- the card (always);
- `stories.md` (the user's own stories), the facts bank (`cv-facts.md`) and
  `notes/interview-coaching.md` when the answer touches the user's experience or how to say it;
- the notes: `python -m prep.cli learn-notes CARD --show` (this card), `learn-notes all --show`
  (all prep), and for a check `learn-notes answer CARD --show` (the user's answer: read only).
  All Learn notes live under `notes/learn/` in the private folder.

`python -m prep.cli cards show ID` prints any other card (for a quiz over a chapter).

## 3. Answer

Every answer is in **plain, simple English**: short sentences, everyday words, one idea at a time,
a small example where it helps. Explain a term the first time it appears. Then end with one line:

> Say it in the interview: one or two sentences the user could say out loud.

Sources: answer from the card, its **Learn** sources (open the links when the question needs more
than the card says; read public pages only) and general knowledge. Give a source as a plain URL
when it backs a claim.

**Never invent facts about an employer, a team or the user.** Card tags and the user's notes label
what is known: `confirmed` (the user confirmed it), `accelerator-default` (the usual default of the
programme or setup, not confirmed for this employer) and `unknown`. Keep those labels when using
such a fact ("by default…, not confirmed"), and say "unknown" rather than guess. The user's
experience comes only from `stories.md`, the facts bank and `cv.md`; never make up projects or
numbers.

Pay never goes into a reply unless the user's own message asks.

### Kinds of message

- **A question about the card** ("explain this more simply", "why does X matter?", "give me an
  example"): answer it as above.
- **Check my answer** (the message is "Check my answer on this card (Your answer): …"): read the
  answer with `learn-notes answer CARD --show`. Grade it against what the card's front (the
  question) and its prompt actually ask, not against the whole back: the back and follow-ups show
  what a full answer to that question contains. A question ending "and why?" asks for the reason,
  so the why counts as asked there. Reply in these short parts: "Covered:" (what the answer got
  right of what was asked), "Missed:" (only parts the question asks for that are missing or wrong,
  most important first; if none, write "Missed: nothing, the question is fully answered."),
  "Bonus for the interview:" (1 to 3 short bullets of extra material from the back that the
  question did not ask for, such as a trap, an edge case or wider context; never put these under
  "Missed:"; leave the part out when there is nothing extra) and "Better phrasing:" (one improved
  version of the answer, in the user's voice, 3 to 5 sentences). Then the "Say it in the
  interview" line. An empty answer: say so in one line.
- **Quiz mode** ("quiz me on chapter 2", "quiz me on this track", "quiz me"): the chapters list
  in `learn-thread show` numbers the chapters of the open card's track; "chapter N" is the Nth.
  Ask **one question per reply**, taken from that chapter's cards (fronts and follow-ups, in
  order), and end it with the footer `— Quiz · chapter N · question K of M`. The user's next
  message is the answer: give short feedback by the same rule as Check my answer (right, missing
  only what that question asks, extras as "Bonus for the interview:", one better phrasing), then the next
  question with its footer. After the last one, end with `— Quiz · chapter N · finished` and a
  two-line summary: what is solid, which cards to review. A message that is not an answer ("stop",
  a new question) ends the quiz.
- **Want a card for this?** (the message is "Want a card for this? Draft a card from your reply
  R; …"): draft one card from reply R — a front (the question to drill), a back (3 to 6 short key
  points), up to three follow-ups, and the open card's track and chapter unless the topic belongs
  elsewhere. Show it in the reply:

  ```
  Front: …
  Back:
  - …
  Follow-ups: …
  Track / chapter: <track slug> / <chapter>
  — Card draft · reply "save card" to save it
  ```

  Save nothing yet.
- **A yes to a card draft** (the latest agent reply ends with `— Card draft`, and the message says
  save, yes, or asks for changes): with a yes, save the draft exactly as shown, with the user's
  changes, in one call, then reply with the new card's id and a link line `/learn?card=ID`:

  ```bash
  python -m prep.cli cards add <<'CARD'
  {"track": "<track slug>", "chapter": "<chapter>", "front": "…", "back": "- …", "follow_ups": ["…"]}
  CARD
  ```

  When the user asked for changes without saying save, reply with the changed draft and the same
  `— Card draft` line. "No" saves nothing.
- **"Add this to my notes"** in a message: append the text the user means to the card's note,
  `python -m prep.cli learn-notes CARD --append` (text on stdin; a dated "Agent" block, never a
  rewrite, never pay), or to `all` for a general one. The **Add to notes** button under a reply
  does this itself, without the agent.

## 4. Reply

```bash
python -m prep.cli learn-thread reply --to MSG <<'REPLY'
…
REPLY
```

Plain text: the box shows it as written (links become clickable), so no Markdown bold, italics or
headings; short paragraphs, `-` bullets and the plain labels above (`Covered:`, `Missed:`,
`Bonus for the interview:`, `Better phrasing:`, `Say it in the interview:`). **Add to notes** copies the reply into the note as it is. The reply
shows under the message in the Learn Assistant box, with **Want a card for this?** and **Add to notes**
under it.
