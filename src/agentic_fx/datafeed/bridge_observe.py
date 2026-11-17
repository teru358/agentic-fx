"""bridge への HTTP 呼出しの client 側の受動観測。

応答の `X-Bridge-*` header と client から見た経過時間・timeout・接続失敗の種別を、1 要求 1 行で
`<root>/logs/bridge-metrics/` の下へ書く (daemon は `client.jsonl`、CLI は `client-cli.jsonl`。
process の種類ごとに file を分け、深夜の日替わりで前日分を互いに消し合わない)。書込みは標準 logging の QueueHandler への
enqueue だけで、呼出しは待たない (詰まったら捨てて欠落数を数える)。日次ローテーション
(UTC 0 時、14 世代) は TimedRotatingFileHandler に任せる。呼出しの戻り値・例外・本文・timeout には
触れない。観測の失敗は握り、header が読めない値は 0 にせず `lock_state` を `missing` /
`unavailable` にして数値を欠落させる。

bridge への要求には固定ラベルの `X-Afx-Caller` を常に付ける (bridge 側の記録が呼出し元を分ける)。
記録先は `configure` で root を渡した process だけ。未設定なら何もしない。
行には URL・host・port・生の header・口座・価格・秘密・boot id・履歴 slot の ID を書かない。
"""
from __future__ import annotations

import atexit
import contextvars
import json
import logging
import logging.handlers
import math
import os
import queue
import stat
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import urlsplit

import httpx

CALLERS = ("tick", "mission", "cli", "unknown")
CALLER_HEADER = "X-Afx-Caller"
RETENTION_DAYS = 14
METRICS_DIR_PARTS = ("logs", "bridge-metrics")
FILE_NAME = "client.jsonl"
CLI_FILE_NAME = "client-cli.jsonl"
DEFAULT_QUEUE_SIZE = 2048

# bridge protocol の固定 error code (mt5_bridge/bridge_metrics.py の ERROR_CODES と同じ集合)。
ERROR_CODES = frozenset({
    "auth_invalid", "order_key_required", "local_only_forbidden",
    "route_not_found", "method_not_allowed", "request_validation_failed",
    "internal_error", "mt5_not_connected", "runtime_not_initialized",
    "symbol_unavailable", "tick_unavailable", "history_copy_failed",
    "quote_failed", "history_failed", "order_failed",
    "time_rule_mismatch", "time_rule_unverified",
    "history_request_busy", "history_request_duplicate", "history_request_id_invalid",
    "invalid_range", "invalid_request", "invalid_timestamp", "closed_deal_not_found",
    "soft_halted", "preflight_rejected", "preflight_error", "order_rejected",
    "modify_no_fields", "modify_failed", "close_failed", "resume_refused",
})
_LOCK_STATES = frozenset({"measured", "not_locked", "unavailable"})
_MAX_MS = 1.0e9
_MAX_COUNT = 1_000_000

_monotonic: Callable[[], float] = time.monotonic

_lock = threading.Lock()
_sink: "_Sink | None" = None
_default_caller = "unknown"
_caller_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "bridge_observe_caller", default=None)


