---
name: task
description: Add, list and complete job-search tasks in inter_prep (kinds apply, prep, drill, admin, with optional due date and linked application). Use when the user types /task or asks to add a to-do, see open tasks, or mark one done.
---

# /task

Run from the repo root.

- **Add**: `python -m prep.cli task add "Title" [--kind apply|prep|drill|admin] [--due DATE] [--app APP_ID]`
  - Default kind is `admin`. Pick the kind from the wording: revising a topic is `prep`,
    practising a weak spot is `drill`, sending an application is `apply`.
  - Dates take `YYYY-MM-DD`, `today`, `tomorrow` or `+N` days. Ask for a due date only if the
    user implied one but did not give it.
  - `--app` links the task to an application id (see `python -m prep.cli apps`).
- **List**: `python -m prep.cli task list` (open tasks), `--done`, or `--all`.
- **Complete**: `python -m prep.cli task done TASK_ID`. If the user names a task by title,
  list first, match it, and confirm when more than one fits.
- **Change or drop**: `python -m prep.cli task edit TASK_ID [--title T] [--due D | --no-due]`,
  `python -m prep.cli task delete TASK_ID`.

These are the same tasks as on the dashboard's Applications page: a task with `--app` shows
in that application's panel; one without shows only here and in /today. Stage tasks are created and closed
automatically when an application changes stage (`python -m prep.cli update ID --status S`);
closed ones show in `--all` as "(closed)". An application's next action is always its next
open task.

Reply with the line the command printed, or a short list. No extra commentary.
