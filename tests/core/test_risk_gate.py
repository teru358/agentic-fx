from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.config import load_settings
from agentic_fx.core.contracts import (
    Horizon, InstrumentSpec, Origin, Quote, TradeIntent,
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
                now=NOW)
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
