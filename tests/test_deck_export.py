"""Tests for client.overlay.deck_export — Arena-importable deck strings."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class _Suggestion:
    main_deck: list[str] = field(default_factory=list)
    lands: list[str] = field(default_factory=list)
    nonbasic_lands: list[str] = field(default_factory=list)


def test_build_arena_deck_string_matches_legacy_format():
    from client.overlay.deck_export import build_arena_deck_string

    suggestion = _Suggestion(
        main_deck=["Skywatcher", "Skywatcher", "Aether Bolt"],
        lands=["Plains"] * 2 + ["Island"],
        nonbasic_lands=["Mistveil Bridge"],
    )
    pool = [
        "Skywatcher", "Skywatcher", "Aether Bolt",
        "Mistveil Bridge", "Useless Ogre",
    ]
    text = build_arena_deck_string(suggestion, pool)

    assert text == (
        "Deck\n"
        "1 Aether Bolt\n"
        "2 Skywatcher\n"
        "1 Island\n"
        "1 Mistveil Bridge\n"
        "2 Plains\n"
        "\n"
        "Sideboard\n"
        "1 Useless Ogre"
    )


def test_build_arena_deck_string_no_sideboard_section_when_pool_used_up():
    from client.overlay.deck_export import build_arena_deck_string

    suggestion = _Suggestion(main_deck=["Aether Bolt"], lands=["Mountain"])
    text = build_arena_deck_string(suggestion, ["Aether Bolt"])
    assert "Sideboard" not in text
    assert text.startswith("Deck\n")


def test_build_pool_string():
    from client.overlay.deck_export import build_pool_string

    text = build_pool_string(["Aether Bolt", "Skywatcher", "Aether Bolt"])
    assert text == "2 Aether Bolt\n1 Skywatcher"


def test_deck_tab_copy_delegates_to_export(qapp=None):
    """DeckTab clipboard output must remain byte-identical (regression guard)."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])

    from client.overlay.deck_export import build_arena_deck_string
    from client.overlay.ui.deck_tab import DeckTab
    from common.inference.deck_builder import DeckSuggestion

    sug = DeckSuggestion(
        archetype="WU Fliers",
        main_deck=["Skywatcher", "Aether Bolt"],
        lands=["Plains", "Island"],
        nonbasic_lands=[],
    )
    pool = ["Skywatcher", "Aether Bolt", "Useless Ogre"]

    tab = DeckTab()
    tab._suggestions = {"WU": sug}
    tab._current_key = "WU"
    tab._pool_names = list(pool)
    tab._copy_to_clipboard()

    clipboard = app.clipboard()
    assert clipboard.text() == build_arena_deck_string(sug, pool)
