"""bridge の要求ごとの構造化記録。1 要求 1 行の JSON を `logs/bridge-metrics.jsonl` へ書く。

要求の処理は標準 logging の QueueHandler への enqueue だけを行い、書込みを待たない。
書込み・日次ローテーション (UTC 0 時、14 世代) は QueueListener 配下の
TimedRotatingFileHandler が受け持つ。記録の失敗は応答へ出さない。
行には URL・query・API キー・生の header・boot id・価格・履歴 slot の ID を書かない。
"""
from __future__ import annotations

import atexit
import json
import logging
import logging.handlers
import os
import queue
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import bridge_metrics

CALLER_HEADER = "X-Afx-Caller"
CALLERS = frozenset({"tick", "mission", "cli"})
RETENTION_DAYS = 14
DEFAULT_QUEUE_SIZE = 2048

_LOCK_STATES = frozenset({"measured", "not_locked", "unavailable"})
_SLOT_STATES = frozenset({"none", "waiting", "holding", "completed"})
_SIMPLE_ROUTES = frozenset({
    "/health", "/account", "/server-time", "/positions", "/symbols", "/order",
    "/admin/status", "/admin/halt", "/admin/resume",
})


def caller_of(raw: bytes | None) -> str:
    """request header `X-Afx-Caller` の生 byte を固定集合と照合する。他・欠落は unknown。"""
    if raw is None:
        return "unknown"
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return "unknown"
    return text if text in CALLERS else "unknown"


def route_of(path: str) -> str:
    """path から固定の route 名を作る。生の path は残さない。末尾の `/` は無視する。"""
    if path.endswith("/") and len(path) > 1:
        path = path.rstrip("/")
    if path in _SIMPLE_ROUTES:
        return path
    parts = path.split("/")
    if len(parts) == 3 and parts[0] == "" and parts[2] != "":
        if parts[1] == "quote":
            return "/quote/{symbol}"
        if parts[1] == "ohlcv":
            return "/ohlcv/{symbol}"
    if len(parts) == 4 and parts[0] == "" and parts[1] == "positions" and parts[2] != "":
        if parts[3] in ("closed-deal", "modify", "close"):
            return f"/positions/{{ticket}}/{parts[3]}"
    return "/other"


def _number(headers: dict[str, str], name: str) -> float | None:
    try:
        return float(headers[name])
    except (KeyError, ValueError):
        return None


def build_row(*, caller_raw: bytes | None, path: str, status: int,
              error_code: str | None, headers: list[tuple[str, str]]) -> dict[str, Any]:
    """1 要求 1 行。`headers` は応答に付けた計測 header。時刻はここで 1 回だけ取る。"""
    named = {name: value for name, value in headers}
    state = named.get(bridge_metrics.H_LOCK_STATE)
    if state not in _LOCK_STATES:
        state = "unavailable"
    figures = state != "unavailable"
    count = _number(named, bridge_metrics.H_LOCK_ACQ) if figures else None
    slot = named.get(bridge_metrics.H_HISTORY_STATE)
    if error_code is not None and error_code not in bridge_metrics.ERROR_CODES:
        error_code = "unknown"
    return {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "caller": caller_of(caller_raw),
        "route": route_of(path),
        "status": status,
        "error_code": error_code,
        "lock_state": state,
        "wait_ms": _number(named, bridge_metrics.H_LOCK_WAIT) if figures else None,
        "hold_ms": _number(named, bridge_metrics.H_LOCK_HOLD) if figures else None,
        "wait_max_ms": _number(named, bridge_metrics.H_LOCK_WAIT_MAX) if figures else None,
        "hold_max_ms": _number(named, bridge_metrics.H_LOCK_HOLD_MAX) if figures else None,
        "acquisitions": int(count) if count is not None else None,
        "history_state": slot if slot in _SLOT_STATES else None,
    }


