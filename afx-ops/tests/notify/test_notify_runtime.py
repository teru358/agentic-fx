"""notify の配送 loop (fake client・fake 送信・実 state dir)。

event の取得 → payload → 送信 → snapshot 確定の順序、重複・crash 窓・失敗時の cursor を
network なしで固定する。実 kernel・実 TLS の結合は test_notify_isolated が受け持つ。
"""
from __future__ import annotations

import io
import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from afx_ops.api_client import ApiError, Reply
from afx_ops.notify import runtime
from afx_ops.notify.delivery import DeliveryError
from afx_ops.notify.retry import MAX_ATTEMPTS, retry_delays
from afx_ops.notify.state import (NotifyState, advance_delivery, initialize_event_cursor, load,
                                  observe_high, save)

URL = b"https://hooks.example.test/afx/9f2c"


def _event(event_id: int, *, code: str = "service_health_changed", **override) -> dict:
    event = {"id": event_id, "ts": "2026-10-11T00:00:00+00:00", "code": code,
             "ref": "instance", "fields": {"component_code": "api", "health_code": "ok"}}
    event.update(override)
    return event


@dataclass
class _Iso:
    """runtime が使う Isolated の属性だけを持つ代役。"""
    state_dir: Path
    secrets: dict
    feature: str = "notify"


class FakeClient:
    """events の応答を順に返す。尽きたら空 page。"""

    def __init__(self, pages: list) -> None:
        self.pages = list(pages)
        self.calls: list[tuple[int, int]] = []

    def events(self, *, after: int = 0, limit: int = 100) -> Reply:
        self.calls.append((after, limit))
        page = self.pages.pop(0) if self.pages else None
        if isinstance(page, Exception):
            raise page
        if isinstance(page, Reply):
            return page
        if page is None:
            page = {"events": [], "high_watermark": after}
        return Reply(200, page, None)


class FakeSend:
    """webhook の代役。statuses の順に結果を返す (int は status、例外はそのまま投げる)。"""

    def __init__(self, results: list | None = None) -> None:
        self.results = list(results or [])
        self.bodies: list[dict] = []

    def __call__(self, target, body: bytes) -> None:
        self.bodies.append(json.loads(body))
        result = self.results.pop(0) if self.results else 200
        if isinstance(result, Exception):
            raise result
        if not 200 <= result < 300:
            from afx_ops.notify.delivery import classify_status
            raise classify_status(result)

    @property
    def ids(self) -> list[int]:
        return [body["event_id"] for body in self.bodies]


def _dir_fd(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)


def _state(path: Path) -> NotifyState:
    fd = _dir_fd(path)
    try:
        return load(fd)
    finally:
        os.close(fd)


def _seed(path: Path, *ids: int, high: int = 0) -> None:
    state = initialize_event_cursor(NotifyState.empty(), 0)
    for event_id in ids:
        state = advance_delivery(state, event_id)
    if high:
        state = observe_high(state, high)
    fd = _dir_fd(path)
    try:
        save(fd, state)
    finally:
        os.close(fd)


@pytest.fixture
def fresh_dir(tmp_path) -> Path:
    """snapshot がまだ無い state dir。"""
    path = tmp_path / "state"
    path.mkdir(mode=0o700)
    return path


@pytest.fixture
def state_dir(fresh_dir) -> Path:
    """event cursor の初期化が済んだ (cursor 0 から読む) state dir。"""
    fd = _dir_fd(fresh_dir)
    try:
        save(fd, initialize_event_cursor(NotifyState.empty(), 0))
    finally:
        os.close(fd)
    return fresh_dir


def _run(state_dir, client, send, *, url=URL, sleeps=None, polls=1, err=None):
    """polls 回の取得で止める。(rc, stderr)。"""
    seen = []

    def stop(_delivered: int) -> bool:
        seen.append(1)
        return len(seen) >= polls

    out = io.StringIO() if err is None else err
    rc = runtime.run(_Iso(state_dir, {"webhook.url": url}), client=client, send=send,
                     sleep=(sleeps.append if sleeps is not None else lambda _s: None),
                     monotonic=lambda: 0.0, stop=stop, stderr=out)
    return rc, out.getvalue()


def test_two_xx_delivers_each_event_once_and_commits_the_cursor(state_dir):
    client = FakeClient([{"events": [_event(1), _event(2)], "high_watermark": 2}])
    send = FakeSend([200, 204])
    rc, err = _run(state_dir, client, send)
    assert (rc, err) == (0, "")
    assert send.ids == [1, 2]
    assert _state(state_dir) == NotifyState(cursor=2, high_watermark=2, event_cursor_initialized=True)
    assert client.calls[0] == (0, runtime.EVENTS_LIMIT) and runtime.EVENTS_LIMIT == 500


