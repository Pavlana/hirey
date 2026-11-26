import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from prep import db
from tests.helpers import IsolatedTestCase


class GapDbTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.conn = db.connect(Path(self._tmp.name) / "prep.db")
        self.addCleanup(self.conn.close)
        self.app = db.add_application(self.conn, "Initech", "Engineer", status="technical")

    def drills(self, gap_id: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM tasks WHERE gap_id = ? ORDER BY id", (gap_id,))]

    def test_weaknesses_become_gaps_with_one_drill_each(self) -> None:
        found = db.add_gap_drills(self.conn, ["Dynamic programming", "Talking through trade-offs"], self.app,
                                  label="interview")
        self.assertEqual(found["gaps"], found["new_gaps"])
        for gap_id in found["new_gaps"]:
            gap = self.conn.execute("SELECT * FROM gaps WHERE id = ?", (gap_id,)).fetchone()
            self.assertEqual((gap["session"], gap["application_id"], gap["status"]), ("interview", self.app, "open"))
            [task] = self.drills(gap_id)
            self.assertEqual((task["kind"], task["status"], task["title"]), ("drill", "todo", f"Drill: {gap['gap']}"))

    def test_open_gap_named_again_is_matched_not_duplicated(self) -> None:
        [fit_gap] = db.save_cv_fit(self.conn, self.app, 3, ["Kubernetes  in production"], "/x/fit.md", "2026-11-01")
        self.assertEqual(self.drills(fit_gap), [])  # a CV fit gap has no drill until a debrief names it
        found = db.add_gap_drills(self.conn, ["kubernetes in PRODUCTION", "kubernetes in production"])
        self.assertEqual((found["gaps"], found["new_gaps"], len(found["tasks"])), ([fit_gap], [], 1))
        again = db.add_gap_drills(self.conn, ["Kubernetes in production"])
        self.assertEqual((again["gaps"], again["tasks"]), ([fit_gap], []))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM gaps").fetchone()[0], 1)

    def test_closed_gap_named_again_is_new(self) -> None:
        first = db.add_gap_drills(self.conn, ["Graphs"])
        db.close_gap(self.conn, first["gaps"][0])
        again = db.add_gap_drills(self.conn, ["Graphs"])
        self.assertEqual(len(again["new_gaps"]), 1)
        self.assertNotEqual(again["gaps"], first["gaps"])

    def test_close_gap_completes_drills(self) -> None:
        found = db.add_gap_drills(self.conn, ["Graphs"], self.app, "2026-11-20")
        [gap_id], [task_id] = found["gaps"], found["tasks"]
        self.assertEqual(db.close_gap(self.conn, gap_id), [task_id])
        self.assertEqual(db.get_task(self.conn, task_id)["status"], "done")
        self.assertEqual(db.close_gap(self.conn, gap_id), [])  # closing again changes nothing
        with self.assertRaises(LookupError):
            db.close_gap(self.conn, 999)

    def test_invalid_input_records_nothing(self) -> None:
        for kwargs in (dict(day="10/11/2026"), dict(due_date="soon")):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                db.add_gap_drills(self.conn, ["Graphs"], **kwargs)
        with self.assertRaises(LookupError):
            db.add_gap_drills(self.conn, ["Graphs"], 999)
        for table in ("gaps", "tasks"):
            self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)

    def test_database_from_before_sessions_were_dropped_still_works(self) -> None:
        path = Path(self._tmp.name) / "old.db"
        with closing(sqlite3.connect(path)) as old:
            old.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT)")
            old.execute("CREATE TABLE gaps (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, session TEXT, "
                        "gap TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', "
                        "session_id INTEGER REFERENCES sessions(id))")
            old.execute("INSERT INTO gaps (date, session, gap) VALUES ('2026-01-01', 'mock', 'Old gap')")
            old.commit()
        with closing(db.connect(path)) as conn:
            found = db.add_gap_drills(conn, ["old  gap", "New gap"])
            self.assertEqual((len(found["new_gaps"]), len(found["tasks"])), (1, 2))


class GapCliTest(IsolatedTestCase):
    def test_gap_add_list_and_close(self) -> None:
        code, out, err = self.run_cli("gap", "add", "Capacity estimates", "Caching", "--due", "+2")
        self.assertEqual(code, 0, err)
        self.assertIn("gaps: 2 (2 new): #1 #2", out)
        self.assertEqual(out.count("[drill] Drill: "), 2)
        self.assertIn("1 open drill", self.run_cli("gap", "list")[1])
        code, out, _ = self.run_cli("gap", "close", "1")
        self.assertIn("1 drill task done", out)
        self.assertIn("Capacity estimates", self.run_cli("gap", "list", "--closed")[1])
        self.assertEqual(self.run_cli("gap", "close", "42")[0], 2)

    def test_gap_add_matches_open_gap(self) -> None:
        self.assertEqual(self.run_cli("gap", "add", "Graphs")[0], 0)
        code, out, _ = self.run_cli("gap", "add", "graphs", "Tries")
        self.assertIn("gaps: 2 (1 new)", out)
        self.assertEqual(out.count("[drill]"), 1)

    def test_session_and_mock_commands_are_gone(self) -> None:
        for argv in (("session", "list"), ("mock", "path", "1", "--stage", "technical")):
            self.assertEqual(self.run_cli(*argv)[0], 2)


class DebriefDbTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.conn = db.connect(Path(self._tmp.name) / "prep.db")
        self.addCleanup(self.conn.close)
        self.app = db.add_application(self.conn, "Initech", "Engineer", status="applied")

    def count(self, table: str) -> int:
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_debrief_fills_the_interview_stage_stored(self) -> None:
        db.move_application(self.conn, self.app, "technical", interview_date="2026-11-10", today="2026-11-01")
        due_before = {t["id"]: t["due_date"] for t in db.list_tasks(self.conn, "todo")}
        found = db.record_debrief(self.conn, self.app, "2026-11-10", None, "Two-sum; LRU cache",
                                  "Clear tests", "Talk while coding", "/p/sessions/t.md", ["LRU cache"])
        [row] = [dict(r) for r in self.conn.execute("SELECT * FROM interviews")]
        self.assertEqual(row["id"], found["interview_id"])
        self.assertEqual((row["stage"], row["questions"], row["went_well"], row["to_improve"], row["file_path"]),
                         ("technical", "Two-sum; LRU cache", "Clear tests", "Talk while coding", "/p/sessions/t.md"))
        [gap_id] = found["new_gaps"]
        self.assertEqual(self.conn.execute("SELECT session FROM gaps WHERE id = ?", (gap_id,)).fetchone()[0], "interview")
        [drill] = found["tasks"]
        self.assertEqual(db.get_task(self.conn, drill)["title"], "Drill: LRU cache")
        for task_id, due in due_before.items():  # a debrief moves no task dates
            self.assertEqual(db.get_task(self.conn, task_id)["due_date"], due)
        self.assertEqual([d["id"] for d in db.debriefs(self.conn, self.app)], [row["id"]])

    def test_debrief_without_a_stored_interview_adds_one(self) -> None:
        db.move_application(self.conn, self.app, "rejected", today="2026-11-12")
        [debrief] = [t for t in db.list_tasks(self.conn, "todo") if t["title"] == db.DEBRIEF_TASK]
        found = db.record_debrief(self.conn, self.app, "2026-11-11", "final", None, None, "Stories too long",
                                  gaps=["STAR stories"])
        row = self.conn.execute("SELECT * FROM interviews WHERE id = ?", (found["interview_id"],)).fetchone()
        self.assertEqual((row["date"], row["stage"], row["to_improve"]), ("2026-11-11", "final", "Stories too long"))
        self.assertEqual(found["debrief_tasks"], [debrief["id"]])
        self.assertEqual(db.get_task(self.conn, debrief["id"])["status"], "done")
        self.assertEqual(len(found["tasks"]), 1)

    def test_invalid_debrief_records_nothing(self) -> None:
        with self.assertRaises(ValueError):
            db.record_debrief(self.conn, self.app, "2026-11-10", "offer")
        with self.assertRaises(ValueError):
            db.record_debrief(self.conn, self.app, "10/11/2026")
        with self.assertRaises(LookupError):
            db.record_debrief(self.conn, 99)
        self.assertEqual((self.count("interviews"), self.count("gaps")), (0, 0))


class DebriefCliTest(IsolatedTestCase):
    def debrief(self, stdin: str | None, *argv: str) -> tuple[int, str, str]:
        with mock.patch("sys.stdin") as fake:
            fake.read.return_value = stdin
            return self.run_cli("debrief", *argv, *(("--transcript",) if stdin is not None else ()))

    def test_debrief_saves_transcript_under_private_sessions(self) -> None:
        with closing(db.connect(self.prep_db)) as conn:
            app = db.add_application(conn, "Initech", "Engineer", status="technical")
        argv = ("--app", str(app), "--date", "2026-11-10", "--questions", "LRU cache",
                "--to-improve", "Pace", "--gap", "Talking while coding")
        code, out, err = self.debrief("Q: design an LRU cache\nA: ...", *argv)
        self.assertEqual(code, 0, err)
        path = self.tmp / "private" / "sessions" / "2026-11-10-initech-technical-debrief.md"
        self.assertEqual(path.read_text(encoding="utf-8"), "Q: design an LRU cache\nA: ...")
        self.assertIn("notes saved", out)
        self.assertIn("gaps: 1 (1 new): #1", out)
        self.assertIn("[drill] Drill: Talking while coding", out)
        code, out, err = self.debrief("second", *argv)  # same name taken: a new file, never overwritten
        self.assertEqual(code, 0, err)
        self.assertTrue(path.with_name(path.stem + "-2.md").exists())
        with closing(db.connect(self.prep_db)) as conn:
            self.assertEqual(db.debriefs(conn, app)[1]["file_path"], str(path.resolve()))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM interviews").fetchone()[0], 2)

    def test_bad_debrief_writes_no_file_and_no_rows(self) -> None:
        code, _, err = self.debrief("notes", "--gap", "x")
        self.assertEqual(code, 2, "--app is required")
        code, _, err = self.debrief("notes", "--app", "99")
        self.assertEqual(code, 2)
        code, _, err = self.debrief("notes", "--app", "1", "--score", "coding=3")
        self.assertEqual(code, 2, "scores are gone")
        self.assertFalse(list((self.tmp / "private").rglob("*.md")))
        with closing(db.connect(self.prep_db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM interviews").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
