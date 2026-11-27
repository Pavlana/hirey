# Job criteria (example)

A made-up example of the criteria file. The real one is private (default `notes/criteria.md` in
private_dir, or `criteria_file` in the settings) and never committed. New offers' Top 5 this week, the
Research worker's job screens and the company checks all read it, so changing it there changes them all.

Every criterion must be answerable yes / no / unknown from the company data, the job ad or the CV fit
check. Plain code reads the gate names, the Level "Out:" list, the Location line and the Ranking order;
the agent reads the rest.

## Gates (fail one and the job is not shown)

1. **Company**: on getjo's watch list, or checked with verdict strong or possible.
2. **Sector**: product companies and enterprises with an in-house tech team, boutique AI consultancies.
   Out: example sectors the user does not want (defence, gambling).
3. **Interviews**: no puzzle-style coding rounds and no timed online coding screens. Checked once per
   company. Unknown = HOLD: listed under "Held: checking interviews" until the company is checked.
4. **Role family**: hands-on AI engineering roles (AI engineer, LLM engineer, AI platform engineer).
5. **Level**: senior individual contributor only. Out: lead, principal, staff, head, manager, junior,
   graduate. A title with no level is judged from the ad.
6. **Team**: not a gate. A small team can still be interesting; a team of one or an unclear team is a red
   flag and a question to clarify in the job's panel.
7. **Location**: London on-site or hybrid, or remote within the UK.
8. **Pay**: at or above the floor written in the real file. Unknown pay passes; only a stated figure below
   the floor fails. Only pass / fail / unknown is ever stored.

## Ranking (order among jobs that pass)

1. Builds agents or LLM products.
2. Evals, quality or guardrails in the role.
3. CV fit score.
4. Company verdict: strong above possible.
5. Product company above boutique consultancy.
6. Permanent above contract (both pass).
7. Fresh posting: under 2 weeks.

## Volume

A top 5 a week. Anything not acted on rolls into the next week's ranking.
