"""Unit tests for MemoryWatcher snapshot diff and walker stubs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from unittest.mock import patch

from client.overlay.log_watcher import (
    DeckPoolDetectedEvent,
    DraftCompleteEvent,
    DraftEndEvent,
    DraftStartEvent,
    PackEvent,
    PickEvent,
)
from client.overlay.memory_watcher import (
    MemoryWatcher,
    _DraftSnapshot,
    _diff_snapshots,
)
from client.overlay.memory.walker import read_draft_state, read_deck_pool


@dataclass
class _StubSession:
    """Minimal session stand-in — only ``image`` attribute is read."""
    image: Any = None

    def ensure_attached(self) -> bool:  # noqa: D401 — matches MemorySession API
        return True


def test_read_draft_state_none_when_image_unavailable():
    """No memory image attached → returns None (caller treats as not in draft)."""
    assert read_draft_state(_StubSession()) is None


def test_read_deck_pool_none_when_image_unavailable():
    """Same null guard as read_draft_state."""
    assert read_deck_pool(_StubSession()) is None


def test_memory_watcher_emits_deck_pool_event_on_first_detection():
    """When read_draft_state is None but read_deck_pool returns a pool,
    the watcher emits a single DeckPoolDetectedEvent and stops firing on
    subsequent ticks while the pool stays the same."""
    mw = MemoryWatcher()
    events: list = []
    mw.add_callback(lambda e: events.append(e))

    session = _StubSession()

    deck_payload = {"event_name": "QuickDraft_EOE_20260511",
                    "card_pool": [96808, 96827, 96778]}

    with patch("client.overlay.memory_watcher.read_draft_state", return_value=None), \
         patch("client.overlay.memory_watcher.read_deck_pool",
               return_value=deck_payload):
        # The watcher's _tick takes a session argument; pass a stub.
        mw._tick(session)
        mw._tick(session)
        mw._tick(session)

    deck_events = [e for e in events if isinstance(e, DeckPoolDetectedEvent)]
    assert len(deck_events) == 1
    assert deck_events[0].card_grpids == [96808, 96827, 96778]
    assert deck_events[0].event_name == "QuickDraft_EOE_20260511"


def test_memory_watcher_re_emits_deck_pool_when_pool_changes():
    """A different draft (new fingerprint) re-emits after we leave the
    deck-builder back into a draft view and return."""
    mw = MemoryWatcher()
    events: list = []
    mw.add_callback(lambda e: events.append(e))
    session = _StubSession()

    first = {"event_name": "QuickDraft_EOE_20260511", "card_pool": [1, 2, 3]}
    second = {"event_name": "PremierDraft_SOS_20260601", "card_pool": [4, 5, 6]}

    with patch("client.overlay.memory_watcher.read_draft_state", return_value=None):
        with patch("client.overlay.memory_watcher.read_deck_pool",
                   return_value=first):
            mw._tick(session)
            mw._tick(session)
        with patch("client.overlay.memory_watcher.read_deck_pool",
                   return_value=second):
            mw._tick(session)

    deck_events = [e for e in events if isinstance(e, DeckPoolDetectedEvent)]
    assert len(deck_events) == 2
    assert deck_events[0].event_name == "QuickDraft_EOE_20260511"
    assert deck_events[1].event_name == "PremierDraft_SOS_20260601"


def test_memory_watcher_does_not_emit_deck_pool_during_active_draft():
    """While a draft is active (read_draft_state returns data), the
    deck-pool path is skipped and the fingerprint is reset so the
    transition into deck-builder afterwards still fires."""
    mw = MemoryWatcher()
    events: list = []
    mw.add_callback(lambda e: events.append(e))
    session = _StubSession()

    active_draft = {
        "is_active": True, "event_name": "QuickDraft_EOE_20260511",
        "pack_number": 0, "pick_number": 0,
        "current_pack": [96667], "picked_cards": [],
    }
    deck_payload = {"event_name": "QuickDraft_EOE_20260511",
                    "card_pool": [96808, 96827]}

    # Tick during active draft — no deck-pool emission.
    with patch("client.overlay.memory_watcher.read_draft_state",
               return_value=active_draft), \
         patch("client.overlay.memory_watcher.read_deck_pool",
               return_value=deck_payload):
        mw._tick(session)
    # Transition to deck-builder — read_draft_state returns None now.
    with patch("client.overlay.memory_watcher.read_draft_state",
               return_value=None), \
         patch("client.overlay.memory_watcher.read_deck_pool",
               return_value=deck_payload):
        mw._tick(session)

    deck_events = [e for e in events if isinstance(e, DeckPoolDetectedEvent)]
    assert len(deck_events) == 1
    assert deck_events[0].card_grpids == [96808, 96827]


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


def test_diff_drops_phantom_pack_above_two():
    """Pack indices in a 3-pack draft are 0..2. Frames with pack_number=3
    (or higher) are phantom — they pollute the DB with "Pack 4" rows.
    The diff should suppress events from these frames."""
    prev = _snapshot(pack_number=2, pick_number=10)
    curr = _snapshot(pack_number=3, pick_number=0, current_pack=(1, 2, 3))
    assert _diff_snapshots(prev, curr) == []


def test_diff_drops_phantom_pick_above_fourteen():
    """A pack with pick_numbers above the legitimate range is phantom.

    With 15-card boosters in rotation, 1-indexed ``pick=15`` with one
    card left is a valid last-pick (total=16, normalised to (0,14)).
    Use ``len=2`` here so total=17 doesn't match any known pack-size
    invariant — that's the unambiguous phantom signal we want to drop.
    """
    prev = _snapshot(pack_number=1, pick_number=14)
    curr = _snapshot(pack_number=1, pick_number=15, current_pack=(1, 2))
    assert _diff_snapshots(prev, curr) == []


def test_diff_drops_negative_position():
    """Negative pack/pick = read failure (e.g. memory walk returned -1).
    Don't emit events from sentinel positions."""
    prev = _snapshot(pack_number=0, pick_number=0)
    curr = _snapshot(pack_number=-1, pick_number=0, current_pack=(1, 2, 3))
    assert _diff_snapshots(prev, curr) == []


