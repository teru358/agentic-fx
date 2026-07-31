import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

from agentic_fx.config import load_settings
from agentic_fx.core.contracts import Bar, FixedClock, OrderStatus
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.datafeed.sources import INTERVAL_MIN
from agentic_fx.store import orders, reflections
from agentic_fx.store.db import connect, init_db
from agentic_fx.tools import (
    account_tools, market_tools, news_tools, reflection_tools,
)
from agentic_fx.tools.registry import ToolRegistry

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


def _bars(n=120, interval="1h"):
    step = timedelta(minutes=INTERVAL_MIN[interval])
    return [Bar("USDJPY", interval, NOW - step * (n - i),
                148.0, 148.2, 147.8, 148.1, 100) for i in range(n)]


def test_market_tools():
    provider = MagicMock()
    provider.get_bars.return_value = _bars()
    econ = MagicMock()
    econ.upcoming.return_value = [{"name": "CPI"}]
    reg = ToolRegistry()
    reg.register_all(market_tools.build(provider, econ, SETTINGS))
    ohlcv = reg.func("get_ohlcv")(pair="USDJPY", timeframe="1h")
    assert len(ohlcv) == 100  # 直近 100 本に制限
    assert set(ohlcv[0]) == {"ts", "open", "high", "low", "close"}
    ind = reg.func("get_indicators")(pair="USDJPY", timeframe="1h")
    assert "rsi_14" in ind
    assert reg.func("get_econ_calendar")(days=1) == [{"name": "CPI"}]
    # F3: Verify days*24 multiplication (catches mutation days → days*24)
    econ.upcoming.assert_called_with(hours=24)


def test_tools_do_not_resample_and_delegate_the_timeframe_to_the_provider():
    """4h は provider にそのまま要求する (ツール層で 1h から作り直さない)。

    MT5 のようにネイティブ 4h を持つソースがある環境で 1h から再構成すると、
    実運用とバックテストで違う 4h を見ることになる (プラン 3 で provider 側に
    ネイティブ / 導出の判断を集約した)。
    """
    provider = MagicMock()
    provider.get_bars.return_value = _bars(n=30, interval="4h")
    reg = ToolRegistry()
    reg.register_all(market_tools.build(provider, MagicMock(), SETTINGS))
    reg.func("get_ohlcv")(pair="USDJPY", timeframe="4h")
    provider.get_bars.assert_called_with("USDJPY", "4h")


def test_get_indicators_passes_timeframe_to_provider():
    """F5: get_indicators must pass timeframe unchanged to provider.

    Catches hardcoded timeframe mutations inside get_indicators.
    """
    provider = MagicMock()
    provider.get_bars.return_value = _bars(n=30, interval="4h")
    reg = ToolRegistry()
    reg.register_all(market_tools.build(provider, MagicMock(), SETTINGS))
    reg.func("get_indicators")(pair="USDJPY", timeframe="4h")
    provider.get_bars.assert_called_with("USDJPY", "4h")


def test_timeframe_enum_comes_from_settings():
    """timeframe の enum は設定の datafeed.intervals から作られる。"""
    reg = ToolRegistry()
    reg.register_all(market_tools.build(MagicMock(), MagicMock(), SETTINGS))
    spec = next(s for s in reg.openai_tools(allowed=["get_ohlcv"]))
    enum = spec["function"]["parameters"]["properties"]["timeframe"]["enum"]
    assert enum == list(SETTINGS.datafeed.intervals)


def test_no_tool_exposes_an_arbitrary_history_window():
    """履歴期間を任意に切り出せる引数をどのツールも持たないこと。

    設計書 §6 の過剰適合防御は「エージェントに任意期間の履歴を与えない」
    ことに依存している。規約だけだと後から `since` 等が足されても気付けない
    ため、tool schema をテストで固定する (codex レビュー 6)。

    M-4: market_tools だけでなく account_tools / reflection_tools も含める
    — 主張名 (「どのツールも」) を実際に全ツールでカバーする。
    """
    reg = ToolRegistry()
    reg.register_all(market_tools.build(MagicMock(), MagicMock(), SETTINGS))
    reg.register_all(news_tools.build(MagicMock()))
    reg.register_all(account_tools.build(MagicMock(), MagicMock()))
    reg.register_all(reflection_tools.build(MagicMock(), MagicMock()))
    banned = {"since", "until", "from", "to", "start", "end",
              "start_date", "end_date", "lookback", "lookback_days", "bars",
              "period", "window", "range", "history_days", "count"}
    # 既存 API だけで検査する (ToolRegistry に新メソッドは足さない)
    for spec in reg.openai_tools(allowed=reg.names()):
        fn = spec["function"]
        params = set(fn["parameters"].get("properties", {}))
        assert not (params & banned), \
            f"{fn['name']} が期間指定引数を持っている: {params & banned}"


