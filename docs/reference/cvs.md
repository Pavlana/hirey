# CVs: master CV, tailored versions, PDF and checks

[Overview](../how-it-works.md) · [Reference index](README.md)

The master CV is `private_dir/cv.md`. Every tailored CV starts from it and is a separate file;
the master is changed only through `cv save` or the My CV editor. A tailored CV is planned first, written
once from the agreed plan, then edited in the panel. Each tailored version gets a PDF and a rule check;
the AI check runs when asked for, and the panel shows one verdict. (This page is named `cvs.md` because any file named
`cv.md` is gitignored and blocked by the privacy check.)

## Contents

- [Master CV](#master-cv)
- [Tailoring plan](#tailoring-plan)
- [Tailored CV](#tailored-cv)
- [Editor](#editor)
- [Comments for the agent](#comments-for-the-agent)
- [PDF](#pdf)
- [Rule check](#rule-check)
- [AI check](#ai-check)
- [Verdict](#verdict)
- [Commands](#commands)

## Master CV

- **My CV** in the Applications header opens it in the side panel (`/?cv=1`; Esc and ✕ close it):
  skills as chips grouped by theme, the CV itself and its **History**.
- Skills groups come from the Skills section: one group per `- **Theme:** a, b; c` line, or per
  `- **Theme**` bullet with its items as indented `  - item` sub-bullets (`prep/cvfile.py`).
- History: the current version, older versions folded under **Older versions**, each with its date,
  a one-line note, **Open**, **Compare** (a line diff against the current CV) and **Restore**.
- The CV section has an **Edit** box ([Editor](#editor)); a save goes through `cvfile.write_cv` like
  `cv save`. With the panel open the Applications Assistant box reads "About my CV"; the agent
  ([assistants.md](assistants.md)) edits a draft and saves it with `cv save --note "…"`.
- `cv save` first stores the current text as a version, then replaces `cv.md` atomically (temp file
  and `os.replace`). Empty or unchanged text is refused ("No changes, nothing saved").
- Restore keeps the text it replaces as a version, so the toast's **Undo** brings it back.
- A hand edit of `cv.md` shows as "changed since the last version" and is stored as a version
  before the next change.
- Versions live in `private_dir/cv-history/cv-<YYYYMMDD-HHMMSS>.md`; their notes and hashes in
  `cv_versions` ([database.md](database.md)). Nothing is deleted.

## Tailoring plan

A short, plain plan agreed before any CV is written: it keeps only what preparing to apply needs
(`prep/tailorplan.py`).

- **Tailor CV** in an application's panel queues a `plan` request
  ([research.md](research.md#requests-and-the-queue)); the Research worker, or the Applications
  Assistant when none is set, writes it with the `/research` skill. Every tailored CV starts with a plan
  (**Just write it** was removed on 27 Nov 2026; "New from cv.md" on the Tailored CV card queues a new plan).
- The file is `private_dir/cv-tailored/plans/PLAN_<Company>_<app id>.md`, with two sections
  (`tailorplan.SECTIONS`):

| Section | What it holds |
| --- | --- |
| `## What the role needs` | the 3-5 must-haves in the posting's words, each ✓ have, ~ partly or ✗ not yet, with a few plain words |
| `## What will change in the CV` | the summary in outline, the order of sections and bullets, what is cut or shortened |

- No evidence references or cv.md quotes in the plan: the agent still traces every claim (the rule and AI
  checks guarantee it). No questions by default; one the CV truly needs goes to the Applications Assistant
  box.
- `plan save` refuses a plan without the two sections or with pay; it records the plan as a draft
  (`tailor_plans`), closes the request and posts a note in the Applications Assistant box. A plan written
  before, with the four sections What the role needs, Evidence, Order and Questions
  (`tailorplan.OLD_SECTIONS`), still shows and saves.
- The panel's **Tailoring plan** card shows the plan, **Edit** (stale-save protected, as the Notes card),
  **Write the CV** (also "Save and write the CV" inside the editor) and **Redo plan**; for an older plan
  with questions, each with its answer or "not yet" and how many are answered.
- An answer given in the Applications Assistant box to an older plan's question is written under it by
  `/ask` (`plan answer APP N`). Answers stay in the plan: nothing is written to the facts bank or
  `stories.md` unless the user asks for it in words.
- **Write the CV** (or `plan write APP`) marks the plan agreed and queues `tailor`; the tailoring packet
  then names the agreed plan and the CV is written from it.
- Wording the user approves for reuse goes to `cv-tailored/_style.md` with `style add [--app APP]`,
  dated and tagged with its application.

## Tailored CV

- The `tailor` request comes from Write the CV (with an agreed plan; requests from before may have none); the
  Research worker, or the Applications Assistant, writes it with the `/research` skill.
- Each version is its own file:
  `private_dir/cv-tailored/CV_<Company>_<RoleShort>_<DDMMYY>_<n>.md`. `cv.md` is never changed.
- `tailor path APP [--role-short X] [--copy]` prints the next file name; `--copy` starts it from the
  latest version (for a revision). `role_short` is set once per application.
- `tailor save APP [--to MSG]` records the version (refused when the text equals the latest
  version), posts the note and any questions in the Applications Assistant box, makes the PDF, runs
  the rule check. It does not queue the AI check.
- Tailoring may rephrase, reorder and cut, never add experience. Missing facts become questions.
- The user edits the latest version in the panel ([Editor](#editor)). Changes asked for in the
  Applications Assistant box are made in place too (`tailor draft`, then `tailor edit APP --to MSG`);
  a whole new version (`tailor path --copy`, `tailor save`) only when asked for.
- `cv sent APP FILE_NAME` records which version was sent; the card shows "CV v2 · sent".
- The panel's Tailored CV card shows the latest version and the sent one, with **Open**, **PDF**,
  **Undo edit** (when there is one), **AI check** and **New from cv.md**; older versions fold under
  **Older versions**; then the check, the comments count with **Revise commented lines** and **Edit**.

## Editor

The same editor serves the latest tailored version (Tailored CV card) and the master CV (My CV panel),
in `prep/cvedit.py`: the Notes card's Edit form with its stale-save stamp, a monospace text box, Save
(also Cmd/Ctrl-S) and the optional vim keys of the Learn notes editor. No autosave: each save remakes
the PDF. After a save the editor closes and the CV's card is refreshed and flashed, so the saved
version, its PDF and its buttons are in view; the PDF link carries the PDF's time (`t`), so an edit
saved in place always opens the new PDF.

- **Stamp**: the sha256 of the file the editor opened on. A save over a file that changed meanwhile is
  refused (400); the editor comes back with the user's text and the current text above it.
- **Tailored CV**: the body is saved over the latest version in place, its front matter kept. Before
  each save the text it replaces is copied to `cv-tailored/edits/<version stem>_e<n>.md` and recorded
  in `tailored_cv_edits`. **Undo edit** (or `tailor undo`) brings it back; the text Undo replaces is
  kept beside it (`.undone.md`). Nothing is deleted.
- Each save or undo remakes the PDF (a PDF of the old text is removed when WeasyPrint is not reachable),
  runs the rule check again, and marks an AI check of that version out of date: its findings are
  dropped and its scores kept with `stale`, the card says "AI check out of date" and offers **AI check
  again**.
- **Sent is frozen**: when the latest version is the one marked sent, a save writes the next version
  instead (`CV_…_<n+1>.md`), so the text the company saw is kept.
- **Agent edits**: `tailor draft APP` writes a working copy (`cv-tailored/drafts/`, with its stamp
  beside it); `tailor edit APP [--to MSG]` saves it the same way, author `agent`, refused when the
  version changed after the draft was made.
- **Master CV**: a save goes through `cvfile.write_cv` (the replaced text kept as a version first, note
  "Edited in the panel" unless one is typed); the toast's **Undo** restores that version.
- CV text and comments are never logged.

## Comments for the agent

- In either editor, a line starting with `%%` is a comment on the nearest line above it
  ("%% make this sound less junior").
- Comments are split off before anything is written and kept in `cv_comments` (one set per tailored CV,
  one for the master CV), so they never reach the file, the PDF or the checks. `cvpdf` and the rule
  check also skip any `%%` line found in a file.
- The editor, `tailor draft` and `cv draft` show them again under their line (a comment whose line is
  gone goes to the top). Each save replaces the set: deleting a comment line handles it.
- **Revise commented lines (N)** posts one message to the Applications Assistant (`/ask`) asking it to
  change only those lines; the message names how many comments, never their text.

## PDF

- Every saved version gets a PDF beside it (`.pdf`, same stem), from one committed template
  (`prep/cv_template.html`, rendered by `prep/cvpdf.py` with WeasyPrint).
- Layout: A4, one column. The name with the contact block under it and the summary beside it; then
  skills (one line per group), experience and projects with each role's dates at the right margin,
  then education. Real selectable text in a system font, no tables or images.
- Contact details come from the lines under `# Name` (for example `Mobile: … · Email: … · LinkedIn: …`).
- A normal CV fits two pages; the page count is printed and a longer one gets a warning.
- Needs the venv from [usage.md](../usage.md#setup); the skills use `pdf_python`
  ([configuration.md](configuration.md)). Without WeasyPrint the version is still saved, without a PDF.
- Served at `/research/pdf?app=ID&v=N` with its own headers ([routes.md](routes.md)).
- Try the template: `.venv/bin/python -m prep.cvpdf cv.example.md data/example-cv.pdf`
  (`--html` prints the HTML).

## Rule check

`prep/cvcheck.py` runs after every `tailor save`, `tailor pdf` and `tailor check`. Each finding is
**block** (fix before sending) or **look** (read this line), with its line number in the `.md` file.

- Blocks: more pages than the limit, a missing email or contact detail, a placeholder left in the
  text (brackets, TODO, `??`), stated pay, another application's company name.
- Looks: a pay word, dates that differ from `cv.md` or the facts bank, the job's must-have keywords
  that are missing.
- Wording: stock phrases, repeated openers, bullets with no numbers.
- A list of what changed against `cv.md`.

Dates and claims may come from `cv.md` or the facts bank: true facts that `cv.md` leaves out, in
`private_dir/cv-facts.md` or `facts_file` (template: `cv-facts.example.md`). Findings never quote a
line that mentions pay, and none of this is logged.

## AI check

The Tailored CV card's **AI check** button queues a `cv_eval` request for the latest version (shown
while that version has no AI check and none is queued; `research queue APP --kind cv_eval` does the
same). It costs a model pass, so a save never queues it. The agent does it in a separate pass from the
one that wrote the CV and writes `cv-tailored/<version stem>.eval.md` (TOML front matter);
`tailor eval APP [--version N]` stores it (`prep/cveval.py`).

- **Grounding**: every changed or added claim traced to `cv.md`, the facts bank or the agreed
  wording (`_style.md`). An unsourced claim blocks.
- **Fit**: the CV fit rubric (1–5) on the tailored copy; below the master CV's fit for the same job
  asks for a look.
- **Must-haves**: each of the job's must-haves covered or not; a gap asks for a look.
- **Six-second skim**: does the top of page one show this role (1–5); 2 or less asks for a look.
- **Editorial**: true but sloppy wording (a general term next to its own examples, a fact said
  twice, off-style wording), each a look with a suggestion that keeps the job's keywords.
- **Grammar and spelling**: an editorial entry with `kind = "grammar"` or `"spelling"`, shown as
  "Grammar: …" or "Spelling: …"; the summary counts them.
- Grounding covers the user's own edits as well: every changed claim is traced, whoever wrote it.
- An edit after the AI check marks it out of date ([Editor](#editor)).

Findings are placed by their quoted words, so line numbers come from the file, not the model.

## Verdict

The rule and AI findings share one row in `tailored_cv_checks` and one verdict (`db.check_verdict`):

| Verdict | When |
| --- | --- |
| Blocked | any blocking finding |
| Look at these lines | only look findings |
| Ready | no findings |

Until the AI check is asked for, the verdict is the rule check's alone. Once asked, the card shows
"AI check pending" until it is in, then its scores.

## Commands

| Command | What it does |
| --- | --- |
| `cv show`, `cv skills`, `cv history` | read the master CV, its skills, its versions |
| `cv draft` | copy `cv.md` to the draft the agent edits (prints its path) |
| `cv save --note "…" [--to MSG]` | save a new master version (backup first, atomic) |
| `cv restore V`, `cv diff V` | restore or compare a master version |
| `tailor path APP [--role-short X] [--copy]` | the next tailored file |
| `tailor save APP [--to MSG]` | record a version (note on stdin), PDF, rule check |
| `tailor pdf APP [--version N]`, `tailor check APP [--version N]` | remake the PDF; check again |
| `tailor eval APP [--version N]` | store the AI check |
| `tailor draft APP`, `tailor edit APP [--to MSG]`, `tailor undo APP` | revise the latest version in place; undo |
| `plan path`, `plan save`, `plan show`, `plan answer APP N`, `plan write` | the tailoring plan |
| `style add [--app APP]` | add approved wording to `_style.md` |
| `cv sent APP FILE_NAME` | record which version was sent |