# ---------------------------------------------------------------------------
# 1-indexed snapshot normalization
#
# Arena's ``_currentPack`` / ``_currentPick`` are 1-indexed for human drafts
# on some game versions (see commit 04214b7 in this submodule). The walker
# returns the raw values, so _diff_snapshots must normalize before emitting.
# Detection uses the pack-size invariant: an unopened pack has
# len(current_pack) + pick_number == pack_size (14 for Arena standard) in
# 0-indexed, or pack_size + 1 in 1-indexed.
# ---------------------------------------------------------------------------


def test_diff_normalizes_first_pick_of_premier_draft():
    """1-indexed start: pack=1, pick=1, full 14-card pack."""
    fourteen = tuple(range(101, 115))  # 14 grpIds
    curr = _snapshot(
        pack_number=1, pick_number=1,
        current_pack=fourteen, picked_cards=(),
    )
    events = _diff_snapshots(None, curr)
    pack_events = [e for e in events if isinstance(e, PackEvent)]
    assert len(pack_events) == 1
    assert pack_events[0].pack_number == 0
    assert pack_events[0].pick_number == 0


def test_diff_normalizes_final_pack_first_pick():
    """Pack 3 of a Premier draft (1-indexed) → pack 2 emitted.

    Pre-normalization this frame is dropped by the envelope clamp
    (pack > 2), which is what made Premier-draft pack 3 disappear.
    """
    fourteen = tuple(range(301, 315))
    prev = _snapshot(
        pack_number=2, pick_number=14, current_pack=(199,), picked_cards=(),
    )
    curr = _snapshot(
        pack_number=3, pick_number=1, current_pack=fourteen, picked_cards=(199,),
    )
    events = _diff_snapshots(prev, curr)
    pack_events = [e for e in events if isinstance(e, PackEvent)]
    assert len(pack_events) == 1
    assert pack_events[0].pack_number == 2
    assert pack_events[0].pick_number == 0


