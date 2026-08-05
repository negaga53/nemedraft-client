"""CardArtCache network behaviour.

Regression cover for the June 2026 outage: ``cards.scryfall.io`` (the image
CDN that ``/cards/named?format=image`` redirects to) answers 400 Bad Request
when the request carries httpx's default ``python-httpx/x.y`` User-Agent, so
every uncached thumbnail silently failed.
"""

from __future__ import annotations

import httpx
import pytest

from client.overlay import card_art


@pytest.fixture()
def captured(monkeypatch, tmp_path):
    """Patch httpx.Client inside card_art with a MockTransport recorder."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"\xff\xd8jpegbytes")

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(card_art.httpx, "Client", fake_client)
    return seen


def test_fetch_sends_identifying_user_agent(captured, tmp_path):
    cache = card_art.CardArtCache(cache_dir=tmp_path)
    path = cache.get("Super-Skrull")

    assert path is not None and path.exists()
    assert len(captured) == 1
    ua = captured[0].headers.get("user-agent", "")
    assert "python-httpx" not in ua, (
        "Scryfall's image CDN rejects the default httpx User-Agent with 400"
    )
    assert ua.startswith("NemeDraft/")


def test_user_agent_constant_is_identifying():
    assert "python-httpx" not in card_art.USER_AGENT
    assert card_art.USER_AGENT.startswith("NemeDraft/")
