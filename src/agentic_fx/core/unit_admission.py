"""afx 本体が自分の systemd user unit を同定し、delegation と停止の層を admission で検査する部品。

改善 mission の CLI 部分木を mission cgroup に閉じ込めるには、afx が `Delegate=pids memory` の
user service の main PID であり、unit の停止が有限時間で cgroup 全体を SIGKILL することが前提になる。
その前提を起動時に一度だけ確かめる。

- unit 名は libsystemd の `sd_pid_get_user_unit(0)` からだけ得る。失敗に fallback を持たない
  (`/proc/self/cgroup` を自前で解釈すると、unit の外 (session scope 等) を unit と取り違え得る)。
- property は固定 argv の `systemctl --user show` だけで読む。実行 file・env・入出力を固定し、
  出力は 11 key がちょうど各 1 回であることを要求する (systemctl は未知の property 名を
  黙って捨てるので、key の数え上げが唯一の検査になる)。
- 判定の失敗は全部 `AdmissionError` (固定 reason) で、呼び出し側は backend を fail closed にする。

配線前は隔離の保証なし (未有効): この module は afx の起動経路と改善 backend の選択には
まだ繋がっていない。
"""
from __future__ import annotations

import ctypes
import errno
import os
import re
import select
import stat
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable

REASON_DELEGATION_MISSING = "cgroup_delegation_missing"
REASON_TOPOLOGY_UNSUPPORTED = "cgroup_topology_unsupported"
REASON_STOP_POLICY_UNSUPPORTED = "unit_stop_policy_unsupported"
ADMISSION_REASONS = frozenset({
    REASON_DELEGATION_MISSING, REASON_TOPOLOGY_UNSUPPORTED, REASON_STOP_POLICY_UNSUPPORTED})

LIBSYSTEMD = "libsystemd.so.0"
USER_UNIT_SYMBOL = "sd_pid_get_user_unit"

SHOW_PROPERTIES = (
    "Delegate", "DelegateControllers", "MainPID",
    "Type", "NotifyAccess", "KillMode",
    "TimeoutStopUSec", "SendSIGKILL", "FinalKillSignal",
    "TimeoutStopFailureMode", "TimeoutAbortUSec",
)
SYSTEMCTL_CANDIDATES = ("/usr/bin/systemctl", "/bin/systemctl")
SHOW_TIMEOUT_SEC = 5
SHOW_STDOUT_LIMIT = 4096

#: systemd の unit 名の上限 (NUL を含まず 255)
_UNIT_NAME_MAX = 255
_SERVICE_NAME = re.compile(r"[A-Za-z0-9:_.\\-]+(@[A-Za-z0-9:_.\\-]+)?\.service")
_DECIMAL_PID = re.compile(r"[1-9][0-9]*")
#: systemctl show の duration 表示 (format_timespan) の 1 項
#: 整数部・小数部とも 20 桁まで (show の出力上限内で現実に取り得る範囲)。float を経由しない
_TIMESPAN_TERM = re.compile(r"([0-9]{1,20})(?:\.([0-9]{1,20}))?(y|month|w|d|h|min|s|ms|us|µs)")
_TIMESPAN_USEC = {
    "y": 31_557_600_000_000, "month": 2_629_800_000_000, "w": 604_800_000_000,
    "d": 86_400_000_000, "h": 3_600_000_000, "min": 60_000_000, "s": 1_000_000,
    "ms": 1_000, "us": 1, "µs": 1,
}


class AdmissionError(Exception):
    """admission の失敗。`reason` は固定 reason、`detail` は運用者向けの説明。"""

    def __init__(self, reason: str, detail: str, properties: dict[str, str] | None = None):
        assert reason in ADMISSION_REASONS
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail
        self.properties = properties


@dataclass(frozen=True)
class UnitAdmission:
    unit: str
    properties: dict[str, str] = field(default_factory=dict)


def _delegation(detail: str) -> AdmissionError:
    return AdmissionError(REASON_DELEGATION_MISSING, detail)


# ------------------------------------------------------------------ unit の同定


def _libc_free() -> Callable[[ctypes.c_void_p], None]:
    libc = ctypes.CDLL(None)
    free = libc.free
    free.argtypes = [ctypes.c_void_p]
    free.restype = None
    return free


def _errno_name(rc: int) -> str:
    return errno.errorcode.get(-rc, str(-rc))


def is_service_unit_name(name: str) -> bool:
    return len(name) <= _UNIT_NAME_MAX and _SERVICE_NAME.fullmatch(name) is not None


def unit_name_from_result(rc: int, pointer: int | None, *,
                          free: Callable[[ctypes.c_void_p], None],
                          read: Callable[[int], bytes] = ctypes.string_at) -> str:
    """`sd_pid_get_user_unit` の戻り値と出力 pointer から unit 名を取り出す。

    非 null pointer は成否によらず copy の後に一度だけ free する (libsystemd が malloc した文字列)。
    """
    try:
        if rc < 0:
            raise _delegation(f"sd_pid_get_user_unit が {_errno_name(rc)} を返した")
        if not pointer:
            raise _delegation("sd_pid_get_user_unit が成功したのに pointer が null")
        raw = read(pointer)
    finally:
        if pointer:
            free(ctypes.c_void_p(pointer))
    try:
        name = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise _delegation("unit 名が UTF-8 でない") from None
    if not is_service_unit_name(name):
        raise _delegation(f"unit 名が .service の文法外: {name[:80]!r}")
    return name


