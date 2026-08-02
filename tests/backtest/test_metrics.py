"""compute_metrics のテスト (プラン 6 Task 8)。

上書き節 A (task-8-brief.md, コントローラ照合 2026-08-01) の入力契約:
- BacktestResult.orders は orders テーブルの生行 dict (status/realized_pnl/
  stop_loss/avg_fill_price/quantity/pair)。closed 判定は status=="closed"。
- fail closed: closed 行で realized_pnl/stop_loss/avg_fill_price/quantity
  が欠損、あるいはリスク額 0 (avg_fill_price==stop_loss) は ValueError。
- avg_r のリスク額 = abs(avg_fill_price - stop_loss) * quantity *
  contract_size (USDJPY は 100_000)。
"""
from __future__ import annotations

import pytest

from agentic_fx.backtest.metrics import METRIC_KEYS, compute_metrics
from agentic_fx.backtest.runner import BacktestResult

from tests.backtest.conftest import H


def _closed_row(pnl: float, *, avg_fill_price=148.0, stop_loss=147.8,
                quantity=0.1, pair="USDJPY", id_=1) -> dict:
    return {
        "id": id_,
        "status": "closed",
        "realized_pnl": pnl,
        "avg_fill_price": avg_fill_price,
        "stop_loss": stop_loss,
        "quantity": quantity,
        "pair": pair,
    }


def _result_with_closed(pnls: list[float], *, equity_curve=None,
                        fallback_spread_used=False) -> BacktestResult:
    orders = [_closed_row(p, id_=i) for i, p in enumerate(pnls)]
    curve = equity_curve if equity_curve is not None else [
        (H.isoformat(), 1_000_000.0)]
    return BacktestResult(
        orders=orders, equity_curve=curve, start=H, end=H,
        source="dukascopy", fallback_spread_used=fallback_spread_used)


def test_metrics_basic_and_evaluable_threshold():
    res = _result_with_closed(pnls=[+100.0] * 20 + [-50.0] * 10)
    m = compute_metrics(res)
    assert m["trades"] == 30 and m["evaluable"] is True
    assert abs(m["pf"] - (2000.0 / 500.0)) < 1e-9

    m2 = compute_metrics(_result_with_closed(pnls=[+100.0] * 29))
    assert m2["evaluable"] is False


def test_zero_trades_returns_none_metrics_but_not_evaluable():
    res = BacktestResult(
        orders=[], equity_curve=[(H.isoformat(), 1_000_000.0)], start=H,
        end=H, source="dukascopy", fallback_spread_used=True)
    m = compute_metrics(res)
    assert m == {
        "trades": 0, "pf": None, "win_rate": None, "avg_r": None,
        "total_pnl": 0.0, "max_drawdown": 0.0, "evaluable": False,
        "fallback_spread_used": True,
    }


def test_pf_none_when_no_losses():
    res = _result_with_closed(pnls=[+100.0] * 30)
    m = compute_metrics(res)
    assert m["pf"] is None
    assert m["win_rate"] == 1.0


def test_pf_zero_when_no_profit_but_losses_exist():
    res = _result_with_closed(pnls=[-10.0] * 30)
    m = compute_metrics(res)
    assert m["pf"] == 0.0
    assert m["win_rate"] == 0.0


def test_zero_pnl_trade_not_counted_as_win():
    res = _result_with_closed(pnls=[0.0] * 30)
    m = compute_metrics(res)
    assert m["win_rate"] == 0.0


def test_only_closed_orders_counted():
    orders = [_closed_row(100.0, id_=1),
              {"id": 2, "status": "open", "realized_pnl": None,
               "avg_fill_price": None, "stop_loss": None, "quantity": None,
               "pair": "USDJPY"}]
    res = BacktestResult(
        orders=orders, equity_curve=[(H.isoformat(), 1_000_000.0)], start=H,
        end=H, source="dukascopy", fallback_spread_used=False)
    m = compute_metrics(res)
    assert m["trades"] == 1


