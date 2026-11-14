from __future__ import annotations

import importlib.util
import hashlib
from pathlib import Path

import pytest
import re

from agentic_fx.backtest import time_repair
from agentic_fx.store.db import connect, init_db


def test_time_repair_module_is_available():
    assert importlib.util.find_spec("agentic_fx.backtest.time_repair") is not None


def test_weekly_anchors_detect_cash_open_and_continuity(tmp_path):
    from datetime import date, datetime, timedelta, timezone
    conn = connect(tmp_path / "anchors.db")
    init_db(conn)
    monday0 = date(2025, 11, 3)
    eur = []
    us = []
    for week in range(18):
        monday = monday0 + timedelta(days=7 * week)
        sunday_open = datetime.combine(monday - timedelta(days=1), datetime.min.time(), timezone.utc) + timedelta(hours=21)
        for index in range(198):
            stamp = (sunday_open + timedelta(minutes=5 * index)).isoformat()
            eur.append(("EURUSD", "5m", stamp, 1., 1., 1., 1., 1., "mt5", None))
        endpoint = (sunday_open + timedelta(minutes=5 * 198)).isoformat()
        eur.append(("EURUSD", "5m", endpoint, 1., 1., 1., 1., 1., "mt5", None))
        for day in range(3):
            current = datetime.combine(monday + timedelta(days=day), datetime.min.time(), timezone.utc)
            for step in range(7):
                stamp = (current + timedelta(hours=13, minutes=5 * step)).isoformat()
                volume = 3. if step == 6 else 1.
                us.append(("US500", "5m", stamp, 1., 1., 1., 1., volume, "mt5", None))
    conn.executemany("INSERT INTO ohlcv_history VALUES (?,?,?,?,?,?,?,?,?,?)", eur + us)
    conn.commit()
    anchors = time_repair._anchors(conn)
    assert len(anchors) == 18
    assert all(item["passed"] and item["us500_1330_days"] == 3 and
               item["eurusd_opening_window_bars"] == 198 for item in anchors)
    conn.execute("DELETE FROM ohlcv_history WHERE symbol='EURUSD' AND bar_time=?",
                 ((datetime.combine(monday0 - timedelta(days=1), datetime.min.time(), timezone.utc)
                   + timedelta(hours=21, minutes=5)).isoformat(),))
    conn.commit()
    assert time_repair._anchors(conn)[0]["reasons"] == ["eurusd_opening_window_not_contiguous"]


def test_cache_fingerprint_literals_match_design_table():
    from pathlib import Path
    design = Path(__file__).parents[2] / "docs/superpowers/specs/2026-11-02-utc-time-rules-dst-design.md"
    text = design.read_text(encoding="utf-8")
    expected = []
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 5 and re.fullmatch(r"C[123] (?:1m|15m|1h)", cells[0]):
            expected.append(cells)
    assert len(expected) == 6
    for row, item in zip(expected, time_repair.CACHE_SETS):
        assert row[0] == f"{item.name.split('-')[0]} {item.interval}"
        assert row[2] == str(item.count)
        assert row[3].strip("`") == item.key_sha256
        assert row[4].strip("`") == item.value_sha256
        parts = row[1].split(" 〜 ")
        start = parts[0]
        assert item.start.replace("T", " ")[:16] == start
        if len(parts) == 2:
            actual_end = item.end[5:10] + " " + item.end[11:16]
            assert actual_end.endswith(parts[1])
        else:
            assert item.end == item.start


def test_cache_fingerprint_collection_is_not_empty():
    assert len(time_repair.CACHE_SETS) == 6
    assert {item.name for item in time_repair.CACHE_SETS} == {
        "C1-1m", "C1-15m", "C1-1h", "C2-1m", "C2-1h", "C3-15m"}


def test_cache_groups_before_window_and_other_sources_are_ignored(tmp_path):
    conn = connect(tmp_path / "cache.db")
    init_db(conn)
    for interval in ("1m", "15m", "1h"):
        conn.execute("INSERT INTO ohlcv_cache VALUES (?,?,?,?,?,?,?,?,?)",
                     ("USDJPY", interval, "2026-12-31T12:00:00+00:00", 1, 1, 1, 1, 1, "mt5-live"))
    conn.execute("INSERT INTO ohlcv_cache VALUES (?,?,?,?,?,?,?,?,?)",
                 ("USDJPY", "1m", "2026-10-03T02:00:00+00:00", 1, 1, 1, 1, 1, "yfinance"))
    conn.commit()
    assert [item["status"] for item in time_repair._cache_status(conn)] == ["absent_before_cache_window"] * 6


def test_partial_cache_group_is_rejected(tmp_path):
    conn = connect(tmp_path / "cache.db")
    init_db(conn)
    conn.execute("INSERT INTO ohlcv_cache VALUES (?,?,?,?,?,?,?,?,?)",
                 ("USDJPY", "1m", "2026-10-02T21:00:00+00:00", 1, 1, 1, 1, 1, "mt5-live"))
    conn.commit()
    with pytest.raises(time_repair.RepairError) as error:
        time_repair._cache_status(conn)
    assert error.value.exit_code == 3


def test_complete_cache_group_with_changed_value_is_rejected(tmp_path, monkeypatch):
    conn = connect(tmp_path / "cache.db")
    init_db(conn)
    values = ("USDJPY", "1m", "2026-10-02T21:00:00+00:00", 1., 1., 1., 1., 1., "mt5-live")
    conn.execute("INSERT INTO ohlcv_cache VALUES (?,?,?,?,?,?,?,?,?)", values)
    conn.commit()
    row = conn.execute("SELECT * FROM ohlcv_cache").fetchone()
    item = time_repair.CacheSet("C1-1m", "1m", row["bar_time"], row["bar_time"], 1,
                                time_repair._rows_digest([row], values=False),
                                time_repair._rows_digest([row], values=True))
    monkeypatch.setattr(time_repair, "CACHE_SETS", (item,))
    conn.execute("UPDATE ohlcv_cache SET close=2")
    conn.commit()
    with pytest.raises(time_repair.RepairError) as error:
        time_repair._cache_status(conn)
    assert error.value.exit_code == 3


