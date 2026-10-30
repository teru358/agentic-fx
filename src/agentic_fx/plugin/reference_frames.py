"""Live reference frames for strategy calls, built from the closed native cache."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pandas as pd

from agentic_fx.backtest.timeframes import (
    TF_MINUTES, _COLUMNS, _table, floor_to_bucket,
)
from agentic_fx.datafeed.requirements import native_interval
from agentic_fx.plugin.reference import (
    AlignedReference, actual_reference_bars, align_reference,
    complete_reference_buckets,
)
from agentic_fx.plugin.reference_time import timeframe_seconds

# Reasons a reference frame cannot be used.  Fetch states reported by ingest
# win over what the cache alone shows, in this order.
FETCH_STATE_REASONS = ("source_unavailable", "fetch_failed", "budget_deferred")
REFERENCE_REASONS = (*FETCH_STATE_REASONS, "missing", "age_exceeded",
                     "indicator_unavailable")
RUNTIME_WINDOW_REASON = "reference_window_exceeded_at_runtime"


def actual_bars_for(df_index: pd.DatetimeIndex, strategy_timeframe: str,
                    reference_timeframe: str) -> int:
    tf = pd.Timedelta(seconds=timeframe_seconds(strategy_timeframe))
    return actual_reference_bars(df_index[0] + tf, df_index[-1] + tf,
                                 reference_timeframe)


def load_reference_frame(conn: sqlite3.Connection, symbol: str, *,
                         reference_timeframe: str, source: str,
                         d_last: pd.Timestamp,
                         now: datetime, grace: timedelta, actual_bars: int,
                         max_age_seconds: int) -> pd.DataFrame:
    """Closed reference rows over the real-time span the strategy window covers.

    The span is ``[d_last - actual_bars * ref_tf, d_last]`` floored to the
    reference grid; weekends and gaps simply have no rows.  It reaches back at
    least ``max_age + ref_tf`` so a row that is still fresh at ``d_last`` is
    never cut off by a short strategy window.  Only rows that
    ended by ``d_last`` and are confirmed by ``now`` are returned.  4h / 1d
    frames are aggregated from complete UTC hours only.
    """
    native = native_interval(reference_timeframe)
    native_width = timedelta(minutes=TF_MINUTES[native])
    ref_width = timedelta(seconds=timeframe_seconds(reference_timeframe))
    d_last_dt = pd.Timestamp(d_last).to_pydatetime()
    lookback = max(ref_width * actual_bars,
                   timedelta(seconds=max_age_seconds) + ref_width)
    lower = floor_to_bucket(d_last_dt - lookback, reference_timeframe)
    now_utc = now.astimezone(timezone.utc)
    rows = conn.execute(
        f"SELECT bar_time, open, high, low, close, volume FROM {_table(source)} "
        "WHERE symbol=? AND interval=? AND source=? AND bar_time>=? AND bar_time<? "
        "ORDER BY bar_time",
        (symbol, native, source, lower.isoformat(), d_last_dt.isoformat())).fetchall()
    index = pd.DatetimeIndex(pd.to_datetime([r["bar_time"] for r in rows], utc=True))
    df = pd.DataFrame({c: [r[c] for r in rows] for c in _COLUMNS},
                      index=index, columns=list(_COLUMNS), dtype="float64")
    df = df[(df.index + native_width + grace <= now_utc)
            & (df.index + native_width <= pd.Timestamp(d_last))]
    if reference_timeframe in ("4h", "1d"):
        df = complete_reference_buckets(df, effective_timeframe=reference_timeframe)
    return df


def judge_reference(frame: pd.DataFrame, *, bucket_start: pd.Timestamp,
                    strategy_timeframe: str, reference_timeframe: str,
                    max_age_seconds: int) -> AlignedReference:
    return align_reference(
        frame, strategy_start=bucket_start, strategy_timeframe=strategy_timeframe,
        reference_timeframe=reference_timeframe, max_age_seconds=max_age_seconds)


def latest_native_end(conn: sqlite3.Connection, symbol: str, *,
                      reference_timeframe: str, source: str,
                      d_last: pd.Timestamp) -> pd.Timestamp | None:
    """End of the newest native row that had closed by ``d_last``, if any."""
    native = native_interval(reference_timeframe)
    width = timedelta(minutes=TF_MINUTES[native])
    limit = (pd.Timestamp(d_last) - pd.Timedelta(width)).to_pydatetime()
    row = conn.execute(
        f"SELECT MAX(bar_time) FROM {_table(source)} "
        "WHERE symbol=? AND interval=? AND source=? AND bar_time<=?",
        (symbol, native, source, limit.isoformat())).fetchone()
    if not row or row[0] is None:
        return None
    return pd.Timestamp(row[0]).tz_convert("UTC") + pd.Timedelta(width)