def test_payload_is_the_allowlisted_projection_only(state_dir):
    client = FakeClient([{"events": [_event(
        5, code="approval_state_changed", ref="approval:3",
        fields={"state": "rejected", "decision_code": "reject", "decided_by": "api:op"})],
        "high_watermark": 5}])
    send = FakeSend()
    assert _run(state_dir, client, send)[0] == 0
    assert send.bodies == [{"event_id": 5, "ts": "2026-10-11T00:00:00+00:00",
                            "code": "approval_state_changed", "ref": "approval:3",
                            "state": "rejected", "decision_code": "reject"}]


def test_polling_resumes_after_the_saved_cursor(state_dir):
    _seed(state_dir, 3, 4)
    client = FakeClient([{"events": [_event(5)], "high_watermark": 5}])
    send = FakeSend()
    assert _run(state_dir, client, send)[0] == 0
    assert client.calls[0][0] == 4
    assert send.ids == [5]


def test_duplicate_fetch_is_delivered_only_once(state_dir):
    # 同じ event が page をまたいで、また page の中で重複して返っても送るのは一度
    client = FakeClient([
        {"events": [_event(1), _event(1), _event(2)], "high_watermark": 2},
        {"events": [_event(1), _event(2)], "high_watermark": 2},
    ])
    send = FakeSend()
    rc, _ = _run(state_dir, client, send, polls=2)
    assert rc == 0
    assert send.ids == [1, 2]
    assert _state(state_dir).cursor == 2


def test_already_delivered_ids_below_the_cursor_are_not_sent(state_dir):
    _seed(state_dir, 7)
    client = FakeClient([{"events": [_event(6), _event(7), _event(8)], "high_watermark": 8}])
    send = FakeSend()
    assert _run(state_dir, client, send)[0] == 0
    assert send.ids == [8]


def test_out_of_order_page_is_delivered_in_id_order(state_dir):
    client = FakeClient([{"events": [_event(3), _event(2)], "high_watermark": 3}])
    send = FakeSend()
    assert _run(state_dir, client, send)[0] == 0
    assert send.ids == [2, 3]


def test_crash_after_two_xx_before_commit_resends_the_same_id(state_dir, monkeypatch):
    client = FakeClient([{"events": [_event(1)], "high_watermark": 1}])
    send = FakeSend()

    class Crash(BaseException):
        pass

    def crash(_fd, _state):
        raise Crash()
    monkeypatch.setattr(runtime, "save", crash)
    with pytest.raises(Crash):
        _run(state_dir, client, send)
    monkeypatch.undo()
    assert _state(state_dir).cursor == 0
    # 再起動: 同じ cursor から読み直し、同じ event_id をもう一度送る (at-least-once)
    again = FakeClient([{"events": [_event(1)], "high_watermark": 1}])
    assert _run(state_dir, again, send)[0] == 0
    assert send.ids == [1, 1]
    assert _state(state_dir).cursor == 1


@pytest.mark.parametrize("status, reason, attempts", [
    (404, "http_4xx", 1),
    (400, "http_4xx", 1),
    (302, "redirect", 1),
    (308, "redirect", 1),
    (429, "retry_exhausted", MAX_ATTEMPTS),
    (503, "retry_exhausted", MAX_ATTEMPTS),
])
def test_non_two_xx_keeps_cursor_and_stops_before_the_next_event(state_dir, status, reason,
                                                                  attempts):
    _seed(state_dir, 1)
    client = FakeClient([{"events": [_event(2), _event(3)], "high_watermark": 3}])
    send = FakeSend([status] * MAX_ATTEMPTS)
    sleeps: list[float] = []
    rc, err = _run(state_dir, client, send, sleeps=sleeps)
    assert rc == runtime.RC_NOTIFY_FAILED != 0
    assert err.splitlines()[-1] == f"afx-ops: {reason}"
    assert send.ids == [2] * attempts
    assert _state(state_dir) == NotifyState(cursor=1, high_watermark=1, event_cursor_initialized=True)
    assert sleeps == list(retry_delays()[:attempts - 1])


@pytest.mark.parametrize("cause", ["dns_failed", "connect_failed", "tls_failed", "timeout",
                                   "connection_lost"])