def test_cache_absence_inside_retained_window_is_rejected(tmp_path):
    conn = connect(tmp_path / "cache.db")
    init_db(conn)
    conn.execute("INSERT INTO ohlcv_cache VALUES (?,?,?,?,?,?,?,?,?)",
                 ("USDJPY", "1m", "2026-10-02T12:00:00+00:00", 1, 1, 1, 1, 1, "mt5-live"))
    conn.commit()
    with pytest.raises(time_repair.RepairError) as error:
        time_repair._cache_status(conn)
    assert error.value.exit_code == 3


def test_closed_cache_row_outside_pinned_sets_is_rejected(tmp_path, monkeypatch):
    conn = connect(tmp_path / "cache.db")
    init_db(conn)
    conn.executemany("INSERT INTO ohlcv_cache VALUES (?,?,?,?,?,?,?,?,?)", [
        ("USDJPY", "1m", "2026-10-04T22:00:00+00:00", 1, 1, 1, 1, 1, "mt5-live"),
        ("USDJPY", "1m", "2026-10-03T12:00:00+00:00", 1, 1, 1, 1, 1, "mt5-live")])
    conn.commit()
    monkeypatch.setattr(time_repair, "CACHE_SETS", (time_repair.CacheSet(
        "C1-1m", "1m", "2026-10-02T21:00:00+00:00", "2026-10-03T11:59:00+00:00", 900,
        "0" * 64, "0" * 64),))
    with pytest.raises(time_repair.RepairError) as error:
        time_repair._cache_status(conn)
    assert error.value.exit_code == 3


@pytest.mark.parametrize("mutation", ["close", "delete", "other_interval", "extra_row"])
def test_unrecognized_history_fingerprint_is_read_only(tmp_path, monkeypatch, mutation):
    conn, _ = _db(tmp_path, monkeypatch)
    if mutation == "close":
        conn.execute("UPDATE ohlcv_history SET close=close+1 WHERE symbol='EURUSD'")
    elif mutation == "delete":
        conn.execute("DELETE FROM ohlcv_history WHERE symbol='EURUSD'")
    elif mutation == "other_interval":
        conn.execute("INSERT INTO ohlcv_history VALUES (?,?,?,?,?,?,?,?,?,?)",
                         ("EURUSD", "15m", "2026-01-05T12:05:00+00:00", 1, 1, 1, 1, 1, "mt5", None))
    else:
        conn.execute("INSERT INTO ohlcv_history VALUES (?,?,?,?,?,?,?,?,?,?)",
                         ("AUDUSD", "5m", "2026-01-05T12:05:00+00:00", 1, 1, 1, 1, 1, "mt5", None))
    conn.commit()
    before = conn.serialize()
    with pytest.raises(time_repair.RepairError) as error:
        time_repair.build_plan(conn, indicator_pending="keep")
    assert error.value.exit_code == 3
    assert conn.serialize() == before


def test_closed_cache_row_for_another_symbol_is_rejected(tmp_path, monkeypatch):
    conn = connect(tmp_path / "cache-symbol.db")
    init_db(conn)
    item = time_repair.CACHE_SETS[0]
    conn.execute("INSERT INTO ohlcv_cache VALUES (?,?,?,?,?,?,?,?,?)",
                 ("EURUSD", item.interval, item.start, 1, 2, .5, 1.5, 7, "mt5-live"))
    with pytest.raises(time_repair.RepairError):
        time_repair._cache_status(conn)


def test_ohlcv_comparison_tolerates_prices_and_requires_equal_volume():
    assert time_repair._ohlcv_matches((1, 2, .5, 1.5, 7),
                                      (1 + 1e-10, 2, .5, 1.5, 7))
    assert not time_repair._ohlcv_matches((1, 2, .5, 1.5, 7),
                                          (1, 2, .5, 1.5, 7 + 1e-10))


def test_bridge_rejects_duplicate_remote_times(tmp_path, monkeypatch):
    conn, _ = _db(tmp_path, monkeypatch)
    bar = {"time": "2025-11-02T22:00:00+00:00", "open": 1, "high": 2,
           "low": .5, "close": 1.5, "volume": 1}
    def fetch(route, params):
        return ({"offset_source": "rule", "time_rule_state": "ok", "measured_age_sec": 1}
                if route == "/server-time" else {"bars": [bar, dict(bar)]})
    with pytest.raises(time_repair.RepairError):
        time_repair._bridge_check(conn, "http://fake", fetch)


def test_bridge_rejects_rows_before_window_start(tmp_path, monkeypatch):
    conn, _ = _db(tmp_path, monkeypatch)
    def fetch(route, params):
        if route == "/server-time":
            return {"offset_source": "rule", "time_rule_state": "ok", "measured_age_sec": 1}
        return {"bars": [{"time": "2025-11-02T21:55:00+00:00", "open": 1, "high": 2,
                           "low": .5, "close": 1.5, "volume": 1}]}
    with pytest.raises(time_repair.RepairError, match="outside the requested window"):
        time_repair._bridge_check(conn, "http://fake", fetch)


