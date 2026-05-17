"""Unit tests for MemoryWatcher snapshot diff and walker stubs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from client.overlay.log_watcher import (
    DraftCompleteEvent,
    DraftEndEvent,
    DraftStartEvent,
    PackEvent,
    PickEvent,
)
from client.overlay.memory_watcher import _DraftSnapshot, _diff_snapshots
from client.overlay.memory.walker import read_draft_state


@dataclass
class _StubSession:
    """Minimal session stand-in — only ``image`` attribute is read."""
    image: Any = None


def test_read_draft_state_none_when_image_unavailable():
    """No memory image attached → returns None (caller treats as not in draft)."""
    assert read_draft_state(_StubSession()) is None


def _snapshot(**kw: Any) -> _DraftSnapshot:
    base = {
        "is_active": True,
        "event_name": "QuickDraft_EOE_20260511",
        "pack_number": 0,
        "pick_number": 0,
        "current_pack": (96667, 96760, 96823),
        "picked_cards": (),
    }
    base.update(kw)
    return _DraftSnapshot.from_payload(base)


def test_diff_emits_draft_start_when_inactive_becomes_active():
    events = _diff_snapshots(None, _snapshot())
    assert any(isinstance(e, DraftStartEvent) for e in events)
    assert any(isinstance(e, PackEvent) for e in events)
    pack_event = next(e for e in events if isinstance(e, PackEvent))
    assert pack_event.card_grpids == [96667, 96760, 96823]
    assert pack_event.pack_number == 0
    assert pack_event.pick_number == 0


def test_diff_emits_draft_end_when_active_becomes_inactive():
    prev = _snapshot()
    curr = _snapshot(is_active=False, current_pack=(), picked_cards=())
    events = _diff_snapshots(prev, curr)
    assert events == [DraftEndEvent()]


def test_diff_emits_pack_event_on_pack_change():
    prev = _snapshot()
    curr = _snapshot(
        pack_number=0,
        pick_number=1,
        current_pack=(96760, 96823),
        picked_cards=(96667,),
    )
    events = _diff_snapshots(prev, curr)
    pack_events = [e for e in events if isinstance(e, PackEvent)]
    pick_events = [e for e in events if isinstance(e, PickEvent)]
    assert len(pack_events) == 1
    assert pack_events[0].card_grpids == [96760, 96823]
    assert pack_events[0].pick_number == 1
    # Pick event reports the grpId added to picked_cards, tagged with the
    # PREVIOUS pack/pick (the pick that just resolved).
    assert len(pick_events) == 1
    assert pick_events[0].card_grpids == [96667]
    assert pick_events[0].pack_number == 0
    assert pick_events[0].pick_number == 0


def test_diff_emits_no_pack_event_when_unchanged():
    snap = _snapshot()
    assert _diff_snapshots(snap, snap) == []


def test_diff_emits_draft_complete_when_last_pack_empties():
    prev = _snapshot(pack_number=2, pick_number=13, current_pack=(99,))
    curr = _snapshot(pack_number=2, pick_number=13, current_pack=(),
                     picked_cards=(99,))
    events = _diff_snapshots(prev, curr)
    assert any(isinstance(e, DraftCompleteEvent) for e in events)


# ---------------------------------------------------------------------------
# SetDataManager.lookup_stats fallback ladder
# ---------------------------------------------------------------------------

class _StubBundle:
    """Minimal stand-in for ``_SetBundle`` — only ``card_map`` is read."""

    def __init__(self, card_map):
        self.card_map = card_map


class _StubCardRatings:
    def __init__(self, stats):
        self.deck_colors = {"All Decks": stats}


def _make_mgr_with_bundles(bundles):
    """Build a SetDataManager and inject pre-fabricated bundles."""
    from unittest.mock import patch
    from common.data.set_data_manager import SetDataManager

    # Patch __init__'s file-read so we don't need a card_id_map.
    with patch.object(SetDataManager, "__init__", lambda self: None):
        mgr = SetDataManager()  # type: ignore[call-arg]
    mgr._lock = __import__("threading").Lock()
    mgr._sets = bundles
    mgr._default_draft_format = "PremierDraft"
    return mgr


def test_lookup_stats_returns_primary_when_gihwr_present():
    mgr = _make_mgr_with_bundles({
        ("EOE", "QuickDraft"): _StubBundle({
            "Foo": _StubCardRatings({"gihwr": 0.55, "ata": 4.0, "iwd": 0.01}),
        }),
        ("EOE", "PremierDraft"): _StubBundle({
            "Foo": _StubCardRatings({"gihwr": 0.62, "ata": 3.0, "iwd": 0.05}),
        }),
    })
    stats, source = mgr.lookup_stats(
        "EOE", "Foo", formats=["QuickDraft", "PremierDraft"],
    )
    assert source == "QuickDraft"
    assert stats["gihwr"] == 0.55  # primary wins, no fallback


def test_lookup_stats_falls_back_when_primary_has_no_gihwr():
    mgr = _make_mgr_with_bundles({
        ("EOE", "QuickDraft"): _StubBundle({
            "Foo": _StubCardRatings({"gihwr": 0.0, "ata": 0.0, "iwd": 0.0}),
        }),
        ("EOE", "PremierDraft"): _StubBundle({
            "Foo": _StubCardRatings({"gihwr": 0.62, "ata": 3.0, "iwd": 0.05}),
        }),
    })
    stats, source = mgr.lookup_stats(
        "EOE", "Foo", formats=["QuickDraft", "PremierDraft"],
    )
    assert source == "PremierDraft"
    assert stats["gihwr"] == 0.62


def test_lookup_stats_returns_ata_only_when_no_gihwr_anywhere():
    """Card with ATA but no GIHWR in primary stays in primary — no
    cross-format mixing of ATA values."""
    mgr = _make_mgr_with_bundles({
        ("EOE", "QuickDraft"): _StubBundle({
            "Foo": _StubCardRatings({"gihwr": 0.0, "ata": 11.08, "iwd": 0.0}),
        }),
        ("EOE", "PremierDraft"): _StubBundle({
            "Foo": _StubCardRatings({"gihwr": 0.0, "ata": 0.0, "iwd": 0.0}),
        }),
    })
    stats, source = mgr.lookup_stats(
        "EOE", "Foo", formats=["QuickDraft", "PremierDraft"],
    )
    assert source == "QuickDraft"
    assert stats["gihwr"] == 0.0
    assert stats["ata"] == 11.08


def test_lookup_stats_returns_empty_when_card_missing_everywhere():
    mgr = _make_mgr_with_bundles({
        ("EOE", "QuickDraft"): _StubBundle({}),
        ("EOE", "PremierDraft"): _StubBundle({}),
    })
    stats, source = mgr.lookup_stats(
        "EOE", "Foo", formats=["QuickDraft", "PremierDraft"],
    )
    assert stats == {}
    assert source == ""


def test_lookup_stats_skips_format_with_no_bundle():
    mgr = _make_mgr_with_bundles({
        ("EOE", "PremierDraft"): _StubBundle({
            "Foo": _StubCardRatings({"gihwr": 0.6, "ata": 3.0}),
        }),
    })
    # QuickDraft bundle not loaded — should skip silently to PremierDraft.
    stats, source = mgr.lookup_stats(
        "EOE", "Foo", formats=["QuickDraft", "PremierDraft"],
    )
    assert source == "PremierDraft"
    assert stats["gihwr"] == 0.6
