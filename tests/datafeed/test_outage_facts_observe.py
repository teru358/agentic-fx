"""observe が、状態の保存と同じ transaction で停止の facts を書く。状態の遷移は変えない。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.core import market_hours
from agentic_fx.core.contracts import Bar
from agentic_fx.datafeed.outage import IngestTickReport, OutageStateMachine
from agentic_fx.store import outage_facts
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.ohlcv import upsert_cache_bars

PAIR = "USDJPY"
SRC = "mt5-live"
HARD_KEYS = frozenset({(PAIR, "1m"), (PAIR, "1h")})
WIDTHS = {"1m": timedelta(minutes=1), "1h": timedelta(minutes=60)}
GRACE = timedelta(seconds=30)
K1M, K1H = (PAIR, "1m"), (PAIR, "1h")


def _machine(conn):
    return OutageStateMachine(conn, hard_keys=HARD_KEYS, interval_widths=WIDTHS,
                              grace=GRACE, storage_source=SRC)


def _report(*, failed=(), succeeded=(), empty=()):
    return IngestTickReport(
        attempted=frozenset(succeeded) | frozenset(k for k, _ in failed),
        succeeded=frozenset(succeeded), failed=frozenset(failed),
        deferred=frozenset(), empty=frozenset(empty))


def _seed(conn, interval, ts):
    upsert_cache_bars(conn, [Bar(PAIR, interval, ts, 1, 1, 1, 1, 1)], source=SRC)


def _db(tmp_path):
    conn = connect(tmp_path / "o.db")
    init_db(conn)
    return conn


def _at(hour, minute, second=0):
    return datetime(2026, 9, 24, hour, minute, second, tzinfo=timezone.utc)


def test_a_healthy_tick_records_the_observed_watermark_and_health(tmp_path):
    conn = _db(tmp_path)
    _seed(conn, "1m", _at(9, 59))
    _seed(conn, "1h", _at(9, 0))
    m = _machine(conn)
    assert m.observe(_at(10, 1), _report(succeeded=[K1M, K1H])) == "ready"
    facts = outage_facts.load_key_facts(conn, SRC)
    assert facts[K1M] == outage_facts.KeyFacts(_at(9, 59), None, True, _at(10, 1))
    assert facts[K1H] == outage_facts.KeyFacts(_at(9, 0), None, True, _at(10, 1))
    assert outage_facts.load_health(conn) == outage_facts.Health(
        True, _at(10, 1), True, _at(10, 1))


def test_a_stalled_key_records_its_stall_start_and_unknown_results_stay_null(tmp_path):
    conn = _db(tmp_path)
    watermark = _at(9, 40)
    _seed(conn, "1m", watermark)
    _seed(conn, "1h", _at(9, 0))
    m = _machine(conn)
    now = _at(10, 1)
    m.observe(now, _report(succeeded=[K1H]))
    facts = outage_facts.load_key_facts(conn, SRC)
    assert facts[K1M].observed_through == watermark
    assert facts[K1M].stall_start == market_hours.next_bar_confirmation(
        watermark, WIDTHS["1m"], GRACE)
    assert facts[K1M].last_attempt_ok is None
    assert facts[K1H].stall_start is None and facts[K1H].last_attempt_ok is True
    health = outage_facts.load_health(conn)
    assert health.auto_healthy is False and health.manual_healthy is False


def test_a_failed_key_records_false_and_a_key_without_bars_records_a_null_watermark(tmp_path):
    conn = _db(tmp_path)
    m = _machine(conn)
    m.observe(_at(10, 1), _report(failed=[(K1M, "ConnectError")], succeeded=[K1H]))
    facts = outage_facts.load_key_facts(conn, SRC)
    assert facts[K1M].observed_through is None
    assert facts[K1M].last_attempt_ok is False
    assert facts[K1H].observed_through is None and facts[K1H].last_attempt_ok is True


def test_facts_are_written_in_the_same_transaction_as_the_state(tmp_path):
    conn = _db(tmp_path)
    _seed(conn, "1m", _at(9, 59))
    _seed(conn, "1h", _at(9, 0))
    m = _machine(conn)

    def broken(**kwargs):
        raise RuntimeError("state save failed")

    original = m._save_state
    m._save_state = broken
    with pytest.raises(RuntimeError, match="state save failed"):
        m.observe(_at(10, 1), _report(succeeded=[K1M, K1H]))
    assert outage_facts.load_key_facts(conn, SRC) == {}
    m._save_state = original
    m.observe(_at(10, 2), _report(succeeded=[K1M, K1H]))
    assert set(outage_facts.load_key_facts(conn, SRC)) == {K1M, K1H}


def test_a_closed_market_observation_writes_no_facts(tmp_path):
    conn = _db(tmp_path)
    _seed(conn, "1m", _at(9, 59))
    m = _machine(conn)
    saturday = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    assert m.observe(saturday, _report()) == "ready"
    assert outage_facts.load_key_facts(conn, SRC) == {}


def test_opening_an_epoch_stores_the_source_and_keeps_the_current_null_substitute(tmp_path):
    conn = _db(tmp_path)
    m = _machine(conn)
    now = _at(10, 1)
    assert m.observe(now, _report(failed=[(K1M, "ConnectError")], succeeded=[K1H])) == "degraded"
    rows = {(r["pair"], r["interval"]): r for r in conn.execute(
        "SELECT * FROM datafeed_outage_gap")}
    assert {r["source"] for r in rows.values()} == {SRC}
    # この task では、水位のない key の起点は従来どおり観測時刻
    assert rows[K1M]["gap_start"] == now.isoformat()


def test_a_closed_state_machine_row_keeps_working_with_a_null_gap_start(tmp_path):
    conn = _db(tmp_path)
    m = _machine(conn)
    m.observe(_at(10, 1), _report(failed=[(K1M, "ConnectError")], succeeded=[K1H]))
    conn.execute("UPDATE datafeed_outage_gap SET gap_start=NULL")
    conn.commit()
    summary = m.gap_summary(_at(10, 2))
    assert summary["gap_starts"] == {}
    assert summary["unprocessed_positions"] == 0
