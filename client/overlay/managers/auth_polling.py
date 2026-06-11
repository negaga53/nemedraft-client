"""Server health / auth polling and the OAuth login lifecycle.

The 30s status poll used to run synchronous HTTP on the Qt main thread
(health + user-info + token refresh — several seconds of UI freeze when
the server is down); it now runs on a worker QThread. The manager emits
signals only; ``OverlayApp`` bridges them to the home tab.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from client.overlay.api_client import NemeDraftClient
from client.overlay.auth_client import AuthClient
from client.overlay.managers.worker_pool import WorkerPool

logger = logging.getLogger("overlay")


@dataclass(frozen=True)
class ServerStatus:
    """Snapshot of server reachability and auth state for the home tab."""

    reachable: bool
    authenticated: bool
    email: str
    is_vip: bool
    has_arena_player_id: bool
    maintenance: bool
    supported_sets: list[str] = field(default_factory=list)


class _StatusPollWorker(QThread):
    """Runs one status poll (health, token refresh, VIP sync) off-thread."""

    finished_status = Signal(object)  # ServerStatus

    def __init__(
        self,
        api_client: NemeDraftClient,
        auth_client: AuthClient,
        has_arena_player_id: bool,
    ) -> None:
        super().__init__()
        self._api_client = api_client
        self._auth_client = auth_client
        self._has_arena_player_id = has_arena_player_id

    def run(self) -> None:  # noqa: D401
        api = self._api_client
        auth = self._auth_client

        health = api.health()
        reachable = health is not None
        maintenance = bool(health.get("maintenance")) if health else False

        # Auto-refresh expired tokens so the sign-in button doesn't
        # reappear when the user returns from a long draft.
        if (
            self._has_arena_player_id
            and auth.session
            and auth.session.is_expired
        ):
            auth.refresh()
        authed = auth.is_authenticated

        # Ask the server for the current VIP status so upgrades made
        # since the JWT was issued (and stale claims from a restored
        # session) are picked up.  On failure we keep the cached value.
        if authed and reachable and auth.session is not None:
            info = api.fetch_user_info()
            if info is not None and info.is_vip != auth.session.is_vip:
                old_is_vip = auth.session.is_vip
                # /api/me reflects the live DB value, but the JWT in
                # session.token has is_vip baked in at login time —
                # /api/predict trusts the JWT claim, so a mid-session
                # VIP promotion (or revocation) leaves the gate stale
                # until the JWT is re-issued. Refresh now so the next
                # predict call sees the new claim.
                refreshed = auth.refresh()
                if refreshed is None:
                    # No refresh token, or Supabase/server unreachable:
                    # mirror the value locally so the home tab UI is
                    # accurate, but the JWT stays stale and predict will
                    # keep being rejected until the user signs in again.
                    auth.session.is_vip = info.is_vip
                logger.info(
                    "VIP status changed: %s -> %s (jwt refresh: %s)",
                    old_is_vip, auth.session.is_vip,
                    "ok" if refreshed else "failed",
                )

        email = auth.user_email
        is_vip = bool(authed and auth.session and auth.session.is_vip)
        supported = list(health.get("supported_sets", [])) if health else []

        self.finished_status.emit(ServerStatus(
            reachable=reachable,
            authenticated=authed,
            email=email,
            is_vip=is_vip,
            has_arena_player_id=self._has_arena_player_id,
            maintenance=maintenance,
            supported_sets=supported,
        ))


class _LoginWorker(QThread):
    """Runs one OAuth login flow off the UI thread."""

    finished_session = Signal(object)  # ServerSession | None
    error = Signal(str)

    def __init__(self, auth: AuthClient, provider: str) -> None:
        super().__init__()
        self._auth = auth
        self._provider = provider

    def run(self) -> None:  # noqa: D401
        try:
            if self._provider == "google":
                session = self._auth.login_google()
            elif self._provider == "discord":
                session = self._auth.login_discord()
            else:
                session = self._auth.login_microsoft()
            self.finished_session.emit(session)
        except Exception as exc:
            self.error.emit(str(exc))


class AuthPollingManager(QObject):
    """Owns the 30s status poll, VIP refresh, and OAuth login workers."""

    status_changed = Signal(object)       # ServerStatus
    supported_sets_changed = Signal(list)
    login_started = Signal()
    login_succeeded = Signal()
    login_failed = Signal(str)

    POLL_INTERVAL_MS = 30_000

    def __init__(
        self,
        api_client: NemeDraftClient,
        auth_client: AuthClient,
        *,
        has_player_id: Callable[[], bool],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._api_client = api_client
        self._auth_client = auth_client
        self._has_player_id = has_player_id
        self._pool = WorkerPool()
        self._poll_inflight = False
        self._login_worker: _LoginWorker | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(self.POLL_INTERVAL_MS)
        self._timer.timeout.connect(self.poll_now)

    def start(self) -> None:
        self._timer.start()
        self.poll_now()

    def stop(self) -> None:
        self._timer.stop()

    def is_vip(self) -> bool:
        """Return True if the user is authenticated with VIP status."""
        if not self._auth_client.is_authenticated:
            return False
        session = self._auth_client.session
        return bool(session and session.is_vip)

    # -- status polling ----------------------------------------------------

    def poll_now(self) -> None:
        """Run one status poll on a background worker."""
        if self._poll_inflight:
            return
        self._poll_inflight = True
        worker = _StatusPollWorker(
            self._api_client, self._auth_client, self._has_player_id(),
        )
        worker.finished_status.connect(self._on_poll_done)
        self._pool.launch(worker)

    def _on_poll_done(self, status: object) -> None:
        self._poll_inflight = False
        if not isinstance(status, ServerStatus):
            return
        self.status_changed.emit(status)
        if status.supported_sets:
            self.supported_sets_changed.emit(status.supported_sets)

    # -- login / logout ------------------------------------------------------

    def login(self, provider: str) -> None:
        """Run OAuth in a background thread to avoid blocking the UI."""
        # Cancel any in-flight login first
        if self._login_worker is not None and self._login_worker.isRunning():
            self._auth_client.cancel_login()
            self._login_worker.wait(2000)

        self.login_started.emit()
        worker = _LoginWorker(self._auth_client, provider)
        worker.finished_session.connect(self._on_login_done)
        worker.error.connect(self._on_login_error)
        self._login_worker = worker
        self._pool.launch(worker)

    def _on_login_done(self, session: object) -> None:
        if session is None:
            self.login_failed.emit("Login failed — please try again")
            return
        self.poll_now()
        self.login_succeeded.emit()

    def _on_login_error(self, msg: str) -> None:
        self.login_failed.emit(f"Login error: {msg}")

    def logout(self) -> None:
        self._auth_client.logout()
        self.poll_now()
