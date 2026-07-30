from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.config import load_settings
from agentic_fx.core.contracts import ConversionRate, Horizon, InstrumentSpec
from agentic_fx.core.sizing import SizeResult, SizingError, compute_size

RISK = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example").risk

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)

SPEC = InstrumentSpec(symbol="USDJPY", pip_size=0.01, min_lot=0.01,
                      max_lot=50.0, lot_step=0.01, contract_size=100_000,
                      base_currency="USD", quote_currency="JPY")

# USDJPY のクォート通貨 (JPY) は口座通貨 (JPY) と同じなので恒等変換。
IDENTITY_JPY = ConversionRate(1.0, "JPY", "JPY", (NOW,))


def test_basic_day_sizing():
    # equity 1,000,000 JPY, risk 0.5% = 5,000 JPY
    # SL 距離 0.50 JPY + spread 1.0pip(0.01) = 0.51 → loss_per_lot = 51,000 JPY
    # raw = 5000/51000 = 0.0980... → floor to 0.09
    r = compute_size(equity=1_000_000, entry_price=148.50, stop_loss=148.00,
                     horizon=Horizon.DAY, pair="USDJPY", spec=SPEC, risk=RISK,
                     account_currency="JPY", quote_to_account=IDENTITY_JPY)
    assert isinstance(r, SizeResult)
    assert r.quantity == pytest.approx(0.09)
    assert r.quantity * r.loss_per_lot <= 5_000  # 丸め後最終検証


def test_swing_halves_risk():
    day = compute_size(equity=1_000_000, entry_price=148.50, stop_loss=148.00,
                       horizon=Horizon.DAY, pair="USDJPY", spec=SPEC, risk=RISK,
                       account_currency="JPY", quote_to_account=IDENTITY_JPY)
    swing = compute_size(equity=1_000_000, entry_price=148.50, stop_loss=148.00,
                         horizon=Horizon.SWING, pair="USDJPY", spec=SPEC,
                         risk=RISK, account_currency="JPY",
                         quote_to_account=IDENTITY_JPY)
    assert swing.risk_amount == pytest.approx(day.risk_amount / 2)
    assert swing.quantity < day.quantity
    assert swing.quantity == pytest.approx(0.04)  # swing_risk_factor=0.5 で半減


def test_below_min_lot_rejected():
    with pytest.raises(SizingError, match="min_lot"):
        compute_size(equity=10_000, entry_price=148.50, stop_loss=140.00,
                     horizon=Horizon.DAY, pair="USDJPY", spec=SPEC, risk=RISK,
                     account_currency="JPY", quote_to_account=IDENTITY_JPY)


def test_max_lot_cap():
    tight = compute_size(equity=1_000_000_000, entry_price=148.50,
                         stop_loss=148.49, horizon=Horizon.DAY, pair="USDJPY",
                         spec=SPEC, risk=RISK, account_currency="JPY",
                         quote_to_account=IDENTITY_JPY)
    assert tight.quantity == SPEC.max_lot


def test_rounding_is_floor_only():
    # raw が 0.0999 のような場合に 0.10 へ切上げないこと
    r = compute_size(equity=1_019_000, entry_price=148.50, stop_loss=148.00,
                     horizon=Horizon.DAY, pair="USDJPY", spec=SPEC, risk=RISK,
                     account_currency="JPY", quote_to_account=IDENTITY_JPY)
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
                account_currency="JPY", quote_to_account=IDENTITY_JPY)
    base.update(kw)
    with pytest.raises(SizingError):
        compute_size(**base)


def test_fail_closed_on_bad_spec():
    bad = InstrumentSpec(symbol="USDJPY", pip_size=0.01, min_lot=0.01,
                         max_lot=50.0, lot_step=0.0, contract_size=100_000,
                         base_currency="USD", quote_currency="JPY")
    with pytest.raises(SizingError, match="spec"):
        compute_size(equity=1_000_000, entry_price=148.50, stop_loss=148.00,
                     horizon=Horizon.DAY, pair="USDJPY", spec=bad, risk=RISK,
                     account_currency="JPY", quote_to_account=IDENTITY_JPY)


def test_size_result_values_asserted():
    r = compute_size(equity=1_000_000, entry_price=148.50, stop_loss=148.00,
                     horizon=Horizon.DAY, pair="USDJPY", spec=SPEC, risk=RISK,
                     account_currency="JPY", quote_to_account=IDENTITY_JPY)
    assert r.risk_amount == pytest.approx(5_000)
    assert r.loss_per_lot == pytest.approx(51_000)


