"""バックテスト成績集計 (プラン 6 Task 8)。

Task 7 の ``BacktestResult`` (orders は orders テーブルの生行、equity_curve
は実現損益ベース) から成績指標を集計する。closed 注文のみを対象にする。

fail closed 方針 (上書き節 A + fix round 1 F2): closed 行の
``realized_pnl`` / ``stop_loss`` / ``avg_fill_price`` / ``quantity`` が
欠損・非有限 (nan/inf)、``quantity`` が 0 以下、あるいは avg_r のリスク額
が 0 (``avg_fill_price == stop_loss``) になるのは、実運用の risk gate
契約上あり得ないデータ破損なので黙って skip せず ``ValueError`` にする。
"""
from __future__ import annotations

import math

from agentic_fx.backtest.runner import BacktestResult
from agentic_fx.datafeed.price_provider import _SPECS

# §6: 30 取引未満は足切り／採用いずれの判定にも使わない。
EVALUABLE_MIN_TRADES = 30

# compute_metrics の返却キー一覧 (公開定数)。fix round 1 F1 (codex Important):
# save_harness_run(metrics={...}) は任意 dict を受け取れるため、metrics に
# "period_start"/"period_end" 等の余計なキーを混入させれば in_sample_view
# の列遮断 (holdout 遮断 1) を metrics_json 経由で密輸できてしまう。
# 遮断境界は「改善ループが読む唯一の面」である in_sample_view 側なので、
# 読み側 (store/backtest_runs.py) がこの定数で白リスト濾過する。
METRIC_KEYS = frozenset({
    "trades", "pf", "win_rate", "avg_r", "max_drawdown", "total_pnl",
    "evaluable", "fallback_spread_used",
})


def _max_drawdown(equity_curve: list[tuple[str, float]]) -> float:
    """equity_curve のピーク比ドローダウン (0.0〜1.0 想定) の最大値。

    Task 7 契約で curve は必ず先頭 seed 点を持つ (空にならない)。
    equity <= 0 のような異常値も特別扱いせずそのまま計算する
    (上書き節 A: ガード不要)。
    """
    peak = equity_curve[0][1]
    max_dd = 0.0
    for _, equity in equity_curve:
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _require_finite(value: float | None, field: str, order_id) -> float:
    """fix round 1 F2 (codex Minor): None だけでなく非有限値 (nan/inf) も
    fail closed する。負・0 の quantity は呼び出し側で別途弾く (符号は
    フィールドごとに意味が違うため、ここでは有限性のみを見る)。
    """
    if value is None:
        raise ValueError(
            f"closed order id={order_id!r} has {field}=None "
            "(data corruption)")
    if not math.isfinite(value):
        raise ValueError(
            f"closed order id={order_id!r} has non-finite {field}={value!r} "
            "(data corruption)")
    return value


def _closed_metrics(closed: list[dict]) -> tuple[float, float, float, int, list[float]]:
    """closed 行を 1 回走査して (total_pnl, gross_profit, gross_loss, wins,
    r_multiples) を出す。"""
    total_pnl = 0.0
    gross_profit = 0.0
    gross_loss = 0.0
    wins = 0
    r_multiples: list[float] = []

    for row in closed:
        order_id = row.get("id")
        pnl = _require_finite(row["realized_pnl"], "realized_pnl", order_id)
        stop_loss = _require_finite(row["stop_loss"], "stop_loss", order_id)
        avg_fill_price = _require_finite(
            row["avg_fill_price"], "avg_fill_price", order_id)
        quantity = row["quantity"]
        if quantity is None or not math.isfinite(quantity) or quantity <= 0:
            raise ValueError(
                f"closed order id={order_id!r} has invalid "
                f"quantity={quantity!r} (data corruption — must be a "
                "finite positive number)")
        risk_price = abs(avg_fill_price - stop_loss)
        if risk_price == 0:
            raise ValueError(
                f"closed order id={order_id!r} has zero risk "
                "(avg_fill_price == stop_loss — data corruption)")

        contract_size = _SPECS[row["pair"]].contract_size  # 未知 pair は KeyError で fail closed
        risk_amount = risk_price * quantity * contract_size
        r_multiples.append(pnl / risk_amount)

        total_pnl += pnl
        if pnl > 0:
            wins += 1
            gross_profit += pnl
        elif pnl < 0:
            gross_loss += -pnl

    return total_pnl, gross_profit, gross_loss, wins, r_multiples


def compute_metrics(result: BacktestResult) -> dict:
    """``BacktestResult`` から成績指標 dict を集計する (closed のみ)。"""
    closed = [row for row in result.orders if row["status"] == "closed"]
    trades = len(closed)
    max_drawdown = _max_drawdown(result.equity_curve)

    if trades == 0:
        return {
            "trades": 0,
            "pf": None,
            "win_rate": None,
            "avg_r": None,
            "max_drawdown": max_drawdown,
            "total_pnl": 0.0,
            "evaluable": False,
            "fallback_spread_used": result.fallback_spread_used,
        }

    total_pnl, gross_profit, gross_loss, wins, r_multiples = _closed_metrics(closed)

    pf = None if gross_loss == 0 else gross_profit / gross_loss
    win_rate = wins / trades
    avg_r = sum(r_multiples) / len(r_multiples)

    return {
        "trades": trades,
        "pf": pf,
        "win_rate": win_rate,
        "avg_r": avg_r,
        "max_drawdown": max_drawdown,
        "total_pnl": total_pnl,
        "evaluable": trades >= EVALUABLE_MIN_TRADES,
        "fallback_spread_used": result.fallback_spread_used,
    }
