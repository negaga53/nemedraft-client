"""Stylesheet generation — one builder for both window modes.

``build_stylesheet(glass)`` renders the entire application stylesheet
from tokens. The only ``glass`` delta is the root background (translucent
rgba + rounded corners vs opaque hex) — everything else is shared, which
is what collapsed the old OVERLAY/TRANSPARENT stylesheet pair.

Widget variants are addressed by objectName and enumerable dynamic
properties (``tint``, ``medal``, ``status``, ``severity``, ``picked``,
``recommended``, …) instead of per-widget setStyleSheet calls.
"""

from __future__ import annotations

from client.overlay.ui.theme import tokens as t


def _tint_rules() -> str:
    """Per-color row rules: colored left stroke + low-alpha wash."""
    rules: list[str] = []
    for key in ("W", "U", "B", "R", "G", "M", "C"):
        stroke = t.TINT_STROKES[key]
        wash = t.TINT_WASHES[key]
        rules.append(f"""
CardRow[tint="{key}"] {{
    background-color: {wash};
    border-left: 2px solid {stroke};
}}""")
    return "\n".join(rules)


def _deck_tint_rules() -> str:
    """Deck-row variant of the per-color tint rules."""
    rules: list[str] = []
    for key in ("W", "U", "B", "R", "G", "M", "C"):
        rules.append(f"""
_DeckCardRow[tint="{key}"] {{
    background-color: {t.TINT_WASHES[key]};
    border-left: 2px solid {t.TINT_STROKES[key]};
}}""")
    return "\n".join(rules)


