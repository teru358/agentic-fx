"""bridge の受動計測 (lock の待ち・占有、応答 header、履歴専用 slot) のテスト。

body・detail・status は変えず、計測は response header だけで運ぶ。
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import bridge_metrics
import mt5_client
import server
from mt5_client import Mt5Client, TimeRuleUnavailableError

_BRIDGE_DIR = Path(__file__).resolve().parent.parent
_OPEN_NOW = datetime(2026, 6, 30, 12, tzinfo=timezone.utc)
_FROM = "2026-06-30T00:00:00Z"
_TO = "2026-06-30T06:00:00Z"
_OHLCV_QUERY = f"from={_FROM}&to={_TO}&interval=1h"


# ── 部品 ──────────────────────────────────────────────────────────

def _call(method, path, headers=None, query="", client=("127.0.0.1", 1), send_raises=False):
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
        if send_raises:
            raise OSError("client went away")
        sent.append(msg)

    asyncio.run(server.app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    hdr = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    return start["status"], hdr, body


def _h(headers, name):
    return headers.get(name.lower())


class _FakeMt5:
    TIMEFRAME_H1 = 16385

    def __init__(self):
        self.copy_calls = 0
        self.select_ok = True
        self.tick = SimpleNamespace(time=1700000000, bid=1.0, ask=1.1)
        self.rates = [{"time": 1700000000, "open": 1.0, "high": 1.0, "low": 1.0,
                       "close": 1.0, "tick_volume": 1}]
        self.entered = threading.Event()
        self.release: threading.Event | None = None
        self.symbols = [SimpleNamespace(name="USDJPY")]

    def last_error(self):
        return (1, "fake")

    def symbol_select(self, symbol, enable):
        return self.select_ok

    def symbol_info_tick(self, symbol):
        return self.tick

    def symbol_info(self, symbol):
        return SimpleNamespace(spread=1)

    def symbols_get(self):
        return self.symbols

    def terminal_info(self):
        return object()

    def copy_rates_range(self, symbol, tf, a, b):
        self.copy_calls += 1
        self.entered.set()
        if self.release is not None:
            assert self.release.wait(10)
        return self.rates


def _real_client(mt5=None) -> Mt5Client:
    """`__new__` で組み立てた実 client。時刻規則は検査済みの状態に仕込む。"""
    client = Mt5Client.__new__(Mt5Client)
    client._lock = threading.Lock()
    client._mt5 = mt5 or _FakeMt5()
    client._connected = True
    client._now = lambda: _OPEN_NOW
    stamp = _OPEN_NOW.timestamp()
    client._last_measure_attempt_at = stamp
    client._measured_at = stamp
    client._measured_offset_sec = 10800
    client._match_streak = 3
    return client


def _configure(monkeypatch, client, api_key=""):
    monkeypatch.setattr(
        server, "_settings",
        SimpleNamespace(auth_required=bool(api_key), api_key=api_key,
                        mt5_server="s", mt5_login=1),
    )
    monkeypatch.setattr(server, "_client", client)
    monkeypatch.setattr(server, "_runtime", SimpleNamespace(
        dry_run=True, soft_halted=False, is_hard_halted=False, accepts_new_orders=True,
    ))
    monkeypatch.setattr(bridge_metrics, "history_slot", bridge_metrics.HistorySlot())


# ── lock の計測 ───────────────────────────────────────────────────

def _fake_clock(monkeypatch, values):
    it = iter(values)
    monkeypatch.setattr(bridge_metrics, "_clock", lambda: next(it))


def test_timed_lock_records_wait_and_hold_per_acquisition(monkeypatch):
    client = _real_client()
    metrics = bridge_metrics.RequestMetrics()
    token = bridge_metrics.bind_metrics(metrics)
    try:
        _fake_clock(monkeypatch, [0.0, 0.5, 0.75, 1.0, 1.0, 4.0])
        with client._timed_lock():
            pass
        # 2 回目: 待ち 0.25、占有 3.0
        _fake_clock(monkeypatch, [10.0, 10.25, 13.25])
        with client._timed_lock():
            pass
    finally:
        bridge_metrics.unbind_metrics(token)
    assert metrics.acquisitions == 2
    assert metrics.wait_total == pytest.approx(0.5 + 0.25)
    assert metrics.hold_total == pytest.approx(0.25 + 3.0)
    assert metrics.wait_max == pytest.approx(0.5)
    assert metrics.hold_max == pytest.approx(3.0)


def test_timed_lock_outside_a_request_records_nothing_and_still_locks():
    client = _real_client()
    with client._timed_lock():
        assert client._lock.locked()
    assert not client._lock.locked()
    assert bridge_metrics.current_metrics() is None


def test_timed_lock_does_not_depend_on_the_lock_type():
    entered = []

    class _Gate:
        def __enter__(self):
            entered.append("in")

        def __exit__(self, *exc):
            entered.append("out")
            return False

    client = _real_client()
    client._lock = _Gate()
    metrics = bridge_metrics.RequestMetrics()
    token = bridge_metrics.bind_metrics(metrics)
    try:
        with client._timed_lock():
            pass
    finally:
        bridge_metrics.unbind_metrics(token)
    assert entered == ["in", "out"] and metrics.acquisitions == 1


def test_timed_lock_records_and_releases_when_the_body_raises():
    client = _real_client()
    metrics = bridge_metrics.RequestMetrics()
    token = bridge_metrics.bind_metrics(metrics)
    try:
        with pytest.raises(RuntimeError):
            with client._timed_lock():
                raise RuntimeError("boom")
    finally:
        bridge_metrics.unbind_metrics(token)
    assert metrics.acquisitions == 1 and not metrics.in_lock
    assert not client._lock.locked()


def test_timed_lock_measures_real_contention():
    client = _real_client()
    holding = threading.Event()
    release = threading.Event()

    def holder():
        with client._lock:
            holding.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    assert holding.wait(5)
    metrics = bridge_metrics.RequestMetrics()
    token = bridge_metrics.bind_metrics(metrics)
    threading.Timer(0.08, release.set).start()
    try:
        with client._timed_lock():
            pass
    finally:
        bridge_metrics.unbind_metrics(token)
        t.join(5)
    assert metrics.wait_total >= 0.05


def test_every_lock_site_in_the_client_goes_through_the_timed_helper():
    source = (_BRIDGE_DIR / "mt5_client.py").read_text(encoding="utf-8")
    assert "with self._lock" not in source
    assert source.count("self._timed_lock()") >= 18


def test_pool_thread_reuse_does_not_leak_a_requests_metrics():
    client = _real_client()
    pool = __import__("concurrent.futures", fromlist=["ThreadPoolExecutor"]).ThreadPoolExecutor(1)
    try:
        for _ in range(2):
            metrics = bridge_metrics.RequestMetrics()
            token = bridge_metrics.bind_metrics(metrics)
            try:
                ctx = contextvars.copy_context()
                pool.submit(ctx.run, lambda: client.ping()).result()
            finally:
                bridge_metrics.unbind_metrics(token)
            assert metrics.acquisitions == 1   # 同じ thread でも要求ごとに 1
        # context を複製せずに使い回された thread へは何も漏れない
        leak = bridge_metrics.RequestMetrics()
        token = bridge_metrics.bind_metrics(leak)
        try:
            pool.submit(lambda: client.ping()).result()
        finally:
            bridge_metrics.unbind_metrics(token)
        assert leak.acquisitions == 0
    finally:
        pool.shutdown()


def test_concurrent_requests_report_their_own_counts(monkeypatch):
    _configure(monkeypatch, _real_client())
    results: list[str | None] = []

    def one():
        _, hdr, _ = _call("GET", "/symbols")
        results.append(_h(hdr, "X-Bridge-Lock-Acquisitions"))

    threads = [threading.Thread(target=one) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert results == ["1"] * 6


# ── 応答 header ───────────────────────────────────────────────────

_BASE = ("X-Bridge-Metrics-Version", "X-Bridge-Boot-Id", "X-Bridge-Lock-State",
         "X-Bridge-Lock-Wait-Ms", "X-Bridge-Lock-Hold-Ms", "X-Bridge-Lock-Acquisitions",
         "X-Bridge-Lock-Wait-Max-Ms", "X-Bridge-Lock-Hold-Max-Ms")


def _assert_base(hdr):
    for name in _BASE:
        assert _h(hdr, name) is not None, name
    assert _h(hdr, "X-Bridge-Metrics-Version") == "1"
    assert _h(hdr, "X-Bridge-Boot-Id") == bridge_metrics.BOOT_ID


def test_success_response_carries_measured_lock_headers(monkeypatch):
    _configure(monkeypatch, _real_client())
    status, hdr, body = _call("GET", "/symbols")
    assert status == 200 and json.loads(body) == ["USDJPY"]
    _assert_base(hdr)
    assert _h(hdr, "X-Bridge-Lock-State") == "measured"
    assert _h(hdr, "X-Bridge-Lock-Acquisitions") == "1"
    assert float(_h(hdr, "X-Bridge-Lock-Hold-Ms")) >= 0.0
    assert _h(hdr, "X-Bridge-Error-Code") is None


def test_a_response_without_lock_use_is_not_locked(monkeypatch):
    _configure(monkeypatch, _real_client())
    status, hdr, _ = _call("GET", "/no-such-path")
    assert status == 404
    assert _h(hdr, "X-Bridge-Lock-State") == "not_locked"
    assert _h(hdr, "X-Bridge-Lock-Acquisitions") == "0"


def test_boot_id_is_opaque_and_stable_within_the_process():
    assert re.fullmatch(r"[0-9a-f]{16}", bridge_metrics.BOOT_ID)
    assert bridge_metrics.BOOT_ID == bridge_metrics.BOOT_ID


def test_boot_id_differs_between_processes():
    code = "import bridge_metrics; print(bridge_metrics.BOOT_ID)"
    outs = {
        subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(_BRIDGE_DIR)!r}); {code}"],
                       capture_output=True, text=True, check=True, timeout=60).stdout.strip()
        for _ in range(2)
    }
    assert len(outs) == 2


class _ErrClient:
    """handler ごとに決めた例外を投げる fake client。"""
    is_connected = True

    def __init__(self, exc=None, closed=None):
        self._exc = exc
        self._closed = closed

    def ping(self):
        return True

    def get_symbols(self):
        if self._exc:
            raise self._exc
        return ["USDJPY"]

    def get_closed_deal(self, ticket):
        return self._closed

    def get_quote(self, symbol):
        raise self._exc

    def copy_rates_range(self, *a):
        raise self._exc


def test_unmatched_and_malformed_requests_carry_headers_and_keep_bodies(monkeypatch):
    _configure(monkeypatch, _ErrClient())
    status, hdr, body = _call("GET", "/no-such-path")
    assert (status, body) == (404, b'{"detail":"Not Found"}')
    _assert_base(hdr)
    assert _h(hdr, "X-Bridge-Error-Code") == "route_not_found"

    status, hdr, _ = _call("POST", "/symbols")
    assert status == 405
    _assert_base(hdr)
    assert _h(hdr, "X-Bridge-Error-Code") == "method_not_allowed"

    status, hdr, _ = _call("GET", "/positions/abc/closed-deal")
    assert status == 422
    _assert_base(hdr)
    assert _h(hdr, "X-Bridge-Error-Code") == "request_validation_failed"


def test_http_exception_keeps_its_detail_and_gains_a_code(monkeypatch):
    _configure(monkeypatch, _ErrClient(closed=None))
    status, hdr, body = _call("GET", "/positions/7/closed-deal")
    assert (status, json.loads(body)) == (404, {"detail": "closed deal not found for ticket=7"})
    _assert_base(hdr)
    assert _h(hdr, "X-Bridge-Error-Code") == "closed_deal_not_found"


def test_rejections_401_and_403_carry_headers_but_no_slot_detail(monkeypatch):
    _configure(monkeypatch, _ErrClient(), api_key="k-1")
    slot = bridge_metrics.history_slot
    entry = slot.admit("req-1", bridge_metrics.RequestMetrics())[1]
    status, hdr, body = _call("GET", "/symbols", {"X-Bridge-Api-Key": "wrong"})
    assert (status, json.loads(body)) == (401, {"detail": "invalid or missing X-Bridge-Api-Key"})
    _assert_base(hdr)
    assert _h(hdr, "X-Bridge-Error-Code") == "auth_invalid"
    assert not any(k.startswith("x-bridge-history") for k in hdr)

    _configure(monkeypatch, _ErrClient(), api_key="")
    status, hdr, body = _call("POST", "/order", {"X-Bridge-Api-Key": "x"})
    assert status == 403
    _assert_base(hdr)
    assert _h(hdr, "X-Bridge-Error-Code") == "order_key_required"
    assert not any(k.startswith("x-bridge-history") for k in hdr)

    status, hdr, body = _call("GET", "/symbols", client=("192.168.1.9", 1))
    assert (status, json.loads(body)) == (403, {"detail": server.LOCAL_ONLY_DETAIL})
    _assert_base(hdr)
    assert _h(hdr, "X-Bridge-Error-Code") == "local_only_forbidden"
    assert not any(k.startswith("x-bridge-history") for k in hdr)
    assert entry is not None


def test_unexpected_exception_response_still_carries_headers(monkeypatch):
    _configure(monkeypatch, _ErrClient(exc=ZeroDivisionError("x")))
    with pytest.raises(ZeroDivisionError):
        _call("GET", "/symbols")
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "GET", "scheme": "http", "path": "/symbols", "raw_path": b"/symbols",
             "query_string": b"", "headers": [], "client": ("127.0.0.1", 1),
             "server": ("127.0.0.1", 8812), "root_path": ""}
    with pytest.raises(ZeroDivisionError):
        asyncio.run(server.app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    hdr = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    assert start["status"] == 500
    _assert_base(hdr)
    assert _h(hdr, "X-Bridge-Error-Code") == "internal_error"


def test_a_measurement_fault_becomes_unavailable_without_touching_the_response(monkeypatch):
    _configure(monkeypatch, _real_client())
    monkeypatch.setattr(bridge_metrics, "lock_headers",
                        lambda m: (_ for _ in ()).throw(RuntimeError("fault")))
    status, hdr, body = _call("GET", "/symbols")
    assert (status, json.loads(body)) == (200, ["USDJPY"])
    assert _h(hdr, "X-Bridge-Lock-State") == "unavailable"
    assert _h(hdr, "X-Bridge-Lock-Wait-Ms") is None
    assert _h(hdr, "X-Bridge-Boot-Id") == bridge_metrics.BOOT_ID


def test_health_response_carries_headers_and_its_body_has_no_new_field(monkeypatch):
    _configure(monkeypatch, _real_client())
    status, hdr, body = _call("GET", "/health")
    assert status == 200
    assert set(json.loads(body)) == {
        "status", "mt5_connected", "dry_run", "server", "login",
        "time_rule_state", "measured_at"}
    _assert_base(hdr)


# ── 固定 error code ───────────────────────────────────────────────

def test_client_failures_map_to_fixed_codes_without_changing_details(monkeypatch):
    mt5 = _FakeMt5()
    _configure(monkeypatch, _real_client(mt5))

    mt5.select_ok = False
    status, hdr, body = _call("GET", "/quote/USDJPY")
    assert (status, json.loads(body)["detail"]) == (404, "symbol_select(USDJPY) failed: (1, 'fake')")
    assert _h(hdr, "X-Bridge-Error-Code") == "symbol_unavailable"
    status, hdr, body = _call("GET", "/ohlcv/USDJPY", query=_OHLCV_QUERY)
    assert (status, json.loads(body)["detail"]) == (404, "symbol_select(USDJPY) failed: (1, 'fake')")
    assert _h(hdr, "X-Bridge-Error-Code") == "symbol_unavailable"

    mt5.select_ok = True
    mt5.rates = None
    status, hdr, body = _call("GET", "/ohlcv/USDJPY", query=_OHLCV_QUERY)
    assert (status, json.loads(body)["detail"]) == (404, "copy_rates_range failed: (1, 'fake')")
    assert _h(hdr, "X-Bridge-Error-Code") == "history_copy_failed"

    mt5.tick = None
    status, hdr, body = _call("GET", "/quote/USDJPY")
    assert (status, json.loads(body)["detail"]) == (404, "symbol_info_tick(USDJPY) failed: (1, 'fake')")
    assert _h(hdr, "X-Bridge-Error-Code") == "tick_unavailable"


@pytest.mark.parametrize("detail,code", [
    ("time_rule_mismatch", "time_rule_mismatch"),
    ("time_rule_unverified", "time_rule_unverified"),
])
def test_time_rule_503_carries_the_state_as_code(monkeypatch, detail, code):
    _configure(monkeypatch, _ErrClient(exc=TimeRuleUnavailableError(detail)))
    for path, query in (("/quote/USDJPY", ""), ("/ohlcv/USDJPY", _OHLCV_QUERY)):
        status, hdr, body = _call("GET", path, query=query)
        assert (status, json.loads(body)) == (503, {"detail": detail})
        assert _h(hdr, "X-Bridge-Error-Code") == code


def test_not_connected_and_input_errors_have_codes(monkeypatch):
    client = _real_client()
    client._connected = False
    _configure(monkeypatch, client)
    status, hdr, body = _call("GET", "/symbols")
    assert (status, json.loads(body)) == (503, {"detail": "MT5 not connected"})
    assert _h(hdr, "X-Bridge-Error-Code") == "mt5_not_connected"

    client._connected = True
    status, hdr, body = _call("GET", "/ohlcv/USDJPY", query="from=garbage&to=x")
    assert status == 400 and json.loads(body)["detail"].startswith("invalid timestamp: ")
    assert _h(hdr, "X-Bridge-Error-Code") == "invalid_timestamp"
    status, hdr, body = _call("GET", "/ohlcv/USDJPY", query=f"from={_TO}&to={_FROM}")
    assert (status, json.loads(body)) == (400, {"detail": "to must be after from"})
    assert _h(hdr, "X-Bridge-Error-Code") == "invalid_range"
    status, hdr, body = _call("GET", "/ohlcv/USDJPY", query=f"from={_FROM}&to={_TO}&interval=2h")
    assert (status, json.loads(body)) == (400, {"detail": "unsupported interval: 2h"})
    assert _h(hdr, "X-Bridge-Error-Code") == "invalid_request"


def test_every_http_exception_site_in_the_server_assigns_a_code():
    import ast

    tree = ast.parse((_BRIDGE_DIR / "server.py").read_text(encoding="utf-8"))
    missing = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "HTTPException"):
            if not any(kw.arg == "headers" for kw in node.keywords):
                missing.append(node.lineno)
    assert missing == []


# ── 履歴専用 slot ─────────────────────────────────────────────────

def _history(rid, query=_OHLCV_QUERY, extra=None, **kw):
    headers = {"X-Bridge-History-Request-Id": rid, **(extra or {})}
    return _call("GET", "/ohlcv/USDJPY", headers, query, **kw)


def test_history_request_is_tracked_and_reported_completed(monkeypatch):
    mt5 = _FakeMt5()
    _configure(monkeypatch, _real_client(mt5))
    status, hdr, _ = _history("req-1")
    assert status == 200
    assert _h(hdr, "X-Bridge-History-Request-Id") == "req-1"
    assert _h(hdr, "X-Bridge-History-State") == "completed"
    assert float(_h(hdr, "X-Bridge-History-Wait-Ms")) >= 0.0
    assert float(_h(hdr, "X-Bridge-History-Hold-Ms")) >= 0.0
    # 同じ slot を、別の既存応答が lock なしの観測で運ぶ
    _, hdr2, _ = _call("GET", "/symbols")
    assert _h(hdr2, "X-Bridge-History-Request-Id") == "req-1"
    assert _h(hdr2, "X-Bridge-History-State") == "completed"


def test_slot_state_is_none_before_any_history_request(monkeypatch):
    _configure(monkeypatch, _real_client())
    _, hdr, _ = _call("GET", "/symbols")
    assert _h(hdr, "X-Bridge-History-State") == "none"
    assert _h(hdr, "X-Bridge-History-Request-Id") is None
    assert _h(hdr, "X-Bridge-History-Wait-Ms") is None


def test_plain_requests_do_not_overwrite_the_slot(monkeypatch):
    _configure(monkeypatch, _real_client())
    _history("req-1")
    for _ in range(3):
        _call("GET", "/symbols")
        _call("GET", "/ohlcv/USDJPY", query=_OHLCV_QUERY)   # header の無い履歴取得
    _, hdr, _ = _call("GET", "/health")
    assert _h(hdr, "X-Bridge-History-Request-Id") == "req-1"
    assert _h(hdr, "X-Bridge-History-State") == "completed"


def test_same_id_resend_does_not_run_native_again(monkeypatch):
    mt5 = _FakeMt5()
    _configure(monkeypatch, _real_client(mt5))
    assert _history("req-1")[0] == 200
    status, hdr, body = _history("req-1")
    assert status == 409
    assert _h(hdr, "X-Bridge-Error-Code") == "history_request_duplicate"
    assert mt5.copy_calls == 1


def test_slot_follows_waiting_holding_completed_and_blocks_other_ids(monkeypatch):
    mt5 = _FakeMt5()
    mt5.release = threading.Event()
    client = _real_client(mt5)
    _configure(monkeypatch, client)
    out: dict = {}

    t = threading.Thread(target=lambda: out.update(first=_history("req-1")))
    t.start()
    assert mt5.entered.wait(10)

    # native の最中は holding。別 ID は native の前に拒否され、本文は新しい固定文言
    snap = bridge_metrics.history_slot.snapshot()
    assert dict(snap)["X-Bridge-History-State"] == "holding"
    assert dict(snap)["X-Bridge-History-Request-Id"] == "req-1"
    status, hdr, body = _history("req-2")
    assert status == 409
    assert _h(hdr, "X-Bridge-Error-Code") == "history_request_busy"
    assert mt5.copy_calls == 1
    # 同 ID の再送も native を重複実行しない
    assert _history("req-1")[0] == 409
    assert mt5.copy_calls == 1

    mt5.release.set()
    t.join(10)
    status, hdr, _ = out["first"]
    assert status == 200 and _h(hdr, "X-Bridge-History-State") == "completed"

    # 完了後は別 ID を受け付ける
    mt5.release = None
    status, hdr, _ = _history("req-2")
    assert status == 200 and _h(hdr, "X-Bridge-History-Request-Id") == "req-2"
    assert mt5.copy_calls == 2


def test_slot_reads_waiting_while_the_lock_is_held_by_someone_else(monkeypatch):
    mt5 = _FakeMt5()
    client = _real_client(mt5)
    _configure(monkeypatch, client)
    out: dict = {}
    client._lock.acquire()
    t = threading.Thread(target=lambda: out.update(r=_history("req-w")))
    t.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = dict(bridge_metrics.history_slot.snapshot()).get("X-Bridge-History-State")
        if state == "waiting":
            break
        time.sleep(0.01)
    try:
        assert state == "waiting"
    finally:
        client._lock.release()
        t.join(10)
    assert out["r"][0] == 200
    assert dict(bridge_metrics.history_slot.snapshot())["X-Bridge-History-State"] == "completed"


def test_completion_is_after_native_failure_too(monkeypatch):
    mt5 = _FakeMt5()
    mt5.rates = None
    _configure(monkeypatch, _real_client(mt5))
    status, hdr, _ = _history("req-1")
    assert status == 404
    assert _h(hdr, "X-Bridge-History-State") == "completed"
    assert _h(hdr, "X-Bridge-Error-Code") == "history_copy_failed"


def test_disconnect_does_not_drop_the_tracking(monkeypatch):
    mt5 = _FakeMt5()
    mt5.release = threading.Event()
    _configure(monkeypatch, _real_client(mt5))

    def gone():
        try:
            _history("req-1", send_raises=True)
        except OSError:
            pass

    t = threading.Thread(target=gone)
    t.start()
    assert mt5.entered.wait(10)
    assert dict(bridge_metrics.history_slot.snapshot())["X-Bridge-History-State"] == "holding"
    mt5.release.set()
    t.join(10)
    deadline = time.monotonic() + 10
    while (dict(bridge_metrics.history_slot.snapshot())["X-Bridge-History-State"] != "completed"
           and time.monotonic() < deadline):
        time.sleep(0.01)
    snap = dict(bridge_metrics.history_slot.snapshot())
    assert snap["X-Bridge-History-State"] == "completed"
    assert snap["X-Bridge-History-Request-Id"] == "req-1"


def test_invalid_history_request_id_is_rejected_before_native(monkeypatch):
    mt5 = _FakeMt5()
    _configure(monkeypatch, _real_client(mt5))
    status, hdr, _ = _history("bad id with spaces")
    assert status == 400
    assert _h(hdr, "X-Bridge-Error-Code") == "history_request_id_invalid"
    assert mt5.copy_calls == 0
    assert dict(bridge_metrics.history_slot.snapshot())["X-Bridge-History-State"] == "none"


def test_slot_details_ride_authenticated_responses_only(monkeypatch):
    mt5 = _FakeMt5()
    _configure(monkeypatch, _real_client(mt5), api_key="k-1")
    key = {"X-Bridge-Api-Key": "k-1"}
    status, hdr, _ = _call("GET", "/ohlcv/USDJPY", {**key, "X-Bridge-History-Request-Id": "req-1"},
                           _OHLCV_QUERY)
    assert status == 200 and _h(hdr, "X-Bridge-History-State") == "completed"
    # 認証無しの /health には slot の詳細を載せない (計測 header は載る)
    _, hdr, _ = _call("GET", "/health")
    _assert_base(hdr)
    assert not any(k.startswith("x-bridge-history") for k in hdr)
    _, hdr, _ = _call("GET", "/health", key)
    assert _h(hdr, "X-Bridge-History-Request-Id") == "req-1"
    # 認証に失敗した履歴要求は slot を動かさない
    status, _, _ = _call("GET", "/ohlcv/USDJPY",
                         {"X-Bridge-Api-Key": "nope", "X-Bridge-History-Request-Id": "req-9"},
                         _OHLCV_QUERY)
    assert status == 401
    assert dict(bridge_metrics.history_slot.snapshot())["X-Bridge-History-Request-Id"] == "req-1"
    assert mt5.copy_calls == 1


def test_slot_snapshot_does_not_wait_on_any_lock():
    slot = bridge_metrics.HistorySlot()
    slot.admit("req-1", bridge_metrics.RequestMetrics())
    got: list = []
    with slot._mutex:
        t = threading.Thread(target=lambda: got.append(slot.snapshot()))
        t.start()
        t.join(2)
        assert not t.is_alive()
    assert dict(got[0])["X-Bridge-History-State"] == "waiting"


def test_completed_is_visible_only_after_the_finally_that_releases_the_lock(monkeypatch):
    seen: list[str] = []
    mt5 = _FakeMt5()
    client = _real_client(mt5)
    _configure(monkeypatch, client)
    original = bridge_metrics.HistorySlot.complete

    def spy(self, entry):
        seen.append("lock_free" if not client._lock.locked() else "lock_held")
        original(self, entry)

    monkeypatch.setattr(bridge_metrics.HistorySlot, "complete", spy)
    assert _history("req-1")[0] == 200
    assert seen == ["lock_free"]


# ── 単一 process の配備条件 ───────────────────────────────────────

def test_the_documented_launch_is_a_single_process_with_an_app_object(monkeypatch):
    import uvicorn

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(app=app, **kw))
    monkeypatch.setattr(server, "load_settings", lambda: SimpleNamespace(host="127.0.0.1", port=8812))
    server.main()
    assert seen["app"] is server.app
    assert seen.get("workers", 1) == 1
    assert not seen.get("reload", False)


def test_uvicorn_refuses_several_workers_for_an_app_object():
    import uvicorn

    with pytest.raises(SystemExit):
        uvicorn.run(server.app, host="127.0.0.1", port=0, workers=2)


# ── 計測の失敗を業務処理から隔離 ──────────────────────────────────

def _fault_clock(monkeypatch, fail_at):
    """`fail_at` 回目の時計取得で例外にする (1 始まり)。"""
    state = {"n": 0}

    def clock():
        state["n"] += 1
        if state["n"] == fail_at:
            raise RuntimeError("clock fault")
        return float(state["n"])

    monkeypatch.setattr(bridge_metrics, "_clock", clock)


def _break_record(monkeypatch):
    def record(self, wait, hold):
        raise RuntimeError("record fault")

    monkeypatch.setattr(bridge_metrics.RequestMetrics, "record", record)


_FAULTS = {
    "clock_before_acquire": lambda mp: _fault_clock(mp, 1),
    "clock_after_acquire": lambda mp: _fault_clock(mp, 2),
    "clock_at_release": lambda mp: _fault_clock(mp, 3),
    "record": _break_record,
}


@pytest.mark.parametrize("fault", sorted(_FAULTS))
def test_measurement_fault_leaves_the_business_response_byte_identical(monkeypatch, fault):
    _configure(monkeypatch, _real_client())
    baseline = _call("GET", "/symbols")
    _FAULTS[fault](monkeypatch)
    client = server._client
    status, hdr, body = _call("GET", "/symbols")
    assert (status, body) == (baseline[0], baseline[2])
    assert _h(hdr, "X-Bridge-Lock-State") == "unavailable"
    assert not client._lock.locked()


@pytest.mark.parametrize("fault", sorted(_FAULTS))
def test_measurement_fault_keeps_native_exceptions_and_lock_release(monkeypatch, fault):
    client = _real_client()
    _FAULTS[fault](monkeypatch)
    metrics = bridge_metrics.RequestMetrics()
    token = bridge_metrics.bind_metrics(metrics)
    try:
        with pytest.raises(RuntimeError, match="native"):
            with client._timed_lock():
                raise RuntimeError("native")
    finally:
        bridge_metrics.unbind_metrics(token)
    assert not client._lock.locked()
    assert metrics.failed


@pytest.mark.parametrize("fault", sorted(_FAULTS))
def test_measurement_fault_never_leaves_zero_numbers_in_the_history_slot(monkeypatch, fault):
    mt5 = _FakeMt5()
    _configure(monkeypatch, _real_client(mt5))
    _FAULTS[fault](monkeypatch)
    status, hdr, _ = _history("req-1")
    assert status == 200
    assert _h(hdr, "X-Bridge-History-State") == "completed"
    assert _h(hdr, "X-Bridge-History-Wait-Ms") is None
    assert _h(hdr, "X-Bridge-History-Hold-Ms") is None
    snap = dict(bridge_metrics.history_slot.snapshot())
    assert snap["X-Bridge-History-State"] == "completed"
    assert "X-Bridge-History-Hold-Ms" not in snap
    assert mt5.copy_calls == 1


# ── 占有計測の境界 ────────────────────────────────────────────────

def test_hold_includes_time_spent_releasing_the_lock(monkeypatch):
    now = {"t": 0.0}
    monkeypatch.setattr(bridge_metrics, "_clock", lambda: now["t"])

    class _SlowRelease:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            now["t"] += 5.0
            return False

    metrics = bridge_metrics.RequestMetrics()
    token = bridge_metrics.bind_metrics(metrics)
    try:
        with bridge_metrics.timed_lock(_SlowRelease()):
            now["t"] += 1.0
    finally:
        bridge_metrics.unbind_metrics(token)
    assert metrics.hold_total == pytest.approx(6.0)


def test_holding_state_lasts_until_the_lock_is_released(monkeypatch):
    seen = []
    metrics = bridge_metrics.RequestMetrics()

    class _Probe:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            seen.append(metrics.in_lock)
            return False

    token = bridge_metrics.bind_metrics(metrics)
    try:
        with bridge_metrics.timed_lock(_Probe()):
            pass
    finally:
        bridge_metrics.unbind_metrics(token)
    assert seen == [True] and not metrics.in_lock


def test_reentrant_lock_keeps_holding_until_the_outermost_release():
    seen = []
    metrics = bridge_metrics.RequestMetrics()
    lock = threading.RLock()
    token = bridge_metrics.bind_metrics(metrics)
    try:
        with bridge_metrics.timed_lock(lock):
            with bridge_metrics.timed_lock(lock):
                seen.append(metrics.in_lock)
            seen.append(metrics.in_lock)
        seen.append(metrics.in_lock)
    finally:
        bridge_metrics.unbind_metrics(token)
    assert seen == [True, True, False]
    assert metrics.acquisitions == 2


# ── 認証済みの想定外 500 ──────────────────────────────────────────

class _PingBreaks(_ErrClient):
    def ping(self):
        raise ZeroDivisionError("x")


def _unexpected(monkeypatch, headers):
    _configure(monkeypatch, _PingBreaks(exc=ZeroDivisionError("x")), api_key="k-1")
    bridge_metrics.history_slot.admit("req-s", bridge_metrics.RequestMetrics())
    hdrs = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "GET", "scheme": "http", "path": "/health", "raw_path": b"/health",
             "query_string": b"", "headers": hdrs, "client": ("127.0.0.1", 1),
             "server": ("127.0.0.1", 8812), "root_path": ""}
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    with pytest.raises(ZeroDivisionError):
        asyncio.run(server.app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 500
    return {k.decode().lower(): v.decode() for k, v in start["headers"]}


def test_authenticated_unexpected_500_carries_the_slot(monkeypatch):
    hdr = _unexpected(monkeypatch, {"X-Bridge-Api-Key": "k-1"})
    assert _h(hdr, "X-Bridge-History-Request-Id") == "req-s"
    assert _h(hdr, "X-Bridge-History-State") == "waiting"
    assert _h(hdr, "X-Bridge-Error-Code") == "internal_error"


def test_unauthenticated_unexpected_500_has_no_slot(monkeypatch):
    for headers in ({}, {"X-Bridge-Api-Key": "wrong"}):
        hdr = _unexpected(monkeypatch, headers)
        assert not any(k.startswith("x-bridge-history") for k in hdr)
        assert _h(hdr, "X-Bridge-Error-Code") == "internal_error"


# ── native 実行中の切断・取消し ───────────────────────────────────

def _drive_history_with_disconnect(monkeypatch, mt5, *, mode):
    """native を止めたまま、`mode` (disconnect | cancel) を起こす。"""
    result: dict = {}
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "GET", "scheme": "http", "path": "/ohlcv/USDJPY",
             "raw_path": b"/ohlcv/USDJPY", "query_string": _OHLCV_QUERY.encode(),
             "headers": [(b"x-bridge-history-request-id", b"req-1")],
             "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8812), "root_path": ""}

    async def run():
        gone = asyncio.Event()
        sent: list = []

        async def receive():
            if mode == "disconnect":
                await gone.wait()
                return {"type": "http.disconnect"}
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            sent.append(msg)

        task = asyncio.ensure_future(server.app(scope, receive, send))
        loop = asyncio.get_running_loop()
        assert await loop.run_in_executor(None, mt5.entered.wait, 10)
        result["during"] = dict(bridge_metrics.history_slot.snapshot())
        if mode == "disconnect":
            gone.set()
        else:
            task.cancel()
        await asyncio.sleep(0.2)
        result["after_gone"] = dict(bridge_metrics.history_slot.snapshot())
        result["calls_after_gone"] = mt5.copy_calls
        other = await loop.run_in_executor(None, lambda: _history("req-2"))
        same = await loop.run_in_executor(None, lambda: _history("req-1"))
        result["other"], result["same"] = other, same
        result["calls_after_retries"] = mt5.copy_calls
        mt5.release.set()
        try:
            await task
        except BaseException:  # noqa: BLE001
            pass
        for _ in range(200):
            snap = dict(bridge_metrics.history_slot.snapshot())
            if snap["X-Bridge-History-State"] == "completed":
                break
            await asyncio.sleep(0.05)
        result["final"] = snap

    asyncio.run(run())
    return result


@pytest.mark.parametrize("mode", ["disconnect", "cancel"])
def test_disconnect_while_native_runs_keeps_the_slot_open_until_release(monkeypatch, mode):
    mt5 = _FakeMt5()
    mt5.release = threading.Event()
    _configure(monkeypatch, _real_client(mt5))
    r = _drive_history_with_disconnect(monkeypatch, mt5, mode=mode)
    assert r["during"]["X-Bridge-History-State"] == "holding"
    assert r["after_gone"]["X-Bridge-History-State"] == "holding"
    assert r["other"][0] == 409 and _h(r["other"][1], "X-Bridge-Error-Code") == "history_request_busy"
    assert r["same"][0] == 409
    assert r["calls_after_retries"] == 1 == r["calls_after_gone"]
    assert r["final"]["X-Bridge-History-State"] == "completed"
    assert r["final"]["X-Bridge-History-Request-Id"] == "req-1"
    assert mt5.copy_calls == 1
