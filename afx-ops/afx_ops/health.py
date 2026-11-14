"""bot の health snapshot (`gateway.snapshot` version 1) の schema・writer・reader。

bot 本体の状態を、cursor を持つ `bot.snapshot` とは別の file に、表示専用で置く。daemon (本体
package) は `afx_ops` を import できないので、同じ契約の reader を `agentic_fx.ops.bot_health` が
別に持つ。両方を同じ golden JSON で検査する。

snapshot には ID・URL・session・event 本文・例外文を入れない。field は固定、terminal_code は固定の
allowlist だけ、数値は有限だけ。
"""
from __future__ import annotations

import json
import math
import os
import re
import stat
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from afx_ops.bot.exits import PERMANENT_REASONS, TRANSIENT_REASONS
from afx_ops.snapshot import SnapshotError, write_snapshot

SNAPSHOT_NAME = "gateway.snapshot"
VERSION = 1
MAX_BYTES = 4096
# observed_at がこの秒数より古ければ stale
STALE_SECONDS = 120.0
# 書込みの周期 (遷移が無くても最新を置く) と、遷移を見つける周期
WRITE_INTERVAL = 60.0
TICK_INTERVAL = 1.0
# 終端 snapshot を待つ上限
TERMINAL_WAIT = 1.0
# 配送がこれ以上詰まっていれば stalled
DELIVERY_STALL_SECONDS = 120.0
MAX_LATENCY_MS = 3_600_000.0

# terminal_code に入れてよい固定語。恒久 / 一時で分ける (別の側の語は拒否)
SHUTDOWN_TRANSIENT_CODES = frozenset({"daemon_unreachable"})
PERMANENT_CODES = frozenset(PERMANENT_REASONS)
TRANSIENT_CODES = frozenset(TRANSIENT_REASONS) | SHUTDOWN_TRANSIENT_CODES
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


# ------------------------------------------------------------------ snapshot の組立て

def _wall(value: float | None, mono_now: float, wall_now: float) -> str | None:
    if value is None:
        return None
    return format_time(wall_now - (mono_now - value))


def build(tracker, *, wall_now: float, mono_now: float, latency_ms: object = None,
          loop_acknowledged: float | None = None, terminal: str | None = None,
          terminal_code: str | None = None) -> dict[str, Any]:
    """`ProgressTracker` の今の値から snapshot を作る。monotonic の時刻は wall clock に換算する。"""
    stalled = tracker.delivery_stalled_since
    if stalled is not None and mono_now - stalled >= DELIVERY_STALL_SECONDS:
        delivery = "stalled"
    elif tracker.last_delivery_success_at is not None:
        delivery = "healthy"
    else:
        delivery = "idle"
    latency: float | int | None = None
    if type(latency_ms) in (int, float) and math.isfinite(latency_ms) \
            and 0 <= latency_ms <= MAX_LATENCY_MS:
        latency = latency_ms
    since = tracker.gateway_unhealthy_since
    return {
        "version": VERSION, "observed_at": format_time(wall_now),
        "gateway_state": tracker.gateway_state,
        "gateway_unhealthy_since": _wall(since, mono_now, wall_now),
        "last_ready_at": _wall(tracker.last_ready_at, mono_now, wall_now),
        "last_resumed_at": _wall(tracker.last_resumed_at, mono_now, wall_now),
        "last_disconnect_at": _wall(tracker.last_disconnect_at, mono_now, wall_now),
        "last_ack_latency_ms": latency,
        "delivery_state": delivery,
        "delivery_stalled_since": _wall(stalled, mono_now, wall_now),
        "last_delivery_success_at": _wall(tracker.last_delivery_success_at, mono_now, wall_now),
        "poll_started_at": _wall(tracker.poll_started_at, mono_now, wall_now),
        "last_poll_completed_at": _wall(tracker.last_poll_completed_at, mono_now, wall_now),
        "loop_acknowledged_at": _wall(loop_acknowledged, mono_now, wall_now),
        "terminal": terminal, "terminal_code": terminal_code,
    }


def terminal_for(reason: object) -> tuple[str, str] | None:
    """失敗理由 (固定語) を (terminal, terminal_code) にする。表に無い語は None (保存しない)。"""
    if reason in PERMANENT_CODES:
        return "permanent", str(reason)
    if reason in TRANSIENT_CODES:
        return "transient", str(reason)
    return None


# ------------------------------------------------------------------ 単一 writer

