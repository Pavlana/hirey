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
PAGES = ("/", "/offers", "/applications")
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
        for chart in ("donut", "treemap", "funnel", "columns"):
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
            app_id = db.add_application(conn, "<script>alert(1)</script>", "Engineer", status="applied")
            db.add_task(conn, "<b>x</b>", due_date="2026-01-01", application_id=app_id)
            db.add_comment(conn, app_id, "<img src=x onerror=alert(2)>")
            db.add_task(conn, "<img src=x onerror=alert(1)>")
        for path in PAGES:
            with self.subTest(path=path):
                body = self.get(path)[2]
                self.assertNotIn("<script>alert", body)
                self.assertNotIn("<img src=x", body)
        self.assertIn("&lt;script&gt;", self.get("/applications")[2])
        self.assertIn("&lt;img src=x onerror=alert(2)&gt;", self.get("/applications")[2])

    def test_pay_never_rendered(self) -> None:
        with self.conn() as conn:
            app_id = db.add_application(conn, "Paycorp", "Engineer", status="applied", pay=PAY)
            db.add_task(conn, "Pay-linked task", due_date="2026-01-01", application_id=app_id)
        for path in PAGES:
            with self.subTest(path=path):
                body = self.get(path)[2]
                if path == "/applications":
                    self.assertIn("Paycorp", body)
                self.assertNotIn(PAY, body)
        status, headers, body = self.post("/tasks/add", {"title": "Another", "kind": "prep"})
        self.assertNotIn(PAY, headers["Location"] + body)
        status, headers, body = self.post("/applications/move", {"app_id": str(app_id), "status": "technical"})
        self.assertNotIn(PAY, headers["Location"] + self.get(headers["Location"])[2])

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
        status, reply = self.post_json("/offers/apply", {"job_id": "lever:globex-labs:abc"})
        self.assertEqual((status, reply["ok"]), (200, True))
        self.assertNotIn("html", reply)
        with self.conn() as conn:
            app_id = db.find_by_job(conn, "lever:globex-labs:abc")["id"]
        self.assertEqual(reply["redirect"], f"/applications?app={app_id}")

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
        status, headers, _ = self.post("/offers/apply", {"job_id": "lever:globex-labs:abc", "return": "/offers?score=8"})
        with self.conn() as conn:
            app = db.find_by_job(conn, "lever:globex-labs:abc")
            tasks = [(t["title"], t["kind"], t["due_date"]) for t in db.list_tasks(conn) if t["application_id"] == app["id"]]
        self.assertEqual((status, headers["Location"]), (303, f"/applications?app={app['id']}&msg=applied"))
        self.assertEqual((app["company"], app["status"], app["date_applied"]), ("Globex Labs", "applied", TODAY))
        self.assertEqual(tasks, [("Chase if no reply", "admin", "2026-01-27")])
        _, _, body = self.get(headers["Location"])
        self.assertIn("Recorded as applied", body)
        self.assertIn("Chase if no reply", body)
        self.assertIn(f'<article class="app-card is-focus" id="app-{app["id"]}"><details open>', body)
        self.assertNotIn("Globex Labs", self.get("/offers")[2])

    def test_mark_applied_twice_does_not_duplicate(self) -> None:
        for _ in range(2):
            self.post("/offers/apply", {"job_id": "lever:globex-labs:abc"})
        with self.conn() as conn:
            self.assertEqual(sum(a["getjo_job_id"] == "lever:globex-labs:abc" for a in db.list_applications(conn)), 1)

    def test_mark_applied_bad_input(self) -> None:
        self.assertEqual(self.post("/offers/apply", {"job_id": "no:such:job"})[0], 400)
        self.assertEqual(self.post("/offers/apply", {})[0], 400)
        self.assertEqual(self.post("/offers/interested", {"job_id": "no:such:job"})[0], 400)

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
        self.assertEqual(status, 303)
        self.assertRegex(headers["Location"], r"^/applications\?app=\d+&msg=applied$")


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
        self.assertNotIn('action="/offers/interested"', body)
        self.assertEqual(self.post("/offers/skip", {"job_id": initech, "reason": "pay"})[0], 400)

        self.post("/offers/apply", {"job_id": initech})
        self.assertEqual(OfferFiltersTest.companies(self, "?show=applied"), {"Acme Rockets", "Initech Cloud"})
        self.assertEqual(OfferFiltersTest.companies(self, "?show=interested"), set())


