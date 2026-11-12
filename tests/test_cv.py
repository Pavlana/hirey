"""The master CV: rendering and escaping, skills parsing, saved versions (backup first, atomic),
diff, restore and its undo, path checks, the CLI, the My CV panel and "About my CV" messages.
A temp private_dir and cv.example.md only; no network."""

import json
import os
from pathlib import Path
from unittest import mock

from prep import assistant, cvfile, db, privacy, web
from tests.helpers import IsolatedTestCase
from tests.test_web import HOST, WebTestCase

REPO = Path(__file__).resolve().parent.parent
CV = (REPO / "cv.example.md").read_text(encoding="utf-8")
EDITED = CV.replace("Python, Go", "Python, Go, Rust")


class CvTestCase(IsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.private = self.tmp / "private"
        self.private.mkdir()
        (self.private / "cv.md").write_text(CV, encoding="utf-8")
        self.conn = db.connect(self.prep_db)
        self.addCleanup(self.conn.close)

    def cv(self) -> str:
        return (self.private / "cv.md").read_text(encoding="utf-8")


class SkillsTest(IsolatedTestCase):
    def test_themed_lines(self) -> None:
        self.assertEqual(cvfile.skills(CV), [
            ("Cloud & infrastructure", ["Azure", "AWS", "Terraform", "Kubernetes (AKS, EKS)"]),
            ("Delivery", ["GitHub Actions", "deployment pipelines", "on-call"]),
            ("Languages", ["Python", "Go"]),
            ("Security", ["identity and access management", "workload identity"]),
        ])

    def test_flat_list_and_sub_headings(self) -> None:
        flat = "# Name\n\n## Skills\n\nAzure, AWS, Terraform.\n\n## Education\n\nBSc, somewhere\n"
        self.assertEqual(cvfile.skills(flat), [("Skills", ["Azure", "AWS", "Terraform"])])
        nested = "## Technical skills\n### Cloud\n- Azure\n- AWS\n### Languages\nPython; Go\n# Next\nx, y\n"
        self.assertEqual(cvfile.skills(nested), [("Cloud", ["Azure", "AWS"]), ("Languages", ["Python", "Go"])])
        plain = "## Skills\nCloud: Azure, AWS\n"
        self.assertEqual(cvfile.skills(plain), [("Cloud", ["Azure", "AWS"])])
        self.assertEqual(cvfile.skills("# Name\n\nNo such section.\n"), [])


class RenderTest(IsolatedTestCase):
    def test_bold_italic_and_escaping(self) -> None:
        out = web.render_markdown("## Hi <b>\n\n**Lead** at *Acme* & co\n- **x:** <script>alert(1)</script>\n")
        self.assertIn("<h3>Hi &lt;b&gt;</h3>", out)
        self.assertIn("<p><strong>Lead</strong> at <em>Acme</em> &amp; co</p>", out)
        self.assertIn("<li><strong>x:</strong> &lt;script&gt;alert(1)&lt;/script&gt;</li>", out)
        self.assertNotIn("<script>", out)
        self.assertEqual(web.render_markdown("2 * 3 * 4"), "<p>2 * 3 * 4</p>")  # spaced stars stay
        self.assertEqual(web.render_markdown('**"><img src=x>**'), "<p><strong>&quot;&gt;&lt;img src=x&gt;</strong></p>")


class WriteTest(CvTestCase):
    def test_first_save_keeps_the_old_text_first(self) -> None:
        result = cvfile.write_cv(self.conn, self.private, EDITED, "Added Rust to Languages")
        self.assertEqual(result, {"version": 2, "previous": 1})
        self.assertEqual(self.cv(), EDITED)
        old, new = sorted(db.cv_versions(self.conn), key=lambda v: v["id"])
        self.assertEqual((old["note"], new["note"]), (cvfile.FIRST_NOTE, "Added Rust to Languages"))
        self.assertEqual(cvfile.read_version(self.private, old), CV)
        self.assertEqual(cvfile.read_version(self.private, new), EDITED)
        self.assertRegex(old["file_name"], r"^cv-\d{8}-\d{6}\.md$")
        self.assertRegex(new["file_name"], r"^cv-\d{8}-\d{6}-2\.md$", "same second: a numbered name")
        self.assertEqual(cvfile.history(self.conn, self.private)[1], 2)
        leftovers = [p.name for p in (self.private, cvfile.history_dir(self.private)) for p in p.iterdir()
                     if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [], "no temp file left behind")

    def test_no_second_backup_when_history_has_it(self) -> None:
        cvfile.write_cv(self.conn, self.private, EDITED, "one")
        result = cvfile.write_cv(self.conn, self.private, CV, "two")
        self.assertEqual(result, {"version": 3, "previous": 2})

    def test_a_hand_edit_is_saved_before_the_next_change(self) -> None:
        cvfile.write_cv(self.conn, self.private, EDITED, "one")
        (self.private / "cv.md").write_text(EDITED + "\nHand note.\n", encoding="utf-8")
        self.assertIsNone(cvfile.history(self.conn, self.private)[1], "nothing is current after a hand edit")
        result = cvfile.write_cv(self.conn, self.private, CV, "two")
        found = db.cv_version(self.conn, result["previous"])
        self.assertEqual(found["note"], cvfile.FOUND_NOTE)
        self.assertIn("Hand note.", cvfile.read_version(self.private, found))

    def test_refuses_empty_unchanged_or_noteless(self) -> None:
        for text, note in (("  \n", "x"), (CV, "x"), (EDITED, "  ")):
            with self.subTest(note=note), self.assertRaises(ValueError):
                cvfile.write_cv(self.conn, self.private, text, note)
        self.assertEqual(self.cv(), CV)
        self.assertEqual(db.cv_versions(self.conn), [])

    def test_a_failed_replace_leaves_cv_md_whole(self) -> None:
        with mock.patch.object(os, "replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            cvfile.write_cv(self.conn, self.private, EDITED, "one")
        self.assertEqual(self.cv(), CV)
        self.assertEqual([p.name for p in self.private.iterdir() if p.name.endswith(".tmp")], [])

    def test_restore_and_undo(self) -> None:
        cvfile.write_cv(self.conn, self.private, EDITED, "Added Rust")
        result = cvfile.restore(self.conn, self.private, 1)
        self.assertEqual(self.cv(), CV)
        self.assertEqual(result, {"version": 3, "previous": 2})
        self.assertTrue(db.cv_version(self.conn, 3)["note"].startswith("Restored v1 ("))
        cvfile.restore(self.conn, self.private, result["previous"])  # undo
        self.assertEqual(self.cv(), EDITED)
        with self.assertRaises(ValueError):
            cvfile.restore(self.conn, self.private, 4)  # already current
        with self.assertRaises(LookupError):
            cvfile.restore(self.conn, self.private, 99)

    def test_diff(self) -> None:
        [hunk] = cvfile.diff(CV, EDITED)
        self.assertIn(("-", "- **Languages:** Python, Go"), hunk)
        self.assertIn(("+", "- **Languages:** Python, Go, Rust"), hunk)
        self.assertLessEqual(sum(1 for op, _ in hunk if op == " "), 2 * cvfile.DIFF_CONTEXT)
        self.assertEqual(cvfile.diff(CV, CV), [])
        self.assertIn("+- **Languages:** Python, Go, Rust", cvfile.unified(CV, EDITED, "v1", "cv.md"))

    def test_paths_outside_history_are_never_read(self) -> None:
        cvfile.write_cv(self.conn, self.private, EDITED, "one")
        (self.tmp / "secret.md").write_text("outside", encoding="utf-8")
        for name in ("../../secret.md", "cv.md", "_draft.md", "cv-20260101-000000.md/../../cv.md"):
            with self.subTest(name=name):
                self.assertIsNone(cvfile.version_path(self.private, {"file_name": name}))
        link = cvfile.history_dir(self.private) / "cv-20260101-000000.md"
        link.symlink_to(self.tmp / "secret.md")
        self.assertIsNone(cvfile.version_path(self.private, {"file_name": link.name}), "a symlink out is refused")
        self.assertIsNone(cvfile.version_path(None, {"file_name": link.name}))


class CliTest(CvTestCase):
    def test_draft_save_history_diff_restore(self) -> None:
        code, out, _ = self.run_cli("cv", "draft")
        draft = Path(out.splitlines()[0])
        self.assertEqual((code, draft), (0, cvfile.draft_path(self.private)))
        draft.write_text(EDITED, encoding="utf-8")
        self.assertEqual(self.cv(), CV, "the draft, not cv.md, is edited")
        code, out, _ = self.run_cli("cv", "save", "--note", "Added Rust to Languages")
        self.assertEqual(code, 0, out)
        self.assertIn("Saved v2", out)
        self.assertIn("undo: cv restore 1", out)
        self.assertFalse(draft.exists(), "the draft goes once saved")
        self.assertEqual(self.cv(), EDITED)
        code, out, _ = self.run_cli("cv", "history")
        self.assertIn("v2", out)
        self.assertIn("Added Rust to Languages · CURRENT", out)
        code, out, _ = self.run_cli("cv", "diff", "1")
        self.assertIn("+- **Languages:** Python, Go, Rust", out)
        code, out, _ = self.run_cli("cv", "skills")
        self.assertIn("Languages: Python, Go, Rust", out)
        code, out, _ = self.run_cli("cv", "restore", "1")
        self.assertEqual((code, self.cv()), (0, CV))
        code, out, _ = self.run_cli("cv", "show")
        self.assertIn("saved as v3", out)

    def test_save_refuses_a_missing_or_unchanged_draft(self) -> None:
        code, _, err = self.run_cli("cv", "save", "--note", "x")
        self.assertEqual(code, 2)
        self.assertIn("cv draft", err)
        self.run_cli("cv", "draft")
        code, _, err = self.run_cli("cv", "save", "--note", "x")
        self.assertEqual(code, 2)
        self.assertIn("nothing changed", err)
        self.assertEqual(db.cv_versions(self.conn), [])

    def test_cv_messages_in_the_thread(self) -> None:
        msg = db.add_message(self.conn, None, "Add Rust to my skills", topic="cv")
        general = db.add_message(self.conn, None, "Which tasks need me?")
        code, out, _ = self.run_cli("thread", "pending")
        self.assertIn(f"message {msg} about the master CV (/ask cv {msg})", out)
        code, out, _ = self.run_cli("thread", "show", "--message", str(msg), "--claim")
        self.assertEqual(code, 0, out)
        self.assertIn("Messages about the master CV", out)
        self.assertIn("cv draft", out)
        self.assertIn(f"Claimed for this session: message {msg}.", out)
        code, out, _ = self.run_cli("thread", "show", "--claim")
        self.assertIn(f"Claimed for this session: message {general}.", out, "a general claim leaves CV messages")
        code, out, _ = self.run_cli("queue")
        self.assertIn("2 messages waiting (applications my CV, general); 2 already being answered", out)
        draft = Path(self.run_cli("cv", "draft")[1].splitlines()[0])
        draft.write_text(EDITED, encoding="utf-8")
        code, out, err = self.run_cli("cv", "save", "--note", "Added Rust", "--to", str(general))
        self.assertEqual(code, 2, "a general message is not answered by a CV save")
        self.assertEqual(self.cv(), CV, "and nothing was written")
        code, out, _ = self.run_cli("cv", "save", "--note", "Added Rust", "--to", str(msg))
        self.assertEqual(code, 0, out)
        reply = db.thread(self.conn, topic="cv")[-1]
        self.assertEqual((reply["author"], reply["reply_to"], reply["topic"], reply["body"]),
                         ("agent", msg, "cv", "Done (v2): Added Rust"))

    def test_queue_counts_cv_messages(self) -> None:
        db.add_message(self.conn, None, "Shorten my summary", topic="cv")
        code, out, _ = self.run_cli("queue")
        self.assertIn("(applications my CV)", out)

    def test_ping_command(self) -> None:
        settings = mock.Mock(assistant_task="7", assistant_project=None)
        self.assertEqual(assistant.ask_command(settings, None, 12, "dev3", topic="cv")[-1], "/ask cv 12")


class PanelTest(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.private = self.settings.private_dir
        self.private.mkdir(parents=True, exist_ok=True)
        (self.private / "cv.md").write_text(CV, encoding="utf-8")

    def write(self, text: str, note: str) -> dict:
        with self.conn() as conn:
            return cvfile.write_cv(conn, self.private, text, note)

    def panel(self, path: str) -> str:
        status, _, body = self.get(path)
        self.assertEqual(status, 200)
        return body[body.index('id="panel"'):body.index("</aside>", body.index('id="panel"'))]

    def test_my_cv_panel(self) -> None:
        body = self.get("/")[2]
        self.assertIn('href="/?cv=1" data-open-panel>My CV</a>', body)
        self.write(EDITED, "Added <Rust>")
        panel = self.panel("/?cv=1")
        self.assertIn('data-cv="1"', panel)
        self.assertIn('<span class="chip">Rust</span>', panel)
        self.assertIn("<dt>Languages</dt>", panel)
        self.assertIn("<strong>Senior Platform Engineer — Brightwater Widgets (fictional)</strong>", panel)
        self.assertIn("Added &lt;Rust&gt;", panel)
        self.assertIn("saved as v2", panel)
        self.assertIn('action="/cv/restore"', panel)  # v1 can be restored
        self.assertIn('href="/?cv=1&amp;compare=1"', panel)
        body = self.get("/?cv=1")[2]
        self.assertIn('href="/?cv=1" data-open-panel aria-current="true">My CV</a>', body)
        body = self.get("/?cv=1&sel=cv")[2]  # ticked: My CV is what the next message is about
        self.assertIn('<input type="hidden" name="about" value="cv"><span class="about-name">My CV</span>', body)

    def test_cv_and_job_panels_exclude_each_other(self) -> None:
        body = self.get("/?cv=1&app=1")[2]
        self.assertIn('data-cv="1"', body)
        self.assertIn('data-href="/?app=1&amp;sel=a%3A1"', body, "a row opens its panel (ticked: nothing else is) and closes My CV")
        self.assertIn('href="/" aria-label="Close" data-close-panel', body)
        body = self.get("/?view=offers&app=1")[2]
        self.assertIn('href="/?view=offers&amp;cv=1" data-open-panel>My CV</a>', body)

    def test_open_and_compare(self) -> None:
        self.write(EDITED, "Added Rust")
        panel = self.panel("/?cv=1&v=1")
        self.assertIn("Version v1", panel)
        self.assertIn("Python, Go</li>", panel)
        self.assertIn("← Current CV", panel)
        panel = self.panel("/?cv=1&compare=1")
        self.assertIn('<div class="diff-line diff-del"', panel)
        self.assertIn("Python, Go, Rust</span>", panel)
        self.assertIn("only in v1", panel)
        self.assertNotIn("Version v99", self.panel("/?cv=1&v=99"))  # unknown: the plain panel

    def test_restore_endpoint_and_undo(self) -> None:
        self.write(EDITED, "Added Rust")
        status, _, out = self.post("/cv/restore", {"version": "1", "return": "/?cv=1&v=1"},
                                   {**HOST, "Accept": "application/json"})
        reply = json.loads(out)
        self.assertEqual((status, reply["ok"], reply["version"], reply["previous"]), (200, True, 3, 2))
        self.assertEqual((self.private / "cv.md").read_text(encoding="utf-8"), CV)
        status, headers, _ = self.post("/cv/restore", {"version": "2", "return": "/?cv=1"})
        self.assertEqual((status, headers["Location"]), (303, "/?cv=1&msg=cv_restored"))
        self.assertEqual((self.private / "cv.md").read_text(encoding="utf-8"), EDITED)
        status, _, out = self.post("/cv/restore", {"version": "4"}, {**HOST, "Accept": "application/json"})
        self.assertEqual(status, 400, "already current")
        status, _, _ = self.post("/cv/restore", {"version": "1"}, {**HOST, "Origin": "http://evil.example"})
        self.assertEqual(status, 403)

    def test_a_tampered_version_row_is_not_served(self) -> None:
        self.write(EDITED, "Added Rust")
        (self.tmp / "secret.md").write_text("OUTSIDE SECRET", encoding="utf-8")
        with self.conn() as conn:
            conn.execute("UPDATE cv_versions SET file_name = '../../secret.md' WHERE id = 1")
            conn.commit()
        panel = self.panel("/?cv=1&v=1")
        self.assertNotIn("OUTSIDE SECRET", panel)
        self.assertIn("missing or unreadable", panel)

    def test_about_my_cv_message(self) -> None:
        with mock.patch.object(assistant, "notify_message_in_background") as bg:
            status, _, out = self.post("/applications/message", {"topic": "cv", "body": "Add Rust to my skills"},
                                       {**HOST, "Accept": "application/json"})
        self.assertEqual(status, 200)
        with self.conn() as conn:
            [m] = db.pending_messages(conn)
            self.assertEqual((m["application_id"], m["getjo_job_id"], m["topic"]), (None, None, "cv"))
            self.assertEqual(db.claim_messages(conn, None), [], "not a general message")
        bg.assert_called_once_with(self.settings, None, m["id"], job=False, topic="cv")
        self.assertIn('<a class="msg-about msg-about-cv" href="/?cv=1" title="The master CV">My CV</a>',
                      json.loads(out)["html"])
        status, _, _ = self.post("/applications/message", {"topic": "pay", "body": "x"},
                                 {**HOST, "Accept": "application/json"})
        self.assertEqual(status, 400)
        with mock.patch.object(assistant, "notify_message_in_background"):
            self.post("/applications/message", {"app_id": "1", "topic": "cv", "body": "about the job"})
        with self.conn() as conn:
            self.assertIsNone(db.thread(conn, 1)[-1]["topic"], "an application's message has no topic")

    def test_no_cv_or_private_dir(self) -> None:
        (self.private / "cv.md").unlink()
        self.assertIn("No cv.md yet.", self.panel("/?cv=1"))


class PrivacyTest(IsolatedTestCase):
    def test_history_files_are_private(self) -> None:
        self.assertTrue(privacy.is_forbidden("cv-history/cv-20261110-140500.md"))
        self.assertTrue(privacy.is_forbidden("cv-history/_draft.md"))
        self.assertIn("cv-history/*", (REPO / ".gitignore").read_text(encoding="utf-8"))
