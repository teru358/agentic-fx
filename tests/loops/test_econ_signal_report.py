"""人間向けの econ signal 集計 (abandoned・decision・容量・worker) と日次 report file。"""
from __future__ import annotations

import os
import stat
import threading
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agentic_fx.loops import econ_signal_report as esr
from agentic_fx.store import ohlcv_slices, signal_decisions as sd
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.signal_shadow import ShadowSpread

# 2026-10-30 (金) 13:00Z は取引日の途中 (夏時間の NY 17:00 = 21:00Z)
NOW = datetime(2026, 10, 30, 13, 0, tzinfo=timezone.utc)
DAY_START = datetime(2026, 10, 29, 21, 0, tzinfo=timezone.utc)
SPREAD = ShadowSpread(pip_size=0.01, spread_pips=1.2, source="quote")


def _conn(tmp_path):
    conn = connect(tmp_path / "d.db")
    init_db(conn)
    return conn


def _signal(conn, *, status="pending", reason=None, abandoned_at=None,
            kind="strategy", version="executable_v1", plugin="p", bar="12:45"):
    conn.execute(
        "INSERT INTO signals (plugin,content_hash,pair,timeframe,bar_ts,kind,"
        "payload_json,status,created_at,contract_version,abandon_reason,abandoned_at) "
        "VALUES (?,?,'USDJPY','15m',?,?, '{}',?,?,?,?,?)",
        (plugin, "h", f"2026-10-30T{bar}:00+00:00", kind, status, NOW.isoformat(),
         version, reason, abandoned_at))
    conn.commit()


def _ab(at):
    return at.isoformat()


def test_abandoned_is_counted_per_reason_by_abandoned_at(tmp_path):
    conn = _conn(tmp_path)
    inside = NOW - timedelta(hours=1)
    earlier = DAY_START - timedelta(days=3)
    for i, reason in enumerate(esr.ABANDON_REASONS):
        _signal(conn, status="abandoned", reason=reason, abandoned_at=_ab(inside),
                bar=f"0{i}:00")
    _signal(conn, status="abandoned", reason="freshness_expired",
            abandoned_at=_ab(earlier), bar="09:00")
    out = esr.abandoned_status(conn, now=NOW)
    assert out["latest_day"] == {r: 1 for r in esr.ABANDON_REASONS}
    assert out["total"]["freshness_expired"] == 2
    assert out["total"]["deploy_linked"] == 1
    assert out["latest_day_start"] == DAY_START.isoformat()


def test_cleanup_and_legacy_unknown_are_separate_from_reason_counts(tmp_path):
    conn = _conn(tmp_path)
    inside = _ab(NOW - timedelta(hours=1))
    _signal(conn, status="abandoned", reason="ineligible_cleanup",
            abandoned_at=inside, kind="indicator", version=None, bar="10:00")
    conn.execute(
        "INSERT INTO signals (plugin,content_hash,pair,timeframe,bar_ts,kind,"
        "payload_json,status,created_at,abandon_reason,abandoned_at) VALUES "
        "('old','h','USDJPY','15m','2026-09-01T00:00:00+00:00','strategy','{}',"
        "'abandoned',?, 'legacy_unknown', NULL)", (NOW.isoformat(),))
    conn.commit()
    out = esr.abandoned_status(conn, now=NOW)
    assert all(v == 0 for v in out["total"].values())
    assert all(v == 0 for v in out["latest_day"].values())
    assert out["cleanup"] == {"total": 1, "latest_day": 1}
    assert out["legacy_unknown_total"] == 1


def test_decision_denominator_is_decision_rows_only(tmp_path):
    conn = _conn(tmp_path)
    mid = conn.execute(
        "INSERT INTO missions (loop,runner,model,status,started_at,trigger) "
        "VALUES ('trade','local','m','running',?,'signal')",
        (NOW.isoformat(),)).lastrowid
    _signal(conn, status="claimed", bar="11:00")
    sid = conn.execute("SELECT id FROM signals").fetchone()[0]
    conn.execute("UPDATE signals SET claimed_by_mission_id=?, claimed_at=? WHERE id=?",
                 (mid, NOW.isoformat(), sid))
    conn.commit()
    sd.commit_reject(conn, mission_id=mid, signal_id=sid, actor="system",
                     reject_code="econ_window", spread=SPREAD,
                     output={"decision": "reject"}, transcript=[], now=NOW)
    _signal(conn, status="abandoned", reason="source_inactive",
            abandoned_at=_ab(NOW), bar="12:00")
    out = esr.decision_status(conn, since=DAY_START, until=NOW + timedelta(minutes=1))
    assert out == {"total": 1, "by_kind": {"reject:system:econ_window": 1}}


