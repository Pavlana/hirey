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
│ prep/getjo.py   matches, locations  │        │ cv.md, notes/, sessions/  │
│ prep/db.py      prep.db             │        │ read only by skills that  │
│   applications, status_history,     │        │ need it (phase 3+)        │
│   application_comments, interviews, │        └───────────────────────────┘
│   tasks, gaps, job_decisions        │
│ prep/stats.py   dashboard numbers   │
│ prep/cli.py     commands            │
│ prep/web.py     dashboard server    │
└───────▲──────────────────▲──────────┘
        │                  │
 Claude Code skills    browser → http://localhost:8765
 /today /applied /task (127.0.0.1 only; kept running by launchd)
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
   A take-home test or a second interview is a comment, not a stage. Every status change
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
   back into a stage creates fresh ones). Interested: Apply (+3). Company research and the CV
   fit check are the agent's job, not to-dos (planned separately); older "Research the
   company" and "Check CV fit / tailor CV" tasks count as Interested auto tasks
   (`db.RETIRED_STAGE_TASKS`) and close when the application moves on. Applied: Chase if no reply (+7), and the open Apply task is
   marked done. Recruiter call: Prepare for the call. Coding interview: Coding practice and
   Review the role's tech stack. System design: System design practice. Company interview:
   Prepare stories, company values, questions to ask. Interview-stage tasks are due the day
   before the interview date (today if that has passed; +2 days without a date). Offer:
   Review the offer (+2). Rejected and No response: Debrief — what to improve (+3).
   Withdrawn adds none. Auto tasks carry their stage in `tasks.stage`; tasks made before that
   column existed got it from their title when the column was added. Hand-made tasks have
   none and are never closed automatically.
7. No response: an application still in Applied whose last stage change (latest
   `status_history` row, else `date_applied`, else `created_at`) is `no_response_days` (14)
   or more days old moves to `no_response` with the comment "auto: no reply in 14 days"
   (`db.auto_no_response`). The Chase task (day 7) is closed and a Debrief task added. It runs
   on every dashboard request and in `prep.cli today`. Moving the card back works as usual.
8. Comments: a stage change can carry a comment (`status_history.comment`); free comments go
   to `application_comments`. `db.timeline` merges both by time for the card's history.
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
   restart. Applied and Interested on the Job Offers page and `/applied --job-id` all go
   through `db.record_application`, so they behave the same way (one application per getjo
   job, never moved back to an earlier stage; an explicit `to_apply` never replaces a later
   status).
11. Job Offers triage writes `job_decisions`: one row per getjo job, `interested`, `applied`
   or `skipped` with a reason (`db.SKIP_REASONS`: location, seniority, role_type, stack, company, pay,
   contract_type, other) and an optional comment. The row copies the job's company, title, location, score,
   url and source at decision time, so the label still reads correctly after getjo drops the
   job. Undo deletes a skip row; an applied row is never undone there. Applications linked to
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

## Dashboard

`prep/web.py` uses the standard library's `ThreadingHTTPServer`. `handle()` routes a request
and returns (status, headers, body) without touching a socket, which is what the tests call.
Pages are built from plain strings by small helpers in `web.py` (app shell with a sidebar,
cards, stat tiles, badges for status/tier/score/kind/due date, tables, buttons, empty states);
every database value is HTML-escaped and the `pay` column is never shown. Statuses are shown through `db.status_label`
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

`app.js` is an enhancement only: it makes table headers sortable, scrolls a highlighted
Applications card into view, shows the interview-date field of Move to… only for interview
stages, makes the Job Offers filters live (selects and checkboxes reload with the new
query; company and role narrow the loaded rows as you type and update the URL), and sends forms marked `data-ajax` with `fetch` and
`Accept: application/json`. For such requests the POST endpoints answer JSON
(`{"ok": true, "message": …, "html": …}` or `{"ok": false, "message": …}` with status 400)
instead of a redirect; `html` is a server-rendered, escaped table row (a newly added general task).
Results show as toasts; marking a task done offers Undo, which posts `/tasks/reopen`, and a
skip offers Undo, which posts `/offers/unskip`. A row's Skipped link opens a `<dialog>` with
one button per skip reason and a comment box; it posts `/offers/skip` with the clicked reason
(`FormData(form, submitter)`). Without JavaScript the link opens `GET /offers/skip`, a plain
form (reason select + comment) that changes nothing until posted, and every form posts
normally and redirects.

