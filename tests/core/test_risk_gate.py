from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.config import load_settings
from agentic_fx.core.contracts import (
    Action, Direction, EntryType, Horizon, InstrumentSpec, Origin, Quote,
    TradeIntent,
)
from agentic_fx.core.risk_gate import GateContext, GateResult, evaluate

RISK = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example").risk
NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)  # 水曜
SPEC = InstrumentSpec(symbol="USDJPY", pip_size=0.01, min_lot=0.01,
                      max_lot=50.0, lot_step=0.01, contract_size=100_000)
QUOTE = Quote(symbol="USDJPY", bid=148.49, ask=148.51, ts=NOW, source="test")


def _intent(**over):
    d = {"action": "open", "pair": "USDJPY", "direction": "long",
         "entry_type": "limit", "horizon": "day", "limit_price": 148.20,
         "expires_in": "4h", "stop_loss": 147.80, "take_profit": 149.00,
         "confidence": 0.7, "reasoning": "t"}
    d.update(over)
    return TradeIntent.from_llm_dict(d, origin=Origin.SCHEDULER)


def _ctx(**over):
    base = dict(quote=QUOTE, spec=SPEC, equity=1_000_000.0, hwm=1_000_000.0,
                daily_start_equity=1_000_000.0, open_position_count=0,
                existing_risk_total=0.0, existing_notional=0.0,
                kill_switch_latched=False, has_unresolved_unknown=False,
                now=NOW, account_currency="JPY")
    base.update(over)
    return GateContext(**base)


def test_valid_intent_accepted_with_size():
    r = evaluate(_intent(), _ctx(), RISK)
    assert r.accepted and r.size is not None and r.size.quantity > 0
    assert r.entry_price == 148.20


def test_market_entry_price_is_ask_for_long():
    it = _intent(entry_type="market", limit_price=None, expires_in=None)
    r = evaluate(it, _ctx(), RISK)
    assert r.entry_price == QUOTE.ask


def test_missing_tp_rejected():
    r = evaluate(_intent(take_profit=None), _ctx(), RISK)
    assert not r.accepted and any("take_profit" in x for x in r.reasons)


def test_sl_wrong_side_rejected():
    r = evaluate(_intent(stop_loss=148.90), _ctx(), RISK)  # long なのに SL > entry
    assert not r.accepted and any("stop-loss side" in x for x in r.reasons)


def test_sl_distance_bounds():
    too_near = evaluate(_intent(stop_loss=148.18), _ctx(), RISK)  # 2pips
    assert any("sl distance" in x for x in too_near.reasons)
    too_far = evaluate(_intent(stop_loss=145.00, take_profit=153.5),
                       _ctx(), RISK)  # 320pips
    assert any("sl distance" in x for x in too_far.reasons)


def test_rr_below_min_rejected():
    # RR = (0.40-0.01)/(0.40+0.01) ≈ 0.95 < 1.5
    r = evaluate(_intent(take_profit=148.60), _ctx(), RISK)
    assert any("rr" in x for x in r.reasons)


def test_limit_deviation_rejected():
    r = evaluate(_intent(limit_price=146.0, stop_loss=145.6,
                         take_profit=146.7), _ctx(), RISK)  # 乖離 >0.5%
    assert any("deviation" in x for x in r.reasons)


def test_kill_switch_latched_rejects():
    r = evaluate(_intent(), _ctx(kill_switch_latched=True), RISK)
    assert any("kill" in x for x in r.reasons)


def test_drawdown_triggers_kill():
    r = evaluate(_intent(), _ctx(equity=979_000.0), RISK)  # DD 2.1%
    assert any("kill" in x for x in r.reasons)


def test_daily_loss_rejects():
    r = evaluate(_intent(), _ctx(equity=989_000.0,
                                 daily_start_equity=1_000_000.0), RISK)  # -1.1%
    assert any("daily" in x for x in r.reasons)


