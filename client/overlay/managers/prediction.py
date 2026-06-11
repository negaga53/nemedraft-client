"""Prediction lifecycle — dispatch, retry backoff, signals, deck suggestions.

The manager never reads ``DraftState`` or touches widgets: it pulls a
fresh :class:`PredictionRequest` snapshot from ``request_provider`` at
every attempt (state may advance between retries) and reports outcomes
via Qt signals only. ``OverlayApp`` is the sole signal-to-widget bridge.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Signal

from client.overlay.api_client import NemeDraftClient
from client.overlay.managers.worker_pool import WorkerPool
from client.overlay.managers.workers import (
    DeckSuggestionsWorker,
    PredictionWorker,
    SignalsWorker,
)

logger = logging.getLogger("overlay")


@dataclass(frozen=True)
class PredictionRequest:
    """Immutable snapshot of the draft state a prediction is made for."""

    pack_cards: tuple[str, ...]
    pool_cards: tuple[str, ...]
    set_code: str
    pack_number: int
    pick_number: int
    draft_format: str
    arena_format: str
    last_pick: str | None


class PredictionManager(QObject):
    """Owns the predict/retry loop and the signals / deck-suggestion fetches."""

    loading = Signal(int, int)               # pack_number, pick_number
    results_ready = Signal(object, int, int)  # list[Pick], pack, pick
    retrying = Signal(int, int)              # attempt number, delay ms
    gave_up = Signal()
    signals_ready = Signal(object)           # SignalResult
    deck_suggestions_ready = Signal(object)  # dict[str, DeckSuggestion]

    RETRY_TIMEOUT_S = 60        # total window to keep retrying
    RETRY_INTERVALS_MS = (      # delay before each successive retry
        2_000, 3_000, 5_000, 5_000, 10_000, 10_000, 15_000, 15_000,
    )

    def __init__(
        self,
        api_client: NemeDraftClient,
        *,
        request_provider: Callable[[], PredictionRequest | None],
        is_active: Callable[[], bool],
        time_fn: Callable[[], float] = time.monotonic,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._api_client = api_client
        self._request_provider = request_provider
        self._is_active = is_active
        self._time_fn = time_fn

        self._pool = WorkerPool()
        self._current_worker: PredictionWorker | None = None
        self._retry_attempt = 0
        self._retry_start = 0.0
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._on_retry_timeout)

    # -- prediction ------------------------------------------------------

    def request_prediction(self) -> None:
        """Start a fresh prediction attempt, resetting retry state."""
        self._retry_timer.stop()
        self._retry_attempt = 0
        self._retry_start = self._time_fn()
        self._attempt()

    def cancel(self) -> None:
        """Stop retrying and discard any in-flight worker's result."""
        self._retry_timer.stop()
        self._retry_attempt = 0
        self._retry_start = 0.0
        self._current_worker = None

    def _attempt(self) -> None:
        request = self._request_provider()
        if request is None:
            return
        self.loading.emit(request.pack_number, request.pick_number)

        worker = PredictionWorker(
            self._api_client,
            pack_cards=list(request.pack_cards),
            pool_cards=list(request.pool_cards),
            set_code=request.set_code,
            pack_number=request.pack_number,
            pick_number=request.pick_number,
            draft_format=request.draft_format,
            arena_format=request.arena_format,
            last_pick=request.last_pick,
        )
        # Closure carries the worker identity — newest dispatch wins, and
        # results from a worker started for a now-stale pack are discarded.
        worker.finished_ok.connect(
            lambda results, pn, pk, w=worker: self._on_worker_finished(
                w, results, pn, pk,
            ),
        )
        worker.failed.connect(
            lambda err, pn, pk, w=worker: self._on_worker_failed(w, err, pn, pk),
        )
        self._current_worker = worker
        self._pool.launch(worker)

    def _on_worker_finished(
        self,
        worker: object,
        results: list,
        pack_number: int,
        pick_number: int,
    ) -> None:
        if worker is not self._current_worker:
            logger.debug(
                "Discarding stale prediction worker result for P%dP%d",
                pack_number + 1, pick_number + 1,
            )
            return
        self._current_worker = None

        if not results:
            logger.warning(
                "No prediction results from server (attempt %d)",
                self._retry_attempt + 1,
            )
            self._schedule_retry()
            return

        self._retry_attempt = 0
        self._retry_start = 0.0
        self.results_ready.emit(results, pack_number, pick_number)

    def _on_worker_failed(
        self,
        worker: object,
        error: str,
        pack_number: int,
        pick_number: int,
    ) -> None:
        if worker is not self._current_worker:
            return
        self._current_worker = None
        logger.warning(
            "Prediction failed for P%dP%d (attempt %d): %s",
            pack_number + 1, pick_number + 1, self._retry_attempt + 1, error,
        )
        self._schedule_retry()

    def _schedule_retry(self) -> None:
        elapsed = self._time_fn() - self._retry_start
        if elapsed >= self.RETRY_TIMEOUT_S:
            logger.error(
                "Server unreachable after %ds — giving up", int(elapsed),
            )
            self.cancel()
            self.gave_up.emit()
            return

        idx = min(self._retry_attempt, len(self.RETRY_INTERVALS_MS) - 1)
        delay = self.RETRY_INTERVALS_MS[idx]
        self._retry_attempt += 1
        logger.info(
            "Retrying prediction in %dms (attempt %d, %.0fs elapsed)",
            delay, self._retry_attempt, elapsed,
        )
        self.retrying.emit(self._retry_attempt, delay)
        self._retry_timer.start(delay)

    def _on_retry_timeout(self) -> None:
        if not self._is_active():
            logger.info("Draft no longer active — cancelling retry")
            return
        self._attempt()

    # -- signals / deck suggestions ---------------------------------------

    def update_signals(
        self,
        *,
        seen_items: list[dict],
        set_code: str,
        draft_format: str,
    ) -> None:
        """Fetch signal scores on a background worker."""
        worker = SignalsWorker(
            self._api_client,
            seen_items=seen_items,
            set_code=set_code,
            draft_format=draft_format,
        )
        worker.finished_ok.connect(self._on_signals_done)
        self._pool.launch(worker)

    def _on_signals_done(self, result: object) -> None:
        if result is not None:
            self.signals_ready.emit(result)

    def update_deck_suggestions(
        self,
        *,
        pool_cards: list[str],
        set_code: str,
        draft_format: str,
    ) -> None:
        """Fetch deck suggestions on a background worker."""
        worker = DeckSuggestionsWorker(
            self._api_client,
            pool_cards=pool_cards,
            set_code=set_code,
            draft_format=draft_format,
        )
        worker.finished_ok.connect(self._on_deck_suggestions_done)
        self._pool.launch(worker)

    def _on_deck_suggestions_done(self, suggestions: object) -> None:
        if suggestions:
            self.deck_suggestions_ready.emit(suggestions)
