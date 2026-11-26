"""Keep the docs in step with the code.

Two kinds of check, both standard library only:

- Coverage (`check_repo`): facts that live in the code must appear on the reference page that owns
  them: every dashboard route in docs/reference/routes.md, every setting and env var in
  configuration.md, every table and migrated column in database.md, every CLI command in
  docs/usage.md, every skill in assistants.md. Plus every relative link and anchor in the Markdown
  resolves, every image a doc shows exists, and no screenshot sits unused.
- Changed code (`check_changes`): docs/docs-map.json says which doc pages own which code. A change to
  mapped code must come with a change to one of its pages, unless the pull request says why not with a
  line "Docs: not needed, <reason>".

Run: `python -m prep.docs_check` (coverage), `python -m prep.docs_check --changed origin/main`
(coverage and the changed-code rule, as CI does). Exit 1 lists every problem.
"""

import argparse
import ast
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS_MAP = "docs/docs-map.json"
SCREENSHOTS = "docs/screenshots"
NOT_NEEDED = re.compile(r"^\s*Docs:\s*not needed\b\s*[,:—-]?\s*(\S.*)?$", re.IGNORECASE | re.MULTILINE)


def _read(root: Path, rel: str) -> str:
    path = root / rel
    return path.read_text(encoding="utf-8") if path.exists() else ""


# --- facts from the code -------------------------------------------------------------------------

def routes(root: Path) -> set[str]:
    """Every path prep/web.py answers: `path == "/x"`, `path.startswith("/x/")`, `path in ("/a", "/b")`,
    the keys of the `pages` dict and of ACTIONS. Static files are left out."""
    tree = ast.parse(_read(root, "prep/web.py"))
    found: set[str] = set()

    def is_path(node: ast.AST) -> bool:
        return isinstance(node, ast.Name) and node.id == "path"

    def add(node: ast.AST) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("/"):
            found.add(node.value)
        elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            for elt in node.elts:
                add(elt)
        elif isinstance(node, ast.Name):  # path in OLD_OFFERS
            for top in tree.body:
                if isinstance(top, ast.Assign) and any(isinstance(t, ast.Name) and t.id == node.id for t in top.targets):
                    add(top.value)

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and is_path(node.left):
            for comparator in node.comparators:
                add(comparator)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == "startswith" and is_path(node.func.value)):
            for arg in node.args:
                add(arg)
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pages" for t in node.targets) \
                and isinstance(node.value, ast.Dict):
            for key in node.value.keys:
                add(key)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id == "ACTIONS" and isinstance(node.value, ast.Dict):
                    for key in node.value.keys:
                        add(key)
                if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name) \
                        and target.value.id == "ACTIONS":
                    add(target.slice)
    return found


