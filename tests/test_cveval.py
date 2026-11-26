"""The AI check of a tailored CV (grounding, fit, skim): parsing the agent's .eval.md file, mapping it to
findings, merging them into the verdict, the queue on every tailor save, the version race, the packet,
`tailor eval`, the panel card and the facts bank setting. Made-up CVs, companies and outputs only; no
network and no model call: the agent's output is a fixture file."""

import io
import os
from contextlib import closing
from pathlib import Path
from unittest import mock

from prep import config, cveval, db, privacy, research
from tests.helpers import IsolatedTestCase
from tests.test_cvcheck import CLEAN, MASTER, OFFSET
from tests.test_research import write
from tests.test_web import TODAY, WebTestCase

# The CLEAN tailored CV's lines (file line numbers, front matter counted) used below.
SUMMARY_LINE = OFFSET + 5   # "Platform engineer who builds delivery pipelines for AI teams."
TERRAFORM_LINE = OFFSET + 9  # "- Moved 40 services to Terraform."
KUBERNETES_LINE = OFFSET + 14  # "- Ran Kubernetes clusters."


def eval_text(version: int = 1, fit: int = 4, skim: int = 4, skim_why: str = "Title and AI pipelines up top",
              claims: str = "", must_haves: str = "", editorial: str = "") -> str:
    return (f'+++\nversion = {version}\nchecked_at = 2026-01-21\nfit_score = {fit}\nskim_score = {skim}\n'
            f'skim_why = "{skim_why}"\n{claims}{must_haves}{editorial}+++\n\n## Notes\nFree text.\n')


# A made-up tailored CV with wording that is true but sloppy: a general term next to its own examples in
# the skills line (a job keyword), and a word the agreed wording bans.
EDITED = (CLEAN.replace('keywords = ["Terraform"', 'keywords = ["LLM APIs", "Terraform"')
          .replace("- Terraform, Kubernetes, Python", "- LLM APIs, Acme Model API, Globex Model API, Python")
          .replace("- Built a shared deployment pipeline.", "- Spearheaded a shared deployment pipeline."))
PIPELINE_LINE = OFFSET + 10  # "- Spearheaded a shared deployment pipeline."
SKILLS_LINE = OFFSET + 17    # "- LLM APIs, Acme Model API, Globex Model API, Python"


def editorial(quote: str, issue: str, suggestion: str) -> str:
    return f'[[editorial]]\nquote = "{quote}"\nissue = "{issue}"\nsuggestion = "{suggestion}"\n'


REDUNDANT = editorial("LLM APIs, Acme Model API, Globex Model API", "a general term next to its own examples",
                      "LLM APIs (Acme Model API, Globex Model API)")
BANNED = editorial("Spearheaded a shared deployment pipeline", "spearheaded is a banned word in the agreed wording",
                   "Built a shared deployment pipeline")


SOURCED = ('[[claims]]\nquote = "builds delivery pipelines for AI teams"\nsource = "cv.md"\n'
           'note = "reworded from the summary"\n')
UNSOURCED = ('[[claims]]\nquote = "Moved 40 services to terraform"\nsource = "none"\n'
             'note = "cv.md says 40 services; fine"\n'  # the agent's line number would be wrong: none is given
             '[[claims]]\nquote = "ran kubernetes  clusters"\nsource = "none"\nnote = "no team size in cv.md"\n')
LOST = '[[claims]]\nquote = "Led a team of nine"\nsource = "none"\nnote = "not in the CV at all"\n'
MUST = ('[[must_haves]]\nterm = "Terraform"\ncovered = false\n'   # in the file's keywords: the rules handle it
        '[[must_haves]]\nterm = "Go"\ncovered = false\n'
        '[[must_haves]]\nterm = "Python"\ncovered = true\n')


