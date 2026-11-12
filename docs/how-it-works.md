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
│ inter_prep                          │        │ private_dir (gitignored)  │
│ prep/getjo.py   matches, open/closed│        │ cv.md, notes/, sessions/, │
│ prep/db.py      prep.db             │        │ companies/, fit/,         │
│   applications, status_history,     │        │ cv-tailored/ (written by  │
│   application_comments, interviews, │        │ /research and /ask),      │
│   tasks, gaps, sessions,            │
│   job_decisions,                    │        │ cv-history/ (cvfile.py);  │
│   agent_requests, company_profiles, │        │ read only by skills that  │
│   cv_fits, tailored_cvs,            │        │ need them                 │
│   tailored_cv_versions,             │        └───────────────────────────┘
│   agent_messages, cv_versions,      │
│   agent_message_targets,            │
│   job_briefs, brief_requests        │
│ prep/stats.py   dashboard numbers   │
│ prep/cli.py     commands            │
│ prep/web.py     dashboard server    │
│ prep/research.py results files      │
│ prep/cvfile.py  master CV versions  │
│ prep/assistant.py  dev3 message     │
└───────▲──────────────────▲──────────┘
        │                  │
 Claude Code skills    browser → http://localhost:8765
 /today /applied /task (127.0.0.1 only; kept running by launchd)
 /stage /research /ask /debrief
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
   an explicit `to_apply` never replaces a later status). It also moves the job's Agent box
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
   its scoring prompt); inter_prep never calls getjo. Comments are included, so they reach
   whatever prompt getjo builds from the file.

13. Agent research and CV fit. The agent, not the dashboard, researches the company and
   checks the CV against the job. The dashboard and the CLI only queue requests in
   `agent_requests` (kind `research` or `cv_fit`, status `pending` → `claimed` → `done`,
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
15. Agent box. One conversation with the standing assistant for the whole search, in a box under
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
   or contacts anyone. Messages are stored and shown verbatim (in the Agent box and by
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
   transaction). `tailor save` prints the copy's word count and warns above
   `research.TAILOR_MAX_WORDS` (1,000, about two pages).
   `private_dir/cv-tailored/_style.md` (`research.style_file`) holds wording agreed in the thread,
   such as a career gap line, reused in every copy; the packet and `thread show` print its path.
   The tailoring rules (length, structure, impact, keyword coverage in the note) are in the
   /research skill. While a tailor request is open, the panel's button reads "Tailoring…" and the
   panel is busy (`data-busy`, and `pending` in `/applications/thread?app=ID`), so the page polls
   until the note arrives in the Agent box. Which version was sent is recorded only when the user says
   so: `cli cv sent APP FILE_NAME` (`db.record_cv_sent`; the name of one of the application's
   versions, with or without `.md`) stores the file name, its version and the time in
   `applications.cv_sent`, `cv_sent_version` and `cv_sent_at`. Applying records nothing and
   copies nothing. A tailored copy saved before versions were kept becomes one version row on
   the next connect (`db._backfill_tailored_versions`; the file keeps its old name). A tailored
   copy blocks Undo Interested. Tailored files are agent-written: never pay.
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
   `--to`, checked first, the reply answers that CV message). `cv restore`, `cv diff`, `cv history`,
   `cv skills` and `cv show` complete it. Skills: the section under the first heading containing
   "skill"; `- **Theme:** a, b; c` lines (or `### Theme`) are groups. Only recorded version names
   resolving inside private_dir are read. Notes and CV text are never logged. With the panel open the
   Agent box's "About my CV" sends `topic=cv` (`agent_messages.topic`, carried by the reply), tagged
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
   sends `/research briefs` to the standing assistant once (a count, no ids;
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
22. Practice sessions, gaps and drills (Phase 3 data layer; no dashboard view yet).
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
   completed. An outcome in the same text (next round, rejection) is recorded by `/stage`.

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
its own muted hue: Applications ochre, Dashboard sage (dark olive to pale lime);
six more themes (dusty blue, eucalyptus, slate indigo, dusty violet, mauve rose, terracotta) are ready for new pages,
each as ten shades `s1`–`s10` set by a `theme-*` class on `<body>` and used by badges, charts,
buttons and meters; installed Helvetica Neue-style fonts only; responsive down to phone
width) and `app.js`. Charts come from `prep/charts.py`: server-rendered inline SVG (donut,
treemap, funnel, columns) coloured by shade-step CSS classes, so they need no script and no inline
style, and every label is escaped. The site is branded "getjo" in the page title and sidebar. The
Content-Security-Policy allows only these same-origin files (`script-src 'self'`, no inline
script or style), and nothing is fetched from another host (no CDN, fonts or chart libraries).