def test_transport_failures_are_retried_to_the_limit(state_dir, cause):
    client = FakeClient([{"events": [_event(1)], "high_watermark": 1}])
    send = FakeSend([DeliveryError(cause, retryable=True)] * MAX_ATTEMPTS)
    rc, err = _run(state_dir, client, send)
    assert rc == runtime.RC_NOTIFY_FAILED
    assert len(send.ids) == MAX_ATTEMPTS
    # 同じ原因の warning は throttle で 1 行、最後に固定 reason
    assert err.splitlines() == [f"afx-ops: warning {cause}", "afx-ops: retry_exhausted"]
    assert _state(state_dir).cursor == 0


def test_transient_failure_then_success_commits(state_dir):
    client = FakeClient([{"events": [_event(1)], "high_watermark": 1}])
    send = FakeSend([DeliveryError("timeout", retryable=True), 500, 200])
    rc, err = _run(state_dir, client, send)
    assert rc == 0
    assert send.ids == [1, 1, 1]
    assert err.splitlines() == ["afx-ops: warning timeout", "afx-ops: warning http_5xx"]
    assert _state(state_dir).cursor == 1


def test_high_watermark_regression_stops_without_touching_the_snapshot(state_dir):
    _seed(state_dir, 4, 9)
    before = (state_dir / "notify.snapshot").read_bytes()
    client = FakeClient([{"events": [_event(8)], "high_watermark": 8}])
    send = FakeSend()
    rc, err = _run(state_dir, client, send)
    assert rc == runtime.RC_NOTIFY_FAILED
    assert err == "afx-ops: high_watermark_regressed\n"
    assert send.bodies == []
    assert (state_dir / "notify.snapshot").read_bytes() == before


def test_saved_high_watermark_catches_a_rewind_that_the_cursor_alone_misses(state_dir):
    # cursor は 3 だが、前回の起動では watermark 20 まで見えていた。API が 15 を返すのは巻き戻り
    _seed(state_dir, 3, high=20)
    before = (state_dir / "notify.snapshot").read_bytes()
    send = FakeSend()
    rc, err = _run(state_dir, FakeClient([{"events": [_event(4)], "high_watermark": 15}]), send)
    assert (rc, err) == (runtime.RC_NOTIFY_FAILED, "afx-ops: high_watermark_regressed\n")
    assert send.bodies == []
    assert (state_dir / "notify.snapshot").read_bytes() == before


@pytest.mark.parametrize("api_high", [20, 21])
def test_equal_or_larger_watermark_proceeds_and_is_saved(state_dir, api_high):
    _seed(state_dir, 3, high=20)
    send = FakeSend()
    rc, _ = _run(state_dir, FakeClient([{"events": [_event(4)], "high_watermark": api_high}]), send)
    assert rc == runtime.RC_OK and send.ids == [4]
    assert _state(state_dir).high_watermark == api_high


def test_a_larger_watermark_without_events_is_still_saved(state_dir):
    rc, _ = _run(state_dir, FakeClient([{"events": [], "high_watermark": 7}]), FakeSend())
    assert rc == runtime.RC_OK
    assert _state(state_dir).high_watermark == 7


@pytest.mark.parametrize("bad", [
    _event(2, code="approval_payload"),
    _event(2, fields={"component_code": "api", "health_code": "ok", "note": "x"}),
    _event(2, fields={"component_code": "api"}),
    _event(2, fields={"component_code": "api", "health_code": "free text with spaces"}),
    _event(2, ref="instance\nx"),
    {"id": "2", "ts": "t", "code": "service_health_changed", "ref": "instance", "fields": {}},
    {"id": 2},
    "event",
])
def test_payload_error_fails_closed_after_delivering_the_valid_prefix(state_dir, bad):
    client = FakeClient([{"events": [_event(1), bad, _event(3)], "high_watermark": 3}])
    send = FakeSend()
    rc, err = _run(state_dir, client, send)
    assert rc == runtime.RC_NOTIFY_FAILED
    assert err == "afx-ops: payload_invalid\n"
    assert send.ids == [1]
    assert _state(state_dir).cursor == 1


@pytest.mark.parametrize("reply", [
    Reply(200, None, "internal"),
    Reply(403, None, "forbidden"),
    Reply(200, {"events": "x", "high_watermark": 1}, None),
    Reply(200, {"events": [], "high_watermark": "1"}, None),
    Reply(200, {"events": [], "high_watermark": -1}, None),
    Reply(200, {"events": []}, None),
])
def test_invalid_events_reply_stops_with_a_fixed_reason(state_dir, reply):
    send = FakeSend()
    rc, err = _run(state_dir, FakeClient([reply]), send)
    assert rc == runtime.RC_NOTIFY_FAILED
    assert err == "afx-ops: events_invalid\n"
    assert send.bodies == []


