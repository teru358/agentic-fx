import json
import os
import sqlite3
import signal
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from agentic_fx.core.contracts import (
    Action, ConversionRate, Direction, EntryType, FixedClock, Horizon,
    InstrumentSpec, Origin, Quote, TradeIntent,
)
from agentic_fx.runners.base import MissionResult
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.runners.worker_runner import WorkerRunner
from agentic_fx.service import (
    _assert_tools_registered, _check_llama_swap, _validate_startup,
    build_app, build_splash, run_init, run_service,
)
from agentic_fx.tools import market_tools
from tests.store.test_rag import FakeEmbedding

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def _init(tmp_path):
    (tmp_path / "config").mkdir()
    src = open("config/settings.yaml.example", encoding="utf-8").read()
    (tmp_path / "config" / "settings.yaml.example").write_text(src)
    with patch("agentic_fx.service.PriceProvider") as pp, \
         patch("agentic_fx.service._check_llama_swap"):
        pp.return_value.healthcheck.return_value = "yfinance"
        run_init(tmp_path)


def _seam_app(tmp_path, runner):
    """run_service のテスト用シームで注入する App を組み立てる (F4)。

    FakeEmbedding を注入することで chromadb のモデル DL を避け、
    テスト高速化を実現する (Task 0)。

    分離方式 (着手前検証、1815 errors の根本原因の一部):
    `run_service`/`_scheduler_tick_once` を実際に走らせるテストがこの
    ヘルパを広く共有しているが、improve レーンは大半のテストの関心外。
    Scheduler の catch-up 起動判定 (`latest_scheduled_occurrence`) は
    **常に**直近の過去 occurrence を見つける (改善スケジュールの特定
    時刻に一致させる必要はない — 初回 tick は必ず 1 回 catch-up する
    設計、設計書 §3.1) ため、`ImproveSupervisor.tick` が実発火し実
    WorkerRunner (実 subprocess・実 llama-swap 接続) を spawn していた
    (`tests/loops/test_gate_reject_alert.py::_app_with_threshold` と
    同じ根本原因)。`schedule.improve_at` を動かしてテストを黙らせる
    対処 (231a485、本 Task で revert 済み) は出荷既定を汚すため却下 —
    かわりにこのヘルパで `on_improve_tick` を無効化し、improve を実際に
    試すテスト (`test_improve_tick_and_supervisor_wired_after_task12` 等、
    `_seam_app` を経由せず `_init`+`build_app` を直接使う) だけが明示的に
    有効化する。
    """
    _init(tmp_path)
    app = build_app(tmp_path, runner=runner, clock=FixedClock(NOW),
                    embedding_fn=FakeEmbedding())
    app.scheduler.on_improve_tick = None
    return app


@contextmanager
def _no_real_network():
    """run_service の scheduler スレッドは起動直後に実時刻で 1 tick 実行する
    (`last = 0.0` のため `time.monotonic() - last >= 60` が即座に真になる)。
    `on_news_cycle`/`on_econ_cycle` は本物の `NewsCollector.collect` /
    `EconCalendar.refresh` に配線されており、これらは実際に外部 HTTP
    (RSS フィード・ForexFactory カレンダー) を叩く。`_stop_event` を事前
    set したテストでは scheduler スレッドの while 条件が起動時点で偽になる
    ため実害はないが (実測: 高速)、`_KeyboardInterruptOnMainWait` を使う
    テストは stop_event が未 set の状態でスレッドが走り出すため、メイン
    スレッドの KeyboardInterrupt 処理と競合して実 HTTP が発火しうる (実測:
    2 秒超のブレ)。フェッチ層そのものを patch して、タイミング (スレッド
    レース) に関係なく構造的に実 HTTP を遮断する。`app.scheduler.tick(...)`
    を直接呼ぶテスト (`test_tick_propagates_trigger_to_missions_row`) にも
    同じ理由で使う。"""
    from agentic_fx.datafeed.econ_calendar import CalendarFetch
    with patch("agentic_fx.datafeed.news_collector.fetch_feed",
               return_value=[]), \
         patch("agentic_fx.datafeed.news_collector.fetch_web",
               return_value=[]), \
         patch("agentic_fx.datafeed.econ_calendar.fetch_ff_calendar",
               return_value=CalendarFetch(events=[], dropped=0)):
        yield


def test_build_app_wires_everything(tmp_path):
    _init(tmp_path)
    fake = FakeRunner([MissionResult("completed",
                                     {"action": "hold", "reasoning": "w"},
                                     [])])
    app = build_app(tmp_path, runner=fake, clock=FixedClock(NOW))
    assert app.trade_loop is not None
    assert app.scheduler is not None
    assert isinstance(app.core_lock, type(threading.RLock()))
    assert app.conn_core is not app.conn_shell  # スレッド別接続
    # F5: Commands は conn_shell 束縛の broker を持つ (conn_core をシェルスレッド
    # から触らせない配線の回帰ピン — conn_core/app.broker に差し替える変異を
    # 検出する)
    assert app.commands.conn is app.conn_shell
    assert app.commands.broker is not app.broker
    # ツールが登録されている
    for name in ("get_ohlcv", "search_news", "get_positions",
                 "get_recent_reflections"):
        assert name in app.registry.names()


def test_build_app_wires_one_stop_event_to_scheduler_and_worker(tmp_path):
    _init(tmp_path)
    stop_event = threading.Event()
    app = build_app(tmp_path, clock=FixedClock(NOW),
                    embedding_fn=FakeEmbedding(), stop_event=stop_event)
    try:
        assert app.stop_event is stop_event
        assert app.scheduler._stop_event is stop_event
        assert isinstance(app.runner, WorkerRunner)
        assert app.runner._stop_event is stop_event
        assert app.runner._on_rpc_leak is not None
        assert app.commands.health_latch is app.health_latch
    finally:
        app.close()


def test_build_app_activity_write_failure_actually_latches_health(tmp_path):
    """レビュー1周目 I-2a: `on_write_failure` が「渡されている」ではなく
    「効いている」ことを確認する。

    旧テストは配線の有無しか見ておらず、`build_app` から
    `on_write_failure=` を**丸ごと削除しても全件 1709 passed** だった
    (指揮者が実測)。「配線されているが誰も効かない」型の欠陥をこのピンで
    捕まえる。
    """
    from agentic_fx.activity import Category

    _init(tmp_path)
    app = build_app(tmp_path, clock=FixedClock(NOW),
                    embedding_fn=FakeEmbedding())
    try:
        assert app.health_latch.is_latched() is False

        def boom(*a, **k):
            raise OSError("disk full")

        # ActivityLog は「write は例外を送出しない」契約なので、失敗は
        # コールバック経由でしか観測できない。
        with patch.object(type(app.activity._path), "open", boom):
            app.activity.write(Category.SYSTEM, "x", "y")

        assert app.health_latch.is_latched() is True, (
            "activity 書き込み失敗が health latch に届いていない "
            "(build_app の on_write_failure 配線が効いていない)")
        assert "activity write failed" in app.health_latch.summary()[0]
    finally:
        app.close()


def test_build_app_rpc_leak_callback_latches_health_and_sets_stop(tmp_path):
    """レビュー1周目 I-2b: `_on_rpc_leak` の中身が効いていることを確認する。

    旧テストは `_on_rpc_leak is not None` しか見ておらず、閉包から
    `stop_event.set()` の 1 行を削除しても全件緑だった (指揮者が実測)。
    RPC leak は「dispatcher スレッドが二度と回収されない」状態なので、
    latch だけでなく**停止まで到達する**ことが要件 (設計書 §4.3 codex I3-1)。
    """
    _init(tmp_path)
    stop_event = threading.Event()
    app = build_app(tmp_path, clock=FixedClock(NOW),
                    embedding_fn=FakeEmbedding(), stop_event=stop_event)
    try:
        assert app.health_latch.is_latched() is False
        assert stop_event.is_set() is False

        app.runner._on_rpc_leak()

        assert app.health_latch.is_latched() is True
        assert stop_event.is_set() is True, (
            "RPC leak が停止シーケンスを起動していない")
    finally:
        app.close()


def test_build_app_registers_get_signals(tmp_path):
    """⑦プラン 7 Task 9: signal_tools.build が service に配線され、起動時
    _assert_tools_registered を通ること (build_app 相当の起動テスト)。"""
    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
    assert "get_signals" in app.registry.names()


def test_get_signals_via_registry_execute_returns_decoded_payload(tmp_path):
    """A (advisor 指摘): これまでのテストは `tool.func(...)` を直接呼ぶだけ
    で、本番経路である `registry.execute` (jsonschema 検証 + json.dumps)
    を一度も通していなかった (verify-integration-not-just-units と同型の
    穴)。実際に LLM 相当の呼び出し形 (dict 引数 → JSON 文字列) で疎通する
    ことを確認する。"""
    import json as _json

    from agentic_fx.store import signals as signals_store

    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
    signals_store.add(
        app.conn_core, plugin="sig1", content_hash="h1", pair="USDJPY",
        timeframe="1h", bar_ts=(NOW - timedelta(hours=1)).isoformat(),
        kind="signal", payload={"direction": "long"}, now=NOW)

    raw = app.registry.execute(
        "get_signals", {"pair": "USDJPY"}, ["get_signals"])
    out = _json.loads(raw)
    assert isinstance(out, list) and len(out) == 1
    assert out[0]["payload"] == {"direction": "long"}


def test_get_signals_via_registry_execute_since_hours_over_max_is_schema_error(tmp_path):
    """スキーマ maximum が先に弾く経路 (関数側クランプとは別の防御層):
    LLM 経由 (registry.execute) では since_hours=100 はクランプされず
    invalid arguments エラーになる — brief ①の「クランプ」は関数を直接
    呼ぶ経路 (テスト・将来の呼び出し元) の防波堤であり、registry.execute
    経由では二重防御のうちスキーマ側が先に発火する (opus R2 M11 の
    「lookback 上限で遮断は保たれる」ことの確認)。"""
    import json as _json

    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
    raw = app.registry.execute(
        "get_signals", {"pair": "USDJPY", "since_hours": 100},
        ["get_signals"])
    out = _json.loads(raw)
    assert "error" in out


def test_bless_is_not_registered_as_a_tool(tmp_path):
    """改善ループ非露出ピンの一部: bless は人間 CLI 専用であり、そもそも
    ToolDef として registry に登録されない (プラン 9 の allowed リスト
    実装前でも、この経路自体が存在しないことを固定する)。"""
    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
    assert "bless" not in app.registry.names()


def test_build_app_wires_approved_plugins_into_market_tools(tmp_path):
    """プラン 7 Task 3: `approved_plugins(conn_core, root / "plugins")` の
    結果が `market_tools.build(..., indicator_plugins=...)` まで実際に届く
    ことのピン。`indicator_plugins=approved` の削除や `plugins_dir` の
    typo (例: `root / "plugin"`) をしても、plugins/ が存在しない通常の
    テスト環境では `approved_plugins` が `[]` を返すだけで単体テストは
    通ってしまう — 配線そのものを検証しないと検出できない回帰
    (メモリ: verify-integration-not-just-units)。
    """
    _init(tmp_path)
    sentinel_meta = object()  # market_tools.build に渡る値だけを見る (中身は不問)
    captured: dict = {}
    real_build = market_tools.build

    def spy_build(*args, **kwargs):
        captured.update(kwargs)
        return real_build(*args, **{**kwargs, "indicator_plugins": None})

    with patch("agentic_fx.service.plugin_loader.approved_plugins") as approved, \
         patch("agentic_fx.tools.mission_registry.market_tools.build",
               side_effect=spy_build) as build_spy:
        approved.return_value = [sentinel_meta]
        app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))

    assert approved.call_count == 1
    conn_arg, plugins_dir_arg = approved.call_args[0]
    assert conn_arg is app.conn_core
    assert plugins_dir_arg == tmp_path / "plugins"
    assert build_spy.call_count == 1
    assert captured["indicator_plugins"] == [sentinel_meta]
    # spy 経由でも実装 (real_build) を実際に呼んでおり、登録は正常に完了する
    assert "get_ohlcv" in app.registry.names()


def test_splash_contains_key_fields(tmp_path):
    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
    splash = build_splash(app)
    assert "learning" in splash
    assert "USDJPY" in splash
    assert "qwen" in splash  # runner モデル名


def test_on_trade_mission_runs_loop_and_reflection(tmp_path):
    """Task 13 fix (coordinator指摘): scheduler.tick() 起点で on_trade_mission の
    配線を検証。Future 待ちを patch 内側に移動。"""
    _init(tmp_path)
    fake = FakeRunner([MissionResult("completed",
                                     {"action": "hold", "reasoning": "w"},
                                     [])])
    app = build_app(tmp_path, runner=fake, clock=FixedClock(NOW))
    from agentic_fx.core.accounting import record_snapshot
    record_snapshot(app.conn_core, now=NOW, balance=1_000_000,
                    equity=1_000_000)
    # プラン 8 Task 13: on_trade_mission は supervisor 経由で非同期実行される
    # ため、supervisor を起動してから tick を呼ぶ必要がある。
    app.supervisor.start()
    try:
        # spy: supervisor.try_submit() が返す Future をキャプチャする
        from agentic_fx.core.supervisor import MissionSupervisor
        captured = []
        original_try_submit = MissionSupervisor.try_submit
        def spy_try_submit(self, kind, **kw):
            f = original_try_submit(self, kind, **kw)
            if f is not None:
                captured.append(f)
            return f
        # 重要: Future.result() を patch の内側で呼ぶ
        with _no_real_network(), \
             patch.object(MissionSupervisor, "try_submit", spy_try_submit), \
             patch.object(app.provider, "healthcheck", return_value="yfinance"), \
             patch.object(app.trade_loop.provider, "healthcheck",
                          return_value="yfinance"):
            app.scheduler.tick(NOW)
            assert captured, "no Future was captured"
            assert captured[0] is not None
            captured[0].result(timeout=5.0)  # Mission 完了まで待つ (patch 内側)
        assert len(fake.missions) >= 1  # trade mission が実行された
        rows = app.conn_core.execute("SELECT * FROM missions").fetchall()
        assert any(r["loop"] == "trade" for r in rows)
    finally:
        app.supervisor.shutdown(drain_exc=RuntimeError("test shutdown"))


def test_on_trade_mission_wrapper_also_runs_reflection(tmp_path):
    """変異テストで発見 (mutation round): `on_trade_mission` から
    `reflection.run_pending()` を消しても `test_on_trade_mission_runs_loop_and_
    reflection` は緑のまま生存する (trade mission 実行の確認しかしていない
    ため)。closed 注文を 1 件用意し、`missions` に reflection loop の行が
    実際に作られること (= reflection.run_pending が呼ばれたこと) を直接
    ピンする。Task 13 fix (coordinator指摘): scheduler.tick() 起点で検証、
    Future 待ちを patch 内側に。"""
    _init(tmp_path)
    fake = FakeRunner([
        MissionResult("completed", {"action": "hold", "reasoning": "w"}, []),
        MissionResult("completed", {"content": "振り返り"}, []),
    ])
    app = build_app(tmp_path, runner=fake, clock=FixedClock(NOW))
    from agentic_fx.core.accounting import record_snapshot
    from agentic_fx.store import orders as orders_store
    record_snapshot(app.conn_core, now=NOW, balance=1_000_000,
                    equity=1_000_000)
    orders_store.insert(
        app.conn_core, pair="USDJPY", direction="long", entry_type="market",
        horizon="day", status="closed", now=NOW, quantity=0.1,
        avg_fill_price=148.0, close_price=149.0, realized_pnl=100.0,
        close_reason="tp")
    # プラン 8 Task 13: on_trade_mission は supervisor 経由で非同期実行される
    # ため、supervisor を起動してから tick を呼ぶ必要がある。
    app.supervisor.start()
    try:
        # spy: supervisor.try_submit() が返す Future をキャプチャする
        from agentic_fx.core.supervisor import MissionSupervisor
        captured = []
        original_try_submit = MissionSupervisor.try_submit
        def spy_try_submit(self, kind, **kw):
            f = original_try_submit(self, kind, **kw)
            if f is not None:
                captured.append(f)
            return f
        # 重要: Future.result() を patch の内側で呼ぶ
        with _no_real_network(), \
             patch.object(MissionSupervisor, "try_submit", spy_try_submit), \
             patch.object(app.provider, "healthcheck", return_value="yfinance"), \
             patch.object(app.trade_loop.provider, "healthcheck",
                          return_value="yfinance"):
            app.scheduler.tick(NOW)
            assert captured, "no Future was captured"
            assert captured[0] is not None
            captured[0].result(timeout=5.0)  # Mission 完了まで待つ (patch 内側)
        rows = app.conn_core.execute("SELECT * FROM missions").fetchall()
        assert any(r["loop"] == "reflection" for r in rows)
        refl_rows = app.conn_core.execute("SELECT * FROM reflections").fetchall()
        assert len(refl_rows) == 1
    finally:
        app.supervisor.shutdown(drain_exc=RuntimeError("test shutdown"))


class _TradeThenBlockingReflectionRunner:
    """trade/ask Mission (`mission.tools` が非空) は即座に completed を
    返し、reflection Mission (`mission.tools == []` — `ReflectionCycle` の
    `_SCHEMA` 構築点のみがこの形) では `release` されるまでブロックする
    (レビュー 1 周目 F5 の pin 専用)。"""

    def __init__(self) -> None:
        self.missions: list = []
        self.entered_reflection = threading.Event()
        self.release = threading.Event()

    def run(self, mission):
        self.missions.append(mission)
        if mission.tools:
            return MissionResult(
                "completed", {"action": "hold", "reasoning": "w"}, [])
        self.entered_reflection.set()
        assert self.release.wait(10.0), "release が来なかった"
        return MissionResult("completed", {"content": "振り返り"}, [])


