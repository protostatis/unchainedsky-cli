import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin" / "unchained"


class LauncherTests(unittest.TestCase):
    def test_launcher_smoke_with_supported_python(self):
        env = os.environ.copy()
        env["UNCHAINED_PYTHON"] = sys.executable

        result = subprocess.run(
            [str(BIN), "--help"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Browser automation over local Chrome CDP.", result.stdout)

    def test_launcher_rejects_unsupported_python(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_python = Path(tmpdir) / "python-old"
            fake_python.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            fake_python.chmod(0o755)

            env = os.environ.copy()
            env["UNCHAINED_PYTHON"] = str(fake_python)

            result = subprocess.run(
                [str(BIN), "--help"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("requires Python 3.10+", result.stderr)


if __name__ == "__main__":
    unittest.main()
