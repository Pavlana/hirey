"""The tailoring plan: a short plan for one job's CV, agreed before the CV is written.

    private_dir/cv-tailored/plans/PLAN_<Company>_<app id>.md    one per application (Redo plan rewrites it)

The agent that tailors writes it (`plan path`, `plan save`); the user reads it in the panel, edits it,
and presses Write the CV (`db.agree_tailor_plan`). Its two sections (27 Nov 2026):

    ## What the role needs          the 3-5 must-haves in the posting's words, each ✓ have / ~ partly / ✗ not
                                    yet with a few plain words (no evidence references or quotes)
    ## What will change in the CV   the summary in outline, the order of sections and bullets, what is cut

The agent still traces every claim to cv.md, the facts bank or _style.md, but that evidence is not shown.
A plan has no questions by default; one the agent truly needs goes to the Applications Assistant box. Plans
written before (four sections: What the role needs, Evidence, Order, Questions) still show and save, and
their numbered questions are still answered on an "Answer:" line (`plan answer`). Nothing here writes to the
facts bank (cv-facts.md) or stories.md: the agent adds a fact there only when the user says so. Wording
the user approves goes to cv-tailored/_style.md (add_style), dated and tagged with its application.
Plan text is never logged; pay is refused.
"""

import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from prep import cvfile, db, research

PLANS_DIR = "plans"
SECTIONS = ("What the role needs", "What will change in the CV")
OLD_SECTIONS = ("What the role needs", "Evidence", "Order", "Questions")  # plans written before 27 Nov 2026
STYLE_HEADING = "## Agreed wording"
MAX_PLAN = 20_000  # characters; a plan is about half a page

_ITEM = re.compile(r"^\s*(\d+)[.)]\s+(.*\S)\s*$")
_ANSWER = re.compile(r"^\s*(?:[-*]\s+)?\*{0,2}Answer:?\*{0,2}:?\s*(.*)$", re.IGNORECASE)
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")


@dataclass
class Question:
    number: int
    text: str
    answer: str
    line: int  # 0-based index of the question's line in the plan


def plan_file(private_dir: Path, company: str, app_id: int) -> Path:
    return private_dir / research.TAILORED_DIR / PLANS_DIR / f"PLAN_{research.company_word(company)}_{app_id}.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _section(lines: list[str], name: str) -> tuple[int, int] | None:
    """(first line after the heading, end) of the `## name` section, or None."""
    start = None
    for i, line in enumerate(lines):
        m = _HEADING.match(line)
        if not m:
            continue
        if start is not None:
            return start, i
        if m.group(1).strip().lower().startswith(name.lower()):
            start = i + 1
    return (start, len(lines)) if start is not None else None


def questions(text: str) -> list[Question]:
    """The numbered questions under `## Questions`, each with its answer ("" while unanswered): the text
    after "Answer:" and the indented lines that follow it."""
    lines = text.splitlines()
    found = _section(lines, "Questions")
    if found is None:
        return []
    out: list[Question] = []
    answering = False
    for i in range(*found):
        line = lines[i]
        item = _ITEM.match(line)
        if item:
            out.append(Question(int(item.group(1)), item.group(2).strip(), "", i))
            answering = False
            continue
        if not out:
            continue
        answer = _ANSWER.match(line)
        if answer and not answering:
            out[-1].answer = answer.group(1).strip()
            answering = True
        elif answering and line.strip():
            out[-1].answer = f"{out[-1].answer} {line.strip()}".strip()
        elif not line.strip():
            answering = False
    return out


def display(text: str) -> str:
    """The plan as Markdown the dashboard's subset shows well: numbered items as bullets ("- 2. …") and each
    question's answer as a sub-bullet ("Answer: …", or "Answer: not yet" while empty)."""
    lines = text.splitlines()
    found = _section(lines, "Questions")
    answers = {q.line: q.answer for q in questions(text)}
    out: list[str] = []
    for i, line in enumerate(lines):
        in_questions = found is not None and found[0] <= i < found[1]
        item = _ITEM.match(line)
        if item:
            out.append(f"- {item.group(1)}. {item.group(2).strip()}")
            if in_questions:
                answer = answers.get(i, "")
                out.append(f"  - **Answer:** {answer}" if answer else "  - **Answer:** *not yet*")
        elif in_questions and (_ANSWER.match(line) or (line.startswith((" ", "\t")) and line.strip())):
            continue  # shown as the sub-bullet above
        else:
            out.append(line)
    return "\n".join(out)


