"""Learn notes: private Markdown files under private_dir/notes/learn/, one per card (`card-<id>.md`),
one general notebook (`all-prep.md`), and the user's own answer to each card (`answer-<id>.md`, the
card's Your answer box). The Learn page saves them; agents append to notes (`cli learn-notes ...
--append`), never rewrite, and only read answers.

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
_KEY = re.compile(r"^(?:all|(?:answer-)?[1-9][0-9]{0,9})$")

__all__ = ["ALL", "MAX_CHARS", "NotesChanged", "answer_key", "append", "essence", "is_answer", "label", "read", "save"]


def answer_key(card_id: int) -> str:
    """The key of a card's own answer (the Your answer box)."""
    return f"answer-{int(card_id)}"


def is_answer(key: str) -> bool:
    return str(key).startswith("answer-")


def _name(key: str) -> str:
    """The file name for `key`: "all", a card id (its note) or "answer-<card id>". ValueError
    otherwise (no path is ever built from raw input)."""
    key = str(key).strip()
    if not _KEY.match(key):
        raise ValueError(f"notes must be 'all', a card id or answer-<card id>; got {key!r}")
    if key == ALL:
        return "all-prep.md"
    return f"{key}.md" if is_answer(key) else f"card-{key}.md"


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


# Older Learn Assistant replies label extras "Bonus for the interview:" (now "Extra for the interview:").
# The pay check skips exactly that label at the start of a line, so such a reply can still be added;
# any other "bonus" is refused as before.
_OLD_LABEL = re.compile(r"^[ \t]*bonus for the interview:", re.IGNORECASE | re.MULTILINE)


def append(private_dir: Path | None, key: str, text: str, day: str | None = None, author: str = "Agent",
           by_user: bool = False) -> str:
    """Add a dated block ("**Agent · 18 Nov**", then the text) at the end; what is there stays as it is.
    Return the new stamp. The agent's text never mentions pay; `by_user` (the user's own words, from the
    Learn page's highlight box) skips that check, as the editor's save does."""
    if is_answer(key):
        raise ValueError("a card's answer is the user's own: agents read it (--show), never add to it")
    if not text.strip():
        raise ValueError("nothing to append")
    if not by_user:
        db.check_no_pay(_OLD_LABEL.sub("", text), "notes from the agent")
    block = db.notes_block(text, day or db.today_iso(), author)
    folder = _folder(private_dir)
    path = folder / _name(key)
    with _locked(folder):
        current = _read(path).rstrip()
        new = normalise(f"{current}\n\n{block}" if current else block)
        _check_length(new)
        _write(path, new)
    return stamp_of(new)


UNDERSTAND = "Understand:"
SAY_IT = "Say it:"


OLD_BETTER = "Better phrasing:"
OLD_SAY = "Say it in the interview:"
_LABEL_LINE = re.compile(r"^[A-Z][A-Za-z ]{1,40}:")  # "Covered:", "Missed:", "Say it:", …


def _unquote(line: str) -> str:
    """A line without the "> " of a Markdown quote."""
    return line.lstrip().lstrip(">").lstrip()


def _closing_block(lines: list[str]) -> str | None:
    """The last "Understand:" line that a "Say it:" line follows, through "Say it:" (see essence)."""
    starts = [i for i, line in enumerate(lines) if line.lstrip().startswith(UNDERSTAND)]
    for start in reversed(starts):
        say = next((j for j in range(start + 1, len(lines)) if lines[j].lstrip().startswith(SAY_IT)), None)
        if say is None:
            continue
        end = say + 1
        while end < len(lines) and lines[end].strip() and not lines[end].lstrip().startswith("—"):
            end += 1
        understand = "\n".join(line.rstrip() for line in lines[start:say]).strip()
        said = "\n".join(line.rstrip() for line in lines[say:end]).strip()
        if understand == UNDERSTAND or said == SAY_IT:
            continue  # a label with nothing after it
        # As written: the blank lines between Understand:, Fix: and Say it: and the code's indentation.
        return "\n".join(line.rstrip() for line in lines[start:end]).strip()
    return None


def _old_value(lines: list[str], label: str) -> str:
    """The text of the last `label` line in an older reply: the rest of that line, then the lines under
    it up to a blank line, a "—" footer or the next "Label:" line ("" when there is none)."""
    starts = [i for i, line in enumerate(lines) if _unquote(line).startswith(label)]
    if not starts:
        return ""
    i = starts[-1]
    parts = [_unquote(lines[i])[len(label):].strip()]
    j = i + 1
    if not parts[0]:
        while j < len(lines) and not lines[j].strip():
            j += 1  # the text starts on the next line
    while j < len(lines):
        line = _unquote(lines[j]).rstrip()
        if not line or line.startswith("—") or _LABEL_LINE.match(line):
            break
        parts.append(line)
        j += 1
    return "\n".join(part for part in parts if part).strip()


def essence(reply: str) -> str | None:
    """What Add to notes keeps of a Learn Assistant reply, or None when it has nothing short to keep
    (then nothing is added). In order:
    1. the closing "Understand:" / "Say it:" block, as written: the last "Understand:" line that a
       "Say it:" line follows wins, so labels quoted earlier are not taken; the block is kept as written
       (an optional "Fix:" part, blank lines and code indentation included) from "Understand:" through
       "Say it:", which runs to the first blank line, a "—" footer line or the end;
    2. an older reply: its "Better phrasing:" text as "Understand:" and its "Say it in the interview:"
       line as "Say it:" (either alone is enough)."""
    lines = reply.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    block = _closing_block(lines)
    if block:
        return block
    better, said = _old_value(lines, OLD_BETTER), _old_value(lines, OLD_SAY)
    kept = ([f"{UNDERSTAND} {better}"] if better else []) + ([f"{SAY_IT} {said}"] if said else [])
    return "\n".join(kept) or None
