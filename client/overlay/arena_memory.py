"""Read MTG Arena account identity through the memory-backed tracker daemon."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

DEFAULT_DAEMON_PORT = 6842
DEFAULT_DAEMON_URL = f"http://localhost:{DEFAULT_DAEMON_PORT}"
STARTUP_DEADLINE_SECONDS = 3.0
STARTUP_POLL_SECONDS = 0.2


@dataclass(frozen=True)
class ArenaPlayerIdentity:
    """Arena account identity read from MTG Arena process memory.

    Args:
        player_id: Stable Wizards account ID returned by the daemon.
        display_name: Arena display name, when available.
        persona_id: Arena persona ID, when available.
        elapsed_ms: Time reported by the daemon for the memory read.
    """

    player_id: str
    display_name: str = ""
    persona_id: str = ""
    elapsed_ms: int = 0


def get_arena_player_identity(
    *,
    base_url: str | None = None,
    timeout: float = 1.5,
    autostart: bool = True,
) -> ArenaPlayerIdentity | None:
    """Return the current Arena account identity from process memory.

    This talks to mtga-tracker-daemon's ``/playerId`` endpoint. That daemon
    reads MTG Arena's Mono memory graph, avoiding the unreliable Player.log
    ``authenticateResponse`` path.

    Args:
        base_url: Optional daemon base URL. Defaults to
            ``MTGA_TRACKER_DAEMON_URL`` or ``http://localhost:6842``.
        timeout: Per-request HTTP timeout in seconds.
        autostart: Whether to start a configured daemon executable if the
            first request fails.

    Returns:
        The Arena identity, or ``None`` when Arena or the daemon is unavailable.
    """
    daemon_url = _normalise_daemon_url(base_url)
    identity = _fetch_player_identity(daemon_url, timeout=timeout)
    if identity is not None:
        return identity

    if not autostart or not _autostart_enabled():
        return None

    if _daemon_is_reachable(daemon_url, timeout=timeout):
        return None

    if not _start_daemon(daemon_url):
        return None

    return _wait_for_player_identity(daemon_url, timeout=timeout)


def get_arena_player_id(
    *,
    base_url: str | None = None,
    timeout: float = 1.5,
    autostart: bool = True,
) -> str | None:
    """Return only the memory-backed Arena player ID.

    Args:
        base_url: Optional daemon base URL.
        timeout: Per-request HTTP timeout in seconds.
        autostart: Whether to start a configured daemon executable if needed.

    Returns:
        The player ID string, or ``None`` when it cannot be read.
    """
    identity = get_arena_player_identity(
        base_url=base_url,
        timeout=timeout,
        autostart=autostart,
    )
    return identity.player_id if identity else None


def _fetch_player_identity(base_url: str, *, timeout: float) -> ArenaPlayerIdentity | None:
    """Fetch and parse ``/playerId`` from a running daemon.

    Args:
        base_url: Normalized daemon base URL.
        timeout: HTTP timeout in seconds.

    Returns:
        Parsed identity, or ``None`` if the endpoint is unavailable or invalid.
    """
    try:
        response = httpx.get(f"{base_url}/playerId", timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        logger.debug("Arena memory identity request failed", exc_info=True)
        return None

    if not isinstance(payload, dict):
        logger.debug("Arena memory identity response was not an object: %r", payload)
        return None

    error = _clean_string(payload.get("error"))
    if error:
        logger.debug("Arena memory identity unavailable: %s", error)
        return None

    player_id = _clean_string(
        payload.get("playerId")
        or payload.get("PlayerID")
        or payload.get("AccountID")
    )
    if not player_id:
        logger.debug("Arena memory identity response had no playerId: %r", payload)
        return None

    return ArenaPlayerIdentity(
        player_id=player_id,
        display_name=_clean_string(payload.get("displayName") or payload.get("DisplayName")),
        persona_id=_clean_string(payload.get("personaId") or payload.get("PersonaID")),
        elapsed_ms=_clean_int(payload.get("elapsedTime")),
    )


def _wait_for_player_identity(base_url: str, *, timeout: float) -> ArenaPlayerIdentity | None:
    """Poll briefly for the daemon to start and read Arena memory.

    Args:
        base_url: Normalized daemon base URL.
        timeout: HTTP timeout in seconds.

    Returns:
        Parsed identity, or ``None`` after the startup deadline expires.
    """
    deadline = time.monotonic() + STARTUP_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        identity = _fetch_player_identity(base_url, timeout=timeout)
        if identity is not None:
            return identity
        time.sleep(STARTUP_POLL_SECONDS)
    return None


def _daemon_is_reachable(base_url: str, *, timeout: float) -> bool:
    """Return whether the daemon is already accepting HTTP requests.

    Args:
        base_url: Normalized daemon base URL.
        timeout: HTTP timeout in seconds.

    Returns:
        ``True`` when the daemon status endpoint responds successfully.
    """
    try:
        response = httpx.get(f"{base_url}/status", timeout=timeout)
        response.raise_for_status()
    except httpx.HTTPError:
        return False
    return True


def _start_daemon(base_url: str) -> bool:
    """Start mtga-tracker-daemon when an executable is configured.

    Args:
        base_url: Normalized daemon base URL, used to derive the port.

    Returns:
        ``True`` if a daemon process was started, otherwise ``False``.
    """
    command = _daemon_command(base_url)
    if command is None:
        return False

    popen_kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    try:
        subprocess.Popen(command, **popen_kwargs)
    except OSError:
        logger.debug("Failed to start mtga-tracker-daemon: %s", command, exc_info=True)
        return False

    logger.info("Started mtga-tracker-daemon for Arena memory identity")
    return True


def _daemon_command(base_url: str) -> list[str] | None:
    """Build the daemon launch command from environment or PATH.

    Args:
        base_url: Normalized daemon base URL, used to derive the port.

    Returns:
        Command arguments suitable for :class:`subprocess.Popen`, or ``None``.
    """
    port = str(_daemon_port(base_url))

    executable = _clean_path(os.getenv("MTGA_TRACKER_DAEMON_EXE"))
    if executable:
        return [executable, "-p", port]

    project = _clean_path(os.getenv("MTGA_TRACKER_DAEMON_PROJECT"))
    if project:
        return ["dotnet", "run", "--project", project, "--", "-p", port]

    bundled = _find_bundled_daemon()
    if bundled is not None:
        return [str(bundled), "-p", port]

    path_name = "mtga-tracker-daemon.exe" if sys.platform == "win32" else "mtga-tracker-daemon"
    on_path = shutil.which(path_name)
    if on_path:
        return [on_path, "-p", port]

    source_project = _find_source_daemon_project()
    if source_project is not None:
        return ["dotnet", "run", "--project", str(source_project), "--", "-p", port]

    return None


def _find_bundled_daemon() -> Path | None:
    """Find a daemon executable next to the overlay, if one is bundled.

    Returns:
        Path to the executable, or ``None``.
    """
    executable_names = (
        ["mtga-tracker-daemon.exe", "MTGATrackerDaemon.exe"]
        if sys.platform == "win32"
        else ["mtga-tracker-daemon"]
    )
    search_roots = [Path(sys.executable).resolve().parent]
    if not getattr(sys, "frozen", False):
        search_roots.append(Path(__file__).resolve().parents[2])

    for root in search_roots:
        for name in executable_names:
            candidate = root / name
            if candidate.is_file():
                return candidate
    return None


def _find_source_daemon_project() -> Path | None:
    """Find a sibling mtga-tracker-daemon source checkout.

    Returns:
        Path to the daemon ``.csproj`` file, or ``None``.
    """
    relative_project = (
        Path("mtga-tracker-daemon")
        / "src"
        / "mtga-tracker-daemon"
        / "mtga-tracker-daemon.csproj"
    )
    for parent in Path(__file__).resolve().parents:
        candidate = parent / relative_project
        if candidate.is_file():
            return candidate
    return None


def _normalise_daemon_url(base_url: str | None) -> str:
    """Normalize daemon URLs so endpoint paths append predictably.

    Args:
        base_url: Optional explicit daemon URL.

    Returns:
        URL without a trailing slash.
    """
    value = (base_url or os.getenv("MTGA_TRACKER_DAEMON_URL") or DEFAULT_DAEMON_URL).strip()
    return value.rstrip("/") or DEFAULT_DAEMON_URL


def _daemon_port(base_url: str) -> int:
    """Extract the daemon port from a normalized URL.

    Args:
        base_url: Normalized daemon base URL.

    Returns:
        The configured port, defaulting to ``6842``.
    """
    parsed = urlparse(base_url)
    return parsed.port or DEFAULT_DAEMON_PORT


def _autostart_enabled() -> bool:
    """Return whether daemon autostart is enabled by environment.

    Returns:
        ``False`` only for explicit false-like environment values.
    """
    value = os.getenv("MTGA_TRACKER_DAEMON_AUTOSTART", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _clean_path(value: str | None) -> str:
    """Clean a path-like environment variable.

    Args:
        value: Raw environment variable value.

    Returns:
        Stripped path string, or an empty string.
    """
    return (value or "").strip().strip('"')


def _clean_string(value: object) -> str:
    """Convert a JSON value into a stripped string.

    Args:
        value: JSON scalar value.

    Returns:
        A stripped string, or an empty string for missing values.
    """
    return "" if value is None else str(value).strip()


def _clean_int(value: object) -> int:
    """Convert a JSON value into an integer.

    Args:
        value: JSON scalar value.

    Returns:
        Parsed integer, or ``0`` when parsing fails.
    """
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0
