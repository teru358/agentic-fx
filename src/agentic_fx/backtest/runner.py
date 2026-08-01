"""BacktestRunner — in-memory 再生・synthetic Mission・先読み禁止 (プラン 6 Task 7)。

実運用と同一コード (Scheduler / Executor / PaperBroker / missions /
TradeIntent.from_llm_dict / market_hours.is_market_open) を、in-memory の
sqlite 接続と ``ReplayClock``/``BarFeed`` (Task 6) で駆動する。

mode=learning / autopilot=off での再生 (実運用 Phase 2 と同一条件での再生が
忠実 — レビュー裁定 codex I5)。trading モードでの再生・約定監視は Phase 3
のスコープ。

先読み禁止 (§6) の配線契約 (レビュー裁定 2026-08-01, Task 6 fix round 1 の
設計変更を反映 — brief 本文の ``bar_at(current_ts)`` 配線は使わない):

- ``bars_fn``/``quote_fn`` は ``BarFeed.latest_completed_1m(current_ts)`` —
  ``bar_at(now)`` は ``[now, now+1m)`` のまだ形成中のバーを返すため、判断・
  約定材料に使うと先読みになる。
- バケット集約 (評価 timeframe の確定バー) だけは ``BarFeed.bar_at`` を
  直接読む — バケット終端の tick では、そのバケットに属する全ての 1 分足が
  既に完成しているため先読みではない。
- equity は実現損益ベース (``PaperBroker.equity()`` の契約どおり)。含み
  損益込みの評価は Task 8 のスコープ。
"""
from __future__ import annotations

import re
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from agentic_fx.backtest.replay import BarFeed, ReplayClock, quote_from_bar
from agentic_fx.config import Settings
from agentic_fx.core import market_hours
from agentic_fx.core.accounting import record_snapshot
from agentic_fx.core.contracts import Bar, ConversionRate, Origin, TradeIntent
from agentic_fx.core.executor import Executor
from agentic_fx.core.notifier import Notifier
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.core.scheduler import Scheduler
from agentic_fx.datafeed.price_provider import _SPECS
from agentic_fx.store import missions
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.state import StateStore

# strategy plugin アダプタはこの型に合わせる (プラン 7)。引数は確定した
# 評価 timeframe バー、返り値は LLM 出力と同形の dict / None = 提案なし。
IntentSource = Callable[[Bar], dict | None]

_TF_RE = re.compile(r"^(\d+)(m|h)$")
# バケット境界の錨。実運用の datafeed.bars.BAR_ANCHOR="epoch" と揃える
# (start 相対だと start が tf 格子に乗らない再生で境界がずれる)。
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class _NullActivity:
    """no-op ActivityLog。ActivityLog.write は例外を出さない契約 (実物と
    同じ) — write のみを提供する (scheduler/executor は write しか呼ばない)。"""

    def write(self, *args, **kwargs) -> None:
        pass


@dataclass
class BacktestResult:
    orders: list[dict]
    equity_curve: list[tuple[str, float]]
    start: datetime
    end: datetime
    source: str
    fallback_spread_used: bool


def _parse_timeframe(tf: str) -> timedelta:
    m = _TF_RE.match(tf)
    if not m:
        raise ValueError(f"unsupported eval_timeframe: {tf!r}")
    n, unit = int(m.group(1)), m.group(2)
    return timedelta(hours=n) if unit == "h" else timedelta(minutes=n)


def _aggregate_bucket(feed: BarFeed, symbol: str, interval: str,
                      bucket_start: datetime, tf: timedelta) -> Bar | None:
    """[bucket_start, bucket_start+tf) の 1m バーを集約する。

    レビュー裁定 codex I3: bucket は UTC 半開区間、部分欠損はある分だけで
    集約 (実運用の get_ohlcv も欠損込みで返すため忠実)、全欠損はスキップ。
    このループは全て「その時点で完成済み」の 1 分足を読むだけ (バケット
    終端の tick で呼ばれる = バケット内の全 1 分足は既に過去) なので
    ``feed.bar_at`` の直接使用は先読みにならない。
    """
    bars: list[Bar] = []
    t = bucket_start
    while t < bucket_start + tf:
        bar = feed.bar_at(t)
        if bar is not None:
            bars.append(bar)
        t += timedelta(minutes=1)
    if not bars:
        return None
    return Bar(
        symbol=symbol, interval=interval, ts=bucket_start,
        open=bars[0].open, high=max(b.high for b in bars),
        low=min(b.low for b in bars), close=bars[-1].close,
        volume=sum(b.volume for b in bars))


