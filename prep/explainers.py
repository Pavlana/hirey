"""Learn explainers: one private HTML page per card, shown under the card on the Learn page.

Files live under private_dir/learn/explainers/: `<card id>.html` (written by the Learn Assistant with
the /explainer skill) and `<card id>.json`, the sidecar this module writes (`record`): the getjo
commit the page was written from and, for every code snippet it quotes, the file, the function
(dotted qualname, "" for the whole file) and the sha256 of that code at the time.

`check` compares each snippet with getjo's working copy now. getjo is only read: its files, and
`git rev-parse HEAD` / `git log -1` (never a fetch, never a write). Paths are built from an int card id
and, for snippets, only accepted when they stay inside the getjo checkout.
"""

import ast
import hashlib
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from prep.config import Settings

log = logging.getLogger("prep.explainers")

FOLDER = Path("learn") / "explainers"
MAX_HTML = 2 * 1024 * 1024  # an explainer page larger than this is not served
STATES = ("none", "up_to_date", "changed", "missing", "unknown")


class SnippetError(ValueError):
    """A snippet names a file or function that is not in getjo's working copy."""


def folder(private_dir: Path | None) -> Path:
    if private_dir is None:
        raise ValueError("private_dir is not set, so explainers have nowhere to go")
    return Path(private_dir) / FOLDER


def html_path(private_dir: Path | None, card_id: int) -> Path:
    return folder(private_dir) / f"{int(card_id)}.html"


def sidecar_path(private_dir: Path | None, card_id: int) -> Path:
    return folder(private_dir) / f"{int(card_id)}.json"


def read_html(private_dir: Path | None, card_id: int) -> bytes | None:
    """The explainer page's bytes, or None when there is none (or it is too large to serve)."""
    if private_dir is None:
        return None
    path = html_path(private_dir, card_id)
    try:
        if not path.is_file() or path.stat().st_size > MAX_HTML:
            return None
        return path.read_bytes()
    except OSError:
        return None


def read_sidecar(private_dir: Path | None, card_id: int) -> dict | None:
    if private_dir is None:
        return None
    try:
        data = json.loads(sidecar_path(private_dir, card_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# --- getjo, read only ------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str | None:
    """A read-only git command in getjo's checkout (rev-parse, log); None when it fails."""
    try:
        out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=5,
                             env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})  # no index refresh writes
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def head(repo: Path | None) -> dict | None:
    """{"commit": full sha, "short": 7 chars, "date": YYYY-MM-DD} of getjo's HEAD, or None."""
    if repo is None:
        return None
    line = _git(repo, "log", "-1", "--format=%H %cs")
    if not line or " " not in line:
        return None
    commit, day = line.split(" ", 1)
    return {"commit": commit, "short": commit[:7], "date": day}


def _inside(repo: Path, rel: str) -> Path:
    """`rel` under the repo, or SnippetError (absolute paths, .. out of it, symlinks out of it)."""
    if not rel or Path(rel).is_absolute():
        raise SnippetError(f"snippet file must be a path inside getjo, got {rel!r}")
    root = repo.resolve()
    path = (root / rel).resolve()
    if path != root and root not in path.parents:
        raise SnippetError(f"snippet file must be a path inside getjo, got {rel!r}")
    return path


def _find(tree: ast.AST, qualname: str) -> ast.AST | None:
    """The function or class at dotted `qualname` (Class.method, outer.inner)."""
    node: ast.AST = tree
    for part in qualname.split("."):
        found = None
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and child.name == part:
                found = child
        if found is None:
            return None
        node = found
    return node


def snippet_text(repo: Path, rel: str, qualname: str = "") -> str:
    """The code a snippet points at now: a Python function or class located with ast (decorators
    included), or the whole file when `qualname` is empty. SnippetError when it is not there."""
    path = _inside(repo, rel)
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise SnippetError(f"{rel} is not in getjo") from e
    if not qualname:
        return source
    if path.suffix != ".py":
        raise SnippetError(f"a function can only be located in a Python file, not {rel}")
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise SnippetError(f"{rel} does not parse") from e
    node = _find(tree, qualname)
    if node is None:
        raise SnippetError(f"{qualname} is not in {rel}")
    lines = source.splitlines(keepends=True)
    first = min([node.lineno, *(d.lineno for d in getattr(node, "decorator_list", []))])
    return "".join(lines[first - 1:node.end_lineno])


