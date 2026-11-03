"""人間向けの成績行に添える 3 項目 (勝率・max DD・kill switch 回数) の語と桁。

語と桁はここを正本とする。afx-ops の `afx_ops/metric_words.py` は別パッケージなので
同じリテラルの写しを持ち、一致は test が `ast` で確かめる。agent 向けの面では使わない。"""
from __future__ import annotations

import math

WIN_RATE_LABEL = "勝率"
MAX_DD_LABEL = "max DD"
KILL_SWITCH_LABEL = "kill switch"
KILL_SWITCH_UNIT = "回"
PERCENT_FORMAT = "{:.1f}%"


def _is_finite_number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _percent(value: object) -> str | None:
    if not _is_finite_number(value):
        return None
    return PERCENT_FORMAT.format(value * 100)


def metric_items(metrics: object) -> list[str]:
    """勝率・max DD・kill switch 回数のうち、読める項目だけを順に返す。
    鍵が無い (旧 payload)・型が違う項目は黙って省く。"""
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
