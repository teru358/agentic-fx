"""`GET /server-time` と、オフセット未確定時の 503 配線テスト。

オフセットが確定していない状態で時刻付きの値を返すと、ずれた時刻のまま
サイジングや発注判断が進んでしまう。読み取り系エンドポイントは
ServerTimeUnknownError を 503 に map し、呼出側に「今は使えない」と伝える。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import server
from mt5_client import ServerTimeUnknownError


class _FakeClient:
    def __init__(self, *, connected: bool = True, raises: bool = False) -> None:
        self.is_connected = connected
        self._raises = raises

    def _boom(self):
        raise ServerTimeUnknownError(
            "MT5 server time offset is unknown; refusing to report times"
        )

    def get_server_time(self):
        if self._raises:
            self._boom()
        return {
            "server_offset_sec": 10800,
            "server_time": "2026-07-28T14:12:04+00:00",
            "utc_time": "2026-07-28T11:12:04+00:00",
            "offset_source": "live",
        }

    def get_quote(self, symbol):
        self._boom()

    def get_positions(self):
        self._boom()

    def get_closed_deal(self, ticket):
        self._boom()

    def copy_rates_range(self, symbol, interval, date_from, date_to):
        self._boom()


def _patch(monkeypatch, client) -> None:
    monkeypatch.setattr(server, "_client", client)
    monkeypatch.setattr(server, "_settings", SimpleNamespace(auth_required=False))


def test_server_time_returns_both_clocks(monkeypatch):
    _patch(monkeypatch, _FakeClient())
    body = server.server_time()
    assert body["server_offset_sec"] == 10800
    assert body["offset_source"] == "live"
    assert body["utc_time"] == "2026-07-28T11:12:04+00:00"


def test_server_time_returns_503_when_offset_unknown(monkeypatch):
    _patch(monkeypatch, _FakeClient(raises=True))
    with pytest.raises(HTTPException) as exc:
        server.server_time()
    assert exc.value.status_code == 503


def test_server_time_requires_mt5_connection(monkeypatch):
    _patch(monkeypatch, _FakeClient(connected=False))
    with pytest.raises(HTTPException) as exc:
        server.server_time()
    assert exc.value.status_code == 503


@pytest.mark.parametrize("call", [
    lambda: server.quote("USDJPY"),
    lambda: server.positions(),
    lambda: server.closed_deal(123),
    lambda: server.ohlcv("USDJPY", from_="2026-07-28T00:00:00+00:00",
                         to="2026-07-28T12:00:00+00:00", interval="1h"),
])
def test_read_endpoints_return_503_when_offset_unknown(monkeypatch, call):
    """404/500 に化けさせない。オフセット未確定は一時的な利用不能 = 503。"""
    _patch(monkeypatch, _FakeClient(raises=True))
    with pytest.raises(HTTPException) as exc:
        call()
    assert exc.value.status_code == 503