def test_unknown_pair_fail_closed():
    # pair="GBPUSD" is unknown in pair_rules (only USDJPY/EURUSD exist)
    # Use spec with GBPUSD symbol to avoid mismatch check
    gbpusd_spec = InstrumentSpec(symbol="GBPUSD", pip_size=0.0001, min_lot=0.01,
                                 max_lot=50.0, lot_step=0.01, contract_size=100_000,
                                 base_currency="GBP", quote_currency="USD")
    rate = ConversionRate(148.5, "USD", "JPY", (NOW,))
    with pytest.raises(SizingError, match="pair_rules"):
        compute_size(equity=1_000_000, entry_price=1.1, stop_loss=1.09,
                     horizon=Horizon.DAY, pair="GBPUSD", spec=gbpusd_spec,
                     risk=RISK, account_currency="JPY",
                     quote_to_account=rate)


def test_pair_spec_symbol_mismatch_rejected():
    # Important a: pair="EURUSD" + spec.symbol="USDJPY" は不整合でエラーにすべき
    with pytest.raises(SizingError, match="pair.*spec.symbol|spec.symbol.*pair"):
        compute_size(equity=1_000_000, entry_price=1.1, stop_loss=1.09,
                     horizon=Horizon.DAY, pair="EURUSD", spec=SPEC, risk=RISK,
                     account_currency="JPY", quote_to_account=IDENTITY_JPY)


def test_fail_closed_on_none_equity():
    # Important b: equity=None は SizingError にすべき
    with pytest.raises(SizingError):
        compute_size(equity=None, entry_price=148.50, stop_loss=148.00,
                     horizon=Horizon.DAY, pair="USDJPY", spec=SPEC, risk=RISK,
                     account_currency="JPY", quote_to_account=IDENTITY_JPY)


def test_fail_closed_on_none_entry_price():
    # Important b: entry_price=None は SizingError にすべき
    with pytest.raises(SizingError):
        compute_size(equity=1_000_000, entry_price=None, stop_loss=148.00,
                     horizon=Horizon.DAY, pair="USDJPY", spec=SPEC, risk=RISK,
                     account_currency="JPY", quote_to_account=IDENTITY_JPY)


def test_fail_closed_on_none_stop_loss():
    # Important b: stop_loss=None は SizingError にすべき
    with pytest.raises(SizingError):
        compute_size(equity=1_000_000, entry_price=148.50, stop_loss=None,
                     horizon=Horizon.DAY, pair="USDJPY", spec=SPEC, risk=RISK,
                     account_currency="JPY", quote_to_account=IDENTITY_JPY)


def test_fail_closed_on_none_spec():
    # Important b: spec=None は SizingError にすべき
    with pytest.raises(SizingError):
        compute_size(equity=1_000_000, entry_price=148.50, stop_loss=148.00,
                     horizon=Horizon.DAY, pair="USDJPY", spec=None, risk=RISK,
                     account_currency="JPY", quote_to_account=IDENTITY_JPY)


def test_account_currency_match_is_unaffected():
    # USDJPY はクォート通貨が JPY で口座通貨 (JPY) と一致するため、
    # 換算層導入前と同じ数量 (day 0.09) が得られること (非退行確認)。
    r = compute_size(equity=1_000_000, entry_price=148.50, stop_loss=148.00,
                     horizon=Horizon.DAY, pair="USDJPY", spec=SPEC, risk=RISK,
                     account_currency="JPY", quote_to_account=IDENTITY_JPY)
    assert r.quantity == pytest.approx(0.09)


# --- 換算層 (口座通貨と換算、改訂第 16 版) ----------------------------------


def test_quote_to_account_required():
    # Critical: quote_to_account を省略したら fail closed (推測で埋めない)
    with pytest.raises(SizingError, match="quote_to_account"):
        compute_size(equity=1_000_000, entry_price=148.50, stop_loss=148.00,
                     horizon=Horizon.DAY, pair="USDJPY", spec=SPEC, risk=RISK,
                     account_currency="JPY", quote_to_account=None)


