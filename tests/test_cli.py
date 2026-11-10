import json
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
        self.assertIn("Tasks due today or overdue (0)", out)
        self.assertNotIn("No response", out)
        self.assertIn("matches ≥7 not yet applied or skipped (3)", out)
        self.assertIn("Initech Cloud — DevOps Engineer", out)
        self.assertNotIn("Hooli Data", out)  # score 6

    def test_overdue_tasks_and_linked_matches(self) -> None:
        with closing(db.connect(self.prep_db)) as conn:
            app_id = db.add_application(conn, "Acme Rockets", "Platform Engineer",
                                        getjo_job_id="greenhouse:acme-rockets:1001", status="applied")
            db.add_task(conn, "Chase recruiter", "admin", days(-1), app_id)
            db.add_task(conn, "Revise TCP", "prep", days(0))
            db.add_task(conn, "Future task", "prep", days(3))
        code, out, _ = self.run_cli("today", "--limit", "1")
        self.assertEqual(code, 0)
        self.assertIn(f"Chase recruiter due {days(-1)} — Acme Rockets", out)
        self.assertIn("Revise TCP", out)
        self.assertNotIn("Future task", out)
        self.assertIn("(2)", out)
        self.assertNotIn("greenhouse:acme-rockets:1001", out)
        self.assertIn("and 1 more", out)

    def test_skipped_jobs_not_listed(self) -> None:
        with closing(db.connect(self.prep_db)) as conn:
            db.record_decision(conn, {"id": "ashby:initech-cloud:x1", "company": "Initech Cloud"}, "skipped", "location")
        _, out, _ = self.run_cli("today")
        self.assertIn("(2)", out)
        self.assertNotIn("Initech Cloud", out)

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
        self.assertNotIn("next action", out)
        self.assertIn(f"Chase if no reply due {days(7)}", out)

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
        with closing(db.connect(self.prep_db)) as conn:
            titles = [t["title"] for t in db.app_tasks(conn)[1]]
        self.assertEqual(titles, ["Coding practice", "Review the role's tech stack", "Thank-you note"])

    def test_planned_application_has_no_date_applied(self) -> None:
        _, out, _ = self.run_cli("applied", "--job-id", "lever:globex-labs:abc", "--status", "to_apply")
        self.assertIsNone(self.app()["date_applied"])
        self.assertIn("[interested]", out)
        self.assertNotIn("Apply due", out)  # the review task follows the agent's results
        self.assertIn("queued for the agent: Company research and CV fit", out)
        self.assertIn('"Review research and apply" task follows', out)
        self.assertNotIn("Research the company", out)  # the agent's job (company research), not a to-do
        with closing(db.connect(self.prep_db)) as conn:
            row = conn.execute("SELECT decision FROM job_decisions").fetchone()
        self.assertEqual(row[0], "interested")

    def test_applied_job_leaves_today_matches(self) -> None:
        self.run_cli("applied", "--job-id", "lever:globex-labs:abc")
        _, out, _ = self.run_cli("today")
        self.assertNotIn("id: lever:globex-labs:abc", out)

    def test_update_and_list(self) -> None:
        self.run_cli("applied", "--company", "Nowhere Ltd", "--role", "SRE")
        code, out, _ = self.run_cli("update", "1", "--status", "technical", "--next-action", "Prep", "--next-date", "tomorrow")
        self.assertEqual(code, 0)
        self.assertIn(f"[coding interview] next: Prep ({days(1)})", out)
        _, out, _ = self.run_cli("apps", "--status", "technical")
        self.assertIn("Nowhere Ltd", out)
        code, _, err = self.run_cli("update", "99", "--status", "offer")
        self.assertEqual(code, 2)
        self.assertIn("no application", err)

    def test_update_status_runs_stage_tasks(self) -> None:
        self.run_cli("applied", "--company", "Nowhere Ltd", "--role", "SRE")
        code, out, _ = self.run_cli("update", "1", "--status", "system_design", "--interview-date", days(5),
                                    "--comment", "Panel of three")
        self.assertEqual(code, 0, out)
        self.assertIn(f"System design practice due {days(4)}", out)
        with closing(db.connect(self.prep_db)) as conn:
            tasks = {t["title"]: t["status"] for t in db.list_tasks(conn, None)}
            history = db.timeline(conn, 1)
            interview = conn.execute("SELECT date, stage FROM interviews").fetchone()
        self.assertEqual(tasks, {"Chase if no reply": "closed", "System design practice": "todo"})
        self.assertEqual(history[-1], {"at": history[-1]["at"], "status": "system_design", "comment": "Panel of three"})
        self.assertEqual(tuple(interview), (days(5), "system_design"))

    def test_update_flag_checks(self) -> None:
        self.run_cli("applied", "--company", "Nowhere Ltd", "--role", "SRE")
        for argv in (("--comment", "x"), ("--interview-date", "+2"), ("--next-date", "+2"), ()):
            with self.subTest(argv=argv):
                code, _, _ = self.run_cli("update", "1", *argv)
                self.assertNotEqual(code, 0)
        code, _, _ = self.run_cli("update", "1", "--clear-next")
        self.assertNotEqual(code, 0)  # removed: the next action follows the tasks

    def test_update_to_same_stage_says_so(self) -> None:
        self.run_cli("applied", "--company", "Nowhere Ltd", "--role", "SRE")
        code, out, _ = self.run_cli("update", "1", "--status", "applied", "--interview-date", "+2")
        self.assertEqual(code, 0)
        self.assertIn("Already applied: nothing moved; --interview-date not saved (not an interview stage)", out)

    def test_same_interview_stage_keeps_date_and_comment(self) -> None:
        # A rescheduled call or a second round: nothing moves, but nothing the user said is lost.
        self.run_cli("applied", "--company", "Nowhere Ltd", "--role", "SRE")
        self.run_cli("update", "1", "--status", "recruiter_call")
        code, out, _ = self.run_cli("update", "1", "--status", "recruiter_call", "--interview-date", "+3",
                                    "--comment", "Moved to Thursday 14:00")
        self.assertEqual(code, 0)
        self.assertIn(f"Already recruiter call: nothing moved; comment saved; interview date {days(3)} saved", out)
        with closing(db.connect(self.prep_db)) as conn:
            self.assertEqual(db.timeline(conn, 1)[-1]["comment"], "Moved to Thursday 14:00")
            self.assertEqual([tuple(r) for r in conn.execute("SELECT date, stage FROM interviews")],
                             [(days(3), "recruiter_call")])

    def test_reschedule_moves_prep_tasks(self) -> None:
        self.run_cli("applied", "--company", "Nowhere Ltd", "--role", "SRE")
        self.run_cli("update", "1", "--status", "technical", "--interview-date", "+5")
        code, out, _ = self.run_cli("update", "1", "--status", "technical", "--interview-date", "+9",
                                    "--comment", "Moved to next Friday")
        self.assertEqual(code, 0)
        self.assertIn(f"Already coding interview: nothing moved; comment saved; interview date {days(9)} saved; "
                      f"2 prep tasks moved to {days(8)}", out)
        self.assertIn(f"Coding practice due {days(8)}", out)
        _, out, _ = self.run_cli("update", "1", "--status", "technical", "--interview-date", "+9")
        self.assertNotIn("moved to", out)  # same date: nothing to move

    def test_comment(self) -> None:
        self.run_cli("applied", "--company", "Nowhere Ltd", "--role", "SRE")
        code, out, _ = self.run_cli("comment", "1", "Second interview booked")
        self.assertEqual(code, 0)
        self.assertIn("Comment added", out)
        with closing(db.connect(self.prep_db)) as conn:
            self.assertEqual(db.timeline(conn, 1)[-1]["comment"], "Second interview booked")
        self.assertEqual(self.run_cli("comment", "99", "x")[0], 2)
        self.assertEqual(self.run_cli("comment", "1", "  ")[0], 2)

    def test_today_lists_auto_no_response(self) -> None:
        with closing(db.connect(self.prep_db)) as conn:
            app_id, _ = db.record_application(conn, "Quiet Co", "SRE", date_applied=days(-20), today=days(-20))
            conn.execute("UPDATE status_history SET changed_at = ? WHERE application_id = ?",
                         (days(-20) + "T09:00:00Z", app_id))
            conn.commit()
        _, out, _ = self.run_cli("today")
        self.assertIn("Moved to No response (no reply in 14 days), last 7 days (1):", out)
        self.assertIn("Quiet Co — SRE", out)
        self.assertEqual(self.app(app_id)["status"], "no_response")
        _, out, _ = self.run_cli("today")  # already moved: still listed, not moved twice
        self.assertIn("Quiet Co — SRE", out)
        with closing(db.connect(self.prep_db)) as conn:
            self.assertEqual(db.status_history(conn, app_id), ["applied", "no_response"])

    def test_no_response_days_setting(self) -> None:
        with closing(db.connect(self.prep_db)) as conn:
            app_id, _ = db.record_application(conn, "Quiet Co", "SRE", date_applied=days(-5), today=days(-5))
            conn.execute("UPDATE status_history SET changed_at = ? WHERE application_id = ?",
                         (days(-5) + "T09:00:00Z", app_id))
            conn.commit()
        self.run_cli("today")
        self.assertEqual(self.app(app_id)["status"], "applied")
        with mock.patch.dict(os.environ, {"PREP_NO_RESPONSE_DAYS": "5"}):
            _, out, _ = self.run_cli("today")
        self.assertIn("no reply in 5 days", out)
        self.assertEqual(self.app(app_id)["status"], "no_response")


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


