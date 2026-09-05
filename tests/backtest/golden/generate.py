"""再現可能な 1m backtest golden-v1 の生成器。"""
from __future__ import annotations

import json
import math
from datetime import timedelta
from pathlib import Path
from typing import Any

from agentic_fx.backtest.metrics import compute_metrics
from agentic_fx.backtest.runner import run_replay
from agentic_fx.store import ohlcv

from tests.backtest.factories import SETTINGS, WED, _conn, _row_at, DATASET_1M


_GOLDEN_PATH = Path(__file__).with_name("golden-v1.json")
_DAY = timedelta(days=1)


def _history(conn) -> None:
    """工場関数で生成した三日間の 1m 履歴に、SL/TP/DD の足を埋め込む。"""
    rows = []
    for minute in range(3 * 24 * 60):
        ts = WED + timedelta(minutes=minute)
        rows.append(_row_at(ts, o=148.5, h=148.6, l=148.4, c=148.5))

    # market entry は tick 13:01 に completed bar 13:00 の close で行われる。
    # 次の completed bar を scheduler が評価する tick で SL/TP に到達させる。
    for day, high, low in ((0, 148.6, 147.0), (1, 150.0, 148.4)):
        index = day * 24 * 60 + 61
        ts = WED + _DAY * day + timedelta(hours=1, minutes=1)
        rows[index] = _row_at(ts, o=148.5, h=high, l=low, c=148.5)

    # 三日目は大きなギャップで損失を発生させ、次の open 判定で kill switch
    # をラッチする。golden が観測シームの空配列変異を検出するための足である。
    index = 2 * 24 * 60 + 61
    ts = WED + _DAY * 2 + timedelta(hours=1, minutes=1)
    rows[index] = _row_at(ts, o=100.0, h=100.1, l=99.9, c=100.0)
    ohlcv.import_history_bars(conn, rows, source="dukascopy")


def _intent(bar):
    day = (bar.ts.date() - WED.date()).days
    if bar.ts.hour == 12 and day in (0, 1):
        return {"action": "open", "pair": "USDJPY", "direction": "long",
                "entry_type": "market", "horizon": "swing",
                "stop_loss": 148.3, "take_profit": 149.0,
                "reasoning": f"golden-{day}"}
    if bar.ts.hour == 12 and day == 2:
        return {"action": "open", "pair": "USDJPY", "direction": "long",
                "entry_type": "market", "horizon": "swing",
                "stop_loss": 148.3, "take_profit": 149.0,
                "reasoning": "golden-kill"}
    if bar.ts.hour == 13 and day == 2:
        return {"action": "open", "pair": "USDJPY", "direction": "long",
                "entry_type": "market", "horizon": "swing",
                "stop_loss": 148.3, "take_profit": 149.0,
                "reasoning": "golden-blocked"}
    return None


def _json_safe(value: Any) -> Any:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("golden-v1 must not contain non-finite floats")
        return value
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    return value


def build_golden() -> dict:
    """fresh in-memory history DB で決定的な観測面を返す。"""
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        conn = _conn(Path(tmp))
        _history(conn)
        result = run_replay(
            SETTINGS, symbol="USDJPY", dataset=DATASET_1M, start=WED,
            end=WED + _DAY * 3, intent_source=_intent, eval_timeframe="1h",
            history_conn=conn)
    return _json_safe({
        "orders": result.orders,
        "equity_curve": result.equity_curve,
        "metrics": compute_metrics(result),
        "fallback_spread_used": result.fallback_spread_used,
        "snapshots": result.snapshots,
        "kill_switch_events": result.kill_switch_events,
        "start": result.start.isoformat(),
        "end": result.end.isoformat(),
        "first_decision_at": result.first_decision_at.isoformat()
        if result.first_decision_at is not None else None,
    })


def main() -> None:
    _GOLDEN_PATH.write_text(
        json.dumps(build_golden(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
