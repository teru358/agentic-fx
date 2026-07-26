import pytest

from agentic_fx.config import load_settings
from agentic_fx.core.contracts import Horizon, InstrumentSpec
from agentic_fx.core.sizing import SizeResult, SizingError, compute_size

from pathlib import Path

RISK = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example").risk

SPEC = InstrumentSpec(symbol="USDJPY", pip_size=0.01, min_lot=0.01,
                      max_lot=50.0, lot_step=0.01, contract_size=100_000)


def test_basic_day_sizing():
    # equity 1,000,000 JPY, risk 0.5% = 5,000 JPY
    # SL 距離 0.50 JPY + spread 1.0pip(0.01) = 0.51 → loss_per_lot = 51,000 JPY
    # raw = 5000/51000 = 0.0980... → floor to 0.09
    r = compute_size(equity=1_000_000, entry_price=148.50, stop_loss=148.00,
                     horizon=Horizon.DAY, pair="USDJPY", spec=SPEC, risk=RISK,
                     account_currency="JPY")
    assert isinstance(r, SizeResult)
    assert r.quantity == pytest.approx(0.09)
    assert r.quantity * r.loss_per_lot <= 5_000  # 丸め後最終検証


def test_swing_halves_risk():
    day = compute_size(equity=1_000_000, entry_price=148.50, stop_loss=148.00,
                       horizon=Horizon.DAY, pair="USDJPY", spec=SPEC, risk=RISK,
                       account_currency="JPY")
    swing = compute_size(equity=1_000_000, entry_price=148.50, stop_loss=148.00,
                         horizon=Horizon.SWING, pair="USDJPY", spec=SPEC, risk=RISK,
                         account_currency="JPY")
    assert swing.risk_amount == pytest.approx(day.risk_amount / 2)
    assert swing.quantity < day.quantity
    assert swing.quantity == pytest.approx(0.04)  # swing_risk_factor=0.5 で半減


def test_below_min_lot_rejected():
    with pytest.raises(SizingError, match="min_lot"):
        compute_size(equity=10_000, entry_price=148.50, stop_loss=140.00,
                     horizon=Horizon.DAY, pair="USDJPY", spec=SPEC, risk=RISK,
                     account_currency="JPY")


def test_max_lot_cap():
    tight = compute_size(equity=1_000_000_000, entry_price=148.50,
                         stop_loss=148.49, horizon=Horizon.DAY, pair="USDJPY",
                         spec=SPEC, risk=RISK, account_currency="JPY")
    assert tight.quantity == SPEC.max_lot


def test_rounding_is_floor_only():
    # raw が 0.0999 のような場合に 0.10 へ切上げないこと
    r = compute_size(equity=1_019_000, entry_price=148.50, stop_loss=148.00,
                     horizon=Horizon.DAY, pair="USDJPY", spec=SPEC, risk=RISK,
                     account_currency="JPY")
    assert r.quantity == pytest.approx(0.09)  # 切下げのみ、四捨五入ではない
    assert r.quantity * r.loss_per_lot <= r.risk_amount + 1e-6


@pytest.mark.parametrize("kw", [
    {"equity": 0}, {"equity": -1}, {"entry_price": 0},
    {"stop_loss": 148.50},  # SL 距離ゼロ
    {"equity": float("nan")}, {"entry_price": float("inf")},
    {"stop_loss": float("nan")},
])
def test_fail_closed_on_bad_inputs(kw):
    base = dict(equity=1_000_000, entry_price=148.50, stop_loss=148.00,
                horizon=Horizon.DAY, pair="USDJPY", spec=SPEC, risk=RISK,
                account_currency="JPY")
    base.update(kw)
    with pytest.raises(SizingError):
        compute_size(**base)


def test_fail_closed_on_bad_spec():
    bad = InstrumentSpec(symbol="USDJPY", pip_size=0.01, min_lot=0.01,
                         max_lot=50.0, lot_step=0.0, contract_size=100_000)
    with pytest.raises(SizingError, match="spec"):
        compute_size(equity=1_000_000, entry_price=148.50, stop_loss=148.00,
                     horizon=Horizon.DAY, pair="USDJPY", spec=bad, risk=RISK,
                     account_currency="JPY")


