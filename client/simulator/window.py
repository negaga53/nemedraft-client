"""MTG Arena–style draft simulator window."""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

import httpx
from PySide6.QtCore import QMutex, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from client.simulator.engine import DraftCard, DraftEngine

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Layout constants
# ---------------------------------------------------------------------------

GRID_COLS = 5
GRID_ROWS = 3
CARD_SPACING = 8
DECK_PANEL_WIDTH = 250
HEADER_HEIGHT = 52
BOTTOM_HEIGHT = 58
TIMER_SECONDS = 75

# Card aspect ratio (standard MTG: 63 mm × 88 mm ≈ 5:7).
CARD_RATIO = 63 / 88  # width / height

# ---------------------------------------------------------------------------
# Art cache directory for full-size card images
# ---------------------------------------------------------------------------

from client.overlay.env import _project_root

SIM_ART_CACHE = _project_root() / "data" / "card_art_cache" / "normal"

# ---------------------------------------------------------------------------
# Color palette
# ---------------------------------------------------------------------------

BG_DARK = "#0d0d0d"
BG_HEADER = "#101018"
BG_PANEL = "#15151f"
BG_BOTTOM = "#101018"
BG_CARD_PLACEHOLDER = "#22222e"
GOLD = "#cfb53b"
ORANGE = "#d43f00"
TEXT_PRIMARY = "#e8e8e8"
TEXT_SECONDARY = "#777777"
TEXT_DIM = "#555555"

_MANA_BG: dict[str, str] = {
    "W": "#c8b870",
    "U": "#1a5fa0",
    "B": "#38352e",
    "R": "#b33030",
    "G": "#1e6e3c",
}

# ---------------------------------------------------------------------------
# Stylesheet
# ---------------------------------------------------------------------------

SIMULATOR_STYLESHEET = f"""
QMainWindow {{
    background-color: {BG_DARK};
}}
QWidget {{
    color: {TEXT_PRIMARY};
    font-family: "Segoe UI", "Arial", sans-serif;
}}
QFrame#header {{
    background-color: {BG_HEADER};
    border-bottom: 1px solid #2a2a3a;
}}
QLabel#headerSet {{
    font-size: 18px;
    font-weight: bold;
    color: {GOLD};
}}
QLabel#headerInfo {{
    font-size: 15px;
    color: {TEXT_PRIMARY};
}}
QLabel#headerPickInfo {{
    font-size: 14px;
    color: {GOLD};
    font-weight: bold;
}}
QFrame#deckPanel {{
    background-color: {BG_PANEL};
    border-left: 1px solid #2a2a3a;
}}
QLabel#deckHeader {{
    font-size: 14px;
    font-weight: bold;
    color: {TEXT_PRIMARY};
    padding: 6px;
}}
QLabel#deckCount {{
    font-size: 12px;
    color: {TEXT_SECONDARY};
    padding: 0 6px;
}}
QLabel#deckCard {{
    font-size: 11px;
    color: #ccccdd;
    padding: 1px 8px;
}}
QLabel#sideHeader {{
    font-size: 13px;
    font-weight: bold;
    color: {TEXT_PRIMARY};
    padding: 6px;
    border-top: 1px solid #2a2a3a;
}}
QLabel#sideCount {{
    font-size: 12px;
    color: {TEXT_SECONDARY};
    padding: 0 6px;
}}
QFrame#bottomBar {{
    background-color: {BG_BOTTOM};
    border-top: 1px solid #2a2a3a;
}}
QLabel#timerLabel {{
    font-size: 16px;
    font-weight: bold;
    color: {TEXT_PRIMARY};
    min-width: 50px;
}}
QProgressBar#timerBar {{
    border: none;
    background: #222;
    border-radius: 4px;
    max-height: 10px;
    min-height: 10px;
}}
QProgressBar#timerBar::chunk {{
    background: qlineargradient(x1:0, y1:0.5, x2:1, y2:0.5,
        stop:0 #d43f00, stop:1 #ff6600);
    border-radius: 4px;
}}
QPushButton#confirmBtn {{
    background-color: {ORANGE};
    color: white;
    font-size: 16px;
    font-weight: bold;
    border: none;
    border-radius: 8px;
    padding: 8px 28px;
    min-width: 170px;
    min-height: 38px;
}}
QPushButton#confirmBtn:hover {{
    background-color: #e04f10;
}}
QPushButton#confirmBtn:disabled {{
    background-color: #444;
    color: #888;
}}
QLabel#draftComplete {{
    font-size: 24px;
    font-weight: bold;
    color: {GOLD};
}}
"""


