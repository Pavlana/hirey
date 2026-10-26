import os
import unittest
from contextlib import closing
from datetime import date, timedelta
from unittest import mock

from prep import cli, db
from tests.helpers import IsolatedTestCase


def days(n: int) -> str:
    return (date.today() + timedelta(days=n)).isoformat()


class TodayTest(IsolatedTestCase):
    def test_empty_db_lists_matches(self) -> None:
        code, out, _ = self.run_cli("today")
        self.assertEqual(code, 0)
        self.assertIn("Next actions due (0)", out)
        self.assertIn("matches ≥7 not yet in applications (3)", out)
        self.assertIn("Initech Cloud — DevOps Engineer", out)
        self.assertNotIn("Hooli Data", out)  # score 6

    def test_overdue_tasks_and_linked_matches(self) -> None:
        with closing(db.connect(self.prep_db)) as conn:
            db.add_application(conn, "Acme Rockets", "Platform Engineer", getjo_job_id="greenhouse:acme-rockets:1001",
                               status="applied", next_action="Chase recruiter", next_action_date=days(-1))
            db.add_task(conn, "Revise TCP", "prep", days(0))
            db.add_task(conn, "Future task", "prep", days(3))
        code, out, _ = self.run_cli("today", "--limit", "1")
        self.assertEqual(code, 0)
        self.assertIn("Chase recruiter", out)
        self.assertIn("Revise TCP", out)
        self.assertNotIn("Future task", out)
        self.assertIn("(2)", out)
        self.assertNotIn("greenhouse:acme-rockets:1001", out)
        self.assertIn("and 1 more", out)

    def test_without_getjo(self) -> None:
        with mock.patch.dict(os.environ, {"PREP_GETJO_DB": str(self.tmp / "missing.db")}):
            code, out, _ = self.run_cli("today")
        self.assertEqual(code, 0)
        self.assertIn("skipped", out)

    def test_does_not_touch_private_dir(self) -> None:
        self.run_cli("today")
        self.assertFalse((self.tmp / "private").exists())


