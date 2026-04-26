"""Home tab — displays Arena/server status, login UI, and waits for a draft."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from client.overlay.i18n import tr
from client.overlay.log_watcher import find_log_path

logger = logging.getLogger(__name__)

# How often (ms) to poll for Arena process / log file / server health.
_POLL_INTERVAL_MS = 3000


def _is_arena_running() -> bool:
    """Check whether the MTG Arena process is running."""
    from client.overlay.log_watcher import is_arena_running
    return is_arena_running()


def _log_file_exists() -> bool:
    """Check whether the Arena Player.log file exists on disk."""
    return find_log_path() is not None


def _detailed_logs_enabled() -> bool | None:
    """Check whether detailed logging is enabled in Arena's Player.log.

    Returns:
        True if enabled, False if explicitly disabled, None if cannot determine.
    """
    path = find_log_path()
    if path is None:
        return None
    try:
        # Read just the first ~4KB — the flag appears near the top.
        with open(path, errors="replace") as f:
            head = f.read(4096)
        if "DETAILED LOGS: DISABLED" in head:
            return False
        if "DETAILED LOGS: ENABLED" in head:
            return True
    except Exception:
        logger.debug("Failed to read log for detailed-logs check", exc_info=True)
    return None


class _StatusRow(QWidget):
    """A single status indicator: coloured dot + label."""

    _DOT_STYLES = {
        "ok":   "color: #4CAF50; font-size: 14px; background: transparent;",
        "warn": "color: #FFC107; font-size: 14px; background: transparent;",
        "err":  "color: #F44336; font-size: 14px; background: transparent;",
    }

    def __init__(self, label_key: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 4, 12, 4)
        row.setSpacing(10)

        self._dot = QLabel("●")
        self._dot.setFixedWidth(20)
        self._dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self._dot)

        self._label = QLabel(tr(label_key))
        self._label.setObjectName("statusRowLabel")
        self._label.setStyleSheet("font-size: 13px; color: #ccccdd;")
        self._label_key = label_key
        row.addWidget(self._label, stretch=1)

        self._status_label = QLabel("")
        self._status_label.setStyleSheet("font-size: 12px; color: #888888;")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        row.addWidget(self._status_label)

        self.set_status("err")

    def set_status(self, status: str, detail: str = "") -> None:
        """Update the dot colour and optional detail text.

        Args:
            status: One of ``"ok"``, ``"warn"``, ``"err"``.
            detail: Short text shown to the right (e.g. "Running").
        """
        self._dot.setStyleSheet(self._DOT_STYLES.get(status, self._DOT_STYLES["err"]))
        self._status_label.setText(detail)

    def retranslate(self) -> None:
        self._label.setText(tr(self._label_key))


class HomeTab(QWidget):
    """Landing screen shown before a draft starts.

    Periodically checks:
    - Whether MTG Arena is running (process check).
    - Whether the Arena Player.log exists (log file check).
    - Whether the NemeDraft server is reachable and the user is authenticated.

    Emits :attr:`login_google_requested` / :attr:`login_microsoft_requested`
    when the user clicks a sign-in button, and :attr:`logout_requested` when
    they click "Log out".  The parent app should connect these to the
    :class:`overlay.auth_client.AuthClient`.
    """

    login_google_requested = Signal()
    login_microsoft_requested = Signal()
    logout_requested = Signal()
    simulator_detected = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)

        # Brand lockup (serif wordmark).
        self._brand_label = QLabel(tr("home_title"))
        self._brand_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._brand_label.setStyleSheet(
            "font-family: Georgia, serif; color: #cfb53b;"
            " font-size: 22px; font-weight: 700; letter-spacing: 1px;"
            " padding: 14px 0 2px 0;"
        )
        layout.addWidget(self._brand_label)

        self._subtitle = QLabel(tr("home_subtitle"))
        self._subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._subtitle.setStyleSheet(
            "font-size: 11px; color: #888888; padding: 0 0 14px 0;"
        )
        self._subtitle.setWordWrap(True)
        layout.addWidget(self._subtitle)

        # Status card — rounded container with 4 rows.
        from PySide6.QtWidgets import QFrame
        self._status_card = QFrame()
        self._status_card.setStyleSheet(
            "QFrame { background: rgba(20,20,36,.6); border-radius: 6px; }"
        )
        sc_layout = QVBoxLayout(self._status_card)
        sc_layout.setContentsMargins(0, 4, 0, 4)
        sc_layout.setSpacing(0)

        self._arena_row = _StatusRow("home_arena_status")
        self._log_row = _StatusRow("home_log_status")
        self._server_row = _StatusRow("home_server_status")
        self._draft_row = _StatusRow("home_draft_status")
        for row in (self._arena_row, self._log_row, self._server_row, self._draft_row):
            sc_layout.addWidget(row)
        layout.addWidget(self._status_card)

        # --- Login section (visible when not authenticated) ---
        self._login_section = QWidget()
        login_layout = QVBoxLayout(self._login_section)
        login_layout.setContentsMargins(20, 12, 20, 4)
        login_layout.setSpacing(8)

        self._login_prompt = QLabel(tr("home_login_prompt"))
        self._login_prompt.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._login_prompt.setStyleSheet("font-size: 13px; color: #ccccdd; padding: 4px 0;")
        login_layout.addWidget(self._login_prompt)

        _BTN_STYLE = (
            "QPushButton { font-size: 13px; padding: 8px 16px; border-radius: 4px; "
            "border: 1px solid #444466; background: %s; color: #ffffff; } "
            "QPushButton:hover { background: %s; }"
        )

        self._google_btn = QPushButton("  " + tr("home_login_google"))
        self._google_btn.setStyleSheet(_BTN_STYLE % ("#4285F4", "#3367D6"))
        self._google_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._google_btn.clicked.connect(self.login_google_requested.emit)
        login_layout.addWidget(self._google_btn)

        self._microsoft_btn = QPushButton("  " + tr("home_login_microsoft"))
        self._microsoft_btn.setStyleSheet(_BTN_STYLE % ("#2F2F2F", "#444444"))
        self._microsoft_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._microsoft_btn.clicked.connect(self.login_microsoft_requested.emit)
        login_layout.addWidget(self._microsoft_btn)

        self._login_error = QLabel("")
        self._login_error.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._login_error.setStyleSheet("font-size: 11px; color: #F44336;")
        self._login_error.setWordWrap(True)
        self._login_error.hide()
        login_layout.addWidget(self._login_error)

        layout.addWidget(self._login_section)

        # --- Logged-in section (visible when authenticated) ---
        self._loggedin_section = QWidget()
        loggedin_layout = QVBoxLayout(self._loggedin_section)
        loggedin_layout.setContentsMargins(20, 8, 20, 4)
        loggedin_layout.setSpacing(6)

        self._user_label = QLabel("")
        self._user_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._user_label.setStyleSheet("font-size: 12px; color: #4CAF50;")
        loggedin_layout.addWidget(self._user_label)

        self._vip_badge = QLabel("")
        self._vip_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._vip_badge.setStyleSheet(
            "font-size: 12px; font-weight: bold; color: #cfb53b; padding: 2px 0;"
        )
        self._vip_badge.hide()
        loggedin_layout.addWidget(self._vip_badge)

        self._logout_btn = QPushButton(tr("home_logout_btn"))
        self._logout_btn.setStyleSheet(
            "QPushButton { font-size: 11px; padding: 4px 12px; border-radius: 3px; "
            "border: 1px solid #555; background: transparent; color: #888888; } "
            "QPushButton:hover { color: #F44336; border-color: #F44336; }"
        )
        self._logout_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._logout_btn.clicked.connect(self.logout_requested.emit)
        logout_row = QHBoxLayout()
        logout_row.addStretch()
        logout_row.addWidget(self._logout_btn)
        logout_row.addStretch()
        loggedin_layout.addLayout(logout_row)

        self._loggedin_section.hide()
        layout.addWidget(self._loggedin_section)

        layout.addStretch(1)

        # Waiting hint at the bottom.
        self._hint = QLabel(tr("home_hint"))
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint.setStyleSheet("font-size: 11px; color: #666688; padding: 8px 0;")
        self._hint.setWordWrap(True)
        layout.addWidget(self._hint)

        # Internal state.
        self._server_reachable = False
        self._authenticated = False
        self._is_vip = False
        self._draft_active = False
        self._log_found = False
        self._has_arena_player_id = False
        self._draft_loading_detail: str = ""
        self._draft_untrained = False
        self._lobby_ready = False
        self._unsupported_format = False
        self._simulator_active = False
        self._maintenance = False

        # Periodic poll timer.
        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

        # Run an immediate check.
        self._poll()

    # -- public API ----------------------------------------------------------

    def set_server_status(
        self,
        reachable: bool,
        authenticated: bool,
        email: str = "",
        is_vip: bool = False,
        *,
        has_arena_player_id: bool = False,
        maintenance: bool = False,
    ) -> None:
        """Called by the main app to update server/auth state."""
        self._server_reachable = reachable
        self._authenticated = authenticated
        self._is_vip = is_vip
        self._has_arena_player_id = has_arena_player_id
        self._maintenance = maintenance
        self._update_server_status(email, is_vip=is_vip)
        # Refresh the draft row so the "VIP required" warning clears
        # immediately when VIP is granted (and vice-versa).
        self._update_draft_status()

    def set_draft_active(self, active: bool) -> None:
        """Called when a draft start/end event is detected."""
        self._draft_active = active
        if not active:
            self._draft_loading_detail = ""
            self._draft_untrained = False
        self._lobby_ready = False
        self._update_draft_status()

    def set_draft_loading(self, detail: str) -> None:
        """Show a yellow dot with a loading step message.

        Args:
            detail: Short description of what is being loaded, e.g.
                ``"Loading TMT card data..."``.  Pass ``""`` to clear.
        """
        self._draft_loading_detail = detail
        self._update_draft_status()

    def set_draft_untrained(self, untrained: bool) -> None:
        """Flag the draft session as using an untrained model.

        When *untrained* is True the draft row stays yellow with a
        warning that the experience may be suboptimal.
        """
        self._draft_untrained = untrained
        self._update_draft_status()

    def set_lobby_ready(self, ready: bool) -> None:
        """Show a green dot when set data has been pre-loaded from lobby."""
        self._lobby_ready = ready
        self._update_draft_status()

    def set_unsupported_format(self, unsupported: bool) -> None:
        """Show a red dot when the draft format is not supported."""
        self._unsupported_format = unsupported
        self._update_draft_status()

    def show_login_error(self, message: str) -> None:
        """Display an error message in the login section."""
        self._login_error.setText(message)
        self._login_error.show()

    def set_login_buttons_enabled(self, enabled: bool) -> None:
        """Enable or disable login buttons (e.g. while login is in progress)."""
        self._google_btn.setEnabled(enabled)
        self._microsoft_btn.setEnabled(enabled)

    def retranslate(self) -> None:
        """Refresh all labels after a language change."""
        self._brand_label.setText(tr("home_title"))
        self._subtitle.setText(tr("home_subtitle"))
        self._hint.setText(tr("home_hint"))
        self._login_prompt.setText(tr("home_login_prompt"))
        self._google_btn.setText("  " + tr("home_login_google"))
        self._microsoft_btn.setText("  " + tr("home_login_microsoft"))
        self._logout_btn.setText(tr("home_logout_btn"))
        self._arena_row.retranslate()
        self._log_row.retranslate()
        self._server_row.retranslate()
        self._draft_row.retranslate()

    # -- internal ------------------------------------------------------------

    def _poll(self) -> None:
        """Check Arena process and log file status."""
        # Check for draft simulator before the real Arena process.
        sim_lock = self._check_simulator()
        if sim_lock:
            log_path = sim_lock.get("log_path", "")
            self._arena_row.set_status("ok", "Simulator")
            if log_path and Path(log_path).exists():
                self._log_row.set_status("ok", "Simulator log")
                self._log_found = True
            else:
                self._log_row.set_status("err", tr("home_status_not_found"))
            if not self._simulator_active:
                self._simulator_active = True
                self.simulator_detected.emit(log_path)
        else:
            self._simulator_active = False
            arena_running = _is_arena_running()
            log_exists = _log_file_exists()
            self._log_found = log_exists

            if arena_running:
                self._arena_row.set_status("ok", tr("home_status_running"))
            else:
                self._arena_row.set_status("err", tr("home_status_not_running"))

            if log_exists:
                detailed = _detailed_logs_enabled()
                if detailed is False:
                    self._log_row.set_status("warn", tr("home_status_detailed_disabled"))
                else:
                    self._log_row.set_status("ok", tr("home_status_found"))
            else:
                self._log_row.set_status("err", tr("home_status_not_found"))

        self._update_login_visibility()
        self._update_draft_status()

    def _check_simulator(self) -> dict | None:
        """Check whether the draft simulator is running via its lock file."""
        try:
            from client.simulator.main import read_simulator_lock
        except ImportError:
            return None

        return read_simulator_lock()

    def _update_login_visibility(self) -> None:
        """Show or hide the login section based on log + auth + player ID state."""
        if self._authenticated:
            self._login_section.hide()
            self._loggedin_section.show()
        elif self._log_found and self._has_arena_player_id:
            self._login_section.show()
            self._loggedin_section.hide()
        else:
            # No Player.log or no player ID → hide sign-in buttons
            self._login_section.hide()
            self._loggedin_section.hide()

    def _update_server_status(self, email: str = "", *, is_vip: bool = False) -> None:
        if self._server_reachable and self._maintenance:
            # Yellow dot + translated label when the server is quiescing.
            # We still keep the logged-in section visible if authed so the
            # user knows their session is fine — maintenance only blocks
            # *new* drafts, not ongoing ones.
            self._server_row.set_status("warn", tr("home_status_maintenance"))
            if self._authenticated:
                self._user_label.setText(f"Logged in as {email}" if email else "Logged in")
                if is_vip:
                    self._vip_badge.setText("\u2B50 VIP Member")
                    self._vip_badge.show()
                else:
                    self._vip_badge.hide()
                self._login_error.hide()
        elif self._authenticated:
            self._server_row.set_status("ok", f"Connected · {email}" if email else "Connected")
            self._user_label.setText(f"Logged in as {email}" if email else "Logged in")
            if is_vip:
                self._vip_badge.setText("\u2B50 VIP Member")
                self._vip_badge.show()
            else:
                self._vip_badge.hide()
            self._login_error.hide()
        elif self._server_reachable:
            if self._has_arena_player_id:
                self._server_row.set_status("warn", tr("home_status_not_signed_in"))
            else:
                self._server_row.set_status("warn", tr("home_status_no_player_id"))
        else:
            self._server_row.set_status("err", "Server unavailable")

        self._update_login_visibility()

    def _update_draft_status(self) -> None:
        if self._unsupported_format:
            self._draft_row.set_status("err", tr("home_status_unsupported_format"))
        elif self._draft_loading_detail:
            self._draft_row.set_status("warn", self._draft_loading_detail)
        elif self._draft_untrained:
            self._draft_row.set_status(
                "ok", tr("home_status_untrained"),
            )
        elif self._draft_active:
            if self._authenticated and self._is_vip:
                self._draft_row.set_status("ok", tr("home_status_active"))
            else:
                self._draft_row.set_status("warn", tr("home_status_active_no_vip"))
        elif self._lobby_ready:
            self._draft_row.set_status("ok", tr("home_status_ready"))
        else:
            self._draft_row.set_status("err", tr("home_status_waiting"))
