"""bot の health snapshot (`gateway.snapshot` version 1) の安全な reader。

bot (`afx_ops`) が置く表示専用の file を、daemon と local CLI が読む。`afx_ops` を import できない
ので `afx_ops.health` の reader と同じ契約を別に持つ。両方を同じ golden JSON で検査する。
読取りの失敗・不正は全部 `unknown` に閉じ、呼ぶ側 (status) を止めない。
"""
from __future__ import annotations

import json
import math
import os
import re
import stat
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SNAPSHOT_NAME = "gateway.snapshot"
VERSION = 1
MAX_BYTES = 4096
# observed_at がこの秒数より古ければ stale
STALE_SECONDS = 120.0
MAX_LATENCY_MS = 3_600_000.0

# terminal_code に入れてよい固定語。恒久 / 一時で分ける (別の側の語は拒否)。bot 側の定義の写し
PERMANENT_CODES = frozenset({
    "token_invalid", "discord_login_failed", "bot_state_invalid", "payload_invalid",
    "events_invalid", "high_watermark_regressed", "discord_rejected"})
TRANSIENT_CODES = frozenset({
    "bot_state_unavailable", "discord_unavailable", "gateway_failed", "gateway_unhealthy",
    "delivery_stalled", "poller_stalled", "bot_internal_error", "daemon_unreachable"})
TERMINAL_CODES = PERMANENT_CODES | TRANSIENT_CODES

FIELDS = ("version", "observed_at", "gateway_state", "gateway_unhealthy_since", "last_ready_at",
          "last_resumed_at", "last_disconnect_at", "last_ack_latency_ms", "delivery_state",
          "delivery_stalled_since", "last_delivery_success_at", "poll_started_at",
          "last_poll_completed_at", "loop_acknowledged_at", "terminal", "terminal_code")
_TIME_FIELDS = ("gateway_unhealthy_since", "last_ready_at", "last_resumed_at", "last_disconnect_at",
                "delivery_stalled_since", "last_delivery_success_at", "poll_started_at",
                "last_poll_completed_at", "loop_acknowledged_at")
GATEWAY_STATES = ("starting", "healthy", "unhealthy")
DELIVERY_STATES = ("idle", "healthy", "stalled")

# web / status の表示の語
NOT_CONFIGURED = "not_configured"
UNKNOWN = "unknown"
STALE = "stale"
PERMANENT_FAILURE = "permanent-failure"
HEALTHY = "healthy"
UNHEALTHY = "unhealthy"
STALLED = "stalled"
STATES = (HEALTHY, UNHEALTHY, STALLED, STALE, UNKNOWN, NOT_CONFIGURED, PERMANENT_FAILURE)

_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")


def format_time(epoch: float) -> str:
    return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: object) -> float | None:
    if type(value) is not str or _TIMESTAMP.fullmatch(value) is None:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return parsed.timestamp()


# ------------------------------------------------------------------ 検査と正規化

def parse(raw: bytes) -> dict[str, Any] | None:
    """exact schema の検査。違反は None (呼ぶ側は unknown にする)。"""
    if type(raw) is not bytes or len(raw) > MAX_BYTES:
        return None
    try:
        record = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if type(record) is not dict or set(record) != set(FIELDS):
        return None
    version = record["version"]
    if type(version) is not int or version != VERSION:
        return None
    if _parse_time(record["observed_at"]) is None:
        return None
    for name in _TIME_FIELDS:
        if record[name] is not None and _parse_time(record[name]) is None:
            return None
    if record["gateway_state"] not in GATEWAY_STATES or record["delivery_state"] not in DELIVERY_STATES:
        return None
    if type(record["gateway_state"]) is not str or type(record["delivery_state"]) is not str:
        return None
    latency = record["last_ack_latency_ms"]
    if latency is not None:
        if type(latency) not in (int, float) or not math.isfinite(latency) \
                or not 0 <= latency <= MAX_LATENCY_MS:
            return None
    terminal, code = record["terminal"], record["terminal_code"]
    if terminal is None:
        if code is not None:
            return None
    elif terminal == "permanent":
        if type(code) is not str or code not in PERMANENT_CODES:
            return None
    elif terminal == "transient":
        if type(code) is not str or code not in TRANSIENT_CODES:
            return None
    else:
        return None
    return record


def _reject_constant(_name: str) -> Any:
    raise ValueError("non-finite")