@pytest.mark.parametrize("where", ["events", "send", "state"])
def test_unexpected_exception_is_one_fixed_line_without_traceback(state_dir, monkeypatch, where):
    leak = "https://hooks.example.test/afx/9f2c secret-token"
    client = FakeClient([{"events": [_event(1)], "high_watermark": 1}])
    send = FakeSend()
    if where == "events":
        client = FakeClient([RuntimeError(leak)])
    elif where == "send":
        send = FakeSend([RuntimeError(leak)])
    else:
        def broken(_fd, _state):
            raise ValueError(leak)
        monkeypatch.setattr(runtime, "save", broken)
    rc, err = _run(state_dir, client, send)
    assert rc == runtime.RC_NOTIFY_FAILED
    assert err == "afx-ops: notify_internal_error\n"
    assert _state(state_dir).cursor == 0


def test_api_failure_is_unreachable_and_not_retried_in_process(state_dir):
    client = FakeClient([ApiError("no_socket")])
    rc, err = _run(state_dir, client, FakeSend(), polls=5)
    assert rc == runtime.RC_UNREACHABLE == 3
    assert err == "afx-ops: no_socket\n"
    assert len(client.calls) == 1


@pytest.mark.parametrize("url", [b"http://hooks.example.test/x", b"https://h:8443/x",
                                 b"https://u:p@h/x", b"\xff", b"https:///x"])
def test_invalid_url_stops_before_reading_events(state_dir, url):
    client = FakeClient([])
    rc, err = _run(state_dir, client, FakeSend(), url=url)
    assert rc == runtime.RC_NOTIFY_FAILED
    assert err == "afx-ops: url_invalid\n"
    assert client.calls == []


def test_corrupt_snapshot_stops_without_sending(state_dir):
    (state_dir / "notify.snapshot").write_text("{", encoding="ascii")
    client = FakeClient([{"events": [_event(1)], "high_watermark": 1}])
    send = FakeSend()
    rc, err = _run(state_dir, client, send)
    assert rc == runtime.RC_NOTIFY_FAILED
    assert err == "afx-ops: notify_state_invalid\n"
    assert send.bodies == [] and client.calls == []


def test_idle_polls_sleep_the_fixed_interval_and_full_pages_do_not(state_dir):
    full = {"events": [_event(i) for i in range(1, runtime.EVENTS_LIMIT + 1)],
            "high_watermark": runtime.EVENTS_LIMIT + 1}
    client = FakeClient([full, {"events": [_event(runtime.EVENTS_LIMIT + 1)],
                                "high_watermark": runtime.EVENTS_LIMIT + 1}])
    sleeps: list[float] = []
    rc, _ = _run(state_dir, client, FakeSend(), sleeps=sleeps, polls=3)
    assert rc == 0
    # 満杯の page の直後は待たずに次を読み、満たない page の後だけ固定間隔で待つ
    assert sleeps == [runtime.POLL_INTERVAL]
    assert client.calls[1][0] == runtime.EVENTS_LIMIT


def test_secret_url_never_reaches_stderr(state_dir):
    client = FakeClient([{"events": [_event(1)], "high_watermark": 1}])
    send = FakeSend([DeliveryError("tls_failed", retryable=True)] * MAX_ATTEMPTS)
    _rc, err = _run(state_dir, client, send)
    assert b"hooks.example.test" not in err.encode() and "9f2c" not in err


def test_runtime_has_no_extra_state_or_locking():
    source = Path(runtime.__file__).read_text(encoding="utf-8")
    for word in ("sqlite", "flock", "F_SETLK", "lockf", "fcntl"):
        assert word not in source


def test_a_short_page_is_followed_by_the_fixed_wait(state_dir):
    client = FakeClient([{"events": [_event(1)], "high_watermark": 1},
                         {"events": [], "high_watermark": 1}])
    sleeps: list[float] = []
    rc, _ = _run(state_dir, client, FakeSend(), sleeps=sleeps, polls=2)
    assert rc == 0
    assert sleeps == [runtime.POLL_INTERVAL]


