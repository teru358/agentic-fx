"""bridge の銘柄一覧の取得と、`afx check` / 起動前検査の watch_symbol_unknown。"""
from __future__ import annotations

import json

import httpx
import pytest

import agentic_fx.service as service_mod
from agentic_fx.activity import ActivityLog
from agentic_fx.setup import preflight
from agentic_fx.setup.checks import CheckReason, list_bridge_symbols
from agentic_fx.setup.preflight import Finding, run_check

from tests.setup.test_preflight import clean_initial_env, make_root  # noqa: F401  (autouse fixture)


class Resp:
    def __init__(self, data, status_code=200):
        self._data, self.status_code = data, status_code

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "http://b/")
            raise httpx.HTTPStatusError("x", request=request,
                                        response=httpx.Response(self.status_code, request=request))


def status_error(code):
    request = httpx.Request("GET", "http://b/")
    return httpx.HTTPStatusError("x", request=request, response=httpx.Response(code, request=request))


def listing(fetch, **kw):
    return list_bridge_symbols("http://b/", fetch=fetch, **kw)


def test_the_listing_keeps_plain_names_once_and_drops_the_rest():
    names = ["JP225", "JP225", "US500", "", " X", "A" * 33, 7, None, "bad\nname", "EURUSD.x"]
    result = listing(lambda url, **k: Resp(names))
    assert result.result == "ok" and result.params["symbols"] == ("JP225", "US500", "EURUSD.x")


def test_the_listing_asks_for_symbols_with_the_headers_and_a_timeout():
    seen = []

    def fetch(url, **k):
        seen.append((url, k))
        return Resp([])

    listing(fetch, headers={"X-Bridge-Api-Key": "k"})
    assert seen == [("http://b/symbols", {"timeout": 10, "headers": {"X-Bridge-Api-Key": "k"}})]


@pytest.mark.parametrize("fetch,reason", [
    (lambda url, **k: Resp([], 401), CheckReason.AUTH_REQUIRED),
    (lambda url, **k: Resp([], 403), CheckReason.AUTH_REQUIRED),
    (lambda url, **k: Resp([], 503), CheckReason.CONNECT_ERROR),
    (lambda url, **k: Resp(ValueError("x")), CheckReason.INVALID_RESPONSE),
    (lambda url, **k: Resp({"a": 1}), CheckReason.INVALID_RESPONSE),
    (lambda url, **k: (_ for _ in ()).throw(httpx.ConnectError("refused")), CheckReason.CONNECT_ERROR),
    (lambda url, **k: (_ for _ in ()).throw(httpx.ReadTimeout("slow")), CheckReason.CONNECT_ERROR),
    (lambda url, **k: (_ for _ in ()).throw(status_error(401)), CheckReason.AUTH_REQUIRED),
])
def test_a_failed_listing_is_unverified_with_a_fixed_reason(fetch, reason):
    result = listing(fetch)
    assert result.result == "unverified" and result.reason_code is reason and not result.params


# --- afx check / 起動前検査 -------------------------------------------------------

def watch_root(tmp_path, watch, **extra):
    return make_root(tmp_path, datafeed={"watch_symbols": watch, "primary": "mt5",
                                         "mt5": {"enabled": True, "bridge_url": "http://bridge.invalid:8812"},
                                         "yfinance": {"enabled": False}},
                     **extra)


def serve(monkeypatch, payload=None, *, status=200, error=None):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        if error is not None:
            raise error
        return Resp(payload, status)

    monkeypatch.setattr(httpx, "get", get)
    return calls


def test_an_unknown_watch_symbol_is_a_warning_not_a_fatal(tmp_path, monkeypatch):
    serve(monkeypatch, ["JP225", "US500"])
    report = run_check(watch_root(tmp_path, ["JP225", "NOPE"]))
    assert report.fatal == [] and report.warning == ["watch_symbol_unknown"]
    assert "NOPE" in report.messages["watch_symbol_unknown"]
    assert "JP225" not in report.messages["watch_symbol_unknown"]
    assert report.ok


def test_known_watch_symbols_give_no_warning(tmp_path, monkeypatch):
    serve(monkeypatch, ["JP225", "US500"])
    report = run_check(watch_root(tmp_path, ["JP225"]))
    assert report.fatal == [] and report.warning == []


@pytest.mark.parametrize("kind", ["connect", "auth", "down"])
def test_an_unreachable_bridge_is_unchecked_never_unknown(tmp_path, monkeypatch, kind):
    if kind == "connect":
        serve(monkeypatch, error=httpx.ConnectError("refused"))
    else:
        serve(monkeypatch, [], status=401 if kind == "auth" else 503)
    report = run_check(watch_root(tmp_path, ["NOPE"]))
    assert report.warning == ["watch_symbols_unchecked"] and report.fatal == []
    assert "watch_symbol_unknown" not in report.warning


def test_no_watch_symbols_means_no_request_and_the_same_report(tmp_path, monkeypatch):
    calls = serve(monkeypatch, error=AssertionError("must not call the bridge"))
    report = run_check(watch_root(tmp_path, []))
    assert calls == [] and report.fatal == [] and report.warning == []


def test_the_key_comes_from_the_environment_or_dotenv_without_changing_the_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("MT5_BRIDGE_API_KEY", raising=False)
    root = watch_root(tmp_path, ["JP225"])
    (root / ".env").write_text("MT5_BRIDGE_API_KEY=FROMDOTENV\n")
    calls = serve(monkeypatch, ["JP225"])
    run_check(root)
    assert calls[0][1]["headers"] == {"X-Bridge-Api-Key": "FROMDOTENV"}
    import os
    assert "MT5_BRIDGE_API_KEY" not in os.environ


def test_warning_codes_are_registered_so_they_never_fail_the_check():
    assert preflight.is_warning("watch_symbol_unknown") and preflight.is_warning("watch_symbols_unchecked")
    fatal, warning = preflight.classify([Finding("watch_symbol_unknown")])
    assert fatal == [] and warning == ["watch_symbol_unknown"]


def test_the_daemon_start_records_a_startup_warning_and_continues(tmp_path):
    from agentic_fx.config import load_settings
    root = watch_root(tmp_path, ["JP225", "NOPE"])
    settings = load_settings(root / "config" / "settings.yaml")
    ctx = service_mod.StartupContext(None, [], [], bridge_fetch=lambda url, **k: Resp(["JP225"]),
                                     bridge_api_key="k")
    assert service_mod._startup_watch_symbols(settings, ctx) is settings
    assert [(f.code, f.message) for f in ctx.findings] == [("watch_symbol_unknown", "NOPE")]


def test_the_startup_check_is_part_of_the_shared_list():
    assert service_mod._startup_watch_symbols in service_mod.STARTUP_CHECKS
