"""Colours and styling constants for the overlay UI."""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# MTG colour identity → display colours (hex)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Console HUD palette
# ---------------------------------------------------------------------------

BG_PRIMARY     = "#0f0f1c"            # main window background
BG_ELEVATED    = "rgba(20,20,36,.6)"  # status cards, deck rail cards
BG_CARD        = "rgba(20,20,36,.55)" # slightly lighter elevated surface
BORDER_SUBTLE  = "#1f1f30"            # dividers, section bottoms
BORDER_SOFT    = "#2a2a3e"            # input borders, soft outlines
ACCENT_GOLD    = "#cfb53b"            # rank 1, section caps, active tab
TEXT_PRIMARY   = "#e0e0e0"
TEXT_SECONDARY = "#ccccdd"
TEXT_MUTED     = "#888888"
TEXT_DIM       = "#666666"

MTG_COLORS = {
    "W": "#F9FAF4",  # White — warm off-white
    "U": "#0E68AB",  # Blue
    "B": "#150B00",  # Black
    "R": "#D3202A",  # Red
    "G": "#00733E",  # Green
}

# Multi-colour / gold
COLOR_MULTI = "#CFB53B"
# Colourless / artifacts
COLOR_COLORLESS = "#A8A8A8"

# Mana pip display colours (slightly brighter for visibility on dark bg).
_MANA_PIP_COLORS: dict[str, str] = {
    "W": "#F5E6A3",
    "U": "#3B8FD4",
    "B": "#9B8E82",
    "R": "#E74C3C",
    "G": "#2ECC71",
}

_PIP_RE = re.compile(r"\{(.*?)\}")


def mana_pips_html(mana_cost: str) -> str:
    """Convert Scryfall mana cost like ``{1}{W}{U}`` to HTML with coloured dots.

    Returns:
        HTML string suitable for ``QLabel`` with rich text.
    """
    if not mana_cost:
        return ""
    parts: list[str] = []
    for m in _PIP_RE.finditer(mana_cost):
        pip = m.group(1).upper()
        if pip in _MANA_PIP_COLORS:
            parts.append(f'<font color="{_MANA_PIP_COLORS[pip]}">●</font>')
        elif pip == "X":
            parts.append('<font color="#A8A8A8">X</font>')
        elif "/" in pip:
            # Hybrid — show both colours.
            options = pip.split("/")
            cols = [_MANA_PIP_COLORS.get(o.strip(), "#A8A8A8") for o in options]
            parts.append(f'<font color="{cols[0]}">●</font>')
        else:
            # Generic / colourless number.
            parts.append(f'<font color="#A8A8A8">{pip}</font>')
    return "".join(parts)


def color_pips_html(colors: list[str]) -> str:
    """Convert a list of WUBRG colour letters to coloured dot HTML."""
    if not colors:
        return '<font color="#A8A8A8">●</font>'
    return "".join(
        f'<font color="{_MANA_PIP_COLORS.get(c, "#A8A8A8")}">●</font>'
        for c in colors
    )


def card_row_bg(colors: list[str], is_top: bool = False) -> str:
    """Return a subtle RGBA background CSS for a card row based on its colours.

    Args:
        colors: Card colours (W, U, B, R, G).
        is_top: Whether this is the #1 ranked card.

    Returns:
        CSS ``background-color`` value.
    """
    _BG_MAP = {
        "W": "rgba(245, 230, 163, 25)",
        "U": "rgba(59, 143, 212, 25)",
        "B": "rgba(155, 142, 130, 20)",
        "R": "rgba(231, 76, 60, 25)",
        "G": "rgba(46, 204, 113, 25)",
    }
    if is_top:
        alpha_boost = 15
    else:
        alpha_boost = 0

    if not colors:
        return f"rgba(168, 168, 168, {10 + alpha_boost})"
    if len(colors) > 1:
        return f"rgba(207, 181, 59, {20 + alpha_boost})"
    base = _BG_MAP.get(colors[0], f"rgba(168, 168, 168, {10 + alpha_boost})")
    if alpha_boost:
        # Increase alpha for top card.
        base = base.rsplit(",", 1)[0] + f", {25 + alpha_boost})"
    return base


def short_type(type_line: str) -> str:
    """Abbreviate a Scryfall type line to a short display string.

    Examples:
        ``"Creature — Elf Warrior"`` → ``"Cre"``
        ``"Instant"`` → ``"Ins"``
    """
    tl = type_line.lower().split("—")[0].strip()
    if "creature" in tl:
        return "Cre"
    if "instant" in tl:
        return "Ins"
    if "sorcery" in tl:
        return "Sor"
    if "enchantment" in tl:
        return "Enc"
    if "artifact" in tl:
        return "Art"
    if "planeswalker" in tl:
        return "Plw"
    if "land" in tl:
        return "Lnd"
    if "battle" in tl:
        return "Bat"
    return "???"

# ---------------------------------------------------------------------------
# Score rating colour scale (green → yellow → red)
# ---------------------------------------------------------------------------

SCORE_COLOR_HIGH = "#4CAF50"    # Top pick — green
SCORE_COLOR_MID = "#FFC107"     # Mid-tier — amber
SCORE_COLOR_LOW = "#F44336"     # Worst pick — red


