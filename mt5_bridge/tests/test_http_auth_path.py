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


def _call(method: str, path: str, headers: dict | None = None, body: dict | None = None,
          client=("127.0.0.1", 1)):
    raw = json.dumps(body).encode() if body is not None else b""
    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if body is not None:
        hdrs.append((b"content-type", b"application/json"))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": hdrs, "client": client,
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


_LOCAL_ONLY_BODY = {"detail": server.LOCAL_ONLY_DETAIL}
_ALL_CALLS = _ORDER_CALLS + [
    ("GET", "/health", None),
    ("GET", "/symbols", None),
    ("GET", "/quote/USDJPY", None),
    ("GET", "/admin/status", None),
]


@pytest.mark.parametrize("client", [("192.168.1.5", 4000), ("10.0.0.2", 1), ("::2", 1), ("localhost", 1), None])
@pytest.mark.parametrize("method,path,body", _ALL_CALLS)
def test_keyless_bridge_refuses_non_loopback_client_on_every_endpoint(monkeypatch, client, method, path, body):
    _configure(monkeypatch, "")
    assert _call(method, path, None, body, client=client) == (403, _LOCAL_ONLY_BODY)


@pytest.mark.parametrize("client", [("127.0.0.1", 1), ("::1", 1), ("::ffff:127.0.0.1", 1)])
def test_keyless_bridge_serves_price_and_health_to_loopback_client(monkeypatch, client):
    _configure(monkeypatch, "")
    assert _call("GET", "/symbols", client=client) == (200, ["USDJPY"])
    assert _call("GET", "/health", client=client)[0] == 200


def test_keyed_bridge_accepts_remote_client_with_correct_key(monkeypatch):
    _configure(monkeypatch, "secret")
    remote = ("192.168.1.5", 4000)
    assert _call("GET", "/symbols", {"X-Bridge-Api-Key": "secret"}, client=remote) == (200, ["USDJPY"])
    assert _call("GET", "/symbols", client=remote)[0] == 401
    assert _call("GET", "/health", client=remote)[0] == 200


def test_key_comparison_goes_through_compare_digest(monkeypatch):
    import secrets

    seen = []
    real = secrets.compare_digest
    monkeypatch.setattr(secrets, "compare_digest", lambda a, b: seen.append((a, b)) or real(a, b))
    _configure(monkeypatch, "secret")
    assert _call("GET", "/symbols", {"X-Bridge-Api-Key": "secret"})[0] == 200
    assert seen == [(b"secret", b"secret")]


# 認証の分類。新しい POST route を足したら、どちらかに入れなければ下のテストが落ちる。
_POST_WITHOUT_ORDER_AUTH: set[str] = set()


def test_every_post_route_is_behind_order_auth_or_explicitly_classified():
    unprotected = set()
    for r in server.app.routes:
        if "POST" not in getattr(r, "methods", ()):
            continue
        deps = {d.call for d in r.dependant.dependencies}
        if server.require_order_api_key not in deps:
            unprotected.add(r.path)
    assert unprotected == _POST_WITHOUT_ORDER_AUTH


def test_order_routes_are_registered_through_the_order_router():
    router_paths = {r.path for r in server.order_router.routes}
    assert router_paths == {
        "/order", "/positions/{ticket}/modify", "/positions/{ticket}/close",
        "/admin/halt", "/admin/resume",
    }


_DOC_PATHS = ["/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect", "/no-such-path"]


@pytest.mark.parametrize("path", _DOC_PATHS)
def test_keyless_bridge_refuses_remote_client_on_doc_and_unknown_paths(monkeypatch, path):
    _configure(monkeypatch, "")
    assert _call("GET", path, client=("192.168.1.5", 4000)) == (403, _LOCAL_ONLY_BODY)


@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/redoc"])
def test_doc_endpoints_are_not_served_even_to_loopback(monkeypatch, path):
    _configure(monkeypatch, "")
    assert _call("GET", path)[0] == 404


@pytest.mark.parametrize("path", ["/symbols", "/health", "/openapi.json"])
def test_forwarded_header_does_not_make_remote_client_local(monkeypatch, path):
    _configure(monkeypatch, "")
    status, data = _call(
        "GET", path, {"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1", "Forwarded": "for=127.0.0.1"},
        client=("192.168.1.5", 4000),
    )
    assert (status, data) == (403, _LOCAL_ONLY_BODY)


def test_main_disables_proxy_headers(monkeypatch):
    import uvicorn

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
    monkeypatch.setattr(server, "load_settings", lambda: SimpleNamespace(host="127.0.0.1", port=8812))
    server.main()
    assert seen["proxy_headers"] is False