def identify_user_unit(library: str = LIBSYSTEMD, symbol: str = USER_UNIT_SYMBOL) -> str:
    """自分 (pid 0) の user unit 名を libsystemd に一度だけ問い合わせる。fallback は持たない。"""
    try:
        lib = ctypes.CDLL(library)
    except OSError as e:
        raise _delegation(f"{library} を dlopen できない (OSError: {e})") from None
    try:
        fn = getattr(lib, symbol)
    except AttributeError as e:
        raise _delegation(f"{symbol} が無い (AttributeError: {e})") from None
    fn.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
    fn.restype = ctypes.c_int
    free = _libc_free()
    out = ctypes.c_void_p(None)
    rc = fn(0, ctypes.byref(out))
    return unit_name_from_result(rc, out.value, free=free)


# ------------------------------------------------------------------ systemctl show


def check_systemctl_executable(path: str) -> None:
    """root 所有、group / other 非書込みの通常 file (symlink 不可) であることを確かめる。"""
    try:
        st = os.lstat(path)
    except OSError as e:
        raise _delegation(f"{path} を lstat できない ({e.strerror})") from None
    if stat.S_ISLNK(st.st_mode):
        raise _delegation(f"{path} が symlink")
    if not stat.S_ISREG(st.st_mode):
        raise _delegation(f"{path} が通常 file でない")
    if st.st_uid != 0:
        raise _delegation(f"{path} の所有者が root でない (uid {st.st_uid})")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise _delegation(f"{path} が group / other から書込み可能 ({oct(st.st_mode & 0o7777)})")


def locate_systemctl(candidates: tuple[str, ...] = SYSTEMCTL_CANDIDATES) -> str:
    """先頭から最初に存在する候補を検査して返す。存在するが不合格の候補を飛ばして次へは行かない。"""
    for path in candidates:
        if os.path.lexists(path):
            check_systemctl_executable(path)
            return path
    raise _delegation(f"systemctl が無い ({', '.join(candidates)})")


def show_argv(path: str, unit: str) -> list[str]:
    argv = [path, "--user", "--no-pager", "show"]
    for prop in SHOW_PROPERTIES:
        argv += ["-p", prop]
    argv += ["--", unit]
    return argv


def sanitized_env(uid: int | None = None) -> dict[str, str]:
    """systemctl に渡す env の全部。bus の場所は uid から作る runtime dir だけで決まる。"""
    if uid is None:
        uid = os.getuid()
    return {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
        "SYSTEMD_PAGER": "",
        "PAGER": "cat",
        "SYSTEMD_LOG_LEVEL": "err",
        "XDG_RUNTIME_DIR": f"/run/user/{uid}",
    }


def _kill_and_reap(proc: subprocess.Popen) -> None:
    try:
        proc.kill()
    except OSError:
        pass
    proc.wait()


