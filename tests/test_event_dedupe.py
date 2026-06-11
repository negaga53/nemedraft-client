"""Tests for client.overlay.events — event signatures, dedupe, UiMarshaler."""

from __future__ import annotations

import os
import time

# Must be set before any PySide6 import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_event_signature_pack_and_pick_are_distinct():
    from client.overlay.events import event_signature
    from client.overlay.log_watcher import PackEvent, PickEvent

    pack = PackEvent(card_grpids=[1, 2, 3], pack_number=0, pick_number=4)
    pick = PickEvent(card_grpids=[1, 2, 3], pack_number=0, pick_number=4)
    assert event_signature(pack) != event_signature(pick)
    assert event_signature(pack) == event_signature(
        PackEvent(card_grpids=[1, 2, 3], pack_number=0, pick_number=4)
    )


def test_event_signature_unique_source_events_bypass_dedupe():
    from client.overlay.events import event_signature
    from client.overlay.log_watcher import LogRotatedEvent, ReplayDoneEvent

    assert event_signature(ReplayDoneEvent()) is None
    assert event_signature(LogRotatedEvent()) is None


def test_should_drop_duplicate_within_window():
    from client.overlay.events import should_drop_duplicate
    from client.overlay.log_watcher import PackEvent

    recent: dict[tuple, float] = {}
    event = PackEvent(card_grpids=[10, 11], pack_number=1, pick_number=2)
    assert should_drop_duplicate(event, recent) is False
    assert should_drop_duplicate(event, recent) is True


def test_should_drop_duplicate_expires_after_window(monkeypatch):
    from client.overlay import events
    from client.overlay.log_watcher import PackEvent

    recent: dict[tuple, float] = {}
    event = PackEvent(card_grpids=[10, 11], pack_number=1, pick_number=2)
    assert events.should_drop_duplicate(event, recent) is False

    # Age the recorded signature past the dedupe window.
    sig = events.event_signature(event)
    recent[sig] = time.monotonic() - events.DEDUPE_WINDOW_S - 0.1
    assert events.should_drop_duplicate(event, recent) is False


def test_ui_marshaler_dispatches_event_with_replay_flag(qapp):
    from client.overlay.events import UiMarshaler
    from client.overlay.log_watcher import DraftEndEvent

    received: list[tuple[object, bool]] = []
    loads: list[str] = []

    marshaler = UiMarshaler()
    marshaler.bind(
        on_event=lambda ev, replaying: received.append((ev, replaying)),
        on_set_load=loads.append,
    )

    marshaler.event_received.emit(DraftEndEvent(), True)
    marshaler.set_load_requested.emit("TMT")
    qapp.processEvents()

    assert len(received) == 1
    assert isinstance(received[0][0], DraftEndEvent)
    assert received[0][1] is True
    assert loads == ["TMT"]
