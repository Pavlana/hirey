import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from prep import db

TODAY = "2026-03-10"


class DbTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "sub" / "prep.db"
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)

    def test_creates_tables(self) -> None:
        names = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"applications", "interviews", "tasks", "gaps", "status_history"} <= names)
        self.assertNotIn("sessions", names)
        self.assertNotIn("cards", names)

    def test_connect_is_idempotent(self) -> None:
        db.add_application(self.conn, "Acme", "Engineer")
        again = db.connect(self.path)
        self.addCleanup(again.close)
        self.assertEqual(len(db.list_applications(again)), 1)

    def test_add_application_defaults(self) -> None:
        app = db.get_application(self.conn, db.add_application(self.conn, "Acme", "Engineer"))
        self.assertEqual(app["status"], "to_apply")
        self.assertEqual(app["puzzle_round"], "unknown")
        self.assertTrue(app["created_at"].endswith("Z"))
        self.assertEqual(app["created_at"], app["updated_at"])

    def test_invalid_values_rejected(self) -> None:
        with self.assertRaises(ValueError):
            db.add_application(self.conn, "Acme", "Engineer", status="hired")
        with self.assertRaises(ValueError):
            db.add_application(self.conn, "Acme", "Engineer", tier="D")
        with self.assertRaises(ValueError):
            db.add_application(self.conn, "Acme", "Engineer", next_action_date="next week")
        with self.assertRaises(ValueError):
            db.add_application(self.conn, "Acme", "Engineer", salary="x")
        with self.assertRaises(ValueError):
            db.add_task(self.conn, "x", kind="chores")

    def test_update_application(self) -> None:
        app_id = db.add_application(self.conn, "Acme", "Engineer", next_action="Call", next_action_date=TODAY)
        db.update_application(self.conn, app_id, status="technical", next_action=None)
        app = db.get_application(self.conn, app_id)
        self.assertEqual(app["status"], "technical")
        self.assertIsNone(app["next_action"])
        with self.assertRaises(LookupError):
            db.update_application(self.conn, 999, status="applied")

    def test_overdue_actions(self) -> None:
        due = db.add_application(self.conn, "A", "r", next_action="x", next_action_date="2026-03-01")
        db.add_application(self.conn, "B", "r", next_action="x", next_action_date="2026-03-11")
        db.add_application(self.conn, "C", "r", status="rejected", next_action="x", next_action_date="2026-03-01")
        today = db.add_application(self.conn, "D", "r", next_action="x", next_action_date=TODAY)
        self.assertEqual([a["id"] for a in db.overdue_actions(self.conn, TODAY)], [due, today])

    def test_linked_job_ids(self) -> None:
        db.add_application(self.conn, "A", "r", getjo_job_id="gh:a:1")
        db.add_application(self.conn, "B", "r")
        self.assertEqual(db.linked_job_ids(self.conn), {"gh:a:1"})
        self.assertEqual(db.find_by_job(self.conn, "gh:a:1")["company"], "A")

    def test_tasks(self) -> None:
        overdue = db.add_task(self.conn, "old", "prep", "2026-03-01")
        db.add_task(self.conn, "later", "drill", "2026-04-01")
        undated = db.add_task(self.conn, "whenever")
        self.assertEqual([t["id"] for t in db.tasks_due(self.conn, TODAY)], [overdue])
        self.assertEqual(db.list_tasks(self.conn)[-1]["id"], undated)

        db.complete_task(self.conn, overdue)
        self.assertEqual(db.tasks_due(self.conn, TODAY), [])
        done = db.list_tasks(self.conn, "done")
        self.assertEqual(done[0]["id"], overdue)
        self.assertIsNotNone(done[0]["done_at"])
        with self.assertRaises(LookupError):
            db.complete_task(self.conn, 999)

    def test_reopen_task(self) -> None:
        task_id = db.add_task(self.conn, "undo me")
        db.complete_task(self.conn, task_id)
        db.reopen_task(self.conn, task_id)
        task = db.get_task(self.conn, task_id)
        self.assertEqual((task["status"], task["done_at"]), ("todo", None))
        db.reopen_task(self.conn, task_id)  # already open: no-op
        with self.assertRaises(LookupError):
            db.reopen_task(self.conn, 999)
        self.assertIsNone(db.get_task(self.conn, 999))

    def test_interviews_and_gaps(self) -> None:
        app_id = db.add_application(self.conn, "A", "r")
        db.add_interview(self.conn, app_id, TODAY, stage="technical")
        gap_id = db.add_gap(self.conn, "explain DNS", TODAY, "mock")
        task_id = db.add_task(self.conn, "drill DNS", "drill", gap_id=gap_id)
        db.close_gap(self.conn, gap_id)
        self.assertEqual(self.conn.execute("SELECT status FROM gaps").fetchone()[0], "closed")
        self.assertEqual(self.conn.execute("SELECT gap_id FROM tasks WHERE id = ?", (task_id,)).fetchone()[0], gap_id)
        with self.assertRaises(sqlite3.IntegrityError):
            db.add_interview(self.conn, 999, TODAY)

    def test_missing_columns_are_added(self) -> None:
        with mock.patch.dict(db.MIGRATIONS, {"tasks": {"estimate": "INTEGER"}}):
            conn = db.connect(self.path)
            self.addCleanup(conn.close)
            columns = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        self.assertIn("estimate", columns)

    def test_status_history(self) -> None:
        app_id = db.add_application(self.conn, "Acme", "Engineer", status="applied")
        db.update_application(self.conn, app_id, status="applied", next_action="Chase")  # unchanged: no row
        db.update_application(self.conn, app_id, status="technical")
        db.update_application(self.conn, app_id, notes="went well")
        self.assertEqual(db.status_history(self.conn, app_id), ["applied", "technical"])
        default = db.add_application(self.conn, "Globex", "Engineer")
        self.assertEqual(db.status_history(self.conn, default), ["to_apply"])

    def test_record_application_reuses_and_never_moves_back(self) -> None:
        app_id, created = db.record_application(self.conn, "Acme", "Engineer", getjo_job_id="g:acme:1",
                                                link="https://example.com/1")
        self.assertTrue(created)
        app = db.get_application(self.conn, app_id)
        self.assertEqual((app["status"], app["date_applied"]), ("applied", db.today_iso()))
        db.update_application(self.conn, app_id, status="technical", date_applied="2026-01-02")
        again, created = db.record_application(self.conn, "Acme", "Engineer", getjo_job_id="g:acme:1",
                                               next_action="Prepare")
        self.assertEqual((again, created), (app_id, False))
        app = db.get_application(self.conn, app_id)
        self.assertEqual((app["status"], app["date_applied"], app["next_action"]),
                         ("technical", "2026-01-02", "Prepare"))
        by_link, _ = db.record_application(self.conn, "Acme", "Engineer", link="https://example.com/1")
        self.assertEqual(by_link, app_id)

    def test_recent_done_tasks(self) -> None:
        first = db.add_task(self.conn, "First")
        second = db.add_task(self.conn, "Second")
        db.add_task(self.conn, "Open")
        db.complete_task(self.conn, first)
        db.complete_task(self.conn, second)
        self.conn.execute("UPDATE tasks SET done_at = '2026-01-01T00:00:00Z' WHERE id = ?", (first,))
        self.assertEqual([t["title"] for t in db.recent_done_tasks(self.conn)], ["Second", "First"])
        self.assertEqual(len(db.recent_done_tasks(self.conn, 1)), 1)

    def test_today_iso(self) -> None:
        self.assertEqual(db.today_iso(), date.today().isoformat())
        self.assertLess(db.today_iso(), (date.today() + timedelta(days=1)).isoformat())


