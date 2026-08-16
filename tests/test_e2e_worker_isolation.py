"""プラン8 E2E — 設計書 §9 受入条件 1・2 (実 subprocess・実統合)。"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from agentic_fx.core.contracts import (
    Bar, FixedClock, InstrumentSpec, OrderStatus, Quote,
)
from agentic_fx.service import build_app, run_init
from agentic_fx.store import orders as orders_store
from agentic_fx.store import ohlcv as ohlcv_store

from tests.conftest import _LLAMA_SWAP_UNREACHABLE
from tests.store.test_rag import FakeEmbedding


_REPO_ROOT = Path(__file__).resolve().parents[1]

# 実 llama-swap の既定アドレス。差し替え先 (即 ECONNREFUSED) は
# tests/conftest.py の共有定数を使う。
_LLAMA_SWAP_REAL = 'base_url: "http://localhost:8080/v1"'


def _install_settings(root: Path) -> None:
    """既存 E2E と同じ settings.example → run_init の初期化。"""
    (root / "config").mkdir()
    src = (_REPO_ROOT / "config" / "settings.yaml.example").read_text(
        encoding="utf-8")
    # **ネットワーク隔離** (2026-08-16 実測): `build_app` は `runner=` 未指定
    # なら本物の `WorkerRunner` を作り、それを **TradeLoop と ReflectionCycle
    # の両方**に注入する。テストが `app.trade_loop.runner` だけ差し替えても
    # reflection は本物を握ったままなので、`scheduler.tick` が実 subprocess
    # (`mission_worker`) を起動し、子は `llama_swap.base_url` = 実 llama-swap
    # (`http://localhost:8080/v1`) へ本当に POST していた (journal 実測:
    # 1 スイートあたり 1 件・約 28 秒・35B モデルのロードまで誘発)。
    # 到達不能アドレスへ差し替えて即 ECONNREFUSED にする
    # (`tests/runners/test_worker_runner.py` の実 subprocess E2E と同じ手)。
    assert _LLAMA_SWAP_REAL in src, (
        "settings.yaml.example の llama_swap.base_url 表記が変わった — "
        "テストのネットワーク隔離が静かに外れる")
    src = src.replace(_LLAMA_SWAP_REAL, _LLAMA_SWAP_UNREACHABLE)
    (root / "config" / "settings.yaml.example").write_text(
        src, encoding="utf-8")
    with patch("agentic_fx.service.PriceProvider") as provider, \
         patch("agentic_fx.service._check_llama_swap"):
        provider.return_value.healthcheck.return_value = "yfinance"
        assert run_init(root) == 0


def test_worker_runner_kills_process_that_ignores_sigterm(tmp_path):
    """受入条件 1: SIGTERM 無視プロセスのツリーを SIGKILL で刈る。"""
    from agentic_fx.config import WorkerSettings
    from agentic_fx.runners.worker_runner import WorkerRunner

    ready = tmp_path / "worker-tree-ready"
    proc = subprocess.Popen(
        ["sh", "-c",
         "trap '' TERM; "
         "sh -c 'trap \"\" TERM; touch \"$1\"; "
         "while :; do sleep 60; done' child \"$1\" & wait",
         "parent", str(ready)],
        start_new_session=True)
    pgid = proc.pid
    try:
        deadline = time.monotonic() + 2.0
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "SIGTERM 無視の子プロセスが起動しなかった"

        runner = WorkerRunner.__new__(WorkerRunner)
        settings = WorkerSettings(worker_terminate_grace_sec=1.0)

        start = time.monotonic()
        runner._escalate_kill(proc, settings)
        elapsed = time.monotonic() - start

        assert proc.poll() is not None
        assert elapsed < 5.0

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            pytest.fail("プロセスグループが残存している "
                        "(worker ツリーを kill できていない)")
    finally:
        # assertion failure 時にも、このテストが作ったプロセスツリーを残さない。
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=3.0)


def test_funds_protection_continues_during_blocked_mission(tmp_path):
    """受入条件 2: Mission がブロック中でも本番配線の SL 監視は継続する。"""
    from agentic_fx.runners.base import Mission, MissionResult

    _install_settings(tmp_path)
    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    safe_bar = Bar("USDJPY", "1m", now, 148.20, 148.30, 148.00,
                   148.20, 100.0)
    sl_bar = Bar("USDJPY", "1m", now + timedelta(minutes=1),
                 148.00, 148.10, 147.50, 147.60, 100.0)
    current_bar = {"value": safe_bar}
    quote = Quote("USDJPY", 148.19, 148.21, now, "test")
    spec = InstrumentSpec("USDJPY", 0.01, 0.01, 50.0, 0.01,
                          100_000, "USD", "JPY")
    app = build_app(
        tmp_path, clock=FixedClock(now), quote_fn=lambda _pair: quote,
        spec_fn=lambda _pair: spec,
        bars_fn=lambda _pair: current_bar["value"],
        embedding_fn=FakeEmbedding())

    reached = threading.Event()
    release = threading.Event()

    class BlockingRunner:
        def run(self, mission: Mission) -> MissionResult:
            reached.set()
            if not release.wait(10.0):
                raise TimeoutError("test runner release was not signalled")
            return MissionResult(
                "completed", {"action": "hold", "reasoning": "test"}, [])

    try:
        # TradeLoop の read-only healthcheck_provider.get_bars は bars_fn 注入
        # の対象外。実 DB キャッシュを用意し、本物の healthcheck を通す。
        ohlcv_store.upsert_cache_bars(
            app.conn_core,
            [Bar("USDJPY", "1h", now, 148.00, 148.30, 147.90,
                 148.20, 100.0)],
            source="yfinance")
        oid = orders_store.insert(
            app.conn_core, pair="USDJPY", direction="long",
            entry_type="market", horizon="swing", status="open", now=now,
            quantity=0.1, remaining_quantity=0.0,
            avg_fill_price=148.20, filled_quantity=0.1,
            stop_loss=147.80, take_profit=149.00,
            filled_at=now.isoformat())

        # 本番配線で runner に届かせる。外部データ hook はこのテストの対象外
        # なので、直前実行済み扱いにしてネットワーク I/O を発生させない。
        app.scheduler._last_news = now
        app.scheduler._last_econ = now
        app.trade_loop.runner = BlockingRunner()
        app.supervisor.start()

        with app.core_lock:
            app.scheduler.tick(now)
        assert reached.wait(5.0), (
            "scheduler→on_trade_mission→supervisor.try_submit→_trade_fn→"
            "TradeLoop.run_once→runner.run の本番配線に到達しなかった")

        current_bar["value"] = sl_bar
        acquired = app.core_lock.acquire(timeout=3.0)
        assert acquired, "Mission 実行中に core_lock を取得できなかった"
        try:
            app.scheduler.tick(now + timedelta(minutes=1))
        finally:
            app.core_lock.release()

        assert orders_store.get(app.conn_core, oid)["status"] == \
            OrderStatus.CLOSED.value
    finally:
        release.set()
        app.supervisor.shutdown(drain_exc=RuntimeError("test cleanup"))
        app.supervisor.join(timeout=10.0)
        app.close()


def test_kill_switch_latch_fires_identically_via_open_and_open_from_snapshot(
        tmp_path):
    """裁定書 FC-6: source pin と両 OPEN 経路のラッチ副作用を固定する。"""
    import inspect

    from agentic_fx.core.accounting import record_snapshot
    from agentic_fx.core.executor import Executor
    from tests.core.test_executor_snapshot import (
        NOW, _insert_intent, _make_executor, _open_intent,
        _start_trade_mission,
    )

    src = inspect.getsource(Executor._evaluate_and_execute_open)
    assert '"kill switch" in r and "latched" not in r' in src
    assert "self.state.update(kill_switch_latched=True)" in src

    direct = _make_executor(tmp_path / "direct")
    snapshot_path = tmp_path / "snapshot"
    snap = _make_executor(snapshot_path)
    record_snapshot(direct.conn, now=NOW, balance=970_000, equity=970_000)
    record_snapshot(snap.conn, now=NOW, balance=970_000, equity=970_000)
    assert direct.state.load().kill_switch_latched is False
    assert snap.state.load().kill_switch_latched is False

    intent = _open_intent()
    direct_out = direct.handle_intent(
        intent, _start_trade_mission(direct.conn))
    snap_mid = _start_trade_mission(snap.conn)
    snap_iid = _insert_intent(snap.conn, snap_mid, intent)
    execution_snapshot = snap.gather_open_snapshot(intent, exposure_pairs=[])
    snapshot_out = snap.open_from_snapshot(
        intent, snap_iid, execution_snapshot, max_snapshot_age_sec=999.0)

    assert direct_out["result"] == snapshot_out["result"] == "rejected"
    assert direct_out["reasons"] == snapshot_out["reasons"]
    assert direct.state.load().kill_switch_latched is True
    assert snap.state.load().kill_switch_latched is True
    assert "kill_switch_latched" in (
        tmp_path / "direct" / "activity.log").read_text(encoding="utf-8")
    assert "kill_switch_latched" in (
        snapshot_path / "activity.log").read_text(encoding="utf-8")
