"""Top 5 this week: the criteria file, the gate, ranking, the week's picks, company checks and job screens.
Made-up data only (tests/fixtures/search-example.json, criteria.example.md)."""

import json
import os
from contextlib import closing
from pathlib import Path
from unittest import mock

from prep import criteria, db, search, web
from prep.config import REPO_ROOT, load_settings
from prep.seed_example import SEARCH_FIXTURES, add_search_example, build_getjo_db, build_prep_db, load_fixtures
from tests.helpers import IsolatedTestCase
from tests.test_web import HOST, TODAY

EXAMPLE = REPO_ROOT / "criteria.example.md"
INITRODE = "ashby:initrode-ai:a1"
STERLING = "ashby:sterling-labs:s7"


def crit() -> criteria.Criteria:
    return criteria.parse(EXAMPLE.read_text(encoding="utf-8"), EXAMPLE)


def job(**kw) -> dict:
    return {"id": "x:y:1", "company": "Example Co", "title": "Senior AI Engineer", "location": "London",
            "score": 8, "posted_at": "2026-01-18T00:00:00Z", "first_seen": "2026-01-18T08:00:00Z", **kw}


class CriteriaTest(IsolatedTestCase):
    def test_example_file_is_read(self) -> None:
        c = crit()
        self.assertEqual(c.gates, ("company", "sector", "interviews", "role_family", "level", "team", "location", "pay"))
        self.assertIn("staff", c.level_out)
        self.assertIn("graduate", c.level_out)  # the Out list runs over two lines
        self.assertEqual((c.cities, c.remote_regions), (("London",), ("UK",)))
        self.assertEqual(c.ranking, ("builds_agents", "evals", "fit", "verdict", "product", "permanent", "fresh"))
        self.assertEqual(c.fresh_days, 14)
        self.assertNotIn("pay", c.gate_text)  # the pay gate's text (its figure) is never kept

    def test_a_gate_left_out_is_off_and_order_follows_the_file(self) -> None:
        text = "## Gates\n\n1. **Level**: senior. Out: lead.\n\n## Ranking\n\n1. Fresh posting: under 5 days.\n2. CV fit score.\n"
        c = criteria.parse(text, Path("x"))
        self.assertEqual(c.gates, ("level",))
        self.assertEqual(c.ranking, ("fresh", "fit"))
        self.assertEqual(c.fresh_days, 5)

    def test_missing_file(self) -> None:
        with self.assertRaises(criteria.CriteriaMissing):
            criteria.load(load_settings())
        with mock.patch.dict(os.environ, {"PREP_CRITERIA_FILE": str(EXAMPLE)}):
            self.assertEqual(criteria.load(load_settings()).path, EXAMPLE)