def run_replay(settings: Settings, *, symbol: str, source: str,
               start: datetime, end: datetime,
               intent_source: IntentSource,
               eval_timeframe: str = "1h",
               history_conn: sqlite3.Connection) -> BacktestResult:
    """``[start, end)`` を 1 分刻みで再生し、``BacktestResult`` を返す。

    実運用のコア (Scheduler/Executor/PaperBroker/missions/TradeIntent) を
    そのまま使い、in-memory の sqlite 接続と一時ファイルの ``StateStore``
    で駆動する — 実 DB (``data/``) には一切触れない。``history_conn`` は
    OHLCV 履歴の読み取り専用接続で、実行時状態を持つ in-memory 接続とは
    別物 (``BarFeed`` にのみ渡す)。
    """
    tf = _parse_timeframe(eval_timeframe)

    conn = connect(Path(":memory:"))
    init_db(conn)
    state = StateStore(Path(tempfile.mkdtemp()) / "state.json")

    # レビュー裁定 sonnet C1: PaperBroker.equity() は settings.paper.
    # starting_balance 基準のため、backtest.initial_balance と二重基準に
    # ならないよう bt_settings を作り、broker/executor/scheduler には
    # これを渡す。record_snapshot の初期投入と一致させる (kill switch が
    # 初回 tick で誤ラッチしないため)。
    initial_balance = settings.backtest.initial_balance
    bt_settings = settings.model_copy(update={
        "paper": settings.paper.model_copy(
            update={"starting_balance": initial_balance})})
    record_snapshot(conn, now=start, balance=initial_balance,
                    equity=initial_balance)

    feed = BarFeed(history_conn, symbol, source=source, start=start, end=end)
    clock = ReplayClock(start)

    fallback_spread_used = False
    current_ts = start

    def _spread(bar_ts: datetime) -> float:
        nonlocal fallback_spread_used
        sp = feed.spread_at(bar_ts)
        if sp is not None:
            return sp
        fallback_spread_used = True
        rule = bt_settings.risk.pair_rules[symbol]
        return rule.assumed_spread_pips * _SPECS[symbol].pip_size

    def bars_fn(pair: str) -> Bar | None:
        if pair != symbol:
            return None
        return feed.latest_completed_1m(current_ts)

    def quote_fn(pair: str):
        # bar が None なら例外を投げてよい (executor 側の既存例外処理に
        # 任せる — 無 quote で発注が通る方が危険。レビュー裁定 上書き A)。
        bar = feed.latest_completed_1m(current_ts)
        if bar is None:
            raise ValueError(
                f"no completed 1m bar for {pair} at "
                f"{current_ts.isoformat()} (no-lookahead quote source)")
        return quote_from_bar(bar, _spread(bar.ts))

    def spec_fn(pair: str):
        return _SPECS[pair]  # 表に無い pair は KeyError で fail closed

    def rate_fn(ccy: str, account_ccy: str, now: datetime) -> ConversionRate:
        if ccy == account_ccy:
            return ConversionRate(1.0, ccy, account_ccy, (now,))
        spec = _SPECS[symbol]
        if ccy == spec.base_currency and account_ccy == spec.quote_currency:
            bar = feed.latest_completed_1m(current_ts)
            if bar is None:
                raise ValueError(
                    f"no completed 1m bar for rate conversion at "
                    f"{current_ts.isoformat()}")
            return ConversionRate(bar.close, ccy, account_ccy, (bar.ts,))
        raise ValueError(
            f"マルチ通貨換算は未対応 (single-symbol replay): "
            f"{ccy}->{account_ccy}")

    broker = PaperBroker(conn, bt_settings, clock)
    activity = _NullActivity()
    executor = Executor(
        conn=conn, broker=broker, settings=bt_settings, state_store=state,
        activity=activity, notifier=Notifier(False, None), clock=clock,
        quote_fn=quote_fn, spec_fn=spec_fn, rate_fn=rate_fn)
    scheduler = Scheduler(
        conn=conn, executor=executor, settings=bt_settings, state_store=state,
        activity=activity, bars_fn=bars_fn,
        on_trade_mission=lambda reason: None,   # 取引判断は cron でなく IntentSource 駆動
        on_news_cycle=lambda: None, on_econ_cycle=lambda: None)

    equity_curve: list[tuple[str, float]] = [
        (start.isoformat(), initial_balance)]
    pending_proposal: dict | None = None
    now = start
    while now < end:
        current_ts = now
        scheduler.tick(now)            # 市場クローズ判定は tick 内部 — 無条件に毎分呼ぶ
        if pending_proposal is not None:
            mid = missions.start(conn, "trade", "backtest", "intent-source", now)
            missions.finish(conn, mid, "completed", pending_proposal, [], now)
            intent = TradeIntent.from_llm_dict(pending_proposal,
                                               origin=Origin.SCHEDULER)
            executor.handle_intent(intent, mid)
            pending_proposal = None
        # バケット境界の錨は UTC epoch (実運用の datafeed.bars.resample /
        # BAR_ANCHOR="epoch" と同じ規律) — `start` 相対にすると、start が
        # tf の格子に乗っていない再生 (例: start=12:30, tf="1h") で
        # [12:30,13:30) のような実運用に存在しない境界のバケットを作って
        # しまう。epoch 錨なら常に実運用と同じ [12:00,13:00) 等の境界になる。
        if (now - _EPOCH) % tf == timedelta(0):
            bucket_start = now - tf
            closed_bar = _aggregate_bucket(feed, symbol, eval_timeframe,
                                           bucket_start, tf)
            if closed_bar is not None and market_hours.is_market_open(now):
                pending_proposal = intent_source(closed_bar)
        equity_curve.append((now.isoformat(), broker.equity()[1]))
        now = clock.advance()

    order_rows = [dict(r) for r in
                 conn.execute("SELECT * FROM orders ORDER BY id").fetchall()]
    return BacktestResult(
        orders=order_rows, equity_curve=equity_curve, start=start, end=end,
        source=source, fallback_spread_used=fallback_spread_used)
