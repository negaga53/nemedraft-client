"""SceneChange handling — EventLanding → DeckBuilder must carry the
destination scene on the emitted DraftLobbyEvent so main.py can switch
the overlay's deck tab when the player navigates back to their deck."""

from __future__ import annotations

from client.overlay.log_watcher import (
    DraftEndEvent,
    DraftLobbyEvent,
    LogWatcher,
)


def _build_watcher() -> tuple[LogWatcher, list]:
    watcher = LogWatcher.__new__(LogWatcher)
    watcher._callbacks = []  # type: ignore[attr-defined]
    events: list = []
    watcher._callbacks.append(events.append)  # type: ignore[attr-defined]
    return watcher, events


def test_leave_event_landing_to_deckbuilder_carries_destination():
    watcher, events = _build_watcher()
    blob = {
        "SceneChange": True,
        "fromSceneName": "EventLanding",
        "toSceneName": "DeckBuilder",
    }
    watcher._dispatch(blob, '"SceneChange": "1"')
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, DraftLobbyEvent)
    assert ev.context == ""
    assert ev.destination == "DeckBuilder"


def test_leave_event_landing_to_home_carries_destination():
    watcher, events = _build_watcher()
    blob = {
        "SceneChange": True,
        "fromSceneName": "EventLanding",
        "toSceneName": "Home",
    }
    watcher._dispatch(blob, '"SceneChange": "1"')
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, DraftLobbyEvent)
    assert ev.destination == "Home"


def test_enter_event_landing_emits_context_and_empty_destination():
    """Entering the lobby still carries no destination (we're not leaving)."""
    watcher, events = _build_watcher()
    blob = {
        "SceneChange": True,
        "fromSceneName": "Home",
        "toSceneName": "EventLanding",
        "context": "EOE_Quick_Draft",
    }
    watcher._dispatch(blob, '"SceneChange": "1"')
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, DraftLobbyEvent)
    assert ev.context == "EOE_Quick_Draft"
    assert ev.destination == ""


def test_leave_draft_screen_still_emits_draftend():
    """Sanity: from=Draft → to=Home still emits DraftEndEvent, not DraftLobbyEvent."""
    watcher, events = _build_watcher()
    blob = {
        "SceneChange": True,
        "fromSceneName": "Draft",
        "toSceneName": "Home",
    }
    watcher._dispatch(blob, '"SceneChange": "1"')
    assert len(events) == 1
    assert isinstance(events[0], DraftEndEvent)