def snippet_hash(repo: Path, rel: str, qualname: str = "") -> str:
    return hashlib.sha256(snippet_text(repo, rel, qualname).encode("utf-8")).hexdigest()


def parse_snippet(spec: str) -> tuple[str, str]:
    """"evals/run.py:to_label" -> ("evals/run.py", "to_label"); "README.md" -> ("README.md", "")."""
    rel, _, qualname = spec.partition(":")
    return rel.strip(), qualname.strip()


# --- the sidecar ------------------------------------------------------------------------------

def record(settings: Settings, card_id: int, snippets: list[tuple[str, str]], now: str) -> dict:
    """Write the sidecar for card `card_id`: getjo's HEAD and each snippet's hash as it is now.
    The HTML must exist already. SnippetError for a file or function getjo does not have."""
    if settings.getjo_repo is None:
        raise ValueError("getjo_repo is not set (and getjo_db's folder is not a git checkout)")
    if not html_path(settings.private_dir, card_id).is_file():
        raise ValueError(f"write the explainer first: {html_path(settings.private_dir, card_id)}")
    at = head(settings.getjo_repo)
    if at is None:
        raise ValueError(f"{settings.getjo_repo} is not a git checkout git can read")
    rows = [{"file": rel, "function": qn, "sha256": snippet_hash(settings.getjo_repo, rel, qn)}
            for rel, qn in snippets]
    data = {"card_id": int(card_id), "commit": at["commit"], "commit_date": at["date"], "written_at": now,
            "snippets": rows}
    path = sidecar_path(settings.private_dir, card_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    log.info("explainer sidecar for card %s recorded (%d snippets)", card_id, len(rows))
    return data


def label(snippet: dict) -> str:
    """"evals/run.py · to_label" (or the file alone)."""
    return snippet["file"] + (f" · {snippet['function']}" if snippet.get("function") else "")


def check(settings: Settings, card_id: int) -> dict:
    """Is card `card_id`'s explainer still true to getjo?

    {"state": none | up_to_date | changed | missing | unknown, "commit", "short", "date", "files",
     "changed": [labels whose code differs], "missing": [labels no longer found]}.
    none: no explainer page. unknown: a page without a readable sidecar, or no getjo checkout to compare with.
    missing wins over changed (a function gone or renamed)."""
    result = {"state": "none", "commit": "", "short": "", "date": "", "files": [], "changed": [], "missing": []}
    if settings.private_dir is None or not html_path(settings.private_dir, card_id).is_file():
        return result
    side = read_sidecar(settings.private_dir, card_id)
    if not side or not isinstance(side.get("snippets"), list):
        return {**result, "state": "unknown"}
    commit = str(side.get("commit", ""))
    snippets = [s for s in side["snippets"] if isinstance(s, dict) and s.get("file")]
    files = list(dict.fromkeys(str(s["file"]) for s in snippets))
    result.update(commit=commit, short=commit[:7], date=str(side.get("commit_date", "")), files=files)
    if settings.getjo_repo is None or not Path(settings.getjo_repo).is_dir():
        return {**result, "state": "unknown"}
    for s in snippets:
        try:
            now = snippet_hash(settings.getjo_repo, str(s["file"]), str(s.get("function", "")))
        except SnippetError:
            result["missing"].append(label(s))
            continue
        if now != s.get("sha256"):
            result["changed"].append(label(s))
    result["state"] = "missing" if result["missing"] else "changed" if result["changed"] else "up_to_date"
    return result


def cards_with_explainers(private_dir: Path | None) -> list[int]:
    """Card ids that have an explainer page, ascending."""
    if private_dir is None or not folder(private_dir).is_dir():
        return []
    return sorted(int(p.stem) for p in folder(private_dir).glob("*.html") if p.stem.isdigit())
