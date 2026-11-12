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
# <https://…> or <me@example.org>: prep/md.py does not turn these into links, so the PDF prints the brackets.
AUTOLINK = re.compile(r"<(?:https?://|www\.)[^<>\s]+>|<[^<>\s]+@[^<>\s]+>", re.IGNORECASE)
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
        if AUTOLINK.search(plain):
            out.append(_finding("placeholder", "look", f"Line {n} has a link in <…>, printed with its brackets; "
                                "write it as [text](url) or plain text", n, line))
            plain = AUTOLINK.sub("", plain)
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


def check_companies(lines: list[tuple[int, str]], company: str, others: list[str], sources: str | None) -> list[dict]:
    """Another application's company named in this CV (left over from its version). Names in cv.md or
    the facts bank (`sources`: a past employer, a tool) and names inside this company's are not flagged."""
    names = set()
    for other in others:
        names |= {n for n in _company_names(other)
                  if not _mentions(n, company) and not (sources and _mentions(n, sources))}
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
    gaps = _terms(front_matter(tailored), "gaps")
    return out + [f"{words} words (cv.md: {research.word_count(master_body)})"] + (
        [f"Not claimed (gaps): {', '.join(gaps)}"] if gaps else [])


# Job terms: recruiter systems match the posting's words literally. The tailored file's front matter
# lists the job's must-have terms (keywords) in the posting's wording, and the ones the CV cannot
# truthfully claim (gaps); every other keyword must appear in the body as written.
SENIORITY = {"senior", "sr", "junior", "jr", "mid", "mid-level", "graduate", "intern"}
TITLE_SPLIT = re.compile(r"\s*(?:,|\(|\s[-–—|]\s|/|\bat\b)\s*", re.IGNORECASE)
# Words and habits that make a CV read as machine-written to a recruiter. Only lines the agent wrote
# (not the user's own lines from cv.md) are looked at; every finding is a look, never a block.
STOCK_PHRASES = (
    "spearheaded", "spearheading", "leveraged", "leveraging", "passionate", "results-driven", "dynamic",
    "seamless", "seamlessly", "robust", "delve", "delved", "fast-paced", "cutting-edge", "synergy",
    "synergies", "proven track record", "detail-oriented", "team player", "self-starter", "go-getter",
    "innovative", "transformative", "world-class", "best-in-class", "thought leader", "utilize", "utilized",
    "utilise", "utilised", "showcasing", "a testament to", "tapestry", "in today's", "ever-evolving",
    "game-changer", "game-changing", "holistic", "meticulous", "meticulously", "adept at", "pivotal",
    "championed", "fostered", "realm",
)
STOCK = re.compile(r"(?<![\w-])(?:" + "|".join(re.escape(w) for w in STOCK_PHRASES) + r")(?![\w-])", re.IGNORECASE)
BOLD = re.compile(r"\*\*[^*]+\*\*")
SENTENCE_DASH = re.compile(r"—|\s–\s")
MIN_OPENER_REPEATS = 3
MIN_BULLETS_FOR_NUMBERS = 4


def front_matter(text: str) -> dict:
    try:
        return research.split_front_matter(text)[0]
    except ValueError:
        return {}


def _terms(front: dict, key: str) -> list[str] | None:
    """A list of strings from the front matter, [] when absent, None when it is not a list of strings."""
    value = front.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None
    return [v.strip() for v in value if v.strip()]


def check_keywords(lines: list[tuple[int, str]], front: dict) -> list[dict]:
    """Every must-have term (front matter `keywords`, less `gaps`) in the body, in the job's wording."""
    keywords, gaps = _terms(front, "keywords"), _terms(front, "gaps")
    if keywords is None or gaps is None:
        return [_finding("keywords", "look", "keywords and gaps in the front matter must be lists of strings")]
    if not keywords:
        return [_finding("keywords", "look", "No keywords in the front matter: the job's must-have terms "
                         "were not checked")]
    body = "\n".join(line for _, line in lines)
    skip = {g.lower() for g in gaps}
    missing = [k for k in keywords if k.lower() not in skip and not _mentions(k, body)]
    if not missing:
        return []
    return [_finding("keywords", "look", "Must-have terms not in the CV in the job's wording: "
                     + ", ".join(missing) + " (true: add them as written; not true: list them under gaps)")]


