"""Tests for client.overlay.draft_summary — pure pick-recap model."""

from __future__ import annotations

from client.overlay.draft_state import DraftState, PickHistoryEntry


def _pick(card: str, rank: int, score: float) -> dict:
    return {"card": card, "rank": rank, "score": score}


def _entry(pack, pick, picked_cards, picks):
    return PickHistoryEntry(
        pack_number=pack,
        pick_number=pick,
        picked_card=picked_cards[0] if picked_cards else "",
        picked_cards=list(picked_cards),
        picks=picks,
    )


def _state(arena_format="PremierDraft", recommend_count=1) -> DraftState:
    state = DraftState()
    state.set_code = "TMT"
    state.arena_format = arena_format
    state.recommend_count = recommend_count
    return state


def test_summary_basic_followed_and_ignored():
    state = _state()
    state.pool = ["Aether Bolt", "Useless Ogre"]
    state.pick_history[(0, 0)] = _entry(
        0, 0, ["Aether Bolt"],
        [_pick("Aether Bolt", 1, 0.9), _pick("Skywatcher", 2, 0.7)],
    )
    state.pick_history[(0, 1)] = _entry(
        0, 1, ["Useless Ogre"],
        [_pick("Skywatcher", 1, 0.8), _pick("Useless Ogre", 2, 0.5)],
    )

    from client.overlay.draft_summary import build_draft_summary

    summary = build_draft_summary(state)
    assert summary.set_code == "TMT"
    assert summary.arena_format == "PremierDraft"
    assert summary.pool == ["Aether Bolt", "Useless Ogre"]
    assert len(summary.rows) == 2

    first, second = summary.rows
    assert first.followed_recommendation is True
    assert first.top_recommendation == "Aether Bolt"
    assert second.followed_recommendation is False
    assert summary.picks_made == 2
    assert summary.recommendations_followed == 1


def test_summary_rows_sorted_by_pack_then_pick():
    state = _state()
    state.pick_history[(1, 0)] = _entry(1, 0, ["B"], [_pick("B", 1, 0.9)])
    state.pick_history[(0, 3)] = _entry(0, 3, ["A"], [_pick("A", 1, 0.9)])

    from client.overlay.draft_summary import build_draft_summary

    rows = build_draft_summary(state).rows
    assert [(r.pack_number, r.pick_number) for r in rows] == [(0, 3), (1, 0)]


def test_summary_replay_rows_have_no_recommendation_verdict():
    # Replay-only entries carry score 0.0 picks — no prediction was made.
    state = _state()
    state.pick_history[(0, 0)] = _entry(
        0, 0, ["Aether Bolt"],
        [_pick("Aether Bolt", 0, 0.0), _pick("Skywatcher", 0, 0.0)],
    )

    from client.overlay.draft_summary import build_draft_summary

    row = build_draft_summary(state).rows[0]
    assert row.followed_recommendation is None
    assert build_draft_summary(state).recommendations_followed == 0


def test_summary_missing_pick_has_no_verdict():
    state = _state()
    state.pick_history[(0, 0)] = _entry(0, 0, [], [_pick("A", 1, 0.9)])

    from client.overlay.draft_summary import build_draft_summary

    summary = build_draft_summary(state)
    assert summary.rows[0].followed_recommendation is None
    assert summary.picks_made == 0


def test_summary_picktwo_uses_recommend_count_and_both_cards():
    state = _state(arena_format="PickTwo", recommend_count=2)
    picks = [
        _pick("Best", 1, 0.9),
        _pick("Second", 2, 0.8),
        _pick("Third", 3, 0.4),
    ]
    # Both picked cards within the top-2 recommendations → followed.
    state.pick_history[(0, 0)] = _entry(0, 0, ["Best", "Second"], picks)
    # One of the two picks outside the top-2 → not followed.
    state.pick_history[(0, 1)] = _entry(0, 1, ["Best", "Third"], picks)

    from client.overlay.draft_summary import build_draft_summary

    rows = build_draft_summary(state).rows
    assert rows[0].picked_cards == ["Best", "Second"]
    assert rows[0].followed_recommendation is True
    assert rows[1].followed_recommendation is False


def test_summary_legacy_picked_card_fallback():
    # Older history entries (pre-PickTwo) may have picked_card set but
    # picked_cards empty.
    state = _state()
    state.pick_history[(0, 0)] = PickHistoryEntry(
        pack_number=0, pick_number=0, picked_card="Aether Bolt",
        picks=[_pick("Aether Bolt", 1, 0.9)],
    )

    from client.overlay.draft_summary import build_draft_summary

    row = build_draft_summary(state).rows[0]
    assert row.picked_cards == ["Aether Bolt"]
    assert row.followed_recommendation is True
