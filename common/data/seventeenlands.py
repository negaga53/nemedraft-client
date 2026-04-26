"""17Lands API client with local JSON caching."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://www.17lands.com"
USER_AGENT = "NemeDraft/0.1 (Educational Tool)"
CACHE_TTL_HOURS = 24
THROTTLE_SECONDS = 1.5

# All two-colour archetypes plus the aggregate.
COLOR_PAIRS: list[str] = [
    "All Decks",
    "WU", "WB", "WR", "WG",
    "UB", "UR", "UG",
    "BR", "BG",
    "RG",
]

# Fields extracted from the 17Lands response per card.
STAT_FIELDS: list[str] = [
    "ever_drawn_win_rate",   # GIHWR
    "opening_hand_win_rate", # OHWR
    "drawn_win_rate",        # GDWR
    "win_rate",              # GPWR (game-play WR)
    "never_drawn_win_rate",  # GNSWR
    "drawn_improvement_win_rate",  # IWD
    "avg_seen",              # ALSA
    "avg_pick",              # ATA
    "ever_drawn_game_count", # sample count
]

# Mapping from raw 17Lands keys to short names used internally.
FIELD_ALIASES: dict[str, str] = {
    "ever_drawn_win_rate": "gihwr",
    "opening_hand_win_rate": "ohwr",
    "drawn_win_rate": "gdwr",
    "win_rate": "gpwr",
    "never_drawn_win_rate": "gnswr",
    "drawn_improvement_win_rate": "iwd",
    "avg_seen": "alsa",
    "avg_pick": "ata",
    "ever_drawn_game_count": "samples",
}


def _cache_dir(base: Path) -> Path:
    d = base / "cache" / "17lands"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_key(
    set_code: str,
    draft_format: str,
    start_date: str,
    end_date: str,
    colors: str,
    user_group: str,
) -> str:
    parts = [set_code.lower(), draft_format.lower(), start_date, end_date,
             colors.lower().replace(" ", "_"), user_group.lower().replace("+", "plus")]
    return "_".join(parts) + ".json"


def _find_freshest_cache(
    cache_dir: Path,
    set_code: str,
    draft_format: str,
    colors: str,
    user_group: str,
) -> Path | None:
    """Find the most recently modified cache file matching the non-date parts.

    When the date range rolls over at midnight the exact cache key changes,
    but the data is still perfectly usable for a few more hours.  This
    function finds the newest file whose key matches everything *except*
    the date window so that stale-but-recent data can be served instantly.
    """
    prefix = f"{set_code.lower()}_{draft_format.lower()}_"
    suffix = f"_{colors.lower().replace(' ', '_')}_{user_group.lower().replace('+', 'plus')}.json"

    best: Path | None = None
    best_mtime: float = 0.0
    for p in cache_dir.glob(f"{prefix}*{suffix}"):
        mt = p.stat().st_mtime
        if mt > best_mtime:
            best = p
            best_mtime = mt
    return best


def _is_cache_stale(path: Path, hours: float = CACHE_TTL_HOURS) -> bool:
    if not path.exists():
        return True
    age = time.time() - path.stat().st_mtime
    return age > hours * 3600


def _atomic_write_json(path: Path, data: object) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@dataclass
class CardRatings:
    """Per-card statistics from 17Lands.

    ``deck_colors`` maps colour key (e.g. ``"All Decks"``, ``"UB"``) to a
    dict of stat short names (gihwr, ata, …) → float.
    """

    name: str
    deck_colors: dict[str, dict[str, float]]
    colors: list[str]
    mana_cost: str
    cmc: int
    rarity: str
    image_url: str


class SeventeenLandsClient:
    """Fetches card ratings from the 17Lands public API.

    Args:
        cache_base: Root directory under which ``cache/17lands/`` will be
            created for JSON caching.
        user_group: Player-skill filter sent to the API.  One of
            ``"All"``, ``"Platinum+"``, ``"Diamond+"``, ``"Mythic"``.
        date_range_days: How many days back from *today* to fetch.
        draft_format: Arena event format string.
    """

    def __init__(
        self,
        cache_base: Path,
        *,
        user_group: str = "All",
        date_range_days: int = 90,
        draft_format: str = "PremierDraft",
    ) -> None:
        self._cache = _cache_dir(cache_base)
        self.user_group = user_group
        self.date_range_days = date_range_days
        self.draft_format = draft_format
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={"User-Agent": USER_AGENT},
            timeout=30.0,
        )
        self._last_request_time: float = 0.0

    def close(self) -> None:
        self._client.close()

    # -- public API ----------------------------------------------------------

    def load_cached_set_data(self, set_code: str) -> dict[str, CardRatings] | None:
        """Load set data from disk cache only — no network requests.

        Returns:
            Card map if any cached data was found, *None* otherwise.
        """
        card_map: dict[str, CardRatings] = {}
        found = 0
        for colors in COLOR_PAIRS:
            recent = _find_freshest_cache(
                self._cache, set_code, self.draft_format, colors, self.user_group,
            )
            if recent:
                with open(recent, encoding="utf-8") as f:
                    data = json.load(f)
                self._merge_archetype(card_map, data, colors)
                found += 1
        if found == 0:
            return None
        logger.info(
            "17Lands: %s loaded %d cards from cache (%d/%d archetypes)",
            set_code, len(card_map), found, len(COLOR_PAIRS),
        )
        return card_map

    def download_set_data(self, set_code: str) -> dict[str, CardRatings]:
        """Fetch all archetype data for a set and merge into a per-card map.

        Returns:
            Dict mapping card name → :class:`CardRatings`.
        """
        from datetime import date, timedelta

        end = date.today()
        start = end - timedelta(days=self.date_range_days)
        start_str = start.isoformat()
        end_str = end.isoformat()

        card_map: dict[str, CardRatings] = {}

        for colors in COLOR_PAIRS:
            data, from_cache = self._fetch_archetype_with_cache(
                set_code, start_str, end_str, colors,
            )
            if data is None:
                continue
            self._merge_archetype(card_map, data, colors)
            if not from_cache:
                self._throttle()

        logger.info(
            "17Lands: %s loaded %d cards across %d archetypes",
            set_code, len(card_map), len(COLOR_PAIRS),
        )
        return card_map

    # -- internals -----------------------------------------------------------

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request_time
        if elapsed < THROTTLE_SECONDS:
            time.sleep(THROTTLE_SECONDS - elapsed)

    def _fetch_archetype_with_cache(
        self,
        set_code: str,
        start_date: str,
        end_date: str,
        colors: str,
    ) -> tuple[list[dict] | None, bool]:
        """Return (data_array, from_cache).

        Uses a two-tier cache strategy:
        1. Exact-key match (same date range) — used if within TTL.
        2. Fuzzy match (same set/format/colors, any date range) — used if
           the freshest file is within TTL, avoiding redundant fetches when
           the date window rolls over at midnight.
        """
        key = _cache_key(set_code, self.draft_format, start_date, end_date,
                         colors, self.user_group)
        path = self._cache / key

        # Tier 1: exact key, still fresh.
        if not _is_cache_stale(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f), True

        # Tier 2: any recent file for the same set/format/colors.
        recent = _find_freshest_cache(
            self._cache, set_code, self.draft_format, colors, self.user_group,
        )
        if recent and not _is_cache_stale(recent):
            with open(recent, encoding="utf-8") as f:
                return json.load(f), True

        # Network fetch
        params: dict[str, str] = {
            "expansion": set_code.upper(),
            "format": self.draft_format,
            "start_date": start_date,
            "end_date": end_date,
        }
        if colors != "All Decks":
            params["colors"] = colors
        if self.user_group and self.user_group != "All":
            params["user_group"] = self.user_group

        logger.debug("Fetching 17Lands: %s colors=%s", set_code, colors)
        try:
            resp = self._client.get("/card_ratings/data", params=params)
            self._last_request_time = time.time()
        except httpx.HTTPError:
            logger.warning("17Lands request failed for %s/%s", set_code, colors, exc_info=True)
            # Try stale cache as fallback
            if path.exists():
                with open(path, encoding="utf-8") as f:
                    return json.load(f), True
            return None, False

        if resp.status_code == 429:
            logger.warning("17Lands rate limited — using stale cache if available")
            if path.exists():
                with open(path, encoding="utf-8") as f:
                    return json.load(f), True
            return None, False

        if resp.status_code != 200:
            logger.warning("17Lands returned %d for %s/%s", resp.status_code, set_code, colors)
            return None, False

        data = resp.json()
        if not data:
            # Empty response — don't cache
            return None, False

        _atomic_write_json(path, data)
        return data, False

    def _merge_archetype(
        self,
        card_map: dict[str, CardRatings],
        data: list[dict],
        colors: str,
    ) -> None:
        """Merge one archetype's card array into the master map."""
        for raw in data:
            name = raw.get("name", "")
            if not name:
                continue

            stats: dict[str, float] = {}
            for raw_key, alias in FIELD_ALIASES.items():
                val = raw.get(raw_key)
                stats[alias] = float(val) if val is not None else 0.0

            if name in card_map:
                card_map[name].deck_colors[colors] = stats
            else:
                # Extract colour identity from Scryfall-style colour list
                card_colors: list[str] = []
                for c in raw.get("color", ""):
                    if c in "WUBRG":
                        card_colors.append(c)

                img = raw.get("url", "")
                if img and not img.startswith("http"):
                    img = BASE_URL + img

                card_map[name] = CardRatings(
                    name=name,
                    deck_colors={colors: stats},
                    colors=card_colors,
                    mana_cost=raw.get("mana_cost", ""),
                    cmc=int(raw.get("cmc", 0)),
                    rarity=raw.get("rarity", ""),
                    image_url=img,
                )
