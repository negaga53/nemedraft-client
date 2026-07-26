"""NemeDraftClient queue methods."""

from __future__ import annotations

import httpx
import pytest

from client.overlay.api_client import NemeDraftClient, QueueStatus, SeatStatus


class FakeAuth:
    def get_token(self) -> str:
        return "tok"

    def refresh(self):
        return None


def _client(handler) -> NemeDraftClient:
    env = type("E", (), {"server_url": "http://x", "server_port": 1})()
    c = NemeDraftClient(env, FakeAuth())
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    c._base = "http://x"
    return c


def test_queue_heartbeat_parses_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/queue/heartbeat"
        return httpx.Response(200, json={
            "state": "queued", "position": 3, "queue_length": 9,
            "active_drafters": 40, "capacity": 40,
            "offer_expires_in_s": None, "cooldown_remaining_s": 0.0,
            "eta_s": 54, "seat": None, "reason": "",
        })

    status = _client(handler).queue_heartbeat(
        want_seat=True, arena_running=True, set_code="TMT",
        draft_active=False,
    )
    assert isinstance(status, QueueStatus)
    assert status.state == "queued"
    assert status.position == 3
    assert status.eta_s == 54


def test_predict_marks_no_seat_on_409():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": {
            "error": "no_seat",
            "queue": {"state": "queued", "position": 2, "queue_length": 5,
                      "active_drafters": 40, "capacity": 40,
                      "offer_expires_in_s": None, "cooldown_remaining_s": 0.0,
                      "eta_s": 36, "seat": None, "reason": ""},
        }})

    c = _client(handler)
    picks = c.predict(["Island"], [], "TMT")
    assert picks == []
    assert c.last_no_seat is not None
    assert c.last_no_seat.state == "queued"


def test_last_no_seat_cleared_on_later_success():
    """A 409 sets last_no_seat; a subsequent 200 must clear it back to None.

    This is the clearing behaviour that has to live on the success path
    of _authed_request itself — not something predict()-specific — so
    that any later successful call (predict, signals, deck-suggestions,
    heartbeat...) clears a stale no-seat flag.
    """
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(409, json={"detail": {
                "error": "no_seat",
                "queue": {"state": "queued", "position": 1, "queue_length": 1,
                          "active_drafters": 40, "capacity": 40,
                          "offer_expires_in_s": None, "cooldown_remaining_s": 0.0,
                          "eta_s": 12, "seat": None, "reason": ""},
            }})
        return httpx.Response(200, json={"picks": []})

    c = _client(handler)
    c.predict(["Island"], [], "TMT")
    assert c.last_no_seat is not None

    c.predict(["Island"], [], "TMT")
    assert c.last_no_seat is None


def test_seated_heartbeat_parses_seat_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "state": "seated", "position": None, "queue_length": 0,
            "active_drafters": 40, "capacity": 40,
            "offer_expires_in_s": None, "cooldown_remaining_s": 0.0,
            "eta_s": None,
            "seat": {
                "shot_clock_remaining_s": 42.5,
                "shot_clock_total_s": 60.0,
                "grace_remaining_s": 5.0,
                "absent": False,
            },
            "reason": "",
        })

    status = _client(handler).queue_heartbeat(
        want_seat=True, arena_running=True, set_code="TMT",
        draft_active=True,
    )
    assert isinstance(status.seat, SeatStatus)
    assert status.seat.shot_clock_remaining_s == 42.5
    assert status.seat.shot_clock_total_s == 60.0
    assert status.seat.grace_remaining_s == 5.0
    assert status.seat.absent is False


def test_health_surfaces_aggregate_fields():
    """health() bypasses _authed_request (no auth needed); its raw dict
    already carries the three aggregate fields the queue UI needs."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/health"
        return httpx.Response(200, json={
            "status": "ok", "capacity": 40, "active_drafters": 12,
            "queue_length": 3,
        })

    c = _client(handler)
    data = c.health()
    assert data["capacity"] == 40
    assert data["active_drafters"] == 12
    assert data["queue_length"] == 3


def test_409_with_unexpected_body_does_not_raise():
    """Servers can return unexpected bodies mid-deploy (plain text, or a
    detail that isn't the no_seat shape). The client must degrade
    gracefully instead of raising."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, content=b"upstream error", headers={
            "content-type": "text/plain",
        })

    c = _client(handler)
    picks = c.predict(["Island"], [], "TMT")
    assert picks == []
    assert c.last_no_seat is None


def test_409_with_non_no_seat_detail_does_not_raise():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "something else"})

    c = _client(handler)
    picks = c.predict(["Island"], [], "TMT")
    assert picks == []
    assert c.last_no_seat is None


def test_queue_release_returns_bool():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/queue/release"
        return httpx.Response(200, json={
            "state": "idle", "position": None, "queue_length": 0,
            "active_drafters": 40, "capacity": 40,
            "offer_expires_in_s": None, "cooldown_remaining_s": 0.0,
            "eta_s": None, "seat": None, "reason": "",
        })

    assert _client(handler).queue_release() is True


def test_queue_release_false_on_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "server error"})

    assert _client(handler).queue_release() is False