class OfferActionsTest(WebTestCase):
    """Each new Job Offers row: Applied · Interested · Skipped, and nothing else."""
    GLOBEX = "lever:globex-labs:abc"

    def app(self) -> dict:
        with self.conn() as conn:
            return db.find_by_job(conn, self.GLOBEX)

    def tasks(self) -> list[tuple]:
        app_id = self.app()["id"]
        with self.conn() as conn:
            return [(t["title"], t["kind"], t["due_date"], t["status"])
                    for t in db.list_tasks(conn, None) if t["application_id"] == app_id]

    def test_row_has_exactly_three_actions(self) -> None:
        _, _, body = self.get("/offers")
        for gone in ('name="tier"', 'name="next_action"', 'name="next_date"', 'type="date"', "Follow up"):
            self.assertNotIn(gone, body)
        row = body[body.index("Globex Labs"):]
        row = row[:row.index("</tr>")]
        self.assertIn('action="/offers/apply"', row)
        self.assertIn('action="/offers/interested"', row)
        self.assertIn('href="/offers/skip?job_id=lever%3Aglobex-labs%3Aabc&amp;return=', row)
        self.assertIn(f'data-skip="{self.GLOBEX}" data-remove', row)
        self.assertEqual([b for b in ("Applied", "Interested", "Skipped") if f">{b}<" in row],
                         ["Applied", "Interested", "Skipped"])
        self.assertEqual(row.count("<button"), 2)  # Skipped is a link, not a third form

    def test_interested(self) -> None:
        status, headers, _ = self.post("/offers/interested", {"job_id": self.GLOBEX})
        app = self.app()
        self.assertEqual((status, headers["Location"]), (303, f"/applications?app={app['id']}&msg=interested"))
        self.assertEqual((app["status"], app["date_applied"]), ("to_apply", None))
        self.assertEqual(self.tasks(), [("Apply", "apply", "2026-01-23", "todo")])
        with self.conn() as conn:
            self.assertEqual(conn.execute("SELECT decision FROM job_decisions WHERE getjo_job_id = ?",
                                          (self.GLOBEX,)).fetchone()[0], "interested")
        self.assertEqual(OfferFiltersTest.companies(self, "?show=interested"), {"Globex Labs"})
        _, _, body = self.get(headers["Location"])
        self.assertIn("Saved as interested", body)
        self.assertIn(f'<article class="app-card is-focus" id="app-{app["id"]}"><details open>', body)

    def test_interested_then_applied(self) -> None:
        self.post("/offers/interested", {"job_id": self.GLOBEX})
        self.post("/offers/interested", {"job_id": self.GLOBEX})  # a repeat adds nothing
        self.assertEqual(len(self.tasks()), 1)
        self.post("/offers/apply", {"job_id": self.GLOBEX})
        app = self.app()
        self.assertEqual((app["status"], app["date_applied"]), ("applied", TODAY))
        self.assertIn(("Apply", "apply", "2026-01-23", "done"), self.tasks())
        self.assertIn(("Chase if no reply", "admin", "2026-01-27", "todo"), self.tasks())
        self.assertEqual(OfferFiltersTest.companies(self, "?show=applied"), {"Acme Rockets", "Globex Labs"})

    def test_interested_never_moves_an_application_back(self) -> None:
        self.post("/offers/apply", {"job_id": self.GLOBEX})
        with self.conn() as conn:
            db.update_application(conn, self.app()["id"], status="technical")
        self.post("/offers/interested", {"job_id": self.GLOBEX})
        self.assertEqual(self.app()["status"], "technical")
        self.assertEqual([t[0] for t in self.tasks()], ["Chase if no reply"])

    def test_skipped_job_cannot_be_tracked(self) -> None:
        self.post("/offers/skip", {"job_id": self.GLOBEX, "reason": "pay"})
        for path in ("/offers/apply", "/offers/interested"):
            with self.subTest(path=path):
                self.assertEqual(self.post(path, {"job_id": self.GLOBEX})[0], 400)
        self.assertIsNone(self.app())

    def test_skip_dialog(self) -> None:
        _, _, body = self.get("/offers")
        dialog = body[body.index('<dialog id="skip-dialog"'):body.index("</dialog>")]
        self.assertIn('action="/offers/skip"', dialog)
        for reason in db.SKIP_REASONS:
            self.assertIn(f'name="reason" value="{reason}"', dialog)
        self.assertIn('<textarea name="comment"', dialog)
        self.assertNotIn("skip-dialog", self.get("/offers?show=applied")[2])  # no Skipped links there

    def test_skip_page_without_js(self) -> None:
        status, _, body = self.get("/offers/skip?job_id=lever%3Aglobex-labs%3Aabc&return=%2Foffers%3Fscore%3D8")
        self.assertEqual(status, 200)
        self.assertIn("Globex Labs", body)
        self.assertIn('<select name="reason" required', body)
        self.assertIn('name="comment"', body)
        self.assertIn('name="return" value="/offers?score=8"', body)
        self.assertIn('href="/offers?score=8"', body)  # Cancel keeps the filters
        self.assertIn('aria-current="page"', body)
        with self.conn() as conn:
            self.assertNotIn(self.GLOBEX, db.job_decisions(conn))  # reading the form changes nothing
        status, headers, _ = self.post("/offers/skip", {"job_id": self.GLOBEX, "reason": "location",
                                                        "comment": "", "return": "/offers?score=8"})
        self.assertEqual((status, headers["Location"]), (303, "/offers?score=8&msg=skipped"))
        self.assertEqual(self.get("/offers/skip?job_id=no:such:job")[0], 400)
        self.assertEqual(self.get("/offers/skip?return=http://evil.example.com/")[0], 400)
        _, _, body = self.get(f"/offers/skip?job_id={self.GLOBEX}&return=http://evil.example.com/")
        self.assertIn('href="/offers"', body)

    def test_interested_label_everywhere(self) -> None:
        self.post("/offers/interested", {"job_id": self.GLOBEX})
        _, _, body = self.get("/")
        self.assertNotIn("To apply", body)
        self.assertNotIn("to apply", body)
        self.assertIn("Interested", body)
        _, out, _ = self.run_cli("apps", "--status", "to_apply")
        self.assertIn("[interested]", out)


