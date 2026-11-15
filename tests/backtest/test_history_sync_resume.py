"""中断した取得操作の再開: 固定尾と未確定 suffix だけを取り、確定 prefix を分母へ戻さない。"""
from __future__ import annotations

from datetime import datetime, timedelta

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
    assert [o.resumed for o in result.ops] == [True, False]    # 完走後に今回の recent が続く


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
