"""Side-effect boundaries for first-run environment checks."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType

from typing import Any, Callable, Literal, Mapping

from urllib.parse import urlsplit

import httpx

from agentic_fx.core.cli_bin import resolve_cli_bin


class CheckReason(StrEnum):
    AUTH_REQUIRED = "auth_required"
    AUTH_UNSUPPORTED = "auth_unsupported"
    BIN_NOT_FOUND = "bin_not_found"
    BRIDGE_NOT_CONNECTED = "bridge_not_connected"
    CONNECT_ERROR = "connect_error"
    CREDENTIALS_MISSING = "credentials_missing"
    DEFERRED_BY_USER = "deferred_by_user"
    ENV_KEY_MISSING = "env_key_missing"
    INVALID_RESPONSE = "invalid_response"
    LANDLOCK_UNAVAILABLE = "landlock_unavailable"
    MODEL_LIST_UNAVAILABLE = "model_list_unavailable"
    MODEL_NOT_FOUND = "model_not_found"
    OPS_EXEC_NOT_FOUND = "ops_exec_not_found"
    PYTEST_UNAVAILABLE = "pytest_unavailable"
    SERVER_TIME_UNKNOWN = "server_time_unknown"
    TERMINAL_DISCONNECTED = "terminal_disconnected"
    USER_SESSION_MISSING = "user_session_missing"
    USER_DECLINED_PROBE = "user_declined_probe"
    VERIFY_FAILED = "verify_failed"
    KEY_WITHHELD_PLAIN_HTTP = "key_withheld_plain_http"
    VERSION_CHECK_FAILED = "version_check_failed"


CheckState = Literal["ok", "failed", "unverified", "planned"]

_CHECK_STATES = frozenset({"ok", "failed", "unverified", "planned"})
_ENV_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]*")
_CLI_BACKENDS = frozenset({"claude", "codex", "opencode"})
# params のキーは検査の種類ごとに固定。値の型も固定で、自由文は入らない。
_MODEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}")
_MAX_LISTED_MODELS = 200
_MAX_LISTED_SYMBOLS = 20_000
_MAX_SYMBOL_BYTES = 32
_PARAM_OWNER = {
    "model_count": ("llama_swap",),
    "models": ("llama_swap", "codex_models", "opencode_models"),
    "server_time": ("bridge",),
    "symbols": ("bridge_symbols",),
    "backend": ("cli_backend",),
    "missing_keys": ("env",),
}


def _normalise_server_time(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("server_time must be an ISO 8601 string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("server_time must carry a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _is_raw_symbol(value: object) -> bool:
    """A broker symbol name that can be kept in settings: non-empty, printable, at most 32 bytes."""
    return (isinstance(value, str) and bool(value) and value.isprintable()
            and not value[0].isspace() and not value[-1].isspace()
            and len(value.encode("utf-8")) <= _MAX_SYMBOL_BYTES)


@dataclass(frozen=True)
class CheckResult:
    kind: str
    result: CheckState
    reason_code: CheckReason | None
    at: datetime
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.result not in _CHECK_STATES:
            raise ValueError("unknown check state")
        if self.reason_code is not None and not isinstance(self.reason_code, CheckReason):
            raise ValueError("reason_code must be a CheckReason")
        if not isinstance(self.params, Mapping):
            raise ValueError("params must be a mapping")
        clean: dict[str, Any] = {}
        for key, value in self.params.items():
            if self.kind not in _PARAM_OWNER.get(key, ()):
                raise ValueError("unsupported check parameter")
            if key == "model_count":
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError("model_count must be a non-negative int")
                clean[key] = value
            elif key == "models":
                if (not isinstance(value, (list, tuple)) or len(value) > _MAX_LISTED_MODELS
                        or not all(isinstance(m, str) and _MODEL_ID_RE.fullmatch(m) for m in value)):
                    raise ValueError("models must be model identifiers")
                clean[key] = tuple(value)
            elif key == "symbols":
                if (not isinstance(value, (list, tuple)) or len(value) > _MAX_LISTED_SYMBOLS
                        or not all(_is_raw_symbol(v) for v in value)):
                    raise ValueError("symbols must be raw symbol names")
                clean[key] = tuple(value)
            elif key == "server_time":
                clean[key] = _normalise_server_time(value)
            elif key == "backend":
                if value not in _CLI_BACKENDS:
                    raise ValueError("unknown backend")
                clean[key] = value
            else:
                if (not isinstance(value, list)
                        or not all(isinstance(k, str) and _ENV_KEY_RE.fullmatch(k)
                                   for k in value)):
                    raise ValueError("missing_keys must be env key names")
                clean[key] = tuple(value)
        # 検査済みの値は生成後に書き換えられない (固定 schema を保つ)。
        object.__setattr__(self, "params", MappingProxyType(clean))


def _now(now: Callable[[], datetime] | None) -> datetime:
    return (now or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)


def _result(kind: str, result: CheckState, reason: CheckReason | None = None,
            *, params: dict[str, Any] | None = None,
            now: Callable[[], datetime] | None = None) -> CheckResult:
    return CheckResult(kind, result, reason, _now(now), params or {})


def _is_auth_status(response: Any) -> bool:
    return getattr(response, "status_code", None) in (401, 403)


def check_llama_swap(base_url: str, model: str, *, fetch: Callable[..., Any],
                     timeout: float = 5, now: Callable[[], datetime] | None = None) -> CheckResult:
    """Check the model catalogue without retaining its URL or model names.

    Only transport, status and decode failures become ``connect_error``; any
    other exception from the caller's fetch propagates unchanged.
    """
    try:
        response = fetch(f"{base_url.rstrip('/')}/models", timeout=timeout)
        if _is_auth_status(response):
            return _result("llama_swap", "failed", CheckReason.AUTH_UNSUPPORTED, now=now)
        response.raise_for_status()
        items = response.json().get("data", [])
        ids = [item.get("id") for item in items if isinstance(item, dict)]
    except (httpx.RequestError, httpx.HTTPStatusError, ValueError, TypeError):
        return _result("llama_swap", "unverified", CheckReason.CONNECT_ERROR, now=now)
    if model not in ids:
        return _result("llama_swap", "unverified", CheckReason.MODEL_NOT_FOUND,
                       params={"model_count": len(ids)}, now=now)
    return _result("llama_swap", "ok", params={"model_count": len(ids)}, now=now)


def list_llama_swap_models(base_url: str, *, fetch: Callable[..., Any], timeout: float = 5,
                           now: Callable[[], datetime] | None = None) -> CheckResult:
    """List model identifiers from the catalogue; identifiers that are not plain names are dropped.

    The catalogue is read-only (``/models`` never loads a model). Authentication
    requests are ``auth_unsupported``; an empty list is ``model_not_found``.
    """
    try:
        response = fetch(f"{base_url.rstrip('/')}/models", timeout=timeout)
        if _is_auth_status(response):
            return _result("llama_swap", "failed", CheckReason.AUTH_UNSUPPORTED, now=now)
        response.raise_for_status()
        items = response.json().get("data", [])
        ids = [item.get("id") for item in items if isinstance(item, dict)]
    except (httpx.RequestError, httpx.HTTPStatusError, ValueError, TypeError, AttributeError):
        return _result("llama_swap", "unverified", CheckReason.CONNECT_ERROR, now=now)
    names = [m for m in ids if isinstance(m, str) and _MODEL_ID_RE.fullmatch(m)][:_MAX_LISTED_MODELS]
    if not names:
        return _result("llama_swap", "unverified", CheckReason.MODEL_NOT_FOUND,
                       params={"model_count": 0}, now=now)
    return _result("llama_swap", "ok", params={"model_count": len(names), "models": list(names)}, now=now)


class _BridgeFailure(Exception):
    def __init__(self, reason: CheckReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


def _bridge_payload(response: Any, expected: type) -> Any:
    """Read a bridge response body that must be of type ``expected``; failures carry a fixed reason."""
    if _is_auth_status(response):
        raise _BridgeFailure(CheckReason.AUTH_REQUIRED)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        if error.response.status_code in (401, 403):
            raise _BridgeFailure(CheckReason.AUTH_REQUIRED) from None
        raise _BridgeFailure(CheckReason.CONNECT_ERROR) from None
    try:
        payload = response.json()
    except ValueError:
        raise _BridgeFailure(CheckReason.INVALID_RESPONSE) from None
    if not isinstance(payload, expected):
        raise _BridgeFailure(CheckReason.INVALID_RESPONSE)
    return payload


def _bridge_json(response: Any) -> dict[str, Any]:
    return _bridge_payload(response, dict)


def check_bridge(base_url: str, pair: str, *, fetch: Callable[..., Any],
                 timeout: float = 10, headers: dict[str, str] | None = None,
                 now: Callable[[], datetime] | None = None) -> CheckResult:
    """Check bridge health and one quote while retaining only server time."""
    base = base_url.rstrip("/")
    try:
        health = _bridge_json(fetch(f"{base}/health", timeout=timeout, headers=headers or {}))
        connected = health.get("mt5_connected")
        if not isinstance(connected, bool):
            return _result("bridge", "unverified", CheckReason.INVALID_RESPONSE, now=now)
        if not connected:
            return _result("bridge", "unverified", CheckReason.BRIDGE_NOT_CONNECTED, now=now)
        quote = _bridge_json(fetch(f"{base}/quote/{pair}", timeout=timeout,
                                   headers=headers or {}))
    except _BridgeFailure as failure:
        return _result("bridge", "unverified", failure.reason, now=now)
    except httpx.HTTPStatusError as error:
        reason = (CheckReason.AUTH_REQUIRED if error.response.status_code in (401, 403)
                  else CheckReason.CONNECT_ERROR)
        return _result("bridge", "unverified", reason, now=now)
    except httpx.HTTPError:
        return _result("bridge", "unverified", CheckReason.CONNECT_ERROR, now=now)
    try:
        server_time = _normalise_server_time(quote.get("time"))
    except ValueError:
        return _result("bridge", "unverified", CheckReason.SERVER_TIME_UNKNOWN, now=now)
    return _result("bridge", "ok", params={"server_time": server_time}, now=now)


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
BRIDGE_KEY_ENV = "MT5_BRIDGE_API_KEY"


def may_send_bridge_key(url: str) -> bool:
    """Whether the API key header may go to this destination (https, or http on this machine)."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    return parts.scheme == "https" or (parts.scheme == "http" and host in _LOOPBACK_HOSTS)


