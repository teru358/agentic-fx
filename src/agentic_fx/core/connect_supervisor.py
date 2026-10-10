"""改善 mission の connect supervisor: CLI 部分木の `connect` を検査して代行する。

seccomp `improve-cli/1` は `connect` を USER_NOTIF にする。worker の中でこの
module の receiver thread が listener を唯一の所有者として持ち、通知ごとに次を行う。

- tracee の socket の open file description を `pidfd_getfd` で複製し、
  `process_vm_readv` で 1 回だけ写した sockaddr を検査して、複製に raw `connect`
  する。`SECCOMP_USER_NOTIF_FLAG_CONTINUE` は使わず、各通知は一度だけ処理する。
- 複製の connect は handler の期限で打ち切る。tracee の socket が blocking なら、tracee が
  connect で停止している間だけ一時的に O_NONBLOCK を立て、poll で `CONNECT_WAIT_SEC` まで
  待ってから元の flag に戻す。期限切れは shutdown して EPERM (`connect_timeout`)。
  残余: flag は open file description に付くので、その間に tracee の別 thread が同じ socket
  を使うと O_NONBLOCK が見える (EAGAIN を受けうる)。同じ principal の中の窓で、境界は越えない。
- 許可宛先は `127.0.0.1:<proxy port>` と、opencode だけの `127.0.0.1:8080`。
  proxy 宛は proxy の同期 handoff (pending の登録と ack の待ち) を通し、差し込み
  口が無い構成では接続しない (fail closed)。

状態 (`RUNNING / STOPPING / CLOSED / STUCK`)、`in_flight`、committed の記録は一つの
lock で守る。RUNNING → STOPPING が stop の線形化点で、listener を close できるのは
receiver thread だけ (`_close_listener`)。drain の期限内に handler が終わらなければ
STUCK とし、listener は開いたまま worker process の終了で消える。

注入点 (`Faults`) は constructor 引数だけで渡せる。環境変数や settings からは
有効にできない。
"""
from __future__ import annotations

import collections
import ctypes
import errno as _errno
import os
import select
import socket
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from agentic_fx.core import cli_launch_protocol as P

# --- profile 定数 (settings から変えられない) -----------------------------------
CONNECT_SLOTS = 16
HANDLER_DEADLINE_SEC = 2.0
WATCHDOG_INTERVAL_SEC = 0.1
PROXY_ACK_DEADLINE_SEC = 1.0
# watchdog の期限の手前に残す、応答を返すための余裕
RESPONSE_MARGIN_SEC = 0.25
# 複製への connect を待つ上限 (handler の起動から)。この後に proxy の ack を待っても
# watchdog の期限に届かないように、ack の分と余裕を引いておく
CONNECT_WAIT_SEC = HANDLER_DEADLINE_SEC - PROXY_ACK_DEADLINE_SEC - RESPONSE_MARGIN_SEC
# stop の期限で応答待ちが 0 件のとき、receiver の close を待つ猶予
_STOP_GRACE_SEC = 0.1
# handler thread を起こせる生涯の回数。同時に動く数は slot (CONNECT_SLOTS) で抑え、
# 生涯の数は thread 生成の暴走に対する防御で、接続数の割り当てではない。数えるのは
# slot を取って実際に起動した handler だけ。超えたら supervisor の失敗として新規通知を拒否する
HANDLER_SPAWN_MAX = 1 << 16
LOOPBACK = "127.0.0.1"
OPENCODE_UPSTREAM_PORT = 8080
SOCKADDR_MIN = 16
SOCKADDR_MAX = 128

# --- state ---------------------------------------------------------------------
RUNNING = "RUNNING"
STOPPING = "STOPPING"
CLOSED = "CLOSED"
STUCK = "STUCK"

# --- 固定 reason ---------------------------------------------------------------
SECCOMP_USER_NOTIF_UNAVAILABLE = P.SECCOMP_USER_NOTIF_UNAVAILABLE
PIDFD_THREAD_UNAVAILABLE = "pidfd_thread_unavailable"
PTRACE_SCOPE_UNSUPPORTED = "ptrace_scope_unsupported"
SUPERVISOR_DIED = "supervisor_died"
SUPERVISOR_STALLED = "supervisor_stalled"
# 通知 1 件の拒否理由 (supervisor の障害ではない)
SUPERVISOR_RESOURCE = "supervisor_resource"
CONNECT_TIMEOUT = "connect_timeout"
SANDBOX_UNAVAILABLE = "sandbox_unavailable"

# --- x86_64 の syscall 番号と kernel 定数 ----------------------------------------
_SYS_SECCOMP = 317
_SYS_PROCESS_VM_READV = 310
_SYS_PIDFD_OPEN = 434
_SYS_PIDFD_GETFD = 438
_SECCOMP_GET_NOTIF_SIZES = 3
_NR_CONNECT = 42
_AUDIT_ARCH_X86_64 = 0xC000003E
PIDFD_THREAD = os.O_EXCL
_SO_DOMAIN = 39
_SO_PROTOCOL = 38
YAMA_PTRACE_SCOPE = "/proc/sys/kernel/yama/ptrace_scope"

# struct seccomp_notif / seccomp_notif_resp の、この module が読む・書く範囲
_NOTIF = struct.Struct("<QIIiIQ6Q")      # id, pid, flags, nr, arch, ip, args[6]
_RESP = struct.Struct("<QqiI")           # id, val, error, flags
assert _NOTIF.size == 80 and _RESP.size == 24


def _ioc(direction: int, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord("!") << 8) | nr


