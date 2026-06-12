"""Settings tab — user-configurable scoring weights, data source, and overlay appearance."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from client.overlay.card_art import CardArtCache
from client.overlay.config import LOG_FILE, OverlayConfig
from client.overlay.i18n import LANGUAGE_NAMES, Translator, tr


class SettingsTab(QWidget):
    """Settings panel that mutates an :class:`OverlayConfig` in place.

    Emits :pyattr:`settings_changed` whenever the user adjusts a value.
    """

    settings_changed = Signal()
    setting_changed = Signal(str, object)  # ("overlay.show_art", True) per changed key.
    language_changed = Signal(str)  # Emitted with new language code.
    opacity_preview = Signal(float)  # Live opacity during slider drag (0.0–1.0).

    def __init__(
        self,
        config: OverlayConfig,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._cfg = config

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(scroll.Shape.NoFrame)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)

        # -- Data section ----------------------------------------------------
        self._sec_data = self._section(tr("section_data"))
        layout.addWidget(self._sec_data)

        ug_row = QHBoxLayout()
        self._ug_lbl = QLabel(tr("player_group_label"))
        self._ug_lbl.setToolTip(tr("player_group_tooltip"))
        ug_row.addWidget(self._ug_lbl)
        self._ug_combo = QComboBox()
        self._ug_combo.addItems(["All", "Platinum+", "Diamond+", "Mythic"])
        self._ug_combo.setCurrentText(config.data.user_group)
        ug_row.addWidget(self._ug_combo, stretch=1)
        layout.addLayout(ug_row)

        # -- Overlay section -------------------------------------------------
        self._sec_overlay = self._section(tr("section_overlay"))
        layout.addWidget(self._sec_overlay)

        opacity_row = QHBoxLayout()
        self._opacity_lbl = QLabel(tr("opacity_label"))
        self._opacity_lbl.setToolTip(tr("opacity_tooltip"))
        opacity_row.addWidget(self._opacity_lbl)
        self._opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self._opacity_slider.setRange(30, 100)
        self._opacity_slider.setValue(int(config.overlay.opacity * 100))
        self._opacity_label = QLabel(f"{int(config.overlay.opacity * 100)}%")
        self._opacity_label.setFixedWidth(36)
        opacity_row.addWidget(self._opacity_slider, stretch=1)
        opacity_row.addWidget(self._opacity_label)
        layout.addLayout(opacity_row)

        self._show_art_checkbox = QCheckBox(tr("show_art_label"))
        self._show_art_checkbox.setChecked(config.overlay.show_art)
        self._show_art_checkbox.toggled.connect(self._sync)
        layout.addWidget(self._show_art_checkbox)

        self._transparent_checkbox = QCheckBox(tr("transparent_mode_label"))
        self._transparent_checkbox.setToolTip(tr("transparent_mode_tooltip"))
        self._transparent_checkbox.setChecked(config.overlay.transparent)
        self._transparent_checkbox.toggled.connect(self._sync)
        layout.addWidget(self._transparent_checkbox)

        # -- Language section ------------------------------------------------
        self._sec_language = self._section(tr("section_language"))
        layout.addWidget(self._sec_language)

        lang_row = QHBoxLayout()
        self._lang_lbl = QLabel(tr("language_label"))
        self._lang_lbl.setToolTip(tr("language_tooltip"))
        lang_row.addWidget(self._lang_lbl)
        self._lang_combo = QComboBox()
        for code, display_name in LANGUAGE_NAMES.items():
            self._lang_combo.addItem(display_name, code)
        # Select current language.
        current_idx = self._lang_combo.findData(config.display.language)
        if current_idx >= 0:
            self._lang_combo.setCurrentIndex(current_idx)
        lang_row.addWidget(self._lang_combo, stretch=1)
        layout.addLayout(lang_row)

        # -- Maintenance section --------------------------------------------
        self._sec_maintenance = self._section(tr("section_maintenance"))
        layout.addWidget(self._sec_maintenance)

        cache_row = QHBoxLayout()
        self._cache_info = QLabel()
        self._cache_info.setObjectName("settingsCaption")
        cache_row.addWidget(self._cache_info, stretch=1)
        self._clear_cache_btn = QPushButton(tr("clear_cache_btn"))
        self._clear_cache_btn.setMinimumWidth(110)
        self._clear_cache_btn.clicked.connect(self._on_clear_cache)
        cache_row.addWidget(self._clear_cache_btn)
        layout.addLayout(cache_row)
        self._update_cache_label()

        logs_row = QHBoxLayout()
        logs_lbl = QLabel(tr("export_logs_label"))
        logs_lbl.setObjectName("settingsCaption")
        logs_row.addWidget(logs_lbl, stretch=1)
        self._export_logs_btn = QPushButton(tr("export_logs_btn"))
        self._export_logs_btn.setMinimumWidth(110)
        self._export_logs_btn.clicked.connect(self._on_export_logs)
        logs_row.addWidget(self._export_logs_btn)
        layout.addLayout(logs_row)

        layout.addStretch()
        scroll.setWidget(inner)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(scroll)

        # Connect signals.
        self._ug_combo.currentTextChanged.connect(self._sync)
        # Live preview while dragging — no persist, no re-predict.
        self._opacity_slider.valueChanged.connect(self._on_opacity_preview)
        # Persist + re-render only when the user releases the slider.
        self._opacity_slider.sliderReleased.connect(self._on_opacity_committed)
        self._lang_combo.currentIndexChanged.connect(self._on_language_changed)

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _section(title: str) -> QLabel:
        lbl = QLabel(title)
        lbl.setObjectName("sectionTitle")
        return lbl

    def _on_opacity_preview(self, value: int) -> None:
        """Update the label + emit a lightweight preview signal while dragging."""
        self._opacity_label.setText(f"{value}%")
        self.opacity_preview.emit(value / 100.0)

    def _on_opacity_committed(self) -> None:
        """Persist opacity once the slider is released (avoids per-tick save + predict)."""
        self._sync()

    def _on_language_changed(self, index: int) -> None:
        code = self._lang_combo.itemData(index)
        if code and code != self._cfg.display.language:
            self._cfg.display.language = code
            translator = Translator.instance()
            translator.set_language(code)
            self.language_changed.emit(code)
            self.settings_changed.emit()

    def _sync(self, *_args: object) -> None:
        """Push all widget values into the config object.

        Emits ``setting_changed`` for each key whose value actually
        changed (live-apply hooks), then the legacy blob signal for
        persistence + re-predict.
        """
        c = self._cfg
        new_values = {
            "data.user_group": self._ug_combo.currentText(),
            "overlay.opacity": self._opacity_slider.value() / 100.0,
            "overlay.show_art": self._show_art_checkbox.isChecked(),
            "overlay.transparent": self._transparent_checkbox.isChecked(),
        }
        old_values = {
            "data.user_group": c.data.user_group,
            "overlay.opacity": c.overlay.opacity,
            "overlay.show_art": c.overlay.show_art,
            "overlay.transparent": c.overlay.transparent,
        }

        c.data.user_group = new_values["data.user_group"]
        c.overlay.opacity = new_values["overlay.opacity"]
        c.overlay.show_art = new_values["overlay.show_art"]
        c.overlay.transparent = new_values["overlay.transparent"]

        for key, value in new_values.items():
            if old_values[key] != value:
                self.setting_changed.emit(key, value)

        self.settings_changed.emit()

    def _update_cache_label(self) -> None:
        art_cache = CardArtCache()
        size_bytes = art_cache.cache_size_bytes()
        if size_bytes < 1024:
            size_str = f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            size_str = f"{size_bytes / 1024:.1f} KB"
        else:
            size_str = f"{size_bytes / (1024 * 1024):.1f} MB"
        self._cache_info.setText(tr("cache_info", size=size_str))

    def _on_clear_cache(self) -> None:
        art_cache = CardArtCache()
        art_count = art_cache.clear()
        self._clear_cache_btn.setText(tr("cache_cleared", count=art_count))
        self._update_cache_label()

    def _on_export_logs(self) -> None:
        """Save the overlay log file to a user-chosen location."""
        import shutil

        dest, _ = QFileDialog.getSaveFileName(
            self,
            tr("export_logs_btn"),
            "nemedraft_logs.txt",
            "Text Files (*.txt);;All Files (*)",
        )
        if not dest:
            return
        if LOG_FILE.exists():
            shutil.copy2(LOG_FILE, dest)
        else:
            from pathlib import Path

            Path(dest).write_text("No log data available.\n", encoding="utf-8")
        self._export_logs_btn.setText(tr("logs_exported"))
        from PySide6.QtCore import QTimer

        QTimer.singleShot(
            1500, lambda: self._export_logs_btn.setText(tr("export_logs_btn")),
        )

    def retranslate(self) -> None:
        """Refresh all static labels with the current language."""
        self._sec_data.setText(tr("section_data"))
        self._ug_lbl.setText(tr("player_group_label"))
        self._ug_lbl.setToolTip(tr("player_group_tooltip"))
        self._sec_overlay.setText(tr("section_overlay"))
        self._opacity_lbl.setText(tr("opacity_label"))
        self._opacity_lbl.setToolTip(tr("opacity_tooltip"))
        self._show_art_checkbox.setText(tr("show_art_label"))
        self._transparent_checkbox.setText(tr("transparent_mode_label"))
        self._transparent_checkbox.setToolTip(tr("transparent_mode_tooltip"))
        self._sec_language.setText(tr("section_language"))
        self._lang_lbl.setText(tr("language_label"))
        self._lang_lbl.setToolTip(tr("language_tooltip"))
        self._sec_maintenance.setText(tr("section_maintenance"))
        self._clear_cache_btn.setText(tr("clear_cache_btn"))
        self._update_cache_label()
        self._export_logs_btn.setText(tr("export_logs_btn"))