class ParseTest(IsolatedTestCase):
    def read(self, text: str) -> dict:
        return cveval.read_eval(write(self.tmp / "x.eval.md", text))

    def test_reads_scores_claims_and_must_haves(self) -> None:
        found = self.read(eval_text(version=2, claims=SOURCED + UNSOURCED, must_haves=MUST))
        self.assertEqual((found["version"], found["fit_score"], found["skim_score"], found["checked_at"]),
                         (2, 4, 4, "2026-01-21"))
        self.assertEqual([c["source"] for c in found["claims"]], ["cv.md", "none", "none"])
        self.assertEqual([(m["term"], m["covered"]) for m in found["must_haves"]],
                         [("Terraform", False), ("Go", False), ("Python", True)])

    def test_refuses_what_it_cannot_use(self) -> None:
        for text, why in (
            (eval_text(fit=6), "fit_score"),
            (eval_text(skim=0), "skim_score"),
            (eval_text(skim_why=""), "skim_why"),
            (eval_text(version=0), "version"),
            (eval_text(claims='[[claims]]\nquote = "x"\nsource = "memory"\n'), "source"),
            (eval_text(claims='[[claims]]\nsource = "none"\n'), "quote"),
            (eval_text(must_haves='[[must_haves]]\nterm = "Go"\ncovered = "yes"\n'), "covered"),
            ("no front matter", r"\+\+\+"),
        ):
            with self.subTest(why=why), self.assertRaisesRegex(ValueError, why):
                self.read(text)

    def test_reads_editorial_entries(self) -> None:
        found = self.read(eval_text(editorial=REDUNDANT))
        self.assertEqual(found["editorial"], [{"quote": "LLM APIs, Acme Model API, Globex Model API",
                                               "issue": "a general term next to its own examples",
                                               "suggestion": "LLM APIs (Acme Model API, Globex Model API)",
                                               "kind": "wording"}])
        self.assertEqual(self.read(eval_text())["editorial"], [], "editorial is optional")
        for text, why in (
            (editorial("", "x", "y"), "editorial 1 needs a quote"),
            (editorial("x", "", "y"), "editorial 1 needs an issue"),
            ('[[editorial]]\nquote = "x"\nissue = "y"\n', "editorial 1 needs a suggestion"),
            (editorial("x", "y", "z") + 'kind = "style"\n', "kind must be one of wording, grammar, spelling"),
        ):
            with self.subTest(why=why), self.assertRaisesRegex(ValueError, why):
                self.read(eval_text(editorial=text))

    def test_eval_file_sits_beside_its_version(self) -> None:
        self.assertEqual(cveval.eval_file(Path("/p/cv-tailored/CV_Globex_FDE_101126_2.md")),
                         Path("/p/cv-tailored/CV_Globex_FDE_101126_2.eval.md"))
        self.assertTrue(privacy.is_forbidden("cv-tailored/CV_Globex_FDE_101126_2.eval.md"))


class FindingsTest(IsolatedTestCase):
    def findings(self, master_fit: int | None = 4, **kw) -> list[dict]:
        review = cveval.read_eval(write(self.tmp / "x.eval.md", eval_text(**kw)))
        return cveval.findings(review, CLEAN, master_fit)

    def test_unsourced_claims_block_with_the_line_found_by_their_words(self) -> None:
        found = self.findings(claims=SOURCED + UNSOURCED)
        self.assertEqual([(f["check"], f["severity"], f["line"]) for f in found],
                         [("grounding", "block", TERRAFORM_LINE), ("grounding", "block", KUBERNETES_LINE)])
        self.assertEqual(found[0]["text"], "- Moved 40 services to Terraform.", "the line comes from the file")
        self.assertEqual(found[1]["text"], "- Ran Kubernetes clusters.", "case and spacing do not matter")
        self.assertIn("no team size in cv.md", found[1]["message"])

    def test_sourced_claims_and_good_scores_flag_nothing(self) -> None:
        self.assertEqual(self.findings(claims=SOURCED, fit=4, skim=3), [])

    def test_fit_and_skim(self) -> None:
        found = self.findings(master_fit=4, fit=3, skim=2, skim_why="Page one opens with an unrelated role",
                              must_haves=MUST)
        self.assertEqual([(f["check"], f["severity"]) for f in found], [("fit", "look")] * 2 + [("skim", "look")])
        messages = " | ".join(f["message"] for f in found)
        self.assertIn("Must-haves the CV does not cover: Go |", messages, "Terraform is left to the keyword rule")
        self.assertIn("Fit 3/5 is below the master CV's 4/5", messages)
        self.assertIn("Six-second skim 2/5: Page one opens with an unrelated role", messages)
        self.assertEqual(self.findings(master_fit=None, fit=1), [], "no master fit: nothing to compare")

    def test_editorial_entries_ask_for_a_look_on_their_line(self) -> None:
        review = cveval.read_eval(write(self.tmp / "x.eval.md", eval_text(editorial=REDUNDANT + BANNED)))
        found = cveval.findings(review, EDITED, 4)
        self.assertEqual([(f["check"], f["severity"], f["line"]) for f in found],
                         [("editorial", "look", PIPELINE_LINE), ("editorial", "look", SKILLS_LINE)])
        self.assertEqual(found[0]["message"], "Editorial: spearheaded is a banned word in the agreed wording; "
                                              "suggest: Built a shared deployment pipeline")
        self.assertEqual(found[1]["text"], "- LLM APIs, Acme Model API, Globex Model API, Python")
        self.assertIn("suggest: LLM APIs (Acme Model API, Globex Model API)", found[1]["message"])
        self.assertEqual(cveval.summary(review, 4)["editorial"], 2)

    def test_never_shows_pay(self) -> None:
        text = CLEAN.replace("- Ran the on-call rotation.", "- Expected salary £123,456")
        review = cveval.read_eval(write(self.tmp / "x.eval.md", eval_text(
            skim=1, skim_why="Leads with a £123,456 figure",
            claims='[[claims]]\nquote = "Expected salary"\nsource = "none"\nnote = "salary £123,456 not in cv.md"\n')))
        found = cveval.findings(review, text, None)
        self.assertNotIn("123,456", repr(found) + repr(cveval.summary(review, None)))
        self.assertEqual([f["check"] for f in found], ["skim", "grounding"])


