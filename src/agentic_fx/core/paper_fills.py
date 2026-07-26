"""ペーパー約定判定 (純関数) — 1 分足 high/low、SL 優先、ギャップ滑り。設計書 §5。"""
from __future__ import annotations

from agentic_fx.core.contracts import Bar


def check_limit_fill(order: dict, bar: Bar, spread: float = 0.0) -> float | None:
    price = order["requested_price"]
    half = spread / 2
    if order["direction"] == "long":
        # long のエントリーは ask 側 (bar + half) で判定 — mid/bid 系列に対して保守側
        if bar.open + half <= price:      # ギャップ: 有利な open で約定
            return bar.open + half
        if bar.low + half <= price:
            return price
    else:
        # short のエントリーは bid 側 (bar - half) で判定
        if bar.open - half >= price:
            return bar.open - half
        if bar.high - half >= price:
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
        if entry_same_bar:
            # 同一バー内でのエントリー成立と TP 到達の順序は判定不能
            # (SL 未到達のためタイブレークにも該当しない) → この足では確定させない
            return None
        return ("tp", tp - half if long else tp + half)
    return None
