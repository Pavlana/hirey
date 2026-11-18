"""Resend and Cancel on a user message still waiting, in the Applications Assistant and Learn Assistant
boxes: same message id re-pinged (no new row), cancelled messages kept but out of every pending count,
refused by the claiming CLI, and a late reply labelled "after cancel". dev3 is never called (the ping
functions are mocked). Fictional data only."""

import json
import time
from unittest import mock

from prep import assistant, db
from tests.test_learn_agent import FORM, JSON, LearnTestCase
from tests.test_thread import ACME
from tests.test_web import WebTestCase


class ResendCancelBase(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        # never a real dev3 call: every background ping is stubbed, and so is the command runner
        for name in ("notify_message_in_background", "notify_learn_in_background", "notify_in_background",
                     "notify_briefs_in_background", "_send"):
            patcher = mock.patch.object(assistant, name)
            patcher.start()
            self.addCleanup(patcher.stop)
        assistant._PINGS.clear()
        self.addCleanup(assistant._PINGS.clear)

    def with_tasks(self) -> None:
        self.settings = self.settings.__class__(**{**self.settings.__dict__, "assistant_task": 9022,
                                                   "prep_assistant_task": 9053})

    def post_json(self, path: str, form: dict) -> tuple[int, dict]:
        status, _, body = self.post(path, form, JSON)
        return status, json.loads(body)

    def rows(self) -> int:
        with self.conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM agent_messages").fetchone()[0]

    def status(self, msg: int) -> str:
        with self.conn() as conn:
            return db.get_message(conn, msg)["status"]


class StorageTest(ResendCancelBase):
    def test_cancel_drops_out_of_pending_and_claims(self) -> None:
        with self.conn() as conn:
            msg = db.add_message(conn, ACME, "Is this remote?")
            db.cancel_message(conn, msg)
            self.assertEqual(db.get_message(conn, msg)["status"], "cancelled")
            self.assertNotIn(msg, [m["id"] for m in db.pending_messages(conn)])
            self.assertEqual(db.claim_messages(conn, ACME), [])
            self.assertFalse(db.claim_message(conn, msg))
            with self.assertRaises(ValueError):
                db.cancel_message(conn, msg)  # only a waiting message
            with self.assertRaises(LookupError):
                db.cancel_message(conn, msg, learn=True)  # not a Learn message

    def test_claimed_message_can_be_cancelled_and_a_late_reply_is_kept(self) -> None:
        with self.conn() as conn:
            msg = db.add_message(conn, ACME, "Is this remote?")
            self.assertTrue(db.claim_message(conn, msg))
            db.cancel_message(conn, msg)
            reply = db.reply_message(conn, ACME, msg, "Hybrid, two days.")
            self.assertEqual(db.get_message(conn, msg)["status"], "cancelled")
            self.assertEqual(db.get_message(conn, reply)["reply_to"], msg)
            with self.assertRaises(LookupError):
                db.cancel_message(conn, reply)  # an agent reply is never cancelled

    def test_reopen_drops_a_claim(self) -> None:
        with self.conn() as conn:
            msg = db.add_message(conn, ACME, "Is this remote?")
            db.claim_message(conn, msg)
            db.reopen_message(conn, msg)
            self.assertEqual(db.get_message(conn, msg)["status"], "pending")
            self.assertTrue(db.claim_message(conn, msg))
            db.reply_message(conn, ACME, msg, "Yes.")
            with self.assertRaises(ValueError):
                db.reopen_message(conn, msg)  # answered: refused


class ApplicationsBoxTest(ResendCancelBase):
    def ask(self) -> int:
        with self.conn() as conn:
            return db.add_message(conn, ACME, "Is this remote?")

    def test_waiting_message_has_cancel_and_resend_only_with_a_task(self) -> None:
        msg = self.ask()
        body = self.get(f"/?app={ACME}")[2]
        self.assertIn('action="/applications/cancel"', body)
        self.assertNotIn('action="/applications/resend"', body)  # no assistant task: run /ask by hand
        self.with_tasks()
        body = self.get(f"/?app={ACME}")[2]
        self.assertIn(f'<input type="hidden" name="msg_id" value="{msg}">', body)
        self.assertRegex(body, r'data-resend-due="\d+" hidden')  # just sent: not due yet

    def test_resend_is_shown_at_once_after_a_failed_ping_or_after_a_while(self) -> None:
        msg = self.ask()
        self.with_tasks()
        assistant._record(msg, 0)
        self.assertRegex(self.get(f"/?app={ACME}")[2], r'data-resend-due="\d+" title')
        assistant._PINGS[msg] = (time.time() - assistant.RESEND_AFTER - 1, True)
        self.assertRegex(self.get(f"/?app={ACME}")[2], r'data-resend-due="\d+" title')

    def test_poll_reports_a_failed_ping(self) -> None:
        msg = self.ask()
        self.assertEqual(json.loads(self.get("/applications/thread")[2])["failed"], [])
        assistant._record(msg, 0)
        self.assertEqual(json.loads(self.get("/applications/thread")[2])["failed"], [msg])
        with self.conn() as conn:
            learn_msg = db.add_learn_message(conn, 1, "Quiz me")
        assistant._record(learn_msg, 0)
        self.assertEqual(json.loads(self.get("/learn/thread?card=1")[2])["failed"], [learn_msg])

    def test_resend_repings_the_same_message(self) -> None:
        msg = self.ask()
        self.with_tasks()
        with self.conn() as conn:
            db.claim_message(conn, msg)
        before = self.rows()
        with mock.patch.object(assistant, "notify_message", return_value=1) as ping:
            status, reply = self.post_json("/applications/resend", {"msg_id": str(msg)})
        self.assertEqual(status, 200)
        self.assertEqual(reply["message"], "Sent to the Applications Assistant again.")
        ping.assert_called_once_with(self.settings, ACME, msg, job=False, topic=None)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.status(msg), "pending")

    def test_resend_failure_and_refusals(self) -> None:
        msg = self.ask()
        status, reply = self.post_json("/applications/resend", {"msg_id": str(msg)})
        self.assertEqual((status, reply["ok"]), (400, False))
        self.assertIn("No Applications Assistant task is set", reply["message"])
        self.with_tasks()
        with mock.patch.object(assistant, "notify_message", return_value=0):
            status, reply = self.post_json("/applications/resend", {"msg_id": str(msg)})
        self.assertEqual(status, 400)
        self.assertEqual(reply["message"], "Couldn't reach the Applications Assistant (seq 9022). Is its task running?")
        with self.conn() as conn:
            db.reply_message(conn, ACME, msg, "Yes.")
        with mock.patch.object(assistant, "notify_message", return_value=1) as ping:
            status, reply = self.post_json("/applications/resend", {"msg_id": str(msg)})
        self.assertEqual(status, 400)
        self.assertIn("answered", reply["message"])
        ping.assert_not_called()

    def test_cancel_greys_it_and_a_late_reply_says_after_cancel(self) -> None:
        msg = self.ask()
        status, reply = self.post_json("/applications/cancel", {"msg_id": str(msg)})
        self.assertEqual(status, 200)
        self.assertIn("msg-user is-cancelled", reply["html"])
        self.assertIn('<span class="msg-flag">Cancelled</span>', reply["html"])
        self.assertNotIn("data-pending", reply["html"])
        self.assertFalse(json.loads(self.get("/applications/thread")[2])["pending"])
        with self.conn() as conn:
            db.reply_message(conn, ACME, msg, "Late answer.")
        self.assertIn('<span class="msg-flag">after cancel</span>', self.get("/")[2])
        self.assertEqual(self.post_json("/applications/cancel", {"msg_id": str(msg)})[0], 400)

    def test_cancel_by_form_redirects_and_needs_same_origin(self) -> None:
        msg = self.ask()
        self.assertEqual(self.post("/applications/cancel", {"msg_id": str(msg)},
                                   {**FORM, "Origin": "http://evil.example"})[0], 403)
        status, headers, _ = self.post("/applications/cancel", {"msg_id": str(msg), "return": "/"}, FORM)
        self.assertEqual((status, headers["Location"]), (303, "/?msg=cancelled"))
        self.assertEqual(self.status(msg), "cancelled")

    def test_cli_refuses_a_cancelled_message_and_queue_skips_it(self) -> None:
        msg = self.ask()
        with self.conn() as conn:
            db.cancel_message(conn, msg)
        code, out, _ = self.run_cli("thread", "show", "--message", str(msg), "--claim")
        self.assertEqual(code, 0)
        self.assertIn(f"Message {msg} was cancelled by the user", out)
        self.assertNotIn("Claimed for this session", out)
        self.assertNotIn("Agent thread", self.run_cli("queue")[1])


