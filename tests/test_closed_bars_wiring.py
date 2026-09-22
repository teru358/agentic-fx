from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from agentic_fx.backtest.timeframes import load_resampled_frame
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import Bar, FixedClock, Quote
from agentic_fx.datafeed.price_provider import PriceProvider
from agentic_fx.plugin.loader import PluginMeta
from agentic_fx.plugin.signal_producer import SignalProducer
from agentic_fx.service import build_app, run_init
from agentic_fx.store import ohlcv, orders, snapshots
from agentic_fx.tools import market_tools

from tests.store.test_rag import FakeEmbedding


UTC = timezone.utc


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.value = now

    def now(self) -> datetime:
        return self.value


class RecordingActivity:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def write(self, _category, event: str, _summary: str, ref_id=None) -> None:
        self.events.append((event, ref_id))


def _init(root: Path) -> None:
    (root / "config").mkdir()
    example = (Path(__file__).parents[1] / "config" / "settings.yaml.example")
    (root / "config" / "settings.yaml.example").write_text(
        example.read_text(encoding="utf-8"), encoding="utf-8")
    with patch("agentic_fx.service.PriceProvider") as provider, \
         patch("agentic_fx.service._check_llama_swap"):
        provider.return_value.healthcheck.return_value = "yfinance"
        assert run_init(root) == 0


def _bar(ts: datetime, *, low: float = 156.60, close: float = 156.70) -> Bar:
    return Bar("USDJPY", "1m", ts, 156.70, max(156.80, close), low, close, 10.0)


def _app(root: Path, now: datetime, bars: dict[str, Bar | None]):
    app = build_app(
        root, clock=FixedClock(now), embedding_fn=FakeEmbedding(),
        quote_fn=lambda pair: Quote(pair, 156.69, 156.71, now, "test"),
        bars_fn=lambda pair: bars.get(pair))
    app.scheduler.on_improve_tick = None
    app.scheduler.on_news_cycle = lambda: None
    app.scheduler.on_econ_cycle = lambda: None
    app.scheduler.on_signal_maintenance = None
    app.scheduler.on_cache_maintenance = None
    return app


def test_forming_bar_never_reaches_readers_after_fetch_failure(tmp_path):
    first_now = datetime(2026, 7, 22, 9, 0, 30, tzinfo=UTC)
    clock = MutableClock(first_now)
    _init(tmp_path)
    settings = load_settings(tmp_path / "config" / "settings.yaml")
    from agentic_fx.store.db import connect
    conn = connect(tmp_path / "data" / "agentic.db")
    provider = PriceProvider(conn, settings, clock)
    closed = _bar(datetime(2026, 7, 22, 8, 59, tzinfo=UTC))
    forming = _bar(datetime(2026, 7, 22, 9, 0, tzinfo=UTC), close=157.70)

    with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               side_effect=[[closed, forming], RuntimeError("offline")]):
        assert provider.latest_1m_bar("USDJPY").ts == closed.ts
        assert [b.ts for b in ohlcv.load_cache_bars(
            conn, "USDJPY", "1m", source="yfinance")] == [closed.ts]

        app = build_app(tmp_path, provider=provider, clock=clock,
                        embedding_fn=FakeEmbedding())
        app.scheduler.on_improve_tick = None
        app.scheduler.on_news_cycle = lambda: None
        app.scheduler.on_econ_cycle = lambda: None
        app.scheduler.on_signal_maintenance = None
        app.scheduler.on_cache_maintenance = None
        orders.insert(
            app.conn_core, pair="USDJPY", direction="long", entry_type="market",
            horizon="day", status="open", now=first_now - timedelta(minutes=5),
            quantity=0.01, remaining_quantity=0.0, avg_fill_price=156.70,
            filled_quantity=0.01, stop_loss=100.0, take_profit=300.0,
            filled_at=(first_now - timedelta(minutes=5)).isoformat())
        app.scheduler.tick(first_now)
        initial = snapshots.latest(app.conn_core)
        assert initial is not None

        clock.value = datetime(2026, 7, 22, 9, 1, 45, tzinfo=UTC)
        app.scheduler.tick(clock.now())
        after_failure = snapshots.latest(app.conn_core)
        assert after_failure is not None
        assert (after_failure["equity"], after_failure["hwm"]) == (
            initial["equity"], initial["hwm"])
        assert provider.latest_1m_bar("USDJPY").ts == closed.ts
        # キャッシュ層に形成中行が残る場合でも、producer の reader 境界は
        # cutoff/grace で同じ行を読ませない。
        clock.value = datetime(2026, 7, 22, 9, 2, 15, tzinfo=UTC)
        later_forming = _bar(datetime(2026, 7, 22, 9, 1, tzinfo=UTC))
        ohlcv.upsert_cache_bars(conn, [later_forming], source="yfinance")

        producer = SignalProducer()
        meta = PluginMeta("one_minute", "signal", tmp_path, {}, "1m",
                          ("USDJPY",), 1, "test-hash")
        seen: list[datetime] = []

        def record(_meta, payload, *, settings):
            seen.extend(ts.to_pydatetime() for ts in payload["df"].index)
            return {"signals": []}

        producer.evaluate_due_plugins(
            conn, plugins=[meta], now=clock.now(), source="yfinance",
            sandbox_run=record, settings=settings, resolved_by_identity={})
        frame = load_resampled_frame(
            conn, "USDJPY", "1m", source="yfinance", base_interval="1m",
            until=clock.now(), cutoff=clock.now(),
            grace=timedelta(seconds=settings.datafeed.closed_bar_grace_sec),
            max_bars=10)
        assert later_forming.ts not in frame.index
        assert all(ts != later_forming.ts for ts in seen)

        readonly = PriceProvider(conn, settings, clock, readonly=True)
        tool = next(t for t in market_tools.build(readonly, object(), settings)
                    if t.name == "get_ohlcv")
        rows = tool.func("USDJPY", "1m")
        assert rows[-1]["ts"] != forming.ts.isoformat()
        app.close()
    conn.close()


