---
name: applied
description: Record a job application in inter_prep from a getjo job id, a job URL, or a company and role, linking it to the getjo job when one matches; it gets its first tasks automatically. Use when the user types /applied or says they applied (or are interested in applying) somewhere.
---

# /applied

Records an application. It never submits anything anywhere. Same rules as the Interested button
on the dashboard's Job Offers page and the Apply button in an Interested card's panel.

1. Work out what identifies the job:
   - a getjo id (`source:company:job_id`, as shown by `/today`) → `--job-id`
   - a URL → `--url` (add `--company`/`--role` if getjo does not know the URL)
   - otherwise → `--company` and `--role`

2. Work out the stage from what the user said; do not ask for a next action:
   - sent → default (status `applied`, date applied = today unless the user gave one: `--date`)
   - only interested / planning to apply → `--status to_apply` (shown as "Interested")
   Pass tier (A/B/C), type (contract/permanent), contact or notes only if the user gave them.
   Do not ask about pay. If the user offers it, pass it with `--pay`; it stays in the local
   database only and must not be repeated in replies, logs or files.

3. Run from the repo root, for example:

   ```bash
   python -m prep.cli applied --job-id "greenhouse:acme:123"
   python -m prep.cli applied --job-id "greenhouse:acme:123" --status to_apply
   ```

   Dates take `YYYY-MM-DD`, `today`, `tomorrow` or `+N` days. Running it again for the same
   job updates the existing row and never moves it back to an earlier stage.
   The first tasks are created automatically:
   - Interested: no task yet. Company research and a CV fit check are queued for the agent;
     the output says so. Offer to run /research now. Once both are saved, a "Review research
     and apply" task (due in 2 days) appears.
   - Applied: Chase if no reply (due in 7 days); an open Review research and apply (or older
     Apply) task is marked done.

4. Report the printed line in one or two sentences: what was recorded, whether it was linked
   to a getjo job, and the tasks created. If the output says several getjo jobs matched, show
   the ids and ask which one, then re-run with `--job-id`.

Later changes go through the stage, which creates and closes tasks the same way whether /stage,
/ask or the command below makes them:

```bash
python -m prep.cli update <id> --status technical --interview-date 2026-02-03 --comment "two rounds"
python -m prep.cli comment <id> "take-home test sent"
```

Stages: to_apply (Interested), applied, recruiter_call, technical (Coding interview),
system_design, final (Company interview), offer, rejected, no_response, withdrawn, skipped
(closed from Interested without applying: `python -m prep.cli skip <id> --reason R`). For the
four interview stages, pass `--interview-date` when the user gave one: the prep tasks are due
the day before. A take-home test or a second interview is a comment, not a stage.
`--next-action "..." [--next-date D]` adds a task to the application. List:
`python -m prep.cli apps [--status S]`.
