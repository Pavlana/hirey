import shutil
import subprocess
import unittest

from prep import privacy
from prep.config import REPO_ROOT


class PatternTest(unittest.TestCase):
    def test_forbidden(self) -> None:
        for path in ("cv.md", "docs/cv.md", "Anything.docx", "x/tracker.xlsx", "a.pdf", "data/prep.db",
                     "data/notes.txt", "notes/today.md", "sessions/s1.md", "companies/acme.md", "fit/3-acme.md", "mock-interview-kit.md",
                     "stories.md", "gaps.md", "design-cases.md", ".env", ".env.local",
                     "config/settings.toml", "other/jobs.db", ".claude/skills/board/SKILL.md"):
            self.assertTrue(privacy.is_forbidden(path), path)

    def test_allowed(self) -> None:
        for path in ("cv.example.md", "notes/week.example.md", "sessions/mock.example.md", "data/.gitkeep",
                     ".env.example", "config/settings.example.toml", "README.md", "prep/db.py",
                     "tests/fixtures/example.json", ".claude/skills/today/SKILL.md"):
            self.assertFalse(privacy.is_forbidden(path), path)


class TrackedFilesTest(unittest.TestCase):
    def test_no_personal_file_is_tracked(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git not installed")
        result = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True)
        if result.returncode != 0:
            self.skipTest("not a git checkout")
        bad = privacy.violations(result.stdout.splitlines())
        self.assertEqual(bad, [], f"personal files are tracked: {bad}")


if __name__ == "__main__":
    unittest.main()