def evaluate(record: Mapping[str, Any] | None, now: float) -> dict[str, Any]:
    """検査済みの record (無ければ None) を、表示の語と非機密の値に正規化する。"""
    if record is None:
        return {"state": UNKNOWN, "observed_at": None, "gateway_state": None,
                "delivery_state": None, "terminal_code": None}
    observed = _parse_time(record["observed_at"])
    gateway, delivery = record["gateway_state"], record["delivery_state"]
    terminal_code = record["terminal_code"]
    if record["terminal"] == "permanent":
        state = PERMANENT_FAILURE
    elif observed is None or now - observed > STALE_SECONDS:
        state = STALE
    elif gateway != "healthy" or record["terminal"] == "transient":
        state = UNHEALTHY
    elif delivery == "stalled":
        state = STALLED
    else:
        state = HEALTHY
    return {"state": state, "observed_at": record["observed_at"], "gateway_state": gateway,
            "delivery_state": delivery, "terminal_code": terminal_code}


# ------------------------------------------------------------------ path と reader

def resolve_state_dir(environ: Mapping[str, str] | None = None, home: str | None = None) -> Path:
    """bot の state dir。絶対 path の `XDG_STATE_HOME` だけを採用し、空・相対は既定の `~/.local/state`。"""
    env = os.environ if environ is None else environ
    value = env.get("XDG_STATE_HOME", "")
    base = Path(value) if value and os.path.isabs(value) else (
        Path(home if home is not None else os.path.expanduser("~")) / ".local/state")
    return base / "agentic-fx" / "ops" / "bot"


def read_raw(directory: os.PathLike | str) -> bytes | None:
    """安全に 1 回だけ読む。symlink・FIFO・directory・過大・I/O 失敗は None。"""
    path = os.path.join(os.fspath(directory), SNAPSHOT_NAME)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            return None
        data = os.read(fd, MAX_BYTES + 1)
        return None if len(data) > MAX_BYTES else data
    except OSError:
        return None
    finally:
        os.close(fd)


def read_health(directory: os.PathLike | str, now: float) -> dict[str, Any]:
    raw = read_raw(directory)
    return evaluate(None if raw is None else parse(raw), now)


# ------------------------------------------------------------------ 設定との合成

ALTERNATE_NONE = "none"
ALTERNATE_CONFIGURED = "configured"


def _enabled(root: Path, feature: str) -> bool | None:
    """機能の enable が完了しているか。stage を読めないときは None。"""
    from agentic_fx.ops_setup.stages import read_stage
    try:
        stage = read_stage(root, feature, missing_ok=True)
    except Exception:  # noqa: BLE001 — 表示専用。読めない = 判断しない
        return None
    return stage is not None and stage.get("operation") == "enable" and stage.get("stage") == "enabled"


def alternate_push(root: Path) -> str:
    """notify の enable 完了状態だけで決める。秘密 file・webhook URL は読まない。読めなければ none。"""
    return ALTERNATE_CONFIGURED if _enabled(root, "notify") is True else ALTERNATE_NONE


def collect(root: Path, now: float, *, state_dir: os.PathLike | str | None = None) -> dict[str, Any]:
    """status の `bot_health`。非機密の値だけ。どの失敗でも例外を出さず unknown にする。"""
    try:
        push = alternate_push(root)
    except Exception:  # noqa: BLE001
        push = ALTERNATE_NONE
    try:
        enabled = _enabled(root, "bot")
        if enabled is False:
            result = evaluate(None, now)
            result["state"] = NOT_CONFIGURED
        elif enabled is None:
            result = evaluate(None, now)
        else:
            result = read_health(resolve_state_dir() if state_dir is None else state_dir, now)
    except Exception:  # noqa: BLE001
        result = evaluate(None, now)
    result["interaction"] = result["state"] == HEALTHY
    result["alternate_push"] = push
    return result


WARNING_ALTERNATE_NONE = "alternate_push=none"


def lines(health: Mapping[str, Any] | None) -> list[str]:
    """`afx status` / `afx ops bot status` の表示行。固定語だけ。"""
    if not isinstance(health, Mapping):
        return []
    state = health.get("state")
    state = state if isinstance(state, str) and state in STATES else UNKNOWN
    push = health.get("alternate_push")
    push = push if push in (ALTERNATE_NONE, ALTERNATE_CONFIGURED) else ALTERNATE_NONE
    out = [f"bot health: {state} alternate_push={push}"]
    if push == ALTERNATE_NONE:
        out.append(f"warning: {WARNING_ALTERNATE_NONE}")
    return out
