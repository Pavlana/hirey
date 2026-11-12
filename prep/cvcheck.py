"""The automatic check of a tailored CV version: rule checks that need no AI, a verdict and a short
list of what changed against the master CV (cv.md). Runs after every `tailor save` (and `tailor pdf`,
`tailor check`); the panel's Tailored CV card shows the result.

Every finding is "block" (fix it before sending) or "look" (read this line), with the line number
in the version's .md file. The verdict follows from all findings (db.check_verdict): Blocked, Look
at these lines, or Ready. The rules store their findings as source "rules" (db.save_cv_check); an AI
check stores its own source on the same row and the verdict covers both.

Dates and claims may come from cv.md or from the facts bank, private_dir/cv-facts.md (true facts
not in the master CV). Findings may quote a CV line, but never one that mentions pay. None of this
is logged.
"""

import difflib
import re
import sqlite3
from pathlib import Path

from prep import cvfile, cvpdf, db, research

SOURCE = "rules"
LABELS = {"ready": "Ready", "look": "Look at these lines", "blocked": "Blocked"}
MAX_TEXT = 160  # characters of a flagged line kept with its finding
MIN_COMPANY = 3  # shorter company names are not searched for

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
URL = re.compile(r"(?:https?://|www\.)[^\s)>\]]+|\b(?:linkedin\.com|github\.com)/[^\s)>\]]+", re.IGNORECASE)
PHONE = re.compile(r"\+?\d[\d ().-]{7,}\d")
MD_LINK = re.compile(r"\[[^\]]*\]\([^)]*\)")
PLACEHOLDERS = (
    (re.compile(r"\[[^\]]*\]"), "a placeholder in brackets"),
    (re.compile(r"\b(?:TODO|TBD|TBC|FIXME|XXX+)\b"), "a placeholder"),
    (re.compile(r"\blorem ipsum\b", re.IGNORECASE), "placeholder text"),
    (re.compile(r"<[^<>\n]{1,40}>|\{\{[^}]*\}\}"), "a template placeholder"),
    (re.compile(r"\?\?"), "a question left in the text"),
)
YEAR = re.compile(r"\b(?:19[5-9]\d|20\d\d)\b")
RANGE = re.compile(r"\b((?:19|20)\d\d)\s*(?:–|—|-|to)\s*((?:19|20)\d\d|present|now|current|today)\b", re.IGNORECASE)


def facts_file(private_dir: Path) -> Path:
    return private_dir / research.FACTS_FILE


def body_lines(text: str) -> list[tuple[int, str]]:
    """(line number in the file, line) for the Markdown body, after any +++ front matter."""
    lines = text.lstrip("﻿").splitlines()
    start = 0
    if lines and lines[0].strip() == research.FENCE:
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == research.FENCE), None)
        start = end + 1 if end is not None else 0
    return [(i + 1, lines[i]) for i in range(start, len(lines))]


def _finding(check: str, severity: str, message: str, line: int | None = None, text: str = "") -> dict:
    text = text.strip()
    if db.PAY_PATTERN.search(text):  # a flagged line is shown; one that mentions pay never is
        text = ""
    return {"check": check, "severity": severity, "line": line, "message": message,
            "text": text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1] + "…"}


