"""Ticking jobs for the Agent box: the sel query (chips, limit, Clear all, auto-tick), messages about
several targets (agent_message_targets, comparisons), their migration, the All | Selected filter, the
id-only ping and `thread show` for a comparison. Made-up data only; dev3 is never called."""

from unittest import mock
from urllib.parse import parse_qs, urlencode, urlsplit

from prep import assistant, db, web
from tests.test_web import HOST, WebTestCase

ACME = 1  # Acme Rockets (recruiter_call), linked to getjo job ACME_JOB
STARK, WAYNE, TYRELL = 2, 3, 6
ACME_JOB = "greenhouse:acme-rockets:1001"
GLOBEX = "lever:globex-labs:abc"  # a new offer (score 8)
INITECH = "ashby:initech-cloud:x1"  # a new offer (score 7) with a brief in the fixtures
JSON = {**HOST, "Accept": "application/json"}


def url(*sel: str, **extra: str) -> str:
    return "/?" + urlencode([*extra.items(), *(("sel", k) for k in sel)])


class SelTest(WebTestCase):
    def about(self, body: str) -> str:
        start = body.index('class="agent-about"')
        return body[start:body.index("<textarea", start)]

    def test_parse_keeps_order_drops_repeats_and_bad_values_and_caps_at_four(self) -> None:
        self.assertEqual(web.parse_sel(["a:1", "a:1", "x", "a:z", "j:", "cv", "j:s:c:1"]), ("a:1", "cv", "j:s:c:1"))
        self.assertEqual(web.parse_sel(["a:1", "a:2", "a:3", "a:6", "a:7"]), ("a:1", "a:2", "a:3", "a:6"))
        self.assertEqual(web.toggle_sel(("a:1",), "a:1"), ())
        self.assertIsNone(web.toggle_sel(("a:1", "a:2", "a:3", "a:6"), "cv"))

    def test_nothing_ticked_is_the_whole_search(self) -> None:
        about = self.about(self.get("/")[2])
        self.assertIn("the whole search", about)
        self.assertNotIn('name="about"', about)
        self.assertIn('name="about_set" value="1"', about)
        self.assertNotIn("Clear all", about)

    def test_one_two_and_four_chips(self) -> None:
        about = self.about(self.get(url(f"a:{ACME}"))[2])
        self.assertIn('<input type="hidden" name="about" value="a:1"><span class="about-name">Acme Rockets — Platform Engineer',
                      about)
        self.assertNotIn("compare", about)
        about = self.about(self.get(url(f"a:{ACME}", f"j:{GLOBEX}"))[2])
        self.assertEqual(about.count('class="about-chip"'), 2)
        self.assertIn("Globex Labs — Cloud Security Engineer", about)
        self.assertIn("compare 2", about)
        about = self.about(self.get(url("a:1", "a:2", "a:3", "cv"))[2])
        self.assertEqual(about.count('class="about-chip"'), 4)
        self.assertIn("My CV", about)

    def test_limit_of_four(self) -> None:
        body = self.get(url("a:1", "a:2", "a:3", "a:6"))[2]
        row = body[body.index('id="app-7"'):]
        tick = row[row.index('class="sel-tick"'):row.index("</a>")]
        self.assertIn("msg=sel_limit", tick)  # a fifth tick only says so
        self.assertIn("Up to 4 at a time", self.get(url("a:1", "a:2", "a:3", "a:6", msg="sel_limit"))[2])
        js = self.get("/static/app.js")[2]
        self.assertIn("sel.length >= MAX_SEL", js)

    def test_chip_x_and_clear_all(self) -> None:
        about = self.about(self.get(url("a:1", "a:2", app="1"))[2])
        self.assertIn(f'href="{web.h(url("a:2", app="1"))}" data-sel="a:1"', about)  # × unticks that one only
        self.assertIn(f'href="/?app=1" data-sel-clear>Clear all</a>', about)

    def test_ticks_and_auto_tick_only_when_nothing_is_ticked(self) -> None:
        body = self.get("/")[2]
        row = body[body.index(f'id="app-{STARK}"'):]
        self.assertIn(f'data-href="{web.h(url(f"a:{STARK}", app=str(STARK)))}"', row[:200])  # opening ticks it
        self.assertIn('role="checkbox" aria-checked="false" data-sel="a:2"', row)
        body = self.get(url(f"a:{ACME}"))[2]
        row = body[body.index(f'id="app-{STARK}"'):]
        self.assertIn(f'data-href="{web.h(url(f"a:{ACME}", app=str(STARK)))}"', row[:200])  # opening only shows it
        acme = body[body.index(f'id="app-{ACME}"'):]
        self.assertIn('aria-checked="true" data-sel="a:1"', acme)
        js = self.get("/static/app.js")[2]
        self.assertIn("if (!sel.length && key && key !== keyOf(location.href)) sel = [key];", js)

    def test_selection_survives_views_panels_and_posts(self) -> None:
        body = self.get(url(f"a:{ACME}"))[2]
        self.assertIn(f'href="{web.h(url(f"a:{ACME}", view="offers"))}" data-view="offers"', body)
        status, headers, _ = self.post("/offers/interested", {"job_id": GLOBEX, "return": url(f"a:{ACME}", view="offers")})
        target = parse_qs(urlsplit(headers["Location"]).query)
        self.assertEqual(target["sel"], [f"a:{ACME}"])
        reply = self.post("/offers/interested", {"job_id": GLOBEX, "return": url("cv")}, JSON)[2]
        self.assertIn("sel=cv", reply)

    def test_unknown_ids_are_dropped_and_a_tracked_job_is_named_by_its_application(self) -> None:
        about = self.about(self.get(url("a:999", "j:nope:x:1", f"j:{ACME_JOB}"))[2])
        self.assertEqual(about.count('class="about-chip"'), 1)
        self.assertIn('value="a:1"', about)

    def test_panel_and_my_cv_ticks(self) -> None:
        body = self.get(url(app=str(ACME)))[2]
        panel = body[body.index('<aside class="panel"'):]
        self.assertIn('data-sel="a:1" aria-label="Ask the agent about Acme Rockets — Platform Engineer"', panel)
        self.assertIn('<span class="sel-text">Ask about this</span>', panel)
        head = body[body.index('class="page-head-actions"'):]
        self.assertIn('data-sel="cv"', head[:400])

    def test_all_selected_filter(self) -> None:
        box = self.get("/")[2]
        self.assertIn('aria-label="Show messages" hidden>', box)  # nothing ticked: no filter
        box = self.get(url(f"a:{ACME}"))[2]
        self.assertIn('aria-label="Show messages">', box)
        self.assertIn('data-filter-chat="sel">Selected</button>', box)
        self.assertIn(f'data-targets="a:{ACME} j:{INITECH}"', box)  # the fixtures' comparison


