"""Scryfall card data: download bulk export and filter to per-set JSONs.

Single source of truth for Scryfall data lives in this client repo. The server
imports from here too (via the editable install of ``common.data.*``).
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from pathlib import Path

import httpx

SCRYFALL_BULK_URL = "https://api.scryfall.com/bulk-data"

# Scryfall's API wants an identifying User-Agent and an explicit Accept; it
# answers 403 to some anonymous clients, and its image CDN rejects httpx's
# default UA outright (see client/overlay/card_art.py).
_HEADERS = {
    "User-Agent": "NemeDraft/0.1 (Educational Tool)",
    "Accept": "application/json",
}

# Bulk-export filenames we know how to read, best first. Scryfall now
# publishes gzipped JSON Lines (``jsonl_download_uri``) and no longer offers
# the single-JSON-array ``download_uri``, but a previously downloaded
# ``default_cards.json`` is still perfectly usable.
_BULK_FILENAMES = ("default_cards.jsonl.gz", "default_cards.jsonl",
                   "default_cards.json")

# Resolved at import time so callers don't need to pass a path. parents[2] is
# the client repo root (this file is at <root>/common/data/scryfall.py).
SCRYFALL_DIR = Path(__file__).resolve().parents[2] / "data" / "scryfall"

# Sets currently shipped with per-set JSONs in SCRYFALL_DIR. Used as the
# default filter target when callers don't pass --sets.
#
# OTP ("Breaking News") and BIG ("The Big Score") are bonus sheets drafted inside
# OTJ; they are fixed one-wave sets, so filtering them by code is exact. The
# *rolling* bonus sheets MAR and SPG are deliberately absent — their codes span
# every expansion's wave, so this date-blind filter would pull all of them and
# clobber the intersected files that scripts/filter_bonus_sheet.py writes.
DEFAULT_SETS = [
    "ECL", "EOE", "FDN", "FIN", "SOS", "TLA", "TMT", "MKM", "BLB", "DSK", "MSH",
    "OTJ", "OTP", "BIG",
]

_FILTERED_FIELDS = [
    "name", "oracle_id", "mana_cost", "cmc", "type_line", "oracle_text",
    "power", "toughness", "colors", "color_identity", "keywords",
    "rarity", "set", "arena_id", "collector_number",
]


def find_bulk_file(directory: Path = SCRYFALL_DIR) -> Path | None:
    """Return the bulk export in *directory*, newest format first, or None."""
    for name in _BULK_FILENAMES:
        candidate = directory / name
        if candidate.exists():
            return candidate
    return None


def iter_bulk_cards(bulk_path: Path) -> Iterator[dict]:
    """Yield card dicts from a bulk export, whatever shape it's in.

    Handles gzipped JSON Lines (what Scryfall publishes today), plain JSON
    Lines, and the legacy single-JSON-array export. The line-oriented formats
    stream, so a multi-GB export never lands in memory at once.
    """
    name = bulk_path.name
    if name.endswith(".jsonl.gz"):
        with gzip.open(bulk_path, "rt", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)
    elif name.endswith(".jsonl"):
        with open(bulk_path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)
    else:
        with open(bulk_path, encoding="utf-8") as f:
            yield from json.load(f)


def download_scryfall_bulk(output_dir: Path = SCRYFALL_DIR) -> Path:
    """Download Scryfall's default_cards bulk export and return the path.

    No-ops when any readable bulk export is already present — callers that
    want fresh data delete it first (``main`` does).
    """
    from tqdm import tqdm

    output_dir.mkdir(parents=True, exist_ok=True)
    existing = find_bulk_file(output_dir)
    if existing is not None:
        print(f"Scryfall bulk data already exists at {existing}")
        return existing

    print("Fetching Scryfall bulk-data index...")
    with httpx.Client(timeout=30, headers=_HEADERS) as client:
        resp = client.get(SCRYFALL_BULK_URL)
        resp.raise_for_status()
        bulk_index = resp.json()

    download_uri = None
    for entry in bulk_index["data"]:
        if entry.get("type") == "default_cards":
            # jsonl_download_uri is what Scryfall serves now; download_uri is
            # kept as a fallback in case the array export ever returns.
            download_uri = (
                entry.get("jsonl_download_uri") or entry.get("download_uri")
            )
            break

    if download_uri is None:
        raise RuntimeError("Could not find default_cards in Scryfall bulk-data index")

    # Keep the export in whatever format it arrives in — the .gz is ~77 MB
    # against ~550 MB decompressed, and iter_bulk_cards reads it directly.
    suffix = ".jsonl.gz" if ".jsonl.gz" in download_uri else (
        ".jsonl" if download_uri.endswith(".jsonl") else ".json"
    )
    out_path = output_dir / f"default_cards{suffix}"

    print(f"Downloading Scryfall default_cards from {download_uri}...")
    with httpx.Client(timeout=600, follow_redirects=True, headers=_HEADERS) as client:
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
    bulk_path: Path | None = None,
    set_codes: list[str] | None = None,
    output_dir: Path = SCRYFALL_DIR,
) -> dict[str, Path]:
    """Filter a Scryfall bulk export to the target sets, write per-set JSONs.

    *bulk_path* defaults to whichever export :func:`find_bulk_file` locates in
    *output_dir*.
    """
    if set_codes is None:
        set_codes = DEFAULT_SETS
    if bulk_path is None:
        bulk_path = find_bulk_file(output_dir)
        if bulk_path is None:
            raise FileNotFoundError(
                f"No Scryfall bulk export in {output_dir} — "
                "run download_scryfall_bulk() first",
            )

    codes_lower = {s.lower() for s in set_codes}
    per_set: dict[str, list[dict]] = {s.lower(): [] for s in set_codes}

    print(f"Filtering Scryfall data for sets: {set_codes} (from {bulk_path.name})...")
    for card in iter_bulk_cards(bulk_path):
        card_set = (card.get("set") or "").lower()
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
        help="Reuse the existing bulk export (default: delete and re-download)",
    )
    args = parser.parse_args()

    # CLI invocation implies "I want fresh data" — drop every cached bulk
    # export unless the caller opts out. download_scryfall_bulk skips when one
    # is present, so without this the CLI silently filters stale data (which
    # is how msh_cards.json ended up with no arena_ids: the bulk snapshot
    # predated MSH's Arena release).
    if not args.keep_bulk:
        while (stale := find_bulk_file(args.output_dir)) is not None:
            print(f"Removing stale {stale} for refresh")
            stale.unlink()

    bulk_path = download_scryfall_bulk(args.output_dir)
    filter_scryfall_for_sets(
        bulk_path,
        set_codes=args.sets,
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    main()
