"""データの停止の通知に使う時間条件。state machine と起動補完が同じ式を使う。"""
from __future__ import annotations

from datetime import datetime, timedelta

from agentic_fx.core import market_hours

# degraded になってから開場時間でこの分数がたっても待ち・復帰にならない停止を、一度だけ知らせる
STOP_OPEN_MINUTES = 10


def stop_after_open_minutes(entered_degraded_at: str | None, now: datetime) -> bool:
    """`entered_degraded_at` から `now` までの開場時間が閾値以上か。

    休場・祝日は積算しない。永続の開始時刻から毎回計算するので、再起動で時計が戻らない。
    開始時刻が無い・読めないときは偽。"""
    if not entered_degraded_at:
        return False
    try:
        entered = datetime.fromisoformat(entered_degraded_at)
    except (TypeError, ValueError):
        return False
    if entered.tzinfo is None:
        return False
    return market_hours.open_time_between(entered, now) >= timedelta(minutes=STOP_OPEN_MINUTES)


def timer_start(entered_degraded_at: str | None, last_result_at: str | None) -> str | None:
    """時間条件を数え始める時刻。要求への結果が出ていれば、その判定時刻から数え直す
    (まだ停止を知らせていない epoch で、判定の後も続く停止を知らせるため。停止を知らせた後の
    epoch には呼び出し側が使わない)。結果の時刻が
    読めなければ degraded に入った時刻を使う。"""
    if last_result_at:
        try:
            result = datetime.fromisoformat(last_result_at)
            entered = datetime.fromisoformat(entered_degraded_at) if entered_degraded_at else None
        except (TypeError, ValueError):
            return entered_degraded_at
        if result.tzinfo is not None and (entered is None or result > entered):
            return last_result_at
    return entered_degraded_at
