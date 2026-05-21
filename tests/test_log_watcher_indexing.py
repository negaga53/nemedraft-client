"""LogWatcher must emit 0-indexed pack/pick numbers regardless of the
Arena-log entry type that produced them.

Arena's log uses different indexing for different draft event types:

* ``DraftStatus=PickNext`` (bot draft / Quick Draft) — 0-indexed ``PackNumber``/``PickNumber``.
* ``Draft.Notify`` (human draft pack) — 1-indexed ``SelfPack``/``SelfPick``.
* ``LogBusinessEvents`` with ``PickGrpId`` (human draft combined) — 1-indexed ``PackNumber``/``PickNumber``.
* ``EventPlayerDraftMakePick`` (human draft pick) — 1-indexed ``Pack``/``Pick``.

The server, ``DraftState``, the UI history navigator and the prediction
cache key all assume 0-indexed values, so the three human-draft
handlers must subtract 1 before emitting. Without this normalization
Premier-draft sessions fragment into four DB drafts (pack 1, pack 2
prefix, two trailing single-pick drafts) and never reach pack 3 because
the envelope clamp drops 1-indexed ``pack=3`` frames.
"""

from __future__ import annotations

from client.overlay.log_watcher import (
    LogWatcher,
    PackEvent,
    PickEvent,
)


def _build_watcher() -> tuple[LogWatcher, list]:
    watcher = LogWatcher.__new__(LogWatcher)
    watcher._callbacks = []  # type: ignore[attr-defined]
    watcher._cur_draft_event = ""  # type: ignore[attr-defined]
    events: list = []
    watcher._callbacks.append(events.append)  # type: ignore[attr-defined]
    return watcher, events


# ---------------------------------------------------------------------------
# Bot draft (Quick Draft) — already 0-indexed, must stay 0-indexed.
# ---------------------------------------------------------------------------


def test_bot_draft_pack_keeps_zero_indexed_values():
    watcher, events = _build_watcher()
    watcher._handle_bot_draft_pack({
        "DraftStatus": "PickNext",
        "EventName": "QuickDraft_EOE_20260201",
        "DraftPack": ["111", "222", "333"],
        "PackNumber": 0,
        "PickNumber": 0,
        "PickedCards": [],
    })
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, PackEvent)
    assert ev.pack_number == 0
    assert ev.pick_number == 0


# ---------------------------------------------------------------------------
# Human draft (Premier Draft) — 1-indexed in Arena log, must normalize.
# ---------------------------------------------------------------------------


def test_human_draft_pack_normalizes_first_pick():
    """SelfPack=1, SelfPick=1 (Arena's first pick) → pack=0, pick=0."""
    watcher, events = _build_watcher()
    watcher._handle_human_draft_pack({
        "PackCards": "111,222,333",
        "SelfPack": 1,
        "SelfPick": 1,
    })
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, PackEvent)
    assert ev.pack_number == 0
    assert ev.pick_number == 0


def test_human_draft_pack_normalizes_final_pack_first_pick():
    """SelfPack=3, SelfPick=1 (start of last pack) → pack=2, pick=0.

    Pre-fix, pack=3 was silently dropped by the envelope clamp in
    ``_emit``, so the overlay lost Premier-draft pack-3 support entirely.
    """
    watcher, events = _build_watcher()
    watcher._handle_human_draft_pack({
        "PackCards": "111,222,333",
        "SelfPack": 3,
        "SelfPick": 1,
    })
    assert len(events) == 1
    assert events[0].pack_number == 2
    assert events[0].pick_number == 0


def test_human_draft_combined_normalizes_both_events():
    """LogBusinessEvents emits a PackEvent and a PickEvent for the same
    (pack, pick); both must be shifted."""
    watcher, events = _build_watcher()
    watcher._handle_human_draft_combined({
        "EventId": "PremierDraft_SOS_20260601",
        "CardsInPack": [111, 222, 333],
        "PackNumber": 2,
        "PickNumber": 14,
        "PickGrpId": 111,
    })
    pack_events = [e for e in events if isinstance(e, PackEvent)]
    pick_events = [e for e in events if isinstance(e, PickEvent)]
    assert len(pack_events) == 1
    assert len(pick_events) == 1
    assert pack_events[0].pack_number == 1
    assert pack_events[0].pick_number == 13
    assert pick_events[0].pack_number == 1
    assert pick_events[0].pick_number == 13


def test_human_draft_combined_zero_pick_grpid_suppresses_pick_event():
    """A combined entry with PickGrpId=0 means the pick hasn't been made yet
    — only the PackEvent should fire."""
    watcher, events = _build_watcher()
    watcher._handle_human_draft_combined({
        "EventId": "PremierDraft_SOS_20260601",
        "CardsInPack": [111, 222],
        "PackNumber": 2,
        "PickNumber": 1,
        "PickGrpId": 0,
    })
    assert all(isinstance(e, PackEvent) for e in events)
    assert len(events) == 1
    assert events[0].pack_number == 1
    assert events[0].pick_number == 0


def test_player_draft_pick_normalizes():
    """EventPlayerDraftMakePick uses 1-indexed Pack/Pick."""
    watcher, events = _build_watcher()
    watcher._handle_player_draft_pick({
        "GrpIds": [111],
        "Pack": 2,
        "Pick": 5,
    })
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, PickEvent)
    assert ev.pack_number == 1
    assert ev.pick_number == 4


def test_player_draft_pick_missing_fields_drops_event():
    """Missing Pack/Pick keys keep the existing sentinel of -1 (i.e. don't
    shift to -2). The envelope clamp in ``_emit`` then drops the event,
    matching pre-existing behaviour."""
    watcher, events = _build_watcher()
    watcher._handle_player_draft_pick({
        "GrpIds": [111],
    })
    assert events == []
