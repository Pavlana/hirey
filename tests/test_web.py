import json
import os
import unittest
from contextlib import closing
from unittest import mock
from urllib.parse import urlencode

from prep import db, web
from prep.config import load_settings
from prep.seed_example import build_prep_db
from tests.helpers import IsolatedTestCase

TODAY = "2026-01-20"  # the fixtures' anchor date
HOST = {"Host": "localhost:8765"}
PAGES = ("/", "/offers", "/tasks")
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

    def test_static_js(self) -> None:
        status, headers, body = self.get("/static/app.js")
        self.assertEqual(status, 200)
        self.assertIn("text/javascript", headers["Content-Type"])
        self.assertIn("nosniff", headers["X-Content-Type-Options"])
        self.assertIn("application/json", body)

    def test_no_external_requests(self) -> None:
        csp = self.get("/")[1]["Content-Security-Policy"]
        self.assertIn("script-src 'self'", csp)
        self.assertIn("connect-src 'self'", csp)
        self.assertNotIn("unsafe-inline", csp)
        for path in ("/static/style.css", "/static/app.js"):
            with self.subTest(path=path):
                body = self.get(path)[2]
                self.assertNotIn("http://", body)
                self.assertNotIn("https://", body)
                self.assertNotIn("@import", body)
        for path in PAGES:
            with self.subTest(path=path):
                body = self.get(path)[2]
                self.assertNotIn(" style=", body, "inline styles are blocked by the CSP")
                self.assertNotIn("<script>", body, "inline scripts are blocked by the CSP")
                self.assertIn('src="/static/app.js"', body)

    def test_shell_marks_active_page(self) -> None:
        for path in PAGES:
            with self.subTest(path=path):
                body = self.get(path)[2]
                self.assertEqual(body.count('aria-current="page"'), 1)
                self.assertIn(f'<a href="{path}" aria-current="page">', body)

    def test_dashboard_charts(self) -> None:
        body = self.get("/")[2]
        for chart in ("donut", "treemap", "funnel", "columns", "line"):
            self.assertIn(f'class="chart {chart}"', body)
        self.assertIn("target 10", body)
        self.assertNotIn(" style=", body)

    def test_each_page_has_its_hue(self) -> None:
        for path, theme in web.PAGE_THEMES.items():
            with self.subTest(path=path):
                self.assertIn(f'<body class="theme-{theme}">', self.get(path)[2])
        self.assertEqual(len(set(web.PAGE_THEMES.values())), len(web.PAGE_THEMES), "each page has its own colour")
        css = self.get("/static/style.css")[2]
        for theme in ("sage", "eucalyptus", "dusty-blue", "slate-indigo", "dusty-violet", "mauve-rose",
                      "terracotta", "ochre"):
            self.assertIn(f".theme-{theme} {{", css)

    def test_progress_redirects_to_dashboard(self) -> None:
        status, headers, _ = self.get("/progress")
        self.assertEqual((status, headers["Location"]), (303, "/"))

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


class HelpersTest(unittest.TestCase):
    EVIL = '<script>"x"&</script>'

    def assertEscaped(self, out: str) -> None:
        self.assertNotIn("<script>", out)
        self.assertNotIn('"x"', out)
        self.assertIn("&lt;script&gt;", out)

    def test_helpers_escape_values(self) -> None:
        e = self.EVIL
        for out in (web._badge(e), web._badge(e, e), web._status_badge(e), web._tier_badge(e), web._kind_badge(e),
                    web._score_badge(e), web._button(e, e, e), web._empty(e, e), web._banner(e, "warning"),
                    web._filter(e, e), web._tile(e, e), web._card(e, "", count=1, cls=e, count_for=e),
                    web._table(((e, e), e), [("ok",)], e, table_id=e), web._table((e,), [], e, hint=e),
                    web._due(e, "2026-01-20"), web._note(e)):
            with self.subTest(out=out[:40]):
                self.assertEscaped(out)

    def test_badge_tone_whitelisted(self) -> None:
        self.assertIn('class="badge badge-neutral"', web._badge("x", "evil onclick"))
        self.assertIn('class="badge s10"', web._status_badge("offer"))
        self.assertIn('class="badge s1"', web._status_badge("rejected"))
        self.assertIn(">recruiter call<", web._status_badge("recruiter_call"))
        self.assertIn('class="badge s10"', web._score_badge(9))
        self.assertIn('class="badge s6"', web._score_badge(7))

    def test_table_sort_keys_and_empty_state(self) -> None:
        out = web._table((("Due", "date"), "Task", ""), [("a", "b", "c")], "none")
        self.assertIn('<th scope="col" data-sort="date">Due</th>', out)
        self.assertIn('<th scope="col" data-sort="text">Task</th>', out)
        self.assertIn('<th scope="col"></th>', out)
        empty = web._table(("Due",), [], "Nothing here.", hint="Try later.")
        self.assertIn('class="empty"', empty)
        self.assertIn("Try later.", empty)
        self.assertNotIn("<table", empty)

    def test_due_states(self) -> None:
        self.assertIn("overdue", web._due("2026-01-19", "2026-01-20"))
        self.assertIn("due-today", web._due("2026-01-20", "2026-01-20"))
        self.assertEqual(web._due("2026-01-21", "2026-01-20"), '<span class="mono">2026-01-21</span>')
        self.assertEqual(web._due(None, "2026-01-20"), "")


