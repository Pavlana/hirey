import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from prep import docs_check as d


class RepoInStepTest(unittest.TestCase):
    """The real repo: every route, setting, table, command and skill is documented, and every link,
    anchor and screenshot resolves. A failure here names the page to update."""

    def test_docs_cover_the_code(self):
        problems = d.check_repo(d.ROOT)
        self.assertEqual(problems, [], "\n" + "\n".join(problems))

    def test_every_code_file_has_an_owning_doc(self):
        files = subprocess.run(["git", "ls-files", "prep", ".claude/skills", "scripts"], cwd=d.ROOT,
                               capture_output=True, text=True, check=True).stdout.split()
        docs_map = d.load_map(d.ROOT)
        self.assertEqual(d.unmapped(files, docs_map["rules"], docs_map["ignore"]), [])


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


class FactsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_routes_from_web(self):
        write(self.root, "prep/web.py", '''
            OLD = ("/old", "/older")
            ACTIONS = {"/a/save": (f, "/", "m")}
            ACTIONS["/a/stop"] = (g, "/", "m")
            def _get(path):
                if path == "/json":
                    pass
                if path.startswith("/files/"):
                    pass
                if path in OLD:
                    pass
                pages = {"/": h, "/learn": h}
        ''')
        self.assertEqual(d.routes(self.root), {"/old", "/older", "/a/save", "/a/stop", "/json", "/files/", "/", "/learn"})

    def test_tables_and_migrated_columns(self):
        write(self.root, "prep/db.py", '''
            SCHEMA = """
            CREATE TABLE IF NOT EXISTS widgets (id INTEGER);
            """
            # CREATE TABLE IF NOT EXISTS skips existing tables
            MIGRATIONS = {"widgets": {"colour": "TEXT"}}
        ''')
        self.assertEqual(d.tables_and_columns(self.root), {"widgets", "colour"})

    def test_settings_and_env_vars(self):
        write(self.root, "prep/config.py", '''
            ENV_SETTINGS = "X_SETTINGS"
            ENV_KEYS = {"port": "X_PORT"}
            WORKER_ROUTES = ("research",)
            class Settings:
                port: int
        ''')
        self.assertEqual(d.settings_keys(self.root), {"X_SETTINGS", "X_PORT", "PREP_WORKER_RESEARCH", "port"})

    def test_untracked_skills_are_left_out(self):
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        write(self.root, ".claude/skills/today/SKILL.md", "x\n")
        write(self.root, ".claude/skills/board/SKILL.md", "private\n")
        subprocess.run(["git", "add", ".claude/skills/today/SKILL.md"], cwd=self.root, check=True)
        self.assertEqual(d.skills(self.root), {"today"})

    def test_route_mentions(self):
        self.assertTrue(d._mentions_route("| `/learn?card=ID` |", "/learn"))
        self.assertTrue(d._mentions_route("`/applications/thread[?app=ID]`", "/applications/thread"))
        self.assertFalse(d._mentions_route("`/learning`", "/learn"))
        self.assertTrue(d._mentions_route("| `/` | home", "/"))
        self.assertFalse(d._mentions_route("`/learn`", "/"))


class LinksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_good_links_pass(self):
        write(self.root, "README.md", "See [usage](docs/usage.md#quick-start) and ![shot](docs/screenshots/a.png).\n")
        write(self.root, "docs/usage.md", "# Usage\n\n## Quick start\n")
        (self.root / "docs/screenshots").mkdir(parents=True, exist_ok=True)
        (self.root / "docs/screenshots/a.png").write_bytes(b"png")
        self.assertEqual(d.check_links(self.root), [])

    def test_missing_file_bad_anchor_and_unused_screenshot(self):
        write(self.root, "README.md", "[gone](docs/gone.md) [usage](docs/usage.md#nope)\n")
        write(self.root, "docs/usage.md", "# Usage\n")
        (self.root / "docs/screenshots").mkdir(parents=True, exist_ok=True)
        (self.root / "docs/screenshots/old.png").write_bytes(b"png")
        problems = d.check_links(self.root)
        self.assertEqual(len(problems), 3, problems)
        self.assertTrue(any("docs/gone.md" in p for p in problems))
        self.assertTrue(any("#nope" in p for p in problems))
        self.assertTrue(any("old.png" in p for p in problems))

    def test_links_inside_code_fences_are_ignored(self):
        write(self.root, "README.md", "```\n[x](nowhere.md)\n```\n")
        self.assertEqual(d.check_links(self.root), [])

    def test_github_anchor_slugs(self):
        self.assertEqual(d.anchors("# Usage & cost page\n## A\n## A\n"), {"usage--cost-page", "a", "a-1"})


class ChangedCodeTest(unittest.TestCase):
    RULES = [{"name": "Evals page", "code": ["prep/evals.py"], "docs": ["docs/reference/evals.md"]},
             {"name": "Dashboard", "code": ["prep/static/*"], "docs": ["docs/reference/*.md", "docs/screenshots/*"]}]

    def test_code_without_its_doc_fails(self):
        problems = d.check_changes(["prep/evals.py", "tests/test_evals.py"], self.RULES)
        self.assertEqual(len(problems), 1)
        self.assertIn("docs/reference/evals.md", problems[0])

    def test_code_with_its_doc_passes(self):
        self.assertEqual(d.check_changes(["prep/evals.py", "docs/reference/evals.md"], self.RULES), [])
        self.assertEqual(d.check_changes(["prep/static/app.js", "docs/screenshots/learn.png"], self.RULES), [])

    def test_stated_reason_waives_the_rule(self):
        self.assertEqual(d.check_changes(["prep/evals.py"], self.RULES, reason="internal rename"), [])

    def test_reason_line_in_pr_body(self):
        self.assertEqual(d.reason_from("Summary\n\nDocs: not needed, internal refactor\n"), "internal refactor")
        self.assertEqual(d.reason_from("docs: Not needed: tests only"), "tests only")
        self.assertIsNone(d.reason_from("The docs are not needed elsewhere."))
        self.assertIsNone(d.reason_from(""))

    def test_unmapped_code_is_reported(self):
        self.assertEqual(d.unmapped(["prep/new.py", "prep/evals.py", "prep/__init__.py", "docs/x.md"],
                                    self.RULES, ["prep/__init__.py"]), ["prep/new.py"])


if __name__ == "__main__":
    unittest.main()
