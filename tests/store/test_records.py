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


def test_reflections_tiebreaker_by_order_id(tmp_path):
    """同一 created_at の複数反射は order_id DESC で決定的順序が保証される。"""
    c = _conn(tmp_path)
    # 同一 created_at の複数 order を作成
    oid1 = orders.insert(c, pair="USDJPY", direction="long", entry_type="market",
                         horizon="day", status=OrderStatus.CLOSED, now=NOW)
    oid2 = orders.insert(c, pair="USDJPY", direction="long", entry_type="market",
                         horizon="day", status=OrderStatus.CLOSED, now=NOW)
    # 同じ created_at で保存 (oid2 が大きい)
    reflections.save(c, oid1, "content1", NOW)
    reflections.save(c, oid2, "content2", NOW)
    # recent() で最新 2 件を取得
    recent_rows = reflections.recent(c, 2)
    assert len(recent_rows) == 2
    # oid2 (新しい order_id) が最初に来ることを確認
    assert recent_rows[0]["order_id"] == oid2
    assert recent_rows[1]["order_id"] == oid1


def test_reflections_recent_for_pair_tiebreaker_by_order_id(tmp_path):
    """recent_for_pair() も同一 created_at の複数反射で order_id DESC で決定的順序。"""
    c = _conn(tmp_path)
    # 同一ペア、同一 created_at の複数 order を作成
    oid1 = orders.insert(c, pair="USDJPY", direction="long", entry_type="market",
                         horizon="day", status=OrderStatus.CLOSED, now=NOW)
    oid2 = orders.insert(c, pair="USDJPY", direction="long", entry_type="market",
                         horizon="day", status=OrderStatus.CLOSED, now=NOW)
    # 別ペアの order (チェック用)
    oid3 = orders.insert(c, pair="EURUSD", direction="short", entry_type="market",
                         horizon="day", status=OrderStatus.CLOSED, now=NOW)
    # 同じ created_at で保存
    reflections.save(c, oid1, "usdjpy_1", NOW)
    reflections.save(c, oid2, "usdjpy_2", NOW)
    reflections.save(c, oid3, "eurusd_1", NOW)
    # recent_for_pair() で USDJPY の最新 2 件を取得
    recent_rows = reflections.recent_for_pair(c, "USDJPY", 2)
    assert len(recent_rows) == 2
    # oid2 (新しい order_id) が最初に来ることを確認
    assert recent_rows[0]["order_id"] == oid2
    assert recent_rows[1]["order_id"] == oid1


def test_snapshots_latest(tmp_path):
    c = _conn(tmp_path)
    snapshots.add(c, ts=NOW, balance=10000, equity=10000, hwm=10000)
    snapshots.add(c, ts=NOW + timedelta(hours=1), balance=10000,
                  equity=10100, hwm=10100)
    assert snapshots.latest(c)["equity"] == 10100


def test_snapshots_add_normalizes_ts_to_utc(tmp_path):
    # add() は account_snapshots への書き込み経路であり、非 UTC の ts が
    # そのまま保存されると first_since/last_before/latest の辞書式比較が壊れる。
    c = _conn(tmp_path)
    jst = timezone(timedelta(hours=9))
    ts_jst = NOW.astimezone(jst)
    snapshots.add(c, ts=ts_jst, balance=10000, equity=10000, hwm=10000)
    row = snapshots.latest(c)
    assert row["ts"] == NOW.isoformat()
    assert row["ts"].endswith("+00:00")


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
    assert ohlcv.upsert_bars(c, bars, source="yfinance") == 3
    ohlcv.upsert_bars(c, bars, source="yfinance")  # 冪等
    loaded = ohlcv.load_bars(c, "USDJPY", "1h", source="yfinance")
    assert len(loaded) == 3
    assert loaded[0].close == 148.2
    assert len(ohlcv.load_bars(c, "USDJPY", "1h", source="yfinance",
                               since=NOW + timedelta(hours=2))) == 1
