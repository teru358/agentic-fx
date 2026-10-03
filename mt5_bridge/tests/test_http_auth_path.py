"""実際の ASGI 経路 (ヘッダ読み取り・本文・status) で認証の挙動を固定する。"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import server

_DENIED_BODY = {"detail": "発注系は API キーが必要"}


class _Client:
    is_connected = True

    def ping(self):
        return True

    def get_symbols(self):
        return ["USDJPY"]

    def place_order_dry_run(self, **kw):
        return {
            "ticket": 1, "symbol": kw["symbol"], "side": kw["side"],
            "volume_lots": kw["volume_lots"], "fill_price": 1.0, "sl": None,
            "tp": None, "time": "2026-01-01T00:00:00Z", "dry_run": True, "magic": 0,
        }


class _Runtime:
    dry_run = True
    soft_halted = False
    is_hard_halted = False
    accepts_new_orders = True

    def soft_halt(self, reason=""):
        pass

    def hard_halt(self, reason=""):
        pass

    def resume(self):
        return True, "ok"


def _call(method: str, path: str, headers: dict | None = None, body: dict | None = None):
    raw = json.dumps(body).encode() if body is not None else b""
    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if body is not None:
        hdrs.append((b"content-type", b"application/json"))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": hdrs, "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 8812), "root_path": "",
    }
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    async def send(msg):
        sent.append(msg)

    asyncio.run(server.app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    data = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(data)


def _configure(monkeypatch, api_key: str):
    monkeypatch.setattr(
        server, "_settings",
        SimpleNamespace(auth_required=bool(api_key), api_key=api_key, mt5_server="s", mt5_login=1),
    )
    monkeypatch.setattr(server, "_client", _Client())
    monkeypatch.setattr(server, "_runtime", _Runtime())


_ORDER_BODY = {"symbol": "USDJPY", "side": "buy", "volume_lots": 0.1}
_ORDER_CALLS = [
    ("POST", "/order", _ORDER_BODY),
    ("POST", "/positions/1/modify", {"sl": 1.0}),
    ("POST", "/positions/1/close", None),
    ("POST", "/admin/halt", {"mode": "soft", "reason": "t"}),
    ("POST", "/admin/resume", None),
]


@pytest.mark.parametrize("method,path,body", _ORDER_CALLS)
def test_order_endpoint_without_server_key_is_403_over_http(monkeypatch, method, path, body):
    _configure(monkeypatch, "")
    assert _call(method, path, {"X-Bridge-Api-Key": "anything"}, body) == (403, _DENIED_BODY)


@pytest.mark.parametrize("method,path,body", _ORDER_CALLS)
def test_order_endpoint_with_wrong_or_missing_header_is_401_over_http(monkeypatch, method, path, body):
    _configure(monkeypatch, "secret")
    assert _call(method, path, {"X-Bridge-Api-Key": "wrong"}, body)[0] == 401
    assert _call(method, path, {"X-Bridge-Api-Key": "secre"}, body)[0] == 401
    assert _call(method, path, None, body)[0] == 401


def test_order_with_correct_header_reaches_handler_over_http(monkeypatch):
    _configure(monkeypatch, "secret")
    status, data = _call("POST", "/order", {"X-Bridge-Api-Key": "secret"}, _ORDER_BODY)
    assert status == 200
    assert data["symbol"] == "USDJPY"


def test_health_is_open_with_and_without_server_key_over_http(monkeypatch):
    for key in ("", "secret"):
        _configure(monkeypatch, key)
        status, data = _call("GET", "/health")
        assert status == 200 and data["status"] == "ok"


def test_read_endpoint_keeps_optional_key_over_http(monkeypatch):
    _configure(monkeypatch, "")
    assert _call("GET", "/symbols") == (200, ["USDJPY"])
    _configure(monkeypatch, "secret")
    assert _call("GET", "/symbols")[0] == 401
    assert _call("GET", "/symbols", {"X-Bridge-Api-Key": "secret"}) == (200, ["USDJPY"])


def test_main_passes_configured_host_to_uvicorn(monkeypatch):
    import uvicorn

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
    monkeypatch.setattr(server, "load_settings", lambda: SimpleNamespace(host="127.0.0.1", port=8812))
    server.main()
    assert seen["host"] == "127.0.0.1" and seen["port"] == 8812
