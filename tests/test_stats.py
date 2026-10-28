import tempfile
import unittest
from pathlib import Path

from prep import db, stats

TODAY = "2026-03-11"  # a Wednesday


class StatsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.conn = db.connect(Path(self._tmp.name) / "prep.db")
        self.addCleanup(self.conn.close)

    def add(self, *statuses: str, applied: str | None = "2026-03-01", tier: str | None = None) -> int:
        app_id = db.add_application(self.conn, "Acme", "Engineer", status=statuses[0],
                                    date_applied=applied, tier=tier)
        for status in statuses[1:]:
            db.update_application(self.conn, app_id, status=status)
        return app_id

    def stage(self, app_id: int) -> dict:
        return next(a for a in stats.application_stages(self.conn) if a["id"] == app_id)

    def test_applied_last_7_days(self) -> None:
        for day in ("2026-03-04", "2026-03-05", "2026-03-11", "2026-03-12"):
            self.add("applied", applied=day)
        self.add("to_apply", applied=None)
        # 03-05..03-11 inclusive: 03-04 is 7 days ago, 03-12 is in the future.
        self.assertEqual(stats.applied_last_7_days(self.conn, TODAY), 2)

    def test_replied_definition(self) -> None:
        cases = {
            ("applied",): False,
            ("applied", "no_response"): False,
            ("applied", "withdrawn"): False,
            ("applied", "rejected"): True,
            ("applied", "recruiter_call"): True,
            ("applied", "technical", "rejected"): True,
            ("applied", "recruiter_call", "withdrawn"): True,
            ("offer",): True,
        }
        for statuses, replied in cases.items():
            with self.subTest(statuses=statuses):
                self.assertEqual(self.stage(self.add(*statuses))["replied"], replied)
        self.assertFalse(self.stage(self.add("to_apply", applied=None))["submitted"])
        self.assertFalse(self.stage(self.add("to_apply", "withdrawn", applied=None))["submitted"])

    def test_rows_older_than_history_use_current_status(self) -> None:
        app_id = self.add("technical")
        self.conn.execute("DELETE FROM status_history")
        self.assertEqual(self.stage(app_id)["stage"], "technical")
        rejected = self.add("rejected", applied=None)
        self.conn.execute("DELETE FROM status_history")
        self.assertEqual((self.stage(rejected)["stage"], self.stage(rejected)["replied"]), ("applied", True))

    def test_reply_rate_and_warning(self) -> None:
        for _ in range(24):
            self.add("applied")
        self.add("applied", "rejected")
        self.add("to_apply", applied=None)  # not counted
        reply = stats.reply_rate(stats.application_stages(self.conn))
        self.assertEqual((reply["submitted"], reply["replied"]), (25, 1))
        self.assertAlmostEqual(reply["rate"], 0.04)
        self.assertTrue(reply["warning"])

    def test_no_warning_below_25_or_at_10_percent(self) -> None:
        for _ in range(20):
            self.add("applied")
        self.assertFalse(stats.reply_rate(stats.application_stages(self.conn))["warning"])  # only 20
        for _ in range(7):
            self.add("applied", "recruiter_call")
        reply = stats.reply_rate(stats.application_stages(self.conn))
        self.assertEqual(reply["submitted"], 27)
        self.assertFalse(reply["warning"])  # 7/27 ≈ 26%
        self.assertIsNone(stats.reply_rate([])["rate"])

    def test_conversion_is_cumulative(self) -> None:
        self.add("applied")
        self.add("applied", "recruiter_call", "rejected")
        self.add("applied", "technical")  # skipped recruiter_call: still counts as reached
        self.add("offer")
        self.add("to_apply", applied=None)
        rows = {stage: (n, share) for stage, n, share in stats.conversion(stats.application_stages(self.conn))}
        self.assertEqual(rows["applied"], (4, 1.0))
        self.assertEqual(rows["recruiter_call"], (3, 0.75))
        self.assertEqual(rows["technical"], (2, 0.5))
        self.assertEqual(rows["offer"], (1, 0.25))
        self.assertEqual([s for s, _, _ in stats.conversion([])], list(stats.FUNNEL))

    def test_counts_by_status_and_tier(self) -> None:
        self.add("applied", tier="A")
        self.add("applied", tier="A")
        self.add("rejected", tier=None)
        apps = stats.application_stages(self.conn)
        self.assertEqual(stats.counts_by_status(apps), [("applied", 2), ("rejected", 1)])
        self.assertEqual(stats.counts_by_tier(apps), [("A", 2), ("unset", 1)])

    def test_per_week(self) -> None:
        for day in ("2026-03-09", "2026-03-11", "2026-03-02", "2026-01-01"):
            self.add("applied", applied=day)
        weeks = stats.applications_per_week(self.conn, TODAY, weeks=3)
        self.assertEqual(weeks, [("2026-02-23", 0), ("2026-03-02", 1), ("2026-03-09", 2)])
        task = db.add_task(self.conn, "Drill")
        db.complete_task(self.conn, task)
        self.conn.execute("UPDATE tasks SET done_at = '2026-03-10T21:00:00Z'")
        self.assertEqual(stats.tasks_done_per_week(self.conn, TODAY, weeks=1), [("2026-03-09", 1)])


if __name__ == "__main__":
    unittest.main()
