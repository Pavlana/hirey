"""Job briefs: storage and its rules (no pay, length, the key facts line), the CLI, the background
queue for new offers at brief_auto_min_score, the queue when a panel is first opened, Write brief /
Redo, polling, the panel (brief or tidied description) and the description formatter. Made-up data;
dev3 is never called."""

import os
from unittest import mock

from prep import assistant, db, web
from prep.config import load_settings
from tests.test_web import HOST, WebTestCase, job_url

GLOBEX = "lever:globex-labs:abc"  # new offer, score 8: queued in the background
WONKA = "ashby:wonka-systems:w2"  # new offer, score 7: queued when its panel opens
INITECH = "ashby:initech-cloud:x1"  # new offer, score 7, with a brief in the fixtures
ACME = 1
ACME_JOB = "greenhouse:acme-rockets:1001"
BRIEF = "remote · Python · Senior · UK\n## Summary\nBackend role.\n## Must-haves\n- Python\n- Postgres"


class StorageTest(WebTestCase):
    def test_save_rules(self) -> None:
        with self.conn() as conn:
            db.save_brief(conn, GLOBEX, BRIEF)
            self.assertEqual(db.job_briefs(conn)[GLOBEX]["facts"], "remote · Python · Senior · UK")
            for bad in ("", "## Summary\nNo facts line first", "x" * (db.MAX_FACTS + 1),
                        "facts\n" + "word " * db.MAX_BRIEF_WORDS, "facts\n- £70,000", "facts\n- 80k base",
                        "facts\n- Salary negotiable", "facts\n- Bonus and equity", "facts\n- $120"):
                with self.subTest(bad=bad[:30]), self.assertRaises(ValueError):
                    db.save_brief(conn, GLOBEX, bad)
            db.save_brief(conn, GLOBEX, "facts\n- payments platform, k8s")  # not pay

    def test_requests(self) -> None:
        with self.conn() as conn:
            self.assertTrue(db.queue_brief(conn, WONKA))
            self.assertFalse(db.queue_brief(conn, WONKA))  # a double click queues once
            self.assertTrue(db.claim_brief(conn, WONKA))
            self.assertFalse(db.claim_brief(conn, WONKA))
            db.save_brief(conn, WONKA, BRIEF)
            self.assertEqual(db.open_briefs(conn, WONKA), [])
            self.assertTrue(db.queue_brief(conn, WONKA, force=True))  # Redo
            self.assertEqual(db.queue_missing_briefs(conn, [WONKA, "x:y:1"]), ["x:y:1"])  # never twice


class CliTest(WebTestCase):
    def test_save_show_next_and_queue_line(self) -> None:
        with mock.patch("sys.stdin") as stdin:
            stdin.read.return_value = BRIEF
            code, out, err = self.run_cli("brief", "save", "--job", WONKA)
        self.assertEqual(code, 0, err)
        self.assertIn("remote · Python", self.run_cli("brief", "show", "--job", WONKA)[1])
        self.assertIn("hybrid 3 days", self.run_cli("brief", "show", "--job", INITECH)[1])
        self.assertIn("not linked to a getjo job", self.run_cli("brief", "show", "2")[2])
        self.assertIn(f"No brief for job {ACME_JOB} yet.", self.run_cli("brief", "show", str(ACME))[1])
        self.run_cli("brief", "queue", "--job", GLOBEX)
        self.assertIn("Job briefs: 1 brief waiting", self.run_cli("queue")[1])
        code, out, _ = self.run_cli("brief", "next")
        self.assertIn(f"Job {GLOBEX}: Globex Labs — Cloud Security Engineer (new offer, not tracked)", out)
        self.assertIn("## Must-haves", out)
        self.assertIn("no pay", out)
        self.assertIn(f"brief save --job {GLOBEX}", out)
        self.assertIn("job description (from getjo, read-only)", out)
        self.assertIn("Nothing pending", self.run_cli("brief", "next")[1])
        with mock.patch("sys.stdin") as stdin:
            stdin.read.return_value = "facts\n- £123k"
            code, _, err = self.run_cli("brief", "save", "--job", GLOBEX)
        self.assertEqual(code, 2)
        self.assertIn("must not mention pay", err)

    def test_research_packet_has_a_brief_step_for_a_tracked_job(self) -> None:
        with self.conn() as conn:
            db.queue_request(conn, ACME, "research")
        out = self.run_cli("research", "next", str(ACME))[1]
        self.assertIn("Job brief for #1", out)
        self.assertIn(f"brief save --job {ACME_JOB}", out)
        self.assertIn("job description", out)


