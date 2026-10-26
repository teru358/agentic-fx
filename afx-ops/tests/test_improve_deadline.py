"""改善 mission の詳細取得は、接続・admission・送信・受信の合計を 1 つの deadline で打ち切る。

既存の `timeout=` 呼び出し (承認の plugin 名補完) は、接続と受信それぞれの期限のまま変えない。
段階ごとの時間は fake の要求で再現する (相手を実際に遅くするのでなく、時計と各段の所要を操作する)。
"""
from __future__ import annotations

import json
import os
import socket
import time
from types import SimpleNamespace

import pytest

from afx_ops import api_client
from afx_ops.api_client import ApiError, BotClient, Endpoint, Reply

BODY = json.dumps({"ok": True, "data": {"id": 7}}).encode()


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


class _FakeRequest:
    """各段の所要 (秒) を時計に足し、渡された期限を記録する。期限を超える段は失敗にする。"""

    def __init__(self, clock: _Clock, **cost) -> None:
        self.clock = clock
        self.cost = {"open": 0.0, "send": 0.0, "receive": 0.0, **cost}
        self.open_timeout = None
        self.limit_values: list[float] = []
        self.receive_timeout = None
        self.sent = False

    def install(self, monkeypatch) -> "_FakeRequest":
        fake = self

        def open_(endpoint, *, timeout=api_client.HTTP_TIMEOUT):
            fake.open_timeout = timeout
            if fake.cost["open"] > timeout:
                fake.clock.now += timeout
                raise ApiError("timeout")
            fake.clock.now += fake.cost["open"]
            return fake

        monkeypatch.setattr(api_client._Request, "open", staticmethod(open_))
        monkeypatch.setattr(api_client, "time", SimpleNamespace(
            monotonic=self.clock.monotonic, sleep=time.sleep))
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def abort(self) -> None:
        pass

    def limit(self, seconds: float) -> None:
        self.limit_values.append(seconds)

    def send(self, payload: bytes) -> None:
        budget = self.limit_values[-1] if self.limit_values else api_client.HTTP_TIMEOUT
        self.sent = True
        if self.cost["send"] > budget:
            self.clock.now += budget
            raise ApiError("response_lost", token_sent=True)
        self.clock.now += self.cost["send"]

    def receive(self, *, timeout=api_client.HTTP_TIMEOUT):
        self.receive_timeout = timeout
        if self.cost["receive"] > timeout:
            self.clock.now += timeout
            raise ApiError("response_lost", token_sent=True)
        self.clock.now += self.cost["receive"]
        return 200, BODY, None


def _client(tmp_path) -> BotClient:
    return BotClient(Endpoint(tmp_path / "afx.sock", tmp_path / "instance.lock"), "token")


def test_every_phase_is_given_only_the_time_that_is_left(monkeypatch, tmp_path):
    clock = _Clock()
    fake = _FakeRequest(clock, open=1.0, send=0.5, receive=0.5).install(monkeypatch)

    reply = _client(tmp_path).improve_run(7, deadline=clock.now + 3.0)

    assert reply == Reply(200, {"id": 7}, None)
    assert fake.open_timeout == pytest.approx(3.0)
    assert fake.limit_values == [pytest.approx(2.0)]
    assert fake.receive_timeout == pytest.approx(1.5)
    assert clock.now == pytest.approx(1000.0 + 2.0)


def test_a_slow_admission_that_uses_up_the_deadline_stops_before_the_key_is_sent(
        monkeypatch, tmp_path):
    clock = _Clock()
    fake = _FakeRequest(clock).install(monkeypatch)

    # open が (timeout の外にある admission の遅さで) deadline を超えて戻った状況
    def late_open(endpoint, *, timeout=api_client.HTTP_TIMEOUT):
        fake.open_timeout = timeout
        clock.now += 3.5
        return fake

    monkeypatch.setattr(api_client._Request, "open", staticmethod(late_open))

    with pytest.raises(ApiError) as info:
        _client(tmp_path).improve_run(7, deadline=clock.now + 3.0)

    assert info.value.reason == "timeout" and not info.value.token_sent
    assert fake.sent is False


def test_a_slow_connect_is_cut_at_the_deadline(monkeypatch, tmp_path):
    clock = _Clock()
    fake = _FakeRequest(clock, open=9.0).install(monkeypatch)

    with pytest.raises(ApiError):
        _client(tmp_path).improve_run(7, deadline=clock.now + 3.0)

    assert fake.open_timeout == pytest.approx(3.0) and clock.now == pytest.approx(1003.0)


