"""bridge への HTTP 呼出しの受動観測。

応答の `X-Bridge-*` header を読んで 1 要求 1 行を `<root>/logs/bridge-metrics/` へ追記する。
呼出しの戻り値・例外・本文・timeout には触れない。観測の失敗 (header の欠落・不正、
書込みの失敗) は握り、呼出しの結果へ出さない。header が読めない値は 0 にせず、
`lock_state` を `missing` / `unavailable` にして数値を欠落させる。

記録先は `configure` で root を渡した process だけ。未設定なら何もしない。
行には URL・host・port・生の header・口座・価格・秘密を書かない。boot id と履歴 slot の ID は
短い hash だけを残す。
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import math
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit

import httpx

CALLERS = ("tick", "mission", "cli", "unknown")
RETENTION_DAYS = 14
METRICS_DIR_PARTS = ("logs", "bridge-metrics")

_FILE_NAME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\.jsonl$")
_TOKEN = re.compile(r"^[a-z0-9_]{1,64}$")
_OPAQUE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_VERSION = re.compile(r"^[0-9]{1,3}$")
_LOCK_STATES = frozenset({"measured", "not_locked", "unavailable"})
_SLOT_STATES = frozenset({"none", "waiting", "holding", "completed"})
_MAX_MS = 1.0e9
_MAX_COUNT = 1_000_000

_monotonic: Callable[[], float] = time.monotonic

_lock = threading.Lock()
_sink: Path | None = None
_default_caller = "unknown"
_pruned_day: date | None = None
_caller_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "bridge_observe_caller", default=None)


def configure(root: Path, default_caller: str = "unknown") -> None:
    """この process の記録先 root と、scope の外で使う caller を決める。"""
    global _sink, _default_caller, _pruned_day
    if default_caller not in CALLERS:
        raise ValueError("bridge observe caller must be one of the fixed labels")
    with _lock:
        _sink = Path(root).joinpath(*METRICS_DIR_PARTS)
        _default_caller = default_caller
        _pruned_day = None


def set_default_caller(label: str) -> None:
    """記録先を変えずに、scope の外の caller だけを決める。"""
    global _default_caller
    if label not in CALLERS:
        raise ValueError("bridge observe caller must be one of the fixed labels")
    with _lock:
        _default_caller = label


def reset() -> None:
    global _sink, _default_caller, _pruned_day
    with _lock:
        _sink = None
        _default_caller = "unknown"
        _pruned_day = None


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
    """URL から固定の route 名を作る。生の path・query・host は残さない。"""
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

def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


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


def _empty_row(caller: str, route: str, elapsed_ms: float) -> dict[str, Any]:
    return {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "caller": caller, "route": route, "status": None, "error_code": None,
        "metrics_version": None, "boot_hash": None, "lock_state": "missing",
        "wait_ms": None, "hold_ms": None, "wait_max_ms": None, "hold_max_ms": None,
        "acquisitions": None, "elapsed_ms": round(elapsed_ms, 3), "failure": None,
        "history_state": None, "history_id_hash": None,
        "history_wait_ms": None, "history_hold_ms": None,
    }


def _fill_from_headers(row: dict[str, Any], headers: Any) -> None:
    version = _header(headers, "X-Bridge-Metrics-Version")
    if version is not None and _VERSION.fullmatch(version):
        row["metrics_version"] = version
    boot = _header(headers, "X-Bridge-Boot-Id")
    if boot is not None and _OPAQUE.fullmatch(boot):
        row["boot_hash"] = _hash(boot)
    code = _header(headers, "X-Bridge-Error-Code")
    if code is not None and _TOKEN.fullmatch(code):
        row["error_code"] = code
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
    slot = _header(headers, "X-Bridge-History-State")
    if slot is not None and slot in _SLOT_STATES:
        row["history_state"] = slot
        slot_id = _header(headers, "X-Bridge-History-Request-Id")
        if slot_id is not None and _OPAQUE.fullmatch(slot_id):
            row["history_id_hash"] = _hash(slot_id)
        if slot == "completed":
            row["history_wait_ms"] = _ms(_header(headers, "X-Bridge-History-Wait-Ms"))
            row["history_hold_ms"] = _ms(_header(headers, "X-Bridge-History-Hold-Ms"))


def _failure_kind(exc: BaseException) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        return "connect"
    if isinstance(exc, (httpx.TransportError, OSError)):
        return "transport"
    return "error"


# ── 書込み ────────────────────────────────────────────────────────

def prune(directory: Path, today: date) -> None:
    """保持期間を過ぎた `YYYY-MM-DD.jsonl` だけを消す。他の名前・symlink・dir は触らない。"""
    cutoff = today - timedelta(days=RETENTION_DAYS - 1)
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return
    for entry in entries:
        match = _FILE_NAME.fullmatch(entry.name)
        if match is None:
            continue
        try:
            day = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            if day >= cutoff or entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                continue
            os.unlink(entry.path)
        except (ValueError, OSError):
            continue


def _append(row: dict[str, Any]) -> None:
    global _pruned_day
    directory = _sink
    if directory is None:
        return
    logs = directory.parent
    if not logs.is_dir():
        return
    today = datetime.now(timezone.utc).date()
    directory.mkdir(exist_ok=True)
    with _lock:
        need_prune = _pruned_day != today
        _pruned_day = today
    if need_prune:
        prune(directory, today)
    line = (json.dumps(row, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
    fd = os.open(directory / f"{today.isoformat()}.jsonl",
                 os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def _record(url: str, started: float, response: Any, exc: BaseException | None) -> None:
    try:
        if _sink is None:
            return
        row = _empty_row(current_caller(), route_of(url), (_monotonic() - started) * 1000.0)
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
        _append(row)
    except Exception:  # noqa: BLE001 - 観測は呼出しの結果に影響させない
        return


# ── 呼出し ────────────────────────────────────────────────────────

def observed(fetch: Callable[..., Any]) -> Callable[..., Any]:
    """`fetch(url, **kwargs)` を観測つきにして返す。戻り値・例外・引数は変えない。"""
    def call(url: str, *args: Any, **kwargs: Any) -> Any:
        started = _monotonic()
        try:
            response = fetch(url, *args, **kwargs)
        except BaseException as exc:
            _record(url, started, None, exc)
            raise
        _record(url, started, response, None)
        return response
    return call


def bridge_get(url: str, *args: Any, **kwargs: Any) -> Any:
    """`httpx.get` を観測つきで呼ぶ (bridge 向け)。"""
    started = _monotonic()
    try:
        response = httpx.get(url, *args, **kwargs)
    except BaseException as exc:
        _record(url, started, None, exc)
        raise
    _record(url, started, response, None)
    return response
