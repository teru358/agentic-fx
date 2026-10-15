from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import builtins
import dataclasses
import subprocess

import httpx
import pytest

from agentic_fx.setup.checks import (
    CheckReason,
    CheckResult,
    check_afx_ops,
    check_bridge,
    check_cli_backend,
    check_env_keys,
    check_landlock,
    check_llama_swap,
    check_pytest,
    probe_claude_trade,
    resolve_cli_bin,
)


@pytest.mark.parametrize("configured", ("./claude", "bin/claude"))
def test_resolve_cli_bin_rejects_relative_configured_paths(configured, tmp_path, monkeypatch):
    executable = tmp_path / "claude"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.chdir(tmp_path)

    assert resolve_cli_bin(configured) is None


def test_resolve_cli_bin_expands_home_before_judging_absoluteness(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_cli_bin("~/.local/bin/claude") == tmp_path / ".local" / "bin" / "claude"
    assert resolve_cli_bin("~/bin/../claude", which=lambda _name: None) == tmp_path / "bin" / ".." / "claude"


def test_resolve_cli_bin_rejects_a_relative_path_lookup_result(tmp_path, monkeypatch):
    executable = tmp_path / "claude"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", ".")

    assert resolve_cli_bin("claude") is None


def test_resolve_cli_bin_returns_none_when_path_is_empty(monkeypatch):
    monkeypatch.setenv("PATH", "")

    assert resolve_cli_bin("claude") is None


def test_resolve_cli_bin_preserves_an_absolute_path(tmp_path):
    executable = tmp_path / "claude"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)

    assert resolve_cli_bin(str(executable)) == executable


class _Response:
    def __init__(self, data, *, status_code: int = 200):
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "http://private.example/x")
            raise httpx.HTTPStatusError("private endpoint failed", request=request,
                                        response=httpx.Response(self.status_code, request=request))


