"""プラン 2 の注入点に、プラン 3 の実装が**実際に**嵌まることを確かめる。

目視のシグネチャ確認では「型が合っているつもり」を検出できない。ここでは
モックを使わずに実物を組み立て、注入点を経由して呼ぶ:

- `Executor(quote_fn=PriceProvider.get_quote, spec_fn=PriceProvider.spec)`
- `Scheduler(bars_fn=PriceProvider.latest_1m_bar,
             on_news_cycle=NewsCollector.collect)`

外部アクセスはしない (sources 層だけを patch し、その先の検証・保存・
resample・RAG は本物を通す)。
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import (
    Bar, FixedClock, InstrumentSpec, Quote,
)
from agentic_fx.core.executor import Executor
from agentic_fx.core.notifier import Notifier
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.core.scheduler import Scheduler
from agentic_fx.datafeed import sources
from agentic_fx.datafeed.health import DataUnhealthy
from agentic_fx.datafeed.news_collector import NewsCollector
from agentic_fx.datafeed.price_provider import PriceProvider
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.rag import Rag
from agentic_fx.store.state import StateStore

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "settings.yaml.example"
# 市場オープン中の水曜 12:00 UTC (market_hours の判定に合わせる)
OPEN_NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
# 土曜 = 市場クローズ (scheduler はクローズ時に on_news_cycle を回す)
CLOSED_NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)


def _bars(pair="USDJPY", n=40, interval="1m", now=OPEN_NOW):
    step = timedelta(minutes=sources.INTERVAL_MIN[interval])
    start = now - step * n
    return [Bar(pair, interval, start + step * i,
                148.0, 148.1, 147.9, 148.05, 10) for i in range(n)]


def _env(tmp_path, now, on_econ_cycle=None):
    from tests.store.test_rag import FakeEmbedding
    settings = load_settings(EXAMPLE)
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    clock = FixedClock(now)
    activity = ActivityLog(tmp_path / "activity.log")
    provider = PriceProvider(conn, settings, clock)
    state = StateStore(tmp_path / "state.json")
    executor = Executor(
        conn=conn, broker=PaperBroker(conn, settings, clock),
        settings=settings, state_store=state, activity=activity,
        notifier=Notifier(enabled=False, webhook_url=None), clock=clock,
        # ★ プラン 2 の注入点にプラン 3 の実装をそのまま渡す
        quote_fn=provider.get_quote, spec_fn=provider.spec,
        rate_fn=lambda ccy, account_ccy, now: provider.to_account_rate(
            ccy, account_ccy, reference_ts=now,
            max_skew_min=settings.datafeed.conversion_skew_max_min))
    collector = NewsCollector(
        conn, Rag(tmp_path / "rag", embedding_function=FakeEmbedding()),
        activity, clock)
    trade_calls: list[str] = []
    econ_calls: list[int] = []
    scheduler = Scheduler(
        conn=conn, executor=executor, settings=settings, state_store=state,
        activity=activity,
        bars_fn=provider.latest_1m_bar,          # ★ 注入点
        on_trade_mission=lambda reason: trade_calls.append(reason),
        on_news_cycle=collector.collect,         # ★ 注入点
        # econ は既定ではカウンタ (EconCalendar.refresh は fetch_ff_calendar を
        # 直接呼ぶため、既定で渡すとこのファイルの全 tick が外部アクセスする)
        on_econ_cycle=on_econ_cycle or (lambda: econ_calls.append(1)))
    return provider, executor, scheduler, trade_calls


def test_executor_injection_points_accept_price_provider(tmp_path):
    """quote_fn / spec_fn が本物の PriceProvider の bound method で動く。"""
    provider, executor, _, _ = _env(tmp_path, OPEN_NOW)
    q = Quote("USDJPY", 148.49, 148.51, OPEN_NOW, "yfinance")
    with patch("agentic_fx.datafeed.price_provider.sources.yf_quote",
               return_value=q):
        got = executor.quote_fn("USDJPY")
    assert isinstance(got, Quote) and got.symbol == "USDJPY"
    assert isinstance(executor.spec_fn("USDJPY"), InstrumentSpec)


def test_scheduler_bars_fn_accepts_latest_1m_bar(tmp_path):
    """オープン中の tick が bars_fn を実際に呼び、Bar | None を受け取れる。"""
    _, _, scheduler, trade_calls = _env(tmp_path, OPEN_NOW)
    with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               return_value=_bars()) as m:
        scheduler.tick(OPEN_NOW)
    assert m.called                     # latest_1m_bar → get_bars → sources
    assert trade_calls == ["cron"]  # trigger が on_trade_mission まで伝搬する


def test_scheduler_bars_fn_tolerates_unhealthy_feed(tmp_path):
    """全滅時は latest_1m_bar が None を返し、tick は例外にならない。"""
    _, _, scheduler, trade_calls = _env(tmp_path, OPEN_NOW)
    with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               side_effect=OSError("offline")):
        scheduler.tick(OPEN_NOW)        # DataUnhealthy を漏らさない
    assert trade_calls == ["cron"]  # trigger が on_trade_mission まで伝搬する


def test_scheduler_news_cycle_accepts_collector_collect(tmp_path):
    """クローズ中の tick が on_news_cycle=collector.collect を**本物のまま**呼ぶ。

    collect をモックすると引数の食い違いを検出できないので実物を通す
    (news_sources は空なのでフェッチは起きない = 外部アクセスなし)。
    NewsCollector.collect は int を返すが Scheduler の注釈は
    Callable[[], None] — 戻り値は使われないので実害はない (報告書の
    typing メモ)。
    """
    _, _, scheduler, _ = _env(tmp_path, CLOSED_NOW)
    scheduler.tick(CLOSED_NOW)
    act = (tmp_path / "activity.log").read_text(encoding="utf-8")
    assert "collected" in act           # collect() が最後まで走った証跡


def test_scheduler_news_cycle_runs_on_open_market_tick(tmp_path):
    """cross-plan 修正①: **開場中**の tick でも本物の collect が走る。

    旧実装では `on_news_cycle` の呼び出しが `if not open_now:` ブロック内の
    1 箇所しかなく (その直後に return)、開場中の tick からは決して到達
    しなかった。市場は週末しか閉じないため、平日はニュース収集も RAG の
    48h 掃除も 1 度も走らなかった (task-9-report.md §6-1 の申し送り)。
    """
    _, _, scheduler, _ = _env(tmp_path, OPEN_NOW)
    with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               return_value=_bars()):
        scheduler.tick(OPEN_NOW)
    act = (tmp_path / "activity.log").read_text(encoding="utf-8")
    assert "collected" in act


def test_scheduler_econ_cycle_accepts_econ_calendar_refresh(tmp_path):
    """cross-plan 修正②: `on_econ_cycle` 注入点に本物の
    `EconCalendar.refresh` が嵌まり、tick から実際に呼ばれて
    econ_events に保存されること (呼び出し元がどこにも無かったのが欠陥②)。

    外部アクセスはしない — sources 層に相当する `fetch_ff_calendar` だけを
    patch し、その先の変換・保存は本物を通す。"""
    from agentic_fx.core.contracts import FixedClock
    from agentic_fx.datafeed.econ_calendar import CalendarFetch, EconCalendar
    from agentic_fx.store import econ_events

    conn = connect(tmp_path / "econ.db")
    init_db(conn)
    activity = ActivityLog(tmp_path / "econ_activity.log")
    cal = EconCalendar(conn, activity, FixedClock(CLOSED_NOW))
    _, _, scheduler, _ = _env(tmp_path, CLOSED_NOW, on_econ_cycle=cal.refresh)
    events = [{"ts": CLOSED_NOW + timedelta(hours=2), "country": "USD",
               "name": "Nonfarm Payrolls", "importance": 3,
               "forecast": "150K", "previous": "140K"}]
    with patch("agentic_fx.datafeed.econ_calendar.fetch_ff_calendar",
               return_value=CalendarFetch(events=events, dropped=0)) as m:
        scheduler.tick(CLOSED_NOW)
    assert m.called
    assert len(econ_events.upcoming(conn, CLOSED_NOW, hours=24)) == 1


# --- レビュー指摘 F1: 換算レート skew 検証は本番配線の入力で実際に発火する
# こと (専用キー conversion_skew_max_min。freshness_max_min の使い回しでは
# reference_ts=now・脚の quote も同じ clock という配線の構造上、通常の
# (処理が速い) 判断では②③が原理的に発火し得ない — レビュアー実測: 配線の
# 閾値を 10_000 や 0.0 に変えても 560 passed だった)。

def test_conversion_rate_snapshot_skew_fires_with_real_wiring(tmp_path):
    """③ (判断内スナップショット全体の時刻差): 実配線の rate_fn
    (`provider.to_account_rate` を `conversion_skew_max_min` で束縛したもの)
    を通し、freshness (20min) 以内だが conversion_skew_max_min (5min) を
    超える quote で実際に fail closed すること。専用キーが無ければ
    (freshness_max_min を使い回していれば) この skew (6分) は 20 分以内
    なので通ってしまう — この対比が M1 (配線の閾値を変える変異) のピンになる。
    """
    provider, executor, _, _ = _env(tmp_path, OPEN_NOW)
    settings = load_settings(EXAMPLE)
    skew_min = settings.datafeed.conversion_skew_max_min
    freshness_min = settings.datafeed.freshness_max_min
    assert skew_min < freshness_min, (
        "この検証の前提: 専用キーが freshness より厳しいこと")
    # freshness には収まる (fresh) が conversion_skew_max_min は超える quote。
    stale_but_fresh = Quote("USDJPY", 148.0, 149.0,
                           OPEN_NOW - timedelta(minutes=skew_min + 1),
                           "yfinance")
    with patch("agentic_fx.datafeed.price_provider.sources.yf_quote",
              return_value=stale_but_fresh):
        with pytest.raises(DataUnhealthy, match="skew"):
            executor.rate_fn("USD", "JPY", OPEN_NOW)


def test_conversion_rate_cross_leg_skew_fires_with_real_wiring(tmp_path):
    """② (クロス2脚間の時刻差): EUR→JPY は USD 経由のクロス (EURUSD ×
    USDJPY)。両脚とも freshness (20min) 以内だが、互いの観測時刻が
    conversion_skew_max_min (5min) を超えて乖離すると実配線で fail closed
    すること。"""
    provider, executor, _, _ = _env(tmp_path, OPEN_NOW)
    settings = load_settings(EXAMPLE)
    skew_min = settings.datafeed.conversion_skew_max_min

    def _fn(pair):
        if pair == "EURUSD":
            return Quote(pair, 1.08, 1.08, OPEN_NOW, "yfinance")
        if pair == "USDJPY":
            return Quote(pair, 148.0, 149.0,
                        OPEN_NOW - timedelta(minutes=skew_min + 1),
                        "yfinance")
        raise OSError(f"no data for {pair}")

    with patch("agentic_fx.datafeed.price_provider.sources.yf_quote",
              side_effect=_fn):
        with pytest.raises(DataUnhealthy, match="skew"):
            executor.rate_fn("EUR", "JPY", OPEN_NOW)
