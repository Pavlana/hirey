"""The master CV (private_dir/cv.md) and its saved versions.

    private_dir/cv.md                              the master CV, read by /research and /ask
    private_dir/cv-history/cv-YYYYMMDD-HHMMSS.md   one file per saved version (a state of cv.md)
    private_dir/cv-history/_draft.md               the agent's working copy (`cli cv draft`)

prep.db's cv_versions table holds each version's note (what changed into it) and its sha256, so
the current version is the one whose text matches cv.md. Every write to cv.md goes through
write_cv(): the text it replaces is saved as a version first (unless history already has it), the
new text is saved as a version with its note, then cv.md is replaced through a temp file and a
rename. Restoring a version is a write of that version's text, so it can be undone the same way.
None of these files go into git (prep/privacy.py); CV text and notes are never logged.
"""

import difflib
import hashlib
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from prep import db, research

HISTORY_DIR = "cv-history"
DRAFT_FILE = "_draft.md"
VERSION_NAME = re.compile(r"cv-\d{8}-\d{6}(?:-\d+)?\.md")
FOUND_NOTE = "Saved as found (changed outside the agent)"
FIRST_NOTE = "First saved copy"
DIFF_CONTEXT = 2


def cv_path(private_dir: Path) -> Path:
    return private_dir / research.CV_FILE


def history_dir(private_dir: Path) -> Path:
    return private_dir / HISTORY_DIR


def draft_path(private_dir: Path) -> Path:
    return history_dir(private_dir) / DRAFT_FILE


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_cv(private_dir: Path) -> str | None:
    """cv.md's text, or None when there is none."""
    path = cv_path(private_dir)
    return path.read_text(encoding="utf-8") if path.is_file() else None


def version_path(private_dir: Path | None, version: dict) -> Path | None:
    """A recorded version's file, only when its name has the version shape and it resolves inside
    private_dir (anything else is never read or served)."""
    if not private_dir or not VERSION_NAME.fullmatch(version["file_name"]):
        return None
    return research.inside(private_dir, str(history_dir(private_dir) / version["file_name"]))


def read_version(private_dir: Path, version: dict) -> str:
    path = version_path(private_dir, version)
    if path is None or not path.is_file():
        raise LookupError(f"the file of v{version['id']} is missing")
    return path.read_text(encoding="utf-8")


def history(conn: sqlite3.Connection, private_dir: Path) -> tuple[list[dict], int | None]:
    """(versions newest first, the id of the version cv.md matches or None after a hand edit)."""
    versions = db.cv_versions(conn)
    text = read_cv(private_dir)
    digest = sha256(text) if text is not None else None
    current = next((v["id"] for v in versions if v["sha256"] == digest), None)
    return versions, current


