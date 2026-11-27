"""The search gate and ranking behind New offers' Top 5 this week.

Plain code where the answer is plain, the agent where it needs judgement:

    candidates (getjo: open, untracked, undecided, score 7+; later other sources, same job dict)
        -> plain gates: company and sector (company_checks), interviews (company_checks), location, level
           words in the title
        -> agent gates (job_screens, written with the job brief): role family, level from the ad, team size,
           location detail, pay pass / fail / unknown, and the ranking signals (agents, evals, contract)
        -> state: failed (a gate says no) · screening (no screen yet) · held (interview style unknown) · passed
        -> passed jobs ranked by the criteria's order, each with one "why" line
        -> the week's picks (top_picks): the first `top_per_week` passed jobs, kept for the week

A job dict needs id, company, title, location, score, posted_at, first_seen; `origin` says where it came
from ("getjo", the default: getjo polls the watch list, so its companies pass the company gate unless a
check says drop). A recruiter's invitation (origin "linkedin", invite_jobs) builds the same dict: its id is
"linkedin:<invitation id>", its score is the CV fit (1-5) doubled onto getjo's 0-10 scale, and it joins the
candidates while its application is still Interested.
Every criterion is yes / no / unknown; pay is pass / fail / unknown and its figure is never seen here.
"""

import json
import re
import sqlite3
from datetime import date, timedelta

from prep import db
from prep.criteria import Criteria

STATES = ("failed", "screening", "held", "passed")
AGENT_GATES = ("role_family", "level", "team", "location", "pay")  # gates the screen answers
GATE_LABELS = {"company": "company", "sector": "sector", "interviews": "interview style", "role_family": "role family",
               "level": "level", "team": "team size", "location": "location", "pay": "pay"}
SCORE = {"yes": 2, "unknown": 1, "no": 0}
_REMOTE = re.compile(r"\b(remote|anywhere|work from home|wfh)\b", re.IGNORECASE)
_REGIONS = {"uk": ("uk", "united kingdom", "england", "scotland", "wales", "great britain", "gb")}


def week_start(today: str) -> str:
    """The Monday of `today`'s week, YYYY-MM-DD."""
    day = date.fromisoformat(today)
    return (day - timedelta(days=day.weekday())).isoformat()


def _words(text: str, words: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(w)}\b", text, re.IGNORECASE) for w in words)


def title_level(title: str | None, crit: Criteria) -> str:
    """The level gate from the title alone: no for an Out word (lead, staff, …), yes for "Senior", else
    unknown (the ad decides)."""
    text = title or ""
    if crit.level_out and _words(text, crit.level_out):
        return "no"
    return "yes" if _words(text, ("senior", "snr", "sr")) else "unknown"


def plain_location(location: str | None, crit: Criteria) -> str:
    """The location gate from getjo's location text: yes in a named city or remote within a named region,
    unknown for bare "Remote" or no location, else no."""
    text = (location or "").strip()
    if not text:
        return "unknown"
    if crit.cities and _words(text, crit.cities):
        return "yes"
    if _REMOTE.search(text):
        regions = tuple(n for r in crit.remote_regions for n in _REGIONS.get(r.lower(), (r.lower(),)))
        return "yes" if regions and _words(text, regions) else "unknown"
    return "no" if crit.cities else "unknown"


def combine(*values: str | None) -> str:
    """One gate value from several answers: no wins, then yes, else unknown."""
    given = [v for v in values if v]
    return "no" if "no" in given else "yes" if "yes" in given else "unknown"


def gates(job: dict, check: dict | None, screen: dict | None, crit: Criteria) -> dict[str, str]:
    """Every gate the criteria name, yes / no / unknown, for one job."""
    screen = screen or {}
    check = check or {}
    out: dict[str, str] = {}
    for key in crit.gates:
        if key == "company":
            verdict = check.get("verdict")
            out[key] = ("no" if verdict == "drop" else "yes" if verdict in ("strong", "possible")
                        else "yes" if job.get("origin", "getjo") == "getjo" else "unknown")
        elif key == "sector":
            out[key] = check.get("sector_ok") or "unknown"
        elif key == "interviews":
            out[key] = check.get("interviews") or "unknown"
        elif key == "level":
            out[key] = combine(title_level(job.get("title"), crit), screen.get("level"))
        elif key == "location":
            out[key] = combine(plain_location(job.get("location"), crit), screen.get("location"))
        elif key == "pay":
            pay = screen.get("pay")
            out[key] = "no" if pay == "fail" else "yes" if pay == "pass" else "unknown"
        else:
            out[key] = screen.get(key) or "unknown"
    return out


