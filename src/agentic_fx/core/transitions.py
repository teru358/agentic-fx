"""orders 状態遷移規則 — 設計書 §12 の遷移図を機械可読化。store は無検証 CRUD のまま。"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from agentic_fx.core.contracts import OrderStatus as S
from agentic_fx.store import orders


class IllegalTransition(Exception):
    pass


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
    return orders.get(conn, order_id)