def _write_atomic(path: Path, text: str) -> None:
    """Write through a temp file in the same folder and a rename, so a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".cv-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _save_version(conn: sqlite3.Connection, private_dir: Path, text: str, note: str) -> int:
    """A new version file (cv-YYYYMMDD-HHMMSS.md, -2, -3… within one second) and its row."""
    stamp = db.now_iso()
    base = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).strftime("cv-%Y%m%d-%H%M%S")
    folder = history_dir(private_dir)
    folder.mkdir(parents=True, exist_ok=True)
    n = 1
    name = f"{base}.md"
    while (folder / name).exists():
        n += 1
        name = f"{base}-{n}.md"
    _write_atomic(folder / name, text)
    return db.add_cv_version(conn, name, note, sha256(text), stamp)


def write_cv(conn: sqlite3.Connection, private_dir: Path, text: str, note: str) -> dict:
    """The one way cv.md is written. Backup first: the text it replaces is saved as a version unless
    the latest version already holds it; then the new text is saved as a version with `note` and
    cv.md is replaced atomically. Refuses empty or unchanged text. Returns {"version": the new
    version's id, "previous": the id of the version holding the replaced text (None if there was no
    cv.md)}."""
    note = " ".join(note.split())
    if not note:
        raise ValueError("say in a short note what changed (--note)")
    if not text.strip():
        raise ValueError("the new CV is empty; cv.md was not changed")
    current = read_cv(private_dir)
    if current == text:
        raise ValueError("nothing changed: the new text is the same as cv.md")
    previous = None
    if current is not None:
        versions = db.cv_versions(conn)
        if versions and versions[0]["sha256"] == sha256(current):
            previous = versions[0]["id"]
        else:
            previous = _save_version(conn, private_dir, current, FOUND_NOTE if versions else FIRST_NOTE)
    version = _save_version(conn, private_dir, text, note)
    _write_atomic(cv_path(private_dir), text)
    return {"version": version, "previous": previous}


def restore(conn: sqlite3.Connection, private_dir: Path, version_id: int) -> dict:
    """Make version `version_id` cv.md again (write_cv, so the replaced text stays a version and the
    restore can be undone by restoring that one)."""
    version = db.cv_version(conn, version_id)
    if version is None:
        raise LookupError(f"no CV version v{version_id}")
    text = read_version(private_dir, version)
    if read_cv(private_dir) == text:
        raise ValueError(f"v{version_id} is already the current CV")
    return write_cv(conn, private_dir, text, f"Restored v{version_id} ({version['created_at'][:10]})")


def save_draft(conn: sqlite3.Connection, private_dir: Path, note: str, path: Path | None = None) -> dict:
    """Write the agent's edited copy (the draft unless `path`) to cv.md through write_cv; the
    draft is removed once saved."""
    source = path or draft_path(private_dir)
    if not source.is_file():
        raise LookupError(f"no file at {source}; run python -m prep.cli cv draft first")
    result = write_cv(conn, private_dir, source.read_text(encoding="utf-8"), note)
    if path is None:
        source.unlink()
    return result


def make_draft(private_dir: Path) -> Path:
    """Copy cv.md to the draft (replacing an older draft) and return its path."""
    text = read_cv(private_dir)
    if text is None:
        raise LookupError(f"no cv.md at {cv_path(private_dir)}")
    path = draft_path(private_dir)
    _write_atomic(path, text)
    return path


# --- reading the CV -------------------------------------------------------------------------

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
# "- **Cloud / Infra:** Azure, AWS" or "**Cloud:** …" or "- Cloud: Azure, AWS"
_THEMED = re.compile(r"^(?:[-*+]\s+)?(?:\*\*(?P<bold>[^*]+?)\*\*\s*:?|(?P<plain>[^:,*]{1,40}):)\s*(?P<rest>.*)$")


def _split_items(text: str) -> list[str]:
    """Split on , and ; outside parentheses; strip Markdown emphasis and trailing punctuation."""
    items, depth, word = [], 0, ""
    for ch in text:
        if ch in "([":
            depth += 1
        elif ch in ")]" and depth:
            depth -= 1
        if ch in ",;" and depth == 0:
            items.append(word)
            word = ""
        else:
            word += ch
    items.append(word)
    cleaned = (re.sub(r"[*_`]", "", i).strip().rstrip(".;:,").strip() for i in items)
    return [i for i in cleaned if i]


def skills(text: str) -> list[tuple[str, list[str]]]:
    """The skills section as (theme, skills) groups, in CV order. The section is the first heading
    containing "skill"; inside it a "- **Theme:** a, b; c" line, a "Theme: a, b" line or a "### Theme"
    sub-heading starts a group, and plain lines (a flat comma list) go to "Skills"."""
    groups: list[tuple[str, list[str]]] = []
    level = None
    theme = None
    for line in text.splitlines():
        heading = _HEADING.match(line.strip())
        if level is None:
            if heading and "skill" in heading.group(2).lower():
                level = len(heading.group(1))
            continue
        if heading:
            if len(heading.group(1)) <= level:
                break
            theme = heading.group(2).strip().rstrip(":")
            continue
        stripped = line.strip()
        if not stripped:
            continue
        m = _THEMED.match(stripped)
        if m and m.group("rest"):
            name = (m.group("bold") or m.group("plain")).strip().rstrip(":").strip()
            groups.append((name, _split_items(m.group("rest"))))
            continue
        items = _split_items(re.sub(r"^[-*+]\s+", "", stripped))
        name = theme or "Skills"
        if groups and groups[-1][0] == name:
            groups[-1][1].extend(items)
        else:
            groups.append((name, items))
    return [(name, items) for name, items in groups if items]


def diff(old: str, new: str, context: int = DIFF_CONTEXT) -> list[list[tuple[str, str]]]:
    """A line diff as hunks of (op, line), op " " (same), "-" (only in old) or "+" (only in new),
    with `context` unchanged lines around each change. Empty when the texts are the same."""
    a, b = old.splitlines(), new.splitlines()
    hunks = []
    for group in difflib.SequenceMatcher(None, a, b, autojunk=False).get_grouped_opcodes(context):
        hunk = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                hunk += [(" ", line) for line in a[i1:i2]]
                continue
            hunk += [("-", line) for line in a[i1:i2]]
            hunk += [("+", line) for line in b[j1:j2]]
        hunks.append(hunk)
    return hunks


def unified(old: str, new: str, old_name: str, new_name: str) -> str:
    """The same diff as unified diff text, for the CLI."""
    return "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                        old_name, new_name, n=DIFF_CONTEXT))
