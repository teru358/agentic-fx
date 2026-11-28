"""停止中に届いた足の接触判定 (保護価格・指値への到達の真偽だけ)。"""
import math
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.core.contracts import Bar
from agentic_fx.core.paper_fills import check_exit, check_limit_fill, exit_hits
from agentic_fx.core.protection_contact import protection_contact

T0 = datetime(2026, 11, 2, 22, 0, tzinfo=timezone.utc)


def _bar(minute: int, *, o=148.0, h=148.05, l=147.95, c=148.0, pair="USDJPY"):
    return Bar(pair, "1m", T0 + timedelta(minutes=minute), o, h, l, c, 1.0)


def _order(**kw):
    base = {"id": 1, "pair": "USDJPY", "direction": "long", "status": "open",
            "requested_price": 148.0, "stop_loss": 147.5, "take_profit": 148.6,
            "created_at": T0.isoformat(), "updated_at": T0.isoformat(),
            "filled_at": T0.isoformat(), "closed_at": None}
    base.update(kw)
    return base


def _kinds(orders, bars, spread=0.0):
    return [(c.order_id, c.bar_time, c.kind) for c in protection_contact(orders, bars, spread)]


@pytest.mark.parametrize("direction,bar,kind", [
    ("long", _bar(5, l=147.4), "sl"),
    ("long", _bar(5, h=148.7), "tp"),
    ("long", _bar(5, l=147.4, h=148.7), "both"),
    ("long", _bar(5), None),
    ("short", _bar(5, h=148.6), "sl"),
    ("short", _bar(5, l=147.4), "tp"),
    ("short", _bar(5, h=148.6, l=147.4), "both"),
    ("short", _bar(5), None),
])
def test_open_order_sl_tp_touch_for_both_directions(direction, bar, kind):
    sl, tp = (147.5, 148.6) if direction == "long" else (148.5, 147.5)
    got = _kinds([_order(direction=direction, stop_loss=sl, take_profit=tp)], [bar])
    assert got == ([] if kind is None else [(1, bar.ts, kind)])


def test_open_gap_beyond_sl_counts_as_touch():
    bar = _bar(5, o=147.0, h=147.1, l=146.9)
    assert _kinds([_order()], [bar])[0][2] == "sl"


def test_half_spread_moves_the_boundary_like_normal_protection():
    order = _order(stop_loss=147.5, take_profit=None)
    bar = _bar(5, l=147.52)
    assert _kinds([order], [bar], 0.0) == []
    assert _kinds([order], [bar], 0.06) == [(1, bar.ts, "sl")]


@pytest.mark.parametrize("direction", ["long", "short"])
@pytest.mark.parametrize("spread", [0.0, 0.02, 0.5])
def test_touch_equals_paper_fills_exit_decision(direction, spread):
    sl, tp = (147.5, 148.6) if direction == "long" else (148.5, 147.5)
    order = _order(direction=direction, stop_loss=sl, take_profit=tp)
    for lo in (147.3, 147.5, 147.51, 147.9):
        for hi in (148.1, 148.5, 148.6, 148.9):
            bar = _bar(5, l=lo, h=hi)
            sl_hit, tp_hit = exit_hits(order, bar, spread)
            exit_ = check_exit(order, bar, spread)
            assert (exit_ is not None) == (sl_hit or (tp_hit and True))
            got = _kinds([order], [bar], spread)
            expected = "both" if sl_hit and tp_hit else "sl" if sl_hit else "tp" if tp_hit else None
            assert got == ([] if expected is None else [(1, bar.ts, expected)])


@pytest.mark.parametrize("direction,price,bar", [
    ("long", 147.9, _bar(5, l=147.85)),
    ("short", 148.1, _bar(5, h=148.15)),
])
def test_pending_fill_limit_reach_is_a_touch_without_filling(direction, price, bar):
    order = _order(status="pending_fill", direction=direction, requested_price=price,
                   stop_loss=140.0 if direction == "long" else 156.0,
                   take_profit=None, filled_at=None)
    assert check_limit_fill(order, bar, 0.0) is not None
    assert _kinds([order], [bar]) == [(1, bar.ts, "limit")]
    assert order["status"] == "pending_fill"


def test_pending_fill_without_reach_is_not_a_contact():
    order = _order(status="pending_fill", requested_price=147.0, stop_loss=140.0,
                   take_profit=None, filled_at=None)
    assert _kinds([order], [_bar(5)]) == []


@pytest.mark.parametrize("status", ["cancelled", "expired"])
def test_unfilled_terminal_order_is_checked_inside_its_window_only(status):
    order = _order(status=status, requested_price=147.9, stop_loss=140.0,
                   take_profit=None, filled_at=None,
                   closed_at=(T0 + timedelta(minutes=10)).isoformat())
    inside = _bar(5, l=147.85)
    after = _bar(12, l=147.85)
    assert _kinds([order], [inside, after]) == [(1, inside.ts, "limit")]


