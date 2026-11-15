"""中断した取得操作の再開: 固定尾と未確定 suffix だけを取り、確定 prefix を分母へ戻さない。"""
from __future__ import annotations

from datetime import datetime, timedelta
import pytest

from agentic_fx.backtest import history_sync as hs
from agentic_fx.store import history_sync as store
from tests.backtest.history_sync_support import SYMBOL, UTC, WIDTH, FakeBridge
from tests.backtest.test_history_sync import E, T, _run, _seeded


class StageJumper:
    """n 回目の呼出し以降は大きな時刻を返し、stage を期限切れにして partial を作る。"""

    def __init__(self, calls_before_expiry):
        self.n = calls_before_expiry
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return 0.0 if self.calls <= self.n else 1_000_000.0


def _partial_kw(n):
    return {"max_rows": 50, "monotonic": StageJumper(n)}


def _resume_row(conn):
    return conn.execute("SELECT * FROM history_sync_resume").fetchone()


def _partial_run(tmp_path, bridge=None, *, n=9):
    conn, settings = _seeded(tmp_path)
    bridge = bridge or FakeBridge()
    result = _run(conn, settings, bridge, run_kw=_partial_kw(n))
    return conn, settings, bridge, result


def _dt(*a):
    return datetime(*a, tzinfo=UTC)


def test_a_partial_operation_saves_the_fixed_tail_and_the_confirmed_prefix(tmp_path):
    conn, settings, _bridge, result = _partial_run(tmp_path)
    recent = result.ops[0]
    assert recent.status == "partial" and result.rc == 1
    row = _resume_row(conn)
    assert (row["op_key"], row["range_start"], row["range_end"]) == (
        "recent", _dt(2026, 11, 9, 11, 30).isoformat(), E.isoformat())
    assert row["tail_start"] == _dt(2026, 11, 9, 9, 30).isoformat()
    assert row["prefix_end"] == _dt(2026, 11, 10, 23).isoformat()
    assert result.cursors[SYMBOL] is None                   # 未完了の cursor は進めない


def test_the_next_run_fetches_only_the_tail_and_the_suffix_and_completes(tmp_path):
    conn, settings, _bridge, _first = _partial_run(tmp_path)
    bridge = FakeBridge()
    result = _run(conn, settings, bridge)
    resumed = result.ops[0]
    assert resumed.resumed and resumed.status == "ok"
    # 要求は尾の日 (11-09) と suffix の日 (11-10 後半・11-11)。prefix だけの窓はない
    assert [c[1].date().isoformat() for c in bridge.calls[:3]] == [
        "2026-11-09", "2026-11-10", "2026-11-11"]
    assert resumed.unchanged == 8 + 50                       # 尾 8 + suffix 50 だけを分類した
    assert _resume_row(conn) is None
    assert result.cursors[SYMBOL] == E.isoformat() and result.rc == 0
    recent = [o for o in result.ops if o.operation == "recent"]
    assert [o.resumed for o in recent] == [True, False]
    assert recent[1].status == "deferred" and recent[1].requests == 0


def test_a_resume_is_not_diluted_by_the_confirmed_prefix(tmp_path):
    conflicts = {(SYMBOL, _dt(2026, 11, 11, 7)): 0.3, (SYMBOL, _dt(2026, 11, 11, 7, 15)): 0.3}
    conn, settings = _seeded(tmp_path)
    # 全体 (200 件の分母に 2 件) なら 1% ちょうどで通る状況
    first = _run(conn, settings, FakeBridge(change=conflicts), run_kw=_partial_kw(9))
    assert first.ops[0].status == "partial"
    assert conn.execute("SELECT COUNT(*) FROM history_conflicts").fetchone()[0] == 0
    row = _resume_row(conn)
    second = _run(conn, settings, FakeBridge(change=conflicts))
    # 再開では分母が (尾 8 + suffix 50) = 58 件で、2 件/58 件は遮断される。前回確定の 150 件で希釈しない
    assert second.ops[0].status == "conflict_circuit_open" and second.rc == 4
    assert conn.execute("SELECT COUNT(*) FROM history_conflicts").fetchone()[0] == 0
    assert _resume_row(conn)["prefix_end"] == row["prefix_end"]    # prefix は維持される
    assert len(second.ops) == 1                                    # 通常の recent は走らせない
    assert second.cursors[SYMBOL] is None


def test_a_failed_resume_does_not_start_the_normal_recent_of_the_same_run(tmp_path):
    conn, settings, _b, _first = _partial_run(tmp_path)
    result = _run(conn, settings, FakeBridge(fail=RuntimeError("down")))
    assert [o.status for o in result.ops] == ["failed"] and result.ops[0].resumed
    assert _resume_row(conn) is not None


