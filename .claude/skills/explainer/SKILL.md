---
name: explainer
description: Write or refresh the interactive explainer page of a Learn card in Hirey — a private HTML page shown under the card that explains the card's idea with a flow or architecture diagram, a grid of what is built, partial or a gap, getjo code copied verbatim with numbered notes, a small worked example or "Try it", and the sentence to say. Records which getjo code it quotes so Hirey can flag the page when that code changes. Use when the Learn Assistant receives "Make an explainer for this card (/explainer)." or "Refresh the explainer for this card (/explainer): …", or when the user types /explainer CARD.
---

# /explainer

An explainer is one private HTML page per Learn card, shown under the card in a sandboxed frame. It
makes the card's idea concrete with getjo, the user's own job-radar project: the real code, what
exists and what does not. Hirey checks the quoted code later and flags the page when getjo changes.
Run every command from the repo root.

## 1. Where it goes

```bash
python -m prep.cli explainers path CARD
python -m prep.cli cards show CARD
```

The first prints the page path (`private_dir/learn/explainers/<card id>.html`), the sidecar path and
getjo's checkout and HEAD. Write **only** the page file there; the sidecar is written by step 4.
Never write anywhere in getjo, and never into the repo.

For a refresh, `python -m prep.cli explainers check --card CARD` names the snippets that changed or
disappeared; read the current page first and keep what is still true.

## 2. Read getjo (read only)

Read the getjo files the card is about in the checkout that `explainers path` prints. Only read:
no edits, no `git fetch`, `pull`, `checkout` or anything else that writes. `git log` and `git show`
are fine.

Every name on the page (file, function, class, constant, column) must exist in getjo now: check
each one with a search before using it. Something the card talks about that getjo does not have is
a **gap**: say so, never describe it as built.

## 3. Write the page

A plain HTML file. Structure, in this order, each part a `<section class="panel">` with an `<h2>`
(leave out a part that does not fit the card):

1. **Shape**: the idea as a flow or architecture diagram: `.flow` with `.step` boxes (a `.num`
   circle, a `<b>` title, a `<span>` line) joined by `<span class="arrow">→</span>`, or inline SVG
   for anything else.
2. **Grid**: `.cols` of columns (a `.colh` heading with a `<small>` line), each with `.box` items: a
   `<b>` title, a `<p>` line, and a pill: `<span class="pill built">built</span>`, `partial` or `gap`.
   Gaps use `.box muted` (dashed). The pill says what getjo does today, not what is planned.
3. **In the code**: one or more `<pre>` blocks, each starting with
   `<span class="src"># path/in/getjo.py · qualname · short commit</span>`, the code **copied
   verbatim** (whitespace too; escape `<`, `>`, `&`), lines worth explaining wrapped in
   `<span class="hl">…</span>` with a `<span class="num">N</span>` at the end, then
   `<ol class="notes">` with one `<li><span class="num">N</span>…</li>` per number. Optional token
   colours: `.k` keyword, `.s` string, `.n` number, `.c` comment. Quote whole functions or a short
   excerpt of one; never edit, shorten inside a line or "tidy" the code.
4. **Try it** (optional): a worked example in a small table, or one control (`<input type="range">`,
   a few buttons) in `.try` that changes a `.verdict` line, with a few lines of plain JavaScript in a
   `<script>` at the end of the section. No libraries.
5. **Say it**: `<p class="say">` with one or two sentences the user could say in an interview.

The head loads exactly these two files and nothing else:

```html
<!doctype html><html lang="en"><head><meta charset="utf-8"><title>…</title>
<link rel="stylesheet" href="/static/explainer.css"><script src="/static/explainer.js" defer></script></head>
```

Rules:

- Inline HTML and SVG only. No CDNs, external fonts, images from the web, iframes, forms or
  `fetch`: the frame's policy blocks them anyway, and the page must work offline.
- Tiny vanilla JavaScript only, for "Try it". `explainer.js` already reports the page height and
  the user's text selection to the Learn page; do not reimplement that.
- Plain, simple English; short labels. Keep the page to what the card asks.
- Honesty: getjo is a personal project. Describe the work as the user's own project, built by
  directing Claude Code agents; never claim a team, users, production traffic or numbers that are
  not in getjo or the user's private files. Name a gap as a gap.
- Never pay, salary or rates, and nothing from the user's private notes beyond what the card needs.

## 4. Record it

After the page is written, record getjo's commit and the code each `<pre>` quotes:

```bash
python -m prep.cli explainers save CARD --snippet path/in/getjo.py:qualname [--snippet …]
```

One `--snippet` per quoted function or class (dotted qualname, e.g. `Sweep.run`); a file quoted
whole is `--snippet path/to/file` with no qualname. Hirey computes the hashes itself and refuses a
file or function that is not in getjo: fix the page if it does. Then `explainers check --card CARD`
must print "up to date".

## 5. Reply

In the Learn Assistant box (`/learn-ask` step 4), two or three plain lines: what the page shows, the
getjo files it quotes and, for a refresh, what changed. The page appears under the card on reload.