class LearnBoxTest(ResendCancelBase, LearnTestCase):
    def test_resend_and_cancel_in_the_learn_box(self) -> None:
        msg = self.ask(1, "Quiz me on chapter 1")
        self.with_tasks()
        body = self.get("/learn?card=1")[2]
        self.assertIn('action="/learn/resend"', body)
        self.assertIn('action="/learn/cancel"', body)
        before = self.rows()
        with mock.patch.object(assistant, "notify_learn", return_value=1) as ping:
            status, reply = self.post_json("/learn/resend", {"msg_id": str(msg), "card_id": "1"})
        self.assertEqual((status, reply["message"]), (200, "Sent to the Learn Assistant again."))
        ping.assert_called_once_with(self.settings, 1, msg)
        self.assertEqual(self.rows(), before)
        with mock.patch.object(assistant, "notify_learn", return_value=0):
            status, reply = self.post_json("/learn/resend", {"msg_id": str(msg), "card_id": "1"})
        self.assertEqual(reply["message"], "Couldn't reach the Learn Assistant (seq 9053). Is its task running?")
        status, reply = self.post_json("/learn/cancel", {"msg_id": str(msg), "card_id": "1"})
        self.assertEqual(status, 200)
        self.assertIn('<span class="msg-flag">Cancelled</span>', reply["html"])
        self.assertFalse(json.loads(self.get("/learn/thread?card=1")[2])["pending"])
        code, out, _ = self.run_cli("learn-thread", "show", str(msg), "--claim")
        self.assertIn(f"Message {msg} was cancelled by the user", out)
        self.assertNotIn("Learn Assistant:", self.run_cli("queue")[1])
        self.answer(msg, "Late answer.")
        self.assertIn('<span class="msg-flag">after cancel</span>', self.get("/learn?card=1")[2])

    def test_an_applications_message_is_not_cancelled_from_the_learn_box(self) -> None:
        with self.conn() as conn:
            msg = db.add_message(conn, ACME, "Is this remote?")
        self.assertEqual(self.post_json("/learn/cancel", {"msg_id": str(msg), "card_id": "1"})[0], 400)
        self.assertEqual(self.status(msg), "pending")
