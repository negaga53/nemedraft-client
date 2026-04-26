"""Set-level metrics and statistical helpers for 17Lands data."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

from common.data.seventeenlands import CardRatings

# Default 17Lands global baseline win-rate (approximate across formats).
_DEFAULT_BASELINE_WR = 0.54


@dataclass
class ColorMetrics:
    """Mean and standard deviation for a stat/colour combination."""

    mean: float = 0.0
    std: float = 0.0


@dataclass
class SetMetrics:
    """Global metrics for a downloaded set, used for z-score normalisation.

    Build via :meth:`from_card_map`.
    """

    # metrics[stat_alias][color_key] → ColorMetrics
    metrics: dict[str, dict[str, ColorMetrics]] = field(default_factory=dict)
    baseline_wr: float = _DEFAULT_BASELINE_WR

    # -- construction --------------------------------------------------------

    @classmethod
    def from_card_map(cls, card_map: dict[str, CardRatings]) -> SetMetrics:
        """Compute per-stat, per-colour mean/std from a card map."""
        obj = cls()
        stat_keys = ["gihwr", "ohwr", "gdwr", "gpwr", "iwd", "alsa", "ata"]

        for stat in stat_keys:
            obj.metrics[stat] = {}
            color_keys: set[str] = set()
            for cr in card_map.values():
                color_keys.update(cr.deck_colors.keys())

            for color in color_keys:
                values: list[float] = []
                seen: set[str] = set()
                for cr in card_map.values():
                    if cr.name in seen:
                        continue
                    seen.add(cr.name)
                    color_stats = cr.deck_colors.get(color)
                    if color_stats is None:
                        continue
                    v = color_stats.get(stat, 0.0)
                    if v != 0.0:
                        values.append(v)

                if len(values) >= 2:
                    obj.metrics[stat][color] = ColorMetrics(
                        mean=statistics.mean(values),
                        std=statistics.pstdev(values),
                    )
                elif values:
                    obj.metrics[stat][color] = ColorMetrics(
                        mean=values[0], std=0.0,
                    )

        # Derive baseline WR from the "All Decks" GIHWR mean.
        ad = obj.metrics.get("gihwr", {}).get("All Decks")
        if ad and ad.mean > 0:
            obj.baseline_wr = ad.mean

        return obj

    # -- query helpers -------------------------------------------------------

    def get_metrics(self, color: str, stat: str) -> tuple[float, float]:
        """Return (mean, std) for a colour/stat pair.  Falls back to All Decks."""
        cm = self.metrics.get(stat, {}).get(color)
        if cm is None:
            cm = self.metrics.get(stat, {}).get("All Decks")
        if cm is None:
            return 0.0, 1.0
        return cm.mean, cm.std if cm.std > 0 else 1.0

    def z_score(self, value: float, color: str, stat: str = "gihwr") -> float:
        """Compute the z-score of *value* relative to the colour baseline."""
        mean, std = self.get_metrics(color, stat)
        if std == 0:
            return 0.0
        return (value - mean) / std


def get_blended_wr(
    card: CardRatings,
    archetype: str,
    pick_number: int,
    set_metrics: SetMetrics,
) -> float:
    """Compute archetype-blended win rate for a card.

    Early in the draft global stats are preferred; later picks lean toward
    the active archetype's stats.

    Args:
        card: Card with per-colour 17Lands data.
        archetype: Active colour pair, e.g. ``"UB"`` or ``"All Decks"``.
        pick_number: Absolute pick (0–44).
        set_metrics: Set-level metrics for baseline.

    Returns:
        Blended win rate as a float (e.g. 0.563).
    """
    global_stats = card.deck_colors.get("All Decks", {})
    arch_stats = card.deck_colors.get(archetype, {})

    global_wr = global_stats.get("gihwr", 0.0)
    arch_wr = arch_stats.get("gihwr", 0.0)
    arch_samples = arch_stats.get("samples", 0.0)

    # Archetype weight ramps from 0.2 to 0.9 across the draft.
    arch_weight = min(0.9, 0.2 + (pick_number / 45) * 0.7)

    # Only trust archetype data if there are enough samples.
    if arch_wr == 0.0 or arch_samples < 100:
        return global_wr

    return global_wr * (1.0 - arch_weight) + arch_wr * arch_weight
