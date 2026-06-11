"""Arena memory polling — player identity retry and event-lobby tracking."""

from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Signal

from client.overlay.arena_memory import ArenaCurrentEvent
from client.overlay.boot import ArenaIdentityResolution
from client.overlay.log_watcher import is_arena_running
from client.overlay.managers.worker_pool import WorkerPool
from client.overlay.managers.workers import (
    ArenaCurrentEventWorker,
    ArenaIdentityWorker,
)

logger = logging.getLogger("overlay")


class ArenaMemoryPoller(QObject):
    """Polls Arena process memory for the player identity and lobby state.

    Emits signals only. The lobby gate (``not state.draft_active``) and
    the identity application (save, auth wiring, auto-login) live in
    ``OverlayApp`` — the poller just reports what memory says.
    """

    identity_resolved = Signal(object)  # ArenaIdentityResolution
    lobby_entered = Signal(str)         # internal event name (context)
    lobby_left = Signal()

    POLL_INTERVAL_MS = 5_000
    # Number of consecutive failed identity reads on an attached session
    # before forcing a re-attach. The cached image may be missing
    # assemblies that loaded after our initial attach (Core loads before
    # SharedClientCore where AccountInformation lives).
    IDENTITY_REATTACH_AFTER = 3

    def __init__(
        self,
        *,
        has_player_id: Callable[[], bool],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._has_player_id = has_player_id
        self._pool = WorkerPool()
        self._identity_failure_count = 0
        # Memory-side draft lobby presence — independent of the log
        # watcher's lobby context, which can be cleared faster than
        # memory polls.
        self._in_draft_lobby = False

        self._identity_worker: ArenaIdentityWorker | None = None
        self._identity_timer = QTimer(self)
        self._identity_timer.setInterval(self.POLL_INTERVAL_MS)
        self._identity_timer.timeout.connect(self._retry_identity)

        self._current_event_worker: ArenaCurrentEventWorker | None = None
        self._current_event_timer = QTimer(self)
        self._current_event_timer.setInterval(self.POLL_INTERVAL_MS)
        self._current_event_timer.timeout.connect(self._poll_current_event)

    def start(self) -> None:
        """Start polling; identity retries only while no player ID is known."""
        if not self._has_player_id():
            self._identity_timer.start()
        self._current_event_timer.start()

    def stop(self) -> None:
        self._identity_timer.stop()
        self._current_event_timer.stop()

    # -- identity ----------------------------------------------------------

    def _retry_identity(self) -> None:
        """Retry the Arena player ID lookup after Arena starts."""
        if self._has_player_id():
            self._identity_timer.stop()
            return
        if self._identity_worker is not None and self._identity_worker.isRunning():
            return
        if not is_arena_running():
            return

        worker = ArenaIdentityWorker()
        worker.finished_identity.connect(self._on_identity_done)
        self._identity_worker = worker
        self._pool.launch(worker)

    def _on_identity_done(self, identity: object) -> None:
        self._identity_worker = None
        if not isinstance(identity, ArenaIdentityResolution):
            self._on_identity_read_failed()
            return

        logger.info(
            "Arena player ID discovered after startup from %s: %s%s",
            identity.source,
            identity.player_id,
            (
                f" (display={identity.display_name or 'unknown'})"
                if identity.source == "memory"
                else ""
            ),
        )
        self._identity_failure_count = 0
        self._identity_timer.stop()
        self.identity_resolved.emit(identity)

    def _on_identity_read_failed(self) -> None:
        """Force a session re-attach after N consecutive failed reads.

        The session may have been attached while MTGA was still loading
        ``SharedClientCore`` (where AccountInformation lives). Detaching
        forces the next ensure_attached to re-walk assemblies and pick up
        anything that loaded since.
        """
        from client.overlay.memory.session import MemorySession

        self._identity_failure_count += 1
        if self._identity_failure_count >= self.IDENTITY_REATTACH_AFTER:
            logger.info(
                "Arena identity not found after %d attempts — "
                "forcing memory session re-attach to refresh assemblies",
                self._identity_failure_count,
            )
            MemorySession.instance().detach()
            self._identity_failure_count = 0

    # -- event lobby ---------------------------------------------------------

    def _poll_current_event(self) -> None:
        """Poll Arena memory for event lobby enter/exit state."""
        if (
            self._current_event_worker is not None
            and self._current_event_worker.isRunning()
        ):
            return
        if not is_arena_running():
            return

        worker = ArenaCurrentEventWorker()
        worker.finished_event.connect(self._on_current_event_done)
        self._current_event_worker = worker
        self._pool.launch(worker)

    def _on_current_event_done(self, current_event: object) -> None:
        self._current_event_worker = None
        if not isinstance(current_event, ArenaCurrentEvent):
            return

        # Reading current event implies the memory session is attached. If
        # we still don't have an Arena player ID, retry that walk now —
        # AccountInformation may have only just populated post sign-in.
        if not self._has_player_id():
            self._retry_identity()

        if current_event.is_draft_lobby:
            self._in_draft_lobby = True
            self.lobby_entered.emit(current_event.internal_event_name)
            return

        # Memory says we are NOT in a draft event lobby. Detect leave when
        # we previously saw one — also catches queue exits after Event_Join
        # (which clears the lobby context field).
        if self._in_draft_lobby:
            self._in_draft_lobby = False
            logger.info(
                "Left draft lobby from memory: content=%s",
                current_event.content_type or "unknown",
            )
            self.lobby_left.emit()
