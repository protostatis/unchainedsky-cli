import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from unchained_cli import cli


ROOT = Path(__file__).resolve().parents[1]


class FakeClient:
    def __init__(self) -> None:
        self.cookies_args = None

    def list_tabs(self):
        return [{"id": "tab-1", "title": "Example", "url": "https://example.com"}]

    def resolve_tab(self, tab_id):
        return "tab-1"

    def navigate(self, tab_id, url):
        return {"frameId": "frame-1"}

    def js_eval(self, tab_id, expression):
        return "https://example.com/final"

    def get_cookies(self, tab_id, urls):
        self.cookies_args = (tab_id, urls)
        return [{"name": "session", "value": "abc", "domain": ".example.com"}]


class CliCommandTests(unittest.TestCase):
    def test_cmd_launch_reports_started_chrome(self):
        args = SimpleNamespace(
            port=9222,
            profile="default",
            headless=False,
            url="https://example.com",
            timeout=15.0,
            chrome_args=[],
            json=False,
        )

        stdout = io.StringIO()
        with mock.patch.object(
            cli._launch,
            "launch_chrome",
            return_value={
                "already_running": False,
                "host": "127.0.0.1",
                "port": 9222,
                "pid": 1234,
                "profile_dir": "/tmp/profile",
                "startup_url": "https://example.com",
            },
        ), redirect_stdout(stdout):
            cli.cmd_launch(args)

        text = stdout.getvalue()
        self.assertIn("Chrome started → http://127.0.0.1:9222 (PID 1234)", text)
        self.assertIn("Profile dir → /tmp/profile", text)
        self.assertIn("Startup URL → https://example.com", text)

    def test_cmd_tabs_outputs_json(self):
        client = FakeClient()
        args = SimpleNamespace(json=True)

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            cli.cmd_tabs(client, args)

        self.assertEqual(
            json.loads(stdout.getvalue()),
            [{"id": "tab-1", "title": "Example", "url": "https://example.com"}],
        )

    def test_cmd_navigate_reports_final_url(self):
        client = FakeClient()
        args = SimpleNamespace(tab="auto", url="https://example.com", json=False)

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            cli.cmd_navigate(client, args)

        self.assertIn("Navigated → https://example.com/final", stdout.getvalue())

    def test_cmd_cookies_get_passes_multiple_urls(self):
        client = FakeClient()
        args = SimpleNamespace(
            tab="auto",
            urls=["https://example.com", "https://api.example.com"],
            json=False,
        )

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            cli.cmd_cookies_get(client, args)

        self.assertEqual(
            client.cookies_args,
            ("tab-1", ["https://example.com", "https://api.example.com"]),
        )
        self.assertIn("(1 cookies)", stdout.getvalue())


class CliSmokeTests(unittest.TestCase):
    def test_module_help_smoke(self):
        result = subprocess.run(
            [sys.executable, "-m", "unchained_cli", "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Browser automation over local Chrome CDP.", result.stdout)
        self.assertIn("launch [url]", result.stdout)
        self.assertIn("cookies get [--urls URL ...]", result.stdout)


if __name__ == "__main__":
    unittest.main()
