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
