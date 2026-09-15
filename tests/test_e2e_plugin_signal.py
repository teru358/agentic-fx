"""E2E: 承認済み strategy plugin → シグナル起動 → 前倒し Mission (プラン 7 Task 10)。

brief 逐語のシナリオ 5 ステップを 1 本の流れで検証する。`build_app` を使う
(Task 8 の `tests/test_service_app.py` が最も近い先例 — フィクスチャの組み方
をそこから流用する)。

① tmp_path に strategy plugin (SMA クロス 1h・levels・pairs: [USDJPY]) を
   置き `submit_plugin` (pytest_runner/run_in_sample_fn は fake — 承認評価
   そのものは Task 6 の関心であり、ここでは高速化のためだけに fake する)
   → `decide(approved)`。
② 1m 履歴 (クロス 1 回・市場オープン時間帯) + オープンポジション 1 件
   (D2 条件) を投入する。
③ `SignalProducer.evaluate_due_plugins` の本番経路
   (`app.scheduler.on_signal_maintenance`) を **非分格子の now** で直接
   呼ぶ (sandbox は実実行 — fake しない。承認済み plugin が実際に
   PluginSession サブプロセスで評価されることの証跡)。
④ scheduler tick → D2 + pending signal → `"signal"` 起動 → claim →
   set_trigger → プロンプト注入 (FakeRunner の受信プロンプトで assert) →
   intent → Risk Gate → ペーパー発注 (market, 即時 open)。
⑤ 中間状態を含めて assert する: signals 行が consumed / missions.trigger
   == "signal:<plugin名>" / orders に発注到達 / cron 締切
   (`scheduler._last_cron_trade`) がシグナル起動で変化しない。

自己レビュー用の変異ピン: `test_producer_step_is_load_bearing_for_signal`
が、③ (producer 呼び出し) をスキップすると signal トリガーの Mission が
一切起動しないこと (= このテストが「何もしなくても green」な空洞テストで
はないこと) を確認する。
"""
from __future__ import annotations

import json
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from agentic_fx.core.contracts import Bar, FixedClock, InstrumentSpec, Quote
from agentic_fx.plugin import approval, strategy_gate
from agentic_fx.plugin.gate_pytest import GateResult
from agentic_fx.plugin.loader import discover
from agentic_fx.plugin.resolve import ResolvedIndicatorSet
from agentic_fx.runners.base import MissionResult
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.service import build_app, run_init
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import orders as orders_store
from agentic_fx.store.db import connect, init_db
from agentic_fx.tools import plugin_loader

from tests.store.test_rag import FakeEmbedding
from tests.test_service_app import _no_real_network

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SMA_CROSS_DIR = _REPO_ROOT / "docs" / "examples" / "plugins" / "sma_cross"

# H は 1h 境界に整列した水曜 12:00 UTC (market open — 他テストと同じ選定理由)。
H = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
# ③ producer 呼び出し: 非分格子の now (バケット進行検出の本番経路)。
NOW_PRODUCER = H + timedelta(hours=1, seconds=3)
# ④ tick 呼び出し: producer と同一バケット floor 内 (追加の実サブプロセス
# 起動を発生させない) かつ cron 締切 (1h) 未到来の時刻。
NOW_TICK = H + timedelta(hours=1, minutes=5)

# [indicator-consumption-wiring] T3 Step 3-1: `_validate_strategy` の
# `resolved` はキーワード必須。sma_cross は依存 0 本なので空集合で足りる。
_EMPTY = ResolvedIndicatorSet.empty(Path("/nonexistent/plugins"))


def _install_settings(root: Path) -> None:
    (root / "config").mkdir()
    src = (_REPO_ROOT / "config" / "settings.yaml.example").read_text(
        encoding="utf-8")
    (root / "config" / "settings.yaml.example").write_text(src)
    with patch("agentic_fx.service.PriceProvider") as pp, \
         patch("agentic_fx.service._check_llama_swap"):
        pp.return_value.healthcheck.return_value = "yfinance"
        run_init(root)


def _write_plugin(root: Path) -> Path:
    """docs/examples/plugins/sma_cross を tmp_path/plugins/sma_cross へ複製する。"""
    d = root / "plugins" / "sma_cross"
    d.mkdir(parents=True)
    for name in ("plugin.py", "config.yaml", "test_plugin.py"):
        shutil.copy(_SMA_CROSS_DIR / name, d / name)
    return d