def env_secret(root: Path, environ: Mapping[str, str], key: str) -> str | None:
    """A secret from the environment, else from ``root/.env`` (the environment is not modified)."""
    if environ.get(key):
        return environ[key]
    try:
        from dotenv import dotenv_values
        value = dotenv_values(root / ".env").get(key)
    except Exception:  # noqa: BLE001 - an unreadable .env means no key
        return None
    return value or None


def bridge_key_headers(url: str, root: Path, environ: Mapping[str, str], *,
                       check_destination: bool = True) -> dict[str, str]:
    """The bridge API key header, or nothing.

    With ``check_destination`` the key is withheld from a destination that is not https or
    this machine; without it the configured destination is trusted (the daemon already uses it).
    """
    if check_destination and not may_send_bridge_key(url):
        return {}
    key = env_secret(root, environ, BRIDGE_KEY_ENV)
    return {"X-Bridge-Api-Key": key} if key else {}


def list_bridge_symbols(base_url: str, *, fetch: Callable[..., Any], timeout: float = 10,
                        headers: dict[str, str] | None = None,
                        now: Callable[[], datetime] | None = None) -> CheckResult:
    """List the broker's raw symbol names from the bridge (`GET /symbols`).

    Names that cannot be kept in settings are dropped. Transport, authentication and
    shape failures map to the same fixed reasons as ``check_bridge``.
    """
    try:
        payload = _bridge_payload(
            fetch(f"{base_url.rstrip('/')}/symbols", timeout=timeout, headers=headers or {}), list)
    except _BridgeFailure as failure:
        return _result("bridge_symbols", "unverified", failure.reason, now=now)
    except httpx.HTTPError:
        return _result("bridge_symbols", "unverified", CheckReason.CONNECT_ERROR, now=now)
    names = list(dict.fromkeys(item for item in payload if _is_raw_symbol(item)))[:_MAX_LISTED_SYMBOLS]
    return _result("bridge_symbols", "ok", params={"symbols": names}, now=now)


