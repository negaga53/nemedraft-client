"""Read MTG Arena account identity and event lobby state from process memory.

This module is a thin facade over :mod:`client.overlay.memory`, which uses
``pymem`` (``OpenProcess`` + ``ReadProcessMemory``) to walk MTG Arena's Mono
runtime. It replaces the previous HTTP client that delegated to a separate
``mtga-tracker-daemon`` subprocess; the public dataclasses and entry-point
signatures are preserved so callers in :mod:`client.overlay.main` do not need
to change.

Memory reads only work on Windows (where Arena ships). On macOS / Linux every
public function returns ``None`` and the overlay falls back to log parsing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from client.overlay.memory.platform import is_memory_supported as _is_memory_supported
from client.overlay.memory.session import MemorySession
from client.overlay.memory.walker import read_current_event, read_player_identity

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArenaPlayerIdentity:
    """Arena account identity read from MTG Arena process memory.

    Args:
        player_id: Arena persona ID — the canonical NemeDraft player key.
        display_name: Arena display name, when available.
        elapsed_ms: Time taken by the memory read, in milliseconds.
    """

    player_id: str
    display_name: str = ""
    elapsed_ms: int = 0


@dataclass(frozen=True)
class ArenaCurrentEvent:
    """Current Arena event landing state read from process memory.

    Args:
        is_in_event_lobby: Whether Arena's current nav content is the event page.
        content_type: Managed type name for the current nav content.
        internal_event_name: Arena internal event name, when on an event page.
        event_state: Arena event state enum value.
        format_type: Arena format type enum value.
        draft_id: Current draft ID, if one has been assigned.
        current_event_state: Course-data event state enum value.
        current_module: Course module enum value.
        elapsed_ms: Time taken by the memory read, in milliseconds.
    """

    is_in_event_lobby: bool
    content_type: str = ""
    internal_event_name: str = ""
    event_state: int = 0
    format_type: int = 0
    draft_id: str = ""
    current_event_state: int = 0
    current_module: int = 0
    elapsed_ms: int = 0

    @property
    def is_draft_lobby(self) -> bool:
        """Return whether the current event is a draft or sealed lobby.

        Returns:
            ``True`` when the current memory state is a draft or sealed lobby.
        """
        lowered = self.internal_event_name.lower()
        return self.is_in_event_lobby and any(
            keyword in lowered for keyword in ("draft", "sealed")
        )


def is_memory_supported() -> bool:
    """Return whether memory reads are usable in this environment."""
    return _is_memory_supported()


def get_arena_player_identity(
    *,
    timeout: float = 1.5,  # noqa: ARG001 — kept for API compatibility
    autostart: bool = True,
) -> ArenaPlayerIdentity | None:
    """Return the current Arena account identity from process memory.

    Args:
        timeout: Retained for API compatibility; ignored.
        autostart: Whether to attach to MTGA if not already attached.

    Returns:
        The Arena identity, or ``None`` when Arena memory is unavailable.
    """
    session = MemorySession.instance()
    if autostart and not session.ensure_attached():
        return None
    if not session.is_attached():
        return None
    payload = read_player_identity(session)
    if not payload:
        return None
    return ArenaPlayerIdentity(
        player_id=_clean_string(payload.get("playerId")),
        display_name=_clean_string(payload.get("displayName")),
        elapsed_ms=_clean_int(payload.get("elapsedTime")),
    )


def get_arena_player_id(
    *,
    timeout: float = 1.5,
    autostart: bool = True,
) -> str | None:
    """Return only the memory-backed Arena player ID.

    Args:
        timeout: Retained for API compatibility; ignored.
        autostart: Whether to attach to MTGA if not already attached.

    Returns:
        The player ID string, or ``None`` when it cannot be read.
    """
    identity = get_arena_player_identity(timeout=timeout, autostart=autostart)
    return identity.player_id if identity else None


def get_arena_current_event(
    *,
    timeout: float = 1.5,  # noqa: ARG001 — kept for API compatibility
    autostart: bool = True,
) -> ArenaCurrentEvent | None:
    """Return the current Arena event landing state from process memory.

    Args:
        timeout: Retained for API compatibility; ignored.
        autostart: Whether to attach to MTGA if not already attached.

    Returns:
        Current event state, or ``None`` when unavailable or unsupported.
    """
    session = MemorySession.instance()
    if autostart and not session.ensure_attached():
        return None
    if not session.is_attached():
        return None
    payload = read_current_event(session)
    if payload is None:
        return None
    return ArenaCurrentEvent(
        is_in_event_lobby=bool(payload.get("isInEventLobby", False)),
        content_type=_clean_string(payload.get("contentType")),
        internal_event_name=_clean_string(payload.get("internalEventName")),
        event_state=_clean_int(payload.get("eventState")),
        format_type=_clean_int(payload.get("formatType")),
        draft_id=_clean_string(payload.get("draftId")),
        current_event_state=_clean_int(payload.get("currentEventState")),
        current_module=_clean_int(payload.get("currentModule")),
        elapsed_ms=_clean_int(payload.get("elapsedTime")),
    )


def _clean_string(value: object) -> str:
    return "" if value is None else str(value).strip()


def _clean_int(value: object) -> int:
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0