def plain_fail(job: dict, check: dict | None, crit: Criteria) -> bool:
    """True when plain code alone already fails the job (no agent work is queued for it)."""
    return "no" in gates(job, check, None, crit).values()


def _age(job: dict, today: str) -> int | None:
    stamp = job.get("posted_at") or job.get("first_seen")
    try:
        return max(0, (date.fromisoformat(today) - date.fromisoformat(stamp[:10])).days) if stamp else None
    except ValueError:
        return None


def signals(job: dict, check: dict | None, screen: dict | None, crit: Criteria, today: str) -> dict:
    """The ranking signals, each a number (higher is better)."""
    screen, check = screen or {}, check or {}
    age = _age(job, today)
    contract = screen.get("contract")
    kind = check.get("kind")
    return {
        "builds_agents": SCORE.get(screen.get("builds_agents") or "unknown", 1),
        "evals": SCORE.get(screen.get("evals") or "unknown", 1),
        "fit": job.get("score") or 0,
        "verdict": {"strong": 2, "possible": 1}.get(check.get("verdict") or "", 0),
        "product": {"product": 2, "consultancy": 0}.get(kind or "", 1),
        "permanent": {"permanent": 2, "contract": 0}.get(contract or "", 1),
        "fresh": 1 if age is not None and age < crit.fresh_days else 0,
    }


def why_line(job: dict, check: dict | None, screen: dict | None, today: str) -> str:
    """One line on why the job ranks where it does, e.g.
    "8/10 fit · strong company · builds agents · evals in role · posted 3 days ago"."""
    screen, check = screen or {}, check or {}
    parts = []
    if job.get("origin") == "linkedin":
        parts.append("LinkedIn")
        if job.get("fit") is not None:
            parts.append(f"{job['score']}/10 CV fit ({job['fit']}/5)")
    elif job.get("score") is not None:
        parts.append(f"{job['score']}/10 fit")
    if check.get("verdict") in ("strong", "possible"):
        parts.append(f"{check['verdict']} company")
    if screen.get("builds_agents") == "yes":
        parts.append("builds agents")
    if screen.get("evals") == "yes":
        parts.append("evals in role")
    if check.get("kind") == "consultancy":
        parts.append("consultancy")
    if screen.get("contract") == "contract":
        parts.append("contract")
    age = _age(job, today)
    if age is not None:
        parts.append("posted today" if age == 0 else f"posted {age} day{'s' if age != 1 else ''} ago")
    return " · ".join(parts)


def evaluate(job: dict, check: dict | None, screen: dict | None, crit: Criteria, today: str) -> dict:
    """The gate result of one job: state (STATES), gates, failed (gate keys that said no), why and rank key."""
    found = gates(job, check, screen, crit)
    failed = [k for k, v in found.items() if v == "no"]
    needs_screen = any(k in AGENT_GATES for k in crit.gates) or bool(set(crit.ranking) & {"builds_agents", "evals"})
    if failed:
        state = "failed"
    elif screen is None and needs_screen:
        state = "screening"
    elif found.get("interviews") == "unknown":
        state = "held"
    else:
        state = "passed"
    sig = signals(job, check, screen, crit, today)
    return {"job": job, "state": state, "gates": found, "failed": failed, "check": check,
            "why": why_line(job, check, screen, today),
            "rank": tuple(sig[k] for k in crit.ranking)}


def evaluate_all(conn: sqlite3.Connection, jobs: list[dict], crit: Criteria, today: str) -> list[dict]:
    """evaluate() for every job, passed ones ranked best first (then by id, so the order is stable);
    other states follow in their getjo order."""
    checks, screens = db.company_checks(conn), db.job_screens(conn)
    rows = [evaluate(j, checks.get(db.company_slug(j.get("company") or "")), screens.get(j["id"]), crit, today)
            for j in jobs]
    rows.sort(key=lambda r: r["job"]["id"])
    rows.sort(key=lambda r: r["rank"] if r["state"] == "passed" else (), reverse=True)
    return rows


def failed_text(row: dict) -> str:
    """The gates a filtered-out job failed, e.g. "level, location"."""
    return ", ".join(GATE_LABELS.get(k, k) for k in row["failed"])


def pick_week(conn: sqlite3.Connection, rows: list[dict], decisions: dict, today: str, n: int) -> list[dict]:
    """The week's Top picks, filled up to `n` from the ranked passed jobs. A pick the user has acted on
    (decided) stays for the week; one not acted on that no longer passes is dropped and replaced."""
    week = week_start(today)
    passed = [r for r in rows if r["state"] == "passed"]
    live = {r["job"]["id"] for r in passed}
    kept = []
    for pick in db.week_picks(conn, week):
        if pick["getjo_job_id"] in decisions or pick["getjo_job_id"] in live:
            kept.append(pick)
        else:
            db.drop_pick(conn, week, pick["getjo_job_id"])
    have = {p["getjo_job_id"] for p in kept}
    for row in passed:
        if len(have) >= n:
            break
        if row["job"]["id"] not in have:
            db.add_pick(conn, week, row["job"], row["why"])
            have.add(row["job"]["id"])
    return db.week_picks(conn, week)