def cli_candidate(bin_value: str, *, which: Callable[[str], str | None]) -> str | None:
    """Resolve a configured CLI name to a candidate path (PATH lookup or absolute)."""
    resolved = resolve_cli_bin(bin_value, which=which)
    return str(resolved) if resolved is not None else None


def is_executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def check_cli_backend(backend: str, bin_value: str, credentials_file: Path | str | None,
                      *, which: Callable[[str], str | None] = shutil.which,
                      run: Callable[..., Any] = subprocess.run,
                      require_elf: bool = False,
                      now: Callable[[], datetime] | None = None) -> CheckResult:
    """Resolve a CLI, invoke its version command, and only stat its credential file."""
    not_found = lambda: _result("cli_backend", "unverified", CheckReason.BIN_NOT_FOUND,  # noqa: E731
                                params={"backend": backend}, now=now)
    resolved = cli_candidate(bin_value, which=which)
    if resolved is None:
        return not_found()
    path = Path(resolved).resolve()
    if not is_executable_file(path):
        return not_found()
    if require_elf:
        try:
            with open(path, "rb") as handle:
                is_elf = handle.read(4) == b"\x7fELF"
        except OSError:
            is_elf = False
        if not is_elf:
            return not_found()
    try:
        completed = run([str(path), "--version"], capture_output=True, timeout=15,
                        stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return _result("cli_backend", "unverified", CheckReason.VERSION_CHECK_FAILED,
                       params={"backend": backend}, now=now)
    if getattr(completed, "returncode", 1) != 0:
        return _result("cli_backend", "unverified", CheckReason.VERSION_CHECK_FAILED,
                       params={"backend": backend}, now=now)
    if credentials_file is not None and not Path(credentials_file).expanduser().is_file():
        return _result("cli_backend", "unverified", CheckReason.CREDENTIALS_MISSING,
                       params={"backend": backend}, now=now)
    return _result("cli_backend", "ok", params={"backend": backend}, now=now)


def probe_claude_trade(bin_path: str | Path, *, run: Callable[..., Any] = subprocess.run,
                       now: Callable[[], datetime] | None = None) -> CheckResult:
    """Perform the explicitly approved single-turn Claude trade probe."""
    try:
        completed = run([str(bin_path), "-p", "ok だけ答えて", "--max-turns", "1"],
                        capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
    except Exception:
        return _result("llm_trade", "failed", CheckReason.CONNECT_ERROR, now=now)
    if getattr(completed, "returncode", 1) != 0:
        return _result("llm_trade", "failed", CheckReason.CONNECT_ERROR, now=now)
    return _result("llm_trade", "ok", now=now)


def _env_file_keys(lines: list[str]) -> set[str]:
    """Names of keys with a non-empty value; values are never retained."""
    names: set[str] = set()
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export") and line[6:7].isspace():
            line = line[6:].lstrip()
        key, marker, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if value in ('""', "''"):
            value = ""
        if marker and value and _ENV_KEY_RE.fullmatch(key):
            names.add(key)
    return names


def present_env_keys(env_file: Path, *, environ: Mapping[str, str] | None = None) -> set[str]:
    """Read only environment-variable names from the process and optional dotenv file."""
    names = {k for k, v in (environ or {}).items() if v}
    try:
        lines = env_file.read_text().splitlines()
    except FileNotFoundError:
        lines = []
    names |= _env_file_keys(lines)
    return names


def check_env_keys(env_file: Path, keys: list[str], *, environ: dict[str, str] | None = None,
                   now: Callable[[], datetime] | None = None) -> CheckResult:
    """Determine required key presence without returning their values."""
    names = present_env_keys(env_file, environ=environ)
    missing = [key for key in keys if key not in names]
    if missing:
        return _result("env", "unverified", CheckReason.ENV_KEY_MISSING,
                       params={"missing_keys": missing}, now=now)
    return _result("env", "ok", now=now)


def check_landlock(*, is_available: Callable[[], bool],
                   now: Callable[[], datetime] | None = None) -> CheckResult:
    """An injected probe that raises is treated as unavailable, with no detail kept."""
    try:
        available = bool(is_available())
    except Exception:
        available = False
    return _result("landlock", "ok" if available else "unverified",
                   None if available else CheckReason.LANDLOCK_UNAVAILABLE, now=now)


def check_pytest(*, find_spec: Callable[[str], Any] = importlib.util.find_spec,
                 now: Callable[[], datetime] | None = None) -> CheckResult:
    """An injected lookup that raises is treated as unavailable, with no detail kept."""
    try:
        available = find_spec("pytest") is not None
    except Exception:
        available = False
    return _result("pytest", "ok" if available else "unverified",
                   None if available else CheckReason.PYTEST_UNAVAILABLE, now=now)


def check_afx_ops(*, resolve: Callable[[], Any],
                  now: Callable[[], datetime] | None = None) -> CheckResult:
    try:
        resolve()
    except Exception:
        return _result("afx_ops", "unverified", CheckReason.OPS_EXEC_NOT_FOUND, now=now)
    return _result("afx_ops", "ok", now=now)


_CODEX_HANDSHAKE = (
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{"name":"agentic-fx","version":"1.0"}}}\n'
    '{"jsonrpc":"2.0","id":2,"method":"model/list","params":{}}\n')


