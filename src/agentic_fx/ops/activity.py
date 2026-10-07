"""Best-effort activity projection with bounded rejection noise."""
from __future__ import annotations

import threading
from collections import defaultdict
from datetime import datetime
from typing import Callable


# 拒否の分類は固定集合。分類名はそのまま activity に書かれるので、自由文を通さない。
REJECTION_CATEGORIES = frozenset({"authentication_failed", "peer_rejected", "limit_rejected"})


class SuppressedActivity:
    """Keep five detailed rejection records per category and minute.

    The caller owns the periodic 30-second tick and shutdown hook; both invoke
    :meth:`flush_due`, so a quiet process still reports the suppressed count.
    """

    def __init__(self, *, wall_clock: Callable[[], datetime],
                 write: Callable[[str, str, str], None]) -> None:
        self._wall_clock = wall_clock
        self._write = write
        # API 接続 thread・30 秒 flush thread・停止処理が同時に触るので、状態は mutex で守る。
        # 書込み (I/O) は lock の外で行い、他の thread を待たせない。
        self._lock = threading.Lock()
        self._minute: str | None = None
        self._counts: dict[str, int] = defaultdict(int)

    def record(self, category: str) -> None:
        if category not in REJECTION_CATEGORIES:
            raise ValueError("unknown rejection category")
        minute = self._minute_key()
        with self._lock:
            pending: list[tuple[str, int]] = []
            if self._minute is not None and minute != self._minute:
                pending = self._take()
            self._minute = minute
            self._counts[category] += 1
            detailed = self._counts[category] <= 5
        self._emit(pending)
        if detailed:
            self._write("SYSTEM", category, category)

    def flush_due(self) -> None:
        minute = self._minute_key()
        with self._lock:
            pending: list[tuple[str, int]] = []
            if self._minute is not None and minute != self._minute:
                pending = self._take()
                self._minute = minute
        self._emit(pending)

    def shutdown(self) -> None:
        with self._lock:
            pending = self._take()
        self._emit(pending)

    def _take(self) -> list[tuple[str, int]]:
        """lock の中で、抑制した件数を取り出して数え直しに入る。"""
        pending = [(category, count - 5) for category, count in self._counts.items()
                   if count > 5]
        self._counts.clear()
        return pending

    def _emit(self, pending: list[tuple[str, int]]) -> None:
        for category, suppressed in pending:
            self._write("SYSTEM", "suppressed", f"{category}: {suppressed} suppressed")

    def _minute_key(self) -> str:
        return self._wall_clock().strftime("%Y-%m-%dT%H:%M")
