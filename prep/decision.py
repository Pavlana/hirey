"""What a job's panel needs to decide: the red flags and the questions for the recruiter call.

The panel (prep/web.py) answers three things in order: what the company is and which red flags the ad and
the research show, what to clarify on the recruiter call, then Tailor CV and apply. This module builds the
first two from what is already stored, in plain code (no agent call):

    company research (company_profiles: red_flags, call_questions, employer_kind)
    + the job brief's "## Red flags / notable" bullets (job_briefs)
    + the job's gates (search.gates with a criteria file, else the screen and the company check)
    -> red_flags(...)      short lines, a gate that says no first, duplicates dropped
    -> call_questions(...) about 4-6 plain questions for the recruiter call, never technical: a fixed
                           template (interview stages, team size, office days, ...) where the gates say
                           unknown, one or two from the research, and pay last as "ask on the call"

Team size is never a gate (criteria.NOT_GATES): a team of one is a red flag, an unclear one a red flag
once the ad has been screened, and a question either way. Pay is only ever a spoken question: the item
carries call_only, the panel gives it no Copy, and nothing here holds a figure.
"""

import re

from prep import db, search
from prep.criteria import Criteria

MAX_QUESTIONS = 6          # pay included
MAX_RESEARCH_QUESTIONS = 2  # role-specific ones from the company research
PAY_QUESTION = "What is the pay range?"

# A gate that says no, as a red flag.
NO_FLAGS = {
    "company": "Company dropped in its check",
    "sector": "Sector outside the criteria",
    "interviews": "LeetCode-style rounds or a timed coding screen reported",
    "role_family": "Not a hands-on AI-building role",
    "level": "Not a senior IC role (lead, staff, manager or junior)",
    "location": "Location outside London or UK remote",
    "pay": "Stated pay below the floor",
}
TEAM_FLAGS = {"no": "Team of one (or very small)", "unknown": "Team size unclear"}

# The fixed recruiter-call template, in this order: (gate key or None for always, question).
TEMPLATE = (
    ("interviews", "What are the interview stages, and what should the skills interview or presentation cover?"),
    ("team", "How many people work in the AI team?"),
    ("location", "How many days a week in the office?"),
    ("product", "What is the product, and who uses it?"),
    ("level", "Is it a senior individual contributor role, or does it include leading people?"),
)


def _bullets(markdown: str, heading: str) -> list[str]:
    """The "-" / "*" bullet lines under the first "## ..." heading starting with `heading` (any case)."""
    out: list[str] = []
    inside = False
    for line in markdown.splitlines():
        if line.startswith("#"):
            inside = line.lstrip("#").strip().lower().startswith(heading)
            continue
        found = re.match(r"^\s*[-*•]\s+(.*\S)", line)
        if inside and found:
            out.append(found.group(1))
    return out


def role_needs(brief_text: str | None) -> list[str]:
    """The job brief's "## Must-haves" bullets: what the role needs."""
    return _bullets(brief_text or "", "must-have")


def brief_flags(brief_text: str | None) -> list[str]:
    """The job brief's "## Red flags / notable" bullets."""
    return _bullets(brief_text or "", "red flag")


def gate_values(job: dict, check: dict | None, screen: dict | None, crit: Criteria | None) -> dict[str, str]:
    """Every value the panel reads, yes / no / unknown: the criteria's gates (search.gates) when there is a
    criteria file, else the screen and the company check as they are. `team` always comes from the screen."""
    screen, check = screen or {}, check or {}
    if crit is not None:
        values = search.gates(job, check, screen, crit)
    else:
        values = {k: screen.get(k) or "unknown" for k in ("role_family", "level", "location")}
        values["interviews"] = check.get("interviews") or "unknown"
        values["pay"] = {"fail": "no", "pass": "yes"}.get(screen.get("pay") or "", "unknown")
    values["team"] = screen.get("team") or "unknown"
    return values


