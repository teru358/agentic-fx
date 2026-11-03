"""時刻規則の検査 (状態 ok / mismatch / unverified) と fail closed のテスト。

換算は NY の規則だけで決まり、tick から測った offset は規則との一致の検査にしか
使わない。検査が通らない間は /ohlcv と /quote を 503 で止める。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone, tzinfo
from types import SimpleNamespace
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest
from fastapi import HTTPException

import mt5_client
import server
from config import BridgeSettings
from mt5_client import Mt5Client, TimeRuleUnavailableError, _market_is_open, utc_to_server_epoch

UTC = timezone.utc
STATE_LOGGER = "mt5_client"


def _at(*args, second=0) -> datetime:
    return datetime(*args, second=second, tzinfo=UTC)


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


class _Mt5:
    """tick は時計に追従する。symbol ごとに「止まった tick」や「取得失敗」を指定できる。"""

    TIMEFRAME_M1 = 1
    TIMEFRAME_H1 = 16385

    def __init__(self, clock: _Clock, offset: int | None = 10800, rates=()):
        self.clock = clock
        self.offset = offset
        self.frozen: dict[str, int | None] = {}
        self.broken: set[str] = set()
        self.rates = rates
        self.tick_calls: list[str] = []
        self.range_args: list[tuple] = []

    def symbol_select(self, symbol, enabled):
        return True

    def symbol_info_tick(self, symbol):
        self.tick_calls.append(symbol)
        if symbol in self.broken:
            raise RuntimeError("terminal gone")
        if symbol in self.frozen:
            value = self.frozen[symbol]
        elif self.offset is None:
            value = None
        else:
            value = int(self.clock.now.timestamp()) + self.offset
        if value is None:
            return None
        return SimpleNamespace(time=value, bid=1.0, ask=1.1)

    def symbol_info(self, symbol):
        return SimpleNamespace(spread=1)

    def copy_rates_range(self, symbol, timeframe, start, end):
        self.range_args.append((start, end))
        return self.rates

    def positions_get(self, **kwargs):
        return []

    def last_error(self):
        return (0, "ok")


def _client(now: datetime, offset: int | None = 10800, rates=()) -> Mt5Client:
    clock = _Clock(now)
    client = Mt5Client(login=1, password="x", server="test")
    client._mt5 = _Mt5(clock, offset, rates)
    client._connected = True
    client._now = lambda: clock.now
    client.clock = clock
    return client


def _stale_tick(client: Mt5Client, offset: int) -> int:
    """測定ガード (30 分丸めの残差 120 秒) で棄却される、10 分古い tick。"""
    return int(client.clock.now.timestamp()) + offset - 600


def _ohlcv(client: Mt5Client, symbol: str = "USDJPY"):
    now = client.clock.now
    return client.copy_rates_range(symbol, "1m", now - timedelta(hours=1), now)


def _state_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.name == STATE_LOGGER and r.getMessage().startswith("MT5 time rule state")]


# ── 市場の開閉 (測定してよい時間帯) ─────────────────────────────────

@pytest.mark.parametrize(("when", "expected"), [
    (_at(2026, 7, 24, 20, 59, second=59), True),     # 夏の金曜の閉場の直前
    (_at(2026, 7, 24, 21, 0), False),
    (_at(2026, 7, 26, 20, 59, second=59), False),    # 夏の日曜の開場の直前
    (_at(2026, 7, 26, 21, 0), True),
    (_at(2026, 11, 6, 21, 59, second=59), True),     # 冬の金曜の閉場の直前
    (_at(2026, 11, 6, 22, 0), False),
    (_at(2026, 11, 8, 21, 59, second=59), False),    # 冬の日曜の開場の直前
    (_at(2026, 11, 8, 22, 0), True),
    (_at(2026, 11, 7, 12, 0), False),                # 土曜
    (_at(2025, 12, 25, 12, 0), False),               # 取引日ラベルが 12/25 (木)
    (_at(2025, 12, 25, 22, 0), True),                # NY 17:00 以後は 12/26 の取引日
    (_at(2026, 1, 1, 12, 0), False),                 # 取引日ラベルが 1/1 (木)
])
def test_measurement_window_follows_the_new_york_rule(when, expected):
    assert _market_is_open(when) is expected


# ── 切替直後のラベル ───────────────────────────────────────────────

def test_first_bar_after_the_switch_is_labelled_by_the_new_rule_without_waiting():
    summer = _at(2026, 10, 30, 20, 0)
    client = _client(summer, 10800)
    assert _ohlcv(client) == []                 # 夏に検査済み (ok)

    client.clock.now = _at(2026, 11, 1, 22, 0, second=5)
    bar_server_epoch = utc_to_server_epoch(_at(2026, 11, 1, 22, 1))
    assert bar_server_epoch - int(_at(2026, 11, 1, 22, 1).timestamp()) == 7200
    client._mt5.offset = 7200
    client._mt5.rates = [{"time": bar_server_epoch, "open": 1, "high": 1, "low": 1,
                          "close": 1, "tick_volume": 1}]

    bars = _ohlcv(client)

    assert [bar["time"] for bar in bars] == ["2026-11-01T22:01:00+00:00"]
    assert client.get_server_time()["time_rule_state"] == "ok"


# ── 食い違い (mismatch) ────────────────────────────────────────────

def _mismatching_client():
    client = _client(_at(2026, 7, 28, 12, 0), 7200)     # 規則は夏 = 10800
    with pytest.raises(TimeRuleUnavailableError, match="time_rule_mismatch"):
        client.get_quote("USDJPY")
    return client


def test_mismatch_blocks_market_data_and_reports_the_evidence():
    client = _mismatching_client()
    with pytest.raises(TimeRuleUnavailableError, match="time_rule_mismatch"):
        _ohlcv(client)

    info = client.get_server_time()
    assert info["measured_offset_sec"] == 7200
    assert info["expected_offset_sec"] == 10800
    assert info["offset_matches_rule"] is False
    assert info["time_rule_state"] == "mismatch"
    assert info["server_offset_sec"] == 10800
    assert info["offset_source"] == "rule"
    assert info["measured_at"] == "2026-07-28T12:00:00+00:00"
    assert info["measured_age_sec"] == 0
    assert client.time_rule_status() == ("mismatch", "2026-07-28T12:00:00+00:00")
    assert client.get_positions() == []          # 口座・建玉の endpoint は止めない


def test_mismatch_returns_only_after_three_consecutive_matching_measurements():
    client = _mismatching_client()
    client._mt5.offset = 10800

    client.clock.advance(seconds=60)
    with pytest.raises(TimeRuleUnavailableError, match="time_rule_mismatch"):
        _ohlcv(client)
    client.clock.advance(seconds=60)
    with pytest.raises(TimeRuleUnavailableError, match="time_rule_mismatch"):
        _ohlcv(client)
    assert client.get_server_time()["match_streak"] == 2

    client.clock.advance(seconds=60)
    assert _ohlcv(client) == []
    assert client.get_server_time()["time_rule_state"] == "ok"


def test_rejected_measurement_does_not_break_the_streak_and_other_value_resets_it():
    client = _mismatching_client()
    client._mt5.offset = 10800
    client.clock.advance(seconds=60)
    client.get_server_time()                                 # 一致 1
    client._mt5.frozen["USDJPY"] = _stale_tick(client, 10800)
    client.clock.advance(seconds=60)
    client.get_server_time()                                 # 棄却 (止まった tick)
    assert client.get_server_time()["match_streak"] == 1
    del client._mt5.frozen["USDJPY"]
    client.clock.advance(seconds=60)
    assert client.get_server_time()["match_streak"] == 2     # 一致 2

    client._mt5.offset = 7200
    client.clock.advance(seconds=60)
    assert client.get_server_time()["match_streak"] == 0     # 違う値の受理で 0

    client._mt5.offset = 10800
    for expected in (1, 2):
        client.clock.advance(seconds=60)
        assert client.get_server_time()["match_streak"] == expected
    with pytest.raises(TimeRuleUnavailableError, match="time_rule_mismatch"):
        _ohlcv(client)


def test_mismatch_stays_mismatch_after_the_measurement_gets_old():
    client = _mismatching_client()
    client._mt5.frozen["USDJPY"] = _stale_tick(client, 10800)
    for _ in range(8):
        client.clock.advance(seconds=60)
        client.get_server_time()

    info = client.get_server_time()
    assert info["measured_age_sec"] == 480
    assert info["time_rule_state"] == "mismatch"


def test_state_changes_are_logged_once_each(caplog):
    caplog.set_level(logging.WARNING, logger=STATE_LOGGER)
    client = _mismatching_client()
    for _ in range(3):
        client.get_server_time()
    client._mt5.offset = 10800
    for _ in range(3):
        client.clock.advance(seconds=60)
        client.get_server_time()
    for _ in range(3):
        client.get_server_time()

    assert _state_lines(caplog) == ["MT5 time rule state: mismatch",
                                    "MT5 time rule state: ok"]


# ── 未検査 (unverified) ────────────────────────────────────────────

def test_first_request_measures_before_answering_and_serves_when_the_tick_is_fresh():
    client = _client(_at(2026, 11, 4, 12, 0), 7200)

    assert _ohlcv(client) == []

    assert client.get_server_time()["time_rule_state"] == "ok"


@pytest.mark.parametrize("failure", ["stale", "no_tick", "raises"])
def test_first_request_without_a_usable_tick_is_unverified(failure):
    client = _client(_at(2026, 11, 4, 12, 0), 7200)
    if failure == "stale":
        client._mt5.frozen["USDJPY"] = _stale_tick(client, 7200)
    elif failure == "no_tick":
        client._mt5.offset = None
    else:
        client._mt5.broken.add("USDJPY")

    with pytest.raises(TimeRuleUnavailableError, match="time_rule_unverified"):
        _ohlcv(client)
    assert client._measured_offset_sec is None


def test_measurement_older_than_five_minutes_is_unverified_until_one_match():
    client = _client(_at(2026, 11, 4, 12, 0, second=0), 7200)
    assert _ohlcv(client) == []                      # 12:00:00 に一致を受理
    client._mt5.frozen["USDJPY"] = _stale_tick(client, 7200)

    for minute in (1, 2, 3, 4):                       # 12:01〜12:04 の測定はすべて棄却
        client.clock.now = _at(2026, 11, 4, 12, minute)
        assert _ohlcv(client) == []
    client.clock.now = _at(2026, 11, 4, 12, 5, second=0)
    assert _ohlcv(client) == []                       # 年齢 300 秒は ok
    assert client.get_server_time()["measured_age_sec"] == 300

    client.clock.now = _at(2026, 11, 4, 12, 5, second=1)
    with pytest.raises(TimeRuleUnavailableError, match="time_rule_unverified"):
        _ohlcv(client)
    info = client.get_server_time()
    assert info["measured_age_sec"] == 301
    assert info["time_rule_state"] == "unverified"

    del client._mt5.frozen["USDJPY"]
    client.clock.now = _at(2026, 11, 4, 12, 6, second=1)
    assert _ohlcv(client) == []                       # 一致 1 回で ok
    assert client.get_server_time()["time_rule_state"] == "ok"


def test_calls_less_than_a_minute_apart_do_not_read_ticks_again():
    client = _client(_at(2026, 11, 4, 12, 0), 7200)
    _ohlcv(client)
    assert client._mt5.tick_calls == ["USDJPY"]

    for seconds in (10, 30, 59):
        client.clock.now = _at(2026, 11, 4, 12, 0) + timedelta(seconds=seconds)
        _ohlcv(client)
        client.get_server_time()
        client.get_positions()
    assert client._mt5.tick_calls == ["USDJPY"]

    client.clock.now = _at(2026, 11, 4, 12, 1)
    _ohlcv(client)
    assert client._mt5.tick_calls == ["USDJPY", "USDJPY"]


def test_requested_symbol_tick_keeps_the_check_alive_when_the_probe_symbol_stalls():
    # 日次ロールオーバーで probe 銘柄だけ 8 分止まる形 (2026-09-28 21:01-21:08)
    client = _client(_at(2026, 11, 4, 12, 0), 7200)
    client._mt5.frozen["USDJPY"] = int(client.clock.now.timestamp()) + 7200 - 480

    assert _ohlcv(client, "EURUSD") == []
    assert client.get_quote("EURUSD").time == "2026-11-04T12:00:00+00:00"
    assert client.get_server_time()["time_rule_state"] == "ok"
    assert sorted(set(client._mt5.tick_calls)) == ["EURUSD", "USDJPY"]


def test_closed_market_does_not_measure_and_is_unverified():
    client = _client(_at(2026, 11, 7, 12, 0), 7200)

    with pytest.raises(TimeRuleUnavailableError, match="time_rule_unverified"):
        _ohlcv(client)
    with pytest.raises(TimeRuleUnavailableError, match="time_rule_unverified"):
        client.get_quote("USDJPY")

    assert client._mt5.tick_calls == ["USDJPY"]       # get_quote 自身の 1 回だけ
    assert client._measured_offset_sec is None


def test_weekend_fake_offset_is_never_measured():
    # 2026-10-03 (土) に tick が金曜の閉場で凍る。測れば 30 分ごとに -1800 を採る形
    frozen = int(_at(2026, 10, 2, 20, 59, second=59).timestamp()) + 10800
    for when in (_at(2026, 10, 3, 0, 30), _at(2026, 10, 3, 6, 0), _at(2026, 10, 3, 12, 0)):
        client = _client(when, 10800)
        client._mt5.frozen["USDJPY"] = frozen

        for _ in range(3):
            client.get_server_time()
            client.get_positions()
            client.clock.advance(seconds=61)

        info = client.get_server_time()
        assert info["measured_offset_sec"] is None
        assert info["measured_at"] is None
        assert info["time_rule_state"] == "unverified"
        assert info["server_offset_sec"] == 10800
        assert client._mt5.tick_calls == []


# ── 起動時の tz データ自己検査 ─────────────────────────────────────

class _UnreadableZone(tzinfo):
    def utcoffset(self, dt):
        raise ZoneInfoNotFoundError("America/New_York")

    def dst(self, dt):
        return None

    def tzname(self, dt):
        return None


@pytest.mark.parametrize("broken", [_UnreadableZone(), ZoneInfo("Etc/UTC")])
def test_constructor_refuses_to_start_without_correct_zone_data(monkeypatch, broken):
    monkeypatch.setattr(mt5_client, "NY", broken)

    with pytest.raises(RuntimeError, match="time-zone data"):
        Mt5Client(login=1, password="x", server="test")


def test_lifespan_does_not_come_up_without_zone_data(monkeypatch, tmp_path):
    settings = BridgeSettings(
        mt5_login=1, mt5_password="x", mt5_server="test", host="127.0.0.1", port=8812,
        api_key="", dry_run=True, halt_state_path=str(tmp_path / "halt.flag"),
        filling_mode="IOC", deviation_points=30, server_time_symbol="USDJPY",
    )
    monkeypatch.setattr(server, "load_settings", lambda: settings)
    monkeypatch.setattr(server, "_client", None)
    monkeypatch.setattr(server, "_settings", None)
    monkeypatch.setattr(server, "_runtime", None)
    monkeypatch.setattr(mt5_client, "NY", _UnreadableZone())

    async def start():
        async with server.lifespan(server.app):
            pass

    with pytest.raises(RuntimeError, match="time-zone data"):
        asyncio.run(start())
    assert server._client is None


def test_zone_data_self_check_passes_with_the_real_database():
    mt5_client._validate_time_rule_data()


# ── endpoint の対応 (503 の detail と /health の項目) ──────────────────

class _EndpointClient:
    is_connected = True

    def __init__(self, error=None):
        self._error = error

    def get_quote(self, symbol):
        raise self._error

    def copy_rates_range(self, *args):
        raise self._error

    def ping(self):
        return True

    def time_rule_status(self):
        return "mismatch", "2026-07-28T12:00:00+00:00"


@pytest.mark.parametrize("detail", ["time_rule_mismatch", "time_rule_unverified"])
def test_market_data_endpoints_answer_503_with_the_state_as_detail(monkeypatch, detail):
    monkeypatch.setattr(server, "_client", _EndpointClient(TimeRuleUnavailableError(detail)))
    monkeypatch.setattr(server, "_settings", SimpleNamespace(auth_required=False))

    for call in (lambda: server.quote("USDJPY"),
                 lambda: server.ohlcv("USDJPY", from_="2026-07-28T00:00:00+00:00",
                                      to="2026-07-28T12:00:00+00:00", interval="1h")):
        with pytest.raises(HTTPException) as exc:
            call()
        assert (exc.value.status_code, exc.value.detail) == (503, detail)


def test_health_stays_200_and_reports_the_state(monkeypatch):
    monkeypatch.setattr(server, "_client", _EndpointClient())
    monkeypatch.setattr(server, "_settings", None)
    monkeypatch.setattr(server, "_runtime", None)

    body = server.health()

    assert (body.status, body.time_rule_state, body.measured_at) == (
        "ok", "mismatch", "2026-07-28T12:00:00+00:00")
