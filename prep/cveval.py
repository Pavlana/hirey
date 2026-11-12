"""The AI check of a tailored CV version: grounding, fit and a six-second skim, written by the agent in a
separate pass from the one that wrote the CV, then turned into findings here with fixed rules.

Every tailor save queues it (agent_requests kind cv_eval; db.save_tailored_cv). `research next` prints
the packet; the agent writes `cv-tailored/<version file stem>.eval.md` with TOML front matter:

    version = 2                          # the tailored version checked
    checked_at = 2026-01-20
    fit_score = 4                        # the CV fit rubric (1-5) on the tailored CV
    skim_score = 3                       # does the top third of page one show this role? 1-5
    skim_why = "one line"
    [[claims]]                           # every claim changed or added against cv.md
    quote = "words copied from the tailored CV line"
    source = "cv.md"                     # cv.md, facts (the facts bank), style (_style.md) or none
    note = "where it comes from, or what is unsupported"
    [[must_haves]]                       # the job's must-have requirements
    term = "Kubernetes"
    covered = true

`save` maps it to findings with source "ai" (db.save_cv_eval merges them into the verdict): a claim with
source "none" blocks; a must-have not covered asks for a look (unless the tailored file's front matter
already lists it under `keywords` or `gaps`, which the rule checks handle); a fit score below the master
CV's fit for the same job asks for a look; a skim score of 2 or less asks for a look. Claims are found
in the CV by their quoted words, so a finding's line number comes from the file, not from the model.
Nothing here is logged.
"""

import re
import sqlite3
from datetime import date
from pathlib import Path

from prep import cvcheck, db, research

SOURCE = "ai"
CLAIM_SOURCES = ("cv.md", "facts", "style", "none")
SUFFIX = ".eval.md"
LOW_SKIM = 2  # a skim score at or below this asks for a look
MAX_NOTE = 160  # characters of the agent's note kept in a finding


def eval_file(version_path: Path) -> Path:
    """cv-tailored/CV_Globex_FDE_101126_2.md -> cv-tailored/CV_Globex_FDE_101126_2.eval.md."""
    return version_path.with_name(version_path.stem + SUFFIX)


def _score(data: dict, name: str) -> int:
    value = data.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
        raise ValueError(f"{name} must be a whole number from 1 to 5; got {value!r}")
    return value


def _tables(data: dict, name: str) -> list[dict]:
    value = data.get(name, [])
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise ValueError(f"{name} must be a list of [[{name}]] tables")
    return value


def read_eval(path: Path) -> dict:
    """The AI check in an .eval.md file: version, checked_at, fit_score, skim_score, skim_why, claims
    (quote, source, note) and must_haves (term, covered). Raises ValueError with what to fix."""
    data, _ = research.split_front_matter(path.read_text(encoding="utf-8"))
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError(f"version must be the tailored version checked (1, 2, …); got {version!r}")
    skim_why = data.get("skim_why")
    if not isinstance(skim_why, str) or not skim_why.strip():
        raise ValueError("skim_why must say in one line why the skim score was given")
    claims = []
    for i, c in enumerate(_tables(data, "claims"), 1):
        quote, source = c.get("quote"), c.get("source")
        if not isinstance(quote, str) or not quote.strip():
            raise ValueError(f"claim {i} needs a quote: words copied from the tailored CV line")
        if source not in CLAIM_SOURCES:
            raise ValueError(f"claim {i}: source must be one of {', '.join(CLAIM_SOURCES)}; got {source!r}")
        note = c.get("note") or ""
        if not isinstance(note, str):
            raise ValueError(f"claim {i}: note must be text")
        claims.append({"quote": quote.strip(), "source": source, "note": note.strip()})
    must_haves = []
    for i, m in enumerate(_tables(data, "must_haves"), 1):
        term, covered = m.get("term"), m.get("covered")
        if not isinstance(term, str) or not term.strip() or not isinstance(covered, bool):
            raise ValueError(f"must-have {i} needs a term and covered = true or false")
        must_haves.append({"term": term.strip(), "covered": covered})
    checked_at = data.get("checked_at")
    return {
        "version": version,
        "checked_at": checked_at.isoformat() if isinstance(checked_at, date) else None,
        "fit_score": _score(data, "fit_score"),
        "skim_score": _score(data, "skim_score"),
        "skim_why": skim_why.strip(),
        "claims": claims,
        "must_haves": must_haves,
    }