def _ok_pytest_runner(_path: Path) -> GateResult:
    return GateResult(passed=True, returncode=0, stdout_tail="4 passed",
                      duration_sec=0.01)


def _fake_run_in_sample(settings_arg, **kwargs) -> dict:
    del settings_arg, kwargs
    # ① fake — 承認バックテストそのものは Task 6 の関心。EVALUABLE_MIN_TRADES
    # (30) 以上にしておき evaluable=True で承認できることも一応満たしておく。
    return {"trades": 30, "pf": 1.5, "win_rate": 0.55, "avg_r": 0.3,
            "max_drawdown": 0.05, "total_pnl": 500.0, "evaluable": True,
            "fallback_spread_used": False}


def _submit_and_approve(root: Path) -> None:
    """① strategy plugin を承認済みにする → decide(approved)。

    [profitability-floor] T1 Step 1-7 (2026-09-13、codex C1): `submit_
    plugin` (legacy API) は strategy candidate を拒否するようになった
    (固定 holdout を含む共有ゲートを経由しない corridor にフロアが課され
    ない抜け道を塞ぐ、pin は tests/plugin/test_approval.py::
    test_submit_plugin_rejects_strategy_kind_f6_5)。ここは承認評価その
    ものではなく e2e シナリオの前提 (承認済み plugin) を作る目的の fake
    corridor なので、`_validate_strategy` を直接呼んで承認 payload を
    組み立て (`submit_plugin` の payload 構築と同形)、
    `approvals_store.create` で直接 approval 行を作る。

    build_app より前に呼ぶこと — plugin の反映は次回起動時のみ (hot reload
    しない、Task 3 の brief 明示)。build_app 呼び出し時点で承認済みでなけ
    れば `approved_plugins` が拾わない。
    """
    from agentic_fx.config import load_settings

    d = _write_plugin(root)
    settings = load_settings(root / "config" / "settings.yaml")
    conn = connect(root / "data" / "agentic.db")
    init_db(conn)

    metas = discover(root / "plugins")
    meta = next(m for m in metas if m.name == "sma_cross")
    assert meta.kind == "strategy" and meta.timeframe == "1h"
    assert meta.pairs == ("USDJPY",)

    metrics, evaluable = approval._validate_strategy(
        conn, meta, settings=settings, now=H,
        run_in_sample_fn=_fake_run_in_sample, resolved=_EMPTY)
    payload = {
        "name": meta.name, "kind": meta.kind,
        "content_hash": meta.content_hash,
        "test_file_hash": "test",
        "pytest": {"returncode": 0, "summary": "1 passed"},
        "metrics": metrics, "evaluable": evaluable,
        "eval_source": settings.backtest.eval_source,
        "base_interval": settings.backtest.dataset().base_interval,
        "eval_timeframe": strategy_gate._eval_timeframe(meta.timeframe),
        "live_source": settings.plugin.producer_source,
        "note": "e2e fake",
    }
    approval_id = approvals_store.create(
        conn, kind="plugin", payload=payload, now=H)
    approvals_store.apply_decision(conn, approval_id, status="approved",
                           decided_by="human_reviewer", now=H)
    conn.close()


def _seed_crossover_history(conn, *, source: str) -> None:
    """② 1m 履歴投入: バケット H で SMA(5) が SMA(20) を上抜けるクロスを
    1 回だけ作る (docs/examples/plugins/sma_cross/test_plugin.py の
    `test_evaluate_opens_long_on_upward_crossover` と同じ手計算値 — 20 本の
    緩やかな下降 (120→101) の直後に急騰 (200) させる)。1 時間バケットにつき
    1 本の 1m バーで足りる (resample はバケット内の存在する行だけを集計する
    — `load_resampled_frame` の完成バケット判定は「行がバケット内に存在す
    るか」のみを見る)。
    """
    from agentic_fx.store import ohlcv as ohlcv_store

    closes = [120.0 - i for i in range(20)] + [200.0]  # 21 本 (H-20h .. H)
    # producer が読むのはライブ source なので**キャッシュ側**へ書く
    # (プラン 9 Task 16 の分割以降、ライブ source は履歴 API が拒否する)。
    bars = [Bar("USDJPY", "1m", H - timedelta(hours=20 - i),
                price, price, price, price, 10.0)
            for i, price in enumerate(closes)]
    ohlcv_store.upsert_cache_bars(conn, bars, source=source)


