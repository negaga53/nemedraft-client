"""Admission-queue heartbeat poller.

Mirrors AuthPollingManager: all HTTP runs on a worker QThread and the
manager emits Qt signals only. Never touch widgets from the worker —
LogWatcher/MemoryWatcher taught us that Qt's stylesheet engine races and
segfaults when widgets are mutated off the main thread.

Cadence adapts to state so the server's 60 s claim window is not half
eaten by a stale 30 s poll.
"""

from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from client.overlay.api_client import NemeDraftClient, QueueStatus
from client.overlay.managers.worker_pool import WorkerPool

logger = logging.getLogger("overlay")

_INTERVALS_MS: dict[str, int] = {
    "idle": 30_000,
    "cooldown": 30_000,
    "queued": 10_000,
    "offered": 3_000,
    "seated": 15_000,
}


class _HeartbeatWorker(QThread):
    """Runs one ``queue_heartbeat`` call off the UI thread."""

    finished_status = Signal(object)   # QueueStatus | None

    def __init__(self, api: NemeDraftClient, payload: dict) -> None:
        super().__init__()
        self._api = api
        self._payload = payload

    def run(self) -> None:  # noqa: D401
        try:
            self.finished_status.emit(self._api.queue_heartbeat(**self._payload))
        except Exception:
            logger.warning("queue heartbeat failed", exc_info=True)
            self.finished_status.emit(None)


class _ReleaseWorker(QThread):
    """Runs one ``queue_release`` call off the UI thread."""

    def __init__(self, api: NemeDraftClient) -> None:
        super().__init__()
        self._api = api

    def run(self) -> None:  # noqa: D401
        try:
            self._api.queue_release()
        except Exception:
            logger.warning("queue release failed", exc_info=True)


class QueueManager(QObject):
    """Owns the adaptive queue heartbeat and the user's join/leave intent."""

    status_changed = Signal(object)    # QueueStatus
    unreachable = Signal()

    def __init__(
        self,
        api_client: NemeDraftClient,
        *,
        arena_running: Callable[[], bool],
        current_set: Callable[[], str],
        draft_active: Callable[[], bool],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._api = api_client
        self._arena_running = arena_running
        self._current_set = current_set
        self._draft_active = draft_active
        self._pool = WorkerPool()
        self._want_seat = False
        self._inflight = False
        self._state = "idle"
        self.current_interval_ms = _INTERVALS_MS["idle"]
        self._timer = QTimer(self)
        self._timer.setInterval(self.current_interval_ms)
        self._timer.timeout.connect(self.poll_now)

    @staticmethod
    def interval_ms_for(state: str) -> int:
        return _INTERVALS_MS.get(state, _INTERVALS_MS["idle"])

    def start(self) -> None:
        self._timer.start()
        self.poll_now()

    def stop(self) -> None:
        self._timer.stop()

    @property
    def state(self) -> str:
        return self._state

    def join_queue(self) -> None:
        """Record that the user wants a seat and poll immediately."""
        self._want_seat = True
        self.poll_now()

    def leave_queue(self) -> None:
        """Record that the user no longer wants a seat and release it.

        The release call and the next heartbeat hit two different
        endpoints with no server-side ordering guarantee, so firing both
        at once risks the heartbeat landing first and reporting the
        pre-release state back to the UI (a one-frame flicker of
        "still queued/seated" right after the user clicked leave). We
        avoid that by *sequencing*: the heartbeat poll only fires once
        the release worker's ``finished`` signal lands, so the server has
        already processed the release by the time we ask it for a fresh
        status.
        """
        self._want_seat = False
        worker = _ReleaseWorker(self._api)
        worker.finished.connect(self.poll_now)
        self._pool.launch(worker)

    def _payload(self) -> dict:
        return {
            "want_seat": self._want_seat,
            "arena_running": bool(self._arena_running()),
            "set_code": self._current_set() or "",
            "draft_active": bool(self._draft_active()),
        }

    def poll_now(self) -> None:
        """Run one heartbeat poll on a background worker.

        Only one poll may be in flight at a time; a second call while
        one is running is a silent no-op (the timer or the next
        join/leave call will trigger the next attempt).
        """
        if self._inflight:
            return
        self._inflight = True
        worker = _HeartbeatWorker(self._api, self._payload())
        worker.finished_status.connect(self._on_poll_done)
        self._pool.launch(worker)

    def _run_poll_sync(self) -> None:
        """Synchronous poll. Test hook only — production always uses the
        worker QThread via :meth:`poll_now`."""
        if self._inflight:
            return
        self._inflight = True
        self._on_poll_done(self._api.queue_heartbeat(**self._payload()))

    def _on_poll_done(self, status: object) -> None:
        # Must run first, unconditionally: if a failure path below this
        # line ever exited early without clearing the flag, poll_now()
        # would refuse every future call and the queue position would
        # freeze on screen forever with no further requests going out.
        self._inflight = False
        if not isinstance(status, QueueStatus):
            self.unreachable.emit()
            return

        # A cooldown means the server rejected our seat request; stop
        # asking until the user opts back in via join_queue(). We do not
        # otherwise infer want_seat from the returned state — only the
        # user's own join_queue()/leave_queue() calls and this cooldown
        # clearing should change intent, so a stale/racy response never
        # silently re-arms a request the user just cancelled.
        if status.state == "cooldown":
            self._want_seat = False

        self._state = status.state
        interval = self.interval_ms_for(status.state)
        if interval != self.current_interval_ms:
            self.current_interval_ms = interval
            self._timer.setInterval(interval)
        self.status_changed.emit(status)
