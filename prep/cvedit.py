"""Editing a CV in the panel: the latest tailored version in place, and the master CV through cv save.

Tailored CV (save_tailored, undo_tailored):
- The editor holds the version's Markdown body (its +++ front matter is kept as it is) and saves it in
  place, as many times as needed. Each save first copies the text it replaces to
  private_dir/cv-tailored/edits/<version stem>_e<n>.md and records it (db.tailored_cv_edits), so Undo
  brings it back; the text Undo replaces is kept beside that copy (.undone.md). Nothing is deleted.
- A save remakes the PDF and runs the rule check again; an AI check done before it is marked out of date.
- The version marked as sent is frozen: a save of it becomes the next version, so the text the company
  saw is kept.
- Every save names the stamp (sha256 of the file) it was opened on; a file changed meanwhile is refused
  (db.NotesChanged) and nothing is written.

Master CV (save_master): the same editor, saved through cvfile.write_cv (a backup version first).

The AI check's editorial changes (apply_ai_changes): the ticked ones replace their quoted words with the suggestion
(exact match, first occurrence on its line) in one save_tailored with author "ai-check", so one Undo brings the text
back; unticked ones are declined. Each change is marked applied, declined or skipped (its words are gone) in the AI
check's summary, so it does not show again for that version, and the rest of the AI check is kept (not out of date:
only the applied words changed, on the same lines).

Comments for the agent: a line starting with "%%" in the editor is a comment on the line above it
("%% make this sound less junior"). Comments are split off before anything is written (split_comments)
and kept in db.cv_comments, so they never reach the file, the PDF or the checks; the editor and the
agent's draft show them again under their line (with_comments). Removing a comment line handles it.
CV text and comments are never logged.
"""

import shutil
import sqlite3
from pathlib import Path

from prep import cvcheck, cvfile, cvpdf, db, research
from prep.config import Settings, facts_path

COMMENT = "%%"
EDITS_DIR = "edits"
DRAFTS_DIR = "drafts"
MAX_CV = 60_000  # characters in the editor


def is_comment(line: str) -> bool:
    return line.lstrip().startswith(COMMENT)


def split_comments(text: str) -> tuple[str, list[tuple[str, str]]]:
    """(the text without its comment lines, [(anchor, comment)]): the anchor is the nearest non-blank
    line above the comment ("" at the top). Empty comments are dropped."""
    kept: list[str] = []
    comments: list[tuple[str, str]] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if is_comment(line):
            body = " ".join(line.lstrip()[len(COMMENT):].split())
            if body:
                anchor = next((k.strip() for k in reversed(kept) if k.strip()), "")
                comments.append((anchor, body))
            continue
        kept.append(line)
    return "\n".join(kept), comments


def with_comments(text: str, comments: list[dict]) -> str:
    """The text with each comment back under its anchor line (the first match after the previous comment's
    place, else the first match anywhere); a comment whose line is gone goes to the top."""
    lines = text.split("\n")
    after: dict[int, list[str]] = {}
    top: list[str] = []
    last = -1
    for c in comments:
        anchor = c["anchor"]
        spot = None
        if anchor:
            spot = next((i for i in range(last + 1, len(lines)) if lines[i].strip() == anchor), None)
            if spot is None:
                spot = next((i for i in range(len(lines)) if lines[i].strip() == anchor), None)
        line = f"{COMMENT} {c['body']}"
        if spot is None:
            top.append(line)
        else:
            after.setdefault(spot, []).append(line)
            last = spot
    out = top[:]
    for i, line in enumerate(lines):
        out.append(line)
        out.extend(after.get(i, []))
    return "\n".join(out)


def split_file(text: str) -> tuple[str, str]:
    """(front matter block with its fences and the line after, body) of a tailored file; ("", text) without."""
    lines = text.split("\n")
    if lines and lines[0].strip() == research.FENCE:
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == research.FENCE), None)
        if end is not None:
            rest = lines[end + 1:]
            while rest and not rest[0].strip():
                rest = rest[1:]
            return "\n".join(lines[:end + 1]) + "\n\n", "\n".join(rest)
    return "", text


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > MAX_CV:
        raise ValueError(f"a CV is at most {MAX_CV:,} characters; this one has {len(text):,}")
    return text.strip("\n") + "\n"


