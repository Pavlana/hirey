import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from prep import db, getjo
from prep.seed_example import build_getjo_db, build_prep_db, load_fixtures


class SeedTest(unittest.TestCase):
    def test_builds_both_databases(self) -> None:
        data = load_fixtures()
        with tempfile.TemporaryDirectory() as tmp:
            prep_path, getjo_path = Path(tmp) / "example.db", Path(tmp) / "example-getjo.db"
            build_prep_db(prep_path, data)
            build_prep_db(prep_path, data)  # rebuilding replaces, never duplicates
            build_getjo_db(getjo_path, data["getjo_jobs"])
            with closing(db.connect(prep_path)) as conn:
                self.assertEqual(len(db.list_applications(conn)), len(data["applications"]))
                next_actions = [a for a in data["applications"] if a.get("next_action")
                                and a["status"] not in ("rejected", "withdrawn", "no_response")]
                self.assertEqual(len(db.list_tasks(conn, None)), len(data["tasks"]) + len(next_actions))
            with closing(getjo.connect_ro(getjo_path)) as gconn:
                self.assertEqual(len(getjo.matches(gconn)), 3)

    def test_fixtures_hold_no_pay(self) -> None:
        for app in load_fixtures()["applications"]:
            self.assertNotIn("pay", app)


if __name__ == "__main__":
    unittest.main()
