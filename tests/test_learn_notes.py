"""Learn notes: the private files (save with a stamp, append), the editor and its routes, `cli learn-notes`."""

import io
import json
import re
from unittest import mock

from prep import learnnotes, privacy, web
from tests.test_web import HOST, TODAY, WebTestCase

JSON = {**HOST, "Accept": "application/json", "Origin": "http://localhost:8765", "Sec-Fetch-Site": "same-origin"}


def stamp_of(body: str) -> str:
    return re.search(r'name="stamp" value="([^"]*)"', body).group(1)


class LearnNotesStoreTest(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.private = self.settings.private_dir

    def test_missing_note_is_empty_and_saves(self) -> None:
        note = learnnotes.read(self.private, "1")
        self.assertEqual((note["text"], note["file"]), ("", "notes/learn/card-1.md"))
        stamp = learnnotes.save(self.private, "1", "# Cache-aside\r\n- read first\r\n\r\n", note["stamp"])
        path = self.private / "notes" / "learn" / "card-1.md"
        self.assertEqual(path.read_text(), "# Cache-aside\n- read first\n")  # \n line ends, one final newline
        self.assertEqual(learnnotes.read(self.private, "1")["stamp"], stamp)
        learnnotes.save(self.private, "all", "general", learnnotes.read(self.private, "all")["stamp"])
        self.assertTrue((self.private / "notes" / "learn" / "all-prep.md").exists())

    def test_stale_save_is_refused_and_the_append_kept(self) -> None:
        opened = learnnotes.read(self.private, "1")["stamp"]
        learnnotes.append(self.private, "1", "Read the TTL section.", day="2026-01-20")
        with self.assertRaises(learnnotes.NotesChanged):
            learnnotes.save(self.private, "1", "my text", opened)
        self.assertIn("**Agent · 20 Jan**\n\nRead the TTL section.", learnnotes.read(self.private, "1")["text"])

    def test_append_adds_at_the_end_and_never_rewrites(self) -> None:
        learnnotes.save(self.private, "1", "mine\n\n\n", learnnotes.read(self.private, "1")["stamp"])
        learnnotes.append(self.private, "1", "one", day="2026-01-20")
        learnnotes.append(self.private, "1", "two", day="2026-01-21")
        self.assertEqual(learnnotes.read(self.private, "1")["text"],
                         "mine\n\n**Agent · 20 Jan**\n\none\n\n**Agent · 21 Jan**\n\ntwo\n")
        with self.assertRaises(ValueError):
            learnnotes.append(self.private, "1", "  ")
        with self.assertRaises(ValueError):
            learnnotes.append(self.private, "1", "the offer is £1")  # never pay from an agent

    def test_keys_never_build_paths(self) -> None:
        for bad in ("../cv", "0", "1/2", "card-1", "", "All "):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                learnnotes.read(self.private, bad if bad != "All " else "ALL")
        with self.assertRaises(ValueError):
            learnnotes.read(None, "1")

    def test_length_limit(self) -> None:
        with self.assertRaises(ValueError):
            learnnotes.save(self.private, "1", "x" * (learnnotes.MAX_CHARS + 1), learnnotes.read(self.private, "1")["stamp"])

    def test_privacy_list_covers_learn_notes(self) -> None:
        self.assertTrue(privacy.is_forbidden("notes/learn/card-1.md"))
        self.assertTrue(privacy.is_forbidden("notes/learn/all-prep.md"))


class LearnNotesWebTest(WebTestCase):
    def post_json(self, form: dict) -> tuple[int, dict]:
        status, headers, body = self.post("/learn/notes", form, JSON)
        self.assertIn("application/json", headers["Content-Type"])
        return status, json.loads(body)

    def test_editor_on_the_card_view(self) -> None:
        body = self.get("/learn?card=1")[2]
        self.assertIn('<form method="post" action="/learn/notes" class="ed" id="learn-editor" data-learn-notes', body)
        self.assertIn('<a class="ed-tab" href="/learn?card=1" aria-current="true">this card</a>', body)
        self.assertIn('<a class="ed-tab" href="/learn?card=1&amp;notes=all">all prep</a>', body)
        self.assertIn("notes/learn/card-1.md", body)
        self.assertIn('data-vim-toggle aria-pressed="false" hidden>vim keys off<', body)  # off by default
        self.assertIn('<script src="/static/vim.js" defer></script>', body)
        all_prep = self.get("/learn?card=1&notes=all")[2]
        self.assertIn("notes/learn/all-prep.md", all_prep)
        self.assertIn('href="/learn?card=1&amp;notes=all" aria-current="true"', all_prep)

    def test_vim_js_is_served(self) -> None:
        status, headers, body = self.get("/static/vim.js")
        self.assertEqual(status, 200)
        self.assertIn("text/javascript", headers["Content-Type"])
        self.assertIn("const HireyVim", body)

    def test_save_and_line_numbers(self) -> None:
        stamp = stamp_of(self.get("/learn?card=1")[2])
        status, reply = self.post_json({"card_id": "1", "note": "card", "stamp": stamp, "text": "a\r\nb\r\nc"})
        self.assertEqual(status, 200)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["stamp"], learnnotes.read(self.settings.private_dir, "1")["stamp"])
        body = self.get("/learn?card=1")[2]
        self.assertIn('<pre class="ed-gutter" aria-hidden="true">1\n2\n3\n4</pre>', body)  # a, b, c and the last line
        self.assertIn('aria-label="Notes for this card">\na\nb\nc\n</textarea>', body)  # leading \n: the parser drops one

    def test_plain_form_save_redirects_back_to_the_tab(self) -> None:
        stamp = stamp_of(self.get("/learn?card=2&notes=all")[2])
        status, headers, _ = self.post("/learn/notes", {"card_id": "2", "note": "all", "stamp": stamp, "text": "x"},
                                       {**HOST, "Origin": "http://localhost:8765"})
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/learn?card=2&notes=all&msg=learn_notes_saved")

    def test_stale_save_keeps_the_append_and_returns_the_current_note(self) -> None:
        stamp = stamp_of(self.get("/learn?card=1")[2])
        learnnotes.append(self.settings.private_dir, "1", "From the agent", day=TODAY)
        status, reply = self.post_json({"card_id": "1", "note": "card", "stamp": stamp, "text": "mine"})
        self.assertEqual(status, 400)
        self.assertFalse(reply["ok"])
        self.assertIn("changed while you were editing", reply["message"])
        self.assertIn("From the agent", reply["text"])
        self.assertEqual(reply["stamp"], learnnotes.read(self.settings.private_dir, "1")["stamp"])
        self.assertIn('class="ed-current" open', reply["html"])
        self.assertIn(">\nmine</textarea>", reply["html"])  # the user's text stays in the editor
        self.assertIn("From the agent", learnnotes.read(self.settings.private_dir, "1")["text"])  # not lost
        # saving again with the stamp it was shown replaces it knowingly
        status, reply = self.post_json({"card_id": "1", "note": "card", "stamp": reply["stamp"], "text": "mine"})
        self.assertEqual(status, 200)

    def test_note_text_is_escaped(self) -> None:
        stamp = stamp_of(self.get("/learn?card=1")[2])
        self.post_json({"card_id": "1", "note": "card", "stamp": stamp, "text": '</textarea><script>alert("x")</script>'})
        body = self.get("/learn?card=1")[2]
        self.assertNotIn("<script>alert", body)
        self.assertIn("&lt;/textarea&gt;&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;", body)
        stamp = stamp_of(body)
        learnnotes.append(self.settings.private_dir, "1", "<b>agent</b>", day=TODAY)
        _, reply = self.post_json({"card_id": "1", "note": "card", "stamp": stamp, "text": "<i>"})
        self.assertNotIn("<b>agent", reply["html"])
        self.assertIn("&lt;b&gt;agent", reply["html"])

    def test_bad_requests(self) -> None:
        for form in ({"card_id": "99", "note": "card", "stamp": "", "text": "x"},
                     {"card_id": "1", "note": "../cv", "stamp": "", "text": "x"},
                     {"note": "card", "stamp": "", "text": "x"},
                     {"card_id": "1", "note": "card", "stamp": "", "text": "x" * (learnnotes.MAX_CHARS + 1)}):
            with self.subTest(form={k: v[:20] for k, v in form.items()}):
                status, reply = self.post_json(form)
                self.assertEqual(status, 400)
                self.assertFalse(reply["ok"])
        self.assertFalse((self.settings.private_dir / "notes" / "learn" / "card-99.md").exists())

    def test_cross_origin_save_refused(self) -> None:
        status, _, _ = self.post("/learn/notes", {"card_id": "1", "note": "card", "stamp": "", "text": "x"},
                                 {**HOST, "Origin": "http://evil.example"})
        self.assertEqual(status, 403)

    def test_without_private_dir(self) -> None:
        with mock.patch.dict("os.environ", {"PREP_PRIVATE_DIR": ""}):
            from prep.config import load_settings
            self.settings = load_settings()
        self.assertIsNone(self.settings.private_dir)
        self.assertIn("Notes need a private folder.", self.get("/learn?card=1")[2])
        status, reply = self.post_json({"card_id": "1", "note": "card", "stamp": "", "text": "x"})
        self.assertEqual(status, 400)

    def test_json_read(self) -> None:
        learnnotes.append(self.settings.private_dir, "all", "general note", day=TODAY)
        status, _, body = self.get("/learn/notes?card=1&note=all")
        reply = json.loads(body)
        self.assertEqual(status, 200)
        self.assertIn("general note", reply["text"])
        self.assertEqual(reply["stamp"], learnnotes.read(self.settings.private_dir, "all")["stamp"])
        self.assertEqual(self.get("/learn/notes?card=99")[0], 404)
        self.assertEqual(self.get("/learn/notes?card=x")[0], 400)


