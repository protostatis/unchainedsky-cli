import io
import unittest
from contextlib import redirect_stderr
from unittest import mock

from unchained_cli import ddm


class DdmTests(unittest.TestCase):
    def test_run_ddm_reports_missing_binary(self):
        stderr = io.StringIO()
        with mock.patch.object(ddm, "_find_binary", return_value=None), redirect_stderr(stderr):
            code = ddm.run_ddm(9222, "auto", [])

        self.assertEqual(code, 1)
        self.assertIn("DDM binary not found.", stderr.getvalue())

    def test_run_ddm_invokes_binary(self):
        completed = mock.Mock(returncode=0)
        with mock.patch.object(ddm, "_find_binary", return_value="/tmp/ddm"), mock.patch.object(
            ddm.subprocess,
            "run",
            return_value=completed,
        ) as run:
            code = ddm.run_ddm(9222, "tab-1", ["--text"])

        self.assertEqual(code, 0)
        run.assert_called_once_with(["/tmp/ddm", "--port", "9222", "--tab", "tab-1", "--text"])


if __name__ == "__main__":
    unittest.main()