def test_diff_keeps_zero_indexed_snapshot_unchanged():
    """0-indexed snapshots (bot draft / Quick Draft via memory) must pass
    through without shift. Invariant: len(current_pack) + pick == 14."""
    fourteen = tuple(range(101, 115))
    curr = _snapshot(
        pack_number=0, pick_number=0,
        current_pack=fourteen, picked_cards=(),
    )
    events = _diff_snapshots(None, curr)
    pack_events = [e for e in events if isinstance(e, PackEvent)]
    assert len(pack_events) == 1
    assert pack_events[0].pack_number == 0
    assert pack_events[0].pick_number == 0


def test_diff_normalizes_mid_pack_one_indexed():
    """1-indexed mid-pack frame: pick=6 with 9 cards remaining
    (sum=15) is the 6th pick of a 14-card pack."""
    nine = tuple(range(201, 210))
    curr = _snapshot(
        pack_number=1, pick_number=6, current_pack=nine, picked_cards=(11, 12, 13, 14, 15),
    )
    events = _diff_snapshots(None, curr)
    pack_events = [e for e in events if isinstance(e, PackEvent)]
    assert len(pack_events) == 1
    assert pack_events[0].pack_number == 0
    assert pack_events[0].pick_number == 5


def test_diff_emits_pick_event_with_normalized_prev_position():
    """When a pick lands, the emitted PickEvent uses the PREVIOUS
    snapshot's position; if prev was 1-indexed it must be shifted too."""
    fourteen = tuple(range(101, 115))
    prev = _snapshot(
        pack_number=1, pick_number=1, current_pack=fourteen, picked_cards=(),
    )
    curr = _snapshot(
        pack_number=1, pick_number=2,
        current_pack=fourteen[1:],
        picked_cards=(101,),
    )
    events = _diff_snapshots(prev, curr)
    pick_events = [e for e in events if isinstance(e, PickEvent)]
    assert len(pick_events) == 1
    # Previous (1.1) → normalized (0.0).
    assert pick_events[0].pack_number == 0
    assert pick_events[0].pick_number == 0


def test_diff_normalizes_first_pick_of_15_card_booster():
    """1-indexed start with a 15-card booster: pack=1, pick=1, full 15-card pack.

    Newer Premier Draft sets ship 15-card packs. At 1-indexed P1P1,
    len(current_pack) + pick_number = 15 + 1 = 16, which is one above
    pack_size for a 15-card pack. The detector must recognise this as
    a 1-indexed snapshot and shift to (0, 0); otherwise the overlay
    silently displays P2P2 at the very first pick.
    """
    fifteen = tuple(range(101, 116))  # 15 grpIds
    curr = _snapshot(
        pack_number=1, pick_number=1,
        current_pack=fifteen, picked_cards=(),
    )
    events = _diff_snapshots(None, curr)
    pack_events = [e for e in events if isinstance(e, PackEvent)]
    assert len(pack_events) == 1
    assert pack_events[0].pack_number == 0
    assert pack_events[0].pick_number == 0


def test_diff_zero_indexed_15_card_first_pick_unchanged():
    """0-indexed 15-card P1P1: pack=0, pick=0, len=15.

    total = 15 matches `pack_size+1` for a 14-card pack (1-indexed signal)
    but the explicit zero in pack/pick proves it's 0-indexed — Arena's
    1-indexed scheme never emits zeros. The frame must pass through
    unchanged, not get spuriously shifted to (-1, -1).
    """
    fifteen = tuple(range(201, 216))
    curr = _snapshot(
        pack_number=0, pick_number=0,
        current_pack=fifteen, picked_cards=(),
    )
    events = _diff_snapshots(None, curr)
    pack_events = [e for e in events if isinstance(e, PackEvent)]
    assert len(pack_events) == 1
    assert pack_events[0].pack_number == 0
    assert pack_events[0].pick_number == 0


def test_diff_normalizes_mid_pack_one_indexed_15_card():
    """1-indexed mid-pack 15-card: pick=6 with 10 cards left → total=16."""
    ten = tuple(range(301, 311))
    curr = _snapshot(
        pack_number=1, pick_number=6, current_pack=ten,
        picked_cards=(11, 12, 13, 14, 15),
    )
    events = _diff_snapshots(None, curr)
    pack_events = [e for e in events if isinstance(e, PackEvent)]
    assert len(pack_events) == 1
    assert pack_events[0].pack_number == 0
    assert pack_events[0].pick_number == 5


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


