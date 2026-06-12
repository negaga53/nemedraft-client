from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client.overlay.draft_state import DraftState, PickHistoryEntry, extract_set_code


def test_mwm_sos_cascade_bot_draft_maps_to_sos() -> None:
    assert extract_set_code("MWM_SOS_Cascade_BotDraft_20260609") == "SOS"


def test_sos_cascade_bot_draft_context_maps_to_sos() -> None:
    assert extract_set_code("SOS_Cascade_BotDraft") == "SOS"


def test_cas_bot_draft_aliases_to_sos() -> None:
    assert extract_set_code("CAS_BotDraft_20260609") == "SOS"


def test_bot_draft_date_is_not_treated_as_set_code() -> None:
    assert extract_set_code("MWM_Cascade_BotDraft_20260609") is None


# -- mid-draft-attach pick-history reconstruction ----------------------------
#
# When the overlay attaches while a draft is already in progress, only the
# CURRENT pack is observed (the MemoryWatcher's first poll, or a log whose
# early packs rotated out). The cumulative pool still tells us *which* cards
# were taken at each past pick, so the navigator can be rebuilt — full pack
# contents stay unavailable.


def _draft_at(pack: int, pick: int, pool: list[str]) -> DraftState:
    st = DraftState()
    st.on_draft_start("PremierDraft_FIN_20260601")  # cards_per_pick == 1
    st.pool = list(pool)
    st.pack_number = pack
    st.pick_number = pick
    return st


def test_reconstruct_history_pack_one_attach() -> None:
    """Attach at P1P8: pool holds the 7 prior picks → entries (0,0)..(0,6)."""
    st = _draft_at(0, 7, [f"P{i}" for i in range(7)])
    st.reconstruct_pick_history_from_pool()
    assert set(st.pick_history) == {(0, i) for i in range(7)}
    assert st.pick_history[(0, 3)].picked_card == "P3"
    assert st.pick_history[(0, 3)].picked_cards == ["P3"]


def test_reconstruct_history_later_pack_attach() -> None:
    """Attach at P2P3 with a 15-card pack-1 history derivable from position."""
    pool = [f"A{i}" for i in range(15)] + ["B0", "B1"]  # pack0 ×15 + pack1 ×2
    st = _draft_at(1, 2, pool)
    st.reconstruct_pick_history_from_pool()
    assert (0, 0) in st.pick_history and (0, 14) in st.pick_history
    assert (1, 0) in st.pick_history and (1, 1) in st.pick_history
    assert st.pick_history[(1, 1)].picked_card == "B1"
    # The current pick (1, 2) is not yet made — must not be fabricated.
    assert (1, 2) not in st.pick_history


def test_reconstruct_does_not_overwrite_scored_entries() -> None:
    """Live-observed entries (with full pack data) survive reconstruction."""
    st = _draft_at(0, 5, ["a", "b", "c", "d", "e"])
    st.pick_history[(0, 2)] = PickHistoryEntry(
        pack_number=0, pick_number=2, picked_card="REAL",
        picks=[{"card": "X", "score": 9.9}],
    )
    st.reconstruct_pick_history_from_pool()
    assert st.pick_history[(0, 2)].picked_card == "REAL"
    assert st.pick_history[(0, 2)].picks == [{"card": "X", "score": 9.9}]
    # The other past picks are still filled in.
    assert (0, 0) in st.pick_history and (0, 4) in st.pick_history


def test_reconstruct_noop_at_first_pick() -> None:
    """P1P1 with an empty pool adds nothing (no past picks exist)."""
    st = _draft_at(0, 0, [])
    st.reconstruct_pick_history_from_pool()
    assert st.pick_history == {}
