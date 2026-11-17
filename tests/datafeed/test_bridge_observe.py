from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from datetime import date, datetime, timedelta, timezone
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
    directory = root / "logs" / "bridge-metrics"
    out: list[dict] = []
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.jsonl")):
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
    assert row["metrics_version"] == "1"
    assert row["lock_state"] == "measured"
    assert row["wait_ms"] == 12.5 and row["hold_ms"] == 40.25
    assert row["wait_max_ms"] == 7.0 and row["hold_max_ms"] == 30.0
    assert row["acquisitions"] == 3
    assert row["boot_hash"] == hashlib.sha256(BOOT.encode()).hexdigest()[:12]
    assert row["failure"] is None
    assert row["elapsed_ms"] >= 0
    text = json.dumps(row)
    for secret in (BOOT, "bridge.example", "8812", "EURUSD", "from"):
        assert secret not in text
    assert datetime.strptime(row["ts"], "%Y-%m-%dT%H:%M:%S.%fZ")


def test_return_value_and_arguments_pass_through_unchanged(monkeypatch, tmp_path):
    bridge_observe.configure(_root(tmp_path), "cli")
    url = f"{URL}/health"
    sentinel = _response(url, headers=_headers())
    calls = _patch_get(monkeypatch, sentinel)
    assert bridge_observe.bridge_get(url, headers={"k": "v"}, timeout=7) is sentinel
    assert calls == [(url, {"headers": {"k": "v"}, "timeout": 7})]


def test_missing_headers_are_recorded_as_missing_not_zero(monkeypatch, tmp_path):
    root = _root(tmp_path)
    bridge_observe.configure(root, "cli")
    _patch_get(monkeypatch, _response(f"{URL}/health"))
    bridge_observe.bridge_get(f"{URL}/health")
    (row,) = _rows(root)
    assert row["lock_state"] == "missing"
    assert row["metrics_version"] is None and row["boot_hash"] is None
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


def test_error_code_is_kept_only_when_a_fixed_token(monkeypatch, tmp_path):
    root = _root(tmp_path)
    bridge_observe.configure(root, "cli")
    url = f"{URL}/ohlcv/X"
    _patch_get(monkeypatch, _response(url, 404, _headers(**{"X-Bridge-Error-Code": "symbol_unavailable"})))
    bridge_observe.bridge_get(url)
    _patch_get(monkeypatch, _response(url, 404, _headers(**{"X-Bridge-Error-Code": "http://secret/x?k=1"})))
    bridge_observe.bridge_get(url)
    first, second = _rows(root)
    assert first["status"] == 404 and first["error_code"] == "symbol_unavailable"
    assert second["error_code"] is None
    assert "secret" not in json.dumps(second)


def test_history_slot_state_and_hashed_id(monkeypatch, tmp_path):
    root = _root(tmp_path)
    bridge_observe.configure(root, "cli")
    _patch_get(monkeypatch, _response(f"{URL}/health", headers=_headers(**{
        "X-Bridge-History-Request-Id": SLOT_ID, "X-Bridge-History-State": "completed",
        "X-Bridge-History-Wait-Ms": "1.000", "X-Bridge-History-Hold-Ms": "2.000"})))
    bridge_observe.bridge_get(f"{URL}/health")
    _patch_get(monkeypatch, _response(f"{URL}/health", headers=_headers(**{
        "X-Bridge-History-State": "none"})))
    bridge_observe.bridge_get(f"{URL}/health")
    _patch_get(monkeypatch, _response(f"{URL}/health", headers=_headers(**{
        "X-Bridge-History-Request-Id": SLOT_ID, "X-Bridge-History-State": "bogus"})))
    bridge_observe.bridge_get(f"{URL}/health")
    done, none, bogus = _rows(root)
    assert done["history_state"] == "completed"
    assert done["history_id_hash"] == hashlib.sha256(SLOT_ID.encode()).hexdigest()[:12]
    assert done["history_wait_ms"] == 1.0 and done["history_hold_ms"] == 2.0
    assert SLOT_ID not in json.dumps(done)
    assert none["history_state"] == "none" and none["history_id_hash"] is None
    assert bogus["history_state"] is None and bogus["history_id_hash"] is None


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


def test_file_is_utc_dated_jsonl_with_private_mode(monkeypatch, tmp_path):
    root = _root(tmp_path)
    bridge_observe.configure(root, "cli")
    _patch_get(monkeypatch, _response(f"{URL}/health", headers=_headers()))
    bridge_observe.bridge_get(f"{URL}/health")
    (path,) = (root / "logs" / "bridge-metrics").iterdir()
    assert path.name == datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".jsonl"
    assert (path.stat().st_mode & 0o777) == 0o600
    assert path.read_text().endswith("\n") and len(path.read_text().splitlines()) == 1
    assert len(path.read_bytes()) < 1024


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


def test_prune_removes_only_old_dated_jsonl(tmp_path):
    root = _root(tmp_path)
    directory = root / "logs" / "bridge-metrics"
    directory.mkdir()
    today = date(2026, 11, 17)
    old = (today - timedelta(days=14)).isoformat() + ".jsonl"
    edge = (today - timedelta(days=13)).isoformat() + ".jsonl"
    for name in (old, edge, today.isoformat() + ".jsonl", "2026-01-01.jsonl",
                 "notes.txt", "2026-01-01.jsonl.bak", "2026-13-45.jsonl"):
        (directory / name).write_text("x\n")
    (directory / "sub.jsonl").mkdir()
    outside = tmp_path / "outside.jsonl"
    outside.write_text("keep")
    (directory / "2025-01-01.jsonl").symlink_to(outside)
    bridge_observe.prune(directory, today)
    remaining = sorted(p.name for p in directory.iterdir())
    assert old not in remaining and "2026-01-01.jsonl" not in remaining
    assert edge in remaining and today.isoformat() + ".jsonl" in remaining
    assert "notes.txt" in remaining and "2026-01-01.jsonl.bak" in remaining
    assert "2026-13-45.jsonl" in remaining and "sub.jsonl" in remaining
    assert outside.read_text() == "keep"


def test_prune_runs_when_the_day_changes(monkeypatch, tmp_path):
    root = _root(tmp_path)
    directory = root / "logs" / "bridge-metrics"
    directory.mkdir()
    (directory / "2020-01-01.jsonl").write_text("x\n")
    bridge_observe.configure(root, "cli")
    _patch_get(monkeypatch, _response(f"{URL}/health", headers=_headers()))
    bridge_observe.bridge_get(f"{URL}/health")
    assert not (directory / "2020-01-01.jsonl").exists()


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
