"""Design tokens — the single source of truth for the overlay's look.

Pure data plus formatting helpers. QSS generation lives in
``theme.qss``; custom-painted widgets (ScoreBar, ManaCurvePlot) read
colors through :func:`qcolor` / :func:`score_gradient` so a token change
restyles them too.
"""

from __future__ import annotations

import re

from PySide6.QtGui import QColor

# ---------------------------------------------------------------------------
# Glass elevation layers (fill, hairline stroke)
# ---------------------------------------------------------------------------

L0_WINDOW_GLASS = "rgba(10, 13, 20, 0.93)"
L0_WINDOW_OPAQUE = "#0a0d14"
L0_WELL = "#0b1018"                      # input/score-bar track wells

L1_PANEL = "rgba(17, 22, 33, 0.78)"      # status card, rail column, nav strip
L1_STROKE = "rgba(255, 255, 255, 0.06)"

L2_CARD = "rgba(26, 33, 48, 0.82)"       # rail cards, chips, buttons
L2_STROKE = "rgba(255, 255, 255, 0.09)"
L2_HOVER = "rgba(44, 54, 76, 0.92)"

L3_POPOVER = "rgba(31, 39, 56, 0.96)"    # previews, dropdown views
L3_STROKE = "rgba(255, 255, 255, 0.13)"

# Top sheen layered into glass panels (the portable "glass" trick —
# real backdrop blur is not available cross-platform in Qt).
SHEEN = (
    "qlineargradient(x1:0, y1:0, x2:0, y2:1,"
    " stop:0 rgba(255,255,255,0.05), stop:0.06 transparent)"
)

HOVER_WASH = "rgba(255, 255, 255, 0.06)"

# ---------------------------------------------------------------------------
# Accent + text
# ---------------------------------------------------------------------------

ACCENT = "#3BD2FF"                       # interactive accent (electric cyan)
ACCENT_DIM = "rgba(59, 210, 255, 0.45)"
ACCENT_WASH = "rgba(59, 210, 255, 0.10)"
ACCENT_TEXT_ON = "#06121c"               # text on an accent-filled chip

TEXT_PRIMARY = "#EAF0F8"
TEXT_SECONDARY = "#A9B4C6"
TEXT_MUTED = "#6C7689"
TEXT_FAINT = "#48536B"

# ---------------------------------------------------------------------------
# Semantic (status) + score scale + medals
# ---------------------------------------------------------------------------

OK = "#3DDC84"
WARN = "#FFC555"
ERR = "#FF5D5D"

SCORE_HIGH = "#3DDC84"
SCORE_HIGH_DEEP = "#1F7A4C"
SCORE_MID = "#FFC555"
SCORE_MID_DEEP = "#8F6A1E"
SCORE_LOW = "#FF5D5D"
SCORE_LOW_DEEP = "#7A2C28"

# Medals are domain flavor (GIH% ranks), not the UI accent.
MEDAL_GOLD = "#E8C268"
MEDAL_SILVER = "#E9EEF5"
MEDAL_BRONZE = "#D08A4E"

# ---------------------------------------------------------------------------
# MTG color identity
# ---------------------------------------------------------------------------

MTG_COLORS = {
    "W": "#F9FAF4",
    "U": "#0E68AB",
    "B": "#150B00",
    "R": "#D3202A",
    "G": "#00733E",
}

COLOR_MULTI = "#E8C268"
COLOR_COLORLESS = "#A8A8A8"

# Mana pip display colours (brighter for visibility on dark bg).
MANA_PIP_COLORS: dict[str, str] = {
    "W": "#F5E6A3",
    "U": "#3B8FD4",
    "B": "#9B8E82",
    "R": "#E74C3C",
    "G": "#2ECC71",
}

# Row tints: colored left stroke + low-alpha wash per identity.
TINT_STROKES: dict[str, str] = {
    "W": "#D9CD9A",
    "U": "#3B8FD4",
    "B": "#8C8278",
    "R": "#E74C3C",
    "G": "#2ECC71",
    "M": MEDAL_GOLD,
    "C": "#8A93A6",
}
TINT_WASHES: dict[str, str] = {
    "W": "rgba(245, 230, 163, 0.08)",
    "U": "rgba(59, 143, 212, 0.08)",
    "B": "rgba(155, 142, 130, 0.07)",
    "R": "rgba(231, 76, 60, 0.08)",
    "G": "rgba(46, 204, 113, 0.08)",
    "M": "rgba(232, 194, 104, 0.08)",
    "C": "rgba(138, 147, 166, 0.06)",
}

