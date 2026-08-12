from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.datafeed import cache_window
from agentic_fx.datafeed.health import DataUnhealthy

UTC = timezone.utc


def test_native_interval_window_equals_lookback_days():
    """spec テスト 2: native 要求 (1h/yfinance) の窓は lookback_days そのもの。"""
    assert cache_window.live_window_days("yfinance", "1h", 5) == 5


def test_native_branch_does_not_consult_finest_native_base(monkeypatch):
    """native 分岐は base 探索を一切行わない。"""
    def _boom(source, interval):
        raise AssertionError("finest_native_base must not be called for a "
                             "native, non-derive-only interval")
    monkeypatch.setattr(cache_window, "finest_native_base", _boom)
    assert cache_window.live_window_days("yfinance", "1h", 5) == 5


def test_derive_interval_window_multiplies_by_ratio():
    """spec テスト 3: 導出要求 (4h/yfinance) の窓は lookback_days × ratio。"""
    assert cache_window.live_window_days("yfinance", "4h", 5) == 20


def test_derive_ratio_uses_finest_native_base_of_requested_source():
    """DERIVE_ONLY 強制の mt5/4h でも 1h base から導出する。"""
    assert cache_window.live_window_days("mt5", "4h", 5) == 20


def test_derive_only_1d_does_not_use_native_4h_base():
    """1d も 4h と同じく DERIVE_ONLY で、mt5 の native 4h を使わない。"""
    assert cache_window.finest_native_base("mt5", "1d") == "1h"


def test_window_raises_data_unhealthy_when_source_cannot_derive(monkeypatch):
    """提供も導出もできない source は DataUnhealthy。"""
    from agentic_fx.datafeed import sources
    monkeypatch.setitem(sources.NATIVE_INTERVALS, "toy", frozenset())
    with pytest.raises(DataUnhealthy, match="toy"):
        cache_window.live_window_days("toy", "5m", 5)


def test_base_candidates_orders_coarse_to_fine():
    assert cache_window.base_candidates("1h") == ["30m", "15m", "5m", "1m"]


def test_base_candidates_excludes_derive_only_intervals():
    """4h/1d は base 候補にならない。"""
    assert "4h" not in cache_window.base_candidates("1d")


def test_finest_native_base_picks_coarsest_native_divisor():
    assert cache_window.finest_native_base("yfinance", "4h") == "1h"


def test_floor_to_interval_rejects_naive_datetime():
    with pytest.raises(ValueError, match="naive"):
        cache_window.floor_to_interval(datetime(2026, 7, 22, 13, 45), "1h")


def test_floor_to_interval_1h_floors_down_within_hour():
    ts = datetime(2026, 7, 22, 13, 45, 30, tzinfo=UTC)
    assert cache_window.floor_to_interval(ts, "1h") == datetime(
        2026, 7, 22, 13, 0, tzinfo=UTC)


def test_floor_to_interval_1d_floors_to_utc_midnight():
    """spec テスト 9: 1d の floor が UTC 00:00 になる。"""
    ts = datetime(2026, 7, 22, 23, 59, 59, tzinfo=UTC)
    assert cache_window.floor_to_interval(ts, "1d") == datetime(
        2026, 7, 22, 0, 0, tzinfo=UTC)


def test_floor_to_interval_1d_across_month_boundary():
    ts = datetime(2026, 8, 1, 3, 0, tzinfo=UTC)
    assert cache_window.floor_to_interval(ts, "1d") == datetime(
        2026, 8, 1, 0, 0, tzinfo=UTC)


def test_floor_to_interval_is_idempotent_on_boundary():
    ts = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    once = cache_window.floor_to_interval(ts, "1h")
    twice = cache_window.floor_to_interval(once, "1h")
    assert once == twice == ts


def test_floor_to_interval_non_utc_offset_normalizes_to_utc():
    jst = timezone(timedelta(hours=9))
    ts = datetime(2026, 7, 22, 21, 30, tzinfo=jst)
    assert cache_window.floor_to_interval(ts, "1h") == datetime(
        2026, 7, 22, 12, 0, tzinfo=UTC)
