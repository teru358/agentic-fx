"""`GET /server-time` の配線テスト。

規則から導いた時計、測定の証拠、検査の状態を 1 回の応答で返す。市場データの
503 (detail が状態) は test_time_rule_state.py が見る。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import server


class _FakeClient:
    def __init__(self, *, connected: bool = True) -> None:
        self.is_connected = connected

    def get_server_time(self):
        return {
            "server_offset_sec": 10800,
            "server_time": "2026-07-28T14:12:04+00:00",
            "utc_time": "2026-07-28T11:12:04+00:00",
            "offset_source": "rule",
            "measured_offset_sec": 10800,
            "measured_at": "2026-07-28T11:12:00+00:00",
            "measured_age_sec": 4,
            "expected_offset_sec": 10800,
            "offset_matches_rule": True,
            "match_streak": 5,
            "time_rule_state": "ok",
        }


def _patch(monkeypatch, client) -> None:
    monkeypatch.setattr(server, "_client", client)
    monkeypatch.setattr(server, "_settings", SimpleNamespace(auth_required=False))


def test_server_time_returns_both_clocks_and_the_check_state(monkeypatch):
    _patch(monkeypatch, _FakeClient())
    body = server.server_time()
    assert body["server_offset_sec"] == 10800
    assert body["offset_source"] == "rule"
    assert body["utc_time"] == "2026-07-28T11:12:04+00:00"
    assert body["time_rule_state"] == "ok"
    assert body["offset_matches_rule"] is True


def test_server_time_requires_mt5_connection(monkeypatch):
    _patch(monkeypatch, _FakeClient(connected=False))
    with pytest.raises(HTTPException) as exc:
        server.server_time()
    assert exc.value.status_code == 503
