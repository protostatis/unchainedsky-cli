#!/usr/bin/env python3
"""Build native DDM and Intel binaries using Nuitka.

Nuitka compiles Python → C → machine code. The resulting binary contains
compiled native code, not extractable bytecode like PyInstaller.

Usage:
    python build_binaries.py          # Build for current platform
    python build_binaries.py --install # Build and install to ~/.unchained/bin/

Prerequisites:
    uv pip install nuitka ordered-set

Output:
    dist/ddm     — native DDM binary
    dist/intel   — native Intel binary
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
DIST = ROOT / "dist"
BIN_DIR = Path.home() / ".unchained" / "bin"


def _run(cmd: list[str], **kwargs):
    print(f"  $ {' '.join(cmd)}")
    subprocess.check_call(cmd, **kwargs)


def build_binary(name: str, entry_module: str):
    """Build a single native binary with Nuitka."""
    print(f"\n=== Building {name} (Nuitka) ===")
    entry_point = ROOT / "unchained_cli" / entry_module

    cmd = [
        sys.executable, "-m", "nuitka",
        "--onefile",
        f"--output-filename={name}",
        f"--output-dir={DIST}",
        # Include cross-imports between ddm and intel engines
        "--include-module=unchained_cli.intel_engine",
        "--include-module=unchained_cli.ddm_engine",
        # Include websockets runtime dependency
        "--include-package=websockets",
        # Clean up build artifacts
        "--remove-output",
        "--assume-yes-for-downloads",
        str(entry_point),
    ]
    _run(cmd)

    binary = DIST / name
    if not binary.exists():
        print(f"ERROR: {binary} not found after build", file=sys.stderr)
        sys.exit(1)

    size_mb = binary.stat().st_size / (1024 * 1024)
    print(f"  -> {binary} ({size_mb:.1f} MB)")
    return binary


def install_binaries():
    """Copy built binaries to ~/.unchained/bin/."""
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("ddm", "intel"):
        src = DIST / name
        dst = BIN_DIR / name
        if src.exists():
            shutil.copy2(src, dst)
            os.chmod(dst, 0o755)
            print(f"  Installed: {dst}")
        else:
            print(f"  SKIP: {src} not found")


def main():
    parser = argparse.ArgumentParser(description="Build native DDM and Intel binaries")
    parser.add_argument("--install", action="store_true",
                        help="Install to ~/.unchained/bin/ after building")
    parser.add_argument("--ddm-only", action="store_true",
                        help="Build only DDM")
    parser.add_argument("--intel-only", action="store_true",
                        help="Build only Intel")
    args = parser.parse_args()

    arch = platform.machine()
    system = platform.system()
    print(f"Platform: {system} {arch}")
    print(f"Python: {sys.version}")

    if not args.intel_only:
        build_binary("ddm", "ddm_engine.py")
    if not args.ddm_only:
        build_binary("intel", "intel_engine.py")

    if args.install:
        print("\n=== Installing ===")
        install_binaries()

    print("\nDone.")


if __name__ == "__main__":
    main()