def test_forming_bar_does_not_advance_exit_cursor_or_duplicate_stop_fill(tmp_path):
    now = datetime(2026, 7, 22, 9, 0, 30, tzinfo=UTC)
    _init(tmp_path)
    from agentic_fx.store.db import connect
    clock = MutableClock(now)
    provider_conn = connect(tmp_path / "data" / "agentic.db")
    provider = PriceProvider(
        provider_conn, load_settings(tmp_path / "config" / "settings.yaml"), clock)
    forming = _bar(now.replace(second=0), low=156.60)
    closed = _bar(now.replace(second=0), low=156.40)
    app = build_app(tmp_path, provider=provider, clock=clock,
                    embedding_fn=FakeEmbedding())
    app.scheduler.on_improve_tick = None
    app.scheduler.on_news_cycle = lambda: None
    app.scheduler.on_econ_cycle = lambda: None
    app.scheduler.on_signal_maintenance = None
    app.scheduler.on_cache_maintenance = None
    try:
        order_id = orders.insert(
            app.conn_core, pair="USDJPY", direction="long", entry_type="market",
            horizon="day", status="open", now=now - timedelta(minutes=5),
            quantity=0.01, remaining_quantity=0.0, avg_fill_price=156.70,
            filled_quantity=0.01, stop_loss=156.45, take_profit=157.20,
            filled_at=(now - timedelta(minutes=5)).isoformat())
        with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
                   side_effect=lambda *_args: [forming if clock.value == now else closed]), \
             patch("agentic_fx.datafeed.price_provider.sources.yf_quote",
                   side_effect=lambda pair: Quote(pair, 156.69, 156.71,
                                                   clock.now(), "test")):
            app.scheduler.tick(now)
            assert orders.get(app.conn_core, order_id)["status"] == "open"
            assert "USDJPY" not in app.scheduler._processed_bar_ts

            clock.value = now + timedelta(seconds=61)
            app.scheduler.tick(clock.now())
            assert orders.get(app.conn_core, order_id)["status"] == "closed"
            closed_count = app.conn_core.execute(
                "SELECT COUNT(*) FROM orders WHERE id=? AND status='closed'", (order_id,)
            ).fetchone()[0]
            clock.value += timedelta(seconds=1)
            app.scheduler.tick(clock.now())
            assert app.conn_core.execute(
                "SELECT COUNT(*) FROM orders WHERE id=? AND status='closed'", (order_id,)
            ).fetchone()[0] == closed_count == 1
    finally:
        app.close()
        provider_conn.close()


def test_unavailable_closed_bar_is_reported_per_pair_and_after_restart(tmp_path):
    now = datetime(2026, 7, 22, 9, 0, 30, tzinfo=UTC)
    _init(tmp_path)
    bars: dict[str, Bar | None] = {
        "USDJPY": _bar(now - timedelta(minutes=10)),
        "GBPJPY": _bar(now),
    }
    app = _app(tmp_path, now, bars)
    activity = RecordingActivity()
    app.scheduler.activity = activity
    try:
        order_id = orders.insert(
            app.conn_core, pair="USDJPY", direction="long", entry_type="market",
            horizon="day", status="pending_fill", now=now - timedelta(minutes=5),
            quantity=0.01, remaining_quantity=0.0, avg_fill_price=156.70,
            filled_quantity=0.01, stop_loss=156.00, take_profit=157.20,
            filled_at=(now - timedelta(minutes=5)).isoformat())
        orders.insert(
            app.conn_core, pair="GBPJPY", direction="long", entry_type="limit",
            horizon="day", status="pending_fill", now=now - timedelta(minutes=5),
            quantity=0.01, remaining_quantity=0.01, avg_fill_price=None,
            filled_quantity=0.0, stop_loss=156.00, take_profit=157.20)
        app.scheduler.tick(now)
        app.scheduler.tick(now + timedelta(seconds=1))
        unavailable = [e for e in activity.events if e[0] == "closed_bar_unavailable"]
        assert unavailable == [("closed_bar_unavailable", "USDJPY")]

        bars["GBPJPY"] = None
        app.scheduler.tick(now + timedelta(seconds=2))
        unavailable = [e for e in activity.events if e[0] == "closed_bar_unavailable"]
        assert unavailable == [
            ("closed_bar_unavailable", "USDJPY"),
            ("closed_bar_unavailable", "GBPJPY"),
        ]

        bars["USDJPY"] = _bar(now)
        app.scheduler.tick(now + timedelta(seconds=3))
        unavailable = [e for e in activity.events if e[0] == "closed_bar_unavailable"]
        assert unavailable == [
            ("closed_bar_unavailable", "USDJPY"),
            ("closed_bar_unavailable", "GBPJPY"),
        ]
        app.scheduler = type(app.scheduler)(
            conn=app.conn_core, executor=app.executor, settings=app.settings,
            state_store=app.state, activity=activity, bars_fn=app.scheduler.bars_fn,
            on_trade_mission=app.scheduler.on_trade_mission,
            on_news_cycle=lambda: None, on_econ_cycle=lambda: None)
        app.scheduler.tick(now + timedelta(seconds=4))
        assert [e for e in activity.events if e[0] == "closed_bar_unavailable"] == [
            ("closed_bar_unavailable", "USDJPY"),
            ("closed_bar_unavailable", "GBPJPY"),
            ("closed_bar_unavailable", "GBPJPY"),
        ]
    finally:
        app.close()