def test_a_resume_passed_by_b_is_dropped_and_the_operation_starts_fresh(tmp_path):
    conn, settings, _b, _first = _partial_run(tmp_path)
    store.write_txn(conn, lambda c: c.execute(
        "UPDATE history_sync_resume SET tail_start=?", (_dt(2026, 9, 1).isoformat(),)),
        sleep=lambda _s: None)
    result = _run(conn, settings, FakeBridge())
    assert not result.ops[0].resumed and result.ops[0].status == "ok"
    assert _resume_row(conn) is None


class _ArmedJumper(StageJumper):
    """arm されるまで 0 を返し、arm 後の n 回の呼出しのあとで stage を期限切れにする。"""

    def __init__(self, n):
        super().__init__(n)
        self.armed = False

    def __call__(self):
        if not self.armed:
            return 0.0
        return super().__call__()


class _ArmingBridge(FakeBridge):
    def __init__(self, jumper, day):
        super().__init__()
        self.jumper, self.day = jumper, day

    def __call__(self, url):
        payload = super().__call__(url)
        if self.calls[-1][1].date().isoformat() == self.day and not self.jumper.armed:
            self.jumper.armed = True
        return payload


def test_a_partial_gap_day_resumes_from_its_confirmed_prefix(tmp_path):
    conn, settings = _seeded(tmp_path)
    day = "2026-11-04"
    conn.execute("DELETE FROM ohlcv_history WHERE symbol=? AND bar_time >= ? AND bar_time < ?",
                 (SYMBOL, _dt(2026, 11, 4, 12).isoformat(), _dt(2026, 11, 4, 18).isoformat()))
    conn.commit()
    jumper = _ArmedJumper(5)
    first = _run(conn, settings, _ArmingBridge(jumper, day),
                 run_kw={"max_rows": 50, "monotonic": jumper})
    gap = next(o for o in first.ops if o.operation == "gap")
    assert gap.status == "partial" and gap.inserted == 0
    row = conn.execute("SELECT * FROM history_sync_resume WHERE op_key LIKE 'gap:%'").fetchone()
    assert row["op_key"] == f"gap:{day}:primary_exact"
    assert row["prefix_end"] == _dt(2026, 11, 4, 10, 30).isoformat()
    second = _run(conn, settings, FakeBridge())
    resumed = next(o for o in second.ops if o.operation == "gap")
    assert resumed.resumed and resumed.status == "ok"
    assert resumed.inserted == 24                       # 穴の 6 時間だけが入る
    assert resumed.unchanged == 8 + (54 - 24)          # 尾 8 + suffix の既存分。prefix 42 本は分類しない
    assert conn.execute("SELECT COUNT(*) FROM history_sync_resume").fetchone()[0] == 0


def test_resume_and_normal_recent_share_each_series_budget(tmp_path):
    conn, settings, _bridge, _first = _partial_run(tmp_path)
    result = _run(conn, settings, FakeBridge())
    recent = [o for o in result.ops if o.operation == 'recent']
    assert recent[0].resumed and recent[0].requests == 3
    assert (recent[1].status, recent[1].reason, recent[1].requests) == (
        'deferred', 'request_budget', 0)
    assert result.cursors[SYMBOL] == E.isoformat()
    assert conn.execute("SELECT 1 FROM history_sync_gaps WHERE kind='unprocessed'").fetchone()


@pytest.mark.parametrize('change_epoch', [False, True])
def test_committed_batch_survives_process_interruption(tmp_path, monkeypatch, change_epoch):
    from agentic_fx.core.history_epoch import history_epoch
    from agentic_fx.store.db import connect
    conn, settings = _seeded(tmp_path)
    armed = [False]
    original = hs._Run._progress

    def progress(self, *args, **kwargs):
        callback = original(self, *args, **kwargs)
        def on_batch(c, batch):
            callback(c, batch)
            armed[0] = True
        return on_batch

    def clock():
        if armed[0] and not conn.in_transaction:
            raise KeyboardInterrupt()
        return 0.0

    monkeypatch.setattr(hs._Run, '_progress', progress)
    with pytest.raises(KeyboardInterrupt):
        _run(conn, settings, FakeBridge(), run_kw={'max_rows': 50, 'monotonic': clock})
    conn.close()
    conn = connect(tmp_path / 't.db')
    row = _resume_row(conn)
    assert row is not None and row['prefix_end'] > row['range_start']
    if change_epoch:
        before = history_epoch(conn)
        conn.execute("INSERT INTO history_revisions (kind, source, interval, symbol, bar_time, "
                     "history_epoch, conflict_id, actor, created_at) "
                     "VALUES ('replace_with_incoming', ?, ?, ?, ?, ?, ?, ?, ?)",
                     ('mt5', '15m', SYMBOL, E.isoformat(), before, 1, 'test', E.isoformat()))
        conn.commit()
        assert history_epoch(conn) != before
    monkeypatch.setattr(hs._Run, '_progress', original)
    resumed = _run(conn, settings, FakeBridge()).ops[0]
    assert resumed.resumed and resumed.status == 'ok'
    assert resumed.unchanged == 158
    assert _resume_row(conn) is None