def pick_outcome(decision: dict | None) -> str:
    """What happened to a pick: applied, interested, skipped or open (not acted on)."""
    return decision["decision"] if decision else "open"


def pick_stats(conn: sqlite3.Connection) -> dict:
    """Pipeline stats' search result: every job ever picked (once, however many weeks), how many were applied
    to, are Interested, were skipped (by reason) or are not acted on yet."""
    found = decisions(conn)
    jobs = list(dict.fromkeys(p["getjo_job_id"] for p in db.all_picks(conn)))
    counts = {"picked": len(jobs), "applied": 0, "interested": 0, "skipped": 0, "open": 0}
    reasons: dict[str, int] = {}
    for job_id in jobs:
        d = found.get(job_id)
        outcome = pick_outcome(d)
        counts[outcome] += 1
        if outcome == "skipped":
            reasons[d["reason"] or "other"] = reasons.get(d["reason"] or "other", 0) + 1
    counts["share"] = counts["applied"] / counts["picked"] if counts["picked"] else None
    counts["reasons"] = sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))
    return counts


def fit_on_ten(fit: int | None) -> int | None:
    """A CV fit (1-5) on getjo's 0-10 scale, so a LinkedIn job and a getjo job compare at a glance."""
    return fit * 2 if fit is not None else None


def invite_job(conn: sqlite3.Connection, inv: dict) -> dict:
    """The job dict of a named invitation (db.invitations), as evaluate() takes it."""
    fit = db.cv_fit(conn, inv["application_id"])
    return {"id": inv["job_id"], "company": inv["company"], "title": inv["role"], "location": inv["location"],
            "score": fit_on_ten(fit["score"] if fit else None), "fit": fit["score"] if fit else None,
            "posted_at": inv["created_at"], "first_seen": None, "origin": "linkedin", "app_id": inv["application_id"]}


def invite_jobs(conn: sqlite3.Connection) -> list[dict]:
    """The candidates from recruiters' invitations: each named one whose application is still Interested."""
    out = []
    for inv in db.invitations(conn, named=True):
        app = db.get_application(conn, inv["application_id"])
        if app and app["status"] == "to_apply":
            out.append(invite_job(conn, inv))
    return out


def invite_eval(conn: sqlite3.Connection, inv: dict, crit: Criteria, today: str) -> dict:
    """The evaluation card of a named invitation: evaluate() of its job, whatever its application's stage."""
    job = invite_job(conn, inv)
    return evaluate(job, db.company_check(conn, job["company"] or ""), db.job_screens(conn).get(job["id"]),
                    crit, today)


def decisions(conn: sqlite3.Connection) -> dict[str, dict]:
    """getjo jobs' decisions and named invitations' (db.invite_decisions), by job id."""
    return {**db.job_decisions(conn), **db.invite_decisions(conn)}


def board(conn: sqlite3.Connection, jobs: list[dict], crit: Criteria, today: str, n: int) -> dict:
    """Everything the Top 5 view shows, from the candidate jobs (getjo's, plus invite_jobs): `picks` (the
    week's, by rank, each with its evaluated `row` while the job is still a candidate, else None), `more`
    (passed, not picked), `held`, `screening` and `failed` rows, and `rows` (all of them)."""
    rows = evaluate_all(conn, list(jobs) + invite_jobs(conn), crit, today)
    decided = decisions(conn)
    picks = pick_week(conn, rows, decided, today, n)
    by_id = {r["job"]["id"]: r for r in rows}
    picked = {p["getjo_job_id"] for p in picks}
    for p in picks:
        p["row"] = by_id.get(p["getjo_job_id"])
        p["outcome"] = pick_outcome(decided.get(p["getjo_job_id"]))
        invite = db.invite_id_of(p["getjo_job_id"])
        inv = db.get_invitation(conn, invite) if invite is not None and p["row"] is None else None
        p["job"] = invite_job(conn, inv) if inv and inv["application_id"] else None  # acted on: still links to it
    return {"picks": picks, "rows": rows,
            "more": [r for r in rows if r["state"] == "passed" and r["job"]["id"] not in picked],
            **{state: [r for r in rows if r["state"] == state] for state in ("held", "screening", "failed")}}


