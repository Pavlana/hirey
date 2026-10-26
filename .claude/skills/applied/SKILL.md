---
name: applied
description: Record a job application in inter_prep from a getjo job id, a job URL, or a company and role, linking it to the getjo job when one matches, and set the next action. Use when the user types /applied or says they applied (or plan to apply) somewhere.
---

# /applied

Records an application. It never submits anything anywhere.

1. Work out what identifies the job:
   - a getjo id (`source:company:job_id`, as shown by `/today`) → `--job-id`
   - a URL → `--url` (add `--company`/`--role` if getjo does not know the URL)
   - otherwise → `--company` and `--role`

2. Ask, in one message, for anything not already given:
   - next action and its date (required: e.g. "Follow up with recruiter", `+7`)
   - optional: tier (A/B/C), type (contract/permanent), contact, notes, whether it is only
     planned (`--status to_apply`) rather than sent.
   Do not ask about pay. If the user offers it, pass it with `--pay`; it stays in the local
   database only and must not be repeated in replies, logs or files.

3. Run from the repo root, for example:

   ```bash
   python -m prep.cli applied --job-id "greenhouse:acme:123" \
       --next-action "Follow up with recruiter" --next-date +7 --tier A
   ```

   Dates take `YYYY-MM-DD`, `today`, `tomorrow` or `+N` days. `--date` sets the date applied
   (default today). Running it again for the same job updates the existing row.

4. Report the printed line in one or two sentences: what was recorded, whether it was linked
   to a getjo job, and the next action. If the output says several getjo jobs matched, show
   the ids and ask which one, then re-run with `--job-id`.

Later changes: `python -m prep.cli update <id> --status technical --next-action "..." --next-date ...`
(`--clear-next` removes the next action). List: `python -m prep.cli apps [--status S]`.
