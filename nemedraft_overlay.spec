# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the NemeDraft overlay client (single-file build)."""

import sys
from pathlib import Path

block_cipher = None
ROOT = Path(SPECPATH)

# ---- Data files to bundle alongside the frozen code ----
datas = [
    # i18n translations and card name cache
    (str(ROOT / "client" / "overlay" / "i18n" / "translations.json"), "client/overlay/i18n"),
    # Base client configuration (bundled as fallback defaults)
    (str(ROOT / ".env.client"), "."),
    # Bundled mtgjson grpId→name fallback — cross-platform; required on
    # macOS/Linux where the MTGA SQLite fallback can't reach the
    # Wine-installed card DB.
    (str(ROOT / "client" / "overlay" / "data" / "grpid_to_name.json"), "client/overlay/data"),
]

# Mana icon SVGs (bundled so they work offline — may not exist in CI)
_mana_icons = ROOT / "data" / "mana_icons"
if _mana_icons.is_dir() and any(_mana_icons.iterdir()):
    datas.append((str(_mana_icons), "data/mana_icons"))

# App icon — needed at runtime for the window icon AND the system-tray
# icon (used by minimize-to-tray on Windows where Qt.Tool windows have no
# taskbar entry). The PyInstaller `icon=` argument below sets the .exe /
# .icns icon but does NOT copy the PNG into the bundle; this datas entry
# does. Per AGENTS.md memory, package-data isn't honored here.
_app_icon = ROOT / "assets" / "icon.png"
if _app_icon.is_file():
    datas.append((str(_app_icon), "assets"))

# Card cache directory (may not exist yet at build time)
card_cache = ROOT / "client" / "overlay" / "i18n" / "card_cache"
if card_cache.is_dir() and any(card_cache.iterdir()):
    datas.append((str(card_cache), "client/overlay/i18n/card_cache"))

# card_id_map.json — required for prediction
_card_id_map = ROOT / "data" / "processed" / "card_id_map.json"
if _card_id_map.is_file():
    datas.append((str(_card_id_map), "data/processed"))

# Scryfall per-set JSON files — required for Arena grpId→name mapping
# Exclude the large bulk dump (default_cards.json) which is only used for
# re-generating the per-set files.
_scryfall = ROOT / "data" / "scryfall"
if _scryfall.is_dir():
    for _sj in _scryfall.glob("*_cards.json"):
        if _sj.name == "default_cards.json":
            continue
        datas.append((str(_sj), "data/scryfall"))

# ---- Hidden imports required at runtime ----
# Only client overlay + the common modules it actually uses.
# The overlay talks to the server API — it does NOT run model inference locally,
# so torch, scipy, common.model.*, and common.inference.predictor are excluded.
hiddenimports = [
    # --- Client overlay ---
    "client",
    "client.overlay",
    "client.overlay.main",
    "client.overlay.boot",
    "client.overlay.events",
    "client.overlay.managers",
    "client.overlay.managers.arena_poller",
    "client.overlay.managers.auth_polling",
    "client.overlay.managers.prediction",
    "client.overlay.managers.set_data",
    "client.overlay.managers.worker_pool",
    "client.overlay.managers.workers",
    "client.overlay.arena_memory",
    "client.overlay.memory_watcher",
    "client.overlay.memory",
    "client.overlay.memory.platform",
    "client.overlay.memory.exceptions",
    "client.overlay.memory.offsets",
    "client.overlay.memory.reader",
    "client.overlay.memory.pe",
    "client.overlay.memory.mono",
    "client.overlay.memory.walker",
    "client.overlay.memory.session",
    "client.overlay.config",
    "client.overlay.env",
    "client.overlay.log_watcher",
    "client.overlay.draft_state",
    "client.overlay.card_mapper",
    "client.overlay.card_art",
    "client.overlay.mana_icons",
    "client.overlay.notifications",
    "client.overlay.api_client",
    "client.overlay.auth_client",
    "client.overlay.updater",
    "client.overlay.signals",
    "client.overlay.i18n",
    "client.overlay.ui",
    "client.overlay.ui.window",
    "client.overlay.ui.home_tab",
    "client.overlay.ui.pack_tab",
    "client.overlay.ui.deck_tab",
    "client.overlay.ui.stats_tab",
    "client.overlay.ui.settings_tab",
    "client.overlay.ui.toast",
    "client.overlay.ui.screen_utils",
    "client.overlay.ui.styles",
    # --- Common: lightweight data/inference modules (no torch) ---
    "common",
    "common.data",
    "common.data.card_stats",
    "common.data.seventeenlands",
    "common.data.set_data_manager",
    "common.inference",
    "common.inference.pool_analyzer",
    "common.inference.deck_builder",
    "common.inference.signals",
]

# pymem / pefile are imported lazily inside client.overlay.memory.platform,
# so PyInstaller's static analyzer cannot see them. They are Windows-only
# (Arena memory access). On macOS/Linux the overlay falls back to log
# parsing and these imports are skipped.
if sys.platform == "win32":
    hiddenimports.extend([
        "pymem",
        "pymem.process",
        "pymem.exception",
        "pefile",
    ])

a = Analysis(
    [str(ROOT / "scripts" / "run_overlay.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Server / training packages — not needed by the overlay client
        "server",
        "training",
        "common.model",
        "common.model.draft_model",
        "common.model.card_encoder",
        "common.model.gnn_module",
        "common.model.scoring_head",
        "common.model.transformer",
        "common.model.lora",
        "common.inference.predictor",
        "common.data.synergy_graph",
        "client.overlay.prediction",
        "client.overlay.ocr",
        # Heavy third-party packages not needed by the client
        "torch",
        "torch_geometric",
        "sentence_transformers",
        "transformers",
        "fastapi",
        "uvicorn",
        "scipy",
        "matplotlib",
        "pandas",
        "polars",
        "pyarrow",
        "IPython",
        "notebook",
        "jupyter",
        "tensorboard",
        "pytest",
        # Dev/CLI packages that inflate the binary
        "pygments",
        "rich",
        "psutil",
        "tqdm",
    ],
    noarchive=False,
    cipher=block_cipher,
)

# Remove api-ms-win-* forwarding DLLs — these are system-provided on
# Windows 10+ and bundling copies from non-system locations (e.g. JDK,
# conda) breaks LoadLibrary resolution for python311.dll.
a.binaries = [
    b for b in a.binaries
    if not b[0].lower().startswith("api-ms-win-")
]

pyz = PYZ(a.pure, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="NemeDraft",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,           # GUI app — no console window
    disable_windowed_traceback=False,
    argv_emulation=sys.platform == "darwin",  # macOS needs argv emulation
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=(
        str(ROOT / "assets" / "version_info.txt")
        if sys.platform == "win32"
           and (ROOT / "assets" / "version_info.txt").exists()
        else None
    ),
    icon=str(
        ROOT / "assets" / (
            "icon.ico" if sys.platform == "win32"
            else "icon.icns" if sys.platform == "darwin"
            else "icon.png"
        )
    ) if (ROOT / "assets").is_dir() else None,
)

# macOS .app bundle
if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="NemeDraft.app",
        icon=(
            str(ROOT / "assets" / "icon.icns")
            if (ROOT / "assets" / "icon.icns").exists()
            else None
        ),
        bundle_identifier="com.nemedraft.overlay",
        info_plist={
            "NSHighResolutionCapable": True,
            "CFBundleShortVersionString": "0.1.0",
        },
    )
