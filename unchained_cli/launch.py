"""Chrome launch helpers for hardened local CDP startup."""
from __future__ import annotations

import json
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
DEFAULT_DATA_DIR = Path.home() / ".unchained"
_CONNECT_TIMEOUT = 2.0


class LaunchError(RuntimeError):
    pass


def _sanitize_profile(name: str) -> str:
    sanitized = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in name.strip())
    if len(sanitized) > 32:
        raise LaunchError(
            f"Profile name {name!r} is too long after sanitization ({len(sanitized)} chars); "
            "use a name that is 32 characters or fewer."
        )
    return sanitized or "default"


def _find_chrome_binary() -> str | None:
    env = os.environ.get("UNCHAINED_CHROME_BIN")
    if env:
        if os.path.isfile(env) and os.access(env, os.X_OK):
            return env
        raise LaunchError(
            f"UNCHAINED_CHROME_BIN={env!r} is not an executable file."
        )

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
    profile_directory: str | None = None,
) -> list[str]:
    chrome_args = [
        f"--user-data-dir={profile_dir}",
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if profile_directory:
        chrome_args.append(f"--profile-directory={profile_directory}")
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
        return json.loads(resp.read())


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
    return [
        tab for tab in data
        if isinstance(tab, dict)
        and tab.get("type") == "page"
        and not (tab.get("url") or "").startswith(("chrome://", "devtools://"))
    ]


def _open_tab(host: str, port: int, url: str) -> dict[str, Any] | None:
    encoded = urllib.parse.quote(url, safe=":/")
    req = urllib.request.Request(
        f"http://{host}:{port}/json/new?{encoded}",
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req, timeout=_CONNECT_TIMEOUT) as resp:
            data = json.loads(resp.read())
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


_PROFILE_CACHE_DIRS = {
    "Cache", "Code Cache", "GPUCache", "ShaderCache",
    "Service Worker", "GrShaderCache", "DawnCache",
}

_LIGHT_PROFILE_FILES = (
    "Preferences",
    "Secure Preferences",
    "Cookies",
    "Cookies-journal",
    "Login Data",
    "Login Data-journal",
    "Web Data",
    "Web Data-journal",
    os.path.join("Network", "Cookies"),
    os.path.join("Network", "Cookies-journal"),
)

_LIGHT_PROFILE_DIRS = (
    "Local Storage",
    "Session Storage",
    "IndexedDB",
)


def _copy_chrome_profile(src_profile: Path, dest_user_data_dir: Path,
                          profile_dir_name: str, mode: str = "full") -> None:
    """Copy a Chrome profile to a sandboxed user-data-dir.

    Also copies Local State from the parent (Chrome needs it for profile
    selection).  Skips cache directories to save time and disk.
    """
    dest_profile = dest_user_data_dir / profile_dir_name

    # Clean previous copy
    if dest_profile.exists():
        shutil.rmtree(dest_profile)
    dest_user_data_dir.mkdir(parents=True, exist_ok=True)

    # Copy Local State from Chrome's parent dir (required for profile resolution)
    local_state = src_profile.parent / "Local State"
    dest_local_state = dest_user_data_dir / "Local State"
    if local_state.is_file() and not dest_local_state.exists():
        shutil.copy2(local_state, dest_local_state)

    if mode == "full":
        shutil.copytree(
            src_profile,
            dest_profile,
            ignore=lambda _d, contents: [c for c in contents if c in _PROFILE_CACHE_DIRS],
        )
    else:
        # Light mode — only cookies, logins, storage
        dest_profile.mkdir(parents=True, exist_ok=True)
        for rel in _LIGHT_PROFILE_FILES:
            src = src_profile / rel
            if not src.is_file():
                continue
            dst = dest_profile / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        for rel in _LIGHT_PROFILE_DIRS:
            src = src_profile / rel
            if not src.is_dir():
                continue
            shutil.copytree(
                src, dest_profile / rel,
                dirs_exist_ok=True,
                ignore=lambda _d, contents: [c for c in contents if c in _PROFILE_CACHE_DIRS],
            )


def _default_chrome_user_data_dir() -> Path | None:
    """Return the default Chrome user-data-dir for the current platform."""
    system = platform.system()
    if system == "Darwin":
        p = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    elif system == "Linux":
        p = Path.home() / ".config" / "google-chrome"
    elif system == "Windows":
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            p = Path(local) / "Google" / "Chrome" / "User Data"
        else:
            return None
    else:
        return None
    return p if p.is_dir() else None


def launch_chrome(
    *,
    port: int = 9222,
    profile: str = "default",
    headless: bool = False,
    stealth: bool = False,
    startup_url: str = "about:blank",
    timeout: float = 15.0,
    extra_args: list[str] | None = None,
    use_existing_profile: bool = False,
) -> dict[str, Any]:
    """Ensure a Chrome instance with CDP is available on the requested port.

    When *use_existing_profile* is True (``--use-profile`` CLI flag), Chrome
    is launched with the real user-data-dir and ``--profile-directory=<profile>``
    so the user's cookies, logins, and extensions are available.  The *profile*
    argument should match a Chrome profile directory name such as ``Default``,
    ``Profile 1``, ``Profile 8``, etc.
    """
    _BLOCKED_EXTRA_ARG_PREFIXES = ("--user-data-dir", "--remote-debugging-port")
    if extra_args:
        for arg in extra_args:
            for blocked in _BLOCKED_EXTRA_ARG_PREFIXES:
                if arg.startswith(blocked):
                    raise LaunchError(
                        f"extra_args may not override {blocked}; "
                        "use the port= or profile= parameters instead."
                    )

    host = DEFAULT_HOST
    startup_url = startup_url or "about:blank"

    if use_existing_profile:
        chrome_udd = Path(os.environ.get("UNCHAINED_CHROME_UDD",
                                          _default_chrome_user_data_dir() or ""))
        if not chrome_udd.is_dir():
            raise LaunchError(
                "Cannot find Chrome user data directory. "
                "Set UNCHAINED_CHROME_UDD=/path/to/Chrome/User\\ Data"
            )
        src_profile = chrome_udd / profile
        if not src_profile.is_dir():
            available = [d.name for d in chrome_udd.iterdir()
                         if d.is_dir() and (d.name == "Default" or d.name.startswith("Profile"))]
            raise LaunchError(
                f"Chrome profile {profile!r} not found at {src_profile}.\n"
                f"Available profiles: {', '.join(sorted(available))}"
            )
        # Copy profile to sandboxed temp dir (same pattern as chrome_bridge.py)
        data_dir = Path(os.environ.get("UNCHAINED_DATA_DIR", DEFAULT_DATA_DIR))
        profile_dir = data_dir / f"prov_{profile.replace(' ', '_')}_{port}"
        profile_name = profile  # keep original name for --profile-directory
        _copy_chrome_profile(src_profile, profile_dir, profile, mode="full")
    else:
        profile_name = _sanitize_profile(profile)
        data_dir = Path(os.environ.get("UNCHAINED_DATA_DIR", DEFAULT_DATA_DIR))
        profile_dir = data_dir / f"chrome_{profile_name}"

    data_dir.mkdir(parents=True, exist_ok=True)

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
            "data_dir": str(data_dir),
            "startup_url": startup_url,
            "opened_tab": opened_tab,
            "headless": headless,
        }

    chrome_bin = _find_chrome_binary()
    if not chrome_bin:
        raise LaunchError(
            "No Chrome/Chromium binary found. Set UNCHAINED_CHROME_BIN or install Chrome."
        )

    if not use_existing_profile:
        profile_dir.mkdir(parents=True, exist_ok=True)
    cmd = _build_launch_command(
        chrome_bin,
        profile_dir=profile_dir,
        port=port,
        startup_url=startup_url,
        headless=headless,
        extra_args=extra_args,
        profile_directory=profile if use_existing_profile else None,
    )
    uses_launcher_wrapper = bool(cmd) and cmd[0] == "open"

    if use_existing_profile:
        log_dir = Path(os.environ.get("UNCHAINED_DATA_DIR", DEFAULT_DATA_DIR))
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "chrome.log"
    else:
        log_path = profile_dir / "chrome.log"
    log_fh = open(log_path, "wb")
    try:
        proc = subprocess.Popen(cmd, stdout=log_fh, stderr=log_fh)
    except OSError as exc:
        log_fh.close()
        raise LaunchError(f"Failed to launch Chrome: {exc}") from exc
    log_fh.close()

    sleep_interval = 0.1
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
                "data_dir": str(data_dir),
                "chrome_log": str(log_path),
                "startup_url": startup_url,
                "opened_tab": opened_tab,
                "headless": headless,
                "chrome_bin": chrome_bin,
            }
            if not uses_launcher_wrapper:
                result["pid"] = proc.pid
            # Stealth: inject fingerprint overrides (auto-enabled with headless)
            if stealth or headless:
                try:
                    from .stealth import inject_stealth
                    inject_stealth(host, port)
                    result["stealth"] = True
                except Exception:
                    result["stealth"] = False
            return result
        time.sleep(sleep_interval)
        sleep_interval = min(sleep_interval * 2, 0.5)

    raise LaunchError(
        f"Chrome did not expose CDP on {host}:{port} within {timeout:.1f}s. "
        f"Try a different --profile if {profile_dir} is already in use."
    )