_IOC_WRITE, _IOC_READ = 1, 2


def notif_ioctls(notif_size: int, resp_size: int) -> tuple[int, int, int]:
    """RECV / SEND / ID_VALID の request 番号を、段 0 で得た大きさから作る。"""
    rw = _IOC_READ | _IOC_WRITE
    return _ioc(rw, 0, notif_size), _ioc(rw, 1, resp_size), _ioc(_IOC_WRITE, 2, 8)


_LIBC = None


def _libc():
    global _LIBC
    if _LIBC is None:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        libc.ioctl.restype = ctypes.c_int
        libc.ioctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]
        libc.connect.restype = ctypes.c_int
        libc.connect.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        libc.getsockname.restype = ctypes.c_int
        libc.getsockname.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
        libc.shutdown.restype = ctypes.c_int
        libc.shutdown.argtypes = [ctypes.c_int, ctypes.c_int]
        libc.getsockopt.restype = ctypes.c_int
        libc.getsockopt.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_void_p, ctypes.c_void_p]
        _LIBC = libc
    return _LIBC


class _IoVec(ctypes.Structure):
    _fields_ = [("base", ctypes.c_void_p), ("len", ctypes.c_size_t)]


# --- probe ---------------------------------------------------------------------

class SupervisorUnavailable(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def probe_notif_sizes() -> tuple[int, int, int]:
    """`SECCOMP_GET_NOTIF_SIZES` の (notif, resp, data)。不可なら SupervisorUnavailable。

    この module が読む範囲より小さい大きさも不可とする。"""
    buf = (ctypes.c_uint16 * 3)()
    ctypes.set_errno(0)
    rc = _libc().syscall(ctypes.c_long(_SYS_SECCOMP), ctypes.c_uint(_SECCOMP_GET_NOTIF_SIZES),
                         ctypes.c_uint(0), buf)
    if rc != 0:
        raise SupervisorUnavailable(SECCOMP_USER_NOTIF_UNAVAILABLE,
                                    f"GET_NOTIF_SIZES errno={ctypes.get_errno()}")
    sizes = (int(buf[0]), int(buf[1]), int(buf[2]))
    if sizes[0] < _NOTIF.size or sizes[1] < _RESP.size:
        raise SupervisorUnavailable(SECCOMP_USER_NOTIF_UNAVAILABLE, f"sizes={sizes}")
    return sizes


def _pidfd_open(tid: int, flags: int) -> int:
    """成功なら fd、失敗なら -errno。"""
    ctypes.set_errno(0)
    rc = _libc().syscall(ctypes.c_long(_SYS_PIDFD_OPEN), ctypes.c_int(tid), ctypes.c_uint(flags))
    return int(rc) if rc >= 0 else -ctypes.get_errno()


def probe_pidfd_thread(pidfd_open: Callable[[int, int], int] | None = None) -> bool:
    """main 以外の thread の tid で `pidfd_open(tid, PIDFD_THREAD)` ができるか。"""
    opener = pidfd_open or _pidfd_open
    hold = threading.Event()
    box: dict[str, int] = {}

    def body() -> None:
        box["tid"] = threading.get_native_id()
        hold.wait(5)

    t = threading.Thread(target=body, name="afx-pidfd-probe", daemon=True)
    t.start()
    try:
        deadline = time.monotonic() + 5
        while "tid" not in box and time.monotonic() < deadline:
            time.sleep(0.001)
        if "tid" not in box:
            return False
        fd = opener(box["tid"], PIDFD_THREAD)
    finally:
        hold.set()
        t.join(5)
    if fd < 0:
        return False
    os.close(fd)
    return True


def probe_ptrace_scope(path: str = YAMA_PTRACE_SCOPE) -> bool:
    """Yama の ptrace_scope が 0 か 1 なら真。Yama が無ければ真。読めない値は偽。"""
    try:
        with open(path, encoding="ascii") as f:
            text = f.read().strip()
    except FileNotFoundError:
        return True
    except (OSError, UnicodeDecodeError):
        return False
    return text in ("0", "1")


def probe_supervisor_capabilities(
    *, yama_path: str = YAMA_PTRACE_SCOPE,
    pidfd_open: Callable[[int, int], int] | None = None,
    notif_sizes: Callable[[], tuple[int, int, int]] | None = None,
) -> tuple[str, ...]:
    """supervisor に要る kernel 能力の不足を固定 reason の組で返す (空 = 全部ある)。"""
    out = []
    try:
        (notif_sizes or probe_notif_sizes)()
    except SupervisorUnavailable:
        out.append(SECCOMP_USER_NOTIF_UNAVAILABLE)
    if not probe_pidfd_thread(pidfd_open):
        out.append(PIDFD_THREAD_UNAVAILABLE)
    if not probe_ptrace_scope(yama_path):
        out.append(PTRACE_SCOPE_UNSUPPORTED)
    return tuple(out)


# --- 構成 ----------------------------------------------------------------------

@dataclass(frozen=True)
class Destinations:
    """許可宛先 (AF_INET の 127.0.0.1 だけ)。`direct_port` は opencode の LLM 上流。"""

    proxy_port: int | None
    direct_port: int | None = None


def destinations_for(backend: str, proxy_port: int) -> Destinations:
    """backend 定数から許可宛先を作る。8080 は opencode だけ。"""
    return Destinations(proxy_port, OPENCODE_UPSTREAM_PORT if backend == "opencode" else None)


class ProxyHandoff(Protocol):
    """proxy 宛 connect の同期 handoff の差し込み口。

    - `add_pending(dup_fd)` は commit の lock の中で呼ばれ、pending entry を返す。
    - `wait_ack(entry, client, deadline)` は connect が 0 / EINPROGRESS のとき、
      複製の `getsockname` を添えて呼ばれ、proxy が同じ client tuple を照合して ack
      を立てたら真。deadline は monotonic の絶対時刻。
    - `remove_pending(entry)` は lock の中で、応答の後に呼ばれる。"""

    def add_pending(self, dup_fd: int) -> object: ...

    def wait_ack(self, entry: object, client: tuple[str, int], deadline: float) -> bool: ...

    def remove_pending(self, entry: object) -> None: ...


@dataclass(frozen=True)
class Faults:
    """試験用の注入点。既定は全部 None で何もしない。

    - `at(point, notif_id)`: 各観測点で呼ぶ (止める・遅らせる・例外を投げる)。
    - `recv_errno()`: 0 以外を返すと RECV を呼ばずにその errno で失敗したことにする。
    - `send_errno(notif_id)`: 同じく SEND。
    - `pidfd_open_errno(tid)`: 同じく `pidfd_open`。
    - `vm_read_len(addrlen)`: `process_vm_readv` の要求長を差し替える (short read)。
    - `notif_sizes()`: 段 0 を差し替える。"""

    at: Callable[[str, int], None] | None = None
    recv_errno: Callable[[], int] | None = None
    send_errno: Callable[[int], int] | None = None
    pidfd_open_errno: Callable[[int], int] | None = None
    vm_read_len: Callable[[int], int] | None = None
    notif_sizes: Callable[[], tuple[int, int, int]] | None = None


# 観測点の名前
POINTS = ("receiver_loop", "after_poll", "after_recv", "after_register", "handler",
          "id_valid_1", "pidfd_open", "id_valid_2", "id_valid_3", "pidfd_getfd",
          "id_valid_4", "before_commit", "after_commit", "before_send")


@dataclass(frozen=True)
class SupervisorFailure:
    reason: str
    detail: str
    classification: str = SANDBOX_UNAVAILABLE


@dataclass
class _Notif:
    id: int
    pid: int
    nr: int
    arch: int
    args: tuple[int, ...]


class _Fault(Exception):
    """supervisor の障害 (想定外の errno など)。"""


class _Silent(Exception):
    """通知が無効になった (tracee が去った)。応答しないで終わる。"""


class ConnectSupervisor:
    """receiver・handler・watchdog を持つ connect の代行者 (1 mission に 1 個)。"""

    def __init__(self, destinations: Destinations, *,
                 proxy_handoff: ProxyHandoff | None = None,
                 on_fault: Callable[[SupervisorFailure], None] | None = None,
                 faults: Faults | None = None,
                 records_max: int = 4096) -> None:
        self._faults = faults or Faults()
        # 段 0: 大きさは起動時に 1 回だけ取る
        sizes = (self._faults.notif_sizes or probe_notif_sizes)()
        if sizes[0] < _NOTIF.size or sizes[1] < _RESP.size:
            raise SupervisorUnavailable(SECCOMP_USER_NOTIF_UNAVAILABLE, f"sizes={sizes}")
        self._notif_size, self._resp_size = sizes[0], sizes[1]
        self._ioc_recv, self._ioc_send, self._ioc_id_valid = notif_ioctls(*sizes[:2])
        self._dest = destinations
        self._proxy = proxy_handoff
        self._on_fault = on_fault

        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._state = RUNNING
        self._in_flight = 0
        self._owned: dict[int, _Notif] = {}
        self._committed: set[int] = set()
        self._started: dict[int, float] = {}
        self._drain_deadline: float | None = None
        self._failure: SupervisorFailure | None = None
        self._slots = threading.BoundedSemaphore(CONNECT_SLOTS)
        self._ack = threading.Event()
        self._efd = os.eventfd(0, os.EFD_CLOEXEC | os.EFD_NONBLOCK)
        self._listener = -1
        self._handed_off = False
        self._receiver: threading.Thread | None = None
        self._watchdog: threading.Thread | None = None
        self._handlers: set[threading.Thread] = set()
        self._spawned = 0
        self.records: collections.deque[dict] = collections.deque(maxlen=records_max)
        self.closed_at: float | None = None
        self.stop_requested_at: float | None = None

    # --- 外向きの操作 ----------------------------------------------------------

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def failure(self) -> SupervisorFailure | None:
        with self._lock:
            return self._failure

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    def handoff(self, listener: int) -> None:
        """listener の所有権を receiver に移す (`launch_cli` の handoff に渡す)。

        正常に戻った時点で watchdog と receiver の thread は動いていて、呼び出し側は
        fd を捨てる。例外のときはこの object は fd を保持していない (呼び出し側が閉じる)。
        watchdog を receiver より先に起動するので、監視を起動できなかったときは listener
        を受け取る前に `supervisor_died` を記録し、CLOSED にして例外を送出する。"""
        with self._lock:
            if self._handed_off or self._state != RUNNING:
                raise RuntimeError("handoff is accepted once, while RUNNING")
            self._handed_off = True
        try:
            # RECV が通知の取り消しで待ちに入らないように (kernel が対応していれば EAGAIN)
            os.set_blocking(listener, False)
        except BaseException:
            self._refuse_handoff()
            raise
        receiver = threading.Thread(target=self._receiver_main, name="afx-connect-receiver",
                                    daemon=True)
        watchdog = threading.Thread(target=self._watchdog_main, name="afx-connect-watchdog",
                                    daemon=True)
        try:
            watchdog.start()
        except BaseException as e:
            # 監視が無い supervisor は動かさない
            self._fault(SUPERVISOR_DIED, f"watchdog start: {e!r}")
            self._refuse_handoff()
            raise
        self._watchdog = watchdog
        self._listener = listener
        self._receiver = receiver
        try:
            receiver.start()
        except BaseException:
            self._listener = -1
            self._receiver = None
            # watchdog は ack で終わる
            self._refuse_handoff()
            raise
        # 以後 listener を閉じられるのは receiver thread だけ

    def _refuse_handoff(self) -> None:
        """listener を受け取らずに終端する (receiver が居ないので close はしない)。"""
        with self._lock:
            self._state = CLOSED
            self.closed_at = time.monotonic()
        self._ack.set()

    def stop(self, deadline: float) -> str:
        """RUNNING → STOPPING を線形化し、deadline (monotonic の絶対時刻) まで
        receiver の CLOSED / STUCK を待って、その state を返す。

        deadline までに receiver が確定しなければ STUCK にする (RECV と commit は
        禁止のまま、listener は閉じない)。stop が STUCK を確定した場合も terminal ack
        (`wait_terminal`) を立てる。呼び出し側はこの返り値か `wait_terminal` で、
        CLOSED / STUCK のどちらでも次の段 (process 群の kill) へ進める。
        どの経路でも deadline + `_STOP_GRACE_SEC` を越えて戻らない。"""
        with self._cv:
            if self._state == RUNNING:
                self._state = STOPPING
                self.stop_requested_at = time.monotonic()
            if self._drain_deadline is None or deadline < self._drain_deadline:
                self._drain_deadline = deadline
            self._cv.notify_all()
            self._kick_locked()
            if not self._handed_off:
                if self._state == STOPPING:
                    self._state = CLOSED
                    self.closed_at = time.monotonic()
                self._ack.set()
        self._ack.wait(max(0.0, deadline - time.monotonic()))
        with self._cv:
            if not self._ack.is_set() and self._in_flight:
                self._mark_stuck_locked()
            if self._state != STOPPING:
                return self._state
        # 応答待ちが無ければ receiver は close の直前にいる。短く待って実際の state を返す
        self._ack.wait(_STOP_GRACE_SEC)
        with self._cv:
            self._mark_stuck_locked()
            return self._state

    def wait_terminal(self, timeout: float) -> bool:
        """CLOSED / STUCK の ack を待つ。"""
        return self._ack.wait(timeout)

    def join(self, timeout: float) -> bool:
        """receiver・watchdog・handler の thread を待つ (STUCK では handler が残りうる)。"""
        end = time.monotonic() + timeout
        threads = [t for t in (self._receiver, self._watchdog) if t is not None]
        with self._lock:
            threads += list(self._handlers)
        for t in threads:
            t.join(max(0.0, end - time.monotonic()))
        return not any(t.is_alive() for t in threads)

    def release(self) -> None:
        """CLOSED で thread が全部終わった後に stop_efd を閉じる (STUCK では閉じない)。"""
        with self._lock:
            if self._state != CLOSED or self._efd < 0:
                return
            alive = [t for t in (self._receiver, self._watchdog) if t is not None and t.is_alive()]
            if alive or self._handlers:
                return
            fd, self._efd = self._efd, -1
        os.close(fd)

    # --- 障害と stop の内部 ----------------------------------------------------

    def _mark_stuck_locked(self) -> None:
        """stop の期限切れの確定。STOPPING のときだけ STUCK にして terminal ack を立てる。"""
        if self._state == STOPPING:
            self._state = STUCK
            self._ack.set()

    def _kick_locked(self) -> None:
        if self._efd >= 0:
            try:
                os.eventfd_write(self._efd, 1)
            except BlockingIOError:
                pass

    def _fault(self, reason: str, detail: str) -> None:
        """最初の障害だけを記録し、新しい RECV・commit・handler 起動を止める。"""
        with self._cv:
            first = self._failure is None
            if first:
                self._failure = SupervisorFailure(reason, detail)
            if self._state == RUNNING:
                self._state = STOPPING
                self.stop_requested_at = time.monotonic()
            self._cv.notify_all()
            self._kick_locked()
            failure = self._failure
        if first and self._on_fault is not None:
            try:
                self._on_fault(failure)
            except Exception:  # noqa: BLE001  通知の失敗で障害の記録は変えない
                pass

    def _at(self, point: str, nid: int) -> None:
        if self._faults.at is not None:
            self._faults.at(point, nid)

    # --- kernel 操作 -----------------------------------------------------------

    def _recv(self) -> _Notif | None:
        """段 1。ENOENT と EAGAIN は None (poll へ戻る)、EINTR は再試行、他は障害。"""
        libc = _libc()
        buf = ctypes.create_string_buffer(self._notif_size)
        while True:
            fake = self._faults.recv_errno() if self._faults.recv_errno else 0
            if fake:
                err = fake
            else:
                ctypes.memset(buf, 0, self._notif_size)
                ctypes.set_errno(0)
                rc = libc.ioctl(self._listener, self._ioc_recv, buf)
                err = 0 if rc == 0 else ctypes.get_errno()
            if err == 0:
                nid, pid, _flags, nr, arch, _ip, *args = _NOTIF.unpack_from(buf.raw, 0)
                return _Notif(nid, pid, nr, arch, tuple(args))
            if err == _errno.EINTR:
                continue
            if err == _errno.ENOENT:
                self.records.append({"stage": 1, "result": "recv_enoent"})
                return None
            if err == _errno.EAGAIN:
                # POLLIN の後に通知が取り消された。次の poll で確かめ直す
                self.records.append({"stage": 1, "result": "recv_eagain"})
                return None
            raise _Fault(f"NOTIF_RECV errno={err}")

    def _send(self, nid: int, rc: int) -> str:
        """段 12。rc は 0 か -errno。ENOENT は正常な race。他は障害。"""
        fake = self._faults.send_errno(nid) if self._faults.send_errno else 0
        if fake:
            err = fake
        else:
            buf = ctypes.create_string_buffer(self._resp_size)
            # error は負の errno、val は使わない、flags は常に 0 (CONTINUE を立てない)
            _RESP.pack_into(buf, 0, nid, 0, rc if rc < 0 else 0, 0)
            ctypes.set_errno(0)
            r = _libc().ioctl(self._listener, self._ioc_send, buf)
            err = 0 if r == 0 else ctypes.get_errno()
        if err == 0:
            return "sent"
        if err == _errno.ENOENT:
            return "enoent"
        raise _Fault(f"NOTIF_SEND errno={err}")

    def _id_valid(self, nid: int) -> bool:
        v = ctypes.c_uint64(nid)
        ctypes.set_errno(0)
        rc = _libc().ioctl(self._listener, self._ioc_id_valid, ctypes.byref(v))
        if rc == 0:
            return True
        err = ctypes.get_errno()
        if err == _errno.ENOENT:
            return False
        raise _Fault(f"NOTIF_ID_VALID errno={err}")

    def _vm_read(self, tid: int, addr: int, alen: int) -> bytes | None:
        """段 6。完全長を 1 回で写せたときだけ bytes。"""
        want = self._faults.vm_read_len(alen) if self._faults.vm_read_len else alen
        buf = ctypes.create_string_buffer(max(alen, 1))
        liov = _IoVec(ctypes.cast(buf, ctypes.c_void_p), want)
        riov = _IoVec(addr, want)
        ctypes.set_errno(0)
        got = _libc().syscall(ctypes.c_long(_SYS_PROCESS_VM_READV), ctypes.c_int(tid),
                              ctypes.byref(liov), ctypes.c_ulong(1), ctypes.byref(riov),
                              ctypes.c_ulong(1), ctypes.c_ulong(0))
        if got != alen:
            return None
        return buf.raw[:alen]

    # --- receiver --------------------------------------------------------------

    def _receiver_main(self) -> None:
        try:
            self._receive_loop()
        except BaseException as e:  # noqa: BLE001  receiver の異常は最外周で確定する
            self._fault(SUPERVISOR_DIED, f"receiver: {e!r}")
        finally:
            self._receiver_finish()

    def _receive_loop(self) -> None:
        lfd = self._listener
        poller = select.poll()
        poller.register(lfd, select.POLLIN)
        poller.register(self._efd, select.POLLIN)
        while True:
            self._at("receiver_loop", 0)
            with self._lock:
                if self._state != RUNNING:
                    return
            events = dict(poller.poll())
            if events.get(self._efd, 0):
                return
            lev = events.get(lfd, 0)
            if lev & (select.POLLERR | select.POLLNVAL):
                raise _Fault(f"listener poll events={lev:#x}")
            if not lev & select.POLLIN:
                if lev & select.POLLHUP:
                    # filter の利用者が居なくなった。以後通知は来ない
                    self.records.append({"stage": 1, "result": "listener_hup"})
                    return
                continue
            self._at("after_poll", 0)
            with self._lock:  # 観測点: poll 復帰後、RECV 前
                if self._state != RUNNING:
                    return
            notif = self._recv()
            if notif is None:
                continue
            self._at("after_recv", notif.id)
            with self._lock:
                self._owned[notif.id] = notif
                self._in_flight += 1
            self._at("after_register", notif.id)
            with self._lock:  # 観測点: RECV 後、handler 起動前
                running = self._state == RUNNING
            if not running:
                self._respond_by_receiver(notif, -_errno.EPERM, "stopping")
                return
            if not self._slots.acquire(blocking=False):
                self._respond_by_receiver(notif, -_errno.EAGAIN, "slots")
                continue
            with self._lock:
                over = self._spawned >= HANDLER_SPAWN_MAX
                if not over:
                    self._spawned += 1
            if over:
                # thread の生涯生成数が上限に達した。以後の通知は受けない
                self._slots.release()
                self._respond_by_receiver(notif, -_errno.EPERM, "spawn_limit")
                self._fault(SUPERVISOR_STALLED, f"handler spawn over {HANDLER_SPAWN_MAX}")
                return
            # thread の構築も start と同じ回収の内側に置く (構築の失敗でも slot と通知を戻す)
            t: threading.Thread | None = None
            try:
                t = threading.Thread(target=self._handler_main, args=(notif,),
                                     name=f"afx-connect-handler-{notif.id:x}", daemon=True)
                with self._lock:
                    self._started[notif.id] = time.monotonic()
                    self._handlers.add(t)
                t.start()
            except BaseException:
                self._slots.release()
                with self._lock:
                    self._spawned -= 1
                    self._started.pop(notif.id, None)
                    if t is not None:
                        self._handlers.discard(t)
                self._respond_by_receiver(notif, -_errno.EPERM, "handler_start")
                raise

    def _respond_by_receiver(self, notif: _Notif, rc: int, why: str) -> None:
        rec = {"id": notif.id, "tid": notif.pid, "by": "receiver", "reason": why,
               "result": rc}
        self.records.append(rec)
        try:
            rec["send"] = self._send(notif.id, rc)
        finally:
            with self._cv:
                self._owned.pop(notif.id, None)
                self._in_flight -= 1
                self._cv.notify_all()

    def _receiver_finish(self) -> None:
        """STOPPING → drain → CLOSED (listener close) または STUCK。ack は必ず立てる。"""
        try:
            with self._cv:
                if self._state == RUNNING:
                    self._state = STOPPING
                    self.stop_requested_at = time.monotonic()
                fallback = time.monotonic() + HANDLER_DEADLINE_SEC
                while self._in_flight and self._state == STOPPING:
                    end = self._drain_deadline if self._drain_deadline is not None else fallback
                    left = end - time.monotonic()
                    if left <= 0:
                        break
                    self._cv.wait(left)
                if self._in_flight == 0 and self._state == STOPPING:
                    closing = True
                elif self._state == STOPPING:
                    self._state = STUCK
                    closing = False
                else:
                    closing = False
            # close は lock の外から呼ぶ。stop() が状態 lock の再取得で待たされないため。
            # close と CLOSED の不可分は _close_listener の短い lock 区間で保つ
            if closing:
                self._close_listener()
        finally:
            self._ack.set()

    def _close_listener(self) -> None:
        """listener を閉じる唯一の経路。receiver thread 以外からは呼べない。

        state がまだ STOPPING のときだけ、close と CLOSED を同じ lock 区間で行う。
        その前に stop() が STUCK を確定していたら listener は閉じない。
        close を lock 内で呼んでよいのは、seccomp listener の close が待つ相手を持たず
        block しないため (stop() の lock 再取得は遅れない)。"""
        if threading.current_thread() is not self._receiver:
            raise RuntimeError("listener is closed only by the receiver thread")
        with self._cv:
            if self._state != STOPPING or self._in_flight:
                return
            fd, self._listener = self._listener, -1
            if fd >= 0:
                os.close(fd)
            self._state = CLOSED
            self.closed_at = time.monotonic()
            self._cv.notify_all()

    # --- watchdog --------------------------------------------------------------

    def _watchdog_main(self) -> None:
        while not self._ack.wait(WATCHDOG_INTERVAL_SEC):
            now = time.monotonic()
            with self._lock:
                late = [nid for nid, t0 in self._started.items()
                        if now - t0 > HANDLER_DEADLINE_SEC]
            if late:
                self._fault(SUPERVISOR_STALLED, f"handler over {HANDLER_DEADLINE_SEC}s: "
                                                f"{len(late)}")

    # --- handler ---------------------------------------------------------------

    def _handler_main(self, notif: _Notif) -> None:
        rec: dict = {"id": notif.id, "tid": notif.pid, "by": "handler"}
        # 記録は応答より前に載せる (段と結果は SEND の前に書く)
        self.records.append(rec)
        try:
            self._at("handler", notif.id)
            self._handle(notif, rec)
        except _Silent:
            rec["result"] = "silent"
        except BaseException as e:  # noqa: BLE001  handler の例外は supervisor の障害
            rec["exception"] = repr(e)
            self._fault(SUPERVISOR_DIED, f"handler: {e!r}")
            if "send" not in rec:
                try:
                    if self._id_valid(notif.id):
                        rec["result"] = -_errno.EPERM
                        rec["send"] = self._send(notif.id, -_errno.EPERM)
                except BaseException:  # noqa: BLE001  応答できなければ close の ENOSYS に任せる
                    pass
        finally:
            self._slots.release()
            with self._cv:
                self._owned.pop(notif.id, None)
                self._committed.discard(notif.id)
                self._started.pop(notif.id, None)
                self._handlers.discard(threading.current_thread())
                self._in_flight -= 1
                self._cv.notify_all()

    def _reply(self, notif: _Notif, rec: dict, stage: int, rc: int) -> None:
        rec["stage"] = stage
        rec["result"] = rc
        rec["send"] = self._send(notif.id, rc)

    def _check_valid(self, notif: _Notif, rec: dict, stage: int, point: str) -> None:
        self._at(point, notif.id)
        if not self._id_valid(notif.id):
            rec["stage"] = stage
            raise _Silent()

    def _handle(self, notif: _Notif, rec: dict) -> None:
        with self._lock:
            began = self._started.get(notif.id, time.monotonic())
        pidfd = -1
        dup = -1
        entry: object | None = None
        proxy = self._proxy
        try:
            # 段 2
            if notif.arch != _AUDIT_ARCH_X86_64 or notif.nr != _NR_CONNECT:
                self._reply(notif, rec, 2, -_errno.EPERM)
                raise _Fault(f"filter mismatch arch={notif.arch:#x} nr={notif.nr}")
            alen = notif.args[2]
            if not SOCKADDR_MIN <= alen <= SOCKADDR_MAX:
                self._reply(notif, rec, 2, -_errno.EINVAL)
                return
            # 段 3
            self._check_valid(notif, rec, 3, "id_valid_1")
            # 段 4
            self._at("pidfd_open", notif.id)
            fake = self._faults.pidfd_open_errno(notif.pid) if self._faults.pidfd_open_errno else 0
            pidfd = -fake if fake else _pidfd_open(notif.pid, PIDFD_THREAD)
            if pidfd < 0:
                rec["pidfd_errno"] = -pidfd
                pidfd = -1
                if not self._id_valid(notif.id):
                    rec["stage"] = 4
                    raise _Silent()
                self._reply(notif, rec, 4, -_errno.EPERM)
                return
            # 段 5
            self._check_valid(notif, rec, 5, "id_valid_2")
            # 段 6
            copy = self._vm_read(notif.pid, notif.args[1], alen)
            if copy is None:
                if not self._id_valid(notif.id):
                    rec["stage"] = 6
                    raise _Silent()
                self._reply(notif, rec, 6, -_errno.EFAULT)
                return
            # 段 7
            self._check_valid(notif, rec, 7, "id_valid_3")
            # 段 8
            kind, err = self._classify(copy)
            if err:
                self._reply(notif, rec, 8, -err)
                return
            rec["dest"] = kind
            # 段 9
            self._at("pidfd_getfd", notif.id)
            dup, err = self._dup_socket(pidfd, notif.args[0])
            if err:
                self._reply(notif, rec, 9, -err)
                return
            # 段 10
            self._check_valid(notif, rec, 10, "id_valid_4")
            # 段 10b: commit
            self._at("before_commit", notif.id)
            if kind == "proxy" and proxy is None:
                # 段 8 と食い違っても、差し込み口が無ければ proxy に繋がない (fail closed)
                self._reply(notif, rec, 10, -_errno.EPERM)
                return
            with self._lock:
                committed = self._state == RUNNING
                if committed:
                    self._committed.add(notif.id)
                    if kind == "proxy" and proxy is not None:
                        entry = proxy.add_pending(dup)
            if not committed:
                self._reply(notif, rec, 10, -_errno.EPERM)
                return
            rec["committed"] = True
            self._at("after_commit", notif.id)
            # 段 11
            try:
                rc, timed_out = _bounded_connect(dup, copy, began + CONNECT_WAIT_SEC)
            except _BlockingRestoreError as e:
                # tracee の socket を元の blocking に戻せなかった。この通知は拒否し、以後の
                # 代行も止める (同じ失敗で tracee の socket の性質を変え続けないため)。
                # supervisor の障害としては仕様の runtime 分類にある supervisor_died で記録する
                rec["reason"] = SUPERVISOR_RESOURCE
                rec["detail"] = repr(e)
                try:
                    _shutdown(dup)
                except OSError:
                    pass
                self._fault(SUPERVISOR_DIED, f"blocking restore: {e!r}")
                self._reply(notif, rec, 11, -_errno.EPERM)
                return
            rec["connect"] = rc
            if timed_out:
                rec["reason"] = CONNECT_TIMEOUT
                # 進行中の connect を止める (SYN_SENT の shutdown は切断になる)
                try:
                    _shutdown(dup)
                except OSError:
                    pass
                self._reply(notif, rec, 11, -_errno.EPERM)
                return
            # 段 11b
            if entry is not None and proxy is not None and rc in (0, -_errno.EINPROGRESS):
                ok = False
                starved = False
                late = False
                try:
                    try:
                        client = _getsockname4(dup)
                    except OSError as e:
                        # fd 枯渇などの環境負荷はこの通知だけの失敗で、supervisor は続ける
                        starved = True
                        rec["reason"] = SUPERVISOR_RESOURCE
                        rec["detail"] = f"getsockname: {e!r}"
                    else:
                        self._at("before_ack", notif.id)
                        # ack の待ちも handler の期限の内側に収める (watchdog は待ちを中断できない)
                        now = time.monotonic()
                        ack_end = min(now + PROXY_ACK_DEADLINE_SEC,
                                      began + HANDLER_DEADLINE_SEC - RESPONSE_MARGIN_SEC)
                        if ack_end <= now:
                            late = True
                            rec["reason"] = CONNECT_TIMEOUT
                        else:
                            ok = proxy.wait_ack(entry, client, ack_end)
                finally:
                    if not ok:
                        # 複製は tracee の socket と description を共有するので、tracee 側も使えなくなる
                        try:
                            _shutdown(dup)
                        except OSError:
                            pass
                if starved or late:
                    rc = -_errno.EPERM
                elif not ok:
                    rec["ack"] = False
                    rc = -_errno.ECONNREFUSED
            # 段 12
            self._at("before_send", notif.id)
            self._reply(notif, rec, 12, rc)
        finally:
            # 後始末は互いに独立させる。前の段の例外で複製や pidfd が残ると、tracee 側の
            # close が実際の close にならない
            try:
                if entry is not None and proxy is not None:
                    with self._lock:
                        proxy.remove_pending(entry)
            finally:
                try:
                    if dup >= 0:
                        os.close(dup)
                finally:
                    if pidfd >= 0:
                        os.close(pidfd)

    def _classify(self, copy: bytes) -> tuple[str, int]:
        """段 8。許可なら (kind, 0)、拒否なら ("", errno)。"""
        family = struct.unpack_from("<H", copy, 0)[0]
        if family == socket.AF_INET6:
            # scope id 付きは形として不正、他の IPv6 (::1、IPv4-mapped を含む) は family で拒否
            if len(copy) >= 28 and struct.unpack_from("<I", copy, 24)[0] != 0:
                return "", _errno.EINVAL
            return "", _errno.EAFNOSUPPORT
        if family != socket.AF_INET:
            return "", _errno.EAFNOSUPPORT
        port = struct.unpack_from("!H", copy, 2)[0]
        host = socket.inet_ntoa(copy[4:8])
        if host != LOOPBACK:
            return "", _errno.ECONNREFUSED
        if self._dest.proxy_port is not None and port == self._dest.proxy_port:
            if self._proxy is None:
                # proxy の同期 handoff が無い構成では proxy に繋がない
                return "", _errno.EPERM
            return "proxy", 0
        if self._dest.direct_port is not None and port == self._dest.direct_port:
            return "direct", 0
        return "", _errno.ECONNREFUSED

    def _dup_socket(self, pidfd: int, raw_fd: int) -> tuple[int, int]:
        """段 9。複製が AF_INET の TCP stream socket なら (fd, 0)。"""
        target = raw_fd & 0xFFFFFFFF
        if target > 0x7FFFFFFF:
            return -1, _errno.EPERM
        ctypes.set_errno(0)
        dup = _libc().syscall(ctypes.c_long(_SYS_PIDFD_GETFD), ctypes.c_int(pidfd),
                              ctypes.c_int(target), ctypes.c_uint(0))
        if dup < 0:
            return -1, _errno.EPERM
        dup = int(dup)
        ok = False
        try:
            values = []
            for opt in (_SO_DOMAIN, socket.SO_TYPE, _SO_PROTOCOL):
                v = _getsockopt_int(dup, opt)
                if v < 0:
                    return -1, _errno.ENOTSOCK if -v == _errno.ENOTSOCK else _errno.EPERM
                values.append(v)
            if values != [socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP]:
                return -1, _errno.EPERM
            ok = True
            return dup, 0
        finally:
            if not ok:
                os.close(dup)


def _getsockopt_int(fd: int, opt: int) -> int:
    v = ctypes.c_int(0)
    ln = ctypes.c_uint32(ctypes.sizeof(v))
    ctypes.set_errno(0)
    if _libc().getsockopt(fd, socket.SOL_SOCKET, opt, ctypes.byref(v), ctypes.byref(ln)) != 0:
        return -ctypes.get_errno()
    return v.value


def _raw_connect(fd: int, sockaddr: bytes) -> int:
    """写し済みの sockaddr で connect する。0 か -errno。fd の flag は変えない。"""
    buf = ctypes.create_string_buffer(sockaddr, len(sockaddr))
    ctypes.set_errno(0)
    rc = _libc().connect(fd, buf, len(sockaddr))
    return 0 if rc == 0 else -ctypes.get_errno()


class _BlockingRestoreError(OSError):
    """一時的に立てた O_NONBLOCK を外せなかった (tracee の socket が nonblocking のまま)。"""


def _bounded_connect(fd: int, sockaddr: bytes, deadline: float) -> tuple[int, bool]:
    """複製に connect し、deadline (monotonic の絶対時刻) までに確定させる。(rc, 期限切れ)。

    元が nonblocking なら flag に触れずに 1 回だけ呼ぶ (EINPROGRESS はそのまま返す)。
    blocking なら一時的に O_NONBLOCK にして poll で待ち、もう一度 connect して結果を
    取る。Linux の inet_stream_connect は SS_CONNECTING の socket への再 connect で
    確定した結果を返し、成功なら SS_CONNECTED に遷移させる (成功は 0、既に確定済みなら
    EISCONN、失敗は ECONNREFUSED 等、未確定は EALREADY)。SO_ERROR を読むだけでは socket が
    SS_CONNECTING のまま残り、tracee の次の connect が 0 を返してしまうので使わない。
    再 connect の 0 と EISCONN は元の blocking connect の成功として 0 を返す。flag は
    応答より前に必ず元へ戻す (tracee は起きた瞬間に flag を観測できる)。
    deadline を既に過ぎていれば connect を始めない (期限切れとして返す)。"""
    if time.monotonic() >= deadline:
        return -_errno.ETIMEDOUT, True
    if not os.get_blocking(fd):
        return _raw_connect(fd, sockaddr), False
    os.set_blocking(fd, False)
    try:
        rc = _raw_connect(fd, sockaddr)
        poller = select.poll()
        poller.register(fd, select.POLLOUT)
        while rc in (-_errno.EINPROGRESS, -_errno.EALREADY):
            left = deadline - time.monotonic()
            if left <= 0:
                return -_errno.ETIMEDOUT, True
            if poller.poll(max(1, int(left * 1000) + 1)):
                rc = _raw_connect(fd, sockaddr)
                if rc == -_errno.EISCONN:
                    rc = 0
        return rc, False
    finally:
        try:
            os.set_blocking(fd, True)
        except OSError as e:
            raise _BlockingRestoreError(e.errno, f"set_blocking: {e!r}") from e


class _SockaddrIn(ctypes.Structure):
    _fields_ = [("family", ctypes.c_uint16), ("port", ctypes.c_uint16),
                ("addr", ctypes.c_ubyte * 4), ("zero", ctypes.c_ubyte * 8)]


def _getsockname4(fd: int) -> tuple[str, int]:
    """複製 fd の自側アドレス。socket object を作らない (既定 timeout で O_NONBLOCK が立ち、
    初期化の失敗で渡された fd を閉じるのを避ける)。AF_INET 以外は EPERM。"""
    sa = _SockaddrIn()
    ln = ctypes.c_uint32(ctypes.sizeof(sa))
    ctypes.set_errno(0)
    if _libc().getsockname(fd, ctypes.byref(sa), ctypes.byref(ln)) != 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))
    if sa.family != socket.AF_INET or ln.value < ctypes.sizeof(sa):
        raise OSError(_errno.EPERM, os.strerror(_errno.EPERM))
    return socket.inet_ntoa(bytes(sa.addr)), socket.ntohs(sa.port)


def _shutdown(fd: int) -> None:
    ctypes.set_errno(0)
    if _libc().shutdown(fd, socket.SHUT_RDWR) != 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))
