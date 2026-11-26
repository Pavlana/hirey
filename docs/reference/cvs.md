# CVs: master CV, tailored versions, PDF and checks

[Overview](../how-it-works.md) · [Reference index](README.md)

The master CV is `private_dir/cv.md`. Every tailored CV starts from it and is a separate file;
the master is changed only through `cv save`. Each tailored version gets a PDF, a rule check and an
AI check, and the panel shows one verdict. (This page is named `cvs.md` because any file named
`cv.md` is gitignored and blocked by the privacy check.)

## Contents

- [Master CV](#master-cv)
- [Tailored CV](#tailored-cv)
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
- There is no edit box. With the panel open the Applications Assistant box reads "About my CV"; the
  agent ([assistants.md](assistants.md)) edits a draft and saves it with `cv save --note "…"`.
- `cv save` first stores the current text as a version, then replaces `cv.md` atomically (temp file
  and `os.replace`). Empty or unchanged text is refused ("No changes, nothing saved").
- Restore keeps the text it replaces as a version, so the toast's **Undo** brings it back.
- A hand edit of `cv.md` shows as "changed since the last version" and is stored as a version
  before the next change.
- Versions live in `private_dir/cv-history/cv-<YYYYMMDD-HHMMSS>.md`; their notes and hashes in
  `cv_versions` ([database.md](database.md)). Nothing is deleted.

## Tailored CV

- **Tailor CV** in an application's panel queues a `tailor` request
  ([research.md](research.md#requests-and-the-queue)); the Research worker, or the Applications
  Assistant when none is set, writes it with the `/research` skill.
- Each version is its own file:
  `private_dir/cv-tailored/CV_<Company>_<RoleShort>_<DDMMYY>_<n>.md`. `cv.md` is never changed.
- `tailor path APP [--role-short X] [--copy]` prints the next file name; `--copy` starts it from the
  latest version (for a revision). `role_short` is set once per application.
- `tailor save APP [--to MSG]` records the version (refused when the text equals the latest
  version), posts the note and any questions in the Applications Assistant box, makes the PDF, runs
  the rule check and queues the AI check.
- Tailoring may rephrase, reorder and cut, never add experience. Missing facts become questions.
- Changes are asked for in the Applications Assistant box and saved as the next version.
- `cv sent APP FILE_NAME` records which version was sent; the card shows "CV v2 · sent".
- The panel's Tailored CV card shows the latest version and the sent one, with **Open**, **PDF** and
  **Redo**; older versions fold under **Older versions**.

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

Every `tailor save` also queues a `cv_eval` request. The agent does it in a separate pass from the
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

Findings are placed by their quoted words, so line numbers come from the file, not the model.

## Verdict

The rule and AI findings share one row in `tailored_cv_checks` and one verdict (`db.check_verdict`):

| Verdict | When |
| --- | --- |
| Blocked | any blocking finding |
| Look at these lines | only look findings |
| Ready | no findings |

The card shows "AI check pending" until the AI check is in, then its scores.

## Commands

| Command | What it does |
| --- | --- |
| `cv show`, `cv skills`, `cv history` | read the master CV, its skills, its versions |
| `cv draft` | copy `cv.md` to the draft the agent edits (prints its path) |
| `cv save --note "…" [--to MSG]` | save a new master version (backup first, atomic) |
| `cv restore V`, `cv diff V` | restore or compare a master version |
| `tailor path APP [--role-short X] [--copy]` | the next tailored file |
| `tailor save APP [--to MSG]` | record a version (note on stdin), PDF, rule check, queue the AI check |
| `tailor pdf APP [--version N]`, `tailor check APP [--version N]` | remake the PDF; check again |
| `tailor eval APP [--version N]` | store the AI check |
| `cv sent APP FILE_NAME` | record which version was sent |
