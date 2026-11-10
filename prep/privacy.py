"""Check that no personal file is tracked (or staged) by git.

Used by tests/test_privacy.py and scripts/pre-commit. Run directly:
    python -m prep.privacy           # checks `git ls-files`
    python -m prep.privacy --staged  # checks files staged for the next commit
"""

import fnmatch
import subprocess
import sys
from pathlib import PurePosixPath

# Matched against the file name (patterns without "/") or the full path (with "/").
FORBIDDEN = (
    "cv.md", "*.docx", "*.xlsx", "*.pdf", "*.db", "*.db-journal", "*.db-wal", "*.db-shm",
    "data/*", "notes/*", "sessions/*", "companies/*", "fit/*", "cv-tailored/*", "cv-history/*", "config/settings.toml", ".claude/skills/board/*",
    "mock-interview-kit.md", "stories.md", "gaps.md", "design-cases.md",
    ".env", ".env.*",
)
ALLOWED = ("*.example.md", "*.example.toml", ".env.example", "data/.gitkeep")


def is_forbidden(path: str) -> bool:
    name = PurePosixPath(path).name
    if any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(path, p) for p in ALLOWED):
        return False
    for pattern in FORBIDDEN:
        target = path if "/" in pattern else name
        if fnmatch.fnmatch(target, pattern):
            return True
    return False


def tracked_files(staged: bool = False) -> list[str]:
    cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"] if staged else ["git", "ls-files"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line]


def violations(paths: list[str]) -> list[str]:
    return [p for p in paths if is_forbidden(p)]


def main(argv: list[str] | None = None) -> int:
    staged = "--staged" in (argv if argv is not None else sys.argv[1:])
    bad = violations(tracked_files(staged))
    if bad:
        print("Personal files must not be committed:", file=sys.stderr)
        for path in bad:
            print(f"  {path}", file=sys.stderr)
        print("Unstage them with: git rm --cached <path>", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
