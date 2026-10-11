"""webhook 配送の固定 retry 規則。"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

CONNECT_READ_TIMEOUT = 10.0
# 1 byte ずつ返し続ける相手でも 1 回の送信が終わるように、交換全体にも上限を置く
TOTAL_TIMEOUT = 30.0
INITIAL_DELAY = 1.0
BACKOFF_MULTIPLIER = 2.0
MAX_DELAY = 60.0
MAX_ATTEMPTS = 8
WARNING_INTERVAL = 60.0

_T = TypeVar("_T")


class RetryError(Exception):
    """配送失敗。reason は固定語で、値や URL は持たない。"""

    def __init__(self, reason: str, *, retryable: bool,
                 retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        self.retry_after = retry_after


def retry_delays() -> tuple[float, ...]:
    delays = []
    delay = INITIAL_DELAY
    for _ in range(MAX_ATTEMPTS - 1):
        delays.append(delay)
        delay = min(MAX_DELAY, delay * BACKOFF_MULTIPLIER)
    return tuple(delays)


class WarningThrottle:
    """同じ原因の warning を固定間隔より頻繁には出さない。"""

    def __init__(self, interval: float = WARNING_INTERVAL) -> None:
        self._interval = interval
        self._last: dict[str, float] = {}

    def allow(self, reason: str, now: float) -> bool:
        previous = self._last.get(reason)
        if previous is not None and now - previous < self._interval:
            return False
        self._last[reason] = now
        return True


def retry(operation: Callable[[], _T], *, sleep: Callable[[float], None] = time.sleep,
          warning: Callable[[str], None] | None = None,
          monotonic: Callable[[], float] = time.monotonic,
          throttle: WarningThrottle | None = None) -> _T:
    """retryable な失敗だけを最大 8 回まで再試行する。"""
    limiter = WarningThrottle() if throttle is None else throttle
    for attempt in range(MAX_ATTEMPTS):
        try:
            return operation()
        except RetryError as exc:
            if warning is not None and limiter.allow(exc.reason, monotonic()):
                warning(exc.reason)
            if not exc.retryable:
                raise
            if attempt == MAX_ATTEMPTS - 1:
                raise RetryError("retry_exhausted", retryable=False) from None
            delay = retry_delays()[attempt]
            if exc.retry_after is not None:
                # 受け手が指示した待ちは短縮に使わず、上限を超えても待たない
                delay = max(delay, min(exc.retry_after, MAX_DELAY))
            sleep(delay)
    raise AssertionError("unreachable")