class AppliedTest(IsolatedTestCase):
    def app(self, app_id: int = 1) -> dict:
        with closing(db.connect(self.prep_db)) as conn:
            return db.get_application(conn, app_id)

    def test_by_job_id(self) -> None:
        code, out, _ = self.run_cli("applied", "--job-id", "lever:globex-labs:abc",
                                    "--next-action", "Follow up", "--next-date", "+3", "--tier", "A")
        self.assertEqual(code, 0, out)
        self.assertIn("linked to getjo job lever:globex-labs:abc", out)
        app = self.app()
        self.assertEqual((app["company"], app["role"], app["status"]), ("Globex Labs", "Cloud Security Engineer", "applied"))
        self.assertEqual(app["link"], "https://jobs.example.com/globex-labs/abc")
        self.assertEqual(app["next_action_date"], days(3))
        self.assertEqual(app["date_applied"], days(0))

    def test_by_url_links_job(self) -> None:
        code, out, _ = self.run_cli("applied", "--url", "https://boards.example.com/acme-rockets/jobs/1001")
        self.assertEqual(code, 0)
        self.assertEqual(self.app()["getjo_job_id"], "greenhouse:acme-rockets:1001")
        self.assertIn("no next action set", out)

    def test_unknown_url_needs_company_and_role(self) -> None:
        code, _, err = self.run_cli("applied", "--url", "https://other.example.com/1")
        self.assertNotEqual(code, 0)
        self.assertIn("--company", err)
        code, out, _ = self.run_cli("applied", "--url", "https://other.example.com/1",
                                    "--company", "Nowhere Ltd", "--role", "SRE")
        self.assertEqual(code, 0)
        self.assertIsNone(self.app()["getjo_job_id"])
        self.assertIn("stored without a link", out)

    def test_company_role_matches_job(self) -> None:
        self.run_cli("applied", "--company", "initech cloud", "--role", "DevOps Engineer")
        self.assertEqual(self.app()["getjo_job_id"], "ashby:initech-cloud:x1")

    def test_unknown_job_id(self) -> None:
        code, _, err = self.run_cli("applied", "--job-id", "nope:x:1")
        self.assertNotEqual(code, 0)
        self.assertIn("no getjo job", err)

    def test_second_call_updates(self) -> None:
        self.run_cli("applied", "--job-id", "lever:globex-labs:abc", "--status", "to_apply")
        code, out, _ = self.run_cli("applied", "--job-id", "lever:globex-labs:abc")
        self.assertIn("Updated", out)
        self.assertEqual(self.app()["status"], "applied")
        with closing(db.connect(self.prep_db)) as conn:
            self.assertEqual(len(db.list_applications(conn)), 1)

    def test_rerun_never_moves_status_backwards(self) -> None:
        self.run_cli("applied", "--job-id", "lever:globex-labs:abc", "--date", "2026-01-02")
        self.run_cli("update", "1", "--status", "technical")
        self.run_cli("applied", "--job-id", "lever:globex-labs:abc", "--next-action", "Thank-you note")
        app = self.app()
        self.assertEqual(app["status"], "technical")
        self.assertEqual(app["date_applied"], "2026-01-02")
        self.assertEqual(app["next_action"], "Thank-you note")

    def test_planned_application_has_no_date_applied(self) -> None:
        self.run_cli("applied", "--job-id", "lever:globex-labs:abc", "--status", "to_apply")
        self.assertIsNone(self.app()["date_applied"])

    def test_applied_job_leaves_today_matches(self) -> None:
        self.run_cli("applied", "--job-id", "lever:globex-labs:abc")
        _, out, _ = self.run_cli("today")
        self.assertNotIn("id: lever:globex-labs:abc", out)

    def test_update_and_list(self) -> None:
        self.run_cli("applied", "--company", "Nowhere Ltd", "--role", "SRE")
        code, out, _ = self.run_cli("update", "1", "--status", "technical", "--next-action", "Prep", "--next-date", "tomorrow")
        self.assertEqual(code, 0)
        self.assertIn("[technical] next: Prep", out)
        _, out, _ = self.run_cli("apps", "--status", "technical")
        self.assertIn("Nowhere Ltd", out)
        code, _, err = self.run_cli("update", "99", "--status", "offer")
        self.assertEqual(code, 2)
        self.assertIn("no application", err)


class TaskTest(IsolatedTestCase):
    def test_add_list_done(self) -> None:
        code, out, _ = self.run_cli("task", "add", "Read about consistent hashing", "--kind", "prep", "--due", "today")
        self.assertEqual(code, 0)
        self.assertIn("#1 [prep] Read about consistent hashing", out)
        _, out, _ = self.run_cli("task", "list")
        self.assertIn("consistent hashing", out)
        code, out, _ = self.run_cli("task", "done", "1")
        self.assertEqual(code, 0)
        self.assertIn("✓", out)
        _, out, _ = self.run_cli("task", "list")
        self.assertEqual(out.strip(), "no tasks")
        _, out, _ = self.run_cli("task", "list", "--done")
        self.assertIn("consistent hashing", out)

    def test_bad_input(self) -> None:
        code, _, _ = self.run_cli("task", "add", "x", "--due", "someday")
        self.assertNotEqual(code, 0)
        code, _, err = self.run_cli("task", "done", "42")
        self.assertEqual(code, 2)
        self.assertIn("no task", err)


class ParseDayTest(unittest.TestCase):
    def test_forms(self) -> None:
        self.assertEqual(cli.parse_day("2026-02-03"), "2026-02-03")
        self.assertEqual(cli.parse_day("today"), days(0))
        self.assertEqual(cli.parse_day("Tomorrow"), days(1))
        self.assertEqual(cli.parse_day("+7"), days(7))
        self.assertEqual(cli.parse_day("+2d"), days(2))


if __name__ == "__main__":
    unittest.main()
