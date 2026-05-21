#!/usr/bin/env python3
"""Regenerate ``client/overlay/data/grpid_to_name.json`` from MTGJSON.

Downloads ``AllIdentifiers.json.gz`` from mtgjson.com, extracts every
card with a non-empty ``identifiers.mtgArenaId``, and writes a flat
``{grpid_str: name}`` JSON object with a ``_meta`` header.

The output is deterministic: cards are iterated in sorted-UUID order so
the first-seen name wins on collisions, and dict keys are emitted as
sorted strings.

Run from anywhere; output path is resolved relative to this file:
    python scripts/refresh_grpid_map.py
"""
from __future__ import annotations

import gzip
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

MTGJSON_URL = "https://mtgjson.com/api/v5/AllIdentifiers.json.gz"

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "client" / "overlay" / "data" / "grpid_to_name.json"


def fetch_mtgjson(url: str = MTGJSON_URL) -> dict:
    print(f"Downloading {url} ...", file=sys.stderr)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "nemedraft-grpid-refresh/1.0"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
        raw = resp.read()
    print(f"  {len(raw):,} bytes downloaded, decompressing...", file=sys.stderr)
    return json.loads(gzip.decompress(raw))


def extract_mapping(payload: dict) -> tuple[dict[str, str], str]:
    """Return ``(grpid->name dict, mtgjson source date)``.

    First-seen wins on collisions. Iteration order is sorted by UUID for
    determinism across runs.
    """
    source_date = payload.get("meta", {}).get("date", "unknown")
    data = payload["data"]
    mapping: dict[str, str] = {}
    for uuid in sorted(data.keys()):
        card = data[uuid]
        aid = card.get("identifiers", {}).get("mtgArenaId")
        name = card.get("name")
        if not aid or not name:
            continue
        key = str(int(aid))  # normalise "123" / 123 → "123"
        mapping.setdefault(key, name)
    return mapping, source_date


def write_output(mapping: dict[str, str], source_date: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sorted_mapping = {k: mapping[k] for k in sorted(mapping, key=int)}
    payload = {
        "_meta": {
            "source": "mtgjson AllIdentifiers",
            "source_date": source_date,
            "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "count": len(sorted_mapping),
        },
        "grpid_to_name": sorted_mapping,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=0, separators=(",", ":"))
        f.write("\n")
    print(
        f"Wrote {len(sorted_mapping):,} grpId→name mappings to "
        f"{out_path.relative_to(REPO_ROOT)} (source {source_date})",
        file=sys.stderr,
    )


def main() -> int:
    payload = fetch_mtgjson()
    mapping, source_date = extract_mapping(payload)
    if len(mapping) < 10_000:
        print(
            f"ERROR: extracted only {len(mapping)} mappings — refusing to "
            f"overwrite (expected ≥10k). Aborting.",
            file=sys.stderr,
        )
        return 1
    write_output(mapping, source_date, OUTPUT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