class GateTest(IsolatedTestCase):
    def test_plain_gates(self) -> None:
        c = crit()
        self.assertEqual(search.title_level("Staff AI Engineer", c), "no")
        self.assertEqual(search.title_level("Lead Engineer, AI", c), "no")
        self.assertEqual(search.title_level("Senior LLM Engineer", c), "yes")
        self.assertEqual(search.title_level("AI Engineer", c), "unknown")
        self.assertEqual(search.plain_location("South West London", c), "yes")
        self.assertEqual(search.plain_location("Remote (UK)", c), "yes")
        self.assertEqual(search.plain_location("Remote", c), "unknown")
        self.assertEqual(search.plain_location("Manchester", c), "no")
        self.assertEqual(search.plain_location("", c), "unknown")

    def test_states(self) -> None:
        c = crit()
        ok = {"verdict": "strong", "sector_ok": "yes", "interviews": "yes", "kind": "product"}
        screen = {k: "yes" for k in db.SCREEN_FIELDS} | {"pay": "unknown", "contract": "permanent"}
        self.assertEqual(search.evaluate(job(), ok, screen, c, TODAY)["state"], "passed")
        self.assertEqual(search.evaluate(job(), ok, None, c, TODAY)["state"], "screening")
        self.assertEqual(search.evaluate(job(), {**ok, "interviews": "unknown"}, screen, c, TODAY)["state"], "held")
        failed = search.evaluate(job(), {**ok, "interviews": "no"}, screen, c, TODAY)
        self.assertEqual((failed["state"], failed["failed"]), ("failed", ["interviews"]))
        self.assertEqual(search.evaluate(job(), {**ok, "verdict": "drop"}, screen, c, TODAY)["failed"], ["company"])
        self.assertEqual(search.evaluate(job(), ok, {**screen, "pay": "fail"}, c, TODAY)["failed"], ["pay"])
        self.assertEqual(search.evaluate(job(), ok, {**screen, "level": "no"}, c, TODAY)["failed"], ["level"])
        # getjo watches the company: no check passes the company gate; another source needs a check
        self.assertEqual(search.gates(job(), None, screen, c)["company"], "yes")
        self.assertEqual(search.gates(job(origin="linkedin"), None, screen, c)["company"], "unknown")

    def test_ranking_follows_the_criteria_order_and_says_why(self) -> None:
        c = crit()
        check = {"verdict": "possible", "kind": "product", "interviews": "yes", "sector_ok": "yes"}
        base = {k: "yes" for k in db.SCREEN_FIELDS} | {"pay": "unknown", "contract": "permanent"}
        agents = search.evaluate(job(id="a", score=7), check, {**base, "evals": "no"}, c, TODAY)
        fit = search.evaluate(job(id="b", score=9), check, {**base, "builds_agents": "no"}, c, TODAY)
        self.assertGreater(agents["rank"], fit["rank"])  # builds agents ranks above a higher fit score
        line = search.why_line(job(score=8), {"verdict": "strong"}, {"builds_agents": "yes", "evals": "yes"}, TODAY)
        self.assertEqual(line, "8/10 fit · strong company · builds agents · evals in role · posted 2 days ago")

    def test_week_start(self) -> None:
        self.assertEqual(search.week_start("2026-01-20"), "2026-01-19")  # a Tuesday
        self.assertEqual(search.week_start("2026-01-19"), "2026-01-19")
        self.assertEqual(search.week_start("2026-01-25"), "2026-01-19")


class BoardTest(IsolatedTestCase):
    """The example data: 13 made-up jobs, their company checks and screens."""

    def setUp(self) -> None:
        super().setUp()
        self.extra = load_fixtures(SEARCH_FIXTURES)
        build_prep_db(self.prep_db, self.fixtures)
        add_search_example(self.prep_db, self.extra, TODAY)
        self.jobs = [{**j, "origin": "getjo"} for j in self.extra["getjo_jobs"]]

    def board(self, today: str = TODAY, n: int = 5) -> dict:
        with closing(db.connect(self.prep_db)) as conn:
            return search.board(conn, self.jobs, crit(), today, n)

    def test_example_board(self) -> None:
        found = self.board()
        self.assertEqual([p["getjo_job_id"] for p in found["picks"]][:2], [INITRODE, "lever:bluth-retail:b3"])
        self.assertEqual(len(found["picks"]), 5)
        self.assertEqual({r["job"]["company"] for r in found["held"]}, {"Sterling Labs"})
        self.assertEqual([r["job"]["company"] for r in found["screening"]], ["Kramerica"])
        self.assertEqual([r["job"]["company"] for r in found["more"]], ["Cogswell Cloud"])
        failed = {r["job"]["company"]: search.failed_text(r) for r in found["failed"]}
        self.assertEqual(failed, {"Gekko Analytics": "level", "Wernham Paper": "interview style",
                                  "Monsters Example": "location", "Hooli Example": "pay"})

    def test_picks_stay_for_the_week_and_refill(self) -> None:
        first = self.board()
        with closing(db.connect(self.prep_db)) as conn:
            db.record_decision(conn, {"id": INITRODE}, "skipped", "location")
        self.jobs = [j for j in self.jobs if j["id"] != INITRODE]  # skipped: no longer a candidate
        again = self.board()
        self.assertEqual([p["getjo_job_id"] for p in again["picks"]], [p["getjo_job_id"] for p in first["picks"]])
        self.assertEqual(again["picks"][0]["outcome"], "skipped")
        # a pick not acted on that stops passing is replaced by the next one
        with closing(db.connect(self.prep_db)) as conn:
            db.save_company_check(conn, "Bluth Retail", {"interviews": "no"}, "user")
        third = self.board()
        ids = [p["getjo_job_id"] for p in third["picks"]]
        self.assertNotIn("lever:bluth-retail:b3", ids)
        self.assertIn("ashby:cogswell-cloud:c6", ids)
        # next week: the skipped job is gone, the rest roll into a new ranking
        nxt = self.board("2026-01-27")
        self.assertNotIn(INITRODE, [p["getjo_job_id"] for p in nxt["picks"]])

    def test_held_company_decided_by_one_click(self) -> None:
        with closing(db.connect(self.prep_db)) as conn:
            db.save_company_check(conn, "Sterling Labs", {"interviews": "yes"}, "user")
        found = self.board(n=10)
        self.assertEqual(found["held"], [])
        self.assertIn(STERLING, [p["getjo_job_id"] for p in found["picks"]])

    def test_pick_stats(self) -> None:
        self.board()
        with closing(db.connect(self.prep_db)) as conn:
            stats = search.pick_stats(conn)
        self.assertEqual((stats["picked"], stats["applied"], stats["skipped"], stats["open"]), (7, 1, 1, 5))
        self.assertEqual(stats["reasons"], [("role_type", 1)])
        self.assertAlmostEqual(stats["share"], 1 / 7)


