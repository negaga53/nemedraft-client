"""Card art must never be fetched on the Qt main thread.

Covers the async art pipeline: ``ArtPrefetchWorker`` (blocking
``CardArtCache.get`` off-thread, one ``art_ready`` per name) and the UI
call sites, which may only consult the non-blocking ``get_cached``.
"""

from __future__ import annotations

import os
import threading
import time

# Must be set before any PySide6 import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _wait_until(predicate, qapp, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        qapp.processEvents()
        time.sleep(0.01)
    return predicate()


class _StubArtCache:
    """Art cache double that records which lookup path was taken.

    ``get`` is the blocking (network) path; ``get_cached`` is the
    non-blocking one. Tests assert on ``get_calls`` / ``get_cached_calls``.
    """

    def __init__(
        self,
        cached: dict | None = None,
        *,
        raise_on: set | None = None,
    ) -> None:
        self.enabled = True
        self._cached = dict(cached or {})
        self._raise_on = set(raise_on or ())
        self.get_calls: list[str] = []
        self.get_cached_calls: list[str] = []
        self.get_threads: list[int] = []

    def get(self, name: str):
        self.get_calls.append(name)
        self.get_threads.append(threading.get_ident())
        if name in self._raise_on:
            raise RuntimeError(f"boom for {name}")
        return self._cached.get(name)

    def get_cached(self, name: str):
        self.get_cached_calls.append(name)
        return self._cached.get(name)


def _pick(card: str):
    from client.overlay.api_client import Pick

    return Pick(
        card=card, score=0.8, rank=1, is_elite=False,
        colors=["U"], mana_cost="{U}", type_line="Instant",
        gihwr=0.58, ata=3.4, iwd=0.0, stats_loaded=True,
    )


# -- ArtPrefetchWorker -------------------------------------------------------


def test_worker_emits_art_ready_per_name_off_the_ui_thread(qapp):
    from client.overlay.managers.workers import ArtPrefetchWorker

    cache = _StubArtCache({"Opt": "/art/opt.jpg", "Shock": None})
    worker = ArtPrefetchWorker(cache, ["Opt", "Shock"])
    seen: list[tuple[str, object]] = []
    worker.art_ready.connect(lambda name, path: seen.append((name, path)))
    worker.start()

    assert worker.wait(5000)
    assert _wait_until(lambda: len(seen) == 2, qapp)
    assert seen == [("Opt", "/art/opt.jpg"), ("Shock", None)]
    # The whole point: the blocking fetch ran on a worker thread.
    assert cache.get_calls == ["Opt", "Shock"]
    assert all(t != threading.get_ident() for t in cache.get_threads)


def test_worker_survives_a_cache_that_raises(qapp):
    from client.overlay.managers.workers import ArtPrefetchWorker

    cache = _StubArtCache({"Opt": "/art/opt.jpg"}, raise_on={"Bogus Card"})
    worker = ArtPrefetchWorker(cache, ["Bogus Card", "Opt"])
    seen: list[tuple[str, object]] = []
    worker.art_ready.connect(lambda name, path: seen.append((name, path)))
    worker.start()

    assert worker.wait(5000)
    # A failure yields None for that card and does not abort the batch.
    assert _wait_until(lambda: len(seen) == 2, qapp)
    assert seen == [("Bogus Card", None), ("Opt", "/art/opt.jpg")]


def test_worker_with_no_names_emits_nothing(qapp):
    from client.overlay.managers.workers import ArtPrefetchWorker

    cache = _StubArtCache()
    worker = ArtPrefetchWorker(cache, [])
    seen: list = []
    worker.art_ready.connect(lambda name, path: seen.append(name))
    worker.start()

    assert worker.wait(5000)
    qapp.processEvents()
    assert seen == []
    assert cache.get_calls == []


# -- PackTab UI paths --------------------------------------------------------


def test_pack_tab_render_never_blocks_on_art(qapp):
    """Live rows + taken rows use ``get_cached`` only."""
    from client.overlay.ui.pack_tab import PackTab

    tab = PackTab(show_art=False)
    cache = _StubArtCache({"Taken One": "/art/taken.jpg"})
    tab.set_art_cache(cache)

    tab.update_predictions(
        [_pick("Opt"), _pick("Shock")],
        None,
        taken_names=["Taken One", "Taken Two"],
        pack_number=0,
        pick_number=8,
    )

    assert cache.get_calls == []
    assert cache.get_cached_calls == ["Taken One", "Taken Two"]
    # The hit is memoised so a re-render doesn't look it up again.
    assert tab._art_paths["Taken One"] == "/art/taken.jpg"
    assert "Taken Two" not in tab._art_paths


def test_pack_tab_hover_never_blocks_on_art(qapp):
    from client.overlay.ui.pack_tab import PackTab

    tab = PackTab(show_art=False)
    cache = _StubArtCache({"Opt": "/art/opt.jpg"})
    tab.set_art_cache(cache)
    tab.update_predictions([_pick("Opt"), _pick("Uncached")], None)
    cache.get_cached_calls.clear()

    # Drive the handlers the rows actually carry, so the wiring is pinned.
    from client.overlay.ui.pack_widgets import CardRow

    rows = [
        tab._card_layout.itemAt(i).widget()
        for i in range(tab._card_layout.count())
        if tab._card_layout.itemAt(i) is not None
        and isinstance(tab._card_layout.itemAt(i).widget(), CardRow)
    ]
    assert len(rows) == 2
    for row in rows:
        row.enterEvent(None)

    assert cache.get_calls == []
    assert cache.get_cached_calls == ["Opt", "Uncached"]


def test_pack_tab_update_card_art_patches_a_visible_row(qapp):
    """The prefetch worker's per-card delivery lands on the rendered row."""
    from client.overlay.ui.pack_tab import PackTab

    tab = PackTab(show_art=False)
    cache = _StubArtCache()
    tab.set_art_cache(cache)
    tab.update_predictions([_pick("Opt")], None)

    tab.update_card_art("Opt", "/art/opt.jpg")

    assert tab._art_paths["Opt"] == "/art/opt.jpg"
    # A miss is recorded too, so later renders don't re-request it.
    tab.update_card_art("Shock", None)
    assert tab._art_paths["Shock"] is None
    assert cache.get_calls == []