class PageTest(WebTestCase):
    def panel(self, path: str) -> str:
        body = self.get(path)[2]
        start = body.index('<aside class="panel')
        return body[start:body.index("</aside>", start)]

    def test_background_queue_only_at_the_min_score_and_one_ping(self) -> None:
        with mock.patch.object(assistant, "notify_briefs_in_background") as bg:
            self.get("/")
            self.get("/?view=offers")
        with self.conn() as conn:
            queued = {r["getjo_job_id"] for r in db.open_briefs(conn)}
        self.assertIn(GLOBEX, queued)  # score 8
        self.assertNotIn(WONKA, queued)  # score 7: when its panel opens
        self.assertNotIn(INITECH, queued)  # has a brief
        bg.assert_called_once()
        self.assertEqual(bg.call_args.args[1], len(queued))

    def test_setting_and_env_override(self) -> None:
        with mock.patch.dict(os.environ, {"PREP_BRIEF_AUTO_MIN_SCORE": "7"}):
            self.settings = load_settings()
        self.assertEqual(self.settings.brief_auto_min_score, 7)
        self.get("/")
        with self.conn() as conn:
            self.assertIn(WONKA, {r["getjo_job_id"] for r in db.open_briefs(conn)})

    def test_opening_a_panel_queues_its_brief_and_polls(self) -> None:
        panel = self.panel(job_url(WONKA))
        self.assertIn("data-busy", panel[:200])
        self.assertIn("Writing brief…", panel)
        self.assertIn('class="description is-clamped" data-clamp', panel)
        status, _, out = self.get(f"/applications/thread?job={WONKA}")
        self.assertIn('"pending": true', out)
        with self.conn() as conn:
            db.save_brief(conn, WONKA, BRIEF)
        self.assertIn('"pending": false', self.get(f"/applications/thread?job={WONKA}")[2])

    def test_brief_shown_with_facts_and_original(self) -> None:
        panel = self.panel(job_url(INITECH))
        self.assertIn('<p class="brief-facts" aria-label="Key facts"><span class="fact">hybrid 3 days</span>', panel)
        self.assertIn("<h3>Must-haves</h3><ul><li>5+ years backend or platform work</li>", panel)
        self.assertIn('<details class="original"><summary>Show original</summary>', panel)
        self.assertIn("Redo brief", panel)
        self.assertNotIn("data-busy", panel[:200])

    def test_brief_is_escaped(self) -> None:
        with self.conn() as conn:
            db.save_brief(conn, INITECH, "<b>x</b> · y\n## <i>Sum</i>\n- <script>alert(1)</script>")
        panel = self.panel(job_url(INITECH))
        self.assertNotIn("<script>", panel)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", panel)

    def test_write_and_redo_buttons(self) -> None:
        panel = self.panel(f"/?app={ACME}")
        self.assertIn('action="/offers/brief"', panel)
        self.assertIn("Write brief", panel)
        with mock.patch.object(assistant, "notify_briefs_in_background") as bg:
            status, _, out = self.post("/offers/brief", {"job_id": ACME_JOB}, {**HOST, "Accept": "application/json"})
            self.post("/offers/brief", {"job_id": ACME_JOB})
        self.assertEqual(status, 200)
        bg.assert_called_once()  # the second click changes nothing
        self.assertIn("data-busy", self.panel(f"/?app={ACME}")[:200])
        self.assertIn('"pending": true', self.get(f"/applications/thread?app={ACME}")[2])
        self.assertEqual(self.post("/offers/brief", {"job_id": "nope:x:1"})[0], 400)


class FormatterTest(WebTestCase):
    def test_description_is_made_readable(self) -> None:
        text = ("About the team\nA fictional team.\n\n----------\n\nRequirements:\n- Python\n• SQL\n* Go\n\n"
                "WHAT WE OFFER\n1. Hybrid\n2) Training\n\nApply by email. Thanks.\n\nok")
        out = web._description(text)
        self.assertEqual(out, "<h4>About the team</h4><p>A fictional team.</p><h4>Requirements</h4>"
                              "<ul><li>Python</li><li>SQL</li><li>Go</li></ul><h4>WHAT WE OFFER</h4>"
                              "<ul><li>Hybrid</li><li>Training</li></ul><p>Apply by email. Thanks.</p><p>ok</p>")
        self.assertNotIn("---", out)

    def test_css(self) -> None:
        css = self.get("/static/style.css")[2]
        self.assertIn(".msg-body { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.45; }", css)
        self.assertNotIn("max-height: 18em", css)  # no scrollbar inside a message: the thread scrolls as one
        self.assertIn(".has-js .description.is-clamped:not(.is-open)", css)  # folded only with the script
