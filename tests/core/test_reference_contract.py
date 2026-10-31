from datetime import timedelta
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from agentic_fx.plugin.reference import (
    align_reference,
    actual_reference_bars,
    canonical_duration_seconds,
    complete_reference_buckets,
    native_reference_bars,
    nominal_reference_bars,
    runtime_reference_window_reason,
)


def test_align_reference_uses_the_bar_closed_at_decision_time_not_one_bar_behind():
    reference = pd.DataFrame(
        {"close": [1.0]},
        index=pd.DatetimeIndex(["2026-10-26T09:00:00Z"]),
    )

    aligned = align_reference(reference, strategy_start=pd.Timestamp("2026-10-26T09:00:00Z"),
                              strategy_timeframe="1h", reference_timeframe="1h",
                              max_age_seconds=3 * 3600)

    assert aligned.row_start == pd.Timestamp("2026-10-26T09:00:00Z")
    assert aligned.age == timedelta(0)
    assert aligned.fresh is True


@pytest.mark.parametrize(("value", "seconds"), [
    ("1s", 1), ("5m", 300), ("3h", 10_800), ("7d", 604_800),
])
def test_reference_duration_is_canonical_seconds(value, seconds):
    assert canonical_duration_seconds(value) == seconds


@pytest.mark.parametrize("value", ["0s", "1.5h", "-1h", "1w", True, 1])
def test_reference_duration_rejects_non_contract_forms(value):
    with pytest.raises(ValueError):
        canonical_duration_seconds(value)


def test_align_reference_is_fresh_at_boundary_and_stale_one_second_after():
    reference = pd.DataFrame(
        {"close": [1.0]}, index=pd.DatetimeIndex(["2026-10-27T07:00:00Z"]),
    )
    fresh = align_reference(reference, strategy_start=pd.Timestamp("2026-10-27T10:00:00Z"),
                            strategy_timeframe="1h", reference_timeframe="1h",
                            max_age_seconds=3 * 3600)
    stale = align_reference(reference, strategy_start=pd.Timestamp("2026-10-27T10:00:01Z"),
                            strategy_timeframe="1h", reference_timeframe="1h",
                            max_age_seconds=3 * 3600)
    assert fresh.fresh is True
    assert stale.fresh is False


def test_align_reference_uses_latest_end_even_when_input_is_reverse_sorted():
    reference = pd.DataFrame(
        {"close": [9.0, 8.0]},
        index=pd.DatetimeIndex(["2026-10-26T09:00:00Z", "2026-10-26T08:00:00Z"]),
    )

    aligned = align_reference(reference, strategy_start=pd.Timestamp("2026-10-26T09:00:00Z"),
                              strategy_timeframe="1h", reference_timeframe="1h",
                              max_age_seconds=3600)

    assert aligned.row_start == pd.Timestamp("2026-10-26T09:00:00Z")
    assert aligned.age == timedelta(0)


def test_align_reference_rejects_duplicate_reference_timestamps():
    reference = pd.DataFrame(
        {"close": [1.0, 2.0]},
        index=pd.DatetimeIndex(["2026-10-26T09:00:00Z", "2026-10-26T09:00:00Z"]),
    )

    with pytest.raises(ValueError, match="must not contain duplicates"):
        align_reference(reference, strategy_start=pd.Timestamp("2026-10-26T09:00:00Z"),
                        strategy_timeframe="1h", reference_timeframe="1h",
                        max_age_seconds=3600)


def test_resolver_import_keeps_pandas_out_of_the_parent_process():
    root = os.fspath(Path(__file__).resolve().parents[2] / "src")
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import agentic_fx.plugin.resolve; "
         "assert 'pandas' not in sys.modules"],
        env={**os.environ, "PYTHONPATH": root}, text=True, capture_output=True,
        check=False)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(("strategy_tf", "rows", "reference_tf", "expected"), [
    ("4h", 100, "1h", 397),
    ("1h", 1000, "4h", 251),
])
def test_nominal_reference_bars_uses_the_inclusive_decision_window(
        strategy_tf, rows, reference_tf, expected):
    assert nominal_reference_bars(rows, strategy_tf, reference_tf) == expected


def test_actual_reference_bars_uses_real_decision_span_including_both_ends():
    assert actual_reference_bars(
        pd.Timestamp("2026-10-23T15:00:00Z"),
        pd.Timestamp("2026-10-27T23:00:00Z"), "1h") == 105


def test_actual_reference_window_over_limit_has_the_fixed_fail_closed_reason():
    actual = actual_reference_bars(
        pd.Timestamp("2026-10-23T15:00:00Z"),
        pd.Timestamp("2026-10-27T23:00:00Z"), "1h")
    assert runtime_reference_window_reason(actual, max_bars=100) == \
        "reference_window_exceeded_at_runtime"
    assert runtime_reference_window_reason(100, max_bars=100) is None


@pytest.mark.parametrize(("reference_tf", "expected"), [
    ("4h", 12), ("1d", 72),
])
def test_native_reference_bars_requires_full_hourly_inputs(reference_tf, expected):
    assert native_reference_bars(3, reference_tf, "1h") == expected


def _hourly_frame(start: str, periods: int) -> pd.DataFrame:
    index = pd.date_range(start, periods=periods, freq="1h", tz="UTC")
    values = list(range(periods))
    return pd.DataFrame({"open": values, "high": [v + 1 for v in values],
                         "low": [v - 1 for v in values], "close": values,
                         "volume": [10] * periods}, index=index)


def test_complete_reference_buckets_drops_incomplete_head_and_tail_4h():
    # 01:00 starts inside the 00:00 bucket (head is unavailable); 12:00--14:00
    # are a partial tail.  Only UTC buckets [04:00,08:00) and [08:00,12:00)
    # are valid reference rows.
    frame = _hourly_frame("2026-10-26T01:00:00Z", 13)
    got = complete_reference_buckets(frame, effective_timeframe="4h")
    assert list(got.index) == [pd.Timestamp("2026-10-26T04:00:00Z"),
                               pd.Timestamp("2026-10-26T08:00:00Z")]
    assert list(got["open"]) == [3, 7]
    assert list(got["close"]) == [6, 10]


def test_complete_reference_buckets_uses_utc_midnight_and_drops_partial_day():
    frame = _hourly_frame("2026-10-25T23:00:00Z", 27)
    got = complete_reference_buckets(frame, effective_timeframe="1d")
    assert list(got.index) == [pd.Timestamp("2026-10-26T00:00:00Z")]
    assert got.iloc[0].to_dict() == {"open": 1, "high": 25, "low": 0,
                                      "close": 24, "volume": 240}
