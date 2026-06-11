"""Tests for client.overlay.managers.prediction.PredictionManager."""

from __future__ import annotations

import os
import threading
import time

# Must be set before any PySide6 import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _request():
    from client.overlay.managers.prediction import PredictionRequest

    return PredictionRequest(
        pack_cards=("Lightning Bolt", "Giant Growth"),
        pool_cards=("Shock",),
        set_code="TMT",
        pack_number=0,
        pick_number=3,
        draft_format="PremierDraft",
        arena_format="PremierDraft",
        last_pick="Shock",
    )


class _FakePick:
    def __init__(self, card: str) -> None:
        self.card = card


class _FakeApiClient:
    """Records the thread each call ran on; behavior is configurable."""

    def __init__(self, *, predict_results=None, predict_error=None) -> None:
        self.predict_results = predict_results or []
        self.predict_error = predict_error
        self.calls: list[str] = []
        self.call_threads: list[int] = []
        self.signals_scores = {"W": 1.0}
        self.deck_raw: dict | None = None

    def predict(self, **kwargs):
        self.calls.append("predict")
        self.call_threads.append(threading.get_ident())
        if self.predict_error is not None:
            raise self.predict_error
        return list(self.predict_results)

    def compute_signals(self, seen_items, set_code, draft_format=""):
        self.calls.append("signals")
        self.call_threads.append(threading.get_ident())
        return dict(self.signals_scores)

    def deck_suggestions(self, *, pool_cards, set_code, draft_format=""):
        self.calls.append("deck")
        self.call_threads.append(threading.get_ident())
        return self.deck_raw


def _wait_until(predicate, qapp, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        qapp.processEvents()
        time.sleep(0.01)
    return predicate()


def _make_manager(api, *, request=None, active=True, time_fn=None):
    from client.overlay.managers.prediction import PredictionManager

    return PredictionManager(
        api,
        request_provider=lambda: request if request is not None else _request(),
        is_active=lambda: active,
        time_fn=time_fn or time.monotonic,
    )


def test_successful_prediction_emits_results_off_main_thread(qapp):
    api = _FakeApiClient(predict_results=[_FakePick("Lightning Bolt")])
    manager = _make_manager(api, request=_request())
    received: list[tuple] = []
    manager.results_ready.connect(lambda r, pn, pk: received.append((r, pn, pk)))
    loading: list[tuple] = []
    manager.loading.connect(lambda pn, pk: loading.append((pn, pk)))

    manager.request_prediction()
    assert _wait_until(lambda: received, qapp)

    results, pack_number, pick_number = received[0]
    assert results[0].card == "Lightning Bolt"
    assert (pack_number, pick_number) == (0, 3)
    assert loading == [(0, 3)]
    # The HTTP call must not run on the Qt main thread.
    assert api.call_threads[0] != threading.get_ident()


def test_failed_prediction_schedules_retry_with_backoff(qapp):
    from client.overlay.managers.prediction import PredictionManager

    api = _FakeApiClient(predict_error=RuntimeError("boom"))
    manager = _make_manager(api, request=_request())
    retries: list[tuple[int, int]] = []
    manager.retrying.connect(lambda attempt, delay: retries.append((attempt, delay)))

    manager.request_prediction()
    assert _wait_until(lambda: retries, qapp)

    attempt, delay = retries[0]
    assert attempt == 1
    assert delay == PredictionManager.RETRY_INTERVALS_MS[0]
    assert manager._retry_timer.isActive()
    manager.cancel()


def test_backoff_schedule_follows_interval_table(qapp):
    from client.overlay.managers.prediction import PredictionManager

    api = _FakeApiClient()
    manager = _make_manager(api, time_fn=lambda: 100.0)
    manager._retry_start = 100.0  # elapsed == 0 — never times out
    retries: list[tuple[int, int]] = []
    manager.retrying.connect(lambda attempt, delay: retries.append((attempt, delay)))

    for _ in range(10):
        manager._schedule_retry()
        manager._retry_timer.stop()

    delays = [d for _, d in retries]
    table = PredictionManager.RETRY_INTERVALS_MS
    assert delays[: len(table)] == list(table)
    # Past the table end, the last interval repeats.
    assert delays[len(table):] == [table[-1]] * (10 - len(table))


def test_gives_up_after_timeout_window(qapp):
    from client.overlay.managers.prediction import PredictionManager

    clock = {"now": 0.0}
    api = _FakeApiClient()
    manager = _make_manager(api, time_fn=lambda: clock["now"])
    gave_up: list[bool] = []
    manager.gave_up.connect(lambda: gave_up.append(True))

    manager._retry_start = 0.0
    clock["now"] = PredictionManager.RETRY_TIMEOUT_S + 1
    manager._schedule_retry()

    assert gave_up == [True]
    assert not manager._retry_timer.isActive()
    assert manager._retry_attempt == 0


def test_stale_worker_results_are_discarded(qapp):
    api = _FakeApiClient()
    manager = _make_manager(api)
    received: list[object] = []
    manager.results_ready.connect(lambda r, pn, pk: received.append(r))

    stale_worker = object()
    manager._on_worker_finished(stale_worker, [_FakePick("X")], 0, 0)
    assert received == []


def test_retry_skipped_when_no_longer_active(qapp):
    api = _FakeApiClient(predict_results=[_FakePick("Y")])
    manager = _make_manager(api, active=False)
    received: list[object] = []
    manager.results_ready.connect(lambda r, pn, pk: received.append(r))

    manager._retry_start = time.monotonic()
    manager._on_retry_timeout()
    qapp.processEvents()
    assert api.calls == []
    assert received == []


def test_update_signals_runs_off_main_thread(qapp):
    api = _FakeApiClient()
    manager = _make_manager(api)
    results: list[object] = []
    manager.signals_ready.connect(results.append)

    manager.update_signals(
        seen_items=[{"card_name": "Shock", "colors": [], "gihwr": 0.0,
                     "ata": 0.0, "pack_number": 0, "pick_number": 1}],
        set_code="TMT",
        draft_format="PremierDraft",
    )
    assert _wait_until(lambda: results, qapp)
    assert results[0] is not None
    assert results[0].scores == {"W": 1.0}
    assert api.call_threads[0] != threading.get_ident()


def test_update_deck_suggestions_converts_and_runs_off_main_thread(qapp):
    api = _FakeApiClient()
    api.deck_raw = {
        "WU": {
            "archetype": "WU Fliers",
            "main_deck": ["Plains"] * 17 + ["Skywatcher"] * 23,
            "main_deck_cmc": [1.0] * 40,
            "lands": {"Plains": 9, "Island": 8},
            "nonbasic_lands": [],
            "score": 0.7,
            "creature_count": 15,
            "spell_count": 8,
            "land_count": 17,
            "avg_cmc": 2.9,
        }
    }
    manager = _make_manager(api)
    results: list[object] = []
    manager.deck_suggestions_ready.connect(results.append)

    manager.update_deck_suggestions(
        pool_cards=["Skywatcher"] * 23, set_code="TMT",
        draft_format="PremierDraft",
    )
    assert _wait_until(lambda: results, qapp)
    suggestions = results[0]
    assert "WU" in suggestions
    assert suggestions["WU"].archetype == "WU Fliers"
    assert api.call_threads[0] != threading.get_ident()