def test_multi_series_budget_includes_resumes_deferrals_and_gap_windows(tmp_path):
    from datetime import date
    from tests.backtest.history_sync_support import REF
    from tests.backtest.test_history_sync import _holes, _weekdays
    conn, settings = _seeded(tmp_path, months=2, symbols=(SYMBOL, REF))
    first = _run(conn, settings, FakeBridge(), run_kw=_partial_kw(9))
    assert first.ops[0].status == 'partial'
    days = [d for i, d in enumerate(_weekdays(date(2026, 9, 14), date(2026, 11, 6))) if i % 2]
    _holes(conn, days)
    bridge = FakeBridge()
    result = _run(conn, settings, bridge)
    recent = [o for o in result.ops if o.operation == 'recent']
    gap_requests = sum(o.requests for o in result.ops if o.operation == 'gap')
    assert recent[0].resumed and recent[0].requests == 3
    assert recent[1].status == 'deferred' and recent[1].requests == 0
    assert gap_requests == 32
    assert result.requests == len(bridge.calls) <= (2 + 2) * 2 + 32


def test_insufficient_recent_discards_provisional_resume_and_next_day_uses_new_range(tmp_path):
    conn, settings = _seeded(tmp_path)
    first = _run(conn, settings, FakeBridge(drop=lambda _symbol, _t: True))
    assert first.ops[0].reason == 'inspection_insufficient' and first.ops[0].requests > 0
    assert _resume_row(conn) is None
    second = _run(conn, settings, FakeBridge(), now=T + timedelta(days=1))
    recent = second.ops[0]
    assert not recent.resumed
    assert recent.end == E + timedelta(days=1)
    assert recent.start > first.ops[0].start


def test_failed_resumed_operation_preserves_inherited_checkpoint(tmp_path):
    conn, settings, _, _ = _partial_run(tmp_path)
    before = dict(_resume_row(conn))
    op = _run(conn, settings, FakeBridge(drop=lambda _symbol, _t: True)).ops[0]
    assert op.resumed and op.reason == 'inspection_insufficient'
    after = dict(_resume_row(conn))
    for key in ('range_start', 'range_end', 'tail_start', 'prefix_end'):
        assert after[key] == before[key]
    assert after['prefix_end'] > after['range_start']


def test_insufficient_gap_discards_provisional_resume(tmp_path):
    from datetime import date
    from tests.backtest.test_history_sync import _holes
    conn, settings = _seeded(tmp_path)
    _holes(conn, [date(2026, 10, 20)])
    result = _run(conn, settings, FakeBridge(drop=lambda _symbol, _t: True))
    gap = next(op for op in result.ops if op.operation == 'gap')
    assert gap.reason == 'inspection_insufficient' and gap.requests > 0
    assert conn.execute("SELECT 1 FROM history_sync_resume WHERE op_key LIKE 'gap:%'").fetchone() is None
    second = _run(conn, settings, FakeBridge(), now=T + timedelta(days=1))
    next_gap = next(op for op in second.ops if op.operation == "gap" and op.day == gap.day)
    assert not next_gap.resumed and next_gap.status == "ok"


def test_insufficient_gaps_count_toward_request_budget(tmp_path):
    from datetime import date
    from tests.backtest.test_history_sync import _holes, _weekdays
    conn, settings = _seeded(tmp_path, months=2)
    days = list(_weekdays(date(2026, 9, 14), date(2026, 11, 6)))[::2][:17]
    assert len(days) == 17
    _holes(conn, days)
    result = _run(conn, settings, FakeBridge(drop=lambda _symbol, _t: True))
    gaps = [op for op in result.ops if op.operation == 'gap']
    assert len(gaps) == 17
    assert sum(op.requests for op in gaps) == 32
    assert sum(op.reason == 'inspection_insufficient' and op.requests == 2 for op in gaps) == 16
    assert gaps[-1].reason == 'request_budget' and gaps[-1].requests == 0