class ImportTest(IsolatedTestCase):
    def test_company_list_json_and_aliases(self) -> None:
        data = {"companies": [
            {"company": "Example Group (Example Media, incl. Sample Press, London)", "sector": "Media",
             "verdict": "strong", "leetcode_risk": "low", "interview_style": "Take-home; no LeetCode"},
            {"company": "Puzzle Co", "sector": "Software consultancy", "verdict": "possible",
             "leetcode_risk": "medium", "interview_style": "HackerRank screen, then live coding"},
            {"company": "Mixed Co", "sector": "Retail", "verdict": "strong",
             "leetcode_risk": "medium", "interview_style": "Mixed: practical exercise; HackerRank reported elsewhere"},
            {"company": "Low Co", "sector": "Retail", "verdict": "strong",
             "leetcode_risk": "low", "interview_style": "Take-home; one report of LeetCode-type questions"},
            {"company": "Dropped Ltd", "sector": "Gambling", "verdict": "drop", "leetcode_risk": "unknown"},
        ]}
        rows = search.parse_company_list("window.REPORT_DATA = " + json.dumps(data) + ";", markdown=False)
        with closing(db.connect(self.prep_db)) as conn:
            self.assertEqual(search.import_companies(conn, rows), {"saved": 5, "kept": 0})
            self.assertEqual(db.company_check(conn, "Mixed Co")["interviews"], "unknown")
            self.assertEqual(db.company_check(conn, "Low Co")["interviews"], "yes")
            self.assertEqual(db.company_check(conn, "Sample Press")["slug"], "example-group")
            self.assertIsNone(db.company_check(conn, "London"))
            puzzle = db.company_check(conn, "Puzzle Co")
            self.assertEqual((puzzle["interviews"], puzzle["kind"]), ("no", "consultancy"))
            self.assertEqual(db.company_check(conn, "Example Media")["interviews"], "yes")
            dropped = db.company_check(conn, "Dropped")
            self.assertEqual((dropped["verdict"], dropped["sector_ok"], dropped["interviews"]), ("drop", "unknown", "unknown"))
            # an agent's or the user's check is kept on a new import
            db.save_company_check(conn, "Puzzle Co", {"interviews": "yes"}, "user")
            self.assertEqual(search.import_companies(conn, rows), {"saved": 4, "kept": 1})
            self.assertEqual(db.company_check(conn, "Puzzle Co")["interviews"], "yes")

    def test_markdown_interview_table(self) -> None:
        text = ("company | LeetCode-style | evidence\n--- | --- | ---\nAlpha Example | yes | live DFS\n"
                "Beta Example (Beta) | no | take-home\nGamma Example | mixed | some HackerRank\n")
        rows = search.parse_company_list(text, markdown=True)
        self.assertEqual([(r[0], r[2]["interviews"]) for r in rows],
                         [("Alpha Example", "no"), ("Beta Example", "yes"), ("Gamma Example", "unknown")])

    def test_cli_import_and_list(self) -> None:
        path = self.tmp / "list.json"
        path.write_text(json.dumps({"companies": [{"company": "Alpha Example", "verdict": "strong",
                                                   "leetcode_risk": "low", "sector": "Retail"}]}), encoding="utf-8")
        code, out, _ = self.run_cli("companies", "import", str(path))
        self.assertEqual(code, 0)
        self.assertIn("1 company checks saved", out)
        _, out, _ = self.run_cli("companies", "list")
        self.assertIn("Alpha Example: verdict strong", out)


