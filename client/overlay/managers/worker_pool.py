"""Reference-keeper for in-flight QThread workers."""

from __future__ import annotations

from PySide6.QtCore import QThread


class WorkerPool:
    """Keeps Python refs to running QThreads until their ``finished`` fires.

    Replacing a single-slot worker pointer drops the Python ref while
    ``QThread.run`` may still be active; Python then GCs the QThread and
    Qt aborts with "QThread: Destroyed while thread is still running".
    ``finished`` fires after ``run()`` has fully returned, so discarding
    on that signal is the only safe release point.
    """

    def __init__(self) -> None:
        self._inflight: set[QThread] = set()

    def launch(self, worker: QThread, *, delete_later: bool = True) -> QThread:
        worker.finished.connect(lambda w=worker: self._inflight.discard(w))
        if delete_later:
            worker.finished.connect(worker.deleteLater)
        self._inflight.add(worker)
        worker.start()
        return worker

    def __contains__(self, worker: QThread) -> bool:
        return worker in self._inflight

    def __len__(self) -> int:
        return len(self._inflight)
