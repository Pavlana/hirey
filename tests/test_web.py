import json
import os
import unittest
from contextlib import closing
from unittest import mock
from urllib.parse import quote, urlencode

from prep import db, web
from prep.config import load_settings
from prep.seed_example import build_getjo_db, build_prep_db
from tests.helpers import IsolatedTestCase

TODAY = "2026-01-20"  # the fixtures' anchor date
HOST = {"Host": "localhost:8765"}
PAGES = ("/", "/?view=offers", "/dashboard")
PAY = "PAY-MARKER-31337"
h = web.h


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
        _, _, body = self.get("/dashboard")
        self.assertIn("2 / 10", body)  # applied 2026-01-14 and 2026-01-16 in the last 7 days
        self.assertIn("43%", body)  # 3 of 7 replied
        self.assertNotIn('class="warning"', body)

    def test_reply_rate_warning_after_25(self) -> None:
        with self.conn() as conn:
            for i in range(25):
                db.add_application(conn, f"Example {i}", "Engineer", status="applied", date_applied="2026-01-02")
        _, _, body = self.get("/dashboard")
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
                self.assertIn(f'<a href="{path.split("?")[0]}" aria-current="page">', body)

    def test_dashboard_charts(self) -> None:
        body = self.get("/dashboard")[2]
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
        self.assertEqual((status, headers["Location"]), (303, "/dashboard"))

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
        self.assertIn("&lt;script&gt;", self.get("/")[2])
        panel = self.get(f"/?app={app_id}")[2]  # comments show in the side panel
        self.assertIn("&lt;img src=x onerror=alert(2)&gt;", panel)
        self.assertNotIn("<img src=x", panel)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", self.get("/")[2])

    def test_pay_never_rendered(self) -> None:
        with self.conn() as conn:
            app_id = db.add_application(conn, "Paycorp", "Engineer", status="applied", pay=PAY)
            db.add_task(conn, "Pay-linked task", due_date="2026-01-01", application_id=app_id)
        for path in PAGES:
            with self.subTest(path=path):
                body = self.get(path)[2]
                if path == "/":
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
        self.assertEqual(reply["redirect"], f"/?app={app_id}")

    def test_errors_are_json(self) -> None:
        for path, form in (("/tasks/done", {"task_id": "999"}),
                           ("/tasks/reopen", {}), ("/offers/apply", {"job_id": "no:such:job"})):
            with self.subTest(path=path):
                status, reply = self.post_json(path, form)
                self.assertEqual(status, 400)
                self.assertFalse(reply["ok"])
                self.assertTrue(reply["message"])


GLOBEX = "lever:globex-labs:abc"  # score 8, Remote, not listed by getjo since 12 Jan: "may be closed"
WONKA = "ashby:wonka-systems:w2"  # score 7, London / Remote, expires on 24 Jan
OSCORP = "greenhouse:oscorp-ai:42"  # closed_at set
PIED = "lever:pied-piper:pp7"  # expires_at passed
NEW_OFFERS = ["Wonka Systems", "Initech Cloud", "Globex Labs"]  # newest day first


def job_url(job_id: str, view: str = "offers") -> str:
    return "/?" + urlencode([("view", view), ("job", job_id)])


def row_url(job_id: str) -> str:
    """A New offers row's link: opening it ticks the job, since nothing else is ticked."""
    return "/?" + urlencode([("view", "offers"), ("job", job_id), ("sel", f"j:{job_id}")])


