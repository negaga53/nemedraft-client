"""Pool composition analysis — tracks colour commitment, curve, and role counts."""

from __future__ import annotations

import json
import re
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Functional CMC helpers
# ---------------------------------------------------------------------------

_COST_REDUCTION_RE = re.compile(r"costs?\s.*?(\d+).*?less", re.IGNORECASE)
_PIP_RE = re.compile(r"\{(.*?)\}")

_ALT_COST_KEYWORDS = [
    "evoke {", "prototype {", "spectacle {", "surge {",
    "cleave {", "blitz {", "prowl {", "madness {", "miracle {",
    "convoke", "affinity for", "improvise", "spree", "sneak {",
]


def functional_cmc(cmc: float, oracle_text: str) -> int:
    """Compute functional mana value, accounting for alternate-cost mechanics.

    Args:
        cmc: Raw converted mana cost from Scryfall.
        oracle_text: Oracle rules text of the card.

    Returns:
        Adjusted CMC as an integer.
    """
    raw = int(cmc)
    text = oracle_text.lower()

    if "landcycling" in text or "bloodrush" in text:
        return min(raw, 2)

    if "disguise {" in text or "morph {" in text or "face down as 2/2" in text:
        return min(raw, 3)

    if "channel —" in text or "channel —" in text:
        return min(raw, 2)

    m = _COST_REDUCTION_RE.search(text)
    if m:
        reduction = int(m.group(1))
        return max(1, raw - reduction)

    if raw > 3 and any(kw in text for kw in _ALT_COST_KEYWORDS):
        return max(2, raw - 2)

    return raw


# ---------------------------------------------------------------------------
# Pip / colour parsing
# ---------------------------------------------------------------------------

CARD_COLORS = ("W", "U", "B", "R", "G")


def parse_pips(mana_cost: str) -> dict[str, int]:
    """Count each colour pip in a mana cost string.

    Hybrid pips (e.g. ``{W/B}``) contribute to both colours.

    Args:
        mana_cost: Scryfall-style mana cost, e.g. ``"{1}{W}{U}"``.

    Returns:
        Dict mapping colour letter to pip count.
    """
    counts: dict[str, int] = {c: 0 for c in CARD_COLORS}
    for m in _PIP_RE.finditer(mana_cost):
        pip = m.group(1)
        options = pip.split("/")
        for opt in options:
            opt = opt.strip().upper()
            if opt in counts:
                counts[opt] += 1
    return counts


def detect_top_colors(pip_totals: dict[str, int], n: int = 2) -> list[str]:
    """Return the top *n* colours by total pip count."""
    ranked = sorted(pip_totals.items(), key=lambda kv: kv[1], reverse=True)
    return [c for c, cnt in ranked[:n] if cnt > 0]


# ---------------------------------------------------------------------------
# Scryfall card cache
# ---------------------------------------------------------------------------

@dataclass
class ScryfallCard:
    """Minimal card data needed for pool analysis."""

    name: str
    mana_cost: str
    cmc: float
    type_line: str
    oracle_text: str
    colors: list[str]
    color_identity: list[str]
    keywords: list[str]
    rarity: str
    power: str = ""
    toughness: str = ""


def load_scryfall_cards(scryfall_dir: Path) -> dict[str, ScryfallCard]:
    """Load all per-set Scryfall JSONs into a name→card lookup."""
    cards: dict[str, ScryfallCard] = {}
    for p in sorted(scryfall_dir.glob("*_cards.json")):
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        for raw in data:
            name = raw.get("name", "")
            if not name or name in cards:
                continue
            cards[name] = ScryfallCard(
                name=name,
                mana_cost=raw.get("mana_cost", ""),
                cmc=float(raw.get("cmc") or 0),
                type_line=raw.get("type_line", ""),
                oracle_text=raw.get("oracle_text", ""),
                colors=raw.get("colors", []),
                color_identity=raw.get("color_identity", []),
                keywords=raw.get("keywords", []),
                rarity=raw.get("rarity", ""),
                power=str(raw.get("power", "")),
                toughness=str(raw.get("toughness", "")),
            )
    logger.info("Loaded %d Scryfall cards for pool analysis", len(cards))
    return cards


