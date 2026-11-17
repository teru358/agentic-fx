from __future__ import annotations

import importlib.util
import json
import logging.handlers
import os
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from agentic_fx.datafeed import bridge_observe, sources

BOOT = "0123456789abcdef"
SLOT_ID = "req-abc.123"
URL = "http://bridge.example.invalid:8812"


def _root(tmp_path: Path) -> Path:
    (tmp_path / "logs").mkdir(exist_ok=True)
    return tmp_path


def _rows(root: Path) -> list[dict]:
    bridge_observe.flush()
    directory = root / "logs" / "bridge-metrics"
    out: list[dict] = []
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("client*.jsonl*")):
        for line in path.read_text().splitlines():
            out.append(json.loads(line))
    return out


def _headers(**overrides) -> dict[str, str]:
    base = {
        "X-Bridge-Metrics-Version": "1",
        "X-Bridge-Boot-Id": BOOT,
        "X-Bridge-Lock-State": "measured",
        "X-Bridge-Lock-Wait-Ms": "12.500",
        "X-Bridge-Lock-Hold-Ms": "40.250",
        "X-Bridge-Lock-Acquisitions": "3",
        "X-Bridge-Lock-Wait-Max-Ms": "7.000",
        "X-Bridge-Lock-Hold-Max-Ms": "30.000",
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def _response(url: str, status: int = 200, headers=None, body=None) -> httpx.Response:
    return httpx.Response(status, headers=headers or {}, json=body if body is not None else {},
                          request=httpx.Request("GET", url))


def _patch_get(monkeypatch, response=None, exc=None):
    calls = []

    def fake(url, **kwargs):
        calls.append((url, kwargs))
        if exc is not None:
            raise exc
        return response if not callable(response) else response(url)

    monkeypatch.setattr(httpx, "get", fake)
    return calls


_CLIENT_KEYS = {
    "ts", "caller", "route", "status", "error_code", "lock_state", "wait_ms", "hold_ms",
    "wait_max_ms", "hold_max_ms", "acquisitions", "elapsed_ms", "timeout_s", "failure",
}


def test_measured_response_is_recorded_without_url_or_raw_ids(monkeypatch, tmp_path):
    root = _root(tmp_path)
    bridge_observe.configure(root, "cli")
    url = f"{URL}/ohlcv/EURUSD"
    _patch_get(monkeypatch, _response(url, headers=_headers()))
    response = bridge_observe.bridge_get(url, params={"from": "x"}, timeout=3)
    assert response.status_code == 200
    (row,) = _rows(root)
    assert row["caller"] == "cli"
    assert row["route"] == "/ohlcv/{symbol}"
    assert row["status"] == 200
    assert row["lock_state"] == "measured"
    assert row["wait_ms"] == 12.5 and row["hold_ms"] == 40.25
    assert row["wait_max_ms"] == 7.0 and row["hold_max_ms"] == 30.0
    assert row["acquisitions"] == 3
    assert row["failure"] is None
    assert row["elapsed_ms"] >= 0 and row["timeout_s"] == 3
    assert set(row) == _CLIENT_KEYS
    text = json.dumps(row)
    for secret in (BOOT, "bridge.example", "8812", "EURUSD", "from"):
        assert secret not in text
    assert datetime.strptime(row["ts"], "%Y-%m-%dT%H:%M:%S.%fZ")


def test_return_value_and_arguments_pass_through_unchanged(monkeypatch, tmp_path):
    bridge_observe.configure(_root(tmp_path), "cli")
    url = f"{URL}/health"
    sentinel = _response(url, headers=_headers())
    calls = _patch_get(monkeypatch, sentinel)
    given = {"k": "v"}
    assert bridge_observe.bridge_get(url, headers=given, timeout=7) is sentinel
    assert calls == [(url, {"headers": {"k": "v", "X-Afx-Caller": "cli"}, "timeout": 7})]
    assert given == {"k": "v"}


def test_missing_headers_are_recorded_as_missing_not_zero(monkeypatch, tmp_path):
    root = _root(tmp_path)
    bridge_observe.configure(root, "cli")
    _patch_get(monkeypatch, _response(f"{URL}/health"))
    bridge_observe.bridge_get(f"{URL}/health")
    (row,) = _rows(root)
    assert row["lock_state"] == "missing"
    for key in ("wait_ms", "hold_ms", "wait_max_ms", "hold_max_ms", "acquisitions"):
        assert row[key] is None


@pytest.mark.parametrize("override", [
    {"X-Bridge-Lock-Wait-Ms": "nan"},
    {"X-Bridge-Lock-Wait-Ms": "-1"},
    {"X-Bridge-Lock-Hold-Ms": "abc"},
    {"X-Bridge-Lock-Acquisitions": "1.5"},
    {"X-Bridge-Lock-Hold-Max-Ms": None},
    {"X-Bridge-Lock-Wait-Ms": "inf"},
])
def test_malformed_numbers_are_unavailable_never_zero(monkeypatch, tmp_path, override):
    root = _root(tmp_path)
    bridge_observe.configure(root, "cli")
    _patch_get(monkeypatch, _response(f"{URL}/health", headers=_headers(**override)))
    bridge_observe.bridge_get(f"{URL}/health")
    (row,) = _rows(root)
    assert row["lock_state"] == "unavailable"
    assert row["wait_ms"] is None and row["hold_ms"] is None and row["acquisitions"] is None


def test_bridge_declared_unavailable_and_not_locked(monkeypatch, tmp_path):
    root = _root(tmp_path)
    bridge_observe.configure(root, "cli")
    _patch_get(monkeypatch, _response(f"{URL}/health", headers={
        "X-Bridge-Metrics-Version": "1", "X-Bridge-Lock-State": "unavailable"}))
    bridge_observe.bridge_get(f"{URL}/health")
    _patch_get(monkeypatch, _response(f"{URL}/health", headers=_headers(
        **{"X-Bridge-Lock-State": "not_locked", "X-Bridge-Lock-Wait-Ms": "0.000",
           "X-Bridge-Lock-Hold-Ms": "0.000", "X-Bridge-Lock-Acquisitions": "0",
           "X-Bridge-Lock-Wait-Max-Ms": "0.000", "X-Bridge-Lock-Hold-Max-Ms": "0.000"})))
    bridge_observe.bridge_get(f"{URL}/health")
    first, second = _rows(root)
    assert first["lock_state"] == "unavailable" and first["wait_ms"] is None
    assert second["lock_state"] == "not_locked" and second["acquisitions"] == 0


def test_unknown_lock_state_value_is_unavailable(monkeypatch, tmp_path):
    root = _root(tmp_path)
    bridge_observe.configure(root, "cli")
    _patch_get(monkeypatch, _response(f"{URL}/health", headers=_headers(
        **{"X-Bridge-Lock-State": "weird"})))
    bridge_observe.bridge_get(f"{URL}/health")
    assert _rows(root)[0]["lock_state"] == "unavailable"


def test_error_code_is_kept_only_when_in_the_protocol_allowlist(monkeypatch, tmp_path):
    root = _root(tmp_path)
    bridge_observe.configure(root, "cli")
    url = f"{URL}/ohlcv/X"
    secret_shaped = "k" * 20 + "4f2a"
    for code in ("symbol_unavailable", "http://secret/x?k=1", "12345678", secret_shaped,
                 "not_a_protocol_code"):
        _patch_get(monkeypatch, _response(url, 404, _headers(**{"X-Bridge-Error-Code": code})))
        bridge_observe.bridge_get(url)
    rows = _rows(root)
    assert [r["error_code"] for r in rows] == [
        "symbol_unavailable", "unknown", "unknown", "unknown", "unknown"]
    assert rows[0]["status"] == 404
    assert "secret" not in json.dumps(rows) and secret_shaped not in json.dumps(rows)


def test_error_code_allowlist_is_the_bridge_protocol_set():
    path = Path(__file__).resolve().parents[2] / "mt5_bridge" / "bridge_metrics.py"
    spec = importlib.util.spec_from_file_location("bridge_protocol_for_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        protocol = module.ERROR_CODES
    finally:
        sys.modules.pop(spec.name, None)
    assert bridge_observe.ERROR_CODES == protocol


def test_history_headers_and_ids_are_not_recorded_on_the_client(monkeypatch, tmp_path):
    root = _root(tmp_path)
    bridge_observe.configure(root, "cli")
    _patch_get(monkeypatch, _response(f"{URL}/health", headers=_headers(**{
        "X-Bridge-History-Request-Id": SLOT_ID, "X-Bridge-History-State": "completed",
        "X-Bridge-History-Wait-Ms": "1.000", "X-Bridge-History-Hold-Ms": "2.000"})))
    bridge_observe.bridge_get(f"{URL}/health")
    (row,) = _rows(root)
    assert set(row) == _CLIENT_KEYS
    text = (root / "logs" / "bridge-metrics" / "client-cli.jsonl").read_text()
    assert SLOT_ID not in text and BOOT not in text and "history" not in text


@pytest.mark.parametrize("url,route", [
    ("http://h:1/health", "/health"),
    ("http://h:1/quote/EURUSD", "/quote/{symbol}"),
    ("http://h:1/ohlcv/JP225.cash?x=1", "/ohlcv/{symbol}"),
    ("http://h:1/ohlcv/JP%20225", "/ohlcv/{symbol}"),
    ("http://h:1/server-time", "/server-time"),
    ("http://h:1/symbols", "/symbols"),
    ("http://h:1/positions/123/closed-deal", "/positions/{ticket}/closed-deal"),
    ("http://h:1/account", "/account"),
    ("http://h:1/totally/unknown/path", "/other"),
    ("http://h:1/", "/other"),
    ("not a url", "/other"),
])
def test_route_names_are_fixed_shapes(url, route):
    assert bridge_observe.route_of(url) == route


def test_caller_defaults_scope_and_nesting(tmp_path):
    assert bridge_observe.current_caller() == "unknown"
    bridge_observe.configure(_root(tmp_path), "cli")
    assert bridge_observe.current_caller() == "cli"
    with bridge_observe.caller_scope("tick"):
        assert bridge_observe.current_caller() == "tick"
        with bridge_observe.caller_scope("mission"):
            assert bridge_observe.current_caller() == "mission"
        assert bridge_observe.current_caller() == "tick"
    assert bridge_observe.current_caller() == "cli"
    with pytest.raises(ValueError):
        with bridge_observe.caller_scope("whatever"):
            pass
    with pytest.raises(ValueError):
        bridge_observe.configure(tmp_path, "daemon")


def test_caller_scope_does_not_leak_to_other_threads(monkeypatch, tmp_path):
    root = _root(tmp_path)
    bridge_observe.configure(root, "cli")
    _patch_get(monkeypatch, _response(f"{URL}/health", headers=_headers()))
    done = threading.Event()

    def other():
        bridge_observe.bridge_get(f"{URL}/health")
        done.set()

    with bridge_observe.caller_scope("tick"):
        bridge_observe.bridge_get(f"{URL}/health")
        thread = threading.Thread(target=other)
        thread.start()
        thread.join()
    first, second = _rows(root)
    assert first["caller"] == "tick" and second["caller"] == "cli"


@pytest.mark.parametrize("exc,kind", [
    (httpx.ReadTimeout("t"), "timeout"),
    (httpx.ConnectTimeout("t"), "timeout"),
    (httpx.ConnectError("c"), "connect"),
    (httpx.ReadError("r"), "transport"),
    (RuntimeError("x"), "error"),
])
def test_failures_are_recorded_and_the_same_exception_propagates(monkeypatch, tmp_path, exc, kind):
    root = _root(tmp_path)
    bridge_observe.configure(root, "tick")
    _patch_get(monkeypatch, exc=exc)
    with pytest.raises(type(exc)) as caught:
        bridge_observe.bridge_get(f"{URL}/quote/EURUSD", timeout=1)
    assert caught.value is exc
    (row,) = _rows(root)
    assert row["failure"] == kind and row["status"] is None
    assert row["lock_state"] == "missing" and row["route"] == "/quote/{symbol}"
    assert "bridge.example" not in json.dumps(row)


def test_http_error_status_is_recorded_before_raise_for_status(monkeypatch, tmp_path):
    root = _root(tmp_path)
    bridge_observe.configure(root, "cli")
    _patch_get(monkeypatch, _response(f"{URL}/health", 503, _headers()))
    response = bridge_observe.bridge_get(f"{URL}/health")
    with pytest.raises(httpx.HTTPStatusError):
        response.raise_for_status()
    assert _rows(root)[0]["status"] == 503


def test_unconfigured_observer_writes_nothing_and_does_not_parse(monkeypatch, tmp_path):
    class Boom:
        status_code = 200

        @property
        def headers(self):
            raise AssertionError("headers must not be read when unconfigured")

    _patch_get(monkeypatch, Boom())
    assert bridge_observe.bridge_get(f"{URL}/health").status_code == 200
    assert not (tmp_path / "logs").exists()


def test_odd_response_objects_never_break_the_call(monkeypatch, tmp_path):
    root = _root(tmp_path)
    bridge_observe.configure(root, "cli")

    class Odd:
        status_code = "x"
        headers = object()

    _patch_get(monkeypatch, Odd())
    assert isinstance(bridge_observe.bridge_get(f"{URL}/health"), Odd)
    (row,) = _rows(root)
    assert row["status"] is None and row["lock_state"] == "missing"


def test_write_failure_is_swallowed(monkeypatch, tmp_path):
    root = _root(tmp_path)
    (root / "logs" / "bridge-metrics").write_text("not a directory")
    bridge_observe.configure(root, "cli")
    sentinel = _response(f"{URL}/health", headers=_headers())
    _patch_get(monkeypatch, sentinel)
    assert bridge_observe.bridge_get(f"{URL}/health") is sentinel


def test_logs_directory_is_never_created(monkeypatch, tmp_path):
    bridge_observe.configure(tmp_path, "cli")
    _patch_get(monkeypatch, _response(f"{URL}/health", headers=_headers()))
    bridge_observe.bridge_get(f"{URL}/health")
    assert not (tmp_path / "logs").exists()


def test_file_is_client_jsonl_under_logs_with_private_mode(monkeypatch, tmp_path):
    root = _root(tmp_path)
    bridge_observe.configure(root, "cli")
    _patch_get(monkeypatch, _response(f"{URL}/health", headers=_headers()))
    bridge_observe.bridge_get(f"{URL}/health")
    _rows(root)
    (path,) = (root / "logs" / "bridge-metrics").iterdir()
    assert path.name == "client-cli.jsonl"
    assert (path.stat().st_mode & 0o777) == 0o600
    assert path.read_text().endswith("\n") and len(path.read_text().splitlines()) == 1
    assert len(path.read_bytes()) < 1024


def test_existing_file_with_a_loose_mode_is_made_private_on_open(monkeypatch, tmp_path):
    root = _root(tmp_path)
    directory = root / "logs" / "bridge-metrics"
    directory.mkdir()
    (directory / "client-cli.jsonl").write_text("")
    (directory / "client-cli.jsonl").chmod(0o666)
    bridge_observe.configure(root, "cli")
    _patch_get(monkeypatch, _response(f"{URL}/health", headers=_headers()))
    bridge_observe.bridge_get(f"{URL}/health")
    assert len(_rows(root)) == 1
    assert stat.S_IMODE((directory / "client-cli.jsonl").stat().st_mode) == 0o600


def test_writer_is_a_queue_in_front_of_a_midnight_utc_rotation_of_14_files(tmp_path):
    bridge_observe.configure(_root(tmp_path), "cli")
    handler = bridge_observe.file_handler()
    assert isinstance(handler, logging.handlers.TimedRotatingFileHandler)
    assert handler.when == "MIDNIGHT" and handler.utc is True and handler.backupCount == 14
    assert isinstance(bridge_observe.queue_handler(), logging.handlers.QueueHandler)


def test_the_call_only_enqueues_and_never_waits_for_a_blocked_writer(monkeypatch, tmp_path):
    root = _root(tmp_path)
    bridge_observe.configure(root, "cli", queue_size=3)
    gate = threading.Event()
    handler = bridge_observe.file_handler()
    real_emit = handler.emit

    def slow_emit(record):
        assert gate.wait(20)
        real_emit(record)

    handler.emit = slow_emit
    sentinel = _response(f"{URL}/health", headers=_headers())
    _patch_get(monkeypatch, sentinel)
    try:
        started = time.monotonic()
        for _ in range(12):
            assert bridge_observe.bridge_get(f"{URL}/health") is sentinel
        assert time.monotonic() - started < 5
        assert bridge_observe.dropped() >= 6
        gate.set()
        bridge_observe.flush()
        bridge_observe.bridge_get(f"{URL}/health")
        rows = _rows(root)
        assert sum(r.get("dropped_before", 0) for r in rows) == bridge_observe.dropped()
    finally:
        gate.set()


def test_a_fifo_at_the_log_path_does_not_block_the_call_or_the_shutdown(monkeypatch, tmp_path):
    root = _root(tmp_path)
    directory = root / "logs" / "bridge-metrics"
    directory.mkdir()
    os.mkfifo(directory / "client-cli.jsonl")
    bridge_observe.configure(root, "cli")
    sentinel = _response(f"{URL}/health", headers=_headers())
    _patch_get(monkeypatch, sentinel)
    started = time.monotonic()
    assert bridge_observe.bridge_get(f"{URL}/health") is sentinel
    bridge_observe.flush()
    bridge_observe.reset()
    assert time.monotonic() - started < 10


def test_nothing_is_written_outside_the_log_directory(monkeypatch, tmp_path):
    root = _root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "client-cli.jsonl").write_text("keep")
    (root / "logs" / "bridge-metrics").symlink_to(outside)
    bridge_observe.configure(root, "cli")
    _patch_get(monkeypatch, _response(f"{URL}/health", headers=_headers()))
    bridge_observe.bridge_get(f"{URL}/health")
    bridge_observe.flush()
    assert (outside / "client-cli.jsonl").read_text() == "keep"
    assert sorted(p.name for p in outside.iterdir()) == ["client-cli.jsonl"]
    bridge_observe.reset()
    (root / "logs" / "bridge-metrics").unlink()
    directory = root / "logs" / "bridge-metrics"
    directory.mkdir()
    (directory / "client-cli.jsonl").symlink_to(outside / "client-cli.jsonl")
    bridge_observe.configure(root, "cli")
    bridge_observe.bridge_get(f"{URL}/health")
    bridge_observe.flush()
    assert (outside / "client-cli.jsonl").read_text() == "keep"


def test_observed_fetch_wrapper_records_injected_fetch(tmp_path):
    root = _root(tmp_path)
    bridge_observe.configure(root, "cli")
    response = _response(f"{URL}/symbols", headers=_headers(), body=["A"])

    def fetch(url, **kwargs):
        return response

    wrapped = bridge_observe.observed(fetch)
    assert wrapped(f"{URL}/symbols", timeout=5, headers={}) is response
    (row,) = _rows(root)
    assert row["route"] == "/symbols" and row["lock_state"] == "measured"

    def boom(url, **kwargs):
        raise httpx.ConnectError("x")

    with pytest.raises(httpx.ConnectError):
        bridge_observe.observed(boom)(f"{URL}/health")
    assert _rows(root)[1]["failure"] == "connect"


def test_rotated_files_beyond_14_days_are_removed_and_nothing_else(tmp_path):
    root = _root(tmp_path)
    directory = root / "logs" / "bridge-metrics"
    directory.mkdir()
    day = datetime(2026, 1, 1)
    for index in range(20):
        (directory / f"client-cli.jsonl.{(day + timedelta(days=index)):%Y-%m-%d}").write_text("x\n")
    for name in ("notes.txt", "client-cli.jsonl.bak", "2026-01-01.jsonl", "other.jsonl.2026-01-01"):
        (directory / name).write_text("keep")
    (directory / "sub").mkdir()
    outside = tmp_path / "outside.jsonl"
    outside.write_text("keep")
    (directory / "client-cli.jsonl.2025-01-01").symlink_to(outside)
    bridge_observe.configure(root, "cli")
    bridge_observe.file_handler().doRollover()
    bridge_observe.flush()
    rotated = sorted(p.name for p in directory.glob("client-cli.jsonl.2???-??-??"))
    assert len(rotated) <= 14
    assert "client-cli.jsonl.2026-01-01" not in rotated and "client-cli.jsonl.2025-01-01" not in rotated
    for name in ("notes.txt", "client-cli.jsonl.bak", "2026-01-01.jsonl", "other.jsonl.2026-01-01"):
        assert (directory / name).read_text() == "keep"
    assert (directory / "sub").is_dir()
    assert outside.read_text() == "keep"


def test_the_day_change_in_utc_rolls_the_file_over(monkeypatch, tmp_path):
    root = _root(tmp_path)
    directory = root / "logs" / "bridge-metrics"
    directory.mkdir()
    current = directory / "client-cli.jsonl"
    current.write_text('{"ts":"old"}\n')
    two_days_ago = time.time() - 2 * 86400
    os.utime(current, (two_days_ago, two_days_ago))
    bridge_observe.configure(root, "cli")
    _patch_get(monkeypatch, _response(f"{URL}/health", headers=_headers()))
    bridge_observe.bridge_get(f"{URL}/health")
    bridge_observe.flush()
    stamp = datetime.fromtimestamp(two_days_ago, timezone.utc).strftime("%Y-%m-%d")
    assert (directory / f"client-cli.jsonl.{stamp}").read_text() == '{"ts":"old"}\n'
    assert len(current.read_text().splitlines()) == 1


def test_every_bridge_request_carries_the_caller_label_even_when_unconfigured(
        monkeypatch, tmp_path):
    calls = _patch_get(monkeypatch, _response(f"{URL}/health", headers=_headers()))
    bridge_observe.bridge_get(f"{URL}/health", headers={"X-Bridge-Api-Key": "x"})
    assert calls[0][1]["headers"] == {"X-Bridge-Api-Key": "x", "X-Afx-Caller": "unknown"}
    bridge_observe.set_default_caller("mission")
    bridge_observe.bridge_get(f"{URL}/health")
    assert calls[1][1]["headers"] == {"X-Afx-Caller": "mission"}
    with bridge_observe.caller_scope("tick"):
        bridge_observe.bridge_get(f"{URL}/health")
    assert calls[2][1]["headers"] == {"X-Afx-Caller": "tick"}


def test_concurrent_threads_and_processes_append_whole_lines(tmp_path):
    root = _root(tmp_path)
    code = (
        "import sys, httpx\n"
        "from pathlib import Path\n"
        "from agentic_fx.datafeed import bridge_observe\n"
        "bridge_observe.configure(Path(sys.argv[1]), 'mission')\n"
        "r = httpx.Response(200, headers={'X-Bridge-Metrics-Version': '1'}, json={},"
        " request=httpx.Request('GET', 'http://h/health'))\n"
        "httpx.get = lambda url, **kw: r\n"
        "for _ in range(60):\n"
        "    bridge_observe.bridge_get('http://h/health')\n"
    )
    procs = [subprocess.Popen([sys.executable, "-c", code, str(root)]) for _ in range(3)]
    bridge_observe.configure(root, "tick")
    response = _response(f"{URL}/health", headers=_headers())
    original = httpx.get
    httpx.get = lambda url, **kw: response
    try:
        threads = [threading.Thread(target=lambda: [bridge_observe.bridge_get(f"{URL}/health")
                                                    for _ in range(60)]) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        httpx.get = original
    for proc in procs:
        assert proc.wait(timeout=60) == 0
    rows = _rows(root)
    assert len(rows) == 360
    assert sum(r["caller"] == "mission" for r in rows) == 180
    assert sum(r["caller"] == "tick" for r in rows) == 180


# ── 呼出し箇所 ─────────────────────────────────────────────────────

def _bars_body():
    return {"symbol": "EURUSD", "interval": "5m", "bars": [
        {"time": "2026-11-02T00:00:00+00:00", "open": 1, "high": 2, "low": 0.5,
         "close": 1.5, "volume": 3}]}


def test_sources_mt5_functions_record_and_keep_results(monkeypatch, tmp_path):
    root = _root(tmp_path)
    bridge_observe.configure(root, "tick")
    start = datetime(2026, 11, 2, tzinfo=timezone.utc)

    def get(url, **kwargs):
        if url.endswith("/health"):
            return _response(url, headers=_headers(), body={"mt5_connected": True})
        if "/quote/" in url:
            return _response(url, headers=_headers(), body={
                "bid": 1.0, "ask": 1.1, "time": "2026-11-02T00:00:00+00:00"})
        return _response(url, headers=_headers(), body=_bars_body())

    monkeypatch.setattr(httpx, "get", get)
    assert sources.mt5_health(URL) is True
    assert sources.mt5_quote(URL, "EURUSD").bid == 1.0
    assert len(sources.mt5_bars_range(URL, "EURUSD", "5m", start, start + timedelta(hours=1))) == 1
    raw = sources.MT5RawSymbol.parse("JP225.cash")
    assert len(sources.mt5_raw_bars_range(URL, raw, "5m", start, start + timedelta(hours=1))) == 1
    assert len(sources.mt5_bars(URL, "EURUSD", "5m", 1)) == 1
    routes = [r["route"] for r in _rows(root)]
    assert routes == ["/health", "/quote/{symbol}", "/ohlcv/{symbol}", "/ohlcv/{symbol}",
                      "/ohlcv/{symbol}"]
    assert {r["caller"] for r in _rows(root)} == {"tick"}


def test_sources_error_types_are_unchanged_and_error_code_recorded(monkeypatch, tmp_path):
    root = _root(tmp_path)
    bridge_observe.configure(root, "tick")
    start = datetime(2026, 11, 2, tzinfo=timezone.utc)
    response = httpx.Response(503, json={"detail": "time_rule_mismatch"},
                              headers=_headers(**{"X-Bridge-Error-Code": "time_rule_mismatch"}),
                              request=httpx.Request("GET", f"{URL}/ohlcv/EURUSD"))
    monkeypatch.setattr(httpx, "get", lambda url, **kw: response)
    with pytest.raises(sources.BridgeTimeRuleMismatch):
        sources.mt5_bars_range(URL, "EURUSD", "5m", start, start + timedelta(hours=1))
    assert _rows(root)[0]["error_code"] == "time_rule_mismatch"
    server_error = httpx.Response(500, json={}, headers=_headers(),
                                  request=httpx.Request("GET", f"{URL}/quote/EURUSD"))
    monkeypatch.setattr(httpx, "get", lambda url, **kw: server_error)
    with pytest.raises(httpx.HTTPStatusError):
        sources.mt5_quote(URL, "EURUSD")
    assert _rows(root)[1]["status"] == 500


def test_sources_timeout_type_is_unchanged(monkeypatch, tmp_path):
    root = _root(tmp_path)
    bridge_observe.configure(root, "tick")
    exc = httpx.ReadTimeout("slow")
    monkeypatch.setattr(httpx, "get", lambda url, **kw: (_ for _ in ()).throw(exc))
    with pytest.raises(httpx.ReadTimeout) as caught:
        sources.mt5_health(URL)
    assert caught.value is exc
    assert _rows(root)[0]["failure"] == "timeout"


def test_non_bridge_sources_are_not_observed(monkeypatch, tmp_path):
    root = _root(tmp_path)
    bridge_observe.configure(root, "tick")
    response = httpx.Response(200, json={"bid": 1, "ask": 1, "timestamp": 1},
                              request=httpx.Request("GET", "https://api.twelvedata.com/quote"))
    monkeypatch.setattr(httpx, "get", lambda url, **kw: response)
    sources.td_quote("k", "EURUSD")
    assert _rows(root) == []


def test_mt5_import_default_fetch_records(monkeypatch, tmp_path):
    from agentic_fx.backtest import mt5_import

    root = _root(tmp_path)
    bridge_observe.configure(root, "cli")
    url = f"{URL}/ohlcv/EURUSD?interval=5m"
    monkeypatch.setattr(httpx, "get", lambda u, **kw: _response(u, headers=_headers(), body=_bars_body()))
    assert mt5_import._default_fetch(url)["symbol"] == "EURUSD"
    (row,) = _rows(root)
    assert row["route"] == "/ohlcv/{symbol}" and row["caller"] == "cli"


def test_time_repair_default_get_records(monkeypatch, tmp_path):
    from agentic_fx.backtest import time_repair

    root = _root(tmp_path)
    bridge_observe.configure(root, "cli")
    seen = []

    def get(url, **kw):
        seen.append(url)
        return _response(url, headers=_headers(), body={})

    monkeypatch.setattr(httpx, "get", get)
    with pytest.raises(time_repair.RepairError):
        time_repair._bridge_check(__import__("sqlite3").connect(":memory:"), URL)
    assert seen and _rows(root)[0]["route"] == "/server-time"


def test_setup_checks_through_observed_fetch_record(tmp_path):
    from agentic_fx.setup import checks

    root = _root(tmp_path)
    bridge_observe.configure(root, "cli")

    def fetch(url, **kwargs):
        if url.endswith("/health"):
            return _response(url, headers=_headers(), body={"mt5_connected": True})
        return _response(url, headers=_headers(), body={"time": "2026-11-02T00:00:00+00:00"})

    result = checks.check_bridge(URL, "EURUSD", fetch=bridge_observe.observed(fetch), now=None)
    assert result.result == "ok"
    assert [r["route"] for r in _rows(root)] == ["/health", "/quote/{symbol}"]


def test_scheduler_tick_marks_tick_caller(monkeypatch, tmp_path):
    from agentic_fx import service

    bridge_observe.configure(_root(tmp_path), "unknown")
    seen = []
    monkeypatch.setattr(service, "_scheduler_tick_body",
                        lambda app: seen.append(bridge_observe.current_caller()))

    class App:
        writer_admission = None

    service._scheduler_tick_once(App())
    assert seen == ["tick"]
    assert bridge_observe.current_caller() == "unknown"


def test_entry_configures_cli_caller_for_commands_and_unknown_for_daemon(monkeypatch, tmp_path):
    from agentic_fx import entry, service
    from agentic_fx.setup import preflight

    root = _root(tmp_path)
    calls = []
    monkeypatch.setattr(bridge_observe, "configure", lambda r, c="unknown": calls.append((Path(r), c)))
    monkeypatch.setattr(preflight, "main", lambda *a, **k: 0)
    assert entry.main(["--root", str(root), "check"]) == 0
    assert calls == [(root, "cli")]
    calls.clear()
    monkeypatch.setattr(service, "run_service", lambda r, daemon=False: 0)
    assert entry.main(["--root", str(root), "--daemon"]) == 0
    assert calls == [(root, "cli"), (root, "unknown")]


def test_mission_worker_labels_its_calls_as_mission():
    import inspect
    from agentic_fx import mission_worker

    assert "bridge_observe" in inspect.getsource(mission_worker.main)


def _with_caller_headers(headers):
    return bridge_observe._with_caller({"headers": headers})["headers"]


def test_caller_label_is_added_to_headers_of_any_mapping_shape_without_losing_keys():
    secret = {"X-Api-Key": "k-secret"}
    for given in (dict(secret), httpx.Headers(secret), [("X-Api-Key", "k-secret")],
                  (("X-Api-Key", "k-secret"),)):
        merged = httpx.Headers(_with_caller_headers(given))
        assert merged["x-api-key"] == "k-secret"
        assert merged[bridge_observe.CALLER_HEADER] == "unknown"
    original = httpx.Headers(secret)
    _with_caller_headers(original)
    assert bridge_observe.CALLER_HEADER not in original
    pairs = [("X-Api-Key", "k-secret")]
    _with_caller_headers(pairs)
    assert pairs == [("X-Api-Key", "k-secret")]


def _block_writer(sink_handler, gate):
    """writer を gate で止め、最初の 1 行を掴むまで待てる Event を返す。"""
    real_emit = sink_handler.emit
    entered = threading.Event()

    def slow_emit(record):
        entered.set()
        assert gate.wait(20)
        real_emit(record)

    sink_handler.emit = slow_emit
    return entered


def test_stopping_with_a_full_queue_neither_raises_nor_hangs(tmp_path):
    root = _root(tmp_path)
    bridge_observe.configure(root, "cli", queue_size=2)
    gate = threading.Event()
    entered = _block_writer(bridge_observe.file_handler(), gate)
    sink = bridge_observe._sink
    sink.emit({"ts": "t"})
    assert entered.wait(10)
    for _ in range(8):
        sink.emit({"ts": "t"})
    assert sink._queue.full()
    timer = threading.Timer(0.3, gate.set)
    timer.start()
    try:
        bridge_observe.reset()
    finally:
        gate.set()
        timer.join()
    assert bridge_observe._sink is None


def _two_concurrent_emits(sink):
    barrier = threading.Barrier(2, timeout=5)
    real = sink._logger.info

    def waiting(*args, **kwargs):
        barrier.wait()
        real(*args, **kwargs)

    sink._logger.info = waiting
    threads = [threading.Thread(target=sink.emit, args=({"ts": "t"},)) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)


def test_concurrent_emits_report_each_drop_once(tmp_path):
    root = _root(tmp_path)
    bridge_observe.configure(root, "cli")
    sink = bridge_observe._sink
    for _ in range(5):
        sink.count_drop()
    _two_concurrent_emits(sink)
    rows = _rows(root)
    assert len(rows) == 2
    assert sum(r.get("dropped_before", 0) for r in rows) == bridge_observe.dropped() == 5


def test_a_dropped_row_hands_back_the_drops_it_was_carrying(tmp_path):
    root = _root(tmp_path)
    bridge_observe.configure(root, "cli", queue_size=1)
    gate = threading.Event()
    _block_writer(bridge_observe.file_handler(), gate)
    sink = bridge_observe._sink
    try:
        for _ in range(12):
            sink.emit({"ts": "t"})
        gate.set()
        bridge_observe.flush()
        sink.emit({"ts": "t"})
        rows = _rows(root)
        assert sum(r.get("dropped_before", 0) for r in rows) == bridge_observe.dropped()
    finally:
        gate.set()


@pytest.mark.parametrize("url,route", [
    ("http://h:1/health/", "/health"),
    ("http://h:1/quote/EURUSD/", "/quote/{symbol}"),
    ("http://h:1/positions/1/close/", "/positions/{ticket}/close"),
])
def test_a_trailing_slash_does_not_change_the_route(url, route):
    assert bridge_observe.route_of(url) == route


def _bridge_request_log():
    path = Path(__file__).resolve().parents[2] / "mt5_bridge"
    name = "bridge_request_log_for_parity"
    sys.path.insert(0, str(path))
    try:
        spec = importlib.util.spec_from_file_location(name, path / "request_log.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(path))
        sys.modules.pop(name, None)
        sys.modules.pop("bridge_metrics", None)
    return module


def test_bridge_and_client_agree_on_error_codes_and_route_shapes():
    bridge = _bridge_request_log()
    assert bridge._SIMPLE_ROUTES == bridge_observe._SIMPLE_ROUTES
    assert bridge.bridge_metrics.ERROR_CODES == bridge_observe.ERROR_CODES
    assert bridge.CALLERS < set(bridge_observe.CALLERS)
    paths = [
        "/health", "/health/", "/account", "/server-time", "/positions", "/symbols", "/order",
        "/admin/status", "/admin/halt", "/admin/resume", "/quote/EURUSD", "/quote/EURUSD/",
        "/ohlcv/JP225.cash", "/ohlcv/", "/quote/", "/positions/1/closed-deal",
        "/positions/1/modify", "/positions/1/close", "/positions/1/other", "/positions//close",
        "/positions/1/close/", "/", "", "/totally/unknown", "/a/b/c/d/e",
    ]
    for path in paths:
        assert bridge.route_of(path) == bridge_observe.route_of(f"http://h:1{path}"), path


def test_cli_and_daemon_write_to_separate_files(monkeypatch, tmp_path):
    root = _root(tmp_path)
    _patch_get(monkeypatch, _response(f"{URL}/health", headers=_headers()))
    bridge_observe.configure(root, "cli")
    bridge_observe.bridge_get(f"{URL}/health")
    bridge_observe.flush()
    bridge_observe.configure(root, "unknown")
    bridge_observe.bridge_get(f"{URL}/health")
    bridge_observe.flush()
    directory = root / "logs" / "bridge-metrics"
    assert sorted(p.name for p in directory.iterdir()) == ["client-cli.jsonl", "client.jsonl"]
    assert json.loads((directory / "client-cli.jsonl").read_text())["caller"] == "cli"
    assert json.loads((directory / "client.jsonl").read_text())["caller"] == "unknown"


def test_the_metrics_directory_is_private_when_created_and_when_it_already_exists(
        monkeypatch, tmp_path):
    _patch_get(monkeypatch, _response(f"{URL}/health", headers=_headers()))
    old = os.umask(0o022)
    try:
        (tmp_path / "fresh").mkdir()
        root = _root(tmp_path / "fresh")
        bridge_observe.configure(root, "cli")
        bridge_observe.bridge_get(f"{URL}/health")
        bridge_observe.flush()
        directory = root / "logs" / "bridge-metrics"
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        bridge_observe.reset()
        directory.chmod(0o755)
        bridge_observe.configure(root, "cli")
        bridge_observe.bridge_get(f"{URL}/health")
        bridge_observe.flush()
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    finally:
        os.umask(old)