class StorageTest(IsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.conn = db.connect(self.prep_db)
        self.addCleanup(self.conn.close)
        self.app, _ = db.record_application(self.conn, "Globex Labs", "Engineer", status="to_apply")
        self.v1 = write(self.tmp / "private" / "cv-tailored" / "CV_GlobexLabs_Eng_200126_1.md", CLEAN)

    def evals(self) -> list[dict]:
        return [r for r in db.open_requests(self.conn, self.app) if r["kind"] == "cv_eval"]

    def save_eval(self, version: int, path: Path, **kw) -> dict:
        return cveval.save(self.conn, self.app, version, write(cveval.eval_file(path), eval_text(version=version, **kw)))

    def test_the_ai_check_is_queued_only_when_asked_for(self) -> None:
        with self.assertRaisesRegex(LookupError, "no tailored CV"):
            db.queue_request(self.conn, self.app, "cv_eval")
        db.save_tailored_cv(self.conn, self.app, str(self.v1), "v1")
        self.assertEqual(self.evals(), [], "a save queues no AI check")
        self.assertEqual(db.eval_state(self.conn, self.app, db.cv_check(self.conn, self.app, 1)), "none")
        db.queue_request(self.conn, self.app, "cv_eval")
        db.save_tailored_cv(self.conn, self.app, str(self.v1), "v2 again")
        db.queue_request(self.conn, self.app, "cv_eval")
        self.assertEqual(len(self.evals()), 1, "an open check is reused: it checks the latest version")
        self.assertEqual(db.eval_state(self.conn, self.app, db.cv_check(self.conn, self.app, 2)), "pending")

    def test_ai_findings_merge_into_the_verdict_and_survive_a_rules_rerun(self) -> None:
        db.save_tailored_cv(self.conn, self.app, str(self.v1), "v1")
        db.save_cv_check(self.conn, self.app, 1, "rules", [], ["Summary: 1 line reworded"])
        found = self.save_eval(1, self.v1, claims=UNSOURCED)
        self.assertEqual(found["verdict"], "blocked", "unsourced claims block")
        self.assertEqual(found["summary"]["editorial"], 0)
        self.assertEqual(found["summary"]["unsourced"], 2)
        self.assertEqual(found["changes"], ["Summary: 1 line reworded"])
        db.save_cv_check(self.conn, self.app, 1, "rules", [{"check": "pages", "severity": "look", "line": None,
                                                           "message": "No PDF"}], [])
        again = db.cv_check(self.conn, self.app, 1)
        self.assertEqual(sorted(f["source"] for f in again["findings"]), ["ai", "ai", "rules"])
        self.assertEqual((again["verdict"], again["summary"]["fit"]), ("blocked", 4), "tailor check keeps the AI check")
        self.assertEqual(db.eval_state(self.conn, self.app, again), "done")
        self.assertEqual(self.evals(), [])
        redone = self.save_eval(1, self.v1, claims=SOURCED)
        self.assertEqual(([f["source"] for f in redone["findings"]], redone["verdict"]), (["rules"], "look"),
                         "a new AI check replaces only its own findings")

    def test_a_version_saved_while_the_check_ran_is_checked_too(self) -> None:
        db.save_tailored_cv(self.conn, self.app, str(self.v1), "v1")
        db.queue_request(self.conn, self.app, "cv_eval")
        db.claim_request(self.conn, self.evals()[0]["id"])
        v2 = write(self.v1.with_name("CV_GlobexLabs_Eng_200126_2.md"), CLEAN)
        db.save_tailored_cv(self.conn, self.app, str(v2), "v2")
        self.save_eval(1, self.v1)
        self.assertEqual(len(self.evals()), 1, "v1's result leaves the request open for v2")
        self.assertIsNotNone(db.cv_check(self.conn, self.app, 1)["summary"])
        self.save_eval(2, v2)
        self.assertEqual(self.evals(), [])

    def test_the_file_must_be_for_that_version(self) -> None:
        db.save_tailored_cv(self.conn, self.app, str(self.v1), "v1")
        with self.assertRaisesRegex(ValueError, "unsourced claim 2: the quote is not in v1"):
            self.save_eval(1, self.v1, claims=SOURCED + LOST)
        self.assertIsNone(db.cv_check(self.conn, self.app, 1), "nothing stored")
        with self.assertRaisesRegex(ValueError, "checks v2, not v1"):
            cveval.save(self.conn, self.app, 1, write(self.tmp / "x.eval.md", eval_text(version=2)))
        with self.assertRaisesRegex(LookupError, "no tailored CV v3"):
            cveval.save(self.conn, self.app, 3, write(self.tmp / "x.eval.md", eval_text(version=3)))

    def test_editorial_findings_look_and_never_block(self) -> None:
        write(self.v1, EDITED)
        db.save_tailored_cv(self.conn, self.app, str(self.v1), "v1")
        self.assertEqual(self.save_eval(1, self.v1, claims=SOURCED)["verdict"], "ready",
                         "no editorial entries: the verdict is unchanged")
        found = self.save_eval(1, self.v1, claims=SOURCED, editorial=REDUNDANT + BANNED)
        self.assertEqual((found["verdict"], [f["check"] for f in found["findings"]]), ("look", ["editorial"] * 2))
        self.assertTrue(cveval.summary_line(found).endswith(" · 0/0 must-haves · 2 editorial"))

    def test_editorial_quote_must_be_in_the_version(self) -> None:
        write(self.v1, EDITED)
        db.save_tailored_cv(self.conn, self.app, str(self.v1), "v1")
        with self.assertRaisesRegex(ValueError, "editorial 2: the quote is not in v1"):
            self.save_eval(1, self.v1, editorial=REDUNDANT + editorial("Led a team of nine", "x", "y"))
        self.assertIsNone(db.cv_check(self.conn, self.app, 1), "nothing stored")

    def test_editorial_suggestion_keeps_the_job_keywords(self) -> None:
        write(self.v1, EDITED)
        db.save_tailored_cv(self.conn, self.app, str(self.v1), "v1")
        drops = editorial("LLM APIs, Acme Model API, Globex Model API", "a general term next to its own examples",
                          "Acme Model API, Globex Model API")
        with self.assertRaisesRegex(ValueError, "keep the job's exact keywords: editorial 1 drops LLM APIs"):
            self.save_eval(1, self.v1, editorial=drops)
        self.assertIsNone(db.cv_check(self.conn, self.app, 1))

    def test_master_fit_is_compared(self) -> None:
        db.save_tailored_cv(self.conn, self.app, str(self.v1), "v1")
        db.save_cv_fit(self.conn, self.app, 4, [], "fit.md")
        found = self.save_eval(1, self.v1, fit=3)
        self.assertEqual((found["verdict"], found["summary"]["master_fit"]), ("look", 4))
        self.assertEqual(cveval.summary_line(found),
                         "fit 3/5 (master CV 4/5) · skim 4/5 · 0 claims, 0 unsourced · 0/0 must-haves")


class CliTest(IsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.private = self.tmp / "private"
        write(self.private / "cv.md", MASTER)
        with closing(db.connect(self.prep_db)) as conn:
            self.app, _ = db.record_application(conn, "Globex Labs", "Engineer", status="to_apply")
        self.run_cli("tailor", "path", str(self.app), "--role-short", "Eng")
        self.file = write(research.tailored_file(self.private, "Globex Labs", "Eng", db.today_iso(), 1), CLEAN)

    def save(self) -> str:
        with mock.patch("prep.cvpdf.write_pdf", return_value=(self.file.with_suffix(".pdf"), 1)), \
                mock.patch("sys.stdin", io.StringIO("Secret tailoring note")):
            out = self.run_cli("tailor", "save", str(self.app))[1]
        with closing(db.connect(self.prep_db)) as conn:
            db.queue_request(conn, self.app, "cv_eval")  # the panel's AI check button
        return out

    def test_asked_check_gets_a_separate_packet(self) -> None:
        self.assertIn("no AI check yet: the panel's AI check button asks for one", self.save())
        out = self.run_cli("research", "next", str(self.app))[1]
        self.assertIn("AI check of the tailored CV (a separate pass", out)
        self.assertIn(f"version: 1 ({self.file})", out)
        self.assertIn(f"file: {cveval.eval_file(self.file)}", out)
        self.assertIn(f"facts bank: {self.private / 'cv-facts.md'} (missing)", out)
        self.assertIn("master CV fit: none yet", out)
        self.assertIn(f"then: python -m prep.cli tailor eval {self.app}", out)
        self.assertNotIn("Secret tailoring note", out, "the writer's note is not part of the check")

    def test_not_claimed_while_a_new_version_is_being_written(self) -> None:
        self.save()
        with closing(db.connect(self.prep_db)) as conn:
            db.queue_request(conn, self.app, "tailor", force=True)
        out = self.run_cli("research", "next", str(self.app))[1]
        self.assertIn("Tailored CV (a copy", out)
        self.assertNotIn("AI check of the tailored CV", out)
        with closing(db.connect(self.prep_db)) as conn:
            self.assertEqual([r["claimed"] for r in db.open_requests(conn, self.app) if r["kind"] == "cv_eval"], [False])

    def test_tailor_eval_stores_and_prints(self) -> None:
        self.save()
        write(cveval.eval_file(self.file), eval_text(claims=UNSOURCED))
        with self.assertLogs("prep", level="INFO") as logs:
            code, out, _ = self.run_cli("tailor", "eval", str(self.app))
        self.assertEqual(code, 0)
        self.assertIn("Check: Blocked (AI check: fit 4/5 · skim 4/5 · 2 claims, 2 unsourced · 0/0 must-haves)", out)
        self.assertIn("[block] No source in cv.md, the facts bank or the agreed wording: no team size in cv.md: "
                      "- Ran Kubernetes clusters.", out)
        self.assertNotIn("team", "\n".join(logs.output))
        code, _, err = self.run_cli("tailor", "eval", str(self.app), "--file", str(self.tmp / "missing.md"))
        self.assertEqual((code, "no file at" in err), (2, True))

    def test_a_check_waiting_for_a_new_version_never_holds_up_other_work(self) -> None:
        self.save()
        with closing(db.connect(self.prep_db)) as conn:
            for kind in db.RESEARCH_KINDS:  # queued on entering Interested
                db.finish_requests(conn, [self.app], kind)
            tailor, _ = db.queue_request(conn, self.app, "tailor", force=True)
            db.claim_request(conn, tailor)  # another session is writing the next version
            other, _ = db.record_application(conn, "Initech Example", "Engineer", status="to_apply")
            db.queue_request(conn, other, "research")
        out = self.run_cli("research", "next")[1]
        self.assertIn(f"Application #{other}: Initech Example", out)
        self.assertIn("being done by another session", self.run_cli("research", "next")[1])

    def test_tailor_packet_names_the_facts_bank_for_the_header(self) -> None:
        facts = write(self.private / "cv-facts.md", "## Contact\n- Email: alex@example.org\n")
        with closing(db.connect(self.prep_db)) as conn:
            db.queue_request(conn, self.app, "tailor")
        out = self.run_cli("research", "next", str(self.app))[1]
        self.assertIn(f"facts bank: {facts} (exists: header from its ## Contact section", out)

    def test_facts_file_setting_is_used(self) -> None:
        facts = write(self.tmp / "elsewhere" / "facts.md", "Led a team of nine in 2015.")
        with mock.patch.dict(os.environ, {"PREP_FACTS_FILE": str(facts)}):
            self.save()
            out = self.run_cli("research", "next", str(self.app))[1]
        self.assertIn(f"facts bank: {facts} (exists)", out)


class PanelTest(WebTestCase):
    MDE = 8  # the fixtures' to_apply application (Massive Dynamic Example)

    def panel(self) -> str:
        body = self.get(f"/?app={self.MDE}")[2]
        start = body.index('<aside class="panel"')
        return body[start:body.index("</aside>", start)]

    def test_card_shows_pending_then_scores(self) -> None:
        path = write(research.tailored_file(self.tmp / "private", "Massive Dynamic Example", "AISE", TODAY, 1), CLEAN)
        with self.conn() as conn:
            db.save_tailored_cv(conn, self.MDE, str(path), "v1")
            db.save_cv_check(conn, self.MDE, 1, "rules", [])
        self.assertIn(">AI check</button>", self.panel())
        self.post("/applications/research", {"app_id": self.MDE, "kind": "cv_eval"})
        panel = self.panel()
        self.assertIn('<span class="badge badge-success">Ready</span><span class="badge badge-neutral">AI check pending</span>',
                      panel)
        with self.conn() as conn:
            cveval.save(conn, self.MDE, 1, write(cveval.eval_file(path), eval_text(
                skim=2, skim_why="Opens with <old> roles", claims=UNSOURCED)))
        panel = self.panel()
        self.assertNotIn("AI check pending", panel)
        self.assertIn('<span class="badge badge-danger">Blocked</span>', panel)
        self.assertIn("AI check: fit 4/5 · skim 2/5 · 2 claims, 2 unsourced", panel)
        self.assertIn("Skim: Opens with &lt;old&gt; roles", panel)
        self.assertIn('<li class="finding-block">No source in cv.md', panel)

    def test_card_shows_editorial_findings_as_look(self) -> None:
        path = write(research.tailored_file(self.tmp / "private", "Massive Dynamic Example", "AISE", TODAY, 1), EDITED)
        with self.conn() as conn:
            db.save_tailored_cv(conn, self.MDE, str(path), "v1")
            db.save_cv_check(conn, self.MDE, 1, "rules", [])
            cveval.save(conn, self.MDE, 1, write(cveval.eval_file(path), eval_text(editorial=REDUNDANT)))
        panel = self.panel()
        self.assertIn('<span class="badge badge-warning">Look at these lines</span>', panel)
        self.assertIn("· 1 editorial", panel)
        self.assertIn('<li class="finding-look">Editorial: a general term next to its own examples; '
                      'suggest: LLM APIs (Acme Model API, Globex Model API)', panel)


class SettingsTest(IsolatedTestCase):
    def test_facts_path_default_and_override(self) -> None:
        settings = config.load_settings()
        self.assertEqual(config.facts_path(settings), self.tmp / "private" / "cv-facts.md")
        with mock.patch.dict(os.environ, {"PREP_FACTS_FILE": "relative/facts.md"}), \
                self.assertRaisesRegex(config.ConfigError, "facts_file must be an absolute path"):
            config.load_settings()

    def test_the_example_template_is_committed_and_impersonal(self) -> None:
        example = config.REPO_ROOT / "cv-facts.example.md"
        self.assertTrue(example.is_file())
        self.assertFalse(privacy.is_forbidden("cv-facts.example.md"))
        self.assertNotRegex(example.read_text(encoding="utf-8"), r"\b(?:you|your|I|my)\b")