def test_bridge_accepts_float_tolerance_and_inclusive_right_edge(tmp_path, monkeypatch):
    conn, _ = _db(tmp_path, monkeypatch)
    from datetime import datetime, timedelta
    symbols = ("EURUSD", "USDJPY", "JP225", "US500", "USOIL", "XAUUSD")
    # Four verification windows each carry one representative bar per instrument.
    windows = time_repair._BRIDGE_WINDOWS[:4]
    monkeypatch.setattr(time_repair, "_BRIDGE_WINDOWS", windows)
    remote_by_request = {}
    conn.execute("DELETE FROM ohlcv_history")
    for start, end, shift in windows:
        for i, symbol in enumerate(symbols):
            stamp = (datetime.fromisoformat(start) - timedelta(seconds=shift)).isoformat()
            conn.execute("INSERT INTO ohlcv_history VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (symbol, "5m", stamp, 1, 2, .5, 1.5, 7, "mt5", None))
            remote_stamp = (datetime.fromisoformat(stamp) + timedelta(seconds=shift)).isoformat()
            bars = [{"time": remote_stamp, "open": 1 + 1e-10, "high": 2, "low": .5,
                     "close": 1.5, "volume": 7},
                    {"time": end, "open": 0, "high": 0, "low": 0, "close": 0, "volume": 0}]
            remote_by_request[(symbol, start)] = bars
    conn.commit()
    def fetch(route, params):
        if route == "/server-time":
            return {"offset_source": "rule", "time_rule_state": "ok", "measured_age_sec": 1}
        return {"bars": remote_by_request[(route.rsplit("/", 1)[1], params["from"])]}
    result = time_repair._bridge_check(conn, "http://fake", fetch)
    assert result["count"] == result["matched"] == 24


def _digest(rows, values=False):
    fields = ("symbol", "interval", "bar_time", "source")
    lines = ["|".join(str(row[name]) for name in fields) + (
        "|" + "|".join(repr(float(row[name])) for name in ("open", "high", "low", "close", "volume"))
        if values else "") for row in rows]
    return hashlib.sha256("\n".join(sorted(lines)).encode()).hexdigest()


def _db(tmp_path, monkeypatch):
    path = tmp_path / "agentic.db"
    conn = connect(path)
    init_db(conn)
    conn.executemany("INSERT INTO ohlcv_history VALUES (?,?,?,?,?,?,?,?,?,?)", [
        ("EURUSD", "5m", "2026-01-05T12:00:00+00:00", 1, 2, .5, 1.5, 7, "mt5", None),
        ("USDJPY", "5m", "2026-01-05T12:05:00+00:00", 2, 3, 1.5, 2.5, 8, "mt5", None)])
    rows = conn.execute("SELECT * FROM ohlcv_history ORDER BY symbol, bar_time").fetchall()
    monkeypatch.setattr(time_repair, "HISTORY_FINGERPRINT", time_repair.HistoryFingerprint(
        "2026-01-05T12:00:00+00:00", "2026-01-05T12:05:00+00:00",
        {"EURUSD": 1, "USDJPY": 1}, _digest(rows), _digest(rows, True)))
    monkeypatch.setattr(time_repair, "_cache_status", lambda _: [
        {"name": item.name, "status": "absent_before_cache_window"}
        for item in time_repair.CACHE_SETS])
    monkeypatch.setattr(time_repair, "_cache_changes", lambda _: ([
        {"name": item.name, "status": "absent_before_cache_window", "delete": 0, "replace": 0}
        for item in time_repair.CACHE_SETS], []))
    monkeypatch.setattr(time_repair, "_anchors", lambda _: [{"week": "synthetic", "passed": True}])
    conn.commit()
    return conn, path


def test_plan_is_read_only_and_hashes_all_fields(tmp_path, monkeypatch):
    conn, path = _db(tmp_path, monkeypatch)
    before_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    before = conn.serialize()
    plan = time_repair.build_plan(conn, indicator_pending="keep", db_path=path)
    assert conn.serialize() == before
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_hash
    assert plan["schema"] == "afx.time_repair.plan/1"
    assert {"db", "fingerprint", "cache_sets", "anchors", "excluded_weeks", "bridge_check",
            "targets", "samples", "collisions", "unexplained", "indicator_pending",
            "approval_changes", "estimate_sec", "plan_hash"} <= plan.keys()
    assert {"H1", "C1", "C2", "C3"} <= plan["targets"].keys()
    assert plan["plan_hash"] == time_repair.plan_hash({k: v for k, v in plan.items() if k != "plan_hash"})


def test_apply_requires_confirmation_then_moves_rows_and_is_idempotent(tmp_path, monkeypatch):
    conn, path = _db(tmp_path, monkeypatch)
    plan = time_repair.build_plan(conn, indicator_pending="keep")
    with pytest.raises(time_repair.RepairError) as error:
        time_repair.apply_plan(conn, path, plan, confirm="wrong")
    assert error.value.exit_code == 4
    time_repair.apply_plan(conn, path, plan, confirm=plan["plan_hash"])
    assert [r[0] for r in conn.execute("SELECT bar_time FROM ohlcv_history ORDER BY bar_time")] == [
        "2026-01-05T13:00:00+00:00", "2026-01-05T13:05:00+00:00"]
    assert time_repair.apply_plan(conn, path, plan, confirm=plan["plan_hash"]) is None
    manifest_path = path.with_name(path.name + ".time-repair.manifest.json")
    manifest = __import__("json").loads(manifest_path.read_text())
    assert manifest["committed"] is True
    assert manifest["plan_hash"] == plan["plan_hash"]
    assert hashlib.sha256(Path(manifest["path"]).read_bytes()).hexdigest() == manifest["sha256"]


def test_excluded_week_is_removed_from_h1_counts_and_digests(tmp_path, monkeypatch):
    conn, _ = _db(tmp_path, monkeypatch)
    monkeypatch.setattr(time_repair, "_anchors", lambda _: [{"week": "2026-01-05", "passed": False,
                                                               "reasons": ["synthetic"]}])
    plan = time_repair.build_plan(conn, indicator_pending="keep")
    assert plan["excluded_weeks"] == [{"week": "2026-01-05", "reason": "synthetic"}]
    assert plan["targets"]["H1"]["count"] == 0
    assert plan["targets"]["H1"]["by_symbol"] == {}
    assert plan["targets"]["H1"]["key_sha256"] == time_repair._rows_digest([], values=False)
    time_repair.apply_plan(conn, _db_path(conn), plan, confirm=plan["plan_hash"])


def _db_path(conn):
    return Path(conn.execute("PRAGMA database_list").fetchone()[2])


def test_six_symbol_synthetic_history_repair_checks_database_invariants(tmp_path, monkeypatch):
    from datetime import date, datetime, timedelta, timezone
    from agentic_fx.core.fx_time import session_boundary_utc
    symbols = ("EURUSD", "USDJPY", "JP225", "US500", "USOIL", "XAUUSD")
    conn = connect(tmp_path / "synthetic.db")
    init_db(conn)
    rows = []
    for week in range(18):
        sunday = date(2025, 11, 2) + timedelta(days=7 * week)
        sunday_open = session_boundary_utc(sunday)
        friday_close = session_boundary_utc(sunday + timedelta(days=5)) - timedelta(minutes=5)
        for symbol in symbols:
            symbol_open = sunday_open + (timedelta(hours=1)
                                         if symbol in {"JP225", "US500", "USOIL", "XAUUSD"}
                                         else timedelta())
            for actual in (symbol_open, friday_close):
                label = (actual - timedelta(hours=1)).isoformat()
                high = 10. if symbol == "USDJPY" and actual.date() == date(2026, 1, 9) else 2.
                rows.append((symbol, "5m", label, 1., high, .5, 1.5, 1., "mt5", None))
    for nfp in (datetime(2025, 12, 16, 13, 30, tzinfo=timezone.utc),
                datetime(2026, 1, 9, 13, 30, tzinfo=timezone.utc),
                datetime(2026, 3, 6, 13, 30, tzinfo=timezone.utc)):
        rows.append(("USDJPY", "5m", (nfp - timedelta(hours=1)).isoformat(), 1., 20., .5, 1.5, 1., "mt5", None))
    summer_sunday = date(2026, 7, 5)
    for symbol in symbols:
        summer_open = session_boundary_utc(summer_sunday) + (timedelta(hours=1)
                      if symbol in {"JP225", "US500", "USOIL", "XAUUSD"} else timedelta())
        for actual in (summer_open,
                       session_boundary_utc(summer_sunday + timedelta(days=5)) - timedelta(minutes=5)):
            rows.append((symbol, "5m", actual.isoformat(), 1., 2., .5, 1.5, 1., "mt5", None))
    conn.executemany("INSERT INTO ohlcv_history VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    selected = conn.execute("SELECT * FROM ohlcv_history WHERE bar_time>=? AND bar_time<=?",
        ("2025-11-02T20:00:00+00:00", "2026-03-06T20:55:00+00:00")).fetchall()
    monkeypatch.setattr(time_repair, "HISTORY_FINGERPRINT", time_repair.HistoryFingerprint(
        "2025-11-02T20:00:00+00:00", "2026-03-06T20:55:00+00:00",
        {symbol: sum(row["symbol"] == symbol for row in selected) for symbol in symbols},
        time_repair._rows_digest(selected, values=False), time_repair._rows_digest(selected, values=True)))
    for interval, stamp in (("1m", "2026-11-03T12:00:00+00:00"),
                            ("15m", "2026-11-03T12:00:00+00:00"),
                            ("1h", "2026-11-03T12:00:00+00:00")):
        conn.execute("INSERT INTO ohlcv_cache VALUES (?,?,?,?,?,?,?,?,?)",
                     ("USDJPY", interval, stamp, 1, 2, .5, 1.5, 1, "mt5-live"))
    conn.commit()
    monkeypatch.setattr(time_repair, "_anchors", lambda _: [{"week": "synthetic", "passed": True}])
    plan = time_repair.build_plan(conn, indicator_pending="keep", db_path=tmp_path / "synthetic.db")
    time_repair.apply_plan(conn, tmp_path / "synthetic.db", plan, confirm=plan["plan_hash"], root=tmp_path)
    assert conn.execute("SELECT count(*) FROM ohlcv_history WHERE bar_time='2025-11-02T22:00:00+00:00'").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM ohlcv_history WHERE bar_time='2025-11-02T23:00:00+00:00'").fetchone()[0] == 4
    assert conn.execute("SELECT count(*) FROM ohlcv_history WHERE bar_time='2025-11-07T21:55:00+00:00'").fetchone()[0] == 6
    assert conn.execute("SELECT count(*) FROM ohlcv_history WHERE bar_time='2026-07-05T21:00:00+00:00'").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM ohlcv_history WHERE bar_time='2026-07-05T22:00:00+00:00'").fetchone()[0] == 4
    for day in ("2025-12-16", "2026-01-09", "2026-03-06"):
        assert conn.execute("SELECT max(high-low) FROM ohlcv_history WHERE symbol='USDJPY' AND bar_time=?",
                            (day + "T13:30:00+00:00",)).fetchone()[0] == 19.5
    assert conn.execute("SELECT count(*) FROM ohlcv_history WHERE bar_time='2026-07-10T20:55:00+00:00'").fetchone()[0] == 6
    assert conn.execute("SELECT count(*) FROM ohlcv_cache WHERE source='mt5-live'").fetchone()[0] == 3
    assert conn.execute("SELECT count(*) FROM ohlcv_history WHERE interval='5m' AND bar_time='2026-01-09T13:30:00+00:00'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM data_migrations WHERE name=?", (time_repair.REPAIR_ID,)).fetchone()[0] == 1
    assert time_repair.apply_plan(conn, tmp_path / "synthetic.db", plan,
                                  confirm=plan["plan_hash"], root=tmp_path) is None


def test_apply_leaves_non_mt5_cache_sources_untouched(tmp_path, monkeypatch):
    conn, path = _db(tmp_path, monkeypatch)
    conn.execute("INSERT INTO ohlcv_cache VALUES (?,?,?,?,?,?,?,?,?)",
                 ("USDJPY", "1m", "2026-10-03T02:00:00+00:00", 1, 2, .5, 1.5, 7, "yfinance"))
    conn.commit()
    before = conn.execute("SELECT * FROM ohlcv_cache WHERE source='yfinance'").fetchall()
    plan = time_repair.build_plan(conn, indicator_pending="keep")
    time_repair.apply_plan(conn, path, plan, confirm=plan["plan_hash"])
    after = conn.execute("SELECT * FROM ohlcv_cache WHERE source='yfinance'").fetchall()
    assert [tuple(row) for row in after] == [tuple(row) for row in before]


def test_bridge_check_reads_rule_state_before_and_after_all_windows(tmp_path, monkeypatch):
    conn, path = _db(tmp_path, monkeypatch)
    calls = []
    def fetch(route, params):
        calls.append((route, params))
        if route == "/server-time":
            return {"offset_source": "rule", "time_rule_state": "ok", "measured_age_sec": 1}
        return {"bars": []}
    plan = time_repair.build_plan(conn, indicator_pending="keep", bridge_url="http://fake",
                                  bridge_fetch=fetch, db_path=path)
    assert plan["bridge_check"]["count"] == 24
    assert len(plan["bridge_check"]["windows"]) == 24
    assert plan["bridge_check"]["matched"] == 0
    assert calls[0][0] == calls[-1][0] == "/server-time"
    assert sum(route != "/server-time" for route, _ in calls) == 24


def test_bridge_http_uses_mt5_auth_headers_and_hides_request_details(monkeypatch):
    import httpx
    from agentic_fx.datafeed import sources
    captured = []
    monkeypatch.setattr(sources, "_mt5_headers", lambda: {"X-API-Key": "secret"})
    def forbidden(*args, **kwargs):
        captured.append(kwargs.get("headers"))
        raise httpx.ConnectError("secret-key leaked")
    monkeypatch.setattr(httpx, "get", forbidden)
    with pytest.raises(time_repair.RepairError) as error:
        time_repair._bridge_check(__import__("sqlite3").connect(":memory:"), "http://fake")
    assert error.value.exit_code == 1
    assert "secret-key" not in str(error.value)
    assert str(error.value) == "bridge server-time unavailable"
    assert captured == [{"X-API-Key": "secret"}]


def test_plan_stops_when_bridge_rule_check_fails(tmp_path, monkeypatch):
    conn, path = _db(tmp_path, monkeypatch)
    def fetch(route, params):
        return {"offset_source": "measured", "time_rule_state": "ok", "measured_age_sec": 1}
    with pytest.raises(time_repair.RepairError) as error:
        time_repair.build_plan(conn, indicator_pending="keep", bridge_url="http://fake",
                               bridge_fetch=fetch, db_path=path)
    assert error.value.exit_code == 1


@pytest.mark.parametrize("state", ["unverified", "mismatch"])
def test_bridge_check_rejects_unverified_states(tmp_path, monkeypatch, state):
    conn, _ = _db(tmp_path, monkeypatch)
    def fetch(route, params):
        return {"offset_source": "rule", "time_rule_state": state, "measured_age_sec": 1}
    with pytest.raises(time_repair.RepairError) as error:
        time_repair._bridge_check(conn, "http://fake", fetch)
    assert error.value.exit_code == 1


def test_bridge_check_rechecks_age_after_fetches(tmp_path, monkeypatch):
    conn, _ = _db(tmp_path, monkeypatch)
    seen = 0
    def fetch(route, params):
        nonlocal seen
        if route == "/server-time":
            seen += 1
            return {"offset_source": "rule", "time_rule_state": "ok",
                    "measured_age_sec": 301 if seen == 2 else 1}
        return {"bars": []}
    with pytest.raises(time_repair.RepairError) as error:
        time_repair._bridge_check(conn, "http://fake", fetch)
    assert error.value.exit_code == 1


def test_bridge_window_with_unmatched_rows_is_rejected(tmp_path, monkeypatch):
    conn, _ = _db(tmp_path, monkeypatch)
    def fetch(route, params):
        if route == "/server-time":
            return {"offset_source": "rule", "time_rule_state": "ok", "measured_age_sec": 1}
        return {"bars": [{"time": "2025-11-02T22:00:00+00:00", "open": 1, "high": 1,
                           "low": 1, "close": 1, "volume": 1}]}
    with pytest.raises(time_repair.RepairError) as error:
        time_repair._bridge_check(conn, "http://fake", fetch)
    assert error.value.exit_code == 3


@pytest.mark.parametrize("point", ["after_backup", "after_H1", "after_C1", "after_C2",
                                    "after_C3", "after_approvals", "after_marker", "before_commit",
                                    "after_supersede"])
def test_precommit_faults_leave_database_unchanged(tmp_path, monkeypatch, point):
    conn, path = _db(tmp_path, monkeypatch)
    plan = time_repair.build_plan(conn, indicator_pending="keep")
    before = conn.serialize()
    def fail(at):
        if at == point:
            raise RuntimeError("injected")
    with pytest.raises(RuntimeError, match="injected"):
        time_repair.apply_plan(conn, path, plan, confirm=plan["plan_hash"], hook=fail)
    assert conn.serialize() == before
    if point == "after_backup":
        manifest = __import__("json").loads(path.with_name(path.name + ".time-repair.manifest.json").read_text())
        assert manifest["committed"] is False
        assert Path(manifest["path"]).is_file()


@pytest.mark.parametrize("point", ["after_recalculate", "after_backup", "before_begin"])
def test_external_commit_after_data_version_snapshot_is_rejected(tmp_path, monkeypatch, point):
    conn, path = _db(tmp_path, monkeypatch)
    plan = time_repair.build_plan(conn, indicator_pending="keep")
    changed = False
    def commit_elsewhere(at):
        nonlocal changed
        if at == point and not changed:
            other = connect(path)
            other.execute("INSERT INTO data_migrations(name, applied_at) VALUES ('unrelated_commit', '2026-01-01T00:00:00+00:00')")
            other.commit()
            other.close()
            changed = True
    with pytest.raises(time_repair.RepairError) as error:
        time_repair.apply_plan(conn, path, plan, confirm=plan["plan_hash"], hook=commit_elsewhere)
    assert error.value.exit_code == 4
    assert conn.execute("SELECT bar_time FROM ohlcv_history WHERE symbol='EURUSD'").fetchone()[0].startswith("2026-01-05T12:")


def test_transaction_digest_change_is_rolled_back(tmp_path, monkeypatch):
    conn, path = _db(tmp_path, monkeypatch)
    plan = time_repair.build_plan(conn, indicator_pending="keep")
    before = conn.serialize()
    def change_inside(at):
        if at == "before_transaction_digest":
            conn.execute("UPDATE ohlcv_history SET close=close+0.1 WHERE symbol='EURUSD'")
    with pytest.raises(time_repair.RepairError) as error:
        time_repair.apply_plan(conn, path, plan, confirm=plan["plan_hash"], hook=change_inside)
    assert error.value.exit_code == 4
    assert conn.serialize() == before


def test_plan_change_after_dry_run_is_rejected(tmp_path, monkeypatch):
    conn, path = _db(tmp_path, monkeypatch)
    plan = time_repair.build_plan(conn, indicator_pending="keep")
    conn.execute("UPDATE ohlcv_history SET close=close+0.1 WHERE symbol='EURUSD'")
    conn.commit()
    with pytest.raises(time_repair.RepairError) as error:
        time_repair.apply_plan(conn, path, plan, confirm=plan["plan_hash"])
    assert error.value.exit_code == 4


def test_pending_approval_added_after_dry_run_is_rejected(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from agentic_fx.store import approvals
    conn, path = _db(tmp_path, monkeypatch)
    plan = time_repair.build_plan(conn, indicator_pending="keep")
    approvals.create(conn, "plugin", {"kind": "strategy", "eval_source": "mt5"}, datetime.now(timezone.utc))
    with pytest.raises(time_repair.RepairError) as error:
        time_repair.apply_plan(conn, path, plan, confirm=plan["plan_hash"])
    assert error.value.exit_code == 4


def test_instance_lock_and_backup_failures_have_exit_one(tmp_path, monkeypatch):
    from agentic_fx.store.instance_lock import acquire_instance_lock
    conn, path = _db(tmp_path, monkeypatch)
    plan = time_repair.build_plan(conn, indicator_pending="keep")
    held = acquire_instance_lock(path.parent)
    try:
        with pytest.raises(time_repair.RepairError) as error:
            time_repair.apply_plan(conn, path, plan, confirm=plan["plan_hash"])
        assert error.value.exit_code == 1
    finally:
        held.close()
    monkeypatch.setattr(time_repair, "_backup_database", lambda *args: (_ for _ in ()).throw(OSError("backup")))
    with pytest.raises(time_repair.RepairError) as error:
        time_repair.apply_plan(conn, path, plan, confirm=plan["plan_hash"])
    assert error.value.exit_code == 1


def test_postcommit_fault_and_corrupt_marker_fail_closed(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from agentic_fx.store import approvals
    conn, path = _db(tmp_path, monkeypatch)
    approval_id = approvals.create(conn, "plugin", {"kind": "strategy", "eval_source": "mt5"},
                                   datetime.now(timezone.utc))
    plan = time_repair.build_plan(conn, indicator_pending="keep")
    def fail(at):
        if at == "after_commit":
            raise RuntimeError("committed fault")
    with pytest.raises(time_repair.RepairError) as error:
        time_repair.apply_plan(conn, path, plan, confirm=plan["plan_hash"], hook=fail)
    assert error.value.exit_code == 5
    assert conn.execute("SELECT 1 FROM data_migrations WHERE name=?", (time_repair.REPAIR_ID,)).fetchone()
    assert conn.execute("SELECT bar_time FROM ohlcv_history WHERE symbol='EURUSD'").fetchone()[0] == "2026-01-05T13:00:00+00:00"
    assert conn.execute("SELECT status FROM approval_requests WHERE id=?", (approval_id,)).fetchone()[0] == "invalidated"


def test_postcommit_fault_restores_manifest_backup_and_original_fingerprint(tmp_path, monkeypatch):
    import json
    import shutil
    conn, path = _db(tmp_path, monkeypatch)
    original_rows = [tuple(row) for row in conn.execute(
        "SELECT * FROM ohlcv_history ORDER BY symbol, interval, bar_time")]
    plan = time_repair.build_plan(conn, indicator_pending="keep")
    def fail(at):
        if at == "after_commit":
            raise RuntimeError("committed fault")
    with pytest.raises(time_repair.RepairError) as error:
        time_repair.apply_plan(conn, path, plan, confirm=plan["plan_hash"], hook=fail)
    assert error.value.exit_code == 5
    manifest = json.loads(path.with_name(path.name + ".time-repair.manifest.json").read_text())
    assert manifest["committed"] is True
    assert hashlib.sha256(Path(manifest["path"]).read_bytes()).hexdigest() == manifest["sha256"]
    conn.close()
    shutil.copyfile(manifest["path"], path)
    restored = connect(path)
    assert [tuple(row) for row in restored.execute(
        "SELECT * FROM ohlcv_history ORDER BY symbol, interval, bar_time")] == original_rows
    assert not restored.execute("SELECT 1 FROM data_migrations WHERE name=?",
                                (time_repair.REPAIR_ID,)).fetchone()


def test_pending_strategy_invalidation_updates_backlog_and_archive_rows(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from agentic_fx.store import approvals, backlog, candidate_archives
    conn, path = _db(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    backlog_id = backlog.add(conn, "repair dependent candidate", "agent", now)
    archive_id = candidate_archives.insert(conn, mission_id=91, name="candidate",
        content_hash="content", artifact_hash="artifact", archive_path="plugins/_archive/91/candidate",
        pair="EURUSD", metrics={}, now=now)
    candidate_archives.settle(conn, archive_id, "pending", expected_from="not_submitted")
    payload = {"kind": "strategy", "eval_source": "mt5", "base_interval": "5m",
               "backlog_id": backlog_id, "mission_id": 91, "name": "candidate",
               "content_hash": "content", "artifact_hash": "artifact"}
    approval_id = approvals.create(conn, "plugin", payload, now)
    plan = time_repair.build_plan(conn, indicator_pending="keep")
    assert [item["id"] for item in plan["approval_changes"]] == [approval_id]
    assert plan["approval_changes"][0]["payload_kind"] == "strategy"
    assert plan["approval_changes"][0]["status"] == "invalidated"
    time_repair.apply_plan(conn, path, plan, confirm=plan["plan_hash"])
    assert conn.execute("SELECT status FROM improvement_backlog WHERE id=?", (backlog_id,)).fetchone()[0] == "observation"
    assert candidate_archives.get(conn, archive_id)["outcome_class"] == "invalidated"


def test_deploy_operations_wait_for_repair_guard(tmp_path, monkeypatch):
    import threading
    import time
    from contextlib import contextmanager
    from datetime import datetime, timezone
    from agentic_fx.plugin import switch
    from agentic_fx.store import approvals

    conn, path = _db(tmp_path, monkeypatch)
    plan = time_repair.build_plan(conn, indicator_pending="keep")
    plugins = tmp_path / "plugins"
    human = plugins / "_human" / "demo"
    human.mkdir(parents=True)
    (human / "plugin.py").write_text("PLUGIN = True\n")
    (human / "config.yaml").write_text("name: demo\nkind: indicator\n")
    now = datetime.now(timezone.utc)
    approval_id = approvals.create(conn, "plugin", {"kind": "indicator", "name": "demo",
        "candidate_origin": "human", "candidate_path": "plugins/_human/demo"}, now)

    class ReachedGuard(Exception):
        pass

    original = switch._plugin_locks
    @contextmanager
    def entered_locks(*args, **kwargs):
        with original(*args, **kwargs):
            raise ReachedGuard()
            yield
    monkeypatch.setattr(switch, "_plugin_locks", entered_locks)

    repair_has_guard = threading.Event()
    release_repair = threading.Event()
    outcomes = {}
    def run_repair():
        repair_conn = connect(path)
        def pause(point):
            if point == "after_recalculate":
                repair_has_guard.set()
                if not release_repair.wait(5):
                    raise TimeoutError("repair guard release timed out")
        try:
            time_repair.apply_plan(repair_conn, path, plan, confirm=plan["plan_hash"],
                                   root=tmp_path, hook=pause)
            outcomes["repair"] = "completed"
        except BaseException as exc:
            outcomes["repair"] = exc
        finally:
            repair_conn.close()
    repair_thread = threading.Thread(target=run_repair)
    repair_thread.start()
    assert repair_has_guard.wait(3)

    def run_approve():
        api_conn = connect(path)
        try:
            switch.approve_candidate(api_conn, approval_id, decided_by="test", now=now,
                plugins_root=plugins, settings=None, deadline=time.monotonic() + 5)
        except BaseException as exc:
            outcomes["approve"] = exc
        finally:
            api_conn.close()
    def run_bless():
        api_conn = connect(path)
        try:
            switch.bless_candidate(api_conn, name="demo", human_dir=human,
                                   settings=None, now=now, decided_by="test")
        except BaseException as exc:
            outcomes["bless"] = exc
        finally:
            api_conn.close()
    operations = [threading.Thread(target=run_approve), threading.Thread(target=run_bless)]
    for operation in operations:
        operation.start()
    time.sleep(.15)
    assert all(operation.is_alive() for operation in operations)
    release_repair.set()
    repair_thread.join(5)
    for operation in operations:
        operation.join(5)
    assert not repair_thread.is_alive()
    assert all(not operation.is_alive() for operation in operations)
    assert isinstance(outcomes["approve"], ReachedGuard)
    assert isinstance(outcomes["bless"], ReachedGuard)
    assert outcomes["repair"] == "completed"


@pytest.mark.parametrize("indicator_mode", ["keep", "invalidate"])
def test_pending_strategy_is_invalidated_in_the_repair_transaction(tmp_path, monkeypatch, indicator_mode):
    from datetime import datetime, timezone
    from agentic_fx.store import approvals
    conn, path = _db(tmp_path, monkeypatch)
    strategy_id = approvals.create(conn, "plugin", {"kind": "strategy", "eval_source": "mt5",
        "base_interval": "5m", "name": "strat"}, datetime.now(timezone.utc))
    indicator_id = approvals.create(conn, "plugin", {"kind": "indicator", "eval_source": None,
        "name": "ind"}, datetime.now(timezone.utc))
    plan = time_repair.build_plan(conn, indicator_pending=indicator_mode)
    assert [item["id"] for item in plan["approval_changes"]] == (
        [strategy_id] if indicator_mode == "keep" else [strategy_id, indicator_id])
    time_repair.apply_plan(conn, path, plan, confirm=plan["plan_hash"])
    strategy = conn.execute("SELECT status, reason, decided_by FROM approval_requests WHERE id=?",
                            (strategy_id,)).fetchone()
    indicator = conn.execute("SELECT status FROM approval_requests WHERE id=?", (indicator_id,)).fetchone()
    assert tuple(strategy) == ("invalidated", "history_time_repaired", "system:time_repair")
    assert indicator[0] == ("pending" if indicator_mode == "keep" else "invalidated")


def test_strategy_approval_from_old_history_epoch_stays_pending(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from agentic_fx.store import approvals
    from agentic_fx.plugin import switch
    conn, path = _db(tmp_path, monkeypatch)
    plan = time_repair.build_plan(conn, indicator_pending="keep")
    time_repair.apply_plan(conn, path, plan, confirm=plan["plan_hash"])
    approval_id = approvals.create(conn, "plugin", {"kind": "strategy", "eval_source": "mt5",
        "name": "strat", "candidate_origin": "human", "candidate_path": "plugins/_human/strat"},
        datetime.now(timezone.utc))
    outcome = switch.approve_candidate(conn, approval_id, decided_by="test", now=datetime.now(timezone.utc),
                                       plugins_root=tmp_path / "plugins", settings=None)
    assert outcome.outcome == "history_epoch_changed"
    assert conn.execute("SELECT status FROM approval_requests WHERE id=?", (approval_id,)).fetchone()[0] == "pending"
    conn.execute("UPDATE ohlcv_history SET close=close+0.1 WHERE symbol='EURUSD'")
    conn.commit()
    with pytest.raises(time_repair.RepairError) as error:
        time_repair.apply_plan(conn, path, plan, confirm=plan["plan_hash"])
    assert error.value.exit_code == 5


def _conflict(conn, symbol, bar_time, *, source="mt5", interval="5m", state="open", digest="a"):
    conn.execute(
        "INSERT INTO history_conflicts (source, interval, symbol, bar_time, history_epoch, "
        "existing_json, incoming_json, existing_digest, incoming_digest, origin, last_origin, "
        "state, first_seen_at, last_seen_at) VALUES (?,?,?,?,'none','{}','{}',?,?, 'import','import',"
        "?,'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')",
        (source, interval, symbol, bar_time, digest, digest, state))


def _conflict_states(conn):
    return {(r["symbol"], r["bar_time"], r["incoming_digest"]): r["state"]
            for r in conn.execute("SELECT * FROM history_conflicts")}


def _seed_conflicts(conn):
    _conflict(conn, "EURUSD", "2026-01-05T12:00:00+00:00")                  # 移動元
    _conflict(conn, "USDJPY", "2026-01-05T13:05:00+00:00")                  # 移動先
    _conflict(conn, "EURUSD", "2026-01-05T12:00:00+00:00", state="kept", digest="k")
    _conflict(conn, "GBPUSD", "2026-01-05T12:00:00+00:00")                  # 別銘柄
    _conflict(conn, "EURUSD", "2026-01-05T12:00:00+00:00", source="dukascopy", digest="d")
    _conflict(conn, "EURUSD", "2026-02-01T12:00:00+00:00")                  # 無関係な時刻
    conn.commit()


def test_repair_supersedes_only_open_conflicts_on_moved_keys(tmp_path, monkeypatch):
    conn, path = _db(tmp_path, monkeypatch)
    _seed_conflicts(conn)
    plan = time_repair.build_plan(conn, indicator_pending="keep")
    time_repair.apply_plan(conn, path, plan, confirm=plan["plan_hash"])
    states = _conflict_states(conn)
    assert states[("EURUSD", "2026-01-05T12:00:00+00:00", "a")] == "superseded"
    assert states[("USDJPY", "2026-01-05T13:05:00+00:00", "a")] == "superseded"
    assert states[("EURUSD", "2026-01-05T12:00:00+00:00", "k")] == "kept"
    assert states[("GBPUSD", "2026-01-05T12:00:00+00:00", "a")] == "open"
    assert states[("EURUSD", "2026-01-05T12:00:00+00:00", "d")] == "open"
    assert states[("EURUSD", "2026-02-01T12:00:00+00:00", "a")] == "open"
    row = conn.execute("SELECT * FROM history_conflicts WHERE state='superseded' LIMIT 1").fetchone()
    assert row["resolution_action"] == "supersede" and row["resolved_by"] == "system:time_repair"
    assert row["bar_time"] in ("2026-01-05T12:00:00+00:00", "2026-01-05T13:05:00+00:00")
    assert row["history_epoch"] == "none"


@pytest.mark.parametrize("point", ["after_supersede", "after_marker", "before_commit"])
def test_supersede_rolls_back_with_the_repair(tmp_path, monkeypatch, point):
    conn, path = _db(tmp_path, monkeypatch)
    _seed_conflicts(conn)
    plan = time_repair.build_plan(conn, indicator_pending="keep")
    before = conn.serialize()
    def fail(at):
        if at == point:
            raise RuntimeError("injected")
    with pytest.raises(RuntimeError, match="injected"):
        time_repair.apply_plan(conn, path, plan, confirm=plan["plan_hash"], hook=fail)
    assert conn.serialize() == before
    assert set(_conflict_states(conn).values()) == {"open", "kept"}


def test_supersede_failure_rolls_back_history_move(tmp_path, monkeypatch):
    conn, path = _db(tmp_path, monkeypatch)
    _seed_conflicts(conn)
    plan = time_repair.build_plan(conn, indicator_pending="keep")
    before = conn.serialize()
    def boom(*args, **kwargs):
        raise sqlite3_error()
    import sqlite3
    sqlite3_error = lambda: sqlite3.OperationalError("audit write failed")
    monkeypatch.setattr(time_repair, "_supersede_open_conflicts", boom)
    with pytest.raises(sqlite3.OperationalError):
        time_repair.apply_plan(conn, path, plan, confirm=plan["plan_hash"])
    assert conn.serialize() == before


def test_noop_rerun_changes_no_conflict_records(tmp_path, monkeypatch):
    conn, path = _db(tmp_path, monkeypatch)
    _seed_conflicts(conn)
    plan = time_repair.build_plan(conn, indicator_pending="keep")
    time_repair.apply_plan(conn, path, plan, confirm=plan["plan_hash"])
    _conflict(conn, "EURUSD", "2026-01-05T13:00:00+00:00", digest="late")
    conn.commit()
    after_first = conn.serialize()
    assert time_repair.apply_plan(conn, path, plan, confirm=plan["plan_hash"]) is None
    assert conn.serialize() == after_first
    assert _conflict_states(conn)[("EURUSD", "2026-01-05T13:00:00+00:00", "late")] == "open"


def test_repair_yields_when_another_history_writer_holds_the_lease(tmp_path, monkeypatch):
    from agentic_fx.store.history_lease import history_mutation_lease
    conn, path = _db(tmp_path, monkeypatch)
    _seed_conflicts(conn)
    plan = time_repair.build_plan(conn, indicator_pending="keep")
    before = conn.serialize()
    other = connect(path)
    try:
        with history_mutation_lease(other):
            with pytest.raises(time_repair.RepairError) as error:
                time_repair.apply_plan(conn, path, plan, confirm=plan["plan_hash"])
    finally:
        other.close()
    assert error.value.exit_code == 1
    assert conn.serialize() == before
    assert not list(path.parent.glob("*.bak-time-repair-*"))


def test_lease_is_held_through_commit_and_released_after(tmp_path, monkeypatch):
    from agentic_fx.store.history_lease import HistoryLeaseBusy, history_mutation_lease
    conn, path = _db(tmp_path, monkeypatch)
    plan = time_repair.build_plan(conn, indicator_pending="keep")
    seen = {}
    def probe(at):
        if at in ("after_backup", "before_commit"):
            other = connect(path)
            try:
                with history_mutation_lease(other):
                    seen[at] = "free"
            except HistoryLeaseBusy:
                seen[at] = "held"
            finally:
                other.close()
    time_repair.apply_plan(conn, path, plan, confirm=plan["plan_hash"], hook=probe)
    assert seen == {"after_backup": "held", "before_commit": "held"}
    with history_mutation_lease(conn):
        pass


def test_writer_admission_is_taken_after_the_lease_and_released(tmp_path, monkeypatch):
    from agentic_fx.store.history_lease import HistoryLeaseBusy, history_mutation_lease
    conn, path = _db(tmp_path, monkeypatch)
    plan = time_repair.build_plan(conn, indicator_pending="keep")
    events = []
    class Admission:
        def admit(self, begin):
            other = connect(path)
            try:
                with history_mutation_lease(other):
                    events.append("lease-free-at-admit")
            except HistoryLeaseBusy:
                events.append("lease-held-at-admit")
            finally:
                other.close()
            begin()
            return True
        def end(self):
            events.append("end")
    time_repair.apply_plan(conn, path, plan, confirm=plan["plan_hash"], admission=Admission())
    assert events == ["lease-held-at-admit", "end"]
    assert not conn.in_transaction


def test_writer_admission_refusal_fails_with_rc1_and_changes_nothing(tmp_path, monkeypatch):
    from agentic_fx.core.writer_admission import WriterAdmission
    conn, path = _db(tmp_path, monkeypatch)
    _seed_conflicts(conn)
    plan = time_repair.build_plan(conn, indicator_pending="keep")
    before = conn.serialize()
    admission = WriterAdmission()
    with admission.scheduler_write():
        with pytest.raises(time_repair.RepairError) as error:
            time_repair.apply_plan(conn, path, plan, confirm=plan["plan_hash"], admission=admission)
    assert error.value.exit_code == 1
    assert conn.serialize() == before
    time_repair.apply_plan(conn, path, plan, confirm=plan["plan_hash"], admission=admission)
