"""Intel dispatcher — uses compiled binary if available, falls back to Python engine.

Search order:
  1. $UNCHAINED_INTEL_BIN  (env override)
  2. ~/.unchained/bin/intel
  3. intel in PATH
  4. Built-in Python engine (intel_engine.py)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


_ENV_KEY = "UNCHAINED_INTEL_BIN"
_DEFAULT_PATH = Path.home() / ".unchained" / "bin" / "intel"


def _find_binary() -> str | None:
    env = os.environ.get(_ENV_KEY)
    if env:
        return env if os.path.isfile(env) and os.access(env, os.X_OK) else None
    if _DEFAULT_PATH.is_file() and os.access(_DEFAULT_PATH, os.X_OK):
        return str(_DEFAULT_PATH)
    return shutil.which("intel")


def run_intel(port: int, tab_id: str, flags: list[str]) -> int:
    """Run Intel via binary or fall back to Python engine."""
    binary = _find_binary()
    if binary:
        cmd = [binary, "--port", str(port), "--tab", tab_id] + flags
        proc = subprocess.run(cmd)
        return proc.returncode

    # Fall back to built-in Python engine
    import unchained_cli.intel_engine as _mod
    _mod._CDP_PORT = port
    args = ["--port", str(port), "--tab", tab_id] + flags
    try:
        import asyncio
        asyncio.run(_mod.run(args))
        return 0
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1
    except Exception as e:
        print(f"Intel error: {e}", file=sys.stderr)
        return 1
