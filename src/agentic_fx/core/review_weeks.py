"""週次の確定の暦。

NY 取引日 (`market_hours.trading_date`) の月〜金を 1 週とし、週の最後の取引日の
終端 (NY 17:00 の rollover) を窓の終端とする。平日の休場では週を割らず、最後の取引日が
前へずれるだけ。週の印は終端の 1 分前の取引日で、確定は終端から猶予の後。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from agentic_fx.core import market_hours
from agentic_fx.core.fx_time import session_boundary_utc
from agentic_fx.store.ohlcv_slices import FINALIZE_GRACE

_ONE_MINUTE = timedelta(minutes=1)
_WEEKDAYS = 5
# 列挙の上限 (壊れた開始週で無限に回らないための安全弁)
MAX_WEEKS = 1040


def parse_label(text: object) -> date | None:
    """`YYYY-MM-DD` の実在日だけを読む。"""
    if type(text) is not str or len(text) != 10:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def week_label(close_at: datetime) -> date:
    return market_hours.trading_date(close_at - _ONE_MINUTE)


def close_of_week(day: date) -> datetime | None:
    """`day` を含む月〜金の週の、最後の取引日の終端。週に取引日が無ければ None。"""
    monday = day - timedelta(days=day.weekday())
    for offset in range(_WEEKDAYS - 1, -1, -1):
        boundary = session_boundary_utc(monday + timedelta(days=offset))
        if market_hours.is_market_open(boundary - _ONE_MINUTE):
            return boundary
    return None


def week_close_for_label(label: date) -> datetime | None:
    """週の印 (最後の取引日) から終端を返す。印が最後の取引日でなければ None。"""
    close = close_of_week(label)
    if close is None or week_label(close) != label:
        return None
    return close


def finalize_at(close_at: datetime) -> datetime:
    return close_at + FINALIZE_GRACE


def _next_monday(day: date) -> date:
    return day - timedelta(days=day.weekday()) + timedelta(days=7)


def latest_due_week(now: datetime) -> date | None:
    """確定の猶予が過ぎた最新の週の印。"""
    monday = now.date() - timedelta(days=now.date().weekday())
    for _ in range(8):
        close = close_of_week(monday)
        if close is not None and finalize_at(close) <= now:
            return week_label(close)
        monday -= timedelta(days=7)
    return None


def due_weeks(now: datetime, *, first: date) -> list[tuple[date, datetime]]:
    """`first` の週から、猶予が過ぎた週までを古い順に (週の印, 終端) で返す。"""
    out: list[tuple[date, datetime]] = []
    day = first - timedelta(days=first.weekday())
    for _ in range(MAX_WEEKS):
        close = close_of_week(day)
        if close is not None:
            if finalize_at(close) > now:
                break
            out.append((week_label(close), close))
        elif day > now.date():
            break
        day = _next_monday(day)
    return out
