"""描画する応答の `display_timezone` から、換算に使う tz 名を決める。"""
from __future__ import annotations

import sys
from collections.abc import Mapping

from afx_ops import display_time

FIELD = "display_timezone"
_warned = False


def _warn_once() -> None:
    """使えない tz を受けたことを固定語で 1 回だけ残す (wire の値は含めない)。"""
    global _warned
    if _warned:
        return
    _warned = True
    try:
        print("afx-ops: display_timezone_unusable (UTC で表示)", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 — 表示の補助で失敗させない
        pass


def zone_of(data: object) -> str:
    """応答 data の tz 名。無い (古い daemon)・使えないときは `UTC`。"""
    if not isinstance(data, Mapping) or FIELD not in data:
        return "UTC"
    name = data[FIELD]
    if display_time.resolve_zone(name) is None:
        _warn_once()
        return "UTC"
    return name  # type: ignore[return-value]