class TasksTest(WebTestCase):
    def test_tasks_page_redirects_to_applications(self) -> None:
        status, headers, _ = self.get("/tasks")
        self.assertEqual((status, headers["Location"]), (303, "/applications"))
        self.assertEqual(self.get("/tasks?app=3")[1]["Location"], "/applications?app=3")
        body = self.get("/applications")[2]
        self.assertNotIn('href="/tasks"', body)
        self.assertNotIn("<span>Tasks</span>", body)

    def test_overdue_highlighted(self) -> None:
        _, _, body = self.get("/applications")
        self.assertIn('<span class="badge s4 overdue">2026-01-19</span>', body)
        self.assertIn("Write STAR story", body)  # recently done

    def test_add_and_done(self) -> None:
        status, headers, _ = self.post("/tasks/add", {
            "title": "Mock system design", "kind": "drill", "due_date": "2026-01-22", "application_id": "1",
            "return": "/applications?app=1",
        })
        self.assertEqual((status, headers["Location"]), (303, "/applications?app=1&msg=task_added"))
        with self.conn() as conn:
            task = next(t for t in db.list_tasks(conn) if t["title"] == "Mock system design")
        self.assertEqual((task["kind"], task["due_date"], task["application_id"]), ("drill", "2026-01-22", 1))
        self.assertIn("Mock system design", self.get("/applications")[2])

        status, headers, _ = self.post("/tasks/done", {"task_id": str(task["id"])})
        self.assertEqual((status, headers["Location"]), (303, "/applications?msg=task_done"))
        with self.conn() as conn:
            self.assertNotIn(task["id"], [t["id"] for t in db.list_tasks(conn)])
        self.assertIn("Task marked done", self.get(headers["Location"])[2])

        status, headers, _ = self.post("/tasks/reopen", {"task_id": str(task["id"])})
        self.assertEqual((status, headers["Location"]), (303, "/applications?msg=task_reopened"))
        with self.conn() as conn:
            self.assertIn(task["id"], [t["id"] for t in db.list_tasks(conn)])

    def test_general_tasks(self) -> None:
        status, headers, out = self.post("/tasks/add", {"title": "Update LinkedIn", "kind": "admin"},
                                         JsonActionsTest.JSON)
        self.assertEqual(status, 200)
        self.assertIn("Update LinkedIn", json.loads(out)["html"])
        body = self.get("/applications")[2]
        general = body[body.index("General tasks"):]
        self.assertIn("Update LinkedIn", general)
        self.assertIn('id="general-tasks"', general)
        with self.conn() as conn:
            app_titles = {t["title"] for t in db.list_tasks(conn) if t["application_id"]}
        for title in app_titles:
            self.assertNotIn(f"<td>{title}</td>", general.split("Recently done")[0])  # they stay on their cards

    def test_bad_task_input(self) -> None:
        for form in ({"title": ""}, {"title": "x", "kind": "nap"}, {"title": "x", "due_date": "monday"},
                     {"title": "x", "application_id": "999"}, {"title": "x", "application_id": "one"}):
            with self.subTest(form=form):
                self.assertEqual(self.post("/tasks/add", form)[0], 400)
        self.assertEqual(self.post("/tasks/done", {"task_id": "999"})[0], 400)
        self.assertEqual(self.post("/tasks/done", {})[0], 400)
        self.assertEqual(self.post("/tasks/reopen", {"task_id": "999"})[0], 400)
        self.assertEqual(self.post("/nope", {})[0], 404)


