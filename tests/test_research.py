"""Agent research queue, results files, CLI commands, assistant messages and the dashboard parts.
Made-up companies and text only; dev3 is never called (subprocess is mocked)."""

import os
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path
from unittest import mock

from prep import assistant, db, research
from prep.config import load_settings
from tests.helpers import IsolatedTestCase
from tests.test_web import TODAY, WebTestCase

PROFILE = """+++
company = "Globex Labs"
what_it_does = "Makes <b>example</b> widgets."
sector = "Robotics"
employees = 120
employees_as_of = 2026-01-02
size_band = "51-250"
london_remote = "London office, hybrid"
recognition = "niche"
recognition_note = "Known among widget makers."
funding = "Series A"
sources = ["https://example.com/about"]
suggested_tier = "b"
tier_reason = "Decent fit, less known."
researched_at = 2026-01-18
+++

# What it does
Widgets for <script>alert(1)</script> people.

- one
- two
"""

FIT = """+++
score = 4
gaps = ["Kubernetes operators", "  kotlin ", "Go", "Rust"]
checked_at = 2026-01-19
+++
## Fit summary
Good.
"""


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class QueueTest(IsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.conn = db.connect(self.prep_db)
        self.addCleanup(self.conn.close)
        self.app = db.add_application(self.conn, "Globex Labs", "Engineer", status="applied")

    def test_slug(self) -> None:
        self.assertEqual(db.company_slug("Acme Rockets Ltd."), "acme-rockets")
        self.assertEqual(db.company_slug("Stark Example Limited"), "stark-example")
        self.assertEqual(db.company_slug("Co"), "co")
        self.assertEqual(db.company_slug("Ünïcode & Co"), "n-code")
        self.assertEqual(db.company_slug("!!!"), "company")

    def test_queue_once_then_reuse(self) -> None:
        rid, created = db.queue_request(self.conn, self.app, "research")
        self.assertTrue(created)
        again, created = db.queue_request(self.conn, self.app, "research", force=True)
        self.assertEqual((again, created), (rid, False))
        self.assertEqual(self.conn.execute("SELECT force FROM agent_requests WHERE id = ?", (rid,)).fetchone()[0], 1)
        with self.assertRaises(ValueError):
            db.queue_request(self.conn, self.app, "astrology")
        with self.assertRaises(LookupError):
            db.queue_request(self.conn, 999, "research")

    def test_unique_index_blocks_a_second_open_request(self) -> None:
        db.queue_request(self.conn, self.app, "cv_fit")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO agent_requests (application_id, kind, requested_at) VALUES (?, 'cv_fit', 'x')",
                              (self.app,))
        self.conn.rollback()
        db.finish_requests(self.conn, [self.app], "cv_fit")
        _, created = db.queue_request(self.conn, self.app, "cv_fit")
        self.assertTrue(created, "a new request is allowed once the old one is done")

    def test_claim_once_and_stale_claims_return(self) -> None:
        rid, _ = db.queue_request(self.conn, self.app, "research")
        self.assertTrue(db.claim_request(self.conn, rid))
        self.assertFalse(db.claim_request(self.conn, rid))
        self.assertTrue(db.open_requests(self.conn)[0]["claimed"])
        self.conn.execute("UPDATE agent_requests SET claimed_at = '2020-01-01T00:00:00Z'")
        self.conn.commit()
        self.assertFalse(db.open_requests(self.conn)[0]["claimed"])
        self.assertTrue(db.claim_request(self.conn, rid))

    def test_interested_queues_both_by_every_path(self) -> None:
        a, _ = db.record_application(self.conn, "Initech Cloud", "SRE", status="to_apply")
        b = db.add_application(self.conn, "Hooli Data", "Dev", status="applied")
        db.move_application(self.conn, b, "to_apply")
        for app_id in (a, b):
            kinds = sorted(r["kind"] for r in db.open_requests(self.conn, app_id))
            self.assertEqual(kinds, ["cv_fit", "research"])
        code, out, _ = self.run_cli("applied", "--company", "Umbrella Ops", "--role", "Ops", "--status", "to_apply")
        self.assertEqual(code, 0)
        self.assertIn("queued for the agent: Company research and CV fit", out)

    def test_interested_again_skips_cv_fit_when_one_exists(self) -> None:
        db.save_cv_fit(self.conn, self.app, 3, [], "/x.md", TODAY)
        db.move_application(self.conn, self.app, "to_apply")
        self.assertEqual([r["kind"] for r in db.open_requests(self.conn, self.app)], ["research"])

    def test_profile_closes_research_for_every_role_at_the_company(self) -> None:
        other = db.add_application(self.conn, "Globex Labs Ltd", "Manager")
        db.queue_request(self.conn, self.app, "research")
        db.queue_request(self.conn, other, "research")
        db.queue_request(self.conn, other, "cv_fit")
        apps = db.save_company_profile(self.conn, "globex-labs", {
            "company": "Globex Labs", "file_path": "/p.md", "researched_at": TODAY, "suggested_tier": "B",
            "sources": ["https://example.com"]})
        self.assertEqual(apps, [self.app, other])
        self.assertEqual([r["kind"] for r in db.open_requests(self.conn)], ["cv_fit"])
        self.assertEqual(db.company_profile(self.conn, "globex-labs")["sources"], ["https://example.com"])
        with self.assertRaises(ValueError):
            db.save_company_profile(self.conn, "x", {"company": "X", "file_path": "/p", "researched_at": TODAY,
                                                     "recognition": "famous"})

    def test_cv_fit_gaps_are_deduplicated(self) -> None:
        db.add_gap(self.conn, "Go")
        new = db.save_cv_fit(self.conn, self.app, 4, ["Kubernetes operators", " go ", "kubernetes  OPERATORS", "Rust"],
                             "/f.md", TODAY)
        rows = self.conn.execute("SELECT gap, session, application_id FROM gaps WHERE id IN (%s)"
                                 % ",".join("?" * len(new)), new).fetchall()
        self.assertEqual([tuple(r) for r in rows],
                         [("Kubernetes operators", "cv fit", self.app), ("Rust", "cv fit", self.app)])
        self.assertEqual(db.cv_fit(self.conn, self.app)["top_gaps"], ["Kubernetes operators", "go", "kubernetes  OPERATORS"])
        for bad in (0, 6, "4", True):
            with self.subTest(score=bad), self.assertRaises(ValueError):
                db.save_cv_fit(self.conn, self.app, bad, [], "/f.md")


