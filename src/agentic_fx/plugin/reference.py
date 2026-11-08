"""Pure reference-series coordinates shared by live and historical callers."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta

import numpy as np
import pandas as pd

from agentic_fx.plugin.reference_time import (
    canonical_duration_seconds, native_reference_bars, nominal_reference_bars,
    reference_window_fits, timeframe_seconds,
)


def actual_reference_bars(decision_first: pd.Timestamp, decision_last: pd.Timestamp,
                          reference_timeframe: str) -> int:
    """Return the wall-clock slot count of the decision span, ends included.

    This sizes how far back a reference frame must be read; it is not the window
    rule (weekends have slots but no rows).  The rule counts rows, see
    ``window_reference_bars``.
    """
    first = pd.Timestamp(decision_first)
    last = pd.Timestamp(decision_last)
    if first.tz is None or last.tz is None:
        raise ValueError("reference decision times must be timezone-aware")
    if last < first:
        raise ValueError("reference decision_last must not precede decision_first")
    span_seconds = (last - first).total_seconds()
    return math.ceil(span_seconds / timeframe_seconds(reference_timeframe)) + 1


def window_reference_bars(reference_index: pd.DatetimeIndex, *,
                          decision_first: pd.Timestamp, decision_last: pd.Timestamp,
                          reference_timeframe: str) -> int:
    """Count the reference bars (rows) that closed inside the decision window.

    A row counts when its end lies in ``[decision_first, decision_last]``; this is
    the number of trading bars the strategy window really covers, the quantity
    ``nominal_reference_bars`` estimates at config time.
    """
    first = pd.Timestamp(decision_first)
    last = pd.Timestamp(decision_last)
    if first.tz is None or last.tz is None:
        raise ValueError("reference decision times must be timezone-aware")
    if len(reference_index) == 0:
        return 0
    ends = reference_index + pd.Timedelta(seconds=timeframe_seconds(reference_timeframe))
    return int(((ends >= first) & (ends <= last)).sum())


def runtime_reference_window_reason(window_bars: int, *, max_bars: int) -> str | None:
    """Return the one fail-closed reason for a runtime reference-window excess."""
    if window_bars < 0 or max_bars < 1:
        raise ValueError("reference bar counts must be non-negative / positive")
    return (None if reference_window_fits(window_bars, max_bars)
            else "reference_window_exceeded_at_runtime")


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


def _closest_closed_bar(reference_index: pd.Index, *, main_index: pd.DatetimeIndex,
                        strategy_timeframe: str, reference_timeframe: str,
                        ) -> tuple[pd.DatetimeIndex, np.ndarray, np.ndarray, np.ndarray]:
    """Locate, for every main row, the last reference bar closed at its decision time.

    Returns the sorted reference index, the position of that bar in it (-1 where no
    bar had closed yet), and the bar end and decision time of each row as int64
    nanoseconds since the epoch.  Sorting, the index checks and the end times are
    computed once; this is the single place the alignment rule lives.
    """
    if len(main_index) and (not isinstance(main_index, pd.DatetimeIndex)
                            or main_index.tz is None or main_index.hasnans):
        raise ValueError("strategy_start must be timezone-aware")
    index = reference_index.sort_values()
    if len(main_index) == 0:
        empty = np.empty(0, dtype=np.int64)
        return index, empty, empty, empty
    if not isinstance(index, pd.DatetimeIndex) or index.tz is None:
        raise ValueError("reference index must be timezone-aware DatetimeIndex")
    if index.has_duplicates:
        raise ValueError("reference index must not contain duplicates")
    ref_ns = index.tz_convert("UTC").as_unit("ns").asi8
    valid = ~np.asarray(index.isna())
    ends = ref_ns + timeframe_seconds(reference_timeframe) * 1_000_000_000
    decisions = (main_index.tz_convert("UTC").as_unit("ns").asi8
                 + timeframe_seconds(strategy_timeframe) * 1_000_000_000)
    chosen = np.full(len(main_index), -1, dtype=np.int64)
    if valid.any():
        valid_pos = np.nonzero(valid)[0]
        # NaT sorts last, so the valid bars are a sorted prefix of ``ends``
        found = np.searchsorted(ends[valid_pos], decisions, side="right") - 1
        chosen = np.where(found >= 0, valid_pos[np.where(found >= 0, found, 0)], -1)
    return index, chosen, ends, decisions


def align_reference(reference: pd.DataFrame, *, strategy_start: pd.Timestamp,
                    strategy_timeframe: str, reference_timeframe: str,
                    max_age_seconds: int) -> AlignedReference:
    """Choose the last closed reference bar available at a strategy decision."""
    start = pd.Timestamp(strategy_start)
    if start.tz is None:
        raise ValueError("strategy_start must be timezone-aware")
    decision = start + pd.Timedelta(seconds=timeframe_seconds(strategy_timeframe))
    index, chosen, ends, decisions = _closest_closed_bar(
        reference.index, main_index=pd.DatetimeIndex([start]),
        strategy_timeframe=strategy_timeframe, reference_timeframe=reference_timeframe)
    position = int(chosen[0])
    if position < 0:
        return AlignedReference(None, decision, None, None, False)
    row_start = index[position]
    end = row_start + pd.Timedelta(seconds=timeframe_seconds(reference_timeframe))
    age = pd.Timedelta(int(decisions[0] - ends[position]), unit="ns")
    return AlignedReference(row_start, decision, end, age,
                            age <= pd.Timedelta(seconds=max_age_seconds))


def align_reference_rows(reference_index: pd.Index, *, main_index: pd.DatetimeIndex,
                         strategy_timeframe: str, reference_timeframe: str,
                         max_age_seconds: int) -> tuple[pd.DatetimeIndex, np.ndarray]:
    """Vector form of ``align_reference`` over every strategy start in ``main_index``.

    Returns the sorted reference index and, per main row, the position in it of the
    last closed fresh reference bar (-1 where none is usable).
    """
    index, chosen, ends, decisions = _closest_closed_bar(
        reference_index, main_index=main_index, strategy_timeframe=strategy_timeframe,
        reference_timeframe=reference_timeframe)
    hit = chosen >= 0
    if not hit.any():
        return index, chosen
    age = decisions - ends[np.where(hit, chosen, 0)]
    fresh = hit & (age <= max_age_seconds * 1_000_000_000)
    return index, np.where(fresh, chosen, -1)
