"""An enable that lands while the collector is mid-fetch must not be overwritten."""
import multiprocessing
import threading

import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.datafeed.news_collector import NewsCollector
from agentic_fx.store import news_health, news_sources
from agentic_fx.store.db import connect, init_db
from tests.datafeed.news_support import FakeRag, MutableClock, T0, raised_status

WAIT = 15


def disable_then_enable(db_path, source_id, name, now, nonce):
    """What `enable` does: source row and health record in one transaction."""
    conn = connect(db_path)
    try:
        news_sources.set_enabled(conn, source_id, False)
        news_sources.set_enabled(conn, source_id, True, commit=False)
        news_health.initialize_for_enable(conn, name, now, nonce=nonce)
        conn.commit()
    finally:
        conn.close()


def _setup(tmp_path):
    db = tmp_path / "race.db"
    conn = connect(db)
    init_db(conn)
    source_id = news_sources.add(conn, name="src", fetcher="feed",
                                 url="https://example.invalid/feed", added_by="user",
                                 now=T0, enabled=True)
    clock = MutableClock()
    collector = NewsCollector(conn, FakeRag(), ActivityLog(tmp_path / "a.log"), clock, timeout_sec=10)
    return db, conn, source_id, collector


def _prepare(conn, case):
    if case == "absent":
        return
    if case == "corrupt":
        conn.execute("INSERT INTO alert_state (key,value,updated_at) VALUES (?,?,?)",
                     (news_health.PREFIX + "src", "garbage", T0.isoformat()))
    elif case == "same_instant":
        news_health.initialize_for_enable(conn, "src", T0, nonce="first")
    conn.commit()


@pytest.mark.parametrize("stop", ["during_fetch", "before_write"])
@pytest.mark.parametrize("case", ["absent", "corrupt", "same_instant"])
def test_enable_during_a_fetch_keeps_the_fresh_record(tmp_path, monkeypatch, case, stop):
    db, conn, source_id, collector = _setup(tmp_path)
    _prepare(conn, case)
    reached, proceed = threading.Event(), threading.Event()
    real_write = news_health.write_if_unchanged

    def fetch(url, name, *, timeout_sec):
        if stop == "during_fetch":
            reached.set()
            assert proceed.wait(WAIT)
        raise raised_status(500)

    def gated_write(*args, **kwargs):
        reached.set()
        assert proceed.wait(WAIT)
        return real_write(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.news_collector.fetch_feed", fetch)
        if stop == "before_write":
            patch.setattr(news_health, "write_if_unchanged", gated_write)
        errors = []

        def run():
            try:
                collector.collect()
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        worker = threading.Thread(target=run)
        worker.start()
        assert reached.wait(WAIT)
        disable_then_enable(db, source_id, "src", T0, "second")
        other = connect(db)
        fresh = news_health.read(other, "src")
        assert fresh.record.reset_nonce == "second"
        proceed.set()
        worker.join(WAIT)
        assert not worker.is_alive() and not errors
        after = news_health.read(other, "src")
        other.close()
    assert after.raw == fresh.raw
    assert after.record.consecutive_failures == 0
    assert after.record.last_outcome is None


def test_without_interference_the_fetch_result_is_written(tmp_path, monkeypatch):
    db, conn, source_id, collector = _setup(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.news_collector.fetch_feed",
                      lambda *a, **k: (_ for _ in ()).throw(raised_status(500)))
        collector.collect()
    assert news_health.read(conn, "src").record.consecutive_failures == 1


def test_enable_from_another_process_during_a_fetch(tmp_path, monkeypatch):
    db, conn, source_id, collector = _setup(tmp_path)
    _prepare(conn, "same_instant")
    ctx = multiprocessing.get_context("spawn")
    exit_codes = []

    def fetch(url, name, *, timeout_sec):
        child = ctx.Process(target=disable_then_enable,
                            args=(db, source_id, "src", T0, "other-process"))
        child.start()
        child.join(60)
        exit_codes.append(child.exitcode)
        raise raised_status(500)

    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.news_collector.fetch_feed", fetch)
        collector.collect()
    assert exit_codes == [0]
    record = news_health.read(conn, "src").record
    assert record.reset_nonce == "other-process"
    assert (record.consecutive_failures, record.last_outcome) == (0, None)


def test_conditional_write_is_one_statement(tmp_path):
    conn = connect(tmp_path / "one.db")
    init_db(conn)
    statements = []
    conn.set_trace_callback(statements.append)
    record = news_health.Health(consecutive_failures=1)
    assert news_health.write_if_unchanged(conn, "src", None, record, T0, commit=False)
    raw = news_health.read(conn, "src").raw
    statements.clear()
    assert news_health.write_if_unchanged(conn, "src", raw, record, T0, commit=False)
    assert len(statements) == 1
    assert statements[0].lstrip().upper().startswith("UPDATE")
    statements.clear()
    assert not news_health.write_if_unchanged(conn, "src", None, record, T0, commit=False)
    assert len(statements) == 1
    assert statements[0].lstrip().upper().startswith("INSERT")
    assert not news_health.write_if_unchanged(conn, "src", "stale", record, T0, commit=False)
