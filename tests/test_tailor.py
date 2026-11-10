"""Tailored CV: the tailor request kind, file naming and versions (every version its own file, all
kept), recording which version was sent, the CLI, the panel and the Job Offers fit badge.
cv.example.md and made-up jobs only; no network."""

import io
from contextlib import closing
from pathlib import Path
from unittest import mock

from prep import assistant, db, privacy, research
from tests.helpers import IsolatedTestCase
from tests.test_research import FIT, write
from tests.test_web import TODAY, WebTestCase

REPO = Path(__file__).resolve().parent.parent
CV = (REPO / "cv.example.md").read_text(encoding="utf-8")
TAILORED = '+++\njob = "Engineer at Globex Labs"\ntailored_at = 2026-01-20\nbased_on = "cv.md"\n+++\n\n' + CV
PAY = "£123,456"


class NamingTest(IsolatedTestCase):
    def test_file_name(self) -> None:
        private = Path("/p")
        self.assertEqual(research.tailored_file(private, "OpenAI", "FDE", "2026-09-21", 1),
                         private / "cv-tailored" / "CV_OpenAI_FDE_210926_1.md")
        self.assertEqual(research.tailored_file(private, "Globex Labs Ltd.", "AI eng", "2026-01-05", 12).name,
                         "CV_GlobexLabs_AIEng_050126_12.md")
        self.assertEqual(research.company_word("Ünïcode / Co & <x>"), "NCodeCoX")  # non-ASCII letters dropped
        self.assertEqual(research.company_word("!!!"), "Company")
        self.assertEqual(research.role_short_word("Forward Deployed Engineer"), "ForwardDeplo")  # 12 at most
        with self.assertRaises(ValueError):
            research.role_short_word("--")