def title_core(role: str) -> str:
    """The job title without seniority or what follows a comma, dash or bracket:
    "Senior Platform Engineer, Data" -> "Platform Engineer"."""
    words = TITLE_SPLIT.split(role.strip(), maxsplit=1)[0].split()
    while len(words) > 1 and words[0].lower().rstrip(".") in SENIORITY:
        words.pop(0)
    return " ".join(words)


def check_title(lines: list[tuple[int, str]], role: str) -> list[dict]:
    """The job's title near the top (the name lines or the first section), where a skim starts."""
    core = title_core(role)
    if not core:
        return []
    top, sections = [], 0
    for _, line in lines:
        if line.startswith("## "):
            sections += 1
            if sections > 1:
                break
        top.append(line)
    if _mentions(core, "\n".join(top)):
        return []
    return [_finding("title", "look", f"The job title ({core}) is not in the name line or the first section")]


def _written(lines: list[tuple[int, str]], master: str | None) -> list[tuple[int, str]]:
    """Body lines the agent wrote: not blank, not a heading, not one of the user's own lines from cv.md."""
    own = {line.strip() for line in (master or "").splitlines()}
    return [(n, line) for n, line in lines
            if line.strip() and not line.lstrip().startswith("#") and line.strip() not in own]


def check_style(lines: list[tuple[int, str]], master: str | None) -> list[dict]:
    """Signs a recruiter reads as machine-written, in the lines the agent wrote: stock words, dashes in
    sentences, many bullets starting with the same word, and rewritten bullets with no number."""
    written = _written(lines, master)
    out = []
    for n, line in written:
        words = list(dict.fromkeys(w.lower() for w in STOCK.findall(line)))
        if words:
            out.append(_finding("style", "look", f"Line {n}: stock words ({', '.join(words)}); say what was "
                                "done instead", n, line))
    dashed = [n for n, line in written if SENTENCE_DASH.search(BOLD.sub("", line))]
    if len(dashed) >= 2:
        out.append(_finding("style", "look", f"Dashes in {len(dashed)} written lines (lines "
                            + ", ".join(map(str, dashed)) + "); use commas or full stops", dashed[0]))
    bullets = [(n, line.lstrip()[2:].strip()) for n, line in written if line.lstrip()[:2] in ("- ", "* ")]
    openers: dict[str, list[int]] = {}
    for n, text in bullets:
        first = re.sub(r"[^\w-]", "", BOLD.sub("", text).split(" ", 1)[0]).lower()
        if first:
            openers.setdefault(first, []).append(n)
    for word, at in openers.items():
        if len(at) >= MIN_OPENER_REPEATS:
            out.append(_finding("style", "look", f"{len(at)} bullets start with \"{word}\" (lines "
                                + ", ".join(map(str, at)) + "); vary the verbs", at[0]))
    if len(bullets) >= MIN_BULLETS_FOR_NUMBERS and not any(re.search(r"\d", t) for _, t in bullets):
        out.append(_finding("style", "look", f"None of the {len(bullets)} rewritten bullets has a number; "
                            "ask for honest figures", bullets[0][0]))
    return out


def check(text: str, company: str, others: list[str], master: str | None, facts: str | None,
          pages: int | None, this_year: int, role: str = "") -> list[dict]:
    """Every rule finding for a tailored CV's text (front matter skipped), in line order."""
    lines = body_lines(text)
    sources = "\n".join(t for t in (master, facts) if t) or None
    findings = (check_pages(pages) + check_contacts(lines, master) + check_placeholders(lines)
                + check_pay(lines, master) + check_dates(lines, sources if master else None, this_year)
                + check_companies(lines, company, others, sources) + check_keywords(lines, front_matter(text))
                + check_title(lines, role) + check_style(lines, master))
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
    findings = check(text, app["company"], others, master, facts, pages, int(db.today_iso()[:4]), app["role"])
    db.save_cv_check(conn, app_id, version, SOURCE, findings, changes(master, text))
    return db.cv_check(conn, app_id, version)
