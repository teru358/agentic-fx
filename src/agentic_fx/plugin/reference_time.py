"""Pandas に依存しない参照系列の時間計算。"""
from __future__ import annotations

import math
import re

_DURATION = re.compile(r"^([1-9][0-9]*)([smhd])$")
_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_TIMEFRAME_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600,
                      "4h": 14400, "1d": 86400}
PLUGIN_TIMEFRAMES = ("15m", "1h", "4h", "1d")
DEFAULT_MAX_AGE_FACTOR = 3


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


def native_reference_bars(actual_bars: int, reference_timeframe: str,
                          native_timeframe: str) -> int:
    if actual_bars < 1:
        raise ValueError("actual_bars must be positive")
    return math.ceil(actual_bars * timeframe_seconds(reference_timeframe)
                     / timeframe_seconds(native_timeframe))