def test_reflection_fn_wiring_does_not_hold_core_lock_during_run(tmp_path):
    """レビュー 1 周目 F5 (指揮者の変異スイープで SURVIVED を実測):
    `service.py` の `_reflection_fn` に `with core_lock:` を戻しても
    `uv run pytest -q` が全 1661 件緑のままだった — 「`ReflectionCycle`
    自身が prepare/commit-core でのみ lock を掴み、配線側 (`_reflection_fn`)
    はそれを丸ごと再度 lock で包まない」という本 task の眼目が配線レベル
    では無防備だった。

    `build_app` で組み立てた実際の `App` を使い、supervisor 経由で
    trade+reflection 連鎖を起動し、reflection Mission が `runner.run()`
    でブロックしている最中に **別スレッド (このテスト自身のメイン
    スレッド、Mission は supervisor スレッドで動く)** から
    `app.core_lock` を取得できることを確認する。取得できなければ
    `_reflection_fn` が再び `with core_lock:` で丸ごと包んでいる
    (regression)。"""
    _init(tmp_path)
    from agentic_fx.core.accounting import record_snapshot
    from agentic_fx.store import orders as orders_store

    runner = _TradeThenBlockingReflectionRunner()
    app = build_app(tmp_path, runner=runner, clock=FixedClock(NOW))
    record_snapshot(app.conn_core, now=NOW, balance=1_000_000,
                    equity=1_000_000)
    orders_store.insert(
        app.conn_core, pair="USDJPY", direction="long", entry_type="market",
        horizon="day", status="closed", now=NOW, quantity=0.1,
        avg_fill_price=148.0, close_price=149.0, realized_pnl=100.0,
        close_reason="tp")

    with _no_real_network(), \
         patch.object(app.provider, "healthcheck", return_value="yfinance"), \
         patch.object(app.trade_loop.provider, "healthcheck",
                      return_value="yfinance"):
        app.supervisor.start()
        try:
            future = app.supervisor.try_submit("trade", trigger="cron")
            assert future is not None, "supervisor がジョブを受理しなかった"
            assert runner.entered_reflection.wait(5.0), (
                "reflection の run 相 (runner.run) に到達しなかった")

            acquired = app.core_lock.acquire(timeout=1.0)
            if acquired:
                app.core_lock.release()
            assert acquired, (
                "reflection の run 相の間、別スレッドから app.core_lock を"
                "取得できるはず (_reflection_fn が with core_lock: で丸ごと"
                "包んでいないこと — プラン8 Task 16 配線レベルの pin)")

            runner.release.set()
            future.result(timeout=10.0)
        finally:
            runner.release.set()  # 万一まだ待っていてもテストをハングさせない
            app.supervisor.shutdown(drain_exc=RuntimeError("test shutdown"))
            app.supervisor.join(timeout=5.0)


def test_tick_propagates_trigger_to_missions_row(tmp_path):
    """tick → on_trade_mission(reason) → run_once(trigger) → missions.trigger。

    この配線は wrapper が引数を捨てても各層の単体テストでは緑のままに
    なるため、tick 起点で通しで検証する (codex レビュー 1-4)。
    Task 13 fix (coordinator指摘): trigger の伝搬を assert し、Future 待ちを patch 内側に。
    """
    _init(tmp_path)
    fake = FakeRunner([MissionResult("completed",
                                     {"action": "hold", "reasoning": "w"},
                                     [])])
    app = build_app(tmp_path, runner=fake, clock=FixedClock(NOW))
    from agentic_fx.core.accounting import record_snapshot
    record_snapshot(app.conn_core, now=NOW, balance=1_000_000,
                    equity=1_000_000)
    # tick は on_news_cycle/on_econ_cycle 経由で本物の NewsCollector.collect /
    # EconCalendar.refresh を呼ぶため、_no_real_network() を挟まないと実 HTTP
    # (RSS フィード・ForexFactory カレンダー) が発火する (実測: 数秒のブレ —
    # グローバル制約「実 HTTP を混入させない」への抵触)。assert 対象の
    # trigger 伝搬とは無関係な経路なので、遮断してもテストの意図は弱まらない。
    # プラン 8 Task 13: on_trade_mission は supervisor 経由で非同期実行される
    # ため、supervisor を起動してから tick を呼ぶ必要がある。
    app.supervisor.start()
    try:
        # spy: supervisor.try_submit() が返す Future をキャプチャする
        from agentic_fx.core.supervisor import MissionSupervisor
        captured = []
        original_try_submit = MissionSupervisor.try_submit
        def spy_try_submit(self, kind, **kw):
            f = original_try_submit(self, kind, **kw)
            if f is not None:
                captured.append(f)
            return f
        # 重要: Future.result() を patch の内側で呼ぶ
        # (非同期実行なので patch が効いている間に完了させる)
        with _no_real_network(), \
             patch.object(MissionSupervisor, "try_submit", spy_try_submit), \
             patch.object(app.provider, "healthcheck", return_value="yfinance"), \
             patch.object(app.trade_loop.provider, "healthcheck",
                          return_value="yfinance"):
            app.scheduler.tick(NOW)
            # Future が返されたことを確認
            assert captured, "no Future was captured (on_trade_mission not called)"
            assert captured[0] is not None, "Supervisor rejected the job"
            # job 完了を待つ (patch の効いている内側で)
            result = captured[0].result(timeout=5.0)

        # job 実行後に mission row が作られているか確認
        # trigger が "cron" であることを assert (C1 本体)
        mission_row = app.conn_core.execute(
            "SELECT * FROM missions WHERE loop='trade' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert mission_row is not None, "Mission was not created in database"
        assert mission_row["trigger"] == "cron", \
            f"trigger was {mission_row['trigger']!r}, expected 'cron'"
    finally:
        app.supervisor.shutdown(drain_exc=RuntimeError("test shutdown"))


# ---- プラン 7 Task 8 fix round 1 F1: service.py の signal 実配線 --------
#
# sonnet の実証: service.py から D2 条件を削除しても、起動時 reclaim を
# 削除しても、テストは全緑のままだった (scheduler/trade_loop の単体テスト
# は service の closure をミラーした local combinator を使っているため —
# 「単体が緑でも配線が誰からも呼ばれない」欠陥クラス)。ここでは
# `build_app` が返す実 App (実 Scheduler・実 on_signal_maintenance・実
# signal_due_fn・実 SignalProducer) を通しで検証する。実サブプロセス
# (PluginSession) だけは `test_signal_eval.py` と同じ流儀で fake に差し替え
# る (sandbox 起動の健全性自体は Task 2 の関心)。


class _FakeSignalSession:
    """`plugin_sandbox.PluginSession` の代わりに注入する fake (F1(a))。
    `signal_producer.py` の `sandbox_run=None` (本番既定) パスが実際に
    使うクラスをそのまま差し替える — sandbox_run 引数の注入シームは
    service.py の配線には存在しない (常に None) ため、これが唯一の seam。
    """

    def __init__(self, meta, *, settings) -> None:
        del meta, settings

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def call(self, payload: dict) -> dict:
        del payload
        return {"signals": [{"direction": "long", "strength": 0.8,
                             "rationale": "up"}]}

    def close(self) -> None:
        pass


def _seed_1m(conn, source: str, start, minutes: int, *, price: float = 100.0,
            symbol: str = "USDJPY") -> None:
    from agentic_fx.core.contracts import Bar
    from agentic_fx.store import ohlcv as ohlcv_store
    # 呼び出し側は producer_source (ライブ) を渡すので**キャッシュ側**へ書く
    # (プラン 9 Task 16 の分割以降、ライブ source は履歴 API が拒否する)。
    bars = [Bar(symbol, "1m", start + timedelta(minutes=i),
                price, price, price, price, 10.0) for i in range(minutes)]
    ohlcv_store.upsert_cache_bars(conn, bars, source=source)


def test_f1a_signal_maintenance_wiring_inserts_rows_via_real_tick(tmp_path):
    """F1(a): 承認済み plugin + settings.pairs 内のデータを仕込み、実
    `scheduler.tick()` を 1 回呼ぶと producer が実際に評価され signals
    テーブルへ行が挿入されること (on_signal_maintenance の実配線)。"""
    from agentic_fx.core.accounting import record_snapshot
    from agentic_fx.plugin.loader import PluginMeta

    _init(tmp_path)
    meta = PluginMeta(name="sig1", kind="signal", path=Path("/nonexistent"),
                      params={}, timeframe="1h", pairs=("USDJPY",),
                      max_bars=50, content_hash="h" * 64)

    with patch("agentic_fx.service.plugin_loader.approved_plugins",
              return_value=[meta]), \
         patch("agentic_fx.plugin.signal_producer.plugin_sandbox.PluginSession",
              _FakeSignalSession):
        app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                        embedding_fn=FakeEmbedding())
        record_snapshot(app.conn_core, now=NOW, balance=1_000_000,
                        equity=1_000_000)
        _seed_1m(app.conn_core, app.settings.plugin.producer_source,
                NOW - timedelta(hours=3), 3 * 60 + 1)
        with _no_real_network(), \
             patch.object(app.provider, "healthcheck", return_value="yfinance"):
            app.scheduler.tick(NOW)

    count = app.conn_core.execute(
        "SELECT COUNT(*) c FROM signals").fetchone()["c"]
    assert count >= 1


def test_f1b_signal_mission_does_not_fire_without_d2_position(tmp_path):
    """F1(b): open/pending_fill の注文が皆無 (D2 不成立) だと、pending
    signal があっても signal トリガーの trade mission が起動しないこと。"""
    from agentic_fx.store import signals as signals_store

    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                    embedding_fn=FakeEmbedding())
    from agentic_fx.core.accounting import record_snapshot
    record_snapshot(app.conn_core, now=NOW, balance=1_000_000,
                    equity=1_000_000)
    signals_store.add(app.conn_core, plugin="sig1", content_hash="h1",
                      pair="USDJPY", timeframe="1h",
                      bar_ts=(NOW - timedelta(hours=1)).isoformat(),
                      kind="signal", payload={"direction": "long"}, now=NOW)
    app.scheduler._last_cron_trade = NOW  # cron を抑制し signal 経路だけ見る
    app.trade_loop.run_once = MagicMock(wraps=app.trade_loop.run_once)

    with _no_real_network(), \
         patch.object(app.provider, "healthcheck", return_value="yfinance"):
        app.scheduler.tick(NOW + timedelta(minutes=5))

    app.trade_loop.run_once.assert_not_called()  # D2 不成立で起動しない


def test_f1c_startup_reclaim_recovers_claimed_signal(tmp_path):
    """F1(c): 停止時に claimed のまま残った signal 行が、次の build_app
    (= 次回起動) 直後、tick を待たずに pending へ回収されること。"""
    from agentic_fx.store import missions as missions_module
    from agentic_fx.store import signals as signals_store

    _init(tmp_path)
    app1 = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                     embedding_fn=FakeEmbedding())
    sid = signals_store.add(
        app1.conn_core, plugin="sig1", content_hash="h1", pair="USDJPY",
        timeframe="1h", bar_ts=(NOW - timedelta(hours=1)).isoformat(),
        kind="signal", payload={"direction": "long"}, now=NOW)
    # Task 13: claimed_by_mission_id に missions(id) への FK が付くため、
    # 実在する mission 行が必要 (以前はダミー整数 999 だった)。
    mid = missions_module.start(app1.conn_core, "trade", "local", "m", NOW)
    lease_min = app1.settings.plugin.signal_lease_min
    old = NOW - timedelta(minutes=lease_min + 5)
    claimed = signals_store.claim_oldest(app1.conn_core, mission_id=mid,
                                         now=old, freshness_bars=None)
    assert claimed is not None and claimed["id"] == sid  # 前提

    # 起動時 recover_interrupted が claim を先に戻さないよう mission を終端化。
    # これにより次回起動で pending 化する唯一の主体が lease 回収になる。
    # ⚠️ `missions.finish` は 6 引数必須 (conn, mission_id, status,
    #    output, transcript, now)。既定値は無い (`store/missions.py:23-24`)。
    #    4 引数だと NOW が output に束縛され TypeError で落ちる (3 周目レビュー)。
    missions_module.finish(app1.conn_core, mid, "completed", None, [], NOW)

    # FC-2 (プラン8): instance_lock (flock) は App の全寿命で保持される
    # ため、同一 root への 2 回目の build_app は 1 回目の instance_lock を
    # 解放してからでないと InstanceAlreadyRunning になる。「再起動」を
    # 模す以上、1 回目のプロセスが終了して lock を手放したことも模す
    # 必要がある (App.close() への instance_lock 配線は Task 19)。
    app1.instance_lock.close()

    # 「再起動」を模して同じ DB に対しもう一度 build_app する
    app2 = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                     embedding_fn=FakeEmbedding())

    row = app2.conn_core.execute(
        "SELECT status FROM signals WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "pending"  # 起動時 reclaim が回収した


# ---- 上書き 3: MissionWatch は 1 インスタンスを共有注入 --------------------

def test_mission_watch_is_shared_single_instance(tmp_path):
    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
    assert app.trade_loop.watch is app.mission_watch
    assert app.reflection.watch is app.mission_watch


# ---- 上書き 4: 起動時 schema 検証 + pairs 非空 ------------------------------

def test_validate_startup_rejects_empty_pairs():
    stub = MagicMock()
    stub.pairs = []
    with pytest.raises(RuntimeError):
        _validate_startup(stub)


def test_validate_startup_accepts_real_settings(tmp_path):
    _init(tmp_path)
    from agentic_fx.config import load_settings
    settings = load_settings(tmp_path / "config" / "settings.yaml")
    _validate_startup(settings)  # 例外を出さない


# ---- 上書き 5: wiring assert ------------------------------------------------

def test_assert_tools_registered_raises_on_missing():
    from agentic_fx.tools.registry import ToolRegistry
    registry = ToolRegistry()
    with pytest.raises(RuntimeError, match="get_ohlcv"):
        _assert_tools_registered(registry, ["get_ohlcv", "search_news"])


def test_assert_tools_registered_passes_when_complete():
    from agentic_fx.tools.registry import ToolDef, ToolRegistry
    registry = ToolRegistry()
    registry.register(ToolDef("get_ohlcv", "d", {"type": "object"},
                              lambda: None))
    _assert_tools_registered(registry, ["get_ohlcv"])  # 例外なし


# ---- 上書き 6: reflection_tools の本番配線 (pairs enum) ---------------------

def test_registry_pair_enum_matches_settings_pairs(tmp_path):
    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
    tools = app.registry.openai_tools(["get_ohlcv"])
    params = tools[0]["function"]["parameters"]
    assert params["properties"]["pair"]["enum"] == list(app.settings.pairs)


# ---- 上書き 7: runner close の所有権 ----------------------------------------

def test_owns_runner_false_when_injected(tmp_path):
    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
    assert app.owns_runner is False


def test_owns_runner_true_when_built_locally(tmp_path):
    _init(tmp_path)
    app = build_app(tmp_path, clock=FixedClock(NOW))
    assert app.owns_runner is True
    assert isinstance(app.runner, WorkerRunner)
    app.runner.close()


# ---- 上書き 2: _check_llama_swap (3 分岐 x 成功/失敗) -----------------------

class _LlamaSwapStub:
    base_url = "http://localhost:8080/v1"


class _RunnerChoiceStub:
    model = "qwen3.6-35b"


class _RunnerStub:
    trade = _RunnerChoiceStub()
    improve = _RunnerChoiceStub()  # 既定は trade と同一モデル (既存 6 テストの前提を変えない)


class _StubSettings:
    llama_swap = _LlamaSwapStub()
    runner = _RunnerStub()


class _RunnerChoiceStubTrade:
    model = "trade-m"


class _RunnerChoiceStubImprove:
    model = "improve-m"


class _RunnerStubDiff:
    trade = _RunnerChoiceStubTrade()
    improve = _RunnerChoiceStubImprove()


class _StubSettingsDiff:
    llama_swap = _LlamaSwapStub()
    runner = _RunnerStubDiff()


class _LlamaSwapStubTrailingSlash:
    base_url = "http://localhost:8080/v1/"


class _StubSettingsTrailingSlash:
    llama_swap = _LlamaSwapStubTrailingSlash()
    runner = _RunnerStub()


class _LlamaSwapStubNoV1:
    # 1 周目 codex I3: /v1 を含まない base_url。`endswith("/v1")` の**偽側**を
    # 踏む唯一のスタブ (他は全て /v1 終端)。
    base_url = "http://localhost:8080"


class _StubSettingsNoV1:
    llama_swap = _LlamaSwapStubNoV1()
    runner = _RunnerStub()


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# F1 (fix round 1): 6 分岐テスト全部で httpx.get / httpx.post の**両方**を必ず
# patch する。model_missing 分岐を掃引したところ、`if model not in ids:` を
# `if False:` に変異させると、post 未 patch のテストでは実際に httpx.post が
# 実行され (llama-swap 稼働環境なら 404 → smoke 警告、未稼働なら
# ConnectError → 同)、いずれも警告文にモデル名が含まれるため assert が
# 誤って通っていた (SURVIVED 実測)。「呼ばれてはならない分岐で post が
# 呼ばれたら即 AssertionError にする」ことで、ネットワーク到達を構造的に
# 遮断しつつ、誤って post まで到達する変異を検出できるようにする。
# 加えて各テストの assert には**分岐固有の文言**を必須にする。


def _forbidden_post(branch: str):
    return patch("httpx.post", side_effect=AssertionError(
        f"httpx.post は {branch} 分岐では呼ばれてはならない"))


def test_check_llama_swap_ok(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.6-35b"}]})
        return httpx.Response(200, json={})
    client = _mock_client(handler)
    with patch("httpx.get", client.get), patch("httpx.post", client.post):
        _check_llama_swap(_StubSettings())
    out = capsys.readouterr().out
    assert "OK" in out and "qwen3.6-35b" in out
    assert "存在しません" not in out and "smoke" not in out and "一覧" not in out


def test_check_llama_swap_model_missing(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "other-model"}]})
    client = _mock_client(handler)
    with patch("httpx.get", client.get), _forbidden_post("model_missing"):
        _check_llama_swap(_StubSettings())
    out = capsys.readouterr().out
    assert "qwen3.6-35b" in out and "警告" in out
    assert "存在しません" in out  # 分岐固有の文言
    assert "OK" not in out


def test_check_llama_swap_list_connect_error(capsys):
    with patch("httpx.get", side_effect=httpx.ConnectError("refused")), \
         _forbidden_post("一覧取得失敗 (ConnectError)"):
        _check_llama_swap(_StubSettings())
    out = capsys.readouterr().out
    assert "警告" in out and "一覧" in out
    assert "OK" not in out and "smoke" not in out


def test_check_llama_swap_list_http_error(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)
    client = _mock_client(handler)
    with patch("httpx.get", client.get), \
         _forbidden_post("一覧取得失敗 (HTTPStatusError)"):
        _check_llama_swap(_StubSettings())
    out = capsys.readouterr().out
    # 一覧取得失敗の警告文であること (smoke 失敗の警告文と取り違えていないか
    # を区別する — "警告" だけの assert だと 3 分岐のどれでも通ってしまう)
    assert "警告" in out and "一覧" in out
    assert "OK" not in out and "smoke" not in out


