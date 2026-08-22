"""ImproveSupervisor — 改善レーンの別スロット群 (設計書 §3.1、裁定6:
MissionSupervisor の一般化ではなく別クラス)。"""
from __future__ import annotations

import logging
import queue
import threading
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from agentic_fx.core.scheduler import latest_scheduled_occurrence, period_key_of
from agentic_fx.store import db as db_mod
from agentic_fx.store import improve_waves

if TYPE_CHECKING:
    from agentic_fx.config import Settings
    from agentic_fx.core.contracts import Clock

_log = logging.getLogger("agentic_fx.improve_supervisor")

_MAX_SPAWN_ATTEMPTS = 2  # 初回 + 再試行 1 回 (設計書 §3.1 手順⑥)


class ImproveSupervisor:
    def __init__(self, *, capacity: int, root: Path, settings: "Settings",
                 clock: "Clock", db_path: Path,
                 stop_event: threading.Event) -> None:
        self._capacity = capacity
        self._root = root
        self._settings = settings
        self._clock = clock
        self._db_path = db_path
        self._stop_event = stop_event
        # 9.5 節で slot worker スレッド群を構築する。ここでは wave 作成
        # だけを実装する (Step-by-step の意図的な最小実装)。
        self._slot_queue: "queue.Queue[tuple[str, int, int] | None]" = \
            queue.Queue()
        self._workers: list[threading.Thread] = []
        self._started = False

    def tick(self, now: datetime) -> None:
        s = self._settings.schedule
        occurrence = latest_scheduled_occurrence(
            now, cadence=s.improve, at=s.improve_at,
            display_timezone=self._settings.display_timezone)
        period_key = period_key_of(occurrence, cadence=s.improve)
        conn = self._conn()
        owns = getattr(self, "_conn_for_test", None) is None
        try:
            open_slots = self._capacity - self._running_slot_count(conn)
            m = min(self._settings.improve.parallel, max(open_slots, 0))
            if m == 0:
                return
            created = improve_waves.create_wave_and_slots(
                conn, period_key=period_key, now=now, expected=m, commit=True)
            if not created:
                return  # 既に消費済みの period (再 tick)
            for k in range(m):
                self._slot_queue.put((period_key, k, -1))  # mission_id は claim 時に確定
        finally:
            if owns:
                conn.close()

    def _running_slot_count(self, conn) -> int:
        row = conn.execute(
            "SELECT count(*) c FROM improve_wave_slots "
            "WHERE status IN ('claimed','running')").fetchone()
        return row["c"]

    def _conn(self):
        # テストシーム: `_conn_for_test` が設定されていればそれを使う
        # (Step 3 時点の暫定。9.5 節で正式な dispatcher/slot 接続分離に
        # 置き換える)。
        conn_for_test = getattr(self, "_conn_for_test", None)
        if conn_for_test is not None:
            return conn_for_test
        return db_mod.connect(self._db_path)

    def submit_manual(self) -> int:
        raise NotImplementedError  # 9.7 節で実装

    def shutdown(self) -> None:
        raise NotImplementedError  # 9.5 節で実装

    def join(self, timeout: float) -> None:
        raise NotImplementedError  # 9.5 節で実装
