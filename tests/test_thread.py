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
        self.conn.execute("UPDATE agent_messages SET claimed_at = '2000-01-01T00:00:00Z' WHERE id = ?", (first,))
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

    def test_agent_box_under_the_list_not_in_the_panel(self) -> None:
        _, _, body = self.get(f"/applications?app={ACME}")
        box = body[body.index('<section class="card card-agent agent" id="agent"'):body.index('<aside class="panel"')]
        self.assertGreater(body.index('id="agent"'), body.index('<table id="app-list">'))  # under the list
        panel = body[body.index('<aside class="panel"'):]
        self.assertNotIn("Any London office?", panel)  # not in the card
        self.assertNotIn('action="/applications/message"', panel)
        q, a = box.index("Any London office?"), box.index("4 stages: recruiter call")
        self.assertLess(q, a)  # newest at the bottom
        self.assertIn(f'<a class="msg-about" href="/applications?app={ACME}">Acme Rockets</a>', box)
        self.assertIn(f'name="app_id" value="{ACME}" checked> About Acme Rockets', box)
        self.assertIn('data-filter-chat="job"', box)
        self.assertIn('href="#agent" data-ask-agent>Ask the agent</a>', panel)  # phones: from the sheet to the box
        css = self.get("/static/style.css")[2]
        self.assertIn("body.is-asking .panel { display: none; }", css)
        body = self.get("/applications")[2]  # no panel: about the whole search
        box = body[body.index('id="agent"'):body.index("<dialog")]
        self.assertIn("About: the whole search", box)
        self.assertNotIn('name="app_id"', box)
        self.assertIn('<a href="https://careers.acme-rockets.example/process" rel="noreferrer noopener" target="_blank">', box)
        self.assertIn('action="/applications/message"', box)
        self.assertNotIn("data-pending", box)

    def test_a_message_about_the_whole_search(self) -> None:
        with mock.patch.object(assistant, "notify_message_in_background") as bg:
            status, _, out = self.post("/applications/message", {"body": "Which tasks need me?"},
                                       {**HOST, "Accept": "application/json"})
        self.assertEqual(status, 200)
        with self.conn() as conn:
            [m] = db.pending_messages(conn)
        self.assertIsNone(m["application_id"])
        bg.assert_called_once_with(self.settings, None, m["id"])
        self.assertIn('<span class="msg-about">all jobs</span>', json.loads(out)["html"])

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
                     {"app_id": 999, "body": "hi"}, {"app_id": "x", "body": "hi"}):
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
        reply = json.loads(self.get("/applications/thread")[2])  # no panel open
        with self.conn() as conn:
            count = len(db.thread(conn))
        self.assertEqual((reply["pending"], reply["status"], reply["messages"]), (False, None, count))
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
        self.assertIn(f"Messages about #{self.app} (1 message, oldest first)", out)
        self.assertIn(f"thread reply --to {self.msg}", out)
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
            conn.execute("DROP TABLE agent_messages")
            conn.execute("DROP TABLE application_messages")
            conn.commit()
        out = self.run_cli("queue")[1]
        self.assertIn("Agent work pending: 1 company research", out)
        self.assertNotIn("Agent thread", out)


