"""Assistant scaling, step 3: several workers on one route (router choice, claim isolation), the runs
record (done, blocked with a reason, stopped, lapsed), Stop on one worker only, the Workers sidebar
and its JSON (kinds, companies and times only: never message text or pay), escaping, and the Run
history page. dev3 is never called. Fictional data only."""

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from unittest import mock

from prep import assistant, db, workers
from tests.test_resend_cancel import ResendCancelBase
from tests.test_thread import ACME, HOSTILE, STARK
from tests.test_web import PAY
from tests.test_workers import ago, settings

RESEARCH = (("research", 71), ("research", 72))


def research_settings(**kw):
    return settings(workers=RESEARCH, **kw)


class ConfigTest(ResendCancelBase):
    def load(self, text: str, env: dict | None = None):
        from prep.config import load_settings
        path = self.tmp / "settings.toml"
        path.write_text(text, encoding="utf-8")
        with mock.patch.dict(os.environ, env or {}):
            for key in ("PREP_WORKER_RESEARCH", "PREP_WORKER_COMMS"):
                if key not in (env or {}):
                    os.environ.pop(key, None)
            return load_settings(path)

    def test_a_list_gives_one_pair_per_seq_in_order(self) -> None:
        from prep.config import worker_task, worker_tasks
        s = self.load("[workers]\nresearch = [72, 71, 72]\ncomms = 73\n")
        self.assertEqual(s.workers, (("comms", 73), ("research", 72), ("research", 71)))
        self.assertEqual(worker_tasks(s, "research"), (72, 71))
        self.assertEqual(worker_task(s, "research"), 72)

    def test_env_takes_a_comma_list(self) -> None:
        s = self.load("[workers]\nresearch = 70\n", {"PREP_WORKER_RESEARCH": "71, 72"})
        self.assertEqual(s.workers, (("research", 71), ("research", 72)))


class RouterTest(ResendCancelBase):
    def test_first_idle_worker_then_round_robin(self) -> None:
        s = research_settings()
        with self.conn() as conn:
            self.assertEqual(assistant.pick(s, conn, "research"), 71)
            [r] = assistant.route_requests(s, conn, [{"id": db.queue_request(conn, ACME, "research")[0],
                                                      "application_id": ACME, "kind": "research"}])
            self.assertEqual(r["worker"], 71)  # 71 now has an assigned item waiting
            self.assertEqual(assistant.pick(s, conn, "research"), 72)
            rid = db.queue_request(conn, STARK, "cv_fit")[0]
            db.assign_requests(conn, [rid], 72)
            assistant._TURNS.clear()
            self.assertEqual([assistant.pick(s, conn, "research") for _ in range(3)], [71, 72, 71])  # both busy
            self.assertEqual(assistant.pick(s, conn, "research", exclude=(71,)), 72)

    def test_one_application_stays_with_one_worker_and_the_ping_goes_there(self) -> None:
        s = research_settings()
        with self.conn() as conn:
            ids = [db.queue_request(conn, ACME, k)[0] for k in ("research", "cv_fit")]
            reqs = assistant.route_requests(s, conn, [{"id": i, "application_id": ACME, "kind": k}
                                                      for i, k in zip(ids, ("research", "cv_fit"))])
            self.assertEqual({r["worker"] for r in reqs}, {71})
            self.assertEqual({r["worker"] for r in db.open_requests(conn, ACME)}, {71})
        [argv] = assistant.commands(s, reqs, "dev3")
        self.assertEqual(argv[3], "seq:71")

    def test_a_worker_takes_its_own_work_first(self) -> None:
        with self.conn() as conn:
            theirs = db.queue_request(conn, ACME, "research")[0]
            db.assign_requests(conn, [theirs], 72)
            db.queue_request(conn, STARK, "research")
        with mock.patch.dict(os.environ, {"DEV3_TASK_SEQ": "71"}):
            code, out, _ = self.run_cli("research", "next")
        self.assertEqual(code, 0)
        self.assertIn(f"Application #{STARK}", out)  # unassigned, not 72's older item
        with self.conn() as conn:
            [run] = db.open_runs(conn)
            self.assertEqual((run["worker"], run["application_id"]), (71, STARK))
        with mock.patch.dict(os.environ, {"DEV3_TASK_SEQ": "71"}):
            _, out, _ = self.run_cli("research", "next")
        self.assertIn(f"Application #{ACME}", out)  # nothing of its own left: 72's waiting work