def _output_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else ""


def _model_ids(candidates: list[Any]) -> list[str]:
    seen: list[str] = []
    for item in candidates:
        if isinstance(item, str) and _MODEL_ID_RE.fullmatch(item) and item not in seen:
            seen.append(item)
    return seen[:_MAX_LISTED_MODELS]


def _listing(kind: str, ids: list[str], now: Callable[[], datetime] | None) -> CheckResult:
    if not ids:
        return _result(kind, "unverified", CheckReason.MODEL_LIST_UNAVAILABLE, now=now)
    return _result(kind, "ok", params={"models": ids}, now=now)


def list_codex_models(*, bin_path: str | Path, run: Callable[..., Any] = subprocess.run,
                      timeout: float = 10, now: Callable[[], datetime] | None = None) -> CheckResult:
    """Read the model catalogue from ``codex app-server`` (``initialize`` then ``model/list``).

    Hidden entries are dropped; only identifiers are kept. Any failure is
    ``model_list_unavailable`` and keeps no output.
    """
    kind = "codex_models"
    try:
        completed = run([str(bin_path), "app-server"], input=_CODEX_HANDSHAKE, capture_output=True,
                        text=True, timeout=timeout)
        if getattr(completed, "returncode", 1) != 0:
            return _result(kind, "unverified", CheckReason.MODEL_LIST_UNAVAILABLE, now=now)
        stdout = _output_text(getattr(completed, "stdout", ""))
    except subprocess.TimeoutExpired as expired:
        # app-server は stdin が閉じても直ぐには終わらないことがある。応答が先に出ていれば読む。
        stdout = _output_text(expired.stdout)
    except (OSError, subprocess.SubprocessError):
        return _result(kind, "unverified", CheckReason.MODEL_LIST_UNAVAILABLE, now=now)
    handshake: dict[str, Any] | None = None
    listing: dict[str, Any] | None = None
    for line in stdout.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        if message.get("id") == 1 and handshake is None:
            handshake = message
        elif message.get("id") == 2 and listing is None:
            listing = message
    if handshake is None or "error" in handshake or not isinstance(handshake.get("result"), dict):
        return _result(kind, "unverified", CheckReason.MODEL_LIST_UNAVAILABLE, now=now)
    if listing is None or "error" in listing:
        return _result(kind, "unverified", CheckReason.MODEL_LIST_UNAVAILABLE, now=now)
    result = listing.get("result")
    data = result.get("data") if isinstance(result, dict) else None
    ids: list[Any] = []
    if isinstance(data, list):
        ids = [entry.get("id") for entry in data
               if isinstance(entry, dict) and entry.get("hidden") is not True]
    return _listing(kind, _model_ids(ids), now)


def list_opencode_models(*, bin_path: str | Path, run: Callable[..., Any] = subprocess.run,
                         timeout: float = 10, now: Callable[[], datetime] | None = None) -> CheckResult:
    """Read ``provider/model`` identifiers, one per line, from ``opencode models``."""
    kind = "opencode_models"
    try:
        completed = run([str(bin_path), "models"], capture_output=True, text=True, timeout=timeout,
                        stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return _result(kind, "unverified", CheckReason.MODEL_LIST_UNAVAILABLE, now=now)
    if getattr(completed, "returncode", 1) != 0:
        return _result(kind, "unverified", CheckReason.MODEL_LIST_UNAVAILABLE, now=now)
    lines = [line.strip() for line in _output_text(getattr(completed, "stdout", "")).splitlines()]
    return _listing(kind, _model_ids(lines), now)
