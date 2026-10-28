import os
from contextlib import closing
from unittest import mock
from urllib.parse import urlencode

from prep import db, web
from prep.config import load_settings
from prep.seed_example import build_prep_db
from tests.helpers import IsolatedTestCase

TODAY = "2026-01-20"  # the fixtures' anchor date
HOST = {"Host": "localhost:8765"}
PAGES = ("/", "/matches", "/tasks", "/progress")
PAY = "PAY-MARKER-31337"


class WebTestCase(IsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        build_prep_db(self.prep_db, self.fixtures)
        self.settings = load_settings()

    def get(self, path: str, headers: dict | None = None) -> tuple[int, dict, str]:
        status, hdrs, body = web.handle(self.settings, "GET", path, HOST if headers is None else headers, today=TODAY)
        return status, dict(hdrs), body.decode("utf-8")

    def post(self, path: str, form: dict, headers: dict | None = None) -> tuple[int, dict, str]:
        body = urlencode(form).encode()
        status, hdrs, out = web.handle(self.settings, "POST", path, HOST if headers is None else headers, body, today=TODAY)
        return status, dict(hdrs), out.decode("utf-8")

    def conn(self):
        return closing(db.connect(self.prep_db))


class PagesTest(WebTestCase):
    def test_every_page_renders(self) -> None:
        for path in PAGES:
            with self.subTest(path=path):
                status, headers, body = self.get(path)
                self.assertEqual(status, 200)
                self.assertIn("text/html", headers["Content-Type"])
                self.assertIn("Content-Security-Policy", headers)
                self.assertNotIn("http://", body.replace("https://", ""), "no external requests")

    def test_pipeline_numbers(self) -> None:
        _, _, body = self.get("/")
        self.assertIn("2 / 10", body)  # applied 2026-01-14 and 2026-01-16 in the last 7 days
        self.assertIn("Follow up by email", body)  # overdue next action
        self.assertIn("43%", body)  # 3 of 7 replied
        self.assertNotIn('class="warning"', body)

    def test_reply_rate_warning_after_25(self) -> None:
        with self.conn() as conn:
            for i in range(25):
                db.add_application(conn, f"Example {i}", "Engineer", status="applied", date_applied="2026-01-02")
        _, _, body = self.get("/")
        self.assertIn('class="warning"', body)

    def test_static_css(self) -> None:
        status, headers, body = self.get("/static/style.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", headers["Content-Type"])
        self.assertIn("--accent", body)

    def test_not_found(self) -> None:
        self.assertEqual(self.get("/static/../web.py")[0], 404)
        self.assertEqual(self.get("/nope")[0], 404)

    def test_values_are_escaped(self) -> None:
        with self.conn() as conn:
            db.add_application(conn, "<script>alert(1)</script>", "Engineer", status="applied",
                               next_action="<b>x</b>", next_action_date="2026-01-01")
            db.add_task(conn, "<img src=x onerror=alert(1)>")
        for path in PAGES:
            with self.subTest(path=path):
                body = self.get(path)[2]
                self.assertNotIn("<script>alert", body)
                self.assertNotIn("<img src=x", body)
        self.assertIn("&lt;script&gt;", self.get("/")[2])

    def test_pay_never_rendered(self) -> None:
        with self.conn() as conn:
            app_id = db.add_application(conn, "Paycorp", "Engineer", status="applied", pay=PAY,
                                        next_action="Chase", next_action_date="2026-01-01")
            db.add_task(conn, "Pay-linked task", application_id=app_id)
        for path in PAGES:
            with self.subTest(path=path):
                body = self.get(path)[2]
                if path in ("/", "/tasks"):
                    self.assertIn("Paycorp", body)
                self.assertNotIn(PAY, body)
        status, headers, body = self.post("/tasks/add", {"title": "Another", "kind": "prep"})
        self.assertNotIn(PAY, headers["Location"] + body)

    def test_unsafe_job_url_not_linked(self) -> None:
        self.assertEqual(web._link("javascript:alert(1)", "X"), "X")
        self.assertIn('href="https://example.com/a?b=1&amp;c=2"', web._link("https://example.com/a?b=1&c=2", "X"))


class MatchesTest(WebTestCase):
    def test_lists_untracked_matches(self) -> None:
        _, _, body = self.get("/matches")
        self.assertIn("Globex Labs", body)
        self.assertIn("Initech Cloud", body)
        self.assertNotIn("Hooli Data", body)  # score 6
        self.assertNotIn("greenhouse:acme-rockets:1001", body)  # already an application
        self.assertIn("(2)", body)

    def test_mark_applied(self) -> None:
        status, headers, _ = self.post("/matches/apply", {
            "job_id": "lever:globex-labs:abc", "tier": "A", "next_action": "Follow up", "next_date": "2026-01-27",
        })
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/matches?msg=applied")
        with self.conn() as conn:
            app = db.find_by_job(conn, "lever:globex-labs:abc")
        self.assertEqual((app["company"], app["status"], app["tier"]), ("Globex Labs", "applied", "A"))
        self.assertEqual(app["next_action_date"], "2026-01-27")
        _, _, body = self.get(headers["Location"])
        self.assertIn("Recorded as applied", body)
        self.assertNotIn("Globex Labs", body)
        self.assertIn("(1)", body)

    def test_mark_applied_twice_does_not_duplicate(self) -> None:
        for _ in range(2):
            self.post("/matches/apply", {"job_id": "lever:globex-labs:abc"})
        with self.conn() as conn:
            self.assertEqual(sum(a["getjo_job_id"] == "lever:globex-labs:abc" for a in db.list_applications(conn)), 1)

    def test_mark_applied_bad_input(self) -> None:
        self.assertEqual(self.post("/matches/apply", {"job_id": "no:such:job"})[0], 400)
        self.assertEqual(self.post("/matches/apply", {})[0], 400)
        status, _, body = self.post("/matches/apply", {"job_id": "lever:globex-labs:abc", "next_date": "soon"})
        self.assertEqual(status, 400)
        self.assertIn("next_action_date", body)

    def test_without_getjo_db(self) -> None:
        for value in (str(self.tmp / "missing.db"), ""):
            with self.subTest(getjo=value), mock.patch.dict(os.environ, {"PREP_GETJO_DB": value}):
                self.settings = load_settings()
                status, _, body = self.get("/matches")
                self.assertEqual(status, 200)
                self.assertIn("not available", body)
                self.assertEqual(self.post("/matches/apply", {"job_id": "lever:globex-labs:abc"})[0], 400)

    def test_getjo_file_that_is_not_a_jobs_db(self) -> None:
        broken = self.tmp / "broken.db"
        broken.write_bytes(b"not a database")
        with mock.patch.dict(os.environ, {"PREP_GETJO_DB": str(broken)}):
            self.settings = load_settings()
            status, _, body = self.get("/matches")
        self.assertEqual(status, 200)
        self.assertIn("Could not read", body)


class TasksTest(WebTestCase):
    def test_overdue_highlighted(self) -> None:
        _, _, body = self.get("/tasks")
        self.assertIn('<span class="overdue">2026-01-19</span>', body)
        self.assertIn("Write STAR story", body)  # recently done

    def test_add_and_done(self) -> None:
        status, headers, _ = self.post("/tasks/add", {
            "title": "Mock system design", "kind": "drill", "due_date": "2026-01-22", "application_id": "1",
        })
        self.assertEqual((status, headers["Location"]), (303, "/tasks?msg=task_added"))
        with self.conn() as conn:
            task = next(t for t in db.list_tasks(conn) if t["title"] == "Mock system design")
        self.assertEqual((task["kind"], task["due_date"], task["application_id"]), ("drill", "2026-01-22", 1))
        self.assertIn("Mock system design", self.get("/tasks")[2])

        status, headers, _ = self.post("/tasks/done", {"task_id": str(task["id"])})
        self.assertEqual((status, headers["Location"]), (303, "/tasks?msg=task_done"))
        with self.conn() as conn:
            self.assertNotIn(task["id"], [t["id"] for t in db.list_tasks(conn)])
        self.assertIn("Task marked done", self.get(headers["Location"])[2])

    def test_bad_task_input(self) -> None:
        for form in ({"title": ""}, {"title": "x", "kind": "nap"}, {"title": "x", "due_date": "monday"},
                     {"title": "x", "application_id": "999"}, {"title": "x", "application_id": "one"}):
            with self.subTest(form=form):
                self.assertEqual(self.post("/tasks/add", form)[0], 400)
        self.assertEqual(self.post("/tasks/done", {"task_id": "999"})[0], 400)
        self.assertEqual(self.post("/tasks/done", {})[0], 400)
        self.assertEqual(self.post("/nope", {})[0], 404)


class SharedDatabaseTest(WebTestCase):
    def test_cli_write_visible_to_next_request(self) -> None:
        self.assertNotIn("Call the recruiter", self.get("/tasks")[2])
        code, _, _ = self.run_cli("task", "add", "Call the recruiter", "--kind", "admin")
        self.assertEqual(code, 0)
        self.assertIn("Call the recruiter", self.get("/tasks")[2])

        code, _, _ = self.run_cli("applied", "--job-id", "ashby:initech-cloud:x1")
        self.assertEqual(code, 0)
        self.assertNotIn("Initech Cloud", self.get("/matches")[2])

    def test_web_write_visible_to_cli(self) -> None:
        self.post("/tasks/add", {"title": "Added in the browser"})
        _, out, _ = self.run_cli("task", "list")
        self.assertIn("Added in the browser", out)
        self.post("/matches/apply", {"job_id": "lever:globex-labs:abc"})
        _, out, _ = self.run_cli("today")
        self.assertNotIn("Globex Labs", out)


class RequestChecksTest(WebTestCase):
    def test_bad_host_rejected(self) -> None:
        for headers in ({"Host": "evil.example.com"}, {"Host": "localhost.evil.com:8765"}, {"Host": "10.0.0.5:8765"}, {}):
            with self.subTest(headers=headers):
                self.assertEqual(self.get("/", headers)[0], 400)
        for host in ("localhost", "127.0.0.1:8766", "LOCALHOST:8765"):
            with self.subTest(host=host):
                self.assertEqual(self.get("/", {"Host": host})[0], 200)

    def test_cross_origin_post_rejected(self) -> None:
        form = {"title": "Should not be added"}
        for extra in ({"Origin": "http://evil.example.com"}, {"Origin": "null"},
                      {"Origin": "http://localhost:9999"}, {"Sec-Fetch-Site": "cross-site"},
                      {"Origin": "http://localhost:8765", "Sec-Fetch-Site": "same-site"}):
            with self.subTest(extra=extra):
                self.assertEqual(self.post("/tasks/add", form, {**HOST, **extra})[0], 403)
        with self.conn() as conn:
            self.assertNotIn("Should not be added", [t["title"] for t in db.list_tasks(conn)])
        ok = {**HOST, "Origin": "http://localhost:8765", "Sec-Fetch-Site": "same-origin"}
        self.assertEqual(self.post("/tasks/add", form, ok)[0], 303)

    def test_non_loopback_bind_refused(self) -> None:
        for host in ("0.0.0.0", "192.168.1.10", "::", "::1", "example.com", ""):
            with self.subTest(host=host), self.assertRaises(ValueError):
                web.serve(self.settings, host, 0)
        web.check_loopback("127.0.0.1")  # does not raise

    def test_form_too_large(self) -> None:
        status, _, _ = web.handle(self.settings, "POST", "/tasks/add", HOST, b"x" * (web.MAX_BODY + 1))
        self.assertEqual(status, 400)