class ClaimIsolationTest(ResendCancelBase):
    def test_two_connections_never_take_the_same_item(self) -> None:
        with self.conn() as conn:
            rid = db.queue_request(conn, ACME, "tailor")[0]
        with closing(db.connect(self.prep_db)) as a, closing(db.connect(self.prep_db)) as b:
            won = [db.claim_request(a, rid, 71), db.claim_request(b, rid, 72)]
            self.assertEqual(won, [True, False])
            self.assertEqual([(r["worker"]) for r in db.open_runs(b)], [71])


class RunsTest(ResendCancelBase):
    def test_done_blocked_stopped_and_lapsed(self) -> None:
        with self.conn() as conn:
            fit = db.queue_request(conn, ACME, "cv_fit")[0]
            research = db.queue_request(conn, ACME, "research")[0]
            db.claim_request(conn, fit, 71)
            db.claim_request(conn, research, 71)
            db.finish_requests(conn, [ACME], "research")
            self.assertEqual(db.block_requests(conn, ACME, "no posting", "cv_fit"), 1)
            msg = db.add_message(conn, STARK, "Is it remote?")
            db.claim_message(conn, msg, 22)
            db.cancel_message(conn, msg)
            other = db.add_message(conn, STARK, "And the stack?")
            db.claim_message(conn, other, 22)
            conn.execute("UPDATE agent_messages SET claimed_at = ? WHERE id = ?", (ago(minutes=45), other))
            conn.commit()
            db.claim_message(conn, other, 23)  # the claim lapsed: another session takes it
            db.sync_runs(conn)
            got = {(r["kind"], r["worker"]): (r["result"], r["reason"]) for r in db.runs(conn)}
            self.assertEqual(got, {("research", 71): ("done", None), ("cv_fit", 71): ("blocked", "no posting"),
                                   ("ask", 22): ("lapsed", None)} | {("ask", 22): got[("ask", 22)]})
            self.assertIn(("stopped", None), got.values())
            self.assertEqual([(r["kind"], r["worker"]) for r in db.open_runs(conn)], [("ask", 23)])
            self.assertEqual(db.get_message(conn, other)["worker"], 23)

    def test_blocked_reason_rules(self) -> None:
        with self.conn() as conn:
            db.queue_request(conn, ACME, "cv_fit")
            for bad in ("", "x" * (db.MAX_REASON + 1), "pay below £50k"):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    db.block_requests(conn, ACME, bad)

    def test_cli_block_and_brief_block(self) -> None:
        with self.conn() as conn:
            db.queue_request(conn, ACME, "cv_fit")
            db.queue_brief(conn, "job-x")
        code, out, _ = self.run_cli("research", "block", str(ACME), "--kind", "cv_fit", "--reason", "no posting")
        self.assertEqual((code, out.strip()), (0, f"Blocked 1 request for #{ACME}"))
        code, out, _ = self.run_cli("brief", "block", "--job", "job-x", "--reason", "no description")
        self.assertEqual(code, 0)
        with self.conn() as conn:
            self.assertEqual(db.open_requests(conn, ACME), [])
            self.assertEqual(db.open_briefs(conn), [])
            self.assertTrue(db.queue_brief(conn, "job-x"))  # a blocked brief can be asked for again


