from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.core.contracts import (
    Action, Clock, Direction, EntryType, Horizon, IntentParseError,
    Origin, OrderStatus, FixedClock, Signal, StrategyAction,
    StrategyDecision, SystemClock, TradeIntent,
)


def _open_dict(**over):
    d = {
        "action": "open", "pair": "USDJPY", "direction": "long",
        "entry_type": "limit", "horizon": "day",
        "limit_price": 148.20, "expires_in": "4h",
        "stop_loss": 147.80, "take_profit": 149.00,
        "confidence": 0.7, "reasoning": "test",
    }
    d.update(over)
    return d


def test_from_llm_dict_open_limit():
    it = TradeIntent.from_llm_dict(_open_dict(), origin=Origin.SCHEDULER)
    assert it.action is Action.OPEN
    assert it.direction is Direction.LONG
    assert it.entry_type is EntryType.LIMIT
    assert it.horizon is Horizon.DAY
    assert it.expires_in_h == 4.0
    assert it.origin is Origin.SCHEDULER


def test_open_requires_stop_loss():
    with pytest.raises(IntentParseError, match="stop_loss"):
        TradeIntent.from_llm_dict(_open_dict(stop_loss=None), origin=Origin.SCHEDULER)


def test_open_requires_horizon():
    d = _open_dict()
    del d["horizon"]
    with pytest.raises(IntentParseError, match="horizon"):
        TradeIntent.from_llm_dict(d, origin=Origin.SCHEDULER)


def test_limit_requires_limit_price_and_expiry():
    with pytest.raises(IntentParseError, match="limit_price"):
        TradeIntent.from_llm_dict(_open_dict(limit_price=None), origin=Origin.SCHEDULER)


def test_market_rejects_limit_fields():
    d = _open_dict(entry_type="market")
    with pytest.raises(IntentParseError, match="limit"):
        TradeIntent.from_llm_dict(d, origin=Origin.SCHEDULER)


def test_close_requires_order_id():
    with pytest.raises(IntentParseError, match="order_id"):
        TradeIntent.from_llm_dict({"action": "close"}, origin=Origin.SCHEDULER)


def test_close_rejects_bool_true_order_id():
    with pytest.raises(IntentParseError, match="order_id"):
        TradeIntent.from_llm_dict({"action": "close", "order_id": True},
                                  origin=Origin.SCHEDULER)


def test_close_rejects_bool_false_order_id():
    with pytest.raises(IntentParseError, match="order_id"):
        TradeIntent.from_llm_dict({"action": "close", "order_id": False},
                                  origin=Origin.SCHEDULER)


def test_cancel_rejects_zero_order_id():
    with pytest.raises(IntentParseError, match="order_id"):
        TradeIntent.from_llm_dict({"action": "cancel", "order_id": 0},
                                  origin=Origin.SCHEDULER)


def test_cancel_rejects_negative_order_id():
    with pytest.raises(IntentParseError, match="order_id"):
        TradeIntent.from_llm_dict({"action": "cancel", "order_id": -1},
                                  origin=Origin.SCHEDULER)


def test_close_accepts_positive_order_id():
    it = TradeIntent.from_llm_dict({"action": "close", "order_id": 42},
                                   origin=Origin.SCHEDULER)
    assert it.order_id == 42


def test_hold_minimal():
    it = TradeIntent.from_llm_dict({"action": "hold", "reasoning": "様子見"},
                                   origin=Origin.SCHEDULER)
    assert it.action is Action.HOLD


def test_unknown_action_rejected():
    with pytest.raises(IntentParseError, match="action"):
        TradeIntent.from_llm_dict({"action": "buy"}, origin=Origin.SCHEDULER)


def test_nan_price_rejected():
    with pytest.raises(IntentParseError, match="finite"):
        TradeIntent.from_llm_dict(_open_dict(stop_loss=float("nan")),
                                  origin=Origin.SCHEDULER)


def test_order_status_has_all_states():
    names = {s.value for s in OrderStatus}
    assert {"approval_pending", "submitting", "submitted", "pending_fill",
            "protection_pending", "open", "closing", "closed", "cancelling",
            "cancelled", "cancel_unknown", "close_unknown", "submit_unknown",
            "rejected", "expired", "invalidated"} == names


def test_fixed_clock():
    dt = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    assert FixedClock(dt).now() == dt


def _bar_ts():
    return datetime(2026, 8, 2, 1, 0, tzinfo=timezone.utc)


