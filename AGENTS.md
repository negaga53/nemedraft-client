# AGENTS.md — nemedraft-client

> Read by all agents (Claude, Cursor, Copilot, Codex, Gemini…). This is the **overlay client**
> for NemeDraft (MTG Arena draft pick predictor): a public, MIT-licensed PySide6 app.
> When developed as a submodule of the private `nemedraft` parent repo, the parent's
> `AGENTS.md` also applies — its hard rules win on any conflict.

## Hard rules

1. **This is a published open-source repo with downstream users.** Never force-push `main`
   or rewrite history without explicit user confirmation. Releases are consumed by end-user
   overlay builds.
2. **Qt thread safety**: `LogWatcher` / `MemoryWatcher` callbacks run on plain Python threads.
   Never register widget-mutating callbacks directly — route them through `_UiMarshaler`,
   or Qt's stylesheet engine races and segfaults.
3. **PyInstaller bundling**: setuptools package-data is NOT honored by the release pipeline.
   Any new bundled asset must be added to `nemedraft_overlay.spec`'s `datas` list, or it will
   be silently missing from the built `.app` / `.exe`.
4. **No secrets**: `.env.client.local` style files are gitignored and must stay that way.
5. Git operations (commit, push, tag) are managed manually by the user — don't commit or
   push without explicit instruction. When working from the parent repo, every submodule
   commit must be followed by a parent-repo pointer bump.

## Commands

```bash
# Run the overlay (headless for CI: prefix with QT_QPA_PLATFORM=offscreen)
python scripts/run_overlay.py

# Tests (Qt needs offscreen)
QT_QPA_PLATFORM=offscreen pytest tests/ -v

# Refresh the bundled grpId → name map (weekly CI also does this)
python scripts/refresh_grpid_map.py
```

## Key components (`client/overlay/`)

- `LogWatcher` — tails Arena's `Player.log`.
- `ArenaCardMapper` — grpId ↔ name; Scryfall first, bundled `data/grpid_to_name.json` second,
  MTGA SQLite DB third (Windows only). Scryfall wins on collisions.
- `DraftState` — pack/pool state machine; `profile_for` keys `DraftFormatProfile` off the raw
  Arena `arena_format` token (PickTwo picks 2 cards/pass).
- `OverlayPredictor` — wrapper around the remote model API.
- `OverlayWindow` — Qt window rendering ranked pick recommendations.

Scryfall data is owned by this repo (`data/scryfall/`); the parent's `scripts/add_set.py`
reaches into these paths when a new MTG set is added.

## UI / Qt notes

- **Frameless window move/resize must go through the window manager.** `OverlayWindow` is
  `FramelessWindowHint | Tool`, so it has no native title bar or resize border. Dragging and
  resizing use `windowHandle().startSystemMove()` / `startSystemResize(edges)`. Do **not** drag
  via `self.move()` — it silently no-ops on Linux (X11 *and* Wayland) for `Qt.Tool` frameless
  windows, which read as "the title bar doesn't drag." `mousePressEvent` hit-tests a 6px edge
  zone (resize) then the header rect (move); the manual `_drag_pos` path is a fallback only.
- **The look is token-driven.** Font sizes, spacing, radii, and colours live in
  `ui/theme/tokens.py`; the stylesheet is generated in `ui/theme/qss.py`. Bump the `FONT_SIZE_*`
  scale there rather than hardcoding px. Row/column pixel dimensions live in `ui/pack_widgets.py`
  (and must stay in sync: `_W_BAR` == `ScoreBar._W`); the deck strip (archetype + colour
  commitment, mana curve, open lanes) sits full-width below the card table in `pack_tab.py`;
  its height is driven by `_CurveCard`'s fixed-90px `ManaCurvePlot` (`stats_tab.py`). Headless layout/size checks render via
  `OverlayWindow(...).grab()` under `QT_QPA_PLATFORM=offscreen`.
- **The window is opaque-only.** The old glass/transparent mode
  (`WA_TranslucentBackground` + translucent root + rounded corners + the
  Settings "Transparent window" toggle) was removed — it rendered poorly
  (see-through tab bar, remote-desktop/no-compositor artifacts). The whole
  window is `L0_WINDOW_OPAQUE`; only the Settings opacity slider (30–100%, via
  `setWindowOpacity`) controls translucency now. `build_stylesheet()` takes no
  mode argument.
- **A styled `QTabWidget` does not paint behind its tab-bar row.** A
  `QTabBar { background: transparent }` leaves the row unpainted; the bar must
  paint the window background **and** the `QTabWidget` must be in `documentMode`
  so the bar spans the full width (otherwise the gap beside the tabs stays
  unpainted). Guarded by `tests/test_tab_bar_opacity.py`.

## Releasing

Version bumps, tagging, and GitHub Releases follow the parent repo's release flow
(see the parent's release skill / docs). Don't tag or publish without explicit instruction.