def _header(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """The lines before the first section (## heading): the name and contact details."""
    out = []
    for n, line in lines:
        if line.startswith("## "):
            break
        out.append((n, line))
    return out


def _norm_url(url: str) -> str:
    url = re.sub(r"^(?:https?://)?(?:www\.)?", "", url.lower().rstrip(".,;"))
    return url.rstrip("/")


def _contacts(text: str) -> set[tuple[str, str]]:
    """(kind, normalised value) for every email, link and phone number in `text`."""
    found = {("email", e.lower()) for e in EMAIL.findall(text)}
    found |= {("link", _norm_url(u)) for u in URL.findall(text) if "@" not in u}
    found |= {("phone", re.sub(r"\D", "", p)) for p in PHONE.findall(text) if len(re.sub(r"\D", "", p)) >= 9}
    return found


def check_pages(pages: int | None) -> list[dict]:
    if pages is None:
        return [_finding("pages", "look", "No PDF yet, so the page count is unknown: python -m prep.cli tailor pdf APP")]
    if pages > cvpdf.MAX_PAGES:
        return [_finding("pages", "block", f"The PDF has {pages} pages; keep it to {cvpdf.MAX_PAGES}")]
    return []


def check_contacts(lines: list[tuple[int, str]], master: str | None) -> list[dict]:
    """An email and a link in the CV, and every contact detail in cv.md's header kept."""
    body = "\n".join(line for _, line in lines)
    have = _contacts(body)
    out = []
    if not any(kind == "email" for kind, _ in have):
        out.append(_finding("contacts", "block", "No email address"))
    if not any(kind == "link" for kind, _ in have):
        out.append(_finding("contacts", "look", "No link (LinkedIn, GitHub or a site)"))
    if master:
        header = "\n".join(line for _, line in _header(body_lines(master)))
        for kind, value in sorted(_contacts(header) - have):
            out.append(_finding("contacts", "block", f"The {kind} in cv.md is missing: {value}"))
    return out


def check_placeholders(lines: list[tuple[int, str]]) -> list[dict]:
    out = []
    for n, line in lines:
        plain = MD_LINK.sub("", line)
        for pattern, what in PLACEHOLDERS:
            if pattern.search(plain):
                out.append(_finding("placeholder", "block", f"Line {n} has {what}", n, line))
                break
    return out


def check_pay(lines: list[tuple[int, str]], master: str | None) -> list[dict]:
    """A line that mentions pay, unless it is the user's own line from cv.md. The line is never quoted."""
    own = {line.strip() for line in (master or "").splitlines()}
    return [_finding("pay", "block", f"Line {n} mentions pay; take it out", n)
            for n, line in lines if db.PAY_PATTERN.search(line) and line.strip() not in own]


def check_dates(lines: list[tuple[int, str]], sources: str | None, this_year: int) -> list[dict]:
    """Ranges that run backwards, years in the future, and (with cv.md) years cv.md and the facts bank
    never mention: tailoring never changes dates."""
    known = set(YEAR.findall(sources)) if sources else None
    out = []
    for n, line in lines:
        problems = []
        for start, end in RANGE.findall(line):
            if end.isdigit() and int(start) > int(end):
                problems.append(f"{start} is after {end}")
        for year in YEAR.findall(line):
            if int(year) > this_year:
                problems.append(f"{year} is in the future")
            elif known is not None and year not in known:
                problems.append(f"{year} is not in cv.md or the facts bank")
        if problems:
            out.append(_finding("dates", "look", f"Line {n}: " + "; ".join(dict.fromkeys(problems)), n, line))
    return out


def _company_names(company: str) -> set[str]:
    words = company.split()
    while len(words) > 1 and re.sub(r"\W", "", words[-1].lower()) in db._COMPANY_SUFFIXES:
        words.pop()
    return {n for n in (company.strip(), " ".join(words)) if len(n) >= MIN_COMPANY}


def _mentions(name: str, text: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.IGNORECASE) is not None


def check_companies(lines: list[tuple[int, str]], company: str, others: list[str], master: str | None) -> list[dict]:
    """Another application's company named in this CV (left over from its version). Names in cv.md (a
    past employer) and names inside this application's company are not flagged."""
    names = set()
    for other in others:
        names |= {n for n in _company_names(other)
                  if not _mentions(n, company) and not (master and _mentions(n, master))}
    out = []
    for n, line in lines:
        hit = next((name for name in sorted(names, key=len, reverse=True) if _mentions(name, line)), None)
        if hit:
            out.append(_finding("company", "block", f"Line {n} names another application's company: {hit}", n, line))
    return out


def _sections(body: str) -> list[tuple[str, list[str]]]:
    """(## heading or "Header", its non-empty lines) in order."""
    sections = [("Header", [])]
    for line in body.splitlines():
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        elif line.strip():
            sections[-1][1].append(line.strip())
    return [s for s in sections if s[0] != "Header" or s[1]]


def changes(master: str | None, tailored: str) -> list[str]:
    """Short lines on what changed against cv.md: sections added, removed or moved, and per section
    how many lines were reworded, cut or added (or reordered)."""
    body = cvpdf.cv_body(tailored)
    words = research.word_count(body)
    if master is None:
        return [f"No cv.md to compare with; {words} words"]
    master_body = cvpdf.cv_body(master)
    old, new = dict(_sections(master_body)), dict(_sections(body))
    out = [f"Removed section: {name}" for name in old if name not in new]
    out += [f"Added section: {name}" for name in new if name not in old]
    common_old = [n for n in old if n in new]
    common_new = [n for n in new if n in old]
    if common_old != common_new:
        out.append("Sections reordered: " + ", ".join(n for n in common_new if n != "Header"))
    for name in common_new:
        a, b = old[name], new[name]
        if a == b:
            continue
        if sorted(a) == sorted(b):
            out.append(f"{name}: lines reordered")
            continue
        counts = {"reworded": 0, "cut": 0, "added": 0}
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
            if tag in ("replace", "delete"):
                counts["cut"] += max(0, (i2 - i1) - (j2 - j1))
            if tag in ("replace", "insert"):
                counts["added"] += max(0, (j2 - j1) - (i2 - i1))
            if tag == "replace":
                counts["reworded"] += min(i2 - i1, j2 - j1)
        parts = [f"{v} {k}" for k, v in counts.items() if v]
        parts[0] = parts[0].replace(" ", f" line{'s' if parts[0].split()[0] != '1' else ''} ", 1)
        out.append(f"{name}: " + ", ".join(parts))
    if not out:
        out.append("Same text as cv.md")
    return out + [f"{words} words (cv.md: {research.word_count(master_body)})"]


def check(text: str, company: str, others: list[str], master: str | None, facts: str | None,
          pages: int | None, this_year: int) -> list[dict]:
    """Every rule finding for a tailored CV's text (front matter skipped), in line order."""
    lines = body_lines(text)
    sources = "\n".join(t for t in (master, facts) if t) or None
    findings = (check_pages(pages) + check_contacts(lines, master) + check_placeholders(lines)
                + check_pay(lines, master) + check_dates(lines, sources if master else None, this_year)
                + check_companies(lines, company, others, master))
    return sorted(findings, key=lambda f: (f["line"] is not None, f["line"] or 0))


def run(conn: sqlite3.Connection, private_dir: Path | None, app_id: int, version: int,
        pages: int | None) -> dict:
    """Check tailored version `version` of an application, store the rule findings and the changes,
    and return the stored check (with the verdict). `pages`: the PDF's page count, None without a PDF."""
    app = db.get_application(conn, app_id)
    row = next((v for v in db.tailored_versions(conn, app_id) if v["version"] == version), None)
    if app is None or row is None:
        raise LookupError(f"no tailored CV v{version} on application {app_id}")
    text = Path(row["file_path"]).read_text(encoding="utf-8")
    master = cvfile.read_cv(private_dir) if private_dir else None
    facts_path = facts_file(private_dir) if private_dir else None
    facts = facts_path.read_text(encoding="utf-8") if facts_path and facts_path.is_file() else None
    others = [a["company"] for a in db.list_applications(conn) if a["id"] != app_id]
    findings = check(text, app["company"], others, master, facts, pages, int(db.today_iso()[:4]))
    db.save_cv_check(conn, app_id, version, SOURCE, findings, changes(master, text))
    return db.cv_check(conn, app_id, version)
