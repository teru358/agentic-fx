"""``afx news`` next to a running collector, a second CLI process and a held instance lock."""
from __future__ import annotations

import contextlib
import fcntl
import io
import multiprocessing
import os
import sqlite3
import threading
from pathlib import Path

import pytest

from agentic_fx import entry, news_cli
from agentic_fx.activity import ActivityLog
from agentic_fx.datafeed import news_probe
from agentic_fx.datafeed.news_collector import NewsCollector
from agentic_fx.store import news_health, news_sources
from agentic_fx.store.db import connect
from tests.datafeed.news_support import FakeRag, MutableClock, raised_status
from tests.news_cli_support import NOW, OK_URL, FakeHttp, db_path, dump_tables, make_root, run_news
from tests.status_support import held_by_other_process, touch_lock

WAIT = 30
BUSY = "DB が使用中です。もう一度実行してください。\n"


def _activity_lines(root: Path) -> list[str]:
    path = root / "logs" / "activity.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


# --- a collector that is writing -----------------------------------------------------------------

def test_add_and_enable_succeed_while_a_collector_cycle_is_running(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path, sources=tuple((f"s{n}", "feed", f"{OK_URL}{n}", True) for n in range(6)))
    collector_conn = connect(db_path(root))
    collector = NewsCollector(collector_conn, FakeRag(), ActivityLog(root / "logs" / "collector.log"),
                              MutableClock(), timeout_sec=10)
    started, release = threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def slow_fetch(url, name, *, timeout_sec):
        started.set()
        assert release.wait(WAIT)
        raise raised_status(500)

    def cycle():
        try:
            collector.collect()
        except BaseException as exc:  # noqa: BLE001 - reported by the assertion below
            errors.append(exc)

    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.news_collector.fetch_feed", slow_fetch)
        worker = threading.Thread(target=cycle)
        worker.start()
        assert started.wait(WAIT)
        FakeHttp(patch)
        added = run_news(capsys, patch, root, "add", "fresh", "https://example.invalid/fresh")
        disabled = run_news(capsys, patch, root, "disable", "s5")
        enabled = run_news(capsys, patch, root, "enable", "s5")
        release.set()
        worker.join(WAIT)
    collector_conn.close()
    assert not worker.is_alive() and not errors
    assert added[0] == disabled[0] == enabled[0] == 0
    names = [row[1] for row in dump_tables(root)["news_sources"]]
    assert names[-1] == "fresh"


def test_readers_work_while_another_connection_holds_an_uncommitted_write(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path, sources=(("src", "feed", OK_URL, True),))
    holder = connect(db_path(root))
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("UPDATE news_sources SET enabled=0")
    try:
        FakeHttp(monkeypatch)
        rc, out, _ = run_news(capsys, monkeypatch, root, "list")
        assert rc == 0 and "ニュース源: 1 件 (有効 1 / 無効 0)" in out
        assert run_news(capsys, monkeypatch, root, "test", "src")[0] == 0
    finally:
        holder.rollback()
        holder.close()


# --- busy timeout ---------------------------------------------------------------------------------

@pytest.fixture
def short_busy(monkeypatch):
    """Same connection as production, but with a 50 ms wait so the test does not sleep 5 s."""
    real = news_cli.connect_existing_rw

    def connect_short(path):
        conn = real(path)
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        conn.execute("PRAGMA busy_timeout=50")
        return conn

    monkeypatch.setattr(news_cli, "connect_existing_rw", connect_short)


@pytest.mark.parametrize("argv", [("enable", "src"), ("disable", "off"), ("add", "fresh", "https://example.invalid/f")])
def test_a_lock_that_outlasts_the_busy_timeout_is_one_with_nothing_left_behind(
        tmp_path, monkeypatch, capsys, short_busy, argv):
    root = make_root(tmp_path, sources=(("src", "feed", OK_URL, False), ("off", "feed", OK_URL + "2", True)))
    FakeHttp(monkeypatch)
    tables = dump_tables(root)
    holder = connect(db_path(root))
    holder.execute("BEGIN IMMEDIATE")
    try:
        rc, out, err = run_news(capsys, monkeypatch, root, *argv)
    finally:
        holder.rollback()
        holder.close()
    assert (rc, err) == (1, BUSY)
    assert "追加しました" not in out and "にしました" not in out
    assert dump_tables(root) == tables
    assert _activity_lines(root) == []


def test_the_command_succeeds_once_the_lock_is_released(tmp_path, monkeypatch, capsys, short_busy):
    root = make_root(tmp_path, sources=(("src", "feed", OK_URL, False),))
    holder = connect(db_path(root))
    holder.execute("BEGIN IMMEDIATE")
    assert run_news(capsys, monkeypatch, root, "enable", "src")[0] == 1
    holder.rollback()
    holder.close()
    assert run_news(capsys, monkeypatch, root, "enable", "src")[0] == 0


# --- instance lock --------------------------------------------------------------------------------