class ScreenCliTest(IsolatedTestCase):
    """The Research worker's side: the brief packet asks for the screen and the company check; saves store them."""

    WONKA = "ashby:wonka-systems:w2"  # a new offer in example.json

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"PREP_CRITERIA_FILE": str(EXAMPLE)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_packet_brief_with_screen_and_company_check(self) -> None:
        with closing(db.connect(self.prep_db)) as conn:
            db.queue_brief(conn, self.WONKA)
        code, out, _ = self.run_cli("brief", "next")
        self.assertEqual(code, 0)
        self.assertIn("Top 5 screen", out)
        self.assertIn("pay = \"unknown\"", out)
        self.assertIn(f"company check --job {self.WONKA}", out)
        self.assertIn(f"research save --job {self.WONKA}", out)
        text = ('+++\nrole_family = "yes"\nlevel = "yes"\npay = "pass"\nbuilds_agents = "no"\n+++\n'
                "hybrid 2 days · Python · Senior · London\n## Summary\nA made-up role.\n")
        with mock.patch("sys.stdin.read", return_value=text):
            code, out, err = self.run_cli("brief", "save", "--job", self.WONKA)
        self.assertEqual(code, 0, err)
        self.assertIn("Saved the screen", out)
        with closing(db.connect(self.prep_db)) as conn:
            screen = db.job_screens(conn)[self.WONKA]
            self.assertEqual((screen["pay"], screen["team"], screen["builds_agents"]), ("pass", "unknown", "no"))
            self.assertTrue(db.job_briefs(conn, {self.WONKA})[self.WONKA]["brief"].startswith("hybrid 2 days"))
            self.assertEqual(db.open_briefs(conn, self.WONKA), [])
        check = 'verdict = "possible"\nsector_ok = "yes"\ninterviews = "yes"\nkind = "product"\nnote = "Take-home"\n'
        with mock.patch("sys.stdin.read", return_value=check):
            code, out, err = self.run_cli("company", "check", "--job", self.WONKA)
        self.assertEqual(code, 0, err)
        with closing(db.connect(self.prep_db)) as conn:
            saved = db.company_check(conn, "Wonka Systems")
        self.assertEqual((saved["interviews"], saved["source"]), ("yes", "agent"))
        self.assertIsNotNone(saved["agent_checked_at"])

    def test_pay_is_only_pass_fail_unknown(self) -> None:
        with mock.patch("sys.stdin.read", return_value='pay = "90000"\n'):
            code, _, err = self.run_cli("screen", "save", "--job", self.WONKA)
        self.assertEqual(code, 2)
        self.assertIn("pay", err)
        with mock.patch("sys.stdin.read", return_value='note = "budget £100"\n'):
            code, _, err = self.run_cli("company", "check", "--job", self.WONKA)
        self.assertEqual(code, 2)

    def test_screen_only_redo_for_an_old_brief(self) -> None:
        with closing(db.connect(self.prep_db)) as conn:
            db.queue_brief(conn, self.WONKA)
            db.save_brief(conn, self.WONKA, "remote · Python · Senior · London\n## Summary\nMade up.")
            self.assertEqual(db.queue_screens(conn, [self.WONKA]), [self.WONKA])
            self.assertEqual(db.queue_screens(conn, [self.WONKA]), [])  # already open
        _, out, _ = self.run_cli("brief", "next")
        self.assertIn("write only the screen", out)
        with mock.patch("sys.stdin.read", return_value='role_family = "yes"\n'):
            self.assertEqual(self.run_cli("screen", "save", "--job", self.WONKA)[0], 0)
        with closing(db.connect(self.prep_db)) as conn:
            self.assertEqual(db.open_briefs(conn, self.WONKA), [])
            conn.execute("DELETE FROM job_screens")
            conn.commit()
            self.assertEqual(db.queue_screens(conn, [self.WONKA]), [])  # asked once only