class JsonActionsTest(WebTestCase):
    """The page script posts urlencoded forms with Accept: application/json."""
    JSON = {**HOST, "Accept": "application/json", "Origin": "http://localhost:8765", "Sec-Fetch-Site": "same-origin"}

    def post_json(self, path: str, form: dict) -> tuple[int, dict]:
        status, headers, body = self.post(path, form, self.JSON)
        self.assertIn("application/json", headers["Content-Type"])
        return status, json.loads(body)

    def test_add_returns_escaped_row(self) -> None:
        status, reply = self.post_json("/tasks/add", {"title": "<b>Mock</b> & drill", "kind": "drill",
                                                      "due_date": "2026-01-19", "application_id": "1"})
        self.assertEqual(status, 200)
        self.assertEqual((reply["ok"], reply["message"]), (True, "Task added."))
        row = reply["html"]
        self.assertTrue(row.startswith("<tr>"))
        self.assertIn("&lt;b&gt;Mock&lt;/b&gt; &amp; drill", row)
        self.assertIn("overdue", row)
        self.assertIn("Acme Rockets", row)
        self.assertIn('data-ajax="done"', row)
        self.assertNotIn(PAY, row)

    def test_done_then_reopen(self) -> None:
        with self.conn() as conn:
            task_id = db.add_task(conn, "Undo me")
        status, reply = self.post_json("/tasks/done", {"task_id": str(task_id)})
        self.assertEqual((status, reply), (200, {"ok": True, "message": "Task marked done."}))
        status, reply = self.post_json("/tasks/reopen", {"task_id": str(task_id)})
        self.assertEqual((status, reply), (200, {"ok": True, "message": "Task reopened."}))
        with self.conn() as conn:
            self.assertEqual(db.get_task(conn, task_id)["status"], "todo")

    def test_apply(self) -> None:
        status, reply = self.post_json("/offers/apply", {"job_id": "lever:globex-labs:abc", "tier": "B"})
        self.assertEqual((status, reply["ok"]), (200, True))
        self.assertNotIn("html", reply)
        with self.conn() as conn:
            self.assertEqual(db.find_by_job(conn, "lever:globex-labs:abc")["tier"], "B")

    def test_errors_are_json(self) -> None:
        for path, form in (("/tasks/add", {"title": ""}), ("/tasks/done", {"task_id": "999"}),
                           ("/tasks/reopen", {}), ("/offers/apply", {"job_id": "no:such:job"})):
            with self.subTest(path=path):
                status, reply = self.post_json(path, form)
                self.assertEqual(status, 400)
                self.assertFalse(reply["ok"])
                self.assertTrue(reply["message"])


