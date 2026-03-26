"""Chrome launch helpers for hardened local CDP startup."""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_HOST = "127.0.0.1"
DEFAULT_DATA_DIR = Path(
    os.environ.get("UNCHAINED_DATA_DIR", Path.home() / ".unchained")
)
_CONNECT_TIMEOUT = 2.0


class LaunchError(RuntimeError):
    pass


def _sanitize_profile(name: str) -> str:
    sanitized = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in name.strip())
    return sanitized[:32] or "default"


def _find_chrome_binary() -> str | None:
    env = os.environ.get("UNCHAINED_CHROME_BIN")
    if env:
        return env if os.path.isfile(env) and os.access(env, os.X_OK) else None

    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]
    if platform.system() == "Windows":
        for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env_name, "").strip()
            if not base:
                continue
            candidates.extend([
                os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(base, "Chromium", "Application", "chrome.exe"),
                os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
            ])

    for path in candidates:
        if os.path.exists(path):
            return path

    for cmd in (
        "google-chrome",
        "google-chrome-stable",
        "chromium-browser",
        "chromium",
        "chrome",
        "msedge",
    ):
        found = shutil.which(cmd)
        if found:
            return found
    return None


def _build_launch_command(
    chrome_bin: str,
    *,
    profile_dir: Path,
    port: int,
    startup_url: str,
    headless: bool,
    extra_args: list[str] | None,
) -> list[str]:
    chrome_args = [
        f"--user-data-dir={profile_dir}",
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if headless:
        chrome_args.extend([
            "--headless=new",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--mute-audio",
            "--hide-scrollbars",
            "--window-size=1920,1080",
        ])
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            chrome_args.append("--no-sandbox")
    if extra_args:
        chrome_args.extend(extra_args)
    chrome_args.append(startup_url)

    if (
        platform.system() == "Darwin"
        and ".app/Contents/MacOS/" in chrome_bin
    ):
        app_bundle = chrome_bin.split("/Contents/MacOS/", 1)[0]
        return ["open", "-na", app_bundle, "--args", *chrome_args]

    return [chrome_bin, *chrome_args]


def _json_get(host: str, port: int, path: str) -> Any:
    with urllib.request.urlopen(
        f"http://{host}:{port}{path}",
        timeout=_CONNECT_TIMEOUT,
    ) as resp:
        return json_loads(resp.read())


def json_loads(raw: bytes) -> Any:
    import json

    return json.loads(raw)


def _version_json(host: str, port: int) -> dict[str, Any] | None:
    try:
        data = _json_get(host, port, "/json/version")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _page_tabs(host: str, port: int) -> list[dict[str, Any]]:
    try:
        data = _json_get(host, port, "/json")
    except (urllib.error.URLError, OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [tab for tab in data if isinstance(tab, dict) and tab.get("type") == "page"]


def _open_tab(host: str, port: int, url: str) -> dict[str, Any] | None:
    encoded = urllib.parse.quote(url, safe=":/")
    req = urllib.request.Request(
        f"http://{host}:{port}/json/new?{encoded}",
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req, timeout=_CONNECT_TIMEOUT) as resp:
            data = json_loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise LaunchError(f"Failed to open a tab via Chrome CDP on {host}:{port}: {exc}") from exc
    if not isinstance(data, dict):
        raise LaunchError(f"Chrome CDP returned an invalid /json/new response on {host}:{port}")
    return data


def _ensure_page_tab(host: str, port: int, startup_url: str) -> bool:
    if _page_tabs(host, port):
        return False
    _open_tab(host, port, startup_url or "about:blank")
    return True


def launch_chrome(
    *,
    port: int = 9222,
    profile: str = "default",
    headless: bool = False,
    startup_url: str = "about:blank",
    timeout: float = 15.0,
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """Ensure a Chrome instance with CDP is available on the requested port."""
    host = DEFAULT_HOST
    startup_url = startup_url or "about:blank"
    profile_name = _sanitize_profile(profile)
    profile_dir = DEFAULT_DATA_DIR / f"chrome_{profile_name}"
    DEFAULT_DATA_DIR.mkdir(parents=True, exist_ok=True)

    if _version_json(host, port):
        opened_tab = False
        if startup_url != "about:blank":
            _open_tab(host, port, startup_url)
            opened_tab = True
        else:
            opened_tab = _ensure_page_tab(host, port, startup_url)
        return {
            "already_running": True,
            "host": host,
            "port": port,
            "profile": None,
            "profile_dir": None,
            "requested_profile": profile_name,
            "requested_profile_dir": str(profile_dir),
            "startup_url": startup_url,
            "opened_tab": opened_tab,
            "headless": headless,
        }

    chrome_bin = _find_chrome_binary()
    if not chrome_bin:
        raise LaunchError(
            "No Chrome/Chromium binary found. Set UNCHAINED_CHROME_BIN or install Chrome."
        )

    profile_dir.mkdir(parents=True, exist_ok=True)
    cmd = _build_launch_command(
        chrome_bin,
        profile_dir=profile_dir,
        port=port,
        startup_url=startup_url,
        headless=headless,
        extra_args=extra_args,
    )
    uses_launcher_wrapper = bool(cmd) and cmd[0] == "open"

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise LaunchError(f"Failed to launch Chrome: {exc}") from exc

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        exit_code = proc.poll()
        if exit_code is not None:
            if uses_launcher_wrapper:
                if exit_code != 0:
                    raise LaunchError(
                        "Chrome launcher exited before CDP became ready "
                        f"(exit code {exit_code}). Check the Chrome app path and launch flags."
                    )
            else:
                raise LaunchError(
                    "Chrome exited before CDP became ready "
                    f"(exit code {exit_code}). Try a different --profile if "
                    f"{profile_dir} is already in use."
                )
        if _version_json(host, port):
            opened_tab = _ensure_page_tab(host, port, startup_url)
            result = {
                "already_running": False,
                "host": host,
                "port": port,
                "profile": profile_name,
                "profile_dir": str(profile_dir),
                "startup_url": startup_url,
                "opened_tab": opened_tab,
                "headless": headless,
                "chrome_bin": chrome_bin,
            }
            if not uses_launcher_wrapper:
                result["pid"] = proc.pid
            return result
        time.sleep(0.5)

    raise LaunchError(
        f"Chrome did not expose CDP on {host}:{port} within {timeout:.1f}s. "
        f"Try a different --profile if {profile_dir} is already in use."
    )
