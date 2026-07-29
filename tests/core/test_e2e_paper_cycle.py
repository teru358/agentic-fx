"""プラン 2 完成条件: 手製 TradeIntent → gate → sizing → executor →
paper 約定 → 遷移 → close が LLM なしで一巡する。"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.config import load_settings
from agentic_fx.core.accounting import record_snapshot
from agentic_fx.core.contracts import (
    Bar, FixedClock, InstrumentSpec, Origin, Quote, TradeIntent,
)
from agentic_fx.core.executor import Executor
from agentic_fx.core.notifier import Notifier
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.core.scheduler import Scheduler
from agentic_fx.store import missions, orders, snapshots
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.state import StateStore

WED = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")
SPEC = InstrumentSpec("USDJPY", 0.01, 0.01, 50.0, 0.01, 100_000)


def test_full_paper_cycle(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    record_snapshot(conn, now=WED, balance=1_000_000, equity=1_000_000)
    state = StateStore(tmp_path / "state.json")
    activity = ActivityLog(tmp_path / "activity.log")
    clock = FixedClock(WED)
    bars = {}
    executor = Executor(conn=conn,
                        broker=PaperBroker(conn, SETTINGS, clock),
                        settings=SETTINGS, state_store=state,
                        activity=activity,
                        notifier=Notifier(enabled=False, webhook_url=None),
                        clock=clock,
                        quote_fn=lambda p: Quote(p, 148.49, 148.51, WED, "t"),
                        spec_fn=lambda p: SPEC)
    sched = Scheduler(conn=conn, executor=executor, settings=SETTINGS,
                      state_store=state, activity=activity,
                      bars_fn=lambda p: bars.get(p),
                      on_trade_mission=lambda: None,
                      on_news_cycle=lambda: None,
                      on_econ_cycle=lambda: None)

    # 1. 手製 intent (指値 open)
    intent = TradeIntent.from_llm_dict(
        {"action": "open", "pair": "USDJPY", "direction": "long",
         "entry_type": "limit", "horizon": "day", "limit_price": 148.20,
         "expires_in": "6h", "stop_loss": 147.80, "take_profit": 149.00,
         "confidence": 0.8, "reasoning": "e2e"},
        origin=Origin.SCHEDULER)
    mid = missions.start(conn, "trade", "local", "manual", WED)
    out = executor.handle_intent(intent, mid)
    assert out["result"] == "pending"
    oid = out["order_id"]
    assert orders.get(conn, oid)["status"] == "pending_fill"

    # 2. 指値到達 → open
    bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.30, 148.32, 148.18,
                         148.25, 100)
    sched.tick(WED + timedelta(minutes=1))
    assert orders.get(conn, oid)["status"] == "open"

    # 3. TP 到達 → closed (利益)。バーは新しい ts (同一バー再処理防止のため)
    bars["USDJPY"] = Bar("USDJPY", "1m", WED + timedelta(minutes=1),
                         148.90, 149.10, 148.85, 149.05, 100)
    sched.tick(WED + timedelta(minutes=2))
    row = orders.get(conn, oid)
    assert row["status"] == "closed"
    assert row["close_reason"] == "tp"
    assert row["realized_pnl"] > 0

    # 4. 監査痕跡: intent 記録 (accepted)・activity・snapshot 更新
    intent_row = conn.execute("SELECT * FROM trade_intents").fetchone()
    assert conn.execute("SELECT COUNT(*) c FROM trade_intents").fetchone()["c"] == 1
    assert intent_row["gate_result"] == "accepted"
    log = (tmp_path / "activity.log").read_text(encoding="utf-8")
    assert "limit_placed" in log and "limit_filled" in log \
        and "order_closed" in log
    # close 直後 (この tick の mark-to-market は close 前 = unrealized 込み)
    assert snapshots.latest(conn)["equity"] > 1_000_000

    # 5. 次 tick の mark-to-market で realized_pnl が balance に反映されている
    # ことを確認 (open ポジションが無いので stale-bar 分岐は関与しない)
    sched.tick(WED + timedelta(minutes=3))
    snap = snapshots.latest(conn)
    assert snap["balance"] == pytest.approx(1_000_000 + row["realized_pnl"])
