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

# Splash policy: at most this many cards in a 3rd colour, and only when
# the pool already contains a fixing source for that splash colour.
MAX_SPLASH_CARDS = 2


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


def _has_splash_fixing(
    splash_color: str,
    nonbasic_lands: list[str],
    scryfall_cards: dict[str, ScryfallCard],
) -> bool:
    """Return *True* iff the pool's nonbasic lands fix mana for *splash_color*.

    Recognised sources:
    - Universal fixers: any land whose oracle text says
      ``"add one mana of any"`` or ``"search your library for a basic"``.
    - Lands whose color identity contains the splash colour
      (single- or multi-colour duals that produce the splash colour).
    """
    for name in nonbasic_lands:
        sc = scryfall_cards.get(name)
        if not sc:
            continue
        text = sc.oracle_text.lower()
        if "add one mana of any" in text:
            return True
        if "search your library for a basic" in text:
            return True
        if splash_color in set(sc.color_identity):
            return True
    return False


def _select_splashes(
    deck_colors: list[str],
    main_deck: list[str],
    pool_names: list[str],
    scryfall_cards: dict[str, ScryfallCard],
    nonbasic_lands: list[str],
    card_map: dict[str, CardRatings] | None,
    set_metrics: SetMetrics | None,
    archetype_key: str,
    splashable_colors: list[str],
    max_splash: int = MAX_SPLASH_CARDS,
) -> list[tuple[str, ScryfallCard, str, float]]:
    """Pick up to *max_splash* cards from a 3rd colour that beat the weakest main-deck slots.

    A splash candidate must:
    - Have colour identity ⊆ deck_colors ∪ {splash_color}
    - Have at least one pip in *splash_color* (no free splashes)
    - Be in the pool but not already in main_deck
    - Have a fixing source for *splash_color* present in *nonbasic_lands*

    Candidates are ranked by :func:`_card_power`; only those strictly stronger
    than the weakest main-deck card they would displace are returned.
    Returns ``[]`` when no candidates qualify or no 17Lands data is available.
    """
    if not splashable_colors or not card_map or not set_metrics:
        return []

    # Power of each main_deck card, ascending — used for displacement check.
    main_powers = sorted(
        _card_power(n, card_map, set_metrics, archetype_key, 22)
        for n in main_deck
    )

    seen: set[str] = set(main_deck)
    candidates: list[tuple[str, ScryfallCard, str, float]] = []
    for splash_color in splashable_colors:
        if not _has_splash_fixing(splash_color, nonbasic_lands, scryfall_cards):
            continue
        for name in pool_names:
            if name in seen:
                continue
            sc = scryfall_cards.get(name)
            if not sc:
                continue
            tl = sc.type_line.lower()
            if "land" in tl and "creature" not in tl:
                continue
            ci = set(sc.color_identity)
            allowed = set(deck_colors) | {splash_color}
            if not ci.issubset(allowed):
                continue
            if splash_color not in ci:
                continue  # not actually using the splash colour
            power = _card_power(name, card_map, set_metrics, archetype_key, 22)
            candidates.append((name, sc, splash_color, power))
            seen.add(name)

    candidates.sort(key=lambda x: x[3], reverse=True)

    selected: list[tuple[str, ScryfallCard, str, float]] = []
    for cand in candidates:
        if len(selected) >= max_splash:
            break
        # The (len(selected)+1)-th weakest is the one displaced if we accept.
        if len(selected) < len(main_powers):
            if cand[3] > main_powers[len(selected)]:
                selected.append(cand)
        else:
            selected.append(cand)
    return selected


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
    base = max(0.0, min(100.0, 75.0 + z * 12.0))

    # Playability penalty: an archetype that can only field 17 castable
    # spells is unplayable even when those spells are individually strong
    # (the remaining 23 slots get filled with lands). Scale by the share
    # of TARGET_SPELLS actually present so a complete-but-mediocre deck
    # outranks an incomplete-but-strong one.
    completeness = min(1.0, len(deck_names) / TARGET_SPELLS)
    return base * completeness


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

    def _make_pairs(colors: list[str]) -> list[tuple[str, str]]:
        return [
            (colors[i], colors[j])
            for i in range(len(colors))
            for j in range(i + 1, len(colors))
        ]

    pairs = _make_pairs(top)

    # _try_pairs() runs the build loop with a given pair list + min-spells
    # threshold. Returns whatever archetypes produced a deck. Called twice
    # so that a fragmented pool (no top-3 pair has enough castable cards)
    # still gets *some* recommendation via the all-5-colour fallback below.
    results: dict[str, DeckSuggestion] = {}
    pairs_to_try = pairs
    min_spells_threshold = 10  # was 15 — completeness penalty handles thin decks
    fallback_attempted = False
    while True:
        results = _build_for_pairs(
            pool_names=pool_names,
            scryfall_cards=scryfall_cards,
            card_map=card_map,
            set_metrics=set_metrics,
            pairs=pairs_to_try,
            top_colors=top,
            min_spells_threshold=min_spells_threshold,
        )
        if results or fallback_attempted:
            break
        # No archetype passed the threshold — try every 2-colour pair with
        # a relaxed threshold so the user always gets *some* recommendation,
        # even on a 5-colour pool.
        pairs_to_try = _make_pairs(list(CARD_COLORS))
        min_spells_threshold = 0
        fallback_attempted = True
    ordered = dict(sorted(results.items(), key=lambda kv: kv[1].score, reverse=True))
    # Filter out archetypes scoring far below the top option — the UI lists
    # every suggestion in a dropdown, and a deck 20+ points behind the
    # leader is noise (typically a weird 4-splash build into a colour pair
    # the pool doesn't actually support). We always keep at least the
    # top suggestion so the dropdown is never empty.
    if not ordered:
        return ordered
    top_score = next(iter(ordered.values())).score
    return {k: v for k, v in ordered.items() if v.score >= top_score - 20.0}


