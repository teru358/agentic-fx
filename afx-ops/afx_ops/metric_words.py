"""成績行に添える 3 項目 (勝率・max DD・kill switch 回数) の語と桁。

daemon 側 `agentic_fx/metric_words.py` の写し。別パッケージなので import せず、
リテラルの一致は daemon 側の test が `ast` で確かめる。"""
from __future__ import annotations

import math

WIN_RATE_LABEL = "勝率"
MAX_DD_LABEL = "max DD"
KILL_SWITCH_LABEL = "kill switch"
KILL_SWITCH_UNIT = "回"
PERCENT_FORMAT = "{:.1f}%"


def _percent(value: object) -> str | None:
    if type(value) in (int, float) and math.isfinite(value):
        return PERCENT_FORMAT.format(value * 100)
    return None


def metric_items(metrics: object) -> list[str]:
    """読める項目だけを 勝率・max DD・kill switch の順で返す。鍵が無い・型が違う項目は省く。"""
    if not isinstance(metrics, dict):
        return []
    items: list[str] = []
    win_rate = _percent(metrics.get("win_rate"))
    if win_rate is not None:
        items.append(f"{WIN_RATE_LABEL} {win_rate}")
    max_dd = _percent(metrics.get("max_drawdown"))
    if max_dd is not None:
        items.append(f"{MAX_DD_LABEL} {max_dd}")
    latches = metrics.get("kill_switch_latches")
    if type(latches) is int and latches >= 0:
        items.append(f"{KILL_SWITCH_LABEL} {latches}{KILL_SWITCH_UNIT}")
    return items
