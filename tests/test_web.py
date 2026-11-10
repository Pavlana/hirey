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
        panel = self.get(f"/applications?app={app_id}")[2]  # comments show in the side panel
        self.assertIn("&lt;img src=x onerror=alert(2)&gt;", panel)
        self.assertNotIn("<img src=x", panel)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", self.get("/applications")[2])

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
        status, headers, body = self.post("/applications/research", {"app_id": str(app_id), "kind": "research"})
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
        for path, form in (("/tasks/done", {"task_id": "999"}),
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
        self.assertIn(f'data-app="{app["id"]}" class="is-focus"', body)
        self.assertIn(f'<aside class="panel" id="panel" aria-labelledby="panel-title" data-app="{app["id"]}" data-status="applied">', body)
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
        self.assertNotIn('action="/offers/apply"', body)  # applying happens on the board now
        with self.conn() as conn:
            app_id = db.find_by_job(conn, initech)["id"]
        self.assertIn(f'href="/applications?app={app_id}">Open card</a>', body)
        self.assertNotIn('action="/offers/skip"', body)
        self.assertNotIn('action="/offers/interested"', body)
        self.assertEqual(self.post("/offers/skip", {"job_id": initech, "reason": "pay"})[0], 400)

        self.post("/offers/apply", {"job_id": initech})
        self.assertEqual(OfferFiltersTest.companies(self, "?show=applied"), {"Acme Rockets", "Initech Cloud"})
        self.assertEqual(OfferFiltersTest.companies(self, "?show=interested"), set())


class OfferActionsTest(WebTestCase):
    """Each new Job Offers row: Interested · Skipped, and nothing else (applying is on the board)."""
    GLOBEX = "lever:globex-labs:abc"

    def app(self) -> dict:
        with self.conn() as conn:
            return db.find_by_job(conn, self.GLOBEX)

    def tasks(self) -> list[tuple]:
        app_id = self.app()["id"]
        with self.conn() as conn:
            return [(t["title"], t["kind"], t["due_date"], t["status"])
                    for t in db.list_tasks(conn, None) if t["application_id"] == app_id]

    def test_row_has_interested_and_skipped_only(self) -> None:
        _, _, body = self.get("/offers")
        for gone in ('name="tier"', 'name="next_action"', 'name="next_date"', 'type="date"', "Follow up"):
            self.assertNotIn(gone, body)
        row = body[body.index("Globex Labs"):]
        row = row[:row.index("</tr>")]
        self.assertNotIn('action="/offers/apply"', row)
        self.assertIn('action="/offers/interested" class="inline" data-ajax="interested"', row)
        self.assertIn('href="/offers/skip?job_id=lever%3Aglobex-labs%3Aabc&amp;return=', row)
        self.assertIn(f'data-skip="{self.GLOBEX}" data-remove', row)
        self.assertEqual([b for b in ("Applied", "Interested", "Skipped") if f">{b}<" in row],
                         ["Interested", "Skipped"])
        self.assertEqual(row.count("<button"), 1)  # Skipped is a link, not a second form
        self.assertNotIn('action="/offers/apply"', self.get("/offers?show=all")[2])

    def test_interested_stays_on_job_offers(self) -> None:
        # without the script: back to Job Offers with the filters kept and a banner
        back = "/offers?score=7&loc=london&show=new"
        status, headers, _ = self.post("/offers/interested", {"job_id": self.GLOBEX, "return": back})
        app = self.app()
        self.assertEqual((status, headers["Location"]), (303, back + "&msg=interested"))
        self.assertEqual((app["status"], app["date_applied"]), ("to_apply", None))
        self.assertEqual(self.tasks(), [])  # no Apply task: the review task follows the agent's results
        with self.conn() as conn:
            self.assertEqual({r["kind"] for r in db.open_requests(conn, app["id"])}, {"research", "cv_fit"})
        _, _, body = self.get(headers["Location"])
        self.assertIn("Interested · research queued", body)
        self.assertIn('<body class="theme-dusty-blue">', body)

    def test_interested_json_swaps_the_row(self) -> None:
        status, _, out = self.post("/offers/interested", {"job_id": self.GLOBEX},
                                   {**HOST, "Accept": "application/json"})
        reply = json.loads(out)
        app = self.app()
        self.assertEqual(status, 200)
        self.assertNotIn("redirect", reply)
        self.assertEqual((reply["ok"], reply["app_id"], reply["created"]), (True, app["id"], True))
        self.assertIn('<span class="offer-state">Interested · research queued</span>', reply["html"])
        self.assertIn(f'href="/applications?app={app["id"]}">Open card</a>', reply["html"])

    def test_undo_interested(self) -> None:
        self.post("/offers/interested", {"job_id": self.GLOBEX})
        status, _, out = self.post("/offers/uninterested", {"job_id": self.GLOBEX},
                                   {**HOST, "Accept": "application/json"})
        self.assertEqual((status, json.loads(out)["message"]), (200, web.MESSAGES["uninterested"]))
        self.assertIsNone(self.app())
        with self.conn() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM job_decisions").fetchone())
            self.assertEqual(db.open_requests(conn), [])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM status_history WHERE status = 'to_apply' "
                                          "AND application_id NOT IN (SELECT id FROM applications)").fetchone()[0], 0)
        self.assertIn("Globex Labs", OfferFiltersTest.companies(self, ""))  # new again

    def test_undo_interested_refused_once_something_happened(self) -> None:
        cases = {
            "comment": lambda conn, a: db.add_comment(conn, a, "Called them"),
            "task": lambda conn, a: db.add_task(conn, "Ask a friend", "admin", None, a),
            "claimed": lambda conn, a: db.claim_request(conn, db.open_requests(conn, a)[0]["id"]),
            "tier": lambda conn, a: db.update_application(conn, a, tier="A"),
            "moved": lambda conn, a: db.move_application(conn, a, "applied", TODAY),
        }
        for name, change in cases.items():
            with self.subTest(name=name):
                self.post("/offers/interested", {"job_id": self.GLOBEX})
                app_id = self.app()["id"]
                with self.conn() as conn:
                    change(conn, app_id)
                status, _, out = self.post("/offers/uninterested", {"job_id": self.GLOBEX},
                                           {**HOST, "Accept": "application/json"})
                self.assertEqual(status, 400)
                self.assertIn("withdraw it from its card", json.loads(out)["message"])
                self.assertEqual(self.app()["id"], app_id)
                with self.conn() as conn:  # clean up for the next case
                    for table in ("tasks", "application_comments", "agent_requests", "status_history"):
                        conn.execute(f"DELETE FROM {table} WHERE application_id = ?", (app_id,))
                    conn.execute("DELETE FROM applications WHERE id = ?", (app_id,))
                    conn.execute("DELETE FROM job_decisions")
                    conn.commit()

    def test_undo_never_offered_for_an_application_that_existed(self) -> None:
        job_url = next(j["url"] for j in self.fixtures["getjo_jobs"] if j["id"] == self.GLOBEX)
        with self.conn() as conn:  # tracked by its link before, without the getjo id
            old, _ = db.record_application(conn, "Globex Labs", "Cloud Security Engineer", status="to_apply",
                                           link=job_url, today=TODAY)
        _, _, out = self.post("/offers/interested", {"job_id": self.GLOBEX}, {**HOST, "Accept": "application/json"})
        reply = json.loads(out)
        self.assertEqual((reply["app_id"], reply["created"]), (old, False))
        self.assertIn('<span class="offer-state">Interested · research queued</span>', reply["html"])
        with self.conn() as conn:  # once the agent has done its work, the label stops claiming a queue
            conn.execute("UPDATE agent_requests SET status = 'done'")
            conn.commit()
        _, _, out = self.post("/offers/interested", {"job_id": self.GLOBEX}, {**HOST, "Accept": "application/json"})
        self.assertIn('<span class="offer-state">Interested</span>', json.loads(out)["html"])
        with self.conn() as conn:
            self.assertEqual(conn.execute("SELECT decision FROM job_decisions WHERE getjo_job_id = ?",
                                          (self.GLOBEX,)).fetchone()[0], "interested")
        self.assertEqual(OfferFiltersTest.companies(self, "?show=interested"), {"Globex Labs"})

    def test_interested_then_applied(self) -> None:
        self.post("/offers/interested", {"job_id": self.GLOBEX})
        self.post("/offers/interested", {"job_id": self.GLOBEX})  # a repeat adds nothing
        self.assertEqual(len(self.tasks()), 0)
        with self.conn() as conn:
            self.assertEqual(len(db.open_requests(conn, self.app()["id"])), 2)
        self.post("/offers/apply", {"job_id": self.GLOBEX})  # the old endpoint still works (no button)
        app = self.app()
        self.assertEqual((app["status"], app["date_applied"]), ("applied", TODAY))
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

    def test_no_general_tasks_and_no_task_form(self) -> None:
        body = self.get("/applications")[2]
        for gone in ("General tasks", 'id="general-tasks"', "Recently done", 'action="/tasks/add"',
                     "Revise networking basics"):  # a general task: the agent and /today handle those
            self.assertNotIn(gone, body)
        self.assertEqual(self.post("/tasks/add", {"title": "x"})[0], 404)

    def test_done_and_reopen(self) -> None:
        with self.conn() as conn:
            task = db.add_task(conn, "Mock system design", "drill", "2026-01-19", 1)
        panel = self.get("/applications?app=1")[2]
        self.assertIn('<span class="badge s4 overdue">2026-01-19</span>', panel)
        status, headers, _ = self.post("/tasks/done", {"task_id": str(task), "return": "/applications?app=1"})
        self.assertEqual((status, headers["Location"]), (303, "/applications?app=1&msg=task_done"))
        with self.conn() as conn:
            self.assertNotIn(task, [t["id"] for t in db.list_tasks(conn)])
        self.assertIn("Task marked done", self.get(headers["Location"])[2])
        status, headers, _ = self.post("/tasks/reopen", {"task_id": str(task)})
        self.assertEqual((status, headers["Location"]), (303, "/applications?msg=task_reopened"))
        with self.conn() as conn:
            self.assertIn(task, [t["id"] for t in db.list_tasks(conn)])

    def test_bad_task_input(self) -> None:
        self.assertEqual(self.post("/tasks/done", {"task_id": "999"})[0], 400)
        self.assertEqual(self.post("/tasks/done", {})[0], 400)
        self.assertEqual(self.post("/tasks/reopen", {"task_id": "999"})[0], 400)
        self.assertEqual(self.post("/nope", {})[0], 404)


