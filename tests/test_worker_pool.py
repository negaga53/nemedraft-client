"""Tests for client.overlay.managers.worker_pool.WorkerPool."""

from __future__ import annotations

import os
import time

# Must be set before any PySide6 import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _SleepWorker(QThread):
    def __init__(self, duration: float = 0.05) -> None:
        super().__init__()
        self._duration = duration

    def run(self) -> None:
        time.sleep(self._duration)


def _wait_until(predicate, qapp, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        qapp.processEvents()
        time.sleep(0.01)
    return predicate()


def test_launch_keeps_ref_while_running(qapp):
    from client.overlay.managers.worker_pool import WorkerPool

    pool = WorkerPool()
    worker = _SleepWorker(0.2)
    pool.launch(worker, delete_later=False)
    assert worker in pool
    assert len(pool) == 1

    worker.wait(5000)
    assert _wait_until(lambda: worker not in pool, qapp)
    assert len(pool) == 0


def test_launch_returns_worker(qapp):
    from client.overlay.managers.worker_pool import WorkerPool

    pool = WorkerPool()
    worker = _SleepWorker(0.01)
    assert pool.launch(worker, delete_later=False) is worker
    worker.wait(5000)
    _wait_until(lambda: worker not in pool, qapp)


def test_multiple_workers_tracked_independently(qapp):
    from client.overlay.managers.worker_pool import WorkerPool

    pool = WorkerPool()
    fast = _SleepWorker(0.01)
    slow = _SleepWorker(0.3)
    pool.launch(fast, delete_later=False)
    pool.launch(slow, delete_later=False)
    assert len(pool) == 2

    fast.wait(5000)
    assert _wait_until(lambda: fast not in pool, qapp)
    assert slow in pool

    slow.wait(5000)
    assert _wait_until(lambda: slow not in pool, qapp)
