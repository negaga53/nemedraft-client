"""ShotClockBar severity thresholds, label visibility, and local ticking."""

from __future__ import annotations

import os

# Must be set before any PySide6 import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from client.overlay.ui.shot_clock_bar import ShotClockBar


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.mark.usefixtures("qapp")
def test_severity_thresholds():
    bar = ShotClockBar()
    bar.set_remaining(300.0, 300.0)
    assert bar.severity() == "ok"
    bar.set_remaining(80.0, 300.0)      # 26.7%
    assert bar.severity() == "warn"
    bar.set_remaining(10.0, 300.0)
    assert bar.severity() == "crit"


@pytest.mark.usefixtures("qapp")
def test_numeric_label_only_when_critical():
    bar = ShotClockBar()
    bar.set_remaining(300.0, 300.0)
    assert bar.label_text() == ""
    bar.set_remaining(9.0, 300.0)
    assert bar.label_text() == "0:09"


@pytest.mark.usefixtures("qapp")
def test_local_tick_never_goes_negative():
    bar = ShotClockBar()
    bar.set_remaining(2.0, 300.0)
    for _ in range(5):
        bar._tick()
    assert bar.remaining_s() == 0.0
    assert bar.fraction() == 0.0


@pytest.mark.usefixtures("qapp")
def test_ok_warn_boundary_is_inclusive_of_ok():
    """Spec: ok is >50%, warn is <33%. The 33-50% band is unspecified.

    We treat it as still "ok" — severity only escalates once we've actually
    dropped below the warn threshold, so 33% and 50% themselves (and
    everything between) read as "ok". This means a pick that starts at
    exactly 50% doesn't immediately show a warning color, and the bar only
    turns amber once genuinely under a third of the time remains.
    """
    bar = ShotClockBar()
    bar.set_remaining(150.0, 300.0)  # exactly 50%
    assert bar.severity() == "ok"
    bar.set_remaining(100.0, 300.0)  # 33.3%, above the 33% cutoff
    assert bar.severity() == "ok"
    bar.set_remaining(95.0, 300.0)  # 31.7%, clearly under 33%
    assert bar.severity() == "warn"


@pytest.mark.usefixtures("qapp")
def test_server_update_mid_countdown_resets_cleanly():
    bar = ShotClockBar()
    bar.set_remaining(60.0, 300.0)
    for _ in range(5):
        bar._tick()
    assert bar.remaining_s() == 55.0

    # A fresh server value (e.g. a new pack's predict response) should
    # replace the decaying value outright, not blend with it.
    bar.set_remaining(300.0, 300.0)
    assert bar.remaining_s() == 300.0
    assert bar.severity() == "ok"


@pytest.mark.usefixtures("qapp")
def test_clear_stops_timer_and_zeroes_bar():
    bar = ShotClockBar()
    bar.set_remaining(120.0, 300.0)
    assert bar._timer.isActive()

    bar.clear()
    assert not bar._timer.isActive()
    assert bar.remaining_s() == 0.0
    assert bar.fraction() == 0.0
    assert bar.label_text() == ""
    # A cleared bar (seat lost / draft ended) is a neutral non-event, not
    # an emergency — it must not read as "crit".
    assert bar.severity() == "ok"