def settings_keys(root: Path) -> set[str]:
    """Settings field names and their env vars (ENV_KEYS, WORKER_ENV, the settings-file variable)."""
    tree = ast.parse(_read(root, "prep/config.py"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            names |= {item.target.id for item in node.body
                      if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)}
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        for target in targets:
            if not isinstance(target, ast.Name) or node.value is None:
                continue
            if target.id == "ENV_KEYS" and isinstance(node.value, ast.Dict):
                names |= {v.value for v in node.value.values if isinstance(v, ast.Constant)}
            if target.id == "ENV_SETTINGS" and isinstance(node.value, ast.Constant):
                names.add(node.value.value)
            if target.id == "WORKER_ROUTES" and isinstance(node.value, ast.Tuple):
                names |= {f"PREP_WORKER_{e.value.upper()}" for e in node.value.elts if isinstance(e, ast.Constant)}
    return names


def tables_and_columns(root: Path) -> set[str]:
    """Every table created in prep/db.py and every column added through MIGRATIONS."""
    text = _read(root, "prep/db.py")
    names = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\(", text))
    for node in ast.parse(text).body:
        target = node.target if isinstance(node, ast.AnnAssign) else (node.targets[0] if isinstance(node, ast.Assign) else None)
        if isinstance(target, ast.Name) and target.id == "MIGRATIONS" and isinstance(node.value, ast.Dict):
            for columns in node.value.values:
                if isinstance(columns, ast.Dict):
                    names |= {k.value for k in columns.keys if isinstance(k, ast.Constant)}
    return names


def cli_commands(root: Path) -> set[str]:
    """The top-level `python -m prep.cli` commands, from build_parser's subparser choices."""
    sys.path.insert(0, str(root))
    try:
        from prep import cli  # noqa: PLC0415 (imported here so the module stays cheap to import)
        parser = cli.build_parser()
    finally:
        sys.path.pop(0)
    for action in parser._actions:  # argparse keeps subcommands on its _SubParsersAction
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    return set()


def skills(root: Path) -> set[str]:
    """Committed skills: .claude/skills/<name>/SKILL.md tracked by git (private, gitignored skills such as
    board/ stay out). Outside a git checkout, every SKILL.md on disk."""
    base = root / ".claude" / "skills"
    if not base.exists():
        return set()
    listed = subprocess.run(["git", "ls-files", "--", ".claude/skills/*/SKILL.md"], cwd=root,
                            capture_output=True, text=True)
    if listed.returncode == 0:
        return {Path(f).parent.name for f in listed.stdout.split()}
    return {p.parent.name for p in base.glob("*/SKILL.md")}


# --- coverage ------------------------------------------------------------------------------------

def _mentions_route(text: str, route: str) -> bool:
    if route == "/":
        return "`/`" in text or "`/?" in text
    # a prefix route (`/evals/report/`) is documented with a placeholder after it (`/evals/report/NAME.html`);
    # any other route must end where the path ends (`/learn`, `/learn?card=ID`, not `/learning` or `/learn/notes`)
    end = r"\S" if route.endswith("/") else r"[`?#\[ <]"
    return re.search(r"`" + re.escape(route) + r"(?=" + end + r")", text) is not None


def _mentions_word(text: str, word: str) -> bool:
    return re.search(r"(?<![\w-])" + re.escape(word) + r"(?![\w-])", text) is not None


COVERAGE = (
    # (what, facts, doc page that must name each one, how a mention is matched)
    ("route", routes, "docs/reference/routes.md", _mentions_route),
    ("setting", settings_keys, "docs/reference/configuration.md", _mentions_word),
    ("table or column", tables_and_columns, "docs/reference/database.md", _mentions_word),
    ("CLI command", cli_commands, "docs/usage.md",
     lambda text, name: re.search(r"(prep\.cli |` ?| / )" + re.escape(name) + r"(?![\w-])", text) is not None),
    ("skill", skills, "docs/reference/*.md", lambda text, name: re.search(r"`/" + re.escape(name) + r"\b|skills/" + re.escape(name) + "/", text) is not None),
)


def check_coverage(root: Path) -> list[str]:
    problems = []
    for what, facts, page, mentioned in COVERAGE:
        text = "\n".join(p.read_text(encoding="utf-8") for p in sorted(root.glob(page)))
        for name in sorted(facts(root)):
            if not mentioned(text, name):
                problems.append(f"{page}: {what} `{name}` is in the code but not on this page")
    return problems


# --- links, images, screenshots ------------------------------------------------------------------

def markdown_files(root: Path) -> list[str]:
    out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "*.md"], cwd=root,
                         capture_output=True, text=True, check=True).stdout.split()
    return sorted(f for f in out if (root / f).exists() and not f.startswith(".claude/skills/board/"))


def slug(heading: str) -> str:
    """GitHub's anchor for a heading."""
    text = re.sub(r"[`*_]", "", heading.strip().lower())
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(text: str) -> set[str]:
    found, seen, fenced = set(), {}, False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and re.match(r"#{1,6} ", line):
            base = slug(line.lstrip("#"))
            n = seen.get(base, 0)
            found.add(base if n == 0 else f"{base}-{n}")
            seen[base] = n + 1
    return found


