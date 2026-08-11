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


@pytest.fixture()
def no_sleep(monkeypatch):
    """Record sleep durations without actually blocking the test."""
    slept: list[float] = []
    monkeypatch.setattr(card_art.time, "sleep", lambda s: slept.append(s))
    return slept


def test_fetch_retries_on_429_then_succeeds(monkeypatch, tmp_path, no_sleep):
    """A 429 must be retried with a delay, not treated as a permanent miss."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, content=b"\xff\xd8jpegbytes")

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(card_art.httpx, "Client", fake_client)

    cache = card_art.CardArtCache(cache_dir=tmp_path)
    path = cache.get("Desolation of Smaug")

    assert path is not None and path.exists()
    assert len(calls) == 2, "expected exactly one retry after the 429"
    # Retry-After: 2 must be honored (not the default inter-request pacing).
    assert 2.0 in no_sleep


def test_fetch_gives_up_after_max_retries_on_429(monkeypatch, tmp_path, no_sleep):
    """Persistent 429s must not retry forever — cap attempts and return None."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429)

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(card_art.httpx, "Client", fake_client)

    cache = card_art.CardArtCache(cache_dir=tmp_path)
    path = cache.get("Bofur, Reliable Guardian // Concerted Care")

    assert path is None
    assert len(calls) == card_art._MAX_ART_RETRIES + 1
    assert not (tmp_path / cache._path_for("Bofur, Reliable Guardian // Concerted Care").name).exists()


def test_fetch_without_retry_after_header_uses_default_backoff(monkeypatch, tmp_path, no_sleep):
    """A 429 with no Retry-After header still backs off, using a sane default."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429)  # no Retry-After header
        return httpx.Response(200, content=b"\xff\xd8jpegbytes")

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(card_art.httpx, "Client", fake_client)

    cache = card_art.CardArtCache(cache_dir=tmp_path)
    path = cache.get("Vow to Erebor")

    assert path is not None and path.exists()
    assert len(calls) == 2
    assert no_sleep, "must still back off even without a Retry-After header"