class ApplicationsBoardTest(WebTestCase):
    GLOBEX = "lever:globex-labs:abc"

    def app_id(self) -> int:
        self.post("/offers/apply", {"job_id": self.GLOBEX})
        with self.conn() as conn:
            return db.find_by_job(conn, self.GLOBEX)["id"]

    def open_tasks(self, app_id: int) -> list[tuple]:
        with self.conn() as conn:
            return [(t["title"], t["due_date"]) for t in db.app_tasks(conn).get(app_id, [])]

    def test_board_columns_and_closed_lane(self) -> None:
        body = self.get("/applications")[2]
        self.assertIn('<body class="theme-ochre">', body)
        labels = [web._status_label(s) for s in web.BOARD_STAGES]
        self.assertEqual(labels, ["Interested", "Applied", "Recruiter call", "Coding interview", "System design",
                                  "Company interview", "Offer"])
        positions = [body.index(f'<section class="lane" aria-label="{label}">') for label in labels]
        self.assertEqual(positions, sorted(positions))
        closed = body[body.index('<details class="closed-lane">'):]
        for label in ("Rejected", "No response", "Withdrawn"):
            self.assertIn(f'aria-label="{label}"', closed)

    def test_card_shows_days_next_task_and_comments(self) -> None:
        app_id = self.app_id()
        with self.conn() as conn:
            db.add_comment(conn, app_id, "Referral from a former colleague")
        body = self.get(f"/applications?app={app_id}")[2]
        card = body[body.index(f'id="app-{app_id}"'):]
        card = card[:card.index("</article>")]
        self.assertIn("Globex Labs", card)
        self.assertIn("new today", card)
        self.assertIn("Chase if no reply", card)
        self.assertIn('title="comments">', card)
        self.assertIn("Referral from a former colleague", card)
        self.assertEqual(body.count("is-focus"), 1)

    def test_days_in_stage(self) -> None:
        app_id = self.app_id()
        with self.conn() as conn:
            conn.execute("UPDATE status_history SET changed_at = '2026-01-15T09:00:00Z' WHERE application_id = ?",
                         (app_id,))
            conn.commit()
        body = self.get("/applications")[2]
        card = body[body.index(f'id="app-{app_id}"'):]
        self.assertIn("5 days in stage", card[:card.index("</summary>")])

    def test_unknown_focus_is_ignored(self) -> None:
        for bad in ("abc", "99999", ""):
            with self.subTest(app=bad):
                status, _, body = self.get(f"/applications?app={bad}")
                self.assertEqual(status, 200)
                self.assertNotIn("is-focus", body)

    def test_move_with_interview_date_and_comment(self) -> None:
        app_id = self.app_id()
        status, headers, _ = self.post("/applications/move", {
            "app_id": str(app_id), "status": "technical", "interview_date": "2026-01-26",
            "comment": "Two rounds, pairing",
        })
        self.assertEqual((status, headers["Location"]), (303, f"/applications?app={app_id}&msg=moved"))
        self.assertEqual(self.open_tasks(app_id), [("Coding practice", "2026-01-25"),
                                                   ("Review the role's tech stack", "2026-01-25")])
        with self.conn() as conn:
            self.assertEqual(db.get_application(conn, app_id)["status"], "technical")
            chase = conn.execute("SELECT status FROM tasks WHERE application_id = ? AND title = 'Chase if no reply'",
                                 (app_id,)).fetchone()[0]
            interview = conn.execute("SELECT date, stage FROM interviews WHERE application_id = ?",
                                     (app_id,)).fetchone()
        self.assertEqual(chase, "closed")
        self.assertEqual(tuple(interview), ("2026-01-26", "technical"))
        body = self.get(headers["Location"])[2]
        self.assertIn("Moved.", body)
        self.assertIn("Two rounds, pairing", body)
        lane = body[body.index('aria-label="Coding interview"'):]
        self.assertIn(f'id="app-{app_id}"', lane[:lane.index("</section>")])

    def test_move_back_and_to_closed(self) -> None:
        app_id = self.app_id()
        self.post("/applications/move", {"app_id": str(app_id), "status": "rejected"})
        self.assertEqual(self.open_tasks(app_id), [("Debrief — what to improve", "2026-01-23")])
        body = self.get(f"/applications?app={app_id}")[2]
        self.assertIn('<details class="closed-lane" open>', body)
        self.post("/applications/move", {"app_id": str(app_id), "status": "applied"})
        self.assertEqual(self.open_tasks(app_id), [("Chase if no reply", "2026-01-27")])

    def test_interview_date_ignored_for_other_stages(self) -> None:
        app_id = self.app_id()
        self.post("/applications/move", {"app_id": str(app_id), "status": "offer", "interview_date": "2026-01-26"})
        self.assertEqual(self.open_tasks(app_id), [("Review the offer", "2026-01-22")])
        with self.conn() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM interviews WHERE application_id = ?", (app_id,)).fetchone())

    def test_bad_moves_and_comments(self) -> None:
        app_id = str(self.app_id())
        for path, form in (("/applications/move", {"app_id": app_id, "status": "hired"}),
                           ("/applications/move", {"app_id": "999", "status": "offer"}),
                           ("/applications/move", {"status": "offer"}),
                           ("/applications/move", {"app_id": app_id, "status": "technical", "interview_date": "soon"}),
                           ("/applications/move", {"app_id": app_id, "status": "offer", "comment": "x" * 1001}),
                           ("/applications/comment", {"app_id": app_id, "body": ""}),
                           ("/applications/comment", {"app_id": app_id, "body": "x" * 1001})):
            with self.subTest(path=path, form=str(form)[:60]):
                self.assertEqual(self.post(path, form)[0], 400)

    def test_comment(self) -> None:
        app_id = self.app_id()
        status, headers, _ = self.post("/applications/comment", {"app_id": str(app_id), "body": "Take-home test sent"})
        self.assertEqual((status, headers["Location"]), (303, f"/applications?app={app_id}&msg=commented"))
        with self.conn() as conn:
            self.assertEqual([e["comment"] for e in db.timeline(conn, app_id)], [None, "Take-home test sent"])
        self.assertIn("Take-home test sent", self.get(headers["Location"])[2])

    def test_no_response_check_runs_on_a_request(self) -> None:
        app_id = self.app_id()
        with self.conn() as conn:
            conn.execute("UPDATE status_history SET changed_at = '2026-01-01T09:00:00Z' WHERE application_id = ?",
                         (app_id,))
            conn.commit()
        web.handle(self.settings, "GET", "/applications", HOST, today="2026-01-15")
        with self.conn() as conn:
            self.assertEqual(db.get_application(conn, app_id)["status"], "no_response")
            self.assertEqual(db.timeline(conn, app_id)[-1]["comment"], "auto: no reply in 14 days")

    def test_dashboard_counts_applications_not_tasks(self) -> None:
        body = self.get("/")[2]
        for gone in ("Overdue", "Tasks done", "Revise networking basics", 'class="chart line"'):
            self.assertNotIn(gone, body)