class HealthWriter:
    """snapshot を置く唯一の worker。daemon thread 1 本で直列化し、loop thread は fsync しない。

    `submit` は最新の 1 件だけを slot に置く (書込み中に来た分は上書きされる)。書込みの失敗は
    bot を止めず、固定語を遷移時に 1 回だけ `say` へ出す。
    """

    def __init__(self, dir_fd: int, *, say: Callable[[str], None] | None = None,
                 name: str = SNAPSHOT_NAME) -> None:
        self._dir_fd = dir_fd
        self._name = name
        self._say = say
        self._lock = threading.Lock()
        self._slot: tuple[int, dict] | None = None
        self._seq = 0
        self._written = 0
        self._wake = threading.Event()
        self._done = threading.Condition(self._lock)
        self._halt = False
        self._failing = False
        self._thread = threading.Thread(target=self._run, name="afx-ops-health", daemon=True)
        self._thread.start()

    def submit(self, snapshot: dict) -> int:
        """最新の 1 件として置く。返す番号は `wait` で書込みの完了を待つのに使う。"""
        with self._lock:
            self._seq += 1
            self._slot = (self._seq, snapshot)
            seq = self._seq
        self._wake.set()
        return seq

    def wait(self, seq: int, timeout: float) -> bool:
        """`seq` 以後の書込みが終わる (成功・失敗を問わず) まで最大 `timeout` 秒待つ。"""
        with self._done:
            return self._done.wait_for(lambda: self._written >= seq, timeout)

    def _run(self) -> None:
        while True:
            self._wake.wait()
            with self._lock:
                halted = self._halt
                item, self._slot = self._slot, None
                self._wake.clear()
            if item is None:
                if halted:
                    return
                continue
            seq, snapshot = item
            ok = self._write(snapshot)
            with self._done:
                self._written = max(self._written, seq)
                self._done.notify_all()
            self._report(ok)
            if halted:
                with self._lock:
                    if self._slot is None:
                        return
                self._wake.set()

    def _write(self, snapshot: dict) -> bool:
        try:
            write_snapshot(self._dir_fd, self._name, snapshot)
            return True
        except (SnapshotError, OSError, ValueError, TypeError):
            return False
        except Exception:  # noqa: BLE001 — 表示専用の書込みは bot を止めない
            return False

    def _report(self, ok: bool) -> None:
        if ok:
            self._failing = False
            return
        if not self._failing:
            self._failing = True
            if self._say is not None:
                try:
                    self._say("health_snapshot_write_failed")
                except Exception:  # noqa: BLE001
                    pass

    def close(self, timeout: float = TERMINAL_WAIT) -> None:
        """worker を止め、持っている dir fd を閉じる。止まらなくても待ち続けない。"""
        with self._lock:
            self._halt = True
        self._wake.set()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout)
        if not self._thread.is_alive():
            try:
                os.close(self._dir_fd)
            except OSError:
                pass




class Publisher:
    """tracker の値から snapshot を作り、遷移時と一定周期に writer へ最新 1 件を出す。

    組立ては event loop thread (pure、I/O なし)。置換は writer の thread。
    """

    def __init__(self, writer: HealthWriter, tracker, *, wall: Callable[[], float],
                 mono: Callable[[], float], loop_acknowledged: Callable[[], float | None] | None = None,
                 interval: float = WRITE_INTERVAL) -> None:
        self._writer = writer
        self._tracker = tracker
        self._wall = wall
        self._mono = mono
        self._loop_ack = loop_acknowledged
        self._latency: Callable[[], object] | None = None
        self._interval = interval
        self._last_key: tuple | None = None
        self._last_at: float | None = None
        self._terminal: tuple[str, str] | None = None

    def bind_latency(self, source: Callable[[], object]) -> None:
        """`Client.latency` (秒) の取り出し口。表示だけに使い、健全性の根拠にしない。"""
        self._latency = source

    def _safe(self, source: Callable[[], Any] | None) -> Any:
        if source is None:
            return None
        try:
            return source()
        except Exception:  # noqa: BLE001 — 補助の値が取れなくても snapshot は出す (null)
            return None

    def snapshot(self) -> dict | None:
        seconds = self._safe(self._latency)
        latency = seconds * 1000.0 if type(seconds) in (int, float) and math.isfinite(seconds) else None
        ack = self._safe(self._loop_ack)
        terminal, code = self._terminal if self._terminal is not None else (None, None)
        try:
            return build(self._tracker, wall_now=self._wall(), mono_now=self._mono(),
                         latency_ms=latency, loop_acknowledged=ack if type(ack) in (int, float) else None,
                         terminal=terminal, terminal_code=code)
        except Exception:  # noqa: BLE001 — 表示専用。組めなければ出さない
            return None

    def publish(self, *, force: bool = False) -> None:
        snap = self.snapshot()
        if snap is None:
            return
        key = (snap["gateway_state"], snap["delivery_state"], snap["terminal"])
        now = self._mono()
        if force or key != self._last_key or self._last_at is None \
                or now - self._last_at >= self._interval:
            self._last_key, self._last_at = key, now
            self._writer.submit(snap)

    def finalize(self, reason: object) -> None:
        """終了の判断 (恒久 / 一時と固定の code) を最大 1 秒だけ待って保存する。書けなくても戻る。"""
        decided = terminal_for(reason)
        if decided is None:
            return
        self._terminal = decided
        snap = self.snapshot()
        if snap is None:
            return
        try:
            seq = self._writer.submit(snap)
            self._writer.wait(seq, TERMINAL_WAIT)
        except Exception:  # noqa: BLE001
            pass

    async def run(self, sleep=None) -> None:
        """loop 上の周期 task。遷移を `TICK_INTERVAL` 以内に見つける。"""
        import asyncio
        sleeper = asyncio.sleep if sleep is None else sleep
        while True:
            self.publish()
            await sleeper(TICK_INTERVAL)

    def close(self) -> None:
        self._writer.close()