| Page | Shows | Writes |
|------|-------|--------|
| Dashboard `/` | tiles (applied in the last 7 days vs `weekly_target`, total, reply rate + warning), a donut of applications by status, a treemap of tiers split by status, a stage-conversion funnel, applications per week (columns with a target line), last 8 weeks. It counts applications only, never tasks: tasks are a to-do list on the Applications page. `/progress` redirects here | — |
| Job Offers `/offers` | getjo jobs filtered by query string: `score` (minimum, 1–10, default 7; applies to new jobs only, and unscored new jobs never pass; decided jobs show at any score), `company` and `role` (case-insensitive substring), `loc` (repeatable: london, remote, other — from `getjo.location_tags`: word "london"; words remote/anywhere/work from home/wfh; neither or empty is other; a job can be both), `show` (new default, interested, applied, skipped, all). New rows have exactly three actions: Applied, Interested, Skipped; interested rows offer Applied only (skipping a tracked job would mean withdrawing it); skipped rows offer Undo skip. Bad values fall back to defaults. Decided jobs that getjo has dropped show from their snapshot. `/matches` redirects here with its query; a message instead if getjo's database is missing or unreadable. `GET /offers/skip?job_id=…` is the no-script skip form | POST `/offers/apply` (also the old `/matches/apply`), `/offers/interested`, `/offers/skip`, `/offers/unskip`. Applied and Interested redirect to `/applications?app=ID` (JSON: `redirect`), built from the application id only |
| Applications `/applications` | a board: one lane per open stage (Interested … Offer) and a collapsed Closed lane (Rejected, No response, Withdrawn). A card (`<details>`, so it opens without the script) shows company, tier, role, days in stage, the next open task with its due date (overdue in red, today in amber) and a comment count; opened, it holds the open tasks (Done, Add), the job link, Move to… (any stage, interview date, comment), a comment box and the history, newest first. `?app=ID` renders that card open and highlighted (and opens the Closed lane if it is there); an unknown id is ignored. Below: General tasks (no application; add, done, sortable) and the 10 most recently done tasks with Reopen. `/tasks` redirects here with its query | POST `/applications/move`, `/applications/comment` (both redirect to `/applications?app=ID`), `/tasks/add`, `/tasks/done`, `/tasks/reopen` (forms on a card carry `return=/applications?app=ID`, so the card stays open) |

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
and `no_response_days` (default 14, from 1 to 365). It is looked up as
`$INTERPREP_SETTINGS`, then `config/settings.toml` in the checkout, then
`~/.config/inter_prep/settings.toml`. The per-user file is the normal place, because dev3
worktrees, launchd and the Claude app all find it without a shell environment.
`PREP_GETJO_DB`, `PREP_DB`, `PREP_PRIVATE_DIR`, `PREP_DASHBOARD_PORT`,
`PREP_WEEKLY_TARGET` and `PREP_NO_RESPONSE_DAYS` override single keys. A relative `prep_db`
is resolved against the repo root of the running checkout, so `prep_db` should be an absolute
path outside any worktree.

## Boundaries

- getjo is never written to and its code is never imported; the `jobs` table shape is the
  only contract.
- Nothing applies to jobs or contacts anyone. The dashboard listens on loopback only and
  makes no outgoing requests; job links open only when clicked.
- Personal files stay in `private_dir` and gitignored paths; `prep/privacy.py` checks that
  none of them is tracked.