def _build_for_pairs(
    *,
    pool_names: list[str],
    scryfall_cards: dict[str, ScryfallCard],
    card_map: dict[str, CardRatings] | None,
    set_metrics: SetMetrics | None,
    pairs: list[tuple[str, str]],
    top_colors: list[str],
    min_spells_threshold: int,
) -> dict[str, DeckSuggestion]:
    """Inner build loop, factored out so we can retry with a wider pair set."""
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

        if len(main_deck) < min_spells_threshold:
            continue  # too few playables — skip; completeness penalty
            # already discourages thin archetypes that *do* pass

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

        # Try splashing up to MAX_SPLASH_CARDS from a 3rd colour if the pool
        # has fixing for it and the splash beats a weaker main-deck card.
        # Splash is only attempted on full-or-near-full main decks — adding
        # a 3rd colour to a thin pile would make the mana base unworkable.
        # Thin decks (below the 18-spell mark) lean harder on splashes —
        # they have more empty slots to fill and the player likely has no
        # better option than reaching into a 3rd colour. Healthy decks
        # cap at the standard MAX_SPLASH_CARDS so a strong 2-colour build
        # doesn't get diluted by marginal splashes.
        splashable_colors = [c for c in top_colors if c not in deck_colors]
        # In the all-5-colours fallback we splash from any colour so a
        # 5-colour pool always gets *some* coherent build.
        if len(splashable_colors) == 0 and len(top_colors) <= 2:
            splashable_colors = [c for c in CARD_COLORS if c not in deck_colors]
        # Tiered splash cap: very thin pools genuinely need more splashes to
        # reach a playable spell count, even at the cost of a strained mana
        # base — better than 25 lands and no recommendation worth taking.
        if len(main_deck) < 14:
            deck_max_splash = 6
        elif len(main_deck) < 18:
            deck_max_splash = 4
        else:
            deck_max_splash = MAX_SPLASH_CARDS
        splashes = _select_splashes(
            deck_colors=deck_colors,
            main_deck=main_deck,
            pool_names=pool_names,
            scryfall_cards=scryfall_cards,
            nonbasic_lands=nonbasic_lands,
            card_map=card_map,
            set_metrics=set_metrics,
            archetype_key=key,
            splashable_colors=splashable_colors,
            max_splash=deck_max_splash,
        ) if len(main_deck) >= 10 else []

        splash_colors: list[str] = []
        if splashes:
            # Displace the weakest main_deck cards to make room.
            powered = [
                (n, sc, mcmc, _card_power(n, card_map, set_metrics, key, 22))
                for n, sc, mcmc in zip(main_deck, main_deck_cards, main_deck_cmc)
            ]
            powered.sort(key=lambda x: x[3], reverse=True)  # strongest first
            keep_count = min(len(powered), TARGET_SPELLS - len(splashes))
            keep = powered[:keep_count]
            main_deck = [x[0] for x in keep]
            main_deck_cards = [x[1] for x in keep]
            main_deck_cmc = [x[2] for x in keep]
            for splash_name, splash_sc, splash_color, _ in splashes:
                main_deck.append(splash_name)
                main_deck_cards.append(splash_sc)
                main_deck_cmc.append(functional_cmc(splash_sc.cmc, splash_sc.oracle_text))
            splash_colors = sorted({s[2] for s in splashes})

        # Cap nonbasic lands to TARGET_LANDS so we don't exceed 40 cards.
        target_lands = 40 - len(main_deck)
        if len(nonbasic_lands) > target_lands:
            nonbasic_lands = nonbasic_lands[:target_lands]

        fixing_count = len(nonbasic_lands)
        mana_base_colors = list(deck_colors) + splash_colors
        lands = _karsten_mana_base(
            main_deck_cards, mana_base_colors, fixing_count, target_lands,
        )
        # Guarantee at least one basic per splash colour even if karsten's
        # pip allocation rounds to zero — without it, the splash card is
        # uncastable when the universal fixer hasn't been drawn.
        for splash_color in splash_colors:
            splash_basic = _BASIC_NAMES.get(splash_color, "Plains")
            if splash_basic not in lands and lands:
                lands[-1] = splash_basic
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

    return results
