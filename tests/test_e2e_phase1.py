"""Phase 1 完成条件: 学習モードで scheduler tick → Mission → intent →
ペーパー発注 → 約定 → reflection まで LLM なし (FakeRunner) で自走する。"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from agentic_fx.core.contracts import Bar, FixedClock
from agentic_fx.runners.base import MissionResult
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.service import build_app, run_init

from tests.test_service_app import _no_real_network

WED = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)

OPEN_INTENT = {"action": "open", "pair": "USDJPY", "direction": "long",
               "entry_type": "limit", "horizon": "day",
               "limit_price": 148.20, "expires_in": "6h",
               "stop_loss": 147.80, "take_profit": 149.00,
               "reasoning": "e2e"}


HOLD_INTENT = {"action": "hold", "reasoning": "様子見"}


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
    bars = {}
    quote_fn = lambda p: Quote(p, 148.49, 148.51, WED, "test")  # noqa: E731
    # 上書き 2 補正: 実 InstrumentSpec は base_currency/quote_currency も
    # 必須 (逐語テストの 6 引数だけでは TypeError)。USDJPY の実値を明示する。
    spec_fn = lambda p: InstrumentSpec(p, 0.01, 0.01, 50.0, 0.01,  # noqa: E731
                                       100_000, "USD", "JPY")
    # build_app の注入点を使う (build 後の patch は bound クロージャに届かない)
    app = build_app(tmp_path, runner=fake, clock=FixedClock(WED),
                    quote_fn=quote_fn, spec_fn=spec_fn,
                    bars_fn=lambda p: bars.get(p))

    with patch.object(app.provider, "healthcheck", return_value="test"), \
         _no_real_network():
        # tick 1: 毎時 Mission → 指値発注
        app.scheduler.tick(WED)
        rows = app.conn_core.execute("SELECT * FROM orders").fetchall()
        assert len(rows) == 1 and rows[0]["status"] == "pending_fill"

        # tick 2: 約定バー → open
        bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.30, 148.35, 148.15,
                             148.25, 100)
        app.scheduler.tick(WED + timedelta(minutes=1))
        assert app.conn_core.execute(
            "SELECT status FROM orders").fetchone()["status"] == "open"

        # tick 3: TP バー (新しい ts を渡す — 同一バー再処理ガードは
        # unit 側で検証済み: tests/core/test_scheduler.py)。→ closed
        bars["USDJPY"] = Bar("USDJPY", "1m", WED + timedelta(minutes=1),
                             148.90, 149.10, 148.85, 149.05, 100)
        app.scheduler.tick(WED + timedelta(minutes=2))
        row = app.conn_core.execute("SELECT * FROM orders").fetchone()
        assert row["status"] == "closed" and row["realized_pnl"] > 0
        # fix round 1 F3: 正の PnL なら何でも通ってしまうのを塞ぐ — SL 逆行
        # 等ではなく TP 到達でクローズしたことを close_reason で固定する
        assert row["close_reason"] == "tp"
        closed_order_id = row["id"]

        # tick 4 (1 時間後): 2 周目 trade (hold) → reflection 生成
        bars["USDJPY"] = Bar("USDJPY", "1m",
                             WED + timedelta(hours=1),
                             149.00, 149.05, 148.95, 149.00, 100)
        app.scheduler.tick(WED + timedelta(hours=1, minutes=1))
        refl = app.conn_core.execute("SELECT * FROM reflections").fetchall()
        assert len(refl) == 1
        # fix round 1 F3: 件数だけでは誤対象・別内容でも通ってしまうのを塞ぐ
        assert refl[0]["order_id"] == closed_order_id
        assert refl[0]["content"] == "振り返り"

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
