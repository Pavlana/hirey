# Learn import format

`python -m prep.cli cards import FILE [--dry-run]` reads tracks, chapters and cards from one
Markdown file and adds or updates them in prep.db, all or none. A fictional example is
[`learn.example.md`](../learn.example.md). Real import files hold private answers: they live in
`private_dir/learn/` (gitignored) and only their cards reach prep.db.

## Shape

```markdown
Anything before the first "# " heading is ignored: a note on where the cards came from.

# Track title
slug: track-slug          (optional; default made from the title)
subtitle: one line        (optional)
cover: teal               (optional; matcha, teal, amber, rose, violet or slate)

## Chapter title          (optional; cards above the first chapter have none)

### The question on the front of the card
tags: confirmed, accelerator-default
gap: 12                   (optional; the gap this card drills, one card per gap)

Optional prompt: what the question refers to (a code snippet, a failing test),
shown under the question and always visible.
---
The back: short key points in Markdown, as many lines as needed.
Code blocks are fine; lines inside ``` fences are always back text.

follow-ups:
- A likely follow-up question
sources:
- watch | Video title | https://example.com/video | 7 | why it helps
- read | Article title | https://example.com/article | 12
- notes | Two-minute script (private notes file)
```

- `#` starts a track, `##` a chapter in it, `###` a card. The card's back is every other line up
  to the next heading or `follow-ups:` / `sources:` line. Do not use `#`, `##` or `###` headings
  inside a back.
- A line holding only `---` splits the card: everything above the first one is the **prompt**,
  shown under the question before Show answer; the rest is the back. Use it when the question
  refers to code ("what does this print?"). A `---` inside a ``` fence, or a later `---` in the back,
  does not split. Without a `---` the whole text is the back, as before; a card still needs a back.
- The card text is Markdown (no headings): paragraphs, `- ` lists, **bold**, *italic*, `code` and
  ``` fenced code blocks.
- `tags:` takes a comma-separated list. `follow-ups:` and `sources:` stand alone on their line,
  followed by `- ` items, one per line.
- A source is `kind | title | url | minutes | why`. `kind` is `watch`, `read` or `notes`; trailing
  fields may be left out and empty ones are skipped. Minutes are a whole number (`7` or `7 min`).
- One file can hold several tracks.

## Updating

A card is matched by its track and its front (the question, exactly). A match gets the file's
prompt, back, chapter, tags, follow-ups, sources and gap, and keeps its id, position and done or to-do
status. Changing a front makes a new card; the old one stays. Cards that are no longer in the
file are left alone and counted as "not in the file"; remove one with `cards delete ID`. A track is matched by its slug; its title,
and the subtitle and cover when given, are updated.

Importing the same file twice changes nothing ("N unchanged"). `--dry-run` prints the same
counts and saves nothing.
