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
│   applications, interviews,         │        │ need it (phase 3+)        │
│   tasks, gaps                       │        └───────────────────────────┘
│ prep/cli.py     commands            │
└───────▲──────────────────▲──────────┘
        │                  │
 Claude Code skills    local dashboard
 /today /applied /task (phase 2, 127.0.0.1)
```

## Data flow

1. getjo stores each job once in its `jobs` table with a score from 1 to 10.
2. `/today` runs `python -m prep.cli today`. It reads `prep.db` for overdue next actions and
   due tasks, then opens getjo's database read-only and lists jobs scored at or above 7 whose
   id is not in `applications.getjo_job_id`.
3. `/applied` records an application. Given a getjo id, URL, or company and role, it looks the
   job up in getjo and stores the link (`getjo_job_id`), so the job drops out of the match list.
4. `/task` adds, lists and completes tasks. Tasks can point at an application or a gap.

## Configuration

One TOML file holds absolute paths: `getjo_db`, `prep_db`, `private_dir`. It is looked up as
`$INTERPREP_SETTINGS`, then `config/settings.toml` in the checkout, then
`~/.config/inter_prep/settings.toml`. The per-user file is the normal place, because dev3
worktrees, launchd and the Claude app all find it without a shell environment.
`PREP_GETJO_DB`, `PREP_DB` and `PREP_PRIVATE_DIR` override single keys. A relative `prep_db`
is resolved against the repo root of the running checkout, so `prep_db` should be an absolute
path outside any worktree.

## Boundaries

- getjo is never written to and its code is never imported; the `jobs` table shape is the
  only contract.
- Nothing applies to jobs or contacts anyone.
- Personal files stay in `private_dir` and gitignored paths; `prep/privacy.py` checks that
  none of them is tracked.