def test_closed_order_uses_closed_at_as_the_window_end_and_created_at_as_the_start():
    order = _order(status="closed", closed_at=(T0 + timedelta(minutes=10)).isoformat(),
                   created_at=(T0 + timedelta(minutes=3)).isoformat())
    before = _bar(1, l=147.0)
    during = _bar(5, l=147.0)
    after = _bar(10, l=147.0)
    assert _kinds([order], [before, during, after]) == [(1, during.ts, "sl")]


def test_bar_straddling_the_creation_minute_overlaps_the_window():
    order = _order(created_at=(T0 + timedelta(minutes=5, seconds=30)).isoformat())
    assert _kinds([order], [_bar(5, l=147.0)])[0][2] == "sl"


def test_terminal_without_closed_at_falls_back_to_updated_at():
    order = _order(status="cancelled", requested_price=147.9, stop_loss=140.0,
                   take_profit=None, filled_at=None,
                   updated_at=(T0 + timedelta(minutes=6)).isoformat())
    assert _kinds([order], [_bar(5, l=147.85), _bar(7, l=147.85)]) == [
        (1, T0 + timedelta(minutes=5), "limit")]


@pytest.mark.parametrize("status", ["protection_pending", "submitting", "submitted",
                                    "closing", "cancelling", "submit_unknown",
                                    "cancel_unknown", "close_unknown"])
def test_uncertain_states_are_unknown_even_when_the_bar_is_quiet(status):
    assert _kinds([_order(status=status)], [_bar(5)]) == [(1, T0 + timedelta(minutes=5), "unknown")]


@pytest.mark.parametrize("status", ["approval_pending", "rejected", "invalidated"])
def test_orders_that_were_never_live_are_not_evaluated(status):
    assert _kinds([_order(status=status, closed_at=None)], [_bar(5, l=147.0)]) == []


def test_unknown_status_value_is_unknown():
    assert _kinds([_order(status="mystery")], [_bar(5)])[0][2] == "unknown"


@pytest.mark.parametrize("patch", [
    {"direction": None}, {"direction": "sideways"}, {"stop_loss": None},
    {"stop_loss": math.nan}, {"stop_loss": math.inf}, {"take_profit": math.nan},
    {"created_at": None}, {"created_at": "not-a-time"},
    {"created_at": "2026-11-02T22:00:00"},
])
def test_missing_or_non_finite_protection_data_is_unknown(patch):
    assert _kinds([_order(**patch)], [_bar(5)])[0][2] == "unknown"


def test_pending_without_a_finite_limit_is_unknown():
    order = _order(status="pending_fill", requested_price=None, filled_at=None)
    assert _kinds([order], [_bar(5)])[0][2] == "unknown"


def test_closed_order_without_fill_time_is_unknown_and_unreadable_end_is_unknown():
    closed = _order(status="closed", filled_at=None, closed_at=(T0 + timedelta(hours=1)).isoformat())
    assert _kinds([closed], [_bar(5)])[0][2] == "unknown"
    broken = _order(status="closed", closed_at="garbage")
    assert _kinds([broken], [_bar(5)])[0][2] == "unknown"


def test_spread_failure_or_non_finite_spread_is_unknown():
    def boom(pair):
        raise RuntimeError("no spec")
    assert _kinds([_order()], [_bar(5)], boom)[0][2] == "unknown"
    assert _kinds([_order()], [_bar(5)], math.nan)[0][2] == "unknown"
    assert _kinds([_order()], [_bar(5)], lambda pair: 0.0) == []


def test_other_pair_orders_are_not_evaluated():
    assert _kinds([_order(pair="EURUSD")], [_bar(5, l=147.0)]) == []


def test_results_are_ordered_by_bar_time_then_order_id_and_inputs_are_untouched():
    first = _order(id=2)
    second = _order(id=1)
    bars = [_bar(7, l=147.0), _bar(5, l=147.0)]
    snapshot = [dict(first), dict(second)]
    got = _kinds([first, second], bars)
    assert got == [(1, bars[1].ts, "sl"), (2, bars[1].ts, "sl"),
                   (1, bars[0].ts, "sl"), (2, bars[0].ts, "sl")]
    assert [first, second] == snapshot


def test_spread_once_per_pair_and_ordered_contacts():
    calls = []
    def spread(pair):
        calls.append(pair)
        return 0.0
    orders = [_order(id=2), _order(id=1), _order(id=3, pair="EURUSD")]
    bars = [_bar(2, l=147.0), _bar(1, l=147.0), _bar(1, l=147.0, pair="EURUSD")]
    result = protection_contact(orders, bars, spread)
    assert calls.count("USDJPY") == calls.count("EURUSD") == 1
    assert [(c.bar_time, c.order_id) for c in result] == [(bars[1].ts, 1), (bars[1].ts, 2), (bars[2].ts, 3), (bars[0].ts, 1), (bars[0].ts, 2)]


def test_the_live_status_words_match_the_order_store():
    from agentic_fx.core import protection_contact as pc
    from agentic_fx.core.contracts import NON_TERMINAL_STATUSES
    assert pc._NON_TERMINAL == frozenset(NON_TERMINAL_STATUSES)
