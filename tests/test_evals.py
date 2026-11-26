"""The Evals page: getjo's eval catalogue and results files read from a fixture checkout in a temp dir
(tests/fixtures/evals, made-up data), file-name safety, escaping and the empty states. Never the real
getjo checkout."""

import json
import os
import shutil
from pathlib import Path
from unittest import mock

from prep import evals
from prep.config import load_settings
from tests.test_web import WebTestCase

FIXTURE = Path(__file__).parent / "fixtures" / "evals"
RUNPY = "runpy-ci-20260118T111500.json"
OLD_RUNPY = "runpy-ci-20260114T101500.json"
PROMPTFOO = "promptfoo-ci-20260119T120000.json"
REPORT = "promptfoo-ci-20260119T120000.html"


class EvalsTestCase(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.tmp / "getjo-repo"
        shutil.copytree(FIXTURE, self.repo / "evals")
        self.use_repo(self.repo)

    def use_repo(self, repo: Path | str) -> None:
        patcher = mock.patch.dict(os.environ, {"PREP_GETJO_REPO": str(repo)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings = load_settings()

    def results(self) -> Path:
        return self.repo / "evals" / "results"


class CatalogTest(EvalsTestCase):
    def test_reads_entries_and_defaults(self) -> None:
        cat = evals.load_catalog(self.repo)
        self.assertEqual(cat["state"], "ok")
        ids = [e["id"] for e in cat["evals"]]
        self.assertEqual(ids[:2], ["unit-tests", "runpy-ci-gate"])
        gate = evals.find(cat, "runpy-ci-gate")
        self.assertEqual(gate["where"], ["CI", "local"])
        self.assertEqual(evals.find(cat, "drift")["command"], "")

    def test_states(self) -> None:
        self.assertEqual(evals.load_catalog(None)["state"], "no_repo")
        self.assertEqual(evals.load_catalog(self.tmp / "nowhere")["state"], "missing")
        (self.repo / "evals" / "catalog.json").write_text("{not json")
        self.assertEqual(evals.load_catalog(self.repo)["state"], "invalid")

    def test_unknown_status_is_a_gap(self) -> None:
        path = self.repo / "evals" / "catalog.json"
        data = json.loads(path.read_text())
        data["evals"][0]["status"] = "shiny"
        path.write_text(json.dumps(data))
        self.assertEqual(evals.load_catalog(self.repo)["evals"][0]["status"], "gap")

    def test_results_pattern(self) -> None:
        exact = evals.results_pattern("evals/results/runpy-ci-<YYYYMMDDTHHMMSS>.json")
        self.assertTrue(exact.match(RUNPY))
        self.assertFalse(exact.match("runpy-scoring-20260118T111500.json"))
        self.assertFalse(exact.match("runpy-ci-2026.json"))
        any_set = evals.results_pattern("evals/results/promptfoo-<set>-<YYYYMMDDTHHMMSS>.json")
        self.assertTrue(any_set.match(PROMPTFOO))
        self.assertFalse(any_set.match(REPORT))
        self.assertIsNone(evals.results_pattern(""))


class ResultsTest(EvalsTestCase):
    def test_list_results_oldest_first_json_only(self) -> None:
        (self.results() / "notes.txt").write_text("x")
        (self.results() / "runpy-ci-latest.json").write_text("{}")
        names = [f["name"] for f in evals.list_results(self.repo)]
        self.assertEqual(names, ["runpy-ci-20260110T091500.json", OLD_RUNPY, RUNPY, PROMPTFOO])
        self.assertTrue(evals.list_results(self.repo)[-1]["html"])
        self.assertEqual(evals.list_results(self.repo)[2]["when"], "2026-01-18 11:15")
        self.assertEqual(evals.list_results(None), [])

    def test_runpy_summary(self) -> None:
        s = evals.summary(RUNPY, evals.read_json(self.repo, RUNPY))
        self.assertEqual(s["runner"], "runpy")
        self.assertEqual(s["metrics"]["agreement"]["hits"], 10)
        self.assertEqual(s["prompt_sha"], "0f9e8d7c6b5a")
        self.assertTrue(s["passed"])
        self.assertEqual(len(s["sweep"]), 5)
        old = evals.summary(OLD_RUNPY, evals.read_json(self.repo, OLD_RUNPY))
        self.assertEqual(len(old["disagreements"]), 1)

    def test_promptfoo_summary(self) -> None:
        s = evals.summary(PROMPTFOO, evals.read_json(self.repo, PROMPTFOO))
        self.assertEqual(s["models"], ["claude-sonnet-5", "claude-haiku-4-5"])
        haiku = s["per_model"][1]
        self.assertEqual((haiku["pass"], haiku["fail"]), (2, 2))
        self.assertAlmostEqual(haiku["named"]["grounded"], 0.75)
        self.assertAlmostEqual(s["metrics"]["pass_rate"]["rate"], 0.75)
        cell = s["cases"][3]["cells"]["claude-haiku-4-5"]
        self.assertFalse(cell["checks"]["grounded"]["pass"])
        self.assertAlmostEqual(cell["judge"]["score"], 0.6)
        self.assertEqual(cell["score"], 8)

    def test_promptfoo_without_prompts_or_components(self) -> None:
        """Older or trimmed files: per-model numbers come from the rows, checks from namedScores."""
        data = evals.read_json(self.repo, PROMPTFOO)
        data["results"]["prompts"] = []
        for r in data["results"]["results"]:
            r.pop("gradingResult")
        s = evals.promptfoo_summary(data)
        self.assertEqual(s["per_model"][1]["pass"], 2)
        self.assertIn("verdict", s["cases"][1]["cells"]["claude-haiku-4-5"]["checks"])

    def test_history_and_headline(self) -> None:
        cat = evals.load_catalog(self.repo)
        files = evals.list_results(self.repo)
        gate = evals.history(self.repo, evals.find(cat, "runpy-ci-gate"), files)
        self.assertEqual([r["value"] for r in gate], [0.8, 0.9, 1.0])
        self.assertEqual(gate[0]["metric"], "agreement")
        judge = evals.history(self.repo, evals.find(cat, "rubric-judge"), files)
        self.assertEqual(judge[0]["metric"], "rubric_reasons")
        grid = evals.history(self.repo, evals.find(cat, "promptfoo-ci"), files)
        self.assertEqual(grid[0]["metric"], "verdict")

    def test_broken_file_is_skipped(self) -> None:
        (self.results() / "runpy-ci-20260120T090000.json").write_text("{broken")
        with self.assertLogs("prep.evals", "WARNING") as logs:
            gate = evals.history(self.repo, evals.find(evals.load_catalog(self.repo), "runpy-ci-gate"),
                                 evals.list_results(self.repo))
        self.assertEqual(len(gate), 3)
        self.assertNotIn("broken", "\n".join(logs.output))


class FileSafetyTest(EvalsTestCase):
    def test_safe_path_refuses_anything_else(self) -> None:
        (self.repo / "evals" / "secret.json").write_text("{}")
        for name in ("../catalog.json", "..%2Fcatalog.json", "/etc/passwd", "catalog.json", "results/" + RUNPY,
                     RUNPY + "/", "runpy-ci-20260118T111500.json.bak", ".runpy-ci-20260118T111500.json",
                     "runpy-../../x-20260118T111500.json", "", "runpy-CI-20260118T111500.json"):
            with self.subTest(name=name):
                self.assertIsNone(evals.safe_path(self.repo, name))
        self.assertIsNotNone(evals.safe_path(self.repo, RUNPY))
        self.assertIsNone(evals.safe_path(None, RUNPY))

    def test_symlink_out_of_results_is_refused(self) -> None:
        outside = self.tmp / "outside.json"
        outside.write_text("{}")
        (self.results() / "runpy-ci-20260120T090000.json").symlink_to(outside)
        self.assertIsNone(evals.safe_path(self.repo, "runpy-ci-20260120T090000.json"))

    def test_read_html_only_promptfoo_reports(self) -> None:
        self.assertIsNotNone(evals.read_html(self.repo, REPORT))
        self.assertIsNone(evals.read_html(self.repo, RUNPY))
        (self.results() / "runpy-ci-20260118T111500.html").write_text("<p>x</p>")
        self.assertIsNone(evals.read_html(self.repo, "runpy-ci-20260118T111500.html"))

    def test_report_route(self) -> None:
        status, headers, body = self.get(f"/evals/report/{REPORT}")
        self.assertEqual(status, 200)
        self.assertIn("sandbox", headers["Content-Security-Policy"])
        self.assertIn("connect-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("Example promptfoo report", body)
        for path in ("/evals/report/../catalog.json", "/evals/report/..%2Fcatalog.json", f"/evals/report/{RUNPY}",
                     "/evals/report/", "/evals/report/promptfoo-ci-20260119T120000.html/x"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 404)

    def test_run_route_refuses_bad_names(self) -> None:
        for q in ("../catalog.json", REPORT, "", "runpy-ci-20990101T000000.json"):
            with self.subTest(q=q):
                self.assertEqual(self.get(f"/evals/run?file={q}")[0], 404)


class PageTest(EvalsTestCase):
    def test_map(self) -> None:
        status, _, body = self.get("/evals")
        self.assertEqual(status, 200)
        self.assertIn("How evals fit together", body)
        self.assertIn('aria-current="page"><svg', body.split('href="/evals"')[1][:40])
        self.assertIn("theme-eucalyptus", body)
        self.assertIn("eval-card eval-gap", body)
        self.assertIn('data-copy="cmd-runpy-ci-gate"', body)
        self.assertIn("python -m evals.run --ci --min-agreement 8", body)
        self.assertIn("agreement <strong>100%</strong>", body)
        self.assertIn("evals/run.py</span> · <span class=\"mono\">main", body)
        self.assertEqual(body.count('id="eval-runpy-ci-gate"'), 1, "each eval shows once")

    def test_group_by_where(self) -> None:
        _, _, body = self.get("/evals?group=where")
        self.assertIn('eval-group-title">production', body)
        self.assertIn('eval-group-title">CI', body)

    def test_history(self) -> None:
        status, _, body = self.get("/evals?eval=runpy-ci-gate")
        self.assertEqual(status, 200)
        self.assertIn("History · agreement", body)
        self.assertIn("a1b2c3d4e5f6", body)
        self.assertIn('class="chart lines"', body)
        self.assertEqual(self.get("/evals?eval=nope")[0], 404)

    def test_runpy_run(self) -> None:
        status, _, body = self.get(f"/evals/run?file={OLD_RUNPY}")
        self.assertEqual(status, 200)
        for text in ("Confusion matrix", "Threshold sweep", "Disagreements", "Staff Engineer", "Soylent Foods",
                     "gate passed (min 8)", "7 ★"):
            self.assertIn(text, body)

    def test_promptfoo_run(self) -> None:
        status, _, body = self.get(f"/evals/run?file={PROMPTFOO}")
        self.assertEqual(status, 200)
        self.assertIn("Case × model", body)
        self.assertIn(f'href="/evals/report/{REPORT}"', body)
        self.assertIn("dealbreaker quote not in the posting", body)
        self.assertIn("judge 0.60", body)
        self.assertIn("0.65", body)  # the judge's mean for haiku, as a score

    def test_escaping(self) -> None:
        path = self.results() / RUNPY
        data = json.loads(path.read_text())
        data["cases"][0].update(agree=False, title="<script>alert(1)</script>", company='"><img src=x>')
        data["model"] = "<b>m</b>"
        path.write_text(json.dumps(data))
        cat = self.repo / "evals" / "catalog.json"
        c = json.loads(cat.read_text())
        c["evals"][1]["question"] = "<i>q</i>"
        c["evals"][1]["command"] = "run '<x>'"
        cat.write_text(json.dumps(c))
        for page in ("/evals", f"/evals/run?file={RUNPY}", "/evals?eval=runpy-ci-gate"):
            with self.subTest(page=page):
                _, _, body = self.get(page)
                self.assertNotIn("<script>alert", body)
                self.assertNotIn("<img src=x>", body)
                self.assertNotIn("<i>q</i>", body)
                self.assertNotIn("<b>m</b>", body)
                self.assertNotIn("'<x>'", body)

    def test_no_results_yet(self) -> None:
        shutil.rmtree(self.results())
        _, _, body = self.get("/evals")
        self.assertIn("No runs yet", body)
        self.assertIn("python -m evals.run --ci --min-agreement 8", body)
        _, _, body = self.get("/evals?eval=runpy-ci-gate")
        self.assertIn("No runs yet.", body)

    def test_no_repo(self) -> None:
        self.use_repo("")
        status, _, body = self.get("/evals")
        self.assertEqual(status, 200)
        self.assertIn("getjo_repo", body)
        self.assertEqual(self.get(f"/evals/run?file={RUNPY}")[0], 404)
        self.assertEqual(self.get(f"/evals/report/{REPORT}")[0], 404)

    def test_no_catalog(self) -> None:
        (self.repo / "evals" / "catalog.json").unlink()
        _, _, body = self.get("/evals")
        self.assertIn("no eval catalogue here yet", body)
