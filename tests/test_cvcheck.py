"""Tailored CV check: the rule checks, the verdict, the changes vs cv.md, storage (with room for other
checkers' findings), the tailor save hook and the panel card. Made-up CVs and companies only; no network."""

import io
from contextlib import closing
from pathlib import Path
from unittest import mock

from prep import cvcheck, cvpdf, db, privacy, research
from tests.helpers import IsolatedTestCase
from tests.test_research import write
from tests.test_web import TODAY, WebTestCase

MASTER = """# Alex Example
alex@example.org · +44 7700 900123 · https://www.linkedin.com/in/alex-example/

## Summary
Platform engineer building pipelines on public cloud.

## Experience
**Senior Platform Engineer — Brightwater Widgets**, 2019–2024
- Moved 40 services to Terraform.
- Built a shared deployment pipeline.
- Ran the on-call rotation.

**DevOps Engineer — Quillfeather Systems**, 2016–2019
- Ran Kubernetes clusters.

## Skills
- Terraform, Kubernetes, Python
"""
FRONT = ('+++\njob = "Engineer at Globex Labs"\ntailored_at = 2026-01-20\nbased_on = "cv.md"\n'
         'keywords = ["Terraform", "kubernetes", "SOC 2"]\ngaps = ["SOC 2"]\n+++\n\n')
CLEAN = FRONT + MASTER.replace("Platform engineer building pipelines on public cloud.",
                               "Platform engineer who builds delivery pipelines for AI teams.")
OFFSET = FRONT.count("\n")  # the body starts on this file line (a blank line)


def run_check(text: str, master: str | None = MASTER, pages: int | None = 1, others: list[str] = (),
              facts: str | None = None) -> list[dict]:
    return cvcheck.check(text, "Globex Labs Ltd", list(others), master, facts, pages, 2026,
                         "Senior Platform Engineer, Delivery")


def checks(findings: list[dict]) -> list[tuple[str, str]]:
    return [(f["check"], f["severity"]) for f in findings]


