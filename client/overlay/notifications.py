"""Application-wide notification bus for surfacing failures to the user.

Any component — including code running on watcher or worker threads —
calls ``NotificationBus.instance().post(...)``. Qt queues the ``posted``
emission onto the receiver's (main) thread, so no widget is ever touched
off-thread. Dedupe and rate-limiting happen inside ``post`` under a lock.
"""

from __future__ import annotations

import enum
import threading
import time
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QObject, Signal


class Severity(enum.IntEnum):
    INFO = 0
    WARNING = 1
    ERROR = 2


@dataclass(frozen=True)
class Notification:
    """One user-facing notification."""

    message: str
    severity: Severity = Severity.INFO
    key: str = ""           # dedupe key; "" means the message is the key
    timeout_ms: int = 0     # 0 = severity default (ERROR default is sticky)
    detail: str = ""


class NotificationBus(QObject):
    """Singleton bus: thread-safe ``post`` in, main-thread ``posted`` out."""

    posted = Signal(object)  # Notification

    DEDUPE_WINDOW_S = 30.0
    RATE_WINDOW_S = 10.0
    RATE_MAX = 6

    _instance: NotificationBus | None = None

    def __init__(self, time_fn: Callable[[], float] = time.monotonic) -> None:
        super().__init__()
        self._time_fn = time_fn
        self._lock = threading.Lock()
        self._recent: dict[str, float] = {}
        self._burst: list[float] = []
        self._overflow_notified = False

    @classmethod
    def instance(cls) -> NotificationBus:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        cls._instance = None

    def post(
        self,
        message: str,
        *,
        severity: Severity = Severity.INFO,
        key: str = "",
        timeout_ms: int = 0,
        detail: str = "",
    ) -> bool:
        """Post a notification from any thread.

        Returns:
            True when delivered, False when deduped or rate-limited.
        """
        overflow_warning: Notification | None = None
        with self._lock:
            now = self._time_fn()
            dedupe_key = key or message

            last = self._recent.get(dedupe_key)
            if last is not None and now - last < self.DEDUPE_WINDOW_S:
                return False
            self._recent[dedupe_key] = now
            if len(self._recent) > 64:
                cutoff = now - self.DEDUPE_WINDOW_S
                self._recent = {
                    k: t for k, t in self._recent.items() if t >= cutoff
                }

            self._burst = [t for t in self._burst if now - t < self.RATE_WINDOW_S]
            if len(self._burst) >= self.RATE_MAX:
                if not self._overflow_notified:
                    self._overflow_notified = True
                    overflow_warning = Notification(
                        message="Further notifications suppressed",
                        severity=Severity.WARNING,
                        key="__overflow__",
                    )
                else:
                    overflow_warning = None
                delivered = False
            else:
                self._burst.append(now)
                self._overflow_notified = False
                delivered = True

        if overflow_warning is not None:
            self.posted.emit(overflow_warning)
        if delivered:
            self.posted.emit(Notification(
                message=message,
                severity=severity,
                key=key,
                timeout_ms=timeout_ms,
                detail=detail,
            ))
        return delivered