def _seed_open_position(conn) -> int:
    """② D2 条件用のオープンポジション 1 件 (SL/TP 幅は小さく — 総リスク
    上限 (max_total_risk_pct 1.5%) を圧迫しないよう最小ロットにする)。"""
    return orders_store.insert(
        conn, pair="USDJPY", direction="long", entry_type="market",
        horizon="day", status="open", now=H - timedelta(hours=2),
        quantity=0.01, avg_fill_price=145.00, stop_loss=144.80,
        take_profit=145.40, filled_at=(H - timedelta(hours=2)).isoformat())


def _build_app(tmp_path, *, runner, bars: dict):
    return build_app(
        tmp_path, runner=runner, clock=FixedClock(NOW_TICK),
        quote_fn=lambda p: Quote(p, 148.49, 148.51, NOW_TICK, "test"),
        spec_fn=lambda p: InstrumentSpec(p, 0.01, 0.01, 50.0, 0.01,
                                         100_000, "USD", "JPY"),
        bars_fn=lambda p: bars.get(p), embedding_fn=FakeEmbedding())


def test_approved_strategy_signal_triggers_advanced_mission(tmp_path):
    _install_settings(tmp_path)
    _submit_and_approve(tmp_path)  # ①

    open_intent = {
        "action": "open", "pair": "USDJPY", "direction": "long",
        "entry_type": "market", "horizon": "day",
        "stop_loss": 148.31, "take_profit": 148.91,
        "reasoning": "signal e2e"}
    fake = FakeRunner([MissionResult("completed", open_intent, [])])
    bars = {"USDJPY": Bar("USDJPY", "1m", NOW_TICK, 145.05, 145.10, 145.00,
                          145.05, 10.0)}

    app = _build_app(tmp_path, runner=fake, bars=bars)
    # sma_cross が実際に承認済みとして拾われていること (③より前の中間状態)
    assert any(m.name == "sma_cross" for m in
              plugin_loader.approved_plugins(
                  app.conn_core, tmp_path / "plugins",
                  settings=app.settings).inventory.metas)

    _seed_crossover_history(app.conn_core,
                            source=app.settings.plugin.producer_source)
    order_id = _seed_open_position(app.conn_core)

    # プラン 8 Task 13: on_trade_mission は supervisor 経由で非同期実行される
    # ため、supervisor を起動する必要がある。
    app.supervisor.start()
    try:
        with _no_real_network(), \
             patch.object(app.provider, "healthcheck", return_value="yfinance"), \
             patch.object(app.trade_loop.provider, "healthcheck",
                          return_value="yfinance"):
            # ③ producer 本番経路を非分格子 now で直接呼ぶ (実 sandbox 実行)。
            started = time.perf_counter()
            app.scheduler.on_signal_maintenance(NOW_PRODUCER)
            elapsed_producer = time.perf_counter() - started

            signal_row = app.conn_core.execute(
                "SELECT * FROM signals").fetchone()
            assert signal_row is not None  # 中間状態①: signals pending 1 件
            assert signal_row["status"] == "pending"
            assert signal_row["plugin"] == "sma_cross"
            assert signal_row["bar_ts"] == H.isoformat()
            assert signal_row["kind"] == "strategy"
            # 実 plugin (sma_cross) の evaluate() が実際に呼ばれた証跡: 捏造
            # payload ではなく本物のクロス判定結果であること。stop_loss/
            # take_profit の両方が入っていること自体が Task 6 の教訓の再確認
            # (take_profit が無いと Risk Gate の RR ルールで黙殺される)。
            payload = json.loads(signal_row["payload_json"])
            assert payload["direction"] == "long"
            assert payload["stop_loss"] is not None
            assert payload["take_profit"] is not None

            # ④ cron 締切を「直前に済んだ」ことにして signal 起動だけを見る
            # (test_service_app.py の F1(b) と同じ手法)。
            app.scheduler._last_cron_trade = NOW_PRODUCER
            last_cron_before = app.scheduler._last_cron_trade

            started = time.perf_counter()
            app.scheduler.tick(NOW_TICK)
            time.sleep(0.5)  # supervisor スレッドが job を実行するまで待機
            elapsed_tick = time.perf_counter() - started
    finally:
        app.supervisor.shutdown(drain_exc=RuntimeError("test shutdown"))

    print(f"\n[Task 10 実測] on_signal_maintenance (実 sandbox): "
         f"{elapsed_producer:.3f}s / tick: {elapsed_tick:.3f}s")

    # ⑤ assert (brief 逐語)
    # -- signals 行が consumed
    signal_row = app.conn_core.execute(
        "SELECT * FROM signals WHERE id=?", (signal_row["id"],)).fetchone()
    assert signal_row["status"] == "consumed"

    # -- missions.trigger == "signal:<plugin名>"
    mission_row = app.conn_core.execute(
        "SELECT * FROM missions WHERE loop='trade' ORDER BY id DESC "
        "LIMIT 1").fetchone()
    assert mission_row["trigger"] == "signal:sma_cross"
    assert mission_row["status"] == "completed"

    # -- プロンプト搭載: FakeRunner の受信プロンプトに claim した signal が
    #    実際に注入されていること
    assert len(fake.missions) == 1
    prompt = fake.missions[0].prompt
    assert "sma_cross" in prompt
    assert "USDJPY" in prompt
    assert H.isoformat() in prompt

    # -- orders に発注到達 (新規 market 建玉が open で追加された)
    new_orders = app.conn_core.execute(
        "SELECT * FROM orders WHERE id != ? ORDER BY id", (order_id,)
    ).fetchall()
    assert len(new_orders) == 1
    assert new_orders[0]["status"] == "open"
    assert new_orders[0]["pair"] == "USDJPY"

    # -- Risk Gate/承認ゲート/origin 検証を一切迂回していないこと
    #    (trade_intents.gate_result == "accepted" = Risk Gate を実際に通った)
    intent_row = app.conn_core.execute(
        "SELECT * FROM trade_intents ORDER BY id DESC LIMIT 1").fetchone()
    assert intent_row["gate_result"] == "accepted"

    # -- cron 締切不変: signal 起動後の tick で cron Mission が前倒しされない
    assert app.scheduler._last_cron_trade == last_cron_before


