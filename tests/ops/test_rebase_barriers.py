"""HWM 付け直しと core lock を取る書き手 (commit-core / MTM) の競合。

commit-core と MTM は、本物の `current_account` / `record_snapshot` と StateStore を
core lock の中で使う最小の書き手で表す (core lock の取得が競合の境界)。"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import timedelta

import pytest

from agentic_fx.core import accounting
from agentic_fx.ops.contracts import ErrorCode, OpsError
from agentic_fx.store.state import StateStore

from .test_rebase_service import (APPROVER, LOSS, NOW, START, _gen, _ledger, _order,
                                  _preview, _snapshot, rig)  # noqa: F401


class _PausingStore(StateStore):
    """共有 guard に入ったところで止まり、合図まで保持し続ける (rebase が core を保持中)。"""

    def __init__(self, path):
        super().__init__(path)
        self.entered = threading.Event()
        self.proceed = threading.Event()

    @contextmanager
    def shared_guard(self, **kwargs):
        with super().shared_guard(**kwargs) as guard:
            self.entered.set()
            assert self.proceed.wait(30)
            yield guard


def _start(target, results):
    thread = threading.Thread(target=lambda: results.append(target()), daemon=True)
    thread.start()
    return thread


def _paused_rebase(rig):
    store = _PausingStore(rig.state._path)
    service = rig.env.service(rebase=rig.wiring, state_store=store)
    token = service.rebase_preview(APPROVER, _gen(rig))["preview_token"]
    results: list = []
    generation = _gen(rig)

    def post():
        try:
            return service.rebase_kill_switch(APPROVER, generation, token)
        except OpsError as exc:
            return exc.code

    thread = _start(post, results)
    assert store.entered.wait(10)
    return store, thread, results


def test_commit_core_waits_for_rebase_and_sees_the_new_hwm(rig):
    rig.state.reset_kill_switch(_gen(rig))  # 未ラッチ。commit-core が古い HWM を読めば再ラッチする
    store, rebase_thread, rebase_results = _paused_rebase(rig)
    conn_core = rig.env.connect()
    seen: list = []

    def commit_core():
        with rig.core_lock:
            equity, hwm = accounting.current_account(conn_core, NOW)
            drawdown = accounting.drawdown_pct(equity, hwm)
            seen.append((hwm, drawdown))
            if drawdown >= rig.env.settings.risk.drawdown_kill_pct:
                rig.state.update(kill_switch_latched=True)

    core_thread = _start(commit_core, [])
    core_thread.join(0.5)
    assert core_thread.is_alive() and seen == []  # core は rebase が保持している
    store.proceed.set()
    rebase_thread.join(10)
    core_thread.join(10)
    assert isinstance(rebase_results[0], dict)
    assert seen == [(START - LOSS, 0.0)]
    state = rig.state.load()
    assert state.kill_switch_latched is False and state.kill_switch_generation == 1
    conn_core.close()


def test_rebase_waits_for_commit_core_that_latches_on_the_old_hwm(rig):
    rig.state.reset_kill_switch(_gen(rig))
    token = _preview(rig)["preview_token"]
    generation = _gen(rig)
    inside, go = threading.Event(), threading.Event()
    conn_core = rig.env.connect()

    def commit_core():
        with rig.core_lock:
            equity, hwm = accounting.current_account(conn_core, NOW)
            assert accounting.drawdown_pct(equity, hwm) >= 2.0
            inside.set()
            assert go.wait(30)
            rig.state.update(kill_switch_latched=True)  # 古い HWM に基づくラッチ

    core_thread = _start(commit_core, [])
    assert inside.wait(5)
    results: list = []

    def post():
        try:
            return rig.service.rebase_kill_switch(APPROVER, generation, token)
        except OpsError as exc:
            return exc.code

    rebase_thread = _start(post, results)
    rebase_thread.join(0.5)
    assert rebase_thread.is_alive()  # core の取得待ち
    go.set()
    core_thread.join(10)
    rebase_thread.join(10)
    assert results == [ErrorCode.GENERATION_MISMATCH]
    assert _ledger(rig.conn) == []
    assert rig.state.load().kill_switch_generation == generation + 1
    conn_core.close()


def test_mtm_waits_for_rebase_and_continues_from_the_new_hwm(rig):
    store, rebase_thread, rebase_results = _paused_rebase(rig)
    conn_core = rig.env.connect()
    written: list = []

    def mtm():
        with rig.core_lock:
            written.append(accounting.record_snapshot(
                conn_core, now=NOW, balance=START - LOSS, equity=START - LOSS))

    mtm_thread = _start(mtm, [])
    mtm_thread.join(0.5)
    assert mtm_thread.is_alive() and written == []
    store.proceed.set()
    rebase_thread.join(10)
    mtm_thread.join(10)
    assert isinstance(rebase_results[0], dict)
    assert written[0]["hwm"] == START - LOSS  # 付け直し後の HWM を引き継ぐ (old なら START)
    rows = rig.conn.execute(
        "SELECT id,source FROM account_snapshots ORDER BY ts DESC,id DESC").fetchall()
    assert rows[0]["source"] == "paper"
    assert rows[1]["id"] == rebase_results[0]["snapshot_id"]
    conn_core.close()


def test_rebase_waits_for_mtm_and_rejects_a_changed_account(rig):
    token = _preview(rig)["preview_token"]
    generation = _gen(rig)
    inside, go = threading.Event(), threading.Event()
    conn_core = rig.env.connect()

    def mtm():
        with rig.core_lock:
            inside.set()
            assert go.wait(30)
            _order(conn_core, "closed", pnl=-1_000.0)
            accounting.record_snapshot(
                conn_core, now=NOW, balance=START - LOSS - 1_000, equity=START - LOSS - 1_000)

    mtm_thread = _start(mtm, [])
    assert inside.wait(5)
    results: list = []

    def post():
        try:
            return rig.service.rebase_kill_switch(APPROVER, generation, token)
        except OpsError as exc:
            return exc.code

    rebase_thread = _start(post, results)
    rebase_thread.join(0.5)
    assert rebase_thread.is_alive()
    go.set()
    mtm_thread.join(10)
    rebase_thread.join(10)
    assert results == [ErrorCode.PREVIEW_CHANGED]
    assert _ledger(rig.conn) == []
    conn_core.close()
