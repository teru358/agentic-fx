"""notify の取引 event の配送方針 (AC-21、AC-23)。

`deliver_trade_events` が false (既定) のとき、完全検証を通った取引 event は配送せずに cursor と
high watermark を進める。true のときは他の event と同じ形で配送する。検証を通らない event と未知
code は従来どおり進まず止まる。
"""
from __future__ import annotations

import io
import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from afx_ops.api_client import Reply
from afx_ops.notify import runtime
from afx_ops.notify.state import NotifyState, initialize_event_cursor, load, save

URL = b"https://hooks.example.test/afx/9f2c"
TS = "2026-10-20T00:00:00+00:00"


def _health(event_id: int) -> dict:
    return {"id": event_id, "ts": TS, "code": "service_health_changed", "ref": "instance",
            "fields": {"component_code": "api", "health_code": "ok"}}


def _order(event_id: int, **fields) -> dict:
    base = {"state": "filled", "pair": "USDJPY", "side": "long", "entry_type": "market",
            "qty": "0.1", "price": "150.123", "sl": "149.8", "tp": None, "origin_code": "cron",
            "mode": "paper", "state_mode": None, "reason_code": None}
    base.update(fields)
    return {"id": event_id, "ts": TS, "code": "order_state_changed", "ref": "order:12",
            "fields": base}


def _close(event_id: int) -> dict:
    return {"id": event_id, "ts": TS, "code": "position_closed", "ref": "order:12",
            "fields": {"pair": "USDJPY", "side": "long", "qty": "0.1", "entry_price": "150.123",
                       "close_price": "150.9", "pnl": "7770.00", "ccy": "JPY", "pnl_r": "2.40",
                       "held_sec": 60, "reason_code": "tp", "origin_code": None, "mode": "paper",
                       "state_mode": None}}


@dataclass
class _Iso:
    state_dir: Path
    secrets: dict
    config: object = None
    feature: str = "notify"


@dataclass
class _Config:
    deliver_trade_events: bool = False


class _Client:
    def __init__(self, *pages) -> None:
        self.pages = list(pages)
        self.calls: list[int] = []

    def events(self, *, after: int = 0, limit: int = 100) -> Reply:
        self.calls.append(after)
        page = self.pages.pop(0) if self.pages else {"events": [], "high_watermark": after}
        return Reply(200, page, None)


@dataclass
class _Send:
    ids: list = field(default_factory=list)

    def __call__(self, target, body: bytes) -> None:
        import json
        self.ids.append(json.loads(body)["event_id"])


def _state_dir(tmp_path) -> Path:
    path = tmp_path / "state"
    path.mkdir(mode=0o700)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        save(fd, initialize_event_cursor(NotifyState.empty(), 0))
    finally:
        os.close(fd)
    return path


def _state(path: Path) -> NotifyState:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        return load(fd)
    finally:
        os.close(fd)


def _run(state_dir, client, send, *, config="absent", polls=1):
    seen = []

    def stop(_n: int) -> bool:
        seen.append(1)
        return len(seen) >= polls

    out = io.StringIO()
    iso = _Iso(state_dir, {"webhook.url": URL}, None if config == "absent" else config)
    rc = runtime.run(iso, client=client, send=send, sleep=lambda _s: None,
                     monotonic=lambda: 0.0, stop=stop, stderr=out)
    return rc, out.getvalue()


def _page(*events, high=None) -> dict:
    return {"events": list(events), "high_watermark": high or events[-1]["id"]}


@pytest.mark.parametrize("config", ["absent", _Config(), _Config(deliver_trade_events=False)])
def test_ac23_trade_events_are_skipped_and_the_cursor_advances_when_not_enabled(tmp_path, config):
    state_dir = _state_dir(tmp_path)
    client = _Client(_page(_order(1), _close(2)))
    send = _Send()

    assert _run(state_dir, client, send, config=config) == (0, "")

    assert send.ids == []
    assert _state(state_dir) == NotifyState(cursor=2, high_watermark=2,
                                            event_cursor_initialized=True)


def test_ac23_a_skipped_trade_event_does_not_block_the_events_after_it(tmp_path):
    state_dir = _state_dir(tmp_path)
    client = _Client(_page(_health(1), _order(2), _close(3), _health(4)))
    send = _Send()

    assert _run(state_dir, client, send, config=_Config())[0] == 0

    assert send.ids == [1, 4]
    assert _state(state_dir).cursor == 4


def test_ac23_trade_events_are_delivered_in_the_same_shape_when_enabled(tmp_path):
    state_dir = _state_dir(tmp_path)
    client = _Client(_page(_order(1), _health(2), _close(3)))
    send = _Send()

    assert _run(state_dir, client, send, config=_Config(deliver_trade_events=True))[0] == 0

    assert send.ids == [1, 2, 3]
    assert _state(state_dir).cursor == 3


def test_ac23_a_skipped_trade_event_raises_the_high_watermark_with_the_cursor(tmp_path):
    state_dir = _state_dir(tmp_path)
    client = _Client(_page(_order(1), high=9))

    assert _run(state_dir, client, _Send(), config=_Config())[0] == 0

    assert _state(state_dir) == NotifyState(cursor=1, high_watermark=9,
                                            event_cursor_initialized=True)


def test_ac23_a_full_page_of_skipped_events_is_followed_without_waiting(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "EVENTS_LIMIT", 2)
    state_dir = _state_dir(tmp_path)
    client = _Client(_page(_order(1), _order(2)), _page(_order(3)))
    sleeps: list[float] = []
    seen = []

    def stop(_n):
        seen.append(1)
        return len(seen) >= 2

    rc = runtime.run(_Iso(state_dir, {"webhook.url": URL}, _Config()), client=client,
                     send=_Send(), sleep=sleeps.append, monotonic=lambda: 0.0, stop=stop,
                     stderr=io.StringIO())

    assert rc == 0 and client.calls == [0, 2] and sleeps == []


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("bad", [
    _order(2, price="0001.2"),
    _order(2, qty=0.1),
    _order(2, origin_code="@everyone"),
    {**_order(2), "code": "order_state_cancelled"},
    {**_order(2), "ref": "order:0"},
])
def test_ac21_an_invalid_or_unknown_event_stops_without_moving_the_cursor(tmp_path, bad, enabled):
    state_dir = _state_dir(tmp_path)
    client = _Client(_page(_health(1), bad, _health(3)))
    send = _Send()

    rc, err = _run(state_dir, client, send, config=_Config(deliver_trade_events=enabled))

    assert (rc, err) == (1, "afx-ops: payload_invalid\n")
    assert send.ids == [1]
    assert _state(state_dir).cursor == 1


def test_ac23_existing_events_are_delivered_unchanged_without_the_setting(tmp_path):
    state_dir = _state_dir(tmp_path)
    client = _Client(_page(_health(1), _health(2)))
    send = _Send()

    assert _run(state_dir, client, send)[0] == 0
    assert send.ids == [1, 2]
