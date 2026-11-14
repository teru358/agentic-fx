"""履歴取込の共通の流れ (全取得 → 事前遮断 → batch 確定) を、偽の fetch と実 sqlite で確かめる。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from agentic_fx.backtest.cli import (
    CIRCUIT_DISPLAY_LIMIT, CONFLICT_DISPLAY_LIMIT, _print_import_outcome)
from agentic_fx.backtest.importer import import_dukascopy
from agentic_fx.store import history_conflicts as hc
from agentic_fx.store import history_import as hi
from agentic_fx.store import ohlcv
from agentic_fx.store.db import connect
from agentic_fx.store.history_lease import HistoryLeaseBusy, history_mutation_lease
from tests.backtest.factories import H, _bi5, _conn

SEEN = datetime(2026, 11, 14, tzinfo=timezone.utc)


def _t(i, minutes=1):
    return (H + timedelta(minutes=minutes * i)).isoformat()


def _row(i, close=148.1, symbol="USDJPY", interval="1m", minutes=1, source_rows=None):
    return (symbol, interval, _t(i, minutes), 148.0, 148.2, 147.9, close, 10.0, None)


def _seed(conn, rows, source="mt5"):
    ohlcv.import_history_bars(conn, rows, source=source)


def _spec(rows_or_fn, *, source="mt5", symbol="USDJPY", interval="1m", n=1, minutes=1):
    fetch = rows_or_fn if callable(rows_or_fn) else (lambda: list(rows_or_fn))
    start = H
    end = H + timedelta(minutes=minutes * n)
    return hi.SeriesSpec(source, interval, symbol, start, end, end, fetch)


def _run(conn, spec, **kw):
    kw.setdefault("now", lambda: SEEN)
    return hi.run_series(conn, spec, **kw)


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _fail(code="fetch_failed"):
    def fetch():
        raise hi.SeriesFetchError(code, "boom")
    return fetch


# ---- H1: 孤立した衝突の後も続ける ------------------------------------------

@pytest.mark.parametrize("source", ["mt5", "dukascopy"])
def test_isolated_conflict_does_not_stop_later_healthy_bars(tmp_path, source):
    conn = _conn(tmp_path)
    _seed(conn, [_row(0)], source)
    rows = [_row(0, close=148.15)] + [_row(i) for i in range(1, 50)]
    out = _run(conn, _spec(rows, source=source, n=50))
    assert out.status == "completed_with_conflicts"
    assert (out.inserted, out.conflicted_new) == (49, 1)
    assert _count(conn, "ohlcv_history") == 50
    assert _count(conn, "history_conflicts") == 1
    assert hi.summarize([out]).rc == 3


# ---- H2/H3: 系統的なずれは遮断し、系列 0 件 ---------------------------------

def test_systematic_shift_blocks_the_series_and_writes_nothing(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, [_row(i) for i in range(200)])
    rows = [_row(i, close=148.15) for i in range(200)] + [_row(i) for i in range(200, 240)]
    out = _run(conn, _spec(rows, n=240))
    assert out.status == "conflict_circuit_open"
    assert (out.conflicted_new, out.n_test) == (200, 200)
    assert _count(conn, "ohlcv_history") == 200
    assert _count(conn, "history_conflicts") == 0
    assert hi.summarize([out]).rc == 4


@pytest.mark.parametrize("n_unchanged,n_conflict,blocked", [
    (0, 0, False), (0, 1, False), (1, 1, False), (197, 2, True), (198, 2, False),
])
def test_small_n_thresholds_on_real_rows(tmp_path, n_unchanged, n_conflict, blocked):
    conn = _conn(tmp_path)
    existing = [_row(i) for i in range(n_unchanged + n_conflict)]
    _seed(conn, existing)
    rows = [_row(i, close=148.15) if i < n_conflict else _row(i)
            for i in range(n_unchanged + n_conflict)]
    out = _run(conn, _spec(rows, n=max(1, len(rows))))
    # (197, 2): 2/199 は 1% 超で遮断、(198, 2): 2/200 ちょうど 1% は通る
    assert (out.status == "conflict_circuit_open") is blocked


def test_fifteen_minute_series_uses_its_own_small_n(tmp_path):
    conn = _conn(tmp_path)
    rows = [_row(i, interval="15m", minutes=15) for i in range(20)]
    _seed(conn, rows)
    one = [_row(0, close=148.15, interval="15m", minutes=15)] + rows[1:]
    two = [_row(0, close=148.16, interval="15m", minutes=15),
           _row(1, close=148.16, interval="15m", minutes=15)] + rows[2:]
    assert _run(conn, _spec(one, interval="15m", n=20, minutes=15)).status \
        == "completed_with_conflicts"
    out = _run(conn, _spec(two, interval="15m", n=20, minutes=15))
    assert out.status == "conflict_circuit_open"      # 新規 2 件 / N_test 20 = 10%


# ---- C_known: 同じ範囲の再実行で既知の衝突を数え直さない -------------------

def test_rerun_of_the_same_range_counts_known_conflicts_not_new(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, [_row(i) for i in range(400)])
    rows = [_row(i, close=148.15) if i < 3 else _row(i) for i in range(400)]
    first = _run(conn, _spec(rows, n=400))
    assert (first.conflicted_new, first.conflicted_known) == (3, 0)
    later = lambda: datetime(2026, 11, 15, tzinfo=timezone.utc)
    second = _run(conn, _spec(rows, n=400), now=later)
    assert (second.conflicted_new, second.conflicted_known) == (0, 3)
    assert second.status == "completed_with_conflicts"
    recs = [dict(r) for r in conn.execute("SELECT * FROM history_conflicts ORDER BY id")]
    assert [r["seen_count"] for r in recs] == [2, 2, 2]
    assert len({r["first_seen_at"] for r in recs}) == 1
    assert all(r["last_seen_at"] > r["first_seen_at"] for r in recs)


def test_many_known_conflicts_neither_block_nor_dilute(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, [_row(i) for i in range(60)])
    for i in range(25):
        hc.upsert(conn, source="mt5", interval="1m", symbol="USDJPY", bar_time=_t(i),
                  history_epoch="none", existing=(148.0, 148.2, 147.9, 148.1, 10.0, None),
                  incoming=(148.0, 148.2, 147.9, 148.15, 10.0, None), origin="import",
                  seen_at=SEEN.isoformat())
    conn.execute("UPDATE history_conflicts SET state='kept'")
    conn.commit()
    rows = [_row(i, close=148.15) if i < 25 else _row(i) for i in range(60)]
    out = _run(conn, _spec(rows, n=60))
    assert out.status == "completed_with_conflicts"
    assert (out.conflicted_new, out.conflicted_known, out.n_test) == (0, 25, 35)
    assert {r["state"] for r in conn.execute("SELECT state FROM history_conflicts")} == {"kept"}
    # 通常表示は 20 件で打ち切り、省略数を出す
    assert len(out.conflict_lines) == 25


# ---- H14: 複数系列と取得失敗の 0 件化 ---------------------------------------

def test_partial_fetch_failure_zeroes_only_that_series(tmp_path):
    conn = _conn(tmp_path)
    a = _spec([_row(i, symbol="EURUSD") for i in range(5)], symbol="EURUSD", n=5)
    b = _spec(_fail(), symbol="GBPUSD", n=5)
    c = _spec([_row(i, symbol="AUDUSD") for i in range(5)], symbol="AUDUSD", n=5)
    outcome = hi.run_import(conn, [a, b, c], now=lambda: SEEN)
    assert [s.status for s in outcome.series] == ["ok", "failed", "ok"]
    assert outcome.series[1].failure == "fetch_failed"
    assert (outcome.result, outcome.rc) == ("partial", 1)
    symbols = {r[0] for r in conn.execute("SELECT DISTINCT symbol FROM ohlcv_history")}
    assert symbols == {"EURUSD", "AUDUSD"}


def test_all_series_failing_is_failed_not_partial(tmp_path):
    conn = _conn(tmp_path)
    outcome = hi.run_import(conn, [_spec(_fail(), symbol="A"), _spec(_fail("invalid_response"))],
                            now=lambda: SEEN)
    assert (outcome.result, outcome.rc) == ("failed", 1)
    assert _count(conn, "ohlcv_history") == 0


def test_unexpected_fetch_exception_becomes_a_series_failure_with_error_type(tmp_path):
    conn = _conn(tmp_path)

    def boom():
        raise httpx.ConnectError("connection refused")

    out = _run(conn, _spec(boom))
    assert (out.status, out.failure, out.error_type) == ("failed", "fetch_failed", "ConnectError")


def test_technical_failure_outranks_circuit_open_and_conflicts(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, [_row(i, symbol="EURUSD") for i in range(200)])
    circuit = _spec([_row(i, close=148.15, symbol="EURUSD") for i in range(200)],
                    symbol="EURUSD", n=200)
    failed = _spec(_fail(), symbol="GBPUSD")
    out = hi.run_import(conn, [circuit, failed], now=lambda: SEEN)
    assert (out.result, out.rc) == ("failed", 1)


@pytest.mark.parametrize("statuses,result,rc", [
    (["ok"], "ok", 0),
    (["ok", "completed_with_conflicts"], "completed_with_conflicts", 3),
    (["completed_with_conflicts", "conflict_circuit_open"], "conflict_circuit_open", 4),
    (["ok", "conflict_circuit_open"], "conflict_circuit_open", 4),
    (["conflict_circuit_open", "failed"], "failed", 1),
    (["ok", "failed", "conflict_circuit_open"], "partial", 1),
    (["completed_with_conflicts", "partial"], "partial", 1),
    (["failed"], "failed", 1),
    (["partial"], "failed", 1),     # 確定なしの partial (last_committed なし) は failed
])
def test_rc_table_and_priority(statuses, result, rc):
    series = [hi.SeriesOutcome("mt5", "1m", "S", st, "a", "b", "c", "op") for st in statuses]
    out = hi.summarize(series)
    assert (out.result, out.rc) == (result, rc)


# ---- H7: batch 途中失敗 → 元の範囲の再実行 ---------------------------------

def test_mid_batch_failure_is_partial_then_rerun_completes(tmp_path):
    conn = _conn(tmp_path)
    steady = [_row(10 + i) for i in range(200)]
    _seed(conn, [_row(0), _row(3), *steady])
    rows = [_row(0, close=148.15), _row(1), _row(2), _row(3, close=148.16), _row(4), *steady]
    conn.execute(
        f"CREATE TRIGGER boom BEFORE INSERT ON history_conflicts "
        f"WHEN NEW.bar_time = '{_t(3)}' BEGIN SELECT RAISE(ABORT, 'secret 148.16'); END")
    conn.commit()
    out = _run(conn, _spec(rows, n=210), max_rows=2)
    assert out.status == "partial" and out.failure == "write_failed"
    assert out.error_type == "IntegrityError" and "148" not in repr(out.error_type)
    assert out.last_committed_bar_time == _t(1) and out.resume_from == _t(2)
    assert (out.inserted, out.conflicted_new) == (1, 1)
    assert _count(conn, "ohlcv_history") == 203
    assert hi.summarize([out]).rc == 1
    conn.execute("DROP TRIGGER boom")
    conn.commit()
    again = _run(conn, _spec(rows, n=210), max_rows=2)
    assert again.status == "completed_with_conflicts"
    assert (again.inserted, again.conflicted_new, again.conflicted_known) == (2, 1, 1)
    assert _count(conn, "ohlcv_history") == 205
    seen = {r["bar_time"]: r["seen_count"] for r in conn.execute("SELECT * FROM history_conflicts")}
    assert seen == {_t(0): 2, _t(3): 1}


# ---- mutation lease: network 中は持たない / lease → admission → BEGIN ----------

def test_lease_is_not_held_while_fetching_and_is_held_from_classification_on(
        tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    other = connect(tmp_path / "t.db")
    events = []

    def lease_free():
        try:
            with history_mutation_lease(other):
                return True
        except HistoryLeaseBusy:
            return False

    def fetch():
        events.append(("fetch", lease_free(), conn.in_transaction))
        return [_row(0)]

    real = ohlcv.classify_history_rows

    def classify(*a, **k):
        events.append(("classify", lease_free()))
        return real(*a, **k)

    monkeypatch.setattr(ohlcv, "classify_history_rows", classify)

    class Admission:
        def admit(self, begin):
            events.append(("admit", lease_free(), conn.in_transaction))
            begin()
            return True

        def end(self):
            events.append(("end",))

    out = _run(conn, _spec(fetch), admission=Admission())
    assert out.status == "ok"
    assert events == [("fetch", True, False), ("classify", False),
                      ("admit", False, False), ("end",)]
    assert lease_free()                      # 終了後は解放されている


def test_lease_held_by_another_writer_yields_database_busy_with_zero_writes(tmp_path):
    conn = _conn(tmp_path)
    other = connect(tmp_path / "t.db")
    with history_mutation_lease(other):
        out = _run(conn, _spec([_row(0)]))
    assert (out.status, out.failure) == ("failed", "database_busy")
    assert _count(conn, "ohlcv_history") == 0
    assert hi.summarize([out]).rc == 1


def test_busy_write_lock_retries_within_budget_then_gives_up(tmp_path):
    conn = _conn(tmp_path)
    holder = connect(tmp_path / "t.db")
    holder.execute("BEGIN IMMEDIATE")
    slept = []
    out = _run(conn, _spec([_row(0)]), sleep=slept.append)
    holder.rollback()
    assert (out.status, out.failure) == ("failed", "database_busy")
    assert len(slept) == 10 and abs(sum(slept) - 0.25) < 1e-9
    assert _count(conn, "ohlcv_history") == 0


def test_busy_write_lock_released_during_the_wait_lets_the_import_finish(tmp_path):
    conn = _conn(tmp_path)
    holder = connect(tmp_path / "t.db")
    holder.execute("BEGIN IMMEDIATE")
    calls = []

    def sleep(sec):
        calls.append(sec)
        if len(calls) == 2:
            holder.rollback()

    out = _run(conn, _spec([_row(0)]), sleep=sleep)
    assert out.status == "ok" and out.inserted == 1 and len(calls) == 2


def test_busy_timeout_is_zero_during_the_import_and_restored(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    seen = []
    real = ohlcv.apply_classified_history

    def spy(c, *a, **k):
        seen.append(c.execute("PRAGMA busy_timeout").fetchone()[0])
        return real(c, *a, **k)

    before = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    monkeypatch.setattr(ohlcv, "apply_classified_history", spy)
    _run(conn, _spec([_row(0)]))
    assert seen == [0]
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == before


# ---- stage の寿命 -----------------------------------------------------------

def test_stage_expired_after_fetch_writes_nothing(tmp_path):
    conn = _conn(tmp_path)
    ticks = iter([0.0, 601.0, 602.0, 603.0])
    out = _run(conn, _spec([_row(0)]), monotonic=lambda: next(ticks))
    assert (out.status, out.failure) == ("failed", "stage_expired")
    assert _count(conn, "ohlcv_history") == 0
    with history_mutation_lease(conn):       # lease は残っていない
        pass


def test_stage_expiring_between_batches_keeps_the_prefix_and_releases_the_lease(tmp_path):
    conn = _conn(tmp_path)
    rows = [_row(i) for i in range(6)]
    # 取得開始 0 → 取得後 1 → batch 1 の前 2・commit 直前 3 → batch 2 の前 700
    ticks = iter([0.0, 1.0, 2.0, 3.0, 700.0] + [700.0] * 20)
    out = _run(conn, _spec(rows, n=6), max_rows=2, monotonic=lambda: next(ticks))
    assert (out.status, out.failure) == ("partial", "stage_expired")
    assert out.inserted == 2 and out.resume_from == _t(2)
    assert _count(conn, "ohlcv_history") == 2
    with history_mutation_lease(conn):
        pass
    # 次の実行は元の範囲から再開できる
    again = _run(conn, _spec(rows, n=6))
    assert (again.status, again.inserted, again.unchanged) == ("ok", 4, 2)


def test_stage_expiring_inside_a_batch_rolls_that_batch_back(tmp_path):
    conn = _conn(tmp_path)
    rows = [_row(i) for i in range(4)]
    # 取得開始 0 → 取得後 1 → batch 1 前 2 → batch 1 内 (row 0, row 1 で停止判定は
    # clock 不使用) → batch 1 の commit 直前 700
    seq = iter([0.0, 1.0, 2.0, 700.0])
    out = _run(conn, _spec(rows, n=4), max_rows=2,
               monotonic=lambda: next(seq, 700.0))
    assert (out.status, out.failure) == ("failed", "stage_expired")
    assert _count(conn, "ohlcv_history") == 0
    with history_mutation_lease(conn):
        pass


def test_stage_expiring_before_the_last_batch_commit_keeps_only_earlier_batches(tmp_path):
    conn = _conn(tmp_path)
    rows = [_row(i) for i in range(4)]
    # batch 1 は前後とも期限内、batch 2 の前は期限内で commit 直前に越える
    seq = iter([0.0, 1.0, 2.0, 3.0, 4.0, 700.0])
    out = _run(conn, _spec(rows, n=4), max_rows=2,
               monotonic=lambda: next(seq, 700.0))
    assert (out.status, out.failure) == ("partial", "stage_expired")
    assert out.inserted == 2 and out.resume_from == _t(2)
    assert _count(conn, "ohlcv_history") == 2


# ---- Dukascopy も同じ core: 取得失敗は系列 0 件、衝突は rc 3 -----------------

def test_dukascopy_last_hour_fetch_failure_zeroes_the_whole_series(tmp_path):
    conn = _conn(tmp_path)
    good = _bi5([(0, 148_010, 148_000, 1, 1)])
    calls = []

    def fetch(url):
        calls.append(url)
        if len(calls) == 3:
            raise httpx.ConnectError("down")
        return good

    out = import_dukascopy(conn, "USDJPY", H, H + timedelta(hours=3), fetch=fetch,
                           sleep=lambda s: None)
    assert (out.status, out.failure, out.error_type) == ("failed", "fetch_failed", "ConnectError")
    assert _count(conn, "ohlcv_history") == 0


def test_dukascopy_corrupt_payload_is_a_series_failure(tmp_path):
    conn = _conn(tmp_path)
    out = import_dukascopy(conn, "USDJPY", H, H + timedelta(hours=1),
                           fetch=lambda url: b"not lzma", sleep=lambda s: None)
    assert (out.status, out.failure) == ("failed", "invalid_response")
    assert _count(conn, "ohlcv_history") == 0


def test_dukascopy_conflict_completes_with_rc_3(tmp_path):
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(
        conn, [("USDJPY", "1m", H.isoformat(), 1.0, 2.0, 0.5, 1.5, 1, 0.1)],
        source="dukascopy")
    payload = _bi5([(0, 148_010, 148_000, 1, 1), (60_000, 148_020, 148_010, 1, 1)])
    out = import_dukascopy(conn, "USDJPY", H, H + timedelta(hours=2),
                           fetch=lambda url: payload, sleep=lambda s: None)
    assert out.status == "completed_with_conflicts"
    assert out.conflicted_new == 1 and out.inserted >= 1
    assert hi.summarize([out]).rc == 3


# ---- 表示の上限 --------------------------------------------------------------

def _conflicted_outcome(tmp_path, n, *, known):
    conn = _conn(tmp_path)
    _seed(conn, [_row(i) for i in range(n + 3000)])
    rows = [_row(i, close=148.15) if i < n else _row(i) for i in range(n + 3000)]
    if known:
        for i in range(n):
            hc.upsert(conn, source="mt5", interval="1m", symbol="USDJPY", bar_time=_t(i),
                      history_epoch="none", existing=(148.0, 148.2, 147.9, 148.1, 10.0, None),
                      incoming=(148.0, 148.2, 147.9, 148.15, 10.0, None), origin="import",
                      seen_at=SEEN.isoformat())
        conn.commit()
    return _run(conn, _spec(rows, n=n + 3000))


def test_normal_conflicts_are_shown_up_to_twenty_with_omitted(tmp_path, capsys):
    out = _conflicted_outcome(tmp_path, 25, known=True)
    _print_import_outcome(hi.summarize([out]))
    err = capsys.readouterr().err
    assert err.count("conflict id=") == CONFLICT_DISPLAY_LIMIT == 20
    assert "omitted=5" in err and "C_known=25" in err


def test_circuit_open_shows_five_rows_omitted_and_persisted_false(tmp_path, capsys):
    out = _conflicted_outcome(tmp_path, 30, known=False)
    assert out.status == "conflict_circuit_open"
    _print_import_outcome(hi.summarize([out]))
    captured = capsys.readouterr()
    assert captured.err.count("\nconflict new ") == CIRCUIT_DISPLAY_LIMIT == 5
    assert "omitted=25" in captured.err and "persisted=false" in captured.err
    assert "C_new=30" in captured.err and "N_overlap=3030" in captured.err
    assert "result=conflict_circuit_open rc=4" in captured.out
