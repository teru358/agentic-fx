"""notify の改善 event の配送方針。

`deliver_improve_events` が false (既定) のとき、検証を通った改善 event は配送せずに cursor と
high watermark を進める。true のときは固定 payload のまま配送する。notify は詳細を取得しない。
"""
from __future__ import annotations

import io
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from afx_ops.api_client import Reply
from afx_ops.notify import runtime
from afx_ops.notify.state import NotifyState, initialize_event_cursor, load, save

URL = b"https://hooks.example.test/afx/9f2c"
TS = "2026-10-25T00:00:00+00:00"


def _health(event_id: int) -> dict:
    return {"id": event_id, "ts": TS, "code": "service_health_changed", "ref": "instance",
            "fields": {"component_code": "api", "health_code": "ok"}}


def _improve(event_id: int, **fields) -> dict:
    base = {"state": "finished", "trigger": "manual", "result_code": "observation",
            "reason_code": None, "tool_calls": 11}
    base.update(fields)
    return {"id": event_id, "ts": TS, "code": "improve_run_changed", "ref": "improve:2040",
            "fields": base}


@dataclass
class _Iso:
    state_dir: Path
    secrets: dict
    config: object = None
    feature: str = "notify"


@dataclass
class _Config:
    deliver_improve_events: bool = False


class _Client:
    def __init__(self, *pages) -> None:
        self.pages = list(pages)
        self.calls: list[str] = []

    def events(self, *, after: int = 0, limit: int = 100) -> Reply:
        self.calls.append("events")
        page = self.pages.pop(0) if self.pages else {"events": [], "high_watermark": after}
        return Reply(200, page, None)

    def __getattr__(self, name):
        def record(*_a, **_k):
            self.calls.append(name)
            raise AssertionError(name)
        return record


@dataclass
class _Send:
    bodies: list = field(default_factory=list)

    def __call__(self, target, body: bytes) -> None:
        self.bodies.append(json.loads(body))


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


def _run(state_dir, client, send, *, config="absent"):
    out = io.StringIO()
    iso = _Iso(state_dir, {"webhook.url": URL}, None if config == "absent" else config)
    rc = runtime.run(iso, client=client, send=send, sleep=lambda _s: None,
                     monotonic=lambda: 0.0, stop=lambda _n: True, stderr=out)
    return rc, out.getvalue()


def _page(*events, high=None) -> dict:
    return {"events": list(events), "high_watermark": high or events[-1]["id"]}


@pytest.mark.parametrize("config", ["absent", _Config(), _Config(deliver_improve_events=False)])
def test_improve_events_are_skipped_and_the_cursor_advances_by_default(tmp_path, config):
    state_dir = _state_dir(tmp_path)
    send = _Send()

    assert _run(state_dir, _Client(_page(_improve(1), _improve(2, state="started"))), send,
                config=config) == (0, "")

    assert send.bodies == []
    assert _state(state_dir) == NotifyState(cursor=2, high_watermark=2,
                                            event_cursor_initialized=True)


def test_a_skipped_improve_event_does_not_block_the_events_after_it(tmp_path):
    state_dir = _state_dir(tmp_path)
    send = _Send()

    assert _run(state_dir, _Client(_page(_health(1), _improve(2), _health(3))), send,
                config=_Config())[0] == 0

    assert [body["event_id"] for body in send.bodies] == [1, 3]
    assert _state(state_dir).cursor == 3


def test_improve_events_are_delivered_as_the_fixed_payload_when_enabled(tmp_path):
    state_dir = _state_dir(tmp_path)
    send = _Send()
    client = _Client(_page(_improve(1, result_code="gate_failed", reason_code="unprofitable",
                                    tool_calls=10), _health(2)))

    assert _run(state_dir, client, send, config=_Config(deliver_improve_events=True))[0] == 0

    assert send.bodies[0] == {"event_id": 1, "ts": TS, "code": "improve_run_changed",
                              "ref": "improve:2040", "state": "finished", "trigger": "manual",
                              "result_code": "gate_failed", "reason_code": "unprofitable",
                              "tool_calls": 10}
    assert [body["event_id"] for body in send.bodies] == [1, 2]
    # notify は events しか呼ばない (詳細の取得をしない)
    assert client.calls == ["events"]
    assert _state(state_dir).cursor == 2


def test_the_trade_setting_does_not_enable_improve_delivery(tmp_path):
    from dataclasses import make_dataclass
    config = make_dataclass("C", [("deliver_trade_events", bool, True)])()
    state_dir = _state_dir(tmp_path)
    send = _Send()

    assert _run(state_dir, _Client(_page(_improve(1))), send, config=config)[0] == 0

    assert send.bodies == [] and _state(state_dir).cursor == 1


def test_an_unknown_reason_word_does_not_stop_delivery_and_the_cursor_advances(tmp_path):
    state_dir = _state_dir(tmp_path)
    send = _Send()

    assert _run(state_dir, _Client(_page(_improve(1, reason_code="zzz_new_word"), _health(2))),
                send, config=_Config(deliver_improve_events=True))[0] == 0

    assert send.bodies[0]["reason_code"] == "zzz_new_word"
    assert _state(state_dir).cursor == 2


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("bad", [
    _improve(2, reason_code="pf:7.7777"),
    _improve(2, tool_calls=True),
    _improve(2, state="done"),
    {**_improve(2), "ref": "order:1"},
])
def test_an_invalid_improve_event_stops_without_moving_the_cursor(tmp_path, bad, enabled):
    state_dir = _state_dir(tmp_path)
    send = _Send()

    rc, err = _run(state_dir, _Client(_page(_health(1), bad, _health(3))), send,
                   config=_Config(deliver_improve_events=enabled))

    assert (rc, err) == (1, "afx-ops: payload_invalid\n")
    assert [body["event_id"] for body in send.bodies] == [1]
    assert _state(state_dir).cursor == 1
