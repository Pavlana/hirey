"""Stop in the Applications Assistant box: shown on the waiting line whenever the box waits (a message,
or the open panel's queued work); cancels every waiting Applications message (never a Learn one) and
the open panel's agent requests and job brief, keeping the rows. Also the "no reply yet" flag and the
CLI's `thread close` and reply warning. dev3 is never called. Fictional data only."""

import json
import tempfile
from pathlib import Path

from prep import db, web
from tests.test_learn_agent import JSON
from tests.test_resend_cancel import ResendCancelBase
from tests.test_thread import ACME, GLOBEX_JOB, STARK

STOP = 'action="/applications/stop"'


class StopTest(ResendCancelBase):
    def post_json(self, path: str, form: dict) -> tuple[int, dict]:
        status, _, body = self.post(path, form, JSON)
        return status, json.loads(body)

    def test_no_stop_when_nothing_waits(self) -> None:
        self.assertNotIn(STOP, self.get(f"/?app={ACME}")[2])

    def test_stop_cancels_every_waiting_message_but_not_learn(self) -> None:
        with self.conn() as conn:
            a = db.add_message(conn, ACME, "Is this remote?")
            b = db.add_message(conn, None, "Which tasks need me?")
            self.assertTrue(db.claim_message(conn, b))
            learn = db.add_learn_message(conn, None, "Quiz me.")
        body = self.get("/")[2]
        self.assertIn(STOP, body)
        self.assertIn("2 waiting", body)
        status, reply = self.post_json("/applications/stop", {})
        self.assertEqual(status, 200)
        self.assertIn("Stopped", reply["message"])
        self.assertNotIn("data-pending", reply["html"])
        self.assertNotIn(STOP, reply["html"])
        self.assertEqual((self.status(a), self.status(b), self.status(learn)), ("cancelled", "cancelled", "pending"))
        self.assertFalse(json.loads(self.get("/applications/thread")[2])["pending"])

    def test_stop_clears_the_open_panels_requests_only(self) -> None:
        with self.conn() as conn:
            db.queue_request(conn, ACME, "cv_fit")
            rid, _ = db.queue_request(conn, STARK, "research")
            db.claim_request(conn, rid)
        body = self.get(f"/?app={ACME}")[2]
        self.assertIn("CV fit", body)
        self.assertIn(STOP, body)
        self.assertIn(f'name="panel_app" value="{ACME}"', body)
        self.assertTrue(json.loads(self.get(f"/applications/thread?app={ACME}")[2])["pending"])
        status, reply = self.post_json("/applications/stop", {"panel_app": str(ACME)})
        self.assertEqual(status, 200)
        self.assertNotIn("Working on", reply["html"])
        self.assertFalse(json.loads(self.get(f"/applications/thread?app={ACME}")[2])["pending"])
        with self.conn() as conn:
            self.assertEqual(db.open_requests(conn, ACME), [])
            self.assertEqual(len(db.open_requests(conn, STARK)), 1)  # another application's work is untouched
            self.assertEqual(conn.execute("SELECT status FROM agent_requests WHERE application_id = ?",
                                          (ACME,)).fetchone()[0], "cancelled")  # kept, marked
            _, again = db.queue_request(conn, ACME, "cv_fit")
            self.assertTrue(again)  # CV fit can be queued again

    def test_stop_clears_a_job_panels_brief_and_it_can_be_queued_again(self) -> None:
        with self.conn() as conn:
            db.queue_brief(conn, GLOBEX_JOB)
            work = web._panel_work(self.settings, conn, None, GLOBEX_JOB)
            self.assertEqual(len(work), 1)
            self.assertTrue(work[0].startswith("Job brief ("), work)
        status, reply = self.post_json("/applications/stop", {"panel_job": GLOBEX_JOB})
        self.assertEqual(status, 200)
        with self.conn() as conn:
            self.assertEqual(db.open_briefs(conn, GLOBEX_JOB), [])
            self.assertEqual(conn.execute("SELECT status FROM brief_requests WHERE getjo_job_id = ?",
                                          (GLOBEX_JOB,)).fetchone()[0], "cancelled")
            self.assertTrue(db.queue_brief(conn, GLOBEX_JOB))  # Write brief opens it again

    def test_stop_with_an_unknown_panel_still_cancels_messages(self) -> None:
        with self.conn() as conn:
            msg = db.add_message(conn, ACME, "Is this remote?")
        self.assertEqual(self.post_json("/applications/stop", {"panel_app": "99999"})[0], 200)
        self.assertEqual(self.status(msg), "cancelled")

    def test_stop_needs_same_origin(self) -> None:
        from tests.test_learn_agent import FORM
        with self.conn() as conn:
            msg = db.add_message(conn, ACME, "Is this remote?")
        self.assertEqual(self.post("/applications/stop", {}, {**FORM, "Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.status(msg), "pending")

    def test_no_reply_yet_flag_on_an_older_waiting_message(self) -> None:
        with self.conn() as conn:
            old = db.add_message(conn, None, "Which tasks need me?")
            db.claim_message(conn, old)
            new = db.add_message(conn, None, "Which tasks need me?")
            db.reply_message(conn, None, new, "Three.")
            self.assertEqual(db.unreplied_before(conn, new), [old])
        body = self.get("/")[2]
        self.assertIn("no reply yet", body)
        self.assertIn("1 waiting", body)

    def test_reply_warns_about_an_older_unreplied_message_and_close_cancels_it(self) -> None:
        with self.conn() as conn:
            old = db.add_message(conn, ACME, "Is this remote?")
            db.claim_message(conn, old)
            new = db.add_message(conn, ACME, "Is this remote?")
            other = db.add_message(conn, STARK, "Who is the CTO?")  # another subject: not listed
        with tempfile.TemporaryDirectory() as tmp:
            reply = Path(tmp) / "reply.txt"
            reply.write_text("Hybrid, two days.", encoding="utf-8")
            code, out, _ = self.run_cli("thread", "reply", "--to", str(new), "--file", str(reply))
        self.assertEqual(code, 0)
        self.assertIn(f"Also waiting, no reply: message {old}", out)
        self.assertIn(f"thread close {old}", out)
        self.assertNotIn(f"message {other}", out)
        code, out, _ = self.run_cli("thread", "close", str(old))
        self.assertEqual(code, 0)
        self.assertEqual((self.status(old), self.status(other)), ("cancelled", "pending"))
        self.assertNotEqual(self.run_cli("thread", "close", str(old))[0], 0)  # no longer waiting: refused


if __name__ == "__main__":
    import unittest
    unittest.main()
