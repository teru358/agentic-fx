"""The vector alignment must equal calling ``align_reference`` once per main row."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from agentic_fx.plugin.reference import (
    AlignedReference, align_reference, align_reference_rows,
)
from agentic_fx.plugin.reference_time import timeframe_seconds
from agentic_fx.plugin.worker import _align_indicator_to_main


def _oracle_align_reference(reference, *, strategy_start, strategy_timeframe,
                            reference_timeframe, max_age_seconds):
    """Independent one-row implementation the shared alignment is checked against."""
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


def _old(value, *, source, main_index, strategy_timeframe, reference_timeframe,
         max_age_seconds):
    aligned = []
    for strategy_start in main_index:
        selected = _oracle_align_reference(
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
    idx = pd.DatetimeIndex(stamps)
    if idx.tz is None and tz is not None:
        idx = idx.tz_localize(tz)
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
    ref = _ref(ref_idx.tz_convert(["UTC", "Asia/Tokyo", "America/New_York"][seed % 3]), tz=None)
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
    ref = _ref(["2026-10-27T08:00:00Z"])  # ends 09:00, decision 11:00 -> age 7200
    main = pd.DatetimeIndex(["2026-10-27T10:00:00Z"])
    fresh = _both(ref["close"], ref, main, "1h", "1h", 7200)
    stale = _both(ref["close"], ref, main, "1h", "1h", 7199)
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


def test_nat_in_main_index_raises_like_before():
    ref = _ref(["2026-10-27T09:00:00Z"])
    main = pd.DatetimeIndex(["2026-10-27T10:00:00Z", pd.NaT, "2026-10-27T12:00:00Z"])
    assert _both(ref["close"], ref, main, "1h", "1h", 3600) is None


def test_nat_in_reference_index_matches_before():
    idx = pd.DatetimeIndex(["2026-10-27T08:00:00Z", pd.NaT, "2026-10-27T09:00:00Z"])
    ref = pd.DataFrame({"close": [0.0, 1.0, 2.0]}, index=idx)
    _both(ref["close"], ref, _main("2026-10-27T09:00:00Z", 4, "1h"), "1h", "1h", 7200)


@pytest.mark.parametrize("tz", ["Asia/Tokyo", "America/New_York", "Europe/London"])
def test_non_utc_reference_tz_matches_before(tz):
    ref = _ref(pd.date_range("2026-10-27T00:00:00Z", periods=10, freq="h").tz_convert(tz))
    _both(ref["close"], ref, _main("2026-10-27T02:00:00Z", 12, "1h"), "1h", "1h", 3600)


@pytest.mark.parametrize("tz,start", [("America/New_York", "2026-11-01T03:00:00Z"),
                                      ("Europe/London", "2026-10-25T21:00:00Z"),
                                      ("America/New_York", "2026-03-08T03:00:00Z")])
def test_bars_across_dst_change_match_before(tz, start):
    ref = _ref(pd.date_range(start, periods=30, freq="h", tz="UTC").tz_convert(tz))
    main = _main(start, 40, "1h", tz=tz)
    for stf, rtf in (("1h", "1h"), ("1h", "4h"), ("4h", "1h")):
        _both(ref["close"], ref, main[::1], stf, rtf, 14400)
    _both(ref["close"], ref, _main(start, 10, "1D", tz=tz), "1h", "1h", 86400)


@pytest.mark.parametrize("make", [lambda n: np.arange(n, dtype="int64"),
                                  lambda n: np.arange(n) % 2 == 0,
                                  lambda n: np.arange(n, dtype="int32"),
                                  lambda n: np.arange(n, dtype="float32")])
def test_bool_and_int_value_dtypes_match_before(make):
    ref = _ref(pd.date_range("2026-10-27T00:00:00Z", periods=10, freq="h"))
    value = pd.Series(make(10), index=ref.index)
    _both(value, ref, _main("2026-10-27T02:00:00Z", 12, "1h"), "1h", "1h", 3600)


def test_value_with_duplicate_index_is_refused():
    ref = _ref(["2026-10-27T08:00:00Z", "2026-10-27T09:00:00Z"])
    value = pd.Series([1.0, 2.0, 3.0], index=ref.index[[0, 1, 1]])
    with pytest.raises(ValueError, match="unique"):
        _new(value, source=ref, main_index=_main("2026-10-27T10:00:00Z", 2, "1h"),
             strategy_timeframe="1h", reference_timeframe="1h", max_age_seconds=3600)


@pytest.mark.parametrize("values", [[pd.NA, 1.0], ["a", "b"], [None, 1.0]])
def test_value_with_object_dtype_is_refused(values):
    ref = _ref(["2026-10-27T08:00:00Z", "2026-10-27T09:00:00Z"])
    value = pd.Series(values, index=ref.index, dtype=object)
    with pytest.raises(ValueError, match="numeric"):
        _new(value, source=ref, main_index=_main("2026-10-27T10:00:00Z", 2, "1h"),
             strategy_timeframe="1h", reference_timeframe="1h", max_age_seconds=3600)


def test_worker_contract_hands_over_unique_float64_series():
    """Equivalence with the per-row form holds for inputs the plugin contract admits:
    a float64 Series on the frame's own index.  A duplicated frame index is refused
    by the alignment itself.  If the contract loosens, this goes red."""
    from agentic_fx.core.plugin_contract import validate_indicator_result

    ref = _ref(pd.date_range("2026-10-27T00:00:00Z", periods=6, freq="h"))
    for raw in (ref["close"].astype("int64"), ref["close"].astype(object),
                ref["close"].where(ref["close"] > 2)):
        out = validate_indicator_result({"v": raw}, df_index=ref.index, outputs=["v"])["v"]
        assert out.dtype == np.float64 and out.index.is_unique
        _both(out, ref, _main("2026-10-27T02:00:00Z", 8, "1h"), "1h", "1h", 3600)
    dup = ref.iloc[[0, 1, 1]]
    with pytest.raises(ValueError, match="duplicates"):
        align_reference_rows(dup.index, main_index=_main("2026-10-27T05:00:00Z", 2, "1h"),
                             strategy_timeframe="1h", reference_timeframe="1h",
                             max_age_seconds=3600)


def _same_aligned(got, want):
    assert got.fresh is want.fresh or got.fresh == want.fresh
    assert isinstance(got.fresh, bool)
    assert got.decision_time == want.decision_time
    assert got.decision_time.tz == want.decision_time.tz
    for field in ("row_start", "reference_end"):
        a, b = getattr(got, field), getattr(want, field)
        assert (a is None) == (b is None)
        if a is not None:
            assert isinstance(a, pd.Timestamp) and a == b and a.tz == b.tz
    assert (got.age is None) == (want.age is None)
    if got.age is not None:
        assert isinstance(got.age, pd.Timedelta) and got.age == want.age


@pytest.mark.parametrize("seed", range(200))
def test_align_reference_wrapper_matches_the_one_row_oracle(seed):
    rng = np.random.default_rng(1000 + seed)
    stf, rtf = TFS[seed % len(TFS)]
    base = pd.Timestamp("2026-10-01", tz="UTC")
    n_ref = int(rng.integers(0, 40))
    offs = rng.choice(500, size=n_ref, replace=False) * 900
    ref_idx = base + pd.to_timedelta(offs, unit="s")
    ref = _ref(ref_idx.tz_convert(["UTC", "Asia/Tokyo", "America/New_York"][seed % 3]),
               tz=None)
    start = (base + pd.Timedelta(seconds=int(rng.integers(0, 600)) * 900)
             ).tz_convert("UTC" if seed % 2 else "Asia/Tokyo")
    kw = dict(strategy_start=start, strategy_timeframe=stf,
              reference_timeframe=rtf,
              max_age_seconds=int(rng.choice([0, 900, 3600, 86400])))
    _same_aligned(align_reference(ref, **kw), _oracle_align_reference(ref, **kw))


def test_align_reference_stale_keeps_the_bar_and_age():
    ref = _ref(["2026-10-27T09:00:00Z"])
    got = align_reference(ref, strategy_start=pd.Timestamp("2026-10-27T12:00:00Z"),
                          strategy_timeframe="1h", reference_timeframe="1h",
                          max_age_seconds=60)
    assert got.fresh is False
    assert got.reference_end == pd.Timestamp("2026-10-27T10:00:00Z")
    assert got.age == pd.Timedelta(hours=3)


@pytest.mark.parametrize("bad", ["2026-10-27T10:00:00", pd.NaT])
def test_align_reference_wrapper_rejects_naive_or_nat_start(bad):
    ref = _ref(["2026-10-27T09:00:00Z"])
    with pytest.raises(ValueError, match="timezone-aware"):
        align_reference(ref, strategy_start=pd.Timestamp(bad), strategy_timeframe="1h",
                        reference_timeframe="1h", max_age_seconds=3600)


@pytest.mark.parametrize("main", [pd.RangeIndex(3), pd.Index(["a", "b"], dtype=object)])
def test_non_datetime_main_index_raises_value_error(main):
    ref = _ref(["2026-10-27T09:00:00Z"])
    with pytest.raises(ValueError, match="timezone-aware"):
        align_reference_rows(ref.index, main_index=main, strategy_timeframe="1h",
                             reference_timeframe="1h", max_age_seconds=3600)


def test_bad_main_is_reported_before_a_bad_reference():
    naive_ref = _ref(["2026-10-27T09:00:00"], tz=None)
    with pytest.raises(ValueError, match="strategy_start"):
        align_reference_rows(naive_ref.index, main_index=pd.RangeIndex(2),
                             strategy_timeframe="1h", reference_timeframe="1h",
                             max_age_seconds=3600)