class _PrivateRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    """0600 の通常ファイルだけを開く。symlink は辿らず、他人の file・通常でない file は拒否する。"""

    def _open(self):
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(self.baseFilename, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise OSError("log file is not a regular file owned by this user")
            if stat.S_IMODE(info.st_mode) != 0o600:
                os.fchmod(fd, 0o600)
            os.set_blocking(fd, True)
            return os.fdopen(fd, "a", encoding=self.encoding or "utf-8")
        except BaseException:
            os.close(fd)
            raise

    def handleError(self, record):  # noqa: N802 - 記録の失敗は応答へ出さない
        return


def _stop_listener(listener: logging.handlers.QueueListener, q: queue.Queue) -> None:
    """queue が満杯でも listener を止める。止めるために、積まれた未書込みの行を捨てる。"""
    for _ in range(10_000):
        try:
            listener.stop()
            return
        except queue.Full:
            try:
                q.get_nowait()
                q.task_done()
            except queue.Empty:
                pass


class _DropQueueHandler(logging.handlers.QueueHandler):
    """満杯のとき待たずに捨てて、欠落数を数える。"""

    def __init__(self, q: queue.Queue, owner: "RequestLog") -> None:
        super().__init__(q)
        self._owner = owner

    def enqueue(self, record):
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self._owner._count_drop(getattr(record, "dropped_reported", 0))

    def handleError(self, record):  # noqa: N802
        return


class RequestLog:
    """記録先 1 つ分の QueueHandler + QueueListener。start 前・stop 後の emit は何もしない。"""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._logger: logging.Logger | None = None
        self._queue: queue.Queue | None = None
        self.queue_handler: _DropQueueHandler | None = None
        self.file_handler: _PrivateRotatingFileHandler | None = None
        self.listener: logging.handlers.QueueListener | None = None
        self._dropped = 0
        self._reported = 0

    @property
    def active(self) -> bool:
        return self._logger is not None

    @property
    def dropped(self) -> int:
        return self._dropped

    def _count_drop(self, carried: int = 0) -> None:
        """捨てた行を数える。その行が載せていた欠落数は未報告へ戻す。"""
        with self._guard:
            self._dropped += 1
            self._reported -= carried

    def start(self, path: Path, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        self.stop()
        path = Path(path)
        try:
            if path.parent.is_symlink() or not path.parent.is_dir():
                return
            handler = _PrivateRotatingFileHandler(
                str(path), when="midnight", utc=True, backupCount=RETENTION_DAYS,
                encoding="utf-8", delay=True)
            handler.setFormatter(logging.Formatter("%(message)s"))
            q: queue.Queue = queue.Queue(maxsize=queue_size)
            queue_handler = _DropQueueHandler(q, self)
            sink = logging.Logger("bridge.request_log", logging.INFO)
            sink.propagate = False
            sink.addHandler(queue_handler)
            listener = logging.handlers.QueueListener(q, handler, respect_handler_level=False)
            listener.start()
        except Exception:  # noqa: BLE001 - 記録が起動できなくても bridge は動く
            return
        with self._guard:
            self._dropped = 0
            self._reported = 0
        self._queue = q
        self.file_handler = handler
        self.queue_handler = queue_handler
        self.listener = listener
        self._logger = sink
        atexit.register(self.stop)

    def stop(self) -> None:
        sink, self._logger = self._logger, None
        q = self._queue
        listener, self.listener = self.listener, None
        handler, self.file_handler = self.file_handler, None
        self.queue_handler = None
        self._queue = None
        if sink is None:
            return
        try:
            if listener is not None:
                _stop_listener(listener, q)
        except Exception:  # noqa: BLE001 - 停止の失敗で bridge の終了を止めない
            pass
        finally:
            if handler is not None:
                handler.close()
            atexit.unregister(self.stop)

    def flush(self) -> None:
        """積まれた行が書かれるまで待つ (停止・試験用。要求の処理からは呼ばない)。"""
        q = self._queue
        if q is not None:
            q.join()

    def emit(self, row: dict[str, Any]) -> None:
        sink = self._logger
        if sink is None:
            return
        try:
            with self._guard:
                pending = max(self._dropped - self._reported, 0)
                self._reported += pending
            if pending > 0:
                row = {**row, "dropped_before": pending}
            sink.info(json.dumps(row, separators=(",", ":"), ensure_ascii=True),
                      extra={"dropped_reported": pending})
        except Exception:  # noqa: BLE001
            return


request_log = RequestLog()
