"""The plan-first tailoring flow: the tailoring plan (prep/tailorplan.py), the in-place CV editor with
undo and comments (prep/cvedit.py), the CLI and panel around them, and versions per sent CV.
cv.example.md and made-up companies only; no network, no WeasyPrint."""

import io
import json
from pathlib import Path
from unittest import mock

from prep import cvcheck, cvedit, cvfile, cvpdf, db, research, stats, tailorplan
from tests.helpers import IsolatedTestCase
from tests.test_research import write
from tests.test_web import HOST, WebTestCase

REPO = Path(__file__).resolve().parent.parent
CV = (REPO / "cv.example.md").read_text(encoding="utf-8")
FRONT = '+++\njob = "Engineer at Globex Labs"\ntailored_at = 2026-01-20\nbased_on = "cv.md"\n+++\n\n'
PLAN = """## What the role needs

- Must-haves: Python, Azure. Seniority: owns a platform used by many teams.

## Evidence

- Shared deployment pipeline used by 12 teams (cv.md): lead with it.

## Order

1. Summary: platform engineer, Python and Azure first.
2. Brightwater bullets: pipeline, Terraform.

## Questions

1. Lead with the pipeline or the Terraform migration?
   Answer:
2. Say "Senior" in the summary?
   Answer: Yes, as on the Brightwater role.
"""
NEW_PLAN = """## What the role needs

- ✓ Shipping Python services to production
- ~ Azure (one project, no certification)
- ✗ Kubernetes operators

## What will change in the CV

- Summary: platform engineer who owned a shared pipeline; Python and Azure first.
- Brightwater bullets first; the older support role cut to one line.
"""
JSON = {**HOST, "Accept": "application/json", "Origin": "http://localhost:8765", "Sec-Fetch-Site": "same-origin"}


class PlanTextTest(IsolatedTestCase):
    def test_questions_and_answers(self) -> None:
        qs = tailorplan.questions(PLAN)
        self.assertEqual([(q.number, q.answer) for q in qs], [(1, ""), (2, "Yes, as on the Brightwater role.")])
        self.assertEqual(tailorplan.questions("## Order\n1. not a question\n"), [])

    def test_set_answer_replaces_only_that_answer(self) -> None:
        text = tailorplan.set_answer(PLAN, 1, "  The pipeline:\n it shows scope. ")
        self.assertEqual([q.answer for q in tailorplan.questions(text)],
                         ["The pipeline: it shows scope.", "Yes, as on the Brightwater role."])
        again = tailorplan.set_answer(text, 2, "No.")
        self.assertEqual(tailorplan.questions(again)[1].answer, "No.")
        self.assertEqual(again.count("Answer:"), 2)
        with self.assertRaises(LookupError):
            tailorplan.set_answer(PLAN, 9, "x")
        with self.assertRaises(ValueError):
            tailorplan.set_answer(PLAN, 1, "  ")

    def test_check(self) -> None:
        self.assertTrue(tailorplan.check(NEW_PLAN).endswith("\n"))  # two sections, no questions
        self.assertEqual(tailorplan.questions(NEW_PLAN), [])
        with self.assertRaisesRegex(ValueError, "## What will change in the CV"):
            tailorplan.check(NEW_PLAN.split("## What will change")[0])
        self.assertTrue(tailorplan.check(PLAN).endswith("\n"))  # an older four-section plan still saves
        with self.assertRaisesRegex(ValueError, "## What will change in the CV"):
            tailorplan.check(PLAN.split("## Questions")[0])
        with self.assertRaisesRegex(ValueError, "pay"):
            tailorplan.check(PLAN + "\nSalary expectations: none\n")
        with self.assertRaisesRegex(ValueError, "empty"):
            tailorplan.check(" \n")

    def test_display_shows_answers_as_sub_bullets(self) -> None:
        shown = tailorplan.display(PLAN)
        self.assertIn("- 1. Summary: platform engineer", shown)
        self.assertIn("- 1. Lead with the pipeline or the Terraform migration?\n  - **Answer:** *not yet*", shown)
        self.assertIn("  - **Answer:** Yes, as on the Brightwater role.", shown)