def configure(root: Path, default_caller: str = "unknown",
              queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
    """この process の記録先 root と、scope の外で使う caller を決める。"""
    global _sink, _default_caller
    if default_caller not in CALLERS:
        raise ValueError("bridge observe caller must be one of the fixed labels")
    with _lock:
        previous, _sink = _sink, None
        _default_caller = default_caller
    if previous is not None:
        previous.stop()
    sink = _Sink.open(Path(root).joinpath(*METRICS_DIR_PARTS), queue_size,
                      CLI_FILE_NAME if default_caller == "cli" else FILE_NAME)
    with _lock:
        _sink = sink


def set_default_caller(label: str) -> None:
    """記録先を変えずに、scope の外の caller だけを決める。"""
    global _default_caller
    if label not in CALLERS:
        raise ValueError("bridge observe caller must be one of the fixed labels")
    with _lock:
        _default_caller = label


def reset() -> None:
    global _sink, _default_caller
    with _lock:
        previous, _sink = _sink, None
        _default_caller = "unknown"
    if previous is not None:
        previous.stop()


def flush() -> None:
    """積まれた行が書かれるまで待つ (試験・終了用。呼出しの経路からは使わない)。"""
    sink = _sink
    if sink is not None:
        sink.flush()


def dropped() -> int:
    sink = _sink
    return sink.dropped if sink is not None else 0


def file_handler() -> logging.handlers.TimedRotatingFileHandler | None:
    sink = _sink
    return sink.file_handler if sink is not None else None


def queue_handler() -> logging.handlers.QueueHandler | None:
    sink = _sink
    return sink.queue_handler if sink is not None else None


def current_caller() -> str:
    return _caller_var.get() or _default_caller


@contextmanager
def caller_scope(label: str) -> Iterator[None]:
    if label not in CALLERS:
        raise ValueError("bridge observe caller must be one of the fixed labels")
    token = _caller_var.set(label)
    try:
        yield
    finally:
        _caller_var.reset(token)


# ── route ─────────────────────────────────────────────────────────

_SIMPLE_ROUTES = frozenset({
    "/health", "/account", "/server-time", "/positions", "/symbols", "/order",
    "/admin/status", "/admin/halt", "/admin/resume",
})


def route_of(url: str) -> str:
    """URL から固定の route 名を作る。生の path・query・host は残さず、末尾の `/` は無視する。"""
    try:
        path = urlsplit(str(url)).path
    except Exception:  # noqa: BLE001
        return "/other"
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


# ── header の読取 ─────────────────────────────────────────────────

def _header(headers: Any, name: str) -> str | None:
    value = headers.get(name)
    return value if isinstance(value, str) else None


def _ms(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0 or value > _MAX_MS:
        return None
    return round(value, 3)


def _count(text: str | None) -> int | None:
    if text is None or not text.isascii() or not text.isdigit():
        return None
    value = int(text)
    return value if value <= _MAX_COUNT else None


def _empty_row(caller: str, route: str, elapsed_ms: float, timeout: Any) -> dict[str, Any]:
    return {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "caller": caller, "route": route, "status": None, "error_code": None,
        "lock_state": "missing",
        "wait_ms": None, "hold_ms": None, "wait_max_ms": None, "hold_max_ms": None,
        "acquisitions": None, "elapsed_ms": round(elapsed_ms, 3),
        "timeout_s": _timeout_seconds(timeout), "failure": None,
    }


def _timeout_seconds(timeout: Any) -> float | None:
    value = getattr(timeout, "read", timeout)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _fill_from_headers(row: dict[str, Any], headers: Any) -> None:
    code = _header(headers, "X-Bridge-Error-Code")
    if code is not None:
        row["error_code"] = code if code in ERROR_CODES else "unknown"
    state = _header(headers, "X-Bridge-Lock-State")
    if state is None:
        row["lock_state"] = "missing"
    elif state not in _LOCK_STATES:
        row["lock_state"] = "unavailable"
    elif state == "unavailable":
        row["lock_state"] = "unavailable"
    else:
        numbers = (
            _ms(_header(headers, "X-Bridge-Lock-Wait-Ms")),
            _ms(_header(headers, "X-Bridge-Lock-Hold-Ms")),
            _ms(_header(headers, "X-Bridge-Lock-Wait-Max-Ms")),
            _ms(_header(headers, "X-Bridge-Lock-Hold-Max-Ms")),
            _count(_header(headers, "X-Bridge-Lock-Acquisitions")),
        )
        if any(item is None for item in numbers):
            row["lock_state"] = "unavailable"
        else:
            row["lock_state"] = state
            (row["wait_ms"], row["hold_ms"], row["wait_max_ms"], row["hold_max_ms"],
             row["acquisitions"]) = numbers


def _failure_kind(exc: BaseException) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        return "connect"
    if isinstance(exc, (httpx.TransportError, OSError)):
        return "transport"
    return "error"


# ── 書込み ────────────────────────────────────────────────────────

def _private_directory(directory: Path) -> None:
    """記録先 dir を 0700 で作り、既にあれば 0700 に直す (symlink は辿らない)。"""
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid():
            raise OSError("log directory is not owned by this user")
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


class _PrivateRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    """0600 の通常ファイルだけを開く。symlink は辿らず、他人の file・通常でない file は拒否する。"""

    def _open(self):
        directory = Path(self.baseFilename).parent
        logs = directory.parent
        if logs.is_symlink() or not logs.is_dir() or directory.is_symlink():
            raise OSError("log directory is not a plain directory under an existing logs")
        _private_directory(directory)
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

    def handleError(self, record):  # noqa: N802 - 記録の失敗は呼出しへ出さない
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

    def __init__(self, q: queue.Queue, owner: "_Sink") -> None:
        super().__init__(q)
        self._owner = owner

    def enqueue(self, record):
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self._owner.count_drop(getattr(record, "dropped_reported", 0))

    def handleError(self, record):  # noqa: N802
        return


class _Sink:
    """記録先 1 つ分。呼出し側は emit (enqueue だけ) しか使わない。"""

    def __init__(self, q: queue.Queue, handler: _PrivateRotatingFileHandler) -> None:
        self._guard = threading.Lock()
        self._dropped = 0
        self._reported = 0
        self._queue = q
        self.file_handler = handler
        self.queue_handler = _DropQueueHandler(q, self)
        self._logger = logging.Logger("bridge.observe.client", logging.INFO)
        self._logger.propagate = False
        self._logger.addHandler(self.queue_handler)
        self.listener = logging.handlers.QueueListener(q, handler, respect_handler_level=False)
        self._stopped = False

    @classmethod
    def open(cls, directory: Path, queue_size: int, file_name: str = FILE_NAME) -> "_Sink | None":
        """記録先 dir は最初の 1 行を書くときに作る (`logs` が実在の dir のときだけ)。"""
        try:
            handler = _PrivateRotatingFileHandler(
                str(directory / file_name), when="midnight", utc=True,
                backupCount=RETENTION_DAYS, encoding="utf-8", delay=True)
            handler.setFormatter(logging.Formatter("%(message)s"))
            sink = cls(queue.Queue(maxsize=queue_size), handler)
            sink.listener.start()
        except Exception:  # noqa: BLE001 - 記録先を作れなくても呼出しは動く
            return None
        atexit.register(sink.stop)
        return sink

    @property
    def dropped(self) -> int:
        return self._dropped

    def count_drop(self, carried: int = 0) -> None:
        """捨てた行を数える。その行が載せていた欠落数は未報告へ戻す。"""
        with self._guard:
            self._dropped += 1
            self._reported -= carried

    def emit(self, row: dict[str, Any]) -> None:
        with self._guard:
            pending = max(self._dropped - self._reported, 0)
            self._reported += pending
        if pending > 0:
            row = {**row, "dropped_before": pending}
        self._logger.info(json.dumps(row, separators=(",", ":"), ensure_ascii=True),
                          extra={"dropped_reported": pending})

    def flush(self) -> None:
        if not self._stopped:
            self._queue.join()

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        try:
            _stop_listener(self.listener, self._queue)
        except Exception:  # noqa: BLE001 - 停止の失敗で呼出しや終了を止めない
            pass
        finally:
            self.file_handler.close()
            atexit.unregister(self.stop)


def _record(url: str, started: float, response: Any, exc: BaseException | None,
            timeout: Any) -> None:
    try:
        sink = _sink
        if sink is None:
            return
        row = _empty_row(current_caller(), route_of(url),
                         (_monotonic() - started) * 1000.0, timeout)
        if exc is not None:
            row["failure"] = _failure_kind(exc)
        else:
            status = getattr(response, "status_code", None)
            if isinstance(status, int) and not isinstance(status, bool):
                row["status"] = status
            try:
                _fill_from_headers(row, response.headers)
            except Exception:  # noqa: BLE001
                row["lock_state"] = "missing"
        sink.emit(row)
    except Exception:  # noqa: BLE001 - 観測は呼出しの結果に影響させない
        return


# ── 呼出し ────────────────────────────────────────────────────────

def _with_caller(kwargs: dict[str, Any]) -> dict[str, Any]:
    """呼出し元のラベルを request header に足した kwargs を返す (渡された dict は変えない)。"""
    headers = kwargs.get("headers")
    label = current_caller()
    if headers is None:
        merged: Any = {CALLER_HEADER: label}
    elif isinstance(headers, httpx.Headers):
        merged = httpx.Headers(headers)
        merged[CALLER_HEADER] = label
    elif isinstance(headers, Mapping):
        merged = dict(headers)
        merged[CALLER_HEADER] = label
    else:
        try:
            merged = [*headers, (CALLER_HEADER, label)]
        except TypeError:
            return kwargs
    return {**kwargs, "headers": merged}


def observed(fetch: Callable[..., Any]) -> Callable[..., Any]:
    """`fetch(url, **kwargs)` を観測つきにして返す。戻り値・例外は変えない。"""
    def call(url: str, *args: Any, **kwargs: Any) -> Any:
        started = _monotonic()
        kwargs = _with_caller(kwargs)
        try:
            response = fetch(url, *args, **kwargs)
        except BaseException as exc:
            _record(url, started, None, exc, kwargs.get("timeout"))
            raise
        _record(url, started, response, None, kwargs.get("timeout"))
        return response
    return call


def bridge_get(url: str, *args: Any, **kwargs: Any) -> Any:
    """`httpx.get` を観測つきで呼ぶ (bridge 向け)。"""
    started = _monotonic()
    kwargs = _with_caller(kwargs)
    try:
        response = httpx.get(url, *args, **kwargs)
    except BaseException as exc:
        _record(url, started, None, exc, kwargs.get("timeout"))
        raise
    _record(url, started, response, None, kwargs.get("timeout"))
    return response