def check_links(root: Path) -> list[str]:
    files = markdown_files(root)
    texts = {f: (root / f).read_text(encoding="utf-8") for f in files}
    problems, used_images = [], set()
    for f, text in texts.items():
        body = re.sub(r"```.*?```", "", text, flags=re.S)
        for match in re.finditer(r"(!?)\[[^\]]*\]\(([^)\s]+)\)", body):
            image, target = match.group(1), match.group(2)
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            path, _, anchor = target.partition("#")
            resolved = os.path.normpath(os.path.join(os.path.dirname(f), path)) if path else f
            if not (root / resolved).exists():
                problems.append(f"{f}: {'image' if image else 'link'} `{target}` points at a missing file")
                continue
            if image:
                used_images.add(resolved)
            if anchor and resolved.endswith(".md"):
                target_text = texts.get(resolved) or (root / resolved).read_text(encoding="utf-8")
                if anchor not in anchors(target_text):
                    problems.append(f"{f}: link `{target}` names an anchor that {resolved} has no heading for")
    shots = root / SCREENSHOTS
    if shots.exists():
        for png in sorted(shots.glob("*.png")):
            rel = str(png.relative_to(root))
            if rel not in used_images:
                problems.append(f"{rel}: no doc shows this screenshot (use it or delete it)")
    return problems


def check_repo(root: Path = ROOT) -> list[str]:
    return check_coverage(root) + check_links(root)


# --- changed code needs its docs -----------------------------------------------------------------

def load_map(root: Path) -> dict:
    """docs-map.json: {"rules": [{name, code: [globs], docs: [globs]}], "ignore": [globs]}."""
    data = json.loads(_read(root, DOCS_MAP) or '{"rules": []}')
    return {"rules": data["rules"], "ignore": data.get("ignore", [])}


def changed_files(root: Path, base: str) -> list[str]:
    out = subprocess.run(["git", "diff", "--name-only", f"{base}...HEAD"], cwd=root,
                         capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line]


def _matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def check_changes(changed: list[str], rules: list[dict], reason: str | None = None) -> list[str]:
    """One problem per rule whose code changed while none of its doc pages did. A stated reason
    ("Docs: not needed, ...") waives the rule."""
    if reason:
        return []
    problems = []
    for rule in rules:
        code = [p for p in changed if _matches(p, rule["code"])]
        if code and not any(_matches(p, rule["docs"]) for p in changed):
            problems.append(f"{rule['name']}: {', '.join(code)} changed, but none of {', '.join(rule['docs'])} did. "
                            "Update the page that owns it, or say why not in the PR: 'Docs: not needed, <reason>'.")
    return problems


def unmapped(changed: list[str], rules: list[dict], ignore: list[str] = ()) -> list[str]:
    """Changed code files no rule covers (a gap in docs-map.json, reported so the map stays complete)."""
    code = [p for p in changed if p.startswith(("prep/", ".claude/skills/", "scripts/")) and not _matches(p, list(ignore))]
    return [p for p in code if not any(_matches(p, r["code"]) for r in rules)]


def reason_from(text: str) -> str | None:
    match = NOT_NEEDED.search(text or "")
    return (match.group(1) or "no reason given").strip() if match else None


def pr_body(event_path: str | None) -> str:
    if not event_path or not Path(event_path).exists():
        return ""
    event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    return (event.get("pull_request") or {}).get("body") or ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m prep.docs_check", description=__doc__.splitlines()[0])
    parser.add_argument("--changed", metavar="BASE", help="also check code changed since BASE (e.g. origin/main)")
    parser.add_argument("--event", help="GitHub event file; its pull request body may say 'Docs: not needed, ...'")
    parser.add_argument("--reason", help="local equivalent of 'Docs: not needed, <reason>'")
    args = parser.parse_args(argv)
    problems = check_repo(ROOT)
    notes = []
    if args.changed:
        docs_map = load_map(ROOT)
        changed = changed_files(ROOT, args.changed)
        reason = args.reason or reason_from(pr_body(args.event or os.environ.get("GITHUB_EVENT_PATH")))
        problems += check_changes(changed, docs_map["rules"], reason)
        kept = [p for p in changed if (ROOT / p).exists()]  # a deleted file needs no owning page
        problems += [f"{DOCS_MAP}: no rule covers {p}; add it to the map"
                     for p in unmapped(kept, docs_map["rules"], docs_map["ignore"])]
        if reason:
            notes.append(f"Changed-code rule waived: {reason}")
    for line in notes:
        print(line)
    if problems:
        print(f"{len(problems)} docs problem(s):")
        for p in problems:
            print(f"- {p}")
        return 1
    print("Docs are in step with the code.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
