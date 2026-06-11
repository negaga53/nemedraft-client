"""Lazy per-set data loading with a watchdog against hung loads."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from client.overlay.managers.worker_pool import WorkerPool
from client.overlay.managers.workers import SetDataResult, SetDataWorker

logger = logging.getLogger("overlay")


class SetDataManager(QObject):
    """Owns the SetDataWorker lifecycle for the currently-relevant set.

    Emits signals only; the caller decides what to show. On error *and*
    on watchdog timeout the manager degrades to ``is_ready == True`` so
    the draft can proceed — predictions may partially work without the
    full per-set data (same semantics the error path always had).
    """

    progress = Signal(str)
    ready = Signal(object)      # SetDataResult
    failed = Signal(str, str)   # set_code, message

    LOAD_TIMEOUT_MS = 90_000

    def __init__(
        self,
        mapper: object,
        scryfall_dir: Path,
        *,
        load_timeout_ms: int | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._mapper = mapper
        self._scryfall_dir = scryfall_dir
        self._pool = WorkerPool()
        self._worker: SetDataWorker | None = None
        self._loaded_set = ""
        self._ready = False
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.setInterval(load_timeout_ms or self.LOAD_TIMEOUT_MS)
        self._watchdog.timeout.connect(self._on_watchdog_timeout)

    @property
    def loaded_set(self) -> str:
        return self._loaded_set

    @property
    def is_ready(self) -> bool:
        return self._ready

    def ensure(self, set_code: str) -> bool:
        """Start loading data for *set_code* unless already loaded/loading.

        Returns:
            True when a new load was started, False on no-op.
        """
        if not set_code:
            return False
        if self._loaded_set == set_code and self._ready:
            return False
        if (
            self._worker is not None
            and self._worker.isRunning()
            and self._loaded_set == set_code
        ):
            return False  # already loading this set

        self._loaded_set = set_code
        self._ready = False

        worker = SetDataWorker(set_code, self._mapper, self._scryfall_dir)
        worker.progress.connect(self.progress)
        worker.finished_ok.connect(self._on_worker_ok)
        worker.finished_err.connect(
            lambda msg, sc=set_code: self._on_worker_err(sc, msg),
        )
        self._worker = worker
        self._pool.launch(worker, delete_later=False)
        self._watchdog.start()
        return True

    def reset(self) -> None:
        """Forget the loaded set (e.g. after a log rotation)."""
        self._watchdog.stop()
        self._worker = None
        self._loaded_set = ""
        self._ready = False

    def _on_worker_ok(self, result: object) -> None:
        if not isinstance(result, SetDataResult):
            return
        if result.set_code != self._loaded_set:
            logger.debug(
                "Dropping stale set-data result for %s (now loading %s)",
                result.set_code, self._loaded_set,
            )
            return
        self._watchdog.stop()
        self._worker = None
        self._ready = True
        self.ready.emit(result)

    def _on_worker_err(self, set_code: str, message: str) -> None:
        if set_code != self._loaded_set:
            return
        self._watchdog.stop()
        self._worker = None
        logger.error("Failed to load set data for %s: %s", set_code, message)
        # Allow the draft to proceed anyway — predictions may partially work.
        self._ready = True
        self.failed.emit(set_code, message)

    def _on_watchdog_timeout(self) -> None:
        if self._ready or self._worker is None:
            return
        set_code = self._loaded_set
        logger.error(
            "Set data load for %s timed out after %dms — continuing degraded",
            set_code, self._watchdog.interval(),
        )
        # Abandon the worker (WorkerPool keeps the QThread alive until its
        # run() returns); a late finished_ok for the same set still applies.
        self._ready = True
        self.failed.emit(set_code, "timed out")
