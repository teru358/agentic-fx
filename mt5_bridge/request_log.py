"""bridge の要求ごとの構造化記録。1 要求 1 行の JSON を `logs/bridge-metrics/YYYY-MM-DD.jsonl` へ書く。

要求の処理は有界 queue への put_nowait だけを行い、書込みを待たない (満杯は捨てて数える)。
書込みは daemon thread の writer が受け持ち、UTC の日付入りの file へ追記する (改名しない)。
14 日を超えた自分の名前の形の通常 file は、日付の変わり目に writer が消す。
記録の失敗は応答へ出さない。停止は期限付きで、writer が詰まっていても待ち続けない。
行には URL・query・API キー・生の header・boot id・価格・履歴 slot の ID を書かない。
"""
from __future__ import annotations

import atexit
import json
import os
import queue
import re
import stat
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import bridge_metrics

CALLER_HEADER = "X-Afx-Caller"
CALLERS = frozenset({"tick", "mission", "cli"})
RETENTION_DAYS = 14
DEFAULT_QUEUE_SIZE = 2048
STOP_TIMEOUT_S = 2.0
_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

_LOCK_STATES = frozenset({"measured", "not_locked", "unavailable"})
_SLOT_STATES = frozenset({"none", "waiting", "holding", "completed"})
_SIMPLE_ROUTES = frozenset({
    "/health", "/account", "/server-time", "/positions", "/symbols", "/order",
    "/admin/status", "/admin/halt", "/admin/resume",
})


def caller_of(raw: bytes | None) -> str:
    """request header `X-Afx-Caller` の生 byte を固定集合と照合する。他・欠落は unknown。"""
    if raw is None:
        return "unknown"
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return "unknown"
    return text if text in CALLERS else "unknown"


def route_of(path: str) -> str:
    """path から固定の route 名を作る。生の path は残さない。末尾の `/` は無視する。"""
    if path.endswith("/") and len(path) > 1:
        path = path.rstrip("/")
    if path in _SIMPLE_ROUTES:
        return path
    parts = path.split("/")
    if len(parts) == 3 and parts[0] == "" and parts[2] != "":
        if parts[1] == "quote":
            return "/quote/{symbol}"
        if parts[1] == "ohlcv":
            return "/ohlcv/{symbol}"
    if len(parts) == 4 and parts[0] == "" and parts[1] == "positions" and parts[2] != "":
        if parts[3] in ("closed-deal", "modify", "close"):
            return f"/positions/{{ticket}}/{parts[3]}"
    return "/other"


def _number(headers: dict[str, str], name: str) -> float | None:
    try:
        return float(headers[name])
    except (KeyError, ValueError):
        return None


def build_row(*, caller_raw: bytes | None, path: str, status: int,
              error_code: str | None, headers: list[tuple[str, str]]) -> dict[str, Any]:
    """1 要求 1 行。`headers` は応答に付けた計測 header。時刻はここで 1 回だけ取る。"""
    named = {name: value for name, value in headers}
    state = named.get(bridge_metrics.H_LOCK_STATE)
    if state not in _LOCK_STATES:
        state = "unavailable"
    figures = state != "unavailable"
    count = _number(named, bridge_metrics.H_LOCK_ACQ) if figures else None
    slot = named.get(bridge_metrics.H_HISTORY_STATE)
    if error_code is not None and error_code not in bridge_metrics.ERROR_CODES:
        error_code = "unknown"
    return {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "caller": caller_of(caller_raw),
        "route": route_of(path),
        "status": status,
        "error_code": error_code,
        "lock_state": state,
        "wait_ms": _number(named, bridge_metrics.H_LOCK_WAIT) if figures else None,
        "hold_ms": _number(named, bridge_metrics.H_LOCK_HOLD) if figures else None,
        "wait_max_ms": _number(named, bridge_metrics.H_LOCK_WAIT_MAX) if figures else None,
        "hold_max_ms": _number(named, bridge_metrics.H_LOCK_HOLD_MAX) if figures else None,
        "acquisitions": int(count) if count is not None else None,
        "history_state": slot if slot in _SLOT_STATES else None,
    }


