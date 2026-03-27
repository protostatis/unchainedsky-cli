#!/usr/bin/env python3
"""Build standalone DDM and Intel binaries using PyInstaller.

Usage:
    python build_binaries.py          # Build for current platform
    python build_binaries.py --install # Build and install to ~/.unchained/bin/

Output:
    dist/ddm     — standalone DDM binary
    dist/intel   — standalone Intel binary
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
    """Build a single binary with PyInstaller."""
    print(f"\n=== Building {name} ===")
    entry_point = ROOT / "unchained_cli" / entry_module

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--name", name,
        "--distpath", str(DIST),
        "--workpath", str(ROOT / "build" / name),
        "--specpath", str(ROOT / "build"),
        # Hidden imports for the intel <-> ddm cross-import
        "--hidden-import", "unchained_cli.intel_engine",
        "--hidden-import", "unchained_cli.ddm_engine",
        "--clean",
        "--noconfirm",
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
    parser = argparse.ArgumentParser(description="Build DDM and Intel binaries")
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
