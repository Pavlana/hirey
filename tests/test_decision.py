"""The job panel as a decision page: red flags, recruiter-call questions, the recommendation from the CV fit, the
CV fit of a job offer before it is tracked (and its hand-over on Interested), and research queued on opening.
Fictional data only; dev3 is never called."""

from contextlib import closing
from pathlib import Path
from unittest import mock

from prep import assistant, db, decision, research
from tests.test_web import WONKA, WebTestCase, job_url
from tests.helpers import IsolatedTestCase

FIT = """+++
score = 3
gaps = ["Multi-agent systems in production", "ERP integration"]
checked_at = 2026-01-19
+++

## Fit summary
Fictional summary.

## Recommendation
Verdict: Stretch: apply only if cheap — two real gaps, but it builds agents and benchmarks them.

### Gaps that matter less
- ERP integration: learnable on the job
- Product company preference: a preference, not a requirement

### Gaps that are real
- Multi-agent systems in production: likely probed in the technical round

### In its favour (by the criteria)
- Builds agents
- Presentation interview, no puzzle rounds

### What would decide it
- Whether the interview probes <model internals>

### If you apply
- One tailored version; lead with the evals work; do not claim multi-agent production
"""


class RecommendationParseTest(IsolatedTestCase):
    def test_parts_and_verdict(self) -> None:
        rec = research.read_recommendation(FIT.split("+++", 2)[2])
        self.assertEqual(rec["verdict"], "stretch")
        self.assertEqual(rec["why"], "two real gaps, but it builds agents and benchmarks them.")
        self.assertEqual(len(rec["soft"]), 2)
        self.assertEqual(rec["real"], ["Multi-agent systems in production: likely probed in the technical round"])
        self.assertEqual(rec["favour"][1], "Presentation interview, no puzzle rounds")
        self.assertEqual(len(rec["decide"]), 1)
        self.assertTrue(rec["effort"][0].startswith("One tailored version"))

    def test_none_without_a_section_and_a_verdict_is_required(self) -> None:
        self.assertIsNone(research.read_recommendation("## Fit summary\nx"))
        for line in ("Verdict: maybe", "No verdict line"):
            with self.subTest(line=line), self.assertRaises(ValueError):
                research.read_recommendation(f"## Recommendation\n{line}\n### If you apply\n- x")
        self.assertEqual(research.read_recommendation("## Recommendation\n**Verdict:** **Apply**. Strong fit.")["why"],
                         "Strong fit.")

    def test_pay_is_refused(self) -> None:
        with closing(db.connect(self.prep_db)) as conn, self.assertRaisesRegex(ValueError, "pay"):
            db.save_job_fit(conn, "x:y:1", 3, [], "f.md", "2026-01-19",
                            {"verdict": "apply", "why": "Salary is fine", "soft": []})


class FlagsAndQuestionsTest(IsolatedTestCase):
    def test_team_is_a_flag_never_a_gate(self) -> None:
        gates = {"level": "yes", "location": "yes", "interviews": "unknown", "pay": "unknown", "team": "no"}
        self.assertEqual(decision.red_flags(None, None, gates, True, False), ["Team of one (or very small)"])
        self.assertEqual(decision.red_flags(None, None, {**gates, "team": "unknown"}, True, False), ["Team size unclear"])
        self.assertEqual(decision.red_flags(None, None, {**gates, "team": "unknown"}, False, False), [])  # not screened yet

    def test_flags_from_gates_research_brief_and_kind_without_duplicates(self) -> None:
        gates = {"level": "no", "team": "yes", "interviews": "no"}
        brief = "facts\n## Summary\nx\n## Red flags / notable\n- On-call every other week\n- Not a senior IC role (lead, staff, manager or junior)"
        flags = decision.red_flags({"red_flags": ["Unclear location"]}, brief, gates, True, True, "contract")
        self.assertEqual(flags, ["Not a senior IC role (lead, staff, manager or junior)",
                                 "LeetCode-style rounds or a timed coding screen reported",
                                 "Client delivery (consultancy or agency)", "Contract role", "Unclear location",
                                 "On-call every other week"])  # the brief's copy of the level flag is dropped

    def test_call_questions_are_few_plain_and_end_with_pay_for_the_call(self) -> None:
        qs = decision.call_questions({"what_it_does": "Makes widgets.", "call_questions": ["Who uses the agents?", "A", "B"]},
                                     {"team": "unknown", "location": "yes", "level": "yes"})
        texts = [q["text"] for q in qs]
        self.assertEqual(texts[:3], [decision.TEMPLATE[0][1], "How many people work in the AI team?", "Who uses the agents?"])
        self.assertLessEqual(len(qs), decision.MAX_QUESTIONS)
        self.assertEqual(qs[-1], {"text": decision.PAY_QUESTION, "call_only": True})
        self.assertFalse(any(q["call_only"] for q in qs[:-1]))
        self.assertNotIn("What is the product, and who uses it?", texts)  # the research says what it does

    def test_employer_kind(self) -> None:
        self.assertEqual(decision.employer_kind(None, {"kind": "consultancy"}, None), "client")
        self.assertEqual(decision.employer_kind({"employer_kind": "product"}, {"kind": "consultancy"}, None), "product")
        self.assertEqual(decision.employer_kind(None, None, "client"), "client")
        self.assertIsNone(decision.employer_kind(None, {"kind": "unknown"}, None))


