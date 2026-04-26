"""Bidirectional mapping between Arena grpIds, card names, and NemeDraft IDs."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MTGA installation detection
# ---------------------------------------------------------------------------

_RAW_DATA_SUBDIR = os.path.join("MTGA_Data", "Downloads", "Raw")


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def find_mtga_card_db(mtga_install_dir: Path | None = None) -> Path | None:
    """Auto-detect the MTGA ``Raw_CardDatabase_*.mtga`` SQLite file.

    Args:
        mtga_install_dir: MTGA installation root.  If *None*, falls back
            to :func:`overlay.log_watcher.extract_mtga_install_dir`.
    """
    if mtga_install_dir is None:
        from client.overlay.log_watcher import extract_mtga_install_dir

        mtga_install_dir = extract_mtga_install_dir()
    if mtga_install_dir is None:
        return None

    raw_dir = mtga_install_dir / _RAW_DATA_SUBDIR
    if raw_dir.is_dir():
        for p in raw_dir.glob("Raw_CardDatabase_*.mtga"):
            return p
    return None


def update_scryfall(scryfall_dir: Path, set_codes: list[str] | None = None) -> None:
    """Re-download Scryfall bulk data and regenerate per-set JSONs.

    Args:
        scryfall_dir: Target directory for Scryfall JSON files.
        set_codes: Set codes to filter for. *None* uses the default list
            from ``data.download``.
    """
    from common.data.scryfall import download_scryfall_bulk, filter_scryfall_for_sets

    bulk_path = scryfall_dir / "default_cards.json"
    # Remove stale file so the downloader always fetches fresh data
    if bulk_path.exists():
        bulk_path.unlink()
        logger.info("Removed stale %s for refresh", bulk_path)

    download_scryfall_bulk(scryfall_dir)
    filter_scryfall_for_sets(bulk_path, set_codes=set_codes, output_dir=scryfall_dir)
    logger.info("Scryfall data updated in %s", scryfall_dir)


class ArenaCardMapper:
    """Maps Arena card grpIds to card names and NemeDraft internal IDs.

    Uses Scryfall data (arena_id field) as the primary source for the
    grpId→name mapping, and card_id_map.json for name→nemedraft_id.

    Supports both eager (all sets) and lazy (per-set) loading modes.
    """

    def __init__(
        self,
        scryfall_dir: Path,
        card_id_map_path: Path,
        *,
        lazy: bool = False,
    ) -> None:
        self._grpid_to_name: dict[int, str] = {}
        self._name_to_grpid: dict[str, int] = {}
        self._name_to_card_id: dict[str, int] = {}
        self._scryfall_dir = scryfall_dir
        self._loaded_sets: set[str] = set()
        self._mtga_db_loaded = False

        self._load_card_id_map(card_id_map_path)
        if not lazy:
            self._load_scryfall(scryfall_dir)
            self._load_mtga_card_db()

    def _load_card_id_map(self, path: Path) -> None:
        if not path.is_file():
            logger.warning("card_id_map not found at %s — name→id lookup disabled", path)
            return
        with open(path, encoding="utf-8") as f:
            self._name_to_card_id = json.load(f)
        logger.info("Loaded %d entries from card_id_map", len(self._name_to_card_id))

    def load_set(self, set_code: str) -> int:
        """Load Scryfall arena_id mappings for a single set.

        Args:
            set_code: Three-letter set code (e.g. ``"TMT"``).

        Returns:
            Number of new mappings added.
        """
        upper = set_code.upper()
        if upper in self._loaded_sets:
            return 0
        self._loaded_sets.add(upper)

        json_path = self._scryfall_dir / f"{upper.lower()}_cards.json"
        if not json_path.exists():
            logger.warning("No Scryfall file for set %s at %s", upper, json_path)
            return 0

        added = 0
        with open(json_path, encoding="utf-8") as f:
            cards = json.load(f)
        for card in cards:
            arena_id = card.get("arena_id")
            name = card.get("name", "")
            if arena_id and name and int(arena_id) not in self._grpid_to_name:
                self._grpid_to_name[int(arena_id)] = name
                self._name_to_grpid.setdefault(name, int(arena_id))
                added += 1
        logger.info("Loaded %d arena_id mappings for set %s", added, upper)
        return added

    def ensure_mtga_fallback(self) -> None:
        """Load the MTGA client SQLite fallback (idempotent)."""
        if not self._mtga_db_loaded:
            self._load_mtga_card_db()
            self._mtga_db_loaded = True

    def _load_scryfall(self, scryfall_dir: Path) -> None:
        """Load arena_id mappings from all Scryfall per-set JSON files."""
        loaded = 0
        for json_path in sorted(scryfall_dir.glob("*_cards.json")):
            with open(json_path, encoding="utf-8") as f:
                cards = json.load(f)
            for card in cards:
                arena_id = card.get("arena_id")
                name = card.get("name", "")
                if arena_id and name:
                    self._grpid_to_name[int(arena_id)] = name
                    self._name_to_grpid[name] = int(arena_id)
                    loaded += 1

        # Also try the bulk default_cards.json for broader coverage
        bulk_path = scryfall_dir / "default_cards.json"
        if bulk_path.exists():
            with open(bulk_path, encoding="utf-8") as f:
                cards = json.load(f)
            for card in cards:
                arena_id = card.get("arena_id")
                name = card.get("name", "")
                if arena_id and name and int(arena_id) not in self._grpid_to_name:
                    self._grpid_to_name[int(arena_id)] = name
                    self._name_to_grpid.setdefault(name, int(arena_id))
                    loaded += 1

        logger.info("Loaded %d arena_id→name mappings from Scryfall", len(self._grpid_to_name))

    def _load_mtga_card_db(self) -> None:
        """Load grpId→name from the MTGA client's SQLite card database.

        This acts as a fallback for sets where Scryfall hasn't published
        ``arena_id`` values yet.  Only cards not already covered by
        Scryfall are added.
        """
        db_path = find_mtga_card_db()
        if db_path is None:
            logger.debug("MTGA card database not found — skipping fallback")
            return

        added = 0
        try:
            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()
            cur.execute(
                "SELECT c.GrpId, l.Loc "
                "FROM Cards c "
                "JOIN Localizations_enUS l ON c.TitleId = l.LocId "
                "WHERE l.Formatted = 1 AND c.IsToken = 0",
            )
            for grpid, raw_name in cur.fetchall():
                if grpid not in self._grpid_to_name and raw_name:
                    name = _HTML_TAG_RE.sub("", raw_name)
                    self._grpid_to_name[grpid] = name
                    self._name_to_grpid.setdefault(name, grpid)
                    added += 1
            conn.close()
        except Exception:
            logger.exception("Failed to read MTGA card database at %s", db_path)
            return

        if added:
            logger.info(
                "Loaded %d additional grpId→name mappings from MTGA client DB (%s)",
                added,
                db_path.name,
            )

    def grpid_to_name(self, grpid: int) -> str | None:
        """Convert an Arena grpId to a card name."""
        return self._grpid_to_name.get(grpid)

    def grpid_to_card_id(self, grpid: int) -> int:
        """Convert an Arena grpId to a NemeDraft internal card ID (0 if unknown)."""
        name = self._grpid_to_name.get(grpid)
        if name is None:
            return 0
        return self._name_to_card_id.get(name, 0)

    def grpids_to_names(self, grpids: list[int]) -> list[str]:
        """Convert a list of Arena grpIds to card names, skipping unknowns."""
        names = []
        for gid in grpids:
            name = self.grpid_to_name(gid)
            if name is not None:
                names.append(name)
            else:
                logger.warning("Unknown Arena grpId: %d", gid)
        return names

    def name_to_card_id(self, name: str) -> int:
        """Convert a card name to NemeDraft internal ID (0 if unknown)."""
        return self._name_to_card_id.get(name, 0)

    @property
    def known_grpids(self) -> int:
        return len(self._grpid_to_name)