def test_system_filtered_is_a_known_terminal_not_failed_or_running(tmp_path):
    conn = _conn(tmp_path)
    for status in ("system_filtered", "system_filtered", "completed", "failed",
                   "running", "weird"):
        conn.execute(
            "INSERT INTO missions (loop,runner,model,status,started_at) "
            "VALUES ('trade','local','m',?,?)", (status, NOW.isoformat()))
    conn.commit()
    out = esr.mission_status_counts(conn, since=DAY_START,
                                    until=NOW + timedelta(minutes=1))
    assert out["system_filtered"] == 2
    assert (out["failed"], out["running"], out["completed"], out["other"]) == (1, 1, 1, 1)


class _Plan:
    def __init__(self, rows, bytes_, below=False):
        self.rows, self.bytes = rows, bytes_
        self.signals = SimpleNamespace(value=96, producer=96, override=None,
                                       below_producer=below)


@pytest.mark.parametrize("rows,level", [(79, "ok"), (80, "warn"), (99, "warn"),
                                        (100, "full"), (250, "full")])
def test_row_capacity_warns_at_80_and_100_percent(rows, level):
    out = esr.capacity_status(_Plan(100, 10**9), rows=rows, slice_bytes=None,
                              db_path=None)
    assert (out["row_level"], out["level"]) == (level, level)


def test_byte_capacity_uses_base_plus_increment_over_empty_ddl():
    base = ohlcv_slices.SLICE_BASE_BYTES
    plan = _Plan(10**9, base + 1000)
    over = esr.capacity_status(plan, rows=0, slice_bytes=500 + 800, db_path=None,
                               base_bytes=500)
    assert over["used_bytes"] == base + 800
    assert over["byte_level"] == "warn" and over["level"] == "warn"
    full = esr.capacity_status(plan, rows=0, slice_bytes=500 + 1000, db_path=None,
                               base_bytes=500)
    assert full["byte_level"] == "full"
    unrelated = esr.capacity_status(plan, rows=0, slice_bytes=400, db_path=None,
                                    base_bytes=500)
    assert unrelated["used_bytes"] == base


def test_capacity_status_reports_files_free_bytes_and_constants(tmp_path):
    db = tmp_path / "d.db"
    db.write_bytes(b"x" * 10)
    (tmp_path / "d.db-wal").write_bytes(b"y" * 4)
    out = esr.capacity_status(_Plan(100, 10**9, below=True), rows=1, slice_bytes=1,
                              db_path=db)
    assert (out["db_bytes"], out["wal_bytes"]) == (10, 4)
    assert out["free_bytes"] > 0
    assert out["capacity_override_below_producer"] is True
    assert (out["base_bytes"], out["bytes_per_row"]) == (
        ohlcv_slices.SLICE_BASE_BYTES, ohlcv_slices.SLICE_BYTES_PER_ROW)
    assert out["signal_daily"] == 96


def test_content_hash_update_extra_signals_count_toward_the_warning_ratio():
    k, pairs = 2, 3
    extra_rows = k * pairs * ohlcv_slices.SHADOW_ROWS_PER_OWNER_MAX
    capacity = 100 * extra_rows
    base_rows = capacity * 8 // 10 - extra_rows
    before = esr.capacity_status(_Plan(capacity, 10**15), rows=base_rows,
                                 slice_bytes=None, db_path=None)
    after = esr.capacity_status(_Plan(capacity, 10**15),
                                rows=base_rows + extra_rows, slice_bytes=None,
                                db_path=None)
    assert before["row_level"] == "ok"
    assert after["row_level"] == "warn"


_EVENT_SEQ = [0]