class SendTest(WebTestCase):
    def send(self, form: dict) -> tuple[int, str, mock.MagicMock]:
        with mock.patch.object(assistant, "notify_message_in_background") as bg:
            status, _, out = self.post("/applications/message", {"about_set": "1", "body": "Which fits better?", **form},
                                       JSON)
        return status, out, bg

    def test_nothing_ticked_is_general(self) -> None:
        status, _, bg = self.send({})
        self.assertEqual(status, 200)
        msg = bg.call_args.args[2]
        with self.conn() as conn:
            self.assertEqual(db.message_subject(conn, msg), (None, None))
            self.assertEqual(db.message_targets(conn, [msg]), {})
        self.assertIsNone(bg.call_args.args[1])

    def test_one_ticked_is_that_job(self) -> None:
        status, _, bg = self.send({"about": f"j:{GLOBEX}"})
        msg = bg.call_args.args[2]
        with self.conn() as conn:
            self.assertEqual(db.message_subject(conn, msg), (None, GLOBEX))
        self.assertTrue(bg.call_args.kwargs["job"])

    def test_two_ticked_is_a_comparison(self) -> None:
        body = urlencode([("about_set", "1"), ("about", "a:1"), ("about", f"j:{GLOBEX}"), ("about", "cv"),
                          ("body", "Compare them")]).encode()
        with mock.patch.object(assistant, "notify_message_in_background") as bg:
            status, _, out = web.handle(self.settings, "POST", "/applications/message", JSON, body, today="2026-01-20")
        self.assertEqual(status, 200)
        msg = bg.call_args.args[2]
        self.assertEqual(bg.call_args.kwargs["topic"], db.COMPARE)
        with self.conn() as conn:
            self.assertEqual(db.message_subject(conn, msg), (None, None))
            self.assertEqual(db.message_topic(conn, msg), db.COMPARE)
            self.assertEqual([t["key"] for t in db.message_targets(conn, [msg])[msg]], ["a:1", f"j:{GLOBEX}", "cv"])
            [general] = [m for m in db.pending_messages(conn) if m["id"] == msg]
            self.assertEqual(db.claim_messages(conn, None), [])  # never claimed as a general message
        self.assertIn("Globex Labs", out.decode())

    def test_more_than_four_or_unknown_are_refused(self) -> None:
        body = urlencode([("about_set", "1"), *(("about", f"a:{i}") for i in (1, 2, 3, 6, 7)), ("body", "x")]).encode()
        status, _, _ = web.handle(self.settings, "POST", "/applications/message", JSON, body, today="2026-01-20")
        self.assertEqual(status, 400)
        self.assertEqual(self.send({"about": "a:999"})[0], 400)
        self.assertEqual(self.send({"about": "j:nope:x:1"})[0], 400)
        self.assertEqual(self.send({"about": "topic:compare"})[0], 400)  # the topic is never taken from the form

    def test_ping_holds_ids_only(self) -> None:
        settings = mock.Mock(assistant_task=42, assistant_project=None)
        argv = assistant.ask_command(settings, None, 17, "/bin/dev3", topic=db.COMPARE)
        self.assertEqual(argv[-1], "/ask compare 17")
        self.assertEqual(assistant.brief_command(settings, 3, "/bin/dev3")[-1], "/research briefs")