def test_size_result_values_asserted():
    r = compute_size(equity=1_000_000, entry_price=148.50, stop_loss=148.00,
                     horizon=Horizon.DAY, pair="USDJPY", spec=SPEC, risk=RISK,
                     account_currency="JPY")
    assert r.risk_amount == pytest.approx(5_000)
    assert r.loss_per_lot == pytest.approx(51_000)


def test_unknown_pair_fail_closed():
    # pair="GBPUSD" is unknown in pair_rules (only USDJPY/EURUSD exist)
    # Use spec with GBPUSD symbol to avoid mismatch check
    gbpusd_spec = InstrumentSpec(symbol="GBPUSD", pip_size=0.0001, min_lot=0.01,
                                 max_lot=50.0, lot_step=0.01, contract_size=100_000)
    with pytest.raises(SizingError, match="pair_rules"):
        compute_size(equity=1_000_000, entry_price=1.1, stop_loss=1.09,
                     horizon=Horizon.DAY, pair="GBPUSD", spec=gbpusd_spec, risk=RISK,
                     account_currency="JPY")


def test_pair_spec_symbol_mismatch_rejected():
    # Important a: pair="EURUSD" + spec.symbol="USDJPY" は不整合でエラーにすべき
    eurusd_spec = InstrumentSpec(symbol="EURUSD", pip_size=0.0001, min_lot=0.01,
                                 max_lot=50.0, lot_step=0.01, contract_size=100_000)
    with pytest.raises(SizingError, match="pair.*spec.symbol|spec.symbol.*pair"):
        compute_size(equity=1_000_000, entry_price=1.1, stop_loss=1.09,
                     horizon=Horizon.DAY, pair="EURUSD", spec=SPEC, risk=RISK,
                     account_currency="JPY")


def test_fail_closed_on_none_equity():
    # Important b: equity=None は SizingError にすべき
    with pytest.raises(SizingError):
        compute_size(equity=None, entry_price=148.50, stop_loss=148.00,
                     horizon=Horizon.DAY, pair="USDJPY", spec=SPEC, risk=RISK,
                     account_currency="JPY")


def test_fail_closed_on_none_entry_price():
    # Important b: entry_price=None は SizingError にすべき
    with pytest.raises(SizingError):
        compute_size(equity=1_000_000, entry_price=None, stop_loss=148.00,
                     horizon=Horizon.DAY, pair="USDJPY", spec=SPEC, risk=RISK,
                     account_currency="JPY")


def test_fail_closed_on_none_stop_loss():
    # Important b: stop_loss=None は SizingError にすべき
    with pytest.raises(SizingError):
        compute_size(equity=1_000_000, entry_price=148.50, stop_loss=None,
                     horizon=Horizon.DAY, pair="USDJPY", spec=SPEC, risk=RISK,
                     account_currency="JPY")


def test_fail_closed_on_none_spec():
    # Important b: spec=None は SizingError にすべき
    with pytest.raises(SizingError):
        compute_size(equity=1_000_000, entry_price=148.50, stop_loss=148.00,
                     horizon=Horizon.DAY, pair="USDJPY", spec=None, risk=RISK,
                     account_currency="JPY")


def test_account_currency_mismatch_fail_closed():
    # Critical(E): クォート通貨と口座通貨が異なるペアは、口座通貨換算が未実装のため
    # fail closed (SizingError) にする。EURUSD のクォート通貨は USD だが口座通貨は JPY。
    eurusd_spec = InstrumentSpec(symbol="EURUSD", pip_size=0.0001, min_lot=0.01,
                                 max_lot=50.0, lot_step=0.01, contract_size=100_000)
    with pytest.raises(SizingError, match="account currency|口座通貨"):
        compute_size(equity=1_000_000, entry_price=1.1000, stop_loss=1.0960,
                     horizon=Horizon.DAY, pair="EURUSD", spec=eurusd_spec, risk=RISK,
                     account_currency="JPY")


def test_account_currency_match_is_unaffected():
    # USDJPY はクォート通貨が JPY で口座通貨 (JPY) と一致するため、
    # account_currency 導入前と同じ数量 (day 0.09) が得られること (非退行確認)
    r = compute_size(equity=1_000_000, entry_price=148.50, stop_loss=148.00,
                     horizon=Horizon.DAY, pair="USDJPY", spec=SPEC, risk=RISK,
                     account_currency="JPY")
    assert r.quantity == pytest.approx(0.09)
