"""OverlayApp <-> QueueManager wiring: seat gate, shot clock, auto-rejoin guard.

Constructing a full OverlayApp (boot sequence, real workers, real widgets)
is impractical in a unit test, so these tests exercise the handler methods
directly against a minimal stand-in built via ``__new__`` — the same
pattern ``test_pick_history_nav.py`` uses for the event-handling path.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

# Must be set before any PySide6 import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from client.overlay.api_client import QueueStatus, SeatStatus


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _make_app():
    """Just enough state for _on_queue_status / _on_leave_queue_requested
    / _on_prediction_no_seat — all real methods, mocked collaborators."""
    from client.overlay.draft_state import DraftState
    from client.overlay.main import OverlayApp

    app = OverlayApp.__new__(OverlayApp)
    app.state = DraftState()
    app._has_seat = False
    app._user_initiated_leave = False
    app._queue = MagicMock()
    app.window = MagicMock()
    app.api_client = MagicMock()
    return app


def _make_event_app(mapper_names: list[str] | None = None):
    """Minimal OverlayApp for exercising the ``_on_event`` gate sites
    (DraftStartEvent / PackEvent / ReplayDoneEvent all share the same
    ``if self._has_seat: show draft else: stay home + join_queue()``
    shape)."""
    from client.overlay.draft_state import DraftState
    from client.overlay.main import OverlayApp

    app = OverlayApp.__new__(OverlayApp)
    app.state = DraftState()
    app.scryfall_cards = {}
    app._recent_event_signatures = {}
    app._pending_events = []
    app._draft_completed = False
    app._in_lobby_context = ""
    app._cache_dir = Path(tempfile.mkdtemp())

    app.mapper = MagicMock()
    app.mapper.grpids_to_names.return_value = mapper_names or []

    app._set_data = MagicMock()
    app._set_data.is_ready = True
    app._set_data.loaded_set = "FIN"

    app.watcher = MagicMock()
    app.watcher.replaying = False
    app.watcher._cur_draft_event = "PremierDraft_FIN_20260601"

    app.auth_client = MagicMock()
    app.auth_client.is_authenticated = False  # _run_prediction no-ops
    app.auth_client.session = None
    app._auth_polling = MagicMock()

    app._queue = MagicMock()
    app.window = MagicMock()
    return app


# ---------------------------------------------------------------------------
# _on_queue_status
# ---------------------------------------------------------------------------

def test_seated_status_drives_shot_clock_bar(qapp):
    app = _make_app()
    status = QueueStatus(
        state="seated", active_drafters=1, capacity=40,
        seat=SeatStatus(shot_clock_remaining_s=42.0, shot_clock_total_s=60.0),
    )

    app._on_queue_status(status)

    app.window.pack_tab.shot_clock_bar.set_remaining.assert_called_with(42.0, 60.0)
    assert app._has_seat is True


def test_offered_state_counts_as_has_seat(qapp):
    app = _make_app()
    app._on_queue_status(QueueStatus(state="offered", active_drafters=1, capacity=40))
    assert app._has_seat is True


def test_non_queuestatus_payload_is_ignored(qapp):
    """QueueManager.unreachable() has no payload — must not be mistaken
    for a QueueStatus and must not touch _has_seat."""
    app = _make_app()
    app._on_queue_status(None)
    assert app._has_seat is False
    app.window.show_draft_ended.assert_not_called()


def test_seat_loss_with_open_draft_triggers_rejoin(qapp):
    """seated -> idle while a set is still detected re-joins immediately
    (shot clock / grace timeout / server restart)."""
    app = _make_app()
    app.state.set_code = "TMT"

    app._on_queue_status(QueueStatus(state="seated", active_drafters=1, capacity=40))
    assert app._has_seat is True
    app._queue.join_queue.reset_mock()

    app._on_queue_status(QueueStatus(state="idle", active_drafters=1, capacity=40))

    assert app._has_seat is False
    app._queue.join_queue.assert_called_once()
    app.window.show_draft_ended.assert_called()
    app.window.pack_tab.shot_clock_bar.clear.assert_called()


def test_seat_loss_without_open_draft_does_not_rejoin(qapp):
    """If the draft already ended (no set_code), don't re-queue."""
    app = _make_app()
    app.state.set_code = ""

    app._on_queue_status(QueueStatus(state="seated", active_drafters=1, capacity=40))
    app._on_queue_status(QueueStatus(state="idle", active_drafters=1, capacity=40))

    app._queue.join_queue.assert_not_called()


