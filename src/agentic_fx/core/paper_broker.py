"""PaperBroker — Broker 境界の paper 実装。SL/TP 添付は常に成功する想定。

Key assumptions (to avoid double-counting fees):
- realized_pnl is commission-deducted (net value after commission_per_lot)
- fees_swap is overnight swap fees ONLY (separate from realized_pnl, not included in it)
- balance = starting_balance + Σ realized_pnl − Σ fees_swap (no double subtraction of commission)
"""
from __future__ import annotations

import sqlite3

from agentic_fx.config import Settings
from agentic_fx.core.contracts import BrokerResult, Clock


def compute_pnl(order_row: dict, close_price: float, *, contract_size: float,
                commission_per_lot: float) -> float:
    sign = 1.0 if order_row["direction"] == "long" else -1.0
    qty = order_row["quantity"]
    gross = (close_price - order_row["avg_fill_price"]) * contract_size \
        * qty * sign
    return gross - commission_per_lot * qty


class PaperBroker:
    def __init__(self, conn: sqlite3.Connection, settings: Settings,
                 clock: Clock) -> None:
        self._conn = conn
        self._settings = settings
        self._clock = clock

    def equity(self) -> tuple[float, float]:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) AS pnl, "
            "COALESCE(SUM(fees_swap), 0) AS fees FROM orders").fetchone()
        balance = self._settings.paper.starting_balance + row["pnl"] - row["fees"]
        return balance, balance

    def submit(self, order_row: dict, entry_price: float) -> BrokerResult:
        return BrokerResult(status="ok",
                            broker_order_id=f"paper-{order_row['id']}",
                            broker_position_id=f"paper-{order_row['id']}")

    def close(self, order_row: dict, price: float, reason: str) -> BrokerResult:
        return BrokerResult(status="ok",
                            broker_order_id=order_row.get("broker_order_id"),
                            broker_position_id=order_row.get(
                                "broker_position_id"))

    def cancel(self, order_row: dict) -> BrokerResult:
        return BrokerResult(status="ok")

    def reconcile(self, order_row: dict) -> BrokerResult:
        """unknown 状態の照会。paper は常に「注文なし = 取消済み扱い」を返す。
        Phase 3 の Mt5Broker は注文履歴・建玉を照会して真の状態を返す。"""
        return BrokerResult(status="ok", message="not_found")
