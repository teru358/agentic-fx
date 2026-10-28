"""Pure reference-series coordinates shared by live and historical callers."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

_DURATION = re.compile(r"^([1-9][0-9]*)([smhd])$")
_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_TIMEFRAME_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600,
                      "4h": 14400, "1d": 86400}


def canonical_duration_seconds(value: object) -> int:
    if not isinstance(value, str):
        raise ValueError("reference_duration_invalid")
    match = _DURATION.fullmatch(value)
    if match is None:
        raise ValueError("reference_duration_invalid")
    return int(match.group(1)) * _SECONDS[match.group(2)]


def timeframe_seconds(timeframe: str) -> int:
    try:
        return _TIMEFRAME_SECONDS[timeframe]
    except KeyError as exc:
        raise ValueError(f"reference_timeframe_invalid:{timeframe}") from exc


def nominal_reference_bars(strategy_max_bars: int, strategy_timeframe: str,
                           reference_timeframe: str) -> int:
    return math.ceil(((strategy_max_bars - 1) * timeframe_seconds(strategy_timeframe))
                     / timeframe_seconds(reference_timeframe)) + 1


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
    index = reference.index
    if not isinstance(index, pd.DatetimeIndex) or index.tz is None:
        raise ValueError("reference index must be timezone-aware DatetimeIndex")
    ends = index + pd.Timedelta(seconds=timeframe_seconds(reference_timeframe))
    eligible = ends <= decision
    if not eligible.any():
        return AlignedReference(None, decision, None, None, False)
    position = eligible.nonzero()[0][-1]
    end = ends[position]
    age = decision - end
    return AlignedReference(index[position], decision, end, age,
                            age <= pd.Timedelta(seconds=max_age_seconds))