def test_daily_start_unknown_fail_closed():
    r = evaluate(_intent(), _ctx(daily_start_equity=None), RISK)
    assert not r.accepted and any("daily" in x for x in r.reasons)


def test_max_positions_counts_pending():
    r = evaluate(_intent(), _ctx(open_position_count=2), RISK)
    assert any("positions" in x for x in r.reasons)


def test_total_risk_reservation():
    # 既存リスク 12,000 + 新規 ≈5,000 > 1.5% (15,000)
    r = evaluate(_intent(), _ctx(existing_risk_total=12_000.0), RISK)
    assert any("total risk" in x for x in r.reasons)


def test_leverage_cap():
    r = evaluate(_intent(), _ctx(existing_notional=9_990_000_000.0), RISK)
    assert any("leverage" in x for x in r.reasons)


def test_friday_swing_cutoff():
    fri = datetime(2026, 7, 24, 19, 0, tzinfo=timezone.utc)
    q = Quote("USDJPY", 148.49, 148.51, fri, "test")
    r = evaluate(_intent(horizon="swing"),
                 _ctx(now=fri, quote=q), RISK)
    assert any("friday" in x for x in r.reasons)
    day_ok = evaluate(_intent(), _ctx(now=fri, quote=q), RISK)
    assert day_ok.accepted


def test_all_violations_listed():
    r = evaluate(_intent(take_profit=None, stop_loss=148.90),
                 _ctx(open_position_count=2), RISK)
    assert len(r.reasons) >= 3  # 先頭で打ち切らない


def test_unresolved_unknown_blocks_open():
    r = evaluate(_intent(), _ctx(has_unresolved_unknown=True), RISK)
    assert not r.accepted and any("unknown" in x for x in r.reasons)


def test_nan_context_fail_closed():
    r = evaluate(_intent(), _ctx(equity=float("nan")), RISK)
    assert not r.accepted and any("invalid context" in x for x in r.reasons)


def test_nan_quote_fail_closed():
    bad = Quote("USDJPY", float("nan"), 148.51, NOW, "test")
    r = evaluate(_intent(), _ctx(quote=bad), RISK)
    assert not r.accepted and any("invalid context" in x for x in r.reasons)


def test_quote_symbol_mismatch_fail_closed():
    other = Quote("EURUSD", 1.10, 1.1002, NOW, "test")
    r = evaluate(_intent(), _ctx(quote=other), RISK)
    assert not r.accepted and any("symbol" in x for x in r.reasons)


# --- レビュー指摘対応 (task-5 修正: A/B/C/D/E) ---

def test_daily_start_equity_nan_fail_closed():
    # A-1 (Critical): NaN は None と同様に fail closed でなければならない
    r = evaluate(_intent(), _ctx(daily_start_equity=float("nan")), RISK)
    assert not r.accepted and any("invalid context" in x for x in r.reasons)


def test_daily_start_equity_zero_fail_closed_no_exception():
    # A-2 (Important): 0.0 は ZeroDivisionError を漏らさず却下すること
    r = evaluate(_intent(), _ctx(daily_start_equity=0.0), RISK)
    assert not r.accepted and any("invalid context" in x for x in r.reasons)


def test_daily_start_equity_negative_fail_closed():
    r = evaluate(_intent(), _ctx(daily_start_equity=-1_000_000.0), RISK)
    assert not r.accepted and any("invalid context" in x for x in r.reasons)


def test_hwm_zero_fail_closed_kill_switch_not_disabled():
    # A-3 (Important): hwm<=0 で kill switch (DD 判定) が無効化されてはならない
    r = evaluate(_intent(), _ctx(equity=1_000_000.0, hwm=0.0,
                                 daily_start_equity=1_000_000.0), RISK)
    assert not r.accepted and any("invalid context" in x for x in r.reasons)


def test_hwm_negative_fail_closed():
    r = evaluate(_intent(), _ctx(equity=1_000_000.0, hwm=-5.0), RISK)
    assert not r.accepted and any("invalid context" in x for x in r.reasons)