def employer_kind(profile: dict | None, check: dict | None, app_kind: str | None) -> str | None:
    """product or client (a consultancy or agency): the application's kind, else the research's, else the
    company check's; None when nothing says."""
    kind = app_kind or (profile or {}).get("employer_kind")
    if kind:
        return kind
    return {"consultancy": "client", "product": "product"}.get((check or {}).get("kind") or "")


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for item in items:
        key = re.sub(r"\W+", " ", item.lower()).strip()
        if key and key not in seen:
            seen.add(key)
            out.append(item.strip())
    return out


def red_flags(profile: dict | None, brief_text: str | None, gates: dict[str, str], screened: bool,
              client: bool, contract: str | None = None) -> list[str]:
    """The red flags, gates that say no first, then team size, client work, a contract, the research's and
    the brief's own. `screened`: the ad has been screened, so an unclear team is a flag (before that it is
    only a question)."""
    flags = [NO_FLAGS[k] for k, v in gates.items() if v == "no" and k in NO_FLAGS]
    team = gates.get("team", "unknown")
    if team == "no" or (team == "unknown" and screened):
        flags.append(TEAM_FLAGS[team])
    if client:
        flags.append("Client delivery (consultancy or agency)")
    if contract == "contract":
        flags.append("Contract role")
    flags += (profile or {}).get("red_flags") or []
    flags += brief_flags(brief_text)
    return _dedupe(flags)


_OFFICE = re.compile(r"\b(\d\s*days?|fully remote|remote|on-?site)\b", re.IGNORECASE)


def call_questions(profile: dict | None, gates: dict[str, str], facts: str = "") -> list[dict]:
    """About 4-6 questions for the recruiter call: {"text", "call_only"}. The template's questions where the
    answer is not known yet (interview stages always: what to prepare is never in the ad; office days unless
    the brief's key facts `facts` or the research already say), then one or two from the research, then pay
    last, call_only (spoken, never written in a message)."""
    profile = profile or {}
    known_office = bool(_OFFICE.search(facts or "")) or bool(_OFFICE.search(profile.get("london_remote") or ""))
    out = []
    for key, text in TEMPLATE:
        if key == "interviews":
            wanted = True
        elif key == "product":
            wanted = not profile.get("what_it_does")
        elif key == "location":
            wanted = gates.get(key, "unknown") != "yes" and not known_office
        else:
            wanted = gates.get(key, "unknown") != "yes"
        if wanted:
            out.append(text)
    out += list(profile.get("call_questions") or [])[:MAX_RESEARCH_QUESTIONS]
    out = _dedupe(out)[:MAX_QUESTIONS - 1]
    return [{"text": q, "call_only": False} for q in out] + [{"text": PAY_QUESTION, "call_only": True}]


def for_application(conn, app: dict, crit: Criteria | None, location: str | None = None) -> dict:
    """Everything sections 1 and 2 of an application's panel need, also printed by `thread show` for the agent:
    {"profile", "check", "screen", "brief", "gates", "kind", "flags", "questions"}. The job is the application's
    getjo job or, for a recruiter's invitation, its "linkedin:<id>" job; `location` is getjo's, when known."""
    inv = db.invitation_for_app(conn, app["id"])
    if inv is not None:
        job = search.invite_job(conn, inv)
    else:
        job = {"id": app["getjo_job_id"], "company": app["company"], "title": app["role"], "location": location,
               "score": None, "origin": "getjo" if app["getjo_job_id"] else "other"}
    profile = db.company_profile(conn, db.company_slug(app["company"]))
    check = db.company_check(conn, app["company"])
    screen = db.job_screens(conn).get(job["id"]) if job["id"] else None
    brief = db.job_briefs(conn, {app["getjo_job_id"]}).get(app["getjo_job_id"]) if app["getjo_job_id"] else None
    gates = gate_values(job, check, screen, crit)
    kind = employer_kind(profile, check, app.get("employer_kind"))
    flags = red_flags(profile, brief["brief"] if brief else None, gates, screen is not None, kind == "client",
                      (screen or {}).get("contract"))
    return {"profile": profile, "check": check, "screen": screen, "brief": brief, "gates": gates, "kind": kind,
            "flags": flags, "questions": call_questions(profile, gates, brief["facts"] if brief else "")}