class SharedDatabaseTest(WebTestCase):
    def test_cli_write_visible_to_next_request(self) -> None:
        self.assertNotIn("Call the recruiter", self.get("/applications")[2])
        code, _, _ = self.run_cli("task", "add", "Call the recruiter", "--kind", "admin")
        self.assertEqual(code, 0)
        self.assertIn("Call the recruiter", self.get("/applications")[2])

        code, _, _ = self.run_cli("applied", "--job-id", "ashby:initech-cloud:x1")
        self.assertEqual(code, 0)
        self.assertNotIn("Initech Cloud", self.get("/offers")[2])

    def test_web_write_visible_to_cli(self) -> None:
        self.post("/tasks/add", {"title": "Added in the browser"})
        _, out, _ = self.run_cli("task", "list")
        self.assertIn("Added in the browser", out)
        self.post("/offers/apply", {"job_id": "lever:globex-labs:abc"})
        _, out, _ = self.run_cli("today")
        self.assertNotIn("lever:globex-labs:abc", out)  # no longer a new match


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

    def test_referrer_policy_keeps_same_origin_posts_working(self) -> None:
        # no-referrer would make browsers send "Origin: null" on plain form posts (refused).
        _, headers, _ = self.get("/offers")
        self.assertEqual(headers["Referrer-Policy"], "same-origin")
        status, _, _ = self.post("/offers/interested", {"job_id": "lever:globex-labs:abc"},
                                 {**HOST, "Origin": "http://localhost:8765", "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(status, 303)

    def test_non_loopback_bind_refused(self) -> None:
        for host in ("0.0.0.0", "192.168.1.10", "::", "::1", "example.com", ""):
            with self.subTest(host=host), self.assertRaises(ValueError):
                web.serve(self.settings, host, 0)
        web.check_loopback("127.0.0.1")  # does not raise

    def test_form_too_large(self) -> None:
        status, _, _ = web.handle(self.settings, "POST", "/tasks/add", HOST, b"x" * (web.MAX_BODY + 1))
        self.assertEqual(status, 400)