def test_a_full_page_is_followed_by_an_immediate_poll(state_dir):
    full = {"events": [_event(i) for i in range(1, runtime.EVENTS_LIMIT + 1)],
            "high_watermark": runtime.EVENTS_LIMIT}
    client = FakeClient([full, {"events": [], "high_watermark": runtime.EVENTS_LIMIT}])
    sleeps: list[float] = []
    rc, _ = _run(state_dir, client, FakeSend(), sleeps=sleeps, polls=2)
    assert rc == 0
    assert sleeps == []
    assert client.calls[1][0] == runtime.EVENTS_LIMIT


def test_a_full_page_that_makes_no_progress_is_followed_by_the_fixed_wait(state_dir):
    # API が after を無視して cursor 以下だけの満杯 page を返し続けても、tight loop にならない
    limit = runtime.EVENTS_LIMIT
    _seed(state_dir, *range(1, limit + 1))
    full = {"events": [_event(i) for i in range(1, limit + 1)], "high_watermark": limit}
    client = FakeClient([full, full])
    sleeps: list[float] = []
    send = FakeSend()
    rc, _ = _run(state_dir, client, send, sleeps=sleeps, polls=2)
    assert rc == 0
    assert send.bodies == []
    assert sleeps == [runtime.POLL_INTERVAL]


def test_a_429_with_retry_after_waits_that_long_before_resending(state_dir):
    client = FakeClient([{"events": [_event(1)], "high_watermark": 1}])
    send = FakeSend([DeliveryError("http_429", retryable=True, retry_after=30.0), 200])
    sleeps: list[float] = []
    rc, err = _run(state_dir, client, send, sleeps=sleeps)
    assert rc == 0
    assert err == "afx-ops: warning http_429\n"
    assert sleeps[0] == 30.0
    assert _state(state_dir).cursor == 1


# ---------------------------------------------------------------- 初回 cursor (AC-25)


def _raw_snapshot(path: Path) -> dict:
    return json.loads((path / "notify.snapshot").read_text(encoding="ascii"))


def _history() -> list[dict]:
    approve = _event(1, code="approval_state_changed", ref="approval:3", fields={
        "state": "approved", "decision_code": "approve", "decided_by": "operator"})
    improve = _event(2, code="job_state_changed", ref="job:" + "0123456789abcdef" * 2, fields={
        "job_kind": "improve", "state": "done", "result_code": "ok"})
    trade = _event(4, code="order_state_changed", ref="order:1", fields={"state": "filled"})
    return [approve, improve, _event(3), trade, _event(5)]


def test_ac25_a_mixed_history_without_a_snapshot_posts_nothing_and_starts_at_the_end(fresh_dir):
    client = FakeClient([{"events": _history(), "high_watermark": 5}])
    send = FakeSend()
    rc, err = _run(fresh_dir, client, send)
    assert (rc, err) == (0, "")
    assert send.bodies == []
    assert _state(fresh_dir) == NotifyState(cursor=5, high_watermark=5,
                                            event_cursor_initialized=True)
    assert _raw_snapshot(fresh_dir)["event_cursor_initialized"] is True


def test_ac25_an_event_after_the_start_is_delivered(fresh_dir):
    client = FakeClient([{"events": _history(), "high_watermark": 5},
                         {"events": [_event(6)], "high_watermark": 6}])
    send = FakeSend()
    rc, _ = _run(fresh_dir, client, send, polls=2)
    assert rc == 0 and send.ids == [6]
    assert [after for after, _ in client.calls] == [0, 5]
    assert _state(fresh_dir).cursor == 6


def test_ac25_an_empty_history_still_starts_and_a_saved_zero_is_not_absence(fresh_dir):
    client = FakeClient([{"events": [], "high_watermark": 0},
                         {"events": [_event(1)], "high_watermark": 1}])
    send = FakeSend()
    rc, _ = _run(fresh_dir, client, send, polls=2)
    assert rc == 0 and send.ids == [1]
    assert _state(fresh_dir).cursor == 1


def test_ac25_a_failed_first_poll_keeps_the_flag_false_and_the_next_start_skips_history(fresh_dir):
    fd = _dir_fd(fresh_dir)
    try:
        save(fd, NotifyState.empty())
    finally:
        os.close(fd)
    rc, err = _run(fresh_dir, FakeClient([ApiError("unreachable")]), FakeSend())
    assert rc != 0
    assert _raw_snapshot(fresh_dir)["event_cursor_initialized"] is False
    send = FakeSend()
    rc, _ = _run(fresh_dir, FakeClient([{"events": _history(), "high_watermark": 5}]), send)
    assert rc == 0 and send.bodies == []
    assert _state(fresh_dir).event_cursor_initialized is True


