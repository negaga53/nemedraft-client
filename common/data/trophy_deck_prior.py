"""Compact trophy-deck priors for deck-builder scoring."""

from __future__ import annotations

import json
import logging
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from common.inference.pool_analyzer import (
    CARD_COLORS,
    ScryfallCard,
    functional_cmc,
    parse_pips,
)

logger = logging.getLogger(__name__)

PRIOR_VERSION = 1
PAIR_KEY_SEP = "\t"


@dataclass(frozen=True)
class TrophyArchetypePrior:
    """Trophy-deck card, pair, and composition priors for one archetype."""

    deck_count: int
    card_scores: dict[str, float] = field(default_factory=dict)
    pair_scores: dict[str, float] = field(default_factory=dict)
    avg_creatures: float = 0.0
    avg_noncreatures: float = 0.0
    avg_cmc: float = 0.0


@dataclass(frozen=True)
class TrophyDeckPrior:
    """Small runtime artifact distilled from 17Lands trophy deck lists."""

    set_code: str
    archetypes: dict[str, TrophyArchetypePrior] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> TrophyDeckPrior:
        """Load a prior JSON artifact from disk."""
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)

        version = int(raw.get("version", 0))
        if version != PRIOR_VERSION:
            raise ValueError(
                f"unsupported trophy prior version {version} in {path}; "
                f"expected {PRIOR_VERSION}"
            )

        archetypes: dict[str, TrophyArchetypePrior] = {}
        for key, data in raw.get("archetypes", {}).items():
            archetypes[key] = TrophyArchetypePrior(
                deck_count=int(data.get("deck_count", 0)),
                card_scores={
                    str(name): float(score)
                    for name, score in data.get("card_scores", {}).items()
                },
                pair_scores={
                    str(pair): float(score)
                    for pair, score in data.get("pair_scores", {}).items()
                },
                avg_creatures=float(data.get("avg_creatures", 0.0)),
                avg_noncreatures=float(data.get("avg_noncreatures", 0.0)),
                avg_cmc=float(data.get("avg_cmc", 0.0)),
            )
        return cls(set_code=str(raw.get("set_code", "")).upper(), archetypes=archetypes)

    def save(self, path: Path) -> None:
        """Write this prior to a compact JSON artifact."""
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = {
            "version": PRIOR_VERSION,
            "set_code": self.set_code,
            "archetypes": {
                key: {
                    "deck_count": prior.deck_count,
                    "card_scores": prior.card_scores,
                    "pair_scores": prior.pair_scores,
                    "avg_creatures": round(prior.avg_creatures, 3),
                    "avg_noncreatures": round(prior.avg_noncreatures, 3),
                    "avg_cmc": round(prior.avg_cmc, 3),
                }
                for key, prior in sorted(self.archetypes.items())
            },
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(raw, f, separators=(",", ":"), sort_keys=True)

    def card_bonus(self, card_name: str, archetype: str) -> float:
        """Return the normalized trophy inclusion score for one card."""
        prior = self._prior_for(archetype)
        if prior is None:
            return 0.0
        return prior.card_scores.get(card_name, 0.0)

    def creature_soft_cap(self, archetype: str, margin: float = 3.0) -> int | None:
        """Return a forgiving creature cap derived from trophy deck composition."""
        prior = self._prior_for(archetype)
        if prior is None or prior.avg_creatures <= 0:
            return None
        return max(1, round(prior.avg_creatures + margin))

    def archetype_keys(self, min_colors: int = 2) -> list[str]:
        """Return concrete colour archetype keys present in the artifact."""
        keys: list[str] = []
        for key, prior in self.archetypes.items():
            if key == "All Decks" or prior.deck_count <= 0:
                continue
            colors = [c for c in key if c in CARD_COLORS]
            if len(colors) >= min_colors:
                keys.append(key)
        return sorted(keys, key=lambda k: (len(k), k))

    def deck_count(self, archetype: str) -> int:
        """Return the number of trophy decks behind a concrete archetype."""
        prior = self.archetypes.get(archetype)
        return prior.deck_count if prior else 0

    def score_deck(
        self,
        deck_names: list[str],
        archetype: str,
        scryfall_cards: dict[str, ScryfallCard],
    ) -> float:
        """Return a 0..1 trophy-likeness score for a proposed spell suite."""
        prior = self._prior_for(archetype)
        if prior is None or not deck_names:
            return 0.0

        card_component = _mean(
            prior.card_scores.get(name, 0.0)
            for name in deck_names
        )

        pair_values: list[float] = []
        unique_names = sorted(set(deck_names))
        for i, left in enumerate(unique_names):
            for right in unique_names[i + 1:]:
                value = prior.pair_scores.get(_pair_key(left, right), 0.0)
                if value > 0.0:
                    pair_values.append(value)
        pair_component = _mean(pair_values)

        creature_count = 0
        cmcs: list[int] = []
        for name in deck_names:
            sc = scryfall_cards.get(name)
            if not sc:
                continue
            if "creature" in sc.type_line.lower():
                creature_count += 1
            cmcs.append(functional_cmc(sc.cmc, sc.oracle_text or ""))
        noncreature_count = len(deck_names) - creature_count
        avg_cmc = _mean(cmcs)
        composition_component = _composition_score(
            creature_count=creature_count,
            noncreature_count=noncreature_count,
            avg_cmc=avg_cmc,
            prior=prior,
        )

        return _clamp01(
            card_component * 0.45
            + pair_component * 0.35
            + composition_component * 0.20
        )

    def _prior_for(self, archetype: str) -> TrophyArchetypePrior | None:
        return self.archetypes.get(archetype) or self.archetypes.get("All Decks")


def build_prior_from_17lands_game_data(
    game_data_path: Path,
    *,
    set_code: str,
    scryfall_cards: dict[str, ScryfallCard],
    min_pair_count: int = 5,
    max_pairs_per_archetype: int = 1500,
) -> TrophyDeckPrior:
    """Build a compact prior from 17Lands game CSV trophy deck columns."""
    import polars as pl

    deck_cols = _deck_columns(game_data_path)
    if not deck_cols:
        raise ValueError(f"no deck_* columns found in {game_data_path}")

    game_lf = pl.scan_csv(game_data_path, infer_schema_length=10_000)
    outcomes = (
        game_lf
        .group_by("draft_id")
        .agg(
            pl.col("won").sum().alias("wins"),
            (pl.col("won").count() - pl.col("won").sum()).alias("losses"),
        )
        .filter(pl.col("wins") == 7)
        .filter(pl.col("losses").is_in([0, 1, 2]))
        .select("draft_id")
        .collect()
    )
    trophy_ids = outcomes["draft_id"].to_list()
    if not trophy_ids:
        raise ValueError(f"no trophy drafts found in {game_data_path}")

    trophy_games = (
        pl.scan_csv(game_data_path, infer_schema_length=10_000)
        .filter(pl.col("draft_id").is_in(trophy_ids))
        .select(["draft_id", *deck_cols])
        .unique(subset=["draft_id"], keep="first")
        .collect()
    )
    return build_prior_from_trophy_rows(
        trophy_games.iter_rows(named=True),
        deck_columns=deck_cols,
        set_code=set_code,
        scryfall_cards=scryfall_cards,
        min_pair_count=min_pair_count,
        max_pairs_per_archetype=max_pairs_per_archetype,
    )


def build_prior_from_trophy_rows(
    rows,
    *,
    deck_columns: list[str],
    set_code: str,
    scryfall_cards: dict[str, ScryfallCard],
    min_pair_count: int = 5,
    max_pairs_per_archetype: int = 1500,
) -> TrophyDeckPrior:
    """Build a prior from rows that expose 17Lands ``deck_*`` count columns."""
    resolver = _NameResolver(scryfall_cards)
    buckets: dict[str, _MutableArchetypeStats] = defaultdict(_MutableArchetypeStats)

    for row in rows:
        deck_names: list[str] = []
        for col in deck_columns:
            count = int(row.get(col, 0) or 0)
            if count <= 0:
                continue
            name = resolver.resolve(col.removeprefix("deck_"))
            deck_names.extend([name] * count)

        if not deck_names:
            continue

        archetypes = _infer_archetypes(deck_names, scryfall_cards)
        for key in dict.fromkeys(["All Decks", *archetypes]):
            buckets[key].add_deck(deck_names, scryfall_cards)

    archetypes: dict[str, TrophyArchetypePrior] = {}
    for key, stats in buckets.items():
        archetypes[key] = stats.freeze(
            min_pair_count=min_pair_count,
            max_pairs=max_pairs_per_archetype,
        )

    return TrophyDeckPrior(set_code=set_code.upper(), archetypes=archetypes)


def default_prior_path(data_dir: Path, set_code: str) -> Path:
    """Return the conventional artifact path under a processed-data dir."""
    return data_dir / f"{set_code.upper()}_trophy_deck_prior.json"


class _NameResolver:
    def __init__(self, scryfall_cards: dict[str, ScryfallCard]) -> None:
        self._exact = set(scryfall_cards)
        self._front_to_full: dict[str, str] = {}
        for name in scryfall_cards:
            front = name.split(" // ", 1)[0]
            self._front_to_full.setdefault(front, name)

    def resolve(self, name: str) -> str:
        if name in self._exact:
            return name
        return self._front_to_full.get(name, name)


class _MutableArchetypeStats:
    def __init__(self) -> None:
        self.deck_count = 0
        self.card_count: Counter[str] = Counter()
        self.pair_count: Counter[tuple[str, str]] = Counter()
        self.creature_counts: list[int] = []
        self.noncreature_counts: list[int] = []
        self.avg_cmcs: list[float] = []

    def add_deck(
        self,
        deck_names: list[str],
        scryfall_cards: dict[str, ScryfallCard],
    ) -> None:
        self.deck_count += 1
        unique_names = sorted(set(deck_names))
        self.card_count.update(unique_names)
        for i, left in enumerate(unique_names):
            for right in unique_names[i + 1:]:
                self.pair_count[(left, right)] += 1

        creature_count = 0
        cmcs: list[int] = []
        for name in deck_names:
            sc = scryfall_cards.get(name)
            if not sc:
                continue
            if "land" in sc.type_line.lower() and "creature" not in sc.type_line.lower():
                continue
            if "creature" in sc.type_line.lower():
                creature_count += 1
            cmcs.append(functional_cmc(sc.cmc, sc.oracle_text or ""))
        self.creature_counts.append(creature_count)
        self.noncreature_counts.append(max(0, len(cmcs) - creature_count))
        self.avg_cmcs.append(_mean(cmcs))

    def freeze(self, *, min_pair_count: int, max_pairs: int) -> TrophyArchetypePrior:
        max_card_count = max(self.card_count.values(), default=1)
        card_scores = {
            name: round(count / max_card_count, 4)
            for name, count in self.card_count.items()
        }

        raw_pair_scores: list[tuple[str, float]] = []
        for (left, right), count in self.pair_count.items():
            if count < min_pair_count:
                continue
            left_count = self.card_count[left]
            right_count = self.card_count[right]
            if not left_count or not right_count or not self.deck_count:
                continue
            p_ij = count / self.deck_count
            p_i = left_count / self.deck_count
            p_j = right_count / self.deck_count
            pmi = math.log(p_ij / (p_i * p_j))
            if pmi > 0:
                raw_pair_scores.append((_pair_key(left, right), pmi))

        max_pair_score = max((score for _, score in raw_pair_scores), default=1.0)
        pair_scores = {
            pair: round(score / max_pair_score, 4)
            for pair, score in sorted(
                raw_pair_scores, key=lambda item: item[1], reverse=True,
            )[:max_pairs]
        }

        return TrophyArchetypePrior(
            deck_count=self.deck_count,
            card_scores=card_scores,
            pair_scores=pair_scores,
            avg_creatures=_mean(self.creature_counts),
            avg_noncreatures=_mean(self.noncreature_counts),
            avg_cmc=_mean(self.avg_cmcs),
        )


def _deck_columns(path: Path) -> list[str]:
    import polars as pl

    return [
        name
        for name in pl.scan_csv(path, infer_schema_length=1000).collect_schema().names()
        if name.startswith("deck_")
    ]


def _infer_archetypes(
    deck_names: list[str],
    scryfall_cards: dict[str, ScryfallCard],
) -> list[str]:
    pip_totals: dict[str, int] = {c: 0 for c in CARD_COLORS}
    identity_totals: dict[str, int] = {c: 0 for c in CARD_COLORS}
    for name in deck_names:
        sc = scryfall_cards.get(name)
        if not sc:
            continue
        if "land" in sc.type_line.lower() and "creature" not in sc.type_line.lower():
            continue
        pips = parse_pips(sc.mana_cost)
        for color in CARD_COLORS:
            pip_totals[color] += pips[color]
            if color in sc.color_identity:
                identity_totals[color] += 1

    support = {
        color: pip_totals[color] + identity_totals[color]
        for color in CARD_COLORS
    }
    ranked = sorted(
        CARD_COLORS,
        key=lambda color: (support[color], pip_totals[color], identity_totals[color]),
        reverse=True,
    )
    active = [color for color in ranked if support[color] >= 2]
    if len(active) < 2:
        active = [color for color in ranked if support[color] > 0]
    if len(active) < 2:
        return ["All Decks"]

    top_two = sorted(active[:2], key="WUBRG".index)
    keys = ["".join(top_two)]
    if len(active) >= 3:
        multicolor = sorted(active[:5], key="WUBRG".index)
        keys.append("".join(multicolor))
    return keys


def _pair_key(left: str, right: str) -> str:
    a, b = sorted((left, right))
    return f"{a}{PAIR_KEY_SEP}{b}"


def _composition_score(
    *,
    creature_count: int,
    noncreature_count: int,
    avg_cmc: float,
    prior: TrophyArchetypePrior,
) -> float:
    if prior.avg_creatures <= 0 and prior.avg_noncreatures <= 0:
        return 0.0
    creature_score = 1.0 - min(1.0, abs(creature_count - prior.avg_creatures) / 8.0)
    noncreature_score = 1.0 - min(
        1.0, abs(noncreature_count - prior.avg_noncreatures) / 8.0,
    )
    cmc_score = 1.0 - min(1.0, abs(avg_cmc - prior.avg_cmc) / 2.0)
    return _clamp01(creature_score * 0.4 + noncreature_score * 0.3 + cmc_score * 0.3)


def _mean(values) -> float:
    vals = [float(v) for v in values]
    if not vals:
        return 0.0
    return sum(vals) / len(vals)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))