def test_check_llama_swap_smoke_http_error(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.6-35b"}]})
        return httpx.Response(500)
    client = _mock_client(handler)
    with patch("httpx.get", client.get), patch("httpx.post", client.post):
        _check_llama_swap(_StubSettings())
    out = capsys.readouterr().out
    assert "警告" in out and "smoke" in out
    assert "OK" not in out and "存在しません" not in out and "一覧" not in out


def test_check_llama_swap_smoke_timeout(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.6-35b"}]})
        raise httpx.TimeoutException("timeout")
    client = _mock_client(handler)
    with patch("httpx.get", client.get), patch("httpx.post", client.post):
        _check_llama_swap(_StubSettings())
    out = capsys.readouterr().out
    assert "警告" in out and "smoke" in out
    assert "OK" not in out and "存在しません" not in out and "一覧" not in out


# ---- プラン9 Task5: n_ctx 可視化 (CP17〜21) ---------------------------------

def test_check_llama_swap_same_model_calls_props_once(capsys):
    """CP17: trade == improve なら /props は 1 回だけ呼ばれる。"""
    calls = {"props": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.6-35b"}]})
        if path == "/props":
            calls["props"] += 1
            return httpx.Response(200, json={
                "default_generation_settings": {"n_ctx": 65536}})
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    with patch("httpx.get", client.get), patch("httpx.post", client.post):
        _check_llama_swap(_StubSettings())
    assert calls["props"] == 1
    out = capsys.readouterr().out
    assert "ctx 65536" in out


def test_check_llama_swap_different_models_improve_first_trade_last(capsys):
    """CP18: trade != improve なら improve の /props が先 (表示のみ)、
    trade は存在確認→smoke→/props の順で**最後**に処理される。"""
    call_order: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "trade-m"}]})
        if path == "/props":
            model = request.url.params.get("model")
            call_order.append(("props", model))
            return httpx.Response(200, json={
                "default_generation_settings": {"n_ctx": 4096}})
        if path.endswith("/chat/completions"):
            # 1 周目 codex I1: smoke の**対象モデル**まで採る。順序だけを
            # 見ていると `json={"model": improve_model, ...}` への変異が
            # 生存し (実測 1801 passed)、init 終了時に hot なのが trade で
            # なく improve になる — 設計書 §4.4 の中核が破れる。
            call_order.append(("smoke", json.loads(request.content)["model"]))
            return httpx.Response(200, json={})
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    with patch("httpx.get", client.get), patch("httpx.post", client.post):
        _check_llama_swap(_StubSettingsDiff())

    # 種別と対象モデルを組で固定する (kinds だけだと I1 が生存する)
    assert call_order == [
        ("props", "improve-m"), ("smoke", "trade-m"), ("props", "trade-m")]
    assert capsys.readouterr().out == (
        "improve model 'improve-m' ctx 4096\n"
        "llama-swap OK (model 'trade-m' loaded, ctx 4096)\n")


