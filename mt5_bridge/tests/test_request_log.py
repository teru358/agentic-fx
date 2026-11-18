"""bridge の要求ごとの構造化記録 (1 要求 1 行の JSON) のテスト。

記録は有界 queue と daemon thread の writer が、UTC 日付入りの file へ追記する。
要求の処理は queue への put_nowait だけで待たず、記録の失敗は応答に影響させない。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import re
import stat
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import bridge_metrics
import request_log
import server

_BRIDGE_DIR = Path(__file__).resolve().parent.parent
_API_KEY = "k" * 24 + "7f3a"


def _call(method, path, headers=None, query="", client=("127.0.0.1", 1)):
    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": query.encode(), "headers": hdrs, "client": client,
        "server": ("127.0.0.1", 8812), "root_path": "",
    }
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    asyncio.run(server.app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], [(k.decode(), v.decode()) for k, v in start["headers"]], body


@dataclass
class _Quote:
    bid: float
    ask: float


class _Client:
    is_connected = True

    def __init__(self, error=None):
        self._error = error

    def get_quote(self, symbol):
        if self._error is not None:
            raise self._error
        return _Quote(bid=1.2345, ask=1.2346)

    def server_time_status(self):
        return {}

    def time_rule_status(self):
        return ("ok", None)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(server, "_settings", SimpleNamespace(
        auth_required=True, api_key=_API_KEY, mt5_server="s", mt5_login=1))
    monkeypatch.setattr(server, "_client", None)
    monkeypatch.setattr(server, "_runtime", SimpleNamespace(
        dry_run=True, soft_halted=False, is_hard_halted=False, accepts_new_orders=True))
    monkeypatch.setattr(bridge_metrics, "history_slot", bridge_metrics.HistorySlot())


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@pytest.fixture
def log(tmp_path):
    path = tmp_path / "logs" / "bridge-metrics"
    path.parent.mkdir()
    sink = request_log.RequestLog()
    request_log.request_log = sink
    sink.start(path)
    yield sink, path
    sink.stop()


def _text(path):
    return "".join(item.read_text() for item in sorted(path.glob("*.jsonl")))


def _rows(sink, path):
    sink.flush()
    return [json.loads(line) for line in _text(path).splitlines()]


_ALL_KEYS = {
    "ts", "caller", "route", "status", "error_code", "lock_state", "wait_ms", "hold_ms",
    "wait_max_ms", "hold_max_ms", "acquisitions", "history_state",
}


def test_each_request_writes_one_json_line_with_the_fixed_fields(configured, log):
    sink, path = log
    status, _, _ = _call("GET", "/health", {"X-Afx-Caller": "tick"})
    assert status == 200
    (row,) = _rows(sink, path)
    assert set(row) == _ALL_KEYS
    assert row["caller"] == "tick" and row["route"] == "/health" and row["status"] == 200
    assert row["lock_state"] in {"measured", "not_locked", "unavailable"}
    assert row["error_code"] is None
    assert datetime.strptime(row["ts"], "%Y-%m-%dT%H:%M:%S.%fZ")
    assert _text(path).count("\n") == 1
    assert [item.name for item in path.iterdir()] == [f"{_today()}.jsonl"]


@pytest.mark.parametrize("value,expected", [
    ("tick", "tick"), ("mission", "mission"), ("cli", "cli"),
    ("TICK", "unknown"), ("daemon", "unknown"), ("", "unknown"),
    ("tick, mission", "unknown"), (_API_KEY, "unknown"), ("x" * 5000, "unknown"),
])
def test_caller_is_matched_against_the_fixed_set(configured, log, value, expected):
    sink, path = log
    _call("GET", "/health", {"X-Afx-Caller": value})
    (row,) = _rows(sink, path)
    assert row["caller"] == expected
    assert value == "" or value not in json.dumps(row) or value == expected


def test_missing_caller_header_is_unknown(configured, log):
    sink, path = log
    _call("GET", "/health")
    assert _rows(sink, path)[0]["caller"] == "unknown"


@pytest.mark.parametrize("path_in,route", [
    ("/health", "/health"), ("/quote/EURUSD", "/quote/{symbol}"),
    ("/ohlcv/JP225.cash", "/ohlcv/{symbol}"), ("/positions", "/positions"),
    ("/positions/12345/modify", "/positions/{ticket}/modify"),
    ("/positions/12345/closed-deal", "/positions/{ticket}/closed-deal"),
    ("/admin/status", "/admin/status"), ("/server-time", "/server-time"),
    ("/secret-looking-path-9f2c", "/other"), ("/positions/1/2/3", "/other"), ("/", "/other"),
])
def test_route_is_a_fixed_shape_and_raw_path_is_never_written(configured, log, path_in, route):
    sink, path = log
    _call("GET", path_in, {"X-Bridge-Api-Key": _API_KEY}, query="token=abc123&x=1")
    (row,) = _rows(sink, path)
    assert row["route"] == route
    text = _text(path)
    for forbidden in ("EURUSD", "12345", "secret-looking", "token=", "abc123", _API_KEY):
        assert forbidden not in text


def test_error_code_is_recorded_from_the_fixed_set_only(configured, log):
    sink, path = log
    _call("GET", "/no-such-route", {"X-Bridge-Api-Key": _API_KEY})
    _call("GET", "/account")
    rows = _rows(sink, path)
    assert rows[0]["error_code"] == "route_not_found" and rows[0]["status"] == 404
    assert rows[1]["error_code"] == "auth_invalid" and rows[1]["status"] == 401
    assert rows[1]["history_state"] is None


def test_build_row_maps_a_code_outside_the_set_to_unknown():
    row = request_log.build_row(
        caller_raw=b"cli", path="/health", status=500, error_code="12345678",
        headers=[(bridge_metrics.H_LOCK_STATE, "unavailable")])
    assert row["error_code"] == "unknown"
    row = request_log.build_row(
        caller_raw=None, path="/health", status=500, error_code=_API_KEY,
        headers=[(bridge_metrics.H_LOCK_STATE, "unavailable")])
    assert row["error_code"] == "unknown" and _API_KEY not in json.dumps(row)
    row = request_log.build_row(
        caller_raw=None, path="/health", status=200, error_code=None, headers=[])
    assert row["error_code"] is None and row["lock_state"] == "unavailable"


def test_every_error_code_the_bridge_can_emit_is_in_the_fixed_set():
    found: set[str] = set()
    for name in ("server.py", "mt5_client.py"):
        text = (_BRIDGE_DIR / name).read_text()
        found |= set(re.findall(r'_hdr\("([a-z_]+)"\)', text))
        found |= set(re.findall(r'error_code="([a-z_]+)"', text))
        found |= set(re.findall(r'_code\(e, "([a-z_]+)"\)', text))
        if name == "server.py":
            found |= set(re.findall(r'return "([a-z_]+)"', text))
        found |= set(re.findall(r'error_code="([a-z_]+)", emit', text))
    found |= {f"time_rule_{state}" for state in ("mismatch", "unverified")}
    found -= {"ok"}
    assert found, "no codes found"
    assert found <= bridge_metrics.ERROR_CODES, found - bridge_metrics.ERROR_CODES
    assert bridge_metrics.AUTH_REJECTION_CODES <= bridge_metrics.ERROR_CODES


def test_lock_figures_and_history_state_are_recorded_without_ids(configured, log, monkeypatch):
    sink, path = log
    _call("GET", "/ohlcv/EURUSD", {"X-Bridge-Api-Key": _API_KEY,
                                   "X-Bridge-History-Request-Id": "req-secret.1"},
          query="from=2026-06-30T00:00:00Z&to=2026-06-30T06:00:00Z&interval=1h")
    (row,) = _rows(sink, path)
    assert row["history_state"] in {"none", "waiting", "holding", "completed"}
    text = _text(path)
    assert "req-secret" not in text and bridge_metrics.BOOT_ID not in text


def test_no_secret_header_or_price_reaches_the_file(configured, log, monkeypatch):
    sink, path = log
    monkeypatch.setattr(server, "_client", _Client())
    _call("GET", "/quote/EURUSD", {"X-Bridge-Api-Key": _API_KEY, "X-Afx-Caller": "tick",
                                   "Authorization": "Bearer abc"})
    sink.flush()
    text = _text(path)
    for forbidden in (_API_KEY, "1.2345", "Bearer", "http", "127.0.0.1", "X-Bridge"):
        assert forbidden not in text


def test_failed_requests_and_internal_errors_are_recorded(configured, log, monkeypatch):
    sink, path = log
    monkeypatch.setattr(server, "_client", _Client(error=ValueError("boom")))
    with pytest.raises(Exception):
        _call("GET", "/quote/EURUSD", {"X-Bridge-Api-Key": _API_KEY})
    rows = _rows(sink, path)
    assert rows and rows[-1]["status"] == 500 and rows[-1]["error_code"] == "internal_error"


def test_responses_are_identical_with_and_without_the_log(configured, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_client", _Client())
    headers = {"X-Bridge-Api-Key": _API_KEY, "X-Afx-Caller": "cli"}
    before = _call("GET", "/quote/EURUSD", headers)
    path = tmp_path / "bridge-metrics"
    sink = request_log.RequestLog()
    monkeypatch.setattr(request_log, "request_log", sink)
    sink.start(path)
    try:
        after = _call("GET", "/quote/EURUSD", headers)
    finally:
        sink.stop()
    assert before[0] == after[0] and before[2] == after[2]
    assert sorted(k for k, _ in before[1]) == sorted(k for k, _ in after[1])


def test_writer_is_a_bounded_queue_and_a_daemon_thread_outside_the_logging_module(log):
    sink, path = log
    writer = sink._writer
    assert isinstance(writer.queue, queue.Queue) and writer.queue.maxsize > 0
    assert writer._thread.daemon is True and writer._thread.is_alive()
    assert logging.getLogger("bridge.request_log").handlers == []


def _row_at(ts):
    return {"ts": ts, "caller": "tick"}


def test_rows_after_midnight_go_to_the_new_days_file_and_nothing_is_renamed(log):
    sink, path = log
    sink.emit(_row_at("2026-11-16T23:59:59.999Z"))
    sink.emit(_row_at("2026-11-17T00:00:00.001Z"))
    sink.emit(_row_at("2026-11-17T08:00:00.000Z"))
    sink.flush()
    assert sorted(item.name for item in path.iterdir()) == ["2026-11-16.jsonl", "2026-11-17.jsonl"]
    assert len((path / "2026-11-16.jsonl").read_text().splitlines()) == 1
    assert len((path / "2026-11-17.jsonl").read_text().splitlines()) == 2


def test_only_own_files_beyond_14_days_are_removed_at_the_day_change(log):
    sink, path = log
    path.mkdir(exist_ok=True)
    day = datetime(2026, 1, 1)
    for index in range(40):
        (path / f"{(day + timedelta(days=index)):%Y-%m-%d}.jsonl").write_text("x\n")
    for name in ("notes.txt", "2026-01-01.jsonl.bak", "client-2026-01-01.jsonl",
                 "2026-01-01.jsonl.2026-01-01"):
        (path / name).write_text("keep")
    (path / "sub").mkdir()
    outside = path.parent / "outside.jsonl"
    outside.write_text("keep")
    (path / "2025-01-01.jsonl").symlink_to(outside)
    (path / "2025-01-02.jsonl").mkdir()
    sink.emit(_row_at("2026-02-10T00:00:00.000Z"))
    sink.flush()
    kept = sorted(item.name for item in path.glob("2026-??-??.jsonl"))
    assert kept[0] == "2026-01-27.jsonl" and "2026-01-26.jsonl" not in kept
    assert "2026-02-10.jsonl" in kept
    for name in ("notes.txt", "2026-01-01.jsonl.bak", "client-2026-01-01.jsonl",
                 "2026-01-01.jsonl.2026-01-01"):
        assert (path / name).read_text() == "keep"
    assert (path / "sub").is_dir() and (path / "2025-01-02.jsonl").is_dir()
    assert (path / "2025-01-01.jsonl").is_symlink() and outside.read_text() == "keep"


@pytest.mark.parametrize("missing", [
    ("O_NOFOLLOW",), ("O_NOFOLLOW", "O_NONBLOCK", "geteuid", "fchmod"),
])
def test_rows_are_written_where_the_posix_only_attributes_do_not_exist(
        configured, tmp_path, monkeypatch, missing):
    for name in missing:
        monkeypatch.delattr(os, name, raising=False)
    path = tmp_path / "bridge-metrics"
    sink = request_log.RequestLog()
    sink.start(path)
    try:
        sink.emit({"ts": "t"})
        sink.flush()
        assert len(_text(path).splitlines()) == 1
    finally:
        sink.stop()


def test_new_and_existing_files_are_private(configured, tmp_path):
    path = tmp_path / "bridge-metrics"
    sink = request_log.RequestLog()
    sink.start(path)
    try:
        sink.emit({"ts": "t"})
        sink.flush()
        (item,) = path.iterdir()
        assert stat.S_IMODE(item.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    finally:
        sink.stop()
    item.chmod(0o666)
    path.chmod(0o755)
    sink = request_log.RequestLog()
    sink.start(path)
    try:
        sink.emit({"ts": "t"})
        sink.flush()
        assert stat.S_IMODE(item.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    finally:
        sink.stop()


def test_nothing_is_written_outside_the_log_directory(configured, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "target.txt"
    target.write_text("keep")
    link = tmp_path / "bridge-metrics"
    link.symlink_to(outside)
    sink = request_log.RequestLog()
    sink.start(link)
    try:
        sink.emit({"ts": "t"})
        sink.flush()
    finally:
        sink.stop()
    assert target.read_text() == "keep" and sorted(p.name for p in outside.iterdir()) == ["target.txt"]
    link.unlink()
    link.mkdir()
    (link / f"{_today()}.jsonl").symlink_to(target)
    sink = request_log.RequestLog()
    sink.start(link)
    try:
        sink.emit({"ts": "t"})
        sink.flush()
    finally:
        sink.stop()
    assert target.read_text() == "keep"


def test_a_fifo_at_the_log_path_blocks_neither_the_request_nor_the_shutdown(
        configured, tmp_path, monkeypatch):
    path = tmp_path / "bridge-metrics"
    path.mkdir()
    os.mkfifo(path / f"{_today()}.jsonl")
    sink = request_log.RequestLog()
    monkeypatch.setattr(request_log, "request_log", sink)
    sink.start(path)
    started = time.monotonic()
    try:
        assert _call("GET", "/health")[0] == 200
        sink.flush()
    finally:
        sink.stop()
    assert time.monotonic() - started < 10


def test_a_failing_log_never_changes_the_response(configured, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_client", _Client())
    headers = {"X-Bridge-Api-Key": _API_KEY}
    baseline = _call("GET", "/quote/EURUSD", headers)
    missing = tmp_path / "no-such-dir" / "bridge-metrics"
    sink = request_log.RequestLog()
    monkeypatch.setattr(request_log, "request_log", sink)
    sink.start(missing)
    try:
        assert _call("GET", "/quote/EURUSD", headers)[0::2] == (baseline[0], baseline[2])
        sink.flush()
    finally:
        sink.stop()

    class Broken:
        def emit(self, row):
            raise OSError("disk gone")

    monkeypatch.setattr(request_log, "request_log", Broken())
    again = _call("GET", "/quote/EURUSD", headers)
    assert again[0::2] == (baseline[0], baseline[2])


def test_a_blocked_writer_never_makes_a_request_wait_and_drops_are_counted(
        configured, tmp_path, monkeypatch):
    path = tmp_path / "bridge-metrics"
    sink = request_log.RequestLog()
    monkeypatch.setattr(request_log, "request_log", sink)
    sink.start(path, queue_size=3)
    gate = threading.Event()
    _block_writer(sink, gate)
    try:
        started = time.monotonic()
        for _ in range(12):
            assert _call("GET", "/health")[0] == 200
        assert time.monotonic() - started < 5
        assert sink.dropped >= 6
        gate.set()
        sink.flush()
        _call("GET", "/health")
        rows = _rows(sink, path)
        assert any(r.get("dropped_before", 0) > 0 for r in rows)
        assert sum(r.get("dropped_before", 0) for r in rows) == sink.dropped
    finally:
        gate.set()
        sink.stop()


def test_emit_before_start_and_after_stop_is_a_no_op(tmp_path):
    sink = request_log.RequestLog()
    sink.emit({"ts": "t"})
    sink.start(tmp_path / "a")
    sink.stop()
    sink.emit({"ts": "t"})
    assert not (tmp_path / "a").exists() or list((tmp_path / "a").iterdir()) == []


def test_lifespan_starts_the_log_under_logs_and_stops_it(configured, tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(server, "_LOG_DIR", logs)
    settings = server.BridgeSettings(
        mt5_login=1, mt5_password="x", mt5_server="t", host="127.0.0.1", port=8812,
        api_key="", dry_run=True, halt_state_path=str(tmp_path / "halt.flag"),
        filling_mode="IOC", deviation_points=30, server_time_symbol="USDJPY")
    monkeypatch.setattr(server, "load_settings", lambda: settings)

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def connect(self):
            pass

        def disconnect(self):
            pass

        def ping(self):
            return False

    monkeypatch.setattr(server, "Mt5Client", FakeClient)
    monkeypatch.setattr(request_log, "request_log", request_log.RequestLog())

    async def run():
        async with server.lifespan(server.app):
            assert request_log.request_log.active
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: _call("GET", "/health", {"X-Afx-Caller": "cli"}))

    asyncio.run(run())
    assert not request_log.request_log.active
    (written,) = (logs / "bridge-metrics").iterdir()
    assert written.name == f"{_today()}.jsonl"
    (line,) = written.read_text().splitlines()
    assert json.loads(line)["caller"] == "cli"
    assert stat.S_IMODE(written.stat().st_mode) == 0o600


def _block_writer(sink, gate):
    """writer の書込みを gate で止め、最初の 1 行を掴むまで待てる Event を返す。"""
    writer = sink._writer
    real_append = writer._append
    entered = threading.Event()

    def slow_append(day, line):
        entered.set()
        assert gate.wait(20)
        real_append(day, line)

    writer._append = slow_append
    return entered


def _fill_queue(sink, entered):
    sink.emit({"ts": "t"})
    assert entered.wait(10)
    for _ in range(8):
        sink.emit({"ts": "t"})
    assert sink._writer.queue.full()


def test_stopping_with_a_full_queue_neither_raises_nor_hangs(tmp_path):
    sink = request_log.RequestLog()
    sink.start(tmp_path / "a", queue_size=2)
    gate = threading.Event()
    _fill_queue(sink, _block_writer(sink, gate))
    timer = threading.Timer(0.3, gate.set)
    timer.start()
    try:
        sink.stop()
    finally:
        gate.set()
        timer.join()
    assert not sink.active


def test_stop_returns_within_its_deadline_while_a_write_never_returns(tmp_path):
    sink = request_log.RequestLog()
    sink.start(tmp_path / "a", queue_size=2)
    gate = threading.Event()
    _fill_queue(sink, _block_writer(sink, gate))
    try:
        started = time.monotonic()
        sink.stop(timeout=0.3)
        assert time.monotonic() - started < 2
        assert not sink.active
    finally:
        gate.set()


def test_lifespan_disconnects_mt5_even_when_the_log_queue_is_full(configured, tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(server, "_LOG_DIR", logs)
    settings = server.BridgeSettings(
        mt5_login=1, mt5_password="x", mt5_server="t", host="127.0.0.1", port=8812,
        api_key="", dry_run=True, halt_state_path=str(tmp_path / "halt.flag"),
        filling_mode="IOC", deviation_points=30, server_time_symbol="USDJPY")
    monkeypatch.setattr(server, "load_settings", lambda: settings)
    events: list[str] = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def connect(self):
            pass

        def disconnect(self):
            events.append("disconnect")

        def ping(self):
            return False

    monkeypatch.setattr(server, "Mt5Client", FakeClient)
    sink = request_log.RequestLog()
    original_start = sink.start
    original_stop = sink.stop
    sink.start = lambda path: original_start(path, queue_size=2)
    monkeypatch.setattr(request_log, "request_log", sink)
    gate = threading.Event()

    async def run():
        async with server.lifespan(server.app):
            sink.stop = lambda *args, **kwargs: (
                events.append("stop"), original_stop(*args, **kwargs))
            _fill_queue(sink, _block_writer(sink, gate))

    started = time.monotonic()
    try:
        asyncio.run(run())
    finally:
        gate.set()
    assert events == ["disconnect", "stop"]
    assert time.monotonic() - started < 10


def _two_concurrent_emits(sink):
    barrier = threading.Barrier(2, timeout=5)
    real = sink._writer.submit

    def waiting(*args, **kwargs):
        barrier.wait()
        return real(*args, **kwargs)

    sink._writer.submit = waiting
    threads = [threading.Thread(target=sink.emit, args=({"ts": "t"},)) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)


def test_concurrent_emits_report_each_drop_once(log):
    sink, path = log
    for _ in range(5):
        sink._count_drop()
    _two_concurrent_emits(sink)
    rows = _rows(sink, path)
    assert len(rows) == 2
    assert sum(r.get("dropped_before", 0) for r in rows) == sink.dropped == 5


def test_a_dropped_row_hands_back_the_drops_it_was_carrying(tmp_path):
    path = tmp_path / "a"
    sink = request_log.RequestLog()
    sink.start(path, queue_size=1)
    gate = threading.Event()
    _block_writer(sink, gate)
    try:
        for _ in range(12):
            sink.emit({"ts": "t"})
        gate.set()
        sink.flush()
        sink.emit({"ts": "t"})
        rows = _rows(sink, path)
        assert sum(r.get("dropped_before", 0) for r in rows) == sink.dropped
    finally:
        gate.set()
        sink.stop()


@pytest.mark.parametrize("path,route", [
    ("/health/", "/health"), ("/quote/EURUSD/", "/quote/{symbol}"),
    ("/positions/1/close/", "/positions/{ticket}/close"), ("/", "/other"),
])
def test_a_trailing_slash_does_not_change_the_route(path, route):
    assert request_log.route_of(path) == route