class FeedbackExportTest(IsolatedTestCase):
    def test_one_record_per_decision(self) -> None:
        with closing(db.connect(self.prep_db)) as conn:
            db.add_application(conn, "Acme Rockets", "Platform Engineer", getjo_job_id="greenhouse:acme-rockets:1001",
                               status="applied", date_applied="2026-01-05")
            db.record_decision(conn, {"id": "lever:globex-labs:abc", "company": "Globex Labs",
                                      "title": "Cloud Security Engineer", "location": "Remote", "score": 8},
                               "skipped", "role_type", "Security, not platform")
        code, out, _ = self.run_cli("feedback", "export")
        self.assertEqual(code, 0)
        path = self.tmp / "private" / "feedback" / "job_decisions.jsonl"
        self.assertIn(f"Wrote 2 labelled decisions to {path}", out)
        records = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual([tuple(r) for r in records], [cli.FEEDBACK_FIELDS] * 2)
        acme, globex = records  # oldest first
        self.assertEqual((acme["decision"], acme["reason"], acme["location"], acme["score"]),
                         ("applied", None, "London", 9))  # legacy row: location and score from getjo
        self.assertEqual((globex["decision"], globex["reason"], globex["comment"]),
                         ("skipped", "role_type", "Security, not platform"))
        self.assertFalse(path.with_name(path.name + ".tmp").exists())

    def test_to_apply_exported_as_interested(self) -> None:
        self.run_cli("applied", "--job-id", "ashby:initech-cloud:x1", "--status", "to_apply")
        out = self.tmp / "labels.jsonl"
        self.run_cli("feedback", "export", "--out", str(out))
        (record,) = [json.loads(line) for line in out.read_text().splitlines()]
        self.assertEqual((record["getjo_job_id"], record["decision"]), ("ashby:initech-cloud:x1", "interested"))

    def test_stored_interested_exported_once(self) -> None:
        job = {"id": "ashby:initech-cloud:x1", "company": "Initech Cloud", "title": "DevOps Engineer",
               "location": "Remote", "score": 7, "url": "https://example.com/x1", "source": "ashby"}
        with closing(db.connect(self.prep_db)) as conn:
            db.record_application(conn, job["company"], job["title"], getjo_job_id=job["id"], status="to_apply")
            db.record_decision(conn, job, "interested")
        out = self.tmp / "labels.jsonl"
        self.run_cli("feedback", "export", "--out", str(out))
        records = [json.loads(line) for line in out.read_text().splitlines()]
        self.assertEqual([(r["getjo_job_id"], r["decision"], r["location"]) for r in records],
                         [("ashby:initech-cloud:x1", "interested", "Remote")])

    def test_rewrites_and_out_flag(self) -> None:
        out = self.tmp / "elsewhere" / "labels.jsonl"
        out.parent.mkdir()
        out.write_text("stale\n")
        code, stdout, _ = self.run_cli("feedback", "export", "--out", str(out))
        self.assertEqual(code, 0)
        self.assertIn("Wrote 0", stdout)
        self.assertEqual(out.read_text(), "")

    def test_needs_private_dir(self) -> None:
        with mock.patch.dict(os.environ, {"PREP_PRIVATE_DIR": ""}):
            code, _, err = self.run_cli("feedback", "export")
        self.assertNotEqual(code, 0)
        self.assertIn("private_dir", err)


