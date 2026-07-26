from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentic_fx.activity import ActivityLog
from agentic_fx.config import load_settings
from agentic_fx.core.accounting import record_snapshot
from agentic_fx.core.contracts import (
    Bar, FixedClock, InstrumentSpec, Mode, Origin, Quote, TradeIntent,
)
from agentic_fx.core.executor import Executor
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.core.scheduler import Scheduler
from agentic_fx.store import missions, orders
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.state import StateStore

WED = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
SAT = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")
SPEC = InstrumentSpec("USDJPY", 0.01, 0.01, 50.0, 0.01, 100_000)
QUOTE = Quote("USDJPY", 148.49, 148.51, WED, "test")


class Env:
    def __init__(self, tmp_path, quote_fn=None):
        from agentic_fx.core.notifier import Notifier
        self.conn = connect(tmp_path / "t.db")
        init_db(self.conn)
        record_snapshot(self.conn, now=WED, balance=1_000_000, equity=1_000_000)
        self.state = StateStore(tmp_path / "state.json")
        self.tmp_path = tmp_path
        self.bars: dict[str, Bar] = {}
        self.trade_calls = 0
        self.news_calls = 0
        clock = FixedClock(WED)
        self.executor = Executor(
            conn=self.conn, broker=PaperBroker(self.conn, SETTINGS, clock),
            settings=SETTINGS, state_store=self.state,
            activity=ActivityLog(tmp_path / "a.log"),
            notifier=Notifier(enabled=False, webhook_url=None), clock=clock,
            quote_fn=quote_fn or (lambda p: QUOTE), spec_fn=lambda p: SPEC)
        self.sched = Scheduler(
            conn=self.conn, executor=self.executor, settings=SETTINGS,
            state_store=self.state, activity=ActivityLog(tmp_path / "a.log"),
            bars_fn=lambda p: self.bars.get(p),
            on_trade_mission=self._trade, on_news_cycle=self._news)

    def _trade(self):
        self.trade_calls += 1

    def _news(self):
        self.news_calls += 1

    def place_limit(self, price=148.20, sl=147.80, tp=149.00, hours=4):
        it = TradeIntent.from_llm_dict(
            {"action": "open", "pair": "USDJPY", "direction": "long",
             "entry_type": "limit", "horizon": "day", "limit_price": price,
             "expires_in": f"{hours}h", "stop_loss": sl, "take_profit": tp,
             "reasoning": "t"}, origin=Origin.SCHEDULER)
        mid = missions.start(self.conn, "trade", "local", "m", WED)
        return self.executor.handle_intent(it, mid)["order_id"]


def test_hourly_trade_mission(tmp_path):
    env = Env(tmp_path)
    env.sched.tick(WED)
    assert env.trade_calls == 1
    env.sched.tick(WED + timedelta(minutes=30))
    assert env.trade_calls == 1  # まだ 1 時間経っていない
    env.sched.tick(WED + timedelta(hours=1))
    assert env.trade_calls == 2


def test_market_closed_only_news(tmp_path):
    env = Env(tmp_path)
    env.sched.tick(SAT)
    assert env.trade_calls == 0
    assert env.news_calls == 1
    env.sched.tick(SAT + timedelta(minutes=10))
    assert env.news_calls == 1  # 30 分間隔
    env.sched.tick(SAT + timedelta(minutes=31))
    assert env.news_calls == 2


def test_limit_expiry_cancelled(tmp_path):
    env = Env(tmp_path)
    oid = env.place_limit(hours=4)
    env.sched.tick(WED + timedelta(hours=5))
    assert orders.get(env.conn, oid)["status"] == "expired"


def test_limit_fill_then_sl(tmp_path):
    env = Env(tmp_path)
    oid = env.place_limit()
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.30, 148.35, 148.15,
                             148.25, 100)
    env.sched.tick(WED + timedelta(minutes=1))
    assert orders.get(env.conn, oid)["status"] == "open"
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED + timedelta(minutes=1),
                             148.10, 148.15, 147.70, 147.75, 100)
    env.sched.tick(WED + timedelta(minutes=2))
    row = orders.get(env.conn, oid)
    assert row["status"] == "closed"
    assert row["close_reason"] == "sl"
    assert row["realized_pnl"] < 0


def test_stale_or_duplicate_bar_ignored(tmp_path):
    env = Env(tmp_path)
    oid = env.place_limit()
    # 古いバー (10 分前) は fills に使わない
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED - timedelta(minutes=10),
                             148.30, 148.35, 148.15, 148.25, 100)
    env.sched.tick(WED + timedelta(minutes=1))
    assert orders.get(env.conn, oid)["status"] == "pending_fill"
    # 同一バーの再処理はしない
    fresh = Bar("USDJPY", "1m", WED + timedelta(minutes=2), 148.40, 148.45,
                148.30, 148.40, 100)  # 指値未到達
    env.bars["USDJPY"] = fresh
    env.sched.tick(WED + timedelta(minutes=2))
    env.sched.tick(WED + timedelta(minutes=3))  # 同じ bar.ts → skip (副作用なし)
    assert orders.get(env.conn, oid)["status"] == "pending_fill"


