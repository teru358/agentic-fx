"""BacktestRunner — in-memory 再生・synthetic Mission・先読み禁止 (Task 7)。

上書き節 (task-7-brief.md 末尾, コントローラ照合 2026-08-01) がこのテストの
契約:
- bars_fn/quote_fn は latest_completed_1m を使う (bar_at は先読みになる)。
- バケット集約だけは feed.bar_at で直接読む (集約時点で全て完成済み)。
- フィクスチャタイムラインは 13:00/13:01/13:03/13:04 (完成足配線に伴う 1
  tick 後ろへのずれ)。
"""
from datetime import datetime, timedelta, timezone

from agentic_fx.core.accounting import record_snapshot
from agentic_fx.core.contracts import (
    ConversionRate, FixedClock, InstrumentSpec, Origin, Quote, TradeIntent,
)
from agentic_fx.core.executor import Executor
from agentic_fx.core.notifier import Notifier
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.core.scheduler import Scheduler
from agentic_fx.store import missions, orders, ohlcv
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.state import StateStore
from agentic_fx.activity import ActivityLog

from agentic_fx.backtest.runner import run_replay

from tests.backtest.conftest import H, WED, SETTINGS, _conn, _row_at

OPEN = {"action": "open", "pair": "USDJPY", "direction": "long",
        "entry_type": "limit", "horizon": "day",
        "limit_price": 148.20, "expires_in": "6h",
        "stop_loss": 147.80, "take_profit": 149.00,
        "reasoning": "bt"}


def _seed_history(conn):
    """水曜 12:00 から: 1h バー確定 → (次 tick 後に) 指値到達 → TP 到達の 1m 列。

    タイムライン (完成足配線 — 上書き節 A/B。行データは brief のまま):
      13:00 tick = bucket [12:00,13:00) 確定 → proposal 生成 (集約は feed 直読み)
      13:01 tick = tick() 後に pending 執行 → 指値 148.20 発注
                   (quote は完成バー 13:00、close 148.35)
      13:02 tick = 指値まだ到達しない (完成バーは 13:01)
      13:03 tick = fills が完成バー 13:02 (low 148.10) で指値到達を判定
      13:04 tick = 完成バー 13:03 (high 149.10) で TP 到達
    """
    rows = []
    t = WED  # 2026-07-22 (水) 12:00 UTC — 市場オープン
    for i in range(60):          # 12:00-12:59 (評価対象の 1h を構成)
        rows.append(_row_at(t + timedelta(minutes=i), o=148.5, h=148.6,
                            l=148.4, c=148.5))
    for m in (60, 61):           # 13:00, 13:01 — 指値に届かないバー
        rows.append(_row_at(t + timedelta(minutes=m), o=148.4, h=148.45,
                            l=148.30, c=148.35))
    rows.append(_row_at(t + timedelta(minutes=62), o=148.3, h=148.35,
                        l=148.10, c=148.15))       # 13:02 — 指値 148.20 到達
    rows.append(_row_at(t + timedelta(minutes=63), o=148.9,
                        h=149.10, l=148.85, c=149.05))  # 13:03 — TP 到達
    ohlcv.import_bars(conn, rows, source="dukascopy")


def test_full_cycle_open_fill_tp(tmp_path):
    hist = _conn(tmp_path)
    _seed_history(hist)
    fired = []

    def source(bar):
        if not fired:
            fired.append(bar.ts)
            return dict(OPEN)
        return None

    res = run_replay(SETTINGS, symbol="USDJPY", source="dukascopy",
                     start=WED, end=WED + timedelta(hours=2),
                     intent_source=source, eval_timeframe="1h",
                     history_conn=hist)
    closed = [o for o in res.orders if o["status"] == "closed"]
    assert len(closed) == 1 and closed[0]["realized_pnl"] > 0
    assert closed[0]["close_reason"] == "tp"
    assert fired == [WED]  # 評価は 12:00-12:59 の 1h バー確定時のみ
    # 上書き節 B のタイムライン (完成足配線) を厳密にピンする — advisor
    # 指摘: bars_fn/quote_fn に latest_completed_1m の代わりに bar_at を
    # 誤配線しても status/realized_pnl/close_reason は変わらず、この
    # アサーションが無いと先読みが混入したまま緑になる。
    # fills が 13:03 tick (完成バー 13:02, low 148.10) で指値到達判定、
    # TP は 13:04 tick (完成バー 13:03, high 149.10) で確定する。
    assert closed[0]["filled_at"].startswith("2026-07-22T13:03")
    assert closed[0]["closed_at"].startswith("2026-07-22T13:04")


