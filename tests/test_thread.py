"""The Agent thread on an application's side panel: storage, dashboard endpoints, the id-only
ping to the standing assistant and the CLI used by /ask. Made-up text only; dev3 is never
called (subprocess is mocked)."""

import io
import json
import os
import subprocess
from contextlib import closing
from unittest import mock
from urllib.parse import urlencode

from prep import assistant, db, web
from prep.config import load_settings
from tests.helpers import IsolatedTestCase
from tests.test_web import HOST, PAY, TODAY, WebTestCase

ACME = 1  # the fixtures' Acme Rockets (recruiter_call), which has an answered thread
STARK = 2  # Stark Example Ltd (applied), no thread
HOSTILE = '<script>alert(1)</script> & "quotes" --task seq:99 $(rm -rf ~) `id`\nline two'


class StorageTest(IsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.conn = db.connect(self.prep_db)
        self.addCleanup(self.conn.close)
        self.app = db.add_application(self.conn, "Initech Example", "Engineer", status="applied")

    def test_message_is_pending_until_replied(self) -> None:
        msg = db.add_message(self.conn, self.app, "  What is their stack?  ")
        [m] = db.thread(self.conn, self.app)
        self.assertEqual((m["id"], m["author"], m["body"], m["status"]), (msg, "user", "What is their stack?", "pending"))
        self.assertEqual([p["id"] for p in db.pending_messages(self.conn)], [msg])
        reply = db.reply_message(self.conn, self.app, msg, "Python and Go.")
        user, agent = db.thread(self.conn, self.app)
        self.assertEqual(user["status"], "answered")
        self.assertEqual((agent["id"], agent["author"], agent["reply_to"], agent["status"]), (reply, "agent", msg, None))
        self.assertEqual(db.pending_messages(self.conn), [])

    def test_body_rules(self) -> None:
        for body in ("", "   "):
            with self.assertRaises(ValueError):
                db.add_message(self.conn, self.app, body)
        db.add_message(self.conn, self.app, "x" * db.MAX_MESSAGE)
        with self.assertRaises(ValueError):
            db.add_message(self.conn, self.app, "x" * (db.MAX_MESSAGE + 1))
        with self.assertRaises(LookupError):
            db.add_message(self.conn, 999, "hello")

    def test_reply_only_once_and_only_to_a_user_message_on_that_application(self) -> None:
        msg = db.add_message(self.conn, self.app, "Question")
        other = db.add_application(self.conn, "Other Example", "Engineer")
        with self.assertRaises(LookupError):
            db.reply_message(self.conn, other, msg, "wrong thread")
        reply = db.reply_message(self.conn, self.app, msg, "Answer")
        with self.assertRaises(ValueError):
            db.reply_message(self.conn, self.app, msg, "Again")
        with self.assertRaises(LookupError):
            db.reply_message(self.conn, self.app, reply, "to the agent's own reply")
        self.assertEqual(len(db.thread(self.conn, self.app)), 2, "a refused reply stores nothing")

    def test_claim_once_and_stale_claims_are_pending_again(self) -> None:
        first = db.add_message(self.conn, self.app, "One")
        second = db.add_message(self.conn, self.app, "Two")
        self.assertEqual(db.claim_messages(self.conn, self.app), [first, second])
        self.assertEqual(db.claim_messages(self.conn, self.app), [], "a second session gets nothing")
        self.assertTrue(all(p["claimed"] for p in db.pending_messages(self.conn)))
        self.conn.execute("UPDATE application_messages SET claimed_at = '2000-01-01T00:00:00Z' WHERE id = ?", (first,))
        self.conn.commit()
        self.assertEqual(db.claim_messages(self.conn, self.app), [first])
        db.reply_message(self.conn, self.app, second, "Claimed messages can be answered")

    def test_a_thread_blocks_undo_interested(self) -> None:
        job = {"id": "ashby:example:1", "company": "Undo Example", "title": "Engineer"}
        app_id, _ = db.record_application(self.conn, "Undo Example", "Engineer", getjo_job_id=job["id"], status="to_apply")
        db.record_decision(self.conn, job, "interested")
        db.add_message(self.conn, app_id, "Worth applying?")
        with self.assertRaises(ValueError):
            db.undo_interested(self.conn, job["id"])


class EndpointTest(WebTestCase):
    def thread_rows(self, app_id: int) -> list[dict]:
        with self.conn() as conn:
            return db.thread(conn, app_id)

    def with_pay(self) -> None:
        with self.conn() as conn:
            db.update_application(conn, ACME, pay=PAY)
            db.add_message(conn, ACME, "Pending question")

    def test_panel_shows_the_thread_newest_at_the_bottom(self) -> None:
        _, _, body = self.get(f"/applications?app={ACME}")
        self.assertIn('<section class="agent"', body)
        q, a = body.index("Any London office?"), body.index("4 stages: recruiter call")
        self.assertLess(q, a)
        self.assertIn('<a href="https://careers.acme-rockets.example/process" rel="noreferrer noopener" target="_blank">', body)
        self.assertIn('action="/applications/message"', body)
        self.assertNotIn("data-pending", body)

    def test_send_stores_a_user_message_and_redirects_to_the_panel(self) -> None:
        with mock.patch.object(assistant, "notify_message_in_background") as bg:
            status, headers, _ = self.post("/applications/message", {
                "app_id": STARK, "body": "Good employer?", "author": "agent",
                "return": f"/applications?app={STARK}&view=list"})
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], f"/applications?app={STARK}&view=list&msg=message_sent")
        [m] = self.thread_rows(STARK)
        self.assertEqual((m["author"], m["status"], m["body"]), ("user", "pending", "Good employer?"),
                         "the form's author field is ignored")
        bg.assert_called_once_with(self.settings, STARK, m["id"])

    def test_send_as_json_returns_the_escaped_thread(self) -> None:
        with mock.patch.object(assistant, "notify_message_in_background"):
            status, _, out = self.post("/applications/message", {"app_id": STARK, "body": HOSTILE},
                                       {**HOST, "Accept": "application/json"})
        self.assertEqual(status, 200)
        reply = json.loads(out)
        self.assertTrue(reply["ok"])
        self.assertIn("data-pending", reply["html"])
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;quotes&quot;", reply["html"])
        self.assertNotIn("<script>", reply["html"])
        self.assertIn("Waiting for the agent: run /ask", reply["html"], "no assistant configured")

    def test_send_rejects_empty_long_and_unknown(self) -> None:
        json_headers = {**HOST, "Accept": "application/json"}
        for form in ({"app_id": STARK, "body": ""}, {"app_id": STARK, "body": "x" * (db.MAX_MESSAGE + 1)},
                     {"app_id": 999, "body": "hi"}, {"body": "hi"}):
            with self.subTest(form=str(form)[:40]):
                status, _, out = self.post("/applications/message", form, json_headers)
                self.assertEqual(status, 400)
                self.assertFalse(json.loads(out)["ok"])
        self.assertEqual(self.thread_rows(STARK), [])

    def test_a_long_pasted_email_fits_in_the_request(self) -> None:
        body = "Приглашаем на интервью — “вторник” · " * 800  # non-ASCII: URL-encoding makes it much larger
        body = body[: db.MAX_MESSAGE].strip()
        self.assertGreater(len(urlencode({"body": body}).encode()), 64 * 1024)
        with mock.patch.object(assistant, "notify_message_in_background"):
            status, _, _ = self.post("/applications/message", {"app_id": STARK, "body": body})
        self.assertEqual(status, 303)
        self.assertEqual(self.thread_rows(STARK)[0]["body"], body)

    def test_crlf_line_breaks_count_once(self) -> None:
        body = "line\r\n" * 4000  # 20,000 characters as the browser counts them (\n), 24,000 as sent
        self.assertGreater(len(body), db.MAX_MESSAGE)
        with mock.patch.object(assistant, "notify_message_in_background"):
            status, _, _ = self.post("/applications/message", {"app_id": STARK, "body": body})
        self.assertEqual(status, 303)
        self.assertNotIn("\r", self.thread_rows(STARK)[0]["body"])

    def test_thread_json_for_polling(self) -> None:
        status, headers, out = self.get(f"/applications/thread?app={ACME}")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["Content-Type"])
        reply = json.loads(out)
        self.assertEqual((reply["ok"], reply["pending"], reply["status"]), (True, False, "recruiter_call"))
        with self.conn() as conn:
            msg = db.add_message(conn, ACME, "And the salary band?")
        self.assertTrue(json.loads(self.get(f"/applications/thread?app={ACME}")[2])["pending"])
        with self.conn() as conn:
            db.reply_message(conn, ACME, msg, "Not public.")
            db.move_application(conn, ACME, "technical", TODAY)
        reply = json.loads(self.get(f"/applications/thread?app={ACME}")[2])
        self.assertEqual((reply["pending"], reply["status"]), (False, "technical"))
        self.assertIn("Not public.", reply["html"])
        self.assertEqual(self.get("/applications/thread?app=999")[0], 404)
        self.assertEqual(self.get("/applications/thread?app=x")[0], 400)

    def test_working_state_depends_on_the_assistant(self) -> None:
        with self.conn() as conn:
            db.add_message(conn, ACME, "Pending question")
        _, _, body = self.get(f"/applications?app={ACME}")
        self.assertIn("data-pending", body)
        self.assertIn("Waiting for the agent: run /ask", body)
        self.settings = self.settings.__class__(**{**self.settings.__dict__, "assistant_task": 42})
        _, _, body = self.get(f"/applications?app={ACME}")
        self.assertIn("Agent is working…", body)

    def test_linkify_escapes_and_keeps_only_http_links(self) -> None:
        html = web._linkify('see https://x.example/a?b=1&c="<img>" and javascript:alert(1), (https://y.example/p).')
        self.assertEqual(html, 'see <a href="https://x.example/a?b=1&amp;c=" rel="noreferrer noopener" target="_blank">'
                               'https://x.example/a?b=1&amp;c=</a>&quot;&lt;img&gt;&quot; and javascript:alert(1), '
                               '(<a href="https://y.example/p" rel="noreferrer noopener" target="_blank">'
                               'https://y.example/p</a>).')

    def test_no_pay_in_panel_or_json(self) -> None:
        self.with_pay()
        self.assertNotIn(PAY, self.get(f"/applications?app={ACME}")[2])
        self.assertNotIn(PAY, self.get(f"/applications/thread?app={ACME}")[2])

    def test_message_text_is_never_logged(self) -> None:
        with mock.patch.object(assistant, "notify_message_in_background"), self.assertLogs("prep.web", "INFO") as logs:
            self.post("/applications/message", {"app_id": STARK, "body": "SECRET-NOTE-123"})
        self.assertNotIn("SECRET-NOTE-123", "\n".join(logs.output))


