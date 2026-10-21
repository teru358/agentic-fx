"""取引 event の sink。専用の接続と書き込み thread を持ち、`ops_events` に 1 件ずつ書く。

注文処理を待たせないための部品である:
- 接続は `busy_timeout=0`。INSERT が busy (BUSY / LOCKED とその extended code) のときだけ、
  sink thread 内で短い backoff を挟んで最大 `BUSY_RETRY_SEC` 秒 (注入した単調時計で測る) 再試行し、
  期限を超えたら捨てる。停止要求の後も再試行は続け、join が時間切れで放棄されたときだけ中断する。
- 1 件ごとに INSERT とその commit だけを行う短い tx にする。busy 以外の失敗は再試行せず捨てる。
- queue は有限。溢れたら捨てる。
- 捨てた event は理由別に数え、warning (理由別にスロットル) と health 状態に出す。observer 側の
  drop は lock も logging も使わない counter に足すだけで、集計と warning は sink 側が行う。
sink 自身が壊れているときに health event を確実に保存することは約束しない。
"""
from __future__ import annotations

import json
import logging
import queue
import random
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from agentic_fx.core import transitions
from agentic_fx.store.db import connect

from . import trade_events
from .events import EventSchemaError, validate_fields

_log = logging.getLogger(__name__)

COUNTERS = trade_events.DROP_REASONS
COMPONENT_CODE = "trade_event_sink"
RECOVERY_SEC = 60.0
WARNING_INTERVAL_SEC = 60.0
# primary code (下位 8 bit) で比べる。BUSY_RECOVERY / BUSY_SNAPSHOT / LOCKED_SHAREDCACHE 等の
# extended code も busy として扱う。
_BUSY_CODES = frozenset({sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})
_PRIMARY_MASK = 0xFF


def is_busy_error(exc: BaseException) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    return isinstance(code, int) and (code & _PRIMARY_MASK) in _BUSY_CODES
_POLL_SEC = 1.0
BUSY_RETRY_SEC = 1.0
_BACKOFF_START_SEC = 0.005
_BACKOFF_CAP_SEC = 0.05
_BACKOFF_JITTER = 0.2


def backoff_sec(attempt: int, rng=random) -> float:
    """busy 再試行の待ち時間。5 ms 開始、指数で 50 ms 上限、±20% の jitter。"""
    base = min(_BACKOFF_START_SEC * 2 ** min(attempt, 16), _BACKOFF_CAP_SEC)
    return base * (1 + rng.uniform(-_BACKOFF_JITTER, _BACKOFF_JITTER))


