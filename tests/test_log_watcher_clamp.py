"""LogWatcher should drop PackEvent/PickEvent frames outside the
3-pack, 15-card draft envelope. Without this guard the DB records
phantom "Pack 4" rows that fragment one real draft."""

from __future__ import annotations

from client.overlay.log_watcher import (
    DraftCompleteEvent,
    DraftStartEvent,
    LogWatcher,
    PackEvent,
    PickEvent,
)


def _build_watcher() -> tuple[LogWatcher, list]:
    watcher = LogWatcher.__new__(LogWatcher)
    watcher._callbacks = []  # type: ignore[attr-defined]
    events: list = []
    watcher._callbacks.append(events.append)  # type: ignore[attr-defined]
    return watcher, events


def test_emit_drops_pack_event_pack_above_two():
    watcher, events = _build_watcher()
    watcher._emit(PackEvent(card_grpids=[1, 2, 3], pack_number=3, pick_number=0))
    assert events == []


def test_emit_drops_pack_event_pick_above_fourteen():
    watcher, events = _build_watcher()
    watcher._emit(PackEvent(card_grpids=[1], pack_number=2, pick_number=15))
    assert events == []


def test_emit_drops_pick_event_pack_above_two():
    watcher, events = _build_watcher()
    watcher._emit(PickEvent(card_grpids=[1], pack_number=3, pick_number=0))
    assert events == []


def test_emit_drops_negative_pack():
    watcher, events = _build_watcher()
    watcher._emit(PackEvent(card_grpids=[1], pack_number=-1, pick_number=0))
    assert events == []


def test_emit_keeps_valid_pack_event():
    watcher, events = _build_watcher()
    watcher._emit(PackEvent(card_grpids=[1, 2, 3], pack_number=2, pick_number=13))
    assert len(events) == 1


def test_emit_keeps_events_without_pack_pick_attrs():
    """Non-pack events (DraftStart, DraftComplete) have no pack/pick
    fields and should pass through unconditionally."""
    watcher, events = _build_watcher()
    watcher._emit(DraftStartEvent(event_name="PremierDraft_SOS_20260601"))
    watcher._emit(DraftCompleteEvent())
    assert len(events) == 2