def test_ac25_a_malformed_first_page_fails_without_starting(fresh_dir):
    rc, err = _run(fresh_dir, FakeClient([Reply(200, {"events": "x", "high_watermark": 1}, None)]),
                   FakeSend())
    assert (rc, err) == (1, "afx-ops: events_invalid\n")
    assert not (fresh_dir / "notify.snapshot").exists()


def test_ac25_the_start_is_one_snapshot_replacement(fresh_dir, monkeypatch):
    writes = []
    real = runtime.save

    def spy(dir_fd, state):
        real(dir_fd, state)
        writes.append((state.cursor, state.high_watermark, state.event_cursor_initialized))

    monkeypatch.setattr(runtime, "save", spy)
    _run(fresh_dir, FakeClient([{"events": _history(), "high_watermark": 5}]), FakeSend())
    assert writes == [(5, 5, True)]


def test_ac25_a_v2_snapshot_migrates_to_true_and_continues_from_its_cursor(fresh_dir):
    (fresh_dir / "notify.snapshot").write_text(
        '{"cursor": 3, "high_watermark": 3, "version": 2}', encoding="ascii")
    client = FakeClient([{"events": [_event(4), _event(5)], "high_watermark": 5}])
    send = FakeSend()
    rc, _ = _run(fresh_dir, client, send)
    assert rc == 0 and send.ids == [4, 5]
    assert client.calls[0][0] == 3
    assert _raw_snapshot(fresh_dir) == {"version": 3, "cursor": 5, "high_watermark": 5,
                                        "event_cursor_initialized": True}


def test_a_first_page_whose_watermark_is_below_its_event_ids_starts_at_the_largest_id(fresh_dir):
    send = FakeSend()
    client = FakeClient([{"events": [_event(100)], "high_watermark": 50},
                         {"events": [_event(100)], "high_watermark": 100}])
    rc, err = _run(fresh_dir, client, send, polls=2)
    assert (rc, err) == (0, "")
    assert send.bodies == []
    assert client.calls[1][0] == 100
    assert _state(fresh_dir) == NotifyState(cursor=100, high_watermark=100,
                                            event_cursor_initialized=True)


def test_an_initialized_page_whose_watermark_is_below_its_event_ids_delivers_and_advances(state_dir):
    send = FakeSend()
    rc, err = _run(state_dir, FakeClient([{"events": [_event(200)], "high_watermark": 150}]), send)
    assert (rc, err) == (0, "")
    assert send.ids == [200]
    assert _state(state_dir) == NotifyState(cursor=200, high_watermark=200,
                                            event_cursor_initialized=True)


_BAD_PAGES = {
    "no watermark": {"events": []},
    "negative": {"events": [], "high_watermark": -1},
    "boolean": {"events": [], "high_watermark": True},
}


@pytest.mark.parametrize("name", list(_BAD_PAGES))
def test_a_malformed_first_page_saves_and_delivers_nothing(fresh_dir, name):
    send = FakeSend()
    rc, err = _run(fresh_dir, FakeClient([_BAD_PAGES[name]]), send)
    assert (rc, err) == (1, "afx-ops: events_invalid\n")
    assert send.bodies == [] and not (fresh_dir / "notify.snapshot").exists()


@pytest.mark.parametrize("name", list(_BAD_PAGES))
def test_a_malformed_page_after_the_start_changes_nothing(state_dir, name):
    before = _state(state_dir)
    send = FakeSend()
    rc, err = _run(state_dir, FakeClient([_BAD_PAGES[name]]), send)
    assert (rc, err) == (1, "afx-ops: events_invalid\n")
    assert send.bodies == [] and _state(state_dir) == before


def test_the_regression_check_runs_before_the_uninitialised_branch(fresh_dir):
    state = NotifyState.empty()
    object.__setattr__(state, "cursor", 100)
    object.__setattr__(state, "high_watermark", 100)
    loop = runtime._Loop(_Iso(fresh_dir, {}), client=None, send=FakeSend(), sleep=lambda _s: None,
                         monotonic=lambda: 0.0, out=io.StringIO())
    fd = _dir_fd(fresh_dir)
    try:
        with pytest.raises(runtime._Failure, match="high_watermark_regressed"):
            loop._poll_once(FakeClient([{"events": [], "high_watermark": 50}]), None, fd, state)
    finally:
        os.close(fd)
    assert not (fresh_dir / "notify.snapshot").exists()