if __name__ == "__main__":
    unittest.main()


class StageTasksTest(DbTest):
    def titles(self, app_id: int) -> list[tuple]:
        return [(t["title"], t["kind"], t["due_date"], t["status"])
                for t in db.list_tasks(self.conn, None) if t["application_id"] == app_id]

    def test_stage_tasks(self) -> None:
        app_id = db.add_application(self.conn, "Acme", "Engineer")
        self.assertEqual(len(db.stage_tasks(self.conn, app_id, "to_apply", "2026-03-01")), 3)
        self.assertEqual(db.stage_tasks(self.conn, app_id, "to_apply", "2026-03-02"), [])  # idempotent
        self.assertEqual(db.stage_tasks(self.conn, app_id, "technical", "2026-03-02"), [])  # no tasks yet
        db.stage_tasks(self.conn, app_id, "applied", "2026-03-02")
        self.assertEqual(self.titles(app_id), [
            ("Research the company", "prep", "2026-03-03", "todo"),
            ("Check CV fit / tailor CV", "prep", "2026-03-03", "todo"),
            ("Apply", "apply", "2026-03-04", "done"),
            ("Chase if no reply", "admin", "2026-03-09", "todo"),
        ])

    def test_record_application_adds_tasks_on_stage_change_only(self) -> None:
        app_id, _ = db.record_application(self.conn, "Acme", "Engineer", link="https://x/1", today="2026-03-01")
        db.record_application(self.conn, "Acme", "Engineer", link="https://x/1", today="2026-03-05")
        self.assertEqual(self.titles(app_id), [("Chase if no reply", "admin", "2026-03-08", "todo")])
        db.update_application(self.conn, app_id, status="technical")
        db.record_application(self.conn, "Acme", "Engineer", link="https://x/1", status="to_apply")
        self.assertEqual(db.get_application(self.conn, app_id)["status"], "technical")
        self.assertEqual(len(self.titles(app_id)), 1)

    def test_status_label(self) -> None:
        self.assertEqual(db.status_label("to_apply"), "interested")
        self.assertEqual(db.status_label("recruiter_call"), "recruiter call")