class OffersTest(WebTestCase):
    def test_lists_untracked_matches(self) -> None:
        _, _, body = self.get("/offers")
        self.assertIn("Globex Labs", body)
        self.assertIn("Initech Cloud", body)
        self.assertNotIn("Hooli Data", body)  # score 6
        self.assertNotIn("greenhouse:acme-rockets:1001", body)  # already an application
        self.assertIn("(2)", body)

    def test_mark_applied(self) -> None:
        status, headers, _ = self.post("/offers/apply", {
            "job_id": "lever:globex-labs:abc", "tier": "A", "next_action": "Follow up", "next_date": "2026-01-27",
        })
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/offers?msg=applied")
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
            self.post("/offers/apply", {"job_id": "lever:globex-labs:abc"})
        with self.conn() as conn:
            self.assertEqual(sum(a["getjo_job_id"] == "lever:globex-labs:abc" for a in db.list_applications(conn)), 1)

    def test_mark_applied_bad_input(self) -> None:
        self.assertEqual(self.post("/offers/apply", {"job_id": "no:such:job"})[0], 400)
        self.assertEqual(self.post("/offers/apply", {})[0], 400)
        status, _, body = self.post("/offers/apply", {"job_id": "lever:globex-labs:abc", "next_date": "soon"})
        self.assertEqual(status, 400)
        self.assertIn("next_action_date", body)

    def test_without_getjo_db(self) -> None:
        for value in (str(self.tmp / "missing.db"), ""):
            with self.subTest(getjo=value), mock.patch.dict(os.environ, {"PREP_GETJO_DB": value}):
                self.settings = load_settings()
                status, _, body = self.get("/offers")
                self.assertEqual(status, 200)
                self.assertIn("not available", body)
                self.assertEqual(self.post("/offers/apply", {"job_id": "lever:globex-labs:abc"})[0], 400)

    def test_getjo_file_that_is_not_a_jobs_db(self) -> None:
        broken = self.tmp / "broken.db"
        broken.write_bytes(b"not a database")
        with mock.patch.dict(os.environ, {"PREP_GETJO_DB": str(broken)}):
            self.settings = load_settings()
            status, _, body = self.get("/offers")
        self.assertEqual(status, 200)
        self.assertIn("Could not read", body)


