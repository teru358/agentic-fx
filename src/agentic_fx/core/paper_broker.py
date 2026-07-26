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
        """unknown 状態の照会。paper は常に「注文なし = 取消済み扱い」を返す
        (`status="ok", message="not_found"`)。

        レビュー修正 (codex 4): 呼び出し側 (`scheduler._resolve_unknowns`) は
        `status == "ok" かつ message == "not_found"` のときだけ終端状態
        (rejected/cancelled) へ変換する。`status == "ok"` だけを見て終端化
        すると、将来の実装が「照会成功・注文はまだ存在する」を `ok` で返した
        場合に、実注文が残っているのに DB を終端にしてしまう。

        Phase 3 の Mt5Broker は本物の照会結果を `message` で表現する必要が
        ある (例: "not_found" / それ以外は再試行対象として扱われる)。将来的
        には構造化された reconcile 結果型 (not_found / pending / filled /
        open / closed / cancelled、数量、価格、broker ID、保護状態) を
        導入すべきだが、それは Phase 3 のスコープとする。
        """
        return BrokerResult(status="ok", message="not_found")
