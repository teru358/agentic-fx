"""決定論的 position sizing — 設計書 §5。数量は LLM に決めさせない。"""
from __future__ import annotations

import math
from dataclasses import dataclass

from agentic_fx.config import RiskSettings
from agentic_fx.core.contracts import ConversionRate, Horizon, InstrumentSpec


class SizingError(Exception):
    """算出不能・制約違反。呼び出し側は fail closed (発注しない)。"""


@dataclass(frozen=True, slots=True)
class SizeResult:
    quantity: float
    risk_amount: float
    loss_per_lot: float


def compute_size(*, equity: float, entry_price: float, stop_loss: float,
                 horizon: Horizon, pair: str, spec: InstrumentSpec,
                 risk: RiskSettings, account_currency: str,
                 quote_to_account: ConversionRate) -> SizeResult:
    # Important b: None check for numeric inputs and spec
    if spec is None:
        raise SizingError("spec is None")

    for name, v in (("equity", equity), ("entry_price", entry_price),
                    ("stop_loss", stop_loss)):
        if v is None:
            raise SizingError(f"{name} is None")
        try:
            if not math.isfinite(v):
                raise SizingError(f"{name} is not finite")
        except TypeError:
            raise SizingError(f"{name} is not a valid number")

    if equity <= 0 or entry_price <= 0:
        raise SizingError("equity/entry_price must be positive")

    # Important a: Check pair and spec.symbol match
    if spec.symbol != pair:
        raise SizingError(
            f"pair '{pair}' does not match spec.symbol '{spec.symbol}'")

    for name, v in (("pip_size", spec.pip_size), ("lot_step", spec.lot_step),
                    ("contract_size", spec.contract_size),
                    ("min_lot", spec.min_lot), ("max_lot", spec.max_lot)):
        if not math.isfinite(v) or v <= 0:
            raise SizingError(f"invalid spec: {name}={v}")
    rule = risk.pair_rules.get(pair)
    if rule is None:
        raise SizingError(f"pair_rules missing for {pair}")

    # 口座通貨換算 (設計書 §5): loss_per_lot はクォート通貨建てなので
    # quote_to_account で口座通貨へ換算する。呼び出し側が base/quote を
    # 取り違えて渡していないかを ConversionRate のラベルで構造的に検証する
    # (数値の偶然の一致に頼らない — codex 指摘 D-I2 の対策と同根)。
    if quote_to_account is None:
        raise SizingError("quote_to_account is required")
    if (quote_to_account.from_ccy != spec.quote_currency
            or quote_to_account.to_ccy != account_currency):
        raise SizingError(
            f"quote_to_account currency mismatch: expected "
            f"{spec.quote_currency}->{account_currency}, got "
            f"{quote_to_account.from_ccy}->{quote_to_account.to_ccy}")
    rate = quote_to_account.value
    if not math.isfinite(rate) or rate <= 0:
        raise SizingError(f"invalid quote_to_account rate: {rate}")

    pct = risk.risk_per_trade_pct
    if horizon is Horizon.SWING:
        pct *= risk.swing_risk_factor
    risk_amount = equity * pct / 100.0

    sl_distance = abs(entry_price - stop_loss)
    if sl_distance <= 0:
        raise SizingError("stop-loss distance must be positive")
    cost_distance = rule.assumed_spread_pips * spec.pip_size
    # loss_per_lot は口座通貨建て: 価格由来の項 (SL距離+コスト)×contract は
    # クォート通貨建てなので rate を掛ける。commission_per_lot は口座通貨
    # 建て (config で明記) なので**換算しない** (設計書 §5)。
    loss_per_lot = (sl_distance + cost_distance) * spec.contract_size * rate \
        + risk.commission_per_lot
    if loss_per_lot <= 0:
        raise SizingError("loss_per_lot must be positive")

    raw = risk_amount / loss_per_lot
    quantity = math.floor(raw / spec.lot_step + 1e-9) * spec.lot_step
    quantity = round(min(quantity, spec.max_lot), 8)
    if quantity < spec.min_lot:
        raise SizingError(
            f"computed quantity {quantity} below min_lot {spec.min_lot}")
    if quantity * loss_per_lot > risk_amount + 1e-6:
        raise SizingError("post-rounding risk exceeds limit")  # 防御的最終検証
    return SizeResult(quantity=quantity, risk_amount=risk_amount,
                      loss_per_lot=loss_per_lot)
