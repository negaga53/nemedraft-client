# Overlay freeze fix: async card art + visible prediction-loading feedback

Date: 2026-08-06
Status: approved for implementation (user pre-authorized: plan → subagent implementation)

## Problem

The overlay window freezes (unresponsive, no repaints) in two situations:

1. **Before the draft starts** — between entering the draft and seeing the
   first P1P1 predictions, and when attaching to an existing draft.
2. **Between picks** — from the moment a new pack triggers a prediction
   request until shortly after the result arrives.

## Root cause

All server I/O (predict, queue heartbeat, signals, deck suggestions, set
data) already runs on `QThread` workers. The single remaining blocking
call on the Qt main thread is `CardArtCache.get()`, which performs a
synchronous Scryfall HTTP fetch per uncached card, paced at ≥100 ms per
request (`client/overlay/card_art.py`). Call sites on the UI thread:

| Site | Cards fetched | Freeze |
|---|---|---|
| `main.py:1140` `_on_prediction_results` | up to 14 per pack | multi-second freeze right when each prediction lands; worst at P1P1 (cold cache) |
| `main.py:594` `_apply_deck_pool` | whole pool (~45) | 10–20 s freeze on mid-/post-draft attach |
| `ui/pack_tab.py:280` taken-cards render | per taken card | stutter on history render |
| `ui/pack_tab.py:510` hover `enterEvent` | 1 | UI freeze on hover of an uncached card |

Secondary issue: while a prediction is in flight the only feedback is a
3 px indeterminate `QProgressBar` (`pack_tab.loading_bar`), so even the
normal 1–3 s server round-trip reads as a hang.

`CardArtCache.get_cached()` (non-blocking) and the per-card UI patch path
`OverlayWindow.update_card_art()` → `PackTab.update_card_art()` →
`CardRow.set_art()` already exist; no code calls them today. The design
completes that intended pipeline.

## Design

### Part A — asynchronous art pipeline (kills the freezes)

New `ArtPrefetchWorker(QThread)` in `client/overlay/managers/workers.py`,
following the existing worker pattern:

- ctor takes the `CardArtCache` and a `list[str]` of card names;
- `run()` loops the names, calls `cache.get(name)` (blocking is fine — we
  are off-thread; the cache's class-level lock already serialises and
  rate-limits across threads), and emits `art_ready = Signal(str, object)`
  (`card_name, Path | None`) per card as each lands;
- launched through the existing `WorkerPool` (main.py keeps one pool or
  reuses `self._prediction`'s pattern — a small `self._art_pool = WorkerPool()`
  on `OverlayApp` is fine).

Call-site changes (all on the main thread):

1. `_on_prediction_results` (`main.py`): build `art_paths` from
   `art_cache.get_cached()` only; render immediately (rows show text, art
   pops in); collect misses and launch one `ArtPrefetchWorker`; connect
   `art_ready` → `self.window.update_card_art` (queued to the UI thread by
   Qt's cross-thread signal delivery). Late results for a superseded pack
   are harmless: they warm the disk cache and `update_card_art` only
   patches rows that still display that card.
2. `_apply_deck_pool` (`main.py`): same pattern; per-card signal handler
   calls `self.window.deck_tab.set_art_paths({name: path})` (existing
   incremental API) in addition to `window.update_card_art`.
3. `PackTab` taken-cards render + hover `_make_enter`
   (`ui/pack_tab.py`): replace `self._art_cache.get(name)` with
   `self._art_cache.get_cached(name)`. Missing art there is filled by the
   prefetch worker launched from `_on_prediction_results` (taken cards
   come from the same pack) — no extra fetch path needed from the tab.

Threading rules respected: workers never touch widgets (signal-only,
same as every existing manager — see the `_UiMarshaler` lesson).

### Part B — visible loading feedback (spinner + status text)

New `LoadingIndicator` widget in `client/overlay/ui/pack_tab.py` (or a
small `ui/widgets/` module if one fits better): a QPainter-drawn rotating
arc (QTimer-driven, ~80 ms tick; no image assets, so the PyInstaller
`datas` list is untouched) plus a `QLabel` status text, laid out
horizontally, centered, hidden by default.

States and wiring (all signals already exist):

| Trigger | Existing signal/slot | Text (i18n key) |
|---|---|---|
| prediction dispatched | `PredictionManager.loading` → `show_prediction_loading(pn, pk)` | `predicting_pack_pick` — "Getting picks for P{pack}P{pick}…" |
| retry scheduled | `PredictionManager.retrying` → `_on_prediction_retrying` | `prediction_retrying` — "Server busy — retrying (attempt {n})…" |
| results rendered | `update_predictions` → `hide_loading()` | hidden |
| draft started, no pack yet | `show_waiting` path / `show_draft_started` | `waiting_first_pack` — "Draft starting — waiting for the first pack…" |
| give-up | `gave_up` → `_on_server_failure` | hidden (existing toast covers it) |

Placement: inside the pack view page above the card list, replacing the
bare 3 px `loading_bar` as the primary affordance (`show_loading()` /
`hide_loading()` grow a `message: str` parameter; the 3 px bar can stay
or be removed — implementer's choice, keep it simple). The spinner must
keep animating while results are awaited — with Part A in place the main
thread is idle during the wait, so it will.

i18n: add the new keys to **all 9 languages** in
`client/overlay/i18n/translations.json` (en, fr, es, de, pt, it, ja, ko,
zh-Hans), using `tr()` like the neighbouring code. `retranslate()` must
refresh the static parts.

## Testing

- `tests/test_card_art.py`: `get_cached()` never triggers network (already
  partially covered — extend if needed).
- New worker test: `ArtPrefetchWorker` emits `art_ready` per name and
  never raises on fetch failure (mock cache).
- `tests/test_pack_tab.py`-style test: rendering predictions/taken cards
  with an art cache stub asserts `.get()` is never called on the UI path
  (only `get_cached`).
- Spinner tests: visible + text set after `show_loading("msg")`, hidden
  after `update_predictions`; timer stops when hidden (no idle repaint).
- Full suite: `python -m pytest tests/ -x -q` from the client repo root
  (offscreen Qt: tests already handle `QT_QPA_PLATFORM=offscreen`).

## Out of scope

- Server-side latency work, art pre-warming at set-data load time (could
  prefetch the whole set's art on draft start later — YAGNI for now).
- Client release/tagging (separate flow via nemedraft-releasing skill).
