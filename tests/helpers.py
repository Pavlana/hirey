"""Shared test setup: point every setting at a temp dir so real data is never touched."""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from prep import cli
from prep.seed_example import build_getjo_db, load_fixtures


class IsolatedTestCase(unittest.TestCase):
    """Each test gets its own temp prep.db and a fixture getjo db built from tests/fixtures."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.prep_db = self.tmp / "prep.db"
        self.getjo_db = self.tmp / "getjo" / "jobs.db"
        self.fixtures = load_fixtures()
        build_getjo_db(self.getjo_db, self.fixtures["getjo_jobs"])
        env = {
            "INTERPREP_SETTINGS": str(self.tmp / "no-settings.toml"),
            "PREP_DB": str(self.prep_db),
            "PREP_GETJO_DB": str(self.getjo_db),
            "PREP_PRIVATE_DIR": str(self.tmp / "private"),
            "PREP_DASHBOARD_PORT": "",
            "PREP_WEEKLY_TARGET": "",
            "PREP_NO_RESPONSE_DAYS": "",
            "PREP_RESEARCH_MAX_AGE_DAYS": "",
            "PREP_STALE_DAYS": "",
            "PREP_BRIEF_AUTO_MIN_SCORE": "",
            "PREP_ASSISTANT_TASK": "",
            "PREP_ASSISTANT_PROJECT": "",
            "PREP_DEV3_BIN": "",
            "PREP_PDF_PYTHON": "",
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.main(list(argv))
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 2
                if isinstance(e.code, str):
                    err.write(e.code)
        return code, out.getvalue(), err.getvalue()
