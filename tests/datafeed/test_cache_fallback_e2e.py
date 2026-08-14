from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from agentic_fx.core.contracts import Bar, FixedClock
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.service import build_app, run_init
from agentic_fx.store import ohlcv
from tests.store.test_rag import FakeEmbedding
from tests.test_service_app import _no_real_network


OPEN_NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
PAIR = "USDJPY"
LIVE_SOURCE = "yfinance"


def _one_minute_bars(now: datetime = OPEN_NOW, n: int = 181) -> list[Bar]:
    start = now - timedelta(minutes=n - 1)
    return [
        Bar(PAIR, "1m", start + timedelta(minutes=i),
            148.0 + i * 0.0001, 148.1 + i * 0.0001,
            147.9 + i * 0.0001, 148.05 + i * 0.0001, 100.0)
        for i in range(n)
    ]


def _init_root(root) -> None:
    (root / "config").mkdir()
    example = open("config/settings.yaml.example", encoding="utf-8").read()
    (root / "config" / "settings.yaml.example").write_text(
        example, encoding="utf-8")
    with patch("agentic_fx.service.PriceProvider") as provider_cls, \
         patch("agentic_fx.service._check_llama_swap"):
        provider_cls.return_value.healthcheck.return_value = LIVE_SOURCE
        run_init(root)


def _build_real_app(root):
    _init_root(root)
    return build_app(
        root,
        runner=FakeRunner([]),
        clock=FixedClock(OPEN_NOW),
        embedding_fn=FakeEmbedding(),
    )


def _only_one_minute(pair: str, interval: str,
                     lookback_days: int) -> list[Bar]:
    assert pair == PAIR
    if interval != "1m":
        raise OSError(f"fixture intentionally has no live {interval}")
    # 毎回新しい list を返す。呼び出し回数に依存して枯れない。
    return list(_one_minute_bars())


def _all_live_fetchers_down(pair: str, interval: str,
                            lookback_days: int) -> list[Bar]:
    raise OSError(f"all live fetchers down: {pair} {interval}")


def test_tick_processed_bar_marking_persists_one_minute_without_orders(tmp_path):
    """spec ③ テスト 12: 無注文でも settings.pairs の marking が 1m を書く。"""
    app = _build_real_app(tmp_path)
    try:
        assert app.conn_core.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        with _no_real_network(), \
             patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
                   side_effect=_only_one_minute):
            app.scheduler.tick(OPEN_NOW)
        rows = ohlcv.load_cache_bars(
            app.conn_core, PAIR, "1m", source=LIVE_SOURCE)
        assert rows
        assert app.conn_core.execute(
            "SELECT COUNT(*) FROM ohlcv_cache WHERE symbol=? AND interval='1m' "
            "AND source=?", (PAIR, LIVE_SOURCE)).fetchone()[0] == len(rows)
    finally:
        app.close()


def test_build_app_tick_then_final_one_minute_to_one_hour_fallback(tmp_path):
    """spec ③ テスト 11: factory→無注文 tick→live 全滅→最終導出を pin。"""
    app = _build_real_app(tmp_path)
    try:
        assert app.conn_core.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        with _no_real_network(), \
             patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
                   side_effect=_only_one_minute):
            app.scheduler.tick(OPEN_NOW)

        one_hour_count = app.conn_core.execute(
            "SELECT COUNT(*) FROM ohlcv_cache WHERE symbol=? AND interval='1h' "
            "AND source=?", (PAIR, LIVE_SOURCE)).fetchone()[0]
        assert one_hour_count == 0  # 前提条件: 同 source の 1h cache は空

        one_minute_count = app.conn_core.execute(
            "SELECT COUNT(*) FROM ohlcv_cache WHERE symbol=? AND interval='1m' "
            "AND source=?", (PAIR, LIVE_SOURCE)).fetchone()[0]
        if one_minute_count == 0:
            pytest.fail(
                "setup failure: tick did not persist yfinance 1m cache; "
                "test_tick_processed_bar_marking_persists_one_minute_without_orders "
                "owns this production inspection point")

        with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
                   side_effect=_all_live_fetchers_down):
            bars = app.provider.get_bars(PAIR, "1h")

        assert bars
        assert app.provider.bars_origin(PAIR, "1h") == "cache(1m→1h derived)"
    finally:
        app.close()


