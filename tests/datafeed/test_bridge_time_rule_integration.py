"""bridge の時刻規則の検査と afx の ingest / outage の結合。

本物の bridge の `Mt5Client` (Fake の MT5 モジュールを差す) と、本物の
`sources.mt5_bars_range` / `Ingest` / `OutageStateMachine` を HTTP の代わりの
関数でつなぐ。時刻はすべて実時刻 (UTC) の例で固定する。
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import Bar, FixedClock
from agentic_fx.datafeed import sources
from agentic_fx.datafeed.health import DataUnhealthy
from agentic_fx.datafeed.ingest import Ingest
from agentic_fx.datafeed.outage import OutageStateMachine
from agentic_fx.datafeed.price_provider import PriceProvider
from agentic_fx.store import orders
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.ohlcv import upsert_cache_bars

UTC = timezone.utc
PAIR = "USDJPY"
KEY = (PAIR, "1m")
BRIDGE_MODULE = "bridge_mt5_client_under_test"
FIXED_MISMATCH_TEXT = "MT5 bridge time rule mismatch"


def _load_bridge_client():
    if BRIDGE_MODULE in sys.modules:
        return sys.modules[BRIDGE_MODULE]
    path = Path(__file__).parents[2] / "mt5_bridge" / "mt5_client.py"
    spec = importlib.util.spec_from_file_location(BRIDGE_MODULE, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[BRIDGE_MODULE] = module
    spec.loader.exec_module(module)
    return module


bridge = _load_bridge_client()


def _at(*args, second=0) -> datetime:
    return datetime(*args, second=second, tzinfo=UTC)


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now


class _FakeMt5:
    """MT5 の最小 Fake。tick は `first_tick` まで金曜の閉場で凍り、それ以後は時計に追従する。

    1 分足は tick のあった分だけ存在する (日曜は最初の tick の分から)。
    サーバ時刻は `offset` 秒進んでいる (冬の規則は 7200)。
    """

    TIMEFRAME_M1 = 1

    def __init__(self, clock: _Clock, *, offset: int, first_tick: datetime,
                 frozen_tick: datetime, bars_from: datetime, bars_until: datetime | None = None):
        self.clock = clock
        self.offset = offset
        self.first_tick = first_tick
        self.frozen_tick = frozen_tick
        self.bars_from = bars_from.replace(second=0)
        self.bars_until = bars_until

    def symbol_select(self, symbol, enabled):
        return True

    def symbol_info(self, symbol):
        return SimpleNamespace(spread=1)

    def symbol_info_tick(self, symbol):
        at = self.clock.now if self.clock.now >= self.first_tick else self.frozen_tick
        return SimpleNamespace(time=int(at.timestamp()) + self.offset, bid=150.0, ask=150.01)

    def copy_rates_range(self, symbol, timeframe, start, end):
        rates = []
        minute = self.bars_from
        while minute <= self.clock.now:
            if self.bars_until is not None and minute > self.bars_until:
                minute += timedelta(minutes=1)
                continue
            if minute >= self.first_tick.replace(second=0) or minute <= self.frozen_tick:
                server = int(minute.timestamp()) + self.offset
                if int(start.timestamp()) <= server <= int(end.timestamp()):
                    rates.append({"time": server, "open": 150.0, "high": 150.0, "low": 150.0,
                                  "close": 150.0, "tick_volume": 1})
            minute += timedelta(minutes=1)
        return rates

    def last_error(self):
        return (0, "ok")


class _Rig:
    """Fake MT5 + 本物の `Mt5Client` + HTTP の代わりの関数 + afx の ingest / outage。"""

    def __init__(self, tmp_path, monkeypatch, *, start: datetime, mt5_kwargs: dict,
                 cache_until: datetime, conn=None) -> None:
        self.clock = _Clock(start)
        self.client = bridge.Mt5Client(login=1, password="x", server="test")
        self.mt5 = _FakeMt5(self.clock, **mt5_kwargs)
        self.client._mt5 = self.mt5
        self.client._connected = True
        self.client._now = lambda: self.clock.now
        self.statuses: list[tuple[datetime, int]] = []
        monkeypatch.setattr(sources.httpx, "get", self._http_get)

        if conn is None:
            conn = connect(tmp_path / "bars.db")
            init_db(conn)
        self.conn = conn
        minute = cache_until - timedelta(minutes=9)
        while minute <= cache_until:
            upsert_cache_bars(self.conn, [Bar(PAIR, "1m", minute, 150, 150, 150, 150, 1)],
                              source="mt5-live")
            minute += timedelta(minutes=1)
        self.conn.commit()
        settings = SimpleNamespace(
            pairs=[PAIR],
            datafeed=SimpleNamespace(
                primary="mt5", intervals=["1m"], primary_intervals=["1m"],
                ingest_budget_sec=10, closed_bar_grace_sec=30,
                mt5=SimpleNamespace(bridge_url="http://bridge", enabled=True)))
        self.ingest = Ingest(settings)
        self.activity = ActivityLog(tmp_path / "activity.log")
        self.outage = OutageStateMachine(
            self.conn, hard_keys=frozenset({KEY}),
            interval_widths={"1m": timedelta(minutes=1)}, grace=timedelta(seconds=30),
            storage_source="mt5-live", activity=self.activity)
        self.reports = []

    def _http_get(self, url, params=None, headers=None, timeout=None):
        request = httpx.Request("GET", url)
        if "/quote/" in url:
            try:
                quote = self.client.get_quote(url.rsplit("/", 1)[1])
            except bridge.TimeRuleUnavailableError as exc:
                self.statuses.append((self.clock.now, 503))
                return httpx.Response(503, json={"detail": exc.detail}, request=request)
            self.statuses.append((self.clock.now, 200))
            return httpx.Response(200, json=asdict(quote), request=request)
        assert "/ohlcv/" in url
        start = datetime.fromisoformat(params["from"])
        end = datetime.fromisoformat(params["to"])
        try:
            bars = self.client.copy_rates_range(url.rsplit("/", 1)[1], params["interval"],
                                                start, end)
        except bridge.TimeRuleUnavailableError as exc:
            self.statuses.append((self.clock.now, 503))
            return httpx.Response(503, json={"detail": exc.detail}, request=request)
        self.statuses.append((self.clock.now, 200))
        return httpx.Response(200, json={"symbol": PAIR, "interval": params["interval"],
                                         "bars": bars}, request=request)

    def tick(self, now: datetime) -> str:
        self.clock.now = now
        _count, report = self.ingest.prepare(now, self.conn)
        self.ingest.commit(self.conn)
        self.reports.append(report)
        return self.outage.observe(now, report)

    def events(self, prefix: str = "") -> list[str]:
        names = [line.split("\t")[2] for line in self.activity.tail(200)]
        return [name for name in names if name.startswith(prefix)]

    def cache_times(self) -> list[datetime]:
        rows = self.conn.execute(
            "SELECT bar_time FROM ohlcv_cache WHERE symbol=? AND interval='1m' "
            "AND source='mt5-live' ORDER BY bar_time", (PAIR,)).fetchall()
        return [datetime.fromisoformat(row[0]) for row in rows]


def _sunday_open_rig(tmp_path, monkeypatch) -> _Rig:
    # 2026-11-08 (日) 22:00Z 開場。最初の新しい tick は 22:03:10Z。ingest の初回は 22:01:30Z
    return _Rig(
        tmp_path, monkeypatch, start=_at(2026, 11, 8, 22, 1, second=30),
        mt5_kwargs=dict(offset=7200, first_tick=_at(2026, 11, 8, 22, 3, second=10),
                        frozen_tick=_at(2026, 11, 6, 21, 59, second=59),
                        bars_from=_at(2026, 11, 6, 21, 50)),
        cache_until=_at(2026, 11, 6, 21, 59))


def _run_sunday(rig: _Rig, until_minute: int = 10) -> list[tuple[str, str]]:
    seen = []
    for minute in range(1, until_minute + 1):
        now = _at(2026, 11, 8, 22, minute, second=30)
        seen.append((now.strftime("%H:%M:%S"), rig.tick(now)))
    return seen


def _bridge_state_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.name == BRIDGE_MODULE and r.getMessage().startswith("MT5 time rule state")]


def test_sunday_open_while_flat_defers_then_restricts_once_and_recovers(
        tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=BRIDGE_MODULE)
    rig = _sunday_open_rig(tmp_path, monkeypatch)

    states = _run_sunday(rig)

    assert [s for _t, s in states[:2]] == ["ready", "restricted"]      # 22:01:30, 22:02:30
    assert states[-1] == ("22:10:30", "ready")
    assert [(t.strftime("%H:%M:%S"), code) for t, code in rig.statuses[:3]] == [
        ("22:01:30", 503), ("22:02:30", 503), ("22:03:30", 200)]
    # 検査できなかった 2 回は失敗に数えず、key は attempted / failed のどちらにも入らない
    for report in rig.reports[:2]:
        assert KEY not in report.attempted
        assert KEY not in report.succeeded
        assert KEY not in dict(report.failed)
    assert KEY not in rig.ingest.last_errors
    assert rig.events("datafeed_") == ["datafeed_restricted", "datafeed_recovered_auto"]
    assert rig.events("datafeed_degraded") == []
    assert len(rig.events("datafeed_")) <= 2
    # 日曜の足は真の UTC のラベルで入る (21:03 ではない)
    assert _at(2026, 11, 8, 22, 3) in rig.cache_times()
    assert _at(2026, 11, 8, 21, 3) not in rig.cache_times()
    assert _bridge_state_lines(caplog) == ["MT5 time rule state: ok"]
    rig.conn.close()


def test_sunday_open_with_a_swing_position_degrades_once_and_waits_for_resume(
        tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=BRIDGE_MODULE)
    rig = _sunday_open_rig(tmp_path, monkeypatch)
    orders.insert(rig.conn, pair=PAIR, direction="buy", entry_type="market",
                  horizon="swing", status="open", now=_at(2026, 11, 6, 15, 0))
    rig.conn.commit()

    states = _run_sunday(rig)

    assert [s for _t, s in states[:2]] == ["ready", "degraded"]
    assert states[-1] == ("22:10:30", "degraded")                      # resume 待ち
    assert [code for _t, code in rig.statuses[:3]] == [503, 503, 200]
    assert rig.events("datafeed_") == ["datafeed_degraded",
                                       "datafeed_recovered_awaiting_resume"]
    assert rig.events("datafeed_restricted") == []
    assert _bridge_state_lines(caplog) == ["MT5 time rule state: ok"]
    rig.conn.close()


def test_mismatch_degrades_in_one_tick_and_a_flat_account_recovers_by_itself(
        tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=BRIDGE_MODULE)
    rig = _Rig(
        tmp_path, monkeypatch, start=_at(2026, 11, 4, 12, 0, second=30),
        mt5_kwargs=dict(offset=10800, first_tick=_at(2026, 11, 4, 0, 0),
                        frozen_tick=_at(2026, 11, 3, 23, 0), bars_from=_at(2026, 11, 4, 11, 0)),
        cache_until=_at(2026, 11, 4, 11, 58))

    assert rig.tick(_at(2026, 11, 4, 12, 0, second=30)) == "ready"   # 初回の食い違いは未確認 (300 秒までは待つ)
    second = rig.tick(_at(2026, 11, 4, 12, 1, second=30))               # tick が動いて確定

    assert second == "degraded"
    assert rig.ingest.last_errors[KEY] == FIXED_MISMATCH_TEXT
    assert dict(rig.reports[-1].failed)[KEY] == FIXED_MISMATCH_TEXT
    assert rig.events("datafeed_degraded") == ["datafeed_degraded"]

    rig.mt5.offset = 7200                                              # broker の時計が規則に戻る
    states = [rig.tick(_at(2026, 11, 4, 12, minute, second=30)) for minute in range(2, 11)]

    assert states[-1] == "ready"
    assert rig.events("datafeed_degraded") == ["datafeed_degraded"]
    assert rig.events("datafeed_recovered_auto") == ["datafeed_recovered_auto"]
    assert _bridge_state_lines(caplog) == ["MT5 time rule state: mismatch",
                                           "MT5 time rule state: ok"]
    rig.conn.close()


@pytest.mark.parametrize("detail", ["time_rule_mismatch", "time_rule_unverified"])
def test_quote_refusal_reaches_exit_checks_as_a_data_error(tmp_path, monkeypatch, detail):
    """SL / TP の判定が使う quote は、bridge の拒否を DataUnhealthy にして呼び出し側へ返す。"""
    s = load_settings(Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")
    s = s.model_copy(deep=True)
    s.datafeed.mt5.enabled = True
    s.datafeed.primary = "mt5"
    s.datafeed.fallbacks = []
    conn = connect(tmp_path / "q.db")
    init_db(conn)
    provider = PriceProvider(conn, s, FixedClock(_at(2026, 11, 8, 22, 1, second=30)))
    request = httpx.Request("GET", "http://bridge/quote/USDJPY")
    monkeypatch.setattr(sources.httpx, "get", lambda *a, **k: httpx.Response(
        503, json={"detail": detail}, request=request))

    with pytest.raises(DataUnhealthy):
        provider.get_quote(PAIR)
    conn.close()


# ── 資金保護・通知との結合 (Scheduler / Executor / StateStore / 取引 event の observer) ──────────

class _RecordingSink:
    """`TradeEventEmitter` の送り先。本番の sink と同じ 2 つの入口だけを持つ。"""

    def __init__(self) -> None:
        self.events: list[tuple] = []
        self.drops: list[str] = []

    def submit(self, *event) -> None:
        self.events.append(event)

    def note_drop(self, reason: str) -> None:
        self.drops.append(reason)


class _RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, text: str) -> None:
        self.messages.append(text)


class _WireClock:
    def __init__(self, clock: _Clock) -> None:
        self._clock = clock

    def now(self) -> datetime:
        return self._clock.now


SUNDAY_START = _at(2026, 11, 8, 22, 1, second=30)


class _ProductionRig:
    """`_Rig` (bridge + ingest + outage) の DB に、本物の Scheduler / Executor / StateStore を載せる。

    quote は本物の `PriceProvider` が bridge の `/quote` を読み、取引 event は本番と同じ
    `TradeEventEmitter` が `transitions.observe` で受ける。
    """

    def __init__(self, tmp_path, monkeypatch) -> None:
        from tests.core.test_scheduler import Env

        (tmp_path / "sched").mkdir()
        (tmp_path / "rig").mkdir()
        holder: dict = {}
        self.env = Env(tmp_path / "sched", base=SUNDAY_START,
                       state_fn=lambda: holder["rig"].outage.state,
                       quote_fn=lambda pair: holder["provider"].get_quote(pair))
        self.rig = _Rig(
            tmp_path / "rig", monkeypatch, start=SUNDAY_START,
            mt5_kwargs=dict(offset=7200, first_tick=_at(2026, 11, 8, 22, 3, second=10),
                            frozen_tick=_at(2026, 11, 6, 21, 59, second=59),
                            bars_from=_at(2026, 11, 6, 21, 50)),
            cache_until=_at(2026, 11, 6, 21, 59), conn=self.env.conn)
        holder["rig"] = self.rig
        settings = load_settings(
            Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"
        ).model_copy(deep=True)
        settings.datafeed.mt5.enabled = True
        settings.datafeed.primary = "mt5"
        settings.datafeed.fallbacks = []
        holder["provider"] = PriceProvider(self.env.conn, settings,
                                           _WireClock(self.rig.clock))
        self.sink = _RecordingSink()
        self.notifier = _RecordingNotifier()
        self.env.executor.notifier = self.notifier
        from agentic_fx.core import transitions
        from agentic_fx.ops.trade_event_sink import TradeEventEmitter
        self.observer_token = transitions.observe(
            self.env.conn, TradeEventEmitter(self.env.conn, self.sink, state_mode="trading",
                                             ccy="JPY"))
        self.transitions = transitions
        self.kill_switch_seen: list[tuple[bool, int]] = []

    def close(self) -> None:
        self.transitions.unobserve(self.observer_token)
        self.rig.conn.close()

    def tick(self, now: datetime) -> str:
        """本番と同じ順: ingest → outage → scheduler。"""
        state = self.rig.tick(now)
        self.env.sched.tick(now)
        latched = self.env.state.load()
        self.kill_switch_seen.append((latched.kill_switch_latched,
                                      latched.kill_switch_generation))
        return state

    def scheduler_events(self) -> list[str]:
        from agentic_fx.activity import ActivityLog as _Log
        log = _Log(self.env.tmp_path / "a.log")
        return [line.split("\t")[2] for line in log.tail(500)]

    def open_position(self, **fields) -> int:
        values = dict(pair="USDJPY", direction="long", entry_type="market", horizon="swing",
                      status="open", quantity=0.1, avg_fill_price=148.2, stop_loss=140.0,
                      take_profit=160.0, filled_at=_at(2026, 11, 6, 15, 0).isoformat())
        values.update(fields)
        return orders.insert(self.env.conn, now=_at(2026, 11, 6, 15, 0), **values)


@pytest.fixture
def production_rig(tmp_path, monkeypatch):
    rig = _ProductionRig(tmp_path, monkeypatch)
    yield rig
    rig.close()


def test_sunday_open_with_a_position_never_latches_the_kill_switch_and_waits_for_resume(
        production_rig):
    rig = production_rig
    rig.open_position()

    states = []
    missions_when_degraded = None
    for minute in range(1, 11):
        states.append((minute, rig.tick(_at(2026, 11, 8, 22, minute, second=30))))
        if minute == 2:
            missions_when_degraded = rig.env.trade_calls

    assert [s for m, s in states if m in (1, 2)] == ["ready", "degraded"]
    assert states[-1][1] == "degraded"                          # 人の resume 待ち
    assert set(rig.kill_switch_seen) == {(False, 0)}            # latch も世代の更新も無い
    assert rig.sink.events == [] and rig.sink.drops == []       # 注文の状態遷移の通知は 0 件
    assert rig.notifier.messages == []                          # 運用通知も 0 件
    assert rig.env.trade_calls == missions_when_degraded        # degraded の間は新規判断を起こさない
    assert orders.list_by_status(rig.env.conn, "open")          # 建玉は触られない

    # 隙間の足を建玉の SL / TP で見ていないので、確認なしの resume は断られて degraded のまま
    rig.rig.outage.request_resume(_at(2026, 11, 8, 22, 10, second=40))
    assert rig.tick(_at(2026, 11, 8, 22, 11, second=30)) == "degraded"
    assert orders.list_by_status(rig.env.conn, "open")
    rig.rig.outage.request_resume(_at(2026, 11, 8, 22, 11, second=40), acknowledge=True)
    assert rig.tick(_at(2026, 11, 8, 22, 12, second=30)) == "ready"

    assert rig.scheduler_events().count("day_close_deferred") == 0
    assert rig.rig.events("datafeed_") == ["datafeed_degraded",
                                           "datafeed_recovered_awaiting_resume"]
    rig_events = [line.split("\t")[2] for line in rig.rig.activity.tail(200)]
    assert rig_events.count("data_resume_rejected") == 1
    assert rig_events.count("data_resume_accepted") == 1
    assert set(rig.kill_switch_seen) == {(False, 0)}
    assert rig.sink.events == [] and rig.notifier.messages == []


def test_refused_quotes_never_close_at_an_invented_price_and_protection_keeps_running(
        production_rig):
    rig = production_rig
    day_a = rig.open_position(horizon="day")
    day_b = rig.open_position(horizon="day", pair="EURUSD", avg_fill_price=1.10,
                              stop_loss=1.0, take_profit=1.2)
    swing_sl = rig.open_position(avg_fill_price=148.60, stop_loss=148.30)
    rig.env.bars["USDJPY"] = Bar("USDJPY", "1m", _at(2026, 11, 8, 22, 0), 148.4, 148.5, 148.15,
                                 148.2, 10)
    rig.env.bars["EURUSD"] = Bar("EURUSD", "1m", _at(2026, 11, 8, 22, 0), 1.10, 1.101, 1.099,
                                 1.10, 10)

    rig.tick(_at(2026, 11, 8, 22, 1, second=30))          # /quote は 503 time_rule_unverified

    assert (_at(2026, 11, 8, 22, 1, second=30), 503) in rig.rig.statuses     # /quote も /ohlcv も拒否
    assert orders.get(rig.env.conn, day_a)["status"] == "open"
    assert orders.get(rig.env.conn, day_b)["status"] == "open"
    protected = orders.get(rig.env.conn, swing_sl)
    assert (protected["status"], protected["close_reason"]) == ("closed", "sl")
    assert protected["close_price"] == pytest.approx(148.30, abs=0.01)   # 他の建玉の SL は同じ tick で執行
    events = rig.scheduler_events()
    assert events.count("day_close_deferred") == 2
    assert "day_close_error" not in events and "exit_check_error" not in events
    assert [e[0] for e in rig.sink.events] == ["position_closed"]    # 通知は SL の 1 件だけ
    assert rig.notifier.messages == []

    rig.tick(_at(2026, 11, 8, 22, 2, second=30))          # まだ検査できない
    assert orders.get(rig.env.conn, day_a)["status"] == "open"
    assert orders.get(rig.env.conn, day_b)["status"] == "open"
    assert rig.scheduler_events().count("day_close_deferred") == 4    # 毎 tick 再試行して見送る

    rig.tick(_at(2026, 11, 8, 22, 3, second=30))          # 最初の新しい tick を測れた後
    for oid in (day_a, day_b):
        closed = orders.get(rig.env.conn, oid)
        assert (closed["status"], closed["close_reason"]) == ("closed", "day_rollover")
        assert closed["close_price"] == 150.0              # bridge が返した bid (long)
    assert [e[0] for e in rig.sink.events] == ["position_closed"] * 3
    assert rig.sink.drops == []
    assert set(rig.kill_switch_seen) == {(False, 0)}
