"""Toast banners — transient in-overlay notifications from the bus."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from client.overlay.notifications import Notification, Severity


class ToastBanner(QFrame):
    """One notification row; styled via objectName + ``severity`` property."""

    closed = Signal(object)  # self

    def __init__(self, notification: Notification, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("toast")
        self.setProperty("severity", notification.severity.name.lower())
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.notification = notification

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 6, 4)
        layout.setSpacing(6)

        self._label = QLabel(notification.message)
        self._label.setObjectName("toastLabel")
        self._label.setWordWrap(True)
        if notification.detail:
            self._label.setToolTip(notification.detail)
        layout.addWidget(self._label, stretch=1)

        self.close_button: QPushButton | None = None
        if notification.severity == Severity.ERROR:
            btn = QPushButton("✕")
            btn.setObjectName("toastClose")
            btn.setFixedSize(16, 16)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda: self.closed.emit(self))
            layout.addWidget(btn)
            self.close_button = btn

    def text(self) -> str:
        return self._label.text()


class ToastHost(QWidget):
    """Stacks up to ``MAX_VISIBLE`` banners; oldest is evicted first."""

    MAX_VISIBLE = 3
    DEFAULT_TIMEOUTS_MS = {
        Severity.INFO: 4_000,
        Severity.WARNING: 8_000,
        Severity.ERROR: 0,  # sticky — user dismisses
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("toastHost")
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum,
        )
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(6, 2, 6, 2)
        self._layout.setSpacing(4)
        self._banners: list[ToastBanner] = []

    def count(self) -> int:
        return len(self._banners)

    def banners(self) -> list[ToastBanner]:
        return list(self._banners)

    def show_notification(self, notification: object) -> None:
        if not isinstance(notification, Notification):
            return
        while len(self._banners) >= self.MAX_VISIBLE:
            self._remove_banner(self._banners[0])

        banner = ToastBanner(notification, self)
        banner.closed.connect(self._remove_banner)
        self._banners.append(banner)
        self._layout.addWidget(banner)
        banner.show()

        timeout = notification.timeout_ms or self.DEFAULT_TIMEOUTS_MS.get(
            notification.severity, 4_000,
        )
        if timeout > 0:
            QTimer.singleShot(timeout, lambda b=banner: self._remove_banner(b))

    def _remove_banner(self, banner: ToastBanner) -> None:
        if banner not in self._banners:
            return
        self._banners.remove(banner)
        self._layout.removeWidget(banner)
        banner.deleteLater()
