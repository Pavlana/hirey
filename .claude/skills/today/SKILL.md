---
name: today
description: Daily job-search brief from inter_prep — applications that moved to No response, tasks due today or overdue (with their application), and new getjo matches scored 7 or more that are not yet tracked. Use when the user types /today or asks what to do today, what is due, or what new matches there are.
---

# /today

1. Run from the repo root:

   ```bash
   python -m prep.cli today
   ```

   Options: `--threshold N` (default 7), `--limit N` (matches shown, default 15).

2. Reply in short, plain English, in this order:
   - **Moved to No response** (only if the output lists any): applications that sat in
     Applied with no reply and were moved automatically. One line each; mention that each
     got a Debrief task and that moving the card back on the dashboard works as usual.
   - **Due now**: tasks due today or overdue, oldest first, with the company when the task
     belongs to an application. One line each.
   - **New matches** (jobs already applied for or skipped on the Job Offers page are left out):
     company, role, score, link. Keep the getjo id visible: it is what
     `/applied` needs.
   - One suggested first step for the day.

   If a section is empty, say so in one line. If getjo matches were skipped, mention the
   reason from the output and carry on.

Rules:
- Read only what the command prints. Do not open the private folder (CV, notes, sessions)
  for this skill.
- Do not apply to anything or contact anyone. Suggest; the user acts.