def test_quote_to_account_currency_mismatch_fail_closed():
    # base/quote 取り違えの構造的検出: spec.quote_currency=JPY のはずが
    # USD->JPY ではなく EUR->JPY のレートを渡すと fail closed。数値上は
    # 妥当に見えても通貨ラベルが違えば拒否する (D-I2 の防御線)。
    wrong = ConversionRate(163.665, "EUR", "JPY", (NOW,))
    with pytest.raises(SizingError, match="mismatch"):
        compute_size(equity=1_000_000, entry_price=148.50, stop_loss=148.00,
                     horizon=Horizon.DAY, pair="USDJPY", spec=SPEC, risk=RISK,
                     account_currency="JPY", quote_to_account=wrong)


def test_eurusd_golden_matches_measured_order_calc_profit():
    """ブリーフ実測 golden: EURUSD 1 lot +100pt (0.0010) → 100 USD →
    16,367 JPY @ USDJPY 163.665 (order_calc_profit 実測、2026-07-29
    OANDA-Japan)。損失額 (loss_per_lot) がクォート通貨 (USD) 建てで計算され、
    quote_to_account (USD->JPY) で口座通貨へ正しく換算されることを固定する。
    spread/commission を 0 にして純粋に 100pt 分だけを比較する。"""
    eurusd_spec = InstrumentSpec(symbol="EURUSD", pip_size=0.0001, min_lot=0.01,
                                 max_lot=10.0, lot_step=0.01, contract_size=100_000,
                                 base_currency="EUR", quote_currency="USD")
    risk = RISK.model_copy(update={"commission_per_lot": 0.0})
    rate = ConversionRate(163.665, "USD", "JPY", (NOW,))
    r = compute_size(equity=100_000_000, entry_price=1.1000, stop_loss=1.0990,
                     horizon=Horizon.DAY, pair="EURUSD", spec=eurusd_spec,
                     risk=risk, account_currency="JPY",
                     quote_to_account=rate)
    # loss_per_lot = (0.0010 + spread) × 100,000 × 163.665 ≈ 100USD分×163.665
    # spread=1.0pip=0.0001 が乗るので 0.0011 × 100,000 × 163.665 で比較する
    spread = risk.pair_rules["EURUSD"].assumed_spread_pips * eurusd_spec.pip_size
    expected = (0.0010 + spread) * 100_000 * 163.665
    assert r.loss_per_lot == pytest.approx(expected)
    # 100pt (spread なし換算) 分だけを抜き出すと 100 USD → 16,367 JPY
    pnl_jpy_for_100pt = 0.0010 * 100_000 * rate.value
    assert pnl_jpy_for_100pt == pytest.approx(16_367, abs=1)


def test_usdjpy_non_regression_pins_the_multiplication():
    """変異テスト: `× rate` を消しても USDJPY (rate=1.0) は退行しない —
    だからこの非退行だけでは変異を殺せない。EURUSD 側 (rate≠1.0) の golden
    と対で検証することで「本当に掛けているか」をピンする。"""
    r = compute_size(equity=1_000_000, entry_price=148.50, stop_loss=148.00,
                     horizon=Horizon.DAY, pair="USDJPY", spec=SPEC, risk=RISK,
                     account_currency="JPY", quote_to_account=IDENTITY_JPY)
    assert r.quantity == pytest.approx(0.09)


def test_commission_is_not_converted():
    """commission_per_lot は口座通貨建てなので rate を掛けてはならない
    (設計書 §5)。rate≠1.0 でも commission 項がそのまま加算されることを
    固定する — commission に rate を掛ける変異のピン。"""
    eurusd_spec = InstrumentSpec(symbol="EURUSD", pip_size=0.0001, min_lot=0.01,
                                 max_lot=10.0, lot_step=0.01, contract_size=100_000,
                                 base_currency="EUR", quote_currency="USD")
    rate = ConversionRate(163.665, "USD", "JPY", (NOW,))
    r = compute_size(equity=100_000_000, entry_price=1.1000, stop_loss=1.0990,
                     horizon=Horizon.DAY, pair="EURUSD", spec=eurusd_spec,
                     risk=RISK, account_currency="JPY",
                     quote_to_account=rate)
    spread = RISK.pair_rules["EURUSD"].assumed_spread_pips * eurusd_spec.pip_size
    price_term_account = (0.0010 + spread) * 100_000 * rate.value
    # commission_per_lot (config, 口座通貨建て) が rate 倍されていれば
    # loss_per_lot は price_term_account + commission*rate になってしまう。
    # ここでは commission がそのまま (× 1) 加算されていることを確認する。
    assert r.loss_per_lot == pytest.approx(
        price_term_account + RISK.commission_per_lot)