def test_signal_accepts_valid_fields():
    s = Signal(plugin="rsi", pair="USDJPY", timeframe="1h", bar_ts=_bar_ts(),
               direction="long", strength=0.5, rationale="oversold")
    assert s.direction == Direction.LONG
    assert s.stop_loss is None
    assert s.take_profit is None


def test_signal_rejects_invalid_direction():
    with pytest.raises(ValueError, match="direction"):
        Signal(plugin="rsi", pair="USDJPY", timeframe="1h", bar_ts=_bar_ts(),
               direction="up", strength=0.5, rationale="x")


def test_signal_rejects_strength_out_of_range():
    with pytest.raises(ValueError, match="strength"):
        Signal(plugin="rsi", pair="USDJPY", timeframe="1h", bar_ts=_bar_ts(),
               direction="long", strength=1.5, rationale="x")


def test_signal_rejects_negative_strength():
    with pytest.raises(ValueError, match="strength"):
        Signal(plugin="rsi", pair="USDJPY", timeframe="1h", bar_ts=_bar_ts(),
               direction="long", strength=-0.1, rationale="x")


def test_signal_rejects_bool_strength():
    with pytest.raises(ValueError, match="strength"):
        Signal(plugin="rsi", pair="USDJPY", timeframe="1h", bar_ts=_bar_ts(),
               direction="long", strength=True, rationale="x")


def test_signal_rejects_nan_strength():
    with pytest.raises(ValueError, match="strength"):
        Signal(plugin="rsi", pair="USDJPY", timeframe="1h", bar_ts=_bar_ts(),
               direction="long", strength=float("nan"), rationale="x")


def test_signal_accepts_strength_boundaries():
    Signal(plugin="rsi", pair="USDJPY", timeframe="1h", bar_ts=_bar_ts(),
           direction="short", strength=0.0, rationale="x")
    Signal(plugin="rsi", pair="USDJPY", timeframe="1h", bar_ts=_bar_ts(),
           direction="short", strength=1.0, rationale="x")


def test_strategy_decision_hold_minimal():
    d = StrategyDecision(action="hold", rationale="様子見")
    assert d.action == StrategyAction.HOLD
    assert d.direction is None


def test_strategy_decision_open_requires_stop_loss():
    with pytest.raises(ValueError, match="stop_loss"):
        StrategyDecision(action="open", rationale="x", direction="long",
                         entry_type="market")


def test_strategy_decision_open_requires_direction():
    with pytest.raises(ValueError, match="direction"):
        StrategyDecision(action="open", rationale="x", entry_type="market",
                         stop_loss=147.0)


def test_strategy_decision_open_requires_entry_type():
    with pytest.raises(ValueError, match="entry_type"):
        StrategyDecision(action="open", rationale="x", direction="long",
                         stop_loss=147.0)


def test_strategy_decision_open_market_minimal():
    d = StrategyDecision(action="open", rationale="x", direction="long",
                         entry_type="market", stop_loss=147.0)
    assert d.action == StrategyAction.OPEN
    assert d.limit_price is None


def test_strategy_decision_limit_requires_limit_price():
    with pytest.raises(ValueError, match="limit_price"):
        StrategyDecision(action="open", rationale="x", direction="long",
                         entry_type="limit", stop_loss=147.0)


def test_strategy_decision_market_rejects_limit_price():
    with pytest.raises(ValueError, match="limit_price"):
        StrategyDecision(action="open", rationale="x", direction="long",
                         entry_type="market", stop_loss=147.0,
                         limit_price=148.0)


def test_strategy_decision_rejects_exit_action():
    with pytest.raises(ValueError, match="action"):
        StrategyDecision(action="exit", rationale="x")


def test_strategy_decision_rejects_invalid_direction():
    with pytest.raises(ValueError, match="direction"):
        StrategyDecision(action="hold", rationale="x", direction="up")


def test_system_clock_returns_tz_aware_utc():
    """本番用の実時計。naive を作らない (プロジェクト絶対制約)。"""
    before = datetime.now(timezone.utc)
    got = SystemClock().now()
    after = datetime.now(timezone.utc)
    assert got.tzinfo is not None
    # tz-aware なだけでなく **UTC** であること (ローカル時刻の aware を返すと
    # ISO 文字列で保存・比較する store 側で窓が無音でずれる)
    assert got.utcoffset() == timedelta(0)
    assert before <= got <= after


def test_system_clock_satisfies_clock_protocol():
    """Clock を要求する注入点にそのまま渡せること (構造的部分型)。"""
    clock: Clock = SystemClock()
    assert isinstance(clock.now(), datetime)


