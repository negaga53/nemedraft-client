"""Headless tests for the Home tab's draft-access (admission queue) row.

Run with: QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest tests/test_home_tab.py -v
"""

from __future__ import annotations

import os

# Must be set before any PySide6 import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.mark.usefixtures("qapp")
def test_home_tab_renders_queue_states():
    from client.overlay.api_client import QueueStatus, SeatStatus
    from client.overlay.ui.home_tab import HomeTab

    tab = HomeTab()

    tab.set_queue_status(QueueStatus(
        state="queued", position=7, queue_length=23,
        active_drafters=40, capacity=40, eta_s=180,
    ))
    assert "7" in tab.queue_detail_text()
    assert tab.leave_button_visible() is True

    tab.set_queue_status(QueueStatus(
        state="offered", active_drafters=40, capacity=40,
        offer_expires_in_s=47.0,
    ))
    assert tab.offer_progress_visible() is True

    tab.set_queue_status(QueueStatus(
        state="cooldown", cooldown_remaining_s=120.0,
        active_drafters=40, capacity=40,
    ))
    assert "2:00" in tab.queue_detail_text()

    tab.set_queue_status(QueueStatus(
        state="seated", active_drafters=12, capacity=40,
        seat=SeatStatus(shot_clock_remaining_s=100.0, shot_clock_total_s=300.0),
    ))
    assert tab.leave_button_visible() is False


@pytest.mark.usefixtures("qapp")
def test_join_button_requires_intent():
    from client.overlay.api_client import QueueStatus
    from client.overlay.ui.home_tab import HomeTab

    tab = HomeTab()
    tab.set_queue_status(QueueStatus(
        state="idle", active_drafters=40, capacity=40,
    ))
    tab.set_draft_intent(arena_running=False, set_detected=False)
    assert tab.join_button_enabled() is False
    tab.set_draft_intent(arena_running=True, set_detected=True)
    assert tab.join_button_enabled() is True


@pytest.mark.usefixtures("qapp")
def test_idle_with_capacity_free_shows_ready_and_no_controls():
    """``idle`` at capacity shows the Join button; ``idle`` with room to
    spare must be visually distinct — "Ready", no controls at all — so
    the player isn't shown a Join button for a queue that doesn't exist."""
    from client.overlay.api_client import QueueStatus
    from client.overlay.ui.home_tab import HomeTab

    tab = HomeTab()
    tab.set_draft_intent(arena_running=True, set_detected=True)
    tab.set_queue_status(QueueStatus(
        state="idle", active_drafters=5, capacity=40,
    ))

    assert "Ready" in tab.queue_detail_text()
    assert tab.join_button_enabled() is False
    assert tab.leave_button_visible() is False
    assert tab.offer_progress_visible() is False


@pytest.mark.usefixtures("qapp")
def test_join_and_leave_buttons_emit_signals():
    from client.overlay.api_client import QueueStatus
    from client.overlay.ui.home_tab import HomeTab

    tab = HomeTab()

    join_calls: list[bool] = []
    leave_calls: list[bool] = []
    tab.join_queue_requested.connect(lambda: join_calls.append(True))
    tab.leave_queue_requested.connect(lambda: leave_calls.append(True))

    # Join is only clickable once it's actually visible+enabled.
    tab.set_draft_intent(arena_running=True, set_detected=True)
    tab.set_queue_status(QueueStatus(state="idle", active_drafters=40, capacity=40))
    tab._queue_join_btn.click()
    assert join_calls == [True]

    # Leave is shown while queued.
    tab.set_queue_status(QueueStatus(
        state="queued", position=3, queue_length=10,
        active_drafters=40, capacity=40, eta_s=90,
    ))
    tab._queue_leave_btn.click()
    assert leave_calls == [True]


@pytest.mark.usefixtures("qapp")
def test_unset_queue_status_does_not_crash():
    """``set_queue_status`` may not have been called yet on first paint —
    the row must render something sane rather than raising."""
    from client.overlay.ui.home_tab import HomeTab

    tab = HomeTab()  # never call set_queue_status()

    assert isinstance(tab.queue_detail_text(), str)
    assert tab.queue_detail_text() != ""
    assert tab.join_button_enabled() is False
    assert tab.leave_button_visible() is False
    assert tab.offer_progress_visible() is False


@pytest.mark.usefixtures("qapp")
def test_draft_row_does_not_demand_vip():
    """A signed-in non-VIP user with a live draft must not be told VIP is
    required.

    Predictions are gated on an admission seat, not on VIP (server-side
    ``require_seat``; client-side ``_has_seat``). The draft row's old
    ``_is_vip`` gate outlived that migration, so every newly registered
    user saw "Draft detected — VIP required" the moment a draft started —
    a demand the rest of the system no longer makes. Admission state is
    the draft-access row's job, not this row's.
    """
    from client.overlay.ui.home_tab import HomeTab

    tab = HomeTab()
    tab.set_server_status(
        reachable=True, authenticated=True, email="new@example.com",
        is_vip=False, has_arena_player_id=True,
    )
    tab.set_draft_active(True)

    detail = tab.draft_detail_text()
    assert "VIP" not in detail, f"draft row still demands VIP: {detail!r}"


@pytest.mark.usefixtures("qapp")
def test_draft_row_prompts_sign_in_when_unauthenticated():
    """Not signed in is a real blocker and must still be surfaced."""
    from client.overlay.ui.home_tab import HomeTab

    tab = HomeTab()
    tab.set_server_status(
        reachable=True, authenticated=False, has_arena_player_id=True,
    )
    tab.set_draft_active(True)

    detail = tab.draft_detail_text()
    assert detail != ""
    assert "VIP" not in detail


def test_no_vip_gate_strings_remain():
    """The retired VIP copy must be gone from every language, so a stale
    translation can never resurrect the demand."""
    import json
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "client" / "overlay" / "i18n" / "translations.json"
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    for lang, strings in data.items():
        assert "vip_required" not in strings, f"{lang} still has vip_required"
        assert "home_status_active_no_vip" not in strings, (
            f"{lang} still has home_status_active_no_vip"
        )
