from __future__ import annotations

import importlib.util
import hashlib

import pytest

from agentic_fx.backtest import time_repair
from agentic_fx.store.db import connect, init_db


def test_time_repair_module_is_available():
    assert importlib.util.find_spec("agentic_fx.backtest.time_repair") is not None


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
    monkeypatch.setattr(time_repair, "CACHE_SETS", ())
    conn.commit()
    return conn, path


def test_plan_is_read_only_and_hashes_all_fields(tmp_path, monkeypatch):
    conn, _ = _db(tmp_path, monkeypatch)
    before = conn.serialize()
    plan = time_repair.build_plan(conn, indicator_pending="keep")
    assert conn.serialize() == before
    assert plan["schema"] == "afx.time_repair.plan/1"
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
