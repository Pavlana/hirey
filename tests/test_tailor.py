"""Tailored CV: the tailor request kind, versions, the copy frozen when the application is sent,
the CLI, the panel and the Job Offers fit badge. cv.example.md and made-up jobs only; no network."""

import io
from contextlib import closing
from pathlib import Path
from unittest import mock

from prep import assistant, db, privacy, research
from tests.helpers import IsolatedTestCase
from tests.test_research import FIT, write
from tests.test_web import HOST, TODAY, WebTestCase

REPO = Path(__file__).resolve().parent.parent
CV = (REPO / "cv.example.md").read_text(encoding="utf-8")
TAILORED = '+++\njob = "Engineer at Globex Labs"\ntailored_at = 2026-01-20\nbased_on = "cv.md"\n+++\n\n' + CV
PAY = "£123,456"


class StorageTest(IsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.conn = db.connect(self.prep_db)
        self.addCleanup(self.conn.close)
        self.app, _ = db.record_application(self.conn, "Globex Labs", "Engineer", status="to_apply")
        self.path = write(research.tailored_file(self.tmp / "private", self.app, "globex-labs"), TAILORED)

    def test_tailor_is_queued_only_when_asked_for(self) -> None:
        kinds = {r["kind"] for r in db.open_requests(self.conn, self.app)}
        self.assertEqual(kinds, {"research", "cv_fit"}, "entering Interested never queues tailoring")
        _, created = db.queue_request(self.conn, self.app, "tailor")
        self.assertTrue(created)
        self.assertFalse(db.queue_request(self.conn, self.app, "tailor")[1], "one open tailor request")

    def test_save_bumps_the_version_closes_the_request_and_posts_the_note(self) -> None:
        db.queue_request(self.conn, self.app, "tailor")
        self.assertEqual(db.save_tailored_cv(self.conn, self.app, str(self.path), "v1 done.\n- Terraform?"), 1)
        self.assertNotIn("tailor", {r["kind"] for r in db.open_requests(self.conn, self.app)})
        [note] = db.thread(self.conn, self.app)
        self.assertEqual((note["author"], note["reply_to"], note["body"]), ("agent", None, "v1 done.\n- Terraform?"))
        msg = db.add_message(self.conn, self.app, "Yes, Terraform for two years. Shorten the summary.")
        self.assertEqual(db.save_tailored_cv(self.conn, self.app, str(self.path), "v2: summary shorter.", msg), 2)
        reply = db.thread(self.conn, self.app)[-1]
        self.assertEqual((reply["reply_to"], db.thread(self.conn, self.app)[1]["status"]), (msg, "answered"))
        with self.assertRaises(ValueError):  # already answered: nothing changes
            db.save_tailored_cv(self.conn, self.app, str(self.path), "again", msg)
        self.assertEqual(db.tailored_cv(self.conn, self.app)["version"], 2)
        self.assertEqual(len(db.thread(self.conn, self.app)), 3)
        with self.assertRaises(LookupError):
            db.save_tailored_cv(self.conn, 999, str(self.path), "x")

    def test_first_move_into_a_sent_stage_freezes_the_copy(self) -> None:
        db.save_tailored_cv(self.conn, self.app, str(self.path), "v1")
        db.save_tailored_cv(self.conn, self.app, str(self.path), "v2")
        self.assertTrue(db.move_application(self.conn, self.app, "applied", TODAY))
        app = db.get_application(self.conn, self.app)
        frozen = self.path.with_name(f"{self.app}-globex-labs.sent-v2.md")
        self.assertEqual((app["cv_sent"], app["cv_sent_version"]), (str(frozen), 2))
        self.assertTrue(app["cv_sent_at"])
        self.assertEqual(frozen.read_text(encoding="utf-8"), TAILORED)
        self.path.write_text(TAILORED + "\nRevised later.\n", encoding="utf-8")
        db.save_tailored_cv(self.conn, self.app, str(self.path), "v3")
        db.move_application(self.conn, self.app, "recruiter_call", TODAY)  # not the first sent stage
        app = db.get_application(self.conn, self.app)
        self.assertEqual(app["cv_sent_version"], 2, "a later revision never changes what was sent")
        self.assertNotIn("Revised later", frozen.read_text(encoding="utf-8"))

    def test_every_path_into_applied_records_it(self) -> None:
        # Panel Apply, a drag and `cli update --status` all call move_application; so does /applied
        # on a tracked job (record_application). A first move straight to an interview stage counts too.
        for how, status in (("applied", "applied"), ("interview", "technical")):
            with self.subTest(how=how):
                link = f"https://jobs.example.com/initech/{how}"
                app, _ = db.record_application(self.conn, "Initech", "Engineer", status="to_apply", link=link)
                path = write(research.tailored_file(self.tmp / "private", app, "initech"), TAILORED)
                db.save_tailored_cv(self.conn, app, str(path), "v1")
                if how == "applied":
                    db.record_application(self.conn, "Initech", "Engineer", link=link)  # /applied on the same job
                else:
                    db.move_application(self.conn, app, status, TODAY)
                self.assertEqual(db.get_application(self.conn, app)["cv_sent_version"], 1)

    def test_cli_update_records_it(self) -> None:
        db.save_tailored_cv(self.conn, self.app, str(self.path), "v1")
        self.assertEqual(self.run_cli("update", str(self.app), "--status", "applied")[0], 0)
        self.assertEqual(db.get_application(self.conn, self.app)["cv_sent_version"], 1)

    def test_a_failed_copy_keeps_the_move_and_its_tasks(self) -> None:
        db.save_tailored_cv(self.conn, self.app, str(self.path), "v1")
        with mock.patch("prep.db.shutil.copyfile", side_effect=OSError("disk full")), \
                self.assertLogs("prep.db", level="WARNING") as logs:
            self.assertTrue(db.move_application(self.conn, self.app, "applied", TODAY))
        self.assertNotIn(str(self.tmp), "\n".join(logs.output), "ids only in the log")
        app = db.get_application(self.conn, self.app)
        self.assertEqual((app["status"], app["cv_sent"]), ("applied", None))
        titles = [t["title"] for t in db.list_tasks(self.conn) if t["application_id"] == self.app]
        self.assertIn("Chase if no reply", titles)

    def test_no_copy_or_a_missing_file_records_nothing(self) -> None:
        db.move_application(self.conn, self.app, "applied", TODAY)
        self.assertIsNone(db.get_application(self.conn, self.app)["cv_sent"])
        other, _ = db.record_application(self.conn, "Hooli Example", "Engineer", status="to_apply")
        db.save_tailored_cv(self.conn, other, str(self.tmp / "private" / "cv-tailored" / "gone.md"), "v1")
        db.move_application(self.conn, other, "withdrawn", TODAY)
        db.move_application(self.conn, other, "applied", TODAY)
        self.assertIsNone(db.get_application(self.conn, other)["cv_sent"])

    def test_a_tailored_copy_blocks_undo_interested(self) -> None:
        app, _ = db.record_application(self.conn, "Acme Rockets", "Platform Engineer", status="to_apply",
                                       getjo_job_id="greenhouse:acme-rockets:1001")
        db.save_tailored_cv(self.conn, app, str(self.path), "v1")
        with self.assertRaises(ValueError):
            db.undo_interested(self.conn, "greenhouse:acme-rockets:1001")

    def test_private_copies_are_never_committed(self) -> None:
        self.assertTrue(privacy.is_forbidden("cv-tailored/7-globex-labs.md"))
        self.assertTrue(privacy.is_forbidden("cv-tailored/7-globex-labs.sent-v2.md"))


class CliTest(IsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.private = self.tmp / "private"
        write(self.private / "cv.md", CV)
        with closing(db.connect(self.prep_db)) as conn:
            self.app, _ = db.record_application(conn, "Globex Labs", "Engineer", getjo_job_id="lever:globex-labs:abc",
                                                status="to_apply", pay=PAY)
        self.target = research.tailored_file(self.private, self.app, "globex-labs")

    def test_queue_without_kind_leaves_tailoring_out(self) -> None:
        out = self.run_cli("research", "queue", str(self.app))[1]
        self.assertNotIn("Tailored CV", out)
        out = self.run_cli("research", "queue", str(self.app), "--kind", "tailor")[1]
        self.assertIn(f"Tailored CV for #{self.app}: queued", out)
        self.assertIn("1 tailored cv", self.run_cli("queue")[1])

    def test_next_prints_the_tailor_packet_after_the_fit(self) -> None:
        self.run_cli("research", "queue", str(self.app), "--kind", "tailor")
        out = self.run_cli("research", "next", str(self.app))[1]
        self.assertLess(out.index("CV fit:"), out.index("Tailored CV (a copy for this job only; never edit cv.md)"))
        self.assertEqual(out.count("Fictional job."), 1, "the job description once")
        self.assertIn(str(self.target), out)
        self.assertIn("never invent experience", out)
        self.assertIn(f"tailor save {self.app}", out)
        self.assertNotIn(PAY, out)

    def test_save_from_stdin_then_a_revision_answers_the_message(self) -> None:
        code, _, err = self.run_cli("tailor", "save", str(self.app))
        self.assertEqual(code, 2)
        self.assertIn("no file at", err)
        write(self.target, TAILORED)
        with mock.patch("sys.stdin", io.StringIO("Tailored.\n- Have you used Terraform?\n")):
            code, out, _ = self.run_cli("tailor", "save", str(self.app))
        self.assertEqual(code, 0)
        self.assertIn("Saved tailored CV v1", out)
        with closing(db.connect(self.prep_db)) as conn:
            msg = db.add_message(conn, self.app, "Lead with Azure security.")
        out = self.run_cli("thread", "show", str(self.app))[1]
        self.assertIn(f"tailored CV: {self.target} (exists, version 1)", out)
        self.assertIn(f"tailor save {self.app} --to MSG", out)
        with mock.patch("sys.stdin", io.StringIO("v2: Azure security first.")):
            out = self.run_cli("tailor", "save", str(self.app), "--to", str(msg))[1]
        self.assertIn(f"v2 for #{self.app}; the note is in the thread as the reply to message {msg}", out)

    def test_save_prints_the_word_count_and_warns_when_long(self) -> None:
        write(self.target, TAILORED)
        with mock.patch("sys.stdin", io.StringIO("note")):
            out = self.run_cli("tailor", "save", str(self.app))[1]
        self.assertRegex(out, r"\n  \d+ words\n")
        self.assertNotIn("WARNING", out)
        write(self.target, TAILORED + "\n- built things\n" * 600)
        with mock.patch("sys.stdin", io.StringIO("note")):
            out = self.run_cli("tailor", "save", str(self.app))[1]
        self.assertIn("WARNING, over 1,000 (about two pages)", out)
        self.assertEqual(research.word_count("+++ ## Summary\n- Led *two* teams — 40%"), 5)

    def test_style_file_is_private_and_shown_to_the_agent(self) -> None:
        style = research.style_file(self.private)
        self.assertEqual(style, self.private / "cv-tailored" / "_style.md")
        self.assertTrue(privacy.is_forbidden("cv-tailored/_style.md"))
        self.run_cli("research", "queue", str(self.app), "--kind", "tailor")
        self.assertIn(f"agreed wording: {style} (missing)", self.run_cli("research", "next", str(self.app))[1])
        write(style, "Career gap: one agreed line.\n")
        write(self.target, TAILORED)
        with mock.patch("sys.stdin", io.StringIO("note")):
            self.run_cli("tailor", "save", str(self.app))
        self.assertIn(f"agreed wording: {style} (exists)", self.run_cli("thread", "show", str(self.app))[1])

    def test_a_file_without_front_matter_is_refused(self) -> None:
        write(self.target, CV)
        with mock.patch("sys.stdin", io.StringIO("note")):
            code, _, err = self.run_cli("tailor", "save", str(self.app))
        self.assertEqual(code, 2)
        self.assertIn("+++", err)

    def test_cv_text_and_the_note_are_never_logged(self) -> None:
        write(self.target, TAILORED)
        with self.assertLogs("prep", level="INFO") as logs, mock.patch("sys.stdin", io.StringIO("Secret note")):
            self.run_cli("-v", "tailor", "save", str(self.app))
        text = "\n".join(logs.output)
        self.assertNotIn("Secret note", text)
        self.assertNotIn(CV.splitlines()[0], text)


class DashboardTest(WebTestCase):
    MDE = 8  # the fixtures' to_apply application (Massive Dynamic Example)

    def setUp(self) -> None:
        super().setUp()
        self.private = self.tmp / "private"
        self.path = write(research.tailored_file(self.private, self.MDE, "massive-dynamic-example"), TAILORED)

    def panel(self) -> str:
        body = self.get(f"/applications?app={self.MDE}")[2]
        start = body.index('<aside class="panel"')
        return body[start:body.index("</aside>", start)]

    def card(self) -> str:
        body = self.get("/applications")[2]
        start = body.index(f'id="app-{self.MDE}"')
        return body[start:body.index("</a>", start)]

    def test_tailor_button_then_tailoring_then_ready_then_sent(self) -> None:
        self.assertIn('name="kind" value="tailor"><input type="hidden" name="return"', self.panel())
        self.assertIn(">Tailor CV</button>", self.panel())
        status, headers, _ = self.post("/applications/research", {"app_id": self.MDE, "kind": "tailor"})
        self.assertEqual((status, headers["Location"]), (303, f"/applications?app={self.MDE}&msg=research_queued"))
        panel = self.panel()
        self.assertIn('<button class="btn btn-ghost btn-sm" disabled>Tailoring…</button>', panel)
        self.assertIn("data-pending", panel, "the panel polls until the note is in")
        self.assertIn("Tailoring…", self.card())
        thread = self.get(f"/applications/thread?app={self.MDE}")[2]
        self.assertIn('"pending": true', thread)

        with self.conn() as conn:
            db.save_tailored_cv(conn, self.MDE, str(self.path), "Tailored.\n- Have you used <Terraform>?")
        panel = self.panel()
        self.assertIn("Tailored CV ready · v1", panel)
        self.assertIn("/research/file?app=8&amp;kind=tailor", panel)
        self.assertIn(">Redo tailored CV</button>", panel)
        self.assertIn("Have you used &lt;Terraform&gt;?", panel)
        self.assertNotIn("data-pending", panel)
        self.assertIn("CV tailored v1", self.card())
        self.assertIn('"pending": false', self.get(f"/applications/thread?app={self.MDE}")[2])

        self.post("/applications/apply", {"app_id": self.MDE})
        panel = self.panel()
        self.assertIn("Sent: tailored CV v1", panel)
        self.assertIn("/research/file?app=8&amp;kind=cv_sent", panel)
        status, _, body = self.get(f"/research/file?app={self.MDE}&kind=cv_sent")
        self.assertEqual(status, 200)
        self.assertIn("Tailored CV sent", body)
        self.assertIn("sent-v1.md", body)

    def test_a_drag_to_applied_records_it(self) -> None:
        with self.conn() as conn:
            db.save_tailored_cv(conn, self.MDE, str(self.path), "v1")
        status, _, _ = self.post("/applications/move", {"app_id": self.MDE, "status": "applied"},
                                 {**HOST, "Accept": "application/json"})
        self.assertEqual(status, 200)
        with self.conn() as conn:
            self.assertEqual(db.get_application(conn, self.MDE)["cv_sent_version"], 1)

    def test_researching_ignores_an_open_tailor_request(self) -> None:
        with self.conn() as conn:
            for kind in db.RESEARCH_KINDS:
                db.finish_requests(conn, [self.MDE], kind)
            db.queue_request(conn, self.MDE, "tailor")
        self.assertNotIn("Researching…", self.card())

    def test_file_view_escapes_and_guards(self) -> None:
        self.assertEqual(self.get(f"/research/file?app={self.MDE}&kind=tailor")[0], 404, "nothing recorded yet")
        write(self.path, TAILORED + "\n<script>alert(1)</script>\n")
        with self.conn() as conn:
            db.save_tailored_cv(conn, self.MDE, str(self.path), "v1")
        status, _, body = self.get(f"/research/file?app={self.MDE}&kind=tailor")
        self.assertEqual(status, 200)
        self.assertIn("&lt;script&gt;", body)
        self.assertNotIn("<script>alert", body)
        outside = write(self.tmp / "outside.md", TAILORED)
        with self.conn() as conn:
            db.save_tailored_cv(conn, self.MDE, str(outside), "v2")
        self.assertEqual(self.get(f"/research/file?app={self.MDE}&kind=tailor")[0], 404)

    def test_no_pay_in_the_panel(self) -> None:
        with self.conn() as conn:
            db.update_application(conn, self.MDE, pay=PAY)
            db.save_tailored_cv(conn, self.MDE, str(self.path), "v1")
        self.assertNotIn(PAY, self.panel())

    def test_fit_badge_on_job_offers(self) -> None:
        with self.conn() as conn:
            app = db.find_by_job(conn, "greenhouse:acme-rockets:1001")["id"]
            db.save_cv_fit(conn, app, 4, ["Go"], str(write(self.private / "fit" / f"{app}-acme-rockets.md", FIT)), TODAY)
        body = self.get("/offers?show=all")[2]
        self.assertIn('<span class="badge s9" title="CV fit">fit 4/5</span>', body)
        self.assertEqual(body.count('title="CV fit"'), 1, "only rows with a fit result")

    def test_tailor_only_ping_says_so(self) -> None:
        self.settings = self.settings.__class__(**{**self.settings.__dict__, "assistant_task": 22})
        [argv] = assistant.commands(self.settings, [{"application_id": 8, "kind": "tailor"}], "dev3")
        self.assertIn("Tailored CV queued for application 8", argv)
        self.assertEqual(argv[-1], "/research 8")