class ApplicationsPageTest(WebTestCase):
    GLOBEX = "lever:globex-labs:abc"
    JSON = {**HOST, "Accept": "application/json"}

    def app_id(self, status: str = "applied") -> int:
        path = "/offers/apply" if status == "applied" else "/offers/interested"
        self.post(path, {"job_id": self.GLOBEX})
        with self.conn() as conn:
            return db.find_by_job(conn, self.GLOBEX)["id"]

    def open_tasks(self, app_id: int) -> list[tuple]:
        with self.conn() as conn:
            return [(t["title"], t["due_date"]) for t in db.app_tasks(conn).get(app_id, [])]

    @staticmethod
    def row(body: str, app_id: int) -> str:
        start = body.index(f'<tr id="app-{app_id}"')
        return body[start:body.index("</tr>", start)]

    @staticmethod
    def panel(body: str) -> str:
        start = body.index('<aside class="panel"')
        return body[start:body.index("</aside>", start)]

    @staticmethod
    def table(body: str) -> str:
        start = body.index('<table id="app-list">')
        return body[start:body.index("</table>", start)]

    def test_list_columns_and_stage_groups(self) -> None:
        body = self.get("/applications")[2]
        self.assertIn('<body class="theme-ochre">', body)
        table = self.table(body)
        head = table[:table.index("</thead>")]
        labels = [c.split(">", 1)[1].split("<", 1)[0] for c in head.split("<th scope")[1:]]
        self.assertEqual(labels, ["Company", "Role", "Stage", "Next task", "Comment"])  # no due, days, fit or tier
        groups = [g.split('"', 1)[0] for g in table.split('<tbody data-group="')[1:]]
        self.assertEqual(groups, ["to_apply", "applied", "recruiter_call", "technical"])  # pipeline order, open only
        self.assertIn('scope="rowgroup">Coding interview <span class="count">1</span>', table)
        self.assertNotIn("Cyberdyne Example", table)  # closed: behind Show closed
        self.assertIn('href="/applications?closed=1" data-closed-toggle>Show closed</a>', body)
        self.assertIn(f'data-href="/applications?app=3"', self.row(body, 3))
        rows = [{"app": {"id": i}, "next": n, "days": d} for i, n, d in (
            (1, None, 9), (2, {"due_date": None}, 3), (3, {"due_date": "2026-01-25"}, 1),
            (4, {"due_date": "2026-01-21"}, 2), (5, {"due_date": "2026-01-25"}, 8))]
        self.assertEqual([r["app"]["id"] for r in web.list_order(rows)], [4, 5, 3, 2, 1])

    def test_no_board_markup(self) -> None:
        body = self.get("/applications?app=3&view=board")[2]
        for gone in ("data-board", "app-card", "draggable", "data-drop", "move-dialog", "view-toggle",
                     'action="/applications/move"', 'action="/applications/comment"', 'action="/applications/tier"',
                     'action="/tasks/add" class="add add-sm"', "Next stage", "Move to"):
            self.assertNotIn(gone, body)
        js = self.get("/static/app.js")[2]
        for gone in ("dragstart", "move-dialog", "applications-view"):
            self.assertNotIn(gone, js)
        for path in ("/applications/move", "/applications/comment", "/applications/tier"):
            self.assertEqual(self.post(path, {"app_id": "3", "status": "offer"})[0], 404)

    def test_show_closed_and_stage_filter(self) -> None:
        body = self.get("/applications?closed=1")[2]
        table = self.table(body)
        self.assertIn("Cyberdyne Example", table)
        self.assertGreater(table.index('data-group="rejected"'), table.index('data-group="technical"'))
        self.assertIn('href="/applications" data-closed-toggle>Hide closed</a>', body)
        body = self.get("/applications?stage=interviewing")[2]
        table = self.table(body)
        self.assertIn("Acme Rockets", table)
        self.assertNotIn("Tyrell Robotics", table)
        self.assertIn('class="stage-pill is-active" href="/applications" data-stage="interviewing"', body)
        self.assertIn("Cyberdyne Example", self.table(self.get("/applications?stage=closed")[2]))
        for bad in ("nope", "rejected"):
            self.assertEqual(web.list_filters({"stage": [bad], "view": ["board"], "closed": ["yes"]}),
                             {"app": None, "stage": "", "closed": False})

    def test_comment_column_shows_the_latest_and_the_count(self) -> None:
        with self.conn() as conn:
            db.add_comment(conn, 2, "Referral from a former colleague")
            db.add_comment(conn, 2, "Sent portfolio link")
        row = self.row(self.get("/applications")[2], 2)
        self.assertIn('title="Sent portfolio link">Sent portfolio link</span>', row)
        self.assertIn('<span class="count" title="2 comments">+1</span>', row)
        self.assertNotIn("Referral", row)

    def test_panel_order_and_header(self) -> None:
        with self.conn() as conn:
            db.add_comment(conn, 3, "Older note")
            conn.execute("UPDATE application_comments SET created_at = '2026-01-10T09:00:00Z'")
            conn.commit()
            db.add_comment(conn, 3, "Newer note")
            conn.execute("UPDATE status_history SET changed_at = '2026-01-15T09:00:00Z' WHERE application_id = 3")
            conn.commit()
        panel = self.panel(self.get("/applications?app=3&stage=interviewing")[2])
        head = panel[:panel.index("</header>")]
        for part in ("Wayne Analytics", "AI Platform Engineer", ">coding interview</span>", 'class="steps"',
                     "5 days in this stage", "Job posting ↗"):
            self.assertIn(part, head)
        order = [panel.index(s) for s in ('class="panel-comments"', 'class="shortcuts"', 'class="panel-tasks"',
                                           'class="history"')]
        self.assertEqual(order, sorted(order))
        comments = panel[panel.index('class="panel-comments"'):panel.index('class="shortcuts"')]
        self.assertIn("Newer note", comments)  # the latest only
        self.assertNotIn("Older note", comments)
        self.assertIn("1 earlier comment; ask the agent to list them.", comments)
        self.assertNotIn("<form", comments)  # read-only
        self.assertIn('href="/applications?stage=interviewing" aria-label="Close"', panel)
        self.assertIn('name="return" value="/applications?app=3&amp;stage=interviewing"', panel)
        for gone in ("Set tier", "Confirm", 'name="tier"', 'type="date"', "Company and CV fit"):
            self.assertNotIn(gone, panel)

    def test_layout_a_fits_the_viewport_on_laptops(self) -> None:
        css = self.get("/static/style.css")[2]
        laptop = css[css.index("/* Layout A (laptop and up)"):]
        for rule in ("main:has(.apps) { height: 100vh;", ".card-pipeline { flex: 0 1 auto; max-height: 45%; }", ".card-pipeline .stage-strip { flex: none; }",
                     ".card-agent { flex: 1 1 0;", ".card-agent form.ask { flex: none; }"):
            self.assertIn(rule, laptop)
        self.assertLess(css.index("@media (min-width: 801px) {", css.index("/* Layout A")),
                        css.index("main:has(.apps) { height: 100vh;"), "laptop and up only; phones stay stacked")

    def test_unknown_focus_is_ignored(self) -> None:
        for bad in ("abc", "99999", ""):
            with self.subTest(app=bad):
                status, _, body = self.get(f"/applications?app={bad}")
                self.assertEqual(status, 200)
                self.assertNotIn("is-focus", body)
                self.assertNotIn('<aside class="panel"', body)

    def test_shortcuts_for_interested(self) -> None:
        app_id = self.app_id("interested")
        panel = self.panel(self.get(f"/applications?app={app_id}")[2])
        shortcuts = panel[panel.index('class="shortcuts"'):panel.index('class="panel-tasks"')]
        labels = ["Researching…", "Checking CV fit…", "Tailor CV", "Mark applied", ">Skip<"]
        self.assertEqual([shortcuts.index(l) for l in labels], sorted(shortcuts.index(l) for l in labels))
        self.assertIn('action="/applications/apply" class="inline" data-ajax="applied"', shortcuts)
        self.assertIn(f'href="/applications/skip?app={app_id}&amp;return=', shortcuts)
        self.assertIn(f'data-skip="{app_id}"', shortcuts)
        self.assertIn('<dialog id="skip-dialog"', self.get("/applications")[2])
        self.assertIn('action="/applications/skip" class="skip-form" data-field="app_id"', self.get("/applications")[2])

    def test_shortcuts_after_applying(self) -> None:
        panel = self.panel(self.get("/applications?app=3")[2])  # Wayne, coding interview
        shortcuts = panel[panel.index('class="shortcuts"'):panel.index('class="panel-tasks"')]
        self.assertIn("Research company", shortcuts)
        self.assertIn("Applied · ", shortcuts)
        self.assertNotIn("Mark applied", shortcuts)
        self.assertNotIn("data-skip", shortcuts)

    def test_mark_applied_and_undo(self) -> None:
        app_id = self.app_id("interested")
        status, _, out = self.post("/applications/apply", {"app_id": str(app_id)}, self.JSON)
        reply = json.loads(out)
        self.assertEqual((status, reply["ok"], reply["app_id"]), (200, True, app_id))
        with self.conn() as conn:
            app = db.get_application(conn, app_id)
            self.assertEqual((app["status"], app["date_applied"]), ("applied", TODAY))
        self.assertEqual(self.open_tasks(app_id), [("Chase if no reply", "2026-01-27")])
        status, _, out = self.post("/applications/unapply", {"app_id": str(app_id)}, self.JSON)
        self.assertEqual((status, json.loads(out)["message"]), (200, web.MESSAGES["unapplied"]))
        with self.conn() as conn:
            app = db.get_application(conn, app_id)
            self.assertEqual((app["status"], app["date_applied"]), ("to_apply", None))
            self.assertEqual(db.status_history(conn, app_id), ["to_apply"])
        self.assertEqual(self.open_tasks(app_id), [])
        # a plain post (no script) redirects back to the panel
        status, headers, _ = self.post("/applications/apply", {"app_id": str(app_id)})
        self.assertEqual((status, headers["Location"]), (303, f"/applications?app={app_id}&msg=applied_today"))
        self.assertEqual(self.post("/applications/apply", {"app_id": str(app_id)})[0], 400)  # no longer Interested

    def test_skip_json_and_without_script(self) -> None:
        app_id = self.app_id("interested")
        status, _, body = self.get(f"/applications/skip?app={app_id}&return=/applications?app={app_id}")
        self.assertEqual(status, 200)
        self.assertIn('<form method="post" action="/applications/skip"', body)
        self.assertIn('<option value="location">', body)
        with self.conn() as conn:
            self.assertEqual(db.get_application(conn, app_id)["status"], "to_apply")  # reading changes nothing
        self.assertEqual(self.get("/applications/skip?app=999")[0], 404)
        self.assertEqual(self.post("/applications/skip", {"app_id": str(app_id)}, self.JSON)[0], 400)  # no reason
        status, _, out = self.post("/applications/skip", {"app_id": str(app_id), "reason": "location",
                                                          "comment": "On-site only"}, self.JSON)
        self.assertEqual((status, json.loads(out)["message"]), (200, web.MESSAGES["app_skipped"]))
        with self.conn() as conn:
            self.assertEqual(db.get_application(conn, app_id)["status"], "skipped")
            decision = db.job_decisions(conn)[self.GLOBEX]
        self.assertEqual((decision["decision"], decision["reason"], decision["comment"]),
                         ("skipped", "location", "On-site only"))
        other = db.record_application  # a second Interested application, skipped without the script
        with self.conn() as conn:
            plain, _ = other(conn, "Initech", "Engineer", status="to_apply", today=TODAY)
        status, headers, _ = self.post("/applications/skip", {"app_id": str(plain), "reason": "stack",
                                                              "return": f"/applications?app={plain}"})
        self.assertEqual((status, headers["Location"]), (303, f"/applications?app={plain}&msg=app_skipped"))
        offers = self.get("/offers?show=skipped")[2]
        self.assertIn("Globex Labs", offers)
        panel = self.panel(self.get(f"/applications?app={app_id}")[2])
        self.assertIn(">Skipped</button>", panel)
        self.assertIn("skipped: location — On-site only", panel)
        self.assertIn("Globex Labs", self.table(self.get("/applications?closed=1")[2]))

    def test_task_tick(self) -> None:
        panel = self.panel(self.get("/applications?app=3")[2])
        tasks = panel[panel.index('class="panel-tasks"'):]
        self.assertIn('action="/tasks/done" class="inline" data-ajax="tick"', tasks)
        self.assertIn('<button class="tick" aria-label="Mark done: ', tasks)
        task_id = int(tasks.split('name="task_id" value="')[1].split('"')[0])
        status, headers, _ = self.post("/tasks/done", {"task_id": str(task_id), "return": "/applications?app=3"})
        self.assertEqual(headers["Location"], "/applications?app=3&msg=task_done")
        with self.conn() as conn:
            self.assertEqual(db.get_task(conn, task_id)["status"], "done")

    def test_results_cards(self) -> None:
        private = self.tmp / "private"
        (private / "cv-tailored").mkdir(parents=True)
        with self.conn() as conn:
            db.update_application(conn, 3, role_short="AIEng")
            for n in (1, 2):
                path = private / "cv-tailored" / f"CV_WayneAnalytics_AIEng_200126_{n}.md"
                path.write_text("+++\njob = \"x\"\n+++\nCV\n", encoding="utf-8")
                db.save_tailored_cv(conn, 3, str(path), f"v{n}")
            db.record_cv_sent(conn, 3, "CV_WayneAnalytics_AIEng_200126_1")
        panel = self.panel(self.get("/applications?app=3")[2])
        self.assertIn('data-jump="card-tailor">CV v2 · sent v1</a>', panel)
        card = panel[panel.index('id="card-tailor"'):]
        self.assertLess(card.index("_200126_2.md"), card.index("_200126_1.md"))  # newest first
        self.assertIn('/research/file?app=3&amp;kind=tailor&amp;v=1', card)
        sent = card[card.index("_200126_1.md"):]
        self.assertIn(">sent</span>", sent[:sent.index("</li>")])
        status, _, body = self.get("/research/file?app=3&kind=tailor&v=1")
        self.assertEqual(status, 200)
        self.assertIn("CV_WayneAnalytics_AIEng_200126_1.md", body)
        self.assertEqual(self.get("/research/file?app=3&kind=tailor&v=9")[0], 404)
        self.assertEqual(self.get("/research/file?app=3&kind=cv_sent")[0], 404)

    def test_polling_covers_agent_requests(self) -> None:
        app_id = self.app_id("interested")  # research and CV fit queued
        reply = json.loads(self.get(f"/applications/thread?app={app_id}")[2])
        self.assertTrue(reply["pending"])
        self.assertIn(" data-busy", self.panel(self.get(f"/applications?app={app_id}")[2])[:300])
        with self.conn() as conn:
            db.finish_requests(conn, [app_id], "research")
            db.finish_requests(conn, [app_id], "cv_fit")
        self.assertFalse(json.loads(self.get(f"/applications/thread?app={app_id}")[2])["pending"])

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
        self.assertNotIn("Call the recruiter", self.get("/applications?app=1")[2])
        code, _, _ = self.run_cli("task", "add", "Call the recruiter", "--kind", "admin", "--app", "1")
        self.assertEqual(code, 0)
        self.assertIn("Call the recruiter", self.get("/applications?app=1")[2])

        code, _, _ = self.run_cli("applied", "--job-id", "ashby:initech-cloud:x1")
        self.assertEqual(code, 0)
        self.assertNotIn("Initech Cloud", self.get("/offers")[2])

    def test_web_write_visible_to_cli(self) -> None:
        with self.conn() as conn:
            task = db.add_task(conn, "Ticked in the browser", application_id=1)
        self.post("/tasks/done", {"task_id": str(task)})
        _, out, _ = self.run_cli("task", "list", "--done")
        self.assertIn("Ticked in the browser ✓", out)
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
        form = {"app_id": "1", "kind": "research"}
        for extra in ({"Origin": "http://evil.example.com"}, {"Origin": "null"},
                      {"Origin": "http://localhost:9999"}, {"Sec-Fetch-Site": "cross-site"},
                      {"Origin": "http://localhost:8765", "Sec-Fetch-Site": "same-site"}):
            with self.subTest(extra=extra):
                self.assertEqual(self.post("/applications/research", form, {**HOST, **extra})[0], 403)
        with self.conn() as conn:
            self.assertNotIn("research", [r["kind"] for r in db.open_requests(conn, 1)])
        ok = {**HOST, "Origin": "http://localhost:8765", "Sec-Fetch-Site": "same-origin"}
        self.assertEqual(self.post("/applications/research", form, ok)[0], 303)

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
        status, _, _ = web.handle(self.settings, "POST", "/applications/message", HOST, b"x" * (web.MAX_BODY + 1))
        self.assertEqual(status, 400)
