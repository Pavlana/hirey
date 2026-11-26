"""Assistant scaling, step 2: the recruiter communication route. "Draft a reply" requests (paste-in only)
are stored as thread messages, routed to the comms worker (falling back to the Applications Assistant),
left alone by /ask's bulk claims, answered by `reply save` with a draft the box shows with Copy, and
guarded against pay the user did not raise. dev3 is never called. Fictional data and figures only."""

import os
from unittest import mock

from prep import assistant, db
from tests.test_resend_cancel import ResendCancelBase
from tests.test_thread import ACME, GLOBEX_JOB
from tests.test_workers import settings

PASTED = "Hi Sam, thanks for applying to Acme Rockets. Are you free for a 30 min call this week?"


class RouterTest(ResendCancelBase):
    def test_comms_goes_to_the_worker(self) -> None:
        s = settings(assistant_task=9, workers=(("comms", 72),))
        self.assertEqual(assistant.target(s, "comms"), (72, "Comms worker"))
        argv = assistant.ask_command(s, 4, 11, "/bin/dev3", kind="comms")
        self.assertEqual(argv, ["/bin/dev3", "message", "--task", "seq:72", "--subject",
                                "Draft a reply on application 4", "/reply 4 11"])
        self.assertEqual(assistant.ask_command(s, None, 12, "/bin/dev3", job=True, kind="comms")[-1], "/reply job 12")
        self.assertEqual(assistant.ask_command(s, None, 13, "/bin/dev3", kind="comms")[-1], "/reply general 13")
        self.assertEqual(assistant.ask_command(s, 4, 11, "/bin/dev3")[3:4], ["seq:9"])  # /ask stays

    def test_falls_back_to_the_applications_assistant(self) -> None:
        s = settings(assistant_task=9)
        self.assertEqual(assistant.target(s, "comms"), (9, "Applications Assistant"))
        self.assertEqual(assistant.ask_command(s, 4, 11, "/bin/dev3", kind="comms")[3], "seq:9")
        self.assertIsNone(assistant.message_target(settings(), "comms"))  # neither set: nothing is pinged

    def test_notify_sends_ids_only(self) -> None:
        s = settings(workers=(("comms", 72),))  # no assistant_task: the worker alone is enough
        with mock.patch.object(assistant, "dev3_path", return_value="/bin/dev3"), \
                mock.patch.object(assistant, "_send", return_value=1) as send:
            self.assertEqual(assistant.notify_message(s, 4, 11, kind="comms"), 1)
            self.assertEqual(assistant.notify_message(s, 4, 11), 0)  # an /ask message has no task here
        argv = send.call_args[0][0][0]
        self.assertEqual(argv[3], "seq:72")
        self.assertFalse(any("Acme" in a for a in argv))

    def test_comms_setting_and_env(self) -> None:
        from prep.config import load_settings
        path = self.tmp / "s.toml"
        path.write_text("[workers]\ncomms = 72\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"PREP_WORKER_COMMS": "73"}):
            self.assertEqual(load_settings(path).workers, (("comms", 73),))
        os.environ.pop("PREP_WORKER_COMMS")
        self.assertEqual(load_settings(path).workers, (("comms", 72),))


class StorageTest(ResendCancelBase):
    def test_request_shape(self) -> None:
        with self.conn() as conn:
            msg = db.add_draft_request(conn, ACME, "reply", "linkedin", PASTED, "free Tue after 2")
            m = db.get_message(conn, msg)
        self.assertEqual((m["comms"], m["channel"], m["status"], m["author"]), ("reply", "linkedin", "pending", "user"))
        self.assertTrue(m["body"].startswith("Draft a LinkedIn reply (/reply)."))
        self.assertEqual(db.request_parts(m["body"]), {"notes": "free Tue after 2", "pasted": PASTED})

    def test_request_is_validated(self) -> None:
        with self.conn() as conn:
            for args in (("reply", "linkedin", ""), ("call", "email", PASTED), ("reply", "fax", PASTED)):
                with self.subTest(args=args), self.assertRaises(ValueError):
                    db.add_draft_request(conn, ACME, *args)
            with self.assertRaises(LookupError):
                db.add_draft_request(conn, 999, "reply", "email", PASTED)
            db.add_draft_request(conn, ACME, "chase", "email", notes="no answer since the call")  # no paste needed

    def test_bulk_claims_leave_draft_requests(self) -> None:
        with self.conn() as conn:
            draft = db.add_draft_request(conn, ACME, "reply", "email", PASTED)
            ask = db.add_message(conn, ACME, "Is this remote?")
            self.assertEqual(db.claim_messages(conn, ACME), [ask])
            self.assertTrue(db.claim_message(conn, draft))

    def test_an_ask_reply_never_offers_to_close_a_draft_request(self) -> None:
        with self.conn() as conn:
            draft = db.add_draft_request(conn, ACME, "reply", "email", PASTED)
            ask = db.add_message(conn, ACME, "Is this remote?")
            self.assertEqual(db.unreplied_before(conn, ask), [])
            self.assertEqual(db.unreplied_before(conn, draft), [])

    def test_save_draft_email_and_linkedin(self) -> None:
        with self.conn() as conn:
            email = db.add_draft_request(conn, ACME, "reply", "email", PASTED)
            with self.assertRaises(ValueError):
                db.save_draft(conn, email, "Hi Jo, thanks.")  # an email needs a subject
            reply = db.save_draft(conn, email, "Hi Jo,\nThanks.\nSam", "Re: Platform role", "Proposed: Recruiter call")
            r = db.get_message(conn, reply)
            self.assertEqual((r["author"], r["reply_to"], r["comms"], r["channel"]), ("agent", email, "reply", "email"))
            self.assertEqual((r["draft_subject"], r["draft_body"], r["body"]),
                             ("Re: Platform role", "Hi Jo,\nThanks.\nSam", "Proposed: Recruiter call"))
            self.assertEqual(db.get_message(conn, email)["status"], "answered")
            linkedin = db.add_draft_request(conn, ACME, "reply", "linkedin", PASTED)
            with self.assertRaises(ValueError):
                db.save_draft(conn, linkedin, "Hi Jo!", "Re: x")  # no subject on LinkedIn
            with self.assertRaises(LookupError):
                db.save_draft(conn, db.add_message(conn, ACME, "Is this remote?"), "Hi")  # not a draft request

    def test_pay_guard(self) -> None:
        with self.conn() as conn:
            plain = db.add_draft_request(conn, ACME, "reply", "email", PASTED)
            for draft in ("Happy to talk; my salary expectation is flexible.", "I am looking for £100k."):
                with self.subTest(draft=draft), self.assertRaises(ValueError):
                    db.save_draft(conn, plain, draft, "Re: call")
            asked = db.add_draft_request(conn, ACME, "reply", "email", PASTED + " What are your salary expectations?")
            with self.assertRaises(ValueError):  # they asked, but a figure must come from the user's notes
                db.save_draft(conn, asked, "I would expect £100k.", "Re: call")
            db.save_draft(conn, asked, "Could you share the salary range for the role first?", "Re: call")
            given = db.add_draft_request(conn, ACME, "reply", "email", PASTED, "say £100k base")
            db.save_draft(conn, given, "I am looking for £100k base.", "Re: call")


class DashboardTest(ResendCancelBase):
    def test_form_in_panels_and_box(self) -> None:
        body = self.get(f"/?app={ACME}")[2]
        self.assertEqual(body.count('action="/applications/draft"'), 2)  # the panel and the box (whole search)
        self.assertIn(f'<input type="hidden" name="app_id" value="{ACME}">', body.split('action="/applications/draft"')[1])
        self.assertIn('name="channel" value="linkedin" checked', body)

    def test_post_stores_and_routes_to_comms(self) -> None:
        self.settings = self.settings.__class__(**{**self.settings.__dict__, "workers": (("comms", 72),)})
        status, reply = self.post_json("/applications/draft", {"app_id": str(ACME), "comms": "reply", "channel": "email",
                                                               "pasted": PASTED, "notes": "ask about hybrid"})
        self.assertEqual(status, 200, reply)
        with self.conn() as conn:
            msg = db.thread(conn)[-1]
        self.assertEqual((msg["comms"], msg["channel"], msg["application_id"]), ("reply", "email", ACME))
        assistant.notify_message_in_background.assert_called_once_with(self.settings, ACME, msg["id"], job=False,
                                                                        kind="comms", seq=72)
        self.assertIn("→ Comms worker", reply["html"])
        self.assertIn('class="msg-channel msg-channel-email">Email</span>', reply["html"])

    def test_post_is_validated(self) -> None:
        for form in ({"comms": "reply", "channel": "email", "pasted": ""}, {"comms": "x", "channel": "email", "pasted": "a"},
                     {"comms": "reply", "channel": "sms", "pasted": "a"}):
            with self.subTest(form=form):
                self.assertEqual(self.post_json("/applications/draft", form)[0], 400)

    def test_draft_shows_with_copy_and_is_escaped(self) -> None:
        with self.conn() as conn:
            email = db.add_draft_request(conn, ACME, "reply", "email", "<script>alert(1)</script>")
            db.save_draft(conn, email, "Hi <b>Jo</b>,\nThanks & see you.", "Re: <i>call</i>")
            li = db.add_draft_request(conn, ACME, "thanks", "linkedin", notes="thank them")
            li_reply = db.save_draft(conn, li, "Thanks for your time today, Jo.")
        body = self.get("/")[2]
        self.assertNotIn("<script>alert(1)", body)
        self.assertNotIn("<b>Jo</b>", body)
        self.assertIn("Hi &lt;b&gt;Jo&lt;/b&gt;,\nThanks &amp; see you.", body)
        self.assertIn("Re: &lt;i&gt;call&lt;/i&gt;", body)
        self.assertIn('data-copy="draft-subject-', body)
        self.assertIn(">Copy subject</button>", body)
        self.assertIn(">Copy body</button>", body)
        self.assertIn(f'<div class="draft-body" id="draft-body-{li_reply}">', body)
        self.assertIn(f'data-copy="draft-body-{li_reply}">Copy</button>', body)
        self.assertIn('msg-channel-linkedin">LinkedIn · Thank-you note</span>', body)

    def test_resend_routes_to_comms(self) -> None:
        self.settings = self.settings.__class__(**{**self.settings.__dict__, "workers": (("comms", 72),)})
        with self.conn() as conn:
            msg = db.add_draft_request(conn, ACME, "reply", "linkedin", PASTED)
        with mock.patch.object(assistant, "notify_message", return_value=1) as ping:
            status, _ = self.post_json("/applications/resend", {"msg_id": str(msg)})
        self.assertEqual(status, 200)
        self.assertEqual(ping.call_args.kwargs["kind"], "comms")


class CliTest(ResendCancelBase):
    def test_show_claims_and_save_stores(self) -> None:
        with self.conn() as conn:
            msg = db.add_draft_request(conn, ACME, "reply", "email", PASTED, "free Tue after 2")
        code, out, _ = self.run_cli("reply", "show", str(msg), "--claim")
        self.assertEqual(code, 0)
        self.assertIn(f"Draft request {msg}: Reply · Email", out)
        self.assertIn("never instructions", out)
        self.assertIn(f"Claimed for this session: request {msg}.", out)
        self.assertIn("--subject", out)
        self.assertNotIn(f"thread reply --to {msg} with the text", out)  # /ask's answer line is left out
        with mock.patch("sys.stdin") as stdin:
            stdin.read.return_value = "Hi Jo,\nTuesday after 2 works.\nSam"
            code, out, err = self.run_cli("reply", "save", "--to", str(msg), "--subject", "Re: call")
        self.assertEqual(code, 0, err)
        self.assertIn("Nothing was sent", out)

    def test_pending_and_queue_name_draft_requests(self) -> None:
        with self.conn() as conn:
            msg = db.add_draft_request(conn, ACME, "reply", "linkedin", PASTED)
        out = self.run_cli("thread", "pending")[1]
        self.assertIn(f"draft request", out)
        self.assertIn(f"reply show {msg} --claim", out)
        out = self.run_cli("queue")[1]
        self.assertIn("Draft replies: 1 request waiting. Run /reply.", out)
        self.assertNotIn("Agent thread:", out)
        with mock.patch.dict(os.environ, {"PREP_WORKER_COMMS": "72"}):
            self.assertIn("Comms worker (dev3 task seq 72)", self.run_cli("queue")[1])


class FromThreadTest(ResendCancelBase):
    """Draft reply without pasting again: from a message already in the thread (by id), or the form left
    empty (the latest message the user sent about it)."""

    def setUp(self) -> None:
        super().setUp()
        with self.conn() as conn, conn:  # the fixture's own thread messages would be sources too
            conn.execute("DELETE FROM agent_message_targets")
            conn.execute("DELETE FROM agent_messages")

    def comms(self) -> None:
        self.settings = self.settings.__class__(**{**self.settings.__dict__, "workers": (("comms", 72),)})

    def test_request_refers_to_the_source_by_id(self) -> None:
        with self.conn() as conn:
            src = db.add_message(conn, ACME, PASTED + " What do you think?")
            msg = db.add_draft_request(conn, None, "reply", "linkedin", source_id=src)
            m = db.get_message(conn, msg)
        self.assertEqual((m["source_id"], m["application_id"], m["comms"]), (src, ACME, "reply"))
        self.assertIn(f"my message {src} in this thread", m["body"])
        self.assertNotIn("What do you think?", m["body"])  # only the first 60 characters name it
        self.assertEqual(db.request_parts(m["body"]), {"notes": "", "pasted": ""})

    def test_only_the_users_own_plain_messages_are_sources(self) -> None:
        with self.conn() as conn:
            src = db.add_message(conn, ACME, PASTED)
            agent = db.reply_message(conn, ACME, src, "Looks good.")
            draft = db.add_draft_request(conn, ACME, "reply", "email", PASTED)
            cv = db.add_message(conn, None, "Shorten my summary", topic="cv")
            mock_start = db.add_message(conn, ACME, db.MOCK_START.format(label="Recruiter call", stage="recruiter_call"))
            for bad in (agent, draft, cv, mock_start, 999):
                with self.subTest(msg=bad), self.assertRaises(LookupError):
                    db.add_draft_request(conn, None, "reply", "email", source_id=bad)
            with self.assertRaises(ValueError):
                db.add_draft_request(conn, None, "reply", "email", PASTED, source_id=src)  # one or the other

    def test_draft_source_is_the_latest_plain_message_on_that_subject(self) -> None:
        with self.conn() as conn:
            self.assertIsNone(db.draft_source(conn, ACME))
            first = db.add_message(conn, ACME, "First paste")
            latest = db.add_message(conn, ACME, "Second paste")
            db.add_draft_request(conn, ACME, "reply", "email", PASTED)  # a draft request is never the source
            general = db.add_message(conn, None, "General question")
            job = db.add_message(conn, None, "About the offer", job_id=GLOBEX_JOB)
            self.assertEqual(db.draft_source(conn, ACME)["id"], latest)
            self.assertEqual(db.draft_source(conn)["id"], general)
            self.assertEqual(db.draft_source(conn, job_id=GLOBEX_JOB)["id"], job)
            db.cancel_message(conn, latest)
            self.assertEqual(db.draft_source(conn, ACME)["id"], first)

    def test_pay_guard_reads_the_source(self) -> None:
        with self.conn() as conn:
            plain = db.add_draft_request(conn, None, "reply", "email", source_id=db.add_message(conn, ACME, PASTED))
            with self.assertRaises(ValueError):
                db.save_draft(conn, plain, "Happy to discuss salary.", "Re: call")
            asked = db.add_draft_request(conn, None, "reply", "email",
                                         source_id=db.add_message(conn, ACME, "They ask: salary expectations? £90k"))
            with self.assertRaises(ValueError):  # a figure still needs the user's notes, even with one in the source
                db.save_draft(conn, asked, "I would expect £90k.", "Re: call")
            db.save_draft(conn, asked, "Could you share the salary range first?", "Re: call")

    def test_action_row_under_own_messages_only(self) -> None:
        with self.conn() as conn:
            src = db.add_message(conn, ACME, "<b>Hi</b> " + PASTED)
            db.reply_message(conn, ACME, src, "Looks good.")
            db.add_draft_request(conn, ACME, "reply", "email", PASTED)
        body = self.get("/")[2]
        self.assertEqual(body.count('action="/applications/draft-from"'), 1)
        self.assertIn(f'<input type="hidden" name="msg_id" value="{src}">', body)
        self.assertIn('name="channel" value="linkedin"', body)
        self.assertIn('name="channel" value="email"', body)
        self.assertNotIn("<b>Hi</b>", body)

    def test_draft_from_message_routes_to_comms(self) -> None:
        self.comms()
        with self.conn() as conn:
            src = db.add_message(conn, ACME, PASTED)
        status, reply = self.post_json("/applications/draft-from", {"msg_id": str(src), "channel": "email"})
        self.assertEqual(status, 200, reply)
        with self.conn() as conn:
            msg = db.thread(conn)[-1]
            full = db.get_message(conn, msg["id"])
        self.assertEqual((msg["comms"], msg["channel"], msg["application_id"], full["source_id"]),
                         ("reply", "email", ACME, src))
        assistant.notify_message_in_background.assert_called_once_with(self.settings, ACME, msg["id"], job=False,
                                                                        kind="comms", seq=72)
        self.assertIn("→ Comms worker", reply["html"])

    def test_draft_from_message_falls_back_to_the_assistant(self) -> None:
        self.with_tasks()
        with self.conn() as conn:
            src = db.add_message(conn, None, PASTED)
        status, reply = self.post_json("/applications/draft-from", {"msg_id": str(src), "channel": "linkedin"})
        self.assertEqual(status, 200, reply)
        self.assertIn("→ Applications Assistant", reply["html"])

    def test_draft_from_message_is_validated(self) -> None:
        with self.conn() as conn:
            src = db.add_message(conn, ACME, PASTED)
            agent = db.reply_message(conn, ACME, src, "ok")
        for form in ({"msg_id": str(src), "channel": "sms"}, {"msg_id": str(agent), "channel": "email"},
                     {"msg_id": "999", "channel": "email"}, {"channel": "email"}):
            with self.subTest(form=form):
                self.assertEqual(self.post_json("/applications/draft-from", form)[0], 400)

    def test_empty_form_uses_the_named_message(self) -> None:
        with self.conn() as conn:
            src = db.add_message(conn, ACME, "Hi Sam, thanks for applying. A call on Tuesday?")
        body = self.get(f"/?app={ACME}")[2]
        form = body.split('action="/applications/draft"')[-1]  # the panel's
        self.assertIn(f'name="source_id" value="{src}"', form)
        self.assertIn("Left empty, a reply uses your message of", form)
        self.assertIn("“Hi Sam, thanks for applying. A call on Tuesday?”", form)
        with self.conn() as conn:
            db.add_message(conn, ACME, "A newer question")  # sent after the page showed the hint
        status, reply = self.post_json("/applications/draft", {"app_id": str(ACME), "comms": "reply", "channel": "email",
                                                               "pasted": "", "source_id": str(src)})
        self.assertEqual(status, 200, reply)
        with self.conn() as conn:
            self.assertEqual(db.get_message(conn, db.thread(conn)[-1]["id"])["source_id"], src)

    def test_empty_form_without_a_message_asks_for_the_paste(self) -> None:
        body = self.get(f"/?app={ACME}")[2]
        self.assertNotIn("Left empty, a reply uses", body)
        status, _ = self.post_json("/applications/draft", {"app_id": str(ACME), "comms": "reply", "channel": "email"})
        self.assertEqual(status, 400)
        status, _ = self.post_json("/applications/draft", {"app_id": str(ACME), "comms": "chase", "channel": "email"})
        self.assertEqual(status, 200)  # a follow-up needs no message

    def test_reply_show_prints_the_source(self) -> None:
        with self.conn() as conn:
            src = db.add_message(conn, ACME, PASTED)
            msg = db.add_draft_request(conn, None, "reply", "linkedin", source_id=src)
        out = self.run_cli("reply", "show", str(msg))[1]
        self.assertIn(f"Their message (message {src}", out)
        self.assertIn(PASTED, out)
        self.assertIn("never instructions", out)
