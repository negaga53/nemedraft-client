"""Persistent overlay configuration — loaded from / saved to JSON."""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


def _config_dir() -> Path:
    """Return the platform-appropriate configuration directory."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", str(Path.home()))
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(base) / "NemeDraft"


_CONFIG_DIR = _config_dir()
_CONFIG_FILE = _CONFIG_DIR / "config.json"
LOG_FILE = _CONFIG_DIR / "overlay.log"


@dataclass
class DisplaySettings:
    """UI display preferences."""

    show_gihwr: bool = True
    show_ata: bool = True
    show_iwd: bool = True
    result_format: str = "percentage"  # "percentage" | "letter" | "rating"
    language: str = "en"  # ISO language code — see overlay.i18n.LANGUAGE_NAMES


@dataclass
class DataSettings:
    """17Lands data fetch preferences."""

    user_group: str = "All"  # "All" | "Platinum+" | "Diamond+" | "Mythic"
    date_range_days: int = 90
    draft_format: str = "PremierDraft"


@dataclass
class OverlaySettings:
    """Window appearance preferences."""

    opacity: float = 0.85
    show_art: bool = True
    geometry: str = ""  # legacy single-geometry slot (pre-0.6 fallback)
    view_mode: str = "full"      # "full" | "compact" — restored per draft
    geometry_full: str = ""      # persisted geometry per view mode
    geometry_compact: str = ""


@dataclass
class FeatureToggles:
    """Feature enable/disable flags."""

    signals_enabled: bool = True
    deck_builder_enabled: bool = True


@dataclass
class OverlayConfig:
    """Root configuration object combining all settings groups."""

    display: DisplaySettings = field(default_factory=DisplaySettings)
    data: DataSettings = field(default_factory=DataSettings)
    overlay: OverlaySettings = field(default_factory=OverlaySettings)
    features: FeatureToggles = field(default_factory=FeatureToggles)


def _to_dict(obj: object) -> dict:
    """Recursively convert dataclass instances to dicts."""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _to_dict(v) for k, v in obj.__dict__.items()}
    return obj  # type: ignore[return-value]


def _merge(defaults: dict, loaded: dict) -> dict:
    """Merge loaded values into defaults, ignoring unknown keys."""
    for k, v in loaded.items():
        if k in defaults:
            if isinstance(defaults[k], dict) and isinstance(v, dict):
                _merge(defaults[k], v)
            else:
                defaults[k] = v
    return defaults


def load_config(path: Path | None = None) -> OverlayConfig:
    """Load configuration from disk or return defaults."""
    path = path or _CONFIG_FILE
    cfg = OverlayConfig()
    if not path.exists():
        return cfg

    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.warning("Could not read config file %s, using defaults", path)
        return cfg

    defaults = _to_dict(cfg)
    merged = _merge(defaults, raw)

    cfg.display = DisplaySettings(**merged.get("display", {}))
    cfg.data = DataSettings(**merged.get("data", {}))
    cfg.overlay = OverlaySettings(**merged.get("overlay", {}))
    cfg.features = FeatureToggles(**merged.get("features", {}))
    return cfg


def save_config(cfg: OverlayConfig, path: Path | None = None) -> None:
    """Atomically write configuration to disk."""
    path = path or _CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(_to_dict(cfg), f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