def test_deliberate_leave_does_not_trigger_rejoin(qapp):
    """A user-initiated leave_queue() must not auto-rejoin on the next
    status update that reports the seat gone (leave -> detect loss ->
    rejoin would otherwise loop)."""
    app = _make_app()
    app.state.set_code = "TMT"

    app._on_queue_status(QueueStatus(state="seated", active_drafters=1, capacity=40))
    assert app._has_seat is True

    app._on_leave_queue_requested()
    app._queue.leave_queue.assert_called_once()
    app._queue.join_queue.reset_mock()

    # The manager's own release-then-heartbeat sequence eventually reports
    # the seat gone.
    app._on_queue_status(QueueStatus(state="idle", active_drafters=1, capacity=40))

    assert app._has_seat is False
    app._queue.join_queue.assert_not_called()


def test_leave_flag_does_not_leak_to_a_later_involuntary_loss(qapp):
    """The deliberate-leave flag must be consumed by the first status
    update after it's set, not linger and swallow a *later*, genuinely
    involuntary seat loss."""
    app = _make_app()
    app.state.set_code = "TMT"

    app._on_queue_status(QueueStatus(state="seated", active_drafters=1, capacity=40))
    app._on_leave_queue_requested()
    app._on_queue_status(QueueStatus(state="idle", active_drafters=1, capacity=40))
    assert app._user_initiated_leave is False

    # Re-admitted, then loses the seat again — this time involuntarily.
    app._on_queue_status(QueueStatus(state="seated", active_drafters=1, capacity=40))
    app._queue.join_queue.reset_mock()
    app._on_queue_status(QueueStatus(state="idle", active_drafters=1, capacity=40))

    app._queue.join_queue.assert_called_once()


# ---------------------------------------------------------------------------
# _on_prediction_no_seat
# ---------------------------------------------------------------------------

def test_predict_no_seat_switches_to_home_and_reuses_queue_handler(qapp):
    app = _make_app()
    app.state.set_code = "TMT"
    app._on_queue_status(QueueStatus(state="seated", active_drafters=1, capacity=40))
    app._queue.join_queue.reset_mock()

    status = QueueStatus(state="queued", position=2, active_drafters=1, capacity=40)
    app._on_prediction_no_seat(status)

    app.window.pack_tab.hide_loading.assert_called_once()
    assert app._has_seat is False
    app.window.show_draft_ended.assert_called()
    app._queue.join_queue.assert_called_once()


# ---------------------------------------------------------------------------
# Draft-view gate at the _on_event sites (formerly _is_vip())
# ---------------------------------------------------------------------------

def test_draft_start_with_seat_shows_draft_view(qapp):
    from client.overlay.log_watcher import DraftStartEvent

    app = _make_event_app()
    app._has_seat = True

    app._on_event(
        DraftStartEvent(event_name="PremierDraft_FIN_20260601"), replaying=False,
    )

    app.window.show_draft_started.assert_called()
    app._queue.join_queue.assert_not_called()


def test_draft_start_without_seat_stays_home_and_requests_a_seat(qapp):
    from client.overlay.log_watcher import DraftStartEvent

    app = _make_event_app()
    app._has_seat = False

    app._on_event(
        DraftStartEvent(event_name="PremierDraft_FIN_20260601"), replaying=False,
    )

    app.window.show_draft_started.assert_not_called()
    app.window.pack_tab.home_widget.set_draft_active.assert_called_with(True)
    app._queue.join_queue.assert_called_once()