class AgentCommandsTest(IsolatedTestCase):
    """The commands /ask uses for plain requests: task edit/delete, comments, skip, thread show."""

    def setUp(self) -> None:
        super().setUp()
        with closing(db.connect(self.prep_db)) as conn:
            self.app, _ = db.record_application(conn, "Globex Labs", "Engineer", status="to_apply",
                                                getjo_job_id="lever:globex-labs:abc")
            self.task = db.add_task(conn, "Send availability", "admin", days(2), self.app)

    def test_task_edit_and_delete(self) -> None:
        code, out, _ = self.run_cli("task", "edit", str(self.task), "--title", "Send availability to Sam", "--due", "+5")
        self.assertEqual(code, 0)
        self.assertIn(f"Send availability to Sam due {days(5)}", out)
        self.assertNotIn(" due ", self.run_cli("task", "edit", str(self.task), "--no-due")[1].split(" — ")[0])
        self.assertEqual(self.run_cli("task", "edit", str(self.task))[0], 2)  # nothing to change
        code, out, _ = self.run_cli("task", "delete", str(self.task))
        self.assertIn("Deleted: #", out)
        self.assertEqual(self.run_cli("task", "delete", str(self.task))[0], 2)

    def test_comments_list_amend_remove(self) -> None:
        self.run_cli("comment", str(self.app), "Referral from a former colleague")
        out = self.run_cli("comments", "list", str(self.app))[1]
        ref = out.split("\n")[1].split()[0]
        self.assertTrue(ref.startswith("c"))
        self.assertIn("Referral from a former colleague", out)
        self.assertEqual(self.run_cli("comments", "amend", ref, "Referral from Sam")[0], 0)
        self.assertIn("Referral from Sam", self.run_cli("comments", "list", str(self.app))[1])
        self.assertIn(f"Comment {ref} removed.", self.run_cli("comments", "remove", ref)[1])
        self.assertIn("none", self.run_cli("comments", "list", str(self.app))[1])
        self.assertEqual(self.run_cli("comments", "remove", "z9")[0], 2)

    def test_skip(self) -> None:
        self.assertEqual(self.run_cli("skip", str(self.app), "--reason", "nope")[0], 2)
        code, out, _ = self.run_cli("skip", str(self.app), "--reason", "seniority", "--comment", "Staff level")
        self.assertEqual(code, 0)
        self.assertIn("Job Offers shows it under Skipped", out)
        with closing(db.connect(self.prep_db)) as conn:
            self.assertEqual(db.get_application(conn, self.app)["status"], "skipped")
        code, _, err = self.run_cli("skip", str(self.app), "--reason", "seniority")
        self.assertEqual(code, 2)
        self.assertIn("Only an Interested", err)

    def test_thread_show_lists_tasks_and_comments_with_ids(self) -> None:
        self.run_cli("comment", str(self.app), "Take-home due Friday")
        out = self.run_cli("thread", "show", str(self.app))[1]
        self.assertIn("Stage: interested · tier –", out)
        self.assertIn(f"  #{self.task} [admin] Send availability due {days(2)}", out)
        self.assertRegex(out, r"  c\d+ \d{4}-\d\d-\d\d: Take-home due Friday")
