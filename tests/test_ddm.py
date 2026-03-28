import io
import unittest
from contextlib import redirect_stderr
from unittest import mock

from unchained_cli import ddm


class DdmTests(unittest.TestCase):
    def test_run_ddm_falls_back_to_engine_when_no_binary(self):
        """When no binary is found, DDM falls back to the Python engine."""
        with mock.patch.object(ddm, "_find_binary", return_value=None), \
             mock.patch("unchained_cli.ddm_engine.run", side_effect=SystemExit(0)) as engine_run:
            code = ddm.run_ddm(9222, "auto", ["--help"])

        self.assertEqual(code, 0)

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