def test_negative_existing_risk_total_fail_closed():
    # A-4 (Important): 負値は総リスク上限を素通りさせるため fail closed
    r = evaluate(_intent(), _ctx(existing_risk_total=-1.0), RISK)
    assert not r.accepted and any("invalid context" in x for x in r.reasons)


def test_negative_existing_notional_fail_closed():
    r = evaluate(_intent(), _ctx(existing_notional=-1.0), RISK)
    assert not r.accepted and any("invalid context" in x for x in r.reasons)


def test_naive_now_fail_closed():
    # A: ctx.now が tz-naive だと day horizon で friday cutoff チェックを
    # 経由せず素通りしてしまう問題への対応
    naive_now = datetime(2026, 7, 22, 12, 0)  # tzinfo なし
    r = evaluate(_intent(), _ctx(now=naive_now), RISK)
    assert not r.accepted and any("invalid context" in x for x in r.reasons)


def test_nonpositive_stop_loss_fail_closed():
    r = evaluate(_intent(stop_loss=-1.0), _ctx(), RISK)
    assert not r.accepted and any("invalid context" in x for x in r.reasons)


def test_nonfinite_take_profit_fail_closed():
    # TradeIntent.from_llm_dict はパース時に finite を強制するため、ここでは
    # 「from_llm_dict を経由しない呼び出し元」を想定し、dataclass を直接構築して
    # gate 自身の防御的検証 (defense-in-depth) を確認する
    bad = TradeIntent(action=Action.OPEN, origin=Origin.SCHEDULER,
                      pair="USDJPY", direction=Direction.LONG,
                      entry_type=EntryType.LIMIT, horizon=Horizon.DAY,
                      limit_price=148.20, expires_in_h=4.0, stop_loss=147.80,
                      take_profit=float("inf"), confidence=0.7, reasoning="t")
    r = evaluate(bad, _ctx(), RISK)
    assert not r.accepted and any("invalid context" in x for x in r.reasons)


def test_nonpositive_limit_price_fail_closed():
    r = evaluate(_intent(limit_price=0.0), _ctx(), RISK)
    assert not r.accepted and any("invalid context" in x for x in r.reasons)


def test_kill_switch_latched_and_drawdown_both_listed():
    # B: ラッチ済み かつ DD 超過のとき、両方の理由が reasons に入ること
    r = evaluate(_intent(), _ctx(equity=979_000.0, kill_switch_latched=True), RISK)
    assert any("kill switch latched" in x for x in r.reasons)
    assert any("drawdown" in x for x in r.reasons)


def test_rejected_result_has_no_size():
    # D: 却下時は size が None であること (executor の誤用防止)
    r = evaluate(_intent(take_profit=None), _ctx(), RISK)
    assert not r.accepted and r.size is None


def test_hold_action_raises_value_error_not_assert():
    # C: assert ではなく ValueError (python -O でも安全に落ちる)
    hold = _intent(action="hold")
    with pytest.raises(ValueError):
        evaluate(hold, _ctx(), RISK)


def test_eurusd_intent_rejected_via_sizing_failure():
    # E: 口座通貨 (JPY) とクォート通貨 (USD) が異なる EURUSD は
    # sizing 側で fail closed され、gate はそれを理由に却下すること
    eur_spec = InstrumentSpec(symbol="EURUSD", pip_size=0.0001, min_lot=0.01,
                              max_lot=50.0, lot_step=0.01, contract_size=100_000)
    eur_quote = Quote("EURUSD", 1.0999, 1.1001, NOW, "test")
    eur_intent = _intent(pair="EURUSD", limit_price=1.1000, stop_loss=1.0960,
                         take_profit=1.1120, expires_in="4h")
    r = evaluate(eur_intent, _ctx(quote=eur_quote, spec=eur_spec), RISK)
    assert not r.accepted and any("sizing failed" in x for x in r.reasons)
    assert r.size is None
