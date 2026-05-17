"""In-process smoke for the per-format SetDataManager refactor.

Boots a SetDataManager pointed at the repo's data dir, ensures both
PremierDraft and QuickDraft for EOE, waits for the background fetch, then
prints how many cards have non-zero gihwr/ata in each format.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PARENT = Path("C:/Users/home/Documents/Projects/NemeDraft")


def _summarise(card_map, label: str) -> None:
    if card_map is None:
        print(f"{label}: card_map is None")
        return
    total = len(card_map)
    gih = sum(
        1 for cr in card_map.values()
        if cr.deck_colors.get("All Decks", {}).get("gihwr", 0.0) > 0
    )
    ata = sum(
        1 for cr in card_map.values()
        if cr.deck_colors.get("All Decks", {}).get("ata", 0.0) > 0
    )
    print(f"{label}: total={total} gihwr>0={gih} ata>0={ata}")


def main() -> int:
    from common.data.set_data_manager import SetDataManager

    mgr = SetDataManager(
        cache_base=PARENT / "data",
        card_id_map_path=PARENT / "data" / "processed" / "card_id_map.json",
    )

    # Kick off both formats for the EOE case under test, plus a stable
    # control (TMT) to confirm the refactor didn't break older sets.
    for fmt in ("PremierDraft", "QuickDraft"):
        mgr.ensure_set("EOE", draft_format=fmt)
        mgr.ensure_set("TMT", draft_format=fmt)

    # Wait up to 60 s for backgrounds to settle.
    deadline = time.time() + 60
    while time.time() < deadline:
        if all(
            mgr.is_loaded(s, draft_format=f)
            for s in ("EOE", "TMT") for f in ("PremierDraft", "QuickDraft")
        ):
            break
        time.sleep(1)
    print(f"loading status after wait: "
          f"EOE/PD={mgr.is_loaded('EOE', draft_format='PremierDraft')} "
          f"EOE/QD={mgr.is_loaded('EOE', draft_format='QuickDraft')} "
          f"TMT/PD={mgr.is_loaded('TMT', draft_format='PremierDraft')} "
          f"TMT/QD={mgr.is_loaded('TMT', draft_format='QuickDraft')}")

    for set_code in ("EOE", "TMT"):
        for fmt in ("PremierDraft", "QuickDraft"):
            cmap = mgr.get_card_map(set_code, draft_format=fmt)
            _summarise(cmap, f"  {set_code}/{fmt}")

    # And the spot-check against the original P1P1 pack, comparing
    # direct QD lookup with the fallback ladder.
    pack = ["Dark Endurance", "Gene Pollinator", "Wurmwall Sweeper",
            "Flight-Deck Coordinator", "Nanoform Sentinel", "Rig for War",
            "Cryogen Relic", "Sunstar Expansionist", "Kavaron Harrier",
            "All-Fates Scroll", "Dubious Delicacy", "Blast Zone",
            "Evendo, Waking Haven"]
    print("\nP1P1 pack lookup via fallback ladder [QuickDraft, PremierDraft]:")
    for name in pack:
        stats, source = mgr.lookup_stats(
            "EOE", name, formats=["QuickDraft", "PremierDraft"],
        )
        gihwr = stats.get("gihwr", 0.0)
        ata = stats.get("ata", 0.0)
        src_tag = f"({source})" if source else "(no data)"
        print(f"  {name!r:42s} gihwr={gihwr:.3f} ata={ata:.2f} {src_tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