def test_no_lookahead_same_bar(tmp_path):
    """評価に使ったバーの 1m では約定しない — 先読み禁止 (§6)。"""
    hist = _conn(tmp_path)
    # 12:00-12:59 の 1h バー自体に指値到達価格を含める (13:00 以降は到達しない)
    rows = [_row_at(WED + timedelta(minutes=i), o=148.5, h=148.6,
                    l=148.10, c=148.5) for i in range(60)]
    rows.append(_row_at(WED + timedelta(hours=1), o=148.5, h=148.6,
                        l=148.4, c=148.5))
    ohlcv.import_bars(conn=hist, rows=rows, source="dukascopy")
    res = run_replay(SETTINGS, symbol="USDJPY", source="dukascopy",
                     start=WED, end=WED + timedelta(hours=2),
                     intent_source=lambda b: dict(OPEN),
                     eval_timeframe="1h", history_conn=hist)
    assert not [o for o in res.orders if o["status"] in ("open", "closed")]


def test_synthetic_mission_passes_origin_gate(tmp_path):
    """§5 検証を同一コードで通す — missions 行が in-memory に作られ intent が accepted。"""
    hist = _conn(tmp_path)
    _seed_history(hist)
    res = run_replay(SETTINGS, symbol="USDJPY", source="dukascopy",
                     start=WED, end=WED + timedelta(hours=2),
                     intent_source=lambda b: dict(OPEN),
                     eval_timeframe="1h", history_conn=hist)
    assert res.orders  # origin_rejected なら orders は生まれない


def test_real_db_untouched(tmp_path, monkeypatch):
    """バックテストが実 data/ に触れない。"""
    monkeypatch.chdir(tmp_path)
    hist = _conn(tmp_path)
    _seed_history(hist)
    run_replay(SETTINGS, symbol="USDJPY", source="dukascopy",
               start=WED, end=WED + timedelta(hours=1),
               intent_source=lambda b: None, eval_timeframe="1h",
               history_conn=hist)
    assert not (tmp_path / "data").exists()


def test_initial_balance_wiring_no_spurious_killswitch(tmp_path):
    """backtest.initial_balance ≠ paper.starting_balance でも初回 tick で
    kill switch がラッチしない (残高の二重基準を塞ぐ)。"""
    hist = _conn(tmp_path)
    _seed_history(hist)
    s = SETTINGS.model_copy(update={"backtest": SETTINGS.backtest.model_copy(
        update={"initial_balance": 5_000_000.0})})   # paper 側は 1,000,000 のまま
    res = run_replay(s, symbol="USDJPY", source="dukascopy",
                     start=WED, end=WED + timedelta(hours=2),
                     intent_source=lambda b: dict(OPEN),
                     eval_timeframe="1h", history_conn=hist)
    closed = [o for o in res.orders if o["status"] == "closed"]
    assert closed  # ラッチしていれば gate_rejected で 0 件になる
    assert res.equity_curve[0][1] == 5_000_000.0


def test_fallback_spread_used_when_db_spread_missing(tmp_path):
    """DB の spread 列が欠損 (None) の場合、assumed_spread_pips ベースの
    フォールバックへ切り替わり fallback_spread_used=True になる。

    advisor 指摘: `_seed_history`/`_row_at` は既定で spread=0.01 を書き込む
    ため、通常のテストではフォールバック経路 (quote_from_bar を呼ぶ**前**
    に spread を解決する分岐) が一度も実行されず変異に対して無防備だった。
    ここでは全行 spread=None で書き込み、フォールバック分岐 (quote_fn の
    None spread ハンドリング) を強制的に通す。フォールバック解決が
    quote_from_bar 呼び出しの**後**に退化する変異が起きると、Task 6 の
    契約 (spread=None は ValueError) により quote_fn 自体が例外を投げ、
    以下 orders が 1 件も作られなくなる — それも検出できるよう orders が
    実際に作られていることも併せて assert する。
    """
    hist = _conn(tmp_path)
    rows = []
    t = WED
    for i in range(60):
        rows.append(_row_at(t + timedelta(minutes=i), o=148.5, h=148.6,
                            l=148.4, c=148.5, spread=None))
    for m in (60, 61):
        rows.append(_row_at(t + timedelta(minutes=m), o=148.4, h=148.45,
                            l=148.30, c=148.35, spread=None))
    rows.append(_row_at(t + timedelta(minutes=62), o=148.3, h=148.35,
                        l=148.10, c=148.15, spread=None))
    rows.append(_row_at(t + timedelta(minutes=63), o=148.9, h=149.10,
                        l=148.85, c=149.05, spread=None))
    ohlcv.import_bars(hist, rows, source="dukascopy")

    res = run_replay(SETTINGS, symbol="USDJPY", source="dukascopy",
                     start=WED, end=WED + timedelta(hours=2),
                     intent_source=lambda b: dict(OPEN),
                     eval_timeframe="1h", history_conn=hist)
    assert res.fallback_spread_used is True
    assert res.orders  # フォールバックが正しく機能していれば発注は成立する


