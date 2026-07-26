from datetime import datetime, timedelta, timezone

from agentic_fx.core.contracts import Bar, OrderStatus
from agentic_fx.store import econ_events, ohlcv, orders, reflections, snapshots
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def _conn(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    return c


def test_reflection_upsert(tmp_path):
    c = _conn(tmp_path)
    oid = orders.insert(c, pair="USDJPY", direction="long", entry_type="market",
                        horizon="day", status=OrderStatus.CLOSED, now=NOW)
    reflections.save(c, oid, "v1", NOW)
    reflections.save(c, oid, "v2", NOW)
    assert reflections.get(c, oid)["content"] == "v2"
    assert len(reflections.recent(c, 5)) == 1


def test_snapshots_latest(tmp_path):
    c = _conn(tmp_path)
    snapshots.add(c, ts=NOW, balance=10000, equity=10000, hwm=10000)
    snapshots.add(c, ts=NOW + timedelta(hours=1), balance=10000,
                  equity=10100, hwm=10100)
    assert snapshots.latest(c)["equity"] == 10100


def test_econ_upsert_and_upcoming(tmp_path):
    c = _conn(tmp_path)
    ts = NOW + timedelta(hours=3)
    econ_events.upsert(c, ts=ts, country="US", name="CPI", importance=3)
    econ_events.upsert(c, ts=ts, country="US", name="CPI", importance=3,
                       actual="3.1%")  # 上書き
    rows = econ_events.upcoming(c, NOW, hours=24)
    assert len(rows) == 1 and rows[0]["actual"] == "3.1%"
    assert econ_events.upcoming(c, NOW + timedelta(days=2), hours=24) == []


def test_ohlcv_roundtrip(tmp_path):
    c = _conn(tmp_path)
    bars = [Bar("USDJPY", "1h", NOW + timedelta(hours=i),
                148.0, 148.5, 147.9, 148.2, 1000) for i in range(3)]
    assert ohlcv.upsert_bars(c, bars) == 3
    ohlcv.upsert_bars(c, bars)  # 冪等
    loaded = ohlcv.load_bars(c, "USDJPY", "1h")
    assert len(loaded) == 3
    assert loaded[0].close == 148.2
    assert len(ohlcv.load_bars(c, "USDJPY", "1h",
                               since=NOW + timedelta(hours=2))) == 1
