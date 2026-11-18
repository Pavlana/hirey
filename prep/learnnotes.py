"""Learn notes: private Markdown files under private_dir/notes/learn/, one per card (`card-<id>.md`)
and one general notebook (`all-prep.md`). The Learn page's Notes editor saves them; agents append
(`cli learn-notes ... --append`), never rewrite.

A save names the stamp of the text it started from (a hash of the file, "" being the empty file); a
file that changed meanwhile (an agent append) is not overwritten: NotesChanged. Saves and appends hold
a lock on the folder, so the check and the write are one step. Text is never logged.
"""

import fcntl
import hashlib
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from prep import db
from prep.db import NotesChanged

FOLDER = Path("notes") / "learn"
ALL = "all"
MAX_CHARS = 60_000  # fits MAX_BODY (256 KB) once URL-encoded
_KEY = re.compile(r"^(?:all|[1-9][0-9]{0,9})$")

__all__ = ["ALL", "MAX_CHARS", "NotesChanged", "append", "label", "read", "save"]


def _name(key: str) -> str:
    """The file name for `key`: "all" or a card id. ValueError otherwise (no path from raw input)."""
    key = str(key).strip()
    if not _KEY.match(key):
        raise ValueError(f"notes must be 'all' or a card id; got {key!r}")
    return "all-prep.md" if key == ALL else f"card-{key}.md"


def label(key: str) -> str:
    """The file as shown on the page, relative to private_dir: notes/learn/card-3.md."""
    return (FOLDER / _name(key)).as_posix()


def _folder(private_dir: Path | None) -> Path:
    if private_dir is None:
        raise ValueError("private_dir is not set, so Learn notes have nowhere to go")
    return Path(private_dir) / FOLDER


def stamp_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def read(private_dir: Path | None, key: str) -> dict:
    """{"text": the note ("" if none yet), "stamp": its stamp, "file": notes/learn/...}."""
    path = _folder(private_dir) / _name(key)
    text = _read(path)
    return {"text": text, "stamp": stamp_of(text), "file": label(key)}


@contextmanager
def _locked(folder: Path) -> Iterator[None]:
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _write(path: Path, text: str) -> None:
    """Atomic: a temp file in the same folder, then a rename."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _check_length(text: str) -> None:
    if len(text) > MAX_CHARS:
        raise ValueError(f"a Learn note is limited to {MAX_CHARS:,} characters; got {len(text):,}")


def normalise(text: str) -> str:
    """Line ends as \\n (a form posts \\r\\n), no trailing blank lines, one final newline when not empty."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").rstrip()
    return text + "\n" if text else ""


def save(private_dir: Path | None, key: str, text: str, stamp: str) -> str:
    """Replace the note when `stamp` is still its stamp; return the new stamp. NotesChanged when it
    changed since (the text is not saved then)."""
    text = normalise(text)
    _check_length(text)
    folder = _folder(private_dir)
    path = folder / _name(key)
    with _locked(folder):
        if stamp_of(_read(path)) != stamp:
            raise NotesChanged("This note changed while you were editing (the agent may have added to it). "
                               "Your text is kept: compare it with the current note, then save again.")
        _write(path, text)
    return stamp_of(text)


def append(private_dir: Path | None, key: str, text: str, day: str | None = None, author: str = "Agent") -> str:
    """Add a dated block ("**Agent · 18 Nov**", then the text) at the end; what is there stays as it is.
    Return the new stamp. The agent's text never mentions pay."""
    if not text.strip():
        raise ValueError("nothing to append")
    db.check_no_pay(text, "notes from the agent")
    block = db.notes_block(text, day or db.today_iso(), author)
    folder = _folder(private_dir)
    path = folder / _name(key)
    with _locked(folder):
        current = _read(path).rstrip()
        new = normalise(f"{current}\n\n{block}" if current else block)
        _check_length(new)
        _write(path, new)
    return stamp_of(new)