def test_cache_fallback_keeps_trailing_in_progress_bucket(tmp_path):
    """spec ③ テスト 10: 12:00〜12:47 の形成中 1h bucket を落とさない。"""
    app = _build_real_app(tmp_path)
    try:
        boundary = OPEN_NOW.replace(minute=0)
        bars = [
            Bar(PAIR, "1m", boundary + timedelta(minutes=i),
                148.0, 148.1, 147.9, 148.05, 100.0)
            for i in range(48)
        ]
        ohlcv.upsert_cache_bars(app.conn_core, bars, source=LIVE_SOURCE)
        result = app.provider._cached_bars(PAIR, "1h", boundary + timedelta(minutes=47),
                                           [], 0)
        assert result is not None
        derived, origin = result
        assert origin == "cache(1m→1h derived)"
        assert [bar.ts for bar in derived] == [boundary]
    finally:
        app.close()


def test_window_excludes_old_abnormal_bar_and_changes_acceptance(tmp_path):
    """spec ③ テスト 14: 窓外の古い spike は fallback 全体を落とさない。"""
    app = _build_real_app(tmp_path)
    try:
        old = Bar(PAIR, "1h", OPEN_NOW - timedelta(days=10),
                  100.0, 100.0, 100.0, 100.0, 100.0)
        old_spike = Bar(PAIR, "1h", OPEN_NOW - timedelta(days=10) + timedelta(hours=1),
                        150.0, 150.0, 150.0, 150.0, 100.0)
        recent = [
            Bar(PAIR, "1h", OPEN_NOW - timedelta(hours=i),
                148.0, 148.1, 147.9, 148.05, 100.0)
            for i in range(24, -1, -1)
        ]
        ohlcv.upsert_cache_bars(
            app.conn_core, [old, old_spike, *recent], source=LIVE_SOURCE)
        with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
                   side_effect=_all_live_fetchers_down):
            got = app.provider.get_bars(PAIR, "1h", lookback_days=5)
        assert got
        assert all(bar.ts >= OPEN_NOW - timedelta(days=5) for bar in got)
        assert app.provider.bars_origin(PAIR, "1h") == "cache"
    finally:
        app.close()


def test_cache_fallback_ignores_matching_history_rows(tmp_path):
    """改訂 5 テスト 17: 同一キーの history 行を cache loader は読まない。"""
    app = _build_real_app(tmp_path)
    try:
        one_minute = _one_minute_bars()
        ohlcv.upsert_cache_bars(app.conn_core, one_minute, source=LIVE_SOURCE)
        history_ts = one_minute[-1].ts.replace(minute=0)
        # API allowlist ではなく raw SQL で同 source まで一致させる。
        # テーブル名だけを history へ向ける変異がこの行を直読するようにするため。
        app.conn_core.execute(
            "INSERT INTO ohlcv_history "
            "(symbol,interval,bar_time,open,high,low,close,volume,spread,source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (PAIR, "1h", history_ts.isoformat(), 999.0, 999.0, 999.0,
             999.0, 1.0, None, LIVE_SOURCE))
        app.conn_core.commit()
        assert app.conn_core.execute(
            "SELECT COUNT(*) FROM ohlcv_cache WHERE symbol=? AND interval='1h' "
            "AND source=?", (PAIR, LIVE_SOURCE)).fetchone()[0] == 0
        assert app.conn_core.execute(
            "SELECT COUNT(*) FROM ohlcv_history WHERE symbol=? AND interval='1h' "
            "AND source=?", (PAIR, LIVE_SOURCE)).fetchone()[0] == 1

        with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
                   side_effect=_all_live_fetchers_down):
            got = app.provider.get_bars(PAIR, "1h")
        assert got
        assert app.provider.bars_origin(PAIR, "1h") == "cache(1m→1h derived)"
        assert all(bar.close != 999.0 for bar in got)
    finally:
        app.close()