def test_no_command_takes_the_instance_lock_and_all_work_while_it_is_held(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path, sources=(("src", "feed", OK_URL, False),))
    FakeHttp(monkeypatch)
    lock = touch_lock(root)
    calls = []
    real_flock = fcntl.flock
    monkeypatch.setattr(fcntl, "flock", lambda *a, **k: (calls.append(a), real_flock(*a, **k))[1])
    with held_by_other_process(lock):
        results = [run_news(capsys, monkeypatch, root, *argv)[0] for argv in (
            ("list",), ("test", "src"), ("enable", "src"), ("disable", "src"),
            ("add", "fresh", "https://example.invalid/fresh"))]
    assert results == [0, 0, 0, 0, 0]
    assert calls == []


# --- two processes adding the same source --------------------------------------------------------

def _child_add(root, name, url, barrier, record_dir, results):
    """Runs in a spawned process: the fetch waits for the other process before inserting."""
    def probe(fetcher, probed_url, probed_name, *, timeout_sec):
        (Path(record_dir) / f"probe-{os.getpid()}").write_text("x")
        barrier.wait(WAIT)
        return news_probe.ProbeResult(1, 1)

    news_probe.probe = probe
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = entry.main(["--root", root, "news", "add", name, url])
    results.put((rc, out.getvalue(), err.getvalue()))


@pytest.mark.parametrize(("names", "urls"), [
    (("dup", "dup"), ("https://example.invalid/a", "https://example.invalid/b")),
    (("one", "two"), ("https://example.invalid/same", "https://example.invalid/same")),
], ids=["same-name", "same-url"])
def test_two_processes_adding_the_same_source_leave_exactly_one(tmp_path, names, urls):
    root = make_root(tmp_path)
    records = tmp_path / "probes"
    records.mkdir()
    ctx = multiprocessing.get_context("spawn")
    barrier, results = ctx.Barrier(2), ctx.Queue()
    processes = [ctx.Process(target=_child_add, args=(str(root), names[i], urls[i], barrier, str(records), results))
                 for i in range(2)]
    for process in processes:
        process.start()
    outcomes = [results.get(timeout=WAIT * 2) for _ in processes]
    for process in processes:
        process.join(WAIT)
        assert process.exitcode == 0
    assert sorted(rc for rc, _, _ in outcomes) == [0, 1]
    loser = next(outcome for outcome in outcomes if outcome[0] == 1)
    winner = next(outcome for outcome in outcomes if outcome[0] == 0)
    assert loser[1] == "取得: 1 件 (うち本文・要約あり 1 件)\n"
    rows = dump_tables(root)["news_sources"]
    assert len(rows) == 1
    expected = ("名前 dup は既に使われています。\n" if names[0] == names[1]
                else f"URL が源 {rows[0][1]} と重複\n")
    assert loser[2] == expected
    assert "追加しました" in winner[1]
    assert len(list(records.iterdir())) == 2          # one fetch per process, never more
    assert len(_activity_lines(root)) == 1


def test_a_sibling_insert_between_the_precheck_and_the_insert_is_a_duplicate_not_a_crash(
        tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path)

    def racing_probe(fetcher, url, name, *, timeout_sec):
        other = connect(db_path(root))
        news_sources.add(other, name=name, fetcher="feed", url="https://example.invalid/sibling",
                         added_by="user", now=NOW, enabled=True)
        other.close()
        return news_probe.ProbeResult(1, 1)

    monkeypatch.setattr(news_probe, "probe", racing_probe)
    rc, out, err = run_news(capsys, monkeypatch, root, "add", "dup", "https://example.invalid/mine")
    assert (rc, err) == (1, "名前 dup は既に使われています。\n")
    assert "追加しました" not in out
    assert len(dump_tables(root)["news_sources"]) == 1
    assert _activity_lines(root) == []


# --- an enable that lands while the collector is fetching ------------------------------------------

def test_cli_disable_then_enable_during_a_fetch_keeps_the_fresh_health_record(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path, sources=(("src", "feed", OK_URL, True),))
    conn = connect(db_path(root))
    news_health.initialize_for_enable(conn, "src", NOW, nonce="first")
    conn.commit()
    collector = NewsCollector(conn, FakeRag(), ActivityLog(root / "logs" / "collector.log"),
                              MutableClock(), timeout_sec=10)
    reached, proceed = threading.Event(), threading.Event()

    def fetch(url, name, *, timeout_sec):
        reached.set()
        assert proceed.wait(WAIT)
        raise raised_status(500)

    errors: list[BaseException] = []

    def cycle():
        try:
            collector.collect()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.news_collector.fetch_feed", fetch)
        worker = threading.Thread(target=cycle)
        worker.start()
        assert reached.wait(WAIT)
        assert run_news(capsys, patch, root, "disable", "src")[0] == 0
        assert run_news(capsys, patch, root, "enable", "src")[0] == 0
        other = connect(db_path(root))
        fresh = news_health.read(other, "src")
        proceed.set()
        worker.join(WAIT)
        after = news_health.read(other, "src")
        other.close()
    conn.close()
    assert not worker.is_alive() and not errors
    assert fresh.record.reset_nonce not in (None, "first")
    assert after.raw == fresh.raw
    assert after.record.consecutive_failures == 0
