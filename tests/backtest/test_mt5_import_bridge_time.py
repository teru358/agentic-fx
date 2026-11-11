"""import_mt5 と bridge のサーバ時刻 (NY+7h) 換算の結合。

本物の `mt5_bridge/mt5_client.py` の `Mt5Client.copy_rates_range` (Fake の MT5 モジュール
を差す) の応答を、HTTP の代わりの関数で import_mt5 へ渡す。bridge は起動しない。
Fake の MT5 はサーバ時刻の epoch で範囲を受け (両端 inclusive)、足の時刻もサーバ時刻の
epoch で返す。夏 (UTC+3)・冬 (UTC+2)・DST 切替週の窓端を実時刻の例で固定する。
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from agentic_fx.backtest.mt5_import import effective_import_end, import_mt5
from agentic_fx.store import ohlcv
from tests.backtest.factories import _conn

UTC = timezone.utc
GRACE = timedelta(seconds=30)
W = timedelta(minutes=5)
MODULE = "bridge_mt5_client_for_import_time"


def _load_bridge():
    if MODULE in sys.modules:
        return sys.modules[MODULE]
    path = Path(__file__).parents[2] / "mt5_bridge" / "mt5_client.py"
    spec = importlib.util.spec_from_file_location(MODULE, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE] = module
    spec.loader.exec_module(module)
    return module


bridge = _load_bridge()


class _EarlierTicks(dict):
    def get(self, key, default=None):
        return super().get(key, 1)


class _FakeMt5:
    TIMEFRAME_M5 = 5

    def __init__(self, now: datetime):
        self.now = now

    def symbol_select(self, symbol, enable):
        return True

    def symbol_info_tick(self, symbol):
        return type("Tick", (), {"time": int(self.now.timestamp()) + self.offset(),
                                 "bid": 1.0, "ask": 1.0})()

    def offset(self):
        return bridge.utc_to_server_epoch(self.now) - int(self.now.timestamp())

    def copy_rates_range(self, symbol, tf, date_from, date_to):
        """サーバ時刻 epoch の [from, to] (inclusive) にある 5 分足を返す。"""
        lo, hi = int(date_from.timestamp()), int(date_to.timestamp())
        out = []
        base = date_from.astimezone(UTC) - timedelta(days=1)
        t = base - timedelta(seconds=(base - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds() % 300)
        stop = date_to.astimezone(UTC) + timedelta(days=1)
        while t <= stop:
            server = bridge.utc_to_server_epoch(t)
            # DST の巻き戻しで同じサーバ時刻が 2 回ある足は、実 MT5 も曖昧なので出さない
            if bridge.server_epoch_to_utc(server) == t and lo <= server <= hi:
                out.append({"time": server, "open": 1.0, "high": 2.0, "low": 0.5,
                            "close": 1.5, "tick_volume": 3})
            t += W
        return out

    def last_error(self):
        return (0, "ok")


def _bridge_fetch(now: datetime, calls: list):
    client = bridge.Mt5Client(login=1, password="x", server="Test")
    client._mt5 = _FakeMt5(now)
    client._connected = True
    client._now = lambda: now
    client._last_tick_time = _EarlierTicks()

    def fetch(url):
        q = parse_qs(urlsplit(url).query)
        lo = datetime.fromisoformat(q["from"][0])
        hi = datetime.fromisoformat(q["to"][0])
        calls.append((lo, hi))
        bars = client.copy_rates_range("USDJPY", q["interval"][0], lo, hi)
        return {"symbol": "USDJPY", "interval": q["interval"][0], "bars": bars}
    return fetch


def _unambiguous_grid(start: datetime, end: datetime):
    out, t = [], start
    while t < end:
        if bridge.server_epoch_to_utc(bridge.utc_to_server_epoch(t)) == t:
            out.append(t)
        t += W
    return out


CASES = [
    ("summer", datetime(2026, 7, 22, 12, 5, 20, tzinfo=UTC), 10800),
    ("winter", datetime(2026, 11, 4, 12, 5, 20, tzinfo=UTC), 7200),
    ("fall-back week", datetime(2026, 11, 2, 12, 5, 30, tzinfo=UTC), 7200),
    ("spring-forward week", datetime(2027, 3, 17, 12, 5, 30, tzinfo=UTC), 10800),
]


@pytest.mark.parametrize("name, now, offset", CASES, ids=[c[0] for c in CASES])
def test_window_edges_survive_the_servers_time_conversion(tmp_path, name, now, offset):
    assert bridge.utc_to_server_epoch(now) - int(now.timestamp()) == offset
    conn = _conn(tmp_path)
    start = datetime(now.year, now.month, now.day, tzinfo=UTC) - timedelta(days=4)
    requested_end = start + timedelta(days=8)          # 未来の終端 (now より後)
    calls: list = []
    result = import_mt5(conn, "USDJPY", start, requested_end, base_url="http://x",
                        interval="5m", fetch=_bridge_fetch(now, calls),
                        clock=lambda: now, grace=GRACE)
    end = effective_import_end(requested_end, now, grace=GRACE, interval="5m")
    stored = [b.ts for b in ohlcv.load_history_bars(conn, "USDJPY", "5m", source="mt5")]
    expected = _unambiguous_grid(start, end)
    assert stored == expected                      # 窓端も切替週の足も、過不足・重複なし
    assert result.conflicted == 0
    assert requested_end > now and stored[-1] == end - W      # 実効終端の手前の足まで
    assert end == (now.replace(minute=0, second=0) - (W if now.second < 30 else timedelta(0)))
    assert len(calls) == 5 and calls[-1][1] == end           # 日窓 4 本 + 実効終端までの最終窓
    assert len(set(stored)) == len(stored)
