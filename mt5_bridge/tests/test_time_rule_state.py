from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from mt5_client import Mt5Client, TimeRuleUnavailableError, utc_to_server_epoch

UTC = timezone.utc


class _Mt5:
    TIMEFRAME_M1 = 1

    def __init__(self, tick_time, rates=()):
        self.tick_time = tick_time
        self.rates = rates
        self.range_args = []

    def symbol_select(self, symbol, enabled):
        return True

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(time=self.tick_time, bid=1.0, ask=1.1)

    def symbol_info(self, symbol):
        return SimpleNamespace(spread=1)

    def copy_rates_range(self, symbol, timeframe, start, end):
        self.range_args.append((start, end))
        return self.rates

    def last_error(self):
        return (0, "ok")


def _client(now, measured_offset, rates=()):
    client = Mt5Client(login=1, password="x", server="test")
    client._mt5 = _Mt5(int(now.timestamp()) + measured_offset, rates)
    client._connected = True
    client._now = lambda: now
    return client


def test_rates_convert_request_and_each_response_by_dst_rule():
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    summer = utc_to_server_epoch(datetime(2026, 10, 30, 20, 59, tzinfo=UTC))
    winter = utc_to_server_epoch(datetime(2026, 11, 2, 22, tzinfo=UTC))
    client = _client(now, 10800, [
        {"time": summer, "open": 1, "high": 1, "low": 1, "close": 1, "tick_volume": 1},
        {"time": winter, "open": 2, "high": 2, "low": 2, "close": 2, "tick_volume": 2},
    ])

    bars = client.copy_rates_range("USDJPY", "1m", datetime(2026, 10, 30, 20, tzinfo=UTC),
                                   datetime(2026, 11, 3, tzinfo=UTC))

    assert [bar["time"] for bar in bars] == ["2026-10-30T20:59:00+00:00",
                                               "2026-11-02T22:00:00+00:00"]
    assert client._mt5.range_args[0] == (
        datetime.fromtimestamp(utc_to_server_epoch(datetime(2026, 10, 30, 20, tzinfo=UTC)), UTC),
        datetime.fromtimestamp(utc_to_server_epoch(datetime(2026, 11, 3, tzinfo=UTC)), UTC),
    )


def test_mismatch_blocks_market_data_until_three_matching_measurements():
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    client = _client(now, 7200)

    with pytest.raises(TimeRuleUnavailableError, match="time_rule_mismatch"):
        client.get_quote("USDJPY")
    assert client.get_server_time()["time_rule_state"] == "mismatch"

    for minute in (1, 2):
        client._now = lambda minute=minute: now.replace(minute=minute)
        client._mt5.tick_time = int(client._now().timestamp()) + 10800
        with pytest.raises(TimeRuleUnavailableError, match="time_rule_mismatch"):
            client.get_quote("USDJPY")
    client._now = lambda: now.replace(minute=3)
    client._mt5.tick_time = int(client._now().timestamp()) + 10800
    assert client.get_quote("USDJPY").time == "2026-07-28T12:03:00+00:00"


def test_closed_market_does_not_measure_and_is_unverified():
    now = datetime(2026, 11, 7, 12, tzinfo=UTC)
    client = _client(now, 7200)

    with pytest.raises(TimeRuleUnavailableError, match="time_rule_unverified"):
        client.get_quote("USDJPY")

    assert client._measured_offset_sec is None