def _event_set(conn, state, *, expected, purged=False):
    """event set 1 本 (event を 1 件足して結ぶ)。staging は世代を持つ。"""
    _EVENT_SEQ[0] += 1
    event_id = conn.execute(
        "INSERT INTO econ_events (ts,country,name,importance) VALUES (?,?,?,3)",
        (NOW.isoformat(), "US", f"e{_EVENT_SEQ[0]}")).lastrowid
    generation = None
    if state == "staging":
        generation = conn.execute(
            "INSERT INTO ohlcv_slice_generations (started_at) VALUES (?)",
            (NOW.isoformat(),)).lastrowid
    done = None if state in ("awaiting", "staging") else NOW.isoformat()
    return conn.execute(
        "INSERT INTO ohlcv_slice_sets (event_id,pair,range_start,range_end,"
        "expected_rows,state,generation,created_at,completed_at,purged_at,rule_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,1)",
        (event_id, "USDJPY", "2026-10-30T12:00:00+00:00", "2026-10-30T13:00:00+00:00",
         expected, state, generation, NOW.isoformat(), done,
         NOW.isoformat() if purged else None)).lastrowid


def test_capacity_reader_counts_rows_from_the_set_table(tmp_path):
    conn = _conn(tmp_path)
    _event_set(conn, "complete", expected=7)
    _event_set(conn, "complete", expected=5)
    _event_set(conn, "complete", expected=100, purged=True)
    _event_set(conn, "awaiting", expected=50)
    staging = _event_set(conn, "staging", expected=60)
    for minute in range(3):
        conn.execute(
            "INSERT INTO ohlcv_slices (set_id,bar_time,open,high,low,close,volume,"
            "source,quality) VALUES (?,?,1,1,1,1,0,'s','observed')",
            (staging, f"2026-10-30T12:0{minute}:00+00:00"))
    conn.commit()
    assert esr.stored_slice_rows(conn) == 7 + 5 + 3
    reader = esr.CapacityReader(lambda: _Plan(1000, 10**9), tmp_path / "d.db")
    assert reader.status(conn)["rows"] == 15


def test_complete_set_row_count_equals_its_expected_rows(tmp_path):
    conn = _conn(tmp_path)
    set_id = _event_set(conn, "staging", expected=4)
    for minute in range(4):
        conn.execute(
            "INSERT INTO ohlcv_slices (set_id,bar_time,open,high,low,close,volume,"
            "source,quality) VALUES (?,?,1,1,1,1,0,'s','observed')",
            (set_id, f"2026-10-30T12:0{minute}:00+00:00"))
    conn.commit()
    staged = esr.stored_slice_rows(conn)
    conn.execute("UPDATE ohlcv_slice_sets SET state='complete', generation=NULL, "
                 "completed_at=? WHERE id=?", (NOW.isoformat(), set_id))
    conn.commit()
    assert staged == esr.stored_slice_rows(conn) == 4


