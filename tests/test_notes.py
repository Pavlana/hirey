"""The Notes card: storage (save with a stamp, append), the panel card and its route, `cli notes`."""

import io
import json
import re
import unittest
from contextlib import closing
from unittest import mock

from prep import cli, db, md, web
from prep.config import load_settings
from tests.helpers import IsolatedTestCase

TODAY = "2026-01-20"
HOST = {"Host": "localhost:8765"}
JSON = {**HOST, "Accept": "application/json", "Origin": "http://localhost:8765", "Sec-Fetch-Site": "same-origin"}


def stamp_of(body: str) -> str:
    return re.search(r'name="stamp" value="([^"]*)"', body).group(1)


class NotesStoreTest(IsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.conn = db.connect(self.prep_db)
        self.addCleanup(self.conn.close)
        self.app = db.add_application(self.conn, "Acme Rockets", "Platform Engineer", notes="Old note from applied")

    def test_existing_notes_have_no_stamp_and_save(self) -> None:
        self.assertEqual(db.get_notes(self.conn, self.app), {"notes": "Old note from applied", "stamp": ""})
        new = db.save_notes(self.conn, self.app, "  Rewritten  ", "")
        self.assertEqual(db.get_notes(self.conn, self.app), {"notes": "Rewritten", "stamp": new})

    def test_empty_save_clears(self) -> None:
        db.save_notes(self.conn, self.app, "", "")
        self.assertIsNone(db.get_application(self.conn, self.app)["notes"])

    def test_stale_stamp_refused(self) -> None:
        opened = db.get_notes(self.conn, self.app)["stamp"]
        db.append_notes(self.conn, self.app, "Interview is two rounds", "2026-11-17")
        with self.assertRaises(db.NotesChanged):
            db.save_notes(self.conn, self.app, "My edit", opened)
        self.assertIn("Interview is two rounds", db.get_notes(self.conn, self.app)["notes"])

    def test_append_keeps_text_and_dates_block(self) -> None:
        db.append_notes(self.conn, self.app, "- Ask about on-call\n", "2026-11-17")
        self.assertEqual(db.get_notes(self.conn, self.app)["notes"],
                         "Old note from applied\n\n**Agent · 17 Nov**\n\n- Ask about on-call")
        other = db.add_application(self.conn, "Globex", "SRE")
        db.append_notes(self.conn, other, "First", "2026-01-05")
        self.assertEqual(db.get_notes(self.conn, other)["notes"], "**Agent · 5 Jan**\n\nFirst")

    def test_append_refuses_pay_and_empty(self) -> None:
        for text in ("Range is 90k", "salary discussed", "  "):
            with self.subTest(text=text), self.assertRaises(ValueError):
                db.append_notes(self.conn, self.app, text)
        self.assertEqual(db.get_notes(self.conn, self.app)["notes"], "Old note from applied")

    def test_update_notes_moves_stamp(self) -> None:
        db.update_application(self.conn, self.app, notes="From the CLI")
        self.assertTrue(db.get_notes(self.conn, self.app)["stamp"])
        with self.assertRaises(db.NotesChanged):
            db.save_notes(self.conn, self.app, "x", "")

    def test_stage_change_does_not_make_notes_stale(self) -> None:
        opened = db.get_notes(self.conn, self.app)["stamp"]
        db.move_application(self.conn, self.app, "applied")
        db.save_notes(self.conn, self.app, "Still fine", opened)

    def test_too_long_refused(self) -> None:
        with self.assertRaises(ValueError):
            db.save_notes(self.conn, self.app, "x" * (db.MAX_NOTES + 1), "")

    def test_unknown_app(self) -> None:
        with self.assertRaises(LookupError):
            db.save_notes(self.conn, 999, "x", "")


class MarkdownBreaksTest(unittest.TestCase):
    def test_breaks_keep_lines_and_escape(self) -> None:
        out = md.render_markdown("line one\n<b>two</b>\n\n- a", breaks=True)
        self.assertEqual(out, "<p>line one<br>&lt;b&gt;two&lt;/b&gt;</p><ul><li>a</li></ul>")
        self.assertEqual(md.render_markdown("a\nb"), "<p>a b</p>")


class NotesWebTest(IsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings()
        with closing(db.connect(self.prep_db)) as conn:
            self.app = db.add_application(conn, "Acme Rockets", "Platform Engineer", status="applied",
                                          date_applied="2026-01-14",
                                          notes="**Recruiter** call\n<script>alert(1)</script>")

    def get(self, path: str) -> str:
        status, _, body = web.handle(self.settings, "GET", path, HOST, today=TODAY)
        self.assertEqual(status, 200)
        return body.decode()

    def post(self, form: dict, headers: dict = JSON) -> tuple[int, dict, str]:
        from urllib.parse import urlencode
        status, hdrs, out = web.handle(self.settings, "POST", "/applications/notes", headers,
                                       urlencode(form).encode(), today=TODAY)
        return status, dict(hdrs), out.decode()

    def test_panel_shows_results_with_only_notes(self) -> None:
        body = self.get(f"/?app={self.app}")
        self.assertIn('<section class="panel-results"><h3>Results</h3>', body)
        self.assertIn('id="card-notes"', body)
        self.assertIn("<strong>Recruiter</strong> call<br>&lt;script&gt;", body)
        self.assertNotIn("<script>alert", body)
        self.assertIn('action="/applications/notes"', body)

    def test_panel_without_notes_still_has_card(self) -> None:
        with closing(db.connect(self.prep_db)) as conn:
            other = db.add_application(conn, "Globex", "SRE")
        body = self.get(f"/?app={other}")
        self.assertIn('id="card-notes"', body)
        self.assertIn("No notes yet", body)

    def test_save_and_clear(self) -> None:
        stamp = stamp_of(self.get(f"/?app={self.app}"))
        status, _, out = self.post({"app_id": self.app, "stamp": stamp, "notes": "Ask about **team size**"})
        reply = json.loads(out)
        self.assertEqual((status, reply["ok"], reply["message"]), (200, True, "Notes saved."))
        self.assertIn("<strong>team size</strong>", reply["html"])
        stamp = stamp_of(reply["html"])
        status, _, out = self.post({"app_id": self.app, "stamp": stamp, "notes": ""})
        self.assertEqual(status, 200)
        with closing(db.connect(self.prep_db)) as conn:
            self.assertIsNone(db.get_application(conn, self.app)["notes"])

    def test_stale_save_refused_and_text_kept(self) -> None:
        stamp = stamp_of(self.get(f"/?app={self.app}"))
        with closing(db.connect(self.prep_db)) as conn:
            db.append_notes(conn, self.app, "Two rounds, AI tools allowed", "2026-01-20")
        status, _, out = self.post({"app_id": self.app, "stamp": stamp, "notes": "My <draft>"})
        reply = json.loads(out)
        self.assertEqual((status, reply["ok"]), (400, False))
        self.assertIn("changed while you were editing", reply["message"])
        self.assertIn("Two rounds, AI tools allowed", reply["html"])  # the current notes shown
        self.assertIn("My &lt;draft&gt;</textarea>", reply["html"])  # the user's text kept
        self.assertIn("<details class=\"notes-edit\" open>", reply["html"])
        self.assertEqual(stamp_of(reply["html"]), reply["stamp"])
        with closing(db.connect(self.prep_db)) as conn:
            self.assertIn("Two rounds", db.get_notes(conn, self.app)["notes"])

    def test_stale_plain_post_keeps_text_on_page(self) -> None:
        with closing(db.connect(self.prep_db)) as conn:
            db.append_notes(conn, self.app, "Added", "2026-01-20")
        status, _, out = self.post({"app_id": self.app, "stamp": "", "notes": "Mine"}, HOST)
        self.assertEqual(status, 400)
        self.assertIn("Mine</textarea>", out)

    def test_plain_post_redirects_to_panel(self) -> None:
        status, headers, _ = self.post({"app_id": self.app, "stamp": "", "notes": "x",
                                        "return": f"/?app={self.app}"}, HOST)
        self.assertEqual(status, 303)
        self.assertTrue(headers["Location"].startswith(f"/?app={self.app}"))

    def test_cross_origin_refused(self) -> None:
        status, _, _ = self.post({"app_id": self.app, "stamp": "", "notes": "x"},
                                 {**HOST, "Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        with closing(db.connect(self.prep_db)) as conn:
            self.assertIn("Recruiter", db.get_notes(conn, self.app)["notes"])

    def test_unknown_app(self) -> None:
        status, _, out = self.post({"app_id": "999", "stamp": "", "notes": "x"})
        self.assertEqual(status, 400)

    def test_notes_text_never_logged(self) -> None:
        with self.assertLogs("prep.web", "INFO") as logs:
            self.post({"app_id": self.app, "stamp": "", "notes": "SECRET-NOTE-TEXT"})
        self.assertNotIn("SECRET-NOTE-TEXT", "\n".join(logs.output))


class NotesCliTest(IsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        with closing(db.connect(self.prep_db)) as conn:
            self.app = db.add_application(conn, "Acme Rockets", "Platform Engineer", notes="User line")

    def test_show(self) -> None:
        code, out, _ = self.run_cli("notes", str(self.app), "--show")
        self.assertEqual(code, 0)
        self.assertIn("User line", out)

    def test_append_from_argument_and_stdin(self) -> None:
        code, out, _ = self.run_cli("notes", str(self.app), "--append", "Interviewer: a staff engineer")
        self.assertEqual(code, 0)
        self.assertIn("**Agent · ", out)
        with mock.patch("sys.stdin", io.StringIO("**Questions to ask**\n\n- How is on-call shared?\n")):
            code, _, _ = self.run_cli("notes", str(self.app), "--append")
        self.assertEqual(code, 0)
        with closing(db.connect(self.prep_db)) as conn:
            notes = db.get_notes(conn, self.app)["notes"]
        self.assertTrue(notes.startswith("User line\n\n**Agent · "))
        self.assertIn("Interviewer: a staff engineer", notes)
        self.assertIn("- How is on-call shared?", notes)
        self.assertTrue(cli.has_questions_section(notes))

    def test_append_pay_refused(self) -> None:
        code, _, err = self.run_cli("notes", str(self.app), "--append", "Budget £100")
        self.assertEqual(code, 2)
        self.assertIn("pay", err)

    def test_unknown_app(self) -> None:
        code, _, _ = self.run_cli("notes", "999")
        self.assertEqual(code, 2)

    def test_thread_show_prints_notes(self) -> None:
        code, out, _ = self.run_cli("thread", "show", str(self.app))
        self.assertEqual(code, 0)
        self.assertIn("Notes (the panel's Notes card", out)
        self.assertIn("  User line", out)

    def test_questions_section_detection(self) -> None:
        self.assertTrue(cli.has_questions_section("x\n## Questions to ask\n- a"))
        self.assertFalse(cli.has_questions_section("I have questions to ask later"))


if __name__ == "__main__":
    unittest.main()
