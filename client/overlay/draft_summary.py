"""Draft-end summary model (pure, no Qt) — pick recap + follow rate."""

from __future__ import annotations

from dataclasses import dataclass, field

from client.overlay.draft_state import DraftState, PickHistoryEntry


@dataclass(frozen=True)
class PickRecapRow:
    """One pick coordinate in the recap."""

    pack_number: int
    pick_number: int
    picked_cards: list[str]            # 1 normally, 2 for PickTwo
    top_recommendation: str            # best pick the model offered ("" if none)
    followed_recommendation: bool | None  # None when no prediction was recorded


@dataclass(frozen=True)
class DraftSummary:
    """Everything the summary tab renders."""

    set_code: str
    arena_format: str
    pool: list[str]
    rows: list[PickRecapRow] = field(default_factory=list)
    picks_made: int = 0
    recommendations_followed: int = 0


def _picked_cards(entry: PickHistoryEntry) -> list[str]:
    if entry.picked_cards:
        return list(entry.picked_cards)
    if entry.picked_card:
        return [entry.picked_card]
    return []


def build_draft_summary(state: DraftState) -> DraftSummary:
    """Build the recap from ``state.pick_history`` (PickTwo-aware).

    Replay-only history entries carry score-0 placeholder picks (no
    prediction was made for them); their rows get
    ``followed_recommendation = None`` and don't count toward the
    follow rate.
    """
    rows: list[PickRecapRow] = []
    followed_count = 0
    picks_made = 0
    recommend_count = max(1, state.recommend_count)

    for key in sorted(state.pick_history.keys()):
        entry = state.pick_history[key]
        picked = _picked_cards(entry)
        has_prediction = any(p.get("score", 0) > 0 for p in entry.picks)
        top_recommendation = (
            entry.picks[0].get("card", "") if has_prediction else ""
        )

        if picked:
            picks_made += 1

        followed: bool | None
        if not picked or not has_prediction:
            followed = None
        else:
            top_cards = {
                p.get("card", "") for p in entry.picks[:recommend_count]
            }
            followed = all(card in top_cards for card in picked)
            if followed:
                followed_count += 1

        rows.append(PickRecapRow(
            pack_number=entry.pack_number,
            pick_number=entry.pick_number,
            picked_cards=picked,
            top_recommendation=top_recommendation,
            followed_recommendation=followed,
        ))

    return DraftSummary(
        set_code=state.set_code,
        arena_format=state.arena_format,
        pool=list(state.pool),
        rows=rows,
        picks_made=picks_made,
        recommendations_followed=followed_count,
    )
