"""Mission 実行の watchdog 計測点 — begin/end は runner.run を所有する層が呼ぶ。

計測は Mission 単位 (trade / ask / reflection それぞれ)。複合コールバック全体を
計測すると正常 Mission を誤検知するため、ここに置く (追記 2026-07-31 A-1)。"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, replace

_log = logging.getLogger("agentic_fx.mission_watch")


@dataclass(frozen=True)
class MissionWatchEntry:
    mission_id: int
    loop: str
    started: float
    timeout_sec: float
    notified: bool = False


class MissionWatch:
    def __init__(self, time_fn=time.monotonic) -> None:
        self._lock = threading.Lock()
        self._entry: MissionWatchEntry | None = None
        self._time = time_fn

    def begin(self, mission_id: int, loop: str, timeout_sec: float) -> None:
        with self._lock:
            if self._entry is not None:
                # 全 Mission は core_lock 下で直列のため通常起きない。raise は
                # しない (begin の例外で missions.finish が飛ぶ方が害) — 警告して上書き
                _log.warning("mission watch slot occupied by #%s — overwriting",
                             self._entry.mission_id)
            self._entry = MissionWatchEntry(mission_id, loop, self._time(),
                                            timeout_sec)

    def end(self, mission_id: int) -> None:
        with self._lock:
            if self._entry is not None and self._entry.mission_id == mission_id:
                self._entry = None

    def mark_notified(self, mission_id: int) -> None:
        with self._lock:
            if self._entry is not None and self._entry.mission_id == mission_id:
                self._entry = replace(self._entry, notified=True)

    def breached(self, grace_sec: float = 60.0) -> MissionWatchEntry | None:
        """timeout + grace 超過かつ未通知なら entry を返す。それ以外 None。"""
        with self._lock:
            e = self._entry
            if (e is not None and not e.notified
                    and self._time() - e.started > e.timeout_sec + grace_sec):
                return e
            return None
