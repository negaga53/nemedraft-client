"""Tests for the deck-builder scoring heuristic and splash logic.

Coverage:
- Completeness penalty (a 17-spell deck padded with 23 lands shouldn't
  outrank a real 23-spell deck on average-spell-strength alone).
- Splash fixing detection — universal fixers, splash-coloured lands.
- Splash card selection — single off-colour, displaces weaker cards.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from common.inference.deck_builder import (
    MAX_SPLASH_CARDS,
    TARGET_SPELLS,
    _has_splash_fixing,
    _holistic_score,
    _select_splashes,
)


def _card_map(name_to_gihwr: dict[str, float]) -> dict:
    """Build a fake card_map whose entries expose deck_colors["All Decks"]["gihwr"]."""
    card_map: dict = {}
    for name, gihwr in name_to_gihwr.items():
        cr = MagicMock()
        cr.deck_colors = {"All Decks": {"gihwr": gihwr}}
        card_map[name] = cr
    return card_map


def _set_metrics(mean: float = 0.54, std: float = 0.04) -> MagicMock:
    sm = MagicMock()
    sm.get_metrics.return_value = (mean, std)
    return sm


class TestHolisticScoreCompleteness:
    def test_full_deck_outscores_short_deck_with_similar_average(self):
        """A complete 23-spell deck must beat a 17-spell deck of similar quality."""
        # Both archetypes hit the format mean (54% gihwr) — z ≈ 0.
        full = [f"spell{i}" for i in range(TARGET_SPELLS)]
        short = [f"spellS{i}" for i in range(17)]
        card_map = _card_map({n: 0.54 for n in full + short})
        sm = _set_metrics()

        s_full = _holistic_score(full, card_map, sm, {}, "UR")
        s_short = _holistic_score(short, card_map, sm, {}, "UB")

        assert s_full > s_short, (
            f"complete deck ({s_full:.2f}) must outrank a 17-spell deck"
            f" of equal quality ({s_short:.2f})"
        )

    def test_full_mediocre_deck_outranks_short_strong_deck(self):
        """The exact failure mode the user hit: 17 strong spells were beating 23 OK ones."""
        # Short deck: 17 spells, each at 60% gihwr (~1.5 std above mean → strong).
        short = [f"strong{i}" for i in range(17)]
        # Full deck: 23 spells, each at 56% gihwr (slightly above mean → adequate).
        full = [f"ok{i}" for i in range(TARGET_SPELLS)]
        card_map = _card_map({n: 0.60 for n in short} | {n: 0.56 for n in full})
        sm = _set_metrics()

        s_short_strong = _holistic_score(short, card_map, sm, {}, "UB")
        s_full_ok = _holistic_score(full, card_map, sm, {}, "UR")

        assert s_full_ok > s_short_strong, (
            f"a 23-spell adequate deck ({s_full_ok:.2f}) must beat a"
            f" 17-spell strong deck ({s_short_strong:.2f}) — otherwise"
            f" the builder recommends 17 spells + 23 lands"
        )

    def test_completeness_scales_score(self):
        """Penalty factor is linear in spell count below TARGET_SPELLS."""
        names_22 = [f"x{i}" for i in range(22)]
        names_23 = [f"x{i}" for i in range(TARGET_SPELLS)]
        card_map = _card_map({n: 0.54 for n in names_22 + names_23})
        sm = _set_metrics()

        s22 = _holistic_score(names_22, card_map, sm, {}, "UR")
        s23 = _holistic_score(names_23, card_map, sm, {}, "UR")

        # 22 / 23 ≈ 0.957 — small penalty, full deck still wins.
        assert s23 > s22
        assert s22 == pytest.approx(s23 * (22 / TARGET_SPELLS), rel=1e-6)

    def test_returns_minus_one_without_card_map(self):
        assert _holistic_score(["x"], None, _set_metrics(), {}, "U") == -1.0

    def test_returns_minus_one_when_no_gihwr_data(self):
        # No card in card_map → no win rates → -1.0.
        card_map: dict = {}
        sm = _set_metrics()
        assert _holistic_score(["nonexistent"], card_map, sm, {}, "U") == -1.0


def _land(name: str, oracle: str = "", color_identity: tuple = ()):
    """Fake ScryfallCard for a land (only the fields _has_splash_fixing inspects)."""
    sc = MagicMock()
    sc.oracle_text = oracle
    sc.color_identity = color_identity
    return sc


class TestSplashFixingDetection:
    def test_universal_search_basic_counts_as_fixing(self):
        scryfall = {"Terramorphic Expanse": _land(
            "Terramorphic Expanse",
            oracle="Tap, sacrifice: Search your library for a basic land.",
        )}
        assert _has_splash_fixing("W", ["Terramorphic Expanse"], scryfall)

    def test_mana_of_any_counts_as_fixing(self):
        scryfall = {"Rainbow Land": _land(
            "Rainbow Land",
            oracle="Tap: Add one mana of any colour.",
        )}
        assert _has_splash_fixing("R", ["Rainbow Land"], scryfall)

    def test_splash_coloured_land_counts_as_fixing(self):
        scryfall = {"Sacred Foundry": _land(
            "Sacred Foundry",
            oracle="Tap: Add R or W.",
            color_identity=("R", "W"),
        )}
        assert _has_splash_fixing("W", ["Sacred Foundry"], scryfall)
        assert _has_splash_fixing("R", ["Sacred Foundry"], scryfall)

    def test_off_colour_dual_does_not_fix_splash(self):
        # UG dual doesn't fix W.
        scryfall = {"Breeding Pool": _land(
            "Breeding Pool",
            oracle="Tap: Add U or G.",
            color_identity=("U", "G"),
        )}
        assert not _has_splash_fixing("W", ["Breeding Pool"], scryfall)

    def test_no_lands_means_no_fixing(self):
        assert not _has_splash_fixing("W", [], {})


def _spell(
    name: str, color_identity: tuple, type_line: str = "Creature — Knight",
):
    """Fake ScryfallCard for a spell castable in `color_identity`."""
    sc = MagicMock()
    sc.color_identity = color_identity
    sc.type_line = type_line
    sc.oracle_text = ""
    sc.cmc = 3
    sc.mana_cost = "{1}" + "".join("{" + c + "}" for c in color_identity)
    return sc


class TestSelectSplashes:
    def _card_map_with_powers(self, name_to_gihwr: dict[str, float]) -> dict:
        cm: dict = {}
        for name, gihwr in name_to_gihwr.items():
            cr = MagicMock()
            cr.deck_colors = {
                "All Decks": {"gihwr": gihwr, "ata": 5.0, "iwd": 0.05},
                "B": {"gihwr": gihwr, "ata": 5.0, "iwd": 0.05},
                "G": {"gihwr": gihwr, "ata": 5.0, "iwd": 0.05},
                "BG": {"gihwr": gihwr, "ata": 5.0, "iwd": 0.05},
            }
            cm[name] = cr
        return cm

    def test_picks_strongest_splash_with_fixing(self, monkeypatch):
        """Moment-of-Reckoning style: BG pool, W splash card, universal fixer in pool."""
        # Patch _card_power so we can hand-set per-card power without invoking the
        # full blended-WR math.
        from common.inference import deck_builder

        def fake_power(name, *_a, **_k):
            return {"weak_bg": 0.40, "strong_w_splash": 0.65, "mid_bg": 0.55}.get(name, 0.0)

        monkeypatch.setattr(deck_builder, "_card_power", fake_power)

        scryfall = {
            "weak_bg": _spell("weak_bg", ("B", "G")),
            "mid_bg": _spell("mid_bg", ("B", "G")),
            "strong_w_splash": _spell("strong_w_splash", ("B", "W")),
            "Terramorphic Expanse": _land(
                "Terramorphic Expanse",
                oracle="Search your library for a basic land.",
            ),
        }
        sm = _set_metrics()
        cm = self._card_map_with_powers(
            {"weak_bg": 0.50, "mid_bg": 0.55, "strong_w_splash": 0.62},
        )

        selected = _select_splashes(
            deck_colors=["B", "G"],
            main_deck=["weak_bg", "mid_bg"],
            pool_names=["weak_bg", "mid_bg", "strong_w_splash", "Terramorphic Expanse"],
            scryfall_cards=scryfall,
            nonbasic_lands=["Terramorphic Expanse"],
            card_map=cm,
            set_metrics=sm,
            archetype_key="BG",
            splashable_colors=["W"],
        )
        assert len(selected) == 1
        name, sc, splash_color, power = selected[0]
        assert name == "strong_w_splash"
        assert splash_color == "W"
        assert power == 0.65

    def test_no_fixing_means_no_splash(self, monkeypatch):
        """Even a strong splash card is rejected if there's no fixing source."""
        from common.inference import deck_builder
        monkeypatch.setattr(deck_builder, "_card_power", lambda name, *_a, **_k: 0.8)

        scryfall = {
            "main": _spell("main", ("B", "G")),
            "strong_w_splash": _spell("strong_w_splash", ("B", "W")),
        }
        sm = _set_metrics()
        cm = self._card_map_with_powers({"main": 0.55, "strong_w_splash": 0.60})

        selected = _select_splashes(
            deck_colors=["B", "G"],
            main_deck=["main"],
            pool_names=["main", "strong_w_splash"],
            scryfall_cards=scryfall,
            nonbasic_lands=[],  # no fixing
            card_map=cm,
            set_metrics=sm,
            archetype_key="BG",
            splashable_colors=["W"],
        )
        assert selected == []

    def test_weaker_splash_is_rejected(self, monkeypatch):
        """A splash card must beat the weakest main-deck card to be accepted."""
        from common.inference import deck_builder
        # Main deck cards are all power 0.7; splash card is 0.5 — should be rejected.
        powers = {"strong1": 0.7, "strong2": 0.7, "weak_splash": 0.5}
        monkeypatch.setattr(deck_builder, "_card_power", lambda name, *_a, **_k: powers.get(name, 0.0))

        scryfall = {
            "strong1": _spell("strong1", ("B", "G")),
            "strong2": _spell("strong2", ("B", "G")),
            "weak_splash": _spell("weak_splash", ("B", "W")),
            "Plains-source": _land("Plains-source", oracle="Add W.", color_identity=("W",)),
        }
        sm = _set_metrics()
        cm = self._card_map_with_powers({"strong1": 0.6, "strong2": 0.6, "weak_splash": 0.5})

        selected = _select_splashes(
            deck_colors=["B", "G"],
            main_deck=["strong1", "strong2"],
            pool_names=["strong1", "strong2", "weak_splash", "Plains-source"],
            scryfall_cards=scryfall,
            nonbasic_lands=["Plains-source"],
            card_map=cm,
            set_metrics=sm,
            archetype_key="BG",
            splashable_colors=["W"],
        )
        assert selected == [], (
            "weak splash should be rejected when all main_deck slots are stronger"
        )

    def test_caps_at_max_splash_cards(self, monkeypatch):
        """Even if many splash candidates qualify, we accept at most MAX_SPLASH_CARDS."""
        from common.inference import deck_builder

        powers = {f"main{i}": 0.3 for i in range(5)}
        for i in range(5):
            powers[f"splash{i}"] = 0.9
        monkeypatch.setattr(deck_builder, "_card_power", lambda name, *_a, **_k: powers.get(name, 0.0))

        scryfall = {
            **{f"main{i}": _spell(f"main{i}", ("B", "G")) for i in range(5)},
            **{f"splash{i}": _spell(f"splash{i}", ("B", "W")) for i in range(5)},
            "Plains-source": _land("Plains-source", oracle="Add W.", color_identity=("W",)),
        }
        sm = _set_metrics()
        cm = self._card_map_with_powers(
            {f"main{i}": 0.5 for i in range(5)}
            | {f"splash{i}": 0.7 for i in range(5)}
        )

        selected = _select_splashes(
            deck_colors=["B", "G"],
            main_deck=[f"main{i}" for i in range(5)],
            pool_names=[f"main{i}" for i in range(5)]
                + [f"splash{i}" for i in range(5)] + ["Plains-source"],
            scryfall_cards=scryfall,
            nonbasic_lands=["Plains-source"],
            card_map=cm,
            set_metrics=sm,
            archetype_key="BG",
            splashable_colors=["W"],
        )
        assert len(selected) == MAX_SPLASH_CARDS