class RulesTest(IsolatedTestCase):
    def test_a_clean_copy_is_ready(self) -> None:
        self.assertEqual(run_check(CLEAN, others=["Initech"]), [])
        self.assertEqual(db.check_verdict([]), "ready")

    def test_pages(self) -> None:
        self.assertEqual(checks(run_check(CLEAN, pages=3)), [("pages", "block")])
        self.assertEqual(checks(run_check(CLEAN, pages=None)), [("pages", "look")], "no PDF is never a pass")
        self.assertEqual(run_check(CLEAN, pages=2), [])

    def test_contacts(self) -> None:
        dropped = CLEAN.replace(" · +44 7700 900123", "")
        [f] = run_check(dropped)
        self.assertEqual((f["check"], f["severity"]), ("contacts", "block"))
        self.assertIn("phone", f["message"])
        bare = CLEAN.replace("alex@example.org · +44 7700 900123 · https://www.linkedin.com/in/alex-example/", "")
        found = run_check(bare, master=None)
        self.assertEqual(checks(found), [("contacts", "block"), ("contacts", "look")])
        self.assertEqual(found[0]["message"], "No email address")
        # the link written another way is the same link
        self.assertEqual(run_check(CLEAN.replace("https://www.linkedin.com/in/alex-example/",
                                                 "[LinkedIn](linkedin.com/in/alex-example)")), [])

    def test_contacts_from_the_facts_bank(self) -> None:
        facts = ("# Facts\n\n## Contact\n- Name: Alex Example\n- Email: alex.new@example.org\n- Phone:\n"
                 "- GitHub: https://github.com/alex-example\n\n## Tools\n- Email: not@example.org\n")
        self.assertEqual(cvcheck.contact_section(facts),
                         "Alex Example\nalex.new@example.org\nhttps://github.com/alex-example")
        moved = CLEAN.replace("alex@example.org", "alex.new@example.org · github.com/alex-example")
        self.assertEqual(run_check(moved, facts=facts), [], "the facts bank's email replaces cv.md's; the empty "
                         "phone falls back to cv.md's, which is kept")
        found = run_check(CLEAN.replace("alex@example.org", "alex@example.org · github.com/alex-example"), facts=facts)
        self.assertEqual([(f["severity"], f["message"]) for f in found],
                         [("block", "The email in the facts bank is missing: alex.new@example.org")],
                         "cv.md's old email does not stand in for the facts bank's")
        no_email = MASTER.replace("alex@example.org · ", "")
        self.assertEqual(run_check(moved, master=no_email, facts=facts), [], "cv.md without an email is fine")
        self.assertEqual(run_check(CLEAN.replace("https://www.linkedin.com/in/alex-example/",
                                                 "[LinkedIn](linkedin.com/in/alex-example)")), [])

    def test_placeholders_but_not_markdown_links(self) -> None:
        text = CLEAN.replace("- Ran the on-call rotation.", "- Cut costs by [X]%.\n- TODO: add a metric\n- See <company>")
        found = run_check(text)
        self.assertEqual(checks(found), [("placeholder", "block")] * 3)
        line = text.splitlines().index("- Cut costs by [X]%.") + 1
        self.assertEqual((found[0]["line"], found[0]["text"]), (line, "- Cut costs by [X]%."))
        self.assertIn(f"Line {line}", found[0]["message"])
        self.assertEqual(run_check(CLEAN.replace("Python\n", "Python, [my site](https://example.org)\n")), [])
        [f] = run_check(CLEAN.replace("Python\n", "Python, <https://example.org/alex>\n"))
        self.assertEqual((f["check"], f["severity"]), ("placeholder", "look"), "an autolink prints its brackets")

    def test_pay_blocks_and_is_never_quoted(self) -> None:
        text = CLEAN.replace("- Ran the on-call rotation.", "- Looking for £123,456 base salary")
        [f] = run_check(text)
        self.assertEqual((f["check"], f["severity"], f["text"]), ("pay", "block", ""))
        self.assertNotIn("90", f["message"])
        own = MASTER.replace("- Ran the on-call rotation.", "- Saved $2M in cloud spend")
        self.assertEqual(run_check(FRONT + own, master=own), [], "the user's own line from cv.md")
        for line in ("- Day rate: 650", "- Expected compensation around 123k", "- £550 per annum"):
            with self.subTest(line=line):
                [f] = run_check(CLEAN.replace("- Ran the on-call rotation.", line))
                self.assertEqual((f["severity"], f["text"]), ("block", ""))

    def test_figures_from_cv_md_or_the_facts_bank_pass_on_reworded_lines(self) -> None:
        own = MASTER.replace("- Ran the on-call rotation.", "- Cut cloud spend by $2M across 10k users.")
        reworded = FRONT + own.replace("- Cut cloud spend by $2M across 10k users.",
                                       "- Saved $2M a year in cloud spend for 10k users.")
        self.assertEqual(run_check(reworded, master=own), [])
        new = reworded.replace("10k users", "12k users")
        [f] = run_check(new, master=own)
        self.assertEqual((f["check"], f["severity"], f["text"]), ("pay", "look", ""))
        self.assertNotIn("12k", f["message"])
        self.assertEqual(run_check(new, master=own, facts="The platform served 12k users."), [], "the facts bank counts")
        paid = CLEAN.replace("- Ran the on-call rotation.", "- Paid on-call rotation.")
        [f] = run_check(paid)
        line = paid.splitlines().index("- Paid on-call rotation.") + 1
        self.assertEqual((f["severity"], f["message"]), ("look", f"Line {line} uses a pay word; check it is not about pay"))

    def test_dates(self) -> None:
        text = CLEAN.replace("2019–2024", "2024–2019")
        [f] = run_check(text)
        self.assertEqual((f["check"], f["severity"]), ("dates", "look"))
        self.assertIn("2024 is after 2019", f["message"])
        [f] = run_check(CLEAN.replace("2016–2019", "2015–2019"))
        self.assertIn("2015 is not in cv.md", f["message"])
        self.assertEqual(run_check(CLEAN.replace("2016–2019", "2015–2019"), facts="Started in 2015."), [],
                         "the facts bank counts")
        [f] = run_check(CLEAN.replace("2019–2024", "2019–2031"), master=None)
        self.assertIn("2031 is in the future", next(x for x in [f] if x["check"] == "dates")["message"])

    def test_another_applications_company(self) -> None:
        text = CLEAN.replace("for AI teams.", "for AI teams at Initech.")
        [f] = run_check(text, others=["Initech Ltd", "Brightwater Widgets", "Globex", "AB"])
        self.assertEqual((f["check"], f["severity"]), ("company", "block"))
        self.assertIn("Initech", f["message"])
        self.assertEqual(run_check(CLEAN, others=["Brightwater Widgets", "Globex"]), [],
                         "a past employer in cv.md and this company's own name are fine")
        self.assertEqual(run_check(text, others=["Initech"], facts="Contracted for Initech in 2020."), [],
                         "a name from the facts bank is fine")

    def test_job_keywords_in_the_jobs_wording(self) -> None:
        [f] = run_check(CLEAN.replace('"kubernetes"', '"K8s", "CI/CD"'))
        self.assertEqual((f["check"], f["severity"]), ("keywords", "look"))
        self.assertIn("not in the CV in the job's wording: K8s, CI/CD (", f["message"])
        self.assertNotIn("SOC 2", f["message"], "a declared gap is not asked for")
        [f] = run_check(CLEAN.replace('keywords = ["Terraform", "kubernetes", "SOC 2"]\n', ""))
        self.assertIn("No keywords", f["message"])
        [f] = run_check(CLEAN.replace('gaps = ["SOC 2"]', 'gaps = "SOC 2"'))
        self.assertIn("must be lists of strings", f["message"])
        self.assertIn("Not claimed (gaps): SOC 2", cvcheck.changes(MASTER, CLEAN))

    def test_job_title_near_the_top(self) -> None:
        self.assertEqual(cvcheck.title_core("Senior Platform Engineer, Delivery"), "Platform Engineer")
        self.assertEqual(cvcheck.title_core("Sr. AI Engineer (Applied) - London"), "AI Engineer")
        self.assertEqual(cvcheck.title_core("Forward Deployed Engineer at Globex"), "Forward Deployed Engineer")
        found = cvcheck.check(CLEAN, "Globex Labs", [], MASTER, None, 1, 2026, "Site Reliability Engineer")
        self.assertEqual(checks(found), [("title", "look")])
        self.assertIn("(Site Reliability Engineer)", found[0]["message"])

    def test_style_signs_only_in_written_lines(self) -> None:
        text = CLEAN.replace("Platform engineer who builds delivery pipelines for AI teams.",
                             "Passionate, results-driven platform engineer who spearheaded robust platforms.")
        [f] = run_check(text)
        self.assertEqual((f["check"], f["severity"]), ("style", "look"))
        self.assertIn("stock words (passionate, results-driven, spearheaded, robust)", f["message"])
        own = MASTER.replace("Platform engineer building pipelines on public cloud.", "A robust platform engineer.")
        self.assertEqual(run_check(FRONT + own, master=own), [], "the user's own words are never flagged")
        dashes = CLEAN.replace("- Moved 40 services to Terraform.", "- Moved 40 services to Terraform — fast.").replace(
            "- Built a shared deployment pipeline.", "- Built a pipeline – shared by 12 teams.")
        [f] = run_check(dashes)
        self.assertRegex(f["message"], r"^Dashes in 2 written lines")
        self.assertEqual(run_check(CLEAN.replace("- Ran Kubernetes clusters.", "- Ran Kubernetes — at scale.")), [],
                         "one dash and dashes in bold role lines are fine")
        bullets = CLEAN.replace("- Moved 40 services to Terraform.\n- Built a shared deployment pipeline.\n"
                                "- Ran the on-call rotation.",
                                "- Led a move to Terraform.\n- Led a shared pipeline.\n- Led on-call.\n- Led hiring.")
        found = run_check(bullets)
        self.assertEqual([f["message"].split(" (")[0] for f in found],
                         ['4 bullets start with "led"', "None of the 4 rewritten bullets has a number; ask for honest figures"])

    def test_body_line_numbers_count_the_front_matter(self) -> None:
        lines = cvcheck.body_lines(CLEAN)
        self.assertEqual(lines[:2], [(OFFSET, ""), (OFFSET + 1, "# Alex Example")])
        self.assertEqual(cvcheck.body_lines("# A\n")[0], (1, "# A"))