def test_bucket_alignment_uses_utc_epoch_anchor(tmp_path):
    """バケット境界の錨は UTC epoch (実運用 datafeed.bars.BAR_ANCHOR="epoch"
    と同じ規律) — start がその格子に乗っていない再生でも、実運用と同じ
    [13:00,14:00) 等の境界で評価する。start 相対 (elapsed % tf) に退化
    すると [13:30,14:30) のような実運用に存在しない境界で評価してしまう。
    """
    off_grid_start = WED + timedelta(minutes=30)  # 12:30 — 1h グリッドから外れる
    hist = _conn(tmp_path)
    rows = [_row_at(off_grid_start + timedelta(minutes=i), o=148.5, h=148.6,
                    l=148.4, c=148.5)
           for i in range(180)]  # 12:30-15:29 の連続 1m
    ohlcv.import_bars(hist, rows, source="dukascopy")

    fired = []
    run_replay(SETTINGS, symbol="USDJPY", source="dukascopy",
              start=off_grid_start, end=off_grid_start + timedelta(hours=3),
              intent_source=lambda b: fired.append(b.ts),
              eval_timeframe="1h", history_conn=hist)
    assert fired  # 少なくとも 1 回は評価が起きる
    assert all(ts.minute == 0 and ts.second == 0 for ts in fired), fired


# ---------------------------------------------------------------------------
# Step 2 (brief 裁定 codex C3): 実装前の前提検証 — 現行 Scheduler.tick は
# mark-to-market 失敗時に early return し、_expire_limits / _force_close_day
# に到達しない可能性がある。bars_fn が常に None を返す状態でも期限切れの
# pending_fill が expired へ遷移するかを実測する。
#
# 実測結果: Scheduler._mark_to_market は bars_fn=None による stale=True
# ケースでは (record_snapshot 自体は成功するので) True を返し、tick() の
# early return (`if not self._mark_to_market(now): return`) は発火しない。
# early return が起きるのは record_snapshot が ValueError (時系列逆行) を
# 送出したときのみで、単調増加する ReplayClock ではこの経路に入らない。
# よって tick 内部の改修は不要 — このテストは前提が成立することのピン留め。
# ---------------------------------------------------------------------------

def test_expire_limits_reached_despite_missing_bar(tmp_path):
    conn = connect(tmp_path / "sched.db")
    init_db(conn)
    record_snapshot(conn, now=WED, balance=1_000_000, equity=1_000_000)
    state = StateStore(tmp_path / "state.json")
    clock = FixedClock(WED)
    quote = Quote("USDJPY", 148.49, 148.51, WED, "test")
    spec = InstrumentSpec("USDJPY", 0.01, 0.01, 50.0, 0.01, 100_000,
                          "USD", "JPY")

    def rate_fn(ccy, account_ccy, now):
        if ccy == account_ccy:
            return ConversionRate(1.0, ccy, account_ccy, (now,))
        return ConversionRate(quote.ask, ccy, account_ccy, (now,))

    activity = ActivityLog(tmp_path / "a.log")
    executor = Executor(
        conn=conn, broker=PaperBroker(conn, SETTINGS, clock),
        settings=SETTINGS, state_store=state, activity=activity,
        notifier=Notifier(False, None), clock=clock,
        quote_fn=lambda p: quote, spec_fn=lambda p: spec, rate_fn=rate_fn)
    it = TradeIntent.from_llm_dict(dict(OPEN), origin=Origin.SCHEDULER)
    mid = missions.start(conn, "trade", "local", "m", WED)
    oid = executor.handle_intent(it, mid)["order_id"]
    assert orders.get(conn, oid)["status"] == "pending_fill"
    orders.update_fields(conn, oid, now=WED,
                         expires_at=(WED - timedelta(minutes=1)).isoformat())

    sched = Scheduler(
        conn=conn, executor=executor, settings=SETTINGS, state_store=state,
        activity=activity, bars_fn=lambda p: None,
        on_trade_mission=lambda reason: None, on_news_cycle=lambda: None,
        on_econ_cycle=lambda: None)
    sched.tick(WED)
    assert orders.get(conn, oid)["status"] == "expired"
