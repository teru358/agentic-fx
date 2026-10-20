"""取引 event の sink。専用の接続と書き込み thread を持ち、`ops_events` に 1 件ずつ書く。

注文処理を待たせないための部品である:
- 接続は `busy_timeout=0`。DB が busy なら待たずにその event を捨てる。
- 1 件ごとに INSERT とその commit だけを行う短い tx にする。失敗しても再試行せず捨てる。
- queue は有限。溢れたら捨てる。
- 捨てた event は理由別に数え、warning (理由別にスロットル) と health 状態に出す。
sink 自身が壊れているときに health event を確実に保存することは約束しない。
"""
from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from agentic_fx.store.db import connect

from . import trade_events
from .events import EventSchemaError, validate_fields

_log = logging.getLogger(__name__)

COUNTERS = ("queue_drop", "observer_error", "db_busy_drop", "db_error_drop",
            "invalid_value_drop", "worker_death", "shutdown_drop")
COMPONENT_CODE = "trade_event_sink"
RECOVERY_SEC = 60.0
WARNING_INTERVAL_SEC = 60.0
_BUSY_CODES = frozenset({sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})
_POLL_SEC = 1.0


class TradeEventSink:
    def __init__(self, db_path: Path, *, wall_clock: Callable[[], datetime],
                 monotonic: Callable[[], float] = time.monotonic, queue_max: int = 1000,
                 join_timeout: float = 5.0, recovery_sec: float = RECOVERY_SEC) -> None:
        self._db_path, self._wall_clock, self._monotonic = db_path, wall_clock, monotonic
        self._queue: queue.Queue = queue.Queue(maxsize=queue_max)
        self._join_timeout, self._recovery_sec = join_timeout, recovery_sec
        self._lock = threading.Lock()
        self._counters = dict.fromkeys(COUNTERS, 0)
        self._last_drop: float | None = None
        self._last_warned: dict[str, float] = {}
        self._notified = "ok"
        self._conn: sqlite3.Connection | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._abandon = threading.Event()
        self._dead = False

    # ---- 観測 ---------------------------------------------------------------

    def counters(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counters)

    def record_drop(self, reason: str, count: int = 1) -> None:
        if reason not in self._counters:
            raise ValueError(reason)
        now = self._monotonic()
        with self._lock:
            self._counters[reason] += count
            self._last_drop = now
            warn = now - self._last_warned.get(reason, -WARNING_INTERVAL_SEC) \
                >= WARNING_INTERVAL_SEC
            if warn:
                self._last_warned[reason] = now
                total = self._counters[reason]
        if warn:
            _log.warning("trade_event drop: %s (累計 %d)", reason, total)

    def health(self) -> str:
        """直近 `recovery_sec` 秒 (単調時計) に drop が無ければ ok。"""
        with self._lock:
            last = self._last_drop
        if last is None or self._monotonic() - last >= self._recovery_sec:
            return "ok"
        return "degraded"

    def worker_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def connection(self) -> sqlite3.Connection:
        assert self._conn is not None
        return self._conn

    # ---- 投入 ---------------------------------------------------------------

    def submit(self, code: str, ref: str, fields: dict) -> None:
        """queue に渡すだけで待たない。呼び出し側 (observer) には例外を出さない。"""
        if self._stopping.is_set():
            self.record_drop("shutdown_drop")
            return
        if self._dead:
            self.record_drop("queue_drop")
            return
        item = (self._wall_clock().isoformat(), code, ref, fields)
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self.record_drop("queue_drop")

    # ---- worker -------------------------------------------------------------

    def start(self) -> None:
        conn = connect(self._db_path)
        conn.execute("PRAGMA busy_timeout=0")
        self._conn = conn
        self._thread = threading.Thread(target=self._run, name="afx-trade-event-sink",
                                        daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            while not self._abandon.is_set():
                try:
                    item = self._queue.get(timeout=_POLL_SEC)
                except queue.Empty:
                    item = None
                if item is not None:
                    self._write(item)
                self.poll_health()
                if item is None and self._stopping.is_set() and self._queue.empty():
                    return
        except BaseException:  # noqa: BLE001 — worker の死を数え、以降は queue_drop にする
            self._dead = True
            self.record_drop("worker_death")
            _log.exception("trade event sink worker died")

    def _write(self, item: tuple) -> None:
        ts, code, ref, fields = item
        try:
            validate_fields(code, fields, ref)
        except EventSchemaError:
            self.record_drop("invalid_value_drop")
            return
        try:
            self._insert(ts, code, ref, fields)
        except sqlite3.Error as exc:
            self._rollback()
            busy = getattr(exc, "sqlite_errorcode", None) in _BUSY_CODES
            self.record_drop("db_busy_drop" if busy else "db_error_drop")
        except Exception:  # noqa: BLE001
            self._rollback()
            self.record_drop("db_error_drop")

    def _insert(self, ts: str, code: str, ref: str, fields: dict) -> None:
        conn = self.connection
        conn.execute("INSERT INTO ops_events(ts,code,ref,fields_json) VALUES (?,?,?,?)",
                     (ts, code, ref, json.dumps(fields, sort_keys=True, separators=(",", ":"))))
        conn.commit()

    def _rollback(self) -> None:
        try:
            self.connection.rollback()
        except Exception:  # noqa: BLE001
            pass

    def poll_health(self) -> None:
        """状態が変わったときだけ `service_health_changed` を 1 回試みる (保存できなくてもよい)。"""
        state = self.health()
        if state == self._notified:
            return
        self._notified = state
        try:
            self._insert(self._wall_clock().isoformat(), "service_health_changed", "instance",
                         {"component_code": COMPONENT_CODE, "health_code": state})
        except Exception:  # noqa: BLE001 — 補助通知。失敗は drop に数えない
            self._rollback()

    # ---- 停止 ---------------------------------------------------------------

    def stop(self) -> None:
        """queue を排出して最大 `join_timeout` 秒で止める。残りは shutdown_drop に数える。"""
        self._stopping.set()
        thread = self._thread
        if thread is not None:
            try:
                self._queue.put_nowait(None)  # 待っている worker を起こす
            except queue.Full:
                pass
            thread.join(self._join_timeout)
            if thread.is_alive():
                self._abandon.set()
        remaining = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            remaining += item is not None
        if remaining:
            self.record_drop("shutdown_drop", remaining)
        if thread is not None and not thread.is_alive() and self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None


class TradeEventEmitter:
    """`transitions.observe` に渡す observer。遷移後の行から event を作って sink に渡す。

    読むのは遷移後の行と `missions.trigger` の 1 回だけ。flock もファイル I/O もしない
    (`state_mode` は service が起動時に読んだ値をメモリで受け取る)。
    """

    _ORIGIN_SQL = ("SELECT m.trigger FROM trade_intents i JOIN missions m "
                   "ON m.id=i.mission_id WHERE i.id=?")

    def __init__(self, conn: sqlite3.Connection, sink: TradeEventSink, *,
                 state_mode: str | None, ccy: str, mode: str = "paper") -> None:
        self._conn, self._sink = conn, sink
        self._state_mode, self._ccy, self._mode = state_mode, ccy, mode

    def _trigger(self, row: dict) -> object:
        intent_id = row.get("intent_id")
        if intent_id is None:
            return None
        found = self._conn.execute(self._ORIGIN_SQL, (intent_id,)).fetchone()
        return None if found is None else found[0]

    def __call__(self, row: dict, from_status: str, to_status: str) -> None:
        try:
            if not trade_events.has_event(to_status):
                return
            built = trade_events.build_event(
                row, to_status, trigger=self._trigger(row), state_mode=self._state_mode,
                ccy=self._ccy, mode=self._mode)
        except trade_events.InvalidValue:
            self._sink.record_drop("invalid_value_drop")
            return
        except Exception:  # noqa: BLE001 — 通知は注文処理を止めない
            self._sink.record_drop("observer_error")
            return
        if built is not None:
            self._sink.submit(*built)
