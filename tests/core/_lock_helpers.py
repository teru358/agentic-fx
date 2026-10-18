"""Test-only lock doubles shared by the lock-order tests."""
from __future__ import annotations

import threading


class TrackedRLock:
    """テスト専用: 現スレッドが core lock owner かを検出する。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._owner = None
        self._depth = 0

    def __enter__(self):
        self._lock.acquire()
        owner = threading.get_ident()
        if self._owner == owner:
            self._depth += 1
        else:
            self._owner, self._depth = owner, 1
        return self

    def __exit__(self, *_args):
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
        self._lock.release()

    def owned_by_current_thread(self):
        return self._owner == threading.get_ident()