# ---------------------------------------------------------------------------
# Spacing / radius / typography scales
# ---------------------------------------------------------------------------

SPACE = (2, 4, 6, 8, 12, 16)

RADIUS_ROW = 3
RADIUS_CHIP = 4
RADIUS_CARD = 6
RADIUS_PANEL = 10
RADIUS_WINDOW = 12   # glass mode only

FONT_STACK = '"Inter", "Segoe UI", sans-serif'
FONT_SIZE_MICRO = 11
FONT_SIZE_SMALL = 12
FONT_SIZE_DENSE = 13
FONT_SIZE_BODY = 14
FONT_SIZE_TITLE = 15
FONT_SIZE_HEADLINE = 17
FONT_SIZE_PIP = 22      # colour-commitment pip counts — sized to the 22px mana icons
FONT_SIZE_WORDMARK = 23

_PIP_RE = re.compile(r"\{(.*?)\}")


# ---------------------------------------------------------------------------
# Helpers (token-coupled formatting)
# ---------------------------------------------------------------------------

def card_tint(colors: list[str]) -> str:
    """Map a card's color identity to a tint key: W/U/B/R/G, M(ulti), C(olorless)."""
    if not colors:
        return "C"
    if len(colors) > 1:
        return "M"
    return colors[0] if colors[0] in MTG_COLORS else "C"


def mana_pips_html(mana_cost: str) -> str:
    """Convert Scryfall mana cost like ``{1}{W}{U}`` to HTML with coloured dots."""
    if not mana_cost:
        return ""
    parts: list[str] = []
    for m in _PIP_RE.finditer(mana_cost):
        pip = m.group(1).upper()
        if pip in MANA_PIP_COLORS:
            parts.append(f'<font color="{MANA_PIP_COLORS[pip]}">●</font>')
        elif pip == "X":
            parts.append(f'<font color="{COLOR_COLORLESS}">X</font>')
        elif "/" in pip:
            options = pip.split("/")
            cols = [MANA_PIP_COLORS.get(o.strip(), COLOR_COLORLESS) for o in options]
            parts.append(f'<font color="{cols[0]}">●</font>')
        else:
            parts.append(f'<font color="{COLOR_COLORLESS}">{pip}</font>')
    return "".join(parts)


def color_pips_html(colors: list[str]) -> str:
    """Convert a list of WUBRG colour letters to coloured dot HTML."""
    if not colors:
        return f'<font color="{COLOR_COLORLESS}">●</font>'
    return "".join(
        f'<font color="{MANA_PIP_COLORS.get(c, COLOR_COLORLESS)}">●</font>'
        for c in colors
    )


def short_type(type_line: str) -> str:
    """Abbreviate a Scryfall type line: ``"Creature — Elf"`` → ``"Cre"``."""
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


def score_to_color(score: float, max_score: float) -> str:
    """Interpolate a hex colour over the score scale based on relative score."""
    if max_score <= 0:
        return SCORE_MID
    t = score / max_score
    if t > 0.7:
        return SCORE_HIGH
    if t > 0.35:
        return SCORE_MID
    return SCORE_LOW


def medal_color(rank: int) -> str | None:
    """Return medal hex for GIH% rank 1/2/3, else None."""
    return {1: MEDAL_GOLD, 2: MEDAL_SILVER, 3: MEDAL_BRONZE}.get(rank)


def qcolor(token: str) -> QColor:
    """Build a QColor from a token (hex or ``rgba(r, g, b, a)`` string)."""
    if token.startswith("rgba"):
        parts = token[token.index("(") + 1: token.rindex(")")].split(",")
        r, g, b = (int(p.strip()) for p in parts[:3])
        alpha = float(parts[3].strip())
        a = int(alpha * 255) if alpha <= 1 else int(alpha)
        return QColor(r, g, b, a)
    return QColor(token)


def score_gradient(fraction: float) -> tuple[QColor, QColor]:
    """(deep, bright) gradient stops for a score fill at *fraction* (0..1)."""
    if fraction > 0.7:
        return qcolor(SCORE_HIGH_DEEP), qcolor(SCORE_HIGH)
    if fraction > 0.35:
        return qcolor(SCORE_MID_DEEP), qcolor(SCORE_MID)
    return qcolor(SCORE_LOW_DEEP), qcolor(SCORE_LOW)
