"""Deck construction engine — builds suggested 40-card decks from a pool."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from common.data.card_stats import SetMetrics, get_blended_wr
from common.data.seventeenlands import CardRatings
from common.inference.pool_analyzer import (
    CARD_COLORS,
    ScryfallCard,
    detect_tags,
    detect_top_colors,
    functional_cmc,
    parse_pips,
)

logger = logging.getLogger(__name__)

# Deck constraints.
TARGET_SPELLS = 23
TARGET_LANDS = 17
MIN_CREATURES = 9
MIN_NONCREATURES = 6


@dataclass
class DeckSuggestion:
    """A ready-to-play deck suggestion."""

    archetype: str
    main_deck: list[str] = field(default_factory=list)
    main_deck_cmc: list[int] = field(default_factory=list)
    lands: list[str] = field(default_factory=list)  # basic lands
    nonbasic_lands: list[str] = field(default_factory=list)  # fixing / utility
    score: float = 0.0
    creature_count: int = 0
    spell_count: int = 0
    land_count: int = 0
    avg_cmc: float = 0.0


def _card_power(
    name: str,
    card_map: dict[str, CardRatings] | None,
    set_metrics: SetMetrics | None,
    archetype: str,
    pick: int,
) -> float:
    """Score a pool card for inclusion in a specific archetype."""
    if not card_map or not set_metrics:
        return 0.0
    cr = card_map.get(name)
    if not cr:
        return 0.0
    return get_blended_wr(cr, archetype, pick, set_metrics)


def _is_castable(
    card: ScryfallCard,
    deck_colors: list[str],
) -> bool:
    """Check if a card is castable in a two-colour deck."""
    pips = parse_pips(card.mana_cost)
    for c in CARD_COLORS:
        if pips[c] > 0 and c not in deck_colors:
            return False
    return True


def _karsten_mana_base(
    deck_cards: list[ScryfallCard],
    deck_colors: list[str],
    fixing_lands: int,
    total_land_slots: int = TARGET_LANDS,
) -> list[str]:
    """Compute a basic land mana base using Karsten-style pip counting.

    Args:
        deck_cards: Scryfall data for cards in the main deck.
        deck_colors: The deck's two (or more) colours.
        fixing_lands: Number of dual/fixing lands already in pool.
        total_land_slots: Target total land count (default 17).

    Returns:
        List of basic land names to fill to *total_land_slots* total.
    """
    color_pips: dict[str, int] = {c: 0 for c in CARD_COLORS}
    for card in deck_cards:
        pips = parse_pips(card.mana_cost)
        for c in CARD_COLORS:
            color_pips[c] += pips[c]

    # Only track active colours.
    active = {c: color_pips[c] for c in deck_colors if color_pips.get(c, 0) > 0}
    if not active:
        # Fallback: split evenly.
        n = max(len(deck_colors), 1)
        per = total_land_slots // n
        return _basics_for(deck_colors, [per] * n, total_land_slots)

    total_active_pips = sum(active.values())
    if total_active_pips == 0:
        total_active_pips = 1

    basics_budget = max(0, total_land_slots - fixing_lands)
    allocation: dict[str, int] = {}
    for c in deck_colors:
        pips = active.get(c, 0)
        allocation[c] = max(0, round((pips / total_active_pips) * basics_budget))

    # Ensure sum == basics_budget.
    diff = basics_budget - sum(allocation.values())
    if diff != 0 and deck_colors:
        # Add/remove from the primary colour.
        primary = max(deck_colors, key=lambda c: active.get(c, 0))
        allocation[primary] = max(0, allocation.get(primary, 0) + diff)

    return _basics_for(list(allocation.keys()), list(allocation.values()), basics_budget)


_BASIC_NAMES = {"W": "Plains", "U": "Island", "B": "Swamp", "R": "Mountain", "G": "Forest"}


def _basics_for(colors: list[str], counts: list[int], budget: int) -> list[str]:
    lands: list[str] = []
    for c, n in zip(colors, counts):
        name = _BASIC_NAMES.get(c, "Plains")
        lands.extend([name] * n)
    # Pad / trim.
    while len(lands) < budget:
        if colors:
            lands.append(_BASIC_NAMES.get(colors[0], "Plains"))
        else:
            lands.append("Plains")
    return lands[:budget]


def _holistic_score(
    deck_names: list[str],
    card_map: dict[str, CardRatings] | None,
    set_metrics: SetMetrics | None,
    scryfall_cards: dict[str, ScryfallCard],
    archetype: str,
) -> float:
    """Compute a holistic power score (0-100) for a deck."""
    if not card_map or not set_metrics:
        return -1.0

    wrs: list[float] = []
    for name in deck_names:
        cr = card_map.get(name)
        if cr:
            ad = cr.deck_colors.get("All Decks", {})
            gihwr = ad.get("gihwr", 0.0)
            if gihwr > 0:
                wrs.append(gihwr)

    if not wrs:
        return -1.0

    import statistics
    avg_wr = statistics.mean(wrs)
    mean, std = set_metrics.get_metrics("All Decks", "gihwr")
    z = (avg_wr - mean) / std if std > 0 else 0.0
    return max(0.0, min(100.0, 75.0 + z * 12.0))


def suggest_decks(
    pool_names: list[str],
    scryfall_cards: dict[str, ScryfallCard],
    card_map: dict[str, CardRatings] | None = None,
    set_metrics: SetMetrics | None = None,
) -> dict[str, DeckSuggestion]:
    """Generate deck suggestions from a drafted pool.

    Tries each viable two-colour combination and returns the best options
    keyed by colour pair (e.g. ``"UB"``).

    Args:
        pool_names: Card names in the player's pool.
        scryfall_cards: Scryfall data lookup.
        card_map: Optional 17Lands data for power ranking.
        set_metrics: Optional set metrics for z-score calculation.

    Returns:
        Dict mapping archetype key → :class:`DeckSuggestion`, sorted by score.
    """
    # Determine which colours the pool supports.
    pip_totals: dict[str, int] = {c: 0 for c in CARD_COLORS}
    for name in pool_names:
        sc = scryfall_cards.get(name)
        if sc:
            pips = parse_pips(sc.mana_cost)
            for c in CARD_COLORS:
                pip_totals[c] += pips[c]

    top = detect_top_colors(pip_totals, n=3)
    if len(top) < 2:
        top = list(CARD_COLORS[:2])

    # Generate pairs from top colours.
    pairs: list[tuple[str, str]] = []
    for i in range(len(top)):
        for j in range(i + 1, len(top)):
            pairs.append((top[i], top[j]))

    results: dict[str, DeckSuggestion] = {}
    for c1, c2 in pairs:
        order = "WUBRG"
        deck_colors = sorted([c1, c2], key=lambda c: order.index(c))
        key = "".join(deck_colors)

        # Filter castable cards.
        castable: list[tuple[str, float, ScryfallCard]] = []
        for name in pool_names:
            sc = scryfall_cards.get(name)
            if not sc:
                continue
            if "land" in sc.type_line.lower() and "creature" not in sc.type_line.lower():
                continue  # handle lands separately
            if _is_castable(sc, deck_colors):
                power = _card_power(name, card_map, set_metrics, key, 22)
                castable.append((name, power, sc))

        # Sort by power descending.
        castable.sort(key=lambda x: x[1], reverse=True)

        # Take top TARGET_SPELLS spells.
        main_deck: list[str] = []
        main_deck_cmc: list[int] = []
        main_deck_cards: list[ScryfallCard] = []
        for name, _power, sc in castable[:TARGET_SPELLS]:
            main_deck.append(name)
            main_deck_cmc.append(functional_cmc(sc.cmc, sc.oracle_text))
            main_deck_cards.append(sc)

        if len(main_deck) < TARGET_SPELLS:
            if len(main_deck) < 15:
                continue  # too few playables to form any reasonable deck

        # Collect useful nonbasic lands from pool.
        # Rules (matching 17Lands reference):
        #  - Colorless utility lands (no color identity): always include.
        #  - Dual/multi-color lands: include only if ALL colors ⊆ deck_colors.
        #  - Universal fixers ("add one mana of any", fetch basics): always.
        #  - Single-color lands: include only if color ∈ deck_colors.
        nonbasic_lands: list[str] = []
        for name in pool_names:
            sc = scryfall_cards.get(name)
            if not sc:
                continue
            tl = sc.type_line.lower()
            if "land" not in tl or "creature" in tl:
                continue
            # Skip basic lands — those are generated by _karsten_mana_base.
            if name in _BASIC_NAMES.values():
                continue
            ci = set(sc.color_identity)
            text = sc.oracle_text.lower()
            is_universal = (
                "add one mana of any" in text
                or "search your library for a basic" in text
            )
            if is_universal:
                nonbasic_lands.append(name)
            elif not ci:
                # Colorless utility land — always include.
                nonbasic_lands.append(name)
            elif ci.issubset(set(deck_colors)):
                # All of the land's colors fit the deck.
                nonbasic_lands.append(name)

        # Cap nonbasic lands to TARGET_LANDS so we don't exceed 40 cards.
        target_lands = 40 - len(main_deck)
        if len(nonbasic_lands) > target_lands:
            nonbasic_lands = nonbasic_lands[:target_lands]

        fixing_count = len(nonbasic_lands)
        lands = _karsten_mana_base(main_deck_cards, deck_colors, fixing_count, target_lands)
        total_lands = len(nonbasic_lands) + len(lands)
        # Safety: ensure exactly target_lands total by trimming basics.
        if total_lands > target_lands:
            excess = total_lands - target_lands
            lands = lands[:-excess] if excess < len(lands) else []
        score = _holistic_score(main_deck, card_map, set_metrics, scryfall_cards, key)

        # Compute deck stats.
        creatures = sum(
            1 for n in main_deck
            if (sc := scryfall_cards.get(n)) and "creature" in sc.type_line.lower()
        )
        spells = len(main_deck) - creatures
        total_lands = len(lands) + len(nonbasic_lands)
        avg = sum(main_deck_cmc) / max(len(main_deck_cmc), 1)

        results[key] = DeckSuggestion(
            archetype=key,
            main_deck=main_deck,
            main_deck_cmc=main_deck_cmc,
            lands=lands,
            nonbasic_lands=nonbasic_lands,
            score=score,
            creature_count=creatures,
            spell_count=spells,
            land_count=total_lands,
            avg_cmc=round(avg, 2),
        )

    # Sort by score descending.
    return dict(sorted(results.items(), key=lambda kv: kv[1].score, reverse=True))