def test_avg_r_uses_contract_size():
    # risk_price=0.2, quantity=0.1, contract_size=100_000 -> risk_amount=2000
    row = _closed_row(1000.0, avg_fill_price=148.0, stop_loss=147.8,
                      quantity=0.1)
    res = BacktestResult(
        orders=[row], equity_curve=[(H.isoformat(), 1_000_000.0)], start=H,
        end=H, source="dukascopy", fallback_spread_used=False)
    m = compute_metrics(res)
    assert abs(m["avg_r"] - 0.5) < 1e-9


def test_max_drawdown_from_equity_curve():
    curve = [
        (H.isoformat(), 1000.0),
        (H.isoformat(), 1200.0),   # peak
        (H.isoformat(), 900.0),    # dd = (1200-900)/1200 = 0.25
        (H.isoformat(), 1100.0),
    ]
    res = _result_with_closed(pnls=[], equity_curve=curve)
    m = compute_metrics(res)
    assert abs(m["max_drawdown"] - 0.25) < 1e-9


def test_fallback_spread_used_passthrough():
    res = _result_with_closed(pnls=[100.0] * 30, fallback_spread_used=True)
    m = compute_metrics(res)
    assert m["fallback_spread_used"] is True


@pytest.mark.parametrize("field", ["realized_pnl", "stop_loss",
                                    "avg_fill_price"])
def test_fail_closed_on_missing_field(field):
    row = _closed_row(100.0)
    row[field] = None
    res = BacktestResult(
        orders=[row], equity_curve=[(H.isoformat(), 1_000_000.0)], start=H,
        end=H, source="dukascopy", fallback_spread_used=False)
    with pytest.raises(ValueError):
        compute_metrics(res)


@pytest.mark.parametrize("quantity", [None, 0])
def test_fail_closed_on_invalid_quantity(quantity):
    row = _closed_row(100.0)
    row["quantity"] = quantity
    res = BacktestResult(
        orders=[row], equity_curve=[(H.isoformat(), 1_000_000.0)], start=H,
        end=H, source="dukascopy", fallback_spread_used=False)
    with pytest.raises(ValueError):
        compute_metrics(res)


def test_fail_closed_on_zero_risk_amount():
    row = _closed_row(100.0, avg_fill_price=148.0, stop_loss=148.0)
    res = BacktestResult(
        orders=[row], equity_curve=[(H.isoformat(), 1_000_000.0)], start=H,
        end=H, source="dukascopy", fallback_spread_used=False)
    with pytest.raises(ValueError):
        compute_metrics(res)


def test_unknown_pair_raises_keyerror():
    row = _closed_row(100.0, pair="GBPUSD")
    res = BacktestResult(
        orders=[row], equity_curve=[(H.isoformat(), 1_000_000.0)], start=H,
        end=H, source="dukascopy", fallback_spread_used=False)
    with pytest.raises(KeyError):
        compute_metrics(res)


# --- fix round 1 (sonnet+codex 統合裁定 2026-08-01) --------------------------


def test_metric_keys_constant_matches_returned_dict_keys():
    """METRIC_KEYS (公開ホワイトリスト定数) が実際の返却キーと一致すること。
    trades==0 分岐・trades>0 分岐の両方で確認する (F1 の白リストが漏れなく
    保護対象を覆っているかのピン留め)。"""
    zero = compute_metrics(BacktestResult(
        orders=[], equity_curve=[(H.isoformat(), 1_000_000.0)], start=H,
        end=H, source="dukascopy", fallback_spread_used=False))
    assert set(zero.keys()) == METRIC_KEYS

    nonzero = compute_metrics(_result_with_closed(pnls=[100.0] * 30))
    assert set(nonzero.keys()) == METRIC_KEYS


