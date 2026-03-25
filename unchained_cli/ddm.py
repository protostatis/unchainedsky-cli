"""DDM stub — delegates to the compiled ddm binary if installed.

The real DDM (DOM Density Map) implementation is distributed as a
compiled native binary to protect the proprietary algorithm. This
module locates and invokes that binary; source is not recoverable
from the distributed artifact.

Binary search order:
  1. $UNCHAINED_DDM_BIN  (env override)
  2. ~/.unchained/bin/ddm
  3. ddm in PATH
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


_ENV_KEY = "UNCHAINED_DDM_BIN"
_DEFAULT_PATH = Path.home() / ".unchained" / "bin" / "ddm"


def _find_binary() -> str | None:
    env = os.environ.get(_ENV_KEY)
    if env:
        return env if os.path.isfile(env) and os.access(env, os.X_OK) else None
    if _DEFAULT_PATH.is_file() and os.access(_DEFAULT_PATH, os.X_OK):
        return str(_DEFAULT_PATH)
    return shutil.which("ddm")


def run_ddm(port: int, tab_id: str, flags: list[str]) -> int:
    """Invoke the ddm binary and stream its output to stdout/stderr.

    Returns the process exit code.
    Raises SystemExit with a friendly message if the binary is not found.
    """
    binary = _find_binary()
    if not binary:
        print(
            "DDM binary not found.\n"
            "\n"
            "Install it with:\n"
            "  brew install unchainedsky/tap/unchainedsky-ddm\n"
            "\n"
            "Or set UNCHAINED_DDM_BIN=/path/to/ddm",
            file=sys.stderr,
        )
        return 1

    cmd = [binary, "--port", str(port), "--tab", tab_id] + flags
    proc = subprocess.run(cmd)
    return proc.returncode
