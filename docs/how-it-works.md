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
│ prep/getjo.py   matches, locations  │        │ cv.md, notes/, sessions/, │
│ prep/db.py      prep.db             │        │ companies/, fit/,         │
│   applications, status_history,     │        │ cv-tailored/ (written by  │
│   application_comments, interviews, │        │ /research and /ask); read │
│   tasks, gaps, job_decisions,       │        │ only by skills that need  │
│   agent_requests, company_profiles, │        │ them                      │
│   cv_fits, tailored_cvs,            │        └───────────────────────────┘
│   application_messages              │
│ prep/stats.py   dashboard numbers   │
│ prep/cli.py     commands            │
│ prep/web.py     dashboard server    │
│ prep/research.py results files      │
│ prep/assistant.py  dev3 message     │
└───────▲──────────────────▲──────────┘
        │                  │
 Claude Code skills    browser → http://localhost:8765
 /today /applied /task (127.0.0.1 only; kept running by launchd)
 /stage /research /ask
```

## Data flow

1. getjo stores each job once in its `jobs` table with a score from 1 to 10.
2. `/today` runs `python -m prep.cli today`. It runs the no-response check (step 7), lists
   the applications it moved in the last 7 days (from their `auto:` history rows, so a move
   already made by the dashboard still shows) and the open tasks due today or earlier with
   their application, then opens getjo's database read-only and lists jobs scored at or above 7 whose
   id is neither in `applications.getjo_job_id` nor in `job_decisions` (`db.excluded_job_ids`).
3. `/applied` records an application. Given a getjo id, URL, or company and role, it looks the
   job up in getjo and stores the link (`getjo_job_id`), so the job drops out of the match list.
   The status is `applied` with `date_applied` today (unless `--date`), or `to_apply` (shown as
   "Interested") with `--status to_apply`. With a linked job it also writes an `applied` or
   `interested` row to `job_decisions`.
4. `/task` adds, lists and completes tasks. Tasks can point at an application or a gap; the
   dashboard shows an application's tasks on its card and the rest under General tasks.
5. Stages: Interested (`to_apply`) → Applied → Recruiter call → Coding interview
   (`technical`) → System design → Company interview (`final`) → Offer; closed: Rejected,
   No response, Withdrawn (`db.STATUS_LABELS` holds the labels; stored values are unchanged).
   A take-home test or a second interview is a comment, not a stage. `/stage` turns a pasted
   recruiter email, call notes or a short message into these commands: it finds the application
   (`cli apps`), runs `cli update ID --status S [--interview-date D] --comment "summary"` (a short
   summary, never the whole email or pay), and adds extra to-dos with `cli task add --app ID`.
   `cli update` to the stage the application is already in moves nothing but still saves the
   comment (as a free comment) and, for an interview stage, the interview date
   (`db.set_interview_date`); when that date differs from the stage's latest one, the stage's
   open auto tasks move to the day before it (today if it has passed), so a rescheduled
   interview keeps its prep on time. Hand-made tasks keep their dates. Every status change
   goes through `db.move_application` (Move to… on the board, `cli update --status`,
   `record_application` and the no-response check): it writes a `status_history` row with
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
   on every dashboard request and in `prep.cli today`. Moving the card back works as usual.
8. Comments: a stage change can carry a comment (`status_history.comment`); free comments go
   to `application_comments`. `db.timeline` merges both by time for the side panel's history.
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
   opens a fresh connection (and a fresh read-only getjo connection for the Job Offers page),
   so the skills and the browser see each other's writes on the next request, without a
   restart. Interested on the Job Offers page, `/applied --job-id` and the old `/offers/apply`
   endpoint (no button any more) all go through `db.record_application`, so they behave the same way (one application per getjo
   job, never moved back to an earlier stage; an explicit `to_apply` never replaces a later
   status).
11. Job Offers triage writes `job_decisions`: one row per getjo job, `interested`, `applied`
   or `skipped` with a reason (`db.SKIP_REASONS`: location, seniority, role_type, stack, company, pay,
   contract_type, other) and an optional comment. The row copies the job's company, title, location, score,
   url and source at decision time, so the label still reads correctly after getjo drops the
   job. Undo deletes a skip row; an applied row is never undone there. Undo after Interested
   (`db.undo_interested`) deletes the decision and the `to_apply` application with its queued
   requests, in one transaction, only while nothing else has happened to it (one history row;
   no tasks, comments, interviews, gaps, CV fit or tier; no request claimed by the agent), so
   the job is new again; otherwise it is refused and the card's Move to… withdraws it. Applications linked to
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
   (`db.stage_tasks`, so the Job Offers button, `/applied --status to_apply` and Move to
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
   case and spacing). The profile suggests a tier from the rubric in the skill (A: strong fit at
   a recognised or good employer; B: decent fit or less known; C: weak fit or risky); the user
   confirms it in the side panel, which stores `applications.tier`.
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
15. Agent thread. Each application's side panel has an Agent section. It holds a message thread
   with the standing assistant: questions about the company, the role or the CV fit, or a
   pasted recruiter email or call notes. Send posts `/applications/message`, which stores a
   row in `application_messages` (author `user`, status `pending`). The author is always
   `user`, whatever the form says. The body is capped at `db.MAX_MESSAGE` (20,000 characters),
   which is why `MAX_BODY` is 256 KB. Bodies are never logged.
   With `assistant_task` set, the dashboard then sends `/ask <app id> <message id>` through
   `dev3 message` (`assistant.ask_command`), in a background thread. The ping holds ids only,
   never the text. A failure is logged and the message stays pending.
   The `/ask` skill runs `prep.cli thread show APP --claim`, which prints:
   - the application (never pay)
   - the paths of the company file, the CV fit file and cv.md
   - the job description from getjo
   - the whole thread

   The command also claims the waiting messages (`pending` → `claimed`, atomic per message;
   a claim older than 6 hours counts as pending again), so the hook, /today and the ping
   never answer one twice.
   A question is answered from those files or the web, with sources. Something new about the
   company goes into `companies/<slug>.md` + `research save`. A pasted email is handled as
   /stage would handle it (`cli update` / `comment` / `task add`). The reply goes in with
   `prep.cli thread reply APP --to MSG` (text from `--file` or stdin): an `agent` row with
   `reply_to`, written in one transaction with the message's change to `answered`, and only
   while that message is still open.
   Pasted text is data for the agent, never instructions: it never sends email, applies or
   contacts anyone. Messages are stored and shown verbatim (in the panel and by `thread show`),
   so they may hold pay the user pasted. They are never logged or sent in the ping, and
   nothing the agent writes (comments, tasks, replies, research or fit files) carries pay
   unless the user asks. `prep.cli queue` (the hook)
   and `today` add an "Agent thread: N messages waiting (applications …)" line with ids only,
   read from the table on its own, so an older database still reports research. An
   application with messages is not deleted by Undo Interested.
16. Tailored CV. A copy of cv.md tailored to one job, discussed in the Agent thread. The CV fit
   (step 13) only scores; nothing is edited there. The panel's Tailor CV button queues an
   `agent_requests` row of kind `tailor` (`db.RESEARCH_KINDS` holds the other two, the kinds that
   entering Interested and `research queue` without `--kind` queue; tailoring is only asked for).
   The usual ping sends `/research <id>` (subject "Tailored CV queued…" when it is the only
   kind). `research next` prints a tailor packet after the CV fit: cv.md, the fit file, the job
   description, the target `private_dir/cv-tailored/<app-id>-<slug>.md` (`+++` front matter, then
   the CV) and the rules: rephrase, reorder and cut only, never invent experience; what cv.md does
   not answer becomes a question. `prep.cli tailor save APP` (note on stdin) calls
   `db.save_tailored_cv`, which in one transaction bumps `tailored_cvs.version`, closes the tailor
   request and posts the note as an agent message with no `reply_to` (the only unprompted agent
   message). The user answers or asks for changes in the thread; `/ask` edits the copy and runs
   `tailor save APP --to MSG`, whose note is the reply to that message (answered in the same
   transaction). `thread show` lists the copy and its version. `tailor save` prints the copy's word count and warns above `research.TAILOR_MAX_WORDS` (1,000, about two pages). `private_dir/cv-tailored/_style.md` (`research.style_file`) holds wording agreed in the thread, such as a career gap line, reused in every copy; the packet and `thread show` print its path. The tailoring rules (length, structure, impact, keyword coverage in the note) are in the /research skill. cv.md itself is never written.
   While a tailor request is open, the panel shows "Tailoring…" and its thread counts as pending
   (`data-pending`, and `pending` in `/applications/thread`), so the page polls until the note
   arrives. The first move into a sent stage (the one that sets `date_applied`: panel Apply, a
   drag, `cli update --status`, or `/applied` on a tracked job) calls `db.record_cv_sent`: it
   copies the current file to `<app-id>-<slug>.sent-v<N>.md` and stores that path, N and the time
   in `applications.cv_sent`, `cv_sent_version` and `cv_sent_at`, so later revisions never change
   the record. It runs after the stage's tasks; without a copy, with its file missing or when the copy fails (logged with ids only) nothing is recorded and the move stands. A tailored copy
   blocks Undo Interested. Tailored files are agent-written: never pay.

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
its own muted hue: Dashboard sage (dark olive to pale lime), Job Offers dusty blue, Applications ochre;
five more themes (eucalyptus, slate indigo, dusty violet, mauve rose, terracotta) are ready for new pages,
each as ten shades `s1`–`s10` set by a `theme-*` class on `<body>` and used by badges, charts,
buttons and meters; installed Helvetica Neue-style fonts only; responsive down to phone
width) and `app.js`. Charts come from `prep/charts.py`: server-rendered inline SVG (donut,
treemap, funnel, columns) coloured by shade-step CSS classes, so they need no script and no inline
style, and every label is escaped. The site is branded "getjo" in the page title and sidebar. The
Content-Security-Policy allows only these same-origin files (`script-src 'self'`, no inline
script or style), and nothing is fetched from another host (no CDN, fonts or chart libraries).

`app.js` is an enhancement only: it makes table headers sortable, opens the Applications side
panel without a reload (it fetches the `?app=ID` page, swaps in only its panel and pushes the
URL; Esc and ✕ close it), remembers the Board / List choice in `localStorage` (a URL without
`view` gets the remembered one; storage errors are ignored), adds drag and drop between board
columns with native HTML5 drag events (Interested, Applied and Offer move at once; Interviewing
opens a `<dialog>` for the step, default the next one, and the interview date; Closed one for
the outcome; every drop posts `/applications/move` with `Accept: application/json`, then the
page reloads with the Moved toast; a drop on the card's own column does nothing), shows the
interview-date field of Move to… only for interview stages, sends the panel's Agent message with `fetch` (Cmd/Ctrl+Enter
also sends) and swaps in the returned thread, and while a message is pending polls
`GET /applications/thread?app=ID` every 4 seconds for at most 5 minutes (one poll, tied to the open
panel; closing or switching panels stops it). When the reply is in it re-fetches the panel, or reloads the
page when the reply's `status` differs from the panel's `data-status` (a pasted email moved the stage), makes the Job Offers filters live (selects and checkboxes reload with the new
query; company and role narrow the loaded rows as you type and update the URL), and sends forms marked `data-ajax` with `fetch` and
`Accept: application/json`. For such requests the POST endpoints answer JSON
(`{"ok": true, "message": …, "html": …}` or `{"ok": false, "message": …}` with status 400)
instead of a redirect; `html` is a server-rendered, escaped table row (a newly added general task).
Results show as toasts; marking a task done offers Undo, which posts `/tasks/reopen`, and a
skip offers Undo, which posts `/offers/unskip`. Interested on Job Offers swaps the row's
actions cell for the reply's `html` ("Interested · research queued" and an Open card link) and
offers Undo, which posts `/offers/uninterested` and puts the old cell back; Undo is offered
only when the reply says `created` (never for an application that existed before). A row's Skipped link opens a `<dialog>` with
one button per skip reason and a comment box; it posts `/offers/skip` with the clicked reason
(`FormData(form, submitter)`). Without JavaScript the link opens `GET /offers/skip`, a plain
form (reason select + comment) that changes nothing until posted, and every form posts
normally and redirects.

| Page | Shows | Writes |
|------|-------|--------|
| Dashboard `/` | tiles (applied in the last 7 days vs `weekly_target`, total, reply rate + warning), a donut of applications by status, a treemap of tiers split by status, a stage-conversion funnel, applications per week (columns with a target line), last 8 weeks. It counts applications only, never tasks: tasks are a to-do list on the Applications page. `/progress` redirects here | — |
| Job Offers `/offers` | getjo jobs filtered by query string: `score` (minimum, 1–10, default 7; applies to new jobs only, and unscored new jobs never pass; decided jobs show at any score; a tracked job whose application has a CV fit shows a `fit N/5` badge next to the company), `company` and `role` (case-insensitive substring), `loc` (repeatable: london, remote, other — from `getjo.location_tags`: word "london"; words remote/anywhere/work from home/wfh; neither or empty is other; a job can be both), `show` (new default, interested, applied, skipped, all). New rows have two actions: Interested and Skipped (applying happens on the board, after the agent's research); interested and applied rows link to their card (Open card; skipping a tracked job would mean withdrawing it); skipped rows offer Undo skip. Bad values fall back to defaults. Decided jobs that getjo has dropped show from their snapshot. `/matches` redirects here with its query; a message instead if getjo's database is missing or unreadable. `GET /offers/skip?job_id=…` is the no-script skip form | POST `/offers/interested`, `/offers/uninterested`, `/offers/skip`, `/offers/unskip`, and `/offers/apply` (also the old `/matches/apply`; no button, kept for old forms). Interested stays on Job Offers (plain post: back with the filters and a banner; JSON: `html`, `app_id`, `created`); `/offers/apply` redirects to `/applications?app=ID` (JSON: `redirect`), built from the application id only |
| Research file `/research/file?app=ID&kind=research\|cv_fit\|tailor\|cv_sent` | the application's company profile, CV fit file, tailored CV or the frozen copy that was sent, read-only: front matter as a table (any pay/salary key left out) and the body as minimal Markdown (headings, lists, paragraphs, all escaped). Only a path recorded in prep.db that resolves inside `private_dir` is read; anything else (no record, no `private_dir`, a path outside it) is a 404. A missing file shows "File missing" | — |
| Applications `/applications` | a pipeline card and General tasks. The page state is in the query string, so every view renders without the script: `app=ID` opens the side panel, `view=board\|list`, `stage=interested\|applied\|interviewing\|offer\|closed` filters (unknown values are ignored). A stage strip shows the count per column and links to each filter (the active one clears it), next to Board / List links. The board has five columns (`web.BOARD_COLUMNS`): Interested (`to_apply`), Applied, Interviewing (`db.INTERVIEW_STAGES`), Offer, and Closed (Rejected, No response, Withdrawn), a narrow `<details>` strip with counts that expands (open under `?stage=closed`; an open panel never expands it, which would push the board sideways at laptop width). Stored statuses are unchanged. A card is a link to its panel with no forms: company and role (they wrap, never cut), on Interviewing cards one dot per interview step (done, current, ahead, in `INTERVIEW_STAGES` order) and the step label (Recruiter call, Coding, System design, Company interview), the next open task with its due date, tier, CV fit score, "… pending" chips (Interested cards instead show "Researching…" while either request is open, or "Ready: fit N/5 · tier X" once both results exist, plus "Tailoring…" / "CV tailored vN" / "CV sent vN" (`web.tailored_state`); the tier is the confirmed one, else the suggested one: `web.research_state`), comment count and days in stage (Applied only: amber from day 7, red from `no_response_days` − 2). The list view is one table of open applications (closed ones only under the Closed filter): company, role, stage, next task, due, days, fit, tier; dated next tasks first (earliest first), then undated, then none, ties by days in stage. While the panel is open, a CSS rule hides the list's Days, Fit and Tier columns (they are in the panel), so it also holds when the script opens or closes the panel. The side panel (`?app=ID`; an unknown id is ignored) starts, for an Interested application, with an action row: the research state, Company research, CV fit, Tailor CV (as below) and Apply, a `<details>` confirm ("Mark as applied today?") that posts `/applications/apply` (`db.move_application` to applied, only from `to_apply`, as Next stage: Applied, which the row replaces). Then the open tasks (Done, Add), Next stage (the next of Interested → Applied → Recruiter call → Coding → System design → Company interview → Offer, with an interview date when that is an interview step; none for Offer and closed), Move to… (any stage, interview date, comment), the company research, CV fit and tailored CV section (summary, Open, Research / CV fit / Tailor CV / "… pending" / "Tailoring…" / Redo; the tailored CV reads "Tailored CV ready · vN · time", then "Sent: tailored CV vN · time" with Open sent (step 16); for Interested the buttons are in the action row) with the tier select, the Agent thread (step 15: messages oldest first, the newest at the
bottom, kept line breaks, http(s) URLs made into links after escaping, "Agent is working…" while
one is pending with an assistant configured, "Waiting for the agent: run /ask…" without one, and a
textarea with Send), a comment box and the history, newest first. Its ✕ is a link to the same view without `app`; on screens up to 800px it is a full-screen sheet. From 801 to 1599px
wide, with the panel open, the panel is 320px and the board's columns and gaps are narrower, so
the four columns and the Closed strip fit without sideways scrolling, with room to spare at
1440px even with a classic scrollbar; wider screens fit at the normal sizes. A hidden `<dialog>` holds the drop choices for the script. `/tasks` redirects here with its query | POST `/applications/move`, `/applications/apply`, `/applications/comment`, `/applications/research`, `/applications/tier`, `/applications/message` (redirect to `/applications?app=ID`; a panel form's `return` for the same application keeps its `view` and `stage`), `/tasks/add`, `/tasks/done`, `/tasks/reopen` (panel forms carry the panel URL as `return`). `/applications/message` answers JSON with the thread's `html`. GET `/applications/thread?app=ID` is JSON for polling: `{ok, pending, status, html}` (404 for an unknown id) |

Without JavaScript, forms post and then redirect (303) back to the page (Job Offers forms carry
a `return` field so the redirect keeps the filters; it is accepted only for the same path and
its query is re-encoded), which shows a fixed
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
`~/.config/inter_prep/settings.toml`. Install and uninstall steps are in the README.

## Configuration

One TOML file holds absolute paths: `getjo_db`, `prep_db`, `private_dir`, plus the optional
keys `dashboard_port` (default 8765), `weekly_target` (applications per week, default 10)
`no_response_days` (default 14, from 1 to 365), `research_max_age_days` (default 90), and
the standing-assistant keys `assistant_task` (a dev3 task seq), `assistant_project` (a dev3
project id passed as `--project`) and `dev3_bin` (absolute path to the dev3 CLI). It is looked up as
`$INTERPREP_SETTINGS`, then `config/settings.toml` in the checkout, then
`~/.config/inter_prep/settings.toml`. The per-user file is the normal place, because dev3
worktrees, launchd and the Claude app all find it without a shell environment.
`PREP_GETJO_DB`, `PREP_DB`, `PREP_PRIVATE_DIR`, `PREP_DASHBOARD_PORT`,
`PREP_WEEKLY_TARGET`, `PREP_NO_RESPONSE_DAYS`, `PREP_RESEARCH_MAX_AGE_DAYS`,
`PREP_ASSISTANT_TASK`, `PREP_ASSISTANT_PROJECT` and `PREP_DEV3_BIN` override single keys. A relative `prep_db`
is resolved against the repo root of the running checkout, so `prep_db` should be an absolute
path outside any worktree.

## Boundaries

- getjo is never written to and its code is never imported; the `jobs` table shape is the
  only contract.
- Nothing applies to jobs or contacts anyone. The dashboard listens on loopback only and
  makes no outgoing requests; job links open only when clicked. The dashboard never runs the
  agent. Its only outgoing action is the optional local `dev3 message` to the standing
  assistant (`/research <id>` or `/ask <app id> <message id>`).
- Pay is stored only in prep.db: `applications.pay`, and agent-thread messages as the user
  pasted them. It never goes into committed files, logs, the dev3 ping or anything the agent
  writes, unless the user asks.
- Research, CV fit results and tailored CVs stay in `private_dir` and prep.db. Logs hold ids and kinds,
  never CV text, gaps, research or thread messages. The ping to the assistant carries ids only.
- Personal files stay in `private_dir` and gitignored paths; `prep/privacy.py` checks that
  none of them is tracked.