class StopTest(ResendCancelBase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = self.settings.__class__(**{**self.settings.__dict__, "workers": RESEARCH})
        with self.conn() as conn:
            self.mine = db.queue_request(conn, ACME, "tailor")[0]
            self.theirs = db.queue_request(conn, STARK, "cv_fit")[0]
            db.claim_request(conn, self.mine, 71)
            db.claim_request(conn, self.theirs, 72)

    def statuses(self) -> dict:
        with self.conn() as conn:
            return {r["id"]: (r["status"], r["worker"]) for r in conn.execute("SELECT * FROM agent_requests")}

    def test_cancel_touches_only_that_worker(self) -> None:
        status, reply = self.post_json("/workers/stop", {"worker": "71", "then": "cancel"})
        self.assertEqual(status, 200)
        self.assertIn("cancelled", reply["message"])
        self.assertEqual(self.statuses(), {self.mine: ("cancelled", 71), self.theirs: ("claimed", 72)})
        with self.conn() as conn:
            self.assertEqual({(r["worker"], r["result"]) for r in db.runs(conn, result=None)}, {(71, "stopped")})
            self.assertEqual([r["worker"] for r in db.open_runs(conn)], [72])

    def test_back_to_queue_goes_to_another_worker(self) -> None:
        with self.conn() as conn:  # 72 is free again
            db.finish_requests(conn, [STARK], "cv_fit")
        self.post_json("/workers/stop", {"worker": "71", "then": "requeue"})
        self.assertEqual(self.statuses()[self.mine], ("pending", 72))
        sent = [c.args[1] for c in assistant.notify_in_background.call_args_list if c.args[1]]
        self.assertEqual([[r["worker"] for r in reqs] for reqs in sent], [[72]])

    def test_unknown_worker_is_refused(self) -> None:
        status, reply = self.post_json("/workers/stop", {"worker": "99"})
        self.assertEqual(status, 400)
        self.assertEqual(self.statuses()[self.mine], ("claimed", 71))


class SidebarTest(ResendCancelBase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = self.settings.__class__(**{**self.settings.__dict__, "workers": (("research", 71), ("comms", 72)),
                                                   "assistant_task": 22, "prep_assistant_task": 53})
        with self.conn() as conn:
            conn.execute("UPDATE applications SET pay = ? WHERE id = ?", (PAY, ACME))
            conn.commit()

    def workers_json(self, query: str = "") -> dict:
        status, _, body = self.get(f"/workers.json{query}")
        self.assertEqual(status, 200)
        return json.loads(body)

    def test_rows_states_and_no_text_or_pay(self) -> None:
        with self.conn() as conn:
            rid = db.queue_request(conn, ACME, "tailor")[0]
            db.claim_request(conn, rid, 71)
            msg = db.add_message(conn, STARK, f"SECRET question {PAY}")
            db.claim_message(conn, msg, 53)  # wrong owner on purpose: rows go by the seq that claimed
            conn.execute("UPDATE agent_messages SET claimed_at = ? WHERE id = ?", (ago(minutes=40), msg))
            conn.execute("UPDATE runs SET started_at = ? WHERE item = ?", (ago(minutes=40), str(msg)))
            conn.commit()
            waiting = db.add_message(conn, None, "SECRET general")
        reply = self.workers_json()
        rows = {w["name"]: w for w in reply["workers"]}
        self.assertEqual(list(rows), ["Research", "Applications", "Communication", "Learn"])
        self.assertEqual(rows["Research"]["state"], "working")
        self.assertTrue(rows["Research"]["line"].startswith("Tailored CV · Acme Rockets"))
        self.assertEqual((rows["Learn"]["state"], rows["Learn"]["line"]), ("stuck", "no reply in 30 min"))
        self.assertEqual((rows["Communication"]["state"], rows["Communication"]["line"]), ("idle", "idle"))
        self.assertEqual(rows["Applications"]["queued"], 1)
        self.assertEqual(reply["summary"], "1 busy · 1 waiting")
        text = json.dumps(reply)
        self.assertNotIn("SECRET", text)
        self.assertNotIn(PAY, text)
        page = self.get("/")[2]
        side = page[page.index("data-workers"):page.index("Run history")]
        self.assertNotIn(PAY, side)  # the thread shows messages verbatim; the sidebar never does
        self.assertNotIn("SECRET", side)
        del waiting

    def test_blocked_last_run_is_amber_with_its_reason(self) -> None:
        with self.conn() as conn:
            rid = db.queue_request(conn, ACME, "cv_fit")[0]
            db.claim_request(conn, rid, 71)
            db.block_requests(conn, ACME, "no posting")
        rows = {w["name"]: w for w in self.workers_json()["workers"]}
        self.assertEqual((rows["Research"]["state"], rows["Research"]["line"]), ("stuck", "blocked: no posting"))

    def test_escaping(self) -> None:
        with self.conn() as conn:
            conn.execute("UPDATE applications SET company = ? WHERE id = ?", (HOSTILE, ACME))
            conn.commit()
            rid = db.queue_request(conn, ACME, "tailor")[0]
            db.claim_request(conn, rid, 71)
        for body in (self.get("/")[2], self.workers_json()["html"], self.get("/runs?result=running")[2]):
            self.assertNotIn("<script>alert(1)", body)
            self.assertIn("&lt;script&gt;alert(1)", body)

    def test_rows_render_without_the_script(self) -> None:
        with self.conn() as conn:
            db.claim_request(conn, db.queue_request(conn, ACME, "tailor")[0], 71)
        body = self.get("/learn")[2]
        self.assertIn('class="workers" data-workers', body)
        self.assertEqual(body.count('<details class="worker-pop">'), 4)
        self.assertIn('<form method="post" action="/workers/stop"', body)  # Stop works as a plain form
        self.assertIn('href="/runs"', body)

    def test_no_workers_no_section(self) -> None:
        self.settings = self.settings.__class__(**{**self.settings.__dict__, "workers": (), "assistant_task": None,
                                                   "prep_assistant_task": None})
        self.assertNotIn("data-workers", self.get("/")[2])
        self.assertEqual(self.workers_json()["workers"], [])

    def test_toast_for_long_items_finished_since(self) -> None:
        since = (datetime.now(timezone.utc) - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.conn() as conn:
            rid = db.queue_request(conn, ACME, "tailor")[0]
            db.claim_request(conn, rid, 71)
            db.finish_requests(conn, [ACME], "tailor")
            msg = db.add_message(conn, STARK, "Quick question")
            db.claim_message(conn, msg, 22)
            db.reply_message(conn, STARK, msg, "Answer")
        done = self.workers_json(f"?since={since}")["finished"]
        self.assertEqual([d["text"] for d in done], ["Tailored CV for Acme Rockets is ready"])  # not the chat reply
        self.assertEqual(done[0]["href"], f"/?app={ACME}")
        self.assertEqual(self.workers_json()["finished"], [])  # no since: nothing

    def test_run_history_filters(self) -> None:
        with self.conn() as conn:
            for app, seq in ((ACME, 71), (STARK, 22)):
                rid = db.queue_request(conn, app, "research")[0]
                db.claim_request(conn, rid, seq)
            db.block_requests(conn, ACME, "no posting")
        def main(path: str) -> str:
            body = self.get(path)[2]
            return body[body.index("<main"):]  # the sidebar names what workers do now

        body = main("/runs")
        self.assertIn("no posting", body)
        self.assertIn("Run history", body)
        self.assertNotIn("Stark Example Ltd", body)  # still running: not a finished run
        self.assertIn("Stark Example Ltd", main("/runs?result=running"))
        self.assertNotIn("no posting", main("/runs?worker=22"))
        self.assertIn("no posting", main("/runs?result=blocked&worker=71"))


if __name__ == "__main__":
    import unittest
    unittest.main()
