from datetime import timedelta

import pandas as pd
import pytest

from agentic_fx.plugin.reference import (
    align_reference,
    canonical_duration_seconds,
    nominal_reference_bars,
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


@pytest.mark.parametrize(("strategy_tf", "rows", "reference_tf", "expected"), [
    ("4h", 100, "1h", 397),
    ("1h", 1000, "4h", 251),
])
def test_nominal_reference_bars_uses_the_inclusive_decision_window(
        strategy_tf, rows, reference_tf, expected):
    assert nominal_reference_bars(rows, strategy_tf, reference_tf) == expected