def run_show(argv: list[str], env: dict[str, str], *, timeout: float = SHOW_TIMEOUT_SEC,
             limit: int = SHOW_STDOUT_LIMIT) -> bytes:
    """argv を一度だけ起こし、stdout を上限つきで読む。timeout・上限超過・非 0 終了は fail。"""
    deadline = time.monotonic() + timeout
    try:
        proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, close_fds=True)
    except OSError as e:
        raise _delegation(f"systemctl を起こせない ({e.strerror})") from None
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _delegation(f"systemctl が {timeout} 秒の timeout 内に終わらない")
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(fd, limit + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise _delegation(f"systemctl の出力が {limit} byte を超えた")
        remaining = deadline - time.monotonic()
        try:
            rc = proc.wait(timeout=max(remaining, 0))
        except subprocess.TimeoutExpired:
            raise _delegation(f"systemctl が {timeout} 秒の timeout 内に終わらない") from None
    except BaseException:
        _kill_and_reap(proc)
        raise
    finally:
        proc.stdout.close()
    if rc != 0:
        raise _delegation(f"systemctl が終了 code {rc} (user manager に届かない等)")
    return b"".join(chunks)


def parse_show_output(raw: bytes) -> dict[str, str]:
    """`KEY=VALUE` の行がちょうど 11 key 各 1 回であることを要求して dict にする。"""
    if len(raw) > SHOW_STDOUT_LIMIT:
        raise _delegation(f"show の出力が {SHOW_STDOUT_LIMIT} byte を超えた")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise _delegation("show の出力が UTF-8 でない") from None
    if "\x00" in text or "\r" in text:
        raise _delegation("show の出力に NUL / CR がある")
    if text.endswith("\n"):
        text = text[:-1]
    if not text:
        raise _delegation("show の出力が空")
    props: dict[str, str] = {}
    for line in text.split("\n"):
        key, sep, value = line.partition("=")
        if not sep:
            raise _delegation(f"show の行に = が無い: {line[:80]!r}")
        if key not in SHOW_PROPERTIES:
            raise _delegation(f"show の出力に未知の key: {key[:80]!r}")
        if key in props:
            raise _delegation(f"show の出力で key が重複: {key}")
        if value != value.strip():
            raise _delegation(f"show の値の前後に空白: {key}")
        props[key] = value
    missing = [k for k in SHOW_PROPERTIES if k not in props]
    if missing:
        raise _delegation(f"show の出力に key が無い: {', '.join(missing)}")
    return props


def parse_timespan_usec(text: str) -> int | None:
    """systemctl show の duration 表示を usec にする。`infinity` は None。形式違反は ValueError。"""
    if text == "infinity":
        return None
    if text == "0":
        return 0
    if not text:
        raise ValueError("空の duration")
    total = 0
    for term in text.split(" "):
        m = _TIMESPAN_TERM.fullmatch(term)
        if m is None:
            raise ValueError(f"duration の形式外: {text!r}")
        whole, frac, unit = m.groups()
        scale = _TIMESPAN_USEC[unit]
        total += int(whole) * scale
        if frac:
            # 小数部は整数のまま積み、usec 未満は切り捨てる
            total += int(frac) * scale // 10 ** len(frac)
    return total


def judge_properties(props: dict[str, str], main_pid: int) -> None:
    """property を判定する。delegation → main PID → 停止の層の順に、最初の不足の reason で失敗。"""

    def fail(reason: str, detail: str) -> AdmissionError:
        return AdmissionError(reason, detail, dict(props))

    if props["Delegate"] != "yes":
        raise fail(REASON_DELEGATION_MISSING, f"Delegate={props['Delegate']!r}")
    controllers = set(props["DelegateControllers"].split(" ")) - {""}
    if not {"pids", "memory"} <= controllers:
        raise fail(REASON_DELEGATION_MISSING,
                   f"DelegateControllers={props['DelegateControllers']!r} に pids と memory が無い")
    if _DECIMAL_PID.fullmatch(props["MainPID"]) is None or int(props["MainPID"]) != main_pid:
        raise fail(REASON_TOPOLOGY_UNSUPPORTED,
                   f"MainPID={props['MainPID']!r} が自分 ({main_pid}) でない")

    stop = REASON_STOP_POLICY_UNSUPPORTED
    for key, want in (("Type", "exec"), ("NotifyAccess", "none"), ("KillMode", "control-group"),
                      ("SendSIGKILL", "yes"), ("FinalKillSignal", "9")):
        if props[key] != want:
            raise fail(stop, f"{key}={props[key]!r} (要求 {want})")
    try:
        stop_usec = parse_timespan_usec(props["TimeoutStopUSec"])
    except (ValueError, OverflowError) as e:
        raise fail(stop, f"TimeoutStopUSec: {e}") from None
    # systemd は TimeoutStopSec=0 を「timeout 無し」として infinity と表示する。どちらも拒否
    if stop_usec is None or stop_usec <= 0:
        raise fail(stop, f"TimeoutStopUSec={props['TimeoutStopUSec']!r} が正の有限値でない")
    if props["TimeoutStopFailureMode"] not in ("terminate", "kill"):
        raise fail(stop, f"TimeoutStopFailureMode={props['TimeoutStopFailureMode']!r}")
    try:
        parse_timespan_usec(props["TimeoutAbortUSec"])
    except (ValueError, OverflowError) as e:
        raise fail(stop, f"TimeoutAbortUSec: {e}") from None


def admit_unit(*, main_pid: int | None = None, library: str = LIBSYSTEMD,
               symbol: str = USER_UNIT_SYMBOL) -> UnitAdmission:
    """unit の同定 → systemctl の検査と一度の起動 → 出力の解析 → property の判定。"""
    if main_pid is None:
        main_pid = os.getpid()
    unit = identify_user_unit(library, symbol)
    path = locate_systemctl()
    raw = run_show(show_argv(path, unit), sanitized_env())
    props = parse_show_output(raw)
    judge_properties(props, main_pid)
    return UnitAdmission(unit=unit, properties=props)


__all__ = [
    "ADMISSION_REASONS", "AdmissionError", "REASON_DELEGATION_MISSING",
    "REASON_STOP_POLICY_UNSUPPORTED", "REASON_TOPOLOGY_UNSUPPORTED", "SHOW_PROPERTIES",
    "SHOW_STDOUT_LIMIT", "SHOW_TIMEOUT_SEC", "SYSTEMCTL_CANDIDATES", "UnitAdmission",
    "admit_unit", "check_systemctl_executable", "identify_user_unit", "is_service_unit_name",
    "judge_properties", "locate_systemctl", "parse_show_output", "parse_timespan_usec",
    "run_show", "sanitized_env", "show_argv", "unit_name_from_result",
]
