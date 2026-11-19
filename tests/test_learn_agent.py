"""The Learn page's Learn Assistant: Learn messages in agent_messages (kept out of the Applications Assistant box),
the Learn Assistant's ping, the box and its actions (Check my answer, Want a card for this?, Add to notes),
and `cli learn-thread`. Fictional data only."""

import io
import json
import os
from unittest import mock

from prep import assistant, cli, db, learn, learnnotes
from prep.config import load_settings
from tests.helpers import IsolatedTestCase
from tests.test_web import HOST, TODAY, WebTestCase

JSON = {**HOST, "Accept": "application/json", "Origin": "http://localhost:8765", "Sec-Fetch-Site": "same-origin"}
FORM = {**HOST, "Origin": "http://localhost:8765"}


class LearnTestCase(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(assistant, "notify_learn_in_background")
        self.ping = patcher.start()
        self.addCleanup(patcher.stop)

    def post_json(self, path: str, form: dict) -> tuple[int, dict]:
        status, headers, body = self.post(path, form, JSON)
        self.assertIn("application/json", headers["Content-Type"])
        return status, json.loads(body)

    def ask(self, card: int | None, body: str) -> int:
        with self.conn() as conn:
            return db.add_learn_message(conn, card, body)

    def answer(self, msg: int, body: str) -> int:
        with self.conn() as conn:
            return db.reply_message(conn, None, msg, body)


class LearnMessagesStoreTest(LearnTestCase):
    def test_learn_messages_stay_out_of_the_applications_thread(self) -> None:
        msg = self.ask(1, "Explain cache-aside simply")
        with self.conn() as conn:
            general = db.add_message(conn, None, "Which tasks need me?")
            self.assertNotIn(msg, [m["id"] for m in db.thread(conn)])
            self.assertEqual([m["id"] for m in db.pending_messages(conn)], [general])
            self.assertEqual([m["id"] for m in db.pending_messages(conn, topic=db.LEARN)], [msg])
            self.assertEqual(db.claim_messages(conn, None), [general])  # /ask general never takes a Learn one
            self.assertEqual(db.message_count(conn, learn=True), 1)
            reply = db.reply_message(conn, None, msg, "A cache in front of the store.")
            got = db.get_message(conn, reply)
            self.assertEqual((got["topic"], got["card_id"], got["reply_to"]), (db.LEARN, 1, msg))
            self.assertEqual([m["id"] for m in db.thread(conn, topic=db.LEARN, card_id=1)], [msg, reply])
            self.assertEqual(db.thread(conn, topic=db.LEARN, card_id=2), [])
            self.assertEqual(db.message_targets(conn, [msg, reply]), {})
        body = self.get("/")[2]
        self.assertNotIn("Explain cache-aside simply", body)
        self.assertNotIn("A cache in front of the store.", body)

    def test_unknown_card_and_empty_body_refused(self) -> None:
        with self.conn() as conn:
            with self.assertRaises(LookupError):
                db.add_learn_message(conn, 99, "x")
            with self.assertRaises(ValueError):
                db.add_learn_message(conn, 1, "  ")
        self.assertIsNotNone(self.ask(None, "General prep question"))

    def test_pending_learn_message_does_not_spin_the_applications_box(self) -> None:
        self.ask(1, "Quiz me on chapter 1")
        self.assertNotIn("data-pending", self.get("/")[2])
        reply = json.loads(self.get("/applications/thread")[2])
        self.assertFalse(reply["pending"])


class PingTest(IsolatedTestCase):
    def settings(self, **env: str):
        with mock.patch.dict(os.environ, env):
            return load_settings()

    def test_command_holds_ids_only(self) -> None:
        s = self.settings(PREP_PREP_ASSISTANT_TASK="51", PREP_ASSISTANT_TASK="42", PREP_ASSISTANT_PROJECT="e942-abc")
        self.assertEqual(assistant.learn_command(s, 3, 77, "/bin/dev3"), [
            "/bin/dev3", "message", "--task", "seq:51", "--project", "e942-abc",
            "--subject", "Learn question on card 3", "/learn-ask 3 77"])
        self.assertEqual(assistant.learn_command(s, None, 78, "/bin/dev3")[-3:],
                         ["--subject", "Learn question", "/learn-ask general 78"])

    def test_notify_only_with_a_prep_assistant(self) -> None:
        with mock.patch.object(assistant, "_send", return_value=1) as send:
            self.assertEqual(assistant.notify_learn(self.settings(PREP_ASSISTANT_TASK="42"), 3, 77), 0)
            send.assert_not_called()
            self.assertIsNone(assistant.notify_learn_in_background(self.settings(), 3, 77))
            s = self.settings(PREP_PREP_ASSISTANT_TASK="51", PREP_DEV3_BIN="/bin/sh")
            self.assertEqual(assistant.notify_learn(s, 3, 77), 1)
            self.assertEqual(send.call_args[0][0][0][3:5], ["seq:51", "--subject"])


class LearnBoxTest(LearnTestCase):
    def test_box_renders_the_form_and_check_button(self) -> None:
        body = self.get("/learn?card=1")[2]
        self.assertNotIn("The prep agent is not here yet", body)
        self.assertIn('<div class="learn-chat" id="learn-chat" data-card="1">', body)
        self.assertIn('action="/learn/message" class="ask" data-ajax="learn"', body)
        self.assertIn('action="/learn/check" class="answer-check" data-ajax="learn" data-save-answer', body)
        self.assertIn("No messages yet.", body)

    def test_send_stores_the_card_and_pings(self) -> None:
        status, reply = self.post_json("/learn/message", {"card_id": "2", "body": "Why <does> a stampede happen?"})
        self.assertEqual(status, 200)
        self.assertTrue(reply["ok"])
        with self.conn() as conn:
            msg = db.thread(conn, topic=db.LEARN)[-1]
        self.assertEqual((msg["card_id"], msg["author"], msg["status"]), (2, "user", "pending"))
        self.ping.assert_called_once_with(self.settings, 2, msg["id"])
        self.assertIn("Why &lt;does&gt; a stampede happen?", reply["html"])  # escaped
        self.assertIn("data-pending", reply["html"])
        self.assertIn("run /learn-ask", reply["html"])  # no Learn Assistant set
        self.assertEqual(reply["redirect"], "/learn?card=2")

    def test_plain_form_redirects_back_to_the_card(self) -> None:
        status, headers, _ = self.post("/learn/message", {"card_id": "1", "body": "hi"}, FORM)
        self.assertEqual((status, headers["Location"]), (303, "/learn?card=1&msg=learn_sent"))

    def test_send_refusals(self) -> None:
        self.assertEqual(self.post_json("/learn/message", {"card_id": "1", "body": "  "})[0], 400)
        self.assertEqual(self.post_json("/learn/message", {"card_id": "99", "body": "x"})[0], 400)
        self.ping.assert_not_called()

    def test_working_state_with_a_prep_assistant(self) -> None:
        self.ask(1, "Explain TTL")
        self.settings = self.settings.__class__(**{**self.settings.__dict__, "prep_assistant_task": 51})
        self.assertIn("Learn Assistant is working…", self.get("/learn?card=1")[2])

    def test_thread_tag_cuts_a_long_question_and_names_a_removed_card(self) -> None:
        front = "Why would a toy <shop> cache keep\nserving stale prices after the nightly import job finishes?"
        with self.conn() as conn:
            long_id = learn.add_card(conn, {"track": "caching-example", "front": front, "back": "- TTL"})
            gone_id = learn.add_card(conn, {"track": "caching-example", "front": "Gone soon?", "back": "- x"})
        self.ask(long_id, "Explain")
        self.ask(gone_id, "And this")
        with self.conn() as conn:
            learn.delete_card(conn, gone_id)
        body = self.get("/learn?card=1")[2]
        whole = "Why would a toy &lt;shop&gt; cache keep serving stale prices after the nightly import job finishes?"
        self.assertIn(f'<a class="msg-about" href="/learn?card={long_id}" title="{whole}">'
                      'Why would a toy &lt;shop&gt; cache keep serving stale…</a>', body)
        self.assertIn('<span class="msg-about">a removed card</span>', body)
        self.assertNotIn(f"card={gone_id}", body)

    def test_thread_tags_cards_and_reply_actions(self) -> None:
        msg = self.ask(2, "Explain stampede")
        reply = self.answer(msg, "Many misses at once. See https://example.com/stampede")
        body = self.get("/learn?card=1")[2]
        self.assertIn('<a class="msg-about" href="/learn?card=2" title="When does a cache stampede happen?">'
                      'When does a cache stampede happen?</a>', body)
        self.assertNotIn(">card 2<", body)
        self.assertIn('<a class="msg-about is-here" href="/learn?card=2">this card</a>', self.get("/learn?card=2")[2])
        self.assertIn(f'name="reply_id" value="{reply}"', body)
        self.assertIn('action="/learn/card"', body)
        self.assertIn('action="/learn/add-note"', body)
        self.assertIn('href="https://example.com/stampede"', body)
        self.assertEqual(body.count('name="reply_id"'), 2)  # actions under agent replies only

    def test_thread_json(self) -> None:
        msg = self.ask(1, "Quiz me")
        reply = json.loads(self.get("/learn/thread?card=1")[2])
        self.assertEqual((reply["pending"], reply["messages"]), (True, 1))
        self.answer(msg, "Question 1 of 2")
        reply = json.loads(self.get("/learn/thread?card=1")[2])
        self.assertEqual((reply["pending"], reply["messages"]), (False, 2))
        self.assertIn("Question 1 of 2", reply["html"])
        self.assertEqual(self.get("/learn/thread?card=x")[0], 400)


class LearnActionsTest(LearnTestCase):
    def save_answer(self, text: str) -> None:
        key = learnnotes.answer_key(1)
        learnnotes.save(self.settings.private_dir, key, text, learnnotes.read(self.settings.private_dir, key)["stamp"])

    def test_check_my_answer(self) -> None:
        status, reply = self.post_json("/learn/check", {"card_id": "1"})
        self.assertEqual(status, 400)
        self.assertIn("Write your answer first", reply["message"])
        self.save_answer("Read the cache, then the store.")
        status, reply = self.post_json("/learn/check", {"card_id": "1"})
        self.assertEqual(status, 200)
        with self.conn() as conn:
            msg = db.thread(conn, topic=db.LEARN)[-1]
        self.assertEqual((msg["body"], msg["card_id"]), (db.LEARN_CHECK, 1))  # fixed text, never the answer
        self.ping.assert_called_once_with(self.settings, 1, msg["id"])
        self.assertEqual(self.post_json("/learn/check", {})[0], 400)

    def test_want_a_card(self) -> None:
        msg = self.ask(1, "Explain TTL")
        reply = self.answer(msg, "A TTL expires an entry.")
        status, out = self.post_json("/learn/card", {"reply_id": str(reply), "card_id": "1"})
        self.assertEqual(status, 200)
        self.assertIn("drafts it", out["message"])
        with self.conn() as conn:
            ask = db.thread(conn, topic=db.LEARN)[-1]
            cards = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        self.assertEqual((ask["body"], ask["card_id"]), (db.LEARN_CARD.format(reply=reply), 1))
        self.assertEqual(cards, 3)  # nothing saved until the user says yes to the draft
        # only a Learn Assistant reply qualifies: not the user's message, not an Applications message
        self.assertEqual(self.post_json("/learn/card", {"reply_id": str(msg)})[0], 400)
        with self.conn() as conn:
            other = db.add_message(conn, None, "q")
            other_reply = db.reply_message(conn, None, other, "a")
        self.assertEqual(self.post_json("/learn/card", {"reply_id": str(other_reply)})[0], 400)
        self.assertEqual(self.post_json("/learn/card", {"reply_id": "x"})[0], 400)

    def test_add_to_notes_once(self) -> None:
        reply = self.answer(self.ask(1, "Explain TTL"), "A TTL expires an entry.")
        status, out = self.post_json("/learn/add-note", {"reply_id": str(reply), "card_id": "1"})
        self.assertEqual(status, 200)
        self.assertEqual((out["note"], out["note_card"]), ("card", 1))
        text = learnnotes.read(self.settings.private_dir, "1")["text"]
        self.assertIn("**What is a cache-aside read? · ", text)  # the card's question, not its id
        self.assertIn("A TTL expires an entry.", text)  # no Understand / Say it block: the whole reply
        self.assertEqual(out["stamp"], learnnotes.read(self.settings.private_dir, "1")["stamp"])
        status, out = self.post_json("/learn/add-note", {"reply_id": str(reply), "card_id": "1"})
        self.assertEqual(status, 400)
        self.assertIn("already in the notes", out["message"])

    def test_add_to_notes_keeps_only_the_essence(self) -> None:
        body = ("A cache-aside read checks the cache first.\n\nCovered: the cache check.\n"
                "Missed: nothing, the question is fully answered.\n\n"
                "Understand: the app asks the cache; on a miss it reads the <store>\nand fills the cache.\n"
                "Say it: \"I read from the cache and fall back to the database on a miss.\"")
        reply = self.answer(self.ask(1, "Explain"), body)
        status, out = self.post_json("/learn/add-note", {"reply_id": str(reply), "card_id": "1"})
        self.assertEqual(status, 200)
        text = learnnotes.read(self.settings.private_dir, "1")["text"]
        self.assertEqual(text, "**What is a cache-aside read? · 20 Jan**\n\n"
                               "Understand: the app asks the cache; on a miss it reads the <store>\nand fills the cache.\n"
                               "Say it: \"I read from the cache and fall back to the database on a miss.\"\n")
        self.assertNotIn("Covered:", text)
        page = self.get("/learn?card=1")[2]
        self.assertIn("reads the &lt;store&gt;", page)  # escaped in the editor
        self.assertEqual(self.post_json("/learn/add-note", {"reply_id": str(reply), "card_id": "1"})[0], 400)

    def test_old_reply_with_the_bonus_label_can_be_added(self) -> None:
        old = "Covered: TTL.\nMissed: nothing, the question is fully answered.\nBonus for the interview:\n- warm the cache"
        reply = self.answer(self.ask(1, "Check my answer"), old)
        self.assertEqual(self.post_json("/learn/add-note", {"reply_id": str(reply), "card_id": "1"})[0], 200)
        self.assertIn("Bonus for the interview:", learnnotes.read(self.settings.private_dir, "1")["text"])

    def test_general_essence_has_no_question(self) -> None:
        reply = self.answer(self.ask(None, "How?"), "Plan.\n\nUnderstand: one card a day.\nSay it: \"I drill daily.\"")
        self.assertEqual(self.post_json("/learn/add-note", {"reply_id": str(reply)})[0], 200)
        text = learnnotes.read(self.settings.private_dir, "all")["text"]
        self.assertTrue(text.startswith("**Agent · "))
        self.assertIn('Understand: one card a day.\nSay it: "I drill daily."', text)
        self.assertNotIn("Plan.", text)

    def test_general_reply_goes_to_all_prep_and_pay_is_refused(self) -> None:
        reply = self.answer(self.ask(None, "How do I prep?"), "One card a day.")
        status, out = self.post_json("/learn/add-note", {"reply_id": str(reply)})
        self.assertEqual((status, out["note"]), (200, "all"))
        self.assertIn("One card a day.", learnnotes.read(self.settings.private_dir, "all")["text"])
        pay = self.answer(self.ask(1, "q"), "The bonus is large.")
        self.assertEqual(self.post_json("/learn/add-note", {"reply_id": str(pay)})[0], 400)
        self.assertNotIn("bonus", learnnotes.read(self.settings.private_dir, "1")["text"])

    def test_text_is_never_logged(self) -> None:
        with self.assertLogs("prep.web", "INFO") as logs:
            self.post_json("/learn/message", {"card_id": "1", "body": "SECRET-LEARN-123"})
        self.assertNotIn("SECRET-LEARN-123", "\n".join(logs.output))


class LearnCliTest(LearnTestCase):
    def test_show_prints_card_context_and_claims(self) -> None:
        msg = self.ask(1, "Explain cache-aside")
        code, out, _ = self.run_cli("learn-thread", "show", str(msg), "--claim")
        self.assertEqual(code, 0)
        for text in ("card 1 open (Caching for a toy shop · Basics", "front: What is a cache-aside read?",
                     "- Read the cache first", "- What happens on a cold start?",
                     "[read] Example caching notes <https://example.com/caching-notes> · 10 min · short overview",
                     "1. Basics: cards 1, 2", "2. Trade-offs: cards 3", "stories.md (missing)",
                     "notes/interview-coaching.md (missing)", "cv-facts.md (missing)", "notes/learn/answer-1.md",
                     f"Claimed for this session: message {msg}.", f"learn-thread reply --to {msg}",
                     "Learn card 1"):
            self.assertIn(text, out)
        code, out, _ = self.run_cli("learn-thread", "show", str(msg), "--claim")
        self.assertIn("another session is already answering it", out)
        # thread show --message hands a Learn message over too
        self.assertIn("front: What is a cache-aside read?", self.run_cli("thread", "show", "--message", str(msg))[1])

    def test_check_hint_pending_and_reply(self) -> None:
        msg = self.ask(1, db.LEARN_CHECK)
        self.assertIn("learn-notes answer 1 --show", self.run_cli("learn-thread", "show", str(msg))[1])
        out = self.run_cli("learn-thread", "pending")[1]
        self.assertIn(f"message {msg} on card 1 (/learn-ask 1 {msg};", out)
        self.assertNotIn(str(msg), self.run_cli("thread", "pending")[1])
        with mock.patch("sys.stdin", io.StringIO("Covered: the read path.")):
            code, out, _ = self.run_cli("learn-thread", "reply", "--to", str(msg))
        self.assertEqual(code, 0)
        self.assertIn("Learn Assistant", out)
        self.assertIn("No Learn Assistant messages waiting.", self.run_cli("learn-thread", "pending")[1])
        with self.conn() as conn:
            self.assertEqual(db.thread(conn, topic=db.LEARN)[-1]["card_id"], 1)

    def test_show_survives_a_deleted_card(self) -> None:
        msg = self.ask(3, "Explain write-through")
        self.assertEqual(self.run_cli("cards", "delete", "3")[0], 0)
        code, out, _ = self.run_cli("learn-thread", "show", str(msg), "--claim")
        self.assertEqual(code, 0)
        self.assertIn("Card 3 was deleted", out)
        self.assertIn(f"learn-thread reply --to {msg}", out)

    def test_show_refuses_other_messages(self) -> None:
        with self.conn() as conn:
            other = db.add_message(conn, None, "q")
        self.assertNotEqual(self.run_cli("learn-thread", "show", str(other))[0], 0)

    def test_queue_and_today_lines(self) -> None:
        self.ask(1, "Quiz me")
        code, out, _ = self.run_cli("queue")
        self.assertIn("Learn Assistant: 1 message waiting. Run /learn-ask to answer.", out)
        self.assertNotIn("Agent thread", out)
        self.assertEqual(cli._thread_line([{"application_id": None, "topic": db.LEARN}]), "")
