"""Risk Gate — 全ルールを決定論的に検証 (設計書 §5)。1 つでも違反すれば却下。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from agentic_fx.config import RiskSettings
from agentic_fx.core.contracts import (
    Action, Direction, EntryType, Horizon, InstrumentSpec, Quote, TradeIntent,
)
from agentic_fx.core.market_hours import is_friday_after
from agentic_fx.core.sizing import SizeResult, SizingError, compute_size
from agentic_fx.core.accounting import drawdown_pct
from agentic_fx.core.timeutil import as_utc


@dataclass(frozen=True, slots=True)
class GateContext:
    quote: Quote
    spec: InstrumentSpec
    equity: float
    hwm: float
    daily_start_equity: float | None
    open_position_count: int      # 未約定指値・送信中・クローズ中を含む
    existing_risk_total: float    # 上記の予約リスクを含む
    existing_notional: float
    kill_switch_latched: bool
    has_unresolved_unknown: bool  # *_unknown が 1 件でもあれば新規 open 不可
    now: datetime
    account_currency: str


def _validate_context(intent: TradeIntent, ctx: GateContext) -> list[str]:
    """gate 入力自体の健全性 (NaN/Inf/不正 quote は fail closed — 設計書 §5)。"""
    import math
    reasons: list[str] = []
    numbers = [ctx.quote.bid, ctx.quote.ask, ctx.equity, ctx.hwm,
               ctx.existing_risk_total, ctx.existing_notional]
    if any(not math.isfinite(v) for v in numbers) or ctx.equity <= 0 \
            or ctx.quote.bid <= 0 or ctx.quote.bid > ctx.quote.ask:
        reasons.append("invalid context data (fail closed)")
    if intent.pair != ctx.quote.symbol or intent.pair != ctx.spec.symbol:
        reasons.append(f"symbol mismatch: intent={intent.pair} "
                       f"quote={ctx.quote.symbol} spec={ctx.spec.symbol}")
    if ctx.has_unresolved_unknown:
        reasons.append("unresolved unknown orders exist (新規発注停止)")
    if ctx.daily_start_equity is not None and (
            not math.isfinite(ctx.daily_start_equity)
            or ctx.daily_start_equity <= 0):
        reasons.append("invalid context data (fail closed): "
                       "daily_start_equity must be finite and positive")
    if ctx.hwm <= 0:
        reasons.append("invalid context data (fail closed): hwm must be > 0")
    if ctx.existing_risk_total < 0 or ctx.existing_notional < 0:
        reasons.append("invalid context data (fail closed): "
                       "existing_risk_total/existing_notional must be >= 0")
    try:
        as_utc(ctx.now)
    except ValueError:
        reasons.append("invalid context data (fail closed): "
                       "now must be timezone-aware")
    for name, v in (("stop_loss", intent.stop_loss),
                    ("take_profit", intent.take_profit),
                    ("limit_price", intent.limit_price),
                    ("ref_price", intent.ref_price)):
        if v is not None and (not math.isfinite(v) or v <= 0):
            reasons.append(f"invalid context data (fail closed): "
                           f"{name} must be finite and positive")
    # レビュー修正 (codex 7): TradeIntent は公開契約として stop_loss=None を
    # 許容するが、open の評価は SL 必須。ここで検証せず後段の `sl < entry`
    # まで進むと TypeError になる (from_llm_dict 経由なら SL 必須のため実運用
    # では到達しないが、防御的に検証する)。
    if intent.action is Action.OPEN and intent.stop_loss is None:
        reasons.append("invalid context data (fail closed): "
                       "stop_loss is required for open")
    return reasons


@dataclass(frozen=True, slots=True)
class GateResult:
    accepted: bool
    reasons: list[str]
    size: SizeResult | None
    entry_price: float | None


def evaluate(intent: TradeIntent, ctx: GateContext,
             risk: RiskSettings) -> GateResult:
    if intent.action is not Action.OPEN:
        raise ValueError("gate は open のみ対象")
    reasons = _validate_context(intent, ctx)
    if reasons:
        # 入力が信頼できないので以降の評価はしない (fail closed)
        return GateResult(accepted=False, reasons=reasons, size=None,
                          entry_price=None)
    rule = risk.pair_rules.get(intent.pair)
    spread = (rule.assumed_spread_pips * ctx.spec.pip_size) if rule else 0.0

    # entry 価格
    if intent.entry_type is EntryType.MARKET:
        entry = ctx.quote.ask if intent.direction is Direction.LONG \
            else ctx.quote.bid
        # レビュー修正 (codex 5): market は発注直前の最新 quote で再計算するが、
        # 判断時点の参照価格 (ref_price) から乖離しすぎていれば失効させる
        # (設計書 §5)。ref_price が None (Phase 1 で trade loop 未実装のうち)
        # は検証しない — gate 評価と約定が同一処理内で同じ quote を使うため、
        # 構造的にスリッページはゼロ。
        if intent.ref_price is not None:
            slippage_pct = abs(entry - intent.ref_price) / intent.ref_price * 100
            if slippage_pct > risk.max_slippage_pct:
                reasons.append(f"slippage {slippage_pct:.3f}% > "
                               f"{risk.max_slippage_pct}%")
    else:
        entry = intent.limit_price
        mid = (ctx.quote.bid + ctx.quote.ask) / 2
        if abs(entry - mid) / mid * 100 > risk.limit_deviation_pct:
            reasons.append(
                f"limit deviation {abs(entry - mid) / mid * 100:.2f}% > "
                f"{risk.limit_deviation_pct}%")
        if intent.expires_in_h is None or \
                intent.expires_in_h > risk.limit_expiry_max_h:
            reasons.append(f"expiry {intent.expires_in_h}h > "
                           f"{risk.limit_expiry_max_h}h")

    # SL/TP 妥当性
    sl, tp = intent.stop_loss, intent.take_profit
    if tp is None:
        reasons.append("take_profit required (RR rule)")
    if intent.direction is Direction.LONG:
        if not (sl < entry):
            reasons.append("stop-loss side invalid (long requires SL < entry)")
        if tp is not None and not (entry < tp):
            reasons.append("take-profit side invalid (long requires entry < TP)")
    else:
        if not (sl > entry):
            reasons.append("stop-loss side invalid (short requires SL > entry)")
        if tp is not None and not (entry > tp):
            reasons.append("take-profit side invalid (short requires entry > TP)")

    if rule is None:
        reasons.append(f"pair_rules missing for {intent.pair}")
    else:
        dist_pips = abs(entry - sl) / ctx.spec.pip_size
        if not (rule.sl_distance_min_pips <= dist_pips
                <= rule.sl_distance_max_pips):
            reasons.append(
                f"sl distance {dist_pips:.1f}pips outside "
                f"[{rule.sl_distance_min_pips}, {rule.sl_distance_max_pips}]")

    # RR (spread 込み)
    if tp is not None and abs(entry - sl) > 0:
        rr = (abs(tp - entry) - spread) / (abs(entry - sl) + spread)
        if rr < risk.rr_min:
            reasons.append(f"rr {rr:.2f} < {risk.rr_min}")

    # kill switch (ラッチ・現在 DD は独立に判定し、両方該当時は両方を列挙する)
    if ctx.kill_switch_latched:
        reasons.append("kill switch latched")
    if drawdown_pct(ctx.equity, ctx.hwm) >= risk.drawdown_kill_pct:
        reasons.append(
            f"kill switch: drawdown {drawdown_pct(ctx.equity, ctx.hwm):.2f}% "
            f">= {risk.drawdown_kill_pct}%")

    # 日次損失 (不明は fail closed)
    if ctx.daily_start_equity is None:
        reasons.append("daily start equity unknown (fail closed)")
    else:
        loss = (ctx.daily_start_equity - ctx.equity) \
            / ctx.daily_start_equity * 100
        if loss >= risk.daily_loss_limit_pct:
            reasons.append(f"daily loss {loss:.2f}% >= "
                           f"{risk.daily_loss_limit_pct}%")

    # ポジション枠 (未約定指値込み)
    if ctx.open_position_count >= risk.max_positions:
        reasons.append(f"max positions {ctx.open_position_count} >= "
                       f"{risk.max_positions}")

    # 金曜 swing cutoff
    if intent.horizon is Horizon.SWING and \
            is_friday_after(ctx.now, risk.friday_swing_cutoff_ny):
        reasons.append("friday swing cutoff")

    # sizing (fail closed) + 総リスク・レバレッジ
    size: SizeResult | None = None
    try:
        size = compute_size(equity=ctx.equity, entry_price=entry,
                            stop_loss=sl, horizon=intent.horizon,
                            pair=intent.pair, spec=ctx.spec, risk=risk,
                            account_currency=ctx.account_currency)
    except SizingError as e:
        reasons.append(f"sizing failed: {e}")
    if size is not None:
        total = ctx.existing_risk_total + size.quantity * size.loss_per_lot
        cap = ctx.equity * risk.max_total_risk_pct / 100
        if total > cap:
            reasons.append(f"total risk {total:.0f} > cap {cap:.0f}")
        notional = ctx.existing_notional \
            + size.quantity * ctx.spec.contract_size * entry
        if notional / ctx.equity > risk.max_leverage:
            reasons.append(
                f"leverage {notional / ctx.equity:.1f}x > {risk.max_leverage}x")

    accepted = not reasons
    return GateResult(accepted=accepted, reasons=reasons,
                      size=size if accepted else None,
                      entry_price=entry)
