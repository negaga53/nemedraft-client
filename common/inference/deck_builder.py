"""Deck construction engine — builds suggested 40-card decks from a pool."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from common.data.card_stats import SetMetrics, get_blended_wr
from common.data.seventeenlands import CardRatings
from common.data.trophy_deck_prior import TrophyDeckPrior
from common.inference.pool_analyzer import (
    CARD_COLORS,
    ScryfallCard,
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

# The trophy prior is a light nudge, not a replacement for 17Lands GIH WR.
TROPHY_CARD_POWER_BONUS = 0.015
TROPHY_DECK_SCORE_BONUS = 8.0
# Fallback power for cards absent from 17Lands stats but present in
# trophy decks. The range stays strictly below baseline_wr so an unrated
# card never outranks a rated card sitting at the format mean — only
# very weakly-rated cards (well below baseline) lose to it.
_TROPHY_FALLBACK_BASE_PENALTY = 0.10
_TROPHY_FALLBACK_SPAN = 0.05

# Alternative archetypes with fewer playables than this are dropped from
# the dropdown — they fill out as 35+ lands and are noise next to a real
# build. The top suggestion is kept regardless so the UI is never empty.
_PLAYABLE_FLOOR = 14


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


def _card_map_get(
    card_map: dict[str, CardRatings] | None,
    name: str,
) -> CardRatings | None:
    """Look up a card in the 17Lands card_map, falling back to its front face.

    17Lands keys split-named cards (e.g. SOS Prepared) by the front face only
    ("Scheming Silvertongue"), while Scryfall and Arena use the full name
    ("Scheming Silvertongue // Sign in Blood"). Without this fallback every
    split card falls through to trophy_fallback_power and gets silently
    sideboarded. Mirrors the inverse direction in
    ``common/data/trophy_deck_prior.py::_NameResolver``.
    """
    if not card_map:
        return None
    cr = card_map.get(name)
    if cr is not None:
        return cr
    if " // " in name:
        return card_map.get(name.split(" // ", 1)[0])
    return None


def _card_power(
    name: str,
    card_map: dict[str, CardRatings] | None,
    set_metrics: SetMetrics | None,
    archetype: str,
    pick: int,
    trophy_prior: TrophyDeckPrior | None = None,
) -> float:
    """Score a pool card for inclusion in a specific archetype."""
    trophy_card_score = 0.0
    if trophy_prior:
        trophy_card_score = trophy_prior.card_bonus(name, archetype)
    trophy_bonus = trophy_card_score * TROPHY_CARD_POWER_BONUS
    if not card_map or not set_metrics:
        return _trophy_fallback_power(set_metrics, trophy_card_score) or trophy_bonus
    cr = _card_map_get(card_map, name)
    if not cr:
        return _trophy_fallback_power(set_metrics, trophy_card_score) or trophy_bonus
    blended = get_blended_wr(cr, archetype, pick, set_metrics)
    if blended <= 0.0:
        return _trophy_fallback_power(set_metrics, trophy_card_score) or trophy_bonus
    return blended + trophy_bonus


def _trophy_fallback_power(
    set_metrics: SetMetrics | None,
    trophy_card_score: float,
) -> float:
    """Estimate power for cards absent from 17Lands stats but present in trophies."""
    if trophy_card_score <= 0.0:
        return 0.0
    baseline = getattr(set_metrics, "baseline_wr", 0.54) if set_metrics else 0.54
    return baseline - _TROPHY_FALLBACK_BASE_PENALTY + (
        trophy_card_score * _TROPHY_FALLBACK_SPAN
    )


def _is_castable(
    card: ScryfallCard,
    deck_colors: list[str],
) -> bool:
    """Check if a card is castable in a two-colour deck.

    Split-card mana costs (e.g. SOS Prepared ``"{1}{B} // {B}{B}"`` or
    Bind // Liberate ``"{1}{G} // {1}{W}"``) carry two independent costs
    in a single string. Each face can be cast on its own, so the card is
    castable when *any* face is fully on-colour — not the union.
    """
    mc = card.mana_cost or ""
    faces = mc.split(" // ") if " // " in mc else [mc]
    deck_set = set(deck_colors)
    for face in faces:
        face_pips = parse_pips(face)
        if all(face_pips[c] == 0 for c in CARD_COLORS if c not in deck_set):
            return True
    return False


def _karsten_mana_base(
    deck_cards: list[ScryfallCard],
    deck_colors: list[str],
    fixing_lands: int,
    total_land_slots: int = TARGET_LANDS,
) -> list[str]:
    """Compute a basic-land mana base using Karsten source thresholds.

    Args:
        deck_cards: Scryfall data for cards in the main deck.
        deck_colors: The deck's two (or more) colours.
        fixing_lands: Count of dual/fixing lands already in the pool.
            Subtracted from the basics budget; per-color source credit
            is estimated by spreading fixing evenly across deck colors.
            The build loop calls :func:`_karsten_mana_base_with_fixing`
            instead so it can pass exact per-color fixing data.
        total_land_slots: Target total land count (default 17).

    Returns:
        List of basic land names to fill to ``total_land_slots`` total.
    """
    basics_budget = max(0, total_land_slots - fixing_lands)
    if basics_budget == 0:
        return []

    demand = _demand_per_color(deck_cards, deck_colors)
    n = max(len(deck_colors), 1)
    per_color_fixing_estimate = fixing_lands / n
    fixing_sources = {c: per_color_fixing_estimate for c in deck_colors}

    basics = _allocate_basics_for_demand(
        deck_colors=deck_colors,
        demand=demand,
        fixing_sources=fixing_sources,
        basics_budget=basics_budget,
    )
    return _basics_for(
        list(basics.keys()), list(basics.values()), basics_budget,
    )


def _karsten_mana_base_with_fixing(
    *,
    deck_cards: list[ScryfallCard],
    deck_colors: list[str],
    nonbasic_lands: list[str],
    scryfall_cards: dict[str, ScryfallCard],
    total_land_slots: int = TARGET_LANDS,
) -> tuple[list[str], dict[str, float]]:
    """Karsten mana base with exact per-color fixing accounting.

    Used by the build loop; returns the basics list and the computed
    per-color fixing source totals so the caller can run
    :func:`_is_feasible` against them without re-walking
    ``nonbasic_lands``.
    """
    fixing_sources = _fixing_sources_per_color(
        nonbasic_lands, scryfall_cards, deck_colors,
    )
    basics_budget = max(0, total_land_slots - len(nonbasic_lands))
    demand = _demand_per_color(deck_cards, deck_colors)
    basics_map = _allocate_basics_for_demand(
        deck_colors=deck_colors,
        demand=demand,
        fixing_sources=fixing_sources,
        basics_budget=basics_budget,
    )
    basics_list = _basics_for(
        list(basics_map.keys()), list(basics_map.values()), basics_budget,
    )
    return basics_list, fixing_sources


_BASIC_NAMES = {"W": "Plains", "U": "Island", "B": "Swamp", "R": "Mountain", "G": "Forest"}


# Karsten's 40-card limited mana-source table at ~90% reliability, on
# the play, no mulligan. Indexed [pips_in_color][functional_cmc].
# Source: Frank Karsten's 40-card limited supplemental tables.
# CMC > 8 clamps to CMC 8; values below the first listed CMC for a pip
# count (e.g. {C}{C} at CMC 1) are physically impossible and return a
# high sentinel so feasibility checks trip correctly.
_KARSTEN_SOURCES: dict[int, dict[int, int]] = {
    1: {1: 14, 2: 13, 3: 12, 4: 11, 5: 10, 6: 9, 7: 8, 8: 8},
    2: {2: 18, 3: 16, 4: 15, 5: 14, 6: 13, 7: 12, 8: 12},
    3: {3: 20, 4: 19, 5: 18, 6: 17, 7: 16, 8: 16},
}
# We target ~80% reliability (matching limited-pro practice) rather than
# Karsten's published 90% — the difference is one extra source per
# requirement, and 90% pushes too many decks into "demote second color".
_KARSTEN_RELIABILITY_DROP = 1


def _required_sources(pips_in_color: int, functional_cmc: int) -> int:
    """Karsten threshold: sources of a color needed to cast on curve.

    Args:
        pips_in_color: Pips of this color in the card's mana cost
            (clamped to [1, 3]). 0 returns 0.
        functional_cmc: The card's playable turn. Clamped to [1, 8].

    Returns:
        Number of sources of this color the deck needs to cast the
        card on curve ~80% of the time. A value ≥ 19 means the card
        is effectively unplayable in a 2-color 17-land deck.
    """
    if pips_in_color <= 0:
        return 0
    cmc = max(1, min(functional_cmc, 8))
    pips = min(pips_in_color, 3)
    row = _KARSTEN_SOURCES.get(pips, {})
    # Below the first listed CMC for this pip count → physically
    # impossible (e.g. {C}{C} at CMC 1). High sentinel keeps the demand
    # above any plausible source count.
    if cmc not in row:
        return 20
    return max(0, row[cmc] - _KARSTEN_RELIABILITY_DROP)


_FACE_PIP_RE = re.compile(r"\{([^}]+)\}")


def _face_cmc(face: str) -> int:
    """CMC of a single face from a split-card mana cost string.

    ``ScryfallCard.cmc`` reflects the whole card; for split cards we
    need each face's CMC independently. Hybrid and Phyrexian pips
    count as 1 mana, X spells contribute 0 (per Karsten convention).
    """
    total = 0
    for sym in _FACE_PIP_RE.findall(face):
        if sym.isdigit():
            total += int(sym)
        elif sym == "X":
            continue
        else:
            total += 1
    return total


def _demand_per_color(
    main_deck_cards: list[ScryfallCard],
    deck_colors: list[str],
) -> dict[str, int]:
    """Maximum Karsten source demand per color across the main deck.

    For each card, each face is evaluated independently (split cards):
    a face's demand is ``_required_sources(pips_in_color, face_cmc)``.
    Per color, the deck's demand is the maximum demand any card imposes,
    not the sum. By the time you'd want to cast the higher-demand
    spell, you've drawn enough lands to also cast anything cheaper of
    the same color.

    Returns:
        ``{color: max_demand}`` for every color in ``deck_colors`` with at
        least one demanding card. Colors with zero demand are omitted.
    """
    demand: dict[str, int] = {}
    for card in main_deck_cards:
        mc = card.mana_cost or ""
        faces = mc.split(" // ") if " // " in mc else [mc]
        oracle = card.oracle_text or ""
        for face in faces:
            face_pips = parse_pips(face)
            face_cmc_int = functional_cmc(_face_cmc(face), oracle)
            for c in deck_colors:
                pips = face_pips.get(c, 0)
                if pips <= 0:
                    continue
                d = _required_sources(pips, face_cmc_int)
                if d > demand.get(c, 0):
                    demand[c] = d
    return demand


_TAPPED_DUAL_DISCOUNT = 0.85


def _fixing_sources_per_color(
    nonbasic_lands: list[str],
    scryfall_cards: dict[str, ScryfallCard],
    deck_colors: list[str],
) -> dict[str, float]:
    """Source contribution from nonbasic fixing lands, per deck color.

    Discounts duals that enter tapped with no untap escape to 0.85
    sources per color, matching Karsten's empirical adjustment for
    tapped lands in his 40-card limited supplemental tables. Universal
    fixers ("add one mana of any color", basic-fetchers) contribute 1.0
    to every deck color.
    """
    sources: dict[str, float] = {c: 0.0 for c in deck_colors}
    deck_color_set = set(deck_colors)
    for name in nonbasic_lands:
        sc = scryfall_cards.get(name)
        if not sc:
            continue
        text = (sc.oracle_text or "").lower()
        is_universal = (
            "add one mana of any" in text
            or "search your library for a basic" in text
        )
        if is_universal:
            for c in deck_colors:
                sources[c] += 1.0
            continue
        ci = set(sc.color_identity)
        if not ci or not ci.issubset(deck_color_set):
            continue
        enters_tapped = "enters" in text and "tapped" in text
        has_untap_escape = any(
            phrase in text
            for phrase in (
                "you may pay",
                "unless you control",
                "you don't control",
                "untapped",
            )
        )
        per_color = (
            _TAPPED_DUAL_DISCOUNT
            if enters_tapped and not has_untap_escape
            else 1.0
        )
        for c in ci & deck_color_set:
            sources[c] += per_color
    return sources


def _allocate_basics_for_demand(
    *,
    deck_colors: list[str],
    demand: dict[str, int],
    fixing_sources: dict[str, float],
    basics_budget: int,
) -> dict[str, int]:
    """Allocate basic lands to meet per-color source demand within budget.

    When effective demand (demand − fixing) sums ≤ basics_budget, each
    color receives its effective demand and the remainder pads the
    primary (highest-demand) color. When demand exceeds budget, basics
    are allocated proportionally to effective demand and the caller
    must check feasibility via :func:`_is_feasible`.
    """
    if not deck_colors:
        return {}

    effective = {
        c: max(0, demand.get(c, 0) - int(fixing_sources.get(c, 0.0)))
        for c in deck_colors
    }
    total = sum(effective.values())

    basics: dict[str, int] = {c: 0 for c in deck_colors}
    if total == 0:
        n = len(deck_colors)
        per = basics_budget // n
        for c in deck_colors:
            basics[c] = per
    elif total <= basics_budget:
        for c in deck_colors:
            basics[c] = effective[c]
    else:
        for c in deck_colors:
            basics[c] = round((effective[c] / total) * basics_budget)

    diff = basics_budget - sum(basics.values())
    if diff != 0:
        primary = max(deck_colors, key=lambda c: effective.get(c, 0))
        basics[primary] = max(0, basics[primary] + diff)

    return basics


def _is_feasible(
    *,
    demand: dict[str, int],
    basics: dict[str, int],
    fixing_sources: dict[str, float],
) -> bool:
    """True iff every demanded color has enough sources to meet it.

    Sources = basics in that color + floored fixing contribution.
    Fixing is floored to integers (a 0.85 tapped dual contributes 0
    toward an integer demand) since you can't draw 0.85 of a land —
    the discount only captures lower turn-N untap probability.
    Karsten's tables already target a probability, so flooring here is
    the pessimistic-but-honest read.
    """
    for c, d in demand.items():
        if d <= 0:
            continue
        sources = basics.get(c, 0) + int(fixing_sources.get(c, 0.0))
        if sources < d:
            return False
    return True


def _card_max_demand_in_color(card: ScryfallCard, color: str) -> int:
    """Highest Karsten source demand a card imposes for one color.

    For split cards, evaluates each face independently and returns the
    max — matching how :func:`_demand_per_color` handles split cards.
    Returns 0 if the card has no pip in this color.
    """
    mc = card.mana_cost or ""
    faces = mc.split(" // ") if " // " in mc else [mc]
    oracle = card.oracle_text or ""
    best = 0
    for face in faces:
        pips = parse_pips(face).get(color, 0)
        if pips <= 0:
            continue
        face_cmc_int = functional_cmc(_face_cmc(face), oracle)
        best = max(best, _required_sources(pips, face_cmc_int))
    return best


def _demote_infeasible_minority(
    *,
    deck_colors: list[str],
    main_deck: list[str],
    main_deck_cards: list[ScryfallCard],
    main_deck_cmc: list[int],
    main_deck_powers: list[float],
    splash_colors: list[str],
    nonbasic_lands: list[str],
    scryfall_cards: dict[str, ScryfallCard],
    castable: list[tuple[str, float, ScryfallCard]],
) -> tuple[list[str], list[str], list[ScryfallCard], list[int], list[float], str]:
    """Cut high-demand minority-color cards until the mana base is feasible.

    Walks the build's chosen main deck, checks Karsten feasibility at
    17 lands, and if infeasible cuts the highest-demand card in the
    minority color (the deck color with the lowest trial basic
    allocation). Each cut is replaced by the best castable not already
    in the main deck whose color identity fits the remaining palette.
    If the minority color empties out, it's pruned from ``deck_colors``
    and the archetype key shrinks accordingly.

    Returns the (possibly mutated) deck_colors, main_deck lists, and
    archetype key.

    Only 2-color commitments are eligible for demotion. 3-color
    archetypes are explicit trophy-prior builds and ship with their
    own fixing assumptions — stripping them down to mono would
    misrepresent the prior's intent.
    """
    if len(deck_colors) != 2:
        return (
            deck_colors,
            main_deck,
            main_deck_cards,
            main_deck_cmc,
            main_deck_powers,
            "".join(deck_colors),
        )

    in_deck = set(main_deck)
    remaining_castable = [
        (n, p, sc) for (n, p, sc) in castable if n not in in_deck
    ]
    cuts_made = 0
    safety = 0
    while safety < 12:
        safety += 1
        mana_base_colors = list(deck_colors) + list(splash_colors)
        if not mana_base_colors:
            break

        fixing_sources = _fixing_sources_per_color(
            nonbasic_lands, scryfall_cards, mana_base_colors,
        )
        demand = _demand_per_color(main_deck_cards, mana_base_colors)
        basics_budget = max(0, TARGET_LANDS - len(nonbasic_lands))
        trial_basics = _allocate_basics_for_demand(
            deck_colors=mana_base_colors,
            demand=demand,
            fixing_sources=fixing_sources,
            basics_budget=basics_budget,
        )
        if _is_feasible(
            demand=demand,
            basics=trial_basics,
            fixing_sources=fixing_sources,
        ):
            break

        # Identify the minority color among the *committed* deck colors
        # (splashes are excluded — they're capped at MAX_SPLASH_CARDS
        # and have their own fixing guard).
        committed = [c for c in deck_colors if trial_basics.get(c, 0) > 0]
        if len(committed) < 2:
            # Already mono — nothing left to demote.
            break
        minority_color = min(committed, key=lambda c: trial_basics.get(c, 0))

        # Find the highest-demand main_deck card with a pip in minority_color.
        cut_candidates: list[tuple[int, int]] = []
        for i, sc in enumerate(main_deck_cards):
            d = _card_max_demand_in_color(sc, minority_color)
            if d > 0:
                cut_candidates.append((d, i))
        if not cut_candidates:
            # No remaining minority cards but mana base is still
            # infeasible — give up; the proportional fallback will
            # produce something at least.
            break
        cut_candidates.sort(reverse=True)
        _, cut_idx = cut_candidates[0]
        main_deck.pop(cut_idx)
        main_deck_cards.pop(cut_idx)
        main_deck_cmc.pop(cut_idx)
        main_deck_powers.pop(cut_idx)
        cuts_made += 1

        # Refill from the strongest remaining castable that fits the
        # current palette (minus minority_color if it's now empty).
        still_has_minority = any(
            parse_pips(sc.mana_cost or "").get(minority_color, 0) > 0
            for sc in main_deck_cards
        )
        active_palette = set(deck_colors) | set(splash_colors)
        if not still_has_minority:
            active_palette.discard(minority_color)
        refilled_idx: int | None = None
        for k, (rn, rp, rsc) in enumerate(remaining_castable):
            if set(rsc.color_identity).issubset(active_palette):
                main_deck.append(rn)
                main_deck_cards.append(rsc)
                main_deck_cmc.append(
                    functional_cmc(rsc.cmc, rsc.oracle_text or ""),
                )
                main_deck_powers.append(rp)
                refilled_idx = k
                break
        if refilled_idx is not None:
            remaining_castable.pop(refilled_idx)

    # Final prune: only when we actually cut cards. Pruning otherwise
    # would re-key archetypes that legitimately commit to two colors
    # but happen to draft only one (e.g. a trophy-prior "WB" archetype
    # with two mono-B cards in the pool — the archetype key matters
    # for score lookup, so leaving it as "WB" preserves the prior
    # bonus).
    if cuts_made > 0:
        pruned_colors = [
            c for c in deck_colors
            if any(
                parse_pips(sc.mana_cost or "").get(c, 0) > 0
                for sc in main_deck_cards
            )
        ]
        if pruned_colors and pruned_colors != deck_colors:
            deck_colors = pruned_colors
    key = "".join(deck_colors)
    return (
        deck_colors,
        main_deck,
        main_deck_cards,
        main_deck_cmc,
        main_deck_powers,
        key,
    )


def _adaptive_target_lands(
    main_deck_cards: list[ScryfallCard],
    main_deck_cmc: list[int],
) -> int:
    """Pick 16 / 17 / 18 lands from main-deck curve and mana sinks.

    See spec ``2026-05-23-karsten-aware-mana-base-design.md`` §6.
    Defaults to 17 — the empirically optimal count for typical 40-card
    limited per Karsten's MTGO data.
    """
    if not main_deck_cards:
        return TARGET_LANDS

    avg_cmc = sum(main_deck_cmc) / max(len(main_deck_cmc), 1)

    mana_sinks = 0
    for card, cmc in zip(main_deck_cards, main_deck_cmc):
        text = (card.oracle_text or "").lower()
        mc = card.mana_cost or ""
        if cmc >= 6:
            mana_sinks += 1
        elif "{X}" in mc:
            mana_sinks += 1
        elif "crew" in text:
            mana_sinks += 1

    if avg_cmc >= 3.2 or mana_sinks >= 4:
        return 18
    if avg_cmc <= 2.2 and mana_sinks <= 1:
        return 16
    return TARGET_LANDS


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


def _is_fixing_land(
    card: ScryfallCard,
    target_colors: set[str],
    *,
    min_overlap: int = 2,
) -> bool:
    """Return *True* iff *card* is a non-creature land that fixes *target_colors*.

    A land qualifies as a fixing source when it is either a universal
    fixer (``"add one mana of any"`` or ``"search your library for a basic"``)
    or shares at least *min_overlap* colour(s) with the target set via
    its colour identity. Use ``min_overlap=1`` for single-splash fixing
    and ``min_overlap=2`` for multi-colour mana bases.
    """
    tl = card.type_line.lower()
    if "land" not in tl or "creature" in tl:
        return False
    text = (card.oracle_text or "").lower()
    if "add one mana of any" in text or "search your library for a basic" in text:
        return True
    return len(set(card.color_identity) & target_colors) >= min_overlap


def _has_splash_fixing(
    splash_color: str,
    nonbasic_lands: list[str],
    scryfall_cards: dict[str, ScryfallCard],
) -> bool:
    """Return *True* iff the pool's nonbasic lands fix mana for *splash_color*."""
    target = {splash_color}
    for name in nonbasic_lands:
        sc = scryfall_cards.get(name)
        if sc and _is_fixing_land(sc, target, min_overlap=1):
            return True
    return False


def _fixing_source_count(
    pool_names: list[str],
    scryfall_cards: dict[str, ScryfallCard],
    deck_colors: set[str],
) -> int:
    return sum(
        1 for name in pool_names
        if (sc := scryfall_cards.get(name))
        and _is_fixing_land(sc, deck_colors, min_overlap=2)
    )


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
    trophy_prior: TrophyDeckPrior | None = None,
    main_deck_powers: list[float] | None = None,
) -> list[tuple[str, ScryfallCard, str, float]]:
    """Pick up to *max_splash* cards from a 3rd colour that beat the weakest main-deck slots.

    A splash candidate must:
    - Have colour identity ⊆ deck_colors ∪ {splash_color}
    - Have at least one pip in *splash_color* (no free splashes)
    - Be in the pool but not already in main_deck
    - Have a fixing source for *splash_color* present in *nonbasic_lands*

    Candidates are ranked by :func:`_card_power`; only those strictly stronger
    than the weakest main-deck card they would displace are returned.
    Returns ``[]`` when no candidates qualify or no scoring data is available.
    """
    if not splashable_colors or (not card_map and not trophy_prior):
        return []

    # Power of each main_deck card, ascending — used for displacement check.
    if main_deck_powers is not None:
        main_powers = sorted(main_deck_powers)
    else:
        main_powers = sorted(
            _card_power(n, card_map, set_metrics, archetype_key, 22, trophy_prior)
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
            power = _card_power(
                name, card_map, set_metrics, archetype_key, 22, trophy_prior,
            )
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


def _multicolor_prior_candidates(
    *,
    ranked_colors: list[str],
    pool_names: list[str],
    scryfall_cards: dict[str, ScryfallCard],
    trophy_prior: TrophyDeckPrior | None,
) -> list[tuple[str, ...]]:
    if not trophy_prior:
        return []

    supported = set(ranked_colors)
    candidates: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for key in trophy_prior.archetype_keys(min_colors=3):
        colors = tuple(c for c in key if c in CARD_COLORS)
        color_set = set(colors)
        if len(colors) < 3 or not color_set.issubset(supported):
            continue
        # Require near-one-fixer-per-extra-colour: 3C needs 2, 4C needs 3,
        # 5C needs 4. The previous len(colors)-2 floor let 5C-domain slip in
        # with three duals, which is unplayable in practice.
        if _fixing_source_count(
            pool_names, scryfall_cards, color_set,
        ) < max(len(colors) - 1, 2):
            continue
        ordered = tuple(sorted(colors, key="WUBRG".index))
        if ordered not in seen:
            candidates.append(ordered)
            seen.add(ordered)

    candidates.sort(
        key=lambda colors: trophy_prior.deck_count("".join(colors)),
        reverse=True,
    )
    return candidates[:4]


def _holistic_score(
    deck_names: list[str],
    card_map: dict[str, CardRatings] | None,
    set_metrics: SetMetrics | None,
    scryfall_cards: dict[str, ScryfallCard],
    archetype: str,
    trophy_prior: TrophyDeckPrior | None = None,
) -> float:
    """Compute a holistic power score (0-100) for a deck."""
    if not card_map or not set_metrics:
        if trophy_prior:
            return trophy_prior.score_deck(
                deck_names, archetype, scryfall_cards,
            ) * TROPHY_DECK_SCORE_BONUS
        return -1.0

    wrs: list[float] = []
    for name in deck_names:
        cr = _card_map_get(card_map, name)
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
    if trophy_prior:
        base = min(
            100.0,
            base
            + trophy_prior.score_deck(
                deck_names, archetype, scryfall_cards,
            ) * TROPHY_DECK_SCORE_BONUS,
        )

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
    trophy_prior: TrophyDeckPrior | None = None,
) -> dict[str, DeckSuggestion]:
    """Generate deck suggestions from a drafted pool.

    Tries each viable colour combination and returns the best options keyed
    by colour set (e.g. ``"UB"`` or ``"WBG"``).

    Args:
        pool_names: Card names in the player's pool.
        scryfall_cards: Scryfall data lookup.
        card_map: Optional 17Lands data for power ranking.
        set_metrics: Optional set metrics for z-score calculation.
        trophy_prior: Optional compact trophy-deck prior for synergy and
            composition nudges.

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

    ranked_colors = detect_top_colors(pip_totals, n=len(CARD_COLORS))
    top = ranked_colors[:3]
    if len(top) < 2:
        top = list(CARD_COLORS[:2])

    def _make_pairs(colors: list[str]) -> list[tuple[str, ...]]:
        return [
            (colors[i], colors[j])
            for i in range(len(colors))
            for j in range(i + 1, len(colors))
        ]

    pairs = _make_pairs(top)
    pairs.extend(_multicolor_prior_candidates(
        ranked_colors=ranked_colors,
        pool_names=pool_names,
        scryfall_cards=scryfall_cards,
        trophy_prior=trophy_prior,
    ))

    # _build_for_pairs() runs the build loop with a candidate list +
    # min-spells threshold. Returns whatever archetypes produced a deck.
    # Called twice so that a fragmented pool still gets *some*
    # recommendation via the all-5-colour fallback below.
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
            trophy_prior=trophy_prior,
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
    score_filtered = {k: v for k, v in ordered.items() if v.score >= top_score - 20.0}
    # Secondary filter: a deck with fewer than _PLAYABLE_FLOOR spells in
    # main_deck is mostly lands (target_lands = 40 - len(main_deck)) and
    # qualitatively unplayable, even when its avg-spell-quality scores
    # decently. Drop such alternatives so the dropdown only shows
    # buildable options. We keep the top suggestion regardless so the
    # dropdown is never empty — better to surface a thin best-effort
    # recommendation than nothing.
    top_key = next(iter(score_filtered))
    playable = {
        k: v for k, v in score_filtered.items()
        if k == top_key or len(v.main_deck) >= _PLAYABLE_FLOOR
    }
    return playable


def _build_for_pairs(
    *,
    pool_names: list[str],
    scryfall_cards: dict[str, ScryfallCard],
    card_map: dict[str, CardRatings] | None,
    set_metrics: SetMetrics | None,
    trophy_prior: TrophyDeckPrior | None,
    pairs: list[tuple[str, ...]],
    top_colors: list[str],
    min_spells_threshold: int,
) -> dict[str, DeckSuggestion]:
    """Inner build loop, factored out so we can retry with a wider pair set."""
    results: dict[str, DeckSuggestion] = {}
    for colors in pairs:
        order = "WUBRG"
        deck_colors = sorted(colors, key=lambda c: order.index(c))
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
                power = _card_power(name, card_map, set_metrics, key, 22, trophy_prior)
                castable.append((name, power, sc))

        # Sort by power descending.
        castable.sort(key=lambda x: x[1], reverse=True)

        # Take top TARGET_SPELLS spells.
        main_deck: list[str] = []
        main_deck_cmc: list[int] = []
        main_deck_cards: list[ScryfallCard] = []
        main_deck_powers: list[float] = []
        # Trophy prior suggests an average creature count for this archetype;
        # honour it as a soft cap, but only when enough noncreatures are
        # available to fill the remaining slots. Castables are sorted by
        # power descending, so blindly skipping creatures past the cap would
        # drop the *strongest* creatures and replace them with weaker ones
        # later in the list — strictly worse than running over the cap.
        soft_cap = (
            trophy_prior.creature_soft_cap(key) if trophy_prior else None
        )
        if soft_cap is not None:
            noncreature_pool = sum(
                1 for _, _, sc in castable
                if "creature" not in sc.type_line.lower()
            )
            creature_budget: int | None = max(
                soft_cap, TARGET_SPELLS - noncreature_pool,
            )
        else:
            creature_budget = None
        creature_slots_used = 0
        for name, power, sc in castable:
            if len(main_deck) >= TARGET_SPELLS:
                break
            is_creature = "creature" in sc.type_line.lower()
            if (
                creature_budget is not None
                and is_creature
                and creature_slots_used >= creature_budget
            ):
                continue
            main_deck.append(name)
            main_deck_cmc.append(functional_cmc(sc.cmc, sc.oracle_text or ""))
            main_deck_cards.append(sc)
            main_deck_powers.append(power)
            if is_creature:
                creature_slots_used += 1

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
            text = (sc.oracle_text or "").lower()
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
        if len(deck_colors) > 2:
            splashable_colors = []
        else:
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
            trophy_prior=trophy_prior,
            main_deck_powers=main_deck_powers,
        ) if len(main_deck) >= 10 else []

        splash_colors: list[str] = []
        if splashes:
            # Displace the weakest main_deck cards to make room. Reuse the
            # powers we already computed while filling main_deck instead of
            # re-invoking _card_power for every card.
            powered = list(zip(
                main_deck, main_deck_cards, main_deck_cmc, main_deck_powers,
            ))
            powered.sort(key=lambda x: x[3], reverse=True)  # strongest first
            keep_count = min(len(powered), TARGET_SPELLS - len(splashes))
            keep = powered[:keep_count]
            main_deck = [x[0] for x in keep]
            main_deck_cards = [x[1] for x in keep]
            main_deck_cmc = [x[2] for x in keep]
            main_deck_powers = [x[3] for x in keep]
            for splash_name, splash_sc, splash_color, splash_power in splashes:
                main_deck.append(splash_name)
                main_deck_cards.append(splash_sc)
                main_deck_cmc.append(functional_cmc(
                    splash_sc.cmc, splash_sc.oracle_text or "",
                ))
                main_deck_powers.append(splash_power)
            splash_colors = sorted({s[2] for s in splashes})

        # Feasibility check: if the chosen main_deck demands more
        # colored sources than 17 lands + fixing can supply, cut the
        # most-demanding cards in the minority color until the build is
        # feasible (or the minority color drops to zero, demoting it
        # to mono). Splash cards have their own feasibility guard
        # inside _select_splashes (they require a fixing source); the
        # demotion here addresses 2-color *commitments* that can't be
        # supported by the mana base.
        deck_colors, main_deck, main_deck_cards, main_deck_cmc, main_deck_powers, key = (
            _demote_infeasible_minority(
                deck_colors=deck_colors,
                main_deck=main_deck,
                main_deck_cards=main_deck_cards,
                main_deck_cmc=main_deck_cmc,
                main_deck_powers=main_deck_powers,
                splash_colors=splash_colors,
                nonbasic_lands=nonbasic_lands,
                scryfall_cards=scryfall_cards,
                castable=castable,
            )
        )

        # Adaptive land count: pick 16 / 17 / 18 from the chosen main
        # deck's curve and mana-sink count. The build was shaped for
        # 17 spells; if the curve calls for 18 lands cut the weakest
        # spell, and if it calls for 16 try to add one more castable
        # so spell/land totals stay near 23/17 ± 1.
        adaptive_lands = _adaptive_target_lands(
            main_deck_cards, main_deck_cmc,
        )
        if adaptive_lands > TARGET_LANDS and len(main_deck) > 1:
            powered = list(zip(
                main_deck, main_deck_cards, main_deck_cmc, main_deck_powers,
            ))
            powered.sort(key=lambda x: x[3], reverse=True)
            powered = powered[:-1]  # drop the weakest
            main_deck = [x[0] for x in powered]
            main_deck_cards = [x[1] for x in powered]
            main_deck_cmc = [x[2] for x in powered]
            main_deck_powers = [x[3] for x in powered]
        elif adaptive_lands < TARGET_LANDS:
            in_deck = set(main_deck)
            palette = set(deck_colors) | set(splash_colors)
            for n, p, sc in castable:
                if n in in_deck:
                    continue
                if not set(sc.color_identity).issubset(palette):
                    continue
                main_deck.append(n)
                main_deck_cards.append(sc)
                main_deck_cmc.append(
                    functional_cmc(sc.cmc, sc.oracle_text or ""),
                )
                main_deck_powers.append(p)
                break

        # Cap nonbasic lands to adaptive_lands so we don't exceed 40 cards.
        target_lands = adaptive_lands - len(nonbasic_lands)
        if target_lands < 0:
            nonbasic_lands = nonbasic_lands[:adaptive_lands]
            target_lands = 0
        if len(nonbasic_lands) > adaptive_lands:
            nonbasic_lands = nonbasic_lands[:adaptive_lands]

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
        score = _holistic_score(
            main_deck, card_map, set_metrics, scryfall_cards, key, trophy_prior,
        )

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