class StorageTest(WebTestCase):
    def test_backfill_from_single_target_columns(self) -> None:
        with self.conn() as conn:
            msg = db.add_message(conn, ACME, "Old question")
            job = db.add_message(conn, None, "Is it remote?", GLOBEX)
            cv = db.add_message(conn, None, "Shorten my summary", topic="cv")
            general = db.add_message(conn, None, "Which tasks need me?")
            conn.execute("DELETE FROM agent_message_targets WHERE message_id IN (?, ?, ?, ?)", (msg, job, cv, general))
            conn.commit()
        with self.conn() as conn:  # connect back-fills
            found = db.message_targets(conn, [msg, job, cv, general])
        self.assertEqual(found[msg][0]["key"], "a:1")
        self.assertEqual(found[msg][0]["getjo_job_id"], ACME_JOB)  # an application's row carries its job
        self.assertEqual(found[job][0]["key"], f"j:{GLOBEX}")
        self.assertEqual(found[cv][0]["key"], "cv")
        self.assertNotIn(general, found)

    def test_reply_carries_the_targets_and_shows_on_each_application(self) -> None:
        with self.conn() as conn:
            msg = db.add_compare(conn, "Compare", [(ACME, None, None), (STARK, None, None)])
            reply = db.reply_message(conn, None, msg, "Acme.")
            self.assertEqual([t["key"] for t in db.message_targets(conn, [reply])[reply]], ["a:1", "a:2"])
            self.assertIn(msg, [m["id"] for m in db.thread(conn, STARK)])
            self.assertEqual(db.claim_messages(conn, STARK), [])  # claimed only with --message
        with self.assertRaises(ValueError):
            with self.conn() as conn:
                db.add_compare(conn, "x", [(ACME, None, None)])

    def test_interested_and_undo_with_a_comparison_naming_the_job(self) -> None:
        with self.conn() as conn:
            msg = db.add_compare(conn, "Compare", [(ACME, None, None), (None, GLOBEX, None)])
        self.post("/offers/interested", {"job_id": GLOBEX})
        with self.conn() as conn:
            app_id = db.find_by_job(conn, GLOBEX)["id"]
            self.assertIn(f"a:{app_id}", [t["key"] for t in db.message_targets(conn, [msg])[msg]])
        self.assertEqual(self.post("/offers/uninterested", {"job_id": GLOBEX})[0], 303)  # no foreign key failure
        with self.conn() as conn:
            self.assertIsNone(db.find_by_job(conn, GLOBEX))
            self.assertEqual([t["key"] for t in db.message_targets(conn, [msg])[msg]], ["a:1", f"j:{GLOBEX}"])


class CompareShowTest(WebTestCase):
    def test_thread_show_for_a_comparison_claims_only_that_message(self) -> None:
        with self.conn() as conn:
            other = db.add_message(conn, ACME, "Unrelated")
            msg = db.add_compare(conn, "Which fits me better?", [(ACME, None, None), (None, INITECH, None)])
        code, out, err = self.run_cli("thread", "show", "--message", str(msg), "--claim")
        self.assertEqual(code, 0, err)
        self.assertIn("compares 2 targets", out)
        self.assertIn("Application #1: Acme Rockets — Platform Engineer", out)
        self.assertIn(f"Job offer {INITECH}: Initech Cloud", out)
        self.assertIn("hybrid 3 days · Go · Kubernetes", out)  # the brief
        self.assertIn(f"Claimed for this session: message {msg}.", out)
        with self.conn() as conn:
            self.assertEqual([m["id"] for m in db.pending_messages(conn) if not m["claimed"]], [other])
        self.assertIn(f"comparing several jobs (/ask compare {msg})", self.run_cli("thread", "pending")[1])
        self.assertIn("comparisons", self.run_cli("queue")[1])
        code, out, _ = self.run_cli("thread", "reply", "--to", str(msg), "--file", "/dev/null")
        self.assertNotEqual(code, 0)  # empty reply refused