class StorageTest(IsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.conn = db.connect(self.prep_db)
        self.addCleanup(self.conn.close)
        self.app, _ = db.record_application(self.conn, "Globex Labs", "Engineer", status="to_apply")
        self.v1 = write(research.tailored_file(self.tmp / "private", "Globex Labs", "Eng", TODAY, 1), TAILORED)
        self.v2 = write(research.tailored_file(self.tmp / "private", "Globex Labs", "Eng", TODAY, 2), TAILORED)

    def test_tailor_is_queued_only_when_asked_for(self) -> None:
        kinds = {r["kind"] for r in db.open_requests(self.conn, self.app)}
        self.assertEqual(kinds, {"research", "cv_fit"}, "entering Interested never queues tailoring")
        _, created = db.queue_request(self.conn, self.app, "tailor")
        self.assertTrue(created)
        self.assertFalse(db.queue_request(self.conn, self.app, "tailor")[1], "one open tailor request")

    def test_save_adds_a_version_closes_the_request_and_posts_the_note(self) -> None:
        db.queue_request(self.conn, self.app, "tailor")
        self.assertEqual(db.save_tailored_cv(self.conn, self.app, str(self.v1), "v1 done.\n- Terraform?"), 1)
        self.assertNotIn("tailor", {r["kind"] for r in db.open_requests(self.conn, self.app)})
        [note] = db.thread(self.conn, self.app)
        self.assertEqual((note["author"], note["reply_to"], note["body"]), ("agent", None, "v1 done.\n- Terraform?"))
        msg = db.add_message(self.conn, self.app, "Yes, Terraform for two years. Shorten the summary.")
        self.assertEqual(db.save_tailored_cv(self.conn, self.app, str(self.v2), "v2: summary shorter.", msg), 2)
        self.assertEqual((db.thread(self.conn, self.app)[-1]["reply_to"], db.thread(self.conn, self.app)[1]["status"]),
                         (msg, "answered"))
        with self.assertRaises(ValueError):  # already answered: nothing changes
            db.save_tailored_cv(self.conn, self.app, str(self.v2), "again", msg)
        self.assertEqual(db.tailored_cv(self.conn, self.app)["file_path"], str(self.v2))
        self.assertEqual([(v["version"], v["file_path"]) for v in db.tailored_versions(self.conn, self.app)],
                         [(1, str(self.v1)), (2, str(self.v2))])  # every version kept
        with self.assertRaises(LookupError):
            db.save_tailored_cv(self.conn, 999, str(self.v1), "x")

    def test_applying_records_nothing_and_copies_nothing(self) -> None:
        db.save_tailored_cv(self.conn, self.app, str(self.v1), "v1")
        self.assertTrue(db.move_application(self.conn, self.app, "applied", TODAY))
        app = db.get_application(self.conn, self.app)
        self.assertEqual((app["cv_sent"], app["cv_sent_version"]), (None, None))
        self.assertEqual(sorted(p.name for p in self.v1.parent.iterdir()), sorted([self.v1.name, self.v2.name]))
        self.assertFalse(hasattr(db, "sent_copy_path"))

    def test_cv_sent_is_a_recorded_version(self) -> None:
        db.save_tailored_cv(self.conn, self.app, str(self.v1), "v1")
        db.save_tailored_cv(self.conn, self.app, str(self.v2), "v2")
        found = db.record_cv_sent(self.conn, self.app, self.v1.stem)  # without .md
        self.assertEqual(found["version"], 1)
        app = db.get_application(self.conn, self.app)
        self.assertEqual((app["cv_sent"], app["cv_sent_version"]), (self.v1.name, 1))
        self.assertTrue(app["cv_sent_at"])
        db.record_cv_sent(self.conn, self.app, self.v2.name)
        self.assertEqual(db.get_application(self.conn, self.app)["cv_sent_version"], 2)
        with self.assertRaisesRegex(LookupError, "versions: CV_GlobexLabs_Eng"):
            db.record_cv_sent(self.conn, self.app, "CV_Other_X_010126_1.md")

    def test_an_older_tailored_copy_becomes_version_one(self) -> None:
        self.conn.execute("INSERT INTO tailored_cvs (application_id, file_path, version, updated_at) VALUES (?, ?, 3, ?)",
                          (self.app, "/old/7-globex-labs.md", "2026-01-10T09:00:00Z"))
        self.conn.commit()
        with closing(db.connect(self.prep_db)) as conn:  # the backfill runs on connect
            self.assertEqual([(v["version"], v["file_path"]) for v in db.tailored_versions(conn, self.app)],
                             [(3, "/old/7-globex-labs.md")])

    def test_a_tailored_copy_blocks_undo_interested(self) -> None:
        app, _ = db.record_application(self.conn, "Acme Rockets", "Platform Engineer", status="to_apply",
                                       getjo_job_id="greenhouse:acme-rockets:1001")
        db.save_tailored_cv(self.conn, app, str(self.v1), "v1")
        with self.assertRaises(ValueError):
            db.undo_interested(self.conn, "greenhouse:acme-rockets:1001")

    def test_private_copies_are_never_committed(self) -> None:
        self.assertTrue(privacy.is_forbidden("cv-tailored/CV_OpenAI_FDE_210926_1.md"))


class CliTest(IsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.private = self.tmp / "private"
        write(self.private / "cv.md", CV)
        with closing(db.connect(self.prep_db)) as conn:
            self.app, _ = db.record_application(conn, "Globex Labs", "Engineer", getjo_job_id="lever:globex-labs:abc",
                                                status="to_apply", pay=PAY)

    def path(self, n: int) -> Path:
        return research.tailored_file(self.private, "Globex Labs", "Eng", db.today_iso(), n)

    def save(self, *extra: str, note: str = "note") -> tuple[int, str, str]:
        with mock.patch("sys.stdin", io.StringIO(note)):
            return self.run_cli("tailor", "save", str(self.app), *extra)

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
        self.assertIn(f"tailor path {self.app} --role-short X", out)
        self.assertIn("never invent experience", out)
        self.assertIn(f"tailor save {self.app}", out)
        self.assertNotIn(PAY, out)

    def test_path_then_save_then_a_revision_as_a_new_file(self) -> None:
        code, _, err = self.run_cli("tailor", "path", str(self.app))
        self.assertEqual(code, 2)
        self.assertIn("--role-short X", err)  # the short role name comes first
        code, out, _ = self.run_cli("tailor", "path", str(self.app), "--role-short", "eng")
        self.assertEqual((code, out.splitlines()[0]), (0, str(self.path(1))))
        self.assertTrue(self.path(1).parent.is_dir())
        self.assertEqual(self.run_cli("tailor", "path", str(self.app), "--role-short", "FDE")[0], 2, "kept stable")
        code, _, err = self.save()
        self.assertEqual(code, 2)
        self.assertIn("no file at", err)
        write(self.path(1), TAILORED)
        code, out, _ = self.save(note="Tailored.\n- Have you used Terraform?\n")
        self.assertEqual(code, 0)
        self.assertIn(f"Saved tailored CV v1 ({self.path(1).name})", out)
        with closing(db.connect(self.prep_db)) as conn:
            msg = db.add_message(conn, self.app, "Lead with Azure security.")
        out = self.run_cli("thread", "show", str(self.app))[1]
        self.assertIn(f"v1 {self.path(1)} (exists)", out)
        self.assertIn(f"tailor path {self.app} --copy", out)
        out = self.run_cli("tailor", "path", str(self.app), "--copy")[1]
        self.assertEqual(out.splitlines()[0], str(self.path(2)))
        self.assertEqual(self.path(2).read_text(encoding="utf-8"), TAILORED)
        out = self.save("--to", str(msg), note="v2: Azure security first.")[1]
        self.assertIn(f"v2 ({self.path(2).name}) for #{self.app}; the note is in the thread as the reply to message {msg}",
                      out)
        self.assertTrue(self.path(1).is_file(), "every version is kept")
        out = self.run_cli("cv", "sent", str(self.app), self.path(1).stem)[1]
        self.assertIn(f"Recorded: {self.path(1).name} (v1) was sent", out)
        self.assertIn(f"v1 {self.path(1)} (exists) · SENT", self.run_cli("thread", "show", str(self.app))[1])
        code, _, err = self.run_cli("cv", "sent", str(self.app), "CV_Nope_X_010126_1")
        self.assertEqual(code, 2)
        self.assertIn("no tailored CV", err)

    def test_save_prints_the_word_count_and_warns_when_long(self) -> None:
        self.run_cli("tailor", "path", str(self.app), "--role-short", "Eng")
        write(self.path(1), TAILORED)
        out = self.save()[1]
        self.assertRegex(out, r"\n  \d+ words\n")
        self.assertNotIn("WARNING", out)
        write(self.path(2), TAILORED + "\n- built things\n" * 600)
        self.assertIn("WARNING, over 1,000 (about two pages)", self.save()[1])
        self.assertEqual(research.word_count("+++ ## Summary\n- Led *two* teams — 40%"), 5)

    def test_style_file_is_private_and_shown_to_the_agent(self) -> None:
        style = research.style_file(self.private)
        self.assertEqual(style, self.private / "cv-tailored" / "_style.md")
        self.assertTrue(privacy.is_forbidden("cv-tailored/_style.md"))
        self.run_cli("research", "queue", str(self.app), "--kind", "tailor")
        self.assertIn(f"agreed wording: {style} (missing)", self.run_cli("research", "next", str(self.app))[1])
        write(style, "Career gap: one agreed line.\n")
        self.run_cli("tailor", "path", str(self.app), "--role-short", "Eng")
        write(self.path(1), TAILORED)
        self.save()
        self.assertIn(f"agreed wording: {style} (exists)", self.run_cli("thread", "show", str(self.app))[1])

    def test_a_file_without_front_matter_is_refused(self) -> None:
        self.run_cli("tailor", "path", str(self.app), "--role-short", "Eng")
        write(self.path(1), CV)
        code, _, err = self.save()
        self.assertEqual(code, 2)
        self.assertIn("+++", err)

    def test_cv_text_and_the_note_are_never_logged(self) -> None:
        self.run_cli("tailor", "path", str(self.app), "--role-short", "Eng")
        write(self.path(1), TAILORED)
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
        self.path = write(research.tailored_file(self.private, "Massive Dynamic Example", "AISE", TODAY, 1), TAILORED)

    def panel(self) -> str:
        body = self.get(f"/applications?app={self.MDE}")[2]
        start = body.index('<aside class="panel"')
        return body[start:body.index("</aside>", start)]

    def test_tailor_button_then_tailoring_then_versions(self) -> None:
        self.assertIn(">Tailor CV</button>", self.panel())
        status, headers, _ = self.post("/applications/research", {"app_id": self.MDE, "kind": "tailor"})
        self.assertEqual((status, headers["Location"]), (303, f"/applications?app={self.MDE}&msg=research_queued"))
        panel = self.panel()
        self.assertIn("Tailoring…</button>", panel)
        self.assertIn("data-busy", panel, "the page polls until the note is in")
        self.assertIn('"pending": true', self.get(f"/applications/thread?app={self.MDE}")[2])

        with self.conn() as conn:
            db.save_tailored_cv(conn, self.MDE, str(self.path), "Tailored.\n- Have you used <Terraform>?")
            for kind in db.RESEARCH_KINDS:
                db.finish_requests(conn, [self.MDE], kind)
        panel = self.panel()
        self.assertIn('data-jump="card-tailor">CV v1</a>', panel)
        self.assertIn(f"/research/file?app=8&amp;kind=tailor&amp;v=1", panel)
        self.assertIn(self.path.name, panel)
        self.assertIn(">New from cv.md</button>", panel)
        self.assertNotIn("Have you used", panel, "the note goes to the Agent box, not the card")
        self.assertIn("Have you used &lt;Terraform&gt;?", self.get(f"/applications?app={self.MDE}")[2])
        self.assertNotIn("data-busy", panel)
        self.assertIn('"pending": false', self.get(f"/applications/thread?app={self.MDE}")[2])
        self.post("/applications/apply", {"app_id": self.MDE})
        self.assertNotIn(">sent</span>", self.panel(), "applying records no sent CV")
        with self.conn() as conn:
            db.record_cv_sent(conn, self.MDE, self.path.name)
        self.assertIn(">sent</span>", self.panel())

    def test_file_view_escapes_and_guards(self) -> None:
        self.assertEqual(self.get(f"/research/file?app={self.MDE}&kind=tailor")[0], 404, "nothing recorded yet")
        write(self.path, TAILORED + "\n<script>alert(1)</script>\n")
        with self.conn() as conn:
            db.save_tailored_cv(conn, self.MDE, str(self.path), "v1")
        status, _, body = self.get(f"/research/file?app={self.MDE}&kind=tailor")
        self.assertEqual(status, 200)
        self.assertIn("&lt;script&gt;", body)
        self.assertNotIn("<script>alert", body)
        self.assertEqual(self.get(f"/research/file?app={self.MDE}&kind=research&v=1")[0], 404)
        outside = write(self.tmp / "outside.md", TAILORED)
        with self.conn() as conn:
            db.save_tailored_cv(conn, self.MDE, str(outside), "v2")
        self.assertEqual(self.get(f"/research/file?app={self.MDE}&kind=tailor")[0], 404)
        self.assertEqual(self.get(f"/research/file?app={self.MDE}&kind=tailor&v=2")[0], 404)
        self.assertEqual(self.get(f"/research/file?app={self.MDE}&kind=tailor&v=1")[0], 200)

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
