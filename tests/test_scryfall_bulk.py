"""Scryfall bulk download + per-set filtering.

Scryfall's ``/bulk-data`` index dropped ``download_uri`` in favour of
``jsonl_download_uri`` (gzipped JSON Lines), which broke both the download
and the array-shaped read that followed it.
"""

from __future__ import annotations

import gzip
import json

import httpx
import pytest

from common.data import scryfall as sf

_CARDS = [
    {"name": "Agent 13, Sharon Carter", "set": "msh", "arena_id": 104892,
     "oracle_id": "abc", "colors": ["W"], "rarity": "uncommon"},
    {"name": "Super-Skrull", "set": "msh", "arena_id": 105010,
     "oracle_id": "def", "colors": ["B"], "rarity": "rare"},
    {"name": "Lightning Bolt", "set": "lea", "arena_id": None,
     "oracle_id": "ghi", "colors": ["R"], "rarity": "common"},
]


def _write_jsonl_gz(path):
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for c in _CARDS:
            f.write(json.dumps(c) + "\n")
    return path


# -- reading -----------------------------------------------------------------


def test_filter_reads_gzipped_jsonl_bulk(tmp_path):
    bulk = _write_jsonl_gz(tmp_path / "default_cards.jsonl.gz")

    result = sf.filter_scryfall_for_sets(bulk, ["MSH"], tmp_path)

    cards = json.loads((tmp_path / "msh_cards.json").read_text(encoding="utf-8"))
    assert [c["name"] for c in cards] == [
        "Agent 13, Sharon Carter", "Super-Skrull",
    ]
    assert [c["arena_id"] for c in cards] == [104892, 105010]
    assert result["msh"] == tmp_path / "msh_cards.json"


def test_filter_still_reads_a_legacy_json_array_bulk(tmp_path):
    bulk = tmp_path / "default_cards.json"
    bulk.write_text(json.dumps(_CARDS), encoding="utf-8")

    sf.filter_scryfall_for_sets(bulk, ["MSH"], tmp_path)

    cards = json.loads((tmp_path / "msh_cards.json").read_text(encoding="utf-8"))
    assert len(cards) == 2


def test_filter_finds_the_bulk_file_itself_when_not_told(tmp_path):
    _write_jsonl_gz(tmp_path / "default_cards.jsonl.gz")

    sf.filter_scryfall_for_sets(set_codes=["MSH"], output_dir=tmp_path)

    assert (tmp_path / "msh_cards.json").exists()


def test_find_bulk_file_prefers_jsonl_gz_over_legacy(tmp_path):
    (tmp_path / "default_cards.json").write_text("[]", encoding="utf-8")
    assert sf.find_bulk_file(tmp_path).name == "default_cards.json"

    _write_jsonl_gz(tmp_path / "default_cards.jsonl.gz")
    assert sf.find_bulk_file(tmp_path).name == "default_cards.jsonl.gz"


def test_find_bulk_file_returns_none_when_absent(tmp_path):
    assert sf.find_bulk_file(tmp_path) is None


# -- downloading -------------------------------------------------------------


@pytest.fixture()
def fake_scryfall(monkeypatch):
    """Serve a bulk-data index (jsonl_download_uri only) and the payload."""
    payload = gzip.compress(
        b"".join(json.dumps(c).encode() + b"\n" for c in _CARDS),
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/bulk-data":
            return httpx.Response(200, json={"data": [
                {"type": "oracle_cards",
                 "jsonl_download_uri": "https://data.scryfall.io/o.jsonl.gz"},
                {"type": "default_cards",
                 "jsonl_download_uri": "https://data.scryfall.io/d.jsonl.gz"},
            ]})
        return httpx.Response(200, content=payload)

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(sf.httpx, "Client", fake_client)
    return seen


def test_download_uses_the_jsonl_uri_and_keeps_it_compressed(
    tmp_path, fake_scryfall,
):
    out = sf.download_scryfall_bulk(tmp_path)

    assert out.name == "default_cards.jsonl.gz"
    assert out.exists()
    # Written file is readable by the filter path.
    names = [c["name"] for c in sf.iter_bulk_cards(out)]
    assert "Super-Skrull" in names
    # Picked the default_cards entry, not the first one in the index.
    assert str(fake_scryfall[-1].url).endswith("/d.jsonl.gz")


def test_download_sends_identifying_headers(tmp_path, fake_scryfall):
    sf.download_scryfall_bulk(tmp_path)

    index_req = fake_scryfall[0]
    assert "python-httpx" not in index_req.headers.get("user-agent", "")
    assert index_req.headers.get("accept") == "application/json"


def test_download_skips_when_a_bulk_file_already_exists(tmp_path, fake_scryfall):
    existing = _write_jsonl_gz(tmp_path / "default_cards.jsonl.gz")
    before = existing.stat().st_mtime_ns

    out = sf.download_scryfall_bulk(tmp_path)

    assert out == existing
    assert existing.stat().st_mtime_ns == before  # untouched
    assert fake_scryfall == []                    # no requests at all


def test_download_skips_for_a_legacy_json_bulk_too(tmp_path, fake_scryfall):
    legacy = tmp_path / "default_cards.json"
    legacy.write_text(json.dumps(_CARDS), encoding="utf-8")

    assert sf.download_scryfall_bulk(tmp_path) == legacy
    assert fake_scryfall == []


def test_download_errors_when_the_index_has_no_default_cards(
    tmp_path, monkeypatch,
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"type": "rulings"}]})

    real_client = httpx.Client
    monkeypatch.setattr(sf.httpx, "Client", lambda *a, **k: real_client(
        *a, **{**k, "transport": httpx.MockTransport(handler)}))

    with pytest.raises(RuntimeError, match="default_cards"):
        sf.download_scryfall_bulk(tmp_path)
