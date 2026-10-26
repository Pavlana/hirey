import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from prep import config
from prep.config import REPO_ROOT, ConfigError, load_settings

CLEAN_ENV = {"INTERPREP_SETTINGS": "", "PREP_DB": "", "PREP_GETJO_DB": "", "PREP_PRIVATE_DIR": ""}


class LoadSettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        patcher = mock.patch.dict(os.environ, CLEAN_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, text: str) -> Path:
        path = self.tmp / "settings.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_missing_file_gives_defaults(self) -> None:
        s = load_settings(self.tmp / "absent.toml")
        self.assertIsNone(s.getjo_db)
        self.assertIsNone(s.private_dir)
        self.assertEqual(s.prep_db, REPO_ROOT / "data" / "prep.db")

    def test_reads_file(self) -> None:
        path = self.write(f'getjo_db = "{self.tmp}/jobs.db"\nprep_db = "{self.tmp}/prep.db"\nprivate_dir = "{self.tmp}"\n')
        s = load_settings(path)
        self.assertEqual(s.getjo_db, self.tmp / "jobs.db")
        self.assertEqual(s.prep_db, self.tmp / "prep.db")
        self.assertEqual(s.private_dir, self.tmp)

    def test_relative_prep_db_resolves_against_repo_root(self) -> None:
        s = load_settings(self.write('prep_db = "data/other.db"\n'))
        self.assertEqual(s.prep_db, REPO_ROOT / "data" / "other.db")

    def test_env_overrides_file(self) -> None:
        path = self.write(f'getjo_db = "{self.tmp}/a.db"\n')
        with mock.patch.dict(os.environ, {"PREP_GETJO_DB": f"{self.tmp}/b.db"}):
            self.assertEqual(load_settings(path).getjo_db, self.tmp / "b.db")

    def test_settings_path_from_env(self) -> None:
        path = self.write(f'private_dir = "{self.tmp}"\n')
        with mock.patch.dict(os.environ, {"INTERPREP_SETTINGS": str(path)}):
            self.assertEqual(load_settings().private_dir, self.tmp)

    def test_falls_back_to_user_file(self) -> None:
        user_file = self.write(f'private_dir = "{self.tmp}"\n')
        with mock.patch.object(config, "SETTINGS_PATH", self.tmp / "absent.toml"), \
                mock.patch.object(config, "USER_SETTINGS_PATH", user_file):
            self.assertEqual(config.settings_path(), user_file)
            self.assertEqual(load_settings().private_dir, self.tmp)

    def test_repo_file_wins_over_user_file(self) -> None:
        repo_file = self.write("")
        with mock.patch.object(config, "SETTINGS_PATH", repo_file), \
                mock.patch.object(config, "USER_SETTINGS_PATH", self.tmp / "user.toml"):
            self.assertEqual(config.settings_path(), repo_file)
            with mock.patch.dict(os.environ, {"INTERPREP_SETTINGS": str(self.tmp / "env.toml")}):
                self.assertEqual(config.settings_path(), self.tmp / "env.toml")

    def test_relative_getjo_path_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            load_settings(self.write('getjo_db = "../getjo/data/jobs.db"\n'))

    def test_invalid_toml(self) -> None:
        with self.assertRaises(ConfigError):
            load_settings(self.write("getjo_db = \n"))


if __name__ == "__main__":
    unittest.main()
