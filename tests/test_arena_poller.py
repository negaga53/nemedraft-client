"""Tests for client.overlay.managers.arena_poller.ArenaMemoryPoller."""

from __future__ import annotations

import os

# Must be set before any PySide6 import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _make_poller(*, has_player_id=False):
    from client.overlay.managers.arena_poller import ArenaMemoryPoller

    return ArenaMemoryPoller(has_player_id=lambda: has_player_id)


def _lobby_event(name: str = "TMT_Quick_Draft", in_lobby: bool = True):
    from client.overlay.arena_memory import ArenaCurrentEvent

    return ArenaCurrentEvent(
        is_in_event_lobby=in_lobby,
        internal_event_name=name,
        content_type="EventPageContent",
    )


def test_identity_success_emits_and_stops_timer(qapp):
    from client.overlay.boot import ArenaIdentityResolution

    poller = _make_poller()
    poller._identity_timer.start()
    resolved: list[object] = []
    poller.identity_resolved.connect(resolved.append)

    identity = ArenaIdentityResolution(player_id="ABC123", source="memory")
    poller._on_identity_done(identity)

    assert resolved == [identity]
    assert not poller._identity_timer.isActive()
    assert poller._identity_failure_count == 0


def test_identity_reattach_after_three_failures(qapp, monkeypatch):
    import client.overlay.managers.arena_poller as ap

    detaches: list[bool] = []

    class _FakeSession:
        def detach(self):
            detaches.append(True)

    class _FakeMemorySession:
        @classmethod
        def instance(cls):
            return _FakeSession()

    monkeypatch.setattr(
        "client.overlay.memory.session.MemorySession", _FakeMemorySession,
    )

    poller = _make_poller()
    poller._on_identity_done(None)
    poller._on_identity_done(None)
    assert detaches == []
    poller._on_identity_done(None)
    assert detaches == [True]
    assert poller._identity_failure_count == 0


def test_lobby_enter_emits_context(qapp):
    poller = _make_poller(has_player_id=True)
    entered: list[str] = []
    left: list[bool] = []
    poller.lobby_entered.connect(entered.append)
    poller.lobby_left.connect(lambda: left.append(True))

    poller._on_current_event_done(_lobby_event("TMT_Quick_Draft"))
    assert entered == ["TMT_Quick_Draft"]
    assert left == []


def test_lobby_leave_emits_only_after_enter(qapp):
    poller = _make_poller(has_player_id=True)
    left: list[bool] = []
    poller.lobby_left.connect(lambda: left.append(True))

    # Not in lobby, never entered — no leave event.
    poller._on_current_event_done(_lobby_event("", in_lobby=False))
    assert left == []

    poller._on_current_event_done(_lobby_event("SOS_PickTwo_Draft"))
    poller._on_current_event_done(_lobby_event("", in_lobby=False))
    assert left == [True]
    # Repeated non-lobby polls do not re-emit.
    poller._on_current_event_done(_lobby_event("", in_lobby=False))
    assert left == [True]


def test_non_event_payload_is_ignored(qapp):
    poller = _make_poller(has_player_id=True)
    entered: list[str] = []
    poller.lobby_entered.connect(entered.append)

    poller._on_current_event_done(None)
    poller._on_current_event_done("garbage")
    assert entered == []