def test_capacity_reader_does_not_scan_slice_rows_or_dbstat(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    _event_set(conn, "complete", expected=3)
    statements = []
    conn.set_trace_callback(statements.append)
    calls = []
    monkeypatch.setattr(esr, "slice_related_bytes",
                        lambda c: calls.append(1) or 1)
    reader = esr.CapacityReader(lambda: _Plan(1000, 10**9), tmp_path / "d.db")
    for _ in range(3):
        out = reader.status(conn)
    assert calls == []
    assert not any("dbstat" in text.lower() for text in statements)
    assert not any("COUNT(*) FROM ohlcv_slices" in text and "WHERE" not in text
                   for text in statements)
    assert out["slice_bytes"] is None and out["slice_bytes_measured_at"] is None


def test_capacity_reader_shows_the_workers_measurement_and_its_time(tmp_path):
    conn = _conn(tmp_path)
    held = []
    reader = esr.CapacityReader(lambda: _Plan(1000, 10**9), tmp_path / "d.db",
                                bytes_source=lambda: held[-1] if held else None)
    assert reader.status(conn)["slice_bytes"] is None
    held.append((12_345, NOW))
    out = reader.status(conn)
    assert out["slice_bytes"] == 12_345
    assert out["slice_bytes_measured_at"] == NOW.isoformat()
    assert out["used_bytes"] >= ohlcv_slices.SLICE_BASE_BYTES
    held.append(("junk", NOW))
    assert reader.status(conn)["slice_bytes"] is None


def test_format_econ_lines_says_unmeasured_bytes_and_shows_shadow():
    econ = {"slice_worker": "alive", "decisions": None, "abandoned": None,
            "shadow": [{"decision": "accept", "actor": "system", "reject_code": None,
                        "state": "resolved", "result_code": "tp", "n": 3,
                        "avg_pnl_pips": 1.5}],
            "slice_capacity": {
                "rows": 1, "row_capacity": 10, "row_level": "ok", "slice_bytes": None,
                "slice_bytes_measured_at": None, "used_bytes": None,
                "byte_capacity": 100, "byte_level": None, "signal_daily": 96,
                "signal_producer": 96, "signal_override": None, "base_bytes": 5,
                "bytes_per_row": 6, "db_bytes": 7, "wal_bytes": 8, "free_bytes": 9,
                "level": "ok", "capacity_override_below_producer": False}}
    text = "\n".join(esr.format_econ_lines(econ))
    assert "bytes 未測定" in text
    assert "S=96 (producer=96 override=-)" in text
    assert "shadow: accept/system/-:resolved/tp=3" in text


@pytest.mark.parametrize("setup,expected", [
    ("none", "not_started"), ("alive", "alive"), ("dead", "stopped"),
    ("failed", "start_failed")])
def test_worker_health_states(setup, expected):
    health = esr.WorkerHealth()
    stop = threading.Event()
    if setup in ("alive", "dead"):
        thread = threading.Thread(target=stop.wait, daemon=True)
        thread.start()
        health.attach(thread)
        if setup == "dead":
            stop.set()
            thread.join(2)
    if setup == "failed":
        health.mark_start_failed()
    try:
        assert health.state() == expected
    finally:
        stop.set()


def test_econ_signal_status_isolates_failed_sections(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("DROP TABLE signal_shadow")
    broken = SimpleNamespace(status=lambda c: (_ for _ in ()).throw(RuntimeError("x")))
    out = esr.econ_signal_status(conn, now=NOW, capacity=broken, worker=None)
    assert out["slice_capacity"] is None
    assert out["abandoned"] is not None
    assert out["slice_worker"] == "unknown"


def test_report_file_is_owner_only_unique_per_trading_day_and_atomic(tmp_path):
    reports = tmp_path / "econ_signal"
    label = date(2026, 10, 30)
    first = esr.write_report_once(reports, label=label, body="a\n")
    assert first == reports / "econ-signal-2026-10-30.md"
    assert stat.S_IMODE(os.stat(first).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(reports).st_mode) == 0o700
    assert esr.write_report_once(reports, label=label, body="b\n") is None
    assert first.read_text() == "a\n"
    assert sorted(p.name for p in reports.iterdir()) == ["econ-signal-2026-10-30.md"]


def test_old_and_new_close_boundaries_share_one_label():
    summer_close = datetime(2026, 10, 30, 21, 0, tzinfo=timezone.utc)
    winter_close = datetime(2026, 11, 2, 22, 0, tzinfo=timezone.utc)
    assert esr.report_label(summer_close) == date(2026, 10, 30)
    assert esr.report_label(winter_close) == date(2026, 11, 2)
    assert esr.report_label(datetime(2026, 11, 2, 21, 0, tzinfo=timezone.utc)) == date(
        2026, 11, 2)


def test_daily_callback_writes_once_per_trading_day(tmp_path):
    conn = _conn(tmp_path)
    opened = []

    def open_conn():
        opened.append(1)
        return connect(tmp_path / "d.db")

    cb = esr.make_daily_report_callback(
        reports_dir=tmp_path / "econ_signal", open_conn=open_conn, capacity=None,
        worker=esr.WorkerHealth())
    close_at = datetime(2026, 10, 30, 21, 0, tzinfo=timezone.utc)
    cb(close_at)
    cb(close_at)
    cb(close_at)
    files = list((tmp_path / "econ_signal").iterdir())
    assert [f.name for f in files] == ["econ-signal-2026-10-30.md"]
    assert len(opened) == 1
    body = files[0].read_text()
    assert "## abandoned" in body and "slice worker" in body
    conn.close()


def test_daily_callback_failure_leaves_no_final_file(tmp_path):
    def broken():
        raise RuntimeError("db unavailable")

    cb = esr.make_daily_report_callback(
        reports_dir=tmp_path / "econ_signal", open_conn=broken, capacity=None,
        worker=None)
    with pytest.raises(RuntimeError):
        cb(datetime(2026, 10, 30, 21, 0, tzinfo=timezone.utc))
    assert not list((tmp_path / "econ_signal").glob("econ-signal-*")) if (
        tmp_path / "econ_signal").exists() else True
