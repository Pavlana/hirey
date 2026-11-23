# How it works

```
                 public job boards
                        │  (polled by getjo)
                        ▼
┌─────────────────────────────────────┐
│ getjo (separate repo)               │
│ fetch → filter → score with Claude  │──► Telegram (matches)
│ data/jobs.db  (jobs table)          │
└──────────────────┬──────────────────┘
                   │ read-only (sqlite URI mode=ro)
                   ▼
┌─────────────────────────────────────┐        ┌───────────────────────────┐
│ Hirey                               │        │ private_dir (gitignored)  │
│ prep/getjo.py   matches, open/closed│        │ cv.md, cv-facts.md,       │
│ prep/db.py      prep.db             │        │ notes/, sessions/,        │
│   applications, status_history,     │        │ companies/, fit/,         │
│   application_comments, interviews, │        │ cv-tailored/ (written by  │
│   tasks, gaps, sessions,            │        │ /research and /ask),      │
│   job_decisions,                    │        │ cv-history/ (cvfile.py);  │
│   agent_requests, company_profiles, │        │ read only by skills that  │
│   cv_fits, tailored_cvs,            │        │ need them                 │
│   tailored_cv_versions,             │        └───────────────────────────┘
│   tailored_cv_checks,               │
│   agent_messages, cv_versions,      │
│   agent_message_targets,            │
│   job_briefs, brief_requests,       │
│   tracks, chapters, cards           │
│ prep/stats.py   Metrics numbers     │
│ prep/usage.py   Claude token usage  │
│ prep/prices.py  Claude API prices   │
│ prep/cli.py     commands            │
│ prep/web.py     dashboard server    │
│ prep/research.py results files      │
│ prep/cvfile.py  master CV versions  │
│ prep/cvpdf.py   tailored CV → PDF   │
│ prep/cvcheck.py tailored CV check   │
│ prep/cveval.py  AI check of a CV    │
│ prep/assistant.py  dev3 message     │
│ prep/learn.py   Learn cards, tracks │
│ prep/learnnotes.py Learn notes files│
└───────▲──────────────────▲──────────┘
        │                  │
 Claude Code skills    browser → http://localhost:8765
 /today /applied /task (127.0.0.1 only; kept running by launchd)
 /stage /research /ask /debrief /mock
```

## Data flow

1. getjo stores each job once in its `jobs` table with a score from 1 to 10.
2. `/today` runs `python -m prep.cli today`. It runs the no-response check (step 7), lists
   the applications it moved in the last 7 days (from their `auto:` history rows, so a move
   already made by the dashboard still shows) and the open tasks due today or earlier with
   their application, then opens getjo's database read-only and lists jobs scored at or above 7 whose
   id is neither in `applications.getjo_job_id` nor in `job_decisions` (`db.excluded_job_ids`),
   leaving out closed and expired jobs and flagging the rest "may be closed" or "expires in N days"
   (step 19).
3. `/applied` records an application. Given a getjo id, URL, or company and role, it looks the
   job up in getjo and stores the link (`getjo_job_id`), so the job drops out of the match list.
   The status is `applied` with `date_applied` today (unless `--date`), or `to_apply` (shown as
   "Interested") with `--status to_apply`. With a linked job it also writes an `applied` or
   `interested` row to `job_decisions`.
4. `/task` adds, lists, completes, edits (`task edit`: title, due date) and deletes tasks. Tasks
   can point at an application or a gap; the dashboard shows an application's tasks in its panel
   (tick to mark done). Tasks with no application are not on the dashboard; `/task list` and
   `/today` show them. The dashboard adds no tasks: the agent does (`cli task add`).
5. Stages: Interested (`to_apply`) → Applied → Recruiter call → Coding interview
   (`technical`) → System design → Company interview (`final`) → Offer; closed: Rejected,
   No response, Withdrawn, Skipped (`db.STATUS_LABELS` holds the labels; stored values are
   unchanged). Skipped is closing an Interested application without applying (step 17);
   Withdrawn is pulling out of a live process.
   A take-home test or a second interview is a comment, not a stage. `/stage` turns a pasted
   recruiter email, call notes or a short message into these commands: it finds the application
   (`cli apps`), runs `cli update ID --status S [--interview-date D] --comment "summary"` (a short
   summary, never the whole email or pay), and adds extra to-dos with `cli task add --app ID`.
   `cli update` to the stage the application is already in moves nothing but still saves the
   comment (as a free comment) and, for an interview stage, the interview date
   (`db.set_interview_date`); when that date differs from the stage's latest one, the stage's
   open auto tasks move to the day before it (today if it has passed), so a rescheduled
   interview keeps its prep on time. Hand-made tasks keep their dates. Every status change
   goes through `db.move_application` (the panel's Mark applied and Skip, `cli update --status`,
   `cli skip`, `record_application` and the no-response check): it writes a `status_history` row with
   the optional comment, sets `date_applied` the first time the application is sent, stores
   an interview date in `interviews` for the interview stages (`db.INTERVIEW_STAGES`:
   recruiter call, coding interview, system design, company interview) and calls
   `db.stage_tasks`. The update only applies while the status is still the one it read, so
   two concurrent requests never move the same application twice. `update_application`
   alone writes history but runs no stage tasks; `add_application` (seed data, tests) adds none.
6. Stage tasks (`db.STAGE_TASKS`): entering a stage closes the open auto tasks of other
   stages (status `closed`, which is not `done`: they drop off the to-do list without
   showing under Recently done),
   then adds the stage's tasks, skipping a title the application already has open (moving
   back into a stage creates fresh ones). Interested: none at once; the agent does its homework
   first (step 13). When company research and the CV fit are both saved for an application
   still in Interested, `db.review_task` adds "Review research and apply" (`db.REVIEW_TASK`,
   kind apply, +2), called from both saves, so the order does not matter; a Redo adds no
   second one while one is open. Older "Apply" (+3), "Research the company" and "Check CV fit /
   tailor CV" tasks count as Interested auto tasks (`db.RETIRED_STAGE_TASKS`); open ones stay as
   they are and close when the application moves on. Applied: Chase if no reply (+7), and the
   open Interested apply tasks (Review research and apply, or an older Apply) are marked done. Recruiter call: Prepare for the call. Coding interview: Coding practice and
   Review the role's tech stack. System design: System design practice. Company interview:
   Prepare stories, company values, questions to ask. Interview-stage tasks are due the day
   before the interview date (today if that has passed; +2 days without a date). Offer:
   Review the offer (+2). Rejected and No response: Debrief — what to improve (+3).
   Withdrawn adds none. Auto tasks carry their stage in `tasks.stage`; tasks made before that
   column existed got it from their title when the column was added. Hand-made tasks have
   none and are never closed automatically. Entering Interested also queues company research
   and a CV fit for the agent (step 13; the CV fit only when the application has none).
7. No response: an application still in Applied whose last stage change (latest
   `status_history` row, else `date_applied`, else `created_at`) is `no_response_days` (14)
   or more days old moves to `no_response` with the comment "auto: no reply in 14 days"
   (`db.auto_no_response`). The Chase task (day 7) is closed and a Debrief task added. It runs
   on every dashboard request and in `prep.cli today`. The agent can move it back as usual.
8. Comments: a stage change can carry a comment (`status_history.comment`); free comments go
   to `application_comments`. `db.comments` merges both, newest first, each with a ref: `c<id>`
   for a free comment, `h<id>` for a stage change's comment. The panel lists them under its
   header (read-only) and the pipeline list shows the latest with the count
   (`db.latest_comments`). The agent adds them (`cli comment`), lists them with refs
   (`cli comments list`, also in `thread show`), replaces the text (`comments amend REF`) or
   removes one (`comments remove REF`: a free comment is deleted; a stage change keeps its move
   and loses only the comment). `db.timeline` gives the stage changes for the panel's history.
9. Next action: `applications.next_action` / `next_action_date` always hold the application's
   next open task (dated first, earliest first), set by `db.sync_next_action` whenever its
   tasks change. They are kept for older readers and no longer typed by hand;
   `--next-action` on `applied` and `update` adds a task instead. When `tasks.stage` is first
   added to an older database, each still-open application's typed next action becomes an
   admin task with its date (`db.next_actions_to_tasks`), so it keeps showing as due; the
   example data does the same. Applications created
   before `status_history` existed have no history rows; their current status is the only
   known point.
10. The dashboard (`python -m prep.web`) reads and writes the same `prep.db`. Each request
   opens a fresh connection (and a fresh read-only getjo connection: New offers, the pipeline's
   closed badges, the nav count), so the skills and the browser see each other's writes on the
   next request, without a restart. Interested in an offer's panel, `/applied --job-id` and the
   old `/offers/apply` endpoint (no button any more) all go through `db.record_application`, so
   they behave the same way (one application per getjo job, never moved back to an earlier stage;
   an explicit `to_apply` never replaces a later status). It also moves the job's Applications Assistant box
   messages to the application (step 15).
11. New offers triage writes `job_decisions`: one row per getjo job, `interested`, `applied`
   or `skipped` with a reason (`db.SKIP_REASONS`: location, seniority, role_type, stack, company, pay,
   contract_type, other) and an optional comment. The row copies the job's company, title, location, score,
   url and source at decision time, so the label still reads correctly after getjo drops the
   job. Undo deletes a skip row; an applied row is never undone there. Undo after Interested
   (`db.undo_interested`) deletes the decision and the `to_apply` application with its queued
   requests, in one transaction, only while nothing else has happened to it (one history row;
   no tasks, comments, interviews, gaps, CV fit or tier; no request claimed by the agent), so
   the job is new again; otherwise it is refused and the panel's Skip closes it instead. Undo
   skip on a job whose application was skipped from its panel (step 17) deletes the decision
   and moves the application back to Interested in one transaction (`db.undo_skip`), so the
   job reads as interested again, not applied. Applications linked to
   a getjo job without a row (recorded before the table existed) count as applied when read
   (`db.job_decisions`). A job with a linked application reads its label from the live
   status: `interested` while it is `to_apply`, `applied` once it moves on (a skip stays a
   skip), whatever was stored, so each job has one label. These rows are the labelled data
   meant for tuning getjo's search.
12. `python -m prep.cli feedback export` writes every decision, oldest first, to
   `private_dir/feedback/job_decisions.jsonl` (or `--out`): one JSON object per line with
   `getjo_job_id, decision (interested / applied / skipped), reason, comment, decided_at, company, title, location, score, url,
   source`. Rows with no location or score take them from getjo (read-only) when it still has
   the job. The file is rewritten in full through a temp file and a rename, so a reader never
   sees half of it. getjo reads it with its own code (recent decisions as few-shot examples in
   its scoring prompt); Hirey never calls getjo. Comments are included, so they reach
   whatever prompt getjo builds from the file.