def test_a_slow_send_leaves_the_receive_only_what_is_left(monkeypatch, tmp_path):
    clock = _Clock()
    fake = _FakeRequest(clock, open=0.5, send=2.0, receive=1.0).install(monkeypatch)

    with pytest.raises(ApiError) as info:
        _client(tmp_path).improve_run(7, deadline=clock.now + 3.0)

    assert info.value.reason == "response_lost"
    assert fake.receive_timeout == pytest.approx(0.5)
    assert clock.now <= 1000.0 + 3.0 + 1e-9


def test_a_slow_response_is_cut_and_the_total_never_exceeds_the_deadline(monkeypatch, tmp_path):
    clock = _Clock()
    fake = _FakeRequest(clock, open=1.0, send=1.0, receive=5.0).install(monkeypatch)

    with pytest.raises(ApiError) as info:
        _client(tmp_path).improve_run(7, deadline=clock.now + 3.0)

    assert info.value.reason == "response_lost"
    assert fake.receive_timeout == pytest.approx(1.0)
    assert clock.now == pytest.approx(1003.0)


def test_a_deadline_that_has_already_passed_does_not_open_a_connection(monkeypatch, tmp_path):
    clock = _Clock()
    fake = _FakeRequest(clock).install(monkeypatch)

    with pytest.raises(ApiError) as info:
        _client(tmp_path).improve_run(7, deadline=clock.now - 0.01)

    assert info.value.reason == "timeout"
    assert fake.open_timeout is None and fake.sent is False


def test_the_wall_clock_total_stays_inside_a_short_deadline(monkeypatch, tmp_path):
    """実時間の sleep で、各段を足すと deadline を超える状況を作る (0.5 秒以内)。"""
    seen = {}

    class _Slow:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def abort(self):
            pass

        def limit(self, seconds):
            seen["limit"] = seconds

        def send(self, payload):
            time.sleep(0.1)

        def receive(self, *, timeout):
            seen["receive"] = timeout
            time.sleep(timeout)
            raise ApiError("response_lost", token_sent=True)

    def slow_open(endpoint, *, timeout):
        seen["open"] = timeout
        time.sleep(0.15)
        return _Slow()

    monkeypatch.setattr(api_client._Request, "open", staticmethod(slow_open))
    began = time.monotonic()

    with pytest.raises(ApiError):
        _client(tmp_path).improve_run(7, deadline=began + 0.4)

    assert time.monotonic() - began < 0.5
    assert seen["receive"] < 0.2 and seen["limit"] < 0.3


def test_the_name_lookup_timeout_keeps_per_phase_limits(monkeypatch, tmp_path):
    clock = _Clock()
    fake = _FakeRequest(clock, open=1.0, send=0.0, receive=1.0).install(monkeypatch)

    _client(tmp_path).approval(7, timeout=3.0)

    assert fake.open_timeout == 3.0 and fake.receive_timeout == 3.0
    assert fake.limit_values == []


def test_the_default_request_keeps_the_fifteen_second_limits(monkeypatch, tmp_path):
    clock = _Clock()
    fake = _FakeRequest(clock).install(monkeypatch)

    _client(tmp_path).approval(7)

    assert fake.open_timeout == api_client.HTTP_TIMEOUT
    assert fake.receive_timeout == api_client.HTTP_TIMEOUT


def test_improve_run_asks_for_the_mission_path_with_the_deadline(monkeypatch, tmp_path):
    client = _client(tmp_path)
    seen = []
    monkeypatch.setattr(client, "_get", lambda path, **kw: seen.append((path, kw)))

    client.improve_run(2043, deadline=5.0)

    assert seen == [("/v1/improve/runs/2043", {"deadline": 5.0})]
    for bad in (-1, True, "7", None):
        with pytest.raises(ValueError):
            client.improve_run(bad, deadline=5.0)


def test_the_request_object_applies_the_remaining_time_to_a_blocked_send(tmp_path):
    """実 socket: 受け側が読まず buffer が詰まった送信が、limit の時間で止まる。"""
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        pidfd = left.getsockopt(socket.SOL_SOCKET, 77)  # SO_PEERPIDFD
    except OSError:
        left.close()
        right.close()
        pytest.skip("SO_PEERPIDFD の無い kernel (< 6.5)")
    try:
        request = api_client._Request(Endpoint(tmp_path / "s", tmp_path / "l"))
        request._sock = left
        request._pidfd = pidfd
        request.limit(0.2)
        began = time.monotonic()
        with pytest.raises(ApiError) as info:
            request.send(b"x" * (64 * 1024 * 1024))
        assert time.monotonic() - began < 1.0
        assert info.value.reason == "response_lost" and info.value.token_sent
    finally:
        os.close(pidfd)
        left.close()
        right.close()