# ============================================================================
# CardWidget — a single clickable card tile
# ============================================================================

class CardWidget(QFrame):
    """Clickable card tile showing the card image (or a coloured placeholder)."""

    clicked = Signal(str)  # card name

    _BORDER_NORMAL = "border: 2px solid #2a2a3a; border-radius: 5px;"
    _BORDER_HOVER = "border: 2px solid #666; border-radius: 5px;"
    _BORDER_SELECTED = "border: 3px solid #cfb53b; border-radius: 5px;"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._card: DraftCard | None = None
        self._selected = False
        self._pixmap: QPixmap | None = None

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(self._BORDER_NORMAL)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._image = QLabel()
        self._image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image.setWordWrap(True)
        self._image.setStyleSheet(f"background: {BG_CARD_PLACEHOLDER}; border: none;")
        layout.addWidget(self._image)

        # "PICK" banner (hidden by default).
        self._pick_label = QLabel("PICK")
        self._pick_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._pick_label.setStyleSheet(
            "background: rgba(255,255,255,210); color: #111; "
            "font-weight: bold; font-size: 12px; padding: 2px; border: none;"
        )
        self._pick_label.setFixedHeight(22)
        self._pick_label.setVisible(False)
        layout.insertWidget(0, self._pick_label)

    # -- public API ----------------------------------------------------------

    @property
    def card_name(self) -> str | None:
        return self._card.name if self._card else None

    def set_card(self, card: DraftCard) -> None:
        self._card = card
        self._selected = False
        self._pixmap = None
        self._pick_label.setVisible(False)
        self._show_placeholder()
        self._update_border()
        self.setVisible(True)

    def set_image(self, pixmap: QPixmap) -> None:
        self._pixmap = pixmap
        scaled = pixmap.scaled(
            self._image.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._image.setPixmap(scaled)
        self._image.setText("")
        self._image.setStyleSheet("border: none;")

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self._pick_label.setVisible(selected)
        self._update_border()

    def clear_card(self) -> None:
        self._card = None
        self._pixmap = None
        self._pick_label.setVisible(False)
        self._image.clear()
        self._image.setStyleSheet(f"background: {BG_CARD_PLACEHOLDER}; border: none;")
        self.setVisible(False)

    # -- events --------------------------------------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if self._card and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._card.name)

    def enterEvent(self, event) -> None:  # noqa: N802
        if not self._selected and self._card:
            self.setStyleSheet(self._BORDER_HOVER)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._update_border()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._pixmap:
            self.set_image(self._pixmap)

    # -- internals -----------------------------------------------------------

    def _show_placeholder(self) -> None:
        if not self._card:
            return
        colors = self._card.colors or []
        if len(colors) > 1:
            bg = GOLD
        elif len(colors) == 1:
            bg = _MANA_BG.get(colors[0], BG_CARD_PLACEHOLDER)
        else:
            bg = BG_CARD_PLACEHOLDER

        text_parts = [self._card.name]
        if self._card.mana_cost:
            text_parts.append(self._card.mana_cost)
        if self._card.type_line:
            text_parts.append(self._card.type_line)
        text = "\n".join(text_parts)

        self._image.setText(text)
        self._image.setStyleSheet(
            f"background: {bg}; color: #eee; font-size: 10px; padding: 6px; border: none;"
        )

    def _update_border(self) -> None:
        if self._selected:
            self.setStyleSheet(self._BORDER_SELECTED)
        else:
            self.setStyleSheet(self._BORDER_NORMAL)


# ============================================================================
# ImageLoader — background thread for fetching card art from Scryfall
# ============================================================================

from PySide6.QtCore import QThread  # noqa: E402 (grouped with class)


class ImageLoader(QThread):
    """Fetches card images from the Scryfall API in a background thread."""

    image_ready = Signal(str, str)  # card_name, local_file_path

    def __init__(self, cache_dir: Path, parent=None) -> None:
        super().__init__(parent)
        self._cache_dir = cache_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._queue: list[str] = []
        self._mutex = QMutex()

    def enqueue(self, names: list[str]) -> None:
        """Add card names to the fetch queue and start the thread if idle."""
        self._mutex.lock()
        # Avoid duplicates.
        existing = set(self._queue)
        for n in names:
            if n not in existing:
                self._queue.append(n)
        self._mutex.unlock()
        if not self.isRunning():
            self.start()

    def run(self) -> None:
        while True:
            self._mutex.lock()
            if not self._queue:
                self._mutex.unlock()
                break
            name = self._queue.pop(0)
            self._mutex.unlock()

            path = self._fetch(name)
            if path:
                self.image_ready.emit(name, str(path))
            # Small delay to respect Scryfall rate limits (~10 req/s).
            time.sleep(0.12)

    def _fetch(self, name: str) -> Path | None:
        h = hashlib.md5(name.encode()).hexdigest()  # noqa: S324
        path = self._cache_dir / f"{h}.jpg"
        if path.exists():
            return path

        query_name = name.split(" // ")[0]
        try:
            with httpx.Client(timeout=15, follow_redirects=True) as client:
                resp = client.get(
                    "https://api.scryfall.com/cards/named",
                    params={
                        "exact": query_name,
                        "format": "image",
                        "version": "normal",
                    },
                )
            if resp.status_code == 200:
                path.write_bytes(resp.content)
                return path
            logger.debug("Scryfall image %d for %r", resp.status_code, name)
        except Exception:
            logger.debug("Failed to fetch image for %s", name, exc_info=True)
        return None


# ============================================================================
# DeckPanel — right-side panel showing drafted cards
# ============================================================================

class _DeckCardLabel(QLabel):
    """Clickable card label in the deck panel."""

    clicked = Signal(str)

    def __init__(self, card_name: str, cmc: float, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.card_name = card_name
        self.cmc = cmc
        cmc_str = f"{int(cmc)}" if cmc == int(cmc) else f"{cmc:.1f}"
        self.setText(f"  {cmc_str}   {card_name}")
        self.setObjectName("deckCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.card_name)
        super().mousePressEvent(event)


class DeckPanel(QFrame):
    """Right panel showing the player's picked cards and sideboard count."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("deckPanel")
        self.setFixedWidth(DECK_PANEL_WIDTH)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Deck header.
        self._deck_header = QLabel("Deck")
        self._deck_header.setObjectName("deckHeader")
        layout.addWidget(self._deck_header)

        self._deck_count = QLabel("0 Cards")
        self._deck_count.setObjectName("deckCount")
        layout.addWidget(self._deck_count)

        # Scrollable deck card list.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self._card_container = QWidget()
        self._card_container.setStyleSheet("background: transparent;")
        self._card_layout = QVBoxLayout(self._card_container)
        self._card_layout.setContentsMargins(0, 0, 0, 0)
        self._card_layout.setSpacing(0)
        self._card_layout.addStretch()
        scroll.setWidget(self._card_container)
        layout.addWidget(scroll, stretch=1)

        # Sideboard section.
        self._side_header = QLabel("Sideboard")
        self._side_header.setObjectName("sideHeader")
        layout.addWidget(self._side_header)

        self._side_count = QLabel("0 Cards")
        self._side_count.setObjectName("sideCount")
        layout.addWidget(self._side_count)

        scroll_sb = QScrollArea()
        scroll_sb.setWidgetResizable(True)
        scroll_sb.setFrameShape(QFrame.Shape.NoFrame)
        scroll_sb.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scroll_sb.setMaximumHeight(120)
        self._sb_container = QWidget()
        self._sb_container.setStyleSheet("background: transparent;")
        self._sb_layout = QVBoxLayout(self._sb_container)
        self._sb_layout.setContentsMargins(0, 0, 0, 0)
        self._sb_layout.setSpacing(0)
        self._sb_layout.addStretch()
        scroll_sb.setWidget(self._sb_container)
        layout.addWidget(scroll_sb)

        # Track cards: name → DraftCard data.
        self._cards: dict[str, DraftCard] = {}
        # Track which section each card is in.
        self._in_sideboard: set[str] = set()

    def add_card(self, card: DraftCard) -> None:
        """Add a picked card to the deck list."""
        self._cards[card.name] = card
        lbl = _DeckCardLabel(card.name, card.cmc)
        lbl.clicked.connect(self._on_card_clicked)
        idx = self._find_insert_index(self._card_layout, card.cmc)
        self._card_layout.insertWidget(idx, lbl)
        self._update_counts()

    def clear(self) -> None:
        self._clear_layout(self._card_layout)
        self._clear_layout(self._sb_layout)
        self._cards.clear()
        self._in_sideboard.clear()
        self._update_counts()

    def _on_card_clicked(self, card_name: str) -> None:
        """Toggle a card between deck and sideboard."""
        card = self._cards.get(card_name)
        if not card:
            return
        if card_name in self._in_sideboard:
            # Move from sideboard → deck.
            self._remove_from_layout(self._sb_layout, card_name)
            self._in_sideboard.discard(card_name)
            lbl = _DeckCardLabel(card_name, card.cmc)
            lbl.clicked.connect(self._on_card_clicked)
            idx = self._find_insert_index(self._card_layout, card.cmc)
            self._card_layout.insertWidget(idx, lbl)
        else:
            # Move from deck → sideboard.
            self._remove_from_layout(self._card_layout, card_name)
            self._in_sideboard.add(card_name)
            lbl = _DeckCardLabel(card_name, card.cmc)
            lbl.setStyleSheet("color: #888;")
            lbl.clicked.connect(self._on_card_clicked)
            idx = self._find_insert_index(self._sb_layout, card.cmc)
            self._sb_layout.insertWidget(idx, lbl)
        self._update_counts()

    @staticmethod
    def _remove_from_layout(layout: QVBoxLayout, card_name: str) -> None:
        for i in range(layout.count()):
            w = layout.itemAt(i).widget()
            if isinstance(w, _DeckCardLabel) and w.card_name == card_name:
                layout.takeAt(i)
                w.deleteLater()
                return

    @staticmethod
    def _find_insert_index(layout: QVBoxLayout, cmc: float) -> int:
        """Find the insertion index to keep cards sorted by CMC."""
        for i in range(layout.count() - 1):  # skip trailing stretch
            w = layout.itemAt(i).widget()
            if isinstance(w, _DeckCardLabel) and cmc < w.cmc:
                return i
        return layout.count() - 1  # before stretch

    @staticmethod
    def _clear_layout(layout: QVBoxLayout) -> None:
        while layout.count() > 1:
            item = layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _update_counts(self) -> None:
        deck_n = self._card_layout.count() - 1  # minus the stretch
        sb_n = self._sb_layout.count() - 1
        self._deck_count.setText(f"{deck_n} Cards")
        self._side_count.setText(f"{sb_n} Cards")


# ============================================================================
# SimulatorWindow — the main Arena-like draft window
# ============================================================================

class SimulatorWindow(QMainWindow):
    """MTG Arena–style draft simulator window.

    Signals:
        pack_presented: ``(list[str], int, int)`` — card names, pack_number,
            pick_number. Emitted when a new pack is shown.
        pick_confirmed: ``(str,)`` — name of the picked card.
    """

    pack_presented = Signal(list, int, int)
    pick_confirmed = Signal(str)
    skip_draft_requested = Signal()

    def __init__(
        self,
        engine: DraftEngine,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._engine = engine
        self._selected_card: str | None = None
        self._card_widgets: list[CardWidget] = []
        self._timer_remaining = TIMER_SECONDS

        self.setWindowTitle(f"NemeDraft Simulator — {engine.set_code}")
        self.setMinimumSize(1100, 750)
        self.resize(1280, 860)
        self.setStyleSheet(SIMULATOR_STYLESHEET)

        self._image_loader = ImageLoader(SIM_ART_CACHE, self)
        self._image_loader.image_ready.connect(self._on_image_ready)

        self._build_ui()
        self._setup_timer()

    # -- UI construction -----------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())

        content = QHBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(0)
        content.addWidget(self._build_card_grid(), stretch=1)

        self._deck_panel = DeckPanel()
        content.addWidget(self._deck_panel)
        root.addLayout(content, stretch=1)

        root.addWidget(self._build_bottom_bar())

    def _build_header(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("header")
        frame.setFixedHeight(HEADER_HEIGHT)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(16, 0, 16, 0)

        self._set_label = QLabel(self._engine.set_code)
        self._set_label.setObjectName("headerSet")
        layout.addWidget(self._set_label)

        layout.addStretch()

        self._info_label = QLabel("NemeDraft Simulator")
        self._info_label.setObjectName("headerInfo")
        self._info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._info_label)

        layout.addStretch()

        self._pick_label = QLabel("")
        self._pick_label.setObjectName("headerPickInfo")
        layout.addWidget(self._pick_label)

        return frame

    def _build_card_grid(self) -> QWidget:
        self._grid_container = QWidget()
        self._grid_container.setStyleSheet("background: transparent;")
        self._grid_layout = QGridLayout(self._grid_container)
        self._grid_layout.setContentsMargins(16, 12, 8, 12)
        self._grid_layout.setSpacing(CARD_SPACING)

        for row in range(GRID_ROWS):
            for col in range(GRID_COLS):
                cw = CardWidget()
                cw.clicked.connect(self._on_card_clicked)
                cw.setVisible(False)
                self._grid_layout.addWidget(cw, row, col)
                self._card_widgets.append(cw)

        # Draft-complete overlay label (hidden initially).
        self._complete_label = QLabel("Draft Complete!")
        self._complete_label.setObjectName("draftComplete")
        self._complete_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._complete_label.setVisible(False)
        self._grid_layout.addWidget(
            self._complete_label, 0, 0, GRID_ROWS, GRID_COLS
        )

        return self._grid_container

    def _build_bottom_bar(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("bottomBar")
        frame.setFixedHeight(BOTTOM_HEIGHT)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(16, 6, 16, 6)

        self._timer_text = QLabel(self._format_time(TIMER_SECONDS))
        self._timer_text.setObjectName("timerLabel")
        layout.addWidget(self._timer_text)

        self._timer_bar = QProgressBar()
        self._timer_bar.setObjectName("timerBar")
        self._timer_bar.setRange(0, TIMER_SECONDS)
        self._timer_bar.setValue(TIMER_SECONDS)
        self._timer_bar.setTextVisible(False)
        layout.addWidget(self._timer_bar, stretch=1)

        layout.addSpacing(24)

        self._skip_btn = QPushButton("Skip Draft")
        self._skip_btn.setObjectName("confirmBtn")
        self._skip_btn.setStyleSheet(
            "QPushButton { background-color: #555; color: white; font-size: 14px; "
            "font-weight: bold; border: none; border-radius: 8px; padding: 8px 16px; "
            "min-height: 38px; }"
            "QPushButton:hover { background-color: #666; }"
            "QPushButton:disabled { background-color: #333; color: #666; }"
        )
        self._skip_btn.clicked.connect(self._on_skip_draft)
        layout.addWidget(self._skip_btn)

        layout.addSpacing(8)

        self._confirm_btn = QPushButton("Confirm Pick")
        self._confirm_btn.setObjectName("confirmBtn")
        self._confirm_btn.setEnabled(False)
        self._confirm_btn.clicked.connect(self._on_confirm)
        layout.addWidget(self._confirm_btn)

        return frame

    # -- timer ---------------------------------------------------------------

    def _setup_timer(self) -> None:
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._on_timer_tick)

    def _start_timer(self) -> None:
        self._timer_remaining = TIMER_SECONDS
        self._timer_bar.setValue(TIMER_SECONDS)
        self._timer_text.setText(self._format_time(TIMER_SECONDS))
        self._tick_timer.start()

    def _stop_timer(self) -> None:
        self._tick_timer.stop()

    @Slot()
    def _on_timer_tick(self) -> None:
        self._timer_remaining -= 1
        self._timer_bar.setValue(max(0, self._timer_remaining))
        self._timer_text.setText(self._format_time(self._timer_remaining))

        if self._timer_remaining <= 0:
            self._stop_timer()
            # Auto-pick first card in the pack.
            pack = self._engine.get_current_pack()
            if pack:
                self._selected_card = pack[0].name
                self._on_confirm()

    @staticmethod
    def _format_time(seconds: int) -> str:
        m = seconds // 60
        s = seconds % 60
        return f"{m}:{s:02d}"

    # -- card size calculation -----------------------------------------------

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_card_sizes()

    def _update_card_sizes(self) -> None:
        grid_w = (
            self.centralWidget().width()
            - DECK_PANEL_WIDTH
            - 16 - 8  # left + right margins of grid
        )
        grid_h = (
            self.centralWidget().height()
            - HEADER_HEIGHT
            - BOTTOM_HEIGHT
            - 12 - 12  # top + bottom margins of grid
        )

        if grid_w <= 0 or grid_h <= 0:
            return

        # Width-limited.
        card_w = (grid_w - (GRID_COLS - 1) * CARD_SPACING) // GRID_COLS
        card_h = int(card_w / CARD_RATIO)

        # Height-limited.
        max_card_h = (grid_h - (GRID_ROWS - 1) * CARD_SPACING) // GRID_ROWS
        if card_h > max_card_h:
            card_h = max_card_h
            card_w = int(card_h * CARD_RATIO)

        card_w = max(100, card_w)
        card_h = max(140, card_h)

        for cw in self._card_widgets:
            cw.setFixedSize(card_w, card_h)

    # -- pack presentation ---------------------------------------------------

    def present_current_pack(self) -> None:
        """Present the engine's current pack to the player."""
        pack = self._engine.get_current_pack()
        if pack is None:
            self._show_draft_complete()
            return

        pn = self._engine.pack_number
        pk = self._engine.pick_number
        self._pick_label.setText(f"Pack {pn + 1}  /  Pick {pk + 1}")
        self._selected_card = None
        self._confirm_btn.setEnabled(False)

        # Update card widgets.
        names: list[str] = []
        for i, cw in enumerate(self._card_widgets):
            if i < len(pack):
                cw.set_card(pack[i])
                names.append(pack[i].name)
            else:
                cw.clear_card()

        self._update_card_sizes()
        self._start_timer()

        # Start loading images.
        self._image_loader.enqueue(names)

        # Notify overlay.
        self.pack_presented.emit(names, pn, pk)

    def _show_draft_complete(self) -> None:
        self._stop_timer()
        for cw in self._card_widgets:
            cw.clear_card()
        self._complete_label.setVisible(True)
        self._confirm_btn.setEnabled(False)
        self._pick_label.setText("Draft Complete")
        self._timer_text.setText("0:00")
        self._timer_bar.setValue(0)

    # -- card selection & picking --------------------------------------------

    @Slot(str)
    def _on_card_clicked(self, card_name: str) -> None:
        if self._engine.is_draft_complete:
            return
        self._selected_card = card_name
        self._confirm_btn.setEnabled(True)
        for cw in self._card_widgets:
            cw.set_selected(cw.card_name == card_name)

    @Slot()
    def _on_confirm(self) -> None:
        if not self._selected_card:
            return
        name = self._selected_card
        self._stop_timer()

        # Find the DraftCard to add to the deck panel.
        pack = self._engine.get_current_pack()
        picked_card: DraftCard | None = None
        if pack:
            for c in pack:
                if c.name == name:
                    picked_card = c
                    break

        ok = self._engine.player_pick(name)
        if not ok:
            return

        if picked_card:
            self._deck_panel.add_card(picked_card)

        self.pick_confirmed.emit(name)

        # Show next pack (or complete screen).
        self.present_current_pack()

    @Slot()
    def _on_skip_draft(self) -> None:
        """Request the bridge to auto-pick all remaining packs."""
        self._skip_btn.setEnabled(False)
        self._skip_btn.setText("Skipping...")
        self.skip_draft_requested.emit()

    # -- external API for auto-pick ------------------------------------------

    def select_card(self, card_name: str) -> None:
        """Programmatically select a card (e.g. the model's top pick)."""
        self._selected_card = card_name
        self._confirm_btn.setEnabled(True)
        for cw in self._card_widgets:
            cw.set_selected(cw.card_name == card_name)

    def auto_pick(self, card_name: str) -> None:
        """Immediately pick a card without user confirmation (for skip draft)."""
        self._selected_card = card_name
        self._on_confirm()

    # -- image loading callback ----------------------------------------------

    @Slot(str, str)
    def _on_image_ready(self, card_name: str, path: str) -> None:
        pixmap = QPixmap(path)
        if pixmap.isNull():
            return
        for cw in self._card_widgets:
            if cw.card_name == card_name:
                cw.set_image(pixmap)
                break