13. Agent research and CV fit. The agent, not the dashboard, researches the company and
   checks the CV against the job. The dashboard and the CLI only queue requests in
   `agent_requests` (kind `research` or `cv_fit`; `tailor` and `cv_eval` in step 16; status `pending` → `claimed` → `done`,
   `force` for Redo). A partial unique index allows one open request per application and kind,
   so a double click never queues twice. Requests come from entering Interested
   (`db.stage_tasks`, so an offer's Interested button, `/applied --status to_apply` and a move to
   Interested all queue them), from the side panel's Research / CV fit buttons, and from
   `prep.cli research queue`. The `/research` skill loops on `prep.cli research next`, which
   claims one application's requests atomically (a claim older than 6 hours counts as pending
   again) and prints a work packet. For research it gives a verdict: reuse a profile younger than
   `research_max_age_days` (90), or research again when it is stale, missing or forced. For the
   CV fit it gives the cv.md path and the job description, read from getjo with
   `getjo.job_description`. The agent writes Markdown files in `private_dir`:
   `companies/<slug>.md`, one per company (`db.company_slug`: lowercase words, legal suffixes
   dropped; a stable key for later company notes), and `fit/<app-id>-<slug>.md`, one per
   application. Each starts with TOML front matter between `+++` lines (`prep/research.py`).
   `prep.cli research save` and `fit save` read it into `company_profiles` and `cv_fits`, so a
   hand edit followed by a save updates the summary. A company save closes the research
   requests of every application at that company. A fit save adds its gaps to `gaps` (session
   "cv fit", `gaps.application_id`), skipping any gap already open with the same text (ignoring
   case and spacing). The research sets the tier (A: strong fit at a recognised or good
   employer; B: decent fit or less known; C: weak fit or risky; rubric in the skill):
   `research save APP` copies the profile's `suggested_tier` into `applications.tier` of that
   application only, since a profile is shared by every role at the company while the tier
   depends on each role's fit. The panel's company card shows the tier with the profile's
   `tier_reason` (the reason only when the tier still matches the profile). There is no confirm
   step; the user changes it through the agent (`cli update APP --tier X`).
14. Picking up queued work without typing. A committed SessionStart hook
   (`.claude/settings.json`) runs `python3 -m prep.cli queue`. That command opens prep.db with
   `mode=ro`, creates nothing, and prints one line with counts and application ids only. A
   missing database prints nothing. The line offers /research; it is not an order, because the
   hook also runs in coding sessions. `/today` runs /research when work is pending. Optionally,
   `assistant_task` names a long-running dev3 task. When a dashboard request or a CLI
   `applied` / `update` queues new requests, each affected application is sent
   `/research <id>` with `dev3 message --task seq:<N> [--project <assistant_project>]`
   (`prep/assistant.py`). The command is a fixed argument list built from ids, with no shell
   and a 15-second timeout. The dashboard runs it in a background thread. dev3 is found through
   `dev3_bin`, then PATH, then `~/.dev3.0/bin/dev3`. A failure is logged and the requests stay
   queued for the hook or /today.