# --- company list import ------------------------------------------------------------------------
# A researched company list (JSON, or a JS file "window.X = {...};") with a "companies" list of objects:
# company, sector, verdict (strong / possible / drop), leetcode_risk (low / medium / high / unknown),
# interview_style; or a Markdown table "company | yes / no / mixed / unknown | evidence" from an interview
# check, where yes means puzzle-style rounds were reported.
PUZZLE_SCREENS = ("leetcode", "karat", "hackerrank", "codesignal", "codility")
_NEGATED = re.compile(r"\b(?:no|not|without|never)\b[^,.;]{0,30}?\b(?:" + "|".join(PUZZLE_SCREENS) + r")\b", re.IGNORECASE)
_NOT_ALIASES = {"london", "uk", "us", "usa", "remote", "europe"}


def interviews_from(risk: str | None, style: str | None) -> str:
    """The interviews gate from a researched company, from its LeetCode risk: high is no, low is yes. Medium
    or unknown is no only when the style names a puzzle screen and says nothing practical; otherwise unknown
    (held: the Research worker checks it, the user decides). A "no" drops every job at the company, so a
    single report beside a practical process never makes it."""
    level = (risk or "").lower()
    if level in ("high", "low"):
        return "no" if level == "high" else "yes"
    text = _NEGATED.sub("", style or "")
    practical = re.search(r"\b(take-home|pair|pairing|practical|mixed)\b", text, re.IGNORECASE)
    return "no" if _words(text, PUZZLE_SCREENS) and not practical else "unknown"


def split_name(name: str) -> tuple[str, tuple[str, ...]]:
    """("RELX", ("Elsevier", "LexisNexis")) from "RELX (Elsevier, LexisNexis)": the name before the
    brackets and the other names inside ("incl." and "ex-" dropped; places and "in ..." parts left out)."""
    found = re.match(r"^\s*([^(]+?)\s*\((.*)\)\s*$", name)
    if not found:
        return name.strip(), ()
    aliases = []
    for part in re.split(r"[,;]", found.group(2)):
        part = re.sub(r"^(?:incl\.?|including|ex-|formerly|now)\s*", "", part.strip(), flags=re.IGNORECASE).strip()
        if len(part) < 2 or part.lower() in _NOT_ALIASES or part.lower().startswith(("in ", "e.g.")):
            continue
        aliases.append(part)
    return found.group(1).strip(), tuple(aliases)


def _note(text: str | None) -> str | None:
    note = (text or "").strip()[:db.MAX_CHECK_NOTE] or None
    return None if note and db.PAY_PATTERN.search(note) else note


def parse_company_list(text: str, markdown: bool) -> list[tuple[str, tuple[str, ...], dict]]:
    """(company, aliases, check fields) for each company in a researched list (see above)."""
    out = []
    if markdown:
        values = {"yes": "no", "no": "yes"}  # "LeetCode-style: yes" fails the interviews gate
        for line in text.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 2 or cells[1].lower() not in ("yes", "no", "mixed", "unknown"):
                continue
            name, aliases = split_name(cells[0])
            out.append((name, aliases, {"interviews": values.get(cells[1].lower(), "unknown"),
                                        "note": _note(cells[2] if len(cells) > 2 else None)}))
        return out
    body = text[text.index("{"):text.rindex("}") + 1] if "{" in text else text
    data = json.loads(body)
    for c in data.get("companies", []) if isinstance(data, dict) else data:
        name, aliases = split_name(str(c.get("company") or ""))
        if not name:
            continue
        verdict = str(c.get("verdict") or "unknown").lower()
        sector = str(c.get("sector") or "")
        fields = {"verdict": verdict if verdict in db.VERDICTS else "unknown",
                  "sector_ok": "yes" if verdict in ("strong", "possible") else "unknown",
                  "interviews": interviews_from(c.get("leetcode_risk"), c.get("interview_style")),
                  "kind": ("consultancy" if re.search(r"consult|agency|services firm", sector, re.IGNORECASE)
                           else "product" if sector else "unknown"),
                  "note": _note(c.get("interview_style"))}
        out.append((name, aliases, fields))
    return out


def import_companies(conn: sqlite3.Connection, rows: list[tuple[str, tuple[str, ...], dict]]) -> dict:
    """Save imported company checks in one transaction. A company the Research worker or the user has
    checked keeps that check. Returns counts: saved, kept."""
    counts = {"saved": 0, "kept": 0}
    with conn:
        for name, aliases, fields in rows:
            existing = db.company_check(conn, name) or next(
                (db.company_check(conn, a) for a in aliases if db.company_check(conn, a)), None)
            if existing and existing["source"] != "import":
                counts["kept"] += 1
                continue
            db.save_company_check(conn, existing["company"] if existing else name, fields, "import", aliases,
                                  commit=False)
            counts["saved"] += 1
    return counts
