"""口座スナップショットと HWM (入出金調整済み) — 設計書 §5 kill switch 定義。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from agentic_fx.core.market_hours import trading_day_start
from agentic_fx.core.timeutil import as_utc
from agentic_fx.store import snapshots


def record_snapshot(conn: sqlite3.Connection, *, now: datetime, balance: float,
                    equity: float, cashflow: float = 0.0,
                    source: str = "paper") -> dict:
    now = as_utc(now)
    # read-modify-write を単一トランザクションで (HWM の巻き戻り防止)
    conn.execute("BEGIN IMMEDIATE")
    try:
        prev = snapshots.latest(conn)
        prev_hwm = (prev["hwm"] + cashflow) if prev else equity
        hwm = max(prev_hwm, equity)
        conn.execute(
            "INSERT INTO account_snapshots (ts, balance, equity, hwm, "
            "cashflow, source) VALUES (?,?,?,?,?,?)",
            (now.isoformat(), balance, equity, hwm, cashflow, source))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return {"ts": now.isoformat(), "balance": balance, "equity": equity,
            "hwm": hwm, "cashflow": cashflow, "source": source}


def drawdown_pct(equity: float, hwm: float) -> float:
    if hwm <= 0:
        return 0.0
    return max(0.0, (hwm - equity) / hwm * 100.0)


def daily_start_equity(conn: sqlite3.Connection, now: datetime, *,
                       max_age_hours: float = 48.0) -> float | None:
    now = as_utc(now)
    day_start = trading_day_start(now)
    row = snapshots.first_since(conn, day_start)
    if row is None:
        row = snapshots.last_before(conn, day_start)
        if row is None:
            return None
        age = now - datetime.fromisoformat(row["ts"])
        if age > timedelta(hours=max_age_hours):
            return None  # 陳腐化 → fail closed
    # 日初以降の入出金を加算 (入金を損失/利益に混ぜない)
    cf = conn.execute(
        "SELECT COALESCE(SUM(cashflow), 0) AS cf FROM account_snapshots "
        "WHERE ts > ? AND ts <= ?",
        (row["ts"], now.isoformat())).fetchone()["cf"]
    return row["equity"] + cf


def current_account(conn: sqlite3.Connection, now: datetime, *,
                    max_age_min: float = 10.0) -> tuple[float, float] | None:
    now = as_utc(now)
    row = snapshots.latest(conn)
    if row is None:
        return None
    if now - datetime.fromisoformat(row["ts"]) > timedelta(minutes=max_age_min):
        return None  # 陳腐化 → fail closed
    return row["equity"], row["hwm"]
