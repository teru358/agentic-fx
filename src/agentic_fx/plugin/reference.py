"""Pure reference-series coordinates shared by live and historical callers."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from agentic_fx.plugin.reference_time import (
    canonical_duration_seconds, native_reference_bars, nominal_reference_bars,
    timeframe_seconds,
)


def actual_reference_bars(decision_first: pd.Timestamp, decision_last: pd.Timestamp,
                          reference_timeframe: str) -> int:
    """Return the inclusive effective-reference window for actual decision times."""
    first = pd.Timestamp(decision_first)
    last = pd.Timestamp(decision_last)
    if first.tz is None or last.tz is None:
        raise ValueError("reference decision times must be timezone-aware")
    if last < first:
        raise ValueError("reference decision_last must not precede decision_first")
    span_seconds = (last - first).total_seconds()
    return math.ceil(span_seconds / timeframe_seconds(reference_timeframe)) + 1


def runtime_reference_window_reason(actual_bars: int, *, max_bars: int) -> str | None:
    """Return the one fail-closed reason for a runtime reference-window excess."""
    if actual_bars < 1 or max_bars < 1:
        raise ValueError("reference bar counts must be positive")
    return ("reference_window_exceeded_at_runtime"
            if actual_bars > max_bars else None)


def complete_reference_buckets(native: pd.DataFrame, *,
                               effective_timeframe: str) -> pd.DataFrame:
    """Aggregate only complete UTC 1h buckets for a 4h or 1d reference frame.

    A missing leading native hour leaves the corresponding effective row absent
    (therefore unavailable to as-of alignment), and an incomplete trailing bucket
    is likewise excluded.  This function never mutates ``native``.
    """
    if effective_timeframe not in ("4h", "1d"):
        raise ValueError("effective_timeframe must be 4h or 1d")
    required_columns = ("open", "high", "low", "close", "volume")
    missing = [column for column in required_columns if column not in native.columns]
    if missing:
        raise ValueError(f"native reference frame missing columns: {missing}")
    if not isinstance(native.index, pd.DatetimeIndex) or native.index.tz is None:
        raise ValueError("native reference index must be timezone-aware DatetimeIndex")
    if native.index.has_duplicates:
        raise ValueError("native reference index must not contain duplicates")

    source = native.loc[:, list(required_columns)].copy(deep=True)
    source.index = source.index.tz_convert("UTC")
    source = source.sort_index()
    required_rows = native_reference_bars(1, effective_timeframe, "1h")
    rows: list[dict] = []
    labels: list[pd.Timestamp] = []
    bucket_rule = "1D" if effective_timeframe == "1d" else effective_timeframe
    for bucket, group in source.groupby(source.index.floor(bucket_rule)):
        expected = pd.date_range(bucket, periods=required_rows, freq="1h", tz="UTC")
        if not group.index.equals(expected):
            continue
        labels.append(bucket)
        rows.append({"open": group["open"].iloc[0],
                     "high": group["high"].max(),
                     "low": group["low"].min(),
                     "close": group["close"].iloc[-1],
                     "volume": group["volume"].sum()})
    if not rows:
        return pd.DataFrame({column: pd.Series(dtype="float64")
                             for column in required_columns},
                            index=pd.DatetimeIndex([], tz="UTC"))
    return pd.DataFrame(rows, index=pd.DatetimeIndex(labels, tz="UTC"),
                        columns=list(required_columns))


@dataclass(frozen=True, slots=True)
class AlignedReference:
    row_start: pd.Timestamp | None
    decision_time: pd.Timestamp
    reference_end: pd.Timestamp | None
    age: timedelta | None
    fresh: bool


def align_reference(reference: pd.DataFrame, *, strategy_start: pd.Timestamp,
                    strategy_timeframe: str, reference_timeframe: str,
                    max_age_seconds: int) -> AlignedReference:
    """Choose the last closed reference bar available at a strategy decision."""
    decision = pd.Timestamp(strategy_start)
    if decision.tz is None:
        raise ValueError("strategy_start must be timezone-aware")
    decision += pd.Timedelta(seconds=timeframe_seconds(strategy_timeframe))
    reference = reference.sort_index()
    index = reference.index
    if not isinstance(index, pd.DatetimeIndex) or index.tz is None:
        raise ValueError("reference index must be timezone-aware DatetimeIndex")
    if index.has_duplicates:
        raise ValueError("reference index must not contain duplicates")
    index = reference.index
    ends = index + pd.Timedelta(seconds=timeframe_seconds(reference_timeframe))
    eligible = ends <= decision
    if not eligible.any():
        return AlignedReference(None, decision, None, None, False)
    position = ends.argmax()
    if not eligible[position]:
        position = eligible.nonzero()[0][ends[eligible].argmax()]
    end = ends[position]
    age = decision - end
    return AlignedReference(index[position], decision, end, age,
                            age <= pd.Timedelta(seconds=max_age_seconds))
