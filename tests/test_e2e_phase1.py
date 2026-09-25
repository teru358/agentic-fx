"""Phase 1 完成条件: 学習モードで scheduler tick → Mission → intent →
ペーパー発注 → 約定 → reflection まで LLM なし (FakeRunner) で自走する。"""
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from agentic_fx.core.contracts import Bar, FixedClock
from agentic_fx.runners.base import MissionResult
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.service import _scheduler_tick_once, build_app, run_init

from tests.test_service_app import _no_real_network
from tests.store.test_rag import FakeEmbedding

WED = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)

OPEN_INTENT = {"action": "open", "pair": "USDJPY", "direction": "long",
               "entry_type": "limit", "horizon": "day",
               "limit_price": 148.20, "expires_in": "6h",
               "stop_loss": 147.80, "take_profit": 149.00,
               "reasoning": "e2e"}


HOLD_INTENT = {"action": "hold", "reasoning": "様子見"}


def _seed_decision_bar_1h(conn, bar_time, pair="USDJPY"):
    """判断足 (1h) の watermark を ingest 経由でなく直接補う
    (tick1 直後のコメント参照)。"""
    conn.execute(
        "INSERT INTO ohlcv_cache (symbol, interval, bar_time, open, high, "
        "low, close, volume, source) VALUES (?,?,?,?,?,?,?,?,?)",
        (pair, "1h", bar_time.isoformat(), 148.0, 148.1, 147.9, 148.0, 1.0,
         "yfinance"))
    conn.commit()