# --- the tailored CV -------------------------------------------------------------------------------

def latest(conn: sqlite3.Connection, app_id: int) -> tuple[dict, Path]:
    versions = db.tailored_versions(conn, app_id)
    if not versions:
        raise LookupError(f"application {app_id} has no tailored CV yet")
    path = Path(versions[-1]["file_path"])
    if not path.is_file():
        raise LookupError(f"the file of tailored v{versions[-1]['version']} is missing")
    return versions[-1], path


def editor_text(conn: sqlite3.Connection, app_id: int) -> dict:
    """What the editor opens on: {"text": body with comments, "stamp": sha256 of the file, "version"}."""
    version, path = latest(conn, app_id)
    text = path.read_text(encoding="utf-8")
    _, body = split_file(text)
    return {"text": with_comments(body.rstrip("\n"), db.cv_comments(conn, app_id)), "stamp": cvfile.sha256(text),
            "version": version["version"]}


def _backup(path: Path, n: int) -> Path:
    folder = path.parent / EDITS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{path.stem}_e{n}.md"
    while out.exists():
        n += 1
        out = folder / f"{path.stem}_e{n}.md"
    return out


def refresh(settings: Settings, conn: sqlite3.Connection, app_id: int, version: int, path: Path) -> dict:
    """After the text of a version changed: the PDF again (when WeasyPrint is reachable), the rule check
    again and the AI check marked out of date. Returns {"pages", "verdict", "pdf_error", "ai_stale"}."""
    pages, error = None, ""
    try:
        _, pages = cvpdf.write_pdf(path, settings.pdf_python)
    except cvpdf.PdfUnavailable:
        error = "no PDF: WeasyPrint is not reachable"
        stale = cvpdf.pdf_path(path)
        stale.unlink(missing_ok=True)  # a PDF of the old text must not be sent by mistake
    except Exception as e:  # the text is saved; tailor pdf retries
        error = f"no PDF: the render failed ({type(e).__name__})"
        cvpdf.pdf_path(path).unlink(missing_ok=True)
    stale = db.mark_ai_check_stale(conn, app_id, version)
    found = cvcheck.run(conn, settings.private_dir, app_id, version, pages, facts_path(settings))
    return {"pages": pages, "verdict": found["verdict"], "pdf_error": error, "ai_stale": stale}


def save_tailored(settings: Settings, conn: sqlite3.Connection, app_id: int, version: int, text: str,
                  stamp: str | None, author: str = "user") -> dict:
    """Save the editor's text over tailored `version` (the latest). `stamp` None skips the stale check
    (never from the panel). Returns {"version", "new_version" (the sent one was frozen), "changed",
    "comments", "pages", "verdict", "pdf_error"}."""
    current, path = latest(conn, app_id)
    if current["version"] != version:
        raise db.NotesChanged(f"v{current['version']} was saved while you were editing v{version}. Your text is kept: "
                              "compare it with the current version, then save again.")
    old = path.read_text(encoding="utf-8")
    if stamp is not None and cvfile.sha256(old) != stamp:
        raise db.NotesChanged("This CV changed while you were editing (the assistant may have revised it). Your text "
                              "is kept: compare it with the current version, then save again.")
    body, comments = split_comments(_clean(text))
    if not body.strip():
        raise ValueError("the CV is empty; nothing saved")
    front, _ = split_file(old)
    new = front + body.strip("\n") + "\n"
    db.replace_cv_comments(conn, app_id, comments)
    result = {"version": version, "new_version": False, "changed": new != old, "comments": len(comments),
              "pages": None, "verdict": None, "pdf_error": ""}
    if new == old:
        return result
    app = db.get_application(conn, app_id)
    if app.get("cv_sent_version") == version:  # frozen: the edit becomes the next version
        target = research.tailored_file(settings.private_dir, app["company"], app["role_short"] or "CV",
                                        db.today_iso(), version + 1)
        if target.exists():
            raise ValueError(f"{target.name} already exists; nothing saved")
        cvfile._write_atomic(target, new)
        result["version"] = db.save_tailored_cv(conn, app_id, str(target), None)
        result["new_version"] = True
        path = target
    else:
        backup = _backup(path, len(db.cv_edits(conn, app_id, version)) + 1)
        shutil.copyfile(path, backup)
        db.add_cv_edit(conn, app_id, version, str(backup), author)
        cvfile._write_atomic(path, new)
    result.update(refresh(settings, conn, app_id, result["version"], path))
    return result