class OfferFiltersTest(WebTestCase):
    def companies(self, query: str) -> set[str]:
        status, _, body = self.get("/offers" + query)
        self.assertEqual(status, 200)
        names = ("Acme Rockets", "Globex Labs", "Initech Cloud", "Hooli Data", "Umbrella Ops")
        return {n for n in names if f">{n}</a>" in body}

    def test_filters(self) -> None:
        cases = {
            "": {"Globex Labs", "Initech Cloud"},  # new, score >= 7; Acme is applied
            "?score=8": {"Globex Labs"},
            "?score=6": {"Globex Labs", "Initech Cloud", "Hooli Data"},
            "?show=all": {"Acme Rockets", "Globex Labs", "Initech Cloud"},
            "?show=applied": {"Acme Rockets"},  # linked application, no decision row
            "?show=skipped": set(),
            "?company=GLOB": {"Globex Labs"},
            "?role=devops": {"Initech Cloud"},
            "?loc=london": set(),
            "?loc=london&show=all": {"Acme Rockets"},
            "?loc=remote": {"Globex Labs"},
            "?loc=other": {"Initech Cloud"},
            "?loc=london&loc=remote&show=all": {"Acme Rockets", "Globex Labs"},
            "?score=abc&show=nope&loc=mars": {"Globex Labs", "Initech Cloud"},  # bad values: defaults
            "?score=99": {"Globex Labs", "Initech Cloud"},
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(self.companies(query), expected)

    def test_filter_form_keeps_values(self) -> None:
        _, _, body = self.get("/offers?score=8&company=%3Cb%3E&loc=remote&show=all")
        self.assertIn('<option value="8" selected>', body)
        self.assertIn('name="company" value="&lt;b&gt;"', body)
        self.assertIn('value="remote" checked', body)
        self.assertIn('<option value="all" selected>', body)
        _, _, body = self.get("/offers?score=8&company=ex+Labs&loc=remote")
        self.assertIn('name="return" value="/offers?score=8&amp;company=ex+Labs&amp;loc=remote&amp;show=new"', body)

    def test_matches_redirects(self) -> None:
        status, headers, _ = self.get("/matches")
        self.assertEqual((status, headers["Location"]), (303, "/offers"))
        status, headers, _ = self.get("/matches?score=8&loc=remote")
        self.assertEqual((status, headers["Location"]), (303, "/offers?score=8&loc=remote"))
        status, headers, _ = self.post("/matches/apply", {"job_id": "lever:globex-labs:abc"})
        self.assertEqual((status, headers["Location"]), (303, "/offers?msg=applied"))


class SkipTest(WebTestCase):
    GLOBEX = "lever:globex-labs:abc"

    def test_skip_and_undo(self) -> None:
        status, headers, _ = self.post("/offers/skip", {"job_id": self.GLOBEX, "reason": "stack",
                                                        "comment": "<b>No Java</b>"})
        self.assertEqual((status, headers["Location"]), (303, "/offers?msg=skipped"))
        with self.conn() as conn:
            d = db.job_decisions(conn)[self.GLOBEX]
            self.assertIsNone(db.find_by_job(conn, self.GLOBEX))
        self.assertEqual((d["decision"], d["reason"], d["comment"], d["score"]), ("skipped", "stack", "<b>No Java</b>", 8))

        _, _, body = self.get(headers["Location"])
        self.assertIn("Skipped. It no longer appears", body)
        self.assertNotIn("Globex Labs", body)
        _, out, _ = self.run_cli("today")
        self.assertNotIn("Globex Labs", out)

        _, _, body = self.get("/offers?show=skipped")
        self.assertIn("Globex Labs", body)
        self.assertIn("Stack", body)
        self.assertIn("&lt;b&gt;No Java&lt;/b&gt;", body)
        self.assertNotIn("<b>No Java", body)
        self.assertIn('action="/offers/unskip"', body)

        status, headers, _ = self.post("/offers/unskip", {"job_id": self.GLOBEX})
        self.assertEqual((status, headers["Location"]), (303, "/offers?msg=unskipped"))
        self.assertIn("Globex Labs", self.get("/offers")[2])

    def test_mark_applied_records_decision(self) -> None:
        self.post("/offers/apply", {"job_id": self.GLOBEX})
        with self.conn() as conn:
            self.assertEqual(db.job_decisions(conn)[self.GLOBEX]["decision"], "applied")
        self.assertIn("Globex Labs", self.get("/offers?show=applied")[2])
        self.assertNotIn("Globex Labs", self.get("/offers")[2])

    def test_cli_applied_records_decision(self) -> None:
        self.run_cli("applied", "--job-id", "ashby:initech-cloud:x1")
        with self.conn() as conn:
            self.assertEqual(db.job_decisions(conn)["ashby:initech-cloud:x1"]["decision"], "applied")
        self.assertIn("Initech Cloud", self.get("/offers?show=applied")[2])

    def test_bad_skips(self) -> None:
        for form in ({"job_id": self.GLOBEX}, {"job_id": self.GLOBEX, "reason": "boring"},
                     {"job_id": "no:such:job", "reason": "stack"}, {"reason": "stack"},
                     {"job_id": "greenhouse:acme-rockets:1001", "reason": "pay"},  # already applied
                     {"job_id": self.GLOBEX, "reason": "other", "comment": "x" * (web.MAX_COMMENT + 1)}):
            with self.subTest(form=form):
                self.assertEqual(self.post("/offers/skip", form)[0], 400)
        for job_id in (self.GLOBEX, "greenhouse:acme-rockets:1001", ""):  # not skipped / applied / missing
            with self.subTest(unskip=job_id):
                self.assertEqual(self.post("/offers/unskip", {"job_id": job_id})[0], 400)
        with self.conn() as conn:
            self.assertNotIn(self.GLOBEX, db.job_decisions(conn))

    def test_return_keeps_filters_only_for_offers(self) -> None:
        cases = {
            "/offers?score=8&loc=remote&msg=applied": "/offers?score=8&loc=remote&msg=skipped",
            "/offers?company=a%0d%0aSet-Cookie:x": "/offers?company=a%0D%0ASet-Cookie%3Ax&msg=skipped",
            "http://evil.example.com/offers?score=8": "/offers?msg=skipped",
            "//evil.example.com/offers": "/offers?msg=skipped",
            "/tasks?x=1": "/offers?msg=skipped",
        }
        for value, location in cases.items():
            with self.subTest(value=value):
                self.post("/offers/unskip", {"job_id": self.GLOBEX}) if self.GLOBEX in self.decided() else None
                status, headers, _ = self.post("/offers/skip", {"job_id": self.GLOBEX, "reason": "pay", "return": value})
                self.assertEqual((status, headers["Location"]), (303, location))

    def decided(self) -> dict:
        with self.conn() as conn:
            return db.job_decisions(conn)

    def test_skip_json(self) -> None:
        body = urlencode({"job_id": self.GLOBEX, "reason": "seniority"}).encode()
        status, _, out = web.handle(self.settings, "POST", "/offers/skip",
                                    {**HOST, "Accept": "application/json"}, body, today=TODAY)
        self.assertEqual((status, json.loads(out)["ok"]), (200, True))

    def test_snapshot_shown_after_getjo_drops_the_job(self) -> None:
        gone = {"id": "greenhouse:gone:9", "company": "Vanished Co", "title": "SRE", "location": "Remote",
                "score": 9, "url": "https://example.com/gone", "source": "greenhouse"}
        with self.conn() as conn:
            db.record_decision(conn, gone, "skipped", "company")
        _, _, body = self.get("/offers?show=skipped")
        self.assertIn("Vanished Co", body)
        self.assertIn("Vanished Co", self.get("/offers?show=skipped&score=10")[2])  # score filters New only

    def test_score_filter_applies_to_new_only(self) -> None:
        hooli = "workable:hooli-data:77"  # score 6
        self.post("/offers/skip", {"job_id": hooli, "reason": "company"})
        cases = {
            "?show=skipped": {"Hooli Data"},
            "?show=skipped&score=10": {"Hooli Data"},
            "?show=applied&score=10": {"Acme Rockets"},
            "?show=all&score=9": {"Acme Rockets", "Hooli Data"},  # Globex (8) is new, below 9
            "?score=6": {"Globex Labs", "Initech Cloud"},  # Hooli is skipped, not new
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(OfferFiltersTest.companies(self, query), expected)

    def test_to_apply_shows_as_interested(self) -> None:
        initech = "ashby:initech-cloud:x1"
        self.run_cli("applied", "--job-id", initech, "--status", "to_apply")
        self.assertEqual(OfferFiltersTest.companies(self, "?show=interested"), {"Initech Cloud"})
        self.assertNotIn("Initech Cloud", OfferFiltersTest.companies(self, "?show=applied"))
        self.assertNotIn("Initech Cloud", OfferFiltersTest.companies(self, ""))
        _, _, body = self.get("/offers?show=interested")
        self.assertIn('<span class="badge s6">interested</span>', body)
        self.assertIn('action="/offers/apply"', body)
        self.assertNotIn('action="/offers/skip"', body)
        self.assertEqual(self.post("/offers/skip", {"job_id": initech, "reason": "pay"})[0], 400)

        self.post("/offers/apply", {"job_id": initech})
        self.assertEqual(OfferFiltersTest.companies(self, "?show=applied"), {"Acme Rockets", "Initech Cloud"})
        self.assertEqual(OfferFiltersTest.companies(self, "?show=interested"), set())


class TasksTest(WebTestCase):
    def test_overdue_highlighted(self) -> None:
        _, _, body = self.get("/tasks")
        self.assertIn('<span class="badge s4 overdue">2026-01-19</span>', body)
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

        status, headers, _ = self.post("/tasks/reopen", {"task_id": str(task["id"])})
        self.assertEqual((status, headers["Location"]), (303, "/tasks?msg=task_reopened"))
        with self.conn() as conn:
            self.assertIn(task["id"], [t["id"] for t in db.list_tasks(conn)])

    def test_bad_task_input(self) -> None:
        for form in ({"title": ""}, {"title": "x", "kind": "nap"}, {"title": "x", "due_date": "monday"},
                     {"title": "x", "application_id": "999"}, {"title": "x", "application_id": "one"}):
            with self.subTest(form=form):
                self.assertEqual(self.post("/tasks/add", form)[0], 400)
        self.assertEqual(self.post("/tasks/done", {"task_id": "999"})[0], 400)
        self.assertEqual(self.post("/tasks/done", {})[0], 400)
        self.assertEqual(self.post("/tasks/reopen", {"task_id": "999"})[0], 400)
        self.assertEqual(self.post("/nope", {})[0], 404)


class SharedDatabaseTest(WebTestCase):
    def test_cli_write_visible_to_next_request(self) -> None:
        self.assertNotIn("Call the recruiter", self.get("/tasks")[2])
        code, _, _ = self.run_cli("task", "add", "Call the recruiter", "--kind", "admin")
        self.assertEqual(code, 0)
        self.assertIn("Call the recruiter", self.get("/tasks")[2])

        code, _, _ = self.run_cli("applied", "--job-id", "ashby:initech-cloud:x1")
        self.assertEqual(code, 0)
        self.assertNotIn("Initech Cloud", self.get("/offers")[2])

    def test_web_write_visible_to_cli(self) -> None:
        self.post("/tasks/add", {"title": "Added in the browser"})
        _, out, _ = self.run_cli("task", "list")
        self.assertIn("Added in the browser", out)
        self.post("/offers/apply", {"job_id": "lever:globex-labs:abc"})
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