def _day_of(row: dict[str, Any]) -> str:
    """行の ts と同じ時刻から file の日付を作る (ts が読めないときだけ現在の UTC)。"""
    ts = row.get("ts")
    if isinstance(ts, str) and _DAY_RE.fullmatch(ts[:10]):
        return ts[:10]
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class _DailyWriter:
    """有界 queue と daemon thread の writer。UTC 日付入りの file へ追記し、古い file を消す。"""

    def __init__(self, directory: Path, queue_size: int) -> None:
        self.directory = directory
        self.queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._pattern = re.compile(r"(\d{4}-\d{2}-\d{2})\.jsonl")
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._run, name="bridge-request-log", daemon=True)
        self._fd: int | None = None
        self._fd_day: str | None = None
        self._purged_day: str | None = None

    def start(self) -> None:
        self._thread.start()

    def submit(self, day: str, line: str) -> bool:
        """待たずに積む。満杯なら False。"""
        try:
            self.queue.put_nowait((day, line))
            return True
        except queue.Full:
            return False

    def flush(self, timeout: float = 10.0) -> None:
        """積まれた行が処理されるまで期限付きで待つ (停止・試験用。要求の処理からは呼ばない)。"""
        deadline = time.monotonic() + timeout
        while self.queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)

    def stop(self, timeout: float) -> None:
        """終了を伝えて期限付きで待つ。期限を過ぎたら待たずに戻る。"""
        self._stopping.set()
        self._thread.join(timeout)

    def _run(self) -> None:
        try:
            while True:
                try:
                    day, line = self.queue.get(timeout=0.2)
                except queue.Empty:
                    if self._stopping.is_set():
                        return
                    continue
                try:
                    if day != self._purged_day:
                        self._purged_day = day
                        self._purge(day)
                    self._append(day, line)
                except Exception:  # noqa: BLE001 - 記録の失敗は握る
                    self._close()
                finally:
                    self.queue.task_done()
        finally:
            self._close()

    def _close(self) -> None:
        fd, self._fd, self._fd_day = self._fd, None, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def _append(self, day: str, line: str) -> None:
        if self._fd is None or self._fd_day != day:
            self._close()
            self._fd = self._open(day)
            self._fd_day = day
        data = (line + "\n").encode("utf-8")
        while data:
            data = data[os.write(self._fd, data):]

    def _ensure_directory(self) -> None:
        """記録先 dir を作る (親の `logs` が無ければ作らない)。POSIX では 0700。"""
        try:
            self.directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise OSError("log directory is not a plain directory")
        if hasattr(os, "geteuid"):
            info = self.directory.stat()
            if info.st_uid != os.geteuid():
                raise OSError("log directory is not owned by this user")
            if stat.S_IMODE(info.st_mode) != 0o700:
                os.chmod(self.directory, 0o700)

    def _open(self, day: str) -> int:
        self._ensure_directory()
        flags = (os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
        fd = os.open(self.directory / f"{day}.jsonl", flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("log file is not a regular file")
            if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
                raise OSError("log file is not owned by this user")
            if hasattr(os, "fchmod") and stat.S_IMODE(info.st_mode) != 0o600:
                os.fchmod(fd, 0o600)
            if flags & getattr(os, "O_NONBLOCK", 0):
                os.set_blocking(fd, True)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _purge(self, today: str) -> None:
        """14 日を超えた `YYYY-MM-DD.jsonl` の通常 file だけを消す。他の名前・dir・symlink は残す。"""
        try:
            limit = date.fromisoformat(today) - timedelta(days=RETENTION_DAYS)
            entries = list(os.scandir(self.directory))
        except (OSError, ValueError):
            return
        for entry in entries:
            found = self._pattern.fullmatch(entry.name)
            if found is None:
                continue
            try:
                if date.fromisoformat(found.group(1)) < limit and entry.is_file(
                        follow_symlinks=False):
                    os.unlink(entry.path)
            except (OSError, ValueError):
                continue


class RequestLog:
    """記録先 1 つ分の writer。start 前・stop 後の emit は何もしない。"""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._writer: _DailyWriter | None = None
        self._dropped = 0
        self._reported = 0

    @property
    def active(self) -> bool:
        return self._writer is not None

    @property
    def dropped(self) -> int:
        return self._dropped

    def _count_drop(self, carried: int = 0) -> None:
        """捨てた行を数える。その行が載せていた欠落数は未報告へ戻す。"""
        with self._guard:
            self._dropped += 1
            self._reported -= carried

    def start(self, directory: Path, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        self.stop()
        try:
            writer = _DailyWriter(Path(directory), queue_size)
            writer.start()
        except Exception:  # noqa: BLE001 - 記録が起動できなくても bridge は動く
            return
        with self._guard:
            self._dropped = 0
            self._reported = 0
        self._writer = writer
        atexit.register(self.stop)

    def stop(self, timeout: float = STOP_TIMEOUT_S) -> None:
        writer, self._writer = self._writer, None
        if writer is None:
            return
        try:
            writer.stop(timeout)
        except Exception:  # noqa: BLE001 - 停止の失敗で bridge の終了を止めない
            pass
        finally:
            atexit.unregister(self.stop)

    def flush(self) -> None:
        """積まれた行が書かれるまで期限付きで待つ (試験用。要求の処理からは呼ばない)。"""
        writer = self._writer
        if writer is not None:
            writer.flush()

    def emit(self, row: dict[str, Any]) -> None:
        writer = self._writer
        if writer is None:
            return
        try:
            with self._guard:
                pending = max(self._dropped - self._reported, 0)
                self._reported += pending
            if pending > 0:
                row = {**row, "dropped_before": pending}
            line = json.dumps(row, separators=(",", ":"), ensure_ascii=True)
            if not writer.submit(_day_of(row), line):
                self._count_drop(pending)
        except Exception:  # noqa: BLE001
            return


request_log = RequestLog()
