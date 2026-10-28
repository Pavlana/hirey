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
│ prep/getjo.py   matches ≥ threshold │        │ cv.md, notes/, sessions/  │
│ prep/db.py      prep.db             │        │ read only by skills that  │
│   applications, status_history,     │        │ need it (phase 3+)        │
│   interviews, tasks, gaps           │        └───────────────────────────┘
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
2. `/today` runs `python -m prep.cli today`. It reads `prep.db` for overdue next actions and
   due tasks, then opens getjo's database read-only and lists jobs scored at or above 7 whose
   id is not in `applications.getjo_job_id`.
3. `/applied` records an application. Given a getjo id, URL, or company and role, it looks the
   job up in getjo and stores the link (`getjo_job_id`), so the job drops out of the match list.
4. `/task` adds, lists and completes tasks. Tasks can point at an application or a gap.
5. Every new application and every status change also writes a row to `status_history`
   (application, status, time). Applications created before that table existed have no rows;
   their current status is the only known point.
6. The dashboard (`python -m prep.web`) reads and writes the same `prep.db`. Each request
   opens a fresh connection (and a fresh read-only getjo connection for the Matches page),
   so the skills and the browser see each other's writes on the next request, without a
   restart. "Mark applied" on the Matches page and `/applied --job-id` both go through
   `db.record_application`, so they behave the same way (one application per getjo job,
   never moved back to an earlier stage).

## Dashboard

`prep/web.py` uses the standard library's `ThreadingHTTPServer`. `handle()` routes a request
and returns (status, headers, body) without touching a socket, which is what the tests call.
Pages are built from plain strings; every database value is HTML-escaped and the `pay` column
is never shown. One stylesheet is served from `prep/static/`; there is no JavaScript and no
external request (system fonts, no CDN).

| Page | Shows | Writes |
|------|-------|--------|
| Pipeline `/` | applied in the last 7 days vs `weekly_target`, total, reply rate (+ warning), overdue next actions, counts by status and tier | — |
| Matches `/matches` | getjo jobs ≥7 not yet tracked; a message instead if getjo's database is missing or unreadable | POST `/matches/apply` |
| Tasks `/tasks` | open tasks (overdue in red), add form, 10 most recently done | POST `/tasks/add`, `/tasks/done` |
| Progress `/progress` | applications per week, stage conversion, tasks done per week (last 8 weeks) | — |

Forms post and then redirect (303) back to the page, which shows a fixed confirmation; bad
input gets a 400 page with the reason. Local hardening: the server binds to `127.0.0.1` and
`serve()` refuses any other address; requests whose Host header is not `localhost` or
`127.0.0.1` (with any port) are rejected, which blocks DNS rebinding; POSTs with a
cross-origin `Origin` (including `null`) or `Sec-Fetch-Site` are rejected. Logs hold the
request line and status only, never request bodies.

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
  `date_applied`. Tasks done count by the UTC day of `done_at`.

### launchd

`launchd/uk.inter-prep.dashboard.plist.example` runs `python -m prep.web` from the main
checkout with `RunAtLoad` and `KeepAlive`, so the dashboard starts at login and restarts if it
exits. Output goes to the main checkout's `data/dashboard.log` (gitignored). launchd starts
processes without a shell profile, so the server finds `prep_db` and `getjo_db` through
`~/.config/inter_prep/settings.toml`. Install and uninstall steps are in the README.

## Configuration

One TOML file holds absolute paths: `getjo_db`, `prep_db`, `private_dir`, plus the optional
dashboard keys `dashboard_port` (default 8765) and `weekly_target` (applications per week,
default 10). It is looked up as
`$INTERPREP_SETTINGS`, then `config/settings.toml` in the checkout, then
`~/.config/inter_prep/settings.toml`. The per-user file is the normal place, because dev3
worktrees, launchd and the Claude app all find it without a shell environment.
`PREP_GETJO_DB`, `PREP_DB`, `PREP_PRIVATE_DIR`, `PREP_DASHBOARD_PORT` and
`PREP_WEEKLY_TARGET` override single keys. A relative `prep_db`
is resolved against the repo root of the running checkout, so `prep_db` should be an absolute
path outside any worktree.

## Boundaries

- getjo is never written to and its code is never imported; the `jobs` table shape is the
  only contract.
- Nothing applies to jobs or contacts anyone. The dashboard listens on loopback only and
  makes no outgoing requests; job links open only when clicked.
- Personal files stay in `private_dir` and gitignored paths; `prep/privacy.py` checks that
  none of them is tracked.
