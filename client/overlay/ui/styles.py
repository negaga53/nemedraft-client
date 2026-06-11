"""DEPRECATED shim — the design system moved to ``client.overlay.ui.theme``.

Kept only so not-yet-migrated surfaces keep compiling during the v0.6
redesign; every re-export resolves to the new tokens. Delete once the
per-surface migration finishes (grep gate: no imports of this module).
"""

from __future__ import annotations

from client.overlay.ui.theme import tokens as _t
from client.overlay.ui.theme.qss import build_stylesheet

# -- legacy palette names → new tokens ---------------------------------------

BG_PRIMARY = _t.L0_WINDOW_OPAQUE
BG_ELEVATED = _t.L1_PANEL
BG_CARD = _t.L2_CARD
BORDER_SUBTLE = _t.L1_STROKE
BORDER_SOFT = _t.L2_STROKE
ACCENT_GOLD = _t.ACCENT  # the interactive accent is cyan now; medals keep gold
TEXT_PRIMARY = _t.TEXT_PRIMARY
TEXT_SECONDARY = _t.TEXT_SECONDARY
TEXT_MUTED = _t.TEXT_MUTED
TEXT_DIM = _t.TEXT_FAINT

MTG_COLORS = _t.MTG_COLORS
COLOR_MULTI = _t.COLOR_MULTI
COLOR_COLORLESS = _t.COLOR_COLORLESS
_MANA_PIP_COLORS = _t.MANA_PIP_COLORS

SCORE_COLOR_HIGH = _t.SCORE_HIGH
SCORE_COLOR_MID = _t.SCORE_MID
SCORE_COLOR_LOW = _t.SCORE_LOW

mana_pips_html = _t.mana_pips_html
color_pips_html = _t.color_pips_html
short_type = _t.short_type
score_to_color = _t.score_to_color
medal_color = _t.medal_color


def card_row_bg(colors: list[str], is_top: bool = False) -> str:
    """Legacy row wash (inline-styled rows only; CardRow migrates to
    the ``tint`` property + QSS attribute selectors)."""
    del is_top  # the new look marks the top row with an accent stroke
    return _t.TINT_WASHES[_t.card_tint(colors)]


# -- generated stylesheets ------------------------------------------------------

OVERLAY_STYLESHEET = build_stylesheet(glass=False)
TRANSPARENT_STYLESHEET = build_stylesheet(glass=True)
