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
Pages are built from plain strings by small helpers in `web.py` (app shell with a sidebar,
cards, stat tiles, badges for status/tier/score/kind/due date, tables, buttons, empty states);
every database value is HTML-escaped and the `pay` column is never shown. Two static files are
served from `prep/static/`: `style.css` (design tokens; one dark grey theme where each page has
its own muted hue: Dashboard sage (dark olive to pale lime), Matches dusty blue, Tasks terracotta;
five more themes (eucalyptus, slate indigo, dusty violet, mauve rose, ochre) are ready for new pages,
each as ten shades `s1`–`s10` set by a `theme-*` class on `<body>` and used by badges, charts,
buttons and meters; installed Helvetica Neue-style fonts only; responsive down to phone
width) and `app.js`. Charts come from `prep/charts.py`: server-rendered inline SVG (donut,
treemap, funnel, columns, line) coloured by shade-step CSS classes, so they need no script and no inline
style, and every label is escaped. The site is branded "getjo" in the page title and sidebar. The
Content-Security-Policy allows only these same-origin files (`script-src 'self'`, no inline
script or style), and nothing is fetched from another host (no CDN, fonts or chart libraries).

`app.js` is an enhancement only: it makes table headers sortable, shows a filter box on the
Matches and open-tasks tables, and sends forms marked `data-ajax` with `fetch` and
`Accept: application/json`. For such requests the POST endpoints answer JSON
(`{"ok": true, "message": …, "html": …}` or `{"ok": false, "message": …}` with status 400)
instead of a redirect; `html` is a server-rendered, escaped table row (a newly added task).
Results show as toasts; marking a task done offers Undo, which posts `/tasks/reopen`. Without
JavaScript every form posts normally and redirects.

| Page | Shows | Writes |
|------|-------|--------|
| Dashboard `/` | tiles (applied in the last 7 days vs `weekly_target`, total, reply rate + warning), overdue next actions, a donut of applications by status, a treemap of tiers split by status, a stage-conversion funnel, applications per week (columns with a target line) and tasks done per week (line), last 8 weeks. `/progress` redirects here | — |
| Matches `/matches` | getjo jobs ≥7 not yet tracked (sortable, filterable); a message instead if getjo's database is missing or unreadable | POST `/matches/apply` |
| Tasks `/tasks` | add form, open tasks (overdue in red, due today in amber; filterable), 10 most recently done with Reopen | POST `/tasks/add`, `/tasks/done`, `/tasks/reopen` |

Without JavaScript, forms post and then redirect (303) back to the page, which shows a fixed
confirmation (the script turns it into a toast); bad input gets a 400 page with the reason. Local hardening: the server binds to `127.0.0.1` and
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