class ChangesTest(IsolatedTestCase):
    def test_changes_vs_master(self) -> None:
        text = FRONT + MASTER.replace("- Ran the on-call rotation.\n", "").replace(
            "## Skills\n- Terraform, Kubernetes, Python\n", "").replace(
            "## Summary\nPlatform engineer building pipelines on public cloud.\n",
            "## Summary\nPlatform engineer for AI teams.\n").replace(
            "## Experience", "## Skills\n- Python, Terraform\n\n## Experience")
        found = cvcheck.changes(MASTER, text)
        self.assertIn("Sections reordered: Summary, Skills, Experience", found)
        self.assertIn("Summary: 1 line reworded", found)
        self.assertIn("Experience: 1 line cut", found)
        self.assertIn("Skills: 1 line reworded", found)
        self.assertRegex(found[-2], r"^\d+ words \(cv\.md: \d+\)$")
        self.assertEqual(cvcheck.changes(MASTER, FRONT + MASTER)[0], "Same text as cv.md")
        self.assertIn("Removed section: Skills", cvcheck.changes(MASTER, FRONT + MASTER.split("## Skills")[0]))
        self.assertTrue(cvcheck.changes(None, CLEAN)[0].startswith("No cv.md"))


class StorageTest(IsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.conn = db.connect(self.prep_db)
        self.addCleanup(self.conn.close)
        self.app, _ = db.record_application(self.conn, "Globex Labs", "Engineer", status="to_apply")
        path = write(research.tailored_file(self.tmp / "private", "Globex Labs", "Eng", TODAY, 1), CLEAN)
        db.save_tailored_cv(self.conn, self.app, str(path), "v1")

    def test_other_sources_keep_their_findings_and_share_the_verdict(self) -> None:
        self.assertIsNone(db.cv_check(self.conn, self.app, 1), "not checked yet")
        look = {"check": "pages", "severity": "look", "line": None, "message": "No PDF"}
        self.assertEqual(db.save_cv_check(self.conn, self.app, 1, "rules", [look], ["Summary: 1 line reworded"]), "look")
        claim = {"check": "claim", "severity": "block", "line": 9, "message": "Not in cv.md or the facts bank"}
        self.assertEqual(db.save_cv_check(self.conn, self.app, 1, "ai", [claim]), "blocked")
        stored = db.cv_check(self.conn, self.app, 1)
        self.assertEqual([f["source"] for f in stored["findings"]], ["rules", "ai"])
        self.assertEqual(stored["changes"], ["Summary: 1 line reworded"], "kept when not given")
        self.assertEqual(db.save_cv_check(self.conn, self.app, 1, "rules", []), "blocked", "the AI finding stays")
        self.assertEqual(db.save_cv_check(self.conn, self.app, 1, "ai", []), "ready")
        with self.assertRaises(ValueError):
            db.save_cv_check(self.conn, self.app, 1, "rules", [dict(look, severity="fatal")])
        with self.assertRaises(LookupError):
            db.save_cv_check(self.conn, self.app, 2, "rules", [])

    def test_run_reads_cv_md_and_other_applications(self) -> None:
        db.record_application(self.conn, "Initech", "Engineer", status="to_apply")
        path = write(research.tailored_file(self.tmp / "private", "Globex Labs", "Eng", TODAY, 2),
                     CLEAN.replace("for AI teams.", "for AI teams at Initech."))
        db.save_tailored_cv(self.conn, self.app, str(path), "v2")
        write(self.tmp / "private" / "cv.md", MASTER)
        found = cvcheck.run(self.conn, self.tmp / "private", self.app, 2, pages=1)
        self.assertEqual((found["verdict"], checks(found["findings"])), ("blocked", [("company", "block")]))
        self.assertIn("Summary: 1 line reworded", found["changes"])

    def test_the_facts_bank_is_private(self) -> None:
        self.assertTrue(privacy.is_forbidden("cv-facts.md"))
        self.assertEqual(cvcheck.facts_file(Path("/p")), Path("/p/cv-facts.md"))


class CliTest(IsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.private = self.tmp / "private"
        write(self.private / "cv.md", MASTER)
        with closing(db.connect(self.prep_db)) as conn:
            self.app, _ = db.record_application(conn, "Globex Labs", "Engineer", status="to_apply")
        self.run_cli("tailor", "path", str(self.app), "--role-short", "Eng")
        self.file = research.tailored_file(self.private, "Globex Labs", "Eng", db.today_iso(), 1)

    def save(self, pages: int | None) -> str:
        pdf = mock.patch("prep.cvpdf.write_pdf", return_value=(self.file.with_suffix(".pdf"), pages)) if pages else \
            mock.patch("prep.cvpdf.write_pdf", side_effect=cvpdf.PdfUnavailable("no WeasyPrint"))
        with pdf, mock.patch("sys.stdin", io.StringIO("note")):
            return self.run_cli("tailor", "save", str(self.app))[1]

    def test_save_stores_and_prints_the_check(self) -> None:
        write(self.file, CLEAN)
        out = self.save(pages=3)
        self.assertIn(f"  Check: Blocked{PENDING.format(app=self.app)}\n    [block] The PDF has 3 pages; keep it to 2\n", out)
        self.assertIn("    - Summary: 1 line reworded\n", out)
        with closing(db.connect(self.prep_db)) as conn:
            self.assertEqual(db.cv_check(conn, self.app, 1)["verdict"], "blocked")
        with mock.patch("prep.cvpdf.write_pdf", return_value=(self.file.with_suffix(".pdf"), 2)):
            out = self.run_cli("tailor", "pdf", str(self.app))[1]
        self.assertIn(f"  Check: Ready{PENDING.format(app=self.app)}\n", out, "tailor pdf checks again with the new page count")

    def test_a_failed_check_never_undoes_the_save_and_nothing_is_logged(self) -> None:
        write(self.file, CLEAN.replace("- Ran the on-call rotation.", "- Expected salary £123,456"))
        with self.assertLogs("prep", level="INFO") as logs:
            out = self.save(pages=1)
        self.assertIn("Check: Blocked", out)
        self.assertNotIn("123,456", out + "\n".join(logs.output))
        self.assertNotIn("Alex Example", "\n".join(logs.output))
        with mock.patch("prep.cvcheck.run", side_effect=OSError("disk")):
            write(research.tailored_file(self.private, "Globex Labs", "Eng", db.today_iso(), 2), CLEAN)
            out = self.save(pages=1)
        self.assertIn("Saved tailored CV v2", out)
        self.assertIn("no check: OSError", out)


PENDING = " (AI check pending: run python -m prep.cli research next {app} in a separate pass)"


class CheckCommandTest(CliTest):
    def test_check_again_without_weasyprint(self) -> None:
        write(self.file, CLEAN)
        self.save(pages=None)
        with closing(db.connect(self.prep_db)) as conn:
            self.assertEqual(db.cv_check(conn, self.app, 1)["verdict"], "look", "no PDF yet")
        with mock.patch("prep.cvpdf.write_pdf", side_effect=AssertionError("never renders")):
            out = self.run_cli("tailor", "check", str(self.app))[1]
            self.assertIn("v1 (" + self.file.name + f") for #{self.app}: no PDF\n  Check: Look at these lines (AI check pending", out)
            with mock.patch("prep.cvpdf.page_count", return_value=2):
                out = self.run_cli("tailor", "check", str(self.app), "--version", "1")[1]
        self.assertIn(f"PDF 2 pages\n  Check: Ready{PENDING.format(app=self.app)}\n", out)
        code, _, err = self.run_cli("tailor", "check", str(self.app), "--version", "5")
        self.assertEqual((code, "no tailored CV v5" in err), (2, True))

    def test_page_count_reads_compressed_object_streams(self) -> None:
        import zlib
        pages = b"<< /Type /Page /Parent 2 0 R >> << /Type /Page >>"
        pdf = self.tmp / "made-up.pdf"
        pdf.write_bytes(b"%PDF-1.7\n1 0 obj << /Type /Pages /Count 3 >> endobj\n2 0 obj << /Type /Page >> endobj\n"
                        b"3 0 obj << /Type /ObjStm /Filter /FlateDecode >>\nstream\n" + zlib.compress(pages)
                        + b"\nendstream\nendobj\n")
        self.assertEqual(cvpdf.page_count(pdf), 3)
        self.assertIsNone(cvpdf.page_count(self.tmp / "missing.pdf"))


class PanelTest(WebTestCase):
    MDE = 8  # the fixtures' to_apply application (Massive Dynamic Example)

    def panel(self) -> str:
        body = self.get(f"/?app={self.MDE}")[2]
        start = body.index('<aside class="panel"')
        return body[start:body.index("</aside>", start)]

    def test_card_shows_the_verdict_flagged_lines_and_changes(self) -> None:
        path = write(research.tailored_file(self.tmp / "private", "Massive Dynamic Example", "AISE", TODAY, 1), CLEAN)
        with self.conn() as conn:
            db.save_tailored_cv(conn, self.MDE, str(path), "v1")
        self.assertIn("v1 is not checked yet", self.panel())
        with self.conn() as conn:
            db.save_cv_check(conn, self.MDE, 1, "rules", [
                {"check": "placeholder", "severity": "block", "line": 12, "message": "Line 12 has a placeholder",
                 "text": "- Cut costs by <X>%"},
                {"check": "pages", "severity": "look", "line": None, "message": "No PDF yet"}],
                ["Summary: 1 line reworded"])
        panel = self.panel()
        self.assertIn('<span class="badge badge-danger">Blocked</span>', panel)
        self.assertIn('<li class="finding-block">Line 12 has a placeholder<br>'
                      '<span class="mono muted">- Cut costs by &lt;X&gt;%</span></li>', panel)
        self.assertIn("<li>Summary: 1 line reworded</li>", panel)
        with self.conn() as conn:
            db.save_cv_check(conn, self.MDE, 1, "rules", [])
        self.assertIn('<span class="badge badge-success">Ready</span>', self.panel())
