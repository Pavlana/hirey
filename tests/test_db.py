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
        self.assertTrue({"applications", "interviews", "tasks", "gaps", "status_history", "sessions"} <= names)
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

    def test_tasks_due_include_application_and_offers(self) -> None:
        offer = db.add_application(self.conn, "A", "r", status="offer")
        old = db.add_task(self.conn, "Review the offer", "admin", "2026-03-01", offer)
        db.add_task(self.conn, "Later", "admin", "2026-03-11")
        general = db.add_task(self.conn, "General", "drill", TODAY)
        due = db.tasks_due(self.conn, TODAY)
        self.assertEqual([t["id"] for t in due], [old, general])
        self.assertEqual((due[0]["company"], due[0]["app_status"]), ("A", "offer"))
        self.assertIsNone(due[1]["company"])

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
        self.assertEqual(db.stage_tasks(self.conn, app_id, "to_apply", "2026-03-01"), [])  # the agent goes first
        self.assertEqual({r["kind"] for r in db.open_requests(self.conn, app_id)}, {"research", "cv_fit"})
        db.stage_tasks(self.conn, app_id, "applied", "2026-03-02")
        self.assertEqual(self.titles(app_id), [("Chase if no reply", "admin", "2026-03-09", "todo")])

    def test_every_stage(self) -> None:
        expected = {
            "recruiter_call": [("Prepare for the call (questions, pitch, availability)", "prep", "2026-03-12")],
            "technical": [("Coding practice", "drill", "2026-03-12"), ("Review the role's tech stack", "prep", "2026-03-12")],
            "system_design": [("System design practice", "drill", "2026-03-12")],
            "final": [("Prepare stories, company values, questions to ask", "prep", "2026-03-12")],
            "offer": [("Review the offer", "admin", "2026-03-12")],
            "rejected": [("Debrief — what to improve", "prep", "2026-03-13")],
            "no_response": [("Debrief — what to improve", "prep", "2026-03-13")],
            "withdrawn": [],
        }
        for stage, tasks in expected.items():
            with self.subTest(stage=stage):
                app_id, _ = db.record_application(self.conn, stage, "r", today=TODAY)
                self.assertTrue(db.move_application(self.conn, app_id, stage, TODAY))
                open_ = [t for t in self.titles(app_id) if t[3] == "todo"]
                self.assertEqual([t[:3] for t in open_], tasks)
                self.assertEqual([t[3] for t in self.titles(app_id) if t[0] == "Chase if no reply"], ["closed"])

    def test_move_with_interview_date(self) -> None:
        app_id, _ = db.record_application(self.conn, "Acme", "Engineer", today=TODAY)
        db.move_application(self.conn, app_id, "final", TODAY, "Onsite", "2026-03-20")
        self.assertIn(("Prepare stories, company values, questions to ask", "prep", "2026-03-19", "todo"),
                      self.titles(app_id))
        row = self.conn.execute("SELECT date, stage FROM interviews WHERE application_id = ?", (app_id,)).fetchone()
        self.assertEqual(tuple(row), ("2026-03-20", "final"))
        self.assertEqual(db.timeline(self.conn, app_id)[-1]["comment"], "Onsite")
        # an interview that is today or past: prep is due today, never in the past
        db.move_application(self.conn, app_id, "recruiter_call", TODAY, interview_date="2026-03-05")
        self.assertIn(("Prepare for the call (questions, pitch, availability)", "prep", TODAY, "todo"),
                      self.titles(app_id))
        with self.assertRaises(ValueError):
            db.move_application(self.conn, app_id, "technical", TODAY, interview_date="soon")

    def test_move_to_same_status_is_a_no_op(self) -> None:
        app_id, _ = db.record_application(self.conn, "Acme", "Engineer", today=TODAY)
        self.assertFalse(db.move_application(self.conn, app_id, "applied", TODAY))
        self.assertEqual(db.status_history(self.conn, app_id), ["applied"])
        with self.assertRaises(LookupError):
            db.move_application(self.conn, 999, "offer", TODAY)

    def test_move_back_creates_fresh_tasks(self) -> None:
        app_id, _ = db.record_application(self.conn, "Acme", "Engineer", today="2026-03-01")
        db.move_application(self.conn, app_id, "no_response", "2026-03-05")
        db.move_application(self.conn, app_id, "applied", TODAY)
        chase = [t for t in self.titles(app_id) if t[0] == "Chase if no reply"]
        self.assertEqual(chase, [("Chase if no reply", "admin", "2026-03-08", "closed"),
                                 ("Chase if no reply", "admin", "2026-03-17", "todo")])
        self.assertIn(("Debrief — what to improve", "prep", "2026-03-08", "closed"), self.titles(app_id))

    def test_board_move_from_interested_sets_date_applied(self) -> None:
        app_id, _ = db.record_application(self.conn, "Acme", "Engineer", status="to_apply", today=TODAY)
        db.move_application(self.conn, app_id, "recruiter_call", "2026-03-11")
        app = db.get_application(self.conn, app_id)
        self.assertEqual(app["date_applied"], "2026-03-11")
        self.assertNotIn("Apply", {t[0] for t in self.titles(app_id)})
        withdrawn, _ = db.record_application(self.conn, "B", "r", status="to_apply", today=TODAY)
        db.move_application(self.conn, withdrawn, "withdrawn", TODAY)
        self.assertIsNone(db.get_application(self.conn, withdrawn)["date_applied"])

    def test_hand_made_tasks_stay_open(self) -> None:
        app_id, _ = db.record_application(self.conn, "Acme", "Engineer", today=TODAY)
        mine = db.add_task(self.conn, "Ask a friend there", "admin", None, app_id)
        db.move_application(self.conn, app_id, "technical", TODAY)
        self.assertEqual(db.get_task(self.conn, mine)["status"], "todo")

    def test_next_action_follows_tasks(self) -> None:
        app_id, _ = db.record_application(self.conn, "Acme", "Engineer", today=TODAY)
        app = db.get_application(self.conn, app_id)
        self.assertEqual((app["next_action"], app["next_action_date"]), ("Chase if no reply", "2026-03-17"))
        sooner = db.add_task(self.conn, "Send portfolio", "admin", "2026-03-12", app_id)
        self.assertEqual(db.get_application(self.conn, app_id)["next_action"], "Send portfolio")
        db.complete_task(self.conn, sooner)
        self.assertEqual(db.get_application(self.conn, app_id)["next_action"], "Chase if no reply")
        db.reopen_task(self.conn, sooner)
        self.assertEqual(db.get_application(self.conn, app_id)["next_action"], "Send portfolio")
        db.move_application(self.conn, app_id, "withdrawn", TODAY)
        db.complete_task(self.conn, sooner)
        app = db.get_application(self.conn, app_id)
        self.assertEqual((app["next_action"], app["next_action_date"]), (None, None))

    def test_closed_tasks_are_not_done(self) -> None:
        app_id, _ = db.record_application(self.conn, "Acme", "Engineer", today=TODAY)
        db.move_application(self.conn, app_id, "recruiter_call", TODAY)
        self.assertEqual(db.recent_done_tasks(self.conn), [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'done'").fetchone()[0], 0)
        closed = next(t for t in db.list_tasks(self.conn, "closed"))
        db.reopen_task(self.conn, closed["id"])
        self.assertEqual(db.get_task(self.conn, closed["id"])["status"], "todo")


    def test_record_application_adds_tasks_on_stage_change_only(self) -> None:
        app_id, _ = db.record_application(self.conn, "Acme", "Engineer", link="https://x/1", today="2026-03-01")
        db.record_application(self.conn, "Acme", "Engineer", link="https://x/1", today="2026-03-05")
        self.assertEqual(self.titles(app_id), [("Chase if no reply", "admin", "2026-03-08", "todo")])
        db.update_application(self.conn, app_id, status="technical")
        db.record_application(self.conn, "Acme", "Engineer", link="https://x/1", status="to_apply")
        self.assertEqual(db.get_application(self.conn, app_id)["status"], "technical")
        self.assertEqual(len(self.titles(app_id)), 1)  # update_application alone runs no stage tasks

    def test_status_label(self) -> None:
        self.assertEqual(db.status_label("to_apply"), "interested")
        self.assertEqual(db.status_label("recruiter_call"), "recruiter call")


JOB = {"id": "gh:a:1", "company": "Acme", "title": "Engineer", "location": "London", "score": 8,
       "url": "https://example.com/1", "source": "gh"}


class NoResponseTest(DbTest):
    def applied_on(self, day: str, company: str = "Acme") -> int:
        app_id, _ = db.record_application(self.conn, company, "Engineer", date_applied=day, today=day)
        self.conn.execute("UPDATE status_history SET changed_at = ? WHERE application_id = ?",
                          (f"{day}T09:00:00Z", app_id))
        self.conn.commit()
        return app_id

    def test_moves_after_14_days(self) -> None:
        app_id = self.applied_on("2026-02-24")
        self.assertEqual(db.auto_no_response(self.conn, 14, "2026-03-09"), [])  # day 13
        self.assertEqual(db.get_application(self.conn, app_id)["status"], "applied")
        moved = db.auto_no_response(self.conn, 14, TODAY)  # day 14
        self.assertEqual(moved, [{"id": app_id, "company": "Acme", "role": "Engineer"}])
        self.assertEqual(db.get_application(self.conn, app_id)["status"], "no_response")
        self.assertEqual(db.timeline(self.conn, app_id)[-1]["comment"], "auto: no reply in 14 days")
        tasks = {t["title"]: t["status"] for t in db.list_tasks(self.conn, None)}
        self.assertEqual(tasks, {"Chase if no reply": "closed", "Debrief — what to improve": "todo"})
        self.assertEqual(db.auto_no_response(self.conn, 14, TODAY), [])  # a second run is a no-op
        self.assertEqual(db.status_history(self.conn, app_id), ["applied", "no_response"])

    def test_counts_from_last_stage_change(self) -> None:
        app_id = self.applied_on("2026-01-01")
        db.move_application(self.conn, app_id, "recruiter_call", "2026-03-01")
        db.move_application(self.conn, app_id, "applied", "2026-03-01")  # moved back by hand
        self.conn.execute("UPDATE status_history SET changed_at = '2026-03-01T09:00:00Z' WHERE application_id = ? "
                          "AND changed_at > '2026-01-02'", (app_id,))
        self.conn.commit()
        self.assertEqual(db.auto_no_response(self.conn, 14, TODAY), [])
        self.assertEqual(len(db.auto_no_response(self.conn, 14, "2026-03-15")), 1)

    def test_rows_without_history_use_date_applied(self) -> None:
        app_id = db.add_application(self.conn, "Old", "r", status="applied", date_applied="2026-02-01")
        self.conn.execute("DELETE FROM status_history")
        self.conn.commit()
        self.assertEqual(db.last_change_day(self.conn, app_id), "2026-02-01")
        self.assertEqual([m["id"] for m in db.auto_no_response(self.conn, 14, TODAY)], [app_id])

    def test_only_applied_moves(self) -> None:
        for status in ("to_apply", "recruiter_call", "offer", "rejected"):
            db.add_application(self.conn, status, "r", status=status, date_applied="2026-01-01")
        self.conn.execute("UPDATE status_history SET changed_at = '2026-01-01T09:00:00Z'")
        self.conn.commit()
        self.assertEqual(db.auto_no_response(self.conn, 14, TODAY), [])

    def test_custom_days_and_recent_moves(self) -> None:
        app_id = self.applied_on("2026-03-03")
        self.assertEqual(db.auto_no_response(self.conn, 14, TODAY), [])
        self.assertEqual(len(db.auto_no_response(self.conn, 7, TODAY)), 1)
        self.assertEqual(db.timeline(self.conn, app_id)[-1]["comment"], "auto: no reply in 7 days")
        recent = db.recent_auto_moves(self.conn, db.today_iso())
        self.assertEqual([(r["id"], r["company"]) for r in recent], [(app_id, "Acme")])
        far = (date.today() + timedelta(days=30)).isoformat()
        self.assertEqual(db.recent_auto_moves(self.conn, far), [])

    def test_move_by_another_request_wins(self) -> None:
        """The check reads applied, then another request moves the card before the check moves it."""
        app_id = self.applied_on("2026-01-01")
        other = db.connect(self.path)
        self.addCleanup(other.close)
        real = db.last_change_day

        def moved_meanwhile(conn, i):
            day = real(conn, i)
            db.move_application(other, i, "recruiter_call", TODAY)
            return day

        with mock.patch.object(db, "last_change_day", moved_meanwhile):
            self.assertEqual(db.auto_no_response(self.conn, 14, TODAY), [])
        self.assertEqual(db.status_history(self.conn, app_id), ["applied", "recruiter_call"])
        self.assertEqual(db.get_application(self.conn, app_id)["status"], "recruiter_call")

    def test_conditional_update_refuses_a_stale_read(self) -> None:
        """Another request moves the row between move_application's SELECT and its UPDATE."""
        app_id = self.applied_on("2026-01-01")
        other = db.connect(self.path)
        self.addCleanup(other.close)
        conn = self.conn

        class Interleaved:
            def __getattr__(self, name):
                return getattr(conn, name)

            def execute(self, sql, *args):
                cursor = conn.execute(sql, *args)
                if sql.startswith("SELECT status, date_applied"):
                    row = cursor.fetchone()
                    db.move_application(other, app_id, "rejected", TODAY)
                    return mock.Mock(fetchone=lambda: row)
                return cursor

        self.assertFalse(db.move_application(Interleaved(), app_id, "no_response", TODAY))
        self.assertEqual(db.status_history(self.conn, app_id), ["applied", "rejected"])


class CommentsTest(DbTest):
    def test_timeline_and_counts(self) -> None:
        stamps = iter(f"2026-03-10T09:00:0{n}Z" for n in range(9))  # one second apart, in order
        with mock.patch.object(db, "now_iso", lambda: next(stamps)):
            self._events()
        app_id = 1
        events = db.timeline(self.conn, app_id)
        self.assertEqual([(e["status"], e["comment"]) for e in events], [
            ("applied", None), (None, "Second interview booked"), ("technical", "Take-home first"),
            ("system_design", None)])
        self.assertEqual(db.comment_counts(self.conn), {app_id: 2})
        with self.assertRaises(ValueError):
            db.add_comment(self.conn, app_id, "   ")
        with self.assertRaises(LookupError):
            db.add_comment(self.conn, 999, "x")

    def _events(self) -> None:
        app_id, _ = db.record_application(self.conn, "Acme", "Engineer", today=TODAY)
        db.add_comment(self.conn, app_id, "  Second interview booked  ")
        db.move_application(self.conn, app_id, "technical", TODAY, "Take-home first")
        db.move_application(self.conn, app_id, "system_design", TODAY)


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


class MigrationTest(unittest.TestCase):
    def test_adds_columns_and_backfills_stages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prep.db"
            old = sqlite3.connect(path)
            old.executescript("""
                CREATE TABLE applications (id INTEGER PRIMARY KEY AUTOINCREMENT, getjo_job_id TEXT, company TEXT NOT NULL,
                    role TEXT NOT NULL, link TEXT, tier TEXT, type TEXT, source TEXT, date_applied TEXT,
                    status TEXT NOT NULL DEFAULT 'to_apply', last_contact TEXT, next_action TEXT, next_action_date TEXT,
                    puzzle_round TEXT NOT NULL DEFAULT 'unknown', pay TEXT, contact TEXT, notes TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'admin',
                    due_date TEXT, status TEXT NOT NULL DEFAULT 'todo', done_at TEXT, application_id INTEGER,
                    gap_id INTEGER, created_at TEXT NOT NULL);
                CREATE TABLE status_history (id INTEGER PRIMARY KEY AUTOINCREMENT, application_id INTEGER NOT NULL,
                    status TEXT NOT NULL, changed_at TEXT NOT NULL);
                INSERT INTO applications (company, role, status, next_action, next_action_date, created_at, updated_at)
                    VALUES ('Acme', 'r', 'applied', 'Email the hiring manager', '2026-03-12',
                            '2026-03-01T00:00:00Z', '2026-03-01T00:00:00Z');
                INSERT INTO applications (company, role, status, next_action, created_at, updated_at)
                    VALUES ('Gone', 'r', 'rejected', 'Ask for feedback', '2026-03-01T00:00:00Z', '2026-03-01T00:00:00Z');
                INSERT INTO status_history (application_id, status, changed_at) VALUES (1, 'applied', '2026-03-01T00:00:00Z');
                INSERT INTO tasks (title, kind, application_id, created_at) VALUES ('Chase if no reply', 'admin', 1, 'x');
                INSERT INTO tasks (title, kind, application_id, created_at) VALUES ('My own task', 'admin', 1, 'x');
                INSERT INTO tasks (title, kind, application_id, created_at) VALUES ('Research the company', 'prep', 1, 'x');
                INSERT INTO tasks (title, kind, created_at) VALUES ('Chase if no reply', 'admin', 'x');
            """)
            old.close()
            conn = db.connect(path)
            self.addCleanup(conn.close)
            rows = [tuple(r) for r in conn.execute("SELECT title, stage, due_date, application_id FROM tasks ORDER BY id")]
            self.assertEqual(rows, [("Chase if no reply", "applied", None, 1), ("My own task", None, None, 1),
                                    ("Research the company", "to_apply", None, 1),  # retired auto task
                                    ("Chase if no reply", None, None, None),
                                    ("Email the hiring manager", None, "2026-03-12", 1)])  # typed next action
            self.assertEqual(db.get_application(conn, 1)["next_action"], "Email the hiring manager")
            again = db.connect(path)  # runs once: nothing added on the next connect
            again.close()
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 5)
            self.assertIsNone(conn.execute("SELECT comment FROM status_history").fetchone()[0])
            db.move_application(conn, 1, "recruiter_call", TODAY)
            status = [r[0] for r in conn.execute("SELECT status FROM tasks ORDER BY id")]
            self.assertEqual(status[:4], ["closed", "todo", "closed", "todo"])


class ReviewTaskTest(DbTest):
    """Interested adds no Apply task; "Review research and apply" follows once both results are in."""
    PROFILE = {"company": "Acme", "file_path": "/x/companies/acme.md", "researched_at": "2026-03-09",
               "suggested_tier": "A"}

    def open_titles(self, app_id: int) -> list[tuple]:
        return [(t["title"], t["kind"], t["due_date"], t["stage"])
                for t in db.list_tasks(self.conn, "todo") if t["application_id"] == app_id]

    def interested(self, company: str = "Acme") -> int:
        app_id, _ = db.record_application(self.conn, company, "Engineer", status="to_apply", today=TODAY)
        return app_id

    def test_created_once_both_results_are_in_either_order(self) -> None:
        review = [("Review research and apply", "apply", "2026-03-12", "to_apply")]
        first = self.interested()
        self.assertEqual(self.open_titles(first), [])
        db.save_company_profile(self.conn, "acme", self.PROFILE, today=TODAY)
        self.assertEqual(self.open_titles(first), [], "only research is in")
        db.save_cv_fit(self.conn, first, 4, [], "/x/fit/1-acme.md", "2026-01-01")  # an old checked_at
        self.assertEqual([t[:2] for t in self.open_titles(first)], [review[0][:2]])
        # the other order: CV fit first, then the company save adds it
        second = self.interested("Globex")
        db.save_cv_fit(self.conn, second, 3, [], "/x/fit/2-globex.md", TODAY)
        self.assertEqual(self.open_titles(second), [])
        db.save_company_profile(self.conn, "globex", {**self.PROFILE, "company": "Globex"}, today=TODAY)
        self.assertEqual(self.open_titles(second), review)

    def test_redo_adds_no_second_one_while_open(self) -> None:
        app_id = self.interested()
        db.save_company_profile(self.conn, "acme", self.PROFILE, today=TODAY)
        db.save_cv_fit(self.conn, app_id, 4, [], "/x/fit/1-acme.md", TODAY)
        db.queue_request(self.conn, app_id, "research", force=True)
        db.save_company_profile(self.conn, "acme", self.PROFILE, today=TODAY)
        db.save_cv_fit(self.conn, app_id, 5, [], "/x/fit/1-acme.md", TODAY)
        self.assertEqual(len(self.open_titles(app_id)), 1)

    def test_not_after_moving_on_and_done_on_applied(self) -> None:
        later = self.interested()
        db.move_application(self.conn, later, "applied", TODAY)
        db.save_company_profile(self.conn, "acme", self.PROFILE, today=TODAY)
        db.save_cv_fit(self.conn, later, 4, [], "/x/fit/1-acme.md", TODAY)
        self.assertNotIn("Review research and apply", [t[0] for t in self.open_titles(later)])
        # a second role at the same company gets it, and Applied marks it done
        app_id = self.interested()
        db.save_cv_fit(self.conn, app_id, 4, [], "/x/fit/2-acme.md", TODAY)
        self.assertEqual(self.open_titles(app_id)[0][0], "Review research and apply")
        db.move_application(self.conn, app_id, "applied", TODAY)
        done = [t["status"] for t in db.list_tasks(self.conn, None)
                if t["application_id"] == app_id and t["title"] == "Review research and apply"]
        self.assertEqual(done, ["done"])

    def test_old_apply_task_stays_and_review_is_added(self) -> None:
        app_id = self.interested()
        old = db.add_task(self.conn, "Apply", "apply", "2026-03-13", app_id, stage="to_apply")  # made before
        db.save_company_profile(self.conn, "acme", self.PROFILE, today=TODAY)
        db.save_cv_fit(self.conn, app_id, 4, [], "/x/fit/1-acme.md", TODAY)
        self.assertEqual({t[0] for t in self.open_titles(app_id)}, {"Review research and apply", "Apply"})
        db.move_application(self.conn, app_id, "applied", TODAY)
        self.assertEqual(db.get_task(self.conn, old)["status"], "done")

    def test_backfill_still_tags_old_apply_tasks(self) -> None:
        self.assertIn(("Apply", "apply", 3), db.RETIRED_STAGE_TASKS["to_apply"])
        self.assertEqual(db.STAGE_TASKS["to_apply"], ())


class InterviewDateTest(DbTest):
    def test_reschedule_moves_the_stage_tasks(self) -> None:
        app_id, _ = db.record_application(self.conn, "Acme", "Engineer", today=TODAY)
        db.move_application(self.conn, app_id, "technical", TODAY, interview_date="2026-03-20")
        mine = db.add_task(self.conn, "Read their blog", "prep", "2026-03-15", app_id)  # hand-made: untouched
        self.assertEqual(db.set_interview_date(self.conn, app_id, "technical", "2026-03-25", TODAY), (2, "2026-03-24"))
        due = {t["title"]: t["due_date"] for t in db.list_tasks(self.conn) if t["application_id"] == app_id}
        self.assertEqual(due, {"Coding practice": "2026-03-24", "Review the role's tech stack": "2026-03-24",
                               "Read their blog": "2026-03-15"})
        self.assertEqual(db.get_application(self.conn, app_id)["next_action"], "Read their blog")
        # the same date again moves nothing; a date already past makes them due today
        self.assertEqual(db.set_interview_date(self.conn, app_id, "technical", "2026-03-25", TODAY), (0, None))
        self.assertEqual(db.set_interview_date(self.conn, app_id, "technical", "2026-03-01", TODAY), (2, TODAY))
        self.assertEqual(db.get_task(self.conn, mine)["due_date"], "2026-03-15")
        with self.assertRaises(ValueError):
            db.set_interview_date(self.conn, app_id, "applied", "2026-03-25", TODAY)


class AgentFirstTest(DbTest):
    """Skip, Undo of Mark applied, comment amend/remove and task edit/delete (the agent's commands)."""
    JOB = {"id": "lever:globex-labs:abc", "company": "Globex Labs", "title": "Engineer", "location": "London",
           "score": 8, "url": "https://example.com/j", "source": "lever"}

    def interested(self) -> int:
        app_id, _ = db.record_application(self.conn, "Globex Labs", "Engineer", status="to_apply",
                                          getjo_job_id=self.JOB["id"], link=self.JOB["url"], today=TODAY)
        db.record_decision(self.conn, self.JOB, "interested")
        return app_id

    def test_skip_closes_it_and_marks_the_job_skipped(self) -> None:
        app_id = self.interested()
        db.add_task(self.conn, "Hand-made", application_id=app_id)
        db.skip_application(self.conn, app_id, "location", " On-site only ", TODAY)
        app = db.get_application(self.conn, app_id)
        self.assertEqual(app["status"], "skipped")
        self.assertIn("skipped", db.CLOSED_STATUSES)
        self.assertEqual(db.timeline(self.conn, app_id)[-1]["comment"], "skipped: location — On-site only")
        self.assertEqual(db.app_tasks(self.conn).get(app_id), None, "every open task closed")
        self.assertEqual(db.open_requests(self.conn, app_id), [], "queued research closed")
        decision = db.job_decisions(self.conn)[self.JOB["id"]]
        self.assertEqual((decision["decision"], decision["reason"], decision["comment"], decision["location"]),
                         ("skipped", "location", "On-site only", "London"))  # the snapshot is kept
        with self.assertRaisesRegex(ValueError, "Only an Interested"):
            db.skip_application(self.conn, app_id, "location")
        with self.assertRaises(ValueError):
            db.skip_application(self.conn, self.interested_other(), "nope")

    def interested_other(self) -> int:
        return db.record_application(self.conn, "Initech", "Engineer", status="to_apply", today=TODAY)[0]

    def test_skip_without_a_getjo_job_or_a_decision_row(self) -> None:
        app_id = self.interested_other()
        db.skip_application(self.conn, app_id, "stack")
        self.assertEqual(db.get_application(self.conn, app_id)["status"], "skipped")
        app_id, _ = db.record_application(self.conn, "Hooli", "Engineer", status="to_apply",
                                          getjo_job_id="ashby:hooli:1", today=TODAY)
        db.skip_application(self.conn, app_id, "pay")
        self.assertEqual(db.job_decisions(self.conn)["ashby:hooli:1"]["decision"], "skipped")

    def test_undo_skip_on_job_offers_reopens_the_application(self) -> None:
        app_id = self.interested()
        db.skip_application(self.conn, app_id, "location", today=TODAY)
        db.undo_skip(self.conn, self.JOB["id"], TODAY)
        self.assertEqual(db.get_application(self.conn, app_id)["status"], "to_apply")
        self.assertEqual(db.job_decisions(self.conn)[self.JOB["id"]]["decision"], "interested")

    def test_undo_applied(self) -> None:
        app_id = self.interested()
        review = db.add_task(self.conn, db.REVIEW_TASK[0], "apply", None, app_id, stage="to_apply")
        self.assertTrue(db.move_application(self.conn, app_id, "applied", TODAY))
        db.undo_applied(self.conn, app_id)
        app = db.get_application(self.conn, app_id)
        self.assertEqual((app["status"], app["date_applied"]), ("to_apply", None))
        self.assertEqual(db.status_history(self.conn, app_id), ["to_apply"])
        self.assertEqual([t["id"] for t in db.app_tasks(self.conn)[app_id]], [review], "Chase gone, review reopened")
        with self.assertRaisesRegex(ValueError, "moved on"):
            db.undo_applied(self.conn, app_id)  # not applied any more
        db.move_application(self.conn, app_id, "applied", TODAY)
        db.move_application(self.conn, app_id, "recruiter_call", TODAY)
        with self.assertRaises(ValueError):
            db.undo_applied(self.conn, app_id)

    def test_undo_applied_keeps_an_earlier_send_date(self) -> None:
        app_id = db.add_application(self.conn, "Acme", "Engineer", status="applied", date_applied="2026-03-01")
        db.move_application(self.conn, app_id, "to_apply", TODAY)
        db.move_application(self.conn, app_id, "applied", TODAY)
        db.undo_applied(self.conn, app_id)
        self.assertEqual(db.get_application(self.conn, app_id)["date_applied"], "2026-03-01")

    def test_comments_list_amend_remove(self) -> None:
        app_id = db.add_application(self.conn, "Acme", "Engineer", status="applied")
        db.move_application(self.conn, app_id, "technical", TODAY, comment="Coding on Tue")
        free = db.add_comment(self.conn, app_id, "Referral")
        found = db.comments(self.conn, app_id)
        self.assertEqual({c["ref"] for c in found}, {f"c{free}", found[-1]["ref"]})
        stage_ref = next(c["ref"] for c in found if c["status"])
        self.assertTrue(stage_ref.startswith("h"))
        self.assertEqual(db.latest_comments(self.conn)[app_id][1], 2)
        db.amend_comment(self.conn, f"c{free}", "Referral from Sam")
        db.amend_comment(self.conn, stage_ref, "Coding on Wed")
        self.assertEqual(sorted(c["body"] for c in db.comments(self.conn, app_id)), ["Coding on Wed", "Referral from Sam"])
        db.remove_comment(self.conn, stage_ref)
        db.remove_comment(self.conn, f"c{free}")
        self.assertEqual(db.comments(self.conn, app_id), [])
        self.assertEqual(db.status_history(self.conn, app_id)[-1], "technical", "the stage change stays")
        for bad in ("x1", "c", f"c{free}"):
            with self.subTest(ref=bad), self.assertRaises((ValueError, LookupError)):
                db.remove_comment(self.conn, bad)
        with self.assertRaises(ValueError):
            db.amend_comment(self.conn, stage_ref, " ")

    def test_edit_and_delete_task(self) -> None:
        app_id = db.add_application(self.conn, "Acme", "Engineer", status="applied")
        task = db.add_task(self.conn, "Call prep", "prep", "2026-03-12", app_id)
        db.edit_task(self.conn, task, title="Call prep with Sam", due_date="2026-03-13")
        self.assertEqual((db.get_task(self.conn, task)["title"], db.get_application(self.conn, app_id)["next_action_date"]),
                         ("Call prep with Sam", "2026-03-13"))
        db.edit_task(self.conn, task, clear_due=True)
        self.assertIsNone(db.get_task(self.conn, task)["due_date"])
        with self.assertRaises(ValueError):
            db.edit_task(self.conn, task, due_date="Friday")
        db.delete_task(self.conn, task)
        self.assertIsNone(db.get_task(self.conn, task))
        self.assertIsNone(db.get_application(self.conn, app_id)["next_action"])
        with self.assertRaises(LookupError):
            db.delete_task(self.conn, task)
