"""bridge の受動計測。lock の待ち・占有を要求ごとに測り、応答 header で返す。

計測は応答の body・detail・status に一切触れない。担体は HTTP response header だけ。
要求ごとの集計は ContextVar に載せた可変オブジェクトで分ける。thread pool は要求ごとに
呼出し側の context を複製して使うので、使い回される thread へ集計が漏れない。
"""
from __future__ import annotations

import contextvars
import re
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

PROTOCOL_VERSION = "1"

# process 起動ごとの opaque 値。口座・端末・時刻に由来させない。
BOOT_ID = secrets.token_hex(8)

H_VERSION = "X-Bridge-Metrics-Version"
H_BOOT_ID = "X-Bridge-Boot-Id"
H_LOCK_STATE = "X-Bridge-Lock-State"
H_LOCK_WAIT = "X-Bridge-Lock-Wait-Ms"
H_LOCK_HOLD = "X-Bridge-Lock-Hold-Ms"
H_LOCK_ACQ = "X-Bridge-Lock-Acquisitions"
H_LOCK_WAIT_MAX = "X-Bridge-Lock-Wait-Max-Ms"
H_LOCK_HOLD_MAX = "X-Bridge-Lock-Hold-Max-Ms"
H_ERROR_CODE = "X-Bridge-Error-Code"
H_HISTORY_ID = "X-Bridge-History-Request-Id"
H_HISTORY_STATE = "X-Bridge-History-State"
H_HISTORY_WAIT = "X-Bridge-History-Wait-Ms"
H_HISTORY_HOLD = "X-Bridge-History-Hold-Ms"

# 認証の拒否を表す code。この応答には履歴 slot の詳細を載せない。
AUTH_REJECTION_CODES = frozenset({
    "auth_invalid", "order_key_required", "local_only_forbidden",
})

_HISTORY_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _clock() -> float:
    return time.monotonic()


@dataclass
class RequestMetrics:
    """1 要求の lock 集計 (秒)。その要求を処理する thread だけが書く。"""

    acquisitions: int = 0
    wait_total: float = 0.0
    hold_total: float = 0.0
    wait_max: float = 0.0
    hold_max: float = 0.0
    in_lock: bool = False
    history_request_id: str | None = None
    authorized: bool = False

    def record(self, wait: float, hold: float) -> None:
        self.acquisitions += 1
        self.wait_total += wait
        self.hold_total += hold
        self.wait_max = max(self.wait_max, wait)
        self.hold_max = max(self.hold_max, hold)


_current: contextvars.ContextVar[RequestMetrics | None] = contextvars.ContextVar(
    "bridge_request_metrics", default=None,
)


def current_metrics() -> RequestMetrics | None:
    return _current.get()


def bind_metrics(metrics: RequestMetrics) -> contextvars.Token:
    return _current.set(metrics)


def unbind_metrics(token: contextvars.Token) -> None:
    _current.reset(token)


@contextmanager
def timed_lock(lock):
    """`lock` を取り、待ち・占有を現在の要求の集計へ足す。要求の外では何も記録しない。

    lock の型には依存しない (context manager として使えればよい)。
    """
    metrics = _current.get()
    if metrics is None:
        with lock:
            yield
        return
    t_wait = _clock()
    with lock:
        t_hold = _clock()
        metrics.in_lock = True
        try:
            yield
        finally:
            metrics.in_lock = False
            metrics.record(t_hold - t_wait, _clock() - t_hold)


def _ms(seconds: float) -> str:
    return f"{seconds * 1000.0:.3f}"


def lock_headers(metrics: RequestMetrics | None) -> list[tuple[str, str]]:
    if metrics is None:
        return [(H_LOCK_STATE, "unavailable")]
    if metrics.acquisitions == 0:
        return [
            (H_LOCK_STATE, "not_locked"),
            (H_LOCK_WAIT, _ms(0.0)), (H_LOCK_HOLD, _ms(0.0)),
            (H_LOCK_ACQ, "0"),
            (H_LOCK_WAIT_MAX, _ms(0.0)), (H_LOCK_HOLD_MAX, _ms(0.0)),
        ]
    return [
        (H_LOCK_STATE, "measured"),
        (H_LOCK_WAIT, _ms(metrics.wait_total)),
        (H_LOCK_HOLD, _ms(metrics.hold_total)),
        (H_LOCK_ACQ, str(metrics.acquisitions)),
        (H_LOCK_WAIT_MAX, _ms(metrics.wait_max)),
        (H_LOCK_HOLD_MAX, _ms(metrics.hold_max)),
    ]


# ── 履歴専用 slot ─────────────────────────────────────────────────

def valid_history_request_id(value: str) -> bool:
    return _HISTORY_ID_PATTERN.fullmatch(value) is not None


class _Entry:
    __slots__ = ("request_id", "metrics", "completed", "wait_ms", "hold_ms")

    def __init__(self, request_id: str, metrics: RequestMetrics) -> None:
        self.request_id = request_id
        self.metrics = metrics
        self.completed = False
        self.wait_ms = ""
        self.hold_ms = ""


class HistorySlot:
    """履歴取得 1 件分の単一 slot。次の履歴要求の受付まで保持する。

    登録・完了は短い mutex で守るが、観測 (snapshot) は mutex も MT5 の lock も取らず、
    1 回の参照読みと既に確定した値の読出しだけで返す。
    """

    ACCEPTED = "accepted"
    BUSY = "busy"
    DUPLICATE = "duplicate"

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self._entry: _Entry | None = None

    def admit(self, request_id: str, metrics: RequestMetrics) -> tuple[str, _Entry | None]:
        with self._mutex:
            current = self._entry
            if current is not None and current.request_id == request_id:
                return self.DUPLICATE, None
            if current is not None and not current.completed:
                return self.BUSY, None
            entry = _Entry(request_id, metrics)
            self._entry = entry
            return self.ACCEPTED, entry

    def complete(self, entry: _Entry) -> None:
        """全 native 呼出しと lock 解放の後で呼ぶ。この後だけ completed に見える。"""
        entry.wait_ms = _ms(entry.metrics.wait_total)
        entry.hold_ms = _ms(entry.metrics.hold_total)
        entry.completed = True

    def snapshot(self) -> list[tuple[str, str]]:
        entry = self._entry
        if entry is None:
            return [(H_HISTORY_STATE, "none")]
        if entry.completed:
            return [
                (H_HISTORY_ID, entry.request_id), (H_HISTORY_STATE, "completed"),
                (H_HISTORY_WAIT, entry.wait_ms), (H_HISTORY_HOLD, entry.hold_ms),
            ]
        state = "holding" if entry.metrics.in_lock else "waiting"
        return [(H_HISTORY_ID, entry.request_id), (H_HISTORY_STATE, state)]


history_slot = HistorySlot()


def response_headers(
    metrics: RequestMetrics | None, *, error_code: str | None,
    include_slot: bool,
) -> list[tuple[str, str]]:
    """応答に付ける計測 header 一式。失敗しても例外を外へ出さない。"""
    out: list[tuple[str, str]] = [(H_VERSION, PROTOCOL_VERSION), (H_BOOT_ID, BOOT_ID)]
    try:
        out += lock_headers(metrics)
    except Exception:  # noqa: BLE001
        out.append((H_LOCK_STATE, "unavailable"))
    if error_code is not None:
        out.append((H_ERROR_CODE, error_code))
    if include_slot and error_code not in AUTH_REJECTION_CODES:
        try:
            out += history_slot.snapshot()
        except Exception:  # noqa: BLE001
            pass
    return out