class OfferPanelTest(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.private = Path(self.settings.private_dir)
        patcher = mock.patch.object(assistant, "notify_briefs_in_background")
        self.notify = patcher.start()
        self.addCleanup(patcher.stop)

    def panel(self, url: str) -> str:
        body = self.get(url)[2]
        start = body.index('<aside class="panel')
        return body[start:body.index("</aside>", start)]

    def save_fit(self) -> Path:
        path = research.job_fit_file(self.private, WONKA, db.company_slug("Wonka Systems"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(FIT, encoding="utf-8")
        code, out, err = self.run_cli("fit", "save", "--job", WONKA)
        self.assertEqual(code, 0, err)
        self.assertIn("recommendation: Stretch: apply only if cheap", out)
        return path

    def test_opening_an_offer_asks_for_the_fit_and_shows_the_recommendation(self) -> None:
        panel = self.panel(job_url(WONKA))
        self.assertIn("Recommendation: written with the CV fit…", panel)
        self.assertIn("Researching…", panel)
        packet = self.run_cli("brief", "next")[1]
        self.assertIn("CV fit of this job offer", packet)
        self.assertIn(f"fit save --job {WONKA}", packet)
        self.assertIn("company research", packet)
        self.save_fit()
        with self.conn() as conn:
            self.assertEqual(db.find_by_job(conn, WONKA), None)  # nothing tracked, nothing changed
            self.assertEqual(db.job_fit(conn, WONKA)["score"], 3)
        panel = self.panel(job_url(WONKA))
        rec = panel[panel.index('class="decide decide-rec"'):panel.index('class="decide decide-company"')]
        self.assertIn("Stretch: apply only if cheap", rec)
        self.assertIn("CV fit 3/5", rec)
        self.assertIn("Gaps that are real", rec)
        self.assertIn("Whether the interview probes &lt;model internals&gt;", rec)  # escaped

    def test_interested_takes_the_fit_over_with_no_second_run(self) -> None:
        self.panel(job_url(WONKA))
        self.save_fit()
        self.post("/offers/interested", {"job_id": WONKA})
        with self.conn() as conn:
            app = db.find_by_job(conn, WONKA)
            self.assertEqual(db.cv_fit(conn, app["id"])["recommendation"]["verdict"], "stretch")
            self.assertNotIn("cv_fit", {r["kind"] for r in db.open_requests(conn, app["id"])})
        self.assertIn("Stretch: apply only if cheap", self.panel(f"/?app={app['id']}"))

    def test_an_offer_briefed_before_job_fits_asks_for_its_fit_once(self) -> None:
        with self.conn() as conn:
            db.queue_brief(conn, WONKA)
            db.save_brief(conn, WONKA, "hybrid · Python\n## Summary\nx")
        self.panel(job_url(WONKA))
        with self.conn() as conn:
            self.assertEqual(len(db.open_briefs(conn, WONKA)), 1)
            for r in db.open_briefs(conn):  # the other offers' auto briefs are not this test's
                if r["getjo_job_id"] != WONKA:
                    db.cancel_brief(conn, r["getjo_job_id"])
        packet = self.run_cli("brief", "next")[1]
        self.assertIn("the brief is saved: write only the parts below", packet)
        self.save_fit()
        with self.conn() as conn:
            self.assertEqual(db.open_briefs(conn, WONKA), [])  # the fit-only pass closed it
            db.save_brief(conn, WONKA, "hybrid · Python\n## Summary\ny")
            conn.execute("DELETE FROM job_fits")
            conn.commit()
        self.panel(job_url(WONKA))
        with self.conn() as conn:
            self.assertEqual(db.open_briefs(conn, WONKA), [])  # asked once only

    def test_an_offer_shows_what_is_known_and_no_buttons_it_does_not_need(self) -> None:
        with self.conn() as conn:
            db.save_brief(conn, WONKA, "hybrid · Python\n## Summary\nx\n## Must-haves\n- Python services\n- LLM APIs")
        panel = self.panel(job_url(WONKA))
        company = panel[panel.index('class="decide decide-company"'):panel.index('class="decide decide-questions"')]
        self.assertIn("<h4>What the role needs</h4>", company)
        self.assertIn("<li>LLM APIs</li>", company)
        for gone in ("Write brief", "Redo brief", "Tasks", "Draft a reply", "data-copy", 'name="employer_kind"'):
            self.assertNotIn(gone, panel)
        self.assertIn(">Still unknown</h3>", panel)  # the questions, read only, until it is Interested
        self.assertIn("ask on the call", panel)
        self.assertIn(">Interested</button>", panel)

    def test_an_application_has_copy_but_pay_is_for_the_call(self) -> None:
        self.post("/offers/interested", {"job_id": WONKA})
        with self.conn() as conn:
            app_id = db.find_by_job(conn, WONKA)["id"]
        panel = self.panel(f"/?app={app_id}")
        questions = panel[panel.index('class="decide decide-questions"'):panel.index('class="decide decide-apply"')]
        self.assertIn("Copy all", questions)
        pay = questions[questions.index(decision.PAY_QUESTION):]
        self.assertIn("ask on the call", pay[:200])
        self.assertNotIn("data-copy", pay[:pay.index("</li>")])
        hidden = questions[questions.index('-all" hidden>'):]
        self.assertNotIn("pay", hidden[:hidden.index("</span>")].lower())  # Copy all leaves pay out


class ThreadShowTest(WebTestCase):
    def test_thread_show_prints_the_flags_and_questions(self) -> None:
        out = self.run_cli("thread", "show", "1")[1]
        self.assertIn("Questions to clarify on the recruiter call", out)
        self.assertIn(f"  - {decision.PAY_QUESTION} (on the call only)", out)
        self.assertIn("Red flags (the panel's section 1):", out)