def test_day_forced_close_uses_quote_side(tmp_path):
    env = Env(tmp_path)
    oid = env.place_limit()
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.30, 148.35, 148.15,
                             148.25, 100)
    env.sched.tick(WED + timedelta(minutes=1))
    near_close = WED.replace(hour=20, minute=57)
    env.sched.tick(near_close)
    row = orders.get(env.conn, oid)
    assert row["status"] == "closed"
    assert row["close_reason"] == "day_rollover"
    assert row["close_price"] == QUOTE.bid  # long は bid (保守側)


def test_day_close_deferred_on_quote_failure(tmp_path):
    def bad_quote(pair):
        raise RuntimeError("quote down")

    env = Env(tmp_path)
    oid = env.place_limit()
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.30, 148.35, 148.15,
                             148.25, 100)
    env.sched.tick(WED + timedelta(minutes=1))
    env.executor.quote_fn = bad_quote
    near_close = WED.replace(hour=20, minute=57)
    env.sched.tick(near_close)
    row = orders.get(env.conn, oid)
    assert row["status"] == "open"  # 架空価格で閉じない (次 tick 再試行)
    assert "day_close_deferred" in (env.tmp_path / "a.log").read_text(
        encoding="utf-8")


def test_reservation_maintenance_cancels_limit(tmp_path):
    env = Env(tmp_path)
    oid = env.place_limit()
    # 口座が悪化 (balance 300,000): mark-to-market が equity 300,000 を記録し、
    # 総リスク上限 1.5% = 4,500 < 予約リスク ≈4,920 → 約定前に自動取消
    env.executor.broker.equity = lambda: (300_000.0, 300_000.0)
    env.sched.tick(WED + timedelta(minutes=1))
    row = orders.get(env.conn, oid)
    assert row["status"] == "cancelled"
    assert row["close_reason"] == "reservation"


def test_reconcile_resolves_unknown(tmp_path):
    env = Env(tmp_path)
    oid = orders.insert(env.conn, pair="USDJPY", direction="long",
                        entry_type="limit", horizon="day",
                        status="cancel_unknown", now=WED, quantity=0.1,
                        requested_price=148.2, stop_loss=147.8)
    env.sched.tick(WED)
    assert orders.get(env.conn, oid)["status"] == "cancelled"


def test_mark_to_market_snapshot(tmp_path):
    from agentic_fx.store import snapshots
    env = Env(tmp_path)
    oid = env.place_limit()
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.30, 148.35, 148.15,
                             148.25, 100)
    env.sched.tick(WED + timedelta(minutes=1))  # fill @148.20
    # 含み損: バー close 147.9 → (147.9-148.2)*100000*qty
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED + timedelta(minutes=1),
                             147.95, 147.98, 147.90, 147.92, 100)
    env.sched.tick(WED + timedelta(minutes=2))
    snap = snapshots.latest(env.conn)
    assert snap["equity"] < 1_000_000  # unrealized 込み


def test_close_transition_cancels_trading_limits(tmp_path):
    env = Env(tmp_path)
    oid = env.place_limit()
    # NOTE (deviation from brief, 逐語ではない): place_limit() の expires_at は
    # Executor の FixedClock(WED) 基準で計算されるため、デフォルト hours=4 だと
    # WED 16:00 に固定される。gate の limit_expiry_max_h (config で <=24h) 制約上
    # hours を伸ばして Friday close 直前まで生かすことはできない
    # (24h でも Thu 12:00 までにしか届かない)。このテストが検証したいのは
    # 「クローズ移行時に未約定指値を取消す」経路であり、「期限切れ取消」経路
    # ではないため、期限切れが先に発火しないよう expires_at を直接
    # Friday close 後まで延長する。詳細はレポートの懸念事項を参照。
    orders.update_fields(env.conn, oid, now=WED,
                         expires_at=datetime(2026, 7, 24, 22, 0,
                                             tzinfo=timezone.utc).isoformat())
    env.state.update(mode=Mode.TRADING)
    fri_2059 = datetime(2026, 7, 24, 20, 59, tzinfo=timezone.utc)
    fri_2101 = datetime(2026, 7, 24, 21, 1, tzinfo=timezone.utc)
    env.sched.tick(fri_2059)             # open 中
    env.sched.tick(fri_2101)             # クローズ移行 tick
    assert orders.get(env.conn, oid)["status"] == "cancelled"
