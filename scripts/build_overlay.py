"""Build the NemeDraft overlay into a single-file executable.

Usage
-----
    python scripts/build_overlay.py          # build for the current platform
    python scripts/build_overlay.py --clean  # clean previous build first

Requires PyInstaller (``pip install pyinstaller``).

Output
------
- Windows:  ``dist/NemeDraft.exe``
- macOS:    ``dist/NemeDraft.app``  (+ ``dist/NemeDraft`` binary)
- Linux:    ``dist/NemeDraft``
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "nemedraft_overlay.spec"
DIST = ROOT / "dist"
BUILD = ROOT / "build"


def _ensure_pyinstaller() -> None:
    """Check that PyInstaller is importable, else exit with a message."""
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print(
            "ERROR: PyInstaller is not installed.\n"
            "  Install it with:  pip install pyinstaller",
            file=sys.stderr,
        )
        sys.exit(1)


def clean() -> None:
    """Remove previous build artefacts."""
    for d in (DIST, BUILD):
        if d.exists():
            shutil.rmtree(d)
            print(f"Removed {d}")


def build() -> None:
    """Run PyInstaller to produce the single-file binary."""
    _ensure_pyinstaller()

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(SPEC),
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
        "--noconfirm",
    ]

    print(f"Building for {platform.system()} ({platform.machine()})...")
    print(f"  spec:  {SPEC}")
    print(f"  dist:  {DIST}")
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        print("Build FAILED", file=sys.stderr)
        sys.exit(result.returncode)

    # Print the final artefact path
    if sys.platform == "win32":
        artefact = DIST / "NemeDraft.exe"
    elif sys.platform == "darwin":
        artefact = DIST / "NemeDraft.app"
    else:
        artefact = DIST / "NemeDraft"

    if artefact.exists():
        size_mb = artefact.stat().st_size / (1024 * 1024) if artefact.is_file() else 0
        size_info = f" ({size_mb:.1f} MB)" if size_mb else ""
        print(f"\nBuild OK -> {artefact}{size_info}")
    else:
        print(f"\nBuild OK -> {DIST}/")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build NemeDraft overlay executable")
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove dist/ and build/ before building",
    )
    parser.add_argument(
        "--clean-only",
        action="store_true",
        help="Only clean, do not build",
    )
    args = parser.parse_args()

    if args.clean or args.clean_only:
        clean()
    if not args.clean_only:
        build()


if __name__ == "__main__":
    main()