class CommentsTest(IsolatedTestCase):
    def test_split_and_put_back(self) -> None:
        text = "# Name\n\n- Built a pipeline.\n%% make this less junior\n  %%   lead with scale \n- Other.\n%%\n"
        body, comments = cvedit.split_comments(text)
        self.assertNotIn("%%", body)
        self.assertEqual(comments, [("- Built a pipeline.", "make this less junior"),
                                    ("- Built a pipeline.", "lead with scale")])
        back = cvedit.with_comments(body.rstrip("\n"), [{"anchor": a, "body": b} for a, b in comments])
        self.assertEqual(back.splitlines()[2:5], ["- Built a pipeline.", "%% make this less junior", "%% lead with scale"])
        top = cvedit.with_comments("- A.", [{"anchor": "- Gone.", "body": "orphan"}, {"anchor": "", "body": "first"}])
        self.assertEqual(top.splitlines(), ["%% orphan", "%% first", "- A."])

    def test_never_in_the_pdf_or_the_checks(self) -> None:
        text = FRONT + "# Name\n%% a comment\n- Line.\n"
        self.assertNotIn("a comment", cvpdf.cv_body(text))
        self.assertNotIn("a comment", " ".join(line for _, line in cvcheck.body_lines(text)))


class EditorTest(IsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.conn = db.connect(self.prep_db)
        self.addCleanup(self.conn.close)
        self.private = self.tmp / "private"
        write(self.private / "cv.md", CV)
        self.app, _ = db.record_application(self.conn, "Globex Labs", "Engineer", status="to_apply")
        db.update_application(self.conn, self.app, role_short="Eng")
        self.v1 = write(research.tailored_file(self.private, "Globex Labs", "Eng", db.today_iso(), 1), FRONT + CV)
        db.save_tailored_cv(self.conn, self.app, str(self.v1), "v1")
        from prep.config import load_settings
        self.settings = load_settings()
        self.no_pdf = mock.patch("prep.cvpdf.write_pdf", side_effect=cvpdf.PdfUnavailable("no WeasyPrint"))
        self.no_pdf.start()
        self.addCleanup(self.no_pdf.stop)

    def edit(self, old: str, new: str) -> dict:
        current = cvedit.editor_text(self.conn, self.app)
        return cvedit.save_tailored(self.settings, self.conn, self.app, current["version"],
                                    current["text"].replace(old, new), current["stamp"])

    def test_save_in_place_keeps_the_text_before_and_undo_brings_it_back(self) -> None:
        result = self.edit("Built a shared deployment pipeline", "Owned the shared deployment pipeline")
        self.assertEqual((result["version"], result["new_version"], result["changed"]), (1, False, True))
        self.assertIn("Owned the shared", self.v1.read_text(encoding="utf-8"))
        self.assertTrue(self.v1.read_text(encoding="utf-8").startswith("+++\njob ="), "front matter kept")
        [edit] = db.cv_edits(self.conn, self.app)
        self.assertIn("Built a shared", Path(edit["backup_path"]).read_text(encoding="utf-8"))
        self.assertEqual(len(db.tailored_versions(self.conn, self.app)), 1, "no new version")
        self.assertIsNotNone(db.cv_check(self.conn, self.app, 1), "the rule check ran again")
        undone = cvedit.undo_tailored(self.settings, self.conn, self.app)
        self.assertEqual(undone["left"], 0)
        self.assertIn("Built a shared", self.v1.read_text(encoding="utf-8"))
        self.assertTrue(Path(edit["backup_path"]).with_suffix(".undone.md").is_file(), "the undone text is kept")
        with self.assertRaisesRegex(ValueError, "nothing to undo"):
            cvedit.undo_tailored(self.settings, self.conn, self.app)

    def test_comments_stay_out_of_the_file(self) -> None:
        result = self.edit("- Moved 40 services", "%% lead with this\n- Moved 40 services")
        self.assertEqual((result["changed"], result["comments"]), (False, 1), "a comment alone changes no text")
        self.assertNotIn("%%", self.v1.read_text(encoding="utf-8"))
        self.assertEqual([c["body"] for c in db.cv_comments(self.conn, self.app)], ["lead with this"])
        self.assertIn("%% lead with this", cvedit.editor_text(self.conn, self.app)["text"])
        self.edit("%% lead with this\n", "")
        self.assertEqual(db.cv_comments(self.conn, self.app), [], "removing the line handles the comment")

    def test_a_stale_save_is_refused_and_writes_nothing(self) -> None:
        current = cvedit.editor_text(self.conn, self.app)
        self.edit("Python, Go", "Python and Go")
        before = self.v1.read_text(encoding="utf-8")
        with self.assertRaises(db.NotesChanged):
            cvedit.save_tailored(self.settings, self.conn, self.app, 1, current["text"] + "\nmore", current["stamp"])
        self.assertEqual(self.v1.read_text(encoding="utf-8"), before)
        with self.assertRaises(db.NotesChanged):  # an older version is never edited
            cvedit.save_tailored(self.settings, self.conn, self.app, 0, "x", None)

    def test_the_sent_version_is_frozen(self) -> None:
        db.record_cv_sent(self.conn, self.app, self.v1.name)
        frozen = self.v1.read_text(encoding="utf-8")
        result = self.edit("Ran Kubernetes clusters", "Ran production Kubernetes clusters")
        self.assertEqual((result["version"], result["new_version"]), (2, True))
        self.assertEqual(self.v1.read_text(encoding="utf-8"), frozen)
        latest = Path(db.tailored_cv(self.conn, self.app)["file_path"])
        self.assertIn("production Kubernetes", latest.read_text(encoding="utf-8"))
        self.assertEqual(len(db.thread(self.conn, self.app)), 1, "the user's own edit posts nothing")

    def test_an_edit_marks_the_ai_check_out_of_date(self) -> None:
        db.save_cv_eval(self.conn, self.app, 1, [{"check": "editorial", "severity": "look", "line": 3,
                                                 "message": "Editorial"}], {"fit": 4, "skim": 4})
        self.assertEqual(db.eval_state(self.conn, self.app, db.cv_check(self.conn, self.app, 1)), "done")
        self.edit("Python, Go", "Python and Go")
        check = db.cv_check(self.conn, self.app, 1)
        self.assertTrue(check["summary"]["stale"])
        self.assertFalse([f for f in check["findings"] if f["source"] == "ai"], "old AI findings are dropped")
        self.assertEqual(db.eval_state(self.conn, self.app, check), "stale")
        db.queue_request(self.conn, self.app, "cv_eval")
        self.assertEqual(db.eval_state(self.conn, self.app, check), "pending")

    def test_agent_draft_and_edit(self) -> None:
        self.edit("- Moved 40 services", "%% say zero downtime only if true\n- Moved 40 services")
        code, out, _ = self.run_cli("tailor", "draft", str(self.app))
        self.assertEqual(code, 0)
        draft = Path(out.splitlines()[0])
        self.assertIn("%% say zero downtime", draft.read_text(encoding="utf-8"))
        self.assertIn("1 comment(s)", out)
        draft.write_text(draft.read_text(encoding="utf-8").replace("%% say zero downtime only if true\n", "")
                         .replace("Moved 40 services", "Moved 40 services, one at a time,"), encoding="utf-8")
        msg = db.add_message(self.conn, self.app, "Revise only the lines I commented on.")
        with mock.patch("sys.stdin", io.StringIO("Reworded the migration bullet.")):
            code, out, _ = self.run_cli("tailor", "edit", str(self.app), "--to", str(msg))
        self.assertEqual(code, 0, out)
        self.assertIn("edited in place", out)
        self.assertIn("one at a time", self.v1.read_text(encoding="utf-8"))
        self.assertEqual(db.cv_comments(self.conn, self.app), [])
        self.assertFalse(draft.exists())
        self.assertEqual(db.thread(self.conn, self.app)[-1]["reply_to"], msg)
        self.assertEqual(db.cv_edits(self.conn, self.app)[-1]["author"], "agent")
        code, out, _ = self.run_cli("tailor", "undo", str(self.app))
        self.assertEqual(code, 0)
        self.assertNotIn("one at a time", self.v1.read_text(encoding="utf-8"))

    def test_agent_edit_refused_when_the_user_edited_meanwhile(self) -> None:
        code, out, _ = self.run_cli("tailor", "draft", str(self.app))
        self.edit("Python, Go", "Python and Go")
        with mock.patch("sys.stdin", io.StringIO("")):
            code, _, err = self.run_cli("tailor", "edit", str(self.app))
        self.assertNotEqual(code, 0)
        self.assertIn("Python and Go", self.v1.read_text(encoding="utf-8"))

    def test_versions_per_sent_cv(self) -> None:
        self.assertEqual(stats.cv_effort(self.conn), {"sent": 0, "versions": None, "edits": None})
        self.edit("Python, Go", "Python and Go")
        self.edit("Python and Go", "Python, Go")
        v2 = write(research.tailored_file(self.private, "Globex Labs", "Eng", db.today_iso(), 2), FRONT + CV)
        db.save_tailored_cv(self.conn, self.app, str(v2), "v2")
        db.record_cv_sent(self.conn, self.app, v2.name)
        self.assertEqual(stats.cv_effort(self.conn), {"sent": 1, "versions": 2.0, "edits": 2.0})


class MasterEditorTest(IsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.conn = db.connect(self.prep_db)
        self.addCleanup(self.conn.close)
        self.private = self.tmp / "private"
        write(self.private / "cv.md", CV)

    def test_save_goes_through_versions_and_keeps_comments_out(self) -> None:
        current = cvedit.master_text(self.conn, self.private)
        text = current["text"].replace("in Python and Go.", "in Python and Go.\n%% shorten")
        result = cvedit.save_master(self.conn, self.private, text, current["stamp"], "")
        self.assertIsNone(result["version"], "a comment alone changes no text")
        current = cvedit.master_text(self.conn, self.private)
        self.assertIn("%% shorten", current["text"])
        result = cvedit.save_master(self.conn, self.private, current["text"].replace("Python, Go", "Python and Go"),
                                    current["stamp"], "")
        self.assertIsNotNone(result["previous"], "the replaced text is a version first")
        saved = cvfile.read_cv(self.private)
        self.assertIn("Python and Go", saved)
        self.assertNotIn("%%", saved)
        self.assertEqual(db.cv_version(self.conn, result["version"])["note"], "Edited in the panel")
        with self.assertRaises(db.NotesChanged):
            cvedit.save_master(self.conn, self.private, "x", current["stamp"], "")

    def test_cv_draft_shows_comments_and_save_handles_them(self) -> None:
        db.replace_cv_comments(self.conn, None, [("- Wrote internal tooling in Python and Go.", "shorten")])
        code, out, _ = self.run_cli("cv", "draft")
        draft = Path(out.splitlines()[0])
        self.assertIn("%% shorten", draft.read_text(encoding="utf-8"))
        draft.write_text(draft.read_text(encoding="utf-8").replace("%% shorten\n", "")
                         .replace("Wrote internal tooling in Python and Go.", "Wrote tools in Python and Go."),
                         encoding="utf-8")
        code, out, _ = self.run_cli("cv", "save", "--note", "Shorter tooling line")
        self.assertEqual(code, 0, out)
        self.assertNotIn("%%", cvfile.read_cv(self.private))
        self.assertEqual(db.cv_comments(self.conn, None), [])


class PlanCliTest(IsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.conn = db.connect(self.prep_db)
        self.addCleanup(self.conn.close)
        write(self.tmp / "private" / "cv.md", CV)
        self.facts = write(self.tmp / "private" / "cv-facts.md", "# Facts\n")
        self.app, _ = db.record_application(self.conn, "Globex Labs", "Engineer", status="to_apply")

    def test_plan_first_then_write_the_cv(self) -> None:
        db.queue_request(self.conn, self.app, "plan")
        code, out, _ = self.run_cli("research", "next", str(self.app))
        self.assertIn("Tailoring plan (before any CV is written", out)
        self.assertNotIn("Tailored CV (a copy", out)
        code, out, _ = self.run_cli("plan", "path", str(self.app))
        path = Path(out.splitlines()[0])
        self.assertEqual(path.name, f"PLAN_GlobexLabs_{self.app}.md")
        write(path, PLAN)
        with mock.patch("sys.stdin", io.StringIO("")):
            code, out, _ = self.run_cli("plan", "save", str(self.app))
        self.assertEqual(code, 0, out)
        self.assertEqual(db.tailor_plan(self.conn, self.app)["status"], "draft")
        self.assertNotIn("plan", {r["kind"] for r in db.open_requests(self.conn, self.app)})
        self.assertIn("2 questions", db.thread(self.conn, self.app)[-1]["body"])
        msg = db.add_message(self.conn, self.app, "Lead with the pipeline.")
        with mock.patch("sys.stdin", io.StringIO("Lead with the pipeline.")):
            code, out, _ = self.run_cli("plan", "answer", str(self.app), "1", "--to", str(msg))
        self.assertEqual(code, 0, out)
        self.assertEqual(tailorplan.questions(path.read_text(encoding="utf-8"))[0].answer, "Lead with the pipeline.")
        self.assertEqual(self.facts.read_text(encoding="utf-8"), "# Facts\n", "answers never go to the facts bank")
        code, out, _ = self.run_cli("plan", "write", str(self.app))
        self.assertIn("queued", out)
        self.assertEqual(db.tailor_plan(self.conn, self.app)["status"], "agreed")
        code, out, _ = self.run_cli("research", "next", str(self.app))
        self.assertIn("agreed plan:", out)
        self.assertIn(str(path), out)

    def test_just_write_it_has_no_plan(self) -> None:
        db.queue_request(self.conn, self.app, "tailor")
        code, out, _ = self.run_cli("research", "next", str(self.app))
        self.assertIn("no plan: Just write it was pressed", out)

    def test_style_add(self) -> None:
        with mock.patch("sys.stdin", io.StringIO("Career break 2024–2025: building Hirey.")):
            code, out, _ = self.run_cli("style", "add", "--app", str(self.app))
        self.assertEqual(code, 0, out)
        text = research.style_file(self.tmp / "private").read_text(encoding="utf-8")
        self.assertIn("## Agreed wording\n- Career break 2024–2025: building Hirey. (#", text)
        with mock.patch("sys.stdin", io.StringIO("Expected salary 90k")):
            code, _, _ = self.run_cli("style", "add")
        self.assertNotEqual(code, 0)


class PanelTest(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.private = self.tmp / "private"
        write(self.private / "cv.md", CV)
        with self.conn() as conn:
            self.app, _ = db.record_application(conn, "Globex Labs", "Engineer", status="to_apply")
            db.update_application(conn, self.app, role_short="Eng")
        no_pdf = mock.patch("prep.cvpdf.write_pdf", side_effect=cvpdf.PdfUnavailable("no WeasyPrint"))
        no_pdf.start()
        self.addCleanup(no_pdf.stop)

    def panel(self) -> str:
        return self.get(f"/?app={self.app}")[2]

    def test_tailor_cv_always_asks_for_a_plan_first(self) -> None:
        body = self.panel()
        self.assertIn('name="kind" value="plan"', body)
        self.assertNotIn("Just write it", body)
        self.assertNotIn('name="kind" value="tailor"', body)
        self.post("/applications/research", {"app_id": self.app, "kind": "plan"})
        self.assertIn("Planning…", self.panel())

    def test_a_two_section_plan_has_no_question_counter(self) -> None:
        path = write(tailorplan.plan_file(self.private, "Globex Labs", self.app), NEW_PLAN)
        with self.conn() as conn:
            db.save_tailor_plan(conn, self.app, str(path), "Plan ready.")
        body = self.panel()
        self.assertIn("What will change in the CV", body)
        self.assertNotIn("questions answered", body)
        self.assertNotIn("question answered", body)

    def test_plan_card_edit_and_write(self) -> None:
        path = write(tailorplan.plan_file(self.private, "Globex Labs", self.app), PLAN)
        with self.conn() as conn:
            db.save_tailor_plan(conn, self.app, str(path), "Plan ready.")
        body = self.panel()
        self.assertIn('id="card-plan"', body)
        self.assertIn("1 of 2 questions answered", body)
        stamp = cvfile.sha256(PLAN)
        status, _, out = self.post("/applications/plan", {"app_id": self.app, "stamp": "old", "text": PLAN + "x"}, JSON)
        self.assertEqual(status, 400)
        self.assertIn("The plan as it is now", json.loads(out)["html"])
        edited = PLAN.replace("   Answer:\n2.", "   Answer: The pipeline.\n2.")
        status, _, out = self.post("/applications/plan/write", {"app_id": self.app, "stamp": stamp, "text": edited}, JSON)
        self.assertEqual(status, 200, out)
        with self.conn() as conn:
            self.assertEqual(db.tailor_plan(conn, self.app)["status"], "agreed")
            self.assertIn("tailor", {r["kind"] for r in db.open_requests(conn, self.app)})
        self.assertIn("The pipeline.", path.read_text(encoding="utf-8"))

    def test_editor_save_undo_and_revise(self) -> None:
        v1 = write(research.tailored_file(self.private, "Globex Labs", "Eng", TODAY_FILE, 1), FRONT + CV)
        with self.conn() as conn:
            db.save_tailored_cv(conn, self.app, str(v1), "v1")
            current = cvedit.editor_text(conn, self.app)
        self.assertIn('id="tailor-edit"', self.panel())
        text = current["text"].replace("Python, Go", "Python and Go\n%% say which services")
        with self.assertLogs("prep", level="INFO") as logs:
            status, _, out = self.post("/applications/cv/edit", {"app_id": self.app, "version": 1,
                                                                  "stamp": current["stamp"], "text": text}, JSON)
        self.assertEqual(status, 200, out)
        self.assertIn("Undo brings back", json.loads(out)["message"])
        self.assertNotIn("Python and Go", "\n".join(logs.output), "CV text is never logged")
        self.assertNotIn("say which services", "\n".join(logs.output))
        body = self.panel()
        self.assertIn("Undo edit", body)
        self.assertIn("Revise commented lines (1)", body)
        status, _, out = self.post("/applications/cv/edit", {"app_id": self.app, "version": 1,
                                                              "stamp": current["stamp"], "text": "stale"}, JSON)
        self.assertEqual(status, 400)
        reply = json.loads(out)
        self.assertIn("The text as it is now", reply["html"])
        self.assertIn("stale", reply["html"], "the user's text is kept in the editor")
        self.post("/applications/cv/revise", {"app_id": self.app}, JSON)
        with self.conn() as conn:
            [msg] = [m for m in db.thread(conn, self.app) if m["author"] == "user"]
            self.assertIn("Revise only the lines I commented on", msg["body"])
            self.assertNotIn("say which services", msg["body"])
        self.post("/applications/cv/undo", {"app_id": self.app}, JSON)
        self.assertIn("Python, Go", v1.read_text(encoding="utf-8"))

    def test_master_editor(self) -> None:
        body = self.get("/?cv=1")[2]
        self.assertIn('id="master-edit"', body)
        with self.conn() as conn:
            current = cvedit.master_text(conn, self.private)
        status, _, out = self.post("/cv/edit", {"stamp": current["stamp"], "note": "Tooling line",
                                                "text": current["text"].replace("Python, Go", "Python and Go")}, JSON)
        self.assertEqual(status, 200, out)
        self.assertIsNotNone(json.loads(out)["previous"])
        self.assertIn("Python and Go", cvfile.read_cv(self.private))

    def test_stats_tile(self) -> None:
        self.assertIn("Versions per sent CV", self.get("/stats")[2])


TODAY_FILE = "2026-01-20"