def test_market_tools_schema_whitelist():
    """F2: Whitelist pin — exact properties per market tool.

    Blacklist alone misses new param names. Pin exact schema.
    """
    reg = ToolRegistry()
    reg.register_all(market_tools.build(MagicMock(), MagicMock(), SETTINGS))
    specs = {s["function"]["name"]: s["function"]["parameters"]["properties"]
             for s in reg.openai_tools(allowed=reg.names())}

    # get_ohlcv: exactly {"pair", "timeframe"}
    assert set(specs["get_ohlcv"]) == {"pair", "timeframe"}
    # get_indicators: exactly {"pair", "timeframe"}
    assert set(specs["get_indicators"]) == {"pair", "timeframe"}
    # get_econ_calendar: exactly {"days"}
    assert set(specs["get_econ_calendar"]) == {"days"}


def test_get_ohlcv_always_returns_at_most_100_bars():
    """入力が何本でも返すのは直近 100 本まで (期間の実質的な固定)。

    F4: Ordering assertions — catches tail(100) → head(100) mutation.
    """
    provider = MagicMock()
    bars = _bars(n=5000)
    provider.get_bars.return_value = bars
    reg = ToolRegistry()
    reg.register_all(market_tools.build(provider, MagicMock(), SETTINGS))
    ohlcv = reg.func("get_ohlcv")(pair="USDJPY", timeframe="1h")
    assert len(ohlcv) == 100
    # Verify we got the LAST (most recent) 100 bars.
    # _bars: bars[0]=oldest(NOW-step*n), bars[n-1]=newest(NOW-step*1)
    # tail(100) returns bars[4900:5000]
    oldest_in_100 = bars[4900].ts.isoformat()
    newest = bars[4999].ts.isoformat()
    assert ohlcv[0]["ts"] == oldest_in_100, \
        f"First entry should be 100th-newest: {ohlcv[0]['ts']} vs {oldest_in_100}"
    assert ohlcv[-1]["ts"] == newest, \
        f"Last entry should be newest: {ohlcv[-1]['ts']} vs {newest}"


def test_news_tools_drop_url_and_credentials():
    """F1: search_news must drop url field and credentials.

    Registry does NOT sanitize successful results, so credential-bearing
    URLs would reach LLM verbatim. LLM has no fetch tool anyway.
    """
    rag = MagicMock()
    rag.search_news.return_value = [
        {"title": "News", "body": "Content", "source_name": "Source",
         "url": "https://feed.example/x?apikey=SECRET_KEY_12345"}
    ]
    reg = ToolRegistry()
    reg.register_all(news_tools.build(rag))
    result = reg.func("search_news")(query="test")

    # Assert url is not in result
    assert "url" not in result[0], "url field must be dropped"
    # Assert no credential leak in JSON
    result_json = json.dumps(result)
    assert "SECRET" not in result_json, "Credentials leaked to LLM"
    # Assert safe fields are present
    assert result[0]["title"] == "News"
    assert result[0]["body"] == "Content"
    assert result[0]["source_name"] == "Source"


def test_news_and_reflection_tools(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    rag = MagicMock()
    rag.search_news.return_value = [{"title": "t", "body": "b", "source_name": "s"}]
    rag.search_reflections.return_value = [{"content": "c"}]
    reg = ToolRegistry()
    reg.register_all(news_tools.build(rag))
    reg.register_all(reflection_tools.build(conn, rag))
    assert reg.func("search_news")(query="usd")[0]["title"] == "t"
    assert reg.func("search_reflections")(query="q")[0]["content"] == "c"
    oid = orders.insert(conn, pair="USDJPY", direction="long",
                        entry_type="market", horizon="day",
                        status=OrderStatus.CLOSED, now=NOW)
    reflections.save(conn, oid, "振り返り本文", NOW)
    other = orders.insert(conn, pair="EURUSD", direction="short",
                          entry_type="market", horizon="day",
                          status=OrderStatus.CLOSED, now=NOW)
    reflections.save(conn, other, "別ペアの振り返り", NOW)
    recent = reg.func("get_recent_reflections")(pair="USDJPY", n=5)
    assert len(recent) == 1  # ペアで絞られる
    assert recent[0]["content"] == "振り返り本文"


def test_account_tools(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    orders.insert(conn, pair="USDJPY", direction="long", entry_type="limit",
                  horizon="day", status=OrderStatus.PENDING_FILL, now=NOW,
                  quantity=0.1, requested_price=148.2, stop_loss=147.8,
                  take_profit=149.0)
    reg = ToolRegistry()
    reg.register_all(account_tools.build(
        conn, PaperBroker(conn, SETTINGS, FixedClock(NOW))))
    pos = reg.func("get_positions")()
    assert pos[0]["order_id"] > 0
    assert pos[0]["status"] == "pending_fill"
    acct = reg.func("get_account")()
    assert acct["balance"] == 1_000_000


def test_all_tools_have_schemas():
    """M-4: cover all tool modules (market/news/account/reflection), not just
    market+news — the test name claims "all tools".

    build() only stores references (no calls made at build time), so plain
    MagicMocks for conn/broker/rag are enough here.
    """
    provider, econ, rag = MagicMock(), MagicMock(), MagicMock()
    conn, broker = MagicMock(), MagicMock()
    tools = (market_tools.build(provider, econ, SETTINGS)
             + news_tools.build(rag)
             + account_tools.build(conn, broker)
             + reflection_tools.build(conn, rag))
    for t in tools:
        assert t.parameters["type"] == "object"
        assert t.description
