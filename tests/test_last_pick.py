"""Verify the overlay surfaces last_pick to the server."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from client.overlay.draft_state import DraftState


def test_on_pick_sets_last_pick():
    state = DraftState()
    state.on_pack(["Alpha", "Beta", "Gamma"], pack_number=0, pick_number=0)
    state.on_pick("Beta")
    assert state.last_pick == "Beta"


def test_reset_clears_last_pick():
    state = DraftState()
    state.on_pack(["Alpha"], pack_number=0, pick_number=0)
    state.on_pick("Alpha")
    assert state.last_pick == "Alpha"
    state.reset()
    assert state.last_pick is None


def test_api_client_predict_sends_last_pick():
    """ApiClient.predict should add `last_pick` to the request body when given."""
    from client.overlay.api_client import NemeDraftClient

    captured: dict = {}

    class StubClient(NemeDraftClient):
        def __init__(self):
            pass  # bypass real httpx setup

        def _authed_request(self, method, path, *, json=None, timeout=None):
            captured["body"] = json
            return {"picks": []}

    StubClient().predict(
        pack_cards=["A", "B"], pool_cards=[], set_code="SOS",
        pack_number=0, pick_number=1, last_pick="A",
    )
    assert captured["body"]["last_pick"] == "A"


def test_api_client_predict_omits_last_pick_when_none():
    from client.overlay.api_client import NemeDraftClient

    captured: dict = {}

    class StubClient(NemeDraftClient):
        def __init__(self):
            pass

        def _authed_request(self, method, path, *, json=None, timeout=None):
            captured["body"] = json
            return {"picks": []}

    StubClient().predict(
        pack_cards=["A"], pool_cards=[], set_code="SOS",
        pack_number=0, pick_number=0, last_pick=None,
    )
    assert "last_pick" not in captured["body"]