class PingTest(IsolatedTestCase):
    def settings(self, **env: str):
        with mock.patch.dict(os.environ, env):
            return load_settings()

    def test_argv_holds_ids_only(self) -> None:
        s = self.settings(PREP_ASSISTANT_TASK="42", PREP_ASSISTANT_PROJECT="e942-abc")
        self.assertEqual(assistant.ask_command(s, 7, 31, "/bin/dev3"), [
            "/bin/dev3", "message", "--task", "seq:42", "--project", "e942-abc",
            "--subject", "Question on application 7", "/ask 7 31"])

    def test_message_text_never_reaches_dev3(self) -> None:
        with closing(db.connect(self.prep_db)) as conn:
            app = db.add_application(conn, "Ping Example", "Engineer")
        settings = self.settings(PREP_ASSISTANT_TASK="42")
        threads = []
        background = assistant.notify_message_in_background

        def keep(*args):  # the real background call, kept so the test can wait for it
            threads.append(background(*args))

        with mock.patch.object(assistant, "dev3_path", return_value="/bin/dev3"), \
                mock.patch.object(assistant, "notify_message_in_background", side_effect=keep), \
                mock.patch.object(subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            body = urlencode({"app_id": app, "body": HOSTILE}).encode()
            status, _, _ = web.handle(settings, "POST", "/applications/message", HOST, body, today=TODAY)
            threads[0].join(timeout=5)
        self.assertEqual(status, 303)
        run.assert_called_once()
        argv = run.call_args.args[0]
        with closing(db.connect(self.prep_db)) as conn:
            msg = db.thread(conn, app)[0]["id"]
        self.assertEqual(argv, ["/bin/dev3", "message", "--task", "seq:42", "--subject",
                                f"Question on application {app}", f"/ask {app} {msg}"])
        self.assertNotIn("seq:99", " ".join(argv))
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_unset_or_failing_never_raises_and_the_message_stays_pending(self) -> None:
        with mock.patch.object(subprocess, "run") as run:
            self.assertEqual(assistant.notify_message(self.settings(), 1, 2), 0)
            self.assertIsNone(assistant.notify_message_in_background(self.settings(), 1, 2))
            run.assert_not_called()
        s = self.settings(PREP_ASSISTANT_TASK="42")
        with mock.patch.object(assistant, "dev3_path", return_value="/bin/dev3"), \
                mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("dev3", 15)), \
                self.assertLogs("prep.assistant", "WARNING"):
            self.assertEqual(assistant.notify_message(s, 1, 2), 0)