def check(text: str) -> str:
    """The plan's text, stripped; ValueError when it is empty, too long, mentions pay or misses a section."""
    text = text.replace("\r\n", "\n").strip()
    if not text:
        raise ValueError("the plan is empty")
    if len(text) > MAX_PLAN:
        raise ValueError(f"a plan is about half a page; this one has {len(text):,} characters (limit {MAX_PLAN:,})")
    db.check_no_pay(text, "the tailoring plan")
    lines = text.splitlines()
    missing = [s for s in SECTIONS if _section(lines, s) is None]
    if missing and all(_section(lines, s) is not None for s in OLD_SECTIONS):
        missing = []  # an older four-section plan, edited in the panel
    if missing:
        raise ValueError("the plan needs these sections: " + ", ".join(f"## {s}" for s in missing))
    return text + "\n"


def set_answer(text: str, number: int, answer: str) -> str:
    """The plan with question `number` answered (an existing answer is replaced)."""
    answer = " ".join(answer.split())
    if not answer:
        raise ValueError("the answer is empty")
    found = {q.number: q for q in questions(text)}
    if number not in found:
        raise LookupError(f"the plan has no question {number} (questions: {', '.join(map(str, found)) or 'none'})")
    lines = text.splitlines()
    q = found[number]
    end = q.line + 1
    while end < len(lines) and not _ITEM.match(lines[end]) and not _HEADING.match(lines[end]):
        end += 1
    body = [ln for ln in lines[q.line + 1:end]]
    start = next((i for i, ln in enumerate(body) if _ANSWER.match(ln)), None)
    if start is not None:  # drop the old answer and its continuation lines
        stop = start + 1
        while stop < len(body) and body[stop].strip():
            stop += 1
        body = body[:start] + body[stop:]
    body.insert(0, f"   Answer: {answer}")
    return "\n".join(lines[:q.line + 1] + body + lines[end:]) + "\n"


def save_text(conn: sqlite3.Connection, app_id: int, text: str, stamp: str | None) -> None:
    """The user's edit of the plan: written in place when `stamp` (the sha256 of the text it was opened
    on) still matches, else db.NotesChanged with nothing written."""
    plan = db.tailor_plan(conn, app_id)
    if plan is None:
        raise LookupError(f"application {app_id} has no tailoring plan yet")
    path = Path(plan["file_path"])
    if stamp is not None and cvfile.sha256(read(path)) != stamp:
        raise db.NotesChanged("The plan changed while you were editing (the assistant may have answered in it). "
                              "Your text is kept: compare it with the current plan, then save again.")
    cvfile._write_atomic(path, check(text))
    db.touch_tailor_plan(conn, app_id)


def _tag(app: dict, day: str) -> str:
    d = date.fromisoformat(day)
    return f"#{app['id']} {app['company']}, {d.day} {d.strftime('%b %Y')}"


def _append(path: Path, heading: str, lines: list[str]) -> None:
    text = read(path)
    if heading not in text.splitlines():
        text = (text.rstrip() + "\n\n" if text.strip() else "") + heading + "\n"
    cvfile._write_atomic(path, text.rstrip() + "\n" + "".join(f"{line}\n" for line in lines))


def add_style(private_dir: Path, app: dict | None, text: str, day: str | None = None) -> None:
    """Append wording the user approved to cv-tailored/_style.md, dated and tagged with its application."""
    text = " ".join(text.split())
    if not text:
        raise ValueError("nothing to add")
    db.check_no_pay(text, "agreed wording")
    d = date.fromisoformat(day or db.today_iso())
    tag = _tag(app, d.isoformat()) if app else f"{d.day} {d.strftime('%b %Y')}"
    _append(research.style_file(private_dir), STYLE_HEADING, [f"- {text} ({tag})"])
