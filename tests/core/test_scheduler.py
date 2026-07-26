from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.config import load_settings
from agentic_fx.core.accounting import record_snapshot
from agentic_fx.core.contracts import (
    Bar, FixedClock, InstrumentSpec, Mode, Origin, Quote, TradeIntent,
)
from agentic_fx.core.executor import Executor, has_unresolved_unknown
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.core.scheduler import Scheduler
from agentic_fx.store import missions, orders
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.state import StateStore

WED = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)  # 水曜
FRI = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)  # 金曜 正午
SAT = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")
SPEC = InstrumentSpec("USDJPY", 0.01, 0.01, 50.0, 0.01, 100_000)
QUOTE = Quote("USDJPY", 148.49, 148.51, WED, "test")


class Env:
    def __init__(self, tmp_path, quote_fn=None, base=WED):
        from agentic_fx.core.notifier import Notifier
        self.conn = connect(tmp_path / "t.db")
        init_db(self.conn)
        record_snapshot(self.conn, now=base, balance=1_000_000, equity=1_000_000)
        self.state = StateStore(tmp_path / "state.json")
        self.tmp_path = tmp_path
        self.bars: dict[str, Bar] = {}
        self.trade_calls = 0
        self.news_calls = 0
        self.base = base
        clock = FixedClock(base)
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
        mid = missions.start(self.conn, "trade", "local", "m", self.base)
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
    # レビュー修正 9: 元は WED 基準の Env に orders.update_fields で expires_at
    # を直接書き換えて回避していたが、それは gate (limit_expiry_max_h<=24h) を
    # 経由しないテスト専用の裏道だった。正しい直し方はフィクスチャの基準時刻
    # 自体を金曜にすること — hours=12 (gate の 24h 制約内) で
    # expires_at = 金 12:00+12h = 土 00:00 となり、金 21:01 のクローズ移行
    # tick 時点でまだ pending_fill のまま検証対象に到達する。
    env = Env(tmp_path, base=FRI)
    oid = env.place_limit(hours=12)
    env.state.update(mode=Mode.TRADING)
    fri_2059 = datetime(2026, 7, 24, 20, 59, tzinfo=timezone.utc)
    fri_2101 = datetime(2026, 7, 24, 21, 1, tzinfo=timezone.utc)
    env.sched.tick(fri_2059)             # open 中
    env.sched.tick(fri_2101)             # クローズ移行 tick
    assert orders.get(env.conn, oid)["status"] == "cancelled"


def test_day_forced_close_on_friday_2057(tmp_path):
    """申し送り事項: 金曜 20:57 で day 強制クローズが正しく発火することの
    直接検証 (既存の test_day_forced_close_uses_quote_side は WED=水曜 基準
    だったため、金曜ケースは未検証だった — レビュー修正 9)。"""
    env = Env(tmp_path, base=FRI)
    oid = env.place_limit()
    env.bars["USDJPY"] = Bar("USDJPY", "1m", FRI, 148.30, 148.35, 148.15,
                             148.25, 100)
    env.sched.tick(FRI + timedelta(minutes=1))  # fill
    fri_near_close = FRI.replace(hour=20, minute=57)
    env.sched.tick(fri_near_close)
    row = orders.get(env.conn, oid)
    assert row["status"] == "closed"
    assert row["close_reason"] == "day_rollover"
    assert row["close_price"] == QUOTE.bid


def test_same_bar_tp_not_awarded_by_second_loop(tmp_path):
    """レビュー修正 1 (Critical): _process_fills の第 2 ループが、同一 tick
    で第 1 ループが fill させた注文を再取得し、entry_same_bar=False で
    再評価してしまうと、本来「同一バー内の順序判定不能」として見送られる
    べき TP が確定してしまう。"""
    env = Env(tmp_path)
    oid = env.place_limit(price=148.20, sl=147.80, tp=149.00)
    # 指値到達 (low<=148.20) と同時に TP (149.00) にもギャップで到達するバー
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.30, 149.20, 148.15,
                             149.00, 100)
    env.sched.tick(WED + timedelta(minutes=1))
    row = orders.get(env.conn, oid)
    assert row["status"] == "open"  # 同一バーでは TP 確定させない
    assert row["close_reason"] is None


def test_closing_retried_and_blocks_gate_until_resolved(tmp_path):
    """レビュー修正 2 (Critical): CLOSE_UNKNOWN -> CLOSING に遷移した後、
    quote 障害で一度失敗しても CLOSING は次 tick 以降も再走査され、quote が
    復旧すれば CLOSED まで解決する。また解決するまでの間、
    has_unresolved_unknown が True であり続け (executor._UNKNOWN に CLOSING
    を含めた効果)、gate の新規発注停止が維持される。"""
    calls = {"n": 0}

    def flaky_quote(pair):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("quote down")
        return QUOTE

    env = Env(tmp_path, quote_fn=flaky_quote)
    oid = orders.insert(env.conn, pair="USDJPY", direction="long",
                        entry_type="limit", horizon="day",
                        status="close_unknown", now=WED, quantity=0.1,
                        requested_price=148.2, stop_loss=147.8,
                        avg_fill_price=148.2)
    env.sched.tick(WED)  # reconcile -> CLOSING, quote 失敗で再試行は次 tick へ
    row = orders.get(env.conn, oid)
    assert row["status"] == "closing"
    assert has_unresolved_unknown(env.conn)  # gate は新規発注を止め続ける

    env.sched.tick(WED + timedelta(minutes=1))  # quote 復旧 → 再試行で解決
    row2 = orders.get(env.conn, oid)
    assert row2["status"] == "closed"
    assert not has_unresolved_unknown(env.conn)


