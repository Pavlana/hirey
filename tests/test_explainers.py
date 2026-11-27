"""Learn explainers: the private page route (only that folder, its own CSP, sandbox), the card's
Explainer section, the staleness check against a fixture git repo in a temp dir (never the real
getjo), `cli explainers`, and the highlight box (quote in a message, Add to notes). Fictional data only."""

import os
import subprocess
from pathlib import Path
from unittest import mock

from prep import db, explainers, learnnotes
from prep.config import load_settings
from tests.test_learn_agent import FORM, LearnTestCase

RUN_PY = '''"""Example eval runner (fictional)."""


def to_label(score, threshold):
    if score is None:
        return "unscored"
    return "apply" if score >= threshold else "skip"


class Sweep:
    def run(self, scores):
        return [to_label(s, 7) for s in scores]
'''
PAGE = ('<!doctype html><html><head><link rel="stylesheet" href="/static/explainer.css">'
        '<script src="/static/explainer.js" defer></script></head><body><h1>Example</h1></body></html>')


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
                        "GIT_AUTHOR_DATE": "2026-01-12T10:00:00", "GIT_COMMITTER_DATE": "2026-01-12T10:00:00"})


class ExplainerTestCase(LearnTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.tmp / "getjo-repo"
        (self.repo / "evals").mkdir(parents=True)
        (self.repo / "evals" / "run.py").write_text(RUN_PY, encoding="utf-8")
        git(self.repo, "init", "-q")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "init")
        patcher = mock.patch.dict(os.environ, {"PREP_GETJO_REPO": str(self.repo)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings = load_settings()
        self.private = self.settings.private_dir

    def write_page(self, card: int = 1, html: str = PAGE) -> None:
        path = explainers.html_path(self.private, card)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")

    def record(self, card: int = 1, *snippets: tuple[str, str]) -> dict:
        return explainers.record(self.settings, card, list(snippets or [("evals/run.py", "to_label")]),
                                 "2026-01-20T10:00:00Z")


class RouteTest(ExplainerTestCase):
    def test_serves_the_page_with_its_own_policy(self) -> None:
        self.write_page()
        status, headers, body = self.get("/learn/explainer?card=1")
        self.assertEqual(status, 200)
        self.assertEqual(body, PAGE)
        csp = headers["Content-Security-Policy"]
        self.assertIn("sandbox allow-scripts", csp)
        self.assertNotIn("allow-same-origin", csp)
        self.assertIn("connect-src 'none'", csp)
        self.assertIn("frame-ancestors 'self'", csp)
        self.assertIn("default-src 'none'", csp)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_missing_and_bad_ids_are_404(self) -> None:
        self.assertEqual(self.get("/learn/explainer?card=1")[0], 404)
        self.write_page()
        (self.private / "secret.html").write_text("private", encoding="utf-8")
        for q in ("card=../secret", "card=..%2F..%2Fsecret", "card=-1", "card=1.html", "card=", "",
                  "card=%2Fetc%2Fpasswd", "card=00000000001"):
            with self.subTest(q=q):
                status, _, body = self.get(f"/learn/explainer?{q}")
                self.assertEqual(status, 404)
                self.assertNotIn("private", body)

    def test_only_the_explainers_folder(self) -> None:
        # a same-named file elsewhere in private_dir is never served
        (self.private / "learn").mkdir(parents=True, exist_ok=True)
        (self.private / "learn" / "1.html").write_text("wrong folder", encoding="utf-8")
        self.assertEqual(self.get("/learn/explainer?card=1")[0], 404)

    def test_dashboard_pages_may_frame_only_themselves(self) -> None:
        csp = self.get("/learn?card=1")[1]["Content-Security-Policy"]
        self.assertIn("frame-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("script-src 'self';", csp)  # no inline scripts on the dashboard

    def test_static_files(self) -> None:
        for path in ("/static/explainer.css", "/static/explainer.js"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 200)


class SectionTest(ExplainerTestCase):
    def test_none_yet_offers_make(self) -> None:
        body = self.get("/learn?card=1")[2]
        self.assertIn('action="/learn/explainer/make"', body)
        self.assertNotIn("<iframe", body)

    def test_up_to_date_page_in_a_sandboxed_frame(self) -> None:
        self.write_page()
        self.record()
        body = self.get("/learn?card=1")[2]
        self.assertIn('<iframe class="explainer-frame" src="/learn/explainer?card=1" sandbox="allow-scripts"', body)
        self.assertNotIn("allow-same-origin", body)
        self.assertIn("getjo @ ", body)
        self.assertIn("(12 Jan) · evals/run.py · up to date", body)
        self.assertNotIn("getjo has changed", body)

    def test_changed_shows_the_notice(self) -> None:
        self.write_page()
        self.record()
        run = self.repo / "evals" / "run.py"
        run.write_text(RUN_PY.replace('"unscored"', '"none"'), encoding="utf-8")
        body = self.get("/learn?card=1")[2]
        self.assertIn("getjo has changed since this explainer: evals/run.py · to_label", body)
        self.assertIn("Refresh explainer", body)

    def test_make_and_refresh_send_fixed_messages(self) -> None:
        status, reply = self.post_json("/learn/explainer/make", {"card_id": "1"})
        self.assertEqual(status, 200)
        with self.conn() as conn:
            msg = db.thread(conn, topic=db.LEARN)[-1]
        self.assertEqual((msg["body"], msg["card_id"]), (db.LEARN_EXPLAIN, 1))
        self.assertEqual(self.post_json("/learn/explainer/refresh", {"card_id": "1"})[0], 400)  # none yet
        self.answer(msg["id"], "Written.")
        self.write_page()
        self.record()
        (self.repo / "evals" / "run.py").write_text(RUN_PY.replace("7)", "8)"), encoding="utf-8")
        self.assertEqual(self.post_json("/learn/explainer/refresh", {"card_id": "1"})[0], 200)
        with self.conn() as conn:
            msg = db.thread(conn, topic=db.LEARN)[-1]
        self.assertTrue(msg["body"].startswith("Refresh the explainer for this card"))
        self.assertIn("(asked by the user)", msg["body"])  # to_label itself did not change
        self.assertEqual(self.post_json("/learn/explainer/refresh", {"card_id": "1"})[0], 400)  # already on it
        self.answer(msg["id"], "Refreshed.")
        (self.repo / "evals" / "run.py").write_text(RUN_PY.replace('"unscored"', '"none"'), encoding="utf-8")
        self.post_json("/learn/explainer/refresh", {"card_id": "1"})
        with self.conn() as conn:
            msg = db.thread(conn, topic=db.LEARN)[-1]
        self.assertIn("(evals/run.py · to_label)", msg["body"])

    def test_make_never_opens_the_chat_and_the_card_says_it_is_being_written(self) -> None:
        body = self.get("/learn?card=1")[2]
        self.assertNotIn("data-open-chat", body)
        status, reply = self.post_json("/learn/explainer/make", {"card_id": "1"})
        self.assertEqual(status, 200)
        self.assertIn('data-making="1"', reply["explainer"])
        self.assertIn("Being written by the Learn Assistant", reply["explainer"])
        self.assertNotIn('action="/learn/explainer/make"', reply["explainer"])
        self.assertEqual(self.post_json("/learn/explainer/make", {"card_id": "1"})[0], 400)  # once at a time
        body = self.get("/learn?card=1")[2]
        self.assertIn('data-making="1"', body)
        self.assertIn("No Learn Assistant task is set", body)  # no worker in the tests: the state says so
        state = self.get_json("/learn/thread?card=1")
        self.assertTrue(state["making"])
        self.assertIn("data-explainer-state", state["making_html"])
        self.assertEqual(state["explainer"], "")
        with self.conn() as conn:
            msg = db.thread(conn, topic=db.LEARN)[-1]
        self.answer(msg["id"], "Written: a diagram and the code.")
        self.write_page()
        self.record()
        state = self.get_json("/learn/thread?card=1")
        self.assertFalse(state["making"])
        self.assertIn("<iframe", state["explainer"])
        self.assertNotIn("data-making", state["explainer"])

    def test_refresh_keeps_the_page_while_it_is_rewritten(self) -> None:
        self.write_page()
        self.record()
        self.post_json("/learn/explainer/refresh", {"card_id": "1"})
        body = self.get("/learn?card=1")[2]
        self.assertIn("Being refreshed by the Learn Assistant", body)
        self.assertIn("<iframe", body)
        self.assertNotIn('action="/learn/explainer/refresh"', body)  # no second Refresh meanwhile


class CheckTest(ExplainerTestCase):
    def test_states(self) -> None:
        self.assertEqual(explainers.check(self.settings, 1)["state"], "none")
        self.write_page()
        self.assertEqual(explainers.check(self.settings, 1)["state"], "unknown")  # no sidecar yet
        data = self.record(1, ("evals/run.py", "to_label"), ("evals/run.py", "Sweep.run"))
        self.assertEqual(len(data["commit"]), 40)
        self.assertEqual(data["commit_date"], "2026-01-12")
        r = explainers.check(self.settings, 1)
        self.assertEqual((r["state"], r["files"], r["date"]), ("up_to_date", ["evals/run.py"], "2026-01-12"))
        run = self.repo / "evals" / "run.py"
        run.write_text("# a comment above\n" + RUN_PY, encoding="utf-8")  # moved, same code: still up to date
        self.assertEqual(explainers.check(self.settings, 1)["state"], "up_to_date")
        run.write_text(RUN_PY.replace("7)", "8)"), encoding="utf-8")
        r = explainers.check(self.settings, 1)
        self.assertEqual((r["state"], r["changed"]), ("changed", ["evals/run.py · Sweep.run"]))
        run.write_text(RUN_PY.replace("def to_label", "def to_verdict").replace("to_label(s", "to_verdict(s"),
                       encoding="utf-8")
        r = explainers.check(self.settings, 1)
        self.assertEqual(r["state"], "missing")
        self.assertIn("evals/run.py · to_label", r["missing"])
        run.unlink()
        self.assertEqual(explainers.check(self.settings, 1)["state"], "missing")

    def test_never_writes_to_the_repo(self) -> None:
        self.write_page()
        self.record()
        mtimes = {p: p.stat().st_mtime_ns for p in self.repo.rglob("*") if p.is_file()}
        for _ in range(2):
            explainers.check(self.settings, 1)
            explainers.head(self.repo)
        self.assertEqual(mtimes, {p: p.stat().st_mtime_ns for p in self.repo.rglob("*") if p.is_file()})

    def test_record_refuses_unknown_and_outside(self) -> None:
        self.write_page()
        for snippet in (("evals/run.py", "nope"), ("evals/missing.py", ""), ("../outside.py", ""),
                        ("/etc/hosts", ""), ("README.md", "f")):
            with self.subTest(snippet=snippet), self.assertRaises(explainers.SnippetError):
                self.record(1, snippet)

    def test_record_needs_the_page(self) -> None:
        with self.assertRaises(ValueError):
            self.record(2)


class CliTest(ExplainerTestCase):
    def test_path_save_check(self) -> None:
        code, out, _ = self.run_cli("explainers", "path", "1")
        self.assertEqual(code, 0)
        self.assertIn(str(explainers.html_path(self.private, 1)), out)
        self.write_page()
        code, out, err = self.run_cli("explainers", "save", "1", "--snippet", "evals/run.py:to_label")
        self.assertEqual(code, 0, err)
        self.assertIn("evals/run.py · to_label", out)
        code, out, _ = self.run_cli("explainers", "check")
        self.assertEqual(code, 0)
        self.assertIn("card 1: up to date", out)
        (self.repo / "evals" / "run.py").write_text(RUN_PY.replace('"unscored"', '"none"'), encoding="utf-8")
        code, out, _ = self.run_cli("explainers", "check")
        self.assertEqual(code, 1)
        self.assertIn("card 1: changed: evals/run.py · to_label", out)
        code, _, err = self.run_cli("explainers", "save", "1", "--snippet", "evals/run.py:gone")
        self.assertEqual(code, 2)
        self.assertIn("gone is not in evals/run.py", err)


class HighlightTest(ExplainerTestCase):
    def test_ask_with_an_empty_box_explains_the_quote(self) -> None:
        status, reply = self.post_json("/learn/message", {"card_id": "1", "body": " ", "quote": "Set a TTL",
                                                           "quote_src": "card"})
        self.assertEqual(status, 200)
        with self.conn() as conn:
            msg = db.thread(conn, topic=db.LEARN)[-1]
        self.assertEqual(msg["body"], "About this passage (Card):\n> Set a TTL\n\nExplain this.")
        self.assertEqual(reply["msg_id"], msg["id"])
        self.assertIn("Your questions", reply["questions"])
        self.assertIn("Thinking…", reply["questions"])
        self.ping.assert_called_once()
        # still refused with neither words nor a quote, and a quote alone needs a card
        self.assertEqual(self.post_json("/learn/message", {"card_id": "1", "body": ""})[0], 400)
        self.assertEqual(self.post_json("/learn/message", {"body": "", "quote": "x"})[0], 400)

    def test_answer_shows_in_place_and_stays_in_your_questions(self) -> None:
        _, reply = self.post_json("/learn/message", {"card_id": "1", "quote": "Set a <b>TTL</b>",
                                                      "quote_src": "explainer", "body": "why?"})
        msg_id = reply["msg_id"]
        waiting = self.get_json(f"/learn/answer?msg={msg_id}")
        self.assertEqual((waiting["state"], waiting["html"], waiting["reply_id"]), ("waiting", "", None))
        reply_id = self.answer(msg_id, "A **TTL** expires keys. <script>x</script>\n\nUnderstand: it bounds staleness\nSay it: \"A TTL bounds staleness.\"")
        done = self.get_json(f"/learn/answer?msg={msg_id}")
        self.assertEqual((done["state"], done["reply_id"]), ("answered", reply_id))
        self.assertIn("<strong>TTL</strong>", done["html"])  # the safe Markdown renderer
        self.assertIn("&lt;script&gt;", done["html"])
        self.assertNotIn("<script>", done["html"])
        self.assertIn("&lt;b&gt;TTL&lt;/b&gt;", done["questions"])  # the quote is text
        self.assertIn(f'name="reply_id" value="{reply_id}"', done["questions"])  # Add answer to notes
        body = self.get("/learn?card=1")[2]  # the next visit
        self.assertIn('id="learn-questions"', body)
        self.assertIn("Explainer</span>", body)
        self.assertIn("<strong>TTL</strong>", body)
        self.assertIn("why?", body)
        # Add answer to notes keeps the essence, as in the chat
        status, _ = self.post_json("/learn/add-note", {"card_id": "1", "reply_id": str(reply_id)})
        self.assertEqual(status, 200)
        self.assertIn("it bounds staleness", learnnotes.read(self.private, "1")["text"])

    def test_answer_json_refusals_and_plain_questions_stay_out(self) -> None:
        plain = self.ask(1, "quiz me")
        self.assertEqual(self.get_json(f"/learn/answer?msg={plain}")["state"], "waiting")
        self.assertNotIn("quiz me", self.get("/learn?card=1")[2].split('id="learn-questions"')[1].split("</section>")[0])
        self.assertEqual(self.get("/learn/answer?msg=abc")[0], 400)
        self.assertEqual(self.get("/learn/answer?msg=999")[0], 404)
        reply = self.answer(plain, "ok")
        self.assertEqual(self.get(f"/learn/answer?msg={reply}")[0], 404)  # an agent message

    def test_ask_marks_the_quote_and_its_source(self) -> None:
        status, reply = self.post_json("/learn/message", {"card_id": "1", "body": "Why <this>?",
                                                           "quote": "precision,\n recall  per threshold",
                                                           "quote_src": "explainer"})
        self.assertEqual(status, 200)
        with self.conn() as conn:
            msg = db.thread(conn, topic=db.LEARN)[-1]
        self.assertEqual(msg["body"], "About this passage (Explainer):\n> precision, recall per threshold\n\nWhy <this>?")
        self.assertIn("Why &lt;this&gt;?", reply["html"])

    def test_quote_is_capped_and_source_defaults_to_card(self) -> None:
        self.post_json("/learn/message", {"card_id": "1", "body": "q", "quote": "word " * 600, "quote_src": "<x>"})
        with self.conn() as conn:
            msg = db.thread(conn, topic=db.LEARN)[-1]
        self.assertTrue(msg["body"].startswith("About this passage (Card):\n> word"))
        self.assertLess(len(msg["body"]), 1100)

    def test_add_to_notes(self) -> None:
        status, reply = self.post_json("/learn/quote-note", {"card_id": "1", "quote": "Set a TTL", "quote_src": "card",
                                                              "body": "Why 60 seconds?"})
        self.assertEqual(status, 200)
        self.assertEqual((reply["note"], reply["note_card"]), ("card", 1))
        text = learnnotes.read(self.private, "1")["text"]
        self.assertIn("> Set a TTL (Card)\n\nWhy 60 seconds?", text)
        self.assertIn("Highlight", text)
        self.ping.assert_not_called()  # no assistant involved
        with self.conn() as conn:
            self.assertEqual(db.thread(conn, topic=db.LEARN), [])

    def test_add_to_notes_needs_text_and_a_card(self) -> None:
        self.assertEqual(self.post_json("/learn/quote-note", {"card_id": "1"})[0], 400)
        self.assertEqual(self.post_json("/learn/quote-note", {"quote": "x"})[0], 400)
        self.assertEqual(self.post_json("/learn/quote-note", {"card_id": "99", "quote": "x"})[0], 400)

    def test_plain_form_redirects(self) -> None:
        status, headers, _ = self.post("/learn/quote-note", {"card_id": "1", "quote": "x"}, FORM)
        self.assertEqual((status, headers["Location"]), (303, "/learn?card=1&msg=learn_quote_noted"))