class CliTest(IsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        with closing(db.connect(self.prep_db)) as conn:
            self.app = db.add_application(conn, "Globex Example", "Platform Engineer", status="technical",
                                          pay=PAY, link="https://jobs.example.com/globex/1")
            self.msg = db.add_message(conn, self.app, "Thanks for your time today.\nWe'd like to invite you to "
                                                      "a system design interview.")

    def test_show_prints_context_and_thread_but_never_pay_fields(self) -> None:
        code, out, _ = self.run_cli("thread", "show", str(self.app))
        self.assertEqual(code, 0)
        self.assertIn(f"Application #{self.app}: Globex Example — Platform Engineer [coding interview]", out)
        self.assertIn("companies/globex-example.md (missing", out)
        self.assertIn(f"fit/{self.app}-globex-example.md (missing", out)
        self.assertIn("cv.md (missing", out)
        self.assertIn("\n    Thanks for your time today.\n    We'd like to invite you to a system design interview.", out)
        self.assertIn("Thread (1 message, oldest first)", out)
        self.assertIn(f"thread reply {self.app} --to {self.msg}", out)
        self.assertNotIn("pay:", out.lower())
        self.assertNotIn(PAY, out, "applications.pay never shows")

    def test_claim_once(self) -> None:
        out = self.run_cli("thread", "show", str(self.app), "--claim")[1]
        self.assertIn(f"Claimed for this session: message {self.msg}", out)
        out = self.run_cli("thread", "show", str(self.app), "--claim")[1]
        self.assertIn("another session is already answering", out)

    def test_reply_from_stdin_and_file(self) -> None:
        with mock.patch("sys.stdin", io.StringIO("Moved to System design.\n")):
            code, out, _ = self.run_cli("thread", "reply", str(self.app), "--to", str(self.msg))
        self.assertEqual(code, 0)
        self.assertIn(f"message {self.msg} answered", out)
        path = self.tmp / "reply.txt"
        path.write_text("Again", encoding="utf-8")
        code, _, err = self.run_cli("thread", "reply", str(self.app), "--to", str(self.msg), "--file", str(path))
        self.assertEqual(code, 2)
        self.assertIn("already answered", err)
        with closing(db.connect(self.prep_db)) as conn:
            self.assertEqual([m["author"] for m in db.thread(conn, self.app)], ["user", "agent"])

    def test_pending_queue_and_today(self) -> None:
        out = self.run_cli("thread", "pending")[1]
        self.assertIn(f"message {self.msg} on #{self.app} Globex Example — Platform Engineer", out)
        out = self.run_cli("queue")[1]
        self.assertIn(f"Agent thread: 1 message waiting (applications {self.app}). Run /ask to answer.", out)
        self.assertNotIn("Globex", out, "the hook line holds ids only")
        self.assertIn("Agent thread: 1 message waiting", self.run_cli("today")[1])
        for argv in (("thread", "pending"), ("queue",), ("today",)):
            self.assertNotIn(PAY, self.run_cli(*argv)[1])
        with closing(db.connect(self.prep_db)) as conn:
            db.reply_message(conn, self.app, self.msg, "Done")
        self.assertIn("No thread messages waiting.", self.run_cli("thread", "pending")[1])
        self.assertNotIn("Agent thread", self.run_cli("queue")[1])

    def test_queue_on_a_database_without_the_thread_table(self) -> None:
        with closing(db.connect(self.prep_db)) as conn:
            db.queue_request(conn, self.app, "research")
            conn.execute("DROP TABLE application_messages")
            conn.commit()
        out = self.run_cli("queue")[1]
        self.assertIn("Agent work pending: 1 company research", out)
        self.assertNotIn("Agent thread", out)
