"""The vector alignment must equal calling ``align_reference`` once per main row."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from agentic_fx.plugin.reference import align_reference, align_reference_rows
from agentic_fx.plugin.worker import _align_indicator_to_main


def _old(value, *, source, main_index, strategy_timeframe, reference_timeframe,
         max_age_seconds):
    aligned = []
    for strategy_start in main_index:
        selected = align_reference(
            source, strategy_start=strategy_start,
            strategy_timeframe=strategy_timeframe,
            reference_timeframe=reference_timeframe,
            max_age_seconds=max_age_seconds)
        aligned.append(
            value.loc[selected.row_start]
            if selected.fresh and selected.row_start in value.index else float("nan"))
    return pd.Series(aligned, index=main_index, dtype="float64")


def _new(value, *, source, main_index, strategy_timeframe, reference_timeframe,
         max_age_seconds):
    rows = align_reference_rows(
        source.index, main_index=main_index, strategy_timeframe=strategy_timeframe,
        reference_timeframe=reference_timeframe, max_age_seconds=max_age_seconds)
    return _align_indicator_to_main(value, rows=rows, main_index=main_index)


def _both(value, source, main_index, stf, rtf, age):
    kw = dict(source=source, main_index=main_index, strategy_timeframe=stf,
              reference_timeframe=rtf, max_age_seconds=age)
    try:
        old = _old(value, **kw)
    except ValueError as exc:
        with pytest.raises(ValueError, match=str(exc)[:20]):
            _new(value, **kw)
        return None
    new = _new(value, **kw)
    pd.testing.assert_series_equal(new, old, check_exact=True)
    return new


def _ref(stamps, tz="UTC"):
    idx = pd.DatetimeIndex(stamps, tz=tz)
    return pd.DataFrame({"close": np.arange(len(idx), dtype=float)}, index=idx)


def _main(start, n, freq, tz="UTC"):
    start = pd.Timestamp(start)
    if tz is None:
        return pd.date_range(start.tz_localize(None), periods=n, freq=freq)
    start = start.tz_localize("UTC") if start.tz is None else start
    return pd.date_range(start, periods=n, freq=freq).tz_convert(tz)


TFS = [("1h", "1h"), ("1h", "15m"), ("1h", "4h"), ("15m", "1h"), ("1h", "1d"),
       ("4h", "1h")]


@pytest.mark.parametrize("seed", range(300))
def test_random_inputs_match_per_row_alignment(seed):
    rng = np.random.default_rng(seed)
    stf, rtf = TFS[seed % len(TFS)]
    base = pd.Timestamp("2026-10-01", tz="UTC")
    n_ref = int(rng.integers(0, 60))
    offs = rng.choice(500, size=n_ref, replace=False) * 900  # 15m grid, no duplicates
    ref_idx = base + pd.to_timedelta(np.sort(offs) if seed % 3 else offs, unit="s")
    ref = _ref(ref_idx)
    if seed % 5 == 0 and n_ref > 4:
        ref = ref.drop(ref.index[rng.integers(0, n_ref, size=2)])
    value = ref["close"].copy()
    if seed % 4 == 0 and len(value):
        value.iloc[rng.integers(0, len(value))] = np.nan
    if seed % 7 == 0 and len(value) > 3:
        value = value.iloc[:-2]  # value index misses reference rows
    main = _main(base + pd.Timedelta(hours=int(rng.integers(0, 40))),
                 int(rng.integers(0, 80)), stf.replace("m", "min"),
                 tz="UTC" if seed % 2 else "Asia/Tokyo")
    age = int(rng.choice([0, 900, 3600, 7200, 86400]))
    _both(value, ref, main, stf, rtf, age)


def test_end_equal_to_decision_is_eligible_and_one_second_later_is_not():
    ref = _ref(["2026-10-27T09:00:00Z"])
    for start, expect_nan in (("2026-10-27T09:00:00Z", False),
                              ("2026-10-27T08:00:00Z", True)):
        out = _both(ref["close"], ref, pd.DatetimeIndex([start]), "1h", "1h", 3600)
        assert bool(np.isnan(out.iloc[0])) is expect_nan
    out = _both(ref["close"], ref, pd.DatetimeIndex(["2026-10-27T09:00:00Z"]),
                "1h", "1h", 0)
    assert out.iloc[0] == 0.0


def test_age_equal_to_max_is_fresh_and_one_second_over_is_stale():
    ref = _ref(["2026-10-27T08:00:00Z"])  # ends 09:00
    fresh = _both(ref["close"], ref, pd.DatetimeIndex(["2026-10-27T10:00:00Z"]),
                  "1h", "1h", 7200)
    stale = _both(ref["close"], ref, pd.DatetimeIndex(["2026-10-27T10:00:00Z"]),
                  "1h", "1h", 3599)
    assert fresh.iloc[0] == 0.0 and np.isnan(stale.iloc[0])


def test_unsorted_reference_and_nothing_closed_yet():
    ref = _ref(["2026-10-27T11:00:00Z", "2026-10-27T09:00:00Z", "2026-10-27T10:00:00Z"])
    out = _both(ref["close"], ref, _main("2026-10-27T08:00:00Z", 6, "1h"), "1h", "1h", 3600)
    assert out.iloc[:1].isna().all() and out.notna().sum() == 4


def test_duplicate_reference_index_raises_like_before():
    ref = _ref(["2026-10-27T09:00:00Z", "2026-10-27T09:00:00Z"])
    assert _both(ref["close"], ref, _main("2026-10-27T10:00:00Z", 2, "1h"),
                 "1h", "1h", 3600) is None


def test_naive_reference_index_raises_like_before():
    ref = _ref(["2026-10-27T09:00:00"], tz=None)
    assert _both(ref["close"], ref, _main("2026-10-27T10:00:00Z", 2, "1h"),
                 "1h", "1h", 3600) is None


def test_naive_main_index_raises_like_before():
    ref = _ref(["2026-10-27T09:00:00Z"])
    assert _both(ref["close"], ref, _main("2026-10-27T10:00:00", 2, "1h", tz=None),
                 "1h", "1h", 3600) is None


def test_empty_main_and_empty_reference():
    ref = _ref(["2026-10-27T09:00:00Z"])
    out = _both(ref["close"], ref, _main("2026-10-27T10:00:00Z", 0, "1h"), "1h", "1h", 3600)
    assert len(out) == 0
    empty = _ref([])
    out = _both(empty["close"], empty, _main("2026-10-27T10:00:00Z", 3, "1h"),
                "1h", "1h", 3600)
    assert out.isna().all()