def test_lookup_stats_falls_back_to_front_face_for_split_cards():
    """17Lands keys split cards (SOS Prepared, etc.) by their front face only
    ("Harmonized Trio"), while Arena/Scryfall and the predict request use the
    full name ("Harmonized Trio // Brainstorm"). Without a front-face fallback
    the predict response returns gihwr=ata=iwd=0.0 for every split card and
    the UI renders blank stats columns."""
    mgr = _make_mgr_with_bundles({
        ("SOS", "PremierDraft"): _StubBundle({
            "Harmonized Trio": _StubCardRatings(
                {"gihwr": 0.534, "ata": 4.23, "iwd": 0.05},
            ),
        }),
    })
    stats, source = mgr.lookup_stats(
        "SOS", "Harmonized Trio // Brainstorm", formats=["PremierDraft"],
    )
    assert source == "PremierDraft"
    assert stats["gihwr"] == 0.534
    assert stats["ata"] == 4.23


def test_lookup_stats_split_card_prefers_full_name_when_both_present():
    """If 17Lands ever started keying by full name too, the full-name entry
    must win — the front-face fallback is a fallback, not a rewrite."""
    mgr = _make_mgr_with_bundles({
        ("SOS", "PremierDraft"): _StubBundle({
            "Harmonized Trio // Brainstorm": _StubCardRatings(
                {"gihwr": 0.60, "ata": 3.0},
            ),
            "Harmonized Trio": _StubCardRatings(
                {"gihwr": 0.50, "ata": 5.0},
            ),
        }),
    })
    stats, source = mgr.lookup_stats(
        "SOS", "Harmonized Trio // Brainstorm", formats=["PremierDraft"],
    )
    assert source == "PremierDraft"
    assert stats["gihwr"] == 0.60


def test_lookup_stats_split_card_misses_when_neither_form_present():
    """A split-named card whose front face also isn't in 17Lands stays empty —
    no spurious match against an unrelated card."""
    mgr = _make_mgr_with_bundles({
        ("SOS", "PremierDraft"): _StubBundle({
            "Some Other Card": _StubCardRatings({"gihwr": 0.55, "ata": 4.0}),
        }),
    })
    stats, source = mgr.lookup_stats(
        "SOS", "Harmonized Trio // Brainstorm", formats=["PremierDraft"],
    )
    assert stats == {}
    assert source == ""


def test_get_card_ratings_resolves_split_card_front_face():
    """Server-side colors lookup goes through get_card_ratings rather than
    a raw card_map.get(); both predict and signals routes need the same
    front-face fallback for SOS Prepared cards."""
    cr = _StubCardRatings({"gihwr": 0.534, "ata": 4.23})
    cr.colors = ["U"]
    mgr = _make_mgr_with_bundles({
        ("SOS", "PremierDraft"): _StubBundle({"Harmonized Trio": cr}),
    })
    got = mgr.get_card_ratings(
        "SOS", "Harmonized Trio // Brainstorm", draft_format="PremierDraft",
    )
    assert got is cr
    assert got.colors == ["U"]


def test_get_card_ratings_returns_none_for_unknown_card():
    mgr = _make_mgr_with_bundles({
        ("SOS", "PremierDraft"): _StubBundle({}),
    })
    assert mgr.get_card_ratings(
        "SOS", "Harmonized Trio // Brainstorm", draft_format="PremierDraft",
    ) is None


def test_reset_deck_pool_fingerprint_clears_dedup_state():
    """After reset, the next poll must re-fire DeckPoolDetectedEvent even
    for the same Arena deck-pool the watcher just emitted on."""
    mw = MemoryWatcher()
    mw._previous_deck_pool = ("QuickDraft_EOE_20260511", (1, 2, 3))
    mw.reset_deck_pool_fingerprint()
    assert mw._previous_deck_pool is None
