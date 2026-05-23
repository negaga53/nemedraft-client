"""Auto-updater — checks GitHub releases and silently applies updates."""

from __future__ import annotations

import logging
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx

from client.overlay import __version__

logger = logging.getLogger("overlay.updater")

GITHUB_API_URL = (
    "https://api.github.com/repos/negaga53/nemedraft-client/releases/latest"
)

# Map (system, machine) → expected release asset name.
# macOS ships a zipped .app bundle (a directory, not a single binary),
# so the asset has a ``-app.zip`` suffix — matching the build-client
# workflow's ``${{ matrix.asset_name }}-app`` upload step. The release-
# prep script then writes the file out as ``…-app.zip``.
_ASSET_MAP: dict[tuple[str, str], str] = {
    ("Windows", "AMD64"): "NemeDraft-Windows-x64.exe",
    ("Windows", "x86_64"): "NemeDraft-Windows-x64.exe",
    ("Darwin", "arm64"): "NemeDraft-macOS-arm64-app.zip",
    ("Darwin", "x86_64"): "NemeDraft-macOS-x64-app.zip",
    ("Linux", "x86_64"): "NemeDraft-Linux-x64",
    ("Linux", "AMD64"): "NemeDraft-Linux-x64",
}


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse a semver-like string into a comparable tuple of ints."""
    parts: list[int] = []
    for segment in v.split("."):
        try:
            parts.append(int(segment))
        except ValueError:
            break
    return tuple(parts)


def _get_asset_name() -> str | None:
    """Return the expected release asset name for this platform."""
    key = (platform.system(), platform.machine())
    return _ASSET_MAP.get(key)


def check_for_update() -> tuple[str, str] | None:
    """Check GitHub releases for a newer version.

    Returns:
        ``(latest_version, download_url)`` when an update is available,
        ``None`` otherwise.  Also returns ``None`` when running from
        source (not a frozen PyInstaller build).
    """
    if not getattr(sys, "frozen", False):
        logger.debug("Not a frozen build — skipping update check")
        return None

    asset_name = _get_asset_name()
    if not asset_name:
        logger.warning(
            "Unknown platform %s/%s — cannot check for updates",
            platform.system(),
            platform.machine(),
        )
        return None

    try:
        with httpx.Client(timeout=10, follow_redirects=True) as client:
            resp = client.get(
                GITHUB_API_URL,
                headers={"Accept": "application/vnd.github.v3+json"},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        logger.warning("Failed to check for updates", exc_info=True)
        return None

    tag = data.get("tag_name", "")
    latest_version = tag.lstrip("v")

    current = _parse_version(__version__)
    latest = _parse_version(latest_version)
    if not latest or not current:
        logger.warning(
            "Cannot compare versions: current=%s, latest=%s",
            __version__,
            latest_version,
        )
        return None

    if latest <= current:
        logger.info(
            "Up to date (current=%s, latest=%s)", __version__, latest_version
        )
        return None

    # Find the download URL for our platform.
    for asset in data.get("assets", []):
        if asset["name"] == asset_name:
            logger.info(
                "Update available: %s → %s", __version__, latest_version
            )
            return (latest_version, asset["browser_download_url"])

    logger.warning("No asset '%s' found in release %s", asset_name, tag)
    return None


def download_update(
    download_url: str,
    progress_callback: None | (callable) = None,
) -> Path:
    """Download the update binary to a temporary file.

    Args:
        download_url: Browser-download URL from the GitHub release asset.
        progress_callback: Optional ``fn(bytes_downloaded, total_bytes)``.

    Returns:
        Path to the downloaded temporary file.

    Raises:
        httpx.HTTPStatusError: On HTTP errors.
        OSError: On filesystem errors.
    """
    system = platform.system()
    if system == "Windows":
        suffix = ".exe"
    elif system == "Darwin":
        # macOS ships a zipped .app bundle — keep the .zip extension so
        # ``unzip`` recognises the archive later.
        suffix = ".zip"
    else:
        suffix = ""
    fd, tmp_path = tempfile.mkstemp(prefix="nemedraft_update_", suffix=suffix)
    os.close(fd)
    tmp = Path(tmp_path)

    try:
        with httpx.Client(timeout=300, follow_redirects=True) as client:
            with client.stream("GET", download_url) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                downloaded = 0
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=65_536):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback and total:
                            progress_callback(downloaded, total)

        # Mark executable on Linux (single-file binary). macOS downloads
        # a .zip archive — chmod +x on a zip is meaningless and the
        # actual executable inside the bundle keeps its original mode.
        if system == "Linux":
            tmp.chmod(
                tmp.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
            )

        logger.info("Downloaded update to %s (%d bytes)", tmp, downloaded)
        return tmp

    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def apply_update_and_restart(new_binary: Path) -> None:
    """Replace the current executable with *new_binary* and restart.

    On **Windows** a helper batch script is spawned that waits for this
    process to release the file lock, then swaps the binary and launches
    the updated version.

    On **macOS** the downloaded ``.zip`` is unpacked, then a helper
    shell script waits for this process to exit and swaps the entire
    ``.app`` bundle directory before relaunching via ``open``.

    On **Linux** the binary is replaced in-place and the process is
    restarted via ``os.execv``.
    """
    current_exe = Path(sys.executable).resolve()
    logger.info("Applying update: %s → %s", new_binary, current_exe)

    system = platform.system()
    if system == "Windows":
        _apply_windows(current_exe, new_binary)
    elif system == "Darwin":
        _apply_macos(current_exe, new_binary)
    else:
        _apply_unix(current_exe, new_binary)


# -- platform helpers -------------------------------------------------------


def _apply_windows(current_exe: Path, new_binary: Path) -> None:
    """Spawn a batch helper that replaces the exe after this process exits."""
    script_fd, script_path = tempfile.mkstemp(
        prefix="nemedraft_update_", suffix=".cmd"
    )
    # Build the batch script.  It loops until it can overwrite the old exe
    # (which stays locked while this process is alive), then launches the
    # updated binary via ``explorer.exe``.
    #
    # Why explorer.exe instead of ``start``?  When cmd.exe runs under
    # CREATE_NO_WINDOW, ``start`` inherits a broken console/DLL-search
    # environment.  The PyInstaller bootloader extracts python311.dll and
    # its dependencies correctly, but ``LoadLibrary`` fails because the
    # upstream process tree never had proper Shell initialisation.
    # ``explorer.exe "file.exe"`` delegates to the running Windows Shell,
    # which creates a fully initialised process — identical to a
    # double-click.
    script_content = (
        "@echo off\n"
        ":retry\n"
        "ping -n 2 127.0.0.1 >nul 2>&1\n"
        f'copy /b /y "{new_binary}" "{current_exe}" >nul 2>&1\n'
        "if errorlevel 1 goto retry\n"
        f'del /f "{new_binary}" >nul 2>&1\n'
        "ping -n 2 127.0.0.1 >nul 2>&1\n"
        f'explorer.exe "{current_exe}"\n'
        'del "%~f0"\n'
    )
    with os.fdopen(script_fd, "w") as f:
        f.write(script_content)

    # Launch the script in a hidden console (CREATE_NO_WINDOW gives cmd.exe
    # a proper console environment so built-in commands like ``start`` work
    # correctly, unlike DETACHED_PROCESS which strips the console entirely
    # and breaks DLL search path resolution for the launched exe).
    subprocess.Popen(  # noqa: S603
        ["cmd.exe", "/c", script_path],
        creationflags=(
            subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        ),
        close_fds=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    sys.exit(0)


def _apply_unix(current_exe: Path, new_binary: Path) -> None:
    """Replace the binary in-place and restart via ``os.execv``."""
    backup = current_exe.with_suffix(".bak")
    try:
        shutil.move(str(current_exe), str(backup))
        shutil.move(str(new_binary), str(current_exe))
        current_exe.chmod(
            current_exe.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
        )
        backup.unlink(missing_ok=True)
    except Exception:
        # Attempt to restore the backup on failure.
        if backup.exists() and not current_exe.exists():
            shutil.move(str(backup), str(current_exe))
        raise

    # Replace the current process image with the updated binary.
    os.execv(str(current_exe), [str(current_exe)] + sys.argv[1:])


def _apply_macos(current_exe: Path, new_archive: Path) -> None:
    """Replace the running ``.app`` bundle on macOS and relaunch.

    The frozen overlay ships as a ``.app`` directory bundle (typically
    ``/Applications/NemeDraft.app``) with the actual binary buried at
    ``Contents/MacOS/NemeDraft``. macOS file-locks the bundle while the
    binary is executing, so an in-process swap is unsafe — spawn a
    detached shell helper that waits for our PID to exit, extracts the
    new bundle, swaps it in, and relaunches via ``open``.
    """
    # Walk up from the inner Mach-O binary to find the .app directory.
    app_bundle: Path | None = None
    for parent in [current_exe, *current_exe.parents]:
        if parent.suffix == ".app":
            app_bundle = parent
            break
    if app_bundle is None:
        raise RuntimeError(
            f"Could not locate .app bundle starting from {current_exe}; "
            "macOS update requires the overlay to run from a .app build.",
        )

    # Extract the zipped bundle into a sibling tempdir so the helper
    # has a fully-built replacement to move into place.
    extract_dir = Path(tempfile.mkdtemp(prefix="nemedraft_update_extract_"))
    try:
        subprocess.run(
            ["/usr/bin/unzip", "-q", str(new_archive), "-d", str(extract_dir)],
            check=True,
        )
    except Exception:
        shutil.rmtree(extract_dir, ignore_errors=True)
        raise

    extracted_apps = list(extract_dir.glob("*.app"))
    if not extracted_apps:
        shutil.rmtree(extract_dir, ignore_errors=True)
        raise RuntimeError(
            f"No .app bundle found inside {new_archive}; nothing to install.",
        )
    new_app = extracted_apps[0]

    # Strip macOS quarantine xattr so Gatekeeper doesn't block the
    # relaunch (otherwise the user sees an unsigned-app warning every
    # update). The signed-app case will fail this check harmlessly.
    subprocess.run(
        ["/usr/bin/xattr", "-dr", "com.apple.quarantine", str(new_app)],
        check=False,
    )

    # Build a helper that:
    #   1. Polls until our PID exits (releases the bundle's file locks).
    #   2. Removes the old .app and moves the new one into place.
    #   3. Cleans up temp files.
    #   4. Relaunches via ``open`` (delegates to LaunchServices, which
    #      starts the app in its own session — no inherited fds).
    #   5. Self-deletes.
    our_pid = os.getpid()
    script_fd, script_path = tempfile.mkstemp(
        prefix="nemedraft_update_", suffix=".sh",
    )
    script = (
        "#!/bin/bash\n"
        "set -e\n"
        f"while kill -0 {our_pid} 2>/dev/null; do sleep 0.3; done\n"
        "sleep 1\n"
        f'rm -rf "{app_bundle}"\n'
        f'mv "{new_app}" "{app_bundle}"\n'
        f'rm -rf "{extract_dir}"\n'
        f'rm -f "{new_archive}"\n'
        f'open "{app_bundle}"\n'
        f'rm -f "{script_path}"\n'
    )
    with os.fdopen(script_fd, "w") as f:
        f.write(script)
    os.chmod(script_path, 0o755)

    subprocess.Popen(  # noqa: S603 — script path is generated above
        ["/bin/bash", script_path],
        close_fds=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    sys.exit(0)