class TopViewWebTest(IsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        extra = load_fixtures(SEARCH_FIXTURES)
        build_getjo_db(self.getjo_db, self.fixtures["getjo_jobs"] + extra["getjo_jobs"])
        build_prep_db(self.prep_db, self.fixtures)
        add_search_example(self.prep_db, extra, TODAY)
        patcher = mock.patch.dict(os.environ, {"PREP_CRITERIA_FILE": str(EXAMPLE)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings = load_settings()

    def get(self, path: str) -> str:
        status, _, body = web.handle(self.settings, "GET", path, HOST, today=TODAY)
        self.assertEqual(status, 200)
        return body.decode("utf-8")

    def test_top_view(self) -> None:
        body = self.get("/?view=offers")
        self.assertIn("Top 5 this week", body)
        self.assertIn("9/10 fit · strong company · builds agents · evals in role · posted 3 days ago", body)
        self.assertIn("Held: checking interviews", body)
        self.assertIn("Sterling Labs (2)", body)
        self.assertIn("More (1)", body)
        self.assertIn("Filtered out (5)", body)  # with example.json's Initech Cloud (Manchester)
        self.assertIn("Screening: 2 jobs", body)
        self.assertLess(body.index("Initrode AI"), body.index("Held: checking interviews"))
        # All offers and filters show the plain list
        self.assertNotIn("Top 5 this week", self.get("/?view=offers&all=1"))
        self.assertNotIn("Top 5 this week", self.get("/?view=offers&q=sterling"))

    def test_screens_queued_for_candidates_plain_code_does_not_fail(self) -> None:
        self.get("/?view=offers")
        with closing(db.connect(self.prep_db)) as conn:
            open_ids = {r["getjo_job_id"] for r in db.open_briefs(conn)}
        self.assertIn("ashby:kramerica:k9", open_ids)
        self.assertNotIn("greenhouse:gekko-analytics:g10", open_ids)  # Staff: failed by plain code (score 9: no auto brief)

    def test_held_one_click(self) -> None:
        self.get("/?view=offers")
        status, _, _ = web.handle(self.settings, "POST", "/offers/interviews", HOST,
                                  b"company=Sterling+Labs&interviews=no", today=TODAY)
        self.assertEqual(status, 303)
        body = self.get("/?view=offers")
        self.assertNotIn("Held: checking interviews", body)
        with closing(db.connect(self.prep_db)) as conn:
            self.assertEqual(db.company_check(conn, "Sterling Labs")["source"], "user")
        status, _, _ = web.handle(self.settings, "POST", "/offers/interviews", HOST,
                                  b"company=Sterling+Labs&interviews=maybe", today=TODAY)
        self.assertEqual(status, 400)

    def test_stats_card(self) -> None:
        self.get("/?view=offers")
        body = self.get("/stats")
        self.assertIn("Top 5: how many were applied to", body)
        self.assertIn("1 of 7 Top 5 jobs", body)
        self.assertIn("skipped: Role type", body)

    def test_without_criteria(self) -> None:
        with mock.patch.dict(os.environ, {"PREP_CRITERIA_FILE": str(self.tmp / "none.md")}):
            self.settings = load_settings()
            body = self.get("/?view=offers")
        self.assertNotIn("Top 5 this week", body)
        self.assertIn("Top 5 is off", body)
