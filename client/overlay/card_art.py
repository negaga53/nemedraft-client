"""Download and cache Scryfall card art thumbnails for the overlay UI."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Scryfall image API base — art_crop is 626×457, "small" is 146×204.
# We use "small" for the overlay thumbnails (lightweight, fast).
_SCRYFALL_IMAGE_BASE = "https://api.scryfall.com/cards/named"

# Persistent on-disk cache directory
from client.overlay.env import _project_root
DEFAULT_CACHE_DIR = _project_root() / "data" / "card_art_cache"


class CardArtCache:
    """Fetches and caches small card art images from Scryfall.

    Images are stored as ``{cache_dir}/{md5(name)}.jpg`` so names with
    special characters are safe.  The cache is persistent across runs.

    Args:
        cache_dir: Local directory for cached images.
        enabled: When *False* no network requests or file I/O happen and
            ``get`` always returns *None*.
    """

    def __init__(
        self,
        cache_dir: Path = DEFAULT_CACHE_DIR,
        *,
        enabled: bool = True,
    ) -> None:
        self._cache_dir = cache_dir
        self._enabled = enabled
        self._mem: dict[str, Path | None] = {}  # hot in-process cache
        if enabled:
            self._cache_dir.mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        """Whether the cache is active."""
        return self._enabled

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    def get(self, card_name: str) -> Path | None:
        """Return the local path to a cached thumbnail, fetching if needed.

        Args:
            card_name: Exact Scryfall card name.

        Returns:
            Path to the JPEG file, or *None* on failure / disabled.
        """
        if not self._enabled:
            return None

        if card_name in self._mem:
            return self._mem[card_name]

        path = self._path_for(card_name)
        if path.exists():
            self._mem[card_name] = path
            return path

        # Fetch from Scryfall
        fetched = self._fetch(card_name, path)
        self._mem[card_name] = fetched
        return fetched

    def prefetch(self, names: list[str]) -> None:
        """Best-effort batch prefetch (miss only = network)."""
        for name in names:
            self.get(name)

    def cache_size_bytes(self) -> int:
        """Return total size of cached image files in bytes."""
        if not self._cache_dir.exists():
            return 0
        return sum(f.stat().st_size for f in self._cache_dir.glob("*.jpg") if f.is_file())

    def clear(self) -> int:
        """Delete all cached card art files.  Returns number removed."""
        count = 0
        if self._cache_dir.exists():
            for f in self._cache_dir.glob("*.jpg"):
                f.unlink(missing_ok=True)
                count += 1
        self._mem.clear()
        return count

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _path_for(self, card_name: str) -> Path:
        h = hashlib.md5(card_name.encode()).hexdigest()  # noqa: S324
        return self._cache_dir / f"{h}.jpg"

    def _fetch(self, card_name: str, dest: Path) -> Path | None:
        # Split double-faced card names to use the front face
        query_name = card_name.split(" // ")[0]
        try:
            with httpx.Client(timeout=10, follow_redirects=True) as client:
                resp = client.get(
                    _SCRYFALL_IMAGE_BASE,
                    params={"exact": query_name, "format": "image", "version": "small"},
                )
                if resp.status_code != 200:
                    logger.debug("Scryfall image %d for %r", resp.status_code, card_name)
                    return None
                dest.write_bytes(resp.content)
            logger.debug("Cached art for %s → %s", card_name, dest)
            return dest
        except Exception:
            logger.debug("Failed to fetch art for %s", card_name, exc_info=True)
            return None
