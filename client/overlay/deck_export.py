"""Arena-importable deck / pool string builders (pure, no Qt)."""

from __future__ import annotations

from collections import Counter
from typing import Protocol


class _DeckLike(Protocol):
    main_deck: list[str]
    lands: list[str]
    nonbasic_lands: list[str]


def build_arena_deck_string(suggestion: _DeckLike, pool_names: list[str]) -> str:
    """Build the MTG Arena import string for a deck suggestion.

    Bare ``N Card Name`` lines — the format Arena's importer accepts and
    the one this client has always produced (kept byte-identical as the
    regression baseline). Unused pool cards land in the Sideboard section.
    """
    lines: list[str] = ["Deck"]

    name_counts = Counter(suggestion.main_deck)
    for name in sorted(name_counts.keys()):
        lines.append(f"{name_counts[name]} {name}")

    land_counts = Counter(suggestion.nonbasic_lands + suggestion.lands)
    for name in sorted(land_counts.keys()):
        lines.append(f"{land_counts[name]} {name}")

    main_set = Counter(suggestion.main_deck)
    nb_set = Counter(suggestion.nonbasic_lands)
    sb_counts: Counter[str] = Counter()
    for name in pool_names:
        if main_set.get(name, 0) > 0:
            main_set[name] -= 1
        elif nb_set.get(name, 0) > 0:
            nb_set[name] -= 1
        else:
            sb_counts[name] += 1

    if sb_counts:
        lines.append("")
        lines.append("Sideboard")
        for name in sorted(sb_counts.keys()):
            lines.append(f"{sb_counts[name]} {name}")

    return "\n".join(lines)


def build_pool_string(pool_names: list[str]) -> str:
    """Build a plain ``N Card Name`` listing of the whole pool."""
    counts = Counter(pool_names)
    return "\n".join(f"{counts[name]} {name}" for name in sorted(counts.keys()))
