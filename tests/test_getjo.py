import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from prep import getjo
from prep.seed_example import build_getjo_db, load_fixtures


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
        found = getjo.matches(self.conn)
        self.assertEqual([j["id"] for j in found], [
            "ashby:initech-cloud:x1", "lever:globex-labs:abc", "greenhouse:acme-rockets:1001",
        ])
        self.assertNotIn("description", found[0])

    def test_matches_excludes_linked_and_respects_threshold(self) -> None:
        found = getjo.matches(self.conn, {"lever:globex-labs:abc"}, threshold=8)
        self.assertEqual([j["id"] for j in found], ["greenhouse:acme-rockets:1001"])
        self.assertEqual(len(getjo.matches(self.conn, limit=1)), 1)

    def test_lookups(self) -> None:
        self.assertEqual(getjo.get_job(self.conn, "lever:globex-labs:abc")["company"], "Globex Labs")
        self.assertIsNone(getjo.get_job(self.conn, "nope"))
        job = getjo.find_by_url(self.conn, "https://jobs.example.com/globex-labs/abc/")
        self.assertEqual(job["id"], "lever:globex-labs:abc")
        self.assertEqual(len(getjo.find_by_company_role(self.conn, "acme rockets", "platform engineer")), 1)

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