class FilesTest(IsolatedTestCase):
    def test_front_matter(self) -> None:
        profile = research.read_profile(write(self.tmp / "p.md", PROFILE))
        self.assertEqual((profile["employees_as_of"], profile["researched_at"], profile["suggested_tier"]),
                         ("2026-01-02", "2026-01-18", "B"))
        fit = research.read_fit(write(self.tmp / "f.md", FIT))
        self.assertEqual((fit["score"], fit["checked_at"]), (4, "2026-01-19"))
        for text in ("no fence", "+++\nscore = 4\n", "+++\nscore = = 4\n+++\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                research.read_fit(write(self.tmp / "bad.md", text))

    def test_freshness(self) -> None:
        self.assertEqual(research.freshness(None, 90, TODAY), ("missing", None))
        self.assertEqual(research.freshness({"researched_at": "2026-01-10"}, 90, TODAY), ("fresh", 10))
        self.assertEqual(research.freshness({"researched_at": "2025-01-10"}, 90, TODAY)[0], "stale")

    def test_inside(self) -> None:
        private = self.tmp / "private"
        private.mkdir()
        self.assertEqual(research.inside(private, str(private / "fit" / "a.md")), (private / "fit" / "a.md").resolve())
        self.assertIsNone(research.inside(private, str(private / ".." / "prep.db")))
        self.assertIsNone(research.inside(private, "/etc/passwd"))
        self.assertIsNone(research.inside(None, str(private / "a.md")))


class CliTest(IsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.private = self.tmp / "private"
        with closing(db.connect(self.prep_db)) as conn:
            self.app, _ = db.record_application(conn, "Globex Labs", "Engineer", getjo_job_id="lever:globex-labs:abc",
                                                status="to_apply")

    def test_queue_is_read_only_and_prints_ids_only(self) -> None:
        before = self.prep_db.stat().st_mtime_ns, self.prep_db.read_bytes()
        code, out, _ = self.run_cli("queue")
        self.assertEqual(code, 0)
        self.assertIn(f"1 company research, 1 cv fit (applications {self.app})", out)
        self.assertNotIn("Globex", out)
        self.assertEqual((self.prep_db.stat().st_mtime_ns, self.prep_db.read_bytes()), before)

    def test_queue_prints_nothing_without_a_database(self) -> None:
        with mock.patch.dict(os.environ, {"PREP_DB": str(self.tmp / "absent.db")}):
            self.assertEqual(self.run_cli("queue")[:2], (0, ""))
        self.assertFalse((self.tmp / "absent.db").exists())

    def test_next_claims_and_prints_the_packet(self) -> None:
        code, out, _ = self.run_cli("research", "next")
        self.assertEqual(code, 0)
        self.assertIn(f"Application #{self.app}: Globex Labs", out)
        self.assertIn("no profile yet", out)
        self.assertIn(str(self.private / "companies" / "globex-labs.md"), out)
        self.assertIn(str(self.private / "fit" / f"{self.app}-globex-labs.md"), out)
        self.assertIn("cv.md (MISSING", out)
        self.assertIn("Fictional job.", out)  # the getjo description
        self.assertIn("Nothing pending", self.run_cli("research", "next")[1])

    def test_save_both_and_reuse_a_fresh_profile(self) -> None:
        write(self.private / "companies" / "globex-labs.md", PROFILE.replace("2026-01-18", db.today_iso()))
        write(self.private / "fit" / f"{self.app}-globex-labs.md", FIT)
        code, out, err = self.run_cli("research", "save", str(self.app))
        self.assertEqual(code, 0, err)
        self.assertIn("suggested tier B", out)
        code, out, err = self.run_cli("fit", "save", str(self.app))
        self.assertEqual(code, 0, err)
        self.assertIn("4/5, 4 gaps (4 new", out)
        self.assertEqual(self.run_cli("queue")[1], "")
        with closing(db.connect(self.prep_db)) as conn:
            second, _ = db.record_application(conn, "Globex Labs", "Manager", status="to_apply")
        out = self.run_cli("research", "next", str(second))[1]
        self.assertIn("reuse it", out)

    def test_save_errors(self) -> None:
        code, _, err = self.run_cli("fit", "save", str(self.app))
        self.assertNotEqual(code, 0)
        self.assertIn("no file", err)
        write(self.private / "fit" / f"{self.app}-globex-labs.md", FIT.replace("score = 4", "score = 9"))
        code, _, err = self.run_cli("fit", "save", str(self.app))
        self.assertEqual(code, 2)
        self.assertIn("1 to 5", err)

    def test_today_mentions_pending_work(self) -> None:
        self.assertIn("Agent work pending", self.run_cli("today")[1])

    def test_cli_interested_messages_the_assistant(self) -> None:
        with mock.patch.dict(os.environ, {"PREP_ASSISTANT_TASK": "42"}), \
                mock.patch.object(assistant, "dev3_path", return_value="/bin/dev3"), \
                mock.patch.object(subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.run_cli("applied", "--job-id", "ashby:initech-cloud:x1", "--status", "to_apply")
            self.run_cli("applied", "--job-id", "ashby:initech-cloud:x1", "--status", "to_apply")
        self.assertEqual(run.call_count, 1, "only newly queued work is sent")


class AssistantTest(IsolatedTestCase):
    def settings(self, **env: str):
        with mock.patch.dict(os.environ, env):
            return load_settings()

    def test_fixed_argv_from_ids(self) -> None:
        s = self.settings(PREP_ASSISTANT_TASK="42", PREP_ASSISTANT_PROJECT="e942-abc")
        argv = assistant.commands(s, [{"application_id": 7}, {"application_id": 7}, {"application_id": 3}], "/bin/dev3")
        self.assertEqual(argv, [
            ["/bin/dev3", "message", "--task", "seq:42", "--project", "e942-abc", "--subject",
             "Research queued for application 3", "/research 3"],
            ["/bin/dev3", "message", "--task", "seq:42", "--project", "e942-abc", "--subject",
             "Research queued for application 7", "/research 7"],
        ])

    def test_unset_or_failing_never_raises(self) -> None:
        with mock.patch.object(subprocess, "run") as run:
            self.assertEqual(assistant.notify(self.settings(), [{"application_id": 1}]), 0)
            run.assert_not_called()
        s = self.settings(PREP_ASSISTANT_TASK="42")
        with mock.patch.object(assistant, "dev3_path", return_value=None), self.assertLogs("prep.assistant", "WARNING"):
            self.assertEqual(assistant.notify(s, [{"application_id": 1}]), 0)
        with mock.patch.object(assistant, "dev3_path", return_value="/bin/dev3"):
            for effect in (FileNotFoundError(), subprocess.TimeoutExpired("dev3", 15), mock.Mock(returncode=3)):
                with self.subTest(effect=effect), self.assertLogs("prep.assistant", "WARNING"), \
                        mock.patch.object(subprocess, "run", side_effect=None if isinstance(effect, mock.Mock) else effect,
                                          return_value=effect):
                    self.assertEqual(assistant.notify(s, [{"application_id": 1}]), 0)
            with mock.patch.object(subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
                self.assertEqual(assistant.notify(s, [{"application_id": 1}]), 1)
            self.assertNotIn("shell", run.call_args.kwargs)
            self.assertIn("timeout", run.call_args.kwargs)

    def test_dev3_bin_must_exist(self) -> None:
        s = self.settings(PREP_DEV3_BIN=str(self.tmp / "nope"))
        self.assertIsNone(assistant.dev3_path(s))


class DashboardTest(WebTestCase):
    MDE = 8  # the fixtures' to_apply application (Massive Dynamic Example)

    def setUp(self) -> None:
        super().setUp()
        self.private = self.tmp / "private"

    def card(self, app_id: int) -> str:
        """The compact card plus its side panel (where the research section lives)."""
        body = self.get(f"/applications?app={app_id}")[2]
        start = body.index(f'id="app-{app_id}"')
        panel = body.index('<aside class="panel"')
        return body[start:body.index("</a>", start)] + body[panel:body.index("</aside>", panel)]

    def test_buttons_queue_then_show_pending_then_redo(self) -> None:
        card = self.card(self.MDE)
        self.assertIn("Not researched yet.", card)
        self.assertIn('name="kind" value="research"', card)
        status, headers, _ = self.post("/applications/research", {"app_id": self.MDE, "kind": "research"})
        self.assertEqual((status, headers["Location"]), (303, f"/applications?app={self.MDE}&msg=research_queued"))
        self.post("/applications/research", {"app_id": self.MDE, "kind": "research"})  # again: no change
        with self.conn() as conn:
            self.assertEqual(len(db.open_requests(conn, self.MDE)), 1)
        card = self.card(self.MDE)
        self.assertIn("Company research pending</button>", card)
        self.assertIn("Company research pending</span>", card)  # chip on the collapsed card
        self.assertEqual(self.post("/applications/research", {"app_id": self.MDE, "kind": "nope"})[0], 400)

        write(self.private / "companies" / "massive-dynamic-example.md", PROFILE)
        with self.conn() as conn:
            db.save_company_profile(conn, "massive-dynamic-example", research.read_profile(
                self.private / "companies" / "massive-dynamic-example.md"))
        card = self.card(self.MDE)
        self.assertIn("Makes &lt;b&gt;example&lt;/b&gt; widgets.", card)
        self.assertIn("Suggested tier <strong>B</strong>", card)
        self.assertIn(">Redo</button>", card)
        self.assertIn(f"/research/file?app={self.MDE}&amp;kind=research", card)
        self.post("/applications/research", {"app_id": self.MDE, "kind": "research"})
        with self.conn() as conn:
            self.assertEqual(db.open_requests(conn, self.MDE)[0]["force"], 1, "Redo forces new research")

    def test_tier_confirm(self) -> None:
        self.assertEqual(self.post("/applications/tier", {"app_id": self.MDE, "tier": "D"})[0], 400)
        self.post("/applications/tier", {"app_id": self.MDE, "tier": "A"})
        with self.conn() as conn:
            self.assertEqual(db.get_application(conn, self.MDE)["tier"], "A")

    def test_file_page_escapes_and_guards(self) -> None:
        path = write(self.private / "fit" / f"{self.MDE}-mde.md", FIT + "\n<img src=x onerror=alert(1)>\n")
        with self.conn() as conn:
            db.save_cv_fit(conn, self.MDE, 4, ["Go"], str(path), TODAY)
        status, _, body = self.get(f"/research/file?app={self.MDE}&kind=cv_fit")
        self.assertEqual(status, 200)
        self.assertIn("&lt;img src=x", body)
        self.assertNotIn("<img", body)
        self.assertIn("<h3>Fit summary</h3>", body)
        path.unlink()
        self.assertIn("File missing", self.get(f"/research/file?app={self.MDE}&kind=cv_fit")[2])
        for target in (f"/research/file?app={self.MDE}&kind=research", "/research/file?app=999&kind=cv_fit",
                       "/research/file?app=x&kind=cv_fit", f"/research/file?app={self.MDE}&kind=other"):
            with self.subTest(target=target):
                self.assertEqual(self.get(target)[0], 404)
        outside = write(self.tmp / "outside.md", FIT)
        with self.conn() as conn:
            db.save_cv_fit(conn, self.MDE, 4, [], str(outside), TODAY)
        self.assertEqual(self.get(f"/research/file?app={self.MDE}&kind=cv_fit")[0], 404)
        self.settings = self.settings.__class__(**{**self.settings.__dict__, "private_dir": None})
        self.assertEqual(self.get(f"/research/file?app={self.MDE}&kind=cv_fit")[0], 404)

    def test_new_requests_go_to_the_assistant_in_the_background(self) -> None:
        self.settings = self.settings.__class__(**{**self.settings.__dict__, "assistant_task": 42})
        with mock.patch.object(assistant, "notify_in_background") as bg:
            self.post("/applications/research", {"app_id": self.MDE, "kind": "cv_fit"})
            self.post("/applications/research", {"app_id": self.MDE, "kind": "cv_fit"})
        sent = [c.args[1] for c in bg.call_args_list]
        self.assertEqual(len(sent[0]), 1)
        self.assertEqual(sent[0][0]["application_id"], self.MDE)
        self.assertEqual(sent[1], [], "a second click queues nothing new")

    def test_markdown_renderer_is_escaped(self) -> None:
        from prep.web import render_markdown
        html = render_markdown("# T <x>\ntext & more\n\n- a <b>\n- [l](javascript:x)")
        self.assertEqual(html, "<h2>T &lt;x&gt;</h2><p>text &amp; more</p><ul><li>a &lt;b&gt;</li>"
                               "<li>[l](javascript:x)</li></ul>")
