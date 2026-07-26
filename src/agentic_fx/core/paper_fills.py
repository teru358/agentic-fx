"""ペーパー約定判定 (純関数) — 1 分足 high/low、SL 優先、ギャップ滑り。設計書 §5。"""
from __future__ import annotations

from agentic_fx.core.contracts import Bar


def check_limit_fill(order: dict, bar: Bar) -> float | None:
    price = order["requested_price"]
    if order["direction"] == "long":
        if bar.open <= price:      # ギャップ: 有利な open で約定
            return bar.open
        if bar.low <= price:
            return price
    else:
        if bar.open >= price:
            return bar.open
        if bar.high >= price:
            return price
    return None


def check_exit(order: dict, bar: Bar, spread: float, *,
               entry_same_bar: bool = False) -> tuple[str, float] | None:
    sl, tp = order["stop_loss"], order["take_profit"]
    long = order["direction"] == "long"
    half = spread / 2

    if long:
        sl_hit = bar.low - half <= sl or bar.open - half <= sl
        tp_hit = tp is not None and bar.high - half >= tp
    else:
        sl_hit = bar.high + half >= sl or bar.open + half >= sl
        tp_hit = tp is not None and bar.low + half <= tp

    if sl_hit:
        # ギャップ: open が SL を超えていれば open 基準で滑る
        if long:
            base = min(bar.open, sl)
            return ("sl", base - half)
        base = max(bar.open, sl)
        return ("sl", base + half)
    if tp_hit:
        # entry_same_bar の保守則は sl_hit を先に判定することで満たされる
        # (同一バーで SL にも届き得るなら上で SL を返して return 済み)。
        return ("tp", tp - half if long else tp + half)
    return None
