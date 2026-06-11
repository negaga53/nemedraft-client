"""PySide6 overlay window — tabbed always-on-top companion for draft pick ratings."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QByteArray, QPoint, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QCursor,
    QIcon,
    QKeySequence,
    QPixmap,
    QShortcut,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QStyle,
    QSystemTrayIcon,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from client.overlay import __version__
from client.overlay.api_client import Pick
from common.inference.deck_builder import DeckSuggestion
from common.inference.pool_analyzer import PoolAnalysis
from client.overlay.config import OverlayConfig
from client.overlay.i18n import Translator, card_name, tr
from client.overlay.notifications import NotificationBus
from common.inference.signals import SignalResult
from client.overlay.ui._macos import elevate_to_floating
from client.overlay.ui.deck_tab import DeckTab
from client.overlay.ui.pack_tab import PackTab
from client.overlay.ui.settings_tab import SettingsTab
from client.overlay.ui.summary_tab import SummaryTab
from client.overlay.ui.theme import apply_theme
from client.overlay.ui.toast import ToastHost
from client.overlay.ui.view_mode import ViewMode, ViewModeController


class _CardTranslationWorker(QThread):
    """Background thread that loads card name translations."""

    translations_loaded = Signal()

    def __init__(
        self,
        scryfall_dir: Path,
        set_code: str | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._scryfall_dir = scryfall_dir
        self._set_code = set_code

    def run(self) -> None:
        translator = Translator.instance()
        translator.load_card_translations(
            self._scryfall_dir, set_code=self._set_code,
        )
        self.translations_loaded.emit()


class OverlayWindow(QWidget):
    """Main overlay companion window with tabbed interface.

    Args:
        config: Overlay configuration (persisted between sessions).
        transparent: Use frameless click-through transparent mode.
        show_art: Display Scryfall card art thumbnails.
        opacity: Window opacity in transparent mode (0.0–1.0).
    """

    # Thread-safe signals emitted from background threads, handled on UI thread.
    prediction_ready = Signal(list, str, int, int, int, dict)  # results, set_code, pn, pick, pool_size, art_paths
    pool_analysis_ready = Signal(object)   # PoolAnalysis
    signals_ready = Signal(object)         # SignalResult | None
    deck_suggestions_ready = Signal(dict, list, dict)  # dict[str, DeckSuggestion], list[str], scryfall_cards
    pick_history_ready = Signal(object)    # dict[(int,int), PickHistoryEntry]
    draft_complete_signal = Signal()       # switch to deck tab on UI thread
    draft_summary_ready = Signal(object)   # DraftSummary — reveal summary tab
    card_art_ready = Signal(str, object)   # card_name, Path | None — per-card art arrival

    def __init__(
        self,
        config: OverlayConfig,
        *,
        transparent: bool = False,
        show_art: bool = False,
        opacity: float = 0.85,
        scryfall_dir: Path | None = None,
    ) -> None:
        super().__init__()
        self._transparent = transparent
        self._show_art = show_art
        self._config = config
        self._recommend_count = 1
        # View-mode state machine (FULL tabbed view vs COMPACT pin strip).
        self._view_mode = ViewModeController(config, parent=self)
        self._view_mode.mode_changed.connect(self._apply_view_mode)
        from client.overlay.env import bundle_root
        self._scryfall_dir = scryfall_dir or (bundle_root() / "data" / "scryfall")
        self._draft_set_code: str | None = None
        self.setWindowTitle(f"{tr('app_title')} — v{__version__}")

        flags = (
            Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
        )
        if transparent:
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self.setWindowFlags(flags)
        self.setMinimumWidth(560)
        self.setMaximumWidth(720)
        self.resize(620, 820)
        self.setWindowOpacity(opacity)
        apply_theme(self, glass=transparent)

        self._build_ui()
        self.prediction_ready.connect(self._on_prediction)
        self.pool_analysis_ready.connect(self._on_pool_analysis)
        self.signals_ready.connect(self._on_signals)
        self.deck_suggestions_ready.connect(self._on_deck_suggestions)
        self.pick_history_ready.connect(self._on_pick_history)
        self.draft_complete_signal.connect(self._on_draft_complete)
        self.draft_summary_ready.connect(self._on_draft_summary)
        self.card_art_ready.connect(self._on_card_art_ready)
        self._show_status(tr("waiting_for_draft"))

        # Drag support — always enabled (frameless window).
        self._drag_pos = None

        # Window-scoped keyboard shortcuts (no global hooks).
        self._init_shortcuts()

        # System tray icon for minimize-to-tray. Built lazily-but-eagerly:
        # the icon must exist before the minimize button can use it.
        self._tray_icon: QSystemTrayIcon | None = None
        self._build_tray_icon()

    # -- UI construction -----------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(4)

        _BTN_STYLE = (
            "QPushButton { background: rgba(60,60,90,0.8); border: 1px solid #444;"
            " border-radius: 4px; color: #ccc; font-size: 12px; font-weight: bold; }"
            "QPushButton:hover { background: rgba(80,80,120,0.9); }"
        )

        # ------- Row 1: drag handle -------
        drag_row = QHBoxLayout()
        drag_row.setContentsMargins(8, 4, 8, 4)
        drag_row.setSpacing(6)

        self._toggle_btn = QPushButton("▾")
        self._toggle_btn.setFixedSize(26, 22)
        self._toggle_btn.setToolTip(tr("toggle_compact_tooltip"))
        self._toggle_btn.setStyleSheet(
            "QPushButton { background: rgba(50,50,80,.5);"
            " border: 1px solid #2a2a3e; border-radius: 3px; color: #e0e0e0;"
            " font-size: 14px; font-weight: 700; padding: 0; }"
            "QPushButton:hover { background: rgba(80,80,120,.9); color: #fff; }"
        )
        self._toggle_btn.clicked.connect(self._toggle_compact)
        self._toggle_btn.setVisible(False)
        drag_row.addWidget(self._toggle_btn)

        self._brand_label = QLabel("NEMEDRAFT")
        self._brand_label.setStyleSheet(
            "color: #cfb53b; font-weight: 700; font-size: 11px;"
            " letter-spacing: .08em;"
        )
        self._version_label = QLabel(f"v{__version__}")
        self._version_label.setStyleSheet("color: #555; font-size: 10px;")
        drag_row.addWidget(self._brand_label)
        drag_row.addWidget(self._version_label)
        drag_row.addStretch()

        self._min_btn = QPushButton("‒")
        self._min_btn.setFixedSize(26, 22)
        self._min_btn.setToolTip(tr("minimize_tooltip"))
        self._min_btn.setStyleSheet(
            "QPushButton { background: rgba(50,50,80,.5);"
            " border: 1px solid #2a2a3e; border-radius: 3px; color: #e0e0e0;"
            " font-size: 16px; font-weight: 700; padding: 0 0 4px 0; }"
            "QPushButton:hover { background: rgba(80,80,120,.9); color: #fff; }"
        )
        # Hide-to-tray instead of showMinimized: on Windows, ``Qt.Tool +
        # FramelessWindowHint`` windows have no taskbar entry, so a normal
        # minimize disappears the window with no way to bring it back —
        # users report this as "the app closed". The tray icon gives them
        # a reliable restore path.
        self._min_btn.clicked.connect(self._minimize_to_tray)
        drag_row.addWidget(self._min_btn)

        self._close_btn = QPushButton("✕")
        self._close_btn.setFixedSize(26, 22)
        self._close_btn.setToolTip(tr("close_tooltip"))
        self._close_btn.setStyleSheet(
            "QPushButton { background: rgba(90,40,40,.5);"
            " border: 1px solid #402a2a; border-radius: 3px; color: #f0cccc;"
            " font-size: 13px; font-weight: 700; padding: 0; }"
            "QPushButton:hover { background: rgba(180,55,55,.95); color: #fff; }"
        )
        self._close_btn.clicked.connect(self.close)
        drag_row.addWidget(self._close_btn)

        self._drag_row_widget = QWidget()
        self._drag_row_widget.setLayout(drag_row)
        self._drag_row_widget.setCursor(Qt.CursorShape.OpenHandCursor)
        self._drag_row_widget.setStyleSheet(
            "border-bottom: 1px solid #1f1f30;"
        )
        # Pin the header height — without this, toggling compact mode lets
        # Qt redistribute vertical slack into the drag row and the title
        # bar visibly grows/shrinks on every toggle.
        self._drag_row_widget.setFixedHeight(32)
        root.addWidget(self._drag_row_widget)

        # Keep a reference so the drag handling below still works:
        self._header_widget = self._drag_row_widget
        # Legacy attribute kept so callers that did `self.header.setText(...)` compile;
        # it now points at the brand label which doesn't change per pick.
        self.header = self._brand_label

        # Toast banners — visible in both full and compact modes; fed by
        # the NotificationBus (thread-safe post from anywhere).
        self.toast_host = ToastHost()
        root.addWidget(self.toast_host)
        NotificationBus.instance().posted.connect(
            self.toast_host.show_notification,
        )

        # Status line.
        self.status = QLabel("")
        self.status.setObjectName("status")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.status)

        # Tabbed area.
        self.tabs = QTabWidget()
        self.pack_tab = PackTab(show_art=self._show_art)
        self.deck_tab = DeckTab()
        self.summary_tab = SummaryTab()
        self.settings_tab = SettingsTab(self._config)

        self.tabs.addTab(self.pack_tab, tr("tab_pack"))
        self.tabs.addTab(self.deck_tab, tr("tab_deck"))
        self.tabs.addTab(self.summary_tab, tr("tab_summary"))
        self.tabs.addTab(self.settings_tab, tr("tab_settings"))
        # Store tab indices for retranslation.
        self._tab_pack_idx = 0
        self._tab_deck_idx = 1
        self._tab_summary_idx = 2
        self._tab_settings_idx = 3

        # Hide deck/settings tabs until boot completes; the summary tab
        # only appears once a draft completes.
        self.tabs.setTabVisible(self._tab_deck_idx, False)
        self.tabs.setTabVisible(self._tab_summary_idx, False)
        self.tabs.setTabVisible(self._tab_settings_idx, False)

        # Hide entire tabbed area during boot — shown after boot completes.
        self.tabs.setVisible(False)

        root.addWidget(self.tabs, stretch=1)

        # Reliable QObject-to-QObject connection for UI retranslation.
        self.settings_tab.language_changed.connect(self._on_language_changed)

        # Compact mini-view: context pill + top-3 card rows (hidden by default).
        self._mini_container = QWidget()
        self._mini_outer = QVBoxLayout(self._mini_container)
        self._mini_outer.setContentsMargins(0, 4, 0, 0)
        self._mini_outer.setSpacing(2)
        self._mini_pill = QLabel("")
        self._mini_pill.setObjectName("contextPill")
        self._mini_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._mini_pill.setStyleSheet(
            "QLabel#contextPill {"
            "  background: #cfb53b; color: #12121f; font-weight: 700;"
            "  padding: 2px 10px; border-radius: 10px; font-size: 10px;"
            "  letter-spacing: .05em;"
            "}"
        )
        _mini_pill_row = QHBoxLayout()
        _mini_pill_row.addStretch()
        _mini_pill_row.addWidget(self._mini_pill)
        _mini_pill_row.addStretch()
        self._mini_outer.addLayout(_mini_pill_row)
        self._mini_rows_widget = QWidget()
        self._mini_layout = QVBoxLayout(self._mini_rows_widget)
        self._mini_layout.setContentsMargins(0, 0, 0, 0)
        self._mini_layout.setSpacing(1)
        self._mini_outer.addWidget(self._mini_rows_widget)
        self._mini_container.setVisible(False)
        root.addWidget(self._mini_container)

        # Connect deck archetype changes → pack tab stats.
        self.deck_tab.archetype_changed.connect(self._on_archetype_changed)

        # Internal state for cross-tab updates.
        self._deck_suggestions: dict[str, DeckSuggestion] = {}
        self._scryfall_cards: dict = {}
        # Pack history: (pack_number, pick_number) → list of card names.
        self._pack_history: dict[tuple[int, int], list[str]] = {}
        # Last prediction results for compact refresh.
        self._last_results: list[Pick] = []
        self._last_art_paths: dict[str, Path | None] = {}
        self._last_pool_size: int = 0
        self._last_set_code: str = ""
        self._last_pack_number: int = 0
        self._last_pick_number: int = 0

        # Compact-view hover preview state.
        self._mini_preview = None
        self._mini_pending_art: Path | None = None
        self._mini_pending_pos = QPoint()
        self._mini_hover_timer = QTimer(self)
        self._mini_hover_timer.setSingleShot(True)
        self._mini_hover_timer.setInterval(200)
        self._mini_hover_timer.timeout.connect(self._show_mini_preview)


    # -- status helpers ------------------------------------------------------

    def _show_status(self, text: str) -> None:
        self.status.setText(text)
        self.status.setVisible(True)

    def show_loading(self) -> None:
        self._show_status(tr("loading_model"))
        self.tabs.setVisible(False)
        self._saved_geometry: bytes | None = bytes(self.saveGeometry())
        self.resize(self.width(), 100)

    def show_model_ready(self) -> None:
        """Reveal all tabs once the boot worker completes.

        Server authentication state is tracked separately on the home tab.
        """
        self.tabs.setVisible(True)
        self.tabs.setTabVisible(self._tab_deck_idx, True)
        self.tabs.setTabVisible(self._tab_settings_idx, True)
        self.status.setVisible(False)
        saved = getattr(self, "_saved_geometry", None)
        if saved is not None:
            from PySide6.QtCore import QByteArray
            # Restore position only — the full-size height is set below.
            self.restoreGeometry(QByteArray(saved))
            self._saved_geometry = None
        self.resize(self.width(), 800)
        # Ensure the resize sticks after restoreGeometry.
        self.setMinimumHeight(800)
        QTimer.singleShot(0, lambda: self.setMinimumHeight(0))
        # Geometry was re-applied — make sure the header stayed reachable.
        from client.overlay.ui.screen_utils import ensure_on_screen
        ensure_on_screen(self)

    def show_waiting(self) -> None:
        self._show_status(tr("waiting_for_draft"))

    def show_vip_required(self) -> None:
        """Show a message indicating VIP is required for predictions."""
        self._show_status(tr("vip_required"))

    def show_draft_started(self) -> None:
        """Switch the pack tab to the predictions page when a draft begins."""
        self.pack_tab.home_widget.set_draft_active(True)
        self.pack_tab.show_pack_view()
        self.tabs.setCurrentIndex(self._tab_pack_idx)
        self._toggle_btn.setVisible(True)
        self._toggle_btn.setEnabled(True)
        # Restore the user's preferred view mode now that a draft is live
        # (the overlay always boots FULL so the home tab is reachable).
        if self._view_mode.persisted_mode() is ViewMode.COMPACT:
            self._view_mode.set_mode(ViewMode.COMPACT, persist=False)

    def show_draft_ended(self) -> None:
        """Switch back to the home page when the draft / Arena session ends."""
        self.pack_tab.home_widget.set_draft_active(False)
        self.pack_tab.show_home()
        self._toggle_btn.setEnabled(False)
        self._toggle_btn.setVisible(False)
        # System-driven exit — keep the user's preferred mode for next draft.
        self._view_mode.set_mode(ViewMode.FULL, persist=False)

    def show_draft_complete(self) -> None:
        """Thread-safe: emit signal to switch to the deck tab."""
        self.draft_complete_signal.emit()

    def show_draft_summary(self, summary: object) -> None:
        """Thread-safe: populate and reveal the summary tab."""
        self.draft_summary_ready.emit(summary)

    def hide_draft_summary(self) -> None:
        """Clear and hide the summary tab (next draft started)."""
        self.summary_tab.clear()
        self.tabs.setTabVisible(self._tab_summary_idx, False)

    @Slot(object)
    def _on_draft_summary(self, summary: object) -> None:
        """Reveal the summary tab and focus it (runs on UI thread)."""
        self._view_mode.set_mode(ViewMode.FULL, persist=False)
        self.summary_tab.set_summary(summary)
        self.tabs.setTabVisible(self._tab_summary_idx, True)
        self.tabs.setCurrentIndex(self._tab_summary_idx)

    # -- live-applied settings -------------------------------------------------

    def set_show_art(self, enabled: bool) -> None:
        """Apply the show-art toggle live, re-rendering cached results."""
        self._show_art = enabled
        self.pack_tab.set_show_art(enabled)
        if self._last_results:
            self.pack_tab.update_predictions(
                self._last_results, self._last_art_paths,
                pack_number=self._last_pack_number,
                pick_number=self._last_pick_number,
            )
            if self._compact:
                self._refresh_mini()

    def set_transparent(self, enabled: bool) -> None:
        """Apply transparent mode live by re-creating the native window.

        ``WA_TranslucentBackground`` only takes effect at native window
        creation, and ``setWindowFlags`` early-returns when the flags are
        unchanged (ours never change). ``setParent(None, flags)`` is the
        reliable way to force a re-create; ``show()`` re-runs the macOS
        floating elevation via ``showEvent``.
        """
        if enabled == self._transparent:
            return
        self._transparent = enabled
        geometry = self.geometry()
        was_visible = self.isVisible()
        self.hide()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, enabled)
        self.setParent(None, self.windowFlags())
        apply_theme(self, glass=enabled)
        self.setGeometry(geometry)
        if was_visible:
            self.show()

    def show_prediction_loading(self, pack_number: int, pick_number: int) -> None:
        """Surface a loading indicator while a prediction is in flight.

        Called when a new pack opens; hidden once the prediction lands
        on ``_on_prediction``. The pack/pick are passed for future use
        (e.g. updating the context pill to "Predicting P{n}P{m}…") but
        are not strictly required by the current bar UI.
        """
        del pack_number, pick_number  # reserved for future affordances
        self.pack_tab.show_loading()

    # -- system tray (minimize-to-tray) --------------------------------------

    def _app_icon(self) -> QIcon:
        """Return the overlay's app icon, falling back to a Qt standard one.

        The PNG is bundled under ``assets/`` via the PyInstaller spec;
        in dev mode it's resolved relative to the project root.
        """
        from client.overlay.env import bundle_root

        for candidate in (
            bundle_root() / "assets" / "icon.png",
            bundle_root() / "external" / "nemedraft-client" / "assets" / "icon.png",
        ):
            if candidate.is_file():
                return QIcon(str(candidate))
        # Fallback so the tray icon is always something — better than
        # an empty pixmap that some Linux trays refuse to render.
        return self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)

    def _build_tray_icon(self) -> None:
        """Create the system tray icon used by minimize-to-tray.

        No-op when the platform doesn't expose a tray (some Linux
        desktops). In that case the minimize button falls back to
        ``showMinimized`` and the user gets the same behaviour as
        before this fix.
        """
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return

        icon = self._app_icon()
        self.setWindowIcon(icon)

        self._tray_icon = QSystemTrayIcon(icon, self)
        self._tray_icon.setToolTip(f"{tr('app_title')} — v{__version__}")

        menu = QMenu()
        show_action = QAction(tr("tray_show"), self)
        show_action.triggered.connect(self._restore_from_tray)
        menu.addAction(show_action)
        menu.addSeparator()
        quit_action = QAction(tr("tray_quit"), self)
        quit_action.triggered.connect(self._quit_from_tray)
        menu.addAction(quit_action)
        self._tray_icon.setContextMenu(menu)

        self._tray_icon.activated.connect(self._on_tray_activated)

    def _minimize_to_tray(self) -> None:
        """Hide the overlay to the system tray instead of minimising it."""
        if self._tray_icon is None:
            # No tray available — fall back to the old behaviour so at
            # least something happens on click.
            self.showMinimized()
            return
        self._tray_icon.show()
        self.hide()

    def _restore_from_tray(self) -> None:
        """Bring the overlay back from the tray."""
        if self._tray_icon is not None:
            self._tray_icon.hide()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    @Slot(QSystemTrayIcon.ActivationReason)
    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """Restore on left-click / double-click of the tray icon."""
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._restore_from_tray()

    def _quit_from_tray(self) -> None:
        """Quit the app cleanly from the tray menu."""
        if self._tray_icon is not None:
            self._tray_icon.hide()
        QApplication.instance().quit()

    @Slot()
    def _on_draft_complete(self) -> None:
        """Switch to the deck tab (runs on UI thread)."""
        # Exit compact mode so the full deck tab is visible.
        self._view_mode.set_mode(ViewMode.FULL, persist=False)
        self.tabs.setCurrentIndex(self._tab_deck_idx)

    @Slot(str)
    def _on_language_changed(self, _language: str) -> None:
        """Handle language change — retranslate labels then load cards in background."""
        # Immediately retranslate all static UI labels.
        self.retranslate()

        # Determine which set to load translations for.
        set_code = self._draft_set_code or self._last_set_code

        # Show loading status while card translations are fetched.
        self._show_status(tr("loading_translations"))

        # Load card translations in a background thread.
        self._card_worker = _CardTranslationWorker(
            self._scryfall_dir, set_code, parent=self,
        )
        self._card_worker.translations_loaded.connect(self._on_card_translations_ready)
        self._card_worker.start()

    @Slot()
    def _on_card_translations_ready(self) -> None:
        """Card translations loaded — re-render card names."""
        self.status.setVisible(False)
        # Re-render card rows and deck tab with translated card names.
        if self._last_results:
            self.pack_tab.update_predictions(
                self._last_results, self._last_art_paths,
                pack_number=self._last_pack_number,
                pick_number=self._last_pick_number,
            )
            if self._compact:
                self._refresh_mini()
        self.deck_tab.retranslate()

    def load_card_translations_async(self, set_code: str | None = None) -> None:
        """Start background card translation loading for *set_code*."""
        self._card_worker = _CardTranslationWorker(
            self._scryfall_dir, set_code, parent=self,
        )
        self._card_worker.translations_loaded.connect(self._on_card_translations_ready)
        self._card_worker.start()

    def retranslate(self) -> None:
        """Refresh all UI labels and re-render cached data with the current language."""
        self.setWindowTitle(f"{tr('app_title')} — v{__version__}")
        self._toggle_btn.setToolTip(tr("toggle_compact_tooltip"))
        self._min_btn.setToolTip(tr("minimize_tooltip"))
        self._close_btn.setToolTip(tr("close_tooltip"))
        self.tabs.setTabText(self._tab_pack_idx, tr("tab_pack"))
        self.tabs.setTabText(self._tab_deck_idx, tr("tab_deck"))
        self.tabs.setTabText(self._tab_summary_idx, tr("tab_summary"))
        self.tabs.setTabText(self._tab_settings_idx, tr("tab_settings"))
        # Delegate static labels to child tabs.
        self.settings_tab.retranslate()
        self.pack_tab.retranslate()
        self.deck_tab.retranslate()
        self.summary_tab.retranslate()
        # Re-render cached pack results so card names update.
        if self._last_results:
            self.pack_tab.update_predictions(
                self._last_results, self._last_art_paths,
                pack_number=self._last_pack_number,
                pick_number=self._last_pick_number,
            )
            self.pack_tab.set_context(
                self._last_set_code, self._last_pack_number,
                self._last_pick_number, self._last_pool_size,
            )
            if self._compact:
                self._refresh_mini()

    # -- drag support (frameless window — drag from header area) -------------

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            # Only start drag if the click is within the header bar area.
            header_rect = self._header_widget.geometry()
            if header_rect.contains(event.position().toPoint()):
                self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                self._header_widget.setCursor(Qt.CursorShape.ClosedHandCursor)
                event.accept()

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):  # noqa: N802
        if self._drag_pos is not None:
            self._drag_pos = None
            self._header_widget.setCursor(Qt.CursorShape.OpenHandCursor)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        """Quit the entire application when the overlay window is closed."""
        event.accept()
        QApplication.instance().quit()

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802
        """Elevate the macOS NSWindow level after the native window exists."""
        super().showEvent(event)
        elevate_to_floating(self)

    # -- thread-safe update entry points -------------------------------------

    def update_predictions(
        self,
        results: list[Pick],
        set_code: str,
        pack_number: int,
        pick_number: int,
        pool_size: int,
        art_paths: dict[str, Path | None] | None = None,
    ) -> None:
        """Thread-safe: push new prediction results to the UI."""
        self.prediction_ready.emit(
            results, set_code, pack_number, pick_number, pool_size,
            art_paths or {},
        )

    def update_pool_analysis(self, analysis: PoolAnalysis) -> None:
        """Thread-safe: push pool analysis to the stats tab."""
        self.pool_analysis_ready.emit(analysis)

    def update_signals(self, result: SignalResult | None) -> None:
        """Thread-safe: push signal results to the stats tab."""
        self.signals_ready.emit(result)

    def sync_pick_history(self, history: dict) -> None:
        """Thread-safe: push pick history to the pack tab."""
        self.pick_history_ready.emit(history)

    def update_card_art(self, card_name: str, path) -> None:
        """Thread-safe: push art for a single card to the pack tab.

        Per-card updates are name-keyed and idempotent, so a worker that
        finishes after the pack has advanced can still safely deliver
        art for any card still on screen (most P1Px cards persist across
        the pack).
        """
        self.card_art_ready.emit(card_name, path)

    @Slot(str, object)
    def _on_card_art_ready(self, card_name: str, path) -> None:
        """Apply per-card art on the UI thread."""
        self.pack_tab.update_card_art(card_name, path)

    @Slot(object)
    def _on_pick_history(self, history: dict) -> None:
        """Update the pack tab with pick history (runs on UI thread)."""
        self.pack_tab.set_pick_history(history)

    def update_deck_suggestions(
        self,
        suggestions: dict[str, DeckSuggestion],
        pool_names: list[str],
        scryfall_cards: dict | None = None,
    ) -> None:
        """Thread-safe: push deck suggestions to the deck tab."""
        self.deck_suggestions_ready.emit(suggestions, pool_names, scryfall_cards or {})

    # -- slot handlers (UI thread) -------------------------------------------

    @Slot(list, str, int, int, int, dict)
    def _on_prediction(
        self,
        results: list,
        set_code: str,
        pack_number: int,
        pick_number: int,
        pool_size: int,
        art_paths: dict,
    ) -> None:
        """Update the pack tab with new predictions (runs on UI thread)."""
        self.status.setVisible(False)
        # The prediction landed — dismiss the in-flight loading bar.
        self.pack_tab.hide_loading()
        if not results:
            self._show_status(tr("no_predictions"))
            return

        # Store current pack in history for taken-card computation.
        current_names = [r.card for r in results]
        self._pack_history[(pack_number, pick_number)] = current_names

        # Compute taken cards: compare with the pack from 8 picks ago.
        taken_names: list[str] = []
        if pick_number >= 8:
            prev_key = (pack_number, pick_number - 8)
            prev_pack = self._pack_history.get(prev_key)
            if prev_pack:
                current_set = set(current_names)
                taken_names = [n for n in prev_pack if n not in current_set]

        self.pack_tab.update_predictions(
            results, art_paths,
            taken_names=taken_names,
            pack_number=pack_number,
            pick_number=pick_number,
        )
        self.deck_tab.set_art_paths(art_paths)
        self.pack_tab.set_context(set_code, pack_number, pick_number, pool_size)

        # Cache for compact-view refresh and retranslation.
        self._last_results = list(results)
        self._last_art_paths = dict(art_paths) if art_paths else {}
        self._last_pool_size = pool_size
        self._last_set_code = set_code
        self._last_pack_number = pack_number
        self._last_pick_number = pick_number
        if self._compact:
            self._refresh_mini()
        self._refresh_mini_pill()

    @Slot(object)
    def _on_pool_analysis(self, analysis: PoolAnalysis) -> None:
        self.pack_tab.update_pool(analysis)
        self.pack_tab.update_color_pips(analysis.pip_totals)

    @Slot(object)
    def _on_signals(self, result: SignalResult | None) -> None:
        self.pack_tab.update_signals(result)

    @Slot(dict, list, dict)
    def _on_deck_suggestions(
        self,
        suggestions: dict[str, DeckSuggestion],
        pool_names: list[str],
        scryfall_cards: dict | None = None,
    ) -> None:
        self._deck_suggestions = suggestions
        if scryfall_cards:
            self._scryfall_cards = scryfall_cards
            self.pack_tab.set_scryfall(scryfall_cards)

        self.deck_tab.update_suggestions(suggestions, pool_names, scryfall_cards)

        # Update pack tab with the best deck suggestion (by score, not key order).
        if suggestions:
            best_key = max(suggestions, key=lambda k: suggestions[k].score)
            self.pack_tab.update_deck_stats(suggestions[best_key])
            # The summary tab's deck section fills in when suggestions land
            # (they arrive async after draft completion).
            self.summary_tab.set_best_suggestion(
                suggestions[best_key], pool_names,
            )

    def _on_archetype_changed(self, key: str) -> None:
        """When user selects a different archetype in the Deck tab, update Pack stats."""
        sug = self._deck_suggestions.get(key)
        if sug:
            self.pack_tab.update_deck_stats(sug)

    # -- compact / full toggle -----------------------------------------------

    @property
    def _compact(self) -> bool:
        return self._view_mode.is_compact

    def set_recommend_count(self, count: int) -> None:
        """Track the format's recommendation count (PickTwo recommends 2)."""
        self._recommend_count = max(1, count)
        self.pack_tab.set_recommend_count(count)

    def _toggle_compact(self) -> None:
        """User-initiated toggle between full view and the compact strip."""
        self._view_mode.toggle()

    def _apply_view_mode(self, old: ViewMode, new: ViewMode) -> None:
        """Visually switch modes; per-mode geometry is saved and restored."""
        self._view_mode.save_geometry(
            old, bytes(self.saveGeometry().toBase64()).decode(),
        )
        if new is ViewMode.COMPACT:
            self.tabs.setVisible(False)
            self.status.setVisible(False)
            self._mini_container.setVisible(True)
            self.pack_tab.set_compact(True)
            self._toggle_btn.setText("▴")
            self._refresh_mini()
            self.setMinimumWidth(460)
            self.setMaximumWidth(720)
            stored = self._view_mode.geometry_for(new)
            if stored:
                self.restoreGeometry(QByteArray.fromBase64(stored.encode()))
            else:
                self.resize(self.width(), self._compact_height())
            self.setFixedHeight(self._compact_height())
        else:
            self.setMinimumHeight(0)
            self.setMaximumHeight(16777215)  # QWIDGETSIZE_MAX
            self._mini_container.setVisible(False)
            self.pack_tab.set_compact(False)
            self.tabs.setVisible(True)
            self._toggle_btn.setText("▾")
            self.setMinimumWidth(560)
            self.setMaximumWidth(720)
            stored = self._view_mode.geometry_for(new)
            if stored:
                self.restoreGeometry(QByteArray.fromBase64(stored.encode()))
            else:
                self.resize(620, 800)

    def persist_geometry(self) -> None:
        """Save the active mode's geometry (called at app exit)."""
        geometry = bytes(self.saveGeometry().toBase64()).decode()
        self._view_mode.save_geometry(self._view_mode.mode, geometry)
        if not self._compact:
            # Keep the legacy single slot in sync for config downgrades.
            self._config.overlay.geometry = geometry

    def _compact_height(self) -> int:
        """Compact height = drag row + mini pill + N rows + padding."""
        _DRAG = 32       # drag row fixed height
        _PILL = 20       # mini pill + small margin
        _ROW = 30        # CardRow fixed height
        _SPACING = 2 * 4 # inner spacing
        _PADDING = 8     # root padding
        rows = max(3, self._recommend_count)
        return _DRAG + _PILL + rows * _ROW + _SPACING + _PADDING

    # -- keyboard shortcuts (window-scope only) --------------------------------

    def _init_shortcuts(self) -> None:
        self._shortcuts: list[QShortcut] = []

        def add(sequence: str, handler) -> None:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(handler)
            self._shortcuts.append(shortcut)

        add("Ctrl+M", self._shortcut_toggle_compact)
        add("Ctrl+H", self._minimize_to_tray)
        for number, idx_attr in enumerate(
            ("_tab_pack_idx", "_tab_deck_idx", "_tab_summary_idx",
             "_tab_settings_idx"),
            start=1,
        ):
            add(f"Ctrl+{number}",
                lambda attr=idx_attr: self._shortcut_goto_tab(attr))
        add("Esc", self._shortcut_exit_compact)

    def _shortcut_toggle_compact(self) -> None:
        # Compact mode is gated on an active draft (same as the button).
        # isVisibleTo: the button's own flag, independent of whether the
        # window itself is currently shown.
        if (
            not self._toggle_btn.isVisibleTo(self)
            or not self._toggle_btn.isEnabled()
        ):
            return
        self._view_mode.toggle()

    def _shortcut_goto_tab(self, idx_attr: str) -> None:
        if self._compact or not self.tabs.isVisibleTo(self):
            return
        index = getattr(self, idx_attr)
        if self.tabs.isTabVisible(index):
            self.tabs.setCurrentIndex(index)

    def _shortcut_exit_compact(self) -> None:
        if self._view_mode.is_compact:
            self._view_mode.set_mode(ViewMode.FULL)

    def _refresh_mini_pill(self) -> None:
        """Sync the compact-mode context pill with the current pack/pick context."""
        if self._last_set_code:
            text = (
                f"P{self._last_pack_number + 1}·P{self._last_pick_number + 1}"
                f"  ·  {self._last_set_code}  ·  pool {self._last_pool_size}"
            )
        else:
            text = ""
        self._mini_pill.setText(text)

    def _refresh_mini(self) -> None:
        """Populate the mini container with the top picks.

        Shows ``max(3, recommend_count)`` rows so PickTwo (which
        recommends 2 cards) never hides a recommended pick; the first
        ``recommend_count`` rows carry the ``recommended`` property.
        """
        from client.overlay.ui.pack_tab import CardRow

        self._refresh_mini_pill()

        # Clear old rows.
        while self._mini_layout.count():
            item = self._mini_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        row_count = max(3, self._recommend_count)
        top_picks = self._last_results[:row_count]
        if not top_picks:
            return

        max_score = top_picks[0].score if top_picks else 1.0

        # GIH% medal ranks (computed from full results, not just the top rows).
        gihwr_ranks: dict[str, int] = {}
        valid_gihwr = [(p.card, p.gihwr) for p in self._last_results if p.gihwr > 0]
        valid_gihwr.sort(key=lambda x: x[1], reverse=True)
        for rank_idx, (card_name, _) in enumerate(valid_gihwr[:3], start=1):
            gihwr_ranks[card_name] = rank_idx

        for index, p in enumerate(top_picks):
            row = CardRow(self._mini_rows_widget, show_stats=True, show_art=self._show_art)
            art = self._last_art_paths.get(p.card)
            row.set_data(
                p, max_score,
                art_path=art,
                gihwr_rank=gihwr_ranks.get(p.card, 0),
            )
            row.setProperty("recommended", index < self._recommend_count)
            row.setMouseTracking(True)
            row.enterEvent = self._make_mini_enter(p.card)
            row.leaveEvent = self._make_mini_leave()
            self._mini_layout.addWidget(row)

    # -- compact hover preview -----------------------------------------------

    def _make_mini_enter(self, card_name: str):
        def _enter(event):
            art = self._last_art_paths.get(card_name)
            if art and self._show_art:
                self._mini_pending_art = art
                self._mini_pending_pos = QCursor.pos()
                self._mini_hover_timer.start()
        return _enter

    def _make_mini_leave(self):
        def _leave(event):
            self._mini_hover_timer.stop()
            if self._mini_preview:
                self._mini_preview.hide()
        return _leave

    def _show_mini_preview(self) -> None:
        from client.overlay.ui.pack_tab import _CardPreview
        if self._mini_preview is None:
            self._mini_preview = _CardPreview()
        self._mini_preview.show_art(self._mini_pending_art, self._mini_pending_pos)
