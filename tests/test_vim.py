"""The Learn editor's vim keys: runs tests/vim_test.js (pure key handling in prep/static/vim.js)
under node. Skipped where node is not installed; nothing here needs the network."""

import shutil
import subprocess
import unittest
from pathlib import Path

NODE = shutil.which("node")
SCRIPT = Path(__file__).with_name("vim_test.js")


@unittest.skipUnless(NODE, "node is not installed")
class VimKeysTest(unittest.TestCase):
    def test_vim_key_handling(self) -> None:
        result = subprocess.run([NODE, str(SCRIPT)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("vim tests passed", result.stdout)
