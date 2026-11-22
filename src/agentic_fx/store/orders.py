"""orders 永続化 CRUD。状態遷移の許可判定は core (プラン 2) の責務。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from agentic_fx.core.contracts import NON_TERMINAL_STATUSES, OrderStatus

_KNOWN_STATUSES = tuple(status.value for status in OrderStatus)
_OPTIONAL = {
    "intent_id", "approval_id", "client_order_id", "quantity",
    "filled_quantity", "remaining_quantity", "avg_fill_price",
    "requested_price", "close_price", "stop_loss", "take_profit",
    "fees_swap", "realized_pnl", "close_reason", "broker_order_id",
    "broker_position_id", "broker_synced_at", "expires_at",
    "filled_at", "closed_at",
}
# 発注の保護条件 (価格・SL・TP) は insert で決まり、その後は変えない。direction は
# insert の集合に無いので update も受けない。停止中の接触検査はこの不変を前提にする
_IMMUTABLE_AFTER_INSERT = {"requested_price", "stop_loss", "take_profit"}
_UPDATABLE = _OPTIONAL - _IMMUTABLE_AFTER_INSERT


def insert(conn: sqlite3.Connection, *, pair: str, direction: str,
           entry_type: str, horizon: str, status: str,
           now: datetime, **optional) -> int:
    unknown = set(optional) - _OPTIONAL
    if unknown:
        raise ValueError(f"unknown order fields: {unknown}")
    cols = ["pair", "direction", "entry_type", "horizon", "status",
            "created_at", "updated_at", *optional]
    vals = [pair, direction, entry_type, horizon, str(status),
            now.isoformat(), now.isoformat(), *optional.values()]
    q = f"INSERT INTO orders ({','.join(cols)}) VALUES ({','.join('?' * len(vals))})"
    cur = conn.execute(q, vals)
    conn.commit()
    return cur.lastrowid


def get(conn: sqlite3.Connection, order_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    return dict(row) if row else None


def update_fields(conn: sqlite3.Connection, order_id: int, *,
                  now: datetime, **fields) -> None:
    frozen = set(fields) & _IMMUTABLE_AFTER_INSERT
    if frozen:
        raise ValueError(f"immutable order fields: {frozen}")
    unknown = set(fields) - _UPDATABLE - {"status"}
    if unknown:
        raise ValueError(f"unknown order fields: {unknown}")
    fields = {k: (str(v) if k == "status" else v) for k, v in fields.items()}
    sets = ", ".join(f"{k}=?" for k in fields) + ", updated_at=?"
    conn.execute(f"UPDATE orders SET {sets} WHERE id=?",
                 [*fields.values(), now.isoformat(), order_id])
    conn.commit()


def list_by_status(conn: sqlite3.Connection, *statuses) -> list[dict]:
    marks = ",".join("?" * len(statuses))
    rows = conn.execute(
        f"SELECT * FROM orders WHERE status IN ({marks}) ORDER BY id",
        [str(s) for s in statuses]).fetchall()
    return [dict(r) for r in rows]


_ISO_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:*"


def list_overlapping(conn: sqlite3.Connection, pairs: list[str], start: datetime,
                     end: datetime) -> list[dict]:
    """[start, end) と生存期間が重なり得る注文 (全状態、id 順)。

    時間で除外するのは、既知の状態で、作成・終了の時刻が読め、重ならないと分かる注文だけ。
    未知の状態・読めない時刻の注文は候補に残し、評価 (接触の判定) が不明として扱う。
    保存の時刻表記が UTC 以外でも取りこぼさないよう上下を 1 日広げる。厳密な重なりの判定は
    呼び出し側が窓で行う。"""
    if not pairs:
        return []
    pair_marks = ",".join("?" * len(pairs))
    live_marks = ",".join("?" * len(NON_TERMINAL_STATUSES))
    known_marks = ",".join("?" * len(_KNOWN_STATUSES))
    day = timedelta(days=1)
    rows = conn.execute(
        f"SELECT * FROM orders WHERE pair IN ({pair_marks}) AND ("
        f"status NOT IN ({known_marks}) OR created_at NOT GLOB ? "
        f"OR COALESCE(closed_at, updated_at) NOT GLOB ? "
        f"OR (created_at < ? AND (status IN ({live_marks}) "
        f"OR COALESCE(closed_at, updated_at) > ?))) ORDER BY id",
        [*pairs, *_KNOWN_STATUSES, _ISO_GLOB, _ISO_GLOB, (end + day).isoformat(),
         *NON_TERMINAL_STATUSES, (start - day).isoformat()]).fetchall()
    return [dict(r) for r in rows]
