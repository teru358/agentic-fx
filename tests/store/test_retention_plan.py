from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.store import ohlcv
from agentic_fx.store.db import connect, init_db
from agentic_fx.datafeed.requirements import RequirementRegistry


def test_retention_plan_prunes_only_its_source_interval_and_not_history(tmp_path):
    conn = connect(tmp_path / "retention.db")
    init_db(conn)
    now = datetime(2026, 7, 10, tzinfo=timezone.utc)
    for source, interval in (("yfinance", "1m"), ("yfinance", "1h"),
                             ("twelvedata", "1m")):
        conn.execute("INSERT INTO ohlcv_cache VALUES (?,?,?,?,?,?,?,?,?)",
                     ("USDJPY", interval, (now - timedelta(days=5)).isoformat(),
                      1, 2, .5, 1.5, 1, source))
    conn.execute("INSERT INTO ohlcv_history VALUES (?,?,?,?,?,?,?,?,?,?)",
                 ("USDJPY", "1m", now.isoformat(), 1, 2, .5, 1.5, 1,
                  "dukascopy", None))
    plan = ohlcv.RetentionPlan({("yfinance", "1m"): now - timedelta(days=2)})
    assert ohlcv.prune_cache(conn, plan=plan) == 1
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_cache").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_history").fetchone()[0] == 1


def test_retention_plan_legacy_and_new_are_exclusive():
    with pytest.raises(ValueError, match="cache_retention_days"):
        ohlcv.build_retention_plan({"yfinance": {"1m": "2d"}},
                                  legacy_days=30, required_intervals={"1m"})
    plan = ohlcv.build_retention_plan(None, legacy_days=30,
                                     required_intervals={"1m", "1h"})
    assert set(plan.cutoffs) == {("yfinance", "1m"), ("yfinance", "1h"),
                                 ("twelvedata", "1m"), ("twelvedata", "1h"),
                                 ("mt5-live", "1m"), ("mt5-live", "1h")}


def test_new_retention_is_usable_without_a_legacy_default():
    plan = ohlcv.build_retention_plan({"yfinance": {"1m": "2d"}},
                                      legacy_days=None, required_intervals={"1m"})
    assert set(plan.cutoffs) == {("yfinance", "1m")}


def test_new_retention_normalizes_mt5_to_its_live_storage_name():
    plan = ohlcv.build_retention_plan({"mt5": {"1m": "2d"}},
                                      legacy_days=None, required_intervals={"1m"})
    assert set(plan.cutoffs) == {("mt5-live", "1m")}


def test_retention_plan_from_registry_keeps_1m_short_and_other_windows():
    registry = RequirementRegistry()
    registry.register("paper", ["USDJPY"], "1m", 1, hard=True, reason="paper")
    registry.register("strategy", ["USDJPY"], "1h", 400, hard=False, reason="strategy")
    now = datetime(2026, 7, 10, tzinfo=timezone.utc)
    plan = ohlcv.RetentionPlan.from_registry(registry, sources={"yfinance"}, now=now)
    assert plan.cutoffs[("yfinance", "1m")] == now - timedelta(days=2)
    assert plan.cutoffs[("yfinance", "1h")] == now - timedelta(days=30)
