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
    TROPHY_CARD_POWER_BONUS,
    TROPHY_DECK_SCORE_BONUS,
    TARGET_SPELLS,
    _has_splash_fixing,
    _holistic_score,
    _is_fixing_land,
    _multicolor_prior_candidates,
    _select_splashes,
    suggest_decks,
)
from common.data.trophy_deck_prior import (
    TrophyArchetypePrior,
    TrophyDeckPrior,
    build_prior_from_trophy_rows,
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


class TestTrophyDeckPrior:
    def test_card_power_gets_bounded_trophy_prior_bonus(self):
        from common.inference.deck_builder import _card_power

        prior = TrophyDeckPrior(
            set_code="TST",
            archetypes={
                "BG": TrophyArchetypePrior(
                    deck_count=10,
                    card_scores={"synergy_card": 1.0},
                ),
            },
        )
        card_map = _card_map({"synergy_card": 0.55})
        sm = _set_metrics()

        without = _card_power("synergy_card", card_map, sm, "BG", 22)
        with_prior = _card_power("synergy_card", card_map, sm, "BG", 22, prior)

        assert with_prior == pytest.approx(without + TROPHY_CARD_POWER_BONUS)

    def test_holistic_score_uses_trophy_prior_without_17lands_data(self):
        prior = TrophyDeckPrior(
            set_code="TST",
            archetypes={
                "BG": TrophyArchetypePrior(
                    deck_count=10,
                    card_scores={"a": 1.0, "b": 1.0},
                    pair_scores={"a\tb": 1.0},
                    avg_creatures=2.0,
                    avg_noncreatures=0.0,
                    avg_cmc=2.0,
                ),
            },
        )
        scryfall = {
            "a": _spell("a", ("B",)),
            "b": _spell("b", ("G",)),
        }

        score = _holistic_score(["a", "b"], None, None, scryfall, "BG", prior)

        assert 0.0 < score <= TROPHY_DECK_SCORE_BONUS

    def test_suggest_decks_prior_can_choose_more_trophy_like_card(self):
        from common.inference.pool_analyzer import ScryfallCard

        def spell(name: str) -> ScryfallCard:
            return ScryfallCard(
                name=name,
                mana_cost="{1}{B}",
                cmc=2,
                type_line="Creature",
                oracle_text="",
                colors=["B"],
                color_identity=["B"],
                keywords=[],
                rarity="common",
            )

        scryfall = {
            "high_prior": spell("high_prior"),
            "low_prior": spell("low_prior"),
        }
        prior = TrophyDeckPrior(
            set_code="TST",
            archetypes={
                "WB": TrophyArchetypePrior(
                    deck_count=10,
                    card_scores={"high_prior": 1.0, "low_prior": 0.0},
                    avg_creatures=1.0,
                    avg_noncreatures=0.0,
                    avg_cmc=2.0,
                ),
            },
        )

        result = suggest_decks(
            ["low_prior", "high_prior"],
            scryfall,
            card_map=None,
            set_metrics=None,
            trophy_prior=prior,
        )

        top = next(iter(result.values()))
        assert top.main_deck[0] == "high_prior"

    def test_prior_builder_keeps_true_multicolor_archetype(self):
        scryfall = {
            "white": _spell("white", ("W",)),
            "black": _spell("black", ("B",)),
            "green": _spell("green", ("G",)),
        }
        prior = build_prior_from_trophy_rows(
            [{"deck_white": 1, "deck_black": 1, "deck_green": 1}],
            deck_columns=["deck_white", "deck_black", "deck_green"],
            set_code="TST",
            scryfall_cards=scryfall,
            min_pair_count=1,
        )

        assert any(len(key) == 2 for key in prior.archetypes)
        assert "WBG" in prior.archetypes

    def test_multicolor_prior_candidate_can_build_three_color_deck(self):
        from common.inference.pool_analyzer import ScryfallCard

        scryfall: dict[str, ScryfallCard] = {}
        pool: list[str] = []
        card_scores: dict[str, float] = {}
        for i in range(8):
            name = f"black{i}"
            scryfall[name] = ScryfallCard(
                name=name, mana_cost="{1}{B}", cmc=2,
                type_line="Creature", oracle_text="", colors=["B"],
                color_identity=["B"], keywords=[], rarity="common",
            )
            pool.append(name)
            card_scores[name] = 0.8
        for i in range(8):
            name = f"white_green{i}"
            scryfall[name] = ScryfallCard(
                name=name, mana_cost="{1}{W}{G}", cmc=3,
                type_line="Creature", oracle_text="", colors=["W", "G"],
                color_identity=["W", "G"], keywords=[], rarity="common",
            )
            pool.append(name)
            card_scores[name] = 1.0
        # 3C needs >= max(len-1, 2) = 2 fixers under the tightened gate.
        scryfall["any_fixer"] = ScryfallCard(
            name="any_fixer", mana_cost="", cmc=0,
            type_line="Land", oracle_text="Tap: Add one mana of any colour.",
            colors=[], color_identity=[], keywords=[], rarity="common",
        )
        scryfall["wb_dual"] = ScryfallCard(
            name="wb_dual", mana_cost="", cmc=0,
            type_line="Land", oracle_text="Tap: Add W or B.",
            colors=[], color_identity=["W", "B"], keywords=[], rarity="common",
        )
        pool.append("any_fixer")
        pool.append("wb_dual")
        prior = TrophyDeckPrior(
            set_code="TST",
            archetypes={
                "WBG": TrophyArchetypePrior(
                    deck_count=10,
                    card_scores=card_scores,
                    avg_creatures=16.0,
                    avg_noncreatures=0.0,
                    avg_cmc=2.5,
                ),
            },
        )

        result = suggest_decks(
            pool, scryfall, card_map=None, set_metrics=None, trophy_prior=prior,
        )

        assert next(iter(result)) == "WBG"

    def test_trophy_fallback_stays_below_baseline_wr(self):
        """Unrated trophy cards must not outrank a rated card at the format mean."""
        from common.inference.deck_builder import _card_power, _trophy_fallback_power

        sm = _set_metrics(mean=0.54)
        sm.baseline_wr = 0.54
        prior = TrophyDeckPrior(
            set_code="TST",
            archetypes={
                "BG": TrophyArchetypePrior(
                    deck_count=10,
                    # Strongest possible trophy signal.
                    card_scores={"unrated_trophy_card": 1.0},
                ),
            },
        )

        rated_at_baseline = _card_power(
            "average_card", _card_map({"average_card": 0.54}), sm, "BG", 22, prior,
        )
        unrated_in_trophies = _card_power(
            "unrated_trophy_card", _card_map({}), sm, "BG", 22, prior,
        )

        assert unrated_in_trophies < rated_at_baseline, (
            f"unrated trophy card power {unrated_in_trophies:.3f} must stay"
            f" below baseline-rated card power {rated_at_baseline:.3f}"
        )
        # Strongest fallback is still well under baseline (0.54 - 0.05 = 0.49).
        assert _trophy_fallback_power(sm, 1.0) <= sm.baseline_wr - 0.04

    def test_creature_soft_cap_does_not_drop_strongest_creatures(self):
        """When noncreatures are scarce the cap must lift, not skip strong creatures."""
        from common.inference.pool_analyzer import ScryfallCard

        def make(name: str, type_line: str, gihwr: float) -> tuple:
            sc = ScryfallCard(
                name=name, mana_cost="{1}{B}", cmc=2,
                type_line=type_line, oracle_text="", colors=["B"],
                color_identity=["B"], keywords=[], rarity="common",
            )
            return sc, gihwr

        scryfall: dict[str, ScryfallCard] = {}
        gihwrs: dict[str, float] = {}
        # 20 creatures sorted strongest → weakest by GIH WR.
        for i in range(20):
            name = f"creature_{i:02d}"
            sc, wr = make(name, "Creature", 0.70 - i * 0.005)
            scryfall[name] = sc
            gihwrs[name] = wr
        # 5 noncreatures, weakest of the lot.
        for i in range(5):
            name = f"noncreature_{i}"
            sc, wr = make(name, "Instant", 0.40 + i * 0.001)
            scryfall[name] = sc
            gihwrs[name] = wr

        card_map = _card_map(gihwrs)
        # All cards rate identically across archetypes for simplicity.
        for cr in card_map.values():
            cr.deck_colors["BB"] = cr.deck_colors["All Decks"]
        sm = _set_metrics(mean=0.54, std=0.04)
        sm.baseline_wr = 0.54

        prior = TrophyDeckPrior(
            set_code="TST",
            archetypes={
                "BB": TrophyArchetypePrior(
                    deck_count=10,
                    card_scores={n: 1.0 for n in scryfall},
                    # avg_creatures=10 would have produced soft_cap=13;
                    # with only 5 noncreatures available the new code should
                    # raise the effective budget to 18 = TARGET_SPELLS - 5.
                    avg_creatures=10.0,
                    avg_noncreatures=10.0,
                    avg_cmc=2.0,
                ),
            },
        )

        result = suggest_decks(
            list(scryfall),
            scryfall,
            card_map=card_map,
            set_metrics=sm,
            trophy_prior=prior,
        )

        top = next(iter(result.values()))
        # The 5 strongest creatures (creature_00..04) must all survive — under
        # the old algorithm the strongest creatures past the cap were skipped
        # in favour of weaker fillers later in the sort.
        for i in range(5):
            assert f"creature_{i:02d}" in top.main_deck, (
                f"strong creature_{i:02d} dropped by the soft cap"
            )
        # And we should still end up at ~TARGET_SPELLS spells.
        assert len(top.main_deck) >= TARGET_SPELLS - 2

    def test_multicolor_prior_rejects_5c_with_insufficient_fixing(self):
        """A 5C trophy archetype with only 3 fixers must be filtered out."""
        from common.inference.pool_analyzer import ScryfallCard

        # Pool: a single colourless universal fixer + two duals.
        # That's 3 fixing sources. 5C needs max(5-1, 2) = 4 sources.
        scryfall = {
            "any_fixer": ScryfallCard(
                name="any_fixer", mana_cost="", cmc=0,
                type_line="Land", oracle_text="Tap: Add one mana of any colour.",
                colors=[], color_identity=[], keywords=[], rarity="common",
            ),
            "wu_dual": ScryfallCard(
                name="wu_dual", mana_cost="", cmc=0,
                type_line="Land", oracle_text="Tap: Add W or U.",
                colors=[], color_identity=["W", "U"], keywords=[],
                rarity="common",
            ),
            "br_dual": ScryfallCard(
                name="br_dual", mana_cost="", cmc=0,
                type_line="Land", oracle_text="Tap: Add B or R.",
                colors=[], color_identity=["B", "R"], keywords=[],
                rarity="common",
            ),
        }
        ranked_colors = ["W", "U", "B", "R", "G"]
        prior = TrophyDeckPrior(
            set_code="TST",
            archetypes={
                "WUBRG": TrophyArchetypePrior(
                    deck_count=20,
                    card_scores={},
                    avg_creatures=15.0,
                    avg_noncreatures=8.0,
                    avg_cmc=3.0,
                ),
            },
        )

        cands = _multicolor_prior_candidates(
            ranked_colors=ranked_colors,
            pool_names=list(scryfall),
            scryfall_cards=scryfall,
            trophy_prior=prior,
        )

        assert all(len(c) != 5 for c in cands), (
            "5C trophy candidate slipped through with only 3 fixers"
        )

    def test_multicolor_prior_accepts_3c_with_two_fixers(self):
        """A 3C trophy archetype with 2 fixers should still pass the gate."""
        from common.inference.pool_analyzer import ScryfallCard

        scryfall = {
            "any_fixer": ScryfallCard(
                name="any_fixer", mana_cost="", cmc=0,
                type_line="Land", oracle_text="Tap: Add one mana of any colour.",
                colors=[], color_identity=[], keywords=[], rarity="common",
            ),
            "wb_dual": ScryfallCard(
                name="wb_dual", mana_cost="", cmc=0,
                type_line="Land", oracle_text="Tap: Add W or B.",
                colors=[], color_identity=["W", "B"], keywords=[],
                rarity="common",
            ),
        }
        prior = TrophyDeckPrior(
            set_code="TST",
            archetypes={
                "WBG": TrophyArchetypePrior(
                    deck_count=10, card_scores={},
                    avg_creatures=15.0, avg_noncreatures=8.0, avg_cmc=2.5,
                ),
            },
        )

        cands = _multicolor_prior_candidates(
            ranked_colors=["W", "B", "G", "U", "R"],
            pool_names=list(scryfall),
            scryfall_cards=scryfall,
            trophy_prior=prior,
        )

        assert ("W", "B", "G") in cands


class TestIsFixingLand:
    def test_universal_fixer(self):
        from common.inference.pool_analyzer import ScryfallCard
        card = ScryfallCard(
            name="x", mana_cost="", cmc=0, type_line="Land",
            oracle_text="Add one mana of any colour.",
            colors=[], color_identity=[], keywords=[], rarity="common",
        )
        assert _is_fixing_land(card, {"W", "B"}, min_overlap=2)
        assert _is_fixing_land(card, {"W"}, min_overlap=1)

    def test_dual_land_fixes_for_multi_color(self):
        from common.inference.pool_analyzer import ScryfallCard
        card = ScryfallCard(
            name="x", mana_cost="", cmc=0, type_line="Land",
            oracle_text="Tap: Add W or B.",
            colors=[], color_identity=["W", "B"], keywords=[], rarity="common",
        )
        assert _is_fixing_land(card, {"W", "B", "G"}, min_overlap=2)
        assert _is_fixing_land(card, {"W"}, min_overlap=1)
        # Off-colour overlap (only W matches, but min_overlap=2) → reject.
        assert not _is_fixing_land(card, {"W", "U"}, min_overlap=2)

    def test_creature_land_is_not_a_fixer(self):
        from common.inference.pool_analyzer import ScryfallCard
        card = ScryfallCard(
            name="x", mana_cost="", cmc=0, type_line="Land Creature",
            oracle_text="Tap: Add W or B.",
            colors=[], color_identity=["W", "B"], keywords=[], rarity="common",
        )
        assert not _is_fixing_land(card, {"W", "B"}, min_overlap=2)


def _land(name: str, oracle: str = "", color_identity: tuple = ()):
    """Fake ScryfallCard for a land — sets the fields _is_fixing_land inspects."""
    sc = MagicMock()
    sc.type_line = "Land"
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


class TestPlayableFloorFilter:
    """The dropdown should drop archetypes with too few playables.

    A reported failure: a user's 42-card SOS pool returned a top
    recommendation with only 3 main_deck spells + 37 lands. The
    pool can build a healthy UR deck; the thin alternative is
    pure noise next to it and shouldn't be offered.
    """

    def test_thin_alternatives_dropped_when_strong_top_exists(self):
        """If top archetype has 23 playables and an alternative has 3, drop the alternative."""
        # Two synthetic pools: a strong UR build with castable spells +
        # a sparse WB "build" with only 3 castable cards. The score-based
        # filter (>= top - 20) keeps both if both score above the floor;
        # the playable-floor filter should still drop the 3-card option.
        from common.inference.pool_analyzer import ScryfallCard

        def _spell(name, cost, type_line="Instant"):
            return ScryfallCard(
                name=name, mana_cost=cost, cmc=2, type_line=type_line,
                oracle_text="", colors=[], color_identity=[],
                keywords=[], rarity="common",
            )

        scryfall = {}
        pool = []
        # 23 strong UR spells.
        for i in range(23):
            n = f"UR{i}"
            scryfall[n] = _spell(n, "{1}{U}{R}")
            pool.append(n)
        # 3 weak WB spells (would form a 3-card alternative).
        for i in range(3):
            n = f"WB{i}"
            scryfall[n] = _spell(n, "{1}{W}{B}")
            pool.append(n)

        # No card_map → all _holistic_score returns -1.0. The score-based
        # filter then keeps everything within 20 of the top (also -1.0),
        # so all archetypes pass the score filter. The playable floor
        # must drop the 3-card WB archetype.
        result = suggest_decks(pool, scryfall, card_map=None, set_metrics=None)

        assert "UR" in result, f"UR (the buildable archetype) missing: {list(result)}"
        # Anything with <14 main_deck should be filtered unless it's the only option.
        for key, sug in result.items():
            if key != next(iter(result)):  # not the top entry
                assert len(sug.main_deck) >= 14, (
                    f"Alternative {key} has {len(sug.main_deck)} main_deck "
                    f"cards — should have been dropped"
                )

    def test_keeps_top_even_when_thin(self):
        """If the *only* recommendation is thin, keep it — empty dropdown is worse."""
        from common.inference.pool_analyzer import ScryfallCard

        def _spell(name, cost):
            return ScryfallCard(
                name=name, mana_cost=cost, cmc=2, type_line="Instant",
                oracle_text="", colors=[], color_identity=[],
                keywords=[], rarity="common",
            )

        # Pool of only 3 castable cards across all colour combos.
        scryfall = {f"a{i}": _spell(f"a{i}", "{1}{W}{B}") for i in range(3)}
        pool = list(scryfall.keys())

        result = suggest_decks(pool, scryfall, card_map=None, set_metrics=None)

        # Some archetype must be returned (the dropdown can't be empty).
        assert result, "every pool should produce at least one suggestion"


class TestSplitCardLookup:
    """17Lands keys split cards by front face only ("Scheming Silvertongue"),
    while Scryfall and the in-game pool use the full name ("Scheming Silvertongue
    // Sign in Blood"). Without a front-face fallback, _card_power and
    _holistic_score miss the 17Lands data for every split card, the card falls
    through to trophy_fallback_power (≈0.10–0.15), and gets silently sideboarded.
    """

    def test_card_power_falls_back_to_front_face_name(self):
        from common.inference.deck_builder import _card_power

        # 17Lands keyed by front face only.
        card_map = _card_map({"Scheming Silvertongue": 0.591})
        sm = _set_metrics(mean=0.54, std=0.04)

        power = _card_power(
            "Scheming Silvertongue // Sign in Blood",
            card_map, sm, "BG", 22,
        )

        assert power > 0.5, (
            f"power={power:.3f} — split card likely fell through to "
            f"trophy_fallback_power instead of the front-face 17L entry."
        )

    def test_holistic_score_uses_front_face_for_split_cards(self):
        full_name = "Studious First-Year // Rampant Growth"
        card_map = _card_map({"Studious First-Year": 0.57})
        sm = _set_metrics(mean=0.54, std=0.04)

        score = _holistic_score([full_name], card_map, sm, {}, "BG")

        assert score > 0, (
            f"_holistic_score returned {score} — split card not resolved by "
            f"front-face fallback."
        )

    def test_suggest_decks_keeps_strong_split_card_in_main_deck(self):
        """End-to-end: a strong SOS Prepared card in a B pool reaches main_deck."""
        from common.inference.pool_analyzer import ScryfallCard

        scryfall: dict[str, ScryfallCard] = {}
        pool: list[str] = []
        for i in range(25):
            n = f"weak_b_{i}"
            scryfall[n] = ScryfallCard(
                name=n, mana_cost="{1}{B}", cmc=2,
                type_line="Creature", oracle_text="", colors=["B"],
                color_identity=["B"], keywords=[], rarity="common",
            )
            pool.append(n)
        split_name = "Scheming Silvertongue // Sign in Blood"
        scryfall[split_name] = ScryfallCard(
            name=split_name, mana_cost="{1}{B} // {B}{B}", cmc=2,
            type_line="Creature — Vampire Warlock // Sorcery",
            oracle_text="", colors=["B"], color_identity=["B"],
            keywords=["Prepared"], rarity="rare",
        )
        pool.append(split_name)

        # The split card is the strongest, but stored under the front face only.
        card_map = _card_map(
            {f"weak_b_{i}": 0.50 for i in range(25)}
            | {"Scheming Silvertongue": 0.61},
        )
        sm = _set_metrics(mean=0.54, std=0.04)
        sm.baseline_wr = 0.54

        result = suggest_decks(pool, scryfall, card_map=card_map, set_metrics=sm)

        top = next(iter(result.values()))
        assert split_name in top.main_deck, (
            f"strong SOS Prepared card silently sideboarded. "
            f"main_deck = {top.main_deck}"
        )


class TestSplitCardCastability:
    """parse_pips reads every {X} token across both faces, so _is_castable
    rejects a card like Bind // Liberate ({1}{G} // {1}{W}) from a mono-G deck —
    even though the front face can be cast in mono-G. The fix is to treat
    each ' // '-separated face independently."""

    def test_split_with_different_face_colors_castable_in_either(self):
        from common.inference.deck_builder import _is_castable
        from common.inference.pool_analyzer import ScryfallCard

        card = ScryfallCard(
            name="Bind // Liberate", mana_cost="{1}{G} // {1}{W}",
            cmc=2, type_line="Sorcery // Instant",
            oracle_text="", colors=["G", "W"], color_identity=["G", "W"],
            keywords=[], rarity="common",
        )
        assert _is_castable(card, ["G"]), "front face {1}{G} fits mono-G"
        assert _is_castable(card, ["W"]), "back face {1}{W} fits mono-W"
        assert _is_castable(card, ["G", "W"])
        assert not _is_castable(card, ["R"]), "neither face fits mono-R"

    def test_split_with_same_face_colors_castable_in_that_color(self):
        """SOS Prepared like Scheming Silvertongue (both faces B) — mono-B casts it."""
        from common.inference.deck_builder import _is_castable
        from common.inference.pool_analyzer import ScryfallCard

        card = ScryfallCard(
            name="Scheming Silvertongue // Sign in Blood",
            mana_cost="{1}{B} // {B}{B}",
            cmc=2, type_line="Creature // Sorcery",
            oracle_text="", colors=["B"], color_identity=["B"],
            keywords=["Prepared"], rarity="rare",
        )
        assert _is_castable(card, ["B"])
        assert _is_castable(card, ["U", "B"])
        assert not _is_castable(card, ["U"])

    def test_split_face_castability_is_per_face(self):
        """Multi-color front + mono back: castable when at least one face fits."""
        from common.inference.deck_builder import _is_castable
        from common.inference.pool_analyzer import ScryfallCard

        card = ScryfallCard(
            name="Bicolor // Mono", mana_cost="{U}{R} // {1}{R}",
            cmc=2, type_line="Creature // Sorcery",
            oracle_text="", colors=["U", "R"], color_identity=["U", "R"],
            keywords=[], rarity="rare",
        )
        assert _is_castable(card, ["U", "R"])
        assert _is_castable(card, ["R"]), "back face {1}{R} fits mono-R"
        assert not _is_castable(card, ["U"]), "front needs R, back needs R"
        assert not _is_castable(card, ["U", "B"])

    def test_non_split_card_castability_unchanged(self):
        """The single-face path must keep behaving exactly as before."""
        from common.inference.deck_builder import _is_castable
        from common.inference.pool_analyzer import ScryfallCard

        card = ScryfallCard(
            name="Plain", mana_cost="{1}{U}{B}", cmc=3,
            type_line="Creature", oracle_text="",
            colors=["U", "B"], color_identity=["U", "B"],
            keywords=[], rarity="common",
        )
        assert _is_castable(card, ["U", "B"])
        assert not _is_castable(card, ["U"])
        assert not _is_castable(card, ["B"])


# ---------------------------------------------------------------------------
# Karsten-aware mana base (spec 2026-05-23)
# ---------------------------------------------------------------------------


def _sc(
    name: str,
    mc: str,
    cmc: int,
    *,
    type_line: str = "Creature",
    oracle: str = "",
    colors: list[str] | None = None,
    ci: list[str] | None = None,
    power: str = "2",
    toughness: str = "2",
):
    """Test helper — minimal ScryfallCard with sensible defaults."""
    from common.inference.pool_analyzer import ScryfallCard

    return ScryfallCard(
        name=name,
        mana_cost=mc,
        cmc=cmc,
        type_line=type_line,
        oracle_text=oracle,
        colors=colors or [],
        color_identity=ci or [],
        keywords=[],
        rarity="common",
        power=power,
        toughness=toughness,
    )


class TestRequiredSources:
    def test_single_pip_curve(self):
        from common.inference.deck_builder import _required_sources

        # Karsten 40-card single-pip table, with _KARSTEN_RELIABILITY_DROP = 1
        # (so 80% reliability target — matches limited pro practice).
        assert _required_sources(1, 1) == 13  # 14 - 1
        assert _required_sources(1, 2) == 12
        assert _required_sources(1, 3) == 11
        assert _required_sources(1, 4) == 10
        assert _required_sources(1, 5) == 9
        assert _required_sources(1, 6) == 8
        assert _required_sources(1, 7) == 7
        assert _required_sources(1, 8) == 7

    def test_double_pip_curve(self):
        from common.inference.deck_builder import _required_sources

        assert _required_sources(2, 2) == 17
        assert _required_sources(2, 3) == 15
        assert _required_sources(2, 4) == 14
        assert _required_sources(2, 5) == 13
        assert _required_sources(2, 6) == 12

    def test_triple_pip_sentinel_unplayable(self):
        from common.inference.deck_builder import _required_sources

        # Triple pip at any CMC needs ~16-19 sources — effectively
        # unplayable in a 2-color deck. The lookup returns a high
        # sentinel so the feasibility check trips correctly.
        assert _required_sources(3, 3) >= 19
        assert _required_sources(3, 5) >= 17

    def test_clamps_high_cmc(self):
        from common.inference.deck_builder import _required_sources

        # CMC > 8 collapses to the CMC 8 row.
        assert _required_sources(1, 9) == _required_sources(1, 8)
        assert _required_sources(2, 15) == _required_sources(2, 8)

    def test_zero_pips_returns_zero(self):
        from common.inference.deck_builder import _required_sources

        assert _required_sources(0, 3) == 0
        assert _required_sources(0, 7) == 0


class TestDemandPerColor:
    def test_single_card(self):
        from common.inference.deck_builder import _demand_per_color

        cards = [_sc("Grovestrider", "{3}{G}{G}", 5, ci=["G"])]
        demand = _demand_per_color(cards, deck_colors=["G", "U"])
        # GG at CMC 5 → 14 sources at 90%, 13 at 80%.
        assert demand["G"] == 13
        assert demand.get("U", 0) == 0

    def test_max_not_sum(self):
        from common.inference.deck_builder import _demand_per_color

        # Three single-pip green cards at the same CMC: demand should be
        # one card's worth (12 at 90%, 11 at 80%), not three times that.
        cards = [
            _sc("g1", "{2}{G}", 3, ci=["G"], type_line="Sorcery"),
            _sc("g2", "{2}{G}", 3, ci=["G"], type_line="Sorcery"),
            _sc("g3", "{2}{G}", 3, ci=["G"], type_line="Sorcery"),
        ]
        demand = _demand_per_color(cards, deck_colors=["G"])
        assert demand["G"] == 11

    def test_takes_max_across_pips(self):
        from common.inference.deck_builder import _demand_per_color

        # Single-pip 6-drop vs. double-pip 4-drop: double-pip wins.
        six = _sc("Six", "{5}{G}", 6, ci=["G"], type_line="Sorcery")
        four = _sc("Four", "{2}{G}{G}", 4, ci=["G"])
        demand = _demand_per_color([six, four], deck_colors=["G"])
        # Single-pip 6: 9 - 1 = 8. Double-pip 4: 15 - 1 = 14. Max wins.
        assert demand["G"] == 14

    def test_handles_split_cards(self):
        from common.inference.deck_builder import _demand_per_color

        # {1}{G} // {2}{B}{B} — playable on either face, so demand is
        # max(single-pip G CMC 2, double-pip B CMC 4).
        split = _sc(
            "Bind // Liberate",
            "{1}{G} // {2}{B}{B}",
            2,
            ci=["B", "G"],
            type_line="Sorcery // Sorcery",
        )
        demand = _demand_per_color([split], deck_colors=["B", "G"])
        # Single-pip G at CMC 2: 13 - 1 = 12. Double-pip B at CMC 4: 15 - 1 = 14.
        assert demand["G"] == 12
        assert demand["B"] == 14


class TestFixingSourcesPerColor:
    def test_universal_fixer(self):
        from common.inference.deck_builder import _fixing_sources_per_color

        wilds = _sc(
            "Evolving Wilds",
            "",
            0,
            type_line="Land",
            oracle="search your library for a basic land card",
            ci=[],
        )
        sources = _fixing_sources_per_color(
            ["Evolving Wilds"],
            {"Evolving Wilds": wilds},
            deck_colors=["W", "U"],
        )
        # Universal fixer = 1.0 source for every deck color.
        assert sources["W"] == 1.0
        assert sources["U"] == 1.0

    def test_tapped_dual_discounted(self):
        from common.inference.deck_builder import _fixing_sources_per_color

        dual = _sc(
            "Sunken Hollow",
            "",
            0,
            type_line="Land",
            oracle="Sunken Hollow enters the battlefield tapped.\n{T}: Add {U} or {B}.",
            ci=["B", "U"],
        )
        sources = _fixing_sources_per_color(
            ["Sunken Hollow"],
            {"Sunken Hollow": dual},
            deck_colors=["B", "U"],
        )
        assert sources["B"] == 0.85
        assert sources["U"] == 0.85

    def test_untapped_dual_full(self):
        from common.inference.deck_builder import _fixing_sources_per_color

        dual = _sc(
            "Brushland",
            "",
            0,
            type_line="Land",
            oracle="{T}: Add {C}. {T}: Add {G} or {W}. Brushland deals 1 damage to you.",
            ci=["G", "W"],
        )
        sources = _fixing_sources_per_color(
            ["Brushland"],
            {"Brushland": dual},
            deck_colors=["G", "W"],
        )
        assert sources["G"] == 1.0
        assert sources["W"] == 1.0

    def test_off_color_ignored(self):
        from common.inference.deck_builder import _fixing_sources_per_color

        dual = _sc(
            "Stomping Ground",
            "",
            0,
            type_line="Land",
            oracle="{T}: Add {R} or {G}.",
            ci=["G", "R"],
        )
        sources = _fixing_sources_per_color(
            ["Stomping Ground"],
            {"Stomping Ground": dual},
            deck_colors=["W", "U"],
        )
        assert sources.get("W", 0) == 0
        assert sources.get("U", 0) == 0


class TestAllocateBasicsForDemand:
    def test_over_budget_proportional(self):
        from common.inference.deck_builder import _allocate_basics_for_demand

        # UG deck, demand G=11, U=9 → totals 20, over budget. Allocate
        # proportionally to demand: G = round(11/20 * 17) = 9,
        # U = 17 - 9 = 8.
        basics = _allocate_basics_for_demand(
            deck_colors=["G", "U"],
            demand={"G": 11, "U": 9},
            fixing_sources={"G": 0.0, "U": 0.0},
            basics_budget=17,
        )
        assert sum(basics.values()) == 17
        assert basics["G"] == 9
        assert basics["U"] == 8

    def test_under_budget_pads_primary(self):
        from common.inference.deck_builder import _allocate_basics_for_demand

        # Mono-U demand 9, budget 17 — pads to 17 islands.
        basics = _allocate_basics_for_demand(
            deck_colors=["U"],
            demand={"U": 9},
            fixing_sources={"U": 0.0},
            basics_budget=17,
        )
        assert basics["U"] == 17

    def test_fixing_subtracts_from_demand(self):
        from common.inference.deck_builder import _allocate_basics_for_demand

        # UG deck, demand G=11, U=9. One Evolving Wilds → +1 to each.
        # basics_budget = 16 (the Wilds takes a land slot). Effective
        # demand G=10, U=8 → totals 18, still over budget. Allocate
        # proportionally: G = round(10/18 * 16) = 9, U = 16 - 9 = 7.
        basics = _allocate_basics_for_demand(
            deck_colors=["G", "U"],
            demand={"G": 11, "U": 9},
            fixing_sources={"G": 1.0, "U": 1.0},
            basics_budget=16,
        )
        assert sum(basics.values()) == 16
        assert basics["G"] == 9
        assert basics["U"] == 7


class TestIsFeasible:
    def test_satisfied(self):
        from common.inference.deck_builder import _is_feasible

        # G demand 11, allocation gives 11 basics + 0 fixing = 11 sources.
        assert _is_feasible(
            demand={"G": 11, "U": 9},
            basics={"G": 11, "U": 9},
            fixing_sources={"G": 0.0, "U": 0.0},
        )

    def test_starved(self):
        from common.inference.deck_builder import _is_feasible

        # G demand 15 (the EOE GG case), basics gives 5, no G fixing.
        # 5 < 15 → infeasible.
        assert not _is_feasible(
            demand={"G": 15, "U": 12},
            basics={"G": 5, "U": 12},
            fixing_sources={"G": 0.0, "U": 0.0},
        )


class TestAdaptiveTargetLands:
    def test_aggro_runs_16(self):
        from common.inference.deck_builder import _adaptive_target_lands

        # 23 low-curve creatures, avg CMC ~2.0, no 6+ drops, no X-spells.
        cards = [
            _sc(f"Aggro{i}", "{1}{R}", 2, ci=["R"], power="2", toughness="1")
            for i in range(23)
        ]
        cmcs = [2] * 23
        assert _adaptive_target_lands(cards, cmcs) == 16

    def test_midrange_runs_17(self):
        from common.inference.deck_builder import _adaptive_target_lands

        cards = (
            [_sc(f"c2_{i}", "{1}{U}", 2, ci=["U"]) for i in range(8)]
            + [_sc(f"c3_{i}", "{2}{U}", 3, ci=["U"]) for i in range(10)]
            + [_sc(f"c4_{i}", "{3}{U}", 4, ci=["U"]) for i in range(5)]
        )
        cmcs = [2] * 8 + [3] * 10 + [4] * 5
        assert _adaptive_target_lands(cards, cmcs) == 17

    def test_high_curve_runs_18(self):
        from common.inference.deck_builder import _adaptive_target_lands

        # Avg CMC 3.4, with multiple 6-drops.
        cards = (
            [_sc(f"c2_{i}", "{1}{U}", 2, ci=["U"]) for i in range(3)]
            + [_sc(f"c3_{i}", "{2}{U}", 3, ci=["U"]) for i in range(8)]
            + [_sc(f"c4_{i}", "{3}{U}", 4, ci=["U"]) for i in range(8)]
            + [_sc(f"c6_{i}", "{5}{U}", 6, ci=["U"]) for i in range(4)]
        )
        cmcs = [2] * 3 + [3] * 8 + [4] * 8 + [6] * 4
        assert _adaptive_target_lands(cards, cmcs) == 18

    def test_mana_sinks_force_18(self):
        from common.inference.deck_builder import _adaptive_target_lands

        # Low curve but 4 X-spell mana sinks → 18 lands.
        sinks = [
            _sc(f"x_{i}", "{X}{U}", 2, ci=["U"], type_line="Sorcery")
            for i in range(2)
        ] + [
            _sc("x3", "{X}{U}{U}", 3, ci=["U"], type_line="Sorcery"),
            _sc("x4", "{X}{U}{U}{U}", 4, ci=["U"], type_line="Sorcery"),
        ]
        filler = [_sc(f"f{i}", "{1}{U}", 2, ci=["U"]) for i in range(19)]
        cards = sinks + filler
        cmcs = [c.cmc for c in cards]
        assert _adaptive_target_lands(cards, cmcs) == 18


class TestKarstenManaBase:
    def test_satisfies_demand_when_possible(self):
        from common.inference.deck_builder import _karsten_mana_base

        # UG deck: U=9 (CMC 5 single-pip), G=11 (CMC 3 single-pip).
        # Total 20 > 17 → over budget, allocate proportionally.
        cards = [
            _sc("U5", "{4}{U}", 5, ci=["U"], type_line="Sorcery"),
            _sc("G3", "{2}{G}", 3, ci=["G"], power="3", toughness="3"),
        ]
        lands = _karsten_mana_base(cards, ["G", "U"], fixing_lands=0)
        assert len(lands) == 17
        # G demand 11 vs U demand 9 → proportional 11/20*17=9, 17-9=8.
        assert lands.count("Forest") == 9
        assert lands.count("Island") == 8

    def test_eoe_regression_proportional_output(self):
        """The EOE pool that motivated the rework. The rewritten
        _karsten_mana_base allocates the best proportional split it can;
        the demotion path (Task 7) is what turns this into a sensible
        deck. This test only pins the proportional output.
        """
        from common.inference.deck_builder import _karsten_mana_base

        cards = (
            [_sc(f"U{i}", "{1}{U}", 2, ci=["U"]) for i in range(11)]
            + [_sc("UU2", "{U}{U}", 2, ci=["U"])]
            + [_sc("Godmaw", "{5}{G}{G}", 7, ci=["G"], power="3", toughness="3")]
            + [_sc("Grove", "{3}{G}{G}", 5, ci=["G"], power="3", toughness="3")]
            + [_sc("Harm", "{2}{G}{G}", 4, ci=["G"], power="3", toughness="3")]
            + [_sc("Way", "{2}{G}", 3, ci=["G"], power="3", toughness="3")]
            + [_sc("Seed", "{G}", 1, ci=["G"], type_line="Instant")]
        )
        lands = _karsten_mana_base(cards, ["G", "U"], fixing_lands=0)
        assert len(lands) == 17
        assert lands.count("Forest") + lands.count("Island") == 17

    def test_with_fixing_returns_basics_and_sources(self):
        from common.inference.deck_builder import (
            _karsten_mana_base_with_fixing,
        )

        wilds = _sc(
            "Evolving Wilds",
            "",
            0,
            type_line="Land",
            oracle="search your library for a basic land card",
            ci=[],
        )
        scryfall = {"Evolving Wilds": wilds}
        cards = [
            _sc("U3", "{2}{U}", 3, ci=["U"]),
            _sc("G3", "{2}{G}", 3, ci=["G"]),
        ]
        lands, sources = _karsten_mana_base_with_fixing(
            deck_cards=cards,
            deck_colors=["G", "U"],
            nonbasic_lands=["Evolving Wilds"],
            scryfall_cards=scryfall,
            total_land_slots=17,
        )
        # 16 basics + 1 universal fixer = 17 total slots.
        assert len(lands) == 16
        # Universal fixer contributes 1.0 to each color.
        assert sources["G"] == 1.0
        assert sources["U"] == 1.0


class TestDemoteInfeasibleMinority:
    def test_eoe_regression_raises_forests_to_floor_keeps_gg_cards(self):
        """Mostly-blue pool with three GG cards.

        Original behavior (pre-Karsten): 5 Forest / 12 Island with all
        GG cards in main deck — uncastable.

        Karsten-only behavior (intermediate): demoted to mono-U, all G
        cards cut.

        Current behavior (floor + keep-cards): UG with ≥7 Forests, all
        committed-color cards kept including GG cards. The GG cards
        will be under-supported in practice (8 Forests for a GG
        4-drop = 57% on curve) but the user prefers this over the
        algorithm cutting their cards.
        """
        from common.inference.deck_builder import (
            suggest_decks,
            MIN_BASICS_PER_COMMIT_COLOR,
        )

        pool_cards = (
            [_sc(f"BlueU{i}", "{1}{U}", 2, ci=["U"]) for i in range(8)]
            + [_sc(f"BlueUU{i}", "{U}{U}", 2, ci=["U"]) for i in range(3)]
            + [_sc("Mouth", "{6}{U}", 7, ci=["U"])]
            + [_sc("Mechanozoa1", "{4}{U}{U}", 6, ci=["U"])]
            + [_sc("Mechanozoa2", "{4}{U}{U}", 6, ci=["U"])]
            + [_sc("Mechanozoa3", "{4}{U}{U}", 6, ci=["U"])]
            + [
                _sc("DivertA", "{1}{U}", 2, ci=["U"], type_line="Instant"),
                _sc("DivertB", "{1}{U}", 2, ci=["U"], type_line="Instant"),
                _sc("DivertC", "{1}{U}", 2, ci=["U"], type_line="Instant"),
                _sc("DivertD", "{1}{U}", 2, ci=["U"], type_line="Instant"),
            ]
            + [_sc("Godmaw", "{5}{G}{G}", 7, ci=["G"])]
            + [_sc("Grovestrider", "{3}{G}{G}", 5, ci=["G"])]
            + [_sc("Harmonizer", "{2}{G}{G}", 4, ci=["G"])]
            + [_sc("Wayfarer", "{2}{G}", 3, ci=["G"])]
            + [_sc("SeedshipImpact", "{G}", 1, ci=["G"], type_line="Instant")]
        )
        scryfall = {c.name: c for c in pool_cards}
        pool_names = [c.name for c in pool_cards]

        suggestions = suggest_decks(
            pool_names=pool_names, scryfall_cards=scryfall,
        )
        assert suggestions, "expected at least one suggestion"
        top = next(iter(suggestions.values()))

        # The 5-Forest / 12-Island anti-pattern must not recur.
        assert top.lands.count("Forest") >= MIN_BASICS_PER_COMMIT_COLOR, (
            f"Forest count {top.lands.count('Forest')} below floor; "
            f"committed G color must reach {MIN_BASICS_PER_COMMIT_COLOR} basics. "
            f"Lands: {top.lands}"
        )
        assert top.lands.count("Island") >= MIN_BASICS_PER_COMMIT_COLOR, (
            f"Island count {top.lands.count('Island')} below floor."
        )


class TestSuggestDecksAdaptiveLandCount:
    def test_aggro_pool_runs_16_lands(self):
        """All-1-and-2-drop pool should produce a 16-land build."""
        from common.inference.deck_builder import suggest_decks

        # Mono-U so the WU pair (default when top_colors pads to two
        # colors) builds a real deck rather than an empty one.
        pool = (
            [_sc(f"Aggro{i}", "{U}", 1, ci=["U"]) for i in range(12)]
            + [_sc(f"Med{i}", "{1}{U}", 2, ci=["U"]) for i in range(20)]
        )
        scryfall = {c.name: c for c in pool}
        suggestions = suggest_decks(
            pool_names=[c.name for c in pool],
            scryfall_cards=scryfall,
        )
        top = next(iter(suggestions.values()))
        assert top.land_count == 16, (
            f"Aggro deck got {top.land_count} lands, expected 16. "
            f"avg_cmc={top.avg_cmc}"
        )

    def test_high_curve_pool_runs_18_lands(self):
        """Pool with many 5-6 drops should produce an 18-land build."""
        from common.inference.deck_builder import suggest_decks

        pool = (
            [_sc(f"Big{i}", "{5}{U}", 6, ci=["U"]) for i in range(8)]
            + [_sc(f"Mid4{i}", "{3}{U}", 4, ci=["U"]) for i in range(10)]
            + [_sc(f"Mid3{i}", "{2}{U}", 3, ci=["U"]) for i in range(10)]
        )
        scryfall = {c.name: c for c in pool}
        suggestions = suggest_decks(
            pool_names=[c.name for c in pool],
            scryfall_cards=scryfall,
        )
        top = next(iter(suggestions.values()))
        assert top.land_count == 18, (
            f"High-curve deck got {top.land_count} lands, expected 18. "
            f"avg_cmc={top.avg_cmc}"
        )

    def test_total_lands_correct_when_pool_has_nonbasic_lands(self):
        """Regression: nonbasic lands must not be subtracted twice.

        Caught by the audit harness when post-Karsten snapshots showed
        land_count of 11-14 on pools that contained Evolving Wilds.
        _karsten_mana_base subtracts fixing_lands internally to derive
        the basics budget; the caller passes the TOTAL target.
        """
        from common.inference.deck_builder import suggest_decks

        wilds = _sc(
            "Evolving Wilds",
            "",
            0,
            type_line="Land",
            oracle="search your library for a basic land card",
            ci=[],
        )
        # Midrange UB pool (avg CMC ~2.7 → targets 17 lands) + 4 Wilds.
        spells = (
            [_sc(f"U2_{i}", "{1}{U}", 2, ci=["U"]) for i in range(6)]
            + [_sc(f"U3_{i}", "{2}{U}", 3, ci=["U"]) for i in range(6)]
            + [_sc(f"B2_{i}", "{1}{B}", 2, ci=["B"]) for i in range(6)]
            + [_sc(f"B3_{i}", "{2}{B}", 3, ci=["B"]) for i in range(7)]
        )
        pool_names = [c.name for c in spells] + ["Evolving Wilds"] * 4
        scryfall = {c.name: c for c in spells} | {"Evolving Wilds": wilds}

        suggestions = suggest_decks(
            pool_names=pool_names, scryfall_cards=scryfall,
        )
        top = next(iter(suggestions.values()))
        # Midrange curve → 17 lands; 4 Wilds + 13 basics = 17.
        assert top.land_count == 17, (
            f"Expected 17 total lands, got {top.land_count}. "
            f"basics={top.lands}, nonbasics={top.nonbasic_lands}, "
            f"main_deck_size={len(top.main_deck)}"
        )
        assert len(top.nonbasic_lands) == 4
        assert len(top.lands) == 13


class TestHolisticScoreTrophyFallback:
    def test_falls_back_to_trophy_when_card_map_has_no_gihwr(self):
        """When card_map is present but every entry has gihwr == 0
        (e.g. a brand-new set that 17Lands hasn't aggregated game data
        for yet), `_holistic_score` should fall back to the trophy-prior
        score rather than returning the -1.0 sentinel. Otherwise an EOE
        deck score reads "-1.00" in the admin GUI / Discord summary
        even though the recommendation itself is good.
        """
        from unittest.mock import MagicMock
        from common.inference.deck_builder import (
            _holistic_score,
            TROPHY_DECK_SCORE_BONUS,
        )

        # card_map populated but with gihwr=0 everywhere.
        def _empty_cr() -> MagicMock:
            cr = MagicMock()
            cr.deck_colors = {"All Decks": {"gihwr": 0.0}}
            return cr

        card_map = {f"card{i}": _empty_cr() for i in range(5)}
        sm = MagicMock()
        sm.get_metrics.return_value = (0.0, 1.0)

        # Trophy prior returns a known score for the deck.
        trophy = MagicMock()
        trophy.score_deck.return_value = 0.5

        deck = [f"card{i}" for i in range(5)]
        score = _holistic_score(deck, card_map, sm, {}, "UR", trophy)

        # Without the fix this would be -1.0; with the fix it's
        # 0.5 * TROPHY_DECK_SCORE_BONUS = 4.0.
        assert score == 0.5 * TROPHY_DECK_SCORE_BONUS, (
            f"Expected trophy fallback score "
            f"{0.5 * TROPHY_DECK_SCORE_BONUS}, got {score}"
        )

    def test_returns_minus_one_when_no_gihwr_and_no_trophy_prior(self):
        from unittest.mock import MagicMock
        from common.inference.deck_builder import _holistic_score

        def _empty_cr() -> MagicMock:
            cr = MagicMock()
            cr.deck_colors = {"All Decks": {"gihwr": 0.0}}
            return cr

        card_map = {"a": _empty_cr()}
        sm = MagicMock()
        sm.get_metrics.return_value = (0.0, 1.0)

        score = _holistic_score(["a"], card_map, sm, {}, "UR", trophy_prior=None)
        assert score == -1.0


class TestBasicsFloor:
    """A non-splash committed color must receive at least
    MIN_BASICS_PER_COMMIT_COLOR basic lands, taking from over-floor
    committed colors when proportional allocation falls short.
    """

    def test_floor_bumps_under_supplied_minority(self):
        from common.inference.deck_builder import (
            _allocate_basics_for_demand,
            MIN_BASICS_PER_COMMIT_COLOR,
        )

        # Demand heavily skewed toward G (e.g., GG cards in pool).
        # Without floor: R would get ~5, G ~11. With floor: R = 7.
        basics = _allocate_basics_for_demand(
            deck_colors=["R", "G"],
            demand={"R": 8, "G": 16},
            fixing_sources={"R": 0.0, "G": 0.0},
            basics_budget=16,
        )
        assert sum(basics.values()) == 16
        assert basics["R"] >= MIN_BASICS_PER_COMMIT_COLOR
        # Donor (G) loses what R gained; still ≥ floor if possible.
        assert basics["G"] >= MIN_BASICS_PER_COMMIT_COLOR

    def test_floor_does_not_apply_to_splash(self):
        """Splash colors are handled via the splash-basic-guarantee
        elsewhere; the allocator should not bump them to 7 basics."""
        from common.inference.deck_builder import _allocate_basics_for_demand

        basics = _allocate_basics_for_demand(
            deck_colors=["R", "G", "W"],  # W is the splash
            demand={"R": 10, "G": 13, "W": 2},
            fixing_sources={"R": 0.0, "G": 0.0, "W": 0.0},
            basics_budget=16,
            splash_colors=["W"],
        )
        assert sum(basics.values()) == 16
        # Splash W can have <7 basics (it just needs 1-2).
        assert basics["W"] < 7
        # Committed R and G both at floor.
        assert basics["R"] >= 7
        assert basics["G"] >= 7

    def test_floor_skipped_when_budget_too_small(self):
        """If basics_budget can't support floor for all committed
        colors, fall back to proportional and let demotion decide."""
        from common.inference.deck_builder import _allocate_basics_for_demand

        # 3 committed colors, budget only 13 → 3*7 = 21 > 13. Can't meet
        # floor. Allocator should still return a valid allocation summing
        # to budget without crashing.
        basics = _allocate_basics_for_demand(
            deck_colors=["W", "U", "B"],
            demand={"W": 10, "U": 10, "B": 10},
            fixing_sources={"W": 0.0, "U": 0.0, "B": 0.0},
            basics_budget=13,
        )
        assert sum(basics.values()) == 13


class TestDemotionSkippedAboveFloor:
    """Demotion should NOT cut cards if both committed colors can
    reach MIN_BASICS_PER_COMMIT_COLOR basics. The user explicitly
    prefers under-supported cards over cut cards.
    """

    def test_rg_pool_with_five_single_pip_red_keeps_them(self):
        """Single-pip red cards in a green-heavy RG pool should be
        kept when 7+ Mountains can be allocated. The minority-color
        cut path was too aggressive before this change.

        Pool sized so main_deck reaches TARGET_SPELLS (23) and the
        17-land budget actually exercises proportional allocation +
        demotion — small pools over-allocate lands and never trigger
        the bug.
        """
        from common.inference.deck_builder import suggest_decks

        # 18 castable green cards (overflows into TARGET_SPELLS = 23
        # alongside the 5 red cards) → main_deck full, 17 lands.
        pool_cards = (
            [_sc(f"G2_{i}", "{1}{G}", 2, ci=["G"]) for i in range(6)]
            + [_sc(f"G3_{i}", "{2}{G}", 3, ci=["G"]) for i in range(6)]
            + [_sc(f"G4_{i}", "{3}{G}", 4, ci=["G"]) for i in range(6)]
            # Red side: 5 single-pip cards across the curve.
            + [_sc("R1", "{R}", 1, ci=["R"], type_line="Instant")]
            + [_sc(f"R4_{i}", "{3}{R}", 4, ci=["R"]) for i in range(2)]
            + [_sc("R4b", "{3}{R}", 4, ci=["R"], type_line="Sorcery")]
            + [_sc("R7", "{6}{R}", 7, ci=["R"])]
        )
        scryfall = {c.name: c for c in pool_cards}
        pool_names = [c.name for c in pool_cards]

        suggestions = suggest_decks(
            pool_names=pool_names, scryfall_cards=scryfall,
        )
        top = next(iter(suggestions.values()))

        # Main deck should fill out to TARGET_SPELLS, not collapse from
        # over-aggressive demotion cuts. Before the floor change,
        # demotion shrank main_deck to ~10 cards because it kept cutting
        # under-supported R cards instead of allocating 7 Mountains.
        assert len(top.main_deck) >= 22, (
            f"main_deck collapsed to {len(top.main_deck)} cards "
            f"(demotion over-cut). Should be ≥22 with floor. "
            f"archetype={top.archetype}, lands={dict(__import__('collections').Counter(top.lands))}"
        )
        # With floor enforced, all 5 red cards should survive.
        kept_red = [n for n in top.main_deck if n.startswith("R")]
        assert len(kept_red) >= 4, (
            f"Expected ≥4 red cards kept (single-pip safe with floor), "
            f"got {len(kept_red)}: {kept_red}. Mountains: "
            f"{top.lands.count('Mountain')}, archetype: {top.archetype}"
        )
        mountains = top.lands.count("Mountain")
        assert mountains >= 7, (
            f"Expected ≥7 Mountains (floor), got {mountains}. "
            f"Lands: {top.lands}, archetype: {top.archetype}"
        )


class TestColorFallbackOrdering:
    def test_color_match_key_prefers_main_pair(self):
        from common.inference.deck_builder import _color_match_key
        pip = {"W": 3, "U": 14, "B": 0, "R": 0, "G": 10}
        top = ["U", "G", "W"]  # ranked_colors; top-2 = U, G
        ug = _color_match_key("UG", pip, top)
        ugw = _color_match_key("UGW", pip, top)
        mono_u = _color_match_key("U", pip, top)
        # pair first, then 3-colour splash using the pair, then mono — holds
        # even though U (14) > G (10): overlap is membership, not magnitude.
        assert ug > ugw > mono_u

    def test_color_match_key_empty_pool(self):
        from common.inference.deck_builder import _color_match_key
        pip = {c: 0 for c in "WUBRG"}
        assert _color_match_key("UG", pip, []) == (0, -2, 0)

    def _spell(self, name, cost, colors):
        from common.inference.pool_analyzer import ScryfallCard
        return ScryfallCard(
            name=name, mana_cost=cost, cmc=2, type_line="Creature",
            oracle_text="", colors=colors, color_identity=colors,
            keywords=[], rarity="common",
        )

    def test_suggest_decks_orders_by_colors_when_unscored(self):
        scry, pool = {}, []
        for i in range(14):
            scry[f"u{i}"] = self._spell(f"u{i}", "{1}{U}", ["U"]); pool.append(f"u{i}")
        for i in range(10):
            scry[f"g{i}"] = self._spell(f"g{i}", "{1}{G}", ["G"]); pool.append(f"g{i}")
        for i in range(3):
            scry[f"w{i}"] = self._spell(f"w{i}", "{1}{W}", ["W"]); pool.append(f"w{i}")
        result = suggest_decks(pool, scry, card_map=None, set_metrics=None,
                               trophy_prior=None)
        assert result, "expected at least one suggestion"
        assert all(s.score < 0 for s in result.values())  # no 17Lands data
        assert next(iter(result)) == "UG"  # dominant pair first

    def test_suggest_decks_orders_by_score_when_available(self):
        scry, pool = {}, []
        for i in range(14):
            scry[f"u{i}"] = self._spell(f"u{i}", "{1}{U}", ["U"]); pool.append(f"u{i}")
        for i in range(14):
            scry[f"g{i}"] = self._spell(f"g{i}", "{1}{G}", ["G"]); pool.append(f"g{i}")
        card_map = _card_map({n: 0.60 for n in pool})
        sm = _set_metrics(mean=0.55, std=0.03)
        result = suggest_decks(pool, scry, card_map=card_map, set_metrics=sm,
                               trophy_prior=None)
        scores = [s.score for s in result.values()]
        assert scores == sorted(scores, reverse=True)  # ordered by score, not colors