def load_scryfall_cards_for_set(
    scryfall_dir: Path,
    set_code: str,
) -> dict[str, ScryfallCard]:
    """Load a single set's Scryfall JSON into a name→card lookup.

    Args:
        scryfall_dir: Directory containing per-set ``*_cards.json`` files.
        set_code: Three-letter set code (e.g. ``"TMT"``).

    Returns:
        Mapping of card name to :class:`ScryfallCard`.
    """
    cards: dict[str, ScryfallCard] = {}
    p = scryfall_dir / f"{set_code.lower()}_cards.json"
    if not p.exists():
        logger.warning("No Scryfall file for set %s at %s", set_code, p)
        return cards
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    for raw in data:
        name = raw.get("name", "")
        if not name or name in cards:
            continue
        cards[name] = ScryfallCard(
            name=name,
            mana_cost=raw.get("mana_cost", ""),
            cmc=float(raw.get("cmc") or 0),
            type_line=raw.get("type_line", ""),
            oracle_text=raw.get("oracle_text", ""),
            colors=raw.get("colors", []),
            color_identity=raw.get("color_identity", []),
            keywords=raw.get("keywords", []),
            rarity=raw.get("rarity", ""),
            power=str(raw.get("power", "")),
            toughness=str(raw.get("toughness", "")),
        )
    logger.info("Loaded %d Scryfall cards for set %s", len(cards), set_code)
    return cards


# ---------------------------------------------------------------------------
# Role / tag detection (heuristic — no oracle tags from community)
# ---------------------------------------------------------------------------

_REMOVAL_KEYWORDS = [
    "destroy target", "exile target", "deals damage to target",
    "deals damage to any target", "-x/-x", "fights",
    "destroy all", "exile all",
]

_EVASION_KEYWORDS = ["flying", "menace", "trample", "unblockable", "shadow",
                      "fear", "intimidate", "skulk", "can't be blocked"]

_CARD_DRAW_KEYWORDS = ["draw a card", "draw two", "draw three",
                        "draws a card", "draw cards"]


def _has_tag(oracle_text: str, keywords: list[str]) -> bool:
    text = oracle_text.lower()
    return any(kw in text for kw in keywords)


def detect_tags(card: ScryfallCard) -> list[str]:
    """Return heuristic role tags for a card."""
    tags: list[str] = []
    text = card.oracle_text.lower()
    tl = card.type_line.lower()

    if _has_tag(card.oracle_text, _REMOVAL_KEYWORDS):
        tags.append("removal")
    if any(kw.lower() in text or kw.lower() in tl for kw in _EVASION_KEYWORDS):
        tags.append("evasion")
    if _has_tag(card.oracle_text, _CARD_DRAW_KEYWORDS):
        tags.append("card_draw")
    if "creature" in tl:
        tags.append("creature")
    if "land" in tl and "creature" not in tl:
        tags.append("land")
        # Dual / fixing land detection
        ci = card.color_identity
        if len(ci) >= 2 or "add one mana of any" in text:
            tags.append("fixing")
    if any(x in text for x in ["add one mana of any", "add {", "search your library for a basic"]):
        tags.append("fixing")

    return tags


# ---------------------------------------------------------------------------
# Pool analysis result
# ---------------------------------------------------------------------------

