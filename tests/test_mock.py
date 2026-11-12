"""Mock interviews (/mock): the panel's start control, the transcript path under private_dir/sessions/,
recording a mock through `session add`, and the committed question sets. Made-up data only; dev3 is
never called (the ping is mocked)."""

import json
import re
from contextlib import closing
from pathlib import Path
from unittest import mock

from prep import assistant, db, privacy
from tests.helpers import IsolatedTestCase
from tests.test_web import HOST, WebTestCase

ACME = 1  # recruiter_call
STARK = 2  # applied
WAYNE = 3  # technical
CYBERDYNE = 4  # rejected
SKILL = Path(__file__).resolve().parent.parent / ".claude" / "skills" / "mock"


class StartTest(WebTestCase):
    def thread_rows(self, app_id: int) -> list[dict]:
        with self.conn() as conn:
            return db.thread(conn, app_id)

    def test_start_stores_the_fixed_message_and_pings_ask(self) -> None:
        with mock.patch.object(assistant, "notify_message_in_background") as bg:
            status, headers, _ = self.post("/applications/mock", {
                "app_id": WAYNE, "stage": "system_design", "body": "ignored", "return": f"/?app={WAYNE}"})
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], f"/?app={WAYNE}&msg=mock_started")
        [m] = self.thread_rows(WAYNE)
        self.assertEqual((m["author"], m["status"], m["body"]),
                         ("user", "pending", "Start a mock interview: system design (system_design)"))
        bg.assert_called_once_with(self.settings, WAYNE, m["id"])

    def test_start_as_json_returns_the_thread(self) -> None:
        with mock.patch.object(assistant, "notify_message_in_background"):
            status, _, out = self.post("/applications/mock", {"app_id": STARK, "stage": "technical"},
                                       {**HOST, "Accept": "application/json"})
        self.assertEqual(status, 200)
        reply = json.loads(out)
        self.assertIn("Start a mock interview: coding interview (technical)", reply["html"])
        self.assertIn("data-pending", reply["html"])

    def test_bad_stage_or_application_stores_nothing(self) -> None:
        json_headers = {**HOST, "Accept": "application/json"}
        with mock.patch.object(assistant, "notify_message_in_background") as bg:
            for form in ({"app_id": STARK, "stage": "offer"}, {"app_id": STARK, "stage": "../x"},
                         {"app_id": STARK}, {"app_id": 999, "stage": "technical"}, {"stage": "technical"},
                         {"app_id": CYBERDYNE, "stage": "technical"}):
                with self.subTest(form=form):
                    status, _, out = self.post("/applications/mock", form, json_headers)
                    self.assertEqual(status, 400)
                    self.assertFalse(json.loads(out)["ok"])
        bg.assert_not_called()
        self.assertEqual(self.thread_rows(STARK), [])

    def test_panel_offers_a_mock_for_open_applications(self) -> None:
        _, _, page = self.get(f"/?app={WAYNE}")
        form = re.search(r'<form method="post" action="/applications/mock".*?</form>', page, re.S)
        self.assertIsNotNone(form)
        for stage in db.INTERVIEW_STAGES:
            self.assertIn(f'<option value="{stage}"', form.group(0))
        self.assertIn('<option value="technical" selected>', form.group(0), "the current stage first")
        _, _, page = self.get(f"/?app={STARK}")
        self.assertIn('<option value="recruiter_call" selected>', page, "not at an interview yet: the first one")
        _, _, page = self.get(f"/?app={CYBERDYNE}")
        self.assertNotIn('action="/applications/mock"', page, "no mock for a closed application")


class MockPathTest(IsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        with closing(db.connect(self.prep_db)) as conn:
            self.app = db.add_application(conn, "Initech Example", "Engineer", status="technical")

    def test_path_is_under_private_sessions_and_never_reused(self) -> None:
        code, out, err = self.run_cli("mock", "path", str(self.app), "--stage", "technical", "--date", "2026-11-10")
        self.assertEqual(code, 0, err)
        first = Path(out.strip())
        sessions = self.tmp / "private" / "sessions"
        self.assertEqual(first, sessions / f"mock-{self.app}-technical-2026-11-10.md")
        self.assertTrue(sessions.is_dir())
        self.assertFalse(first.exists(), "the skill writes the file")
        first.write_text("transcript\n", encoding="utf-8")
        _, out, _ = self.run_cli("mock", "path", str(self.app), "--stage", "technical", "--date", "2026-11-10")
        self.assertEqual(Path(out.strip()).name, f"mock-{self.app}-technical-2026-11-10-2.md")

    def test_refused_without_private_dir_or_with_bad_input(self) -> None:
        code, _, err = self.run_cli("mock", "path", "999", "--stage", "technical")
        self.assertEqual(code, 2)
        code, _, err = self.run_cli("mock", "path", str(self.app), "--stage", "offer")
        self.assertEqual(code, 2)
        with mock.patch.dict("os.environ", {"PREP_PRIVATE_DIR": ""}):
            code, _, err = self.run_cli("mock", "path", str(self.app), "--stage", "technical")
        self.assertNotEqual(code, 0)
        self.assertIn("private_dir", err)

    def test_a_finished_mock_records_session_scores_gaps_and_drills(self) -> None:
        _, out, _ = self.run_cli("mock", "path", str(self.app), "--stage", "technical")
        path = Path(out.strip())
        path.write_text("# Mock coding interview\n\nQ1. Example question\nA1. Example answer\n", encoding="utf-8")
        code, out, err = self.run_cli("session", "add", "--kind", "mock", "--app", str(self.app), "--stage", "technical",
                                      "--file", str(path), "--score", "problem solving=3", "--score", "communication=4",
                                      "--gap", "Stating complexity before coding")
        self.assertEqual(code, 0, err)
        self.assertIn("[drill] Drill: Stating complexity before coding", out)
        with closing(db.connect(self.prep_db)) as conn:
            [session] = db.list_sessions(conn)
            [gap] = db.list_gaps(conn)
            [drill] = [t for t in db.app_tasks(conn)[self.app] if t["kind"] == "drill" and t.get("gap_id") == gap["id"]]
        self.assertEqual((session["kind"], session["stage"], session["application_id"]), ("mock", "technical", self.app))
        self.assertEqual(session["scores"], {"problem solving": 3, "communication": 4})
        self.assertEqual(session["file_path"], str(path.resolve()))
        self.assertEqual((gap["session_id"], gap["application_id"]), (session["id"], self.app))
        self.assertEqual(drill["title"], "Drill: Stating complexity before coding")


class QuestionSetTest(IsolatedTestCase):
    def test_one_question_set_per_interview_stage_with_score_areas(self) -> None:
        for stage in db.INTERVIEW_STAGES:
            with self.subTest(stage=stage):
                text = (SKILL / "questions" / f"{stage}.md").read_text(encoding="utf-8")
                self.assertIn("Score areas", text)
                self.assertIn("Rubric", text)
                areas = re.findall(r"^- `([a-z -]+)`:", text, re.M)
                self.assertGreaterEqual(len(areas), 3)
                self.assertEqual(areas, [a.lower() for a in areas], "area names are lowercase, as stored")

    def test_skill_files_are_committable(self) -> None:
        root = SKILL.parent.parent.parent
        files = [str(p.relative_to(root)) for p in SKILL.rglob("*.md")]
        self.assertEqual(privacy.violations(files), [])
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn(db.MOCK_START.split(":")[0], skill)
        self.assertIn("mock path", skill)
        self.assertIn("session add --kind mock", skill)
        self.assertIn("· finished.", skill, "the end marker that closes a mock")
