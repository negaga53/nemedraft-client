"""Orchestrates 17Lands data loading per (set, format) with background fetching."""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from common.data.card_stats import SetMetrics
from common.data.seventeenlands import CardRatings, SeventeenLandsClient
from common.data.trophy_deck_prior import TrophyDeckPrior, default_prior_path

logger = logging.getLogger(__name__)

_Callback = Callable[[str, bool], None]

# How long a cached bundle is considered fresh before refresh_all() will
# re-fetch it. ensure_set() within this window is a no-op.
DEFAULT_REFRESH_INTERVAL_S: float = 24 * 60 * 60


class SetDataManager:
    """Manages 17Lands data per ``(set, draft_format)`` pair.

    Downloads run in background threads. Both PremierDraft and QuickDraft
    can coexist in the cache so per-request format selection always reads
    the matching bundle without a refetch.

    Args:
        cache_base: Root directory for the 17Lands cache.
        card_id_map_path: Path to NemeDraft's ``card_id_map.json``.
        user_group: Player-skill filter (``"All"``, ``"Platinum+"``, …).
        date_range_days: Look-back window for 17Lands data.
        draft_format: Default Arena format string for callers that don't
            pass one (kept for backwards compatibility).
        refresh_interval_s: TTL after which a cached bundle is considered
            stale. ``refresh_all`` uses it to decide which entries to
            re-fetch.
        trophy_prior_dir: Directory containing optional
            ``<SET>_trophy_deck_prior.json`` artifacts.
    """

    def __init__(
        self,
        cache_base: Path,
        card_id_map_path: Path,
        *,
        user_group: str = "All",
        date_range_days: int = 90,
        draft_format: str = "PremierDraft",
        refresh_interval_s: float = DEFAULT_REFRESH_INTERVAL_S,
        trophy_prior_dir: Path | None = None,
    ) -> None:
        self._cache_base = cache_base
        self._user_group = user_group
        self._date_range_days = date_range_days
        self._default_draft_format = draft_format
        self._refresh_interval_s = refresh_interval_s
        self._trophy_prior_dir = trophy_prior_dir or card_id_map_path.parent

        # name → nemedraft internal card ID
        with open(card_id_map_path, encoding="utf-8") as f:
            self._name_to_id: dict[str, int] = json.load(f)

        # (set_code, draft_format) → bundle
        self._sets: dict[tuple[str, str], _SetBundle] = {}
        self._trophy_priors: dict[str, TrophyDeckPrior | None] = {}
        self._lock = threading.Lock()
        self._loading: set[tuple[str, str]] = set()

    @property
    def default_draft_format(self) -> str:
        return self._default_draft_format

    # -- public API ----------------------------------------------------------

    def ensure_set(
        self,
        set_code: str,
        *,
        draft_format: str | None = None,
        callback: _Callback | None = None,
        force: bool = False,
    ) -> None:
        """Start loading 17Lands data for *(set_code, draft_format)* if not cached.

        Args:
            set_code: Three-letter set code.
            draft_format: 17Lands format (``"PremierDraft"`` |
                ``"QuickDraft"`` | …). Falls back to the instance default.
            callback: Called as ``callback(set_code, success)`` on the
                background thread once the download finishes.
            force: If True, re-fetch even when a fresh bundle exists.
        """
        fmt = draft_format or self._default_draft_format
        key = (set_code, fmt)
        with self._lock:
            existing = self._sets.get(key)
            if not force and existing and not self._is_stale(existing):
                if callback:
                    callback(set_code, True)
                return
            if key in self._loading:
                return
            self._loading.add(key)

        t = threading.Thread(
            target=self._fetch,
            args=(set_code, fmt, callback),
            daemon=True,
            name=f"17lands-{set_code}-{fmt}",
        )
        t.start()

    def get_card_map(
        self,
        set_code: str,
        *,
        draft_format: str | None = None,
    ) -> dict[str, CardRatings] | None:
        """Return the per-name card map for *(set_code, draft_format)*."""
        fmt = draft_format or self._default_draft_format
        with self._lock:
            bundle = self._sets.get((set_code, fmt))
        return bundle.card_map if bundle else None

    def get_set_metrics(
        self,
        set_code: str,
        *,
        draft_format: str | None = None,
    ) -> SetMetrics | None:
        """Return set-level metrics for *(set_code, draft_format)*."""
        fmt = draft_format or self._default_draft_format
        with self._lock:
            bundle = self._sets.get((set_code, fmt))
        return bundle.metrics if bundle else None

    def get_trophy_prior(
        self,
        set_code: str,
        *,
        draft_format: str | None = None,
    ) -> TrophyDeckPrior | None:
        """Return the optional trophy-deck prior for *(set_code, draft_format)*."""
        fmt = draft_format or self._default_draft_format
        with self._lock:
            bundle = self._sets.get((set_code, fmt))
        return bundle.trophy_prior if bundle else None

    def get_card_ratings_by_id(
        self,
        set_code: str,
        *,
        draft_format: str | None = None,
    ) -> dict[int, CardRatings] | None:
        """Return card ratings keyed by NemeDraft internal ID."""
        fmt = draft_format or self._default_draft_format
        with self._lock:
            bundle = self._sets.get((set_code, fmt))
        return bundle.by_id if bundle else None

    def is_loaded(
        self,
        set_code: str,
        *,
        draft_format: str | None = None,
    ) -> bool:
        fmt = draft_format or self._default_draft_format
        with self._lock:
            return (set_code, fmt) in self._sets

    def lookup_stats(
        self,
        set_code: str,
        card_name: str,
        *,
        formats: list[str],
        archetype: str = "All Decks",
    ) -> tuple[dict[str, float], str]:
        """Find usable 17Lands stats for *card_name*, trying *formats* in order.

        The fallback ladder is **gihwr-driven**: the first format whose
        bundle has ``gihwr > 0`` for *card_name* wins. When no format
        has a usable gihwr, the first format whose bundle has *any*
        signal (``ata > 0``) is returned as a last-resort estimate so
        the player still sees an ATA — same source format, no
        cross-format mixing.

        Args:
            set_code: Three-letter set code.
            card_name: Exact name as keyed in the 17Lands ``card_ratings``
                response.
            formats: Format names to try, highest priority first
                (e.g. ``["QuickDraft", "PremierDraft"]`` when the player
                is on QuickDraft but PD has fuller data for some cards).
            archetype: 17Lands archetype slice — almost always
                ``"All Decks"``.

        Returns:
            ``(stats_dict, source_format)`` where ``source_format`` names
            the format that supplied the data, or ``({}, "")`` when no
            format had any signal for *card_name*.
        """
        ata_only: tuple[dict[str, float], str] | None = None
        for fmt in formats:
            with self._lock:
                bundle = self._sets.get((set_code, fmt))
            if not bundle:
                continue
            cr = bundle.card_map.get(card_name)
            if not cr:
                continue
            stats = cr.deck_colors.get(archetype, {})
            if stats.get("gihwr", 0.0) > 0.0:
                return stats, fmt
            if ata_only is None and stats.get("ata", 0.0) > 0.0:
                ata_only = (stats, fmt)
        if ata_only is not None:
            return ata_only
        return {}, ""

    def refresh_all(self) -> None:
        """Force a re-fetch of every cached *(set, format)* pair.

        Intended to be invoked on a 24 h schedule from the server.
        Returns immediately; the actual downloads run in background
        threads.
        """
        with self._lock:
            pairs = list(self._sets.keys())
        logger.info("17Lands refresh: re-fetching %d (set, format) pairs", len(pairs))
        for set_code, fmt in pairs:
            self.ensure_set(set_code, draft_format=fmt, force=True)

    # -- internals -----------------------------------------------------------

    def _is_stale(self, bundle: "_SetBundle") -> bool:
        return (time.time() - bundle.fetched_at) >= self._refresh_interval_s

    def _fetch(
        self,
        set_code: str,
        draft_format: str,
        callback: _Callback | None,
    ) -> None:
        key = (set_code, draft_format)
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
                if callback:
                    try:
                        callback(set_code, True)
                    except Exception:
                        logger.exception("17Lands callback error (cached)")
                    callback = None  # don't fire again after network refresh

            # Network refresh (no-ops for archetypes still within TTL).
            try:
                card_map = client.download_set_data(set_code)
            finally:
                client.close()

            if not card_map:
                if not success:
                    logger.warning(
                        "17Lands returned no data for %s/%s", set_code, draft_format,
                    )
                with self._lock:
                    self._loading.discard(key)
                return

            self._install_bundle(set_code, card_map, draft_format)
            success = True
        except Exception:
            logger.exception(
                "Failed to load 17Lands data for %s/%s", set_code, draft_format,
            )
            with self._lock:
                self._loading.discard(key)
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
        metrics = SetMetrics.from_card_map(card_map)
        by_id: dict[int, CardRatings] = {}
        for name, cr in card_map.items():
            cid = self._name_to_id.get(name, 0)
            if cid:
                by_id[cid] = cr

        trophy_prior = self._load_trophy_prior(set_code)
        bundle = _SetBundle(
            card_map=card_map,
            metrics=metrics,
            by_id=by_id,
            trophy_prior=trophy_prior,
            draft_format=draft_format,
            fetched_at=time.time(),
        )
        with self._lock:
            self._sets[(set_code, draft_format)] = bundle
            self._loading.discard((set_code, draft_format))

        logger.info(
            "17Lands data ready for %s/%s "
            "(%d cards, %d matched by ID, trophy_prior=%s)",
            set_code,
            draft_format,
            len(card_map),
            len(by_id),
            trophy_prior is not None,
        )

    def _load_trophy_prior(self, set_code: str) -> TrophyDeckPrior | None:
        key = set_code.upper()
        with self._lock:
            if key in self._trophy_priors:
                return self._trophy_priors[key]

        path = default_prior_path(self._trophy_prior_dir, key)
        if not path.exists():
            with self._lock:
                self._trophy_priors[key] = None
            return None

        try:
            prior = TrophyDeckPrior.load(path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            logger.warning(
                "Ignoring invalid trophy-deck prior at %s", path, exc_info=True,
            )
            prior = None

        with self._lock:
            self._trophy_priors[key] = prior
        if prior:
            logger.info(
                "Loaded trophy-deck prior for %s from %s (%d archetypes)",
                key, path, len(prior.archetypes),
            )
        return prior


@dataclass
class _SetBundle:
    card_map: dict[str, CardRatings]
    metrics: SetMetrics
    by_id: dict[int, CardRatings]
    trophy_prior: TrophyDeckPrior | None
    draft_format: str
    fetched_at: float