def undo_tailored(settings: Settings, conn: sqlite3.Connection, app_id: int) -> dict:
    """Bring back the text the latest version had before its newest not-yet-undone edit. The text Undo
    replaces is kept beside that backup (.undone.md). Returns {"version", "left" (edits still undoable),
    "pages", "verdict", "pdf_error"}."""
    current, path = latest(conn, app_id)
    edit = db.last_undoable_edit(conn, app_id, current["version"])
    if edit is None:
        raise ValueError(f"nothing to undo on v{current['version']}")
    backup = research.inside(settings.private_dir, edit["backup_path"])
    if backup is None or not backup.is_file():
        raise LookupError("the saved copy of the earlier text is missing")
    shutil.copyfile(path, backup.with_suffix(".undone.md"))
    cvfile._write_atomic(path, backup.read_text(encoding="utf-8"))
    db.mark_edit_undone(conn, edit["id"])
    left = sum(1 for e in db.cv_edits(conn, app_id, current["version"]) if not e["undone_at"])
    return {"version": current["version"], "left": left, **refresh(settings, conn, app_id, current["version"], path)}


def _replace_in(lines: list[str], change: dict) -> bool:
    """Replace the change's quote with its suggestion in `lines` (the editor text: body and %% comments): on
    the line it was found on when that line is unchanged, else on the first line that holds the quote.
    False when no line holds it any more."""
    quote = change["quote"]
    spots = [i for i, line in enumerate(lines) if quote in line and not is_comment(line)]
    if not spots:
        return False
    i = next((i for i in spots if lines[i].strip() == change.get("text")), spots[0])
    lines[i] = lines[i].replace(quote, change["suggestion"], 1)
    return True


def apply_ai_changes(settings: Settings, conn: sqlite3.Connection, app_id: int, version: int, stamp: str | None,
                     ticked: set[int]) -> dict:
    """Apply the AI check's ticked editorial changes (`ticked`: their numbers) to the latest version in one
    save (save_tailored, author "ai-check"); the other open changes are declined. Returns {"version",
    "new_version", "applied", "declined", "skipped", "pdf_error"}."""
    current, path = latest(conn, app_id)
    if current["version"] != version:
        raise db.NotesChanged(f"v{current['version']} was saved after this AI check was shown; nothing was changed.")
    check = db.cv_check(conn, app_id, version)
    summary = (check or {}).get("summary") or {}
    if not summary or summary.get("stale"):
        raise ValueError(f"v{version} has no up-to-date AI check, so there are no changes to apply")
    edits = [dict(e) for e in summary.get("edits") or []]
    open_ = [e for e in edits if e["state"] == "open"]
    if not open_:
        raise ValueError("there are no open changes from the AI check")
    current_text = editor_text(conn, app_id)
    if stamp is not None and current_text["stamp"] != stamp:
        raise db.NotesChanged("This CV changed after the AI check was shown (an edit or the assistant); nothing was "
                              "changed. Look at the changes again, then apply.")
    lines = current_text["text"].split("\n")
    for e in open_:
        if e["n"] not in ticked:
            e["state"] = "declined"
        else:
            e["state"] = "applied" if _replace_in(lines, e) else "skipped"
    counts = {k: sum(1 for e in open_ if e["state"] == k) for k in ("applied", "declined", "skipped")}
    result = {"version": version, "new_version": False, "pdf_error": "", **counts}
    if counts["applied"]:
        saved = save_tailored(settings, conn, app_id, version, "\n".join(lines), current_text["stamp"],
                              author="ai-check")
        result.update(version=saved["version"], new_version=saved["new_version"], pdf_error=saved["pdf_error"])
    handled = {e["n"] for e in open_}
    target = latest(conn, app_id)[1]
    now = dict(cvcheck.body_lines(target.read_text(encoding="utf-8")))
    kept = []
    for f in check["findings"]:
        if f["source"] != "ai" or f.get("edit") in handled:
            continue
        if f["line"] in now and f["text"] and not db.PAY_PATTERN.search(now[f["line"]]):
            f = {**f, "text": cvcheck._finding(f["check"], f["severity"], "", f["line"], now[f["line"]])["text"]}
        kept.append(f)
    db.save_cv_check(conn, app_id, result["version"], "ai", kept, summary={**summary, "edits": edits, "stale": False})
    return result


