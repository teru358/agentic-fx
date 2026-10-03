from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import server
from config import load_settings

_ENV_KEYS = ("BRIDGE_HOST", "BRIDGE_API_KEY", "DRY_RUN", "BRIDGE_PORT")


@pytest.fixture
def env(monkeypatch, tmp_path):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MT5_LOGIN", "1")
    monkeypatch.setenv("MT5_PASSWORD", "x")
    monkeypatch.setenv("MT5_SERVER", "s")
    return tmp_path / "absent.env"


def test_default_host_is_loopback(env):
    assert load_settings(env).host == "127.0.0.1"


def test_non_loopback_host_without_key_is_refused(env, monkeypatch):
    monkeypatch.setenv("BRIDGE_HOST", "0.0.0.0")
    with pytest.raises(ValueError, match="BRIDGE_API_KEY"):
        load_settings(env)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "localhost"])
def test_loopback_host_without_key_is_allowed(env, monkeypatch, host):
    monkeypatch.setenv("BRIDGE_HOST", host)
    assert load_settings(env).host == host


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "example.test"])
def test_non_loopback_host_with_key_is_allowed(env, monkeypatch, host):
    monkeypatch.setenv("BRIDGE_HOST", host)
    monkeypatch.setenv("BRIDGE_API_KEY", "k")
    assert load_settings(env).host == host


def test_order_dependency_refuses_without_key(monkeypatch):
    monkeypatch.setattr(server, "_settings", SimpleNamespace(auth_required=False, api_key=""))
    with pytest.raises(HTTPException) as e:
        server.require_order_api_key(None)
    assert e.value.status_code == 403
    assert e.value.detail == "発注系は API キーが必要"


def test_order_dependency_checks_key_when_set(monkeypatch):
    monkeypatch.setattr(server, "_settings", SimpleNamespace(auth_required=True, api_key="k"))
    server.require_order_api_key("k")
    with pytest.raises(HTTPException) as e:
        server.require_order_api_key("bad")
    assert e.value.status_code == 401


def _route_deps(method: str, path: str) -> set:
    for r in server.app.routes:
        if getattr(r, "path", None) == path and method in getattr(r, "methods", ()):
            return {d.call for d in r.dependant.dependencies}
    raise AssertionError(f"route not found: {method} {path}")


_ORDER_ROUTES = [
    ("POST", "/order"),
    ("POST", "/positions/{ticket}/modify"),
    ("POST", "/positions/{ticket}/close"),
    ("POST", "/admin/halt"),
    ("POST", "/admin/resume"),
]

_PRICE_ROUTES = [
    ("GET", "/quote/{symbol}"),
    ("GET", "/ohlcv/{symbol}"),
    ("GET", "/positions"),
    ("GET", "/account"),
    ("GET", "/symbols"),
    ("GET", "/server-time"),
    ("GET", "/admin/status"),
]


@pytest.mark.parametrize("method,path", _ORDER_ROUTES)
def test_order_routes_use_order_key_dependency(method, path):
    deps = _route_deps(method, path)
    assert server.require_order_api_key in deps
    assert server.require_api_key not in deps


@pytest.mark.parametrize("method,path", _PRICE_ROUTES)
def test_read_routes_keep_optional_key_dependency(method, path):
    deps = _route_deps(method, path)
    assert server.require_api_key in deps
    assert server.require_order_api_key not in deps


def test_health_has_no_auth_dependency():
    assert _route_deps("GET", "/health") == set()


def test_read_dependency_allows_no_key_when_unset(monkeypatch):
    monkeypatch.setattr(server, "_settings", SimpleNamespace(auth_required=False, api_key=""))
    server.require_api_key(None)


@pytest.mark.parametrize("host", ["LOCALHOST", " 127.0.0.1 ", "[::1]", "::ffff:127.0.0.1"])
def test_loopback_spellings_are_recognised(host):
    from config import is_loopback_host

    assert is_loopback_host(host)


@pytest.mark.parametrize("host", ["", "   ", "example.test", "0.0.0.0", "::", "8.8.8.8"])
def test_unparseable_or_non_loopback_host_is_not_loopback(host):
    from config import is_loopback_host

    assert not is_loopback_host(host)


@pytest.mark.parametrize("host", ["example.test", "not an address"])
def test_hostname_without_key_is_refused(env, monkeypatch, host):
    monkeypatch.setenv("BRIDGE_HOST", host)
    with pytest.raises(ValueError, match="BRIDGE_API_KEY"):
        load_settings(env)


def test_whitespace_only_key_counts_as_unset(env, monkeypatch):
    monkeypatch.setenv("BRIDGE_HOST", "0.0.0.0")
    monkeypatch.setenv("BRIDGE_API_KEY", "   ")
    with pytest.raises(ValueError, match="BRIDGE_API_KEY"):
        load_settings(env)


def test_auth_required_follows_key_presence(env, monkeypatch):
    assert load_settings(env).auth_required is False
    monkeypatch.setenv("BRIDGE_API_KEY", "k")
    assert load_settings(env).auth_required is True


def test_order_dependency_refuses_before_settings_loaded(monkeypatch):
    monkeypatch.setattr(server, "_settings", None)
    with pytest.raises(HTTPException) as e:
        server.require_order_api_key("anything")
    assert e.value.status_code == 403
