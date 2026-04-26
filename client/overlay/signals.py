"""Open-lane signal detection — identifies which colours are being passed."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from common.data.card_stats import SetMetrics
from common.data.seventeenlands import CardRatings

logger = logging.getLogger(__name__)

CARD_COLORS = ("W", "U", "B", "R", "G")


@dataclass
class SeenCard:
    """A card that was seen in a pack but not picked."""

    card_name: str
    colors: list[str]
    gihwr: float
    ata: float
    pack_number: int
    pick_number: int


@dataclass
class SignalResult:
    """Signal strengths per colour. Higher = more open."""

    scores: dict[str, float] = field(
        default_factory=lambda: {c: 0.0 for c in CARD_COLORS}
    )

    def top_signals(self, n: int = 2) -> list[tuple[str, float]]:
        """Return the *n* strongest signals as ``(color, score)`` pairs."""
        ranked = sorted(self.scores.items(), key=lambda kv: kv[1], reverse=True)
        return [(c, s) for c, s in ranked[:n] if s > 0]


class SignalCalculator:
    """Computes draft lane signals from cards seen but not picked.

    Args:
        set_metrics: Set-level mean/std used to identify quality cards.
        card_map: 17Lands card data for colour / GIHWR lookups.
    """

    def __init__(
        self,
        set_metrics: SetMetrics,
        card_map: dict[str, CardRatings],
    ) -> None:
        self._metrics = set_metrics
        self._card_map = card_map

    def calculate(self, seen_cards: list[SeenCard]) -> SignalResult:
        """Compute per-colour signal scores from a list of seen (passed) cards.

        The formula is: ``signal(color) = Σ (pick - ATA) × (GIHWR - baseline)``
        for each quality card that was passed late (pick > ATA).

        Pack 2 is excluded because those packs were passed *by* us, so they
        don't carry information about what neighbours are drafting.

        Note: ``pick_number`` is 0-indexed (from Arena logs) while ``ata`` is
        1-indexed (from 17Lands API), so we add 1 to align them.  GIHWR and
        baseline are stored as fractions (e.g. 0.55), so we scale by 100 to
        produce scores comparable to the 17Lands reference implementation.
        """
        result = SignalResult()
        baseline = self._metrics.baseline_wr

        for sc in seen_cards:
            # Skip Pack 2 — those packs travel the other direction.
            if sc.pack_number == 1:
                continue

            if sc.gihwr <= baseline or sc.ata <= 0:
                continue

            # Convert 0-indexed pick to 1-indexed to match ATA scale.
            lateness = (sc.pick_number + 1) - sc.ata
            if lateness <= 0:
                continue  # card was taken on time or early — no signal

            # Scale from fraction (0.01) to percentage (1.0) for meaningful scores.
            quality_diff = (sc.gihwr - baseline) * 100.0
            card_score = lateness * quality_diff

            for color in sc.colors:
                if color in result.scores:
                    result.scores[color] += card_score

        return result

    def calculate_wheel_retention(
        self,
        p1_cards: list[SeenCard],
        p9_cards: list[SeenCard],
    ) -> SignalResult:
        """Compare Pack 1 quality vs what wheeled to detect open lanes.

        Args:
            p1_cards: Cards first seen in P1P1–P1P4 (early).
            p9_cards: Cards seen wheeling in P1P9+ (late).

        Returns:
            Signal result where high retention = open colour.
        """
        baseline = self._metrics.baseline_wr

        p1_quality: dict[str, float] = {c: 0.0 for c in CARD_COLORS}
        p9_quality: dict[str, float] = {c: 0.0 for c in CARD_COLORS}

        for sc in p1_cards:
            if sc.gihwr > baseline:
                diff = sc.gihwr - baseline
                for color in sc.colors:
                    if color in p1_quality:
                        p1_quality[color] += diff

        for sc in p9_cards:
            if sc.gihwr > baseline:
                diff = sc.gihwr - baseline
                for color in sc.colors:
                    if color in p9_quality:
                        p9_quality[color] += diff

        result = SignalResult()
        for color in CARD_COLORS:
            if p1_quality[color] > 0:
                retention = p9_quality[color] / p1_quality[color]
                if retention > 0.1:
                    result.scores[color] = retention * 20.0

        return result
