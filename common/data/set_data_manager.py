"""Orchestrates 17Lands data loading per set with background fetching."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from common.data.card_stats import SetMetrics
from common.data.seventeenlands import CardRatings, SeventeenLandsClient

logger = logging.getLogger(__name__)


class SetDataManager:
    """Manages 17Lands data for each set the overlay encounters.

    Downloads are performed lazily — on first request for a set — and run in a
    background thread so the UI stays responsive.

    Args:
        cache_base: Root directory for the 17Lands cache.
        card_id_map_path: Path to NemeDraft's ``card_id_map.json``.
        user_group: Player-skill filter (``"All"``, ``"Platinum+"``, …).
        date_range_days: Look-back window for 17Lands data.
        draft_format: Arena event format string.
    """

    def __init__(
        self,
        cache_base: Path,
        card_id_map_path: Path,
        *,
        user_group: str = "All",
        date_range_days: int = 90,
        draft_format: str = "PremierDraft",
    ) -> None:
        self._cache_base = cache_base
        self._user_group = user_group
        self._date_range_days = date_range_days
        self._draft_format = draft_format

        # name → nemedraft internal card ID
        with open(card_id_map_path, encoding="utf-8") as f:
            self._name_to_id: dict[str, int] = json.load(f)

        # Loaded set data: set_code → (card_map, metrics, by_id)
        self._sets: dict[str, _SetBundle] = {}
        self._lock = threading.Lock()
        self._loading: set[str] = set()

    # -- public API ----------------------------------------------------------

    def ensure_set(
        self,
        set_code: str,
        *,
        draft_format: str | None = None,
        callback: _Callback | None = None,
    ) -> None:
        """Start loading 17Lands data for *set_code* if not already cached.

        Args:
            set_code: Three-letter set code.
            draft_format: 17Lands format override (e.g. ``"QuickDraft"``).
                Falls back to the instance default if *None*.
            callback: Called as ``callback(set_code, success)`` on the
                background thread once the download finishes.
        """
        fmt = draft_format or self._draft_format
        with self._lock:
            existing = self._sets.get(set_code)
            if existing and existing.draft_format == fmt:
                if callback:
                    callback(set_code, True)
                return
            if set_code in self._loading:
                return
            self._loading.add(set_code)

        t = threading.Thread(
            target=self._fetch,
            args=(set_code, fmt, callback),
            daemon=True,
            name=f"17lands-{set_code}",
        )
        t.start()

    def get_card_map(self, set_code: str) -> dict[str, CardRatings] | None:
        """Return the per-name card map for *set_code*, or *None* if not loaded yet."""
        with self._lock:
            bundle = self._sets.get(set_code)
        return bundle.card_map if bundle else None

    def get_set_metrics(self, set_code: str) -> SetMetrics | None:
        """Return set-level metrics, or *None* if not loaded yet."""
        with self._lock:
            bundle = self._sets.get(set_code)
        return bundle.metrics if bundle else None

    def get_card_ratings_by_id(self, set_code: str) -> dict[int, CardRatings] | None:
        """Return card ratings keyed by NemeDraft internal ID."""
        with self._lock:
            bundle = self._sets.get(set_code)
        return bundle.by_id if bundle else None

    def is_loaded(self, set_code: str) -> bool:
        with self._lock:
            return set_code in self._sets

    # -- internals -----------------------------------------------------------

    def _fetch(self, set_code: str, draft_format: str, callback: _Callback | None) -> None:
        success = False
        try:
            client = SeventeenLandsClient(
                self._cache_base,
                user_group=self._user_group,
                date_range_days=self._date_range_days,
                draft_format=draft_format,
            )

            # Stale-while-revalidate: serve cached data immediately so the
            # UI can show 17Lands stats without waiting for the network.
            cached_map = client.load_cached_set_data(set_code)
            if cached_map:
                self._install_bundle(set_code, cached_map, draft_format)
                success = True
                # Fire callback early so the UI can refresh with cached data.
                if callback:
                    try:
                        callback(set_code, True)
                    except Exception:
                        logger.exception("17Lands callback error (cached)")
                    callback = None  # Don't fire again after network refresh.

            # Network refresh (will be no-ops for archetypes still within TTL).
            try:
                card_map = client.download_set_data(set_code)
            finally:
                client.close()

            if not card_map:
                if not success:
                    logger.warning("17Lands returned no data for %s/%s", set_code, draft_format)
                with self._lock:
                    self._loading.discard(set_code)
                return

            self._install_bundle(set_code, card_map, draft_format)
            success = True
        except Exception:
            logger.exception("Failed to load 17Lands data for %s", set_code)
            with self._lock:
                self._loading.discard(set_code)
        finally:
            if callback:
                try:
                    callback(set_code, success)
                except Exception:
                    logger.exception("17Lands callback error")

    def _install_bundle(
        self,
        set_code: str,
        card_map: dict[str, CardRatings],
        draft_format: str,
    ) -> None:
        """Build metrics + by-ID lookup and store the bundle."""
        metrics = SetMetrics.from_card_map(card_map)

        by_id: dict[int, CardRatings] = {}
        for name, cr in card_map.items():
            cid = self._name_to_id.get(name, 0)
            if cid:
                by_id[cid] = cr

        with self._lock:
            self._sets[set_code] = _SetBundle(card_map, metrics, by_id, draft_format)
            self._loading.discard(set_code)

        logger.info("17Lands data ready for %s/%s (%d cards, %d matched by ID)",
                     set_code, draft_format, len(card_map), len(by_id))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

from dataclasses import dataclass
from typing import Callable

_Callback = Callable[[str, bool], None]


@dataclass
class _SetBundle:
    card_map: dict[str, CardRatings]
    metrics: SetMetrics
    by_id: dict[int, CardRatings]
    draft_format: str = "PremierDraft"