class OffersViewTest(WebTestCase):
    """The New offers view in the left column of Applications (?view=offers)."""

    def companies(self, query: str = "") -> list[str]:
        status, _, body = self.get("/?view=offers" + query)
        self.assertEqual(status, 200)
        if '<table id="offer-list">' not in body:
            return []
        table = body[body.index('<table id="offer-list">'):]
        table = table[:table.index("</table>")]
        return [cell.split(">", 1)[1].split("</a>", 1)[0] for cell in table.split("<td><a href=")[1:]]

    def test_lists_open_untracked_offers_newest_first(self) -> None:
        self.assertEqual(self.companies(), NEW_OFFERS)  # not Acme (tracked), Hooli (6), Oscorp (closed), Pied Piper (expired)
        body = self.get("/?view=offers")[2]
        self.assertIn('data-view="offers" aria-current="true">New offers <span class="count">3</span></a>', body)
        self.assertIn('data-view="pipeline">Pipeline <span class="count">6</span></a>', body)
        self.assertIn('<span class="nav-count" title="New offers">3</span>', body)
        self.assertIn("6 open · 2 closed · 3 new offers", body)
        head = body[body.index('<table id="offer-list">'):body.index("</thead>")]
        self.assertEqual([c.split(">", 1)[1].split("<", 1)[0] for c in head.split("<th scope")[1:]],
                         ["", "Score", "Company", "Role", "Location"])  # the tick column first
        row = body[body.index(f'data-job="{WONKA}"'):]
        self.assertIn(f'data-href="{h(row_url(WONKA))}"', body)
        self.assertNotIn("<form", row[:row.index("</tr>")])  # the panel holds the actions
        same_day = [{"id": "b", "first_seen": "2026-01-18T09:00:00Z", "score": 7},
                    {"id": "a", "first_seen": "2026-01-18T08:00:00Z", "score": 9},
                    {"id": "c", "first_seen": "2026-01-19T08:00:00Z", "score": 6}]
        found = {"new": same_day, "decisions": {}}
        f = web.home_filters({})
        self.assertEqual([j["id"] for j, _ in web.offer_rows(found, {**f, "q": "", "loc": ()})], ["c", "a", "b"])

    def test_filters(self) -> None:
        cases = {
            "": NEW_OFFERS,
            "&score=8": ["Globex Labs"],
            "&score=6": ["Wonka Systems", "Hooli Data", "Initech Cloud", "Globex Labs"],
            "&q=GLOB": ["Globex Labs"],
            "&q=devops": ["Initech Cloud"],  # the role
            "&loc=london": ["Wonka Systems"],
            "&loc=remote": ["Wonka Systems", "Globex Labs"],
            "&loc=other": ["Initech Cloud"],
            "&loc=london&loc=other": ["Wonka Systems", "Initech Cloud"],
            "&score=abc&loc=mars": NEW_OFFERS,  # bad values: defaults
            "&score=99": NEW_OFFERS,
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(self.companies(query), expected)

    def test_count_follows_the_minimum_score_only(self) -> None:
        self.assertIn('New offers <span class="count">1</span>', self.get("/?view=offers&score=8")[2])
        body = self.get("/?view=offers&q=globex&loc=london")[2]
        self.assertIn('New offers <span class="count">3</span>', body)
        self.assertIn("Nothing matches these filters.", body)
        self.assertIn('<span class="nav-count" title="New offers">3</span>', self.get("/dashboard")[2])

    def test_filter_form_keeps_values(self) -> None:
        body = self.get("/?view=offers&score=8&q=%3Cb%3E&loc=remote")[2]
        form = body[body.index('class="offer-filters"'):body.index("</form>", body.index('class="offer-filters"'))]
        self.assertIn('<input type="hidden" name="view" value="offers">', form)
        self.assertIn('<option value="8" selected>', form)
        self.assertIn('name="q" value="&lt;b&gt;"', form)
        self.assertIn('value="remote" checked', form)
        head = body[body.index('class="card-head"'):body.index("</header>", body.index('class="card-head"'))]
        self.assertIn('href="/?view=offers&amp;score=8&amp;loc=remote&amp;q=%3Cb%3E&amp;skipped=1">Show skipped</a>', head)
        self.assertIn('data-live-filters', form)

    def test_chips(self) -> None:
        body = self.get("/?view=offers")[2]
        row = lambda job: body[body.index(f'data-job="{job}"'):body.index("</tr>", body.index(f'data-job="{job}"'))]
        self.assertIn(">may be closed</span>", row(GLOBEX))
        self.assertIn('<span class="badge badge-warning">expires in 4 days</span>', row(WONKA))
        self.assertNotIn("badge-", row("ashby:initech-cloud:x1").replace("badge s", ""))
        with mock.patch.dict(os.environ, {"PREP_STALE_DAYS": "30"}):
            self.settings = load_settings()
            self.assertNotIn("may be closed", self.get("/?view=offers")[2])

    def test_without_getjo_db(self) -> None:
        for value in (str(self.tmp / "missing.db"), ""):
            with self.subTest(getjo=value), mock.patch.dict(os.environ, {"PREP_GETJO_DB": value}):
                self.settings = load_settings()
                status, _, body = self.get("/?view=offers")
                self.assertEqual(status, 200)
                self.assertIn("not available", body)
                self.assertIn('data-view="offers" aria-current="true">New offers</a>', body)  # no count
                self.assertNotIn("nav-count", body)
                status, _, body = self.get("/")
                self.assertEqual(status, 200)
                self.assertIn("Wayne Analytics", body)  # the pipeline does not need getjo
                self.assertEqual(self.post("/offers/apply", {"job_id": GLOBEX})[0], 400)

    def test_getjo_file_that_is_not_a_jobs_db(self) -> None:
        broken = self.tmp / "broken.db"
        broken.write_bytes(b"not a database")
        with mock.patch.dict(os.environ, {"PREP_GETJO_DB": str(broken)}):
            self.settings = load_settings()
            status, _, body = self.get("/?view=offers")
        self.assertEqual(status, 200)
        self.assertIn("Could not read", body)

    def test_old_pages_redirect(self) -> None:
        cases = {
            "/offers": "/?view=offers",
            "/matches": "/?view=offers",
            "/matches?score=8&loc=remote": "/?view=offers&score=8&loc=remote",
            "/offers?score=8&company=GLOB&role=x&loc=remote&show=skipped": "/?view=offers&score=8&loc=remote&q=GLOB&skipped=1",
            "/offers?role=devops&show=new": "/?view=offers&q=devops",
            "/offers?show=interested": "/?stage=interested",
            "/offers?show=applied&score=9": "/?stage=applied",
            "/offers?score=99&loc=mars": "/?view=offers",
            "/applications": "/",
            "/applications?app=3&stage=interviewing": "/?app=3&stage=interviewing",
            "/tasks?app=3": "/?app=3",
            "/progress": "/dashboard",
        }
        for path, location in cases.items():
            with self.subTest(path=path):
                status, headers, _ = self.get(path)
                self.assertEqual((status, headers["Location"]), (303, location))
        status, headers, _ = self.post("/matches/apply", {"job_id": GLOBEX})
        self.assertEqual(status, 303)
        self.assertRegex(headers["Location"], r"^/\?app=\d+&msg=applied$")

    def test_return_keeps_filters_and_maps_old_pages(self) -> None:
        cases = {
            "/?view=offers&score=8&loc=remote&msg=applied": "/?view=offers&score=8&loc=remote&msg=skipped",
            "/offers?score=8&loc=remote": "/?view=offers&score=8&loc=remote&msg=skipped",  # a form from before the merge
            "/?view=offers&q=a%0d%0aSet-Cookie:x": "/?view=offers&q=a%0D%0ASet-Cookie%3Ax&msg=skipped",
            "http://evil.example.com/?view=offers": "/?msg=skipped",
            "//evil.example.com/offers": "/?msg=skipped",
            "/research/file?app=1": "/?msg=skipped",
        }
        for value, location in cases.items():
            with self.subTest(value=value):
                with self.conn() as conn:
                    if GLOBEX in db.job_decisions(conn):
                        db.undo_skip(conn, GLOBEX)
                status, headers, _ = self.post("/offers/skip", {"job_id": GLOBEX, "reason": "pay", "return": value})
                self.assertEqual((status, headers["Location"]), (303, location))


class OfferPanelTest(WebTestCase):
    """The side panel for a job not yet tracked (?job=), and its actions."""

    def app(self, job_id: str = GLOBEX) -> dict | None:
        with self.conn() as conn:
            return db.find_by_job(conn, job_id)

    def tasks(self) -> list[tuple]:
        app_id = self.app()["id"]
        with self.conn() as conn:
            return [(t["title"], t["kind"], t["due_date"], t["status"])
                    for t in db.list_tasks(conn, None) if t["application_id"] == app_id]

    @staticmethod
    def panel(body: str) -> str:
        start = body.index('<aside class="panel')
        return body[start:body.index("</aside>", start)]

    def test_header_actions_and_description(self) -> None:
        body = self.get(job_url(WONKA))[2]
        panel = self.panel(body)
        # opening it queued its brief (score 7, under brief_auto_min_score): the panel polls until it is in
        self.assertIn(f'<aside class="panel panel-offer" id="panel" aria-labelledby="panel-title" data-job="{WONKA}" data-busy>', panel)
        head = panel[:panel.index("</header>")]
        for part in ("Wonka Systems", "Senior Python Engineer", '<span class="badge s6">7</span>', "London / Remote",
                     "seen 18 Jan", "Job posting ↗", "expires in 4 days", 'href="/?view=offers" aria-label="Close"'):
            self.assertIn(part, head)
        shortcuts = panel[panel.index('class="shortcuts"'):panel.index('class="panel-brief"')]
        self.assertIn('action="/offers/interested" class="inline" data-ajax="interested"', shortcuts)
        self.assertIn(f'name="return" value="{h(job_url(WONKA))}"', shortcuts)
        self.assertIn('data-skip-field="job_id" data-skip-action="/offers/skip"', shortcuts)
        self.assertIn('href="/offers/skip?job_id=ashby%3Awonka-systems%3Aw2&amp;return=', shortcuts)
        description = panel[panel.index('class="description'):]
        self.assertIn("<h4>About the team</h4><p>A fictional team building made-up tools.\nPython, Postgres and AWS.</p>",
                      description)
        self.assertIn("<h4>What you will do</h4><ul><li>Build services</li><li>Review code</li></ul>", description)
        self.assertIn("Writing brief…", panel)
        self.assertLess(panel.index('class="shortcuts"'), panel.index('class="panel-brief"'))  # decide at the top
        self.assertIn(f'data-job="{WONKA}" data-key="j:{WONKA}" data-company="wonka systems" data-role="senior python engineer" class="is-focus"', body)
        about = body[body.index('class="agent-about"'):]
        self.assertIn("the whole search", about[:400])  # opening only shows it; the row link ticks it
        self.assertIn(f'data-sel="j:{WONKA}" aria-label="Ask the agent about Wonka Systems — Senior Python Engineer"', head)
        self.assertIn('data-view="offers" aria-current="true"', body)  # the list stays New offers

    def test_description_is_escaped_and_missing_ones_say_so(self) -> None:
        jobs = [*self.fixtures["getjo_jobs"],
                {"id": "lever:evil:1", "source": "lever", "company": "<b>Evil</b>", "title": "<i>Role</i>",
                 "location": "Remote", "url": "javascript:alert(1)", "description": "<script>alert(1)</script>\n\nok",
                 "first_seen": "2026-01-19T08:00:00Z", "score": 9},
                {"id": "lever:empty:1", "source": "lever", "company": "Empty Co", "title": "SRE", "location": "Remote",
                 "url": "https://jobs.example.com/empty", "description": "", "first_seen": "2026-01-19T08:00:00Z",
                 "score": 9}]
        build_getjo_db(self.getjo_db, jobs)
        panel = self.panel(self.get(job_url("lever:evil:1"))[2])
        self.assertNotIn("<script>", panel)
        self.assertNotIn("<b>Evil", panel)
        self.assertIn("<p>&lt;script&gt;alert(1)&lt;/script&gt;</p><p>ok</p>", panel)
        self.assertNotIn("javascript:", panel)
        self.assertIn("No description in getjo", self.panel(self.get(job_url("lever:empty:1"))[2]))
        self.assertEqual(web._description("Learn Fast &#xa;&#xa; Job Description: R&amp;D &lt;b&gt;"),
                         "<h4>Learn Fast</h4><p>Job Description: R&amp;D &lt;b&gt;</p>")  # board entities decoded once

    def test_tracked_or_unknown_jobs(self) -> None:
        body = self.get("/?job=greenhouse%3Aacme-rockets%3A1001")[2]  # tracked: its application opens
        self.assertIn('data-app="1" data-status="recruiter_call"', self.panel(body))
        for bad in ("no:such:job", ""):
            with self.subTest(job=bad):
                status, _, body = self.get(job_url(bad))
                self.assertEqual(status, 200)
                self.assertNotIn('<aside class="panel', body)
                self.assertNotIn("is-focus", body)

    def test_interested_opens_its_application(self) -> None:
        status, headers, _ = self.post("/offers/interested", {"job_id": GLOBEX, "return": job_url(GLOBEX) + "&score=8"})
        app = self.app()
        self.assertEqual((status, headers["Location"]), (303, f"/?app={app['id']}&msg=interested"))
        self.assertEqual((app["status"], app["date_applied"]), ("to_apply", None))
        self.assertEqual(self.tasks(), [])  # no Apply task: the review task follows the agent's results
        with self.conn() as conn:
            self.assertEqual({r["kind"] for r in db.open_requests(conn, app["id"])}, {"research", "cv_fit"})
            self.assertEqual(db.job_decisions(conn)[GLOBEX]["decision"], "interested")
        body = self.get(headers["Location"])[2]
        self.assertIn("It is in Pipeline → Interested", body)
        self.assertIn(f'data-app="{app["id"]}" data-status="to_apply"', self.panel(body))
        self.assertIn('data-view="pipeline" aria-current="true"', body)
        self.assertNotIn("Globex Labs", "".join(OffersViewTest.companies(self)))
        status, _, out = self.post("/offers/interested", {"job_id": WONKA}, {**HOST, "Accept": "application/json"})
        reply = json.loads(out)
        wonka = self.app(WONKA)
        self.assertEqual((status, reply["ok"], reply["app_id"], reply["created"], reply["redirect"]),
                         (200, True, wonka["id"], True, f"/?app={wonka['id']}"))

    def test_undo_interested(self) -> None:
        self.post("/offers/interested", {"job_id": GLOBEX})
        status, _, out = self.post("/offers/uninterested", {"job_id": GLOBEX}, {**HOST, "Accept": "application/json"})
        self.assertEqual((status, json.loads(out)["message"]), (200, web.MESSAGES["uninterested"]))
        self.assertIsNone(self.app())
        with self.conn() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM job_decisions").fetchone())
            self.assertEqual(db.open_requests(conn), [])
        self.assertIn("Globex Labs", OffersViewTest.companies(self))  # new again

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
                self.post("/offers/interested", {"job_id": GLOBEX})
                app_id = self.app()["id"]
                with self.conn() as conn:
                    change(conn, app_id)
                status, _, out = self.post("/offers/uninterested", {"job_id": GLOBEX},
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
        url = next(j["url"] for j in self.fixtures["getjo_jobs"] if j["id"] == GLOBEX)
        with self.conn() as conn:  # tracked by its link before, without the getjo id
            old, _ = db.record_application(conn, "Globex Labs", "Cloud Security Engineer", status="to_apply",
                                           link=url, today=TODAY)
        _, _, out = self.post("/offers/interested", {"job_id": GLOBEX}, {**HOST, "Accept": "application/json"})
        reply = json.loads(out)
        self.assertEqual((reply["app_id"], reply["created"], reply["redirect"]), (old, False, f"/?app={old}"))
        with self.conn() as conn:
            self.assertEqual(db.job_decisions(conn)[GLOBEX]["decision"], "interested")

    def test_interested_then_applied(self) -> None:
        self.post("/offers/interested", {"job_id": GLOBEX})
        self.post("/offers/interested", {"job_id": GLOBEX})  # a repeat adds nothing
        self.assertEqual(len(self.tasks()), 0)
        with self.conn() as conn:
            self.assertEqual(len(db.open_requests(conn, self.app()["id"])), 2)
        self.post("/offers/apply", {"job_id": GLOBEX})  # the old endpoint still works (no button)
        app = self.app()
        self.assertEqual((app["status"], app["date_applied"]), ("applied", TODAY))
        self.assertIn(("Chase if no reply", "admin", "2026-01-27", "todo"), self.tasks())
        with self.conn() as conn:
            self.assertEqual(db.job_decisions(conn)[GLOBEX]["decision"], "applied")

    def test_interested_never_moves_an_application_back(self) -> None:
        self.post("/offers/apply", {"job_id": GLOBEX})
        with self.conn() as conn:
            db.update_application(conn, self.app()["id"], status="technical")
        self.post("/offers/interested", {"job_id": GLOBEX})
        self.assertEqual(self.app()["status"], "technical")
        self.assertEqual([t[0] for t in self.tasks()], ["Chase if no reply"])

    def test_mark_applied_old_endpoint(self) -> None:
        status, headers, _ = self.post("/offers/apply", {"job_id": GLOBEX, "return": "/offers?score=8"})
        app = self.app()
        self.assertEqual((status, headers["Location"]), (303, f"/?app={app['id']}&msg=applied"))
        self.assertEqual((app["company"], app["status"], app["date_applied"]), ("Globex Labs", "applied", TODAY))
        body = self.get(headers["Location"])[2]
        self.assertIn("Recorded as applied", body)
        self.assertIn(f'data-app="{app["id"]}" data-key="a:{app["id"]}" class="is-focus"', body)
        for _ in range(2):
            self.post("/offers/apply", {"job_id": GLOBEX})
        with self.conn() as conn:
            self.assertEqual(sum(a["getjo_job_id"] == GLOBEX for a in db.list_applications(conn)), 1)
        self.assertEqual(self.post("/offers/apply", {"job_id": "no:such:job"})[0], 400)
        self.assertEqual(self.post("/offers/apply", {})[0], 400)
        self.assertEqual(self.post("/offers/interested", {"job_id": "no:such:job"})[0], 400)

    def test_skip_and_undo(self) -> None:
        status, headers, _ = self.post("/offers/skip", {"job_id": GLOBEX, "reason": "stack", "comment": "<b>No Java</b>",
                                                        "return": job_url(GLOBEX)})
        self.assertEqual((status, headers["Location"]), (303, f"{job_url(GLOBEX)}&msg=skipped".replace("?", "?", 1)))
        with self.conn() as conn:
            d = db.job_decisions(conn)[GLOBEX]
            self.assertIsNone(db.find_by_job(conn, GLOBEX))
        self.assertEqual((d["decision"], d["reason"], d["comment"], d["score"]), ("skipped", "stack", "<b>No Java</b>", 8))
        body = self.get(headers["Location"])[2]
        self.assertIn("Skipped. It no longer appears in New offers.", body)
        panel = self.panel(body)  # the panel stays, now skipped
        self.assertIn(">Skipped · Stack</button>", panel)
        self.assertIn('action="/offers/unskip" class="inline" data-ajax="refresh"', panel)
        self.assertNotIn('action="/offers/interested"', panel)
        self.assertNotIn("Globex Labs", OffersViewTest.companies(self))
        _, out, _ = self.run_cli("today")
        self.assertNotIn("Globex Labs", out)

        body = self.get("/?view=offers&skipped=1")[2]
        self.assertEqual(OffersViewTest.companies(self, "&skipped=1"), ["Globex Labs"])
        self.assertIn("Stack <span class=\"muted\">— &lt;b&gt;No Java&lt;/b&gt;</span>", body)
        self.assertNotIn("<b>No Java", body)
        self.assertIn('action="/offers/unskip" class="inline" data-ajax="refresh"', body)
        self.assertIn('href="/?view=offers">Hide skipped</a>', body)
        self.assertNotIn('name="score"', body[body.index('class="offer-filters"'):])  # the score filter is for new ones

        status, headers, _ = self.post("/offers/unskip", {"job_id": GLOBEX})
        self.assertEqual((status, headers["Location"]), (303, "/?msg=unskipped"))
        self.assertIn("Globex Labs", OffersViewTest.companies(self))

    def test_skipped_list_ignores_the_score_and_keeps_dropped_jobs(self) -> None:
        self.post("/offers/skip", {"job_id": "workable:hooli-data:77", "reason": "company"})  # score 6
        gone = {"id": "greenhouse:gone:9", "company": "Vanished Co", "title": "SRE", "location": "Remote",
                "score": 9, "url": "https://example.com/gone", "source": "greenhouse"}
        with self.conn() as conn:
            db.record_decision(conn, gone, "skipped", "company")
        self.assertEqual(set(OffersViewTest.companies(self, "&skipped=1&score=10")), {"Hooli Data", "Vanished Co"})
        self.assertEqual(OffersViewTest.companies(self, "&skipped=1&loc=other"), [])
        panel = self.panel(self.get("/?view=offers&skipped=1&job=greenhouse%3Agone%3A9")[2])
        self.assertIn("Vanished Co", panel)
        self.assertIn("getjo no longer has this job", panel)
        self.assertNotIn("Hooli Data", OffersViewTest.companies(self, "&score=6"))  # skipped, not new

    def test_bad_skips(self) -> None:
        for form in ({"job_id": GLOBEX}, {"job_id": GLOBEX, "reason": "boring"},
                     {"job_id": "no:such:job", "reason": "stack"}, {"reason": "stack"},
                     {"job_id": "greenhouse:acme-rockets:1001", "reason": "pay"},  # already applied
                     {"job_id": GLOBEX, "reason": "other", "comment": "x" * (web.MAX_COMMENT + 1)}):
            with self.subTest(form=form):
                self.assertEqual(self.post("/offers/skip", form)[0], 400)
        for job_id in (GLOBEX, "greenhouse:acme-rockets:1001", ""):  # not skipped / applied / missing
            with self.subTest(unskip=job_id):
                self.assertEqual(self.post("/offers/unskip", {"job_id": job_id})[0], 400)
        self.post("/offers/skip", {"job_id": GLOBEX, "reason": "pay"})
        for path in ("/offers/apply", "/offers/interested"):  # a skipped job cannot be tracked
            with self.subTest(path=path):
                self.assertEqual(self.post(path, {"job_id": GLOBEX})[0], 400)
        self.assertIsNone(self.app())

    def test_skip_json(self) -> None:
        body = urlencode({"job_id": GLOBEX, "reason": "seniority"}).encode()
        status, _, out = web.handle(self.settings, "POST", "/offers/skip",
                                    {**HOST, "Accept": "application/json"}, body, today=TODAY)
        self.assertEqual((status, json.loads(out)["ok"]), (200, True))

    def test_one_skip_dialog_for_offers_and_applications(self) -> None:
        for path in ("/", "/?view=offers"):
            with self.subTest(path=path):
                body = self.get(path)[2]
                self.assertEqual(body.count('<dialog id="skip-dialog"'), 1)
                dialog = body[body.index('<dialog id="skip-dialog"'):body.index("</dialog>")]
                self.assertIn('<input type="hidden" name="app_id" value=""><input type="hidden" name="job_id" value="">', dialog)
                for reason in db.SKIP_REASONS:
                    self.assertIn(f'name="reason" value="{reason}"', dialog)
                self.assertIn('<textarea name="comment"', dialog)
        js = self.get("/static/app.js")[2]
        self.assertIn("link.dataset.skipAction", js)
        self.assertIn("link.dataset.skipField", js)

    def test_skip_page_without_js(self) -> None:
        back = "/?view=offers&score=8"
        status, _, body = self.get(f"/offers/skip?job_id=lever%3Aglobex-labs%3Aabc&return={quote(back, safe='')}")
        self.assertEqual(status, 200)
        self.assertIn("Globex Labs", body)
        self.assertIn('<select name="reason" required', body)
        self.assertIn(f'name="return" value="{h(back)}"', body)
        self.assertIn(f'href="{h(back)}"', body)  # Cancel keeps the filters
        self.assertIn('<a href="/" aria-current="page">', body)
        with self.conn() as conn:
            self.assertNotIn(GLOBEX, db.job_decisions(conn))  # reading the form changes nothing
        status, headers, _ = self.post("/offers/skip", {"job_id": GLOBEX, "reason": "location", "comment": "",
                                                        "return": back})
        self.assertEqual((status, headers["Location"]), (303, back + "&msg=skipped"))
        self.assertEqual(self.get("/offers/skip?job_id=no:such:job")[0], 400)
        self.assertEqual(self.get("/offers/skip?return=http://evil.example.com/")[0], 400)
        with self.conn() as conn:
            db.undo_skip(conn, GLOBEX)
        _, _, body = self.get(f"/offers/skip?job_id={GLOBEX}&return=http://evil.example.com/")
        self.assertIn('class="btn btn-ghost" href="/">Cancel', body)

    def test_interested_label_everywhere(self) -> None:
        self.post("/offers/interested", {"job_id": GLOBEX})
        _, _, body = self.get("/")
        self.assertNotIn("To apply", body)
        self.assertNotIn("to apply", body)
        self.assertIn("Interested", body)
        _, out, _ = self.run_cli("apps", "--status", "to_apply")
        self.assertIn("[interested]", out)


class ClosedJobsTest(WebTestCase):
    """getjo's posted_at / expires_at / last_seen / closed_at, read when the jobs table has them."""

    def test_closed_and_expired_jobs_leave_new_offers_and_today(self) -> None:
        names = OffersViewTest.companies(self)
        self.assertNotIn("Oscorp AI", names)
        self.assertNotIn("Pied Piper Example", names)
        body = self.get("/?view=offers&score=1")[2]
        self.assertNotIn("Oscorp AI", body)
        _, out, _ = self.run_cli("today", "--threshold", "1")
        self.assertNotIn("Oscorp AI", out)
        self.assertNotIn("Pied Piper Example", out)
        self.assertIn("Globex Labs — Cloud Security Engineer, Remote (seen 2026-01-12) · may be closed", out)
        # a day after the fixtures' expiry date the Wonka job has expired too
        status, _, body = web.handle(self.settings, "GET", "/?view=offers", HOST, today="2026-01-25")
        self.assertNotIn("Wonka Systems", body.decode())

    def test_an_older_getjo_without_the_columns(self) -> None:
        build_getjo_db(self.getjo_db, self.fixtures["getjo_jobs"], old_schema=True)
        names = OffersViewTest.companies(self)
        self.assertEqual(names[:2], ["Wonka Systems", "Oscorp AI"])  # nothing reads as closed
        self.assertIn("Pied Piper Example", names)
        body = self.get("/?view=offers")[2]
        self.assertNotIn("may be closed", body)
        self.assertNotIn("expires in", body)
        _, out, _ = self.run_cli("today")
        self.assertIn("Oscorp AI", out)
        self.post("/offers/interested", {"job_id": OSCORP})
        self.assertNotIn(">closed</span>", self.get("/")[2])

    def test_tracked_jobs_that_close_stay_in_the_pipeline_with_a_badge(self) -> None:
        self.post("/offers/interested", {"job_id": OSCORP})  # closed in getjo
        self.post("/offers/apply", {"job_id": PIED})  # expired
        self.post("/offers/interested", {"job_id": WONKA})  # open until 24 Jan
        with self.conn() as conn:
            ids = {job: db.find_by_job(conn, job)["id"] for job in (OSCORP, PIED, WONKA)}
        body = self.get("/")[2]
        row = lambda app_id: ApplicationsPageTest.row(body, app_id)
        self.assertIn(">closed</span>", row(ids[OSCORP]))
        self.assertIn(">closed</span>", row(ids[PIED]))
        self.assertNotIn(">closed</span>", row(ids[WONKA]))
        self.assertNotIn(">closed</span>", row(1))  # Acme: open
        head = ApplicationsPageTest.panel(self.get(f"/?app={ids[OSCORP]}")[2])
        self.assertIn(">closed</span>", head[:head.index("</header>")])
        later = web.handle(self.settings, "GET", "/", HOST, today="2026-01-25")[2].decode()
        self.assertIn(">closed</span>", ApplicationsPageTest.row(later, ids[WONKA]))
        with self.conn() as conn:  # an interview stage shows no badge: the process goes on
            db.update_application(conn, ids[PIED], status="recruiter_call")
        self.assertNotIn(">closed</span>", ApplicationsPageTest.row(self.get("/")[2], ids[PIED]))


class LandingPageTest(WebTestCase):
    """/ is Applications; the switch between Pipeline and New offers; nav Applications · Dashboard."""

    def test_nav_and_switch(self) -> None:
        body = self.get("/")[2]
        nav = body[body.index('<nav aria-label="Pages">'):body.index("</nav>", body.index('<nav aria-label="Pages">'))]
        self.assertEqual([n.split("</span>")[0] for n in nav.split("<span>")[1:]], ["Applications", "Dashboard"])
        self.assertIn('<a href="/" aria-current="page">', nav)
        self.assertIn('<span class="nav-count" title="New offers">3</span>', nav)
        self.assertIn("<title>Applications · getjo</title>", body)
        switch = body[body.index('class="view-switch"'):body.index("</nav>", body.index('class="view-switch"'))]
        self.assertIn('href="/" data-view="pipeline" aria-current="true">Pipeline <span class="count">6</span>', switch)
        self.assertIn('href="/?view=offers" data-view="offers">New offers <span class="count">3</span>', switch)
        self.assertIn('<table id="app-list">', body)
        self.assertNotIn('id="offer-list"', body)
        dash = self.get("/dashboard")[2]
        self.assertIn('<a href="/dashboard" aria-current="page">', dash)
        self.assertIn('<body class="theme-sage">', dash)

    def test_switch_keeps_the_panel_and_drops_the_other_views_filters(self) -> None:
        body = self.get("/?stage=interviewing&app=3")[2]
        self.assertIn('href="/?view=offers&amp;app=3" data-view="offers"', body)
        body = self.get("/?view=offers&score=8&q=x")[2]
        self.assertIn('href="/" data-view="pipeline"', body)
        self.assertEqual(web.home_filters({"view": ["board"]})["view"], "pipeline")  # an old bookmark

    def test_view_is_remembered_by_the_script_only_for_a_bare_url(self) -> None:
        js = self.get("/static/app.js")[2]
        self.assertIn('location.pathname === "/" && !location.search', js)
        self.assertIn("localStorage.getItem(VIEW_KEY)", js)
        self.assertIn('location.replace(`/?${query}`)', js)
        self.assertIn('localStorage.getItem(SEL_KEY)', js)  # the ticked jobs come back too
        self.assertIn("try { localStorage.setItem(VIEW_KEY, tab.dataset.view); } catch", js)

    def test_new_offers_without_javascript(self) -> None:
        body = self.get("/?view=offers")[2]
        form = body[body.index('class="offer-filters"'):]
        self.assertIn('<form method="get" action="/" class="offer-filters"', body)
        self.assertIn('<button class="btn btn-secondary btn-sm">Filter</button>', form[:form.index("</form>")])
        self.assertIn(f'<a href="{h(row_url(GLOBEX))}">Globex Labs</a>', body)


class TasksTest(WebTestCase):
    def test_tasks_page_redirects_to_applications(self) -> None:
        status, headers, _ = self.get("/tasks")
        self.assertEqual((status, headers["Location"]), (303, "/"))
        self.assertEqual(self.get("/tasks?app=3")[1]["Location"], "/?app=3")
        body = self.get("/")[2]
        self.assertNotIn('href="/tasks"', body)
        self.assertNotIn("<span>Tasks</span>", body)

    def test_no_general_tasks_and_no_task_form(self) -> None:
        body = self.get("/")[2]
        for gone in ("General tasks", 'id="general-tasks"', "Recently done", 'action="/tasks/add"',
                     "Revise networking basics"):  # a general task: the agent and /today handle those
            self.assertNotIn(gone, body)
        self.assertEqual(self.post("/tasks/add", {"title": "x"})[0], 404)

    def test_done_and_reopen(self) -> None:
        with self.conn() as conn:
            task = db.add_task(conn, "Mock system design", "drill", "2026-01-19", 1)
        panel = self.get("/?app=1")[2]
        self.assertIn('<span class="badge s4 overdue">2026-01-19</span>', panel)
        status, headers, _ = self.post("/tasks/done", {"task_id": str(task), "return": "/?app=1"})
        self.assertEqual((status, headers["Location"]), (303, "/?app=1&msg=task_done"))
        with self.conn() as conn:
            self.assertNotIn(task, [t["id"] for t in db.list_tasks(conn)])
        self.assertIn("Task marked done", self.get(headers["Location"])[2])
        status, headers, _ = self.post("/tasks/reopen", {"task_id": str(task)})
        self.assertEqual((status, headers["Location"]), (303, "/?msg=task_reopened"))
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
        body = self.get("/")[2]
        self.assertIn('<body class="theme-ochre">', body)
        table = self.table(body)
        head = table[:table.index("</thead>")]
        labels = [c.split(">", 1)[1].split("<", 1)[0] for c in head.split("<th scope")[1:]]
        self.assertEqual(labels, ["", "Company", "Role", "Stage", "Next task", "Comment"])  # no due, days, fit or tier
        groups = [g.split('"', 1)[0] for g in table.split('<tbody data-group="')[1:]]
        self.assertEqual(groups, ["to_apply", "applied", "recruiter_call", "technical"])  # pipeline order, open only
        self.assertIn('scope="rowgroup">Coding interview <span class="count">1</span>', table)
        self.assertNotIn("Cyberdyne Example", table)  # closed: behind Show closed
        self.assertIn('href="/?closed=1" data-closed-toggle>Show closed</a>', body)
        self.assertIn('data-href="/?app=3&amp;sel=a%3A3"', self.row(body, 3))
        rows = [{"app": {"id": i}, "next": n, "days": d} for i, n, d in (
            (1, None, 9), (2, {"due_date": None}, 3), (3, {"due_date": "2026-01-25"}, 1),
            (4, {"due_date": "2026-01-21"}, 2), (5, {"due_date": "2026-01-25"}, 8))]
        self.assertEqual([r["app"]["id"] for r in web.list_order(rows)], [4, 5, 3, 2, 1])

    def test_no_board_markup(self) -> None:
        body = self.get("/?app=3&view=board")[2]
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
        body = self.get("/?closed=1")[2]
        table = self.table(body)
        self.assertIn("Cyberdyne Example", table)
        self.assertGreater(table.index('data-group="rejected"'), table.index('data-group="technical"'))
        self.assertIn('href="/" data-closed-toggle>Hide closed</a>', body)
        body = self.get("/?stage=interviewing")[2]
        table = self.table(body)
        self.assertIn("Acme Rockets", table)
        self.assertNotIn("Tyrell Robotics", table)
        self.assertIn('class="stage-pill is-active" href="/" data-stage="interviewing"', body)
        self.assertIn("Cyberdyne Example", self.table(self.get("/?stage=closed")[2]))
        for bad in ("nope", "rejected"):
            f = web.home_filters({"stage": [bad], "view": ["board"], "closed": ["yes"]})
            self.assertEqual((f["view"], f["app"], f["job"], f["stage"], f["closed"]), ("pipeline", None, None, "", False))

    def test_comment_column_shows_the_latest_and_the_count(self) -> None:
        with self.conn() as conn:
            db.add_comment(conn, 2, "Referral from a former colleague")
            db.add_comment(conn, 2, "Sent portfolio link")
        row = self.row(self.get("/")[2], 2)
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
        panel = self.panel(self.get("/?app=3&stage=interviewing")[2])
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
        self.assertIn('href="/?stage=interviewing" aria-label="Close"', panel)
        self.assertIn('name="return" value="/?stage=interviewing&amp;app=3"', panel)
        for gone in ("Set tier", "Confirm", 'name="tier"', 'type="date"', "Company and CV fit"):
            self.assertNotIn(gone, panel)

    def test_layout_a_fits_the_viewport_on_laptops(self) -> None:
        css = self.get("/static/style.css")[2]
        laptop = css[css.index("/* Layout A (laptop and up)"):]
        for rule in ("main:has(.apps) { height: 100vh;", ".card-pipeline { flex: 0 1 auto; max-height: 45%; }", ".card-pipeline .stage-strip, .card-pipeline .offer-filters { flex: none; }",
                     ".card-agent { flex: 1 1 0;", ".card-agent form.ask { flex: none; }"):
            self.assertIn(rule, laptop)
        self.assertLess(css.index("@media (min-width: 801px) {", css.index("/* Layout A")),
                        css.index("main:has(.apps) { height: 100vh;"), "laptop and up only; phones stay stacked")

    def test_unknown_focus_is_ignored(self) -> None:
        for bad in ("abc", "99999", ""):
            with self.subTest(app=bad):
                status, _, body = self.get(f"/?app={bad}")
                self.assertEqual(status, 200)
                self.assertNotIn("is-focus", body)
                self.assertNotIn('<aside class="panel"', body)

    def test_shortcuts_for_interested(self) -> None:
        app_id = self.app_id("interested")
        panel = self.panel(self.get(f"/?app={app_id}")[2])
        shortcuts = panel[panel.index('class="shortcuts"'):panel.index('class="panel-tasks"')]
        labels = ["Researching…", "Checking CV fit…", "Tailor CV", "Mark applied", ">Skip<"]
        self.assertEqual([shortcuts.index(l) for l in labels], sorted(shortcuts.index(l) for l in labels))
        self.assertIn('action="/applications/apply" class="inline" data-ajax="applied"', shortcuts)
        self.assertIn(f'href="/applications/skip?app={app_id}&amp;return=', shortcuts)
        self.assertIn(f'data-skip="{app_id}"', shortcuts)
        self.assertIn('<dialog id="skip-dialog"', self.get("/")[2])
        self.assertIn('data-skip-field="app_id" data-skip-action="/applications/skip"', shortcuts)

    def test_shortcuts_after_applying(self) -> None:
        panel = self.panel(self.get("/?app=3")[2])  # Wayne, coding interview
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
        self.assertEqual((status, headers["Location"]), (303, f"/?app={app_id}&msg=applied_today"))
        self.assertEqual(self.post("/applications/apply", {"app_id": str(app_id)})[0], 400)  # no longer Interested

    def test_skip_json_and_without_script(self) -> None:
        app_id = self.app_id("interested")
        status, _, body = self.get(f"/applications/skip?app={app_id}&return=/?app={app_id}")
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
                                                              "return": f"/?app={plain}"})
        self.assertEqual((status, headers["Location"]), (303, f"/?app={plain}&msg=app_skipped"))
        offers = self.get("/?view=offers&skipped=1")[2]
        self.assertIn("Globex Labs", offers)
        panel = self.panel(self.get(f"/?app={app_id}")[2])
        self.assertIn(">Skipped</button>", panel)
        self.assertIn("skipped: location — On-site only", panel)
        self.assertIn("Globex Labs", self.table(self.get("/?closed=1")[2]))

    def test_task_tick(self) -> None:
        panel = self.panel(self.get("/?app=3")[2])
        tasks = panel[panel.index('class="panel-tasks"'):]
        self.assertIn('action="/tasks/done" class="inline" data-ajax="tick"', tasks)
        self.assertIn('<button class="tick" aria-label="Mark done: ', tasks)
        task_id = int(tasks.split('name="task_id" value="')[1].split('"')[0])
        status, headers, _ = self.post("/tasks/done", {"task_id": str(task_id), "return": "/?app=3"})
        self.assertEqual(headers["Location"], "/?app=3&msg=task_done")
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
        panel = self.panel(self.get("/?app=3")[2])
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
        self.assertIn(" data-busy", self.panel(self.get(f"/?app={app_id}")[2])[:300])
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
        web.handle(self.settings, "GET", "/", HOST, today="2026-01-15")
        with self.conn() as conn:
            self.assertEqual(db.get_application(conn, app_id)["status"], "no_response")
            self.assertEqual(db.timeline(conn, app_id)[-1]["comment"], "auto: no reply in 14 days")

    def test_dashboard_counts_applications_not_tasks(self) -> None:
        body = self.get("/dashboard")[2]
        for gone in ("Overdue", "Tasks done", "Revise networking basics", 'class="chart line"'):
            self.assertNotIn(gone, body)


class SharedDatabaseTest(WebTestCase):
    def test_cli_write_visible_to_next_request(self) -> None:
        self.assertNotIn("Call the recruiter", self.get("/?app=1")[2])
        code, _, _ = self.run_cli("task", "add", "Call the recruiter", "--kind", "admin", "--app", "1")
        self.assertEqual(code, 0)
        self.assertIn("Call the recruiter", self.get("/?app=1")[2])

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
        _, headers, _ = self.get("/?view=offers")
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
