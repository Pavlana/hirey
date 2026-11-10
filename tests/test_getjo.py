import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from prep import getjo
from prep.seed_example import build_getjo_db, load_fixtures

NOW = "2026-01-20T12:00:00Z"  # the fixtures' anchor date


class GetjoReadOnlyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # A space in the path checks the file: URI is built correctly.
        self.path = Path(self._tmp.name) / "get jo" / "jobs.db"
        build_getjo_db(self.path, load_fixtures()["getjo_jobs"])
        self.conn = getjo.connect_ro(self.path)
        self.addCleanup(self.conn.close)

    def test_matches_threshold_and_order(self) -> None:
        found = getjo.matches(self.conn, now=NOW)
        self.assertEqual([j["id"] for j in found], [
            "ashby:wonka-systems:w2", "ashby:initech-cloud:x1", "lever:globex-labs:abc", "greenhouse:acme-rockets:1001",
        ])  # not the closed Oscorp job nor the expired Pied Piper one
        self.assertNotIn("description", found[0])
        self.assertEqual(set(found[0]) - set(getjo.BASE_COLUMNS), set(getjo.STATE_COLUMNS))
        later = [j["id"] for j in getjo.matches(self.conn, now="2026-01-24T00:00:00Z")]
        self.assertNotIn("ashby:wonka-systems:w2", later, "expires_at reached: expired")
        self.assertIn("ashby:wonka-systems:w2", [j["id"] for j in getjo.matches(self.conn, now="2026-01-23T23:59:59Z")])

    def test_matches_excludes_linked_and_respects_threshold(self) -> None:
        found = getjo.matches(self.conn, {"lever:globex-labs:abc"}, threshold=8, now=NOW)
        self.assertEqual([j["id"] for j in found], ["greenhouse:acme-rockets:1001"])
        self.assertEqual(len(getjo.matches(self.conn, limit=1)), 1)

    def test_lookups(self) -> None:
        self.assertEqual(getjo.get_job(self.conn, "lever:globex-labs:abc")["company"], "Globex Labs")
        self.assertIsNone(getjo.get_job(self.conn, "nope"))
        job = getjo.find_by_url(self.conn, "https://jobs.example.com/globex-labs/abc/")
        self.assertEqual(job["id"], "lever:globex-labs:abc")
        self.assertEqual(len(getjo.find_by_company_role(self.conn, "acme rockets", "platform engineer")), 1)
        found = getjo.jobs_by_id(self.conn, {"greenhouse:oscorp-ai:42", "lever:globex-labs:abc", "nope"})
        self.assertEqual(set(found), {"greenhouse:oscorp-ai:42", "lever:globex-labs:abc"})  # closed ones too
        self.assertEqual(found["greenhouse:oscorp-ai:42"]["closed_at"], "2026-01-19T06:00:00Z")
        self.assertEqual(getjo.jobs_by_id(self.conn, set()), {})

    def test_an_older_jobs_table_without_the_state_columns(self) -> None:
        old = Path(self._tmp.name) / "old.db"
        build_getjo_db(old, load_fixtures()["getjo_jobs"], old_schema=True)
        with closing(getjo.connect_ro(old)) as conn:
            found = getjo.matches(conn, now=NOW)
            self.assertIn("greenhouse:oscorp-ai:42", [j["id"] for j in found])  # nothing reads as closed
            self.assertIn("lever:pied-piper:pp7", [j["id"] for j in found])
            job = getjo.get_job(conn, "greenhouse:oscorp-ai:42")
        self.assertEqual({k: job[k] for k in getjo.STATE_COLUMNS}, dict.fromkeys(getjo.STATE_COLUMNS))
        self.assertEqual(getjo.job_state(job, NOW, 3), {"closed": False, "may_be_closed": False, "expires_in": None})

    def test_write_attempt_fails(self) -> None:
        with self.assertRaises(sqlite3.OperationalError) as ctx:
            self.conn.execute("UPDATE jobs SET score = 1")
        self.assertIn("readonly", str(ctx.exception))
        with self.assertRaises(sqlite3.OperationalError):
            self.conn.execute("CREATE TABLE x (y)")
        with closing(sqlite3.connect(self.path)) as rw:
            self.assertEqual(rw.execute("SELECT count(*) FROM jobs WHERE score = 1").fetchone()[0], 0)

    def test_missing_db(self) -> None:
        missing = Path(self._tmp.name) / "absent.db"
        with self.assertRaises(getjo.GetjoUnavailable):
            getjo.connect_ro(missing)
        self.assertFalse(missing.exists(), "read-only open must not create the file")
        with self.assertRaises(getjo.GetjoUnavailable):
            getjo.connect_ro(None)


if __name__ == "__main__":
    unittest.main()


class LocationTagsTest(unittest.TestCase):
    def test_rules(self) -> None:
        cases = {
            "London": {"london"},
            "London, UK": {"london"},
            "Hybrid - London": {"london"},
            "central LONDON": {"london"},
            "Remote": {"remote"},
            "Remote (UK)": {"remote"},
            "UK - remote first": {"remote"},
            "Anywhere": {"remote"},
            "London / Remote": {"london", "remote"},
            "Remote or London": {"london", "remote"},
            "Manchester": {"other"},
            "Londonderry": {"other"},
            "Remoteville": {"other"},
            "": {"other"},
            None: {"other"},
        }
        for location, tags in cases.items():
            with self.subTest(location=location):
                self.assertEqual(getjo.location_tags(location), tags)


class JobStateTest(unittest.TestCase):
    def state(self, **job: str | None) -> dict:
        return getjo.job_state({k: job.get(k) for k in getjo.STATE_COLUMNS}, NOW, 3)

    def test_rules(self) -> None:
        self.assertEqual(self.state(), {"closed": False, "may_be_closed": False, "expires_in": None})
        self.assertTrue(self.state(closed_at="2026-01-19T00:00:00Z")["closed"])
        self.assertTrue(self.state(expires_at="2026-01-20T11:59:59Z")["closed"])
        self.assertTrue(self.state(expires_at=NOW)["closed"], "expires_at reached counts as expired")
        self.assertEqual(self.state(expires_at="2026-01-20T23:00:00Z")["expires_in"], 0)
        self.assertEqual(self.state(expires_at="2026-01-27T00:00:00Z")["expires_in"], 7)
        self.assertIsNone(self.state(expires_at="2026-01-28T00:00:00Z")["expires_in"], "more than a week away")
        self.assertFalse(self.state(last_seen="2026-01-17T06:00:00Z")["may_be_closed"], "3 days: not yet")
        self.assertTrue(self.state(last_seen="2026-01-16T06:00:00Z")["may_be_closed"])
        closed = self.state(closed_at="2026-01-19T00:00:00Z", last_seen="2026-01-01T00:00:00Z",
                            expires_at="2026-01-21T00:00:00Z")
        self.assertEqual(closed, {"closed": True, "may_be_closed": False, "expires_in": None})
        self.assertFalse(self.state(last_seen="garbage")["may_be_closed"])