`app.js` is an enhancement only. On a bare `/` (no query at all) it opens the view last picked
with the switch and the jobs last ticked in this browser (`localStorage` `apps-view` and `apps-sel`,
read and written in try/catch; one `location.replace`, never a loop); any URL with a query says
what to show and is left alone. A tick toggles in place: the URL changes with `replaceState`, every
☐ for that key and every form's `return` follow, and only the Agent box's About line is fetched
again (the list keeps its scroll); a fifth tick shows a toast instead. Opening a panel keeps the
ticked jobs (`withSel`) and ticks the opened one only when nothing is ticked. A folded job
description gets a Show all button. It
makes table headers sortable and opens a side panel (an application's or a job offer's) without a
reload: it fetches the `?app=ID` or `?job=ID` page, swaps in its panel and the Agent box's
"About" line, and pushes the URL; a message being typed stays; Esc and ✕ close it, which also
turns About back to the whole search. A message's tag opens its application or offer panel, and
swaps the list too when the tag points at the other view. getjo ids hold ":" and other
characters, so rows are found with `CSS.escape`d attribute selectors. After any change on the
page (an agent reply, finished agent work, Interested, Mark applied, Skip, Undo skip, a ticked
task, an Undo) it fetches the page again and swaps in the list card, the panel, the conversation,
the subtitle and the nav count (`refreshApps`), keeping the panel's scroll position. A done
shortcut button scrolls to its result card. It sends the Agent box's message with `fetch`
(Cmd/Ctrl+Enter also sends), swaps in the returned conversation, filters it to the ticked jobs when Selected is chosen, and
while a message is pending, or the open panel's application has open agent requests (research,
CV fit, tailored CV) or its job has a brief being written (the panel's `data-busy`), polls
`GET /applications/thread[?app=ID | ?job=ID]` every 4 seconds for at most 5 minutes (one poll;
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
submitter)`), then refreshes; a skipped offer gets Undo (`/offers/unskip`). Without JavaScript the
link opens `GET /offers/skip` or `GET /applications/skip`, a plain form (reason select + comment)
that changes nothing until posted, and every form posts normally and redirects.

| Page | Shows | Writes |
|------|-------|--------|
| Dashboard `/dashboard` | tiles (applied in the last 7 days vs `weekly_target`, total, reply rate + warning), a donut of applications by status, a treemap of tiers split by status, a stage-conversion funnel, applications per week (columns with a target line), last 8 weeks. It counts applications only, never tasks: tasks are a to-do list on the Applications page. `/progress` redirects here. The nav shows the number of new offers next to Applications on every page | — |
| New offers `/?view=offers` (the old Job Offers page) | the left column's other view on Applications (the panel, Agent box and layout below are shared). Open getjo jobs (`getjo.matches`, step 19) at or above `score` (1–10, default 7; unscored jobs never pass) that are neither tracked nor decided (`db.excluded_job_ids`; decided jobs live in the pipeline), newest day first (`first_seen`), then highest score (`web.offer_rows`). Filters in the list header, a GET form: `score`, `loc` (repeatable: london, remote, other — from `getjo.location_tags`: word "london"; words remote/anywhere/work from home/wfh; neither or empty is other; a job can be both) and `q` (company or role, case-insensitive substring). `skipped=1` (Show skipped / Hide skipped) lists the skipped jobs instead, at any score, open or closed, latest skip first, with the reason, the comment and Undo skip; a skipped job getjo has dropped shows from its snapshot. Rows: score, company (a link to the offer panel), role with the chips (closed, may be closed, expires in N days) and location; the whole row opens the panel. `job=<getjo id>` opens the offer panel (a tracked job opens its application's panel instead; an unknown id is ignored): company, role, score, location, seen date, the posting link (http(s) only), the chips; Interested · Skip (a skipped job: "Skipped · reason" and Undo skip); the "Ask about this" tick; and the job brief, or while there is none the description from getjo made readable (step 21; `getjo.job_description`, read-only, escaped; a job getjo no longer has says so). Each row starts with its ☐ tick (step 15). The switch's count and the nav count are the open new offers at the score in effect, ignoring location and search (default 7, as `/today`). A message instead of the list if getjo's database is missing or unreadable; the pipeline still works. `/offers` and `/matches` redirect here with their query mapped (`company`/`role` to `q`, `show=skipped` to `skipped=1`, `show=interested\|applied` to the pipeline's `stage`). `GET /offers/skip?job_id=…` is the no-script skip form | POST `/offers/interested`, `/offers/uninterested`, `/offers/skip`, `/offers/unskip`, `/offers/brief` (Write brief / Redo brief, step 21; also on an application's panel), and `/offers/apply` (also the old `/matches/apply`; no button, kept for old forms). Interested and `/offers/apply` redirect to `/?app=ID` (JSON: `redirect`, `app_id`, `created`), built from the application id only; skip and unskip return to the `return` URL |
| Research file `/research/file?app=ID&kind=research\|cv_fit\|tailor[&v=N]` | the application's company profile, CV fit file or tailored CV (the latest, or version N), read-only: front matter as a table (any pay/salary key left out) and the body as minimal Markdown (headings, lists, paragraphs, all escaped). Only a path recorded in prep.db that resolves inside `private_dir` is read; anything else (no record, no `private_dir`, a path outside it) is a 404. A missing file shows "File missing" | — |
| My CV `/?cv=1` | the master CV panel (step 20), opened by **My CV** in the page header; `cv=1` wins over `app`/`job` and a row link clears it. Skills as chips per theme, cv.md rendered, History (vN, time, note, current badge, Open `v=N`, Compare `compare=N` as a line diff against cv.md, Restore) | POST `/cv/restore` (`version`; JSON `{version, previous}`, Undo restores `previous`) |
| Applications `/` (Pipeline view) | a switch at the top of the left column, Pipeline N · New offers M (`view=pipeline\|offers`, links), then the pipeline list with the Agent box under it (step 15: one conversation, newest at the bottom, kept line breaks, http(s) URLs made into links after escaping, each message tagged with its application, its job offer, its targets (a comparison) or "all jobs", "Agent is working…" while one is pending with an assistant configured, "Waiting for the agent: run /ask…" without one, the "About:" chips of the ticked jobs with Clear all, and a textarea with Send; each list row starts with its ☐ tick), and the side panel for one application on the right: halves from 801 to 1599px wide, a third and two thirds from 1600px, a full-screen sheet up to 800px. From 801px the page is exactly the viewport's height and does not scroll: the list card takes at most 45% of the left column and scrolls its rows inside while the switch, the stage strip or offer filters and the column headings stay, the Agent box fills the rest with its messages scrolling and its input always in view, and the panel scrolls inside. Up to 800px everything stacks. The page state is in the query string, so every view renders without the script: `app=ID` opens the panel (an unknown id is ignored; it wins over `job=`), `stage=interested\|applied\|interviewing\|offer\|closed` filters (`web.STAGE_GROUPS`; the four interview stages share Interviewing), `closed=1` lists the closed applications too (Show closed / Hide closed); unknown values and an old `view=board` are ignored. A stage strip shows the count per group and links to each filter (the active one clears it). The list (`web._app_list`) is one table with a `<tbody>` per stored stage in pipeline order (`web.PIPELINE_STAGES`, then `web.CLOSED_LANE` when shown), each with a header row and its count; within a stage, dated next tasks first (earliest first), then undated, then none, ties by days in stage (`web.list_order`). Columns: Company (a link to the panel), Role, Stage (a short label such as Coding or Sys design, the full one on hover: `web.LIST_STAGE_LABELS`), Next task and Comment (the latest, one line, with "+N" when there are more); an Interested or Applied row whose getjo job has closed or expired shows a "closed" badge under the role (step 19); company, role and next task wrap to two lines at most. No board, no drag and drop, no due, days, fit or tier columns. The panel, top to bottom: the header (company, role, stage badge with interview step dots, "N days in this stage", the job posting link, the "closed" badge when it applies, the "Ask about this" tick, ✕ to the same view without `app`); the job brief or the readable description with Write brief (step 21; only with a getjo job); the latest comment (step 8) with the number of earlier ones, read-only; the shortcut row (`web._shortcuts`): Research company, CV fit and Tailor CV queue a request (`/applications/research`), show "Researching…" / "Checking CV fit…" / "Tailoring…" while it is open, and once there is a result link to its card ("Research ✓ 2 Nov", "CV fit 4/5", "CV v2 · sent"); Mark applied and Skip while Interested (step 17), afterwards a disabled "Applied · 3 Nov" or "Skipped"; Tasks, each with a ☐ tick that posts `/tasks/done`; Results, one card per result with Open and Redo (company research with its tier and reason, CV fit with its top gaps, Tailored CV listing every version newest first with its date, Open (`kind=tailor&v=N`) and a "sent" badge on the sent one; Redo there reads "New from cv.md"); and History, the stage changes newest first in a collapsed `<details>`. The panel has no other forms: tasks, comments, stage, interview dates, tier and the sent CV are changed by the agent through the Agent box. `/applications` and `/tasks` redirect here with their query | POST `/applications/apply`, `/applications/unapply`, `/applications/skip`, `/applications/research`, `/applications/message` (redirect to `/?app=ID`; a panel form's `return` for the same application keeps its `stage` and `closed`; `/applications/message` takes `app_id` or `job_id`), `/tasks/done`, `/tasks/reopen`. `/applications/message` answers JSON with the thread's `html`. GET `/applications/skip?app=ID` is the no-script Skip form. GET `/applications/thread[?app=ID]` is JSON for polling: `{ok, pending, status, messages, html}` (404 for an unknown id; `status` is the open panel's application's, null without one; `messages` is the conversation's count, so the script can tell a reply from finished work; `html` is the Agent box's conversation); `pending` is true while any message waits or (with `app`) an agent request for that application is open |

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

### launchd

`launchd/uk.inter-prep.dashboard.plist.example` runs `python -m prep.web` from the main
checkout with `RunAtLoad` and `KeepAlive`, so the dashboard starts at login and restarts if it
exits. Output goes to the main checkout's `data/dashboard.log` (gitignored). launchd starts
processes without a shell profile, so the server finds `prep_db` and `getjo_db` through
`~/.config/inter_prep/settings.toml`. `http://localhost:8765/` opens Applications; the Dashboard is at
`/dashboard`. Install and uninstall steps are in the README.

## Configuration

One TOML file holds absolute paths: `getjo_db`, `prep_db`, `private_dir`, plus the optional
keys `dashboard_port` (default 8765), `weekly_target` (applications per week, default 10)
`no_response_days` (default 14, from 1 to 365), `research_max_age_days` (default 90),
`stale_days` (default 3, from 1 to 365: a getjo job not listed for longer reads "may be closed"),
`brief_auto_min_score` (default 8, from 1 to 10: new offers scored at least this get a job brief
queued in the background; others when their panel is first opened), and
the standing-assistant keys `assistant_task` (a dev3 task seq), `assistant_project` (a dev3
project id passed as `--project`) and `dev3_bin` (absolute path to the dev3 CLI). It is looked up as
`$INTERPREP_SETTINGS`, then `config/settings.toml` in the checkout, then
`~/.config/inter_prep/settings.toml`. The per-user file is the normal place, because dev3
worktrees, launchd and the Claude app all find it without a shell environment.
`PREP_GETJO_DB`, `PREP_DB`, `PREP_PRIVATE_DIR`, `PREP_DASHBOARD_PORT`,
`PREP_WEEKLY_TARGET`, `PREP_NO_RESPONSE_DAYS`, `PREP_RESEARCH_MAX_AGE_DAYS`, `PREP_STALE_DAYS`,
`PREP_BRIEF_AUTO_MIN_SCORE`,
`PREP_ASSISTANT_TASK`, `PREP_ASSISTANT_PROJECT` and `PREP_DEV3_BIN` override single keys. Set to
an empty string, the three assistant keys are cleared (`config.CLEARABLE`): a demo on example
data runs with `PREP_ASSISTANT_TASK=` and never messages the assistant named in the file. An
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
