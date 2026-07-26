"""決定論的 position sizing — 設計書 §5。数量は LLM に決めさせない。"""
from __future__ import annotations

import math
from dataclasses import dataclass

from agentic_fx.config import RiskSettings
from agentic_fx.core.contracts import Horizon, InstrumentSpec


class SizingError(Exception):
    """算出不能・制約違反。呼び出し側は fail closed (発注しない)。"""


@dataclass(frozen=True, slots=True)
class SizeResult:
    quantity: float
    risk_amount: float
    loss_per_lot: float


def compute_size(*, equity: float, entry_price: float, stop_loss: float,
                 horizon: Horizon, pair: str, spec: InstrumentSpec,
                 risk: RiskSettings) -> SizeResult:
    for name, v in (("equity", equity), ("entry_price", entry_price),
                    ("stop_loss", stop_loss)):
        if not math.isfinite(v):
            raise SizingError(f"{name} is not finite")
    if equity <= 0 or entry_price <= 0:
        raise SizingError("equity/entry_price must be positive")
    for name, v in (("pip_size", spec.pip_size), ("lot_step", spec.lot_step),
                    ("contract_size", spec.contract_size),
                    ("min_lot", spec.min_lot), ("max_lot", spec.max_lot)):
        if not math.isfinite(v) or v <= 0:
            raise SizingError(f"invalid spec: {name}={v}")
    rule = risk.pair_rules.get(pair)
    if rule is None:
        raise SizingError(f"pair_rules missing for {pair}")

    pct = risk.risk_per_trade_pct
    if horizon is Horizon.SWING:
        pct *= risk.swing_risk_factor
    risk_amount = equity * pct / 100.0

    sl_distance = abs(entry_price - stop_loss)
    if sl_distance <= 0:
        raise SizingError("stop-loss distance must be positive")
    cost_distance = rule.assumed_spread_pips * spec.pip_size
    loss_per_lot = (sl_distance + cost_distance) * spec.contract_size \
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
