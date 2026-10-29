---
name: applied
description: Record a job application in inter_prep from a getjo job id, a job URL, or a company and role, linking it to the getjo job when one matches; it gets its first tasks automatically. Use when the user types /applied or says they applied (or are interested in applying) somewhere.
---

# /applied

Records an application. It never submits anything anywhere. Same rules as the Applied and
Interested buttons on the dashboard's Job Offers page.

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
   - Interested: Research the company and Check CV fit / tailor CV (due in 2 days); Apply (due in 3 days).
   - Applied: Chase if no reply (due in 7 days); an open Apply task is marked done.

4. Report the printed line in one or two sentences: what was recorded, whether it was linked
   to a getjo job, and the tasks created. If the output says several getjo jobs matched, show
   the ids and ask which one, then re-run with `--job-id`.

Later changes: `python -m prep.cli update <id> --status technical --next-action "..." --next-date ...`
(`--clear-next` removes the next action). List: `python -m prep.cli apps [--status S]`.