def test_check_llama_swap_props_http_failure_does_not_fail_init(capsys):
    """CP19: /props の HTTP 失敗でも init は成功する (表示だけ省略)。"""
    calls = {"props": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.6-35b"}]})
        if path == "/props":
            calls["props"] += 1
            return httpx.Response(500)
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    with patch("httpx.get", client.get), patch("httpx.post", client.post):
        _check_llama_swap(_StubSettings())  # 例外を出さない = init は成功
    assert calls["props"] >= 1  # /props に実際に到達したことを確認
    out = capsys.readouterr().out
    assert "llama-swap OK" in out
    assert "ctx" not in out


@pytest.mark.parametrize("failure_kind", ["request", "value", "type"])
def test_check_llama_swap_props_expected_failure_is_not_displayed(
        capsys, failure_kind):
    """RequestError/ValueError/TypeError は init を落とさず表示を省略する。"""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.6-35b"}]})
        if path == "/props":
            if failure_kind == "request":
                raise httpx.ConnectError("props unavailable")
            if failure_kind == "value":
                return httpx.Response(200, content=b"{")
            return httpx.Response(200, json={
                "default_generation_settings": None})
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    with patch("httpx.get", client.get), patch("httpx.post", client.post):
        _check_llama_swap(_StubSettings())
    assert capsys.readouterr().out == (
        "llama-swap OK (model 'qwen3.6-35b' loaded)\n")


def test_check_llama_swap_props_ctx_wrong_type_is_not_displayed(capsys):
    """CP20 (str): n_ctx が文字列なら表示しない。init は成功する。"""
    calls = {"props": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.6-35b"}]})
        if path == "/props":
            calls["props"] += 1
            return httpx.Response(200, json={
                "default_generation_settings": {"n_ctx": "65536"}})
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    with patch("httpx.get", client.get), patch("httpx.post", client.post):
        _check_llama_swap(_StubSettings())
    assert calls["props"] >= 1
    out = capsys.readouterr().out
    assert "llama-swap OK" in out
    assert "ctx" not in out


def test_check_llama_swap_props_ctx_missing_is_not_displayed(capsys):
    """CP20 (欠落): n_ctx キーが無ければ表示しない。init は成功する。"""
    calls = {"props": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.6-35b"}]})
        if path == "/props":
            calls["props"] += 1
            return httpx.Response(200, json={"default_generation_settings": {}})
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    with patch("httpx.get", client.get), patch("httpx.post", client.post):
        _check_llama_swap(_StubSettings())
    assert calls["props"] >= 1
    out = capsys.readouterr().out
    assert "llama-swap OK" in out
    assert "ctx" not in out


def test_check_llama_swap_props_ctx_bool_is_not_displayed(capsys):
    """CP20 (bool): n_ctx が bool なら表示しない (「bool を除く正整数」)。"""
    calls = {"props": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.6-35b"}]})
        if path == "/props":
            calls["props"] += 1
            return httpx.Response(200, json={
                "default_generation_settings": {"n_ctx": True}})
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    with patch("httpx.get", client.get), patch("httpx.post", client.post):
        _check_llama_swap(_StubSettings())
    assert calls["props"] >= 1
    out = capsys.readouterr().out
    assert "llama-swap OK" in out
    assert "ctx" not in out


@pytest.mark.parametrize("n_ctx", [0, -1])
def test_check_llama_swap_props_ctx_non_positive_is_not_displayed(
        capsys, n_ctx):
    """n_ctx が 0 または負数なら表示しない。"""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.6-35b"}]})
        if path == "/props":
            return httpx.Response(200, json={
                "default_generation_settings": {"n_ctx": n_ctx}})
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    with patch("httpx.get", client.get), patch("httpx.post", client.post):
        _check_llama_swap(_StubSettings())
    assert capsys.readouterr().out == (
        "llama-swap OK (model 'qwen3.6-35b' loaded)\n")


def test_check_llama_swap_props_trailing_slash_base_uses_root(capsys):
    """base_url が /v1/ 終端でも root /props から n_ctx を表示する。"""
    calls = {"props": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.6-35b"}]})
        if path == "/props":
            calls["props"] += 1
            return httpx.Response(200, json={
                "default_generation_settings": {"n_ctx": 32768}})
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    with patch("httpx.get", client.get), patch("httpx.post", client.post):
        _check_llama_swap(_StubSettingsTrailingSlash())
    assert calls["props"] == 1
    assert capsys.readouterr().out == (
        "llama-swap OK (model 'qwen3.6-35b' loaded, ctx 32768)\n")


def test_check_llama_swap_props_timeout_budget_differs_cold_vs_hot(capsys):
    """2 周目レビュー: `/props` は**モデルをロードさせる**ので、cold な
    improve と hot な trade で必要な予算が 4 桁違う。

    実測 (2026-08-12, :8080): qwen3.6-35b-a3b_Q4 は cold 13.86s / hot 0.0005s。
    全経路 timeout=5 だった元実装では improve 側 (常に cold) が必ず
    ReadTimeout → None となり、improve の ctx 行が **trade != improve という
    この機能唯一の対象構成で永久に出なかった**。

    MockTransport は即答するので経過時間では測れない。**予算そのものが契約**
    なので `request.extensions["timeout"]` を直接 assert する。

    3 周目レビュー: 当初は `/props` の 2 箇所しか採っておらず、**cold load
    本体である smoke 自身の `timeout` が pin から漏れていた** — 120 → 5 に
    縮める変異が 1808 passed のまま生存した。cold load を跨ぎうる要求は
    smoke を含めて 1 つの予算 (`_COLD_LOAD_TIMEOUT`) に束ねてあるので、
    **3 要求すべてを 1 本の列で固定する**。
    """
    budgets: list[tuple[str, str | None, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        read_budget = request.extensions["timeout"]["read"]
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "trade-m"}]})
        if path == "/props":
            budgets.append(("props", request.url.params.get("model"),
                            read_budget))
            return httpx.Response(200, json={
                "default_generation_settings": {"n_ctx": 4096}})
        if path.endswith("/chat/completions"):
            budgets.append(("smoke", json.loads(request.content)["model"],
                            read_budget))
            return httpx.Response(200, json={})
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    with patch("httpx.get", client.get), patch("httpx.post", client.post):
        _check_llama_swap(_StubSettingsDiff())

    # 種別 × 対象 × 予算 を組で固定する。cold を踏む 2 要求 (improve /props と
    # smoke) が同額であること、hot な trade /props だけが短いことまで見る。
    assert budgets == [
        ("props", "improve-m", 120),
        ("smoke", "trade-m", 120),
        ("props", "trade-m", 5)]


def test_check_llama_swap_props_base_without_v1_is_not_truncated(capsys):
    """1 周目 codex I3: base_url が /v1 で終わらないとき `[:-3]` を**しない**。

    既存スタブは全て /v1 終端なので `endswith("/v1")` の偽側が一度も踏まれず、
    ガードを外して常に `[:-3]` する変異が生存する (実測 1801 passed)。
    その変異下では http://localhost:8080 が http://localhost: に化ける。
    """
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.6-35b"}]})
        if path == "/props":
            calls.append(str(request.url))
            return httpx.Response(200, json={
                "default_generation_settings": {"n_ctx": 16384}})
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    with patch("httpx.get", client.get), patch("httpx.post", client.post):
        _check_llama_swap(_StubSettingsNoV1())
    # host:port が切り詰められていないことまで見る (パス一致だけだと生存する)
    assert calls == ["http://localhost:8080/props?model=qwen3.6-35b"]
    assert capsys.readouterr().out == (
        "llama-swap OK (model 'qwen3.6-35b' loaded, ctx 16384)\n")


def test_check_llama_swap_unexpected_exception_in_props_is_not_swallowed():
    """CP21: RequestError/HTTPStatusError/ValueError/TypeError/KeyError の
    いずれでもない想定外例外は握らず、_check_llama_swap を通じて呼び出し
    元まで伝播する (init は落ちる)。"""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.6-35b"}]})
        if path == "/props":
            raise RuntimeError("unexpected boom")
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    with patch("httpx.get", client.get), patch("httpx.post", client.post), \
         pytest.raises(RuntimeError, match="unexpected boom"):
        _check_llama_swap(_StubSettings())


# ---- F4 (fix round 1): run_service の shutdown 経路 -------------------------
#
# 上書き 3 は「watchdog スレッド固有の配線は E2E で検証しない (スリープ依存
# テストを作らない)」としているが、shutdown シーケンス自体はこの免除の対象
# 外 (owns_runner ガード・runner.close() の呼び分けが壊れても既存テストは
# 全て緑のまま — 実測 SURVIVED)。`run_service(..., _stop_event=...)` の
# テスト用シームを使い、実スリープ・実シグナル・実 HTTP なしで検証する。
# (`_seam_app` / `_no_real_network` はファイル冒頭に定義 — 前者はここでのみ
# 使うが、後者は `test_tick_propagates_trigger_to_missions_row` からも使う)

def test_run_service_daemon_graceful_shutdown_with_injected_runner(tmp_path):
    """F4-①: 事前 set 済み stop_event + daemon=True で即座に graceful
    shutdown する。owns_runner=False (runner 注入) のため、close 未実装の
    FakeRunner でも close() が呼ばれてはならない — owns_runner ガードが
    `if True:` のように壊れると `FakeRunner` に `close` 属性が無く
    `AttributeError` でこのテスト自体が落ちる (仕様上のピン)。"""
    fake = FakeRunner([])
    app = _seam_app(tmp_path, fake)
    assert app.owns_runner is False
    stop_event = threading.Event()
    stop_event.set()  # 実スリープなしで即座に shutdown 経路へ入る
    with _no_real_network(), \
         patch("agentic_fx.service.build_app", return_value=app), \
         patch("agentic_fx.service.signal.signal"):
        rc = run_service(tmp_path, daemon=True, _stop_event=stop_event)
    assert rc == 0
    act = (tmp_path / "logs" / "activity.log").read_text(encoding="utf-8")
    assert "service_stopped" in act and "graceful" in act


def test_run_service_closes_owned_runner_on_graceful_shutdown(tmp_path):
    """F4-②: owns_runner=True 相当 (`WorkerRunner` の spec を持つ mock に
    差し替え)。graceful shutdown で close() が 1 回だけ呼ばれること。"""
    app = _seam_app(tmp_path, FakeRunner([]))
    mock_runner = MagicMock(spec=WorkerRunner)
    app.runner = mock_runner
    app.owns_runner = True
    stop_event = threading.Event()
    stop_event.set()
    with _no_real_network(), \
         patch("agentic_fx.service.build_app", return_value=app), \
         patch("agentic_fx.service.signal.signal"):
        rc = run_service(tmp_path, daemon=True, _stop_event=stop_event)
    assert rc == 0
    mock_runner.close.assert_called_once()
    act = (tmp_path / "logs" / "activity.log").read_text(encoding="utf-8")
    assert "service_stopped" in act and "graceful" in act


def test_run_service_derives_supervisor_join_timeout_from_dispatch_ceiling(
        tmp_path):
    """レビュー 3 周目 codex E2: `supervisor.join` の budget は
    `shutdown_join_timeout_sec` (Mission の timeout と無関係な固定 30 秒)
    ではなく、`llama_swap.timeout_sec + worker_grace_sec +
    worker_terminate_grace_sec + 10.0` (`ask_wait_timeout_sec` と同じ
    導出) から計算されることを直接確認する。"""
    app = _seam_app(tmp_path, FakeRunner([]))
    app.settings.llama_swap.timeout_sec = 42.0
    app.settings.worker.worker_grace_sec = 3.0
    app.settings.worker.worker_terminate_grace_sec = 2.0
    # 使われないはずの旧 knob — 意図的にかけ離れた値にしておき、これが
    # 使われていたら即座に露見するようにする。
    app.settings.worker.shutdown_join_timeout_sec = 999.0

    recorded: dict = {}
    original_join = app.supervisor.join

    def spy_join(timeout=None):
        recorded["timeout"] = timeout
        return original_join(timeout=timeout)

    app.supervisor.join = spy_join

    stop_event = threading.Event()
    stop_event.set()
    with _no_real_network(), \
         patch("agentic_fx.service.build_app", return_value=app), \
         patch("agentic_fx.service.signal.signal"):
        rc = run_service(tmp_path, daemon=True, _stop_event=stop_event)

    assert rc == 0
    assert recorded.get("timeout") == pytest.approx((42.0 + 3.0 + 2.0) * 4 + 60.0)


def test_scheduler_tick_actually_invokes_the_watchdog_health_check(tmp_path):
    """相互監視の scheduler→watchdog 方向が**実際に配線されている**ことを
    確認する (2周目 ローカル LLM 指摘 — 指揮者が変異で CONFIRMED)。

    `_check_watchdog_health` 自体の分岐と、それを囲む停止中ガードには
    ピンがあったが、**`scheduler_thread` からの呼び出しを丸ごと削除しても
    全件 1721 passed** だった。設計書 §6 の相互監視は「watchdog が
    scheduler/supervisor を見る」方向だけでは片肺で、watchdog 自身が静かに
    止まったときの検出手段が完全に失われる。
    """
    app = _seam_app(tmp_path, FakeRunner([]))
    stop_event = threading.Event()
    calls: list = []

    def recorder(app_, watchdog_obj, stop_, **kwargs):
        calls.append(watchdog_obj)
        stop_.set()   # 1 回観測したら停止させる

    # **バックストップ**: 配線が消えている変異では recorder が呼ばれず
    # stop_event が永久に立たない。ハングは red より悪い (原因が読めない)
    # ので、必ず停止させて assert で落ちるようにする。
    backstop = threading.Timer(5.0, stop_event.set)
    backstop.start()
    try:
        with _no_real_network(), \
             patch("agentic_fx.service.build_app", return_value=app), \
             patch("agentic_fx.service.signal.signal"), \
             patch("agentic_fx.service._check_watchdog_health", recorder):
            run_service(tmp_path, daemon=True, _stop_event=stop_event)
    finally:
        backstop.cancel()

    assert calls, (
        "scheduler tick が watchdog の健全性チェックを呼んでいない "
        "(設計書 §6 の相互監視が片方向になっている)")
    assert isinstance(calls[0], threading.Thread), (
        "watchdog スレッドそのものが渡されていない")


def test_watchdog_ceiling_and_join_budget_come_from_the_same_value(tmp_path):
    """裁定 B の順序関係 (join budget >= watchdog の dispatch ceiling) を
    実際の呼び出しで固定する。

    (レビュー1周目 I-3) 旧ピンは `_default_dispatch_ceiling_sec` を 2 回
    呼んで比較する**恒真テスト**で、watchdog 側の呼び出しを
    `dispatch_ceiling_sec=30.0` に書き換える変異が全件緑のまま生存した
    (指揮者が実測)。**join budget が ceiling より小さいと main が先に join
    を諦め、watchdog の `busy_since` 軸が構造的に到達不能になり
    `fatal_reason` がその経路で永久にラッチしない。**

    あわせて「watchdog は最初のチェックを 30 秒待たずに行う」ことも固定
    する — 待ちが先だと起動直後 30 秒はスレッド死亡を検出できない盲窓に
    なる (このテストが 30 秒でタイムアウトしないこと自体がその pin)。
    """
    app = _seam_app(tmp_path, FakeRunner([]))
    app.settings.llama_swap.timeout_sec = 42.0
    app.settings.worker.worker_grace_sec = 3.0
    app.settings.worker.worker_terminate_grace_sec = 2.0

    stop_event = threading.Event()
    recorded: dict = {}
    original_join = app.supervisor.join

    def spy_join(timeout=None):
        recorded["join_budget"] = timeout
        return original_join(timeout=timeout)

    app.supervisor.join = spy_join

    def fake_watchdog_check(app_, thread_, stop_, **kwargs):
        recorded.setdefault("ceiling", kwargs.get("dispatch_ceiling_sec"))
        stop_.set()   # 1 回観測したら停止させる

    # バックストップ (上の配線テストと同じ理由): 配線が消えた変異で
    # ハングさせず、assert で落とす。
    backstop = threading.Timer(5.0, stop_event.set)
    backstop.start()
    try:
        with _no_real_network(), \
             patch("agentic_fx.service.build_app", return_value=app), \
             patch("agentic_fx.service.signal.signal"), \
             patch("agentic_fx.service._watchdog_check", fake_watchdog_check):
            run_service(tmp_path, daemon=True, _stop_event=stop_event)
    finally:
        backstop.cancel()

    assert recorded.get("ceiling") is not None, (
        "watchdog が dispatch_ceiling_sec を明示的に受け取っていない "
        "(join budget と独立に既定値へ落ちると順序関係を保証できない)")
    assert recorded["join_budget"] >= recorded["ceiling"], (
        f"join budget ({recorded['join_budget']}) が watchdog の "
        f"dispatch ceiling ({recorded['ceiling']}) より小さい — "
        "main が先に join を諦めるため busy_since 軸が到達不能になる")


def test_run_service_records_shutdown_timeout_when_commit_core_is_stuck(tmp_path):
    """レビュー 2 周目 codex D1 (Critical): プラン8 Task 15 で run 相/
    commit-pre/commit-post が core_lock を保持しなくなったため、scheduler
    スレッドは Mission (commit-core を含む) が supervisor スレッドで実行中
    でも即座に tick を終えて `th.join(30)` が成功しうる — 旧コメント
    「tick は core_lock 下で走るため join 完了 = 実行中 Mission も完了」は
    もう成立しない。Mission (commit-core を含む) を実際に実行しているのは
    supervisor スレッドなので、その shutdown/join も判定に加わったことで、
    commit-core が途中で止まっている状況では graceful ではなく
    shutdown_timeout が記録されることを確認する。

    レビュー 3 周目 codex E2 の pin も兼ねる: shutdown_timeout でも
    (子プロセス/接続の leak を防ぐため) `runner.close()` が呼ばれること。
    """
    from agentic_fx.core.accounting import record_snapshot

    entered = threading.Event()
    release = threading.Event()

    fake = FakeRunner([MissionResult(
        "completed", {"action": "hold", "reasoning": "x"}, [])])
    fake.close = MagicMock()  # codex E2 pin: timeout 時も close() されること
    app = _seam_app(tmp_path, fake)
    app.owns_runner = True
    # レビュー 3 周目 codex E2: supervisor.join の budget は
    # `shutdown_join_timeout_sec` (廃止) ではなく
    # `llama_swap.timeout_sec + worker_grace_sec +
    # worker_terminate_grace_sec + 10.0` から導出されるようになった。
    # テストを高速化するため Mission timeout 側を縮める (budget は
    # 0.1+0.1+0.1+10.0 = 10.3 秒 — 固定マージン 10 秒はコード側の定数
    # なので設定では縮められない)。
    app.settings.llama_swap.timeout_sec = 0.1
    app.settings.worker.worker_grace_sec = 0.1
    app.settings.worker.worker_terminate_grace_sec = 0.1
    record_snapshot(app.conn_core, now=NOW, balance=1_000_000,
                    equity=1_000_000)

    original = app.trade_loop.executor.record_and_validate_intent

    def spy(*args, **kwargs):
        entered.set()
        # budget (約 10.3 秒) より長く留まり続け、shutdown 判定が下る前に
        # spy 自身が自然完了してしまわないようにする。
        assert release.wait(30.0), "release が来なかった"
        return original(*args, **kwargs)

    app.trade_loop.executor.record_and_validate_intent = spy

    with patch.object(app.trade_loop.provider, "healthcheck",
                      return_value="yfinance"), _no_real_network(), \
         patch("agentic_fx.service._default_dispatch_ceiling_sec",
               return_value=0.1):
        # supervisor を先に起動し、commit-core の最中 (spy でブロック) に
        # 留めておく — run_service に入る前に「実行中 Mission」の状況を
        # 作る。run_service は内部で app.supervisor.start() を呼ぶため、
        # 二重起動 (別スレッドで queue を奪い合う) を避けて no-op に
        # 差し替える。
        app.supervisor.start()
        future = app.supervisor.try_submit("trade", trigger="cron")
        assert future is not None, "supervisor がジョブを受理しなかった"
        assert entered.wait(5.0), (
            "commit-core (record_and_validate_intent) に到達しなかった")
        app.supervisor.start = lambda: None

        stop_event = threading.Event()
        stop_event.set()  # 実スリープなしで即座に shutdown 経路へ入る
        with patch("agentic_fx.service.build_app", return_value=app), \
             patch("agentic_fx.service.signal.signal"):
            rc = run_service(tmp_path, daemon=True, _stop_event=stop_event)

    assert rc == 1, "commit-core 実行中にも関わらず正常終了 (0) している"
    act = (tmp_path / "logs" / "activity.log").read_text(encoding="utf-8")
    assert "shutdown_timeout" in act, (
        "commit-core 実行中にも関わらず graceful と記録されている")
    # codex E2 pin: shutdown_timeout でも runner.close() は呼ばれる
    # (子プロセス/接続の leak を防ぐ — 以前は join 成功時のみ close していた)
    fake.close.assert_called_once()

    release.set()
    future.result(timeout=5.0)
    app.supervisor.join(timeout=5.0)
    # レビュー 3 周目 (KAT-Coder): 後始末が本当に効いているかを assert する。
    # スレッドが残ると後続テストを汚染するが、join のタイムアウトは黙って
    # 通り過ぎるため、生死を明示的に確かめる。
    assert not app.supervisor.is_alive(), (
        "supervisor スレッドがテスト終了後も生存している (後続テストを汚染する)")


def test_run_service_rejects_try_submit_during_shutdown_before_scheduler_exits(
        tmp_path):
    """レビュー 3 周目 codex E3: `supervisor.shutdown()` は `th.join()`
    より**前**に呼ぶ — stop_event.set() の時点で既に走っていた scheduler
    tick は on_trade_mission → supervisor.try_submit まで到達しうるため、
    shutdown() (新規受付停止) が後回しだと、停止処理の最中に新しい
    trade+reflection Mission が受理されてしまう (それが timeout として
    報告される)。

    scheduler tick を任意の時点でブロックできるようにし、`th.join()` が
    実際にブロック中の間に `try_submit` が既に拒否される (=
    shutdown() が th.join() より前に実行済み) ことを確認する。
    """
    tick_entered = threading.Event()
    tick_release = threading.Event()

    def blocking_tick(app):
        tick_entered.set()
        assert tick_release.wait(10.0), "tick_release が来なかった"

    fake = FakeRunner([])
    app = _seam_app(tmp_path, fake)

    stop_event = threading.Event()
    result_box: dict = {}

    def _run():
        with patch("agentic_fx.service.build_app", return_value=app), \
             patch("agentic_fx.service.signal.signal"), \
             patch("agentic_fx.service._scheduler_tick_once",
                  side_effect=blocking_tick):
            result_box["rc"] = run_service(tmp_path, daemon=True,
                                           _stop_event=stop_event)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    try:
        assert tick_entered.wait(5.0), "scheduler tick が開始しなかった"
        # tick がブロック中 (th.join(30) がまだ完了できない状況) に
        # stop_event を立てる。run_service の finally は
        # supervisor.shutdown() → th.join(30) の順で実行されるはずなので、
        # th.join() がブロックしている間も try_submit は既に拒否される。
        stop_event.set()
        # run_service の finally が supervisor.shutdown() まで到達する
        # ための猶予 (shutdown() 自体は一瞬で終わる — ブロックしない)。
        time.sleep(0.3)
        assert app.supervisor.try_submit("trade", trigger="cron") is None, (
            "shutdown() が th.join() より後に実行されている — 停止処理の"
            "最中に新しい Mission が受理されてしまう")
    finally:
        tick_release.set()
        t.join(timeout=10.0)
    assert not t.is_alive()
    assert result_box.get("rc") == 0


def test_shutdown_sequence_completes_even_if_supervisor_shutdown_raises(tmp_path):
    """レビュー1周目 (指揮者所見) の回帰ピン: `supervisor.shutdown()` の
    失敗で停止シーケンス全体を落とさない。

    `shutdown` → `fail_pending` は `if not future.done()` の直後に
    `future.set_exception()` を呼ぶため、supervisor スレッドが間で完了
    させると `InvalidStateError` が伝播しうる。裸で呼ぶと **th.join /
    app.close / service_stopped の記録が全て飛ぶ** (資源リーク + 元の
    例外が置き換わる)。同ファイルの `instance_lock.close()` と同じ扱い。
    """
    app = _seam_app(tmp_path, FakeRunner([]))

    def boom(*, drain_exc):
        raise RuntimeError("supervisor already torn down")

    app.supervisor.shutdown = boom
    # shutdown が失敗すると supervisor へ停止が伝わらないため、本番では
    # main が join budget (dispatch ceiling — 既定で 20 分超) を丸ごと
    # 待つ。ここで見たいのは「シーケンスが最後まで走るか」だけなので
    # join/is_alive は差し替える (待ち時間そのものは別の申し送り)。
    app.supervisor.join = lambda timeout=None: None
    app.supervisor.is_alive = lambda: False

    stop_event = threading.Event()
    stop_event.set()
    with _no_real_network(), \
         patch("agentic_fx.service.build_app", return_value=app), \
         patch("agentic_fx.service.signal.signal"):
        rc = run_service(tmp_path, daemon=True, _stop_event=stop_event)

    assert rc == 0
    act = (tmp_path / "logs" / "activity.log").read_text(encoding="utf-8")
    assert "service_stopped" in act, (
        "supervisor.shutdown() の失敗で停止シーケンスが途中で落ちている")


def test_interactive_mode_actually_stops_via_stop_event_end_to_end(
        tmp_path, capsys):
    """裁定 C の E2E: 対話モードで `stop_event` が実際にシェルを起こし、
    停止シーケンスが完走する。

    Task 17 は割込み seam を用意しただけで **end-to-end では一度も駆動
    されていなかった** (producer が存在しなかった)。本 task が producer を
    配線したので、ここで初めて通しで確認できる。`run_shell` をモックせず、
    実物の `_InterruptibleLineReader` (select ポーリング) に**データが
    永久に来ないパイプ**を読ませ、別スレッドから `stop_event` を立てる。

    これが無いと「配線はされたがシェルが起きない」型の欠陥
    ([[verify-integration-not-just-units]]) を検出できない — `run_shell`
    単体テスト (Task 17) も `run_service` 側のテスト (run_shell をモック)
    も、結線点のズレは見ない。
    """
    app = _seam_app(tmp_path, FakeRunner([]))

    r, w = os.pipe()          # 書き込み側は誰も書かない = 入力が来ない stdin
    stdin_stream = os.fdopen(r, "rb", buffering=0)
    stop_event = threading.Event()

    class _Stdin:
        """`sys.stdin` の代用 — `_InterruptibleLineReader` が要求するのは
        `fileno()` と `buffer` (生バイト層) だけ。"""
        buffer = stdin_stream

        @staticmethod
        def fileno():
            return r

    timer = threading.Timer(0.5, stop_event.set)
    timer.start()
    started = time.monotonic()
    try:
        with _no_real_network(), \
             patch("agentic_fx.service.build_app", return_value=app), \
             patch("agentic_fx.service.signal.signal"), \
             patch("sys.stdin", _Stdin()):
            rc = run_service(tmp_path, daemon=False, _stop_event=stop_event)
    finally:
        timer.cancel()
        stdin_stream.close()
        os.close(w)
    elapsed = time.monotonic() - started

    assert rc == 0
    assert elapsed < 15.0, (
        f"対話モードで stop_event がシェルを起こしていない (elapsed={elapsed:.1f}s)")
    act = (tmp_path / "logs" / "activity.log").read_text(encoding="utf-8")
    assert "service_stopped" in act
    # **経路の取り違えを防ぐ**: select 非対応のフォールバック
    # (ブロッキング readline) に落ちていたら、割込み seam を一切通らずに
    # 緑になってしまう。フォールバックの告知が出ていないことを確認する。
    assert "select 非対応" not in capsys.readouterr().out


class _KeyboardInterruptOnMainWait(threading.Event):
    """メインスレッドの最初の `wait()` 呼び出しだけ `KeyboardInterrupt` を
    送出する (F2 のピン)。バックグラウンドスレッド (scheduler/watchdog) からの
    `wait()` は本物の `threading.Event.wait()` に委譲する — スレッド判定で
    分岐するため、どのスレッドが先に `wait()` を呼ぶかに依存しない
    (レース非依存)。"""

    def __init__(self) -> None:
        super().__init__()
        self._raised = False

    def wait(self, timeout=None):  # noqa: D102
        if (not self._raised
                and threading.current_thread() is threading.main_thread()):
            self._raised = True
            raise KeyboardInterrupt
        return super().wait(timeout)


def test_run_service_daemon_survives_keyboard_interrupt_during_wait(tmp_path):
    """F2-③: daemon の待機ループ中に `KeyboardInterrupt` が発生しても、素通り
    せず graceful shutdown (stop・join・close・記録) が最後まで実行される
    こと。以前は `stop_event.wait(1)` が try/finally の外にあり、例外が
    そのまま伝播して graceful 記録・runner close をすべて飛ばしていた。"""
    app = _seam_app(tmp_path, FakeRunner([]))
    stop_event = _KeyboardInterruptOnMainWait()
    with _no_real_network(), \
         patch("agentic_fx.service.build_app", return_value=app), \
         patch("agentic_fx.service.signal.signal"):
        rc = run_service(tmp_path, daemon=True, _stop_event=stop_event)
    assert rc == 0
    act = (tmp_path / "logs" / "activity.log").read_text(encoding="utf-8")
    assert "service_stopped" in act and "graceful" in act


def test_run_service_registers_sigterm_in_interactive_mode(tmp_path):
    app = _seam_app(tmp_path, FakeRunner([]))
    stop_event = threading.Event()
    stop_event.set()
    with patch("agentic_fx.service.build_app", return_value=app), \
         patch("agentic_fx.service.signal.signal") as register, \
         patch("agentic_fx.shell.run_shell"):
        assert run_service(tmp_path, daemon=False, _stop_event=stop_event) == 0
    registered = [call.args[0] for call in register.call_args_list]
    assert signal.SIGTERM in registered
    assert signal.SIGINT not in registered


# ---- Task 0: build_app の embedding seam ---------------------------------

def test_build_app_accepts_embedding_fn(tmp_path):
    """embedding_fn 注入で chromadb 既定モデルの probe を回避できる。"""
    _init(tmp_path)
    calls = []

    class TrackingEmbedding(FakeEmbedding):
        def __call__(self, input):  # noqa: A002
            calls.append(list(input))
            return super().__call__(input)

    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                    embedding_fn=TrackingEmbedding())
    assert app.rag is not None
    # Rag 初期化時に probe が実行されるか、あるいは最初の操作で呼ばれるか
    # を確認する (calls に記録がある = fake が通った)。
    if not calls:
        # Rag.__init__ が probe しない場合、add_news を呼んで検証
        app.rag.add_news([{"url": "https://ex.com/a1", "title": "Test",
                           "body": "Test article",
                           "source_name": "ex", "published": None}], NOW)
    assert calls  # embedding function が呼ばれた


# ---- プラン10 Task10-12 Step1: ImproveLoop 注入 --------------------------

def test_build_app_injects_real_improve_loop_into_supervisor(tmp_path):
    """`build_app` が `ImproveSupervisor._improve_loop` へ実 `ImproveLoop`
    を注入すること (旧稿は `None` のまま — Task 10 完了後に Task 12 が
    行う統合裁定 R-i9/R-i2)。rag は `build_app` が構築済みの単一インスタンス
    をそのまま渡す (new しない、T10-B10) ことも合わせて確認する。"""
    from agentic_fx.loops.improve_loop import ImproveLoop

    app = _seam_app(tmp_path, FakeRunner([]))
    assert isinstance(app.improve_supervisor._improve_loop, ImproveLoop)
    assert app.improve_supervisor._improve_loop._rag is app.rag


def test_submit_manual_with_real_improve_loop_prepares_without_notimplementederror(
        tmp_path, monkeypatch):
    """D-10 是正 (D-9): 検収が指摘した「実 ImproveLoop と繋いだ契約テストが
    0 本」の穴を埋める — `build_app` が注入する**本物の** `ImproveLoop`
    (D-1/D-8 で実装した `_compute_partition_hint`/`_materialize_workspace`
    を含む) を `submit_manual()` 経由で一周させ、`prepare()` が
    `NotImplementedError` を出さずに `ctx` (mission_id を持つ実 DB 行) を
    返すところまでを検証する。既存の `_FakeImproveLoop` を使うテスト
    (`tests/core/test_improve_wave_slot_protocol.py`) は D-1 のような
    本体側の空洞を構造的に検出できない (検収 D-9)。

    fake にするのは runner 境界のみ (`_build_worker_runner` の戻り値) —
    実プロセスの spawn (WorkerRunner.run) だけを避け、
    `_compute_partition_hint`/`_materialize_workspace`/`_build_rpc_handlers`/
    `_build_mission_tools`/prompt レンダリングはすべて実物を通す。"""
    from agentic_fx.loops.improve_loop import ImproveLoop

    app = _seam_app(tmp_path, FakeRunner([]))

    captured_ctx: dict = {}

    def _fake_build_worker_runner(self, ctx, *, on_ready=None):
        captured_ctx["ctx"] = ctx
        return FakeRunner([MissionResult(status="failed", output=None)])

    monkeypatch.setattr(
        ImproveLoop, "_build_worker_runner", _fake_build_worker_runner)

    mission_id = app.improve_supervisor.submit_manual()

    assert isinstance(mission_id, int)
    assert captured_ctx["ctx"].mission_id == mission_id
    # allowed_backlog_ids=None (手動 one-shot、印なし) まで
    # `_compute_partition_hint` が実際に評価された証跡
    assert captured_ctx["ctx"].allowed_backlog_ids is None

    conn = sqlite3.connect(tmp_path / "data" / "agentic.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status, loop FROM missions WHERE id=?", (mission_id,)).fetchone()
    conn.close()
    assert row["loop"] == "improve"
    # submit_manual は commit() まで一周する — failed 結果なので終端は失敗
    assert row["status"] == "failed"


# ---- Task 3: B 束小口 6 項目 (maintenance 順序) ---------------------------------

def test_signal_maintenance_reclaims_before_expiring(monkeypatch):
    """reclaim_expired → expire_stale の順で呼ばれる (順序入替、codex M⑤)。
    reclaim で pending に戻った直後の stale 行が、同じ tick 内の
    expire_stale でまだ拾われずに 1 tick 分だけ実行機会を得ることを、
    呼び出し順の記録で確認する。(裁定書 F-16/IM-10) `service.py` の
    実クロージャが呼ぶ module レベル関数 `_run_signal_maintenance` を
    直接呼び、`agentic_fx.store.signals` の実モジュール関数を
    monkeypatch する — テスト内の再定義フェイクに対して assert する
    恒真テストを避ける。
    """
    import agentic_fx.service as service_mod

    calls: list[str] = []

    def fake_reclaim(*a, **k):
        calls.append("reclaim")
        return []

    def fake_expire(*a, **k):
        calls.append("expire")
        return 0

    monkeypatch.setattr(service_mod.signals, "reclaim_expired", fake_reclaim)
    monkeypatch.setattr(service_mod.signals, "expire_stale", fake_expire)

    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    fake_producer = object()  # evaluate_due_plugins は呼ばれない前提で
    # 属性アクセスされたら AttributeError で明示的に落ちるようにする
    # (順序検証の対象外だが、意図せず呼ばれた場合は検出したい)。

    class _NoOpProducer:
        def evaluate_due_plugins(self, **k):
            calls.append("producer")

    service_mod._run_signal_maintenance(
        conn=None, signal_producer=_NoOpProducer(), approved=[],
        settings=service_mod.load_settings(
            Path(__file__).resolve().parents[1]
            / "config" / "settings.yaml.example"),
        now=now)

    assert calls == ["reclaim", "expire", "producer"]


def test_signal_maintenance_state_transition_stale_claimed_becomes_abandoned(tmp_path):
    """stale かつ lease 切れの claimed 行が、reclaim → expire 後に
    abandoned 状態になることを検証する (裁定書 I-1)。

    期待値: stale な claimed 行は reclaim_expired で pending に戻された後、
    同じ tick 内の expire_stale で abandoned に落ちる。最終状態は:
    - status = 'abandoned'
    - requeue_count = 元の値 + 1 (reclaim が +1 する。expire は触らない)
    """
    import agentic_fx.service as service_mod
    from agentic_fx.store.db import connect, init_db

    # テスト用 DB とデータ構築
    _init(tmp_path)
    conn = connect(tmp_path / "data" / "agentic.db")
    settings = service_mod.load_settings(
        tmp_path / "config" / "settings.yaml.example")

    # テスト時点の time
    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    # cutoff よりずっと古いバー (stale 化させる)
    stale_bar_ts = (now - timedelta(hours=100)).isoformat()
    # lease 切れ (lease_min=15分。20分以上前に claimed)
    expired_claimed_at = (now - timedelta(minutes=20)).isoformat()

    # stale かつ lease 切れの claimed 行を INSERT
    cursor = conn.execute(
        """
        INSERT INTO signals
        (plugin, content_hash, pair, timeframe, bar_ts, kind, status,
         requeue_count, claimed_at, created_at, payload_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("test_plugin", "hash1", "USDJPY", "1h", stale_bar_ts, "signal",
         "claimed", 0, expired_claimed_at, now.isoformat(), '{"value": 1}'))
    signal_id = cursor.lastrowid

    # maintenance 実行前の状態確認
    row_before = conn.execute(
        "SELECT status, requeue_count FROM signals WHERE id = ?",
        (signal_id,)).fetchone()
    assert row_before["status"] == "claimed"
    assert row_before["requeue_count"] == 0

    # maintenance 実行
    class _NoOpProducer:
        def evaluate_due_plugins(self, **k):
            pass

    service_mod._run_signal_maintenance(
        conn=conn, signal_producer=_NoOpProducer(), approved=[],
        settings=settings, now=now)

    # maintenance 後の状態確認
    row_after = conn.execute(
        "SELECT status, requeue_count FROM signals WHERE id = ?",
        (signal_id,)).fetchone()

    # 期待値: stale だから claimed → pending (by reclaim) → abandoned (by expire)
    assert row_after["status"] == "abandoned", \
        f"Expected abandoned, got {row_after['status']}"
    # requeue_count は reclaim が +1 する (expire は touch しない)
    assert row_after["requeue_count"] == 1, \
        f"Expected requeue_count=1, got {row_after['requeue_count']}"

    conn.close()


def test_signal_maintenance_callback_integration(tmp_path, monkeypatch):
    """build_app の on_signal_maintenance クロージャが scheduler に
    正しく配線されていることを検証する (codex M-1)。

    scheduler に渡された on_signal_maintenance callback を spy で監視し、
    callback が _run_signal_maintenance を正しい引数で呼んでいることを
    確認する。委譲を削除・旧本体に戻す変異は検出される。
    """
    import agentic_fx.service as service_mod

    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                    embedding_fn=FakeEmbedding())

    # scheduler の on_signal_maintenance callback が _run_signal_maintenance
    # を呼ぶ spy に切り替える
    calls: list[dict] = []

    def spy_run_signal_maintenance(*, conn, signal_producer, approved, settings, now):
        calls.append({
            "conn": conn is not None,
            "signal_producer": signal_producer is not None,
            "approved": approved is not None,
            "settings": settings is not None,
            "now": now is not None,
            "now_value": now
        })

    monkeypatch.setattr(
        service_mod, "_run_signal_maintenance", spy_run_signal_maintenance)

    # scheduler の callback を通じて on_signal_maintenance を呼ぶ
    test_now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    app.scheduler.on_signal_maintenance(test_now)

    # callback が呼ばれたことと、全引数が渡されたことを確認
    assert len(calls) == 1, f"Expected 1 call, got {len(calls)}"
    call = calls[0]
    assert call["conn"] is True, "conn should be passed"
    assert call["signal_producer"] is True, "signal_producer should be passed"
    assert call["approved"] is True, "approved should be passed"
    assert call["settings"] is True, "settings should be passed"
    assert call["now"] is True, "now should be passed"
    assert call["now_value"] == test_now, "now value should match"


def test_validate_startup_rejects_unknown_producer_source():
    """producer_source が KNOWN_OHLCV_SOURCES に含まれない場合、
    RuntimeError で reject する。"""
    from agentic_fx.service import _validate_startup
    from agentic_fx.config import load_settings

    settings = load_settings(
        Path(__file__).resolve().parents[1] / "config" / "settings.yaml.example")
    settings = settings.model_copy(
        update={"plugin": settings.plugin.model_copy(
            update={"producer_source": "typo-source"})})
    with pytest.raises(RuntimeError, match="producer_source"):
        _validate_startup(settings)


def test_app_has_clock_field(tmp_path):
    """App インスタンスが clock フィールドを持つことを確認。"""
    from agentic_fx.core.contracts import FixedClock

    fixed = FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))
    _init(tmp_path)
    app = build_app(tmp_path, clock=fixed)
    assert app.clock is fixed


class _FakeProvider:
    """PriceProvider 互換の最小限 fake (provider seam test 用)。"""
    def __init__(self):
        self.get_quote = lambda pair: None
        self.spec = lambda pair: None
        self.latest_1m_bar = lambda pair: None


def test_build_app_provider_seam_bypasses_quote_fn_patch(tmp_path):
    """provider を直接注入した場合、quote_fn/spec_fn/bars_fn の
    bound-method 差し替えは行われない (provider が全挙動を持つ)。"""
    _init(tmp_path)
    fake_provider = _FakeProvider()
    app = build_app(tmp_path, provider=fake_provider)
    assert app.provider is fake_provider


def test_watchdog_tick_uses_mission_watch_time_fn(monkeypatch):
    """_watchdog_tick の elapsed 算出が MissionWatch の time_fn 経由で
    行われる (fable M4) — 生の time.monotonic() を直接呼ばない。"""
    from agentic_fx.service import _watchdog_tick, App
    from agentic_fx.loops.mission_watch import MissionWatch

    fake_time = [1000.0]
    watch = MissionWatch(time_fn=lambda: fake_time[0])
    watch.begin(mission_id=1, loop="trade", timeout_sec=10.0)
    fake_time[0] = 1000.0 + 10.0 + 61.0  # timeout + grace(60) を超過

    calls: list[str] = []

    class FakeActivity:
        def write(self, *a, **k):
            calls.append("write")

    class FakeNotifier:
        def send(self, *a, **k):
            calls.append("send")

    app = App(conn_core=None, conn_shell=None, settings=None, state=None,
              activity=FakeActivity(), broker=None, executor=None,
              provider=None, econ=None, collector=None, rag=None,
              trade_loop=None, reflection=None, scheduler=None,
              commands=None, registry=None, core_lock=None,
              mission_watch=watch, improve_supervisor=None, notifier=FakeNotifier(), runner=None,
              owns_runner=False, clock=None, instance_lock=None,
              supervisor=None, conn_supervisor=None)
    _watchdog_tick(app)
    assert calls == ["write", "send"]


def test_build_app_provider_seam_rejects_concurrent_quote_fn(tmp_path):
    """provider と quote_fn を併用する場合は ValueError を送出して排他を
    強制する (fail closed: provider が全挙動を握る seam の混合は設定ミス)。"""
    _init(tmp_path)
    fake_provider = _FakeProvider()
    with pytest.raises(ValueError, match="provider と quote_fn/spec_fn/bars_fn は併用不可"):
        build_app(tmp_path, provider=fake_provider,
                  quote_fn=lambda pair: None)


def test_build_app_provider_seam_rejects_concurrent_spec_fn(tmp_path):
    """provider と spec_fn を併用する場合は ValueError を送出して排他を
    強制する。"""
    _init(tmp_path)
    fake_provider = _FakeProvider()
    with pytest.raises(ValueError, match="provider と quote_fn/spec_fn/bars_fn は併用不可"):
        build_app(tmp_path, provider=fake_provider,
                  spec_fn=lambda pair: None)


def test_build_app_provider_seam_rejects_concurrent_bars_fn(tmp_path):
    """provider と bars_fn を併用する場合は ValueError を送出して排他を
    強制する。"""
    _init(tmp_path)
    fake_provider = _FakeProvider()
    with pytest.raises(ValueError, match="provider と quote_fn/spec_fn/bars_fn は併用不可"):
        build_app(tmp_path, provider=fake_provider,
                  bars_fn=lambda pair: None)


def test_build_app_provider_seam_falls_back_to_provider_bound_methods(tmp_path):
    """provider のみ注入 (quote_fn/spec_fn/bars_fn 未指定) の場合でも、
    Executor/Scheduler に渡る quote_fn/spec_fn/bars_fn は None ではなく
    provider の束縛メソッドにフォールバックすること (fallback binding が
    削除されると Mission 実行時に TypeError で遅延失敗する)。"""
    _init(tmp_path)
    fake_provider = _FakeProvider()
    app = build_app(tmp_path, provider=fake_provider)
    # provider のみを渡し、quote_fn/spec_fn/bars_fn は未指定の場合、
    # Executor/Scheduler が保持する関数が provider の束縛メソッドになるはず
    assert app.executor.quote_fn is fake_provider.get_quote
    assert app.executor.spec_fn is fake_provider.spec
    assert app.scheduler.bars_fn is fake_provider.latest_1m_bar


def test_scheduler_tick_once_uses_app_clock(tmp_path):
    """scheduler_thread の 1 tick 分が app.clock.now() を読むことを直接確認
    (app.clock を壁時計に戻す変異で red になるべき)。"""
    from agentic_fx.service import _scheduler_tick_once
    from agentic_fx.core.contracts import FixedClock

    fixed = FixedClock(datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc))
    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=fixed)
    seen: list = []

    def _fake_tick(now):
        # 検収 B2 (2026-08-22): `Scheduler.tick()` は `list[Callable[[], None]]`
        # を返す契約になった (improve tick を core_lock の外で遅延発火する
        # ため)。このスタブも契約に合わせて空リストを返す。
        seen.append(now)
        return []

    app.scheduler.tick = _fake_tick
    _scheduler_tick_once(app)
    assert seen == [fixed.now()]


def test_watchdog_tick_uses_mission_watch_time_fn_directly(tmp_path):
    """_watchdog_tick の elapsed 算出が MissionWatch.time_fn 経由で行われ、
    結果を FakeActivity/FakeNotifier に観測する (time_fn を壁時計に戻す
    変異で red になるべき)。"""
    from agentic_fx.service import _watchdog_tick, App
    from agentic_fx.loops.mission_watch import MissionWatch

    fake_time = [1000.0]
    watch = MissionWatch(time_fn=lambda: fake_time[0])
    watch.begin(mission_id=1, loop="trade", timeout_sec=10.0)
    fake_time[0] = 1000.0 + 10.0 + 71.0  # timeout(10) + grace(60) + margin(1) を超過 → elapsed 81s

    captured_messages: list[str] = []

    class FakeActivity:
        def write(self, category, key, message):
            captured_messages.append(message)

    class FakeNotifier:
        def send(self, message):
            captured_messages.append(message)

    app = App(conn_core=None, conn_shell=None, settings=None, state=None,
              activity=FakeActivity(), broker=None, executor=None,
              provider=None, econ=None, collector=None, rag=None,
              trade_loop=None, reflection=None, scheduler=None,
              commands=None, registry=None, core_lock=None,
              mission_watch=watch, improve_supervisor=None, notifier=FakeNotifier(), runner=None,
              owns_runner=False, clock=None, instance_lock=None,
              supervisor=None, conn_supervisor=None)
    _watchdog_tick(app)
    # elapsed は fake_time に基づいた値 (81s) になるはず。
    # 生の time.monotonic() (システム起動からの経過、通常大きい数値)
    # に戻す変異はこのテストの assertion で red になる。
    assert any("81" in str(msg) for msg in captured_messages), \
        f"elapsed 81s を期待するが captured_messages={captured_messages}"


def test_build_app_calls_assert_tools_registered(tmp_path):
    """build_app が起動時に _assert_tools_registered を呼んで、必須ツール
    が登録されていることを確認する (tool 検証呼び出し削除を検出する)。

    spy で _assert_tools_registered が呼ばれることを直接確認し、引数の
    tool_list が実際に _TRADE_TOOLS であることを検証する。呼び出し削除および
    tool_list を空にする変異に対して red になることを保証する。"""
    from agentic_fx.loops.trade_loop import _TRADE_TOOLS

    _init(tmp_path)

    call_args = []
    original_assert = _assert_tools_registered

    def spy_assert(registry, tool_list):
        call_args.append({
            "registry": registry,
            "tool_list": tool_list,
            "tool_list_is_trade_tools": tool_list is _TRADE_TOOLS,
            "tool_list_not_empty": len(tool_list) > 0,
            "tool_list_value": list(tool_list) if hasattr(tool_list, '__iter__') else tool_list,
        })
        # 本来の検証も実行
        return original_assert(registry, tool_list)

    with patch("agentic_fx.service._assert_tools_registered",
               side_effect=spy_assert):
        app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))

    # _assert_tools_registered が呼ばれたこと
    assert len(call_args) >= 1, \
        "_assert_tools_registered is not called from build_app"
    # 渡された tool_list が _TRADE_TOOLS であること (参照の同一性で確認)
    assert call_args[0]["tool_list_is_trade_tools"], \
        f"tool_list is not _TRADE_TOOLS (got {call_args[0]['tool_list_value']})"
    # tool_list が空でないこと (空集合置換変異を検出する)
    assert call_args[0]["tool_list_not_empty"], \
        "tool_list is empty (should contain required tools like 'get_ohlcv')"
    # ツールが登録されている
    assert "get_ohlcv" in app.registry.names()


def test_build_app_provider_seam_passed_to_mission_registry(tmp_path):
    """build_app が構築した provider が build_mission_registry に実際に
    渡されることを確認する (provider seam が registry に透通する)。

    registry への provider 渡しが削除されると、テスト注入の provider が
    tool registry では無視されるため、後続の E2E 決定性テストが予測不可能に
    なる。provider=provider が削除される変異 (provider=None など) を検出する
    ために、渡された provider インスタンスが非 None であることを確認する。"""
    _init(tmp_path)

    # build_mission_registry の呼び出しを spy して、provider が実際に渡されているか確認
    from agentic_fx.tools import mission_registry as mr_module
    real_build_registry = mr_module.build_mission_registry
    calls: list[dict] = []

    def spy_build_registry(*args, **kwargs):
        provider_arg = kwargs.get("provider")
        calls.append({
            "has_provider": "provider" in kwargs,
            "provider_is_not_none": provider_arg is not None,
            "provider_value": provider_arg,
        })
        return real_build_registry(*args, **kwargs)

    with patch("agentic_fx.service.build_mission_registry",
               side_effect=spy_build_registry):
        app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))

    # build_mission_registry が provider kwarg を受け取ったことを確認
    assert len(calls) >= 1, "build_mission_registry not called"
    assert calls[0]["has_provider"], \
        "provider kwarg not passed to build_mission_registry"
    # provider の値が None でないことを確認 (provider=None 変異を検出する)
    assert calls[0]["provider_is_not_none"], \
        "provider kwarg passed but value is None (should be a PriceProvider instance)"
    # 渡された provider が registry に反映されていることを確認する
    # (registry の tool が provider インスタンスを束縛しているため)
    assert "get_ohlcv" in app.registry.names(), "get_ohlcv not in registry"


# ---- C1: 起動シーケンスの順序が pin されていない (レビュー修正) ----------

def test_c1_startup_order_acquire_lock_before_recover(tmp_path):
    """acquire_instance_lock と missions.recover_interrupted の呼び出し順を
    pin する。順序が acquire_lock → recover であることを spy で確認。"""
    from agentic_fx.store import missions as missions_module
    import agentic_fx.service as service_module

    _init(tmp_path)

    call_order: list[str] = []

    # 実装の参照を保持
    real_acquire = service_module.acquire_instance_lock
    real_recover = missions_module.recover_interrupted

    def spy_acquire(*args, **kwargs):
        call_order.append("lock")
        return real_acquire(*args, **kwargs)

    def spy_recover(*args, **kwargs):
        call_order.append("recover")
        return real_recover(*args, **kwargs)

    with patch("agentic_fx.service.acquire_instance_lock", side_effect=spy_acquire), \
         patch("agentic_fx.service.missions.recover_interrupted", side_effect=spy_recover):
        app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                       embedding_fn=FakeEmbedding())
        app.instance_lock.close()

    assert call_order == ["lock", "recover"], \
        f"Expected ['lock', 'recover'], got {call_order}"


def test_c1_double_startup_preserves_first_mission(tmp_path):
    """二重起動があると、2 回目の build_app が InstanceAlreadyRunning を raise し、
    その後で 1 回目の mission がまだ running のままであること。
    (ロックを後に取る実装ならここが 'interrupted' になって red になる)。"""
    from agentic_fx.store import missions as missions_module
    from agentic_fx.store.instance_lock import InstanceAlreadyRunning

    _init(tmp_path)

    # 1 回目の起動
    app1 = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                    embedding_fn=FakeEmbedding())

    # 1 回目で mission を 1 件作成
    mid = missions_module.start(app1.conn_core, "trade", "local", "qwen",
                               NOW, trigger="test")
    assert mid is not None

    # ロックを解放せずに 2 回目の build_app を試みる
    with pytest.raises(InstanceAlreadyRunning):
        build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                 embedding_fn=FakeEmbedding())

    # 1 回目の mission がまだ running のままであること
    row = app1.conn_core.execute(
        "SELECT status FROM missions WHERE id=?", (mid,)).fetchone()
    assert row["status"] == "running"

    app1.instance_lock.close()


# ---- I3: instance_lock の例外時解放 (レビュー修正) -------------------------

def test_i3_instance_lock_released_on_build_app_exception(tmp_path):
    """build_app の途中で例外が発生しても、acquire_instance_lock を
    close して lock を解放すること。例外を変数に束縛して保持したまま、
    別プロセスが同じ root に対する acquire_instance_lock に成功すること。"""
    from agentic_fx.store.instance_lock import acquire_instance_lock

    _init(tmp_path)

    # build_app を ValueErrorで失敗させる。例外を変数に束縛して保持
    held_exc = None
    try:
        build_app(tmp_path, provider=_FakeProvider(),
                 quote_fn=lambda pair: None)  # ValueError で失敗
    except ValueError as e:
        held_exc = e  # 例外を保持してトレースバックがメモリに残る

    assert held_exc is not None

    # この時点で lock はリリースされていなければならない
    # (held_exc の traceback が build_app のフレームを保持していても)
    fh = acquire_instance_lock(tmp_path / "data")  # 成功すればテスト合格
    fh.close()


def test_i3b_lock_close_failure_does_not_mask_the_original_exception(
        tmp_path, monkeypatch):
    """解放処理が失敗しても、**元の失敗原因が呼び出し元に届く**こと。

    2 周目レビュー指摘 (sonnet Minor / KAT-Coder Critical)。
    `except BaseException: instance_lock.close(); raise` の素の形だと、
    `close()` 自身が送出した例外が伝播してしまい、呼び出し元は「なぜ起動に
    失敗したのか」を見失う (元の例外は `__context__` に退避されるだけで、
    `except ValueError` は成立しなくなる)。ロックは fd なので解放に失敗しても
    プロセス終了時に OS が回収する — 原因の伝播を優先する。
    """
    import agentic_fx.service as svc_mod

    _init(tmp_path)

    real_acquire = svc_mod.acquire_instance_lock

    class _CloseExplodes:
        """close が必ず失敗する lock ラッパ (解放系の故障を模す)。"""

        def __init__(self, fh):
            self._fh = fh

        def close(self):
            raise OSError("simulated close failure (e.g. ENOSPC on flush)")

    holders = []

    def _acquire(db_dir):
        fh = real_acquire(db_dir)
        holders.append(fh)          # 実 lock は保持し、test 終了時に解放する
        return _CloseExplodes(fh)

    monkeypatch.setattr(svc_mod, "acquire_instance_lock", _acquire)

    try:
        # build_app は provider と quote_fn の併用で ValueError を出す。
        # close が失敗しても、呼び出し元に届くのは **ValueError** でなければ
        # ならない (OSError にすり替わったら red)。
        with pytest.raises(ValueError):
            build_app(tmp_path, provider=_FakeProvider(),
                      quote_fn=lambda pair: None)
    finally:
        for fh in holders:
            fh.close()


def test_conn_supervisor_is_readonly(tmp_path):
    """Global Constraints: conn_supervisor は読取専用接続でなければ
    ならない (IM-1/P8-02) — 書込は OperationalError になる。"""
    _init(tmp_path)
    app = build_app(tmp_path)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        app.conn_supervisor.execute(
            "INSERT INTO missions(loop, runner, model, status, "
            "started_at) VALUES ('trade', 'local', 'x', 'running', 'x')")


def test_trade_loop_healthcheck_provider_is_readonly(tmp_path):
    """裁定書 F-6 (CR-5): TradeLoop.provider (healthcheck 専用) は
    conn_supervisor (RO) で構築されている — conn_core への書込可能な
    provider を healthcheck に使っていないことの配線確認。"""
    _init(tmp_path)
    app = build_app(tmp_path)
    assert app.trade_loop.provider.conn is app.conn_supervisor
    assert app.trade_loop.provider.readonly is True


def test_trade_loop_healthcheck_provider_uses_injected_dataflow_seam(tmp_path):
    """レビュー 3 周目 codex E1: quote_fn (テスト・オフライン E2E の注入
    seam) は healthcheck 専用 provider の `get_quote` にも反映される —
    ただし healthcheck_provider 自身は**常に** RO (`conn_supervisor` +
    `readonly=True`) で構築された別インスタンスであること (F-6/CR-5)。

    レビュー 2 周目 codex D2 は「注入時は app.provider をそのまま流用する」
    という形で直したが、これは quote_fn だけを注入し provider= は注入
    しない呼び出し元では、書込可能な conn_core 版 provider を healthcheck
    に使うことになり F-6/CR-5 を打ち消していた (レビュー3周目 codex E1 —
    指揮者の指示ミス)。RO であることと注入 seam が効くことの両方を
    同時に確認する。
    """
    _init(tmp_path)
    calls: list[str] = []

    def quote_fn(pair):
        calls.append(pair)
        return None

    app = build_app(tmp_path, quote_fn=quote_fn)
    # healthcheck_provider は app.provider と別インスタンスの RO 版
    assert app.trade_loop.provider is not app.provider
    assert app.trade_loop.provider.conn is app.conn_supervisor
    assert app.trade_loop.provider.readonly is True
    # それでも注入した quote_fn は healthcheck_provider.get_quote 経由で
    # 呼ばれる (stub が効く)
    app.trade_loop.provider.get_quote("USDJPY")
    assert calls == ["USDJPY"]


def test_trade_loop_healthcheck_provider_reuses_injected_provider_seam(tmp_path):
    """レビュー 3 周目 codex E1 (provider= フル注入版): `provider=` で
    PriceProvider を丸ごと注入した場合も、healthcheck 専用 provider は
    RO (`conn_supervisor` + `readonly=True`) の別インスタンスのまま、
    `get_quote`/`spec`/`latest_1m_bar` だけが注入された provider の
    束縛メソッドを経由する (RO と stub 尊重の両立)。"""
    _init(tmp_path)
    stub_provider = MagicMock()
    stub_provider.get_quote = MagicMock()
    stub_provider.spec = MagicMock()
    stub_provider.latest_1m_bar = MagicMock()
    app = build_app(tmp_path, provider=stub_provider)
    assert app.trade_loop.provider is not stub_provider
    assert app.trade_loop.provider.conn is app.conn_supervisor
    assert app.trade_loop.provider.readonly is True
    app.trade_loop.provider.get_quote("USDJPY")
    stub_provider.get_quote.assert_called_once_with("USDJPY")


def test_check_llama_swap_improve_props_failure_omits_improve_line(capsys):
    """Task 5 / 段 0 の生存変異: **異モデル構成で improve の /props だけが
    失敗**したとき、improve 行を出さない (trade 側は従来どおり表示する)。

    `if improve_ctx is not None:` を外す変異は**フルスイート 1800 passed の
    まま生存する** (指揮者が実測) — 既存の異モデルテストは /props が必ず
    成功する handler しか持たず、/props 失敗のテストは**同一モデル構成**の
    ものしかないため、improve 側のガードだけが未検査で残っていた。

    ガードが無いと init が `improve model 'improve-m' ctx None` と表示する。
    「取得できなかった」ことを「ctx が None である」と読める形で出すのは、
    設定ミスの診断を誤らせる。"""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "trade-m"}]})
        if path == "/props":
            if request.url.params.get("model") == "improve-m":
                return httpx.Response(500)
            return httpx.Response(200, json={
                "default_generation_settings": {"n_ctx": 65536}})
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    with patch("httpx.get", client.get), patch("httpx.post", client.post):
        _check_llama_swap(_StubSettingsDiff())

    out = capsys.readouterr().out
    assert "improve model" not in out
    assert "None" not in out
    # trade 側は影響を受けない。
    assert "llama-swap OK (model 'trade-m' loaded, ctx 65536)" in out


def test_build_app_rejects_cache_retention_below_interval_requirement(
        tmp_path):
    """設計書 D2: cache_retention_days が構成済み intervals の要求日数を
    下回ると起動拒否する (黙って clamp しない)。yfinance で 1d を intervals
    に足すと要求日数が 120 日に跳ね上がる (base=1h, ratio=24, lookback=5)
    ことを使って再現する。"""
    (tmp_path / "config").mkdir()
    src = open("config/settings.yaml.example", encoding="utf-8").read()
    src = src.replace(
        "intervals: [1m, 5m, 15m, 1h, 4h]",
        "intervals: [1m, 5m, 15m, 1h, 4h, 1d]")
    (tmp_path / "config" / "settings.yaml").write_text(src)
    with pytest.raises(RuntimeError, match="cache_retention_days"):
        with patch("agentic_fx.service.PriceProvider"), \
             patch("agentic_fx.service._check_llama_swap"):
            build_app(tmp_path)


def test_build_app_error_message_names_interval_and_required_days(tmp_path):
    (tmp_path / "config").mkdir()
    src = open("config/settings.yaml.example", encoding="utf-8").read()
    src = src.replace(
        "intervals: [1m, 5m, 15m, 1h, 4h]",
        "intervals: [1m, 5m, 15m, 1h, 4h, 1d]")
    (tmp_path / "config" / "settings.yaml").write_text(src)
    with pytest.raises(RuntimeError, match="1d") as exc:
        with patch("agentic_fx.service.PriceProvider"), \
             patch("agentic_fx.service._check_llama_swap"):
            build_app(tmp_path)
    assert "120" in str(exc.value)


def test_build_app_accepts_default_example_intervals_and_retention(tmp_path):
    """既定の settings.yaml.example (intervals 5 種、cache_retention_days=30)
    は起動を通る — 最大要求は 20 日 (4h: base=1h, ratio=4, lookback=5)。"""
    (tmp_path / "config").mkdir()
    src = open("config/settings.yaml.example", encoding="utf-8").read()
    (tmp_path / "config" / "settings.yaml").write_text(src)
    with patch("agentic_fx.service.PriceProvider") as pp, \
         patch("agentic_fx.service._check_llama_swap"):
        pp.return_value.healthcheck.return_value = "yfinance"
        app = build_app(tmp_path)
    app.close()


def test_build_app_wires_cache_maintenance_hook(tmp_path):
    """build_app が Scheduler に on_cache_maintenance を配線すること
    (None のままではない)。"""
    (tmp_path / "config").mkdir()
    src = open("config/settings.yaml.example", encoding="utf-8").read()
    (tmp_path / "config" / "settings.yaml").write_text(src)
    with patch("agentic_fx.service.PriceProvider") as pp, \
         patch("agentic_fx.service._check_llama_swap"):
        pp.return_value.healthcheck.return_value = "yfinance"
        app = build_app(tmp_path)
    try:
        assert app.scheduler.on_cache_maintenance is not None
    finally:
        app.close()


def test_cache_maintenance_prunes_old_cache_rows(tmp_path):
    """配線された on_cache_maintenance が実際に ohlcv_cache を刈ること
    (cutoff = tick 時刻 - cache_retention_days)。"""
    from agentic_fx.core.contracts import Bar
    from agentic_fx.store import ohlcv

    (tmp_path / "config").mkdir()
    src = open("config/settings.yaml.example", encoding="utf-8").read()
    (tmp_path / "config" / "settings.yaml").write_text(src)
    with patch("agentic_fx.service.PriceProvider") as pp, \
         patch("agentic_fx.service._check_llama_swap"):
        pp.return_value.healthcheck.return_value = "yfinance"
        app = build_app(tmp_path, clock=FixedClock(NOW))
    try:
        # ⚠️ **境界の両側**を置く。旧版は 40 日前の 1 本だけを入れて「消えたこと」
        # しか見ておらず、**`cutoff` を retention 非依存の固定値にする変異が生存**
        # していた (ローカル LLM muse-glimmer が検出・指揮者が裏取り) — 例えば
        # `cutoff = NOW` (全削除) でもこのテストは PASS してしまう。
        # 残るべき 1 本を足して初めて「retention 由来の cutoff」を pin できる。
        old_bar = Bar("USDJPY", "1m", NOW - timedelta(days=40),      # 保持外 → 消える
                      148.0, 148.1, 147.9, 148.05, 10)
        keep_bar = Bar("USDJPY", "1m", NOW - timedelta(days=10),     # 保持内 → 残る
                       149.0, 149.1, 148.9, 149.05, 11)
        ohlcv.upsert_cache_bars(app.conn_core, [old_bar, keep_bar],
                                source="yfinance")

        app.scheduler.on_cache_maintenance(NOW)

        rows = ohlcv.load_cache_bars(
            app.conn_core, "USDJPY", "1m", source="yfinance",
            since=NOW - timedelta(days=41))
        # 「消えた」だけでなく「残った」も見る。全削除・無削除の双方を殺す。
        assert [b.ts for b in rows] == [NOW - timedelta(days=10)]
    finally:
        app.close()


def test_cache_maintenance_cutoff_follows_configured_retention_days(tmp_path):
    """cutoff は `datafeed.cache_retention_days` の**設定値**から導出される。

    段 0 の変異 M16-15 (`cutoff = now - timedelta(days=30)` と固定値化) は
    既存テストでは**生存した** — settings.yaml.example の既定が 30 日なので
    固定値 30 が設定値と一致し、等価変異になっていた。既定と異なる保持期間
    (45 日) を明示し、30 日固定なら消えてしまう 35 日前のバーを残ることの
    観測点に使う。
    """
    from agentic_fx.core.contracts import Bar
    from agentic_fx.store import ohlcv

    (tmp_path / "config").mkdir()
    src = open("config/settings.yaml.example", encoding="utf-8").read()
    src = src.replace("cache_retention_days: 30", "cache_retention_days: 45")
    assert "cache_retention_days: 45" in src  # 前提: 置換が効いたこと
    (tmp_path / "config" / "settings.yaml").write_text(src)
    with patch("agentic_fx.service.PriceProvider") as pp, \
         patch("agentic_fx.service._check_llama_swap"):
        pp.return_value.healthcheck.return_value = "yfinance"
        app = build_app(tmp_path, clock=FixedClock(NOW))
    try:
        assert app.settings.datafeed.cache_retention_days == 45  # 前提
        drop_bar = Bar("USDJPY", "1m", NOW - timedelta(days=50),   # 45 日外 → 消える
                       148.0, 148.1, 147.9, 148.05, 10)
        keep_bar = Bar("USDJPY", "1m", NOW - timedelta(days=35),   # 45 日内 → 残る
                       149.0, 149.1, 148.9, 149.05, 11)            # (30 日固定なら消える)
        ohlcv.upsert_cache_bars(app.conn_core, [drop_bar, keep_bar],
                                source="yfinance")

        app.scheduler.on_cache_maintenance(NOW)

        rows = ohlcv.load_cache_bars(
            app.conn_core, "USDJPY", "1m", source="yfinance",
            since=NOW - timedelta(days=51))
        assert [b.ts for b in rows] == [NOW - timedelta(days=35)]
    finally:
        app.close()


def test_build_app_rate_fn_forwards_deadline_check_to_price_provider(tmp_path):
    """束B 1周目 ローカル LLM レビュー (KAT c2 / muse c2): 本番配線
    (`service.py` の `rate_fn` クロージャ) が `deadline_check` を
    `PriceProvider.to_account_rate` まで転送すること。

    `tests/test_wiring.py::_env` は **自前の 3 引数 lambda** を
    `rate_fn` に束縛しているため service.py の本番クロージャを一度も
    通らない。実測: `service.py:571` の `deadline_check=deadline_check`
    を削除しても全 1923 件が green (gather deadline が本番でだけ
    `to_account_rate` の脚に届かなくなる = Task 7 の目的が本番で無効)。"""
    (tmp_path / "config").mkdir()
    src = open("config/settings.yaml.example", encoding="utf-8").read()
    (tmp_path / "config" / "settings.yaml").write_text(src)
    with patch("agentic_fx.service.PriceProvider") as pp, \
         patch("agentic_fx.service._check_llama_swap"):
        pp.return_value.healthcheck.return_value = "yfinance"
        app = build_app(tmp_path)
    try:
        def probe(leg: str) -> None:
            return None

        app.executor.rate_fn("USD", "JPY", NOW, deadline_check=probe)

        kwargs = pp.return_value.to_account_rate.call_args.kwargs
        assert kwargs["deadline_check"] is probe, (
            "本番配線の rate_fn が deadline_check を to_account_rate へ"
            "転送していない")
    finally:
        app.close()


# ---- 本番配線 gather の実駆動 (束B レビュー: rate_fn 契約の宣言/実態一致) ---

_GATHER_SPEC = InstrumentSpec(
    symbol="USDJPY", pip_size=0.01, min_lot=0.01, max_lot=50.0, lot_step=0.01,
    contract_size=100_000, base_currency="USD", quote_currency="JPY")


def _gather_app(tmp_path):
    """build_app の本番配線で executor を組み、gather が実駆動できる最小の
    PriceProvider スタブを当てる (外部アクセスなし)。"""
    (tmp_path / "config").mkdir()
    src = open("config/settings.yaml.example", encoding="utf-8").read()
    (tmp_path / "config" / "settings.yaml").write_text(src)
    with patch("agentic_fx.service.PriceProvider") as pp, \
         patch("agentic_fx.service._check_llama_swap"):
        pp.return_value.healthcheck.return_value = "yfinance"
        pp.return_value.get_quote.return_value = Quote(
            "USDJPY", 148.49, 148.51, NOW, "test")
        pp.return_value.spec.return_value = _GATHER_SPEC
        pp.return_value.to_account_rate.side_effect = (
            lambda ccy, account_ccy, **kw: ConversionRate(
                1.0 if ccy == account_ccy else 148.51, ccy, account_ccy,
                (kw["reference_ts"],)))
        return build_app(tmp_path)


def test_build_app_open_gather_drives_production_rate_fn(tmp_path):
    """本番配線 (build_app) の executor で OPEN gather を実駆動する。

    `Executor.__init__` の `rate_fn` は gather 経由で必ず keyword-only の
    `deadline_check` 付きで呼ばれる (executor.py `cycle_rate_fn`)。この契約に
    適合しない 3 引数 `rate_fn` を配線すると TypeError になり、trade_loop の
    commit-pre が `"execution snapshot unavailable: ..."` の gate 拒否に変換
    する (健全でも発注できない)。既存テストはこの経路を build_app 配線で一度も
    踏まない — `tests/test_wiring.py` は gather を呼ばず、
    `test_build_app_rate_fn_forwards_deadline_check_to_price_provider` は
    `rate_fn` を直接呼ぶだけ。

    kill: 配線側の `rate_fn` を 3 引数に戻すと TypeError で red。
    """
    app = _gather_app(tmp_path)
    try:
        intent = TradeIntent(action=Action.OPEN, origin=Origin.SCHEDULER,
                             reasoning="t", pair="USDJPY",
                             direction=Direction.LONG,
                             entry_type=EntryType.MARKET, horizon=Horizon.DAY,
                             stop_loss=147.0, take_profit=150.0)
        snapshot = app.executor.gather_open_snapshot(intent,
                                                     exposure_pairs=[])
        assert set(snapshot.rates) == {"USD", "JPY"}
        assert snapshot.rates["USD"].value == 148.51
    finally:
        app.close()


def test_build_app_close_gather_is_not_degraded(tmp_path):
    """CLOSE gather は本番配線で degraded に落ちないこと。

    **「例外にならない」ではピンにならない** — `resolve_close_rate` の広い
    `except Exception` が契約不整合の TypeError も吸収するため、3 引数
    `rate_fn` を配線しても `gather_close_snapshot` は raise せず、
    `rate=None` / `rate_degraded=True` で静かに縮退する (realized_pnl が
    未確定のまま残る)。`rate_degraded is False` と
    `rate_degraded_reason is None` まで見て初めて kill できる。
    """
    app = _gather_app(tmp_path)
    try:
        row = {"id": 1, "pair": "USDJPY", "direction": "long"}
        snapshot = app.executor.gather_close_snapshot(row)
        assert snapshot.rate_degraded is False, snapshot.rate_degraded_reason
        assert snapshot.rate_degraded_reason is None
        assert snapshot.rate is not None and snapshot.rate.value == 1.0
    finally:
        app.close()


# ============================================================================
# Step 19-24: Startup checks for CLI backends (プラン10 Task1)
# ============================================================================

def _root_with_settings(tmp_path, **overrides):
    """`_init(tmp_path)` 済みの root で `config/settings.yaml` を yaml 経由で
    上書きする。`overrides` はトップレベルキーの部分辞書 (既存キーとの
    深いマージ — 例 `runner={"improve": {"backend": "claude", "model": "m"}}`
    は `runner.improve.model` 以外の既存フィールドを保持する)。"""
    import yaml as _yaml
    _init(tmp_path)
    path = tmp_path / "config" / "settings.yaml"
    raw = _yaml.safe_load(path.read_text(encoding="utf-8"))

    def _deep_update(d, u):
        for k, v in u.items():
            if isinstance(v, dict) and isinstance(d.get(k), dict):
                _deep_update(d[k], v)
            else:
                d[k] = v

    _deep_update(raw, overrides)
    path.write_text(_yaml.safe_dump(raw), encoding="utf-8")
    return tmp_path


def _find_vendor_codex_bin() -> str | None:
    """`codex` (node ラッパ) を PATH で解決し、その隣の vendor native
    バイナリを探す (裁定 R5)。無ければ `None` (呼び出し側で skip)。"""
    import shutil as _shutil

    wrapper = _shutil.which("codex")
    if wrapper is None:
        return None
    pkg_root = Path(wrapper).resolve().parent.parent  # .../@openai/codex
    candidates = sorted(pkg_root.glob("node_modules/@openai/codex-*/vendor/*/bin/codex"))
    return str(candidates[0]) if candidates else None


def test_build_app_rejects_when_runner_bin_not_resolvable(tmp_path, monkeypatch):
    """①: `runner.improve.backend=claude` で `claude` が PATH 上に無く、
    かつ絶対パスでもなければ起動拒否 (fail closed)。"""
    root = _root_with_settings(tmp_path, runner={
        "improve": {"backend": "claude", "model": "m"},
        "claude": {"bin": "afx-nonexistent-claude-binary"}})
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(RuntimeError, match="claude"):
        build_app(root, clock=FixedClock(NOW), embedding_fn=FakeEmbedding())


def test_build_app_rejects_when_trade_claude_bin_not_resolvable(tmp_path, monkeypatch):
    """Minor 14 の再発防止: `runner.trade.backend=claude` 構成でも
    `_check_cli_backend` が呼ばれる (旧実装は improve しか見ておらず、
    trade+claude は起動時検査が一切走らないまま Mission 実行時に落ちて
    いた)。"""
    root = _root_with_settings(tmp_path, runner={
        "trade": {"backend": "claude", "model": "m"},
        "claude": {"bin": "afx-nonexistent-claude-binary"}})
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(RuntimeError, match="claude"):
        build_app(root, clock=FixedClock(NOW), embedding_fn=FakeEmbedding())


def test_build_app_rejects_codex_node_wrapper(tmp_path):
    """①: codex は ELF (vendor native) を要求し node ラッパを拒否する。"""
    wrapper = tmp_path / "codex-wrapper.js"
    wrapper.write_text("#!/usr/bin/env node\nrequire('./cli')\n")
    wrapper.chmod(0o755)
    root = _root_with_settings(tmp_path, runner={
        "improve": {"backend": "codex", "model": "m"},
        "codex": {"bin": str(wrapper)}})
    with pytest.raises(RuntimeError, match="ELF|vendor native"):
        build_app(root, clock=FixedClock(NOW), embedding_fn=FakeEmbedding())


def test_build_app_rejects_when_version_check_fails(tmp_path):
    """②: `<bin> --version` が非 0 で返れば起動拒否。ELF 要求 (①) と
    独立に検証するため backend=claude (`require_elf=False`) を使う —
    codex はバイナリが ELF でなければ①で先に拒否されるため②単体を
    観測できない。"""
    fake_bin = tmp_path / "fake-claude"
    fake_bin.write_text("#!/bin/sh\nexit 1\n")
    fake_bin.chmod(0o755)
    root = _root_with_settings(tmp_path, runner={
        "improve": {"backend": "claude", "model": "m"},
        "claude": {"bin": str(fake_bin)}})
    with pytest.raises(RuntimeError, match="--version"):
        build_app(root, clock=FixedClock(NOW), embedding_fn=FakeEmbedding())


def test_build_app_rejects_when_credentials_file_missing(tmp_path):
    """③: claude/codex+chatgpt は認証ファイル必須 (codex+llama_swap は要求しない)。
    codex+chatgpt を使うには①②を通す必要があるため vendor native codex を
    使う (無ければ skip — 裁定 R5)。"""
    vendor_codex = _find_vendor_codex_bin()
    if vendor_codex is None:
        pytest.skip("vendor native codex バイナリが見つからない (裁定 R5)")
    root = _root_with_settings(tmp_path, runner={
        "improve": {"backend": "codex", "model": "m"},
        "codex": {"bin": vendor_codex, "provider": "chatgpt",
                  "auth_file": str(tmp_path / "no-such-file.json")}})
    with pytest.raises(RuntimeError, match="credentials|auth"):
        build_app(root, clock=FixedClock(NOW), embedding_fn=FakeEmbedding())


def test_build_app_does_not_require_credentials_for_codex_llama_swap(
        tmp_path, monkeypatch):
    """③ の裏: provider=llama_swap は auth_file 欠落でも起動時検査を通る
    (§1.1-2「provider=llama_swap は空の scratch CODEX_HOME で起動」)。
    `improve.llama_swap_verified=true` も併せて上書きする (さもないと
    llama_swap 分岐自体が別理由で拒否する — 骨格 §1.1-2)。

    段 0 申し送り 1 是正 (検査⑤を codex 分岐でも呼ぶ) により
    `_check_service_initial_env_has_no_secrets` がこの codex 経路でも
    呼ばれるようになった。本テストは③ (認証ファイル必須の裏) だけを
    見るため、実行環境自身の env (pytest 実行元の Claude Code セッションが
    `CLAUDE_CODE_*TOKEN*` 等を export していることがある) に検査結果が
    左右されないよう検査⑤を no-op にする — 検査⑤本体は別テスト
    (`test_check_service_initial_env_has_no_secrets_*`) が pin 済み。"""
    import agentic_fx.service as service_mod

    monkeypatch.setattr(
        service_mod, "_check_service_initial_env_has_no_secrets",
        lambda settings, *, read_initial_env_names=None: None)
    vendor_codex = _find_vendor_codex_bin()
    if vendor_codex is None:
        pytest.skip("vendor native codex バイナリが見つからない (裁定 R5)")
    root = _root_with_settings(tmp_path, runner={
        "improve": {"backend": "codex", "model": "m"},
        "codex": {"bin": vendor_codex, "provider": "llama_swap",
                  "auth_file": str(tmp_path / "absent")}},
        improve={"llama_swap_verified": True})
    build_app(root, clock=FixedClock(NOW), embedding_fn=FakeEmbedding())  # 例外を出さない


def test_build_app_rejects_when_service_initial_env_has_secret_pattern(tmp_path, monkeypatch):
    """⑤ の配線: improve+claude のとき `_check_service_initial_env_has_no_secrets`
    が呼ばれ、例外がそのまま `build_app` から伝播する。検査本体 (`/proc/self/environ`
    の読み取り・パターン照合の正しさ) は `test_check_service_initial_env_has_no_secrets_*`
    (下記) が別途 pin する — ここでは配線のみを見る (R3: `monkeypatch.setenv` は
    `/proc/self/environ` を書き換えないため、実環境の秘密漏れに依存したテストは
    書けない、B1 の再発防止)。"""
    import sys
    import agentic_fx.service as service_mod

    def _raise(settings, *, read_initial_env_names=None):
        raise RuntimeError("SOME_SERVICE_API_KEY leaked")

    monkeypatch.setattr(service_mod, "_check_service_initial_env_has_no_secrets", _raise)
    creds_file = tmp_path / ".credentials.json"
    creds_file.write_text('{"token":"x"}')
    creds_file.chmod(0o600)
    root = _root_with_settings(tmp_path, runner={
        "improve": {"backend": "claude", "model": "m"},
        "claude": {"bin": sys.executable, "credentials_file": str(creds_file)}})
    with pytest.raises(RuntimeError, match="API_KEY"):
        build_app(root, clock=FixedClock(NOW), embedding_fn=FakeEmbedding())


def test_build_app_does_not_check_secret_env_when_improve_backend_is_local(
        tmp_path, monkeypatch):
    """⑤ の裏: backend=local の環境では `_check_service_initial_env_has_no_secrets`
    が一切呼ばれない (骨格 §1.4)。"""
    import agentic_fx.service as service_mod

    def _raise(settings, *, read_initial_env_names=None):
        raise RuntimeError("must not be called for backend=local")

    monkeypatch.setattr(service_mod, "_check_service_initial_env_has_no_secrets", _raise)
    _init(tmp_path)  # improve.backend == "local" (example 既定)
    build_app(tmp_path, clock=FixedClock(NOW), embedding_fn=FakeEmbedding())  # 例外を出さない


def test_check_service_initial_env_has_no_secrets_rejects_leaked_key_via_seam():
    """⑤ 検査本体 (R3): seam に注入した名前集合に秘密パターンがあれば拒否する。
    `settings` 引数は現状未使用 (呼び出し規約を `_check_cli_backend` と
    揃えるために受け取るのみ) — ダミー値でよい。"""
    from agentic_fx.service import _check_service_initial_env_has_no_secrets

    with pytest.raises(RuntimeError, match="API_KEY|secret"):
        _check_service_initial_env_has_no_secrets(
            object(), read_initial_env_names=lambda: {"SOME_SERVICE_API_KEY", "HOME"})


def test_check_service_initial_env_has_no_secrets_passes_when_seam_clean():
    """⑤ の裏 (R3): 秘密パターンに一致する名前が無ければ何もしない。"""
    from agentic_fx.service import _check_service_initial_env_has_no_secrets

    _check_service_initial_env_has_no_secrets(
        object(), read_initial_env_names=lambda: {"HOME", "PATH"})  # 例外を出さない


def test_check_service_initial_env_has_no_secrets_default_seam_is_proc_self_environ():
    """R3 の再発防止 pin (B1): 既定 seam が `_read_proc_self_environ_names`
    (`/proc/self/environ` 読み取り) であること。`os.environ` ベースの reader に
    すり替える変異はこの identity 比較で red になる — `os.environ` は
    `.env`→`load_dotenv()` 由来のキーも含むため、そちらを既定にすると
    「サービスは `.env` を使ってよい」という設計の前提 (§1.4-⑤) に反して
    `.env` 運用が常に起動拒否になる (B1 の実際の欠陥)。"""
    import inspect

    from agentic_fx.service import (
        _check_service_initial_env_has_no_secrets, _read_proc_self_environ_names,
    )

    sig = inspect.signature(_check_service_initial_env_has_no_secrets)
    assert (sig.parameters["read_initial_env_names"].default
            is _read_proc_self_environ_names)


def test_read_proc_self_environ_names_reads_real_proc_self_environ():
    """`_read_proc_self_environ_names` は実プロセスの `/proc/self/environ`
    を読む (Linux 前提)。`HOME`/`PATH` は pytest プロセス自身に必ず存在する
    ため実環境で検証できる。"""
    from agentic_fx.service import _read_proc_self_environ_names

    names = _read_proc_self_environ_names()
    assert "PATH" in names


@pytest.mark.parametrize("leaked_name", [
    "ANTHROPIC_FOO", "MY_TOKEN_X", "SECRETSTUFF", "WEBHOOK_URL", "OPENAI_BASE",
])
def test_check_service_initial_env_has_no_secrets_rejects_each_pattern(leaked_name):
    """#89 (`verified-round1.md` 1-A): `any(pat in k for pat in
    _SECRET_ENV_PATTERNS)` を `any(k.endswith(pat) ...)` に緩める変異は、
    既存 pin が `SOME_SERVICE_API_KEY` (末尾一致) 1 本しか踏まないため
    生存する。前方一致パターン (`ANTHROPIC_`/`OPENAI_`) を含む全 6 パターン
    を個別に踏む。"""
    from agentic_fx.service import _check_service_initial_env_has_no_secrets

    with pytest.raises(RuntimeError, match="secret"):
        _check_service_initial_env_has_no_secrets(
            object(), read_initial_env_names=lambda: {leaked_name, "HOME"})


def test_check_service_initial_env_has_no_secrets_rejects_lowercase_name():
    """#94 (`verified-round1.md` 1-B): `_SECRET_ENV_PATTERNS` は大文字のみ —
    小文字 env 名 (`my_api_key`) が漏れないよう `k.upper()` で照合する。"""
    from agentic_fx.service import _check_service_initial_env_has_no_secrets

    with pytest.raises(RuntimeError, match="secret"):
        _check_service_initial_env_has_no_secrets(
            object(), read_initial_env_names=lambda: {"my_api_key", "HOME"})


def test_check_codex_subscription_expiry_rejects_when_expired(tmp_path):
    """④ (設計書 §1.4、裁定 R4): `chatgpt_subscription_active_until` を
    過ぎていれば起動拒否 (ERROR)。"""
    from agentic_fx.service import _check_codex_subscription_expiry

    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps(
        {"chatgpt_subscription_active_until": "2020-01-01T00:00:00+00:00"}))
    with pytest.raises(RuntimeError, match="expired"):
        _check_codex_subscription_expiry(
            str(auth), clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))


def test_check_codex_subscription_expiry_warns_within_7_days(tmp_path, caplog):
    """④ の境界: 期限まで 7 日以内なら WARNING のみ (起動は継続)。"""
    import logging as _logging

    from agentic_fx.service import _check_codex_subscription_expiry

    active_until = datetime(2026, 1, 5, tzinfo=timezone.utc)
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps(
        {"chatgpt_subscription_active_until": active_until.isoformat()}))
    with caplog.at_level(_logging.WARNING, logger="agentic_fx.service"):
        _check_codex_subscription_expiry(
            str(auth), clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert any("expires soon" in r.message for r in caplog.records)


def test_check_codex_subscription_expiry_passes_when_far_in_future(tmp_path, caplog):
    """④ の裏: 期限まで 7 日超なら WARNING も ERROR も出さない。"""
    import logging as _logging

    from agentic_fx.service import _check_codex_subscription_expiry

    active_until = datetime(2027, 1, 1, tzinfo=timezone.utc)
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps(
        {"chatgpt_subscription_active_until": active_until.isoformat()}))
    with caplog.at_level(_logging.WARNING, logger="agentic_fx.service"):
        _check_codex_subscription_expiry(
            str(auth), clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert caplog.records == []


def test_check_codex_subscription_expiry_missing_key_warns_and_does_not_raise(
        tmp_path, caplog):
    """④: キー欠落は WARNING のみ (fail closed にしない — 形式未実測、裁定 R4)。"""
    import logging as _logging

    from agentic_fx.service import _check_codex_subscription_expiry

    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({}))
    with caplog.at_level(_logging.WARNING, logger="agentic_fx.service"):
        _check_codex_subscription_expiry(str(auth))  # 例外を出さない
    assert any("chatgpt_subscription_active_until" in r.message for r in caplog.records)


def test_check_codex_subscription_expiry_rejects_at_exact_boundary(tmp_path):
    """#90 (`verified-round1.md` 1-A): `active_until <= now` の境界。
    `active_until == now` ちょうどで期限切れ (`<` に緩める変異は
    既存の 4 日差テストだけでは検出できない)。"""
    from agentic_fx.service import _check_codex_subscription_expiry

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps(
        {"chatgpt_subscription_active_until": now.isoformat()}))
    with pytest.raises(RuntimeError, match="expired"):
        _check_codex_subscription_expiry(str(auth), clock=lambda: now)


def test_check_codex_subscription_expiry_warns_at_exact_7_day_boundary(
        tmp_path, caplog):
    """#91 (`verified-round1.md` 1-A): `active_until - now <= timedelta(days=7)`
    の境界。差がちょうど 7 日で WARNING が出る (`<` に緩める変異は既存の
    4 日差テストだけでは検出できない)。"""
    import logging as _logging

    from agentic_fx.service import _check_codex_subscription_expiry

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    active_until = now + timedelta(days=7)
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps(
        {"chatgpt_subscription_active_until": active_until.isoformat()}))
    with caplog.at_level(_logging.WARNING, logger="agentic_fx.service"):
        _check_codex_subscription_expiry(str(auth), clock=lambda: now)
    assert any("expires soon" in r.message for r in caplog.records)


def test_check_codex_subscription_expiry_non_string_value_warns(tmp_path, caplog):
    """#95 (`verified-round1.md` 1-B): `chatgpt_subscription_active_until`
    が数値等の不正型のとき `except (TypeError, ValueError)` 経路で
    WARNING に留める (fail closed にしない、裁定 R4)。"""
    import logging as _logging

    from agentic_fx.service import _check_codex_subscription_expiry

    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"chatgpt_subscription_active_until": 123}))
    with caplog.at_level(_logging.WARNING, logger="agentic_fx.service"):
        _check_codex_subscription_expiry(
            str(auth), clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert any("不正" in r.message for r in caplog.records)


def test_check_cli_version_rejects_when_binary_cannot_be_executed(tmp_path):
    """#84 (`verified-round1.md` 1-A): `except (OSError, TimeoutExpired)`
    経路は `returncode != 0` の 1 例しか実測されていなかった。実行不能な
    bin (存在しないファイル) を渡すと `subprocess.run` が `OSError` を
    送出し、`RuntimeError` に変換されることを pin する。"""
    from agentic_fx.service import _check_cli_version

    missing = tmp_path / "does-not-exist"
    with pytest.raises(RuntimeError, match="version check failed"):
        _check_cli_version(missing)


def test_check_cli_version_rejects_when_binary_times_out(tmp_path, monkeypatch):
    """#84 (`verified-round1.md` 1-A): `subprocess.TimeoutExpired` 経路
    (`timeout=15` の削除は既存 pin では検出されない) — sleep する bin を
    渡し、`_check_cli_version` が呼ぶ `subprocess.run` の `timeout=` 引数を
    強制的に短くする spy で実測する (`_check_cli_version` は関数内
    `import subprocess` で標準 `subprocess` モジュールを束縛するため、
    グローバルな `subprocess.run` を差し替えれば効く)。"""
    import subprocess as _subprocess

    from agentic_fx.service import _check_cli_version

    sleeper = tmp_path / "sleeper.sh"
    sleeper.write_text("#!/bin/sh\nsleep 5\n")
    sleeper.chmod(0o700)

    orig_run = _subprocess.run

    def short_timeout_run(argv, **kwargs):
        kwargs["timeout"] = 0.2
        return orig_run(argv, **kwargs)

    monkeypatch.setattr(_subprocess, "run", short_timeout_run)
    with pytest.raises(RuntimeError, match="version check failed"):
        _check_cli_version(sleeper)


def test_build_app_rejects_llama_swap_when_not_verified(tmp_path):
    """M6: codex+llama_swap で llama_swap_verified=false なら拒否する。"""
    vendor_codex = _find_vendor_codex_bin()
    if vendor_codex is None:
        pytest.skip("vendor native codex バイナリが見つからない (裁定 R5)")
    # llama_swap_verified は既定で False
    root = _root_with_settings(tmp_path, runner={
        "improve": {"backend": "codex", "model": "m"},
        "codex": {"bin": vendor_codex, "provider": "llama_swap",
                  "auth_file": str(tmp_path / "absent")}})
    with pytest.raises(RuntimeError, match="llama_swap_verified"):
        build_app(root, clock=FixedClock(NOW), embedding_fn=FakeEmbedding())


# ============================================================================
# 段 0 F1: 相対 CLI bin の絶対化書き戻し
# ============================================================================

def test_build_app_rewrites_relative_claude_bin_to_absolute_path(tmp_path, monkeypatch):
    """F1: `runner.claude.bin: "claude"` (相対、config の既定値) でも
    `build_app` 後は `app.settings.runner.claude.bin` が絶対パスに
    書き戻されており、`launcher.build_launcher_argv` がその値を
    argv[0] として受理する (相対パスは `ValueError` で拒否される —
    `runners/launcher.py`)。

    fake ELF (`\\x7fELF` マジックバイトのみの実行可能ファイル) を
    `afx-fake-claude` という名前で PATH 上に置き、`_resolve_cli_bin` の
    `shutil.which` 解決対象にする。`_check_cli_version` は `--version` を
    実行するため、fake は shebang 付きシェルスクリプトとして書く
    (ELF マジックはバイナリ判定に使われるのは codex 分岐の
    `require_elf=True` のみ — claude は `require_elf=False` なので実行
    できるファイルであれば足りる)。

    このテストは F1 (書き戻し) だけを見るため、検査⑤
    (`_check_service_initial_env_has_no_secrets`) を no-op にする — 実行
    環境自身 (pytest を起動した Claude Code セッション) が
    `CLAUDE_CODE_*TOKEN*` 等の秘密名パターンに一致する env を export して
    いることがあり、検査⑤本体は実環境に依存させたくない
    (検査⑤本体は別テストが pin 済み)。"""
    import stat

    import agentic_fx.service as service_mod
    from agentic_fx.runners.launcher import build_launcher_argv

    monkeypatch.setattr(
        service_mod, "_check_service_initial_env_has_no_secrets",
        lambda settings, *, read_initial_env_names=None: None)

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    fake_claude = bin_dir / "afx-fake-claude"
    fake_claude.write_text("#!/bin/sh\nexit 0\n")
    fake_claude.chmod(fake_claude.stat().st_mode | stat.S_IEXEC | 0o700)

    import os as _os
    old_path = _os.environ.get("PATH", "")

    import pytest as _pytest
    mp = _pytest.MonkeyPatch()
    try:
        mp.setenv("PATH", f"{bin_dir}:{old_path}")
        root = _root_with_settings(tmp_path, runner={
            "improve": {"backend": "claude", "model": "m"},
            "claude": {"bin": "afx-fake-claude"}})
        app = build_app(root, clock=FixedClock(NOW), embedding_fn=FakeEmbedding())
        try:
            resolved = app.settings.runner.claude.bin
            assert Path(resolved).is_absolute(), (
                f"F1 是正が効いていない — settings.runner.claude.bin が"
                f"相対のまま: {resolved!r}")
            # #86 (`verified-round1.md` 1-A): `.is_absolute()` のみでは
            # 「意図した bin に解決されたか」を見ていない (別の絶対パスに
            # すり替わっても通る) — 実際に PATH 解決したはずの fake_claude
            # 自身に一致することまで pin する。
            assert resolved == str(fake_claude.resolve()), (
                f"意図しない bin に解決されている: {resolved!r} != "
                f"{fake_claude.resolve()!r}")
            # build_launcher_argv が拒否しない (ValueError を出さない) こと
            argv = build_launcher_argv(_os.getpid(), [resolved])
            assert argv[-1] == resolved
        finally:
            app.close()
    finally:
        mp.undo()


def test_build_app_rewrites_relative_codex_bin_to_absolute_path(tmp_path, monkeypatch):
    """F1 の裏 (codex 分岐): `runner.codex.bin` が相対 (PATH 解決) でも
    書き戻し後は絶対パスになる。ELF 必須 (`require_elf=True`) なので
    vendor native codex バイナリを使う (裁定 R5、無ければ skip)。相対値を
    PATH 経由で解決させるため、vendor バイナリへの symlink を専用の
    fake bin dir に置いて PATH の先頭に足す。検査⑤の no-op 理由は
    `test_build_app_rewrites_relative_claude_bin_to_absolute_path` と同じ。"""
    import agentic_fx.service as service_mod

    monkeypatch.setattr(
        service_mod, "_check_service_initial_env_has_no_secrets",
        lambda settings, *, read_initial_env_names=None: None)

    vendor_codex = _find_vendor_codex_bin()
    if vendor_codex is None:
        pytest.skip("vendor native codex バイナリが見つからない (裁定 R5)")
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    link = bin_dir / "afx-fake-codex"
    link.symlink_to(vendor_codex)

    import os as _os
    old_path = _os.environ.get("PATH", "")
    mp = pytest.MonkeyPatch()
    try:
        mp.setenv("PATH", f"{bin_dir}:{old_path}")
        root = _root_with_settings(tmp_path, runner={
            "improve": {"backend": "codex", "model": "m"},
            "codex": {"bin": "afx-fake-codex", "provider": "llama_swap",
                      "auth_file": str(tmp_path / "absent")}},
            improve={"llama_swap_verified": True})
        app = build_app(root, clock=FixedClock(NOW), embedding_fn=FakeEmbedding())
        try:
            resolved = app.settings.runner.codex.bin
            assert Path(resolved).is_absolute(), (
                f"F1 是正が効いていない (codex) — settings.runner.codex.bin が"
                f"相対のまま: {resolved!r}")
        finally:
            app.close()
    finally:
        mp.undo()


def test_check_service_initial_env_has_no_secrets_is_called_for_codex_backend(
        tmp_path, monkeypatch):
    """段 0 申し送り 1: 検査⑤ (`_check_service_initial_env_has_no_secrets`)
    は claude 分岐だけでなく codex 分岐でも呼ばれる (旧実装は claude 分岐
    でしか呼んでいなかった — improve worker は codex 分岐でも同 UID で
    `/proc/<pid>/environ` を読めるため脅威モデルは同じ)。"""
    import agentic_fx.service as service_mod

    def _raise(settings, *, read_initial_env_names=None):
        raise RuntimeError("SOME_SERVICE_API_KEY leaked (codex branch)")

    monkeypatch.setattr(service_mod, "_check_service_initial_env_has_no_secrets", _raise)
    vendor_codex = _find_vendor_codex_bin()
    if vendor_codex is None:
        pytest.skip("vendor native codex バイナリが見つからない (裁定 R5)")
    root = _root_with_settings(tmp_path, runner={
        "improve": {"backend": "codex", "model": "m"},
        "codex": {"bin": vendor_codex, "provider": "llama_swap",
                  "auth_file": str(tmp_path / "absent")}},
        improve={"llama_swap_verified": True})
    with pytest.raises(RuntimeError, match="API_KEY"):
        build_app(root, clock=FixedClock(NOW), embedding_fn=FakeEmbedding())


def test_build_app_wires_codex_subscription_expiry_check(tmp_path, monkeypatch):
    """M13: codex+chatgpt のとき subscription expiry check が呼ばれる。

    段 0 申し送り 1 是正の理由で `_check_service_initial_env_has_no_secrets`
    を no-op にする (上の `test_build_app_does_not_require_credentials_for_codex_llama_swap`
    と同じ理由 — このテストは④の配線だけを見る)。"""
    import agentic_fx.service as service_mod

    monkeypatch.setattr(
        service_mod, "_check_service_initial_env_has_no_secrets",
        lambda settings, *, read_initial_env_names=None: None)

    call_count = [0]

    def _check_expiry(auth_file, *, clock=None):
        call_count[0] += 1

    monkeypatch.setattr(service_mod, "_check_codex_subscription_expiry", _check_expiry)
    vendor_codex = _find_vendor_codex_bin()
    if vendor_codex is None:
        pytest.skip("vendor native codex バイナリが見つからない (裁定 R5)")
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"chatgpt_subscription_active_until": "2027-01-01T00:00:00+00:00"}))
    root = _root_with_settings(tmp_path, runner={
        "improve": {"backend": "codex", "model": "m"},
        "codex": {"bin": vendor_codex, "provider": "chatgpt", "auth_file": str(auth)}})
    build_app(root, clock=FixedClock(NOW), embedding_fn=FakeEmbedding())
    assert call_count[0] > 0, "_check_codex_subscription_expiry が呼ばれていない"



# --- プラン10 Task11f: 起動時 reconcile 配線 pin (B-15/B-7/M6 是正) --------


def test_service_startup_calls_reconcile_sweep_expire_then_approved_plugins_in_order(
        tmp_path, monkeypatch):
    """B-15/M6 の killer: build_app が switch.reconcile_switch_journals →
    switch.sweep_orphans → switch.process_expired_approvals →
    plugin_loader.approved_plugins の順で呼ぶことを、実際の service.py の
    呼び出し経路 (unittest.mock.patch) で確認する。"""
    import unittest.mock as mock
    from agentic_fx.plugin import switch
    from agentic_fx.tools import plugin_loader as plugin_loader_mod

    _init(tmp_path)
    fake = FakeRunner([MissionResult("completed",
                                     {"action": "hold", "reasoning": "w"},
                                     [])])
    with mock.patch.object(switch, "reconcile_switch_journals") as m_reconcile, \
         mock.patch.object(switch, "sweep_orphans") as m_sweep, \
         mock.patch.object(switch, "process_expired_approvals") as m_expire, \
         mock.patch.object(plugin_loader_mod, "approved_plugins", return_value=[]) as m_approved:
        # 逸脱 (11f 実装時の実測): `manager.attach_mock` は「以後の」呼び出し
        # しか `manager.mock_calls` へ記録しない (attach 前の呼び出しは
        # 遡って記録されない) — `unittest.mock` の実装上の性質。プラン骨子は
        # `build_app(...)` の後に `manager.attach_mock` する順で書かれていた
        # ため、そのままでは `call_names == []` になり test 自体が常に失敗
        # する (実測で確認、殺したいはずの M6/M7 変異を注入しなくても red)。
        # `attach_mock` を `build_app` 呼び出し**前**に行う順序へ入れ替えた。
        manager = mock.MagicMock()
        manager.attach_mock(m_reconcile, "reconcile")
        manager.attach_mock(m_sweep, "sweep")
        manager.attach_mock(m_expire, "expire")
        manager.attach_mock(m_approved, "approved")
        build_app(tmp_path, runner=fake, clock=FixedClock(NOW),
                  embedding_fn=FakeEmbedding())
        call_names = [c[0] for c in manager.mock_calls]
        assert call_names == ["reconcile", "sweep", "expire", "approved"]


def test_service_startup_reconcile_failure_does_not_block_startup(tmp_path, monkeypatch):
    """B-15/M7 の killer: reconcile が例外を出しても build_app が完走する
    (try/except を実際に通す — service.py を実行して確認する)。"""
    import unittest.mock as mock
    from agentic_fx.plugin import switch

    _init(tmp_path)
    fake = FakeRunner([MissionResult("completed",
                                     {"action": "hold", "reasoning": "w"},
                                     [])])
    with mock.patch.object(switch, "reconcile_switch_journals",
                           side_effect=RuntimeError("git not found")):
        app = build_app(tmp_path, runner=fake, clock=FixedClock(NOW),
                        embedding_fn=FakeEmbedding())
    assert app is not None


def test_service_startup_reconcile_failure_still_runs_sweep_and_expire(tmp_path, monkeypatch):
    """検収 m10 の pin: 旧稿は reconcile/sweep/expire を単一 try で括って
    いたため、reconcile が例外を出すと同じ起動で sweep も expire も走らな
    かった (acceptance-task11.md m10)。3 呼び出しを別々の try で分離した
    後は、reconcile が失敗しても sweep/expire は独立して実行されること。"""
    import unittest.mock as mock
    from agentic_fx.plugin import switch

    _init(tmp_path)
    fake = FakeRunner([MissionResult("completed",
                                     {"action": "hold", "reasoning": "w"},
                                     [])])
    with mock.patch.object(switch, "reconcile_switch_journals",
                           side_effect=RuntimeError("git not found")) as m_reconcile, \
         mock.patch.object(switch, "sweep_orphans") as m_sweep, \
         mock.patch.object(switch, "process_expired_approvals") as m_expire:
        app = build_app(tmp_path, runner=fake, clock=FixedClock(NOW),
                        embedding_fn=FakeEmbedding())
    assert app is not None
    assert m_reconcile.called
    assert m_sweep.called, "reconcile の失敗で sweep_orphans が道連れになった (m10 の欠陥)"
    assert m_expire.called, "reconcile の失敗で process_expired_approvals が道連れになった (m10 の欠陥)"


def test_improve_tick_and_supervisor_wired_after_task12(tmp_path):
    """R-i9 の分担 (Task 9 = 既定 None フック、Task 12 = 値の配線) が
    Step 4 の diff で実際に満たされていることの検査。B-10 (report-task12.md):
    hook が『渡されている』ではなく『実際に発火する』ことを、
    `Scheduler.tick()` が返す遅延 callable 経由の実行と `improve` コマンドの
    到達の両方で確認する (M1/M2 の真の killer)。"""
    from unittest.mock import patch as _patch
    from agentic_fx.service import _scheduler_tick_once

    _init(tmp_path)
    with _no_real_network():
        app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                        embedding_fn=FakeEmbedding())
        try:
            # M1 の真の killer: on_improve_tick が正しいオブジェクトへ束縛
            # されているか (恒真 assert を避ける — B-10 型 3)。
            assert app.scheduler.on_improve_tick.__self__ is \
                app.improve_supervisor
            # F-5 是正: M1 の真の killer (実発火) を復元する。`on_improve_tick`
            # が単に束縛されているだけでなく、`Scheduler.tick()` が返す遅延
            # callable 経由で実際に呼ばれることを確認する (プラン Step 4 原文、
            # 検収 task12 F-5 で無申告削除が指摘された)。
            #
            # 逸脱 (実測): プラン原文は `_patch.object(ImproveSupervisor,
            # "tick")` (クラス属性) だが、`app.scheduler.on_improve_tick` は
            # build_app 時点で `improve_supervisor.tick` を束縛済みの bound
            # method — クラス属性を後から差し替えても、既に取得済みの bound
            # method は元の未パッチ関数を指したままで patch が効かない
            # (実測: call_count 0 で red)。`app.scheduler.on_improve_tick`
            # 自体を差し替える形へ変更した (ImproveSupervisor は未使用のため
            # import も削除)。
            with _patch.object(app.scheduler, "on_improve_tick") as spy:
                _scheduler_tick_once(app)
                spy.assert_called_once_with(NOW)
            # M2 の真の killer: `improve` コマンドが submit_manual に到達する。
            with _patch.object(app.improve_supervisor, "submit_manual",
                               return_value=42):
                result = app.commands.dispatch("improve")
                assert "42" in result
        finally:
            app.close()
