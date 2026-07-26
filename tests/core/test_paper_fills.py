from datetime import datetime, timezone

from agentic_fx.core.contracts import Bar
from agentic_fx.core.paper_fills import check_exit, check_limit_fill

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def _bar(o, h, l, c):
    return Bar("USDJPY", "1m", NOW, o, h, l, c, 100)


def _long_limit(price):
    return {"direction": "long", "entry_type": "limit",
            "requested_price": price, "stop_loss": price - 0.5,
            "take_profit": price + 1.0}


def test_limit_fill_long_touch():
    assert check_limit_fill(_long_limit(148.20), _bar(148.40, 148.45,
                                                     148.15, 148.30)) == 148.20


def test_limit_no_fill_when_not_reached():
    assert check_limit_fill(_long_limit(148.20), _bar(148.40, 148.50,
                                                     148.25, 148.45)) is None


def test_limit_gap_fills_at_open():
    # 下窓開け: 指値より有利な open で約定
    assert check_limit_fill(_long_limit(148.20), _bar(148.00, 148.10,
                                                     147.90, 148.05)) == 148.00


def test_limit_fill_short():
    order = {"direction": "short", "entry_type": "limit",
             "requested_price": 148.80}
    assert check_limit_fill(order, _bar(148.60, 148.85, 148.55,
                                        148.70)) == 148.80


def _open_long():
    return {"direction": "long", "stop_loss": 148.00, "take_profit": 149.00,
            "avg_fill_price": 148.50}


def test_exit_sl_hit():
    kind, price = check_exit(_open_long(), _bar(148.30, 148.40, 147.95,
                                                148.10), spread=0.02)
    assert kind == "sl"
    assert price <= 148.00  # spread 分保守側


def test_exit_tp_hit():
    kind, price = check_exit(_open_long(), _bar(148.80, 149.10, 148.70,
                                                149.00), spread=0.02)
    assert kind == "tp"


def test_both_touched_sl_wins():
    kind, _ = check_exit(_open_long(), _bar(148.50, 149.10, 147.90, 148.50),
                         spread=0.02)
    assert kind == "sl"


def test_gap_sl_slips_to_open():
    kind, price = check_exit(_open_long(), _bar(147.50, 147.80, 147.40,
                                                147.60), spread=0.02)
    assert kind == "sl"
    assert price <= 147.50  # 指定 SL 148.00 でなくギャップ後 open 基準


def test_entry_same_bar_conservative():
    # 同一バーで entry + SL 到達可能 → 判定不能は最悪 (SL)
    kind, _ = check_exit(_open_long(), _bar(148.60, 149.20, 147.95, 149.00),
                         spread=0.02, entry_same_bar=True)
    assert kind == "sl"


def test_no_exit():
    assert check_exit(_open_long(), _bar(148.40, 148.60, 148.20, 148.50),
                      spread=0.02) is None
