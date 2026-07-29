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


def _env(tmp_path, now):
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
        quote_fn=provider.get_quote, spec_fn=provider.spec)
    collector = NewsCollector(
        conn, Rag(tmp_path / "rag", embedding_function=FakeEmbedding()),
        activity, clock)
    trade_calls: list[int] = []
    scheduler = Scheduler(
        conn=conn, executor=executor, settings=settings, state_store=state,
        activity=activity,
        bars_fn=provider.latest_1m_bar,          # ★ 注入点
        on_trade_mission=lambda: trade_calls.append(1),
        on_news_cycle=collector.collect)         # ★ 注入点
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
    assert trade_calls == [1]


def test_scheduler_bars_fn_tolerates_unhealthy_feed(tmp_path):
    """全滅時は latest_1m_bar が None を返し、tick は例外にならない。"""
    _, _, scheduler, trade_calls = _env(tmp_path, OPEN_NOW)
    with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               side_effect=OSError("offline")):
        scheduler.tick(OPEN_NOW)        # DataUnhealthy を漏らさない
    assert trade_calls == [1]


def test_scheduler_news_cycle_accepts_collector_collect(tmp_path):
    """クローズ中の tick が on_news_cycle=collector.collect を**本物のまま**呼ぶ。

    **CLOSED_NOW を使うのは任意の選択ではない**: `Scheduler.tick` の
    `on_news_cycle` 呼び出しは `if not open_now:` ブロック内の 1 箇所しかなく
    (その直後に return)、市場オープン中の tick からは決して到達しない。
    市場は週末しか閉じないため、この配線のままでは平日にニュースが 1 度も
    収集されない (task-9-report.md §6-1 の申し送り)。

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