@pytest.mark.parametrize('target', [SYMBOL, 'JP225'])
def test_interrupted_recent_queues_prior_days_when_resume_completes(tmp_path, monkeypatch, target):
    from tests.backtest.history_sync_support import REF
    conn, settings = _seeded(tmp_path, symbols=(SYMBOL, REF))
    prior = _dt(2026, 11, 3, 12)
    store.write_txn(conn, lambda c: store.advance_cursor(
        c, 'mt5', '15m', target, last_processed_e=prior, run_id='old', now=T),
        sleep=lambda _s: None)
    original = hs._Run._progress
    armed = [False]

    def progress(self, series_id, symbol, *args, **kwargs):
        callback = original(self, series_id, symbol, *args, **kwargs)
        def on_batch(c, batch):
            callback(c, batch)
            if symbol == target:
                armed[0] = True
        return on_batch

    def clock():
        if armed[0] and not conn.in_transaction:
            raise KeyboardInterrupt()
        return 0.0

    monkeypatch.setattr(hs._Run, '_progress', progress)
    with pytest.raises(KeyboardInterrupt):
        _run(conn, settings, FakeBridge(), run_kw={'max_rows': 50, 'monotonic': clock})
    checkpoint = conn.execute(
        "SELECT * FROM history_sync_resume WHERE symbol=? AND op_key='recent'", (target,)
    ).fetchone()
    assert checkpoint is not None
    assert conn.execute("SELECT COUNT(*) FROM history_sync_gaps WHERE symbol=? AND kind='unprocessed'",
                        (target,)).fetchone()[0] == 0
    monkeypatch.setattr(hs._Run, '_progress', original)
    result = _run(conn, settings, FakeBridge())
    resumed = next(op for op in result.ops if op.symbol == target and op.resumed)
    assert resumed.status == 'ok' and resumed.end == E
    assert result.cursors[target] == E.isoformat()
    days = [r['day_utc'] for r in conn.execute(
        "SELECT day_utc FROM history_sync_gaps WHERE symbol=? AND kind='unprocessed' ORDER BY day_utc",
        (target,))]
    assert days[:6] == ['2026-11-03', '2026-11-04', '2026-11-05',
                        '2026-11-06', '2026-11-08', '2026-11-09']


def test_resumed_queue_stops_at_original_start_when_current_window_moves(tmp_path, monkeypatch):
    conn, settings, _, first = _partial_run(tmp_path)
    assert first.ops[0].status == 'partial'
    original_start = first.ops[0].start
    store.write_txn(conn, lambda c: store.advance_cursor(
        c, 'mt5', '15m', SYMBOL, last_processed_e=_dt(2026, 11, 3, 12),
        run_id='old', now=T), sleep=lambda _s: None)
    captured = []
    original = hs._Run._save_unprocessed

    def capture(self, c, symbol, days):
        if symbol == SYMBOL and days:
            captured.append(days)
        return original(self, c, symbol, days)

    monkeypatch.setattr(hs._Run, '_save_unprocessed', capture)
    result = _run(conn, settings, FakeBridge(), now=T + timedelta(days=1))
    assert result.ops[0].resumed and result.ops[0].status == 'ok'
    assert max(datetime.fromisoformat(span[1]) for spans in captured[0].values()
               for span in spans) == original_start


def test_reconcile_clears_interrupted_gap_resume_before_gap_reopens(tmp_path, monkeypatch):
    from tests.backtest.history_sync_support import seed
    conn, settings = _seeded(tmp_path)
    day = '2026-11-04'
    lo, hi = _dt(2026, 11, 4, 12), _dt(2026, 11, 4, 18)
    conn.execute('DELETE FROM ohlcv_history WHERE symbol=? AND bar_time >= ? AND bar_time < ?',
                 (SYMBOL, lo.isoformat(), hi.isoformat()))
    conn.commit()
    jumper = _ArmedJumper(5)
    first = _run(conn, settings, _ArmingBridge(jumper, day),
                 run_kw={'max_rows': 50, 'monotonic': jumper})
    assert next(op for op in first.ops if op.operation == 'gap').status == 'partial'
    key = f'gap:{day}:primary_exact'
    assert store.get_resume(conn, 'mt5', '15m', SYMBOL, key) is not None
    seed(conn, SYMBOL, lo, hi)
    monkeypatch.setattr(store, 'collect_garbage', lambda *_a, **_kw: {})
    _run(conn, settings, FakeBridge())
    row = conn.execute("SELECT state, resolution FROM history_sync_gaps WHERE symbol=? AND day_utc=? AND kind='primary_exact'",
                       (SYMBOL, day)).fetchone()
    assert (row['state'], row['resolution']) == ('resolved', 'rescan_clear')
    assert store.get_resume(conn, 'mt5', '15m', SYMBOL, key) is None
    conn.execute('DELETE FROM ohlcv_history WHERE symbol=? AND bar_time >= ? AND bar_time < ?',
                 (SYMBOL, lo.isoformat(), hi.isoformat()))
    conn.commit()
    third = _run(conn, settings, FakeBridge())
    reopened = next(op for op in third.ops if op.operation == 'gap' and op.day == day)
    assert not reopened.resumed and reopened.status == 'ok'