def _plain(text: str) -> str:
    """Lowercase words only, so a quote matches its line whatever the Markdown, case or spacing."""
    return " ".join(re.findall(r"\w+", text.lower()))


def locate(quote: str, lines: list[tuple[int, str]]) -> tuple[int | None, str]:
    """The (line number, line) holding the quoted words, or (None, "") when no line has them."""
    want = _plain(quote)
    if not want:
        return None, ""
    for n, line in lines:
        if want in _plain(line):
            return n, line
    return None, ""


def _safe(text: str) -> str:
    """The agent's own words in a finding: dropped when they mention pay, shortened when long."""
    text = " ".join(text.split())
    if db.PAY_PATTERN.search(text):
        return ""
    return text if len(text) <= MAX_NOTE else text[:MAX_NOTE - 1] + "…"


def findings(review: dict, cv_text: str, master_fit: int | None) -> list[dict]:
    """The AI check's findings for a tailored CV's text (front matter skipped), in line order."""
    lines = cvcheck.body_lines(cv_text)
    front = cvcheck.front_matter(cv_text)
    known = {t.lower() for key in ("keywords", "gaps") for t in (cvcheck._terms(front, key) or [])}
    out = []
    for c in review["claims"]:
        if c["source"] != "none":
            continue
        n, line = locate(c["quote"], lines)
        note = _safe(c["note"])
        message = "No source in cv.md, the facts bank or the agreed wording" + (f": {note}" if note else "")
        out.append(cvcheck._finding("grounding", "block", message, n, line or c["quote"]))
    missing = [_safe(m["term"]) for m in review["must_haves"] if not m["covered"] and m["term"].lower() not in known]
    if any(missing):
        out.append(cvcheck._finding("fit", "look", "Must-haves the CV does not cover: " + ", ".join(t for t in missing if t)))
    if master_fit is not None and review["fit_score"] < master_fit:
        out.append(cvcheck._finding("fit", "look", f"Fit {review['fit_score']}/5 is below the master CV's "
                                    f"{master_fit}/5 for this job"))
    if review["skim_score"] <= LOW_SKIM:
        why = _safe(review["skim_why"])
        out.append(cvcheck._finding("skim", "look", f"Six-second skim {review['skim_score']}/5" + (f": {why}" if why else "")))
    return sorted(out, key=lambda f: (f["line"] is not None, f["line"] or 0))


def summary(review: dict, master_fit: int | None) -> dict:
    """The scores kept with the check for the panel: fit (and the master CV's), skim and why, counts."""
    return {
        "fit": review["fit_score"],
        "master_fit": master_fit,
        "skim": review["skim_score"],
        "skim_why": _safe(review["skim_why"]),
        "claims": len(review["claims"]),
        "unsourced": sum(1 for c in review["claims"] if c["source"] == "none"),
        "must_haves": len(review["must_haves"]),
        "covered": sum(1 for m in review["must_haves"] if m["covered"]),
    }


def save(conn: sqlite3.Connection, app_id: int, version: int, path: Path) -> dict:
    """Read the AI check in `path` for tailored version `version`, store its findings and scores (merged
    into the version's verdict) and return the stored check."""
    row = next((v for v in db.tailored_versions(conn, app_id) if v["version"] == version), None)
    if row is None:
        raise LookupError(f"no tailored CV v{version} on application {app_id}")
    review = read_eval(path)
    if review["version"] != version:
        raise ValueError(f"the file checks v{review['version']}, not v{version}")
    fit = db.cv_fit(conn, app_id)
    master_fit = fit["score"] if fit else None
    text = Path(row["file_path"]).read_text(encoding="utf-8")
    db.save_cv_eval(conn, app_id, version, findings(review, text, master_fit), summary(review, master_fit))
    return db.cv_check(conn, app_id, version)


def summary_line(found: dict | None) -> str:
    """One line for the CLI and the panel: "fit 4/5 (master CV 3/5) · skim 4/5 · 9 claims, 1 unsourced"."""
    s = (found or {}).get("summary")
    if not s:
        return ""
    master = f" (master CV {s['master_fit']}/5)" if s.get("master_fit") is not None else ""
    return (f"fit {s['fit']}/5{master} · skim {s['skim']}/5 · {s['claims']} claims, {s['unsourced']} unsourced"
            f" · {s['covered']}/{s['must_haves']} must-haves")
