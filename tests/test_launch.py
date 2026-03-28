import os
import tempfile
import urllib.error
import unittest
from unittest import mock
from pathlib import Path

from unchained_cli import launch


class FakeProcess:
    def __init__(self, pid=1234, returncode=None):
        self.pid = pid
        self.returncode = returncode

    def poll(self):
        return self.returncode


class LaunchTests(unittest.TestCase):
    def test_launch_chrome_uses_existing_cdp_session(self):
        with mock.patch.object(
            launch,
            "_version_json",
            return_value={"Browser": "Chrome"},
        ), mock.patch.object(launch, "_open_tab") as open_tab, mock.patch.object(
            launch.subprocess,
            "Popen",
        ) as popen:
            result = launch.launch_chrome(port=9222, startup_url="https://example.com")

        self.assertTrue(result["already_running"])
        self.assertIsNone(result["profile"])
        self.assertIsNone(result["profile_dir"])
        self.assertEqual(result["requested_profile"], "default")
        open_tab.assert_called_once_with("127.0.0.1", 9222, "https://example.com")
        popen.assert_not_called()

    def test_open_tab_wraps_transport_errors(self):
        with mock.patch.object(
            launch.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("boom"),
        ):
            with self.assertRaisesRegex(launch.LaunchError, "Failed to open a tab via Chrome CDP"):
                launch._open_tab("127.0.0.1", 9222, "https://example.com")

    def test_launch_chrome_starts_binary_with_hardened_flags(self):
        process = FakeProcess(pid=4321)
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            launch,
            "DEFAULT_DATA_DIR",
            Path(tmpdir),
        ), mock.patch.object(
            launch.platform,
            "system",
            return_value="Linux",
        ), mock.patch.object(
            launch,
            "_version_json",
            side_effect=[None, None, {"Browser": "Chrome"}],
        ), mock.patch.object(
            launch,
            "_find_chrome_binary",
            return_value="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ), mock.patch.object(
            launch,
            "_ensure_page_tab",
            return_value=False,
        ) as ensure_tab, mock.patch.object(
            launch.subprocess,
            "Popen",
            return_value=process,
        ) as popen, mock.patch.object(
            launch.time,
            "sleep",
        ):
            result = launch.launch_chrome(
                port=9223,
                profile="demo",
                startup_url="https://example.com",
                extra_args=["--incognito"],
            )

        self.assertFalse(result["already_running"])
        cmd = popen.call_args.args[0]
        self.assertIn("--user-data-dir=", " ".join(cmd))
        self.assertIn("--remote-debugging-port=9223", cmd)
        self.assertIn("--no-first-run", cmd)
        self.assertIn("--no-default-browser-check", cmd)
        self.assertIn("--incognito", cmd)
        self.assertEqual(cmd[-1], "https://example.com")
        ensure_tab.assert_called_once_with("127.0.0.1", 9223, "https://example.com")

    def test_launch_command_uses_open_na_for_macos_app_bundle(self):
        with mock.patch.object(launch.platform, "system", return_value="Darwin"):
            cmd = launch._build_launch_command(
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                profile_dir=Path("/tmp/chrome_demo"),
                port=9222,
                startup_url="about:blank",
                headless=False,
                extra_args=["--incognito"],
            )

        self.assertEqual(cmd[:4], ["open", "-na", "/Applications/Google Chrome.app", "--args"])
        self.assertIn("--user-data-dir=/tmp/chrome_demo", cmd)
        self.assertIn("--remote-debugging-port=9222", cmd)
        self.assertIn("--incognito", cmd)
        self.assertEqual(cmd[-1], "about:blank")

    def test_launch_chrome_allows_open_wrapper_to_exit(self):
        process = FakeProcess(pid=9999, returncode=0)
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            launch,
            "DEFAULT_DATA_DIR",
            Path(tmpdir),
        ), mock.patch.object(
            launch.platform,
            "system",
            return_value="Darwin",
        ), mock.patch.object(
            launch,
            "_version_json",
            side_effect=[None, {"Browser": "Chrome"}],
        ), mock.patch.object(
            launch,
            "_find_chrome_binary",
            return_value="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ), mock.patch.object(
            launch,
            "_ensure_page_tab",
            return_value=False,
        ), mock.patch.object(
            launch.subprocess,
            "Popen",
            return_value=process,
        ), mock.patch.object(
            launch.time,
            "sleep",
        ):
            result = launch.launch_chrome(port=9333, startup_url="about:blank")

        self.assertFalse(result["already_running"])
        self.assertEqual(result["port"], 9333)
        self.assertNotIn("pid", result)

    def test_launch_chrome_raises_on_open_wrapper_failure(self):
        process = FakeProcess(pid=9999, returncode=1)
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            launch,
            "DEFAULT_DATA_DIR",
            Path(tmpdir),
        ), mock.patch.object(
            launch.platform,
            "system",
            return_value="Darwin",
        ), mock.patch.object(
            launch,
            "_version_json",
            return_value=None,
        ), mock.patch.object(
            launch,
            "_find_chrome_binary",
            return_value="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ), mock.patch.object(
            launch.subprocess,
            "Popen",
            return_value=process,
        ), mock.patch.object(
            launch.time,
            "sleep",
        ):
            with self.assertRaisesRegex(launch.LaunchError, "Chrome launcher exited before CDP became ready"):
                launch.launch_chrome(port=9333, timeout=1.0)

    def test_launch_chrome_adds_headless_flags(self):
        process = FakeProcess()
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            launch,
            "DEFAULT_DATA_DIR",
            Path(tmpdir),
        ), mock.patch.object(
            launch.platform,
            "system",
            return_value="Linux",
        ), mock.patch.object(
            launch,
            "_version_json",
            side_effect=[None, {"Browser": "Chrome"}],
        ), mock.patch.object(
            launch,
            "_find_chrome_binary",
            return_value="/tmp/chrome",
        ), mock.patch.object(
            launch,
            "_ensure_page_tab",
            return_value=False,
        ), mock.patch.object(
            launch.subprocess,
            "Popen",
            return_value=process,
        ) as popen, mock.patch.object(
            launch.time,
            "sleep",
        ):
            launch.launch_chrome(headless=True)

        cmd = popen.call_args.args[0]
        self.assertIn("--headless=new", cmd)
        self.assertIn("--disable-gpu", cmd)

    def test_launch_chrome_requires_binary(self):
        with mock.patch.object(launch, "_version_json", return_value=None), mock.patch.object(
            launch,
            "_find_chrome_binary",
            return_value=None,
        ):
            with self.assertRaisesRegex(launch.LaunchError, "No Chrome/Chromium binary found"):
                launch.launch_chrome()

    def test_launch_chrome_already_running_uses_ensure_page_tab_for_blank_url(self):
        with mock.patch.object(
            launch,
            "_version_json",
            return_value={"Browser": "Chrome"},
        ), mock.patch.object(launch, "_ensure_page_tab", return_value=True) as ensure_tab, \
                mock.patch.object(launch, "_open_tab") as open_tab:
            result = launch.launch_chrome(port=9222, startup_url="about:blank")

        self.assertTrue(result["already_running"])
        ensure_tab.assert_called_once_with("127.0.0.1", 9222, "about:blank")
        open_tab.assert_not_called()

    def test_build_launch_command_linux_binary(self):
        with mock.patch.object(launch.platform, "system", return_value="Linux"):
            cmd = launch._build_launch_command(
                "/usr/bin/google-chrome",
                profile_dir=Path("/tmp/chrome_default"),
                port=9222,
                startup_url="about:blank",
                headless=False,
                extra_args=None,
            )

        self.assertEqual(cmd[0], "/usr/bin/google-chrome")
        self.assertIn("--user-data-dir=/tmp/chrome_default", cmd)
        self.assertIn("--remote-debugging-port=9222", cmd)
        self.assertNotIn("open", cmd)

    def test_launch_chrome_rejects_user_data_dir_in_extra_args(self):
        with self.assertRaisesRegex(launch.LaunchError, "--user-data-dir"):
            launch.launch_chrome(extra_args=["--user-data-dir=/tmp/evil"])

    def test_find_chrome_binary_raises_on_bad_env_override(self):
        with mock.patch.dict(os.environ, {"UNCHAINED_CHROME_BIN": "/nonexistent/chrome"}):
            with self.assertRaisesRegex(launch.LaunchError, "UNCHAINED_CHROME_BIN"):
                launch._find_chrome_binary()

    def test_launch_chrome_raises_on_cdp_timeout(self):
        process = FakeProcess(pid=1234)
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            launch,
            "DEFAULT_DATA_DIR",
            Path(tmpdir),
        ), mock.patch.object(
            launch.platform,
            "system",
            return_value="Linux",
        ), mock.patch.object(
            launch,
            "_version_json",
            return_value=None,
        ), mock.patch.object(
            launch,
            "_find_chrome_binary",
            return_value="/tmp/chrome",
        ), mock.patch.object(
            launch.subprocess,
            "Popen",
            return_value=process,
        ), mock.patch.object(
            launch.time,
            "monotonic",
            side_effect=[0.0, 0.1, 0.2, 99.0],
        ), mock.patch.object(
            launch.time,
            "sleep",
        ):
            with self.assertRaisesRegex(launch.LaunchError, "did not expose CDP"):
                launch.launch_chrome(port=9222, timeout=1.0)

    def test_sanitize_profile_raises_on_long_name(self):
        with self.assertRaisesRegex(launch.LaunchError, "too long"):
            launch._sanitize_profile("a" * 33)


if __name__ == "__main__":
    unittest.main()
