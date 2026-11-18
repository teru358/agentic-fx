"""bridge への HTTP 呼出しの client 側の受動観測。

応答の `X-Bridge-*` header と client から見た経過時間・timeout・接続失敗の種別を、1 要求 1 行で
`<root>/logs/bridge-metrics/` の下へ書く (daemon は `client-YYYY-MM-DD.jsonl`、CLI は
`client-cli-YYYY-MM-DD.jsonl`。UTC の日付入りの file へ追記だけで、改名しない)。呼出しは有界 queue への
put_nowait だけで待たない (詰まったら捨てて欠落数を数える)。書込みと 14 日を超えた自分の名前の形の
file の削除は、daemon thread の writer が行う。停止は期限付き。呼出しの戻り値・例外・本文・timeout には
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
import math
import os
import queue
import re
import stat
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import urlsplit

import httpx

CALLERS = ("tick", "mission", "cli", "unknown")
CALLER_HEADER = "X-Afx-Caller"
RETENTION_DAYS = 14
METRICS_DIR_PARTS = ("logs", "bridge-metrics")
FILE_PREFIX = "client-"
CLI_FILE_PREFIX = "client-cli-"
DEFAULT_QUEUE_SIZE = 2048
STOP_TIMEOUT_S = 2.0
_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

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
                      CLI_FILE_PREFIX if default_caller == "cli" else FILE_PREFIX)
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

def _day_of(row: dict[str, Any]) -> str:
    """行の ts と同じ時刻から file の日付を作る (ts が読めないときだけ現在の UTC)。"""
    ts = row.get("ts")
    if isinstance(ts, str) and _DAY_RE.fullmatch(ts[:10]):
        return ts[:10]
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class _DailyWriter:
    """有界 queue と daemon thread の writer。UTC 日付入りの file へ追記し、古い file を消す。"""

    def __init__(self, directory: Path, prefix: str, queue_size: int) -> None:
        self.directory = directory
        self.prefix = prefix
        self.queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._pattern = re.compile(re.escape(prefix) + r"(\d{4}-\d{2}-\d{2})\.jsonl")
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._run, name="bridge-observe", daemon=True)
        self._fd: int | None = None
        self._fd_day: str | None = None
        self._purged_day: str | None = None

    def start(self) -> None:
        self._thread.start()

    def submit(self, day: str, line: str) -> bool:
        """待たずに積む。満杯なら False。"""
        try:
            self.queue.put_nowait((day, line))
            return True
        except queue.Full:
            return False

    def flush(self, timeout: float = 10.0) -> None:
        """積まれた行が処理されるまで期限付きで待つ (試験・終了用。呼出しの経路からは使わない)。"""
        deadline = time.monotonic() + timeout
        while self.queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)

    def stop(self, timeout: float) -> None:
        """終了を伝えて期限付きで待つ。期限を過ぎたら待たずに戻る。"""
        self._stopping.set()
        self._thread.join(timeout)

    def _run(self) -> None:
        try:
            while True:
                try:
                    day, line = self.queue.get(timeout=0.2)
                except queue.Empty:
                    if self._stopping.is_set():
                        return
                    continue
                try:
                    if day != self._purged_day:
                        self._purged_day = day
                        self._purge(day)
                    self._append(day, line)
                except Exception:  # noqa: BLE001 - 観測の失敗は握る
                    self._close()
                finally:
                    self.queue.task_done()
        finally:
            self._close()

    def _close(self) -> None:
        fd, self._fd, self._fd_day = self._fd, None, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def _append(self, day: str, line: str) -> None:
        if self._fd is None or self._fd_day != day:
            self._close()
            self._fd = self._open(day)
            self._fd_day = day
        data = (line + "\n").encode("utf-8")
        while data:
            data = data[os.write(self._fd, data):]

    def _ensure_directory(self) -> None:
        """記録先 dir を作る (親の `logs` が無ければ作らない)。POSIX では 0700。"""
        try:
            self.directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise OSError("log directory is not a plain directory")
        if hasattr(os, "geteuid"):
            info = self.directory.stat()
            if info.st_uid != os.geteuid():
                raise OSError("log directory is not owned by this user")
            if stat.S_IMODE(info.st_mode) != 0o700:
                os.chmod(self.directory, 0o700)

    def _open(self, day: str) -> int:
        self._ensure_directory()
        flags = (os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
        fd = os.open(self.directory / f"{self.prefix}{day}.jsonl", flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("log file is not a regular file")
            if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
                raise OSError("log file is not owned by this user")
            if hasattr(os, "fchmod") and stat.S_IMODE(info.st_mode) != 0o600:
                os.fchmod(fd, 0o600)
            if flags & getattr(os, "O_NONBLOCK", 0):
                os.set_blocking(fd, True)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _purge(self, today: str) -> None:
        """14 日を超えた自分の名前の形の通常 file だけを消す。他の名前・dir・symlink は残す。"""
        try:
            limit = date.fromisoformat(today) - timedelta(days=RETENTION_DAYS)
            entries = list(os.scandir(self.directory))
        except (OSError, ValueError):
            return
        for entry in entries:
            found = self._pattern.fullmatch(entry.name)
            if found is None:
                continue
            try:
                if date.fromisoformat(found.group(1)) < limit and entry.is_file(
                        follow_symlinks=False):
                    os.unlink(entry.path)
            except (OSError, ValueError):
                continue


class _Sink:
    """記録先 1 つ分。呼出し側は emit (put_nowait だけ) しか使わない。"""

    def __init__(self, writer: _DailyWriter) -> None:
        self._guard = threading.Lock()
        self._dropped = 0
        self._reported = 0
        self.writer = writer
        self._stopped = False

    @classmethod
    def open(cls, directory: Path, queue_size: int, prefix: str = FILE_PREFIX) -> "_Sink | None":
        """記録先 dir は最初の 1 行を書くときに作る (`logs` が実在の dir のときだけ)。"""
        try:
            sink = cls(_DailyWriter(directory, prefix, queue_size))
            sink.writer.start()
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
        line = json.dumps(row, separators=(",", ":"), ensure_ascii=True)
        if not self.writer.submit(_day_of(row), line):
            self.count_drop(pending)

    def flush(self) -> None:
        if not self._stopped:
            self.writer.flush()

    def stop(self, timeout: float = STOP_TIMEOUT_S) -> None:
        if self._stopped:
            return
        self._stopped = True
        try:
            self.writer.stop(timeout)
        except Exception:  # noqa: BLE001 - 停止の失敗で呼出しや終了を止めない
            pass
        finally:
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
