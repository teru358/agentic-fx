"""Process-lifetime latch for failures in the app's observability path."""
from __future__ import annotations

import threading


class HealthLatch:
    """Remember every health failure until the process is restarted."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reasons: list[str] = []

    def record_failure(self, reason: str) -> None:
        with self._lock:
            self._reasons.append(reason)

    def is_latched(self) -> bool:
        with self._lock:
            return bool(self._reasons)

    def summary(self) -> list[str]:
        with self._lock:
            return list(self._reasons)