def draft_path(path: Path) -> Path:
    return path.parent / DRAFTS_DIR / f"{path.stem}.draft.md"


def make_draft(conn: sqlite3.Connection, app_id: int) -> tuple[Path, dict]:
    """The agent's working copy of the latest version (`tailor draft`): front matter, then the body with
    the open comments under their lines; its stamp is kept beside it (.stamp) for `tailor edit`."""
    version, path = latest(conn, app_id)
    text = path.read_text(encoding="utf-8")
    front, body = split_file(text)
    draft = draft_path(path)
    cvfile._write_atomic(draft, front + with_comments(body.rstrip("\n"), db.cv_comments(conn, app_id)) + "\n")
    cvfile._write_atomic(draft.with_suffix(".stamp"), cvfile.sha256(text))
    return draft, version


def save_draft(settings: Settings, conn: sqlite3.Connection, app_id: int, draft: Path | None = None) -> dict:
    """`tailor edit`: save the agent's draft over the latest version in place (save_tailored, author agent),
    refused when the version changed since the draft was made. The draft is removed once saved."""
    version, path = latest(conn, app_id)
    source = draft or draft_path(path)
    if not source.is_file():
        raise LookupError(f"no draft at {source}; run python -m prep.cli tailor draft {app_id} first")
    stamp_file = source.with_suffix(".stamp")
    stamp = stamp_file.read_text(encoding="utf-8").strip() if stamp_file.is_file() else None
    _, body = split_file(source.read_text(encoding="utf-8"))
    result = save_tailored(settings, conn, app_id, version["version"], body, stamp, author="agent")
    if draft is None:
        source.unlink(missing_ok=True)
        stamp_file.unlink(missing_ok=True)
    return result


# --- the master CV ---------------------------------------------------------------------------------

def master_text(conn: sqlite3.Connection, private_dir: Path) -> dict:
    """The My CV editor's text (cv.md with the master CV's comments) and stamp (sha256 of cv.md)."""
    text = cvfile.read_cv(private_dir)
    if text is None:
        raise LookupError(f"no cv.md at {cvfile.cv_path(private_dir)}")
    return {"text": with_comments(text.rstrip("\n"), db.cv_comments(conn, None)), "stamp": cvfile.sha256(text)}


def save_master(conn: sqlite3.Connection, private_dir: Path, text: str, stamp: str | None, note: str) -> dict:
    """Save the My CV editor: comments to db.cv_comments, the text through cvfile.write_cv (the replaced
    text kept as a version first). Returns {"version", "previous" (None when only comments changed),
    "comments"}."""
    current = cvfile.read_cv(private_dir)
    if current is None:
        raise LookupError(f"no cv.md at {cvfile.cv_path(private_dir)}")
    if stamp is not None and cvfile.sha256(current) != stamp:
        raise db.NotesChanged("cv.md changed while you were editing (the assistant may have saved a change). Your "
                              "text is kept: compare it with the current CV, then save again.")
    body, comments = split_comments(_clean(text))
    if not body.strip():
        raise ValueError("the CV is empty; cv.md was not changed")
    db.replace_cv_comments(conn, None, comments)
    if body == current or body.strip() == current.strip():
        return {"version": None, "previous": None, "comments": len(comments)}
    return {**cvfile.write_cv(conn, private_dir, body, note or "Edited in the panel"), "comments": len(comments)}
