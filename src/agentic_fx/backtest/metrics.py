"""バックテスト成績集計 (プラン 6 Task 8)。

Task 7 の ``BacktestResult`` (orders は orders テーブルの生行、equity_curve
は実現損益ベース) から成績指標を集計する。closed 注文のみを対象にする。

fail closed 方針 (上書き節 A): closed 行の ``realized_pnl`` /
``stop_loss`` / ``avg_fill_price`` / ``quantity`` が欠損、あるいは
avg_r のリスク額が 0 (``avg_fill_price == stop_loss``) になるのは、
実運用の risk gate 契約上あり得ないデータ破損なので黙って skip せず
``ValueError`` にする。
"""
from __future__ import annotations

from agentic_fx.backtest.runner import BacktestResult
from agentic_fx.datafeed.price_provider import _SPECS

# §6: 30 取引未満は足切り／採用いずれの判定にも使わない。
EVALUABLE_MIN_TRADES = 30


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


def _closed_metrics(closed: list[dict]) -> tuple[float, float, int, float, list[float]]:
    """closed 行を 1 回走査して (total_pnl, gross合計対, wins, ...) を出す。

    戻り値: (total_pnl, gross_profit, gross_loss, wins, r_multiples)
    """
    total_pnl = 0.0
    gross_profit = 0.0
    gross_loss = 0.0
    wins = 0
    r_multiples: list[float] = []

    for row in closed:
        pnl = row["realized_pnl"]
        if pnl is None:
            raise ValueError(
                f"closed order id={row.get('id')!r} has realized_pnl=None "
                "(data corruption — closed orders must have realized_pnl)")
        stop_loss = row["stop_loss"]
        avg_fill_price = row["avg_fill_price"]
        quantity = row["quantity"]
        if stop_loss is None:
            raise ValueError(
                f"closed order id={row.get('id')!r} has stop_loss=None "
                "(data corruption)")
        if avg_fill_price is None:
            raise ValueError(
                f"closed order id={row.get('id')!r} has avg_fill_price=None "
                "(data corruption)")
        if quantity is None or quantity == 0:
            raise ValueError(
                f"closed order id={row.get('id')!r} has invalid "
                f"quantity={quantity!r} (data corruption)")
        risk_price = abs(avg_fill_price - stop_loss)
        if risk_price == 0:
            raise ValueError(
                f"closed order id={row.get('id')!r} has zero risk "
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
