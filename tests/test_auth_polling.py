"""Tests for client.overlay.managers.auth_polling.AuthPollingManager."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field

# Must be set before any PySide6 import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@dataclass
class _FakeSession:
    email: str = "vip@example.com"
    is_vip: bool = True
    is_expired: bool = False


@dataclass
class _FakeUserInfo:
    is_vip: bool = True


class _FakeAuthClient:
    def __init__(self, session: _FakeSession | None = None) -> None:
        self.session = session
        self.refresh_calls = 0
        self.logout_calls = 0
        self.login_results: dict[str, object] = {}
        self.login_error: Exception | None = None

    @property
    def is_authenticated(self) -> bool:
        return self.session is not None and not self.session.is_expired

    @property
    def user_email(self) -> str:
        return self.session.email if self.session else ""

    def refresh(self):
        self.refresh_calls += 1
        return self.session

    def logout(self) -> None:
        self.logout_calls += 1
        self.session = None

    def cancel_login(self) -> None:
        pass

    def login_google(self):
        if self.login_error:
            raise self.login_error
        return self.login_results.get("google")

    def login_microsoft(self):
        return self.login_results.get("microsoft")

    def login_discord(self):
        return self.login_results.get("discord")


class _FakeApiClient:
    def __init__(self, *, health: dict | None = None,
                 user_info: _FakeUserInfo | None = None) -> None:
        self.health_payload = health
        self.user_info = user_info
        self.call_threads: list[int] = []

    def health(self):
        self.call_threads.append(threading.get_ident())
        return self.health_payload

    def fetch_user_info(self):
        return self.user_info


def _wait_until(predicate, qapp, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        qapp.processEvents()
        time.sleep(0.01)
    return predicate()


def _make_manager(api, auth, *, has_player_id=True):
    from client.overlay.managers.auth_polling import AuthPollingManager

    return AuthPollingManager(
        api, auth, has_player_id=lambda: has_player_id,
    )


def test_poll_emits_status_and_supported_sets_off_main_thread(qapp):
    api = _FakeApiClient(
        health={"supported_sets": ["TMT", "SOS"]},
        user_info=_FakeUserInfo(is_vip=True),
    )
    auth = _FakeAuthClient(_FakeSession())
    manager = _make_manager(api, auth)
    statuses: list[object] = []
    sets: list[list] = []
    manager.status_changed.connect(statuses.append)
    manager.supported_sets_changed.connect(sets.append)

    manager.poll_now()
    assert _wait_until(lambda: statuses, qapp)

    status = statuses[0]
    assert status.reachable is True
    assert status.authenticated is True
    assert status.email == "vip@example.com"
    assert status.is_vip is True
    assert status.has_arena_player_id is True
    assert status.maintenance is False
    assert sets == [["TMT", "SOS"]]
    assert api.call_threads[0] != threading.get_ident()


def test_poll_unreachable_server(qapp):
    api = _FakeApiClient(health=None)
    auth = _FakeAuthClient(None)
    manager = _make_manager(api, auth, has_player_id=False)
    statuses: list[object] = []
    manager.status_changed.connect(statuses.append)

    manager.poll_now()
    assert _wait_until(lambda: statuses, qapp)
    status = statuses[0]
    assert status.reachable is False
    assert status.authenticated is False
    assert status.is_vip is False


def test_vip_promotion_triggers_jwt_refresh(qapp):
    session = _FakeSession(is_vip=False)
    api = _FakeApiClient(health={}, user_info=_FakeUserInfo(is_vip=True))
    auth = _FakeAuthClient(session)
    manager = _make_manager(api, auth)
    statuses: list[object] = []
    manager.status_changed.connect(statuses.append)

    manager.poll_now()
    assert _wait_until(lambda: statuses, qapp)
    assert auth.refresh_calls == 1


def test_expired_session_refreshed_before_status(qapp):
    session = _FakeSession(is_expired=True)
    api = _FakeApiClient(health={})
    auth = _FakeAuthClient(session)
    manager = _make_manager(api, auth)
    statuses: list[object] = []
    manager.status_changed.connect(statuses.append)

    manager.poll_now()
    assert _wait_until(lambda: statuses, qapp)
    assert auth.refresh_calls >= 1


def test_is_vip(qapp):
    auth = _FakeAuthClient(_FakeSession(is_vip=True))
    manager = _make_manager(_FakeApiClient(health={}), auth)
    assert manager.is_vip() is True
    auth.session.is_vip = False
    assert manager.is_vip() is False
    auth.session = None
    assert manager.is_vip() is False


def test_login_success_emits_succeeded(qapp):
    api = _FakeApiClient(health={})
    auth = _FakeAuthClient(None)
    auth.login_results["google"] = _FakeSession()
    manager = _make_manager(api, auth)
    started: list[bool] = []
    succeeded: list[bool] = []
    failed: list[str] = []
    manager.login_started.connect(lambda: started.append(True))
    manager.login_succeeded.connect(lambda: succeeded.append(True))
    manager.login_failed.connect(failed.append)

    manager.login("google")
    assert _wait_until(lambda: succeeded, qapp)
    assert started == [True]
    assert failed == []


def test_login_returning_none_emits_failed(qapp):
    api = _FakeApiClient(health={})
    auth = _FakeAuthClient(None)
    manager = _make_manager(api, auth)
    failed: list[str] = []
    manager.login_failed.connect(failed.append)

    manager.login("google")
    assert _wait_until(lambda: failed, qapp)
    assert "failed" in failed[0].lower()


def test_login_exception_emits_failed(qapp):
    api = _FakeApiClient(health={})
    auth = _FakeAuthClient(None)
    auth.login_error = RuntimeError("oauth exploded")
    manager = _make_manager(api, auth)
    failed: list[str] = []
    manager.login_failed.connect(failed.append)

    manager.login("google")
    assert _wait_until(lambda: failed, qapp)
    assert "oauth exploded" in failed[0]


def test_logout_logs_out_and_repolls(qapp):
    api = _FakeApiClient(health={})
    auth = _FakeAuthClient(_FakeSession())
    manager = _make_manager(api, auth)
    statuses: list[object] = []
    manager.status_changed.connect(statuses.append)

    manager.logout()
    assert auth.logout_calls == 1
    assert _wait_until(lambda: statuses, qapp)
    assert statuses[-1].authenticated is False