class GeneralThreadTest(IsolatedTestCase):
    """Messages about the whole search (no application) and the move from the per-application table."""

    def setUp(self) -> None:
        super().setUp()
        self.conn = db.connect(self.prep_db)
        self.addCleanup(self.conn.close)
        self.app, _ = db.record_application(self.conn, "Globex Example", "Platform Engineer", status="applied")

    def test_general_message_claim_and_reply(self) -> None:
        general = db.add_message(self.conn, None, "Which tasks need me?")
        about_app = db.add_message(self.conn, self.app, "Good employer?")
        self.assertEqual(db.claim_messages(self.conn, None), [general], "only the general one")
        self.assertEqual(db.claim_messages(self.conn, self.app), [about_app])
        with self.assertRaises(LookupError):
            db.reply_message(self.conn, self.app, general, "wrong application")
        reply = db.reply_message(self.conn, None, general, "1. Chase Globex")
        self.assertEqual(db.message_application(self.conn, reply), None)
        whole = db.thread(self.conn)
        self.assertEqual([m["application_id"] for m in whole], [None, self.app, None])
        self.assertEqual([m["company"] for m in whole], [None, "Globex Example", None])
        self.assertEqual(len(db.thread(self.conn, self.app)), 1)
        self.assertEqual([m["id"] for m in db.thread(self.conn, limit=2)], [about_app, reply])

    def test_cli_show_and_reply_without_an_application(self) -> None:
        msg = db.add_message(self.conn, None, "Which tasks need me?")
        db.add_task(self.conn, "Chase recruiter", "admin", "2000-01-01", self.app)
        code, out, _ = self.run_cli("thread", "show", "--claim")
        self.assertEqual(code, 0)
        self.assertIn("Open applications:", out)
        self.assertIn(f"#{self.app} Globex Example — Platform Engineer [applied]", out)
        self.assertIn("Tasks that need you", out)
        self.assertIn("1. [overdue · due 2000-01-01] Chase recruiter", out)
        self.assertIn(f"Claimed for this session: message {msg}", out)
        self.assertIn(f"thread reply --to {msg}", out)
        self.assertIn("about the whole search (/ask general)", self.run_cli("thread", "pending")[1])
        self.assertIn("general", self.run_cli("queue")[1])
        with mock.patch("sys.stdin", io.StringIO("1. Chase recruiter (overdue).")):
            code, out, _ = self.run_cli("thread", "reply", "--to", str(msg))
        self.assertEqual(code, 0)
        self.assertIn("about the whole search", out)
        self.assertEqual(self.run_cli("thread", "reply", str(self.app), "--to", str(msg))[0], 2, "APP must match")

    def test_ask_command_for_the_whole_search(self) -> None:
        s = load_settings().__class__(**{**load_settings().__dict__, "assistant_task": 22})
        argv = assistant.ask_command(s, None, 31, "/bin/dev3")
        self.assertEqual(argv[-1], "/ask general 31")
        self.assertIn("Question on the whole search", argv)

    def test_old_thread_rows_are_copied_once_with_their_ids(self) -> None:
        self.conn.execute("INSERT INTO application_messages (id, application_id, author, body, status, created_at) "
                          "VALUES (40, ?, 'user', 'Old question', 'answered', '2026-01-01T09:00:00Z')", (self.app,))
        self.conn.execute("INSERT INTO application_messages (id, application_id, author, body, reply_to, created_at) "
                          "VALUES (41, ?, 'agent', 'Old answer', 40, '2026-01-01T09:05:00Z')", (self.app,))
        self.conn.commit()
        for _ in range(2):
            with closing(db.connect(self.prep_db)) as conn:
                rows = db.thread(conn, self.app)
        self.assertEqual([(m["id"], m["reply_to"], m["body"]) for m in rows],
                         [(40, None, "Old question"), (41, 40, "Old answer")])
        self.assertGreater(db.add_message(self.conn, None, "New"), 41, "new ids continue after the copied ones")


class ReviewOrderTest(IsolatedTestCase):
    def test_order_and_groups(self) -> None:
        with closing(db.connect(self.prep_db)) as conn:
            today = "2026-03-10"
            app = db.add_application(conn, "Acme", "Engineer", status="applied")
            db.move_application(conn, app, "technical", today, interview_date="2026-03-14")
            other = db.add_application(conn, "Globex", "Engineer", status="to_apply")
            ids = {
                "undated": db.add_task(conn, "Undated", "admin", None, None),
                "later": db.add_task(conn, "Later", "admin", "2026-03-20", other),
                "apply": db.add_task(conn, "Review research and apply", "apply", "2026-03-12", other, stage="to_apply"),
                "today": db.add_task(conn, "Today", "admin", today, None),
                "overdue": db.add_task(conn, "Overdue", "prep", "2026-03-01", other),
            }
            order = db.review_tasks(conn, today)
            db.queue_request(conn, other, "research")
        groups = [t["group"] for t in order]
        self.assertEqual(groups, sorted(groups, key=db.REVIEW_GROUPS.index), "groups in REVIEW_GROUPS order")
        titles = [t["title"] for t in order]
        self.assertEqual(titles[:2], ["Overdue", "Today"])
        self.assertTrue(all(t["group"] == "interview prep" for t in order[2:4]))  # Coding practice, tech stack
        self.assertEqual(order[2]["interview_date"], "2026-03-14")
        self.assertEqual(titles[4:], ["Review research and apply", "Later", "Undated"])
        self.assertEqual(order[-1]["company"], None)
        code, out, _ = self.run_cli("review")
        self.assertEqual(code, 0)
        self.assertIn("the top 3 in order:", out)  # the three most urgent only
        self.assertIn("  3. [", out)
        self.assertNotIn("  4. [", out)
        self.assertRegex(out, r"… and \d+ more \(python -m prep.cli review --all\)")
        out = self.run_cli("review", "--all")[1]
        self.assertIn("  4. [", out)
        self.assertNotIn("more (python", out)
        self.assertIn("The agent is working on (not waiting on you): company research for #", out)