class TradeEventSink:
    def __init__(self, db_path: Path, *, wall_clock: Callable[[], datetime],
                 monotonic: Callable[[], float] = time.monotonic, queue_max: int = 1000,
                 join_timeout: float = 5.0, recovery_sec: float = RECOVERY_SEC,
                 busy_retry_sec: float = 0.0) -> None:
        # busy_retry_sec は worker thread だけが busy を待つ上限 (0 = 待たずに捨てる)。
        # core の接続は待たされない。busy は INSERT の前に起きるので、再試行で重複は生じない。
        self._busy_retry_sec = busy_retry_sec
        self._db_path, self._wall_clock, self._monotonic = db_path, wall_clock, monotonic
        self._queue: queue.Queue = queue.Queue(maxsize=queue_max)
        self._join_timeout, self._recovery_sec = join_timeout, recovery_sec
        self._lock = threading.Lock()
        self._counters = dict.fromkeys(COUNTERS, 0)
        # observer 側の drop。加算は lock も I/O も待たない。集計 (_absorb) は読み手が行う。
        self._observed = {r: transitions.LockFreeCounter() for r in COUNTERS}
        self._observed_seen = dict.fromkeys(COUNTERS, 0)
        self._failure_base = transitions.observer_failures.value()
        self._pending_warn: list[tuple[str, int]] = []
        self._inflight = False
        self._inflight_lock = threading.Lock()
        self._closed = threading.Event()
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
            self._absorb()
            return dict(self._counters)

    def note_drop(self, reason: str, count: int = 1) -> None:
        """observer 側の drop の記録。lock も logging も使わない (集計と warning は sink 側)。"""
        counter = self._observed[reason]
        for _ in range(count):
            counter.incr()

    def _count(self, reason: str, delta: int, now: float) -> None:
        """集計の加算・直近 drop 時刻・warning のスロットル。`_lock` を持って worker 側だけが呼ぶ。"""
        self._counters[reason] += delta
        self._last_drop = now
        if now - self._last_warned.get(reason, -WARNING_INTERVAL_SEC) >= WARNING_INTERVAL_SEC:
            self._last_warned[reason] = now
            self._pending_warn.append((reason, self._counters[reason]))

    def _absorb(self) -> None:
        """observer 側の counter を集計へ取り込む。`_lock` を持って呼ぶ。warning は保留する。"""
        now = None
        for reason in COUNTERS:
            seen = self._observed[reason].value()
            if reason == "observer_error":
                seen += transitions.observer_failures.value() - self._failure_base
            delta = seen - self._observed_seen[reason]
            if delta <= 0:
                continue
            if now is None:
                now = self._monotonic()
            self._observed_seen[reason] = seen
            self._count(reason, delta, now)

    def _take_warnings(self) -> list[tuple[str, int]]:
        """保留中の warning を取り出す。`_lock` を持って呼ぶ。"""
        pending, self._pending_warn = self._pending_warn, []
        return pending

    @staticmethod
    def _log_warnings(pending: list[tuple[str, int]]) -> None:
        for reason, total in pending:
            _log.warning("trade_event drop: %s (累計 %d)", reason, total)

    def _flush_warnings(self) -> None:
        with self._lock:
            self._absorb()
            pending = self._take_warnings()
        self._log_warnings(pending)

    def record_drop(self, reason: str, count: int = 1) -> None:
        if reason not in self._counters:
            raise ValueError(reason)
        now = self._monotonic()
        with self._lock:
            self._count(reason, count, now)
            pending = self._take_warnings()
        self._log_warnings(pending)

    def health(self) -> str:
        """直近 `recovery_sec` 秒 (単調時計) に drop が無ければ ok。"""
        with self._lock:
            self._absorb()
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
            self.note_drop("shutdown_drop")
            return
        if self._dead:
            self.note_drop("queue_drop")
            return
        item = (self._wall_clock().isoformat(), code, ref, fields)
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self.note_drop("queue_drop")
            return
        if self._closed.is_set():
            # stop の排出の後に入った item。拾った側が数えるので二重にならない。
            self._drain_count()

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
                    self._inflight = True
                    try:
                        self._write(item)
                    finally:
                        self._release_inflight()
                self._flush_warnings()
                self.poll_health()
                if item is None and self._stopping.is_set() and self._queue.empty():
                    return
        except BaseException:  # noqa: BLE001 — worker の死を数え、以降は queue_drop にする
            self._dead = True
            self.record_drop("worker_death")
            _log.exception("trade event sink worker died")
        finally:
            if self._abandon.is_set() and self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001
                    pass
                self._conn = None

    def _write(self, item: tuple) -> None:
        ts, code, ref, fields = item
        try:
            validate_fields(code, fields, ref)
        except EventSchemaError:
            self.record_drop("invalid_value_drop")
            return
        give_up = self._monotonic() + self._busy_retry_sec
        attempt = 0
        while True:
            try:
                self._insert(ts, code, ref, fields)
                return
            except sqlite3.Error as exc:
                self._rollback()
                busy = is_busy_error(exc)
                remaining = give_up - self._monotonic()
                if busy and remaining > 0:
                    delay = min(backoff_sec(attempt), remaining)
                    attempt += 1
                    if self._abandon.wait(delay):
                        # join が時間切れで放棄された。数える側は stop と取り合って 1 回だけにする
                        if self._release_inflight():
                            self.record_drop("shutdown_drop")
                        return
                    if self._monotonic() >= give_up:
                        self.record_drop("db_busy_drop")
                        return
                    continue
                self.record_drop("db_busy_drop" if busy else "db_error_drop")
                return
            except Exception:  # noqa: BLE001
                self._rollback()
                self.record_drop("db_error_drop")
                return

    def _release_inflight(self) -> bool:
        """処理中の印を下ろす。印が立っていたときだけ True (放棄時にどちらが数えるかの取り合い)。"""
        with self._inflight_lock:
            was, self._inflight = self._inflight, False
        return was

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
                if self._release_inflight():
                    self.record_drop("shutdown_drop")  # worker が抱えたまま。保存されたかは分からない
        self._closed.set()
        self._drain_count()
        if thread is not None and not thread.is_alive() and self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None


    def _drain_count(self) -> None:
        remaining = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            remaining += item is not None
        if remaining:
            self.note_drop("shutdown_drop", remaining)


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
            self._sink.note_drop("invalid_value_drop")
            return
        except Exception:  # noqa: BLE001 — 通知は注文処理を止めない
            self._sink.note_drop("observer_error")
            return
        if built is not None:
            self._sink.submit(*built)
