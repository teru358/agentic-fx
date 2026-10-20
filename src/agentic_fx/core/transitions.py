"""orders 状態遷移規則 — 設計書 §12 の遷移図を機械可読化。store は無検証 CRUD のまま。"""
from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime

from agentic_fx.core.contracts import OrderStatus as S
from agentic_fx.store import orders


_log = logging.getLogger(__name__)


class IllegalTransition(Exception):
    pass


class ObserverError(Exception):
    """observer の登録・解除の誤り (同じ接続への重複登録など)。"""


# commit の後に (遷移後の行, 遷移前 status, 遷移先 status) で呼ばれる読み取り専用の関数。
# observer は flock もファイル I/O もしない。例外は握りつぶされ、遷移の結果に影響しない。
Observer = Callable[[dict, str, str], None]


class _Registry:
    """接続ごとに observer を 1 つだけ持つ登録簿。接続を強参照するので解除が要る。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_conn: dict[int, tuple[sqlite3.Connection, Observer]] = {}

    def add(self, conn: sqlite3.Connection, fn: Observer) -> object:
        if not callable(fn):
            raise ObserverError("observer must be callable")
        with self._lock:
            if id(conn) in self._by_conn:
                raise ObserverError("observer already registered for this connection")
            self._by_conn[id(conn)] = (conn, fn)
        return _Token(id(conn), fn)

    def remove(self, token: object) -> None:
        if not isinstance(token, _Token):
            raise ObserverError("unknown observer token")
        with self._lock:
            held = self._by_conn.get(token.conn_id)
            if held is not None and held[1] is token.fn:
                del self._by_conn[token.conn_id]

    def get(self, conn: sqlite3.Connection) -> Observer | None:
        held = self._by_conn.get(id(conn))
        return held[1] if held is not None and held[0] is conn else None


class _Token:
    __slots__ = ("conn_id", "fn")

    def __init__(self, conn_id: int, fn: Observer) -> None:
        self.conn_id, self.fn = conn_id, fn


_REGISTRY = _Registry()


def observe(conn: sqlite3.Connection, fn: Observer) -> object:
    """接続 `conn` 上の遷移を commit の後に観測する。同じ接続への重複登録は拒否する。"""
    return _REGISTRY.add(conn, fn)


def unobserve(token: object) -> None:
    """登録を解く。解除済みの token は何もしない。"""
    _REGISTRY.remove(token)


TERMINAL: frozenset[S] = frozenset(
    {S.CLOSED, S.CANCELLED, S.REJECTED, S.EXPIRED, S.INVALIDATED})

ALLOWED: dict[S, frozenset[S]] = {
    S.APPROVAL_PENDING: frozenset(
        {S.SUBMITTING, S.REJECTED, S.EXPIRED, S.INVALIDATED}),
    S.SUBMITTING: frozenset({S.SUBMITTED, S.REJECTED, S.SUBMIT_UNKNOWN}),
    S.SUBMITTED: frozenset({S.PENDING_FILL, S.PROTECTION_PENDING}),
    S.PENDING_FILL: frozenset(
        {S.PROTECTION_PENDING, S.EXPIRED, S.CANCELLING}),
    S.CANCELLING: frozenset(
        {S.CANCELLED, S.CANCEL_UNKNOWN, S.PROTECTION_PENDING,
         S.EXPIRED}),  # 取消/約定競合、期限切れ取消の broker 確認後
    S.CANCEL_UNKNOWN: frozenset({S.CANCELLED, S.PROTECTION_PENDING}),
    S.PROTECTION_PENDING: frozenset({S.OPEN, S.CLOSING}),  # 保護確認失敗→緊急クローズ
    S.OPEN: frozenset({S.CLOSING}),
    S.CLOSING: frozenset({S.CLOSED, S.CLOSE_UNKNOWN}),
    S.CLOSE_UNKNOWN: frozenset({S.CLOSED, S.CLOSING}),
    S.SUBMIT_UNKNOWN: frozenset(
        {S.SUBMITTED, S.PENDING_FILL, S.PROTECTION_PENDING, S.REJECTED}),
}


def transition(conn: sqlite3.Connection, order_id: int, to: S,
               now: datetime, **fields) -> dict:
    row = orders.get(conn, order_id)
    if row is None:
        raise IllegalTransition(f"order {order_id} not found")
    cur = S(row["status"])
    if cur in TERMINAL or to not in ALLOWED.get(cur, frozenset()):
        raise IllegalTransition(f"{cur.value} -> {to.value} is not allowed")
    orders.update_fields(conn, order_id, now=now, status=to, **fields)
    new = orders.get(conn, order_id)
    observer = _REGISTRY.get(conn)
    if observer is not None and new is not None:
        try:
            observer(dict(new), cur.value, to.value)
        except Exception:  # noqa: BLE001 — 通知は注文処理を止めない
            _log.warning("transition observer failed", exc_info=False)
    return new
