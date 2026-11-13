---
name: today
description: Daily job-search brief from Hirey — applications that moved to No response, queued company research and CV fit checks (run them), Agent thread messages waiting for an answer (run /ask), tasks due today or overdue (with their application), and new getjo matches scored 7 or more that are not yet tracked. Use when the user types /today or asks what to do today, what is due, or what new matches there are.
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
     got a Debrief task and that the agent can move it back if a reply came.
   - **Agent work** (only if the output has an "Agent work pending" line): run /research now,
     without asking the user to type anything, and give its short summary here. It reads
     cv.md and does web searches about the companies; that is expected.
   - **Agent thread** (only if the output has an "Agent thread" line): messages left in the
     dashboard's Agent box are waiting. Run /ask now and give its one-line
     summary per application.
   - **Due now**: tasks due today or overdue, oldest first, with the company when the task
     belongs to an application. One line each.
   - **New matches** (jobs already tracked or skipped in the dashboard's New offers are left
     out, and so are jobs getjo marks closed or expired): company, role, score, link, and the
     "may be closed" / "expires in N days" flag when the line has one. Keep the getjo id
     visible: it is what `/applied` needs.
   - One suggested first step for the day.

   If a section is empty, say so in one line. If getjo matches were skipped, mention the
   reason from the output and carry on.

Rules:
- Read only what the command prints. Do not open the private folder (CV, notes, sessions)
  for this skill, except through /research and /ask for pending agent work.
- Do not apply to anything or contact anyone. Suggest; the user acts.
