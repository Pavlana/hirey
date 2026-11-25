# Usage and reference

Setup, commands, the dashboard in detail, the Applications Assistant, launchd and the privacy
model. The overview is in the [README](../README.md); components and data flow are in
[how-it-works.md](how-it-works.md).

## Setup

```bash
git clone <this repo> inter_prep && cd inter_prep
mkdir -p ~/.config/inter_prep
cp config/settings.example.toml ~/.config/inter_prep/settings.toml
# edit it: absolute paths for getjo_db, prep_db, private_dir
python -m unittest
```

The tailored CV PDF (and the launchd job below) use a virtual environment in the main checkout.
WeasyPrint needs Pango from Homebrew on macOS:

```bash
brew install pango
/opt/homebrew/opt/python@3.14/bin/python3.14 -m venv .venv    # a versioned Python, not the python3 alias
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest                                    # now also runs the PDF render tests
```

A venv belongs to one Python minor version: after moving to a new one (3.14 to 3.15), delete
`.venv` and create it again with the new interpreter. Patch updates need nothing. If WeasyPrint
reports that it cannot load a library (`gobject-2.0`, `pango-1.0`), run it with
`DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib`.

The skills run the system `python3`, and a dev3 worktree has no venv, so point the settings at
this one for the PDF step:

```toml
pdf_python = "/absolute/path/to/inter_prep/.venv/bin/python"
```

The settings file is looked up in this order:

1. `$INTERPREP_SETTINGS`, if set
2. `config/settings.toml` in the checkout (gitignored), as a per-checkout override
3. `~/.config/inter_prep/settings.toml`

The per-user file is the normal place: every checkout and git worktree, a launchd job and
the Claude app all find it without environment variables. Single keys can be overridden with
`PREP_GETJO_DB`, `PREP_DB`, `PREP_PRIVATE_DIR`, `PREP_DASHBOARD_PORT`,
`PREP_WEEKLY_TARGET`, `PREP_NO_RESPONSE_DAYS`, `PREP_RESEARCH_MAX_AGE_DAYS`, `PREP_STALE_DAYS`,
`PREP_BRIEF_AUTO_MIN_SCORE`, `PREP_ASSISTANT_TASK`, `PREP_PREP_ASSISTANT_TASK`, `PREP_ASSISTANT_PROJECT`, `PREP_DEV3_BIN`,
`PREP_PDF_PYTHON` and `PREP_FACTS_FILE`. Use absolute paths, with `prep_db`
outside any worktree: relative paths resolve differently in each worktree. An empty
`PREP_ASSISTANT_TASK=` (or `PREP_PREP_ASSISTANT_TASK=`, `PREP_ASSISTANT_PROJECT=`, `PREP_DEV3_BIN=`) clears that key, so a run
on example data never messages the Applications and Learn Assistants; empty path and number overrides are
ignored.

## Try it with example data

```bash
python -m prep.seed_example
PREP_ASSISTANT_TASK= PREP_PREP_ASSISTANT_TASK= PREP_DB=data/example.db PREP_GETJO_DB=$PWD/data/example-getjo.db python -m prep.cli today
PREP_ASSISTANT_TASK= PREP_PREP_ASSISTANT_TASK= PREP_DB=data/example.db PREP_GETJO_DB=$PWD/data/example-getjo.db python -m prep.web --port 8766
# then open http://localhost:8766
```

`PREP_ASSISTANT_TASK=` and `PREP_PREP_ASSISTANT_TASK=` (empty) keep the example run away from the standing
assistants set in the settings file: its messages and queued research would otherwise be sent to them.

The seed also writes made-up Claude Code transcripts to `data/example-claude-projects/` (token
counts only, with the dev3 tasks they belong to stored in `data/example.db`). To see them on the
Metrics page's Usage tab, name the example assistants and point at a dev3 that does not exist, so
nothing can be sent anywhere:

```bash
PREP_ASSISTANT_TASK=7 PREP_PREP_ASSISTANT_TASK=9 PREP_ASSISTANT_PROJECT=e0e0e0e0-1111-4222-8333-944445555666 \
PREP_DEV3_BIN=/nonexistent/dev3 PREP_CLAUDE_PROJECTS_DIR=$PWD/data/example-claude-projects \
PREP_DB=data/example.db PREP_GETJO_DB=$PWD/data/example-getjo.db python -m prep.web --port 8766
# then open http://localhost:8766/metrics
```

The seed shifts the example dates so the most recent ones fall around today. The example databases are built from made-up fixtures in `tests/fixtures/example.json`
(fictional companies only) and are gitignored.

## Commands