def test_ref_price_defaults_to_none():
    it = TradeIntent.from_llm_dict(_open_dict(), origin=Origin.SCHEDULER)
    assert it.ref_price is None


def test_ref_price_from_kwarg_not_from_dict():
    # レビュー修正 (codex 5): ref_price はシステムが供給するキーワード引数で
    # あり、d に ref_price キーがあっても無視されること
    it = TradeIntent.from_llm_dict(_open_dict(ref_price=999.0),
                                   origin=Origin.SCHEDULER, ref_price=148.30)
    assert it.ref_price == 148.30


def test_ref_price_threaded_for_close_and_hold():
    close_it = TradeIntent.from_llm_dict(
        {"action": "close", "order_id": 1}, origin=Origin.SCHEDULER,
        ref_price=148.30)
    assert close_it.ref_price == 148.30
    hold_it = TradeIntent.from_llm_dict(
        {"action": "hold", "reasoning": "x"}, origin=Origin.SCHEDULER,
        ref_price=148.30)
    assert hold_it.ref_price == 148.30


# --- レビュー fix round 1 C4/C5: 価格系フィールドの正値・有限検証 -------

def test_signal_accepts_positive_stop_loss_and_take_profit():
    s = Signal(plugin="rsi", pair="USDJPY", timeframe="1h", bar_ts=_bar_ts(),
               direction="long", strength=0.5, rationale="x",
               stop_loss=147.5, take_profit=149.0)
    assert s.stop_loss == 147.5
    assert s.take_profit == 149.0


def test_signal_rejects_nan_stop_loss():
    with pytest.raises(ValueError, match="stop_loss"):
        Signal(plugin="rsi", pair="USDJPY", timeframe="1h", bar_ts=_bar_ts(),
               direction="long", strength=0.5, rationale="x",
               stop_loss=float("nan"))


def test_signal_rejects_infinite_take_profit():
    with pytest.raises(ValueError, match="take_profit"):
        Signal(plugin="rsi", pair="USDJPY", timeframe="1h", bar_ts=_bar_ts(),
               direction="long", strength=0.5, rationale="x",
               take_profit=float("inf"))


def test_signal_rejects_negative_stop_loss():
    with pytest.raises(ValueError, match="stop_loss"):
        Signal(plugin="rsi", pair="USDJPY", timeframe="1h", bar_ts=_bar_ts(),
               direction="long", strength=0.5, rationale="x",
               stop_loss=-1.0)


def test_signal_rejects_zero_take_profit():
    with pytest.raises(ValueError, match="take_profit"):
        Signal(plugin="rsi", pair="USDJPY", timeframe="1h", bar_ts=_bar_ts(),
               direction="long", strength=0.5, rationale="x",
               take_profit=0.0)


def test_signal_rejects_bool_stop_loss():
    with pytest.raises(ValueError, match="stop_loss"):
        Signal(plugin="rsi", pair="USDJPY", timeframe="1h", bar_ts=_bar_ts(),
               direction="long", strength=0.5, rationale="x",
               stop_loss=True)


def test_strategy_decision_rejects_nan_stop_loss():
    with pytest.raises(ValueError, match="stop_loss"):
        StrategyDecision(action="open", rationale="x", direction="long",
                         entry_type="market", stop_loss=float("nan"))


def test_strategy_decision_rejects_negative_take_profit():
    with pytest.raises(ValueError, match="take_profit"):
        StrategyDecision(action="open", rationale="x", direction="long",
                         entry_type="market", stop_loss=147.0,
                         take_profit=-5.0)


def test_strategy_decision_rejects_bool_limit_price():
    with pytest.raises(ValueError, match="limit_price"):
        StrategyDecision(action="open", rationale="x", direction="long",
                         entry_type="limit", stop_loss=147.0,
                         limit_price=True)


def test_strategy_decision_rejects_infinite_limit_price():
    with pytest.raises(ValueError, match="limit_price"):
        StrategyDecision(action="open", rationale="x", direction="long",
                         entry_type="limit", stop_loss=147.0,
                         limit_price=float("inf"))


def test_strategy_decision_accepts_positive_finite_prices():
    d = StrategyDecision(action="open", rationale="x", direction="long",
                         entry_type="limit", stop_loss=147.0,
                         limit_price=148.0, take_profit=150.0)
    assert d.stop_loss == 147.0
    assert d.limit_price == 148.0
    assert d.take_profit == 150.0
