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

Reply with the line the command printed, or a short list. No extra commentary.
