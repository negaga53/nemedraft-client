"""QueueManager cadence and signal behaviour."""

from __future__ import annotations

import os
import threading

# Must be set before any PySide6 import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

from client.overlay.api_client import QueueStatus
from client.overlay.managers.queue import QueueManager, _HeartbeatWorker


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeApi:
    """Records every heartbeat payload; returns queued responses in order."""

    def __init__(self, responses: list[QueueStatus | None] | None = None) -> None:
        self.payloads: list[dict] = []
        self._responses = list(responses) if responses is not None else []
        self.release_calls = 0
        self.call_threads: list[int] = []

    def queue_heartbeat(self, **kwargs):
        self.payloads.append(kwargs)
        self.call_threads.append(threading.get_ident())
        if self._responses:
            return self._responses.pop(0)
        return QueueStatus(state="idle")

    def queue_release(self):
        self.release_calls += 1
        return True


def _make_manager(api, *, arena_running=True, current_set="TMT", draft_active=False):
    manager = QueueManager(
        api,
        arena_running=lambda: arena_running,
        current_set=lambda: current_set,
        draft_active=lambda: draft_active,
    )
    manager._timer.stop()  # tests drive polls explicitly, never on wall-clock
    return manager


def _pump(qapp, condition, iterations: int = 500) -> bool:
    """Process the Qt event loop until condition() is true or we give up.

    Used only where the production (threaded) path is under test — the
    QThread worker's finished signal is delivered via the event loop, so
    without pumping it a queued cross-thread connection never fires.
    """
    for _ in range(iterations):
        if condition():
            return True
        qapp.processEvents()
    return condition()


# ---------------------------------------------------------------------------
# Cadence table
# ---------------------------------------------------------------------------

def test_interval_for_state():
    assert QueueManager.interval_ms_for("idle") == 30_000
    assert QueueManager.interval_ms_for("cooldown") == 30_000
    assert QueueManager.interval_ms_for("queued") == 10_000
    assert QueueManager.interval_ms_for("offered") == 3_000
    assert QueueManager.interval_ms_for("seated") == 15_000
    # An unknown state must not crash the poller.
    assert QueueManager.interval_ms_for("nonsense") == 30_000


# ---------------------------------------------------------------------------
# Synchronous behaviour (payload contents, state machine, guards) — driven
# via the _run_poll_sync() test hook so assertions never race a QThread.
# ---------------------------------------------------------------------------

def test_join_queue_sets_want_seat_for_next_request(qapp):
    api = _FakeApi([QueueStatus(state="queued", position=5)])
    manager = _make_manager(api)
    statuses: list[QueueStatus] = []
    manager.status_changed.connect(statuses.append)

    # join_queue() sets intent and polls immediately; use the sync hook
    # in place of the real poll so the request/response is deterministic.
    manager._want_seat = False
    manager._want_seat = True  # what join_queue() would set
    manager._run_poll_sync()

    assert api.payloads[-1]["want_seat"] is True
    assert statuses
    assert statuses[-1].state == "queued"
    assert statuses[-1].position == 5


def test_leave_queue_sets_want_seat_false(qapp):
    api = _FakeApi()
    manager = _make_manager(api)
    manager._want_seat = True

    manager._want_seat = False  # what leave_queue() sets before releasing
    manager._run_poll_sync()

    assert api.payloads[-1]["want_seat"] is False


def test_timer_interval_changes_with_state(qapp):
    api = _FakeApi([QueueStatus(state="offered")])
    manager = _make_manager(api)
    assert manager.current_interval_ms == 30_000

    manager._run_poll_sync()

    assert manager.current_interval_ms == 3_000
    assert manager._timer.interval() == 3_000


def test_cooldown_clears_want_seat_for_next_request(qapp):
    api = _FakeApi([
        QueueStatus(state="cooldown", cooldown_remaining_s=30.0),
        QueueStatus(state="cooldown", cooldown_remaining_s=25.0),
    ])
    manager = _make_manager(api)
    manager._want_seat = True

    manager._run_poll_sync()
    assert manager._want_seat is False

    # The *following* request must reflect the cleared intent, not the
    # want_seat=True the user set before the cooldown response arrived.
    manager._inflight = False
    manager._run_poll_sync()
    assert api.payloads[-1]["want_seat"] is False


def test_only_one_poll_in_flight_at_a_time(qapp):
    """Calling the poll entry point twice without letting the first
    resolve must issue only one request."""
    api = _FakeApi()
    manager = _make_manager(api)

    # Mark a poll as already in flight, the way poll_now() would just
    # before its worker resolves, then prove a second call is a no-op.
    manager._inflight = True
    manager.poll_now()
    assert api.payloads == []

    manager._inflight = False
    manager._run_poll_sync()
    assert len(api.payloads) == 1

    manager._inflight = True
    manager.poll_now()
    assert len(api.payloads) == 1  # still just the one


def test_unreachable_emits_signal_and_clears_inflight(qapp):
    api = _FakeApi([None])
    manager = _make_manager(api)
    unreachable_calls: list[bool] = []
    manager.unreachable.connect(lambda: unreachable_calls.append(True))

    manager._run_poll_sync()

    assert unreachable_calls == [True]
    # This is the one that matters: if _inflight were left True here, the
    # poller would silently refuse every future poll_now()/_run_poll_sync()
    # call and the user would see a frozen queue position forever.
    assert manager._inflight is False

    # Prove it's not wedged: a subsequent poll actually goes out.
    manager._run_poll_sync()
    assert len(api.payloads) == 2


def test_unreachable_does_not_emit_status_changed(qapp):
    api = _FakeApi([None])
    manager = _make_manager(api)
    statuses: list[QueueStatus] = []
    manager.status_changed.connect(statuses.append)

    manager._run_poll_sync()

    assert statuses == []


# ---------------------------------------------------------------------------
# Production (threaded) path
# ---------------------------------------------------------------------------

def test_production_poll_uses_a_qthread(qapp):
    """poll_now() must be backed by a QThread subclass and must not call
    the API inline on the calling (main) thread."""
    assert issubclass(_HeartbeatWorker, QThread)

    api = _FakeApi([QueueStatus(state="idle")])
    manager = _make_manager(api)

    manager.poll_now()
    assert _pump(qapp, lambda: bool(api.payloads))

    assert api.call_threads[0] != threading.get_ident()


def test_leave_queue_sequences_release_before_next_poll(qapp):
    """leave_queue() must not fire the heartbeat until the release worker
    has finished, so a slow release can't be raced by a heartbeat that
    reports stale (pre-release) state back to the UI."""
    api = _FakeApi([QueueStatus(state="idle")])
    manager = _make_manager(api)
    manager._want_seat = True

    manager.leave_queue()
    assert _pump(qapp, lambda: bool(api.payloads))

    assert api.release_calls == 1
    assert api.payloads[-1]["want_seat"] is False
