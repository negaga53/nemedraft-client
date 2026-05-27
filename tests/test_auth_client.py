"""Tests for AuthClient server-token exchange behaviour."""

from __future__ import annotations

from unittest.mock import patch

import httpx

from client.overlay.auth_client import AuthClient
from client.overlay.env import ClientEnv


def _make_env() -> ClientEnv:
    return ClientEnv(
        server_url="http://localhost",
        server_port=9000,
        supabase_url="http://localhost",
        supabase_anon_key="anon-key",
        saved_server_token="",
        saved_supabase_refresh_token="",
        saved_user_email="",
    )


def test_exchange_timeout_exceeds_server_worst_case():
    """The /api/login read timeout must outlast the server's worst case.

    Server-side /api/login fans out to two sequential-capable Supabase
    round-trips (upsert + VIP lookup), each with a 10s budget. A client
    timeout <= the server's worst case makes the client abandon a request
    the server is still legitimately processing (observed ReadTimeout on a
    cold post-restart connection pool). The client must wait at least 30s.
    """
    client = AuthClient(_make_env(), arena_player_id="ARENA_X")

    captured: dict[str, object] = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["timeout"] = kwargs.get("timeout")
        raise httpx.ReadTimeout("simulated")

    with patch("client.overlay.auth_client.httpx.post", new=fake_post):
        result = client._exchange_for_server_token("dummy-supabase-token")

    assert result is None  # ReadTimeout is caught, not raised
    assert captured["url"] == "http://localhost:9000/api/login"
    assert isinstance(captured["timeout"], (int, float))
    assert captured["timeout"] >= 30, "client must outlast the server's 2x10s Supabase budget"
