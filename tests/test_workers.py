"""Assistant scaling, step 1: the [workers] router (queued research and briefs to a Research worker,
thread messages to the Applications Assistant), claim leases (requests 6 h, messages 30 min) with two
connections racing, WAL with a busy timeout, and the dashboard showing who handles queued work and a
message whose claim lapsed. dev3 is never called. Fictional data only."""

import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from prep import assistant, db, web
from prep.config import ConfigError, Settings, load_settings
from tests.test_resend_cancel import ResendCancelBase
from tests.test_thread import ACME


def ago(**delta: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def settings(**kw) -> Settings:
    return Settings(getjo_db=None, prep_db=Path("/tmp/x.db"), private_dir=None, **kw)


class WorkersSettingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"INTERPREP_SETTINGS": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("PREP_WORKER_RESEARCH", None)

    def load(self, text: str) -> Settings:
        path = Path(self._tmp.name) / "settings.toml"
        path.write_text(text, encoding="utf-8")
        return load_settings(path)

    def test_unset_means_no_workers(self) -> None:
        self.assertEqual(self.load("").workers, ())

    def test_research_worker_from_the_file(self) -> None:
        self.assertEqual(self.load("[workers]\nresearch = 70\n").workers, (("research", 70),))

    def test_env_overrides_and_empty_clears(self) -> None:
        with mock.patch.dict(os.environ, {"PREP_WORKER_RESEARCH": "71"}):
            self.assertEqual(self.load("[workers]\nresearch = 70\n").workers, (("research", 71),))
        with mock.patch.dict(os.environ, {"PREP_WORKER_RESEARCH": ""}):
            self.assertEqual(self.load("[workers]\nresearch = 70\n").workers, ())

    def test_bad_values_are_refused(self) -> None:
        for text in ("[workers]\nresearch = 0\n", "[workers]\nresearch = \"x\"\n", "[workers]\nresearch = true\n",
                     "[workers]\nresaerch = 70\n", "workers = 70\n"):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                self.load(text)


class RouterTest(unittest.TestCase):
    def test_no_worker_is_todays_behaviour(self) -> None:
        s = settings(assistant_task=9)
        for kind in ("research", "cv_fit", "tailor", "cv_eval", "brief", "message"):
            self.assertEqual(assistant.target(s, kind), (9, "Applications Assistant"))
        argv = assistant.commands(s, [{"application_id": 4, "kind": "tailor"}], "/bin/dev3")
        self.assertEqual(argv, [["/bin/dev3", "message", "--task", "seq:9", "--subject",
                                 "Tailored CV queued for application 4", "/research 4"]])

    def test_research_and_briefs_go_to_the_worker_messages_stay(self) -> None:
        s = settings(assistant_task=9, workers=(("research", 70),))
        for kind in ("research", "cv_fit", "tailor", "cv_eval", "brief"):
            self.assertEqual(assistant.target(s, kind), (70, "Research worker"))
        self.assertEqual(assistant.target(s, "message"), (9, "Applications Assistant"))
        argv = assistant.commands(s, [{"application_id": 4, "kind": "research"}, {"application_id": 4, "kind": "cv_fit"}],
                                  "/bin/dev3")
        self.assertEqual(len(argv), 1)
        self.assertEqual(argv[0][3:5], ["seq:70", "--subject"])
        self.assertEqual(assistant.brief_command(s, 2, "/bin/dev3")[3], "seq:70")
        self.assertEqual(assistant.ask_command(s, 4, 11, "/bin/dev3")[3], "seq:9")

    def test_worker_alone_still_gets_pinged(self) -> None:
        s = settings(workers=(("research", 70),), dev3_bin=None)
        with mock.patch.object(assistant, "dev3_path", return_value="/bin/dev3"), \
                mock.patch.object(assistant, "_send", return_value=1) as send:
            self.assertEqual(assistant.notify(s, [{"application_id": 4, "kind": "research"}]), 1)
            self.assertEqual(send.call_args[0][0][0][3], "seq:70")
            self.assertEqual(assistant.notify_briefs(s, 1), 1)
            self.assertEqual(assistant.notify_message(s, 4, 11), 0)  # no assistant_task: messages are not sent
        self.assertIsNone(assistant.notify_in_background(settings(), [{"application_id": 4, "kind": "research"}]))


class LeaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "prep.db"
        with closing(db.connect(self.path)) as conn:
            self.app = db.add_application(conn, company="Acme Robotics", role="Engineer")

    def two(self) -> tuple[sqlite3.Connection, sqlite3.Connection]:
        a, b = db.connect(self.path), db.connect(self.path)
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        return a, b

    def test_wal_and_busy_timeout(self) -> None:
        a, b = self.two()
        self.assertEqual(a.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(a.execute("PRAGMA busy_timeout").fetchone()[0], db.BUSY_TIMEOUT_MS)
        db.add_message(a, self.app, "first")
        db.add_message(b, self.app, "second")
        db.add_message(a, self.app, "third")
        self.assertEqual(len(db.pending_messages(b)), 3)

    def test_two_workers_never_take_the_same_request(self) -> None:
        a, b = self.two()
        rid, _ = db.queue_request(a, self.app, "research")
        self.assertEqual([db.claim_request(a, rid), db.claim_request(b, rid)], [True, False])

    def test_an_expired_request_claim_returns_to_the_queue(self) -> None:
        a, b = self.two()
        rid, _ = db.queue_request(a, self.app, "research")
        db.claim_request(a, rid)
        a.execute("UPDATE agent_requests SET claimed_at = ? WHERE id = ?", (ago(hours=db.CLAIM_HOURS + 1), rid))
        a.commit()
        self.assertFalse(db.open_requests(b)[0]["claimed"])
        self.assertTrue(db.claim_request(b, rid))
        self.assertFalse(db.claim_request(a, rid))

    def test_an_expired_brief_claim_returns_to_the_queue(self) -> None:
        a, b = self.two()
        db.queue_brief(a, "job-1")
        self.assertEqual([db.claim_brief(a, "job-1"), db.claim_brief(b, "job-1")], [True, False])
        a.execute("UPDATE brief_requests SET claimed_at = ?", (ago(hours=db.CLAIM_HOURS + 1),))
        a.commit()
        self.assertTrue(db.claim_brief(b, "job-1"))

    def test_two_workers_never_take_the_same_message(self) -> None:
        a, b = self.two()
        msg = db.add_message(a, self.app, "Is this remote?")
        self.assertEqual([db.claim_message(a, msg), db.claim_message(b, msg)], [True, False])
        self.assertEqual(db.claim_messages(b, self.app), [])

    def test_a_message_claim_lapses_after_its_lease(self) -> None:
        a, b = self.two()
        msg = db.add_message(a, self.app, "Is this remote?")
        db.claim_message(a, msg)
        a.execute("UPDATE agent_messages SET claimed_at = ? WHERE id = ?", (ago(minutes=db.MESSAGE_CLAIM_MINUTES - 5), msg))
        a.commit()
        self.assertFalse(db.claim_lapsed(db.get_message(b, msg)))
        self.assertEqual(db.claim_messages(b, self.app), [])
        a.execute("UPDATE agent_messages SET claimed_at = ? WHERE id = ?", (ago(minutes=db.MESSAGE_CLAIM_MINUTES + 1), msg))
        a.commit()
        self.assertTrue(db.claim_lapsed(db.get_message(b, msg)))
        self.assertFalse(db.pending_messages(b)[0]["claimed"])
        self.assertEqual(db.claim_messages(b, self.app), [msg])
        self.assertFalse(db.claim_message(a, msg))


class DashboardTest(ResendCancelBase):
    def test_queued_work_names_its_handler_and_time(self) -> None:
        with self.conn() as conn:
            db.queue_request(conn, ACME, "cv_fit")
        self.assertIn("CV fit (no assistant task set: run /research)", self.get(f"/?app={ACME}")[2])
        self.with_tasks()
        self.assertIn("CV fit → Applications Assistant", self.get(f"/?app={ACME}")[2])
        self.settings = self.settings.__class__(**{**self.settings.__dict__, "workers": (("research", 70),)})
        body = self.get(f"/?app={ACME}")[2]
        self.assertIn("CV fit → Research worker", body)
        self.assertNotIn("queued Nov", body)  # times live in the Workers sidebar now

    def test_a_lapsed_message_claim_shows_resend_at_once(self) -> None:
        self.with_tasks()
        with self.conn() as conn:
            msg = db.add_message(conn, ACME, "Is this remote?")
            db.claim_message(conn, msg)
        body = self.get("/")[2]
        self.assertIn("→ Applications Assistant", body)
        self.assertNotIn("no reply in 30 min", body)
        with self.conn() as conn:
            conn.execute("UPDATE agent_messages SET claimed_at = ?, created_at = ? WHERE id = ?",
                         (ago(minutes=45), ago(minutes=50), msg))
            conn.commit()
        assistant._PINGS[msg] = (time.time(), True)  # a fresh ping would hide Resend for 30 s
        body = self.get("/")[2]
        self.assertIn("no reply in 30 min", body)
        self.assertNotIn("→ Applications Assistant", body)  # the flag replaces the route tag
        self.assertRegex(body, r'data-resend-due="\d+" title=')  # not hidden
        self.assertIn('action="/applications/stop"', body)


class QueueLineTest(ResendCancelBase):
    def test_session_start_line_names_the_worker(self) -> None:
        with self.conn() as conn:
            db.queue_request(conn, ACME, "research")
        _, out, _ = self.run_cli("queue")
        self.assertNotIn("Research worker (dev3 task", out)
        with mock.patch.dict(os.environ, {"PREP_WORKER_RESEARCH": "70"}):
            _, out, _ = self.run_cli("queue")
        self.assertIn("Research worker (dev3 task seq 70)", out)


if __name__ == "__main__":
    unittest.main()