def score_to_color(score: float, max_score: float) -> str:
    """Interpolate a hex colour from red→yellow→green based on relative score.

    Args:
        score: The card score.
        max_score: The highest score in the pack (for normalisation).

    Returns:
        Hex colour string.
    """
    if max_score <= 0:
        return SCORE_COLOR_MID
    t = score / max_score  # 0..1
    if t > 0.7:
        return SCORE_COLOR_HIGH
    if t > 0.35:
        return SCORE_COLOR_MID
    return SCORE_COLOR_LOW


def medal_color(rank: int) -> str | None:
    """Return medal hex for GIH% rank 1/2/3, else None.

    Silver is pushed toward near-white so it's clearly distinct from the
    neutral #aabbcc stat colour on the dark overlay background.
    """
    return {1: "#cfb53b", 2: "#f5f5f5", 3: "#cd7f32"}.get(rank)


def score_fill_gradient(fraction: float) -> str:
    """CSS linear-gradient string for the ScoreBar fill at *fraction* (0..1)."""
    if fraction > 0.7:
        return "qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #2e7d32, stop:1 #4caf50)"
    if fraction > 0.35:
        return "qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #b4811c, stop:1 #ffc107)"
    return "qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #852a25, stop:1 #f44336)"


# ---------------------------------------------------------------------------
# Shared tab / widget styles for both modes
# ---------------------------------------------------------------------------

_SHARED_WIDGET_STYLES = """
QTabWidget::pane {
    border: none;
    padding: 0;
}

QTabBar::tab {
    padding: 8px 14px;
    font-size: 12px;
    font-weight: 600;
    border: none;
    border-bottom: 2px solid transparent;
    color: #888888;
    text-transform: uppercase;
    letter-spacing: 1px;
    min-width: 60px;
}

QTabBar::tab:selected {
    color: #cfb53b;
    border-bottom: 2px solid #cfb53b;
}

QTabBar::tab:hover {
    color: #e0e0e0;
}

QLabel#sectionTitle {
    font-size: 10px;
    font-weight: 700;
    color: #cfb53b;
    text-transform: uppercase;
    letter-spacing: 1px;
    padding: 4px 0 6px 0;
    border-bottom: 1px solid #1f1f30;
}

QFrame#wheelTracker {
    border-top: 1px solid #333355;
    margin-top: 4px;
}

QLabel#wheelTitle {
    font-size: 11px;
    color: #888888;
    font-weight: bold;
}

QLabel#wheelRow {
    font-size: 11px;
    color: #aaaaaa;
}

QComboBox {
    background-color: #2a2a4a;
    color: #e0e0e0;
    border: 1px solid #444466;
    border-radius: 3px;
    padding: 2px 8px;
}

QComboBox::drop-down {
    border: none;
}

QComboBox QAbstractItemView {
    background-color: #2a2a4a;
    color: #e0e0e0;
    selection-background-color: #3a3a6a;
}

QPushButton {
    background-color: #2a2a4a;
    color: #e0e0e0;
    border: 1px solid #444466;
    border-radius: 3px;
    padding: 5px 12px;
    font-size: 12px;
}

QPushButton:hover {
    background-color: #3a3a6a;
}

QSlider::groove:horizontal {
    height: 4px;
    background: #333355;
    border-radius: 2px;
}

QSlider::handle:horizontal {
    width: 14px;
    margin: -5px 0;
    background: #cfb53b;
    border-radius: 7px;
}

QCheckBox {
    spacing: 6px;
    color: #ccccdd;
}

QCheckBox::indicator {
    width: 14px;
    height: 14px;
    border: 1px solid #555577;
    border-radius: 3px;
    background: #1a1a2e;
}

QCheckBox::indicator:checked {
    background: #cfb53b;
    border-color: #cfb53b;
}
"""

# ---------------------------------------------------------------------------
# Dark-theme stylesheet (applied to the whole overlay window)
# ---------------------------------------------------------------------------

OVERLAY_STYLESHEET = """
QWidget {
    background-color: #0f0f1c;
    color: #e0e0e0;
    font-family: "Segoe UI", "Arial", sans-serif;
    font-size: 12px;
}

QLabel#status {
    font-size: 12px;
    color: #888888;
    padding: 4px 0;
}

QProgressBar {
    border: none;
    background-color: #0b0b18;
    border-radius: 2px;
    max-height: 6px;
    min-height: 6px;
}
""" + _SHARED_WIDGET_STYLES

# ---------------------------------------------------------------------------
# Transparent mode stylesheet (frameless, translucent background)
# ---------------------------------------------------------------------------

TRANSPARENT_STYLESHEET = """
QWidget {
    background-color: rgba(15, 15, 28, 200);
    color: #e0e0e0;
    font-family: "Segoe UI", "Arial", sans-serif;
    font-size: 12px;
    border-radius: 8px;
}

QLabel#status {
    font-size: 12px;
    color: #888888;
    padding: 4px 0;
}

QProgressBar {
    border: none;
    background-color: rgba(10, 10, 25, 180);
    border-radius: 2px;
    max-height: 6px;
    min-height: 6px;
}

QScrollArea {
    background: transparent;
    border: none;
}
""" + _SHARED_WIDGET_STYLES
