import sqlite3
import tempfile
import unittest
from pathlib import Path

from prep import db
from tests.helpers import IsolatedTestCase


class SessionDbTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "prep.db"
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        self.app = db.add_application(self.conn, "Initech", "Engineer", status="technical")

    def drills(self, gap_id: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM tasks WHERE gap_id = ? ORDER BY id", (gap_id,))]

    def test_session_with_weaknesses_creates_gaps_and_drills(self) -> None:
        found = db.record_session(self.conn, "mock", "2026-11-10", self.app, "technical",
                                  {"coding": 3, "communication": 4}, None,
                                  ["Dynamic programming", "Talking through trade-offs"])
        session = db.get_session(self.conn, found["session_id"])
        self.assertEqual((session["kind"], session["stage"], session["application_id"]), ("mock", "technical", self.app))
        self.assertEqual(session["scores"], {"coding": 3, "communication": 4})
        self.assertEqual(len(found["new_gaps"]), 2)
        self.assertEqual(found["gaps"], found["new_gaps"])
        for gap_id in found["new_gaps"]:
            gap = self.conn.execute("SELECT * FROM gaps WHERE id = ?", (gap_id,)).fetchone()
            self.assertEqual((gap["session_id"], gap["session"], gap["application_id"], gap["status"]),
                             (found["session_id"], "mock", self.app, "open"))
            [task] = self.drills(gap_id)
            self.assertEqual((task["kind"], task["status"], task["application_id"]), ("drill", "todo", self.app))
            self.assertEqual(task["title"], f"Drill: {gap['gap']}")
        self.assertEqual(sorted(found["tasks"]), sorted(t["id"] for g in found["new_gaps"] for t in self.drills(g)))

    def test_session_without_weaknesses(self) -> None:
        found = db.record_session(self.conn, "real", stage="final")
        self.assertEqual((found["gaps"], found["tasks"]), ([], []))
        self.assertEqual(db.get_session(self.conn, found["session_id"])["scores"], {})
        self.assertEqual(db.get_session(self.conn, found["session_id"])["date"], db.today_iso())

    def test_open_gap_named_again_is_matched_not_duplicated(self) -> None:
        [fit_gap] = db.save_cv_fit(self.conn, self.app, 3, ["Kubernetes  in production"], "/x/fit.md", "2026-11-01")
        self.assertEqual(self.drills(fit_gap), [])  # a CV fit gap has no drill until practice names it
        found = db.record_session(self.conn, "mock", gaps=["kubernetes in PRODUCTION", "kubernetes in production"])
        self.assertEqual((found["gaps"], found["new_gaps"]), ([fit_gap], []))
        self.assertEqual(len(found["tasks"]), 1)
        gap = self.conn.execute("SELECT * FROM gaps WHERE id = ?", (fit_gap,)).fetchone()
        self.assertEqual((gap["session_id"], gap["session"]), (found["session_id"], db.FIT_GAP_SESSION))
        # a second session naming it again: no second drill, and the first session stays its origin
        again = db.record_session(self.conn, "real", gaps=["Kubernetes in production"])
        self.assertEqual((again["gaps"], again["tasks"]), ([fit_gap], []))
        self.assertEqual(self.conn.execute("SELECT session_id FROM gaps WHERE id = ?", (fit_gap,)).fetchone()[0],
                         found["session_id"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM gaps").fetchone()[0], 1)

    def test_closed_gap_named_again_is_new(self) -> None:
        first = db.record_session(self.conn, "mock", gaps=["Graphs"])
        db.close_gap(self.conn, first["gaps"][0])
        again = db.record_session(self.conn, "mock", gaps=["Graphs"])
        self.assertEqual(len(again["new_gaps"]), 1)
        self.assertNotEqual(again["gaps"], first["gaps"])

    def test_invalid_input_records_nothing(self) -> None:
        bad = [
            dict(kind="practice"),
            dict(kind="mock", stage="offer"),
            dict(kind="mock", scores={"coding": 6}),
            dict(kind="mock", scores={"coding": 0}),
            dict(kind="mock", scores={"coding": True}),
            dict(kind="mock", scores={"coding": "3"}),
            dict(kind="mock", day="10/11/2026"),
            dict(kind="mock", due_date="soon"),
        ]
        for kwargs in bad:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                db.record_session(self.conn, gaps=["Graphs"], **kwargs)
        with self.assertRaises(LookupError):
            db.record_session(self.conn, "mock", application_id=999, gaps=["Graphs"])
        for table in ("sessions", "gaps", "tasks"):
            self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)

    def test_close_gap_completes_drills_and_moves_next_action(self) -> None:
        found = db.record_session(self.conn, "mock", application_id=self.app, gaps=["Graphs"], due_date="2026-11-20")
        [gap_id], [task_id] = found["gaps"], found["tasks"]
        self.assertEqual(db.get_application(self.conn, self.app)["next_action_date"], "2026-11-20")
        self.assertEqual(db.close_gap(self.conn, gap_id), [task_id])
        task = db.get_task(self.conn, task_id)
        self.assertEqual(task["status"], "done")
        self.assertIsNotNone(task["done_at"])
        self.assertEqual(self.conn.execute("SELECT status FROM gaps WHERE id = ?", (gap_id,)).fetchone()[0], "closed")
        self.assertNotEqual(db.get_application(self.conn, self.app)["next_action"], "Drill: Graphs")
        self.assertEqual(db.close_gap(self.conn, gap_id), [])  # closing again changes nothing
        self.assertEqual(db.get_task(self.conn, task_id)["done_at"], task["done_at"])
        with self.assertRaises(LookupError):
            db.close_gap(self.conn, 999)

    def test_add_gap_drills_with_session_uses_its_application(self) -> None:
        session = db.record_session(self.conn, "real", application_id=self.app)["session_id"]
        found = db.add_gap_drills(self.conn, ["Behavioural stories"], session)
        gap = self.conn.execute("SELECT * FROM gaps WHERE id = ?", (found["gaps"][0],)).fetchone()
        self.assertEqual((gap["application_id"], gap["session_id"], gap["session"]), (self.app, session, "real"))
        with self.assertRaises(LookupError):
            db.add_gap_drills(self.conn, ["x"], session_id=999)

    def test_list_gaps_and_sessions(self) -> None:
        db.record_session(self.conn, "mock", "2026-11-01", gaps=["A", "B"])
        db.record_session(self.conn, "real", "2026-11-05", self.app, scores={"design": 2})
        db.close_gap(self.conn, db.list_gaps(self.conn)[0]["id"])
        self.assertEqual([(g["gap"], g["drill_open"]) for g in db.list_gaps(self.conn)], [("B", 1)])
        self.assertEqual(len(db.list_gaps(self.conn, None)), 2)
        sessions = db.list_sessions(self.conn)
        self.assertEqual([(s["date"], s["company"]) for s in sessions], [("2026-11-05", "Initech"), ("2026-11-01", None)])

    def test_session_blocks_undo_interested(self) -> None:
        app = db.add_application(self.conn, "Globex", "Engineer", getjo_job_id="x:globex:1")
        db.record_session(self.conn, "mock", application_id=app)
        with self.assertRaises(ValueError):
            db.undo_interested(self.conn, "x:globex:1")

    def test_old_database_gets_session_column(self) -> None:
        path = Path(self._tmp.name) / "old.db"
        with sqlite3.connect(path) as old:
            old.execute("CREATE TABLE gaps (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, session TEXT, "
                        "gap TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open')")
            old.execute("INSERT INTO gaps (date, session, gap) VALUES ('2026-01-01', 'mock', 'Old gap')")
        old.close()
        conn = db.connect(path)
        self.addCleanup(conn.close)
        columns = {r[1] for r in conn.execute("PRAGMA table_info(gaps)")}
        self.assertTrue({"session_id", "application_id"} <= columns)
        found = db.record_session(conn, "mock", gaps=["old  gap"])
        self.assertEqual(found["new_gaps"], [])
        self.assertEqual(len(found["tasks"]), 1)


class SessionCliTest(IsolatedTestCase):
    def test_session_add_list_and_gap_close(self) -> None:
        code, out, err = self.run_cli("session", "add", "--kind", "mock", "--stage", "system_design",
                                      "--score", "system design=2", "--score", "communication=4",
                                      "--gap", "Capacity estimates", "--gap", "Caching", "--file", "mock-1.md",
                                      "--date", "2026-11-10", "--due", "+2")
        self.assertEqual(code, 0, err)
        self.assertIn("system design 2/5", out)
        self.assertIn("gaps: 2 (2 new)", out)
        self.assertEqual(out.count("[drill] Drill: "), 2)
        with closing_db(self.prep_db) as conn:
            session = db.list_sessions(conn)[0]
            self.assertEqual(session["file_path"], str((self.tmp / "private" / "sessions" / "mock-1.md").resolve()))
            gap_id = db.list_gaps(conn)[0]["id"]
        code, out, _ = self.run_cli("session", "list")
        self.assertIn("2026-11-10 mock system design", out)
        code, out, _ = self.run_cli("gap", "list")
        self.assertIn("Capacity estimates (mock)", out)
        self.assertIn("1 open drill", out)
        code, out, _ = self.run_cli("gap", "close", str(gap_id))
        self.assertEqual(code, 0)
        self.assertIn("1 drill task done", out)
        code, out, _ = self.run_cli("gap", "list", "--closed")
        self.assertIn("Capacity estimates", out)

    def test_gap_add_matches_open_gap(self) -> None:
        self.assertEqual(self.run_cli("gap", "add", "Graphs")[0], 0)
        code, out, _ = self.run_cli("gap", "add", "graphs", "Tries")
        self.assertEqual(code, 0)
        self.assertIn("gaps: 2 (1 new)", out)
        self.assertEqual(out.count("[drill]"), 1)

    def test_bad_input(self) -> None:
        code, _, err = self.run_cli("session", "add", "--kind", "mock", "--score", "coding=7")
        self.assertNotEqual(code, 0)
        code, _, err = self.run_cli("session", "add", "--kind", "mock", "--file", "/etc/passwd", "--gap", "x")
        self.assertEqual(code, 2)
        self.assertIn("private_dir", err)
        code, _, err = self.run_cli("session", "add", "--kind", "mock", "--file", "../../outside.md")
        self.assertEqual(code, 2)
        code, _, err = self.run_cli("gap", "close", "42")
        self.assertEqual(code, 2)
        with closing_db(self.prep_db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)


def closing_db(path: Path):
    from contextlib import closing
    return closing(db.connect(path))


if __name__ == "__main__":
    unittest.main()