def test_closing_retry_db_failure_is_not_swallowed(tmp_path):
    """レビュー修正 2 の項目 3: broker.close 成功後の DB 確定処理
    (compute_pnl/transitions.transition) が失敗した場合、quote 障害と
    同じように黙って握りつぶしてはいけない (broker 側は既に閉じているかも
    しれず、不整合を検知できなくなるため)。"""
    from agentic_fx.core import scheduler as scheduler_mod

    env = Env(tmp_path)
    orders.insert(env.conn, pair="USDJPY", direction="long",
                 entry_type="limit", horizon="day",
                 status="close_unknown", now=WED, quantity=0.1,
                 requested_price=148.2, stop_loss=147.8, avg_fill_price=148.2)

    orig_transition = scheduler_mod.transitions.transition

    def flaky_transition(conn, order_id, to, now, **fields):
        if to.value == "closed":
            raise RuntimeError("db write failed")
        return orig_transition(conn, order_id, to, now, **fields)

    scheduler_mod.transitions.transition = flaky_transition
    try:
        with pytest.raises(RuntimeError, match="db write failed"):
            env.sched.tick(WED)
    finally:
        scheduler_mod.transitions.transition = orig_transition


def test_restart_with_first_tick_closed_still_cancels_trading_limits(tmp_path):
    """レビュー修正 4 (Medium): サービスが市場オープン中に落ち、クローズ後に
    再起動すると、最初の tick で _was_open は None (True でも False でもない)
    になる。この場合も取引モードの未約定実指値は監視外に残さない。"""
    env = Env(tmp_path)
    oid = env.place_limit()
    env.state.update(mode=Mode.TRADING)
    env.sched.tick(SAT)  # 再起動後いきなりクローズ中に最初の tick が来るケース
    assert orders.get(env.conn, oid)["status"] == "cancelled"


def test_processed_bar_marking_covers_pairs_without_tracked_orders(tmp_path):
    """レビュー修正 5: _processed_bar_ts のマーキングが従来
    (PENDING_FILL/OPEN/CLOSED/CANCELLED のいずれかが存在する pair のみ) だと、
    ある tick でそれら 4 状態のどれも無い pair のバーはマーキングされない。
    settings.pairs を直接走査してマーキングすることで、この抜けを塞ぐ。"""
    env = Env(tmp_path)
    bar = Bar("USDJPY", "1m", WED, 148.30, 148.35, 148.15, 148.25, 100)
    env.bars["USDJPY"] = bar
    # この tick 時点では USDJPY に一切注文が無い (4 状態のどれにも該当しない)
    env.sched.tick(WED + timedelta(minutes=1))
    # フィード停止を模して同一バーのまま、直後に新規指値を出す
    oid = env.place_limit()  # entry=148.20 (bar の high/low 内 → 約定しうる)
    env.sched.tick(WED + timedelta(minutes=2))
    # 直前 tick で「処理済み」としてマーキングされた同一バーでは約定させない
    assert orders.get(env.conn, oid)["status"] == "pending_fill"


def test_reservation_maintenance_zero_equity_no_crash(tmp_path):
    """レビュー修正 6: equity<=0 では notional/equity がゼロ除算になる。"""
    env = Env(tmp_path)
    oid = env.place_limit()
    env.executor.broker.equity = lambda: (0.0, 0.0)
    env.sched.tick(WED + timedelta(minutes=1))  # ZeroDivisionError にならない
    row = orders.get(env.conn, oid)
    assert row["status"] == "pending_fill"  # gate 側の fail closed に委ねる


def test_mark_to_market_skips_snapshot_on_stale_bar_but_tick_continues(tmp_path):
    """レビュー修正 7: open ポジションのバーが陳腐化している場合、
    その tick の snapshot 記録は見送る (古い価格で kill switch を誤判定
    させないため) が、tick 自体は継続する。"""
    from agentic_fx.store import snapshots

    env = Env(tmp_path)
    oid = env.place_limit()
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.30, 148.35, 148.15,
                             148.25, 100)
    env.sched.tick(WED + timedelta(minutes=1))  # fill -> OPEN
    snap_before = snapshots.latest(env.conn)
    # バーを更新しないまま (フィード停止) 鮮度切れになるまで進める
    env.sched.tick(WED + timedelta(minutes=7))
    snap_after = snapshots.latest(env.conn)
    assert snap_after["ts"] == snap_before["ts"]  # 新しい snapshot は記録されない
    assert "snapshot_stale_bar_skip" in (env.tmp_path / "a.log").read_text(
        encoding="utf-8")
    assert orders.get(env.conn, oid)["status"] == "open"  # tick 自体は継続