def build_stylesheet(glass: bool) -> str:
    """Render the full application stylesheet for the given window mode."""
    if glass:
        root = f"""
QWidget {{
    background-color: {t.L0_WINDOW_GLASS};
    color: {t.TEXT_PRIMARY};
    font-family: {t.FONT_STACK};
    font-size: {t.FONT_SIZE_BODY}px;
    border-radius: {t.RADIUS_WINDOW}px;
}}
QScrollArea {{
    background: transparent;
    border: none;
    border-radius: 0;
}}
"""
    else:
        root = f"""
QWidget {{
    background-color: {t.L0_WINDOW_OPAQUE};
    color: {t.TEXT_PRIMARY};
    font-family: {t.FONT_STACK};
    font-size: {t.FONT_SIZE_BODY}px;
}}
QScrollArea {{
    background: transparent;
    border: none;
}}
"""

    return root + f"""
/* ---- window chrome -------------------------------------------------- */

QWidget#dragHeader {{
    background-color: {t.L1_PANEL};
    border: none;
    border-bottom: 1px solid {t.L1_STROKE};
    border-radius: 0;
}}

QLabel#brandLabel {{
    color: {t.TEXT_PRIMARY};
    background: transparent;
    border: none;
    font-size: {t.FONT_SIZE_DENSE}px;
    font-weight: 600;
    letter-spacing: 0.12em;
}}

QLabel#brandTick {{
    color: {t.ACCENT};
    background: transparent;
    border: none;
    font-size: {t.FONT_SIZE_DENSE}px;
    font-weight: 700;
}}

QLabel#versionLabel {{
    color: {t.TEXT_FAINT};
    background: transparent;
    border: none;
    font-size: {t.FONT_SIZE_SMALL}px;
}}

QPushButton#windowBtn {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: {t.RADIUS_CHIP}px;
    color: {t.TEXT_SECONDARY};
    font-size: 15px;
    font-weight: 700;
    padding: 0;
}}
QPushButton#windowBtn:hover {{
    background: {t.HOVER_WASH};
    color: {t.TEXT_PRIMARY};
}}

QPushButton#windowBtnClose {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: {t.RADIUS_CHIP}px;
    color: {t.TEXT_SECONDARY};
    font-size: 12px;
    font-weight: 700;
    padding: 0;
}}
QPushButton#windowBtnClose:hover {{
    background: rgba(255, 93, 93, 0.85);
    color: #ffffff;
}}

/* Compact-view toggle — accent-tinted so it's discoverable during a draft. */
QPushButton#compactToggle {{
    background: {t.ACCENT_WASH};
    border: 1px solid {t.ACCENT_DIM};
    border-radius: {t.RADIUS_CHIP}px;
    color: {t.ACCENT};
    font-size: 14px;
    font-weight: 700;
    padding: 0;
}}
QPushButton#compactToggle:hover {{
    background: {t.ACCENT};
    color: {t.ACCENT_TEXT_ON};
}}

QLabel#status {{
    font-size: {t.FONT_SIZE_BODY}px;
    color: {t.TEXT_MUTED};
    background: transparent;
    border: none;
    padding: 4px 0;
}}

/* ---- tab bar --------------------------------------------------------- */

QTabWidget::pane {{
    border: none;
    padding: 0;
}}

QTabBar {{
    background: transparent;
}}

QTabBar::tab {{
    background: transparent;
    padding: 7px 14px;
    font-size: {t.FONT_SIZE_SMALL}px;
    font-weight: 600;
    border: none;
    border-bottom: 2px solid transparent;
    color: {t.TEXT_MUTED};
    text-transform: uppercase;
    letter-spacing: 0.1em;
    min-width: 60px;
}}

QTabBar::tab:selected {{
    color: {t.ACCENT};
    border-bottom: 2px solid {t.ACCENT};
}}

QTabBar::tab:hover {{
    color: {t.TEXT_PRIMARY};
}}

/* ---- section titles --------------------------------------------------- */

QLabel#sectionTitle {{
    font-size: {t.FONT_SIZE_MICRO}px;
    font-weight: 700;
    color: {t.TEXT_SECONDARY};
    background: transparent;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    padding: 4px 0 6px 0;
    border: none;
    border-bottom: 1px solid {t.L1_STROKE};
}}

/* ---- base controls ----------------------------------------------------- */

QComboBox {{
    background-color: {t.L2_CARD};
    color: {t.TEXT_PRIMARY};
    border: 1px solid {t.L2_STROKE};
    border-radius: {t.RADIUS_CHIP}px;
    padding: 2px 8px;
}}

QComboBox:hover {{
    background-color: {t.L2_HOVER};
}}

QComboBox::drop-down {{
    border: none;
}}

QComboBox QAbstractItemView {{
    background-color: {t.L3_POPOVER};
    color: {t.TEXT_PRIMARY};
    border: 1px solid {t.L3_STROKE};
    border-radius: {t.RADIUS_CHIP}px;
    selection-background-color: {t.ACCENT_WASH};
    selection-color: {t.ACCENT};
}}

QPushButton {{
    background-color: {t.L2_CARD};
    color: {t.TEXT_PRIMARY};
    border: 1px solid {t.L2_STROKE};
    border-radius: {t.RADIUS_CHIP}px;
    padding: 5px 12px;
    font-size: {t.FONT_SIZE_BODY}px;
}}

QPushButton:hover {{
    background-color: {t.L2_HOVER};
}}

QPushButton:disabled {{
    color: {t.TEXT_FAINT};
    border-color: {t.L1_STROKE};
}}

QPushButton#accentBtn {{
    border: 1px solid {t.ACCENT_DIM};
    color: {t.ACCENT};
}}
QPushButton#accentBtn:hover {{
    background-color: {t.ACCENT_WASH};
}}

QPushButton#ghostBtn {{
    background: transparent;
    border: 1px solid transparent;
    color: {t.TEXT_SECONDARY};
}}
QPushButton#ghostBtn:hover {{
    background: {t.HOVER_WASH};
    color: {t.TEXT_PRIMARY};
}}

QSlider::groove:horizontal {{
    height: 4px;
    background: {t.L0_WELL};
    border-radius: 2px;
}}

QSlider::handle:horizontal {{
    width: 14px;
    margin: -5px 0;
    background: {t.ACCENT};
    border-radius: 7px;
}}

QCheckBox {{
    spacing: 6px;
    color: {t.TEXT_SECONDARY};
    background: transparent;
}}

QCheckBox::indicator {{
    width: 14px;
    height: 14px;
    border: 1px solid {t.L3_STROKE};
    border-radius: {t.RADIUS_ROW}px;
    background: {t.L0_WELL};
}}

QCheckBox::indicator:checked {{
    background: {t.ACCENT};
    border-color: {t.ACCENT};
}}

QProgressBar {{
    border: none;
    background-color: {t.L0_WELL};
    border-radius: 2px;
    max-height: 6px;
    min-height: 6px;
}}

QProgressBar::chunk {{
    background-color: {t.ACCENT};
    border-radius: 2px;
}}

QToolTip {{
    background-color: {t.L3_POPOVER};
    color: {t.TEXT_PRIMARY};
    border: 1px solid {t.L3_STROKE};
    padding: 4px 6px;
}}

QScrollBar:vertical {{
    background: transparent;
    width: 6px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: rgba(255, 255, 255, 0.12);
    border-radius: 3px;
    min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{
    background: rgba(255, 255, 255, 0.22);
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
}}

/* ---- context pill ------------------------------------------------------- */

QLabel#contextPill {{
    background: {t.L2_CARD};
    color: {t.TEXT_SECONDARY};
    border: 1px solid {t.ACCENT_DIM};
    font-weight: 600;
    padding: 2px 10px;
    border-radius: {t.RADIUS_PANEL}px;
    font-size: {t.FONT_SIZE_SMALL}px;
    letter-spacing: 0.05em;
}}

/* ---- toasts ---------------------------------------------------------------- */

QFrame#toast {{
    background: {t.L3_POPOVER};
    border: 1px solid {t.L3_STROKE};
    border-radius: {t.RADIUS_CHIP}px;
}}
QFrame#toast[severity="warning"] {{ border-color: {t.WARN}; }}
QFrame#toast[severity="error"] {{ border-color: {t.ERR}; }}
QFrame#toast QLabel#toastLabel {{
    color: {t.TEXT_PRIMARY};
    font-size: {t.FONT_SIZE_DENSE}px;
    background: transparent;
    border: none;
}}
QFrame#toast QPushButton#toastClose {{
    background: transparent;
    border: none;
    color: {t.TEXT_MUTED};
    font-size: {t.FONT_SIZE_SMALL}px;
    font-weight: 700;
    padding: 0;
}}
QFrame#toast QPushButton#toastClose:hover {{ color: {t.TEXT_PRIMARY}; }}

/* ---- pack card rows (properties set by CardRow.set_data) ------------------ */

CardRow {{
    background-color: transparent;
    border: none;
    border-left: 2px solid transparent;
    border-radius: {t.RADIUS_ROW}px;
}}
{_tint_rules()}
CardRow:hover {{
    background-color: {t.HOVER_WASH};
}}
CardRow[dimmed="true"] {{
    background-color: rgba(20, 26, 38, 0.55);
    border-left: 2px solid transparent;
}}
CardRow[top="true"] {{
    border-left: 2px solid {t.ACCENT};
}}
CardRow[picked="true"] {{
    background-color: {t.ACCENT_WASH};
    border: 1px solid {t.ACCENT_DIM};
    border-left: 3px solid {t.ACCENT};
}}
CardRow[recommended="true"] {{
    background-color: {t.ACCENT_WASH};
    border-left: 3px solid {t.ACCENT};
}}

/* ---- pack tab chrome ----------------------------------------------------------- */

QProgressBar#predictionLoading {{
    max-height: 3px;
    min-height: 3px;
    margin: 1px 8px 0 8px;
    border-radius: 1px;
}}

QPushButton#navBtn {{
    background: {t.L2_CARD};
    color: {t.TEXT_PRIMARY};
    border: 1px solid {t.L2_STROKE};
    border-radius: {t.RADIUS_CHIP}px;
    padding: 2px 10px;
    font-size: 18px;
    font-weight: bold;
}}
QPushButton#navBtn:hover {{
    background: {t.ACCENT_WASH};
    border-color: {t.ACCENT_DIM};
    color: {t.ACCENT};
}}
QPushButton#navBtn:disabled {{
    background: transparent;
    color: {t.TEXT_FAINT};
    border-color: {t.L1_STROKE};
}}

QLabel#navLabel {{
    color: {t.TEXT_SECONDARY};
    font-size: 15px;
    font-weight: 600;
    letter-spacing: 0.06em;
    background: transparent;
    border: none;
}}

QWidget#navBar {{
    background: {t.L1_PANEL};
    border: none;
    border-top: 1px solid {t.L1_STROKE};
}}

QLabel#takenSeparator {{
    color: {t.TEXT_FAINT};
    font-size: {t.FONT_SIZE_SMALL}px;
    padding: 2px 0;
    background: transparent;
    border: none;
}}

/* ---- pack row internals ------------------------------------------------------ */

QLabel#rowRank {{
    color: {t.TEXT_SECONDARY};
    font-weight: 700;
    font-size: 15px;
    background: transparent;
    border: none;
}}
QLabel#rowRank[top="true"] {{ color: {t.ACCENT}; }}

QLabel#rowArt {{
    background: #141a26;
    border: none;
    border-radius: 2px;
}}

QLabel#rowName {{
    color: {t.TEXT_SECONDARY};
    font-size: {t.FONT_SIZE_BODY}px;
    background: transparent;
    border: none;
}}
QLabel#rowName[top="true"] {{
    color: {t.TEXT_PRIMARY};
    font-weight: 500;
}}
QLabel#rowName[dimmed="true"] {{
    color: {t.TEXT_MUTED};
    font-style: italic;
}}

QLabel#rowStat {{
    color: {t.TEXT_SECONDARY};
    font-size: {t.FONT_SIZE_BODY}px;
    font-weight: 600;
    background: transparent;
    border: none;
}}
QLabel#rowStat[medal="1"] {{ color: {t.MEDAL_GOLD}; font-weight: 700; }}
QLabel#rowStat[medal="2"] {{ color: {t.MEDAL_SILVER}; font-weight: 700; }}
QLabel#rowStat[medal="3"] {{ color: {t.MEDAL_BRONZE}; font-weight: 700; }}
QLabel#rowStat[empty="true"] {{ color: {t.TEXT_MUTED}; font-weight: 400; }}
QLabel#rowStat[skeleton="true"] {{
    background: rgba(120, 140, 180, 0.25);
    border-radius: 3px;
    min-height: 8px;
    max-height: 8px;
    margin: 6px 4px;
}}

QLabel#manaPipFallback {{
    color: {t.TEXT_SECONDARY};
    font-size: {t.FONT_SIZE_SMALL}px;
    background: transparent;
    border: none;
}}

QFrame#columnHeader {{
    background: transparent;
    border: none;
    border-bottom: 1px solid {t.L1_STROKE};
}}
QFrame#columnHeader QLabel {{
    color: {t.TEXT_MUTED};
    font-size: {t.FONT_SIZE_MICRO}px;
    font-weight: 700;
    letter-spacing: 0.06em;
    background: transparent;
    border: none;
}}

/* ---- deck rail -------------------------------------------------------------- */

QFrame#railCard {{
    background: {t.L2_CARD};
    border: 1px solid {t.L1_STROKE};
    border-radius: {t.RADIUS_CARD}px;
}}

QFrame#archetypeCard {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 {t.ACCENT_WASH}, stop:1 {t.L2_CARD});
    border: 1px solid {t.ACCENT_DIM};
    border-radius: {t.RADIUS_CARD}px;
}}

QLabel#archetypeName {{
    color: {t.TEXT_PRIMARY};
    font-weight: 600;
    font-size: {t.FONT_SIZE_TITLE}px;
    background: transparent;
    border: none;
}}

QLabel#railTitle {{
    color: {t.TEXT_MUTED};
    font-size: {t.FONT_SIZE_MICRO}px;
    letter-spacing: 0.08em;
    font-weight: 700;
    background: transparent;
    border: none;
}}

QLabel#railDetail {{
    color: {t.TEXT_MUTED};
    font-size: {t.FONT_SIZE_MICRO}px;
    background: transparent;
    border: none;
}}

QLabel#pipCount {{
    color: {t.TEXT_FAINT};
    font-size: {t.FONT_SIZE_PIP}px;
    background: transparent;
    border: none;
}}
QLabel#pipCount[top="true"] {{
    color: {t.TEXT_PRIMARY};
    font-weight: 700;
}}

QLabel#laneState {{
    font-size: {t.FONT_SIZE_SMALL}px;
    background: transparent;
    border: none;
    color: {t.TEXT_MUTED};
}}
QLabel#laneState[lane="open"] {{ color: {t.OK}; }}
QLabel#laneState[lane="closing"] {{ color: {t.WARN}; }}
QLabel#laneState[lane="closed"] {{ color: {t.TEXT_MUTED}; }}

/* ---- home tab ----------------------------------------------------------------- */

QLabel#homeWordmark {{
    color: {t.TEXT_PRIMARY};
    font-size: {t.FONT_SIZE_WORDMARK}px;
    font-weight: 700;
    letter-spacing: 0.06em;
    padding: 14px 0 2px 0;
    background: transparent;
    border: none;
}}

QLabel#homeSubtitle {{
    font-size: {t.FONT_SIZE_DENSE}px;
    color: {t.TEXT_MUTED};
    padding: 0 0 14px 0;
    background: transparent;
    border: none;
}}

QFrame#statusCard {{
    background: {t.L1_PANEL};
    border: 1px solid {t.L1_STROKE};
    border-radius: {t.RADIUS_PANEL}px;
}}

QLabel#statusDot {{
    font-size: 14px;
    background: transparent;
    border: none;
    color: {t.ERR};
}}
QLabel#statusDot[status="ok"] {{ color: {t.OK}; }}
QLabel#statusDot[status="warn"] {{ color: {t.WARN}; }}
QLabel#statusDot[status="err"] {{ color: {t.ERR}; }}

QLabel#statusRowLabel {{
    font-size: {t.FONT_SIZE_TITLE}px;
    color: {t.TEXT_SECONDARY};
    background: transparent;
    border: none;
}}

QLabel#statusRowDetail {{
    font-size: {t.FONT_SIZE_BODY}px;
    color: {t.TEXT_MUTED};
    background: transparent;
    border: none;
}}

QLabel#loginPrompt {{
    font-size: {t.FONT_SIZE_TITLE}px;
    color: {t.TEXT_SECONDARY};
    padding: 4px 0;
    background: transparent;
    border: none;
}}

QPushButton#oauthGoogle, QPushButton#oauthMicrosoft, QPushButton#oauthDiscord {{
    font-size: {t.FONT_SIZE_TITLE}px;
    padding: 8px 16px;
    min-height: 20px;
    border-radius: {t.RADIUS_CHIP}px;
    border: 1px solid {t.L2_STROKE};
    color: #ffffff;
}}
QPushButton#oauthGoogle {{ background: #4285F4; }}
QPushButton#oauthGoogle:hover {{ background: #3367D6; }}
QPushButton#oauthMicrosoft {{ background: #2F2F2F; }}
QPushButton#oauthMicrosoft:hover {{ background: #444444; }}
QPushButton#oauthDiscord {{ background: #5865F2; }}
QPushButton#oauthDiscord:hover {{ background: #4752C4; }}

QLabel#loginError {{
    font-size: {t.FONT_SIZE_DENSE}px;
    color: {t.ERR};
    background: transparent;
    border: none;
}}

QLabel#loggedInLabel {{
    font-size: {t.FONT_SIZE_BODY}px;
    color: {t.OK};
    background: transparent;
    border: none;
}}

QLabel#vipBadge {{
    font-size: {t.FONT_SIZE_BODY}px;
    font-weight: bold;
    color: {t.MEDAL_GOLD};
    padding: 2px 0;
    background: transparent;
    border: none;
}}

QPushButton#logoutBtn {{
    font-size: {t.FONT_SIZE_DENSE}px;
    padding: 4px 12px;
    border-radius: {t.RADIUS_ROW}px;
    border: 1px solid {t.L3_STROKE};
    background: transparent;
    color: {t.TEXT_MUTED};
}}
QPushButton#logoutBtn:hover {{
    color: {t.ERR};
    border-color: {t.ERR};
}}

QLabel#homeHint {{
    font-size: {t.FONT_SIZE_DENSE}px;
    color: {t.TEXT_FAINT};
    padding: 8px 0;
    background: transparent;
    border: none;
}}

/* ---- summary tab ----------------------------------------------------------------- */

QLabel#summaryHeader {{
    color: {t.TEXT_PRIMARY};
    font-size: {t.FONT_SIZE_HEADLINE}px;
    font-weight: 600;
    background: transparent;
    border: none;
}}

QLabel#summaryStats {{
    color: {t.TEXT_SECONDARY};
    font-size: {t.FONT_SIZE_BODY}px;
    background: transparent;
    border: none;
}}

QLabel#summaryBuild {{
    color: {t.TEXT_SECONDARY};
    font-size: {t.FONT_SIZE_BODY}px;
    background: transparent;
    border: none;
}}

QLabel#summaryRecapRow {{
    color: {t.TEXT_SECONDARY};
    font-size: {t.FONT_SIZE_BODY}px;
    background: transparent;
    border: none;
}}

QPushButton#summaryCopyDeck {{
    border: 1px solid {t.ACCENT_DIM};
    color: {t.ACCENT};
}}
QPushButton#summaryCopyDeck:hover {{
    background-color: {t.ACCENT_WASH};
}}
QPushButton#summaryCopyDeck:disabled {{
    border-color: {t.L1_STROKE};
    color: {t.TEXT_FAINT};
}}

/* ---- settings tab --------------------------------------------------------------- */

QLabel#settingsNote {{
    color: {t.TEXT_MUTED};
    font-size: {t.FONT_SIZE_SMALL}px;
    padding: 0 24px;
    background: transparent;
    border: none;
}}

QLabel#settingsCaption {{
    color: {t.TEXT_SECONDARY};
    font-size: {t.FONT_SIZE_DENSE}px;
    background: transparent;
    border: none;
}}

/* ---- deck tab ----------------------------------------------------------------- */

QLabel#deckArchetypeName {{
    color: {t.TEXT_PRIMARY};
    font-size: {t.FONT_SIZE_HEADLINE}px;
    font-weight: 600;
    background: transparent;
    border: none;
}}

QLabel#deckStatsLine {{
    color: {t.TEXT_SECONDARY};
    font-size: {t.FONT_SIZE_DENSE}px;
    background: transparent;
    border: none;
}}

_DeckCardRow {{
    background-color: transparent;
    border: none;
    border-left: 2px solid transparent;
    border-radius: 2px;
}}
{_deck_tint_rules()}
_DeckCardRow:hover {{
    background-color: {t.HOVER_WASH};
}}
_DeckCardRow[dimmed="true"] {{
    background-color: rgba(20, 26, 38, 0.55);
    border-left: 2px solid transparent;
}}

QLabel#deckCount {{
    font-size: {t.FONT_SIZE_BODY}px;
    color: {t.TEXT_SECONDARY};
    background: transparent;
    border: none;
}}
QLabel#deckCount[dimmed="true"] {{ color: {t.TEXT_FAINT}; }}

QLabel#deckName {{
    font-size: {t.FONT_SIZE_BODY}px;
    color: {t.TEXT_PRIMARY};
    background: transparent;
    border: none;
}}
QLabel#deckName[dimmed="true"] {{
    color: {t.TEXT_MUTED};
    font-style: italic;
}}

QLabel#deckType {{
    color: {t.TEXT_MUTED};
    font-size: {t.FONT_SIZE_DENSE}px;
    background: transparent;
    border: none;
}}
QLabel#deckType[dimmed="true"] {{ color: {t.TEXT_FAINT}; }}

QPushButton#sideboardToggle {{
    background: transparent;
    border: none;
    border-top: 1px solid {t.L1_STROKE};
    color: {t.TEXT_SECONDARY};
    font-size: {t.FONT_SIZE_DENSE}px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-align: left;
    padding: 8px 6px;
}}
QPushButton#sideboardToggle:hover {{
    color: {t.TEXT_PRIMARY};
}}

/* ---- misc shared bits -------------------------------------------------------- */

QFrame#wheelTracker {{
    background: transparent;
    border: none;
    border-top: 1px solid {t.L1_STROKE};
    margin-top: 4px;
}}

QLabel#wheelTitle {{
    font-size: {t.FONT_SIZE_DENSE}px;
    color: {t.TEXT_MUTED};
    background: transparent;
    border: none;
    font-weight: bold;
}}

QLabel#wheelRow {{
    font-size: {t.FONT_SIZE_DENSE}px;
    color: {t.TEXT_SECONDARY};
    background: transparent;
    border: none;
}}
"""