class LearnNotesCliTest(WebTestCase):
    def test_show_and_append(self) -> None:
        code, out, _ = self.run_cli("learn-notes", "1")
        self.assertEqual(code, 0)
        self.assertIn("notes/learn/card-1.md", out)
        self.assertIn("(none)", out)
        code, out, _ = self.run_cli("learn-notes", "1", "--append", "Drill the cold start.")
        self.assertEqual(code, 0)
        self.assertIn("Drill the cold start.", learnnotes.read(self.settings.private_dir, "1")["text"])
        with mock.patch("sys.stdin", io.StringIO("From stdin")):
            code, _, _ = self.run_cli("learn-notes", "all", "--append")
        self.assertEqual(code, 0)
        code, out, _ = self.run_cli("learn-notes", "all", "--show")
        self.assertIn("From stdin", out)
        self.assertIn("all prep", out)

    def test_refusals(self) -> None:
        self.assertNotEqual(self.run_cli("learn-notes", "99", "--append", "x")[0], 0)
        self.assertNotEqual(self.run_cli("learn-notes", "../x")[0], 0)
        self.assertNotEqual(self.run_cli("learn-notes", "1", "--append", "salary chat")[0], 0)
        self.assertEqual(learnnotes.read(self.settings.private_dir, "1")["text"], "")