@dataclass
class PoolAnalysis:
    """Aggregated pool composition stats."""

    total_cards: int = 0
    creature_count: int = 0
    noncreature_spell_count: int = 0
    land_count: int = 0

    # Role counts.
    removal_count: int = 0
    evasion_count: int = 0
    card_draw_count: int = 0
    fixing_count: int = 0

    # Mana curve: index = CMC (0–7+), value = count.
    curve: list[int] = field(default_factory=lambda: [0] * 8)

    # Pip totals across the pool.
    pip_totals: dict[str, int] = field(
        default_factory=lambda: {c: 0 for c in CARD_COLORS}
    )
    # Top 2 lane colours.
    top_colors: list[str] = field(default_factory=list)

    # Early play count (≤2 CMC creatures + ≤2 CMC removal).
    early_plays: int = 0


def analyze_pool(
    pool_names: list[str],
    scryfall_cards: dict[str, ScryfallCard],
) -> PoolAnalysis:
    """Analyze a list of drafted card names.

    Args:
        pool_names: Card names currently in the player's pool.
        scryfall_cards: Scryfall card lookup from :func:`load_scryfall_cards`.

    Returns:
        An aggregated :class:`PoolAnalysis`.
    """
    pa = PoolAnalysis(total_cards=len(pool_names))

    for name in pool_names:
        card = scryfall_cards.get(name)
        if card is None:
            continue

        tags = detect_tags(card)
        tl = card.type_line.lower()
        fcmc = functional_cmc(card.cmc, card.oracle_text)
        cmc_bucket = min(fcmc, 7)

        # Type classification
        is_creature = "creature" in tl
        is_land = "land" in tl and "creature" not in tl

        # Only non-land cards contribute to the mana curve.
        if not is_land:
            pa.curve[cmc_bucket] += 1

        if is_creature:
            pa.creature_count += 1
        elif is_land:
            pa.land_count += 1
        else:
            pa.noncreature_spell_count += 1

        # Role counts
        if "removal" in tags:
            pa.removal_count += 1
        if "evasion" in tags:
            pa.evasion_count += 1
        if "card_draw" in tags:
            pa.card_draw_count += 1
        if "fixing" in tags:
            pa.fixing_count += 1

        # Early plays
        if fcmc <= 2 and (is_creature or "removal" in tags):
            pa.early_plays += 1

        # Pip accumulation
        pips = parse_pips(card.mana_cost)
        for c in CARD_COLORS:
            pa.pip_totals[c] += pips[c]

    pa.top_colors = detect_top_colors(pa.pip_totals)
    return pa


# ---------------------------------------------------------------------------
# Castability evaluation
# ---------------------------------------------------------------------------

# Composition targets (ported from MTGA_Draft_17Lands advisor).
TARGET_EARLY_PLAYS = 7
TARGET_REMOVAL = 3
TARGET_EVASION = 3
TARGET_CARD_DRAW = 2


def castability(
    card: ScryfallCard,
    top_colors: list[str],
    pack_number: int,
    fixing_count: int,
    z_score: float = 0.0,
) -> float:
    """Evaluate how castable a card is given pool colours and fixing.

    Returns:
        A multiplier in ``[0.01, 1.0]`` — 1.0 = fully on-colour.
    """
    pips = parse_pips(card.mana_cost)
    pip_colors = [c for c in CARD_COLORS if pips[c] > 0]

    if not pip_colors:
        return 1.0  # colourless

    off_color_pips = sum(
        pips[c] for c in pip_colors if c not in top_colors
    )

    if off_color_pips == 0:
        return 1.0  # fully on-colour

    # Pack 1 — exploration phase, more lenient.
    if pack_number == 0:
        if off_color_pips >= 2:
            return 0.3
        return 0.6

    # Packs 2–3 — commitment phase.
    if off_color_pips >= 2 and fixing_count < 2:
        return 0.01  # hard lock

    # Bomb splash (high z-score, single off-colour pip).
    if z_score >= 1.5 and off_color_pips == 1:
        splash_mult = 0.35 if pack_number == 2 else 0.45
        if fixing_count >= (4 if pack_number == 2 else 3):
            return splash_mult

    if off_color_pips == 1 and fixing_count >= 2:
        return 0.3  # splashable

    return 0.05 if pack_number == 1 else 0.01