def test_producer_step_is_load_bearing_for_signal(tmp_path):
    """D2 (pending signal 必須) の回帰ピン: producer 評価 (`on_signal_
    maintenance`) を no-op に差し替える (tick() 自身が配線しているため、
    単に呼び出しを省略するだけでは skip にならない — advisor 指摘) と、
    signals テーブルが空のままなので D2 が不成立になり、signal トリガーの
    trade Mission が一切起動しないこと。

    **主 E2E の「空洞テストでないこと」自体はこのテストでは示せない**
    (advisor 指摘): この回帰ピンで到達不能になるのは D2 の pending signal
    条件だけであり、主 E2E の consumed/trigger/orders 系の終端 assert は
    tick() が producer を内部で自走させる配線 (Task 8) により、直接
    `on_signal_maintenance` を呼ばなくても再現できてしまう (このテストの
    実装過程で実際に踏んだ落とし穴 — 最初は「呼び出しを省略するだけ」で
    書いたが signals が 1 件出来てしまい失敗した)。主 E2E が③ (非分格子
    now でのバケット進行検出の本番経路) を本当に通っていることのピンは、
    主 E2E 内の `signal_row["status"] == "pending"` 中間状態 assert
    (`on_signal_maintenance(NOW_PRODUCER)` 直後、tick() 呼び出しより前)
    が担っている — report に自己レビューの変異注入結果を記録する。
    """
    _install_settings(tmp_path)
    _submit_and_approve(tmp_path)

    fake = FakeRunner([])
    bars = {"USDJPY": Bar("USDJPY", "1m", NOW_TICK, 145.05, 145.10, 145.00,
                          145.05, 10.0)}
    app = _build_app(tmp_path, runner=fake, bars=bars)

    _seed_crossover_history(app.conn_core,
                            source=app.settings.plugin.producer_source)
    _seed_open_position(app.conn_core)

    # ③ を意図的に無効化する (producer skip) — `on_signal_maintenance` は
    # tick() 自身にも配線されているため (Task 8)、呼び出しを単に省略する
    # だけでは skip にならない。closure を no-op に差し替えて、producer が
    # 一度も評価されない状態を作る。
    app.scheduler.on_signal_maintenance = lambda now: None
    with _no_real_network(), \
         patch.object(app.provider, "healthcheck", return_value="yfinance"):
        app.scheduler._last_cron_trade = NOW_PRODUCER
        app.scheduler.tick(NOW_TICK)

    assert app.conn_core.execute(
        "SELECT COUNT(*) c FROM signals").fetchone()["c"] == 0
    assert fake.missions == []  # signal トリガーの Mission は起動しない
    mission_row = app.conn_core.execute(
        "SELECT COUNT(*) c FROM missions WHERE loop='trade'").fetchone()
    assert mission_row["c"] == 0
