"""Tests for ArenaCardMapper grpId→name resolution."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from client.overlay.card_mapper import ArenaCardMapper


@pytest.fixture
def empty_scryfall_dir(tmp_path: Path) -> Path:
    d = tmp_path / "scryfall"
    d.mkdir()
    return d


@pytest.fixture
def empty_card_id_map(tmp_path: Path) -> Path:
    p = tmp_path / "card_id_map.json"
    p.write_text("{}")
    return p


def test_bundled_mtgjson_resolves_eoe_grpid(
    empty_scryfall_dir: Path, empty_card_id_map: Path
) -> None:
    """Mac regression: EOE grpIds must resolve via the bundled mtgjson map
    without any Scryfall arena_ids and without the MTGA SQLite fallback."""
    mapper = ArenaCardMapper(
        scryfall_dir=empty_scryfall_dir,
        card_id_map_path=empty_card_id_map,
    )
    # 96612 = "Starfighter Pilot" (EOE) per MTGJSON 2026-05-21
    assert mapper.grpid_to_name(96612) == "Starfighter Pilot"


def test_bundled_mtgjson_covers_supported_sets(
    empty_scryfall_dir: Path, empty_card_id_map: Path
) -> None:
    """Every supported set should resolve at least one known grpId via
    the bundled mtgjson map alone."""
    mapper = ArenaCardMapper(
        scryfall_dir=empty_scryfall_dir,
        card_id_map_path=empty_card_id_map,
    )
    sentinels = {
        "EOE": (96612, "Starfighter Pilot"),
    }
    for set_code, (grpid, name) in sentinels.items():
        assert mapper.grpid_to_name(grpid) == name, set_code


def test_scryfall_wins_over_bundled_mtgjson(tmp_path: Path) -> None:
    """If a per-set Scryfall file provides an arena_id, it should take
    precedence over the bundled mtgjson entry (Scryfall is closer to
    canonical print names; mtgjson is the fallback)."""
    scry = tmp_path / "scryfall"
    scry.mkdir()
    (scry / "test_cards.json").write_text(json.dumps([
        {"arena_id": 96612, "name": "Starfighter Pilot [Scryfall]"}
    ]))
    cmap = tmp_path / "card_id_map.json"
    cmap.write_text("{}")
    mapper = ArenaCardMapper(scryfall_dir=scry, card_id_map_path=cmap)
    assert mapper.grpid_to_name(96612) == "Starfighter Pilot [Scryfall]"


def test_bundled_data_file_is_well_formed() -> None:
    """The committed bundled file must parse and have the expected shape."""
    from importlib.resources import files

    raw = (files("client.overlay.data") / "grpid_to_name.json").read_text()
    data = json.loads(raw)
    assert set(data.keys()) == {"_meta", "grpid_to_name"}
    m = data["grpid_to_name"]
    assert isinstance(m, dict)
    assert len(m) >= 10_000
    assert all(k.isdigit() for k in m)
    assert data["_meta"]["count"] == len(m)
