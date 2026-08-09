"""ReflectionCycle 三相再構成の lock 境界 (プラン8, 設計書 §3.1)。"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.core.contracts import FixedClock
from agentic_fx.loops.mission_finalize import finalize_mission
from agentic_fx.runners.base import Mission, MissionResult
from tests.loops.test_reflection_cycle import _cycle, _closed_order

_NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def test_run_phase_does_not_hold_core_lock(tmp_path):
    """run 相 (runner.run) は core_lock を保持しない — **別スレッド**から
    取得できることで検証する (RLock は同一スレッドからは常に取れるので、
    run() の中で試す形は恒真になる — Task 15 で実測済み)。"""
    conn, rag, cyc = _cycle(tmp_path, [])
    _closed_order(conn)
    entered = threading.Event()
    release = threading.Event()

    class BlockingRunner:
        def run(self, mission):
            entered.set()
            release.wait(5.0)
            return MissionResult("completed", {"content": "振り返り"}, [])

    cyc.runner = BlockingRunner()
    t = threading.Thread(target=cyc.run_pending, daemon=True)
    t.start()
    assert entered.wait(5.0), "run 相に到達しなかった"

    # **プラン誤り訂正**: RLock は取得したスレッドしか release できない。
    # `checker` スレッドで acquire に成功した場合、release も同じ
    # `checker` スレッド内で行う (メインスレッドから release すると
    # `RuntimeError: cannot release un-acquired lock` になる)。
    acquired: list[bool] = []

    def _try_acquire():
        ok = cyc._core_lock.acquire(blocking=False)
        acquired.append(ok)
        if ok:
            cyc._core_lock.release()

    checker = threading.Thread(target=_try_acquire)
    checker.start()
    checker.join(timeout=5.0)
    assert acquired == [True], (
        "run 相では core_lock は解放されているはず (別スレッドが取得できる)")

    release.set()
    t.join(timeout=5.0)
    assert not t.is_alive()


def test_commit_core_finalize_holds_core_lock(tmp_path):
    """commit-core 相 (`finalize_mission` 経由の `missions.finish`) は
    core_lock を保持したまま実行される — 指揮者が追加した変異ピン。
    `finalize_mission` 呼び出しを `with self._core_lock:` から外す変異は
    Step 10 の 4 件・Step 3 のいずれのテストでも検出できず SURVIVED した
    (実測 — `uv run pytest tests/loops/ -q` が 104 件全 PASS のまま)。
    `missions.finish` (`finalize_mission` の内部呼び出し) を spy して
    別スレッドから確認する。"""
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "completed", {"content": "振り返り"}, [])])
    _closed_order(conn)
    entered = threading.Event()
    proceed = threading.Event()
    from agentic_fx.store import missions as missions_store
    original = missions_store.finish

    def spy(*args, **kwargs):
        entered.set()
        assert proceed.wait(5.0), "checker が確認を完了しなかった"
        return original(*args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agentic_fx.loops.mission_finalize.missions.finish", spy)
        t = threading.Thread(target=cyc.run_pending, daemon=True)
        t.start()
        assert entered.wait(5.0), "commit-core (finalize_mission) に到達しなかった"

        acquired: list[bool] = []

        def _try_acquire():
            ok = cyc._core_lock.acquire(blocking=False)
            acquired.append(ok)
            if ok:
                cyc._core_lock.release()

        checker = threading.Thread(target=_try_acquire)
        checker.start()
        checker.join(timeout=5.0)
        assert acquired == [False], (
            "commit-core (finalize_mission) 実行中は他スレッドから "
            "core_lock を取得できないはず")

        proceed.set()
        t.join(timeout=5.0)
        assert not t.is_alive()


def test_commit_post_reflections_save_holds_core_lock(tmp_path):
    """commit-post 相の SQLite 書込 (`reflections.save`) は core_lock を
    保持したまま実行される — 指揮者が追加した変異ピン。この呼び出しを
    `with self._core_lock:` から外す変異は Step 10 の 4 件・Step 3・
    `test_commit_core_finalize_holds_core_lock` のいずれでも検出できず
    SURVIVED した (実測 — `uv run pytest tests/loops/ -q` が全 PASS の
    まま)。`reflections.save` を spy して別スレッドから確認する。"""
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "completed", {"content": "振り返り"}, [])])
    _closed_order(conn)
    entered = threading.Event()
    proceed = threading.Event()
    from agentic_fx.store import reflections as reflections_store
    original = reflections_store.save

    def spy(*args, **kwargs):
        entered.set()
        assert proceed.wait(5.0), "checker が確認を完了しなかった"
        return original(*args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agentic_fx.loops.reflection_cycle.reflections.save", spy)
        t = threading.Thread(target=cyc.run_pending, daemon=True)
        t.start()
        assert entered.wait(5.0), "commit-post (reflections.save) に到達しなかった"

        acquired: list[bool] = []

        def _try_acquire():
            ok = cyc._core_lock.acquire(blocking=False)
            acquired.append(ok)
            if ok:
                cyc._core_lock.release()

        checker = threading.Thread(target=_try_acquire)
        checker.start()
        checker.join(timeout=5.0)
        assert acquired == [False], (
            "commit-post (reflections.save) 実行中は他スレッドから "
            "core_lock を取得できないはず")

        proceed.set()
        t.join(timeout=5.0)
        assert not t.is_alive()


def test_prepare_phase_holds_core_lock(tmp_path):
    """prepare 相 (missions.start 等) は core_lock を保持する — run 相と
    対の不変条件。片方だけでは防御にならない。"""
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "completed", {"content": "振り返り"}, [])])
    _closed_order(conn)
    entered = threading.Event()
    proceed = threading.Event()
    from agentic_fx.store import missions as missions_store
    original = missions_store.start

    def spy(*args, **kwargs):
        entered.set()
        assert proceed.wait(5.0), "checker が確認を完了しなかった"
        return original(*args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agentic_fx.loops.reflection_cycle.missions.start", spy)
        t = threading.Thread(target=cyc.run_pending, daemon=True)
        t.start()
        assert entered.wait(5.0), "prepare 相に到達しなかった"

        acquired: list[bool] = []
        checker = threading.Thread(
            target=lambda: acquired.append(
                cyc._core_lock.acquire(blocking=False)))
        checker.start()
        checker.join(timeout=5.0)
        if acquired and acquired[0]:
            cyc._core_lock.release()
        assert acquired == [False], (
            "prepare 実行中は他スレッドから core_lock を取得できないはず")

        proceed.set()
        t.join(timeout=5.0)
        assert not t.is_alive()


def test_rag_write_does_not_hold_core_lock(tmp_path):
    """RAG 書込は core_lock を保持しない (Rag 自身の内部 lock に委ねる
    — Task 9)。誤って core_lock で包むと RAG の I/O 時間だけ SL/TP
    監視が止まる。"""
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "completed", {"content": "振り返り"}, [])])
    _closed_order(conn)
    entered = threading.Event()
    release = threading.Event()
    original = cyc.rag.add_reflection

    def spy(*args, **kwargs):
        entered.set()
        assert release.wait(5.0), "checker が確認を完了しなかった"
        return original(*args, **kwargs)

    cyc.rag.add_reflection = spy
    t = threading.Thread(target=cyc.run_pending, daemon=True)
    t.start()
    assert entered.wait(5.0), "RAG 書込に到達しなかった"

    # **プラン誤り訂正 (test_run_phase_does_not_hold_core_lock と同じ理由)**:
    # RLock の release は取得したスレッド自身で行う。
    acquired: list[bool] = []

    def _try_acquire():
        ok = cyc._core_lock.acquire(blocking=False)
        acquired.append(ok)
        if ok:
            cyc._core_lock.release()

    checker = threading.Thread(target=_try_acquire)
    checker.start()
    checker.join(timeout=5.0)
    assert acquired == [True], (
        "RAG 書込中は core_lock が解放されているはず "
        "(誤って core_lock で包んでいる)")

    release.set()
    t.join(timeout=5.0)
    assert not t.is_alive()


class _ExecuteSpyConn:
    """`conn.execute` の呼び出しを検知するためのプロキシ。sqlite3.Connection
    は immutable な C 拡張型のためインスタンス属性の直接上書きができない
    (`AttributeError: attribute 'execute' is read-only`) — そのため
    プロキシ経由で `execute` だけを差し替え、他は `__getattr__` で実体へ
    委譲する。"""

    def __init__(self, real, on_sql):
        self._real = real
        self._on_sql = on_sql

    def execute(self, sql, *args, **kwargs):
        self._on_sql(sql)
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_run_pending_select_phase_holds_core_lock(tmp_path):
    """run_pending 冒頭の SELECT (未振り返り closed order の列挙) は
    core_lock 保持中に実行される — 指揮者裁定 (Step 10 変異 4 の専用 pin。
    `test_prepare_phase_holds_core_lock` は `_reflect_one` 内の
    `missions.start` 呼び出しを検知するもので、`run_pending` 冒頭の
    SELECT を包む `with self._core_lock:` を外す変異は検出できない
    ため、別途これを追加する)。"""
    conn, rag, cyc = _cycle(tmp_path, [])
    _closed_order(conn)
    entered = threading.Event()
    proceed = threading.Event()

    def on_sql(sql):
        if sql.strip().startswith("SELECT o.*"):
            entered.set()
            assert proceed.wait(5.0), "checker が確認を完了しなかった"

    cyc.conn = _ExecuteSpyConn(conn, on_sql)
    t = threading.Thread(target=cyc.run_pending, daemon=True)
    t.start()
    assert entered.wait(5.0), "run_pending の SELECT に到達しなかった"

    # release は取得したスレッド自身で行う (RLock の制約 —
    # test_run_phase_does_not_hold_core_lock と同じ理由)。
    acquired: list[bool] = []

    def _try_acquire():
        ok = cyc._core_lock.acquire(blocking=False)
        acquired.append(ok)
        if ok:
            cyc._core_lock.release()

    checker = threading.Thread(target=_try_acquire)
    checker.start()
    checker.join(timeout=5.0)
    assert acquired == [False], (
        "run_pending の SELECT 実行中は他スレッドから core_lock を"
        "取得できないはず")

    proceed.set()
    t.join(timeout=5.0)
    assert not t.is_alive()


def test_finalize_mission_conflict_recorded_when_not_finished(tmp_path):
    """`finalize_mission` の `if not finished:` 分岐 (Step 10 変異 1) の
    専用テスト: `missions.finish` が False (CAS 不一致) を返したとき、
    `mission_finalize_conflict` が activity に記録される。この分岐を
    削除する変異はこのテストで red になる。"""
    conn, rag, cyc = _cycle(tmp_path, [])
    activity = ActivityLog(tmp_path / "finalize_a.log")

    from unittest.mock import patch
    with patch("agentic_fx.loops.mission_finalize.missions.finish",
               return_value=False):
        result = finalize_mission(conn, activity, FixedClock(_NOW), 999,
                                  MissionResult("completed", {}, []))
    assert result is True  # 書込み自体は例外を出していない
    entries = [line for line in
              (tmp_path / "finalize_a.log").read_text().split("\n")
              if "mission_finalize_conflict" in line]
    assert len(entries) >= 1, (
        "missions.finish が False を返したとき mission_finalize_conflict "
        "が記録されるはず")