JOB = {"id": "gh:a:1", "company": "Acme", "title": "Engineer", "location": "London", "score": 8,
       "url": "https://example.com/1", "source": "gh"}


class JobDecisionsTest(DbTest):
    def test_skip_needs_known_reason(self) -> None:
        for reason in (None, "", "boring"):
            with self.subTest(reason=reason), self.assertRaises(ValueError):
                db.record_decision(self.conn, JOB, "skipped", reason)
        with self.assertRaises(ValueError):
            db.record_decision(self.conn, JOB, "maybe")

    def test_skip_snapshot_and_undo(self) -> None:
        db.record_decision(self.conn, JOB, "skipped", "stack", "No Go jobs")
        d = db.job_decisions(self.conn)["gh:a:1"]
        self.assertEqual((d["decision"], d["reason"], d["comment"]), ("skipped", "stack", "No Go jobs"))
        self.assertEqual((d["company"], d["title"], d["location"], d["score"]), ("Acme", "Engineer", "London", 8))
        self.assertEqual(db.excluded_job_ids(self.conn), {"gh:a:1"})
        db.undo_skip(self.conn, "gh:a:1")
        self.assertEqual(db.job_decisions(self.conn), {})
        with self.assertRaises(LookupError):
            db.undo_skip(self.conn, "gh:a:1")

    def test_applied_replaces_skip_and_cannot_be_undone(self) -> None:
        db.record_decision(self.conn, JOB, "skipped", "pay")
        db.record_decision(self.conn, JOB, "applied", "pay")
        d = db.job_decisions(self.conn)["gh:a:1"]
        self.assertEqual((d["decision"], d["reason"]), ("applied", None))
        with self.assertRaises(ValueError):
            db.undo_skip(self.conn, "gh:a:1")

    def test_linked_application_without_a_row_counts_as_applied(self) -> None:
        db.add_application(self.conn, "Globex", "SRE", getjo_job_id="gh:g:2", status="applied",
                           date_applied="2026-03-01")
        d = db.job_decisions(self.conn)["gh:g:2"]
        self.assertEqual((d["decision"], d["decided_at"], d["company"], d["title"]),
                         ("applied", "2026-03-01", "Globex", "SRE"))
        self.assertEqual(db.excluded_job_ids(self.conn), {"gh:g:2"})

    def test_stored_interested_follows_the_application(self) -> None:
        app_id, _ = db.record_application(self.conn, "Acme", "Engineer", getjo_job_id="gh:a:1", status="to_apply")
        db.record_decision(self.conn, JOB, "interested")
        self.assertEqual(db.job_decisions(self.conn)["gh:a:1"]["decision"], "interested")
        db.update_application(self.conn, app_id, status="applied")
        self.assertEqual(db.job_decisions(self.conn)["gh:a:1"]["decision"], "applied")
        self.assertEqual(len(db.job_decisions(self.conn)), 1)

    def test_to_apply_reads_as_interested_until_applied(self) -> None:
        app_id = db.add_application(self.conn, "Acme", "Engineer", getjo_job_id="gh:a:1")  # to_apply
        self.assertEqual(db.job_decisions(self.conn)["gh:a:1"]["decision"], "interested")
        db.record_decision(self.conn, JOB, "applied")
        self.assertEqual(db.job_decisions(self.conn)["gh:a:1"]["decision"], "interested")
        db.update_application(self.conn, app_id, status="applied")
        self.assertEqual(db.job_decisions(self.conn)["gh:a:1"]["decision"], "applied")