| Skill      | CLI                                                                 | What it does |
|------------|---------------------------------------------------------------------|--------------|
| `/today`   | `python -m prep.cli today [--threshold 7] [--limit 15]`              | Applications moved to No response, Learn cards done and to do, tasks due or overdue, new getjo matches |
|            | `python -m prep.cli queue`                                           | Read-only: counts and ids of queued agent work and waiting thread messages (used by the SessionStart hook) |
| `/applied` | `python -m prep.cli applied --job-id ID \| --url URL \| --company C --role R [--status to_apply] [--date D]` | Record an application (applied today, or Interested with `--status to_apply`), linked to getjo when it matches; adds its first tasks |
|            | `python -m prep.cli apps [--status S]`                               | List applications |
| `/stage`   | `python -m prep.cli update ID [--status S [--comment T] [--interview-date D]] [--next-action T [--next-date D]]` | Move an application to a stage (closes the old stage's tasks, adds the new ones); `--next-action` adds a task. Already at that stage: the comment and an interview date are still saved. `/stage` takes a pasted recruiter email, call notes or a one-line message and runs this (plus `comment` / `task add`) itself |
|            | `python -m prep.cli comment ID "text"`                               | Comment on an application |
| `/task`    | `python -m prep.cli task add "Title" [--kind K] [--due D]` / `task list` / `task done ID` | Tasks |
| `/research` | `python -m prep.cli research next [ID]` / `research queue ID [--kind K] [--redo]` / `research save ID` / `fit save ID` | Queued company research and CV fit checks: the agent writes `companies/<slug>.md` and `fit/<id>-<slug>.md` in `private_dir` and saves a summary |
|            | `python -m prep.cli brief next` / `brief save (--job ID \| APP)` / `brief show (--job ID \| APP)` / `brief queue (--job ID \| APP) [--redo]` | Job briefs: claim the oldest waiting one, store it (text on stdin), print it, or ask for one |
| `/research`, `/ask` | `python -m prep.cli tailor path APP [--role-short X] [--copy]` / `tailor save APP [--to MSG]` | Tailored CV: print the next version's file, then save it as a new version (note on stdin); saving also makes the PDF, runs the rule checks and queues the AI check |
|            | `python -m prep.cli tailor pdf APP [--version N]`                    | The PDF of a tailored CV version (latest by default), beside its file |
|            | `python -m prep.cli tailor check APP [--version N]` / `tailor eval APP [--version N] [--file F]` | Check a version again without WeasyPrint; store the AI check (grounding, fit, skim, editorial) from its `.eval.md` file |
| `/ask`     | `python -m prep.cli thread show [APP \| --job ID \| --message MSG \| --cv] [--claim]` / `thread reply [APP] --to MSG` / `thread pending` | The Applications Assistant box: a thread with its context (no id: the whole search; `--job`: an offer not yet tracked; `--message`: a comparison), the reply (text on stdin or `--file`), the messages waiting |
|            | `python -m prep.cli review [--all]`                                  | Read-only: the top 3 open tasks in review order (`--all`: every open task) |
|            | `python -m prep.cli task edit ID [--title T] [--due D \| --no-due]` / `task delete ID` | Change or delete a task |
|            | `python -m prep.cli comments list APP` / `comments amend REF "text"` / `comments remove REF` | An application's comments with their refs (`c12`, `h34`); a removed stage comment keeps its move |
| `/ask`, `/research` | `python -m prep.cli notes APP [--show \| --append [TEXT]]` | The panel's Notes: print them, or add a dated "Agent · 17 Nov" block at the end (TEXT, or stdin when left out); never rewrites the user's text, refuses pay |
| Learn Assistant | `python -m prep.cli learn-notes CARD_ID\|all [--show \| --append [TEXT]]` | The Learn page's Notes: a card's note or the "all prep" notebook; print it, or add a dated "Agent" block at the end; never rewrites, refuses pay |
| Learn Assistant | `python -m prep.cli learn-notes answer CARD_ID [--show]` | The user's own answer on a card (Your answer); read only |
| `/learn-ask` | `python -m prep.cli learn-thread show MSG [--claim]` / `learn-thread reply --to MSG` / `learn-thread pending` | The Learn page's Learn Assistant: a message with its card (front, back, follow-ups, Learn sources), the track's chapters, the private context files and the conversation; the reply (text on stdin or `--file`); the messages waiting |
|            | `python -m prep.cli skip APP --reason R [--comment T]`               | Skip an Interested application (listed under New offers → Show skipped) |
|            | `python -m prep.cli cv sent APP FILE_NAME`                           | Record which tailored version was sent |
|            | `python -m prep.cli cv show \| skills \| history \| draft` / `cv save --note T [--to MSG]` / `cv restore V` / `cv diff V` | The master CV: print it, its skills by theme, its saved versions; save the edited draft (the current text is saved as a version first), restore or compare a version |
| `/mock`    | `python -m prep.cli mock path APP --stage S [--date D]`              | The transcript file for a new mock, under `private_dir/sessions/` |
|            | `python -m prep.cli session add --kind mock\|real [--app ID] [--stage S] [--score AREA=N ...] [--file F] [--gap TEXT ...]` / `session list` | Record a practice session with 1–5 scores per area; each gap named gets a drill task |
|            | `python -m prep.cli gap add TEXT ... [--app ID]` / `gap list [--all \| --closed]` / `gap close ID` | Gaps: each gets a drill task; closing a gap completes its drill tasks |
| `/debrief` | `python -m prep.cli debrief --kind real\|mock [--app ID] [--stage S] [--questions T] [--went-well T] [--to-improve T] [--score AREA=N ...] [--gap TEXT ...] --transcript` | After a real interview or a mock done elsewhere: saves the transcript (stdin) under `private_dir/sessions/`, the interview's questions, went well and to improve (real only), a session with scores, and a gap with a drill task per weakness; completes the Debrief task |
|            | `python -m prep.cli tracks list` / `tracks add TITLE [--subtitle T] [--cover C] [--slug S]` | Learn tracks: chapters, done / total and percent done; add a track |
|            | `python -m prep.cli cards add [--file F]` / `cards list [--track T] [--todo \| --done]` / `cards show ID` / `cards done ID` / `cards undo ID` / `cards delete ID` | Learn cards: add from JSON (one card or a list, all or none), list, show (with `[x]` / `[ ]` before each Learn source), mark done or back to do, remove |
|            | `python -m prep.cli cards import FILE [--dry-run]`                   | Add or update tracks and cards from Markdown ([format](learn-format.md)); the same track and front is updated, its done status kept |
|            | `python -m prep.cli cards gaps [--gap ID ...] [--app ID] [--session ID] [--limit N]` | Read-only: card drafts (JSON) for open gaps without a card; /mock and /debrief fill and save them with `cards add` after a yes |
|            | `python -m prep.cli feedback export [--out PATH]`                    | Write interested/applied/skipped labels as JSONL to `private_dir/feedback/job_decisions.jsonl` for getjo |

Dates accept `YYYY-MM-DD`, `today`, `tomorrow` or `+N` (days from today).

## Dashboard

`python -m prep.web [--port 8765]` serves a local website over `prep.db`, built with the
standard library's `http.server`, plain HTML/CSS and one small optional script for sorting,
filtering and saving without a reload (no build step, no external requests; one dark theme;
charts are server-rendered SVG):

- **Applications** (`/`, the landing page; `/applications` and `/tasks` redirect here): the
  left column switches between **Pipeline N** and **New offers M** (`?view=pipeline|offers`,
  remembered per browser; a bare `/` opens the view picked last). The nav shows the new offers
  count next to Applications. The Applications Assistant box sits under the list in both views; the panel on the
  right shows the selected application or job offer. Details below.
- **Metrics** (`/metrics`, last in the nav; `/dashboard` and `/progress` redirect here), two tabs:
  - **Usage** (`?tab=usage`, the default): Claude Code token use in tokens and US dollars, to see
    which sessions cost the most and where to save. Filters: project (each project seen, or all)
    and 7 / 14 / 30 days. Dollars are an estimate at Claude API list prices, labelled as such: a
    Claude subscription is billed differently. First three dollar tiles, Today, Last 7 days and
    Last 30 days (each with its token total; these windows ignore the days filter), cost per day by
    role for the last 30 days (stacked columns), and **Where the money goes** (cache read, cache
    write, output, input in dollars). Tokens of a model with no known price read "price unknown"
    and count as $0, never at a guessed price. Then who spends what (cost, share of dollars,
    tokens per role) and top tasks by cost (seq, title, turns, cost, tokens, tokens per turn),
    both sorted by dollars and following the days filter; a cost with `*` is partly unpriced.
    Then tiles: tokens, turns and sessions; cache-read share; assistants over the context
    threshold. Then tokens per day (stacked: cache read, cache write, output, fresh input), who uses what (Applications
    Assistant, Learn Assistant, coordinator, build tasks, main checkouts), context size per turn
    for the two assistants and the coordinator (each day's last turn; "time to /compact" above
    `context_warn_tokens`), and **Where to save**: an assistant over the threshold, a task (not an assistant or the coordinator) still
    open after its PR merged, a session with many cache writes. A turn is one API response.
  - **Applications** (`?tab=applications`): applications sent, response rate (a reply, call,
    rejection or recorded interview; a warning below 10% after 25 applications), interviews and
    offers; applications sent per week (last 12 weeks, with the `weekly_target` line); the stage
    funnel Applied → Screen (recruiter call) → Interview (technical, system design or final) →
    Offer; and per getjo source (job board): jobs seen, average score, strong matches (7+) and
    applications sent from them.
- **Learn** (`/learn`; the nav shows the number of cards to do): "X done · Y to do" and the next
  interview (company, stage, day) under the title, then one tile per track: a cover colour with a
  progress ring, the title, chapters and cards, and "X done / Y". Under the tracks, the **Learn
  Assistant** box for general messages, not about one card (for example "start a mock recruiter
  call"). A track opens its first card to
  do (`?track=SLUG`; any card is `?card=ID`). The card view keeps the tracks above it as a compact
  row (cover, ring, "X done / Y"), the open card's track highlighted; below them **Notes** and **Learn Assistant** on the
  left, the card on the right, with the same divider as Applications (its
  width kept per browser on its own). **⤢ Expand** on the Learn Assistant folds Notes away, remembered
  per browser. The card: its track and chapter, "Card N of M" with ← → to the neighbours, tags, the
  question, **Show answer** (the key
  points and likely follow-ups), the **Learn** box under it, always in view (the sources to watch or
  read, the preparation material; links open in a new tab). A tick in front of each source marks it
  done (greyed and struck through; the header reads "Learn · 2 / 5 done"); clicking again unticks it.
  Ticks are saved at once, survive a `cards import` that reorders or edits other sources (a source is
  known by its URL, else its kind and title) and never mark the card itself done. Then **Done** /
  **Not done yet**. Done marks the card done and opens the next card to do in the track (after the
  last one, the tracks again); Not done yet puts it back to do and stays. There is no schedule, due
  date or rating. Phones stack the card above Notes and the Learn Assistant.
  **Notes** has two tabs: **this card** (a note per card) and **all prep** (one general notebook),
  saved as private Markdown files under `private_dir/notes/learn/`. The editor has line numbers and
  a monospace font, about 13–15 lines tall (it scrolls inside, so the Learn Assistant stays in view below); it autosaves 1.5 s after the last change, when it loses focus, and on Cmd/Ctrl-S,
  with "autosaved HH:MM" on its status line. A save over a note that changed meanwhile (an agent
  added to it) never overwrites it: an addition at the end is kept below the text being edited and
  saved again; any other change pauses autosave and shows the note as it is now, and the next Save
  replaces it. **vim keys** on the status line (off by default, remembered per browser) turns on
  normal and insert modes: h j k l, w b, 0 $, i a o O, x dd yy p, u, `:w` saves, Esc back to
  normal, with INSERT/NORMAL and line,col on the status line.
  On the card, **Your answer** sits between the question and Show answer: a plain box (six lines,
  growing as it is typed or dictated) for saying the answer before looking. It is saved the same way
  (`notes/learn/answer-<id>.md`, "saved HH:MM" under it); Show answer then opens the key points
  below it to compare.- **New offers** (`/?view=offers`; the old Job Offers page, `/offers` and `/matches` redirect
  here with their filters): open getjo jobs not yet tracked or skipped, newest day first, then
  highest score. Rows: score, company, role, location, with chips "may be closed" (getjo has not
  listed it for more than `stale_days` days) and "expires in N days" (a week or less). Closed and
  expired jobs are left out. Filters in the list header: minimum score (default 7), London /
  Remote / Other and a search on company or role; they live in the URL. **Show skipped** lists
  the skipped jobs at any score with their reason and **Undo skip**. Clicking a row opens the
  offer panel: company, role, score, location, seen date, the posting link, the chips,
  **Interested · Skip**, and the **job brief**: the agent's short summary of the description (a
  key facts line such as "hybrid 2 days · Python · Senior · London", then must-haves,
  nice-to-haves, where and terms, team, interview process, red flags; never pay), with the original
  behind Show original. Offers scored `brief_auto_min_score` (8) or more get their brief in the
  background; any other offer when its panel is first opened ("Writing brief…" until it is in),
  tracked jobs during company research; **Write brief / Redo brief** asks again. **Interested** records
  the application (status `to_apply`, which queues company research and a CV fit), moves it to
  Pipeline → Interested and opens its panel, with an Undo toast that deletes it again while
  nothing else has happened to it. **Skip** asks for a reason and an optional comment (a pop-up;
  without JavaScript a plain form page) and offers Undo. Ticking a row's ☐ works here
  too: a question about a job not yet tracked ("is this remote?", "how does my CV fit?") goes to
  the agent tagged with that job, and follows the job into its application if it is marked
  Interested. Decided jobs also drop out of `/today`.
- **Pipeline** (the default view of `/`): the pipeline list on the left with
  the Applications Assistant box under it (below), and the selected application on the right (half and half
  from 801px wide, a full-screen sheet on phones). A divider between the two columns moves the
  split: drag it, or focus it (Tab) and use ←/→ (Shift for bigger steps), Home/End for the
  limits, Enter or a double-click for half and half again. Each side keeps a minimum width (360px
  for the list, 420px for the panel); where both do not fit, the split stays at half and half. The
  width is remembered per browser (`localStorage`); without the script, or with storage blocked,
  it is half and half. On a laptop the page fits the screen: the list (at most
  45% of the left column) scrolls inside with its stage filters and headings kept, and the Agent
  box's input is always in view. The list is grouped by stage (Interested → Applied →
  Recruiter call → Coding interview → System design → Company interview → Offer) with the
  columns Company, Role, Stage, Next task and the latest comment (one line, with the count when
  there are more). A stage strip with counts filters it; closed applications (Rejected, No
  response, Withdrawn, Skipped) show with **Show closed**. **All · Product · Client** beside it
  keeps one employer kind (in-house product work or client delivery); a small Product or Client
  label sits under the company, and within each stage product roles come first, then unknown,
  then client. Clicking a row opens the panel:
  company, role, stage with interview steps, days in this stage and the job posting, and an
  **Employer** select (Product / Client / Unknown) that saves on change; the
  latest comment (and how many came before it); the shortcut buttons **Research company · CV fit · Tailor CV · Mark
  applied · Skip** (each shows its state: "Researching…", "CV fit 4/5", "CV v2 · sent"; Mark
  applied and Skip only while Interested); then the tasks (tick ☐ to mark
  one done, with Undo), Results: the **Notes** card (below), then the agent's results (company
  research with the tier and why, CV fit, the tailored CV with Open and Redo: the latest version and
  the sent one, older versions folded under **Older versions**), and the
  stage history. Mark applied records
  Applied today at once and offers Undo, which moves it back to Interested. Skip asks for a
  reason (the same reasons as an offer's Skip) and an optional comment, closes the application as
  Skipped and lists the job under New offers → Show skipped. An Interested or Applied application
  whose getjo job has since closed or expired keeps its place, with a "closed" badge on the row
  and in the panel. Everything else (tasks, comments, stage,
  interview dates, tier, which CV was sent) is asked of the agent in the Applications Assistant box. Esc or ✕
  closes the panel. Every task is added, changed or removed through the agent; tasks with no
  application are listed by `/task` and `/today`.
- **Notes** (in every application's panel, first under Results): one place per job for what is
  worth keeping: questions to ask, notes from a recruiter call or an interview, what the agent
  finds. Shown as Markdown (`**bold**`, `-` lists, `#` headings; line breaks kept; everything
  escaped). **Edit** opens a text box with **Save** and **Cancel**; saving an empty text clears the
  notes. The agent only adds dated blocks at the end ("**Agent · 17 Nov**"): `/ask` after a request to
  note something, a pasted recruiter email or call notes, a mock or a debrief; `/research` adds
  "Questions to ask" after company research when there is none yet. When the agent added something
  while the editor was open, Save is refused with a message: the current notes show above and the
  typed text stays in the box; Save again replaces them. Agents never write pay into notes.

The dashboard binds to `127.0.0.1` only and refuses any other address; requests with a Host
header other than `localhost`/`127.0.0.1` and cross-origin form posts are rejected. The pay
field (`applications.pay`) is never shown. Each request opens the database afresh, so changes
made through the skills appear on the next page load. Optional settings: `dashboard_port` (default 8765), `weekly_target`
(applications per week, the target line on Metrics, default 10) and `no_response_days` (default 14: an application
still in Applied with no stage change for that long moves to No response; checked on every
dashboard request and by `/today`).

### Company research and CV fit

Marking a job Interested queues company research and a CV fit check for the agent. The panel's
**Research company** and **CV fit** shortcuts queue them by hand; once a result exists, its card
below has **Redo**. Once both results are saved for an application still in
Interested, it gets a "Review research and apply" task, due in two days. The
dashboard never runs the agent. The `/research` skill does the work in a Claude Code session.
It writes `companies/<slug>.md` and `fit/<id>-<slug>.md` to `private_dir`, sets the
application's tier (A/B/C) from the research, and the panel shows the summary (tier and why,
fit score, top gaps) with an **Open** link to a read-only view of the file.

**Tailor CV** asks the agent for a copy of `cv.md` tailored to the job. Each version is its own
file, `private_dir/cv-tailored/CV_<Company>_<RoleShort>_<DDMMYY>_<n>.md` (for example
`CV_Globex_FDE_210926_2.md`), and every version is kept; `cv.md` is never changed. Changes are
asked for in the Applications Assistant box and saved as the next version. Telling the agent which file was sent
("sent CV_Globex_FDE_210926_2") marks that version as sent.

### PDF of a tailored CV

Every saved version also gets a PDF beside it, `CV_<Company>_<RoleShort>_<DDMMYY>_<n>.pdf`, made
from one committed template (`prep/cv_template.html`): A4, the name in mint (#3eb489) with the
contact block under it and the summary beside it, then a narrow column (skills, education…) beside a wide one
(experience, projects), real selectable text in a system font with no tables or images. Contact
details come from the lines under `# Name` (for example `Mobile: … · Email: … · LinkedIn: …`). A
normal CV fits two pages; the page count is printed and a longer one gets a warning. The
panel's Tailored CV card has **PDF** next to the latest version.
`python -m prep.cli tailor pdf APP [--version N]` makes or remakes one, and
`.venv/bin/python -m prep.cvpdf cv.example.md data/example-cv.pdf` shows the template on the
made-up example CV. It needs the venv from Setup (and `pdf_python` in the settings, for the
skills); without it, tailored CVs are still saved, just without a PDF.

### Checks of a tailored CV

Each saved version is checked so that only the flagged lines need reading. Rule checks run at
once (pages, contacts, placeholders, pay, dates, other companies, the job's keywords, wording).
Every save also queues an AI check, done by the agent in a separate pass from the one that wrote
the CV: every changed or added claim traced to `cv.md`, the facts bank or the agreed wording
(an unsourced claim blocks), the CV fit rubric on the tailored copy compared with the master CV's
fit, a six-second skim of the top of page one, and an editorial pass (a general term next to its
own examples, the same fact said twice, wording against the agreed wording or its banned words,
titles, dates and names that differ from `cv.md`; each a look with a suggested wording that keeps
the job's keywords). The card shows one verdict (Ready, Look at
these lines, Blocked), "AI check pending" until the AI check is in, then its scores.
`python -m prep.cli tailor eval APP` stores the agent's result. The facts bank holds true facts
that `cv.md` leaves out: copy `cv-facts.example.md` to `private_dir/cv-facts.md` (or set
`facts_file`) and keep it private.

Queued work is picked up three ways:

- The committed SessionStart hook (`.claude/settings.json`) lists pending work at the start of
  a session.
- `/today` runs it.
- With `assistant_task = <seq>` (and optionally `assistant_project`, `dev3_bin`) in the
  settings, new requests are sent at once to that long-running dev3 task as
  `/research <id>`.

### Applications Assistant box

Under the pipeline list is one **Applications Assistant** box: a conversation with the Applications Assistant for the
whole search. Tick ☐ rows (Pipeline or New offers, up to 4, kept in the URL across views and
reloads), the open panel's **Ask about this** or the ☐ by **My CV** to say what a message is
about; "About:" above the input shows them as chips (× unticks one, **Clear all** all of them).
Nothing ticked asks about everything; one tick asks about that job; two or more ask for a
**comparison** (differences, pros and cons, fit per role). Opening a job only shows it, and
ticks it only when nothing else is ticked. Each message shows its jobs as tags (click one to open
it) or "all jobs"; while something is ticked, **Selected** shows only the messages about those.
Typical messages:

- "Which tasks need my review?" The agent answers with the 3 most urgent open tasks in a fixed
  order (overdue, due today, interview prep for the nearest interview, ready to apply, upcoming,
  no date), numbered, with a few words on why, how many more there are ("show the rest" lists
  them), and separately what it is still working on. "Done 1 and 3" or "move 2
  to Friday" then does it. `python -m prep.cli review [--all]` prints the same order.
- A question about the company or the fit ("good employer?", "their stack?", "competitors?").
  The agent answers from the research and fit files and the job description, searches the web
  if needed, cites sources, and updates the company file when it learns something new.
- A pasted recruiter email or call notes. The agent does what `/stage` does (stage, interview
  date, short comment, extra to-dos) and replies in a line or two.
- A change: add, edit or remove a task or a comment, move the stage, set the tier, record which
  CV was sent, skip the job, or revise the tailored CV. The agent runs the command and replies in
  one line; the list, the panel and the conversation refresh on their own.

The dashboard only stores the message and pings the assistant with the application (or
"general") and message ids, never the text. It shows "Applications Assistant is working…" and refreshes when the
reply arrives. Without an assistant the message waits. The SessionStart hook and `/today`
report it, and `/ask` answers it in any Claude Code session. Pasted emails are treated as data:
the agent never sends email, applies or contacts anyone.

A message still waiting has **Cancel** under it, and **Resend** once it has waited 30 seconds
(at once when the first ping failed; only with an assistant task set). Resend pings the assistant
again about the same message; when dev3 cannot reach it the page says so ("Couldn't reach the
Applications Assistant (seq N). Is its task running?"). Cancel keeps the message, greyed and
marked Cancelled; it leaves every waiting count, and an assistant pinged later skips it. A reply
already being written still arrives, marked "after cancel".

### My CV

**My CV** in the Applications header opens the master CV (`private_dir/cv.md`) in the side panel
(`/?cv=1`; Esc and ✕ close it like a job panel): the skills as chips grouped by theme (one group
per `- **Theme:** a, b; c` line of the Skills section, or per `- **Theme**` bullet with its items as
indented `  - item` sub-bullets), the CV itself, and its **History**: the current
version, with the other saved versions folded under **Older versions**, each with its date and a
one-line note of what changed, **Open**, **Compare** (a line diff against the current CV) and
**Restore**. Nothing is deleted: every version stays on disk. A save whose text is the same as the
current one (`cv save` against cv.md, `tailor save` against the latest tailored version) prints
"No changes, nothing saved" and adds no version. Restore keeps the text it replaces as a
version, so the toast's **Undo** brings it back.

There is no edit box. With the panel open the Applications Assistant box reads "About my CV": ask for the change
("add Terraform to my skills", "shorten my summary", "copy the summary from
CV_Globex_FDE_210926_2 into my main CV"). The agent edits a draft and saves it with
`cv save --note "…"`, which first saves the current text as a version, then replaces `cv.md`
atomically; it replies with what changed and the panel refreshes. It never invents experience.
Versions live in `private_dir/cv-history/cv-<YYYYMMDD-HHMMSS>.md` with their notes in prep.db,
never in git. A hand edit of `cv.md` shows as "changed since the last version" and is saved as a
version before the next change.

### Standing assistant (optional)

A long-running dev3 task that answers thread messages and does queued research as soon as
they arrive:

1. On the project's dev3 board, create a task, for example titled "Standing
   assistant", with this description: "Standing research assistant for Hirey. When
   messaged `/ask <app> <msg>` run /ask; when messaged `/research <id>` run /research. Between
   messages, wait. Never commit, push, apply to jobs or contact anyone." Start it with Claude
   Code and leave it running.
2. Note its seq number (shown on the card), and the project id if the dashboard runs outside
   that project (`dev3 current` inside the task prints both).
3. Add to `~/.config/inter_prep/settings.toml`:

   ```toml
   assistant_task = 23                  # the task's seq
   # assistant_project = "<project id>" # only when needed
   # dev3_bin = "/absolute/path/to/dev3" # if launchd cannot find dev3 on PATH
   ```

4. Restart the dashboard (`launchctl kickstart -k gui/$(id -u)/uk.inter-prep.dashboard`, or
   stop and start `python -m prep.web`). A message sent from a panel now shows up in that
   task's terminal as `/ask <app> <msg>`.

### Learn Assistant box

The Learn page's **Learn Assistant** box is a study partner for the open card: one conversation for all of
Learn, each message tagged with the card that was open (a link back to it). The same box sits under the
tracks on the Learn overview; a message sent there is general (tagged "general", no card), and its reply
shows in the same conversation. Typical messages:

- A question about the card ("explain this more simply", "give me an example"). The agent answers in
  plain English from the card, its **Learn** sources (it may open those links) and general knowledge,
  and ends with a short closing block for quick recall, specific to that card: "Understand:" (one to
  three short bullets, each in words or a tiny code snippet, whichever is clearer; the exact error and
  its message when there is one), an optional "Fix:" when the card is about a bug and code shows the
  fix best (a "DON'T" / "INSTEAD:" pair when there is a tempting wrong fix) and "Say it:" (one or two
  quoted sentences to say in the interview). It never invents facts about an
  employer or the user and keeps the `confirmed` / `accelerator-default` / `unknown` labels.
- "Quiz me on chapter 2": one question per reply from that chapter of the open card's track, then
  short feedback on each answer and the next question.
- **Check my answer**, under the card's Your answer box: saves the answer, then asks the agent, which
  replies with what was covered and what was missed of what the card's question asks, extra points
  from the back as "Extra for the interview", one better phrasing and the closing block.
  An empty answer is refused.

Under each agent reply, **Want a card for this?** asks the agent to draft a card from that reply; it
is saved (`cards add`) only after a "save card" in the box. **Add to notes** appends only the reply's
closing block (Understand, Fix, Say it), exactly as written, blank lines and code indentation kept, to
the card's note, under a heading with the card's question and the
day (the all-prep notes for a general reply, under "Agent" and the day); an older reply without those
lines keeps its "Better phrasing" text as Understand and its "Say it in the interview" line as Say it
(either alone is enough). A reply with none of these adds nothing; a line under the buttons says so
and suggests asking the Learn Assistant to "summarise this as Understand / Say it". The whole reply
is never added. At once and once per reply; a reply mentioning pay is refused.

The dashboard stores each message (topic `learn`, with the card id) and pings the Learn Assistant
with ids only: `/learn-ask <card id> <message id>`. Without one the message waits; the SessionStart
hook and `/today` report it, and `/learn-ask` answers it in any Claude Code session. Learn messages
never show in the Applications Assistant box.

Resend and Cancel work here as in the Applications Assistant box. While a message waits, **Stop** on the
waiting line cancels every waiting Learn message (never an Applications one), on the overview and in a card.

### Learn Assistant (optional)

A second long-running dev3 task, separate from the Applications Assistant, that answers the Learn Assistant box:

1. On the project's dev3 board, create a task, for example titled "Learn Assistant", with this
   description: "Standing Learn Assistant for Hirey's Learn page. When messaged
   `/learn-ask <card> <msg>` or `/learn-ask general <msg>`, run /learn-ask. Between messages, wait.
   Never commit, push, apply to jobs or contact anyone." Start it with Claude Code and leave it
   running.
2. Note its seq number (shown on the card).
3. Add to `~/.config/inter_prep/settings.toml` (`assistant_project` and `dev3_bin`, when set, apply
   to both assistants):

   ```toml
   prep_assistant_task = 24             # the Learn Assistant task's seq
   ```

4. Restart the dashboard. A message sent from the Learn page now shows up in that task's terminal
   as `/learn-ask <card> <msg>`.

### Keep it running with launchd (macOS)

The template `launchd/uk.inter-prep.dashboard.plist.example` starts the dashboard at login
and restarts it if it stops. launchd does not read shell profiles, so the dashboard takes its
settings from `~/.config/inter_prep/settings.toml` (set `prep_db` and `getjo_db` there).
Use the main checkout, not a dev3 worktree, which is deleted when its task completes.

Install, from the main checkout, once its `.venv` exists (Setup):

```bash
sed -e "s|/ABSOLUTE/PATH/TO/inter_prep|$PWD|g" \
    launchd/uk.inter-prep.dashboard.plist.example > ~/Library/LaunchAgents/uk.inter-prep.dashboard.plist
plutil -lint ~/Library/LaunchAgents/uk.inter-prep.dashboard.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/uk.inter-prep.dashboard.plist
open http://localhost:8765    # Applications; the Dashboard is at /dashboard
```

(`cp` the template and edit the placeholder by hand if preferred.) The job runs the venv's
Python, `.venv/bin/python` in the main checkout, never the `python3` on the PATH or a pyenv or
asdf shim: launchd has no shell state, and the venv keeps its interpreter when Homebrew's
`python3` moves to a newer version. After a Python minor upgrade, recreate `.venv` (Setup)
and restart the job (`launchctl kickstart -k`, below).

Everyday commands (run from anywhere):

```bash
# Is it running? (look for "state = running" and the pid)
launchctl print gui/$(id -u)/uk.inter-prep.dashboard | grep -E "state =|pid =|last exit"

# Update to the latest main and restart, so the new code is served
git -C /path/to/inter_prep pull --ff-only
launchctl kickstart -k gui/$(id -u)/uk.inter-prep.dashboard

# Follow the log (request lines and errors; never request bodies)
tail -f /path/to/inter_prep/data/dashboard.log

# Stop until the next login, then start again
launchctl bootout gui/$(id -u)/uk.inter-prep.dashboard
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/uk.inter-prep.dashboard.plist

# Something else holds port 8765?
lsof -nP -iTCP:8765 -sTCP:LISTEN
```

`/path/to/inter_prep` is the main checkout. `kickstart -k` restarts a running job; after
`bootout` the job is unloaded, so use `bootstrap` to start it again (it also starts at the
next login on its own).

Uninstall:

```bash
launchctl bootout gui/$(id -u)/uk.inter-prep.dashboard
rm ~/Library/LaunchAgents/uk.inter-prep.dashboard.plist
```

## Token usage (Metrics, Usage tab)

The Usage tab reads Claude Code's transcripts under `claude_projects_dir` (default
`~/.claude/projects`), every `*.jsonl` below it, read only. Of each line it keeps the time, working
directory, session id and, for API responses, the message id, model, four token counts, the
1-hour part of the cache write, the speed (fast mode) and the inference region. Message
text is never stored, logged or shown. Each page load reads only what was appended since the last
one (byte offsets in `prep.db`); `python -m prep.cli usage scan` does the same from the command line,
for example once before the first page load.

A session's working directory names its dev3 task (`~/.dev3.0/worktrees/<project>/<task id>/…`).
Seq, title, type and whether its PR merged come from `dev3` (cached, refreshed at most every 10
minutes); without dev3 the task id is shown. Settings:

- `claude_projects_dir`: absolute path to the transcripts (default `~/.claude/projects`).
- `coordinator_task`: dev3 seq of the coordinator; tasks of dev3 type coordinator count anyway.
- `context_warn_tokens`: context per turn above which an assistant reads "time to /compact"
  (default 300000).
- `[prices."<model id or prefix>"]` with `input`, `output`, `cache_read`, `cache_write` (5-minute
  cache) and optional `cache_write_1h` (default: `cache_write`) in dollars per million tokens:
  overrides the built-in price for every model id starting with the prefix (longest prefix wins),
  or prices a model the built-in list does not know.

Built-in prices live in `prep/prices.py`, copied from Anthropic's pricing page
(https://platform.claude.com/docs/en/about-claude/pricing, with the date it was read). Each
response is priced on its own: a cache write at the 5-minute or 1-hour rate as the transcript
records it (rows read before that was recorded, whose transcript is gone, at the 1-hour rate,
Claude Code's default), fast mode at the fast price, US-only inference at 1.1x, and Claude Haiku 5.5
at its higher rate when the prompt is over 100,000 tokens. When prices change, the table in
`prep/prices.py` is updated by hand.

The Applications and Learn Assistants are the `assistant_task` and `prep_assistant_task` seqs (in
`assistant_project` when set, else in this checkout's project).

## Privacy model

The repository is public (on GitHub as Hirey); personal data stays local.

- Personal files (`cv.md`, `cv-facts.md`, `cv-history/`, `cv-tailored/`, `companies/`, `fit/`,
  `notes/`, `sessions/`, `*.docx`, `*.xlsx`, `*.pdf`, databases, `config/settings.toml`, `.env`,
  prep documents) are gitignored. Committed templates use the
  `.example` suffix.
- Pay is stored only in the local database: in `applications.pay`, and in agent-thread messages
  as pasted. It never goes into committed files, fixtures, logs, commit messages, the dev3 ping
  or files the agents write, unless asked.
- getjo's database is opened read-only (SQLite `mode=ro`); a write attempt fails.
- The dashboard listens on `127.0.0.1` only, makes no external requests and never shows the pay field
  (`applications.pay`).
- Nothing in this project applies to jobs, sends email or posts anywhere.
- `tests/test_privacy.py` fails if `git ls-files` lists any personal file. The same check runs
  as a pre-commit hook on staged files.

### Pre-commit hook

From the repo root, either point git at the `scripts/` folder:

```bash
git config core.hooksPath scripts
```

or copy the hook (works in worktrees too, where `.git` is a file):

```bash
cp scripts/pre-commit "$(git rev-parse --git-path hooks)/pre-commit"
chmod +x "$(git rev-parse --git-path hooks)/pre-commit"
```

Run the check by hand with `python -m prep.privacy` (tracked files) or
`python -m prep.privacy --staged`.

## Docs

- `README.md` — overview
- `docs/ROADMAP.md` — phases and acceptance criteria
- `docs/how-it-works.md` — components and data flow
- `CLAUDE.md` — conventions for the Claude Code agent working in this repo