def test_llama_swap_result_has_only_safe_structured_fields():
    def fetch(url, *, timeout):
        assert url.endswith("/models")
        assert timeout == 7
        return _Response({"data": [{"id": "model-a"}]})

    result = check_llama_swap("http://secret.example/v1", "model-a", fetch=fetch,
                              timeout=7, now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert result.kind == "llama_swap"
    assert result.result == "ok"
    assert result.reason_code is None
    assert result.params == {"model_count": 1}
    assert "secret" not in repr(result)


def test_llama_swap_failure_redacts_url_and_exception():
    def fetch(url, *, timeout):
        raise httpx.ConnectError("token=top-secret http://private.example")

    result = check_llama_swap("http://private.example/v1", "model-a", fetch=fetch)

    assert result.result == "unverified"
    assert result.reason_code is CheckReason.CONNECT_ERROR
    assert "private" not in repr(result)
    assert "secret" not in repr(result)


def test_bridge_checks_health_quote_and_server_time_without_external_details():
    def fetch(url, *, timeout, headers):
        if url.endswith("/health"):
            return _Response({"mt5_connected": True})
        return _Response({"bid": 1.0, "ask": 1.1, "time": "2026-01-01T00:00:00+00:00"})

    result = check_bridge("http://private.example", "USDJPY", fetch=fetch)

    assert result.result == "ok"
    assert result.reason_code is None
    assert result.params == {"server_time": "2026-01-01T00:00:00+00:00"}


def test_cli_backend_resolves_absolute_bin_and_never_reads_credentials(tmp_path):
    credential = tmp_path / "auth.json"
    credential.write_text("very-secret")
    seen = []
    exe = tmp_path / "private" / "claude"
    exe.parent.mkdir()
    exe.write_bytes(b"x")
    exe.chmod(0o755)

    def run(argv, **kwargs):
        seen.append(argv)
        return type("Done", (), {"returncode": 0})()

    result = check_cli_backend("claude", "claude", credential,
                               which=lambda _: str(exe), run=run)

    assert result.result == "ok"
    assert result.params == {"backend": "claude"}
    assert seen == [[str(exe.resolve()), "--version"]]
    assert "private" not in repr(result)
    assert "very-secret" not in repr(result)


def test_cli_backend_reports_fixed_reasons(tmp_path):
    exe = tmp_path / "claude"
    exe.write_bytes(b"x")
    exe.chmod(0o755)
    missing = check_cli_backend("claude", "missing", Path("/no/auth"),
                                which=lambda _: None)
    absent_auth = check_cli_backend("claude", "claude", Path("/no/auth"),
                                    which=lambda _: str(exe),
                                    run=lambda *a, **k: type("Done", (), {"returncode": 0})())

    assert missing.reason_code is CheckReason.BIN_NOT_FOUND
    assert absent_auth.reason_code is CheckReason.CREDENTIALS_MISSING


def test_codex_backend_rejects_non_elf_executable(tmp_path):
    wrapper = tmp_path / "codex"
    wrapper.write_text("#!/bin/sh\n")
    wrapper.chmod(0o755)

    result = check_cli_backend("codex", str(wrapper), None,
                               which=lambda _: str(wrapper), require_elf=True)

    assert result.result == "unverified"
    assert result.reason_code is CheckReason.BIN_NOT_FOUND


def test_claude_trade_probe_uses_one_turn_fixed_prompt():
    argv_seen = []

    def run(argv, **kwargs):
        argv_seen.append(argv)
        return type("Done", (), {"returncode": 0})()

    result = probe_claude_trade("/bin/claude", run=run)

    assert result.result == "ok"
    assert argv_seen == [["/bin/claude", "-p", "ok だけ答えて", "--max-turns", "1"]]


def test_env_landlock_pytest_and_ops_checks_are_structured(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TOKEN=private-value\nEMPTY=\n")

    env_result = check_env_keys(env, ["TOKEN", "EMPTY", "MISSING"])
    assert env_result.result == "unverified"
    assert env_result.reason_code is CheckReason.ENV_KEY_MISSING
    assert env_result.params == {"missing_keys": ("EMPTY", "MISSING")}
    assert "private-value" not in repr(env_result)
    assert check_landlock(is_available=lambda: True).result == "ok"
    assert check_pytest(find_spec=lambda _: object()).result == "ok"
    assert check_afx_ops(resolve=lambda: object()).result == "ok"


def test_results_cannot_store_a_reason_outside_the_fixed_vocabulary():
    result = check_afx_ops(resolve=lambda: (_ for _ in ()).throw(RuntimeError("private failure")))

    assert result.reason_code in set(CheckReason)


NOW = lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)  # noqa: E731


class _BadJson(_Response):
    def json(self):
        raise ValueError("not json")


def _status_error(code):
    request = httpx.Request("GET", "http://private.example/x")
    return httpx.HTTPStatusError("x", request=request,
                                 response=httpx.Response(code, request=request))


# ---- bridge ----

@pytest.mark.parametrize("code", [401, 403])
def test_bridge_auth_status_on_health_and_quote(code):
    health = check_bridge("http://b", "USDJPY", fetch=lambda url, **k: _Response({}, status_code=code))
    assert health.reason_code is CheckReason.AUTH_REQUIRED

    def fetch(url, **k):
        if url.endswith("/health"):
            return _Response({"mt5_connected": True})
        return _Response({}, status_code=code)

    assert check_bridge("http://b", "USDJPY", fetch=fetch).reason_code is CheckReason.AUTH_REQUIRED


def test_bridge_auth_error_raised_by_fetch_is_not_a_connection_error():
    def fetch(url, **k):
        raise _status_error(401)

    result = check_bridge("http://b", "USDJPY", fetch=fetch)
    assert result.reason_code is CheckReason.AUTH_REQUIRED
    assert check_bridge("http://b", "USDJPY", fetch=lambda url, **k: (_ for _ in ()).throw(
        _status_error(500))).reason_code is CheckReason.CONNECT_ERROR


@pytest.mark.parametrize("exc", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")])
def test_bridge_unreachable_is_connect_error(exc):
    def fetch(url, **k):
        raise exc

    result = check_bridge("http://b", "USDJPY", fetch=fetch)
    assert result.reason_code is CheckReason.CONNECT_ERROR
    assert result.result == "unverified"


def test_bridge_server_error_is_connect_error():
    result = check_bridge("http://b", "USDJPY",
                          fetch=lambda url, **k: _Response({}, status_code=500))
    assert result.reason_code is CheckReason.CONNECT_ERROR


def test_bridge_non_json_and_wrong_shape_are_invalid_response():
    assert check_bridge("http://b", "USDJPY",
                        fetch=lambda url, **k: _BadJson({})).reason_code is CheckReason.INVALID_RESPONSE
    assert check_bridge("http://b", "USDJPY",
                        fetch=lambda url, **k: _Response([1])).reason_code is CheckReason.INVALID_RESPONSE
    assert check_bridge("http://b", "USDJPY", fetch=lambda url, **k: _Response(
        {"mt5_connected": "yes"})).reason_code is CheckReason.INVALID_RESPONSE


def test_bridge_quote_failures():
    def make(quote):
        def fetch(url, **k):
            if url.endswith("/health"):
                return _Response({"mt5_connected": True})
            if isinstance(quote, Exception):
                raise quote
            return quote
        return fetch

    assert check_bridge("http://b", "USDJPY", fetch=make(httpx.ConnectError("x"))
                        ).reason_code is CheckReason.CONNECT_ERROR
    assert check_bridge("http://b", "USDJPY", fetch=make(_Response({}, status_code=500))
                        ).reason_code is CheckReason.CONNECT_ERROR
    assert check_bridge("http://b", "USDJPY", fetch=make(_BadJson({}))
                        ).reason_code is CheckReason.INVALID_RESPONSE


def test_bridge_terminal_not_connected():
    result = check_bridge("http://b", "USDJPY",
                          fetch=lambda url, **k: _Response({"mt5_connected": False}))
    assert result.reason_code is CheckReason.BRIDGE_NOT_CONNECTED
    assert result.params == {}


@pytest.mark.parametrize("value", [None, 123, "garbage", "2026-01-01T00:00:00",
                                   "https://private.example/token=secret"])
def test_bridge_invalid_server_time(value):
    def fetch(url, **k):
        if url.endswith("/health"):
            return _Response({"mt5_connected": True})
        return _Response({"time": value})

    result = check_bridge("http://b", "USDJPY", fetch=fetch)
    assert result.reason_code is CheckReason.SERVER_TIME_UNKNOWN
    assert result.params == {}
    assert "secret" not in repr(result)


def test_bridge_server_time_is_normalised_to_utc():
    def fetch(url, **k):
        if url.endswith("/health"):
            return _Response({"mt5_connected": True})
        return _Response({"time": "2026-01-01T03:00:00+03:00"})

    assert check_bridge("http://b", "USDJPY", fetch=fetch).params == {
        "server_time": "2026-01-01T00:00:00+00:00"}


def test_bridge_sends_api_key_header_on_both_requests():
    seen = []

    def fetch(url, *, timeout, headers):
        seen.append((url, timeout, headers))
        if url.endswith("/health"):
            return _Response({"mt5_connected": True})
        return _Response({"time": "2026-01-01T00:00:00+00:00"})

    check_bridge("http://b/", "USDJPY", fetch=fetch, timeout=3,
                 headers={"X-Bridge-Api-Key": "k"})
    assert seen == [("http://b/health", 3, {"X-Bridge-Api-Key": "k"}),
                    ("http://b/quote/USDJPY", 3, {"X-Bridge-Api-Key": "k"})]


# ---- llama-swap ----

@pytest.mark.parametrize("code", [401, 403])
def test_llama_swap_auth_status_is_unsupported(code):
    result = check_llama_swap("http://l", "m", fetch=lambda url, **k: _Response({}, status_code=code))
    assert result.reason_code is CheckReason.AUTH_UNSUPPORTED
    assert result.result == "failed"


def test_llama_swap_timeout_is_connect_error():
    def fetch(url, **k):
        raise httpx.ReadTimeout("slow")

    assert check_llama_swap("http://l", "m", fetch=fetch).reason_code is CheckReason.CONNECT_ERROR


def test_llama_swap_empty_catalogue_is_model_not_found():
    result = check_llama_swap("http://l", "m", fetch=lambda url, **k: _Response({"data": []}))
    assert result.reason_code is CheckReason.MODEL_NOT_FOUND
    assert result.params == {"model_count": 0}


def test_llama_swap_bad_json_is_connect_error():
    result = check_llama_swap("http://l", "m", fetch=lambda url, **k: _BadJson({}))
    assert result.reason_code is CheckReason.CONNECT_ERROR


def test_llama_swap_unexpected_exception_propagates():
    with pytest.raises(AttributeError):
        check_llama_swap("http://l", "m", fetch=lambda url, **k: _Response([1]))

    def fetch(url, **k):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        check_llama_swap("http://l", "m", fetch=fetch)


# ---- cli backend ----

def _exe(path: Path, body: bytes = b"\x7fELF", mode: int = 0o755) -> Path:
    path.write_bytes(body)
    path.chmod(mode)
    return path


def _done(code=0):
    return lambda *a, **k: type("Done", (), {"returncode": code})()


def test_cli_absolute_path_missing_is_bin_not_found(tmp_path):
    calls = []
    result = check_cli_backend("claude", str(tmp_path / "claude"), None,
                               which=lambda _: None, run=lambda *a, **k: calls.append(a))
    assert result.reason_code is CheckReason.BIN_NOT_FOUND
    assert calls == []


def test_cli_non_executable_file_and_directory_are_bin_not_found(tmp_path):
    plain = _exe(tmp_path / "claude", mode=0o644)
    assert check_cli_backend("claude", str(plain), None, which=lambda _: None,
                             run=_done()).reason_code is CheckReason.BIN_NOT_FOUND
    directory = tmp_path / "dir"
    directory.mkdir()
    assert check_cli_backend("claude", str(directory), None, which=lambda _: None,
                             run=_done()).reason_code is CheckReason.BIN_NOT_FOUND


def test_cli_version_timeout_and_nonzero_are_version_check_failed(tmp_path):
    exe = _exe(tmp_path / "claude")

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("claude", 15)

    assert check_cli_backend("claude", str(exe), None, which=lambda _: None,
                             run=timeout).reason_code is CheckReason.VERSION_CHECK_FAILED
    assert check_cli_backend("claude", str(exe), None, which=lambda _: None,
                             run=_done(1)).reason_code is CheckReason.VERSION_CHECK_FAILED
    seen = {}

    def run(argv, **kwargs):
        seen.update(kwargs)
        return _done()()

    check_cli_backend("claude", str(exe), None, which=lambda _: None, run=run)
    assert seen["timeout"] == 15


def test_cli_credentials_file_is_never_opened(tmp_path, monkeypatch):
    exe = _exe(tmp_path / "claude")
    credential = tmp_path / "auth.json"
    credential.write_text("very-secret")
    opened = []
    real_open = builtins.open

    def spy(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: pytest.fail("read_text"))
    monkeypatch.setattr(Path, "read_bytes", lambda *a, **k: pytest.fail("read_bytes"))
    result = check_cli_backend("claude", str(exe), credential, which=lambda _: None, run=_done())

    assert result.result == "ok"
    assert str(credential) not in opened
    assert opened == []


def test_only_codex_requires_elf(tmp_path):
    wrapper = _exe(tmp_path / "tool", body=b"#!/bin/sh\n")
    assert check_cli_backend("claude", str(wrapper), None, which=lambda _: None,
                             run=_done()).result == "ok"
    assert check_cli_backend("opencode", str(wrapper), None, which=lambda _: None,
                             run=_done()).result == "ok"
    assert check_cli_backend("codex", str(wrapper), None, which=lambda _: None,
                             run=_done(), require_elf=True).reason_code is CheckReason.BIN_NOT_FOUND
    elf = _exe(tmp_path / "codex")
    assert check_cli_backend("codex", str(elf), None, which=lambda _: None,
                             run=_done(), require_elf=True).result == "ok"


def test_claude_probe_timeout_and_nonzero_are_fixed_failures():
    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("claude", 60)

    for run in (timeout, _done(2)):
        result = probe_claude_trade("/bin/claude", run=run)
        assert result.result == "failed"
        assert result.reason_code is CheckReason.CONNECT_ERROR
    seen = {}

    def ok(argv, **kwargs):
        seen.update(kwargs)
        return _done()()

    probe_claude_trade("/bin/claude", run=ok)
    assert seen["timeout"] == 60


# ---- .env ----

def test_env_key_forms(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# comment=1\n"
        "\n"
        "export EXPORTED=value\n"
        "  INDENTED = spaced\n"
        "  # INDENTED_COMMENT=x\n"
        "EMPTY=\n"
        "QUOTED_EMPTY=\"\"\n"
        "lower=bad\n"
        "exportFOO=bad\n"
        "PLAIN=v\n")
    keys = ["EXPORTED", "INDENTED", "EMPTY", "QUOTED_EMPTY", "INDENTED_COMMENT",
            "COMMENT", "PLAIN", "FOO"]
    result = check_env_keys(env, keys)
    assert result.params == {"missing_keys": ("EMPTY", "QUOTED_EMPTY", "INDENTED_COMMENT",
                                              "COMMENT", "FOO")}
    assert "value" not in repr(result) and "spaced" not in repr(result)


def test_env_only_environ_and_missing_file(tmp_path):
    result = check_env_keys(tmp_path / "nope", ["A", "B"], environ={"A": "x", "B": ""})
    assert result.params == {"missing_keys": ("B",)}
    assert check_env_keys(tmp_path / "nope", ["A"], environ={"A": "x"}).result == "ok"


# ---- unavailable ----

def test_environment_probes_report_unavailable():
    landlock = check_landlock(is_available=lambda: False)
    assert (landlock.result, landlock.reason_code) == ("unverified", CheckReason.LANDLOCK_UNAVAILABLE)
    pytest_ = check_pytest(find_spec=lambda _: None)
    assert (pytest_.result, pytest_.reason_code) == ("unverified", CheckReason.PYTEST_UNAVAILABLE)

    def resolve():
        raise FileNotFoundError("/private/afx-ops")

    ops = check_afx_ops(resolve=resolve)
    assert (ops.result, ops.reason_code) == ("unverified", CheckReason.OPS_EXEC_NOT_FOUND)
    assert "private" not in repr(ops)


# ---- CheckResult schema ----

def test_check_result_rejects_values_outside_the_schema():
    at = NOW()
    with pytest.raises(ValueError):
        CheckResult("env", "unverified", "arbitrary", at)
    with pytest.raises(ValueError):
        CheckResult("env", "unverified", "env_key_missing", at)
    with pytest.raises(ValueError):
        CheckResult("env", "weird", None, at)
    for kind, params in [
        ("env", {"x": 1}),
        ("env", {"missing_keys": ["lower"]}),
        ("env", {"missing_keys": "A"}),
        ("env", {"missing_keys": ["A B"]}),
        ("env", {"model_count": 1}),
        ("llama_swap", {"model_count": "1"}),
        ("llama_swap", {"model_count": True}),
        ("llama_swap", {"model_count": -1}),
        ("bridge", {"server_time": "https://private.example"}),
        ("bridge", {"server_time": "2026-01-01T00:00:00"}),
        ("bridge", {"server_time": 5}),
        ("cli_backend", {"backend": "http://private.example"}),
        ("cli_backend", {"backend": "claude", "extra": "x"}),
    ]:
        with pytest.raises(ValueError):
            CheckResult(kind, "ok", None, at, params)
    ok = CheckResult("bridge", "ok", None, at, {"server_time": "2026-01-01T09:00:00+09:00"})
    assert ok.params == {"server_time": "2026-01-01T00:00:00+00:00"}
    assert CheckResult("cli_backend", "ok", CheckReason.BIN_NOT_FOUND, at,
                       {"backend": "codex"}).params == {"backend": "codex"}



def test_check_result_params_cannot_be_changed_after_creation():
    result = CheckResult("bridge", "ok", None, NOW(),
                         {"server_time": "2026-01-01T09:00:00+09:00"})
    with pytest.raises(TypeError):
        result.params["free_text"] = "secret"
    with pytest.raises(TypeError):
        result.params["server_time"] = "other"
    with pytest.raises(TypeError):
        del result.params["server_time"]
    assert result.params == {"server_time": "2026-01-01T00:00:00+00:00"}

    missing = CheckResult("env", "unverified", CheckReason.ENV_KEY_MISSING, NOW(),
                          {"missing_keys": ["A"]})
    assert not hasattr(missing.params["missing_keys"], "append")


def test_check_result_params_are_detached_from_the_caller_dict():
    source = {"model_count": 1}
    result = CheckResult("llama_swap", "ok", None, NOW(), source)
    source["model_count"] = 99
    source["free_text"] = "secret"
    assert result.params == {"model_count": 1}


def test_replace_cannot_bypass_the_params_schema():
    result = CheckResult("llama_swap", "ok", None, NOW(), {"model_count": 1})
    with pytest.raises(ValueError):
        dataclasses.replace(result, params={"free_text": "secret"})
    with pytest.raises(ValueError):
        dataclasses.replace(result, params={"model_count": "https://private.example"})
    with pytest.raises(ValueError):
        dataclasses.replace(result, kind="env")
    assert dataclasses.replace(result, params={"model_count": 2}).params == {"model_count": 2}


def test_landlock_and_pytest_checks_treat_an_unexpected_exception_as_unavailable():
    def boom_landlock():
        raise RuntimeError("/private/landlock detail")

    def boom_find_spec(_name):
        raise ValueError("/private/spec detail")

    landlock = check_landlock(is_available=boom_landlock)
    assert (landlock.result, landlock.reason_code) == (
        "unverified", CheckReason.LANDLOCK_UNAVAILABLE)
    pytest_ = check_pytest(find_spec=boom_find_spec)
    assert (pytest_.result, pytest_.reason_code) == (
        "unverified", CheckReason.PYTEST_UNAVAILABLE)
    assert "private" not in repr(landlock) and "private" not in repr(pytest_)


def test_list_models_returns_plain_identifiers_only():
    from agentic_fx.setup.checks import list_llama_swap_models

    def fetch(url, *, timeout):
        assert url == "http://private.example/v1/models" and timeout == 3
        return _Response({"data": [{"id": "qwen3.6-35b-a3b_Q4"}, {"id": "bad id\nwith newline"},
                                   {"id": 7}, "x", {"id": "org/model:Q4"}]})

    result = list_llama_swap_models("http://private.example/v1/", fetch=fetch, timeout=3)
    assert result.result == "ok"
    assert result.params["models"] == ("qwen3.6-35b-a3b_Q4", "org/model:Q4")
    assert result.params["model_count"] == 2
    assert "private" not in repr(result)


@pytest.mark.parametrize("response,state,reason", [
    (_Response({"data": []}), "unverified", CheckReason.MODEL_NOT_FOUND),
    (_Response({"data": []}, status_code=401), "failed", CheckReason.AUTH_UNSUPPORTED),
    (_Response({"data": []}, status_code=403), "failed", CheckReason.AUTH_UNSUPPORTED),
    (_Response({"data": []}, status_code=500), "unverified", CheckReason.CONNECT_ERROR),
    (_Response([1, 2]), "unverified", CheckReason.CONNECT_ERROR)])
def test_list_models_separates_empty_auth_and_connection_failures(response, state, reason):
    from agentic_fx.setup.checks import list_llama_swap_models

    result = list_llama_swap_models("http://x/v1", fetch=lambda url, *, timeout: response)
    assert (result.result, result.reason_code) == (state, reason)


def test_list_models_connection_error_hides_the_exception():
    from agentic_fx.setup.checks import list_llama_swap_models

    def fetch(url, *, timeout):
        raise httpx.ConnectError("token=top-secret")

    result = list_llama_swap_models("http://x/v1", fetch=fetch)
    assert result.reason_code is CheckReason.CONNECT_ERROR
    assert "secret" not in repr(result)


def test_check_result_rejects_free_text_model_parameters():
    with pytest.raises(ValueError):
        CheckResult("llama_swap", "ok", None, datetime(2026, 1, 1, tzinfo=timezone.utc),
                    {"models": ["a b"]})
    with pytest.raises(ValueError):
        CheckResult("bridge", "ok", None, datetime(2026, 1, 1, tzinfo=timezone.utc),
                    {"models": ["a"]})


def _codex_listing_reply(*entries, extra_lines=()):
    import json
    lines = [json.dumps({"id": 1, "result": {"userAgent": "codex/0.0"}}), *extra_lines,
             json.dumps({"id": 2, "result": {"data": list(entries), "nextCursor": None}})]
    return "\n".join(lines) + "\n"


def _model(name, *, hidden=False):
    return {"id": name, "model": name, "displayName": name.upper(), "description": "d", "hidden": hidden}


def test_codex_models_come_from_the_app_server_and_hidden_ones_are_dropped():
    from agentic_fx.setup.checks import list_codex_models

    seen = {}

    def run(argv, **kwargs):
        seen.update(argv=argv, kwargs=kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout=_codex_listing_reply(
            _model("gpt-6.1-sol"), _model("gpt-6.1-old", hidden=True), _model("gpt-6.1-terra"),
            _model("bad id with spaces")), stderr="")

    result = list_codex_models(bin_path="/x/codex", run=run)
    assert result.result == "ok" and result.params["models"] == ("gpt-6.1-sol", "gpt-6.1-terra")
    assert seen["argv"] == ["/x/codex", "app-server"]
    assert '"method":"initialize"' in seen["kwargs"]["input"] and '"method":"model/list"' in seen["kwargs"]["input"]


def test_codex_models_are_read_from_the_output_of_a_server_that_does_not_exit():
    from agentic_fx.setup.checks import list_codex_models

    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 10, output=_codex_listing_reply(_model("gpt-6.1-terra")).encode())

    result = list_codex_models(bin_path="/x/codex", run=run)
    assert result.result == "ok" and result.params["models"] == ("gpt-6.1-terra",)


@pytest.mark.parametrize("reply", [
    subprocess.CompletedProcess([], 1, stdout="", stderr="boom"),
    subprocess.CompletedProcess([], 0, stdout="garbage\n", stderr=""),
    subprocess.CompletedProcess([], 0, stdout='{"id":2,"error":{"code":-1}}\n', stderr=""),
    subprocess.CompletedProcess([], 0, stdout=_codex_listing_reply(), stderr=""),
    OSError("no such file"),
    subprocess.TimeoutExpired([], 10)])
def test_a_codex_listing_that_fails_is_model_list_unavailable(reply):
    from agentic_fx.setup.checks import list_codex_models

    def run(argv, **kwargs):
        if isinstance(reply, BaseException):
            raise reply
        return reply

    result = list_codex_models(bin_path="/x/codex", run=run)
    assert result.result == "unverified" and result.reason_code is CheckReason.MODEL_LIST_UNAVAILABLE
    assert dict(result.params) == {}


@pytest.mark.parametrize("reply", [
    subprocess.CompletedProcess([], 1, stdout=_codex_listing_reply(_model("gpt-6.1-terra")), stderr=""),
    subprocess.CompletedProcess([], 0, stdout='{"id":1,"error":{"code":-1}}\n' +
                               _codex_listing_reply(_model("gpt-6.1-terra")).split("\n", 1)[1], stderr=""),
    subprocess.CompletedProcess([], 0, stdout='{"id":1,"result":{}}\n{"id":2,"error":{"code":-1}}\n', stderr=""),
])
def test_codex_listing_rejects_a_failed_handshake_even_when_models_are_present(reply):
    from agentic_fx.setup.checks import list_codex_models

    result = list_codex_models(bin_path="/x/codex", run=lambda argv, **kwargs: reply)

    assert result.result == "unverified"
    assert result.reason_code is CheckReason.MODEL_LIST_UNAVAILABLE


def test_codex_listing_accepts_the_handshake_reply_after_the_model_list():
    import json
    from agentic_fx.setup.checks import list_codex_models

    stdout = (json.dumps({"id": 2, "result": {"data": [_model("gpt-6.1-terra")], "nextCursor": None}}) + "\n"
              + json.dumps({"id": 1, "result": {"userAgent": "codex/0.0"}}) + "\n")
    reply = subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")

    result = list_codex_models(bin_path="/x/codex", run=lambda argv, **kwargs: reply)

    assert result.result == "ok"
    assert result.params["models"] == ("gpt-6.1-terra",)


def test_opencode_models_are_one_identifier_per_line_without_duplicates_or_noise():
    from agentic_fx.setup.checks import list_opencode_models

    def run(argv, **kwargs):
        assert argv == ["/x/opencode", "models"]
        return subprocess.CompletedProcess(argv, 0, stdout="a/x\n\n  b/y  \na/x\nnot a model!\n", stderr="")

    result = list_opencode_models(bin_path="/x/opencode", run=run)
    assert result.result == "ok" and result.params["models"] == ("a/x", "b/y")


@pytest.mark.parametrize("reply", [subprocess.CompletedProcess([], 2, stdout="a/x\n", stderr=""),
                                   subprocess.CompletedProcess([], 0, stdout="\n", stderr=""),
                                   OSError("gone"), subprocess.TimeoutExpired([], 10)])
def test_an_opencode_listing_that_fails_is_model_list_unavailable(reply):
    from agentic_fx.setup.checks import list_opencode_models

    def run(argv, **kwargs):
        if isinstance(reply, BaseException):
            raise reply
        return reply

    result = list_opencode_models(bin_path="/x/opencode", run=run)
    assert result.reason_code is CheckReason.MODEL_LIST_UNAVAILABLE


def test_model_identifiers_are_a_parameter_only_of_the_listing_checks():
    from agentic_fx.store.setup_record import REASON_CODES

    now = datetime(2026, 10, 13, tzinfo=timezone.utc)
    for kind in ("llama_swap", "codex_models", "opencode_models"):
        assert CheckResult(kind, "ok", None, now, {"models": ["a/x"]}).params["models"] == ("a/x",)
    with pytest.raises(ValueError):
        CheckResult("bridge", "ok", None, now, {"models": ["a/x"]})
    assert {"model_list_unavailable", "verify_failed"} <= REASON_CODES