def test_avg_r_multi_trade_average_is_not_sum():
    """F3 (sonnet I-1 killer): 3 取引の avg_r を手計算で pin する。
    sum(r_multiples) への変異 (/len 削除) を検出する。"""
    rows = [
        _closed_row(1000.0, avg_fill_price=148.0, stop_loss=147.8,
                   quantity=0.1, id_=1),   # risk=2000, r=0.5
        _closed_row(-2000.0, avg_fill_price=150.0, stop_loss=149.5,
                   quantity=0.2, id_=2),   # risk=10000, r=-0.2
        _closed_row(2500.0, avg_fill_price=145.0, stop_loss=144.0,
                   quantity=0.05, id_=3),  # risk=5000, r=0.5
    ]
    res = BacktestResult(
        orders=rows, equity_curve=[(H.isoformat(), 1_000_000.0)], start=H,
        end=H, source="dukascopy", fallback_spread_used=False)
    m = compute_metrics(res)
    expected_avg_r = (0.5 + -0.2 + 0.5) / 3
    assert abs(m["avg_r"] - expected_avg_r) < 1e-9
    # sum() への変異なら 0.8 になるはずなので、平均値との違いで確実に区別できる
    assert abs(m["avg_r"] - 0.8) > 1e-9


def test_max_drawdown_wired_through_nonzero_trades_branch():
    """F4 (sonnet I-2 killer): trades>0 の返却 dict に実際の max_drawdown
    が配線されていることを非フラットカーブで検証する (フラットカーブだと
    0.0 に固定した配線ミスと区別できない)。"""
    curve = [
        (H.isoformat(), 1000.0),
        (H.isoformat(), 1200.0),   # peak
        (H.isoformat(), 900.0),    # dd = (1200-900)/1200 = 0.25
        (H.isoformat(), 1100.0),
    ]
    res = _result_with_closed(pnls=[100.0] * 30, equity_curve=curve)
    m = compute_metrics(res)
    assert m["trades"] == 30
    assert abs(m["max_drawdown"] - 0.25) < 1e-9


def test_avg_r_short_side_uses_abs_risk_price():
    """F5 (sonnet I-3 killer): stop_loss > avg_fill_price (ショート側) でも
    リスク額は abs() で正になるため、勝ちトレードの avg_r は正の値になる
    (abs() が無いと risk_amount が負になり符号が反転する)。"""
    row = _closed_row(500.0, avg_fill_price=148.0, stop_loss=148.5,
                      quantity=0.1)  # risk=abs(148.0-148.5)*0.1*100000=5000
    res = BacktestResult(
        orders=[row], equity_curve=[(H.isoformat(), 1_000_000.0)], start=H,
        end=H, source="dukascopy", fallback_spread_used=False)
    m = compute_metrics(res)
    assert abs(m["avg_r"] - 0.1) < 1e-9
    assert m["avg_r"] > 0


def test_fail_closed_on_negative_quantity():
    """F2 (codex Minor): 負の quantity は 0/None と同様に fail closed。"""
    row = _closed_row(100.0)
    row["quantity"] = -0.1
    res = BacktestResult(
        orders=[row], equity_curve=[(H.isoformat(), 1_000_000.0)], start=H,
        end=H, source="dukascopy", fallback_spread_used=False)
    with pytest.raises(ValueError):
        compute_metrics(res)


def test_fail_closed_on_nan_avg_fill_price():
    """F2 (codex Minor): nan は None と同じくデータ破損として fail closed。"""
    row = _closed_row(100.0)
    row["avg_fill_price"] = float("nan")
    res = BacktestResult(
        orders=[row], equity_curve=[(H.isoformat(), 1_000_000.0)], start=H,
        end=H, source="dukascopy", fallback_spread_used=False)
    with pytest.raises(ValueError):
        compute_metrics(res)


def test_fail_closed_on_inf_realized_pnl():
    """F2 (codex Minor): inf は非標準 JSON (json.dumps は Infinity を出力
    してしまう) の metrics 伝播を防ぐため fail closed。"""
    row = _closed_row(float("inf"))
    res = BacktestResult(
        orders=[row], equity_curve=[(H.isoformat(), 1_000_000.0)], start=H,
        end=H, source="dukascopy", fallback_spread_used=False)
    with pytest.raises(ValueError):
        compute_metrics(res)
