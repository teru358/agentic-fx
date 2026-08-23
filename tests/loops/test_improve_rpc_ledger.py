"""ImproveRpcLedger の状態機械 (設計書 §3.4、§8.1-15)。

race matrix: in-flight RPC の完了タイミングと freeze の相対順序を
全パターン列挙する。"""
from __future__ import annotations

import threading
import time

import pytest

from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger


def _ledger(**kw):
    return ImproveRpcLedger(rpc_timeout_sec_by_kind=kw or
                            {"run_backtest": 600.0, "analyze_corr": 60.0})


def test_open_allows_record():
    ledger = _ledger()
    ledger.record(opaque_ref="r1", kind="run_backtest", params={},
                  result_summary={"pf": 1.1}, trial_count=1)
    ledger.freeze()
    assert len(ledger.entries()) == 1


def test_frozen_after_freeze_rejects_late_record_silently():
    """FROZEN 後の record は例外にせず無視する (§3.4「遅延結果は捨てる」)。"""
    ledger = _ledger()
    ledger.freeze()
    ledger.record(opaque_ref="late", kind="analyze_corr", params={},
                  result_summary={}, trial_count=5)
    assert ledger.entries() == []


def test_entries_before_freeze_raises():
    """FROZEN 前の entries() 呼び出しは禁止 (読み出しは commit 相専用)。"""
    ledger = _ledger()
    with pytest.raises(RuntimeError):
        ledger.entries()


def test_mark_persisted_after_freeze_succeeds():
    ledger = _ledger()
    ledger.freeze()
    ledger.mark_persisted()  # 例外にならない


def test_mark_discarded_after_freeze_succeeds():
    ledger = _ledger()
    ledger.freeze()
    ledger.mark_discarded()


def test_mark_persisted_before_freeze_raises():
    ledger = _ledger()
    with pytest.raises(RuntimeError):
        ledger.mark_persisted()


def test_mark_discarded_before_freeze_raises():
    ledger = _ledger()
    with pytest.raises(RuntimeError):
        ledger.mark_discarded()


def test_double_freeze_raises():
    ledger = _ledger()
    ledger.freeze()
    with pytest.raises(RuntimeError):
        ledger.freeze()


def test_persisted_then_discarded_raises():
    """終端 (PERSISTED/DISCARDED) から別終端への遷移は禁止。"""
    ledger = _ledger()
    ledger.freeze()
    ledger.mark_persisted()
    with pytest.raises(RuntimeError):
        ledger.mark_discarded()


def test_trial_count_is_summed_not_call_count():
    """§8.1-16 と対になる pin: 台帳は呼出回数でなく trial_count をそのまま
    保持する (親が sum するのは commit 相の責務 — ここは 1 呼出しに
    複数 trial が対応することを崩さない)。"""
    ledger = _ledger()
    ledger.record(opaque_ref="a", kind="analyze_corr", params={},
                  result_summary={}, trial_count=25)  # lead-lag 1 呼出しで 25
    ledger.freeze()
    entries = ledger.entries()
    assert entries[0]["trial_count"] == 25


def test_entries_returns_independent_copy_not_internal_list():
    """M10 (段 0 Minor): `entries()` が内部 list を別名で返す変異
    (`return self._entries`) が red になる pin — 呼び出し側が返り値の
    list を変更しても内部状態 (次回 `entries()` の結果) に影響しないこと。
    FROZEN 後の不変性は `_lock` では守れない次元 (別名参照を渡さないこと
    そのものを見る)。"""
    ledger = _ledger()
    ledger.record(opaque_ref="a", kind="run_backtest", params={},
                  result_summary={}, trial_count=1)
    ledger.freeze()
    e = ledger.entries()
    e.append({"opaque_ref": "injected"})
    assert len(ledger.entries()) == 1


def test_concurrent_record_and_freeze_race_matrix():
    """race matrix: record 実行中に freeze が割り込んでも lock により
    「freeze 前に完了した record は必ず記録され、freeze 後の record は
    必ず捨てられる」の二値のどちらかにしかならない (中間状態が無い)。"""
    ledger = _ledger()
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def do_record():
        barrier.wait()
        ledger.record(opaque_ref="race", kind="run_backtest", params={},
                      result_summary={}, trial_count=1)
        results["recorded"] = True

    def do_freeze():
        barrier.wait()
        time.sleep(0.01)  # record が lock を先に取りやすくする (どちらでもテストは成立する)
        ledger.freeze()
        results["frozen"] = True

    t1 = threading.Thread(target=do_record)
    t2 = threading.Thread(target=do_freeze)
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert results == {"recorded": True, "frozen": True}
    entries = ledger.entries()
    assert len(entries) in (0, 1)  # 中間状態 (部分書込) が無いことの弱い pin


def test_entries_stable_after_freeze():
    """freeze() が返った後、entries() の内容は不変。lock により
    freeze 後の record は記録されないことを強く確認する。"""
    import sys
    import contextlib

    ledger = _ledger()
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def hammer_record():
        """freeze の直後も record を試し続ける"""
        barrier.wait()
        for i in range(100):
            ledger.record(opaque_ref=f"hammer_{i}", kind="run_backtest",
                         params={}, result_summary={}, trial_count=1)
        results["hammered"] = True

    def freeze_and_snapshot():
        barrier.wait()
        # 背景スレッドが record を開始するまで待つ
        time.sleep(0.001)
        ledger.freeze()
        snap1 = ledger.entries()
        # 背景スレッドが freeze 後も record を試す間、複数回 snapshot
        time.sleep(0.01)
        snap2 = ledger.entries()
        results["snap1"] = snap1
        results["snap2"] = snap2

    # スイッチ間隔を狭めて競争状態を高頻度化
    old_interval = sys.getswitchinterval()
    try:
        sys.setswitchinterval(1e-6)
        t1 = threading.Thread(target=hammer_record)
        t2 = threading.Thread(target=freeze_and_snapshot)
        t1.start(); t2.start()
        t1.join(); t2.join()
    finally:
        sys.setswitchinterval(old_interval)

    # 2 つの snapshot は必ず同じ (freeze 後の record は全て無視される)
    assert results["snap1"] == results["snap2"]
