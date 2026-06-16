"""Scryfall card data: download bulk export and filter to per-set JSONs.

Single source of truth for Scryfall data lives in this client repo. The server
imports from here too (via the editable install of ``common.data.*``).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

SCRYFALL_BULK_URL = "https://api.scryfall.com/bulk-data"

# Resolved at import time so callers don't need to pass a path. parents[2] is
# the client repo root (this file is at <root>/common/data/scryfall.py).
SCRYFALL_DIR = Path(__file__).resolve().parents[2] / "data" / "scryfall"

# Sets currently shipped with per-set JSONs in SCRYFALL_DIR. Used as the
# default filter target when callers don't pass --sets.
DEFAULT_SETS = ["ECL", "EOE", "FDN", "FIN", "SOS", "TLA", "TMT", "MKM", "BLB", "DSK", "MSH"]

_FILTERED_FIELDS = [
    "name", "oracle_id", "mana_cost", "cmc", "type_line", "oracle_text",
    "power", "toughness", "colors", "color_identity", "keywords",
    "rarity", "set", "arena_id", "collector_number",
]


def download_scryfall_bulk(output_dir: Path = SCRYFALL_DIR) -> Path:
    """Download Scryfall default_cards bulk export and return the path."""
    from tqdm import tqdm

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "default_cards.json"
    if out_path.exists():
        print(f"Scryfall bulk data already exists at {out_path}")
        return out_path

    print("Fetching Scryfall bulk-data index...")
    with httpx.Client(timeout=30) as client:
        resp = client.get(SCRYFALL_BULK_URL)
        resp.raise_for_status()
        bulk_index = resp.json()

    download_uri = None
    for entry in bulk_index["data"]:
        if entry["type"] == "default_cards":
            download_uri = entry["download_uri"]
            break

    if download_uri is None:
        raise RuntimeError("Could not find default_cards in Scryfall bulk-data index")

    print(f"Downloading Scryfall default_cards from {download_uri}...")
    with httpx.Client(timeout=600, follow_redirects=True) as client:
        with client.stream("GET", download_uri) as stream:
            stream.raise_for_status()
            total = int(stream.headers.get("content-length", 0))
            with open(out_path, "wb") as f, tqdm(
                total=total, unit="B", unit_scale=True, desc="Scryfall"
            ) as pbar:
                for chunk in stream.iter_bytes(chunk_size=1024 * 256):
                    f.write(chunk)
                    pbar.update(len(chunk))

    print(f"Saved Scryfall bulk data to {out_path}")
    return out_path


def filter_scryfall_for_sets(
    bulk_path: Path = SCRYFALL_DIR / "default_cards.json",
    set_codes: list[str] | None = None,
    output_dir: Path = SCRYFALL_DIR,
) -> dict[str, Path]:
    """Filter Scryfall bulk JSON to only the target sets, write per-set JSONs."""
    if set_codes is None:
        set_codes = DEFAULT_SETS

    codes_lower = {s.lower() for s in set_codes}
    per_set: dict[str, list[dict]] = {s.lower(): [] for s in set_codes}

    print(f"Filtering Scryfall data for sets: {set_codes}...")
    with open(bulk_path, encoding="utf-8") as f:
        cards = json.load(f)

    for card in cards:
        card_set = card.get("set", "").lower()
        if card_set in codes_lower:
            filtered = {k: card.get(k) for k in _FILTERED_FIELDS}
            per_set[card_set].append(filtered)

    result = {}
    for code, cards_list in per_set.items():
        out = output_dir / f"{code}_cards.json"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(cards_list, f, indent=2)
        print(f"  {code.upper()}: {len(cards_list)} cards -> {out}")
        result[code] = out

    return result


def main():
    """Refresh Scryfall data: download bulk + filter to per-set JSONs."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Download and filter Scryfall card data",
    )
    parser.add_argument(
        "--sets",
        nargs="+",
        default=None,
        help=f"Set codes to filter for (default: {DEFAULT_SETS})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=SCRYFALL_DIR,
        help=f"Output directory (default: {SCRYFALL_DIR})",
    )
    parser.add_argument(
        "--keep-bulk",
        action="store_true",
        help="Reuse existing default_cards.json (default: delete and re-download)",
    )
    args = parser.parse_args()

    # CLI invocation implies "I want fresh data" — drop the cached bulk file
    # unless the caller opts out. download_scryfall_bulk skips if the file
    # exists, so without this the CLI silently filters stale data.
    bulk_path = args.output_dir / "default_cards.json"
    if not args.keep_bulk and bulk_path.exists():
        print(f"Removing stale {bulk_path} for refresh")
        bulk_path.unlink()

    bulk_path = download_scryfall_bulk(args.output_dir)
    filter_scryfall_for_sets(
        bulk_path,
        set_codes=args.sets,
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    main()