15. Applications Assistant box. One conversation with the Applications Assistant for the whole search, in a box under
   the list (not in a panel). What a message is about is ticked, not opened: every Pipeline and New
   offers row, the open panel's header ("Ask about this") and My CV in the page header have a ☐.
   The ticks live in the URL (`sel=a:<app id>`, `sel=j:<getjo id>`, `sel=cv`, repeatable, in order,
   at most `db.MAX_TARGETS` (4); `web.parse_sel`), so every view renders them without the script, and
   they are mirrored to `localStorage` (`apps-sel`, try/catch) only so a bare `/` brings them back.
   `home_url` carries `sel` into every link, form `return` fields carry it through posts, and a
   redirect built from ids (Interested, Mark applied) gets the `return`'s `sel` (`web._with_sel`).
   On render, unknown ids are dropped (an application removed by Undo Interested) and a ticked job
   tracked since is named by its application (`web.sel_targets`). A tick is a link toggling its key
   (`web.toggle_sel`); at the limit it only shows "Up to 4 at a time". Opening a row, tag or My CV
   only shows it, and ticks it only when nothing is ticked yet (`web.auto_sel`, the same rule in the
   script). Above the input, "About:" shows a chip per ticked target (each with ×, a hidden `about`
   field the form posts) and Clear all; nothing ticked reads "the whole search". The form posts
   every `about` value with `about_set=1`: none is a general message, one is a message about that
   application, job offer or the master CV (as before: `application_id`, `getjo_job_id` or `topic`;
   a job tracked since goes to its application, an unknown one is refused), and 2 to 4 are a
   **comparison**: `topic = 'compare'` (`db.COMPARE`, set by `db.add_compare` from the count, never
   taken from the form), no application or job in its own columns. An older form's `app_id` /
   `job_id` / `topic` fields still work. Every message's targets are rows of
   `agent_message_targets` (message id, `application_id` with its getjo id when it has one,
   `getjo_job_id`, `topic`; none for a general message); messages from before the table get one row
   from their columns on connect (`db._backfill_message_targets`). An agent reply copies its
   question's targets. `db.thread(app_id=…)` (and `job_id`, `topic`) also lists comparisons naming
   it; claims by application, job, topic or general never take a comparison (its topic is set), so
   only `thread show --message MSG --claim` claims one, by itself (`db.claim_message`).
   "General" means both columns are NULL everywhere it is read (claims, the general `thread show`,
   the `queue` and `today` lines). A job message is tagged with the company (looked up in getjo,
   else a skip's stored copy, else "job offer") and links to its offer panel. The agent's reply
   carries the job id too. When the job is marked Interested (`db.record_application`, from the
   dashboard or `/applied`), its messages and target rows get the new `application_id` and keep the
   job id, so Undo Interested hands them back to the job (`application_id` NULL again, target rows
   too, so the foreign key never blocks the delete) in the same transaction; those moved messages do not block the undo, while one written after Interested
   (no job id) still does. Messages show oldest first, the latest
   `web.AGENT_MESSAGES` (60), each tagged with its job (a link to its panel), a tag per target for a
   comparison, or "all jobs", and listing its target keys in `data-targets`; while something is
   ticked an All | Selected filter shows (Selected: messages about any ticked target; remembered in
   `localStorage`). The conversation scrolls as one: no message has a scrollbar of its own. Send posts
   `/applications/message`, which stores a row (author `user`, status `pending`). The author is
   always `user`, whatever the form says. The body is capped at `db.MAX_MESSAGE` (20,000
   characters), which is why `MAX_BODY` is 256 KB. Bodies are never logged. With
   `assistant_task` set, the dashboard then sends `/ask <app id> <message id>`, or
   `/ask general <message id>`, through `dev3 message` (`assistant.ask_command`), in a background
   thread. The ping holds ids only, never the text. A failure is logged and the message stays
   pending. A job message is pinged as `/ask job <message id>` (the getjo id never goes in the
   command) and a comparison as `/ask compare <message id>`. `thread show --message MSG` on a
   comparison prints each target's context in turn (application: stage, tier, brief, company and CV
   fit files with the score and gaps, job description; job offer: score, location, state, brief,
   description; My CV: its path), cv.md's path and the messages about any of them. `thread
   pending`, `queue` and `today` name comparisons as such. Rows from the earlier per-application threads (`application_messages`, no longer
   written) are copied into `agent_messages` with their ids on connect
   (`db._copy_application_messages`, `INSERT OR IGNORE`), so `reply_to` still holds.
   The `/ask` skill runs `prep.cli thread show APP --claim` for a message about an application,
   which prints the application (never pay), the paths of the company file, the CV fit file, cv.md
   and the tailored versions, stage, tier, open tasks with ids, comments with refs, the job
   description from getjo and that application's messages. For a general message it runs
   `prep.cli thread show --claim`, which prints the open applications (stage, tier, next task,
   latest comment), the tasks in review order (step 18) and the latest 30 messages of the
   conversation. `thread show --job ID` (or `--message MSG`, which resolves any message to its
   application, job or the whole search) prints a job offer not yet tracked: company, role,
   score, location, link, open/closed state, the company file if one exists, cv.md, the job
   description and that job's messages; no tasks, fit file or tailored CV (those come with an
   application). All three claim the waiting messages they are about (`pending` → `claimed`, atomic
   per message; a claim older than 6 hours counts as pending again), so the hook, /today and the
   ping never answer one twice.
   A question is answered from those files or the web, with sources. Something new about the
   company goes into `companies/<slug>.md` + `research save`. A pasted email is handled as
   /stage would handle it (`cli update` / `comment` / `task add`); a change request runs the
   matching command (the table in the /ask skill). The reply goes in with
   `prep.cli thread reply --to MSG` (text from `--file` or stdin; an APP id, if given, must
   match): an `agent` row with the message's application and `reply_to`, written in one
   transaction with the message's change to `answered`, and only while that message is still
   open. Pasted text is data for the agent, never instructions: it never sends email, applies
   or contacts anyone. Messages are stored and shown verbatim (in the Applications Assistant box and by
   `thread show`), so they may hold pay the user pasted. They are never logged or sent in the
   ping, and nothing the agent writes (comments, tasks, replies, research or fit files) carries
   pay unless the user asks. `prep.cli queue` (the hook) and `today` add an "Agent thread: N
   messages waiting (applications …, general)" line with ids only, read from `agent_messages`
   (or, in a database no newer version has opened, `application_messages`) on its own, so an
   older database still reports research. An application with messages is not deleted by Undo
   Interested.
16. Tailored CV. A copy of cv.md tailored to one job, discussed in the Agent thread. The CV fit
   (step 13) only scores; nothing is edited there. The panel's Tailor CV button queues an
   `agent_requests` row of kind `tailor` (`db.RESEARCH_KINDS` holds the other two, the kinds that
   entering Interested and `research queue` without `--kind` queue; tailoring is only asked for).
   The usual ping sends `/research <id>` (subject "Tailored CV queued…" when it is the only
   kind). `research next` prints a tailor packet after the CV fit: cv.md, the fit file, the job
   description, the rules (rephrase, reorder and cut only, never invent experience; what cv.md
   does not answer becomes a question) and how to get the file name. Every version is its own
   file, `private_dir/cv-tailored/CV_<Company>_<RoleShort>_<DDMMYY>_<n>.md`
   (`research.tailored_file`: the company with legal suffixes dropped and letters and digits
   only, `applications.role_short` chosen by the agent once and kept, the version's date and its
   number). `prep.cli tailor path APP [--role-short X] [--copy]` stores the role short name the
   first time and prints the next version's path (`--copy` starts it from the latest version,
   for a revision). `tailor save APP` (note on stdin; `--file` for another path) calls
   `db.save_tailored_cv`, which in one transaction adds a `tailored_cv_versions` row, points
   `tailored_cvs` at it (the latest version), closes the tailor request and posts the note as an
   agent message with no `reply_to` (the only unprompted agent message). Versions are never
   overwritten or deleted, and cv.md is never written. The user answers or asks for changes in
   the thread; `/ask` runs `tailor path APP --copy`, edits the new file and runs
   `tailor save APP --to MSG`, whose note is the reply to that message (answered in the same
   transaction). A copy whose text (front matter aside) is the same as the latest version's is not
   saved: `tailor save` prints "No changes, nothing saved" and records nothing (the file stays).
   `tailor save` prints the copy's word count and warns above
   `research.TAILOR_MAX_WORDS` (1,000, about two pages).
   `private_dir/cv-tailored/_style.md` (`research.style_file`) holds wording agreed in the thread,
   such as a career gap line, reused in every copy; the packet and `thread show` print its path.
   The tailoring rules (length, structure, impact, keyword coverage in the note) are in the
   /research skill. While a tailor request is open, the panel's button reads "Tailoring…" and the
   panel is busy (`data-busy`, and `pending` in `/applications/thread?app=ID`), so the page polls
   until the note arrives in the Applications Assistant box. Which version was sent is recorded only when the user says
   so: `cli cv sent APP FILE_NAME` (`db.record_cv_sent`; the name of one of the application's
   versions, with or without `.md`) stores the file name, its version and the time in
   `applications.cv_sent`, `cv_sent_version` and `cv_sent_at`. Applying records nothing and
   copies nothing. A tailored copy saved before versions were kept becomes one version row on
   the next connect (`db._backfill_tailored_versions`; the file keeps its old name). A tailored
   copy blocks Undo Interested. Tailored files are agent-written: never pay.
   PDF: `prep/cvpdf.py` turns a version's Markdown into a PDF beside it, same name with `.pdf`
   (`CV_Globex_FDE_210926_2.pdf`, gitignored like every `*.pdf`). The front matter is dropped, the
   body goes through `prep/md.py` (the dashboard's escaped Markdown subset: headings, lists with one level of indented sub-bullets, paragraphs, ``` code blocks, `code`, bold, italic; here with http(s) and
   mailto links kept), is grouped by `cvpdf.cv_layout` and goes into the committed template
   `prep/cv_template.html`. Layout: a header with, on the left, the name on two lines (Helvetica Neue Bold,
   uppercase), the title line, a focus line ("A • B • C", when there is one) and the contact block,
   one item per line; on the right, the text of the first section named like `cvpdf.SUMMARY_WORDS`
   (summary, profile, about), without its heading. Below, a narrow column (sections whose heading has a word of `cvpdf.SIDE_WORDS`: skills, education…) and a
   wide one (experience, projects, anything else). Skills groups (`- **Group**` with indented
   `  - item` sub-bullets) show the group name, then one item per bullet in two tight columns, at 8.5pt. `cvpdf.masthead` sorts the " · " items of
   the lines between `# Name` and the first `##`: contact details (a `Mobile:`/`Email:`/`LinkedIn:`-style
   label, an email, a link, a phone number, or "Town, UK"/"Town, Country") go to the contact block in
   the order location, mobile, email, LinkedIn, GitHub, shown as values only (labels dropped); the first other item is the title (or a role
   after " — " in the name line), the rest, split at commas, the focus line. A role, written as
   `### Title · Company · Mon YYYY – Present` or `**Title — Company**, City, YYYY–YYYY`, becomes the
   title, the dates (the first later part with a year or "Present") and the company line. A role with a company line (a job or project) has its title in the name's mint; one without
   (a break) is slate like the section headings; dates are grey caps, company lines plain. The summary starts level with the title line under the name. Words are
   kept as written; only grouped (no heading renamed). A CV without a `# Name` heading stays one
   column. Black on white; the name in mint (#3eb489), section headings and rules in slate (#59656f); a
   system font (no font file), real selectable text, ligatures off, bullets as text in the line, no
   tables, images, headers or footers. The HTML (and so the text order applicant tracking systems
   read) is name, title, focus, contact, summary, narrow column, wide column; a CSS grid puts the
   summary beside the name. A narrow-column section that does not fit moves whole to the next page.
   WeasyPrint
   renders it (the only third-party library, `requirements.txt`, imported only here). `tailor save`
   makes the PDF of the version it saves and prints the page count, with a warning above two
   pages (a CV of `TAILOR_MAX_WORDS` words fits two); a failed or impossible render prints
   "no PDF: …" and never undoes the save. `tailor pdf APP [--version N]` (re)makes one, the latest
   by default, and fails when it cannot. When the running Python cannot load WeasyPrint (the
   skills use the system `python3`; a worktree has no venv), the `pdf_python` setting names one
   that can, and `cvpdf` runs `pdf_python -m prep.cvpdf IN OUT` from the same checkout. The panel's
   Tailored CV card shows **PDF** on the latest version once its file exists; `/research/pdf?app=ID&v=N`
   serves it inline (only the `.pdf` beside a recorded version inside `private_dir`, else 404), with
   its own headers since the pages' CSP can block the browser's PDF viewer.
   Check: after the PDF, `tailor save` checks the version (`prep/cvcheck.py`, `cvcheck.run`), so
   reviewing a tailored CV means reading only the flagged lines and skimming the PDF. Rule checks,
   no AI: the PDF has at most `cvpdf.MAX_PAGES` pages (no PDF: page count unknown); an email and a
   link are there and every header contact is kept: per field (email, phone, LinkedIn, GitHub,
   other link) the values in the facts bank's `## Contact` section (`cvcheck.contact_section`:
   `- Email: …` lines, empty ones skipped), else those in cv.md's header (the lines before the
   first `##`), since the skills build each tailored header from that section and fall back to cv.md (a phone number
   compared by its digits with a "(0)" after the country code dropped; digits inside a link are no phone); no placeholder (`[X]` outside a Markdown link, TODO, TBD, `<...>`, `{{...}}`,
   lorem ipsum, `??`; a `<https://…>` autolink is a look, since the PDF prints its brackets); pay, in lines other than the user's own from cv.md and never quoted:
   clear pay wording (salary, compensation, per annum, day rate, OTE, bonus…: `cvcheck.PAY_WORDS`)
   within `cvcheck.PAY_NEAR` characters of a number blocks, a money or "k" figure that neither
   cv.md nor the facts bank has is a look (so "$2M" or "10k users" kept from cv.md on a reworded
   line passes), and another word of `db.PAY_PATTERN` (pay, paid) is a look; dates (a range that runs backwards, a future year, a year that
   neither cv.md nor the facts bank `private_dir/cv-facts.md` mentions: tailoring never changes
   dates); no other application's company name (legal suffixes dropped, names in cv.md or the
   facts bank, such as a past employer or a tool, left out); the job's must-have terms (the file's
   front matter `keywords`, in the posting's wording, less `gaps`, the ones the CV cannot
   truthfully claim) each in the body as written, since applicant tracking systems match words
   literally (look); the job title without seniority (`cvcheck.title_core`) in the name lines or
   the first section (look); and signs a recruiter reads as machine-written, only in lines the
   agent wrote (not the user's own lines from cv.md): stock words (`cvcheck.STOCK_PHRASES`), dashes
   inside sentences in two or more lines, three or more bullets opening with the same word, and four
   or more rewritten bullets with no number (all look). Each finding is `block` or `look` with the line number in the .md file
   (front matter counted) and the line, never one that mentions pay. The verdict follows
   (`db.check_verdict`): Blocked when any finding blocks, Look at these lines when any asks for a
   look, else Ready. `cvcheck.changes` lists what changed against cv.md: sections added, removed or
   moved, per section lines reworded, cut or added (or reordered), the word counts and the gaps. Both go
   into `tailored_cv_checks` (one row per version) through `db.save_cv_check(conn, app, v, source,
   findings, changes)`, which replaces only that source's findings ("rules" here) and works the
   verdict out again from all of them: a later checker (AI) adds its own source to the same row
   and verdict. The CLI prints the verdict, flagged lines and changes, and logs only the verdict
   and counts; a failed check prints "no check: …" and never undoes the save. `tailor pdf` checks
   the version again with the new page count; `tailor check APP [--version N]` checks it again
   without WeasyPrint, the page count read from its existing PDF (`cvpdf.page_count`: page objects
   counted in the file and its Flate-compressed object streams; no PDF is a look as before). The Tailored CV card shows the verdict badge beside
   the latest version's PDF link, its flagged lines and the changes; a version saved before checks
   existed reads "not checked yet".
   AI check (`prep/cveval.py`): `db.save_tailored_cv` also queues an `agent_requests` row of kind
   `cv_eval` in the same transaction (`INSERT OR IGNORE`: an open one is reused, since it always
   checks the latest version), so every save, a revision through /ask included, gets one;
   `tailor save` pings the Applications Assistant like other queued work. `research next` leaves it out
   (neither picked nor claimed, so other applications' work goes first) while a tailor request is open
   for that application, and prints the version file
   and number, cv.md, the facts bank, `_style.md`, the job description, the master CV's fit score and
   the result file, never the tailor note or the thread: the /research skill runs it in a fresh
   subagent, a separate pass from the one that wrote the CV. The agent writes
   `cv-tailored/<version file stem>.eval.md` (TOML front matter: `version`, `fit_score` and
   `skim_score` 1-5, `skim_why`, `[[claims]]` with `quote`, `source` cv.md / facts / style / none
   and `note`, `[[must_haves]]` with `term` and `covered`; rubric in the skill) and runs
   `tailor eval APP [--version N] [--file PATH]`. `cveval.read_eval` validates it and
   `cveval.findings` maps it with fixed rules, source "ai": a claim with source `none` blocks (its
   line found in the version by the quoted words, so model line numbers are never trusted; `save`
   refuses the file when such a quote is not in the version, since a blocking finding needs its line); must-haves not covered ask for a look, except terms the version's front
   matter already lists under `keywords` or `gaps` (the rule checks handle those); a `fit_score`
   below the master CV's `cv_fits.score` for the same job asks for a look (compared in code); a
   `skim_score` of `cveval.LOW_SKIM` (2) or less asks for a look. The agent's notes are dropped from
   a finding when they mention pay. `db.save_cv_eval` stores them through `save_cv_check` (merged
   with the rule findings into one verdict; a rule re-check keeps them) with a `summary` (JSON
   column added by MIGRATIONS: fit, master fit, skim and why, claim and must-have counts), and
   closes the `cv_eval` request only when that version is still the latest, so a version saved
   while the check ran is checked next. `db.eval_state` is done, pending or none (versions saved
   before AI checks). The CLI's check line and the card's verdict carry "AI check pending" until it
   is in (the panel is busy and polls meanwhile), then the scores line (`cveval.summary_line`) and
   the skim's why. The facts bank is the `facts_file` setting, default `private_dir/cv-facts.md`
   (`config.facts_path`; template `cv-facts.example.md`), read by the rule checks too.
17. Mark applied and Skip. The panel's two direct actions, both only while the application is
   Interested. Mark applied (`/applications/apply`) moves it to Applied today through
   `db.move_application` (Applied stage tasks, `date_applied`). Its Undo
   (`/applications/unapply`, `db.undo_applied`) works only right after (the latest history row
   is applied, straight from to_apply): in one transaction it deletes that history row and the
   Applied stage's open tasks, reopens the apply tasks the move marked done and clears
   `date_applied` unless the application had been sent before. Skip (`/applications/skip`,
   `cli skip APP --reason R [--comment C]`, `db.skip_application`) takes a reason from
   `db.SKIP_REASONS` and an optional comment, moves the application to `skipped` with the
   comment "skipped: <reason>[ — comment]", closes its open tasks and agent requests, and for a
   getjo job updates (or adds) its `job_decisions` row to skipped with the reason and comment,
   keeping the snapshot, so the job shows under New offers → Show skipped. Skipped is closed: never
   submitted, never a reply (stats).
18. Review order. `prep.cli review` (read-only; also inside the general `thread show`) prints
   the top 3 open tasks (`cli.REVIEW_TOP`; `--all` for every one) and how many more there are,
   in a fixed order (`db.review_tasks`, `db.REVIEW_GROUPS`): overdue, due today,
   interview prep (tasks of the interview stages, nearest interview date first), ready to apply
   (apply kind, such as "Review research and apply"), upcoming by due date, then undated; ties by
   due date and id. Each line has the group, due and interview dates, the task, its application
   (or "no application") and the task id. Then, separately, the agent's own open requests
   (research, CV fit, tailored CV), which wait on the agent, not the user. /ask answers "which
   tasks need me?" with those 3 in that order, each with its task id, so "done 1 and 3" maps
   back, and lists the rest when asked.
19. Open and closed jobs. getjo adds `posted_at`, `expires_at`, `last_seen` and `closed_at` to its
   `jobs` table (getjo docs/design.md, "Open and closed jobs"; all `YYYY-MM-DDTHH:MM:SSZ` UTC).
   `prep/getjo.py` builds its select list per connection from `PRAGMA table_info(jobs)`: on an
   older jobs.db the missing columns read as NULL, so every job reads as open and nothing else
   changes. `getjo.matches` (New offers, `/today`, the counts) leaves out a job with `closed_at`
   set or `expires_at` not after now, compared as text in the same stamp format (the dashboard
   uses noon UTC on a test's `today`). `getjo.get_job` and `jobs_by_id` return closed jobs too.
   `getjo.job_state` gives `closed`, `may_be_closed` (`last_seen` more than `stale_days`, default
   3, days before now) and `expires_in` (days left when 7 or fewer, `getjo.EXPIRES_SOON_DAYS`),
   shown as chips in New offers and the offer panel, and as flags in `/today`. An Interested or
   Applied application whose job has closed or expired stays in the pipeline with a "closed"
   badge on its row and in its panel (interview stages show none: the process goes on).
20. Master CV (`prep/cvfile.py`). `private_dir/cv.md` is shown in the My CV panel (`/?cv=1`) and read
   by /research and /ask. Saved versions are states of the file: `private_dir/cv-history/
   cv-<YYYYMMDD-HHMMSS>[-N].md` plus a `cv_versions` row (vN, file name, note of what changed into it,
   sha256, time). The current version is the one whose sha256 matches cv.md (none after a hand edit).
   Every write goes through `cvfile.write_cv(text, note)`: refuses empty or unchanged text; saves the
   replaced text as a version first unless the latest version holds it; saves the new text as a
   version; replaces cv.md through a temp file and `os.replace`. `cvfile.restore(V)` is a write of
   V's text, so Undo is restoring the version it replaced. The agent never edits cv.md: `cli cv draft`
   copies it to `cv-history/_draft.md`, then `cli cv save --note "…" [--to MSG]` writes it (with
   `--to`, checked first, the reply answers that CV message); a draft the same as cv.md saves nothing
   and prints "No changes, nothing saved" (`cvfile.save_draft` returns None). `cv restore`, `cv diff`, `cv history`,
   `cv skills` and `cv show` complete it. Skills: the section under the first heading containing
   "skill"; `- **Theme:** a, b; c` lines, `### Theme`, or a `- **Theme**` bullet with indented `  - item`
   sub-bullets are groups. Only recorded version names
   resolving inside private_dir are read. Notes and CV text are never logged. With the panel open the
   Applications Assistant box's "About my CV" sends `topic=cv` (`agent_messages.topic`, carried by the reply), tagged
   "My CV", pinged as `/ask cv <id>`; a message with no application, job or topic is general.
   `thread show --cv [--claim]` prints the CV's state, versions, tailored files and CV messages.

21. Job briefs. A short structured summary of a getjo job, written by the agent from the
   description and shown at the top of the job's panel (offers and applications linked to a getjo
   job), so Interested or Skip is decided quickly. Stored per getjo job in `job_briefs` (Markdown:
   the key facts line first, e.g. "hybrid 2 days · Python · LLM APIs · Senior · London", then
   `## Summary`, `## Must-haves`, `## Nice-to-haves`, `## Where & terms`, `## Team & product`,
   `## Interview process` when the posting states it, `## Red flags / notable`). `db.save_brief`
   refuses a heading before the facts line, a facts line over `db.MAX_FACTS` (200) characters, more
   than `db.MAX_BRIEF_WORDS` (300) words and anything that mentions pay (`db.PAY_PATTERN`: currency
   signs, "80k", salary, pay, bonus, compensation…). Requests live in `brief_requests` (one row per
   getjo job, since `agent_requests` needs an application): pending → claimed → done, `force` for
   Redo, stale claims after 6 hours. Who asks: the Applications page queues one for each new offer
   scored at least `brief_auto_min_score` (8) that has neither a brief nor a request
   (`db.queue_missing_briefs`), and for an untracked offer the first time its panel is opened; the
   Write brief / Redo brief button posts `/offers/brief` (`db.queue_brief`); `/research` writes the
   brief of a tracked job during company research (a "Job brief" step in the packet when there is
   none, or on Redo); `/ask` writes one when asked ("key facts"). When the page queued any, it
   sends `/research briefs` to the Applications Assistant once (a count, no ids;
   `assistant.notify_briefs_in_background`). `/research` loops on `prep.cli brief next` (claims the
   oldest, prints the job, the format and the description) and `brief save --job ID` (text on
   stdin); `brief show` prints one. `queue` and `today` add "Job briefs: N waiting". While a brief is
   being written the panel shows "Writing brief…" and is busy (`data-busy`), so the page polls
   `/applications/thread?job=ID` (or `?app=ID`) until it is in. Without a brief, the panel shows
   the original description made readable (`web._description`: "----" separators dropped, a short
   line opening a part or ending in ":" or in capitals as a heading, "-", "•", "1." lines as lists,
   other lines as paragraphs, everything escaped), folded to a few lines with Show all (the script;
   in full without it). With a brief, the facts line shows as chips, the sections through
   `render_markdown` (escaped), and the original behind Show original. `thread show` prints the
   brief for an application, a job offer and each target of a comparison.
22. Practice sessions, gaps and drills (Phase 3 data layer; debriefs in step 23, mocks in step 24).
   `sessions` holds one row per practice session: date, kind (`db.SESSION_KINDS`: `mock`, `real`
   for a real interview debriefed), optional application and interview stage (`db.INTERVIEW_STAGES`),
   scores per area as a JSON object (`{"system design": 3}`, whole numbers 1–5) and the path of its
   transcript or notes, which must be under `private_dir` (a relative `--file` goes to
   `private_dir/sessions/`). `prep.cli session add --kind K [--app ID] [--stage S] [--date D]
   [--score AREA=N ...] [--file F] [--gap TEXT ...] [--due D]` calls `db.record_session`: it checks
   every value first (nothing is written when one is wrong), inserts the session, then
   `db.add_gap_drills` handles the named weaknesses. Each one matches an open gap with the same text
   (ignoring case and spacing, as for CV fit gaps) or becomes a new gap with `gaps.session_id`
   pointing at the session, `session` set to its kind and the session's application. A matched gap
   (for example one from a CV fit) gets the session only when it has none. Every named gap without an
   open task gets one `drill` task, "Drill: <gap>" (`tasks.gap_id`, the gap's application, `--due` or
   undated), so the application's next action follows. CV fit gaps get no drill until a session or
   `gap add` names them. `prep.cli gap add TEXT ... [--session ID] [--app ID] [--due D]` does the
   same without a new session; `gap list [--all | --closed]` shows each gap with its open drill count;
   `gap close ID` (`db.close_gap`) closes the gap and marks its open tasks done. `session list`
   prints the sessions newest first. A session on an application blocks Undo Interested, as gaps do.
23. Debriefs. `/debrief` turns a pasted or dictated transcript of a real interview, or of a mock done
   elsewhere, into one `prep.cli debrief --kind real|mock [--app ID] [--stage S] [--date D]
   [--questions T] [--went-well T] [--to-improve T] [--score AREA=N ...] [--gap TEXT ...] [--due D]
   [--transcript]` call. `--transcript` reads stdin into a new file under `private_dir/sessions/`
   (`DATE-COMPANY-STAGE-debrief.md`, `-2`, `-3`, ... when taken; removed again when nothing is
   recorded). `db.record_debrief` checks every value first, then: for a real interview (an
   application is required) it fills the questions, went well and to improve of the `interviews` row
   `/stage` stored for the same stage and date, or adds one through `add_interview` (never
   `set_interview_date`, so no task dates move); a mock has no `interviews` row (it is not an interview
   with the company, and its date would count as one), its notes stay in the file. Then
   `record_session` adds the session (kind `real` or `mock`; the stage defaults to the application's
   when that is an interview stage) with its gaps and drill tasks (step 22), and the application's
   open "Debrief — what to improve" task (`db.DEBRIEF_TASK`, added on Rejected and No response) is
   completed. An outcome in the same text is recorded by `/stage`: a next round or offer after the
   debrief (the application is still at the interview's stage), a rejection or withdrawal before it
   (with `--stage`), so the Debrief task it adds is completed.
24. Mock interviews (`/mock`), run in the Applications Assistant box. An open application's panel has a Mock interview
   control in its shortcut row: a stage (`db.INTERVIEW_STAGES`, the application's own when it is at
   an interview, else the first) and a button. It posts `/applications/mock` (`web.start_mock`), which
   stores a user message about the application with a fixed body built from the stage
   (`db.MOCK_START`: "Start a mock interview: coding interview (technical)"; no free text) and pings
   `/ask <app> <message>` like any message (step 15). /ask hands it to the /mock skill, which reads
   the stage's generic question set (`.claude/skills/mock/questions/<stage>.md`: score areas, rubric,
   questions) and the job's brief, description and company file, and replies with one question at a
   time. Each question ends with a fixed footer ("— Mock · <stage> · question N of M …") and the
   last reply with "— Mock · <stage> · finished."; while the most recent agent reply with a "— Mock ·"
   line is a question, a new message on that application is an answer, so the thread itself is the
   mock's state (no table). The control is hidden on a closed application and the endpoint refuses one. After the last answer or "end mock", `prep.cli mock path APP
   --stage S` prints the next free `private_dir/sessions/mock-<app>-<stage>-<date>.md` (creating the
   folder, refusing without `private_dir`); the skill writes only that mock's questions and answers
   (pay replaced) with the scores and gaps there, records it with `session add --kind mock --app APP
   --stage S --file PATH --score AREA=N ... --gap ...` (step 22: session row, gaps, one drill task per
   new gap) and replies in the thread with the scores and the drill task ids. No new page.
25. Notes. `applications.notes` is one Markdown text per application, written by the user and the
   agent: questions to ask, notes from a call, what the agent finds. The panel shows it as the
   first card under Results (`web._notes_card`, always there on a tracked application, so Results
   always shows), rendered by `md.render_markdown(..., breaks=True)` (escaped, line breaks kept) with
   the agent's additions at the end. Edit opens a form (a `<details>`) with the text and the notes'
   stamp, `applications.notes_updated_at` (microseconds; moved by every notes write, never by a stage
   change). Save posts `/applications/notes` (`web.save_notes`); `db.save_notes` replaces the text in
   one conditional UPDATE only while the stamp still matches, and empty text clears it. A stale stamp
   (the agent appended meanwhile) raises `db.NotesChanged`: the reply is a 400 with the card again,
   the current notes above an open editor holding the user's text and the new stamp, so nothing is
   lost and a second Save is a deliberate overwrite. The agent writes with `cli notes APP --append`
   (`db.append_notes`): a "**Agent · 17 Nov**" block added at the end, never a rewrite, pay refused
   (`db.check_no_pay`). `/ask` appends after a request to note something, a pasted recruiter email or
   call notes, a mock or a debrief; `/research` appends "Questions to ask" after company research
   when the notes have none (`cli.has_questions_section`). `thread show` and `research next` print
   the notes as context. `cli update --notes` still replaces them (and moves the stamp). Notes text
   is never logged.
26. Learn cards (data and CLI; the Learn page is in the Dashboard section below). `tracks` hold flashcards in order,
   each with a slug, title, subtitle and a cover colour key (`learn.TRACK_COVERS`); `chapters`
   split a track; `cards` hold the question (`front`), an optional `prompt` (Markdown shown under the
   question, always visible: the code a "read the code" card asks about), key points in Markdown (`back`), tags and
   likely follow-ups (JSON lists of strings), sources (a JSON list of `{kind, title, url, minutes,
   why}`, kind `watch`, `read` or `notes`), an optional `source_gap_id`, and a status: `todo` or
   `done` (`learn.CARD_STATUSES`) with `done_at`. There is no schedule and no due date: progress is
   cards done out of all. Allowed values are checked in `prep/learn.py`. `cli tracks add` adds a
   track; `cli cards add` takes a JSON card or a list of them on stdin (or `--file`), names the
   track by id or slug and the chapter by title (added when new), and adds all or none
   (`learn.add_cards`); a new card is to do. `cli cards done ID` marks it done and `cards undo ID`
   puts it back to do (`learn.set_status`). `cli cards list [--track T] [--todo | --done]` lists
   cards in track, chapter and card order; `cards show ID` prints one, with `[x]` / `[ ]` before each
   source and "N / M done". A source is ticked done on its own (`card_source_done`: card_id,
   source_key, done_at; `learn.set_source_done`, `learn.source_progress`): the key is the source's
   URL, else `kind:title` (`learn.source_key`), so an import that reorders or edits other sources
   keeps the ticks, and only ticks matching a current source are counted. Ticks never change the
   card's own status; `learn.delete_card` removes them with the card. `cli tracks list` gives each
   track's chapters, "done X / Y" and the percent done. `/today` prints "Learn: X done, Y to do"
   with "done/all" per track whenever any card exists. Card content (real answers, stories) stays
   in prep.db; fixtures hold one fictional track with three cards.
27. Learn import and gap cards. `cli cards import FILE [--dry-run]` reads a Markdown file
   (`docs/learn-format.md`: `#` track, `##` chapter, `###` card front, the back, then `tags:`,
   `gap:`, `follow-ups:` and `sources:` lines; text above a card's first `---` line is its prompt;
   ``` fences are card text, so a `---` inside one does not split) with
   `learn.parse_markdown`, then `learn.import_markdown` adds or updates everything in one
   transaction. A track is matched by slug and a card by track and front: a match takes the file's
   prompt, back, chapter, tags, follow-ups, sources and gap and keeps its id, position and status, so a
   second import of the same file changes nothing. Cards missing from the file are kept and counted;
   `cli cards delete ID` removes one (`learn.delete_card`).
   `--dry-run` rolls back and prints the same counts. Real import files are private
   (`private_dir/learn/`, gitignored); `learn.example.md` is a fictional one. Gaps become cards in
   two steps: `cli cards gaps [--gap ID] [--app ID] [--session ID] [--limit N]` is read-only and
   prints JSON drafts for open gaps with no card (track `gaps-to-drill`, chapter the application's
   company, front the gap, an empty back, `source_gap_id`); the agent writes the backs, shows them,
   and saves with `cards add` only after the user says yes. `learn.add_card` refuses a second card
   for the same gap and creates the "Gaps to drill" track with its first card. `/debrief` asks in
   the chat; `/mock` ends with a `— Gap cards · session #S` line and `/ask` saves on the user's
   "save cards" reply.
28. Learn notes (`prep/learnnotes.py`). Private Markdown files under `private_dir/notes/learn/`
   (gitignored and caught by `prep/privacy.py`'s `notes/*`): `card-<id>.md`, one per card,
   `all-prep.md`, one general notebook, and `answer-<id>.md`, the user's own answer to a card (the
   card's Your answer box). A file name comes from a card id, `all` or `answer-<id>` only, never from
   raw input. The stamp is a short SHA-256 of the file's text (a missing file is the empty text).
   `learnnotes.save(key, text, stamp)` writes the text (line ends as `\n`, one final newline,
   atomic: a temp file then a rename) only when the file still has `stamp`, else `NotesChanged`;
   `learnnotes.append` adds a dated "**Agent · 18 Nov**" block at the end (`db.notes_block`), never
   a rewrite, pay refused. Both hold an `flock` on `notes/learn/.lock`, so the check and the write
   are one step. Agents use `cli learn-notes CARD_ID|all [--show | --append [TEXT]]` (stdin when TEXT
   is left out) and read answers with `cli learn-notes answer CARD_ID --show`; answers are never
   appended to (`learnnotes.append` and the CLI refuse). At most 60,000 characters per note. Notes text is never logged.
29. Learn Assistant (Learn 4). The Learn page's Learn Assistant box is a second conversation in `agent_messages`:
   rows with topic `learn` (`db.LEARN`, set by `db.add_learn_message` only, never taken from a form)
   and `card_id`, the card open when the message was sent (NULL: prep in general); a reply copies the
   question's topic and card. Every reader of the Applications conversation leaves them out:
   `db.thread()` and `db.pending_messages()` without `topic="learn"`, `db.message_count()` without
   `learn=True`, `thread pending`, `_thread_line` (today and the SessionStart `queue`, which print
   their own "Learn Assistant: N messages waiting. Run /learn-ask." line instead), and
   `thread show --message` hands a Learn message to `learn-thread show`. POST `/learn/message`
   (`card_id`, `body`) stores a message; `/learn/check` (`card_id`) stores the fixed `db.LEARN_CHECK`,
   refused while `notes/learn/answer-<id>.md` is empty (the page script saves the answer first);
   `/learn/card` (`reply_id`) stores `db.LEARN_CARD` naming a Learn Assistant reply. Each pings
   `prep_assistant_task` in a background thread with `assistant.learn_command`: `dev3 message --task
   seq:<N> [--project …] --subject "Learn question on card <id>" "/learn-ask <card id|general> <msg id>"`,
   ids only. `/learn/add-note` (`reply_id`) appends the essence of that reply to its card's note (all-prep for
   a general one) with `learnnotes.append` on the server: `learnnotes.essence` takes the reply's last
   "Understand:" line that a "Say it:" line follows, up to the blank line or "—" footer after "Say it:",
   kept as written (an optional "Fix:" part, any code snippets, blank lines and indentation)
   (else an older reply's last "Better phrasing:" text as "Understand:" and its "Say it in the
   interview:" line as "Say it:", either alone; else nothing: a 400 with `web.NO_SUMMARY`, which
   `app.js` shows as a `.msg-hint` line under that reply's buttons; never the whole reply), under a heading with the card's question and the day ("Agent"
   for a general one); refused when that text is already there or mentions pay (`learnnotes.append`
   skips only the old "Bonus for the interview:" label at the start of a line, now "Extra for the
   interview:"). `/learn-ask` (`.claude/skills/learn-ask/SKILL.md`) reads a message with `cli learn-thread show
   MSG --claim` (the card's front, back, follow-ups, tags and Learn sources, the track's chapters with
   card ids for quiz mode, the paths of `stories.md`, the facts bank, `notes/interview-coaching.md`
   and the card, answer and all-prep notes, and the latest 30 Learn messages) and answers with
   `learn-thread reply --to MSG`. A card the agent drafts is saved with `cards add` only after the
   user's yes in the box. The box (`web._learn_agent`, `web._learn_thread`) lists the latest 60 Learn
   messages oldest first, each tagged with its card (a link showing the card's question cut to about 50 characters,
   the whole question as its title, from `learn.fronts`; "this card" for the open one; "a removed card",
   not a link, for a deleted one), with
   Want a card for this? and Add to notes under each agent reply; while one waits, `app.js` polls GET
   `/learn/thread?card=ID` (pending, count, HTML) every 4 s for up to 5 minutes, then swaps the
   conversation in and tells the Notes editor to pick up an agent's addition (`learn:refresh`).
   The Learn overview (`/learn` without `card`/`track`) shows the same box under the tracks
   (`web._learn_agent(…, None)`, `.learn-general`): its form has no `card_id`, so the message is stored with
   `card_id` NULL, pinged as `/learn-ask general <msg id>`, redirects to `/learn`, and the poll asks
   `/learn/thread?card=` (no card). While a Learn message waits, the waiting line has Stop: POST
   `/learn/stop` (optional `card_id`, for the page to go back to) calls `db.cancel_waiting_messages(conn,
   learn=True)`, cancelling every waiting Learn message (never an Applications one; Applications Stop
   never touches a Learn one), rows kept.
30. Resend and Cancel, in both boxes. A user message still waiting (status in `db.WAITING`: pending
   or claimed) gets Cancel and, with the box's assistant task set, Resend (`web._wait_actions`).
   `assistant` keeps each message ping's time and outcome in memory (`last_ping`, this dashboard
   process only); Resend renders hidden with `data-resend-due` (epoch ms, `assistant.RESEND_AFTER`,
   30 s after the last ping or the message's creation) and `app.js` reveals it when due, or at once
   when the last ping failed. POST `/applications/resend` and `/learn/resend` (`msg_id`; the Learn one
   with `card_id`) go through `ACTIONS` (loopback, same-origin): `db.reopen_message` puts the same
   row back to pending (a claim dropped), then `assistant.notify_message` / `notify_learn` pings
   synchronously with the same id (no new row); a failed ping is a 400 "Couldn't reach the
   Applications/Learn Assistant (seq N). Is its task running?". POST `/applications/cancel` and
   `/learn/cancel` call `db.cancel_message`: status `cancelled` (allowed in `db.MESSAGE_STATUSES`; no
   schema change), so it leaves `pending_messages`, the claims, `thread pending`, `queue` and
   `today`. `thread show --message MSG` and `learn-thread show MSG` print "was cancelled by the user …
   Do nothing" and stop, and the /ask and /learn-ask skills do nothing. Both refuse a message that is
   answered, cancelled, or in the other box's conversation. A reply to a cancelled message is still
   stored (the status stays cancelled); the box flags the message "Cancelled" (greyed) and the reply
   "after cancel" (`web._msg_flags`). Cancel on a message answered or cancelled since the page was
   drawn is not an error: it answers 200 with "It was no longer waiting" and the fresh conversation.
31. Stop, in the Applications Assistant box. The box's waiting line (`web._thread`, a `div.thread-state`)
   shows while any Applications message waits ("Applications Assistant is working… (N waiting)") or the
   open panel has queued work ("Working on CV fit · Job brief…", `web._panel_work`: its application's
   open `agent_requests` and its job's open `brief_requests`), with a Stop button at its end. POST
   `/applications/stop` (`web.stop_waiting`, through `ACTIONS`) cancels every waiting Applications
   message (`db.cancel_waiting_messages`, which calls `db.cancel_message` for each; Learn messages are
   never touched; one answered meanwhile is skipped) and, for the open panel (`panel_app` or `panel_job`,
   which `app.js` adds to every `data-ajax="thread"` post from `#panel`), its open agent requests
   (`db.cancel_requests`) and job brief (`db.cancel_brief`). Those rows are kept with status
   `cancelled` (`db.REQUEST_STATUSES`; a brief row is re-opened by Write brief / Redo, and an agent
   request queued again is a new row). The script then swaps in the conversation, stops the poll and
   refreshes the list and panel. Nothing is sent to the assistant or its terminal. A claimed message
   never expires on its own (a stale claim only counts as pending again), so one the assistant answered
   past keeps the box waiting until Stop or Cancel: the box flags a waiting message "no reply yet" when
   a newer user message on the same subject (application, job offer, topic; not comparisons) was
   answered, and `thread reply --to MSG` prints "Also waiting, no reply: message N" for each
   (`db.unreplied_before`), with `thread close N` (marks it cancelled, kept) as the way out when the
   reply covered it. Nothing is closed automatically.

## Dashboard

`prep/web.py` uses the standard library's `ThreadingHTTPServer`. `handle()` routes a request
and returns (status, headers, body) without touching a socket, which is what the tests call.
Pages are built from plain strings by small helpers in `web.py` (app shell with a sidebar,
cards, stat tiles, badges for status/tier/score/kind/due date, tables, buttons, empty states);
every database value is HTML-escaped and `applications.pay` is never shown. Agent-thread messages
are shown verbatim (step 15), so they may contain pay the user pasted. Statuses are shown through `db.status_label`
(`db.STATUS_LABELS`): `to_apply` reads "Interested" in badges, charts and CLI output; the stored
value and the `--status` choice stay `to_apply`. Two static files are
served from `prep/static/`: `style.css` (design tokens; one dark grey theme where each page has
its own muted hue: Applications ochre, Metrics sage (dark olive to pale lime);
six more themes (dusty blue, eucalyptus, slate indigo, dusty violet, mauve rose, terracotta) are ready for new pages,
each as ten shades `s1`–`s10` set by a `theme-*` class on `<body>` and used by badges, charts,
buttons and meters; installed Helvetica Neue-style fonts only; responsive down to phone
width) and `app.js`. Charts come from `prep/charts.py`: server-rendered inline SVG (funnel,
columns, stacked columns, share bar, trend) coloured by shade-step CSS classes, so they need no script and no inline
style, and every label is escaped. The site is branded "Hirey" in the page title and sidebar. The
Content-Security-Policy allows only these same-origin files (`script-src 'self'`, no inline
script or style), and nothing is fetched from another host (no CDN, fonts or chart libraries).

`app.js` is an enhancement only. On a bare `/` (no query at all) it opens the view last picked
with the switch and the jobs last ticked in this browser (`localStorage` `apps-view` and `apps-sel`,
read and written in try/catch; one `location.replace`, never a loop); any URL with a query says
what to show and is left alone. A tick toggles in place: the URL changes with `replaceState`, every
☐ for that key and every form's `return` follow, and only the Applications Assistant box's About line is fetched
again (the list keeps its scroll); a fifth tick shows a toast instead. Opening a panel keeps the
ticked jobs (`withSel`) and ticks the opened one only when nothing is ticked. A folded job
description gets a Show all button. It
makes table headers sortable and opens a side panel (an application's or a job offer's) without a
reload: it fetches the `?app=ID` or `?job=ID` page, swaps in its panel and the Applications Assistant box's
"About" line, and pushes the URL; a message being typed stays; Esc and ✕ close it, which also
turns About back to the whole search. A message's tag opens its application or offer panel, and
swaps the list too when the tag points at the other view. getjo ids hold ":" and other
characters, so rows are found with `CSS.escape`d attribute selectors. After any change on the
page (an agent reply, finished agent work, Interested, Mark applied, Skip, Undo skip, a ticked
task, an Undo) it fetches the page again and swaps in the list card, the panel, the conversation,
the subtitle and the nav count (`refreshApps`), keeping the panel's scroll position. A done
shortcut button scrolls to its result card. It sends the Applications Assistant box's message with `fetch`
(Cmd/Ctrl+Enter also sends), swaps in the returned conversation, filters it to the ticked jobs when Selected is chosen, and
while a message is pending, or the open panel's application has open agent requests (research,
CV fit, tailored CV) or its job has a brief being written (the panel's `data-busy`), polls
`GET /applications/thread[?app=ID | ?job=ID]` every 4 seconds for 5 minutes, then every 30 seconds
until it is done or Stop is pressed (the waiting line then says so, keeping Stop; one poll;
opening or closing a panel restarts it); when nothing is pending any more it refreshes
everything ("The agent replied." when the message count changed, else "The agent's work is
in."). It makes the New offers filters live (listeners on the document, since the list is swapped
in again): a select or checkbox reloads with the new query; the search narrows the loaded rows
(company or role) as you type and updates the URL and the forms' `return`, and a search wider
than the one the server rendered (the box's `defaultValue`) reloads after a pause, keeping the
cursor in the box; "/" focuses it. It sends forms marked `data-ajax` with `fetch` and `Accept:
application/json`. For such requests the POST endpoints answer JSON (`{"ok": true, "message": …,
…}` or `{"ok": false, "message": …}` with status 400) instead of a redirect; `html` is
server-rendered and escaped (the Agent thread). Results show as toasts. Interested in an offer's
panel opens the reply's `redirect` (its application panel, Pipeline view) and offers Undo, which
posts `/offers/uninterested` and goes back to the offer; Undo is offered only when the reply says
`created` (never for an application that existed before). Mark applied offers Undo
(`/applications/unapply`) and a ticked panel task offers Undo (`/tasks/reopen`). A Skip link (an
offer panel's, an Interested application panel's) opens the one `<dialog>` on the page, with a
button per skip reason and a comment box; the link's `data-skip-action` and `data-skip-field`
say where it posts and which hidden field its `data-skip` fills (`/offers/skip` and `job_id`, or
`/applications/skip` and `app_id`), and it posts the clicked reason (`FormData(form,
submitter)`), then refreshes; a skipped offer gets Undo (`/offers/unskip`). The Notes card's form
(`data-notes`) posts with `fetch` and swaps in the card from the reply's `html`, on success and on a
stale refusal alike; Cancel closes the editor and restores the saved text. While its editor is open,
a refresh of the panel (an agent reply, finished work) keeps that card as it is, text and stamp. Without JavaScript the
link opens `GET /offers/skip` or `GET /applications/skip`, a plain form (reason select + comment)
that changes nothing until posted, and every form posts normally and redirects.

| Page | Shows | Writes |
|------|-------|--------|
| Metrics `/metrics` | Two tabs (`tab=usage`, the default, or `applications`; `web.metrics_filters`), pill links next to the title; `/dashboard` and `/progress` redirect here. **Usage** (`web.usage_tab`): `usage.sync` first (reads appended transcript lines, refreshes dev3 task details when needed), then `usage.report` for the filters `project` (a project name or `all`; default this repo's project, `usage.home_project`) and `days` (7, 14 or 30): dollars first (`web.money_section`: the API-price estimate note, Today / Last 7 days / Last 30 days tiles, cost per day by role for 30 days, where the money goes), who spends what and top tasks by cost (sorted by dollars, following `days`), then tiles (tokens, turns, sessions; cache-read share; how many assistants are over `context_warn_tokens`), tokens per day (`charts.stacked_columns`, full card width), top tasks by cost, who spends what (`charts.share_bar` and a table), context size per turn (each day's last turn, `charts.trend`), and Where to save (rules in `usage.report`). **Applications** (`web.applications_tab`): tiles (sent, response rate + warning, interviews, offers), applications per week (12 weeks, target line, full width), the four-step funnel (`stats.stage_funnel`) and job boards (`web.boards_table`: `getjo.source_stats` over the read-only getjo db). Never reads pay; no message content. The nav shows the new offers count next to Applications on every page | — |
| New offers `/?view=offers` (the old Job Offers page) | the left column's other view on Applications (the panel, Applications Assistant box and layout below are shared). Open getjo jobs (`getjo.matches`, step 19) at or above `score` (1–10, default 7; unscored jobs never pass) that are neither tracked nor decided (`db.excluded_job_ids`; decided jobs live in the pipeline), newest day first (`first_seen`), then highest score (`web.offer_rows`). Filters in the list header, a GET form: `score`, `loc` (repeatable: london, remote, other — from `getjo.location_tags`: word "london"; words remote/anywhere/work from home/wfh; neither or empty is other; a job can be both) and `q` (company or role, case-insensitive substring). `skipped=1` (Show skipped / Hide skipped) lists the skipped jobs instead, at any score, open or closed, latest skip first, with the reason, the comment and Undo skip; a skipped job getjo has dropped shows from its snapshot. Rows: score, company (a link to the offer panel), role with the chips (closed, may be closed, expires in N days) and location; the whole row opens the panel. `job=<getjo id>` opens the offer panel (a tracked job opens its application's panel instead; an unknown id is ignored): company, role, score, location, seen date, the posting link (http(s) only), the chips; Interested · Skip (a skipped job: "Skipped · reason" and Undo skip); the "Ask about this" tick; and the job brief, or while there is none the description from getjo made readable (step 21; `getjo.job_description`, read-only, escaped; a job getjo no longer has says so). Each row starts with its ☐ tick (step 15). The switch's count and the nav count are the open new offers at the score in effect, ignoring location and search (default 7, as `/today`). A message instead of the list if getjo's database is missing or unreadable; the pipeline still works. `/offers` and `/matches` redirect here with their query mapped (`company`/`role` to `q`, `show=skipped` to `skipped=1`, `show=interested\|applied` to the pipeline's `stage`). `GET /offers/skip?job_id=…` is the no-script skip form | POST `/offers/interested`, `/offers/uninterested`, `/offers/skip`, `/offers/unskip`, `/offers/brief` (Write brief / Redo brief, step 21; also on an application's panel), and `/offers/apply` (also the old `/matches/apply`; no button, kept for old forms). Interested and `/offers/apply` redirect to `/?app=ID` (JSON: `redirect`, `app_id`, `created`), built from the application id only; skip and unskip return to the `return` URL |
| Learn `/learn` | "X done · Y to do" (`learn.counts`) and the next interview on or after today (`db.next_interview`: company, stage, day) as the subtitle; the nav's Learn badge is the cards to do on every page. Without `card`/`track`: a grid of tracks (`learn.list_tracks`): a cover gradient class per `learn.TRACK_COVERS` key, a progress ring (SVG with `pathLength` and `stroke-dasharray` attributes, no inline style), title, subtitle, chapters · cards, "X done / Y", then the Learn Assistant box for general messages (no card; item 29). `track=SLUG\|ID` opens the track's first card to do, else its first card (`learn.first_card`); a track with no cards shows the grid with a notice. `card=ID`: the tracks again as a compact row (`.tracks-row`, the card's track `.is-current` with `aria-current`), then the card view (`web._learn_view`): the left column holds Notes and the Learn Assistant card (`web._learn_agent`, below), a divider (`web._split_handle`, the Applications code in `app.js` with `data-split-key="learn-split"`), and the card: track · chapter, "Card N of M" with ← → links, tags, the question (escaped, line breaks kept), the prompt when the card has one (`.learn-prompt`, through `prep/md.py`, always visible), Show answer (a `<details>`: the back through `prep/md.py` without links, and the follow-ups), the Learn box outside the fold (`web._learn_sources`, `#learn-sources`: "Learn · N / M done", each source with a tick form (`.tick` button, `role="checkbox"`) posting the state wanted, ticked rows `.is-done`: muted and lightly struck through; links with `_link`: http(s) only, new tab; left out when a card has none), and Done / Not done yet forms. ⤢ Expand toggles `.is-agent-wide` (Notes hidden), kept as `learn-agent-expanded` in `localStorage`. Notes (`web._notes_panel`): tabs "this card" (`card=ID`) and "all prep" (`&notes=all`) as links, and the editor (`web._notes_editor`, a form): a line-number gutter (one number per logical line, each as tall as that line's wrapped rows: `app.js` measures them in a hidden mirror with the textarea's width, again on resize), a monospace textarea that soft-wraps to the panel (no sideways scroll) about 13–15 lines tall (`clamp(200px, 34vh, 340px)`, so the Learn Assistant stays in view) (escaped, with one leading newline the parser drops), the note's stamp, and a status line (mode, `notes/learn/…` file, save time, line,col, the vim keys toggle). `app.js` (`autosaving()`, shared with Your answer) autosaves 1.5 s after the last change, on blur and on Cmd/Ctrl-S, posting as JSON, and sends a last save with `sendBeacon` on `pagehide`; a refused save whose current note starts with the text the editor last had (an agent append) is merged (the addition below the user's text) and saved again, any other change pauses autosave and shows the current note (the next explicit save replaces it). Coming back to the tab with nothing unsaved fetches GET `/learn/notes?card=ID[&note=all]` (text and stamp) and picks up an addition. Vim keys: `static/vim.js` is pure key handling (`HireyVim.step(state, key)` → state, handled, save, changed; tested under node by `tests/vim_test.js` when node is installed), `app.js` feeds it keydowns and shows the normal-mode cursor as a one-character selection; off by default, kept as `learn-vim` in `localStorage`. On the card, between the question and Show answer, Your answer (`web._answer_box`): a plain textarea from six rows that `app.js` grows with its text, saved by the same `autosaving()` (note `answer`) with "saved HH:MM"; no vim keys; left out without `private_dir`. Without `private_dir` Notes says so. An unknown or malformed id is a 404 | POST `/learn/notes` (`card_id`, `note` card, all or answer, `stamp`, `text`: `learnnotes.save`; a stale stamp is a 400 whose JSON adds `html` (the editor or answer box with the user's text under the current note), `stamp` and `text` (the current note); plain form posts redirect to the tab), POST `/learn/done` (`card_id`: marks it done; redirects to the next card to do in the track after it, wrapping round (`learn.next_todo`), or to `/learn` with "Every card in that track is done"), `/learn/undo` (`card_id`: back to do; the card stays open), `/learn/source` (`card_id`, `key`, `done` 1 or 0: ticks or unticks one source, `learn.set_source_done`; an unknown key is a 400; plain posts redirect to the card, JSON adds `html`, the Learn box, which `app.js` swaps in (`data-ajax="source"`) keeping focus on the tick). JSON gives `redirect` |
| Research file `/research/file?app=ID&kind=research\|cv_fit\|tailor[&v=N]` | the application's company profile, CV fit file or tailored CV (the latest, or version N), read-only: front matter as a table (any pay/salary key left out) and the body as minimal Markdown (headings, lists, paragraphs, all escaped). Only a path recorded in prep.db that resolves inside `private_dir` is read; anything else (no record, no `private_dir`, a path outside it) is a 404. A missing file shows "File missing" | — |
| Tailored CV PDF `/research/pdf?app=ID&v=N` | the PDF beside tailored version N (`cvpdf.pdf_path`), inline as `application/pdf`. Only for a version recorded in prep.db whose `.pdf` resolves inside `private_dir` and exists; anything else is a 404. Sent with its own headers (`PDF_HEADERS`: `frame-ancestors 'none'`, nosniff, no-store) | — |
| My CV `/?cv=1` | the master CV panel (step 20), opened by **My CV** in the page header; `cv=1` wins over `app`/`job` and a row link clears it. Skills as chips per theme, cv.md rendered, History (only the current version shows; the others are folded newest first in a closed `<details>` "Older versions (N)"; each: vN, time, note, current badge, Open `v=N`, Compare `compare=N` as a line diff against cv.md, Restore) | POST `/cv/restore` (`version`; JSON `{version, previous}`, Undo restores `previous`) |
| Applications `/` (Pipeline view) | a switch at the top of the left column, Pipeline N · New offers M (`view=pipeline\|offers`, links), then the pipeline list with the Applications Assistant box under it (step 15: one conversation, newest at the bottom, kept line breaks, http(s) URLs made into links after escaping, each message tagged with its application, its job offer, its targets (a comparison) or "all jobs", "Applications Assistant is working…" while one is pending with an assistant configured, "Waiting for the agent: run /ask…" without one, the "About:" chips of the ticked jobs with Clear all, and a textarea with Send; each list row starts with its ☐ tick), and the side panel for one application on the right: half and half from 801px wide with a divider between them (`web.SPLIT_HANDLE`, a `role="separator"` that `app.js` drags or moves with the arrow keys, Home/End and Enter or a double-click for half and half; minimum 360px list, 420px panel; kept per browser in `localStorage` as `apps-split` and applied as the `--split-l`/`--split-r` grid fractions; shown only with the script, from 801px and while a panel is open), a full-screen sheet up to 800px. From 801px the page is exactly the viewport's height and does not scroll: the list card takes at most 45% of the left column and scrolls its rows inside while the switch, the stage strip or offer filters and the column headings stay, the Applications Assistant box fills the rest with its messages scrolling and its input always in view, and the panel scrolls inside. Up to 800px everything stacks. The page state is in the query string, so every view renders without the script: `app=ID` opens the panel (an unknown id is ignored; it wins over `job=`), `stage=interested\|applied\|interviewing\|offer\|closed` filters (`web.STAGE_GROUPS`; the four interview stages share Interviewing), `closed=1` lists the closed applications too (Show closed / Hide closed), `employer=product\|client` keeps only that employer kind (All · Product · Client beside Show closed; the stage strip's counts follow it, the Pipeline count does not); unknown values and an old `view=board` are ignored. A stage strip shows the count per group and links to each filter (the active one clears it). The list (`web._app_list`) is one table with a `<tbody>` per stored stage in pipeline order (`web.PIPELINE_STAGES`, then `web.CLOSED_LANE` when shown), each with a header row and its count; within a stage, dated next tasks first (earliest first), then undated, then none, ties by days in stage (`web.list_order`), then regrouped product, unknown, client by `applications.employer_kind` with that order kept inside each group (`web.employer_order`). Columns: Company (a link to the panel, with a small Product or Client label under it; none while unknown), Role, Stage (a short label such as Coding or Sys design, the full one on hover: `web.LIST_STAGE_LABELS`), Next task and Comment (the latest, one line, with "+N" when there are more); an Interested or Applied row whose getjo job has closed or expired shows a "closed" badge under the role (step 19); company, role and next task wrap to two lines at most. No board, no drag and drop, no due, days, fit or tier columns. The panel, top to bottom: the header (company, role, stage badge with interview step dots, "N days in this stage", the job posting link, the "closed" badge when it applies, the Product or Client label beside the company and an Employer select (Product / Client / Unknown) that posts `/applications/employer` on change, the "Ask about this" tick, ✕ to the same view without `app`); the job brief or the readable description with Write brief (step 21; only with a getjo job); the latest comment (step 8) with the number of earlier ones, read-only; the shortcut row (`web._shortcuts`): Research company, CV fit and Tailor CV queue a request (`/applications/research`), show "Researching…" / "Checking CV fit…" / "Tailoring…" while it is open, and once there is a result link to its card ("Research ✓ 2 Nov", "CV fit 4/5", "CV v2 · sent"); Mark applied and Skip while Interested (step 17), afterwards a disabled "Applied · 3 Nov" or "Skipped"; Tasks, each with a ☐ tick that posts `/tasks/done`; Results, always shown: first the Notes card (step 25; Edit, Save, Cancel), then one card per result with Open and Redo (company research with its tier and reason, CV fit with its top gaps, Tailored CV listing the latest version and, when another one was sent, the sent one (newest first, each with its date, Open (`kind=tailor&v=N`) and a "sent" badge on the sent one), the rest folded in a closed `<details>` "Older versions (N)"; Redo there reads "New from cv.md"); and History, the stage changes newest first in a collapsed `<details>`. The panel has no other forms but Notes and Employer: tasks, comments, stage, interview dates, tier and the sent CV are changed by the agent through the Applications Assistant box; the Mock interview control (step 24) only starts a mock there. `/applications` and `/tasks` redirect here with their query | POST `/applications/apply`, `/applications/unapply`, `/applications/skip`, `/applications/research`, `/applications/message`, `/applications/mock` (step 24), `/applications/employer` (`app_id`, `employer_kind`: product, client or empty for unknown), `/applications/notes` (step 25: `app_id`, `stamp`, `notes`; JSON `html` is the Notes card; a stale stamp is a 400 with `html` and the new `stamp`) (redirect to `/?app=ID`; a panel form's `return` for the same application keeps its `stage` and `closed`; `/applications/message` takes `app_id` or `job_id`), `/tasks/done`, `/tasks/reopen`. `/applications/message` answers JSON with the thread's `html`. GET `/applications/skip?app=ID` is the no-script Skip form. GET `/applications/thread[?app=ID]` is JSON for polling: `{ok, pending, status, messages, html}` (404 for an unknown id; `status` is the open panel's application's, null without one; `messages` is the conversation's count, so the script can tell a reply from finished work; `html` is the Applications Assistant box's conversation); `pending` is true while any message waits or (with `app`) an agent request for that application is open |

Without JavaScript, forms post and then redirect (303) back to the page (forms carry a `return`
field so the redirect keeps the view, filters and panel; it is accepted only for `/`, or an old
`/offers`, `/matches`, `/applications` or `/tasks` URL, which is mapped as its GET redirect is,
and its query is re-encoded), which shows a fixed
confirmation (the script turns it into a toast); bad input gets a 400 page with the reason. Local hardening: the server binds to `127.0.0.1` and
`serve()` refuses any other address; requests whose Host header is not `localhost` or
`127.0.0.1` (with any port) are rejected, which blocks DNS rebinding; POSTs with a
cross-origin `Origin` (including `null`) or `Sec-Fetch-Site` are rejected. The
`Referrer-Policy` is `same-origin`, not `no-referrer`: under `no-referrer` browsers send
`Origin: null` on plain form posts, which that check would refuse. Logs hold the
request line and status only, never request bodies or skip comments.

### Definitions (`prep/stats.py`)

- **Submitted**: the application was sent — it has a `date_applied`, or its status (now or in
  its history) is a funnel stage, `rejected` or `no_response`.
- **Funnel**: `applied → recruiter_call → technical → system_design → final → offer`. The
  furthest stage is the latest of these in the status history plus the current status; a
  submitted application with none of them (e.g. only `rejected`) counts as `applied`.
- **Replied**: furthest stage is `recruiter_call` or later, or the application was
  `rejected`. `applied`, `no_response` and a withdrawal straight from `applied` are not
  replies.
- **Reply rate**: replied / submitted. A warning shows when it is below 10% after 25 or more
  submitted applications.
- **Stage conversion**: for each funnel stage, the number of submitted applications that
  reached it or a later stage (cumulative, so a skipped stage still counts) and their share
  of all submitted applications.
- **Weeks** start on Monday. "Last 7 days" is today and the six days before, by
  `date_applied`.
- **Practice**: every `sessions` row (mock and real); each area's 1–5 scores in session order
  (date, then id), areas grouped ignoring case with the first spelling shown. **Open gaps** counts
  every open gap, including those from CV fit checks.

### Token usage (prep/usage.py)

The Metrics page's Usage tab reads Claude Code transcripts (`claude_projects_dir`, every `*.jsonl`
below it) read only. A line is decoded only when it contains the text `"usage"`; of it only the
timestamp, `cwd`, `sessionId`, `message.id`, `message.model`, the four token counts,
`cache_creation.ephemeral_1h_input_tokens` (`cache_write_1h`, NULL when the line has no split),
`usage.speed` and `usage.inference_geo` are copied out,
and message content is never stored, logged or shown. `usage_files` keeps each file's byte offset
after its last complete line, so a page load reads only appended bytes (a file that shrank is read
again). `usage_messages` has one row per `message.id`: a streamed response spans several lines with
the same id, and each count keeps its largest value, so lines read twice never count twice. Days are
local dates.

`usage.place` maps a working directory to (project, 8-char task id): dev3 worktrees
(`~/.dev3.0/worktrees/<folder>/<id>/…` or `<checkout>/.dev3/worktrees/<id>/…`), else the dev3 project
whose checkout holds it, else the folder name. `usage.refresh_tasks` fills `usage_projects` and
`usage_tasks` from `dev3 projects list`, `dev3 tasks list --project P` (only tasks with usage) and
`dev3 task show` (PR merged, open tasks only), with ids only on the command line, when a task is
unknown or the cache is older than 10 minutes, and not again within a minute of a failure. Roles:
`assistant_task` / `prep_assistant_task` / `coordinator_task` seqs in this repo's project (or
`assistant_project`), dev3 type coordinator, other tasks (build tasks), main checkouts. A turn is one
API response; context size is input + cache read + cache write. Where to save: an assistant or
coordinator whose latest turn is over `context_warn_tokens`; an open task whose PR merged (not the assistants or the coordinator, which stay open); a session
whose cache writes are at least 25% of its tokens and 1M tokens.

Dollars: `usage.price` picks a price for one group of responses (same day, place, session, model,
speed, region and long-prompt flag) — a `[prices]` settings prefix first, else the built-in table in
`prep/prices.py` (exact id, or with a date suffix or `[...]` tag; Anthropic's list prices with the
source URL and date), else None ("price unknown", counted as $0 and shown separately, never
guessed). Fast mode scales every price by the fast input price over the standard one; US-only
inference is 1.1x; Claude Haiku 5.5 uses its long-prompt row when input + cache read + cache write
is over 100,000. `usage.cost` splits the dollars into cache read, cache write (5-minute part at
the 5-minute rate, `cache_write_1h` at the 1-hour rate; an unknown split at the 1-hour rate),
output and input. `usage.report` returns these per task and role for the days filter, and under
`money` fixed windows (today, 7 and 30 days), dollars per day per role for 30 days, the split, and
the unpriced models with their tokens. Adding the `cache_write_1h` column empties `usage_files`
once, so transcripts still on disk are read again (rows merge by message id).

### launchd

`launchd/uk.inter-prep.dashboard.plist.example` runs `.venv/bin/python -m prep.web` (the main
checkout's venv, so a Homebrew `python3` update leaves it alone) from the main
checkout with `RunAtLoad` and `KeepAlive`, so the dashboard starts at login and restarts if it
exits. Output goes to the main checkout's `data/dashboard.log` (gitignored). launchd starts
processes without a shell profile, so the server finds `prep_db` and `getjo_db` through
`~/.config/inter_prep/settings.toml`. `http://localhost:8765/` opens Applications; Metrics is at
`/metrics`. Install and uninstall steps are in `docs/usage.md`.

## Configuration

One TOML file holds absolute paths: `getjo_db`, `prep_db`, `private_dir`, plus the optional
keys `dashboard_port` (default 8765), `weekly_target` (applications per week, default 10),
`claude_projects_dir` (default `~/.claude/projects`), `coordinator_task`, `context_warn_tokens`
(default 300000), `[prices."<model>"]` (the Usage tab, below),
`no_response_days` (default 14, from 1 to 365), `research_max_age_days` (default 90),
`stale_days` (default 3, from 1 to 365: a getjo job not listed for longer reads "may be closed"),
`brief_auto_min_score` (default 8, from 1 to 10: new offers scored at least this get a job brief
queued in the background; others when their panel is first opened), and
the standing-assistant keys `assistant_task` (a dev3 task seq), `prep_assistant_task` (the seq of the
Learn Assistant that answers the Learn page's Learn Assistant box), `assistant_project` (a dev3
project id passed as `--project`) and `dev3_bin` (absolute path to the dev3 CLI), and `pdf_python`
(absolute path to a Python with WeasyPrint, for the tailored CV PDF; step 16), and `facts_file`
(absolute path to the facts bank, default `private_dir/cv-facts.md`; step 16). It is looked up as
`$INTERPREP_SETTINGS`, then `config/settings.toml` in the checkout, then
`~/.config/inter_prep/settings.toml`. The per-user file is the normal place, because dev3
worktrees, launchd and the Claude app all find it without a shell environment.
`PREP_GETJO_DB`, `PREP_DB`, `PREP_PRIVATE_DIR`, `PREP_DASHBOARD_PORT`,
`PREP_WEEKLY_TARGET`, `PREP_NO_RESPONSE_DAYS`, `PREP_RESEARCH_MAX_AGE_DAYS`, `PREP_STALE_DAYS`,
`PREP_BRIEF_AUTO_MIN_SCORE`,
`PREP_ASSISTANT_TASK`, `PREP_PREP_ASSISTANT_TASK`, `PREP_ASSISTANT_PROJECT`, `PREP_DEV3_BIN`, `PREP_PDF_PYTHON` and `PREP_FACTS_FILE` override single keys. Set to
an empty string, the four assistant keys are cleared (`config.CLEARABLE`): a demo on example
data runs with `PREP_ASSISTANT_TASK= PREP_PREP_ASSISTANT_TASK=` and never messages the assistants named in the file. An
empty path or number override is ignored (an empty `PREP_DB` never means the default). A relative `prep_db`
is resolved against the repo root of the running checkout, so `prep_db` should be an absolute
path outside any worktree.

## Boundaries

- getjo is never written to and its code is never imported; the `jobs` table shape is the
  only contract.
- Nothing applies to jobs or contacts anyone. The dashboard listens on loopback only and
  makes no outgoing requests; job links open only when clicked. The dashboard never runs the
  agent. Its only outgoing action is the optional local `dev3 message` to the standing
  assistant (`/research <id>`, `/research briefs` or `/ask <app id> <message id>`).
- Pay is stored only in prep.db: `applications.pay`, and agent-thread messages as the user
  pasted them. It never goes into committed files, logs, the dev3 ping or anything the agent
  writes, unless the user asks.
- Research, CV fit results and tailored CVs stay in `private_dir` and prep.db. Logs hold ids and kinds,
  never CV text, gaps, research or thread messages. The ping to the assistant carries ids only.
- Personal files stay in `private_dir` and gitignored paths; `prep/privacy.py` checks that
  none of them is tracked.