def test_phase1_full_cycle(tmp_path):
    (tmp_path / "config").mkdir()
    src = open("config/settings.yaml.example", encoding="utf-8").read()
    (tmp_path / "config" / "settings.yaml.example").write_text(src)
    # 上書き 1: _check_llama_swap も patch する (素のままだと実 httpx.get /
    # POST timeout=120 の実待ちが混入し得る)
    with patch("agentic_fx.service.PriceProvider") as pp, \
         patch("agentic_fx.service._check_llama_swap"):
        pp.return_value.healthcheck.return_value = "yfinance"
        run_init(tmp_path)

    # 実行順に合わせた結果列 (#codex 指摘: reflection は 2 周目の trade の後):
    # tick1: trade → OPEN / (closed なし、reflection Mission は走らない)
    # tick4: trade → HOLD → reflection → content
    fake = FakeRunner([
        MissionResult("completed", OPEN_INTENT, []),
        MissionResult("completed", HOLD_INTENT, []),
        MissionResult("completed", {"content": "振り返り"}, []),
    ])

    from agentic_fx.core.contracts import InstrumentSpec, Quote
    bars = [Bar("USDJPY", "1h", WED - timedelta(hours=2),
                148.00, 148.10, 147.90, 148.05, 100)]
    quote_fn = lambda p: Quote(p, 148.49, 148.51, WED, "test")  # noqa: E731
    # 上書き 2 補正: 実 InstrumentSpec は base_currency/quote_currency も
    # 必須 (逐語テストの 6 引数だけでは TypeError)。USDJPY の実値を明示する。
    spec_fn = lambda p: InstrumentSpec(p, 0.01, 0.01, 50.0, 0.01,  # noqa: E731
                                       100_000, "USD", "JPY")
    # build_app の注入点を使う (build 後の patch は bound クロージャに届かない)
    app = build_app(tmp_path, runner=fake, clock=FixedClock(WED),
                    quote_fn=quote_fn, spec_fn=spec_fn,
                    embedding_fn=FakeEmbedding())
    app.scheduler.on_improve_tick = None
    # The scheduler reads the committed cache.  Feed the same fake source into
    # ingest, then use its prepare -> commit -> scheduler production route.
    app.ingest.fetch = lambda pair, interval, start, end, **_: [
        bar for bar in bars if bar.interval == interval]

    # プラン 8 Task 13: on_trade_mission は supervisor 経由で非同期実行される
    # ため、supervisor を起動する必要がある。
    app.supervisor.start()
    try:
        with patch.object(app.provider, "healthcheck", return_value="test"), \
             patch.object(app.trade_loop.provider, "healthcheck",
                          return_value="test"), \
             _no_real_network():
            # tick 1: fake source から ingest が commit した確定 1h 足で
            # cron Mission → 指値発注
            _scheduler_tick_once(app)
            time.sleep(0.5)  # supervisor スレッドが job を実行するまで待機
            rows = app.conn_core.execute("SELECT * FROM orders").fetchall()
            assert len(rows) == 1 and rows[0]["status"] == "pending_fill"

            # [outage-stop-and-backfill]: OutageStateMachine は判断足
            # (1h, hard key) の watermark 停滞も見る。このフィクスチャは
            # 元々 1h バーを tick1 で 1 回 (2 時間前の値) 流すだけだったため、
            # 数分後には判断足が「停滞」判定されてしまう (実装は正しい —
            # 本物の feed が判断足を更新し続けなければ同じ degraded が起きる)。
            # tick2〜tick4 の narrative (指値約定・TP クローズ・2 周目 cron)
            # 自体は判断足の更新頻度と無関係なので、ingest の fetch 経路を
            # 経由せず直接 DB に「鮮度だけ十分な」1h バーを 1 本補い、cron
            # watermark cursor もこの値に合わせておく (これを合わせないと
            # 直後の tick で「新しい判断足が来た」と誤認し、想定外の cron
            # Mission が余分に 1 回発火してしまう — 本テストの目的である
            # tick4 の 2 周目 cron 発火とは無関係な artifact)。
            _seed_decision_bar_1h(app.conn_core, WED - timedelta(minutes=59))
            app.scheduler._cron_watermarks[("USDJPY", "1h")] = (
                WED - timedelta(minutes=59))
            # ingest 自身の内部 backoff (`next_probe_at`) は直接 SQL で入れた
            # この行の raw MAX を「次の期待確定時刻」の計算に使ってしまう
            # (confirmed 判定を経ない) ため、そのままだと tick2/tick3 で
            # 1h を再 probe → 空応答 → 次の期待時刻が tick4 より後ろに
            # 弾かれる。tick2/tick3 の間はスキップし、tick4 でちょうど
            # 再 probe されるよう明示的に置く (fetch 経路を経由しない
            # 直接挿入ゆえの補正)。
            app.ingest.next_probe_at[("USDJPY", "1h")] = (
                WED + timedelta(minutes=4))

            # tick 2: 約定バー → open
            bars[:] = [Bar("USDJPY", "1m", WED, 148.30, 148.35, 148.15,
                           148.25, 100)]
            app.clock = FixedClock(WED + timedelta(minutes=2))
            _scheduler_tick_once(app)
            time.sleep(0.5)
            assert app.outage.state == "ready"
            assert app.conn_core.execute(
                "SELECT status FROM orders").fetchone()["status"] == "open"

            # tick 3: TP バー (新しい ts を渡す — 同一バー再処理ガードは
            # unit 側で検証済み: tests/core/test_scheduler.py)。→ closed
            bars[:] = [Bar("USDJPY", "1m", WED + timedelta(minutes=1),
                           148.90, 149.10, 148.85, 149.05, 100)]
            app.clock = FixedClock(WED + timedelta(minutes=3))
            _scheduler_tick_once(app)
            time.sleep(0.5)
            row = app.conn_core.execute("SELECT * FROM orders").fetchone()
            assert row["status"] == "closed" and row["realized_pnl"] > 0
            # fix round 1 F3: 正の PnL なら何でも通ってしまうのを塞ぐ — SL 逆行
            # 等ではなく TP 到達でクローズしたことを close_reason で固定する
            assert row["close_reason"] == "tp"
            closed_order_id = row["id"]

            # tick 4 (1 時間後): 2 周目 trade (hold) → reflection 生成
            # [outage-stop-and-backfill]: 1m (hard key) の bar_time は
            # tick 時刻 (13:01:00) から見て確定済み (`bar_time + 1分 + 猶予
            # 30秒 <= now`) でなければならない — 13:00:00 だと 13:01:30 まで
            # 確定しないため、13:01:00 の tick では停滞判定され degraded に
            # なる (実装は正しい: 60 秒間隔の polling を想定した設計であり、
            # このフィクスチャのような 1 時間の tick 間隔ジャンプでは
            # bar_time を意図的に確定境界の内側に置く必要がある)。
            bars[:] = [
                Bar("USDJPY", "1m", WED + timedelta(minutes=59),
                    149.00, 149.05, 148.95, 149.00, 100),
                Bar("USDJPY", "1h", WED,
                    149.00, 149.05, 148.95, 149.00, 100),
            ]
            app.clock = FixedClock(WED + timedelta(hours=1, minutes=1))
            _scheduler_tick_once(app)
            time.sleep(0.5)
            refl = app.conn_core.execute("SELECT * FROM reflections").fetchall()
            assert len(refl) == 1
            # fix round 1 F3: 件数だけでは誤対象・別内容でも通ってしまうのを塞ぐ
            assert refl[0]["order_id"] == closed_order_id
            assert refl[0]["content"] == "振り返り"
    finally:
        app.supervisor.shutdown(drain_exc=RuntimeError("test shutdown"))

    # 監査痕跡: 全 Mission が意図した status で完了している (#codex 指摘)
    missions = app.conn_core.execute(
        "SELECT loop, status FROM missions ORDER BY id").fetchall()
    assert [(m["loop"], m["status"]) for m in missions] == [
        ("trade", "completed"), ("trade", "completed"),
        ("reflection", "completed")]
    act = (tmp_path / "logs" / "activity.log").read_text(encoding="utf-8")
    for ev in ("decision", "limit_placed", "limit_filled", "order_closed",
               "reflection_created"):
        assert ev in act
    assert "intent_parse_failed" not in act  # 途中の Mission 失敗を見逃さない
