"""Deck-strip archetype card — identity header, score chip, fill bar, pips."""

from __future__ import annotations

import os

# Must be set before any PySide6 import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def make_card(qapp):
    """Build _ArchetypeCard instances and tear them down deterministically —
    orphaned top-level widgets GC'd mid-suite crash Qt teardown."""
    from client.overlay.ui.pack_rail import _ArchetypeCard

    cards = []

    def _make():
        card = _ArchetypeCard()
        cards.append(card)
        return card

    yield _make
    for card in cards:
        card.deleteLater()
    qapp.processEvents()


def test_archetype_display_name_mapping():
    from client.overlay.ui.pack_rail import archetype_display_name

    assert archetype_display_name(["U", "R"]) == "Izzet"
    assert archetype_display_name(["R", "U"]) == "Izzet"  # order-insensitive
    assert archetype_display_name(["W", "U", "B"]) == "Esper"
    assert archetype_display_name(["R"]) == "Mono-Red"
    assert archetype_display_name(["W", "U", "B", "R"]) == "4-Color"
    assert archetype_display_name(["W", "U", "B", "R", "G"]) == "5-Color"
    assert archetype_display_name([]) == "—"


def test_archetype_card_header_and_fill(make_card):
    card = make_card()
    card.set_values("UR", 4.3, ["U", "R"], 18)

    assert card.name_label.text() == "Izzet"
    assert card.score_chip.text() == "4.3"
    assert card.score_chip.isVisibleTo(card)
    assert card.count_label.text() == "18/40"
    assert card.count_bar.value() == 18
    assert card.count_bar.maximum() == 40
    # Two colour icons shown, three hidden.
    shown = [c for c in "WUBRG" if card._color_icons[c].isVisibleTo(card)]
    assert shown == ["U", "R"] or set(shown) == {"U", "R"}


def test_archetype_card_hides_score_when_unavailable(make_card):
    card = make_card()
    card.set_values("WG", -1.0, ["W", "G"], 45)

    assert not card.score_chip.isVisibleTo(card)
    assert card.count_bar.value() == 40  # clamped to the bar's range


def test_archetype_card_pips_top_two_highlight(make_card):
    card = make_card()
    card.set_pips({"W": 0, "U": 9, "B": 1, "R": 7, "G": 0})

    assert card._pip_counts["U"].property("top") is True
    assert card._pip_counts["R"].property("top") is True
    assert card._pip_counts["W"].property("top") is False
    assert card._pip_counts["B"].property("top") is False
