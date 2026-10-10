"""mission worker 側の mission cgroup: 自己位置の検査、作成、上限、interface fd の保持、
launcher の join、`cgroup.kill`、`populated 0` の待ち。

topology (afx 本体が admission で作る)::

    <afx unit>/
      ├─ supervisor/   afx 本体と mission worker (ここに居なければ始めない)
      └─ m-<nonce>/    CLI 部分木だけ。この module が作る

worker は outer Landlock の前に `m-<nonce>/` を作り、`pids.max`・`memory.max`・
`memory.swap.max=0` を書き、`cgroup.procs` (書き込み)・`cgroup.kill` (書き込み)・
`cgroup.events` (読み取り) を `O_CLOEXEC` で開いて保持する。隔離の後は path による
cgroup 操作をしない (outer Landlock で EACCES になる)。保持 fd は CLOEXEC なので、
launcher の exec 後の部分木はこれらを持たない。

rmdir は afx 本体 (`cgroup_host.MissionCgroupHost.cleanup_mission`) の仕事で、ここでは
しない (作成に失敗したときの空の cgroup の片付けだけ例外)。
"""
from __future__ import annotations

import errno as _errno
import os
import select
import time
from collections.abc import Callable
from pathlib import Path

from agentic_fx.core import cli_launch_protocol as P
from agentic_fx.core import unit_admission as ua
from agentic_fx.core.cgroup_host import CGROUP_MOUNT, MISSION_PREFIX, SUPERVISOR, _MISSION_NAME

#: profile 定数 (settings から変えられない)。実 CLI の peak の実測で確定するまでの仮の値
PIDS_MAX = 4096
MEMORY_MAX = 2 * 1024 ** 3

REASON_CREATE_FAILED = "cgroup_create_failed"
REASON_TOPOLOGY_UNSUPPORTED = ua.REASON_TOPOLOGY_UNSUPPORTED
REASON_JOIN_FAILED = P.CGROUP_JOIN_FAILED
SANDBOX_UNAVAILABLE = "sandbox_unavailable"

#: populated の読み直しの間隔 (POLLPRI の通知を取りこぼしても進むように)
_POLL_SEC = 0.01
_EVENTS_READ_MAX = 4096
#: join 直後の cgroup.procs は launcher 数個だけだが、pids.max (4096) 個の pid が載っても足りる大きさ
_PROCS_READ_MAX = 65536


class MissionCgroupError(Exception):
    """固定 reason で mission を始めない (いずれも `sandbox_unavailable`)。"""

    classification = SANDBOX_UNAVAILABLE

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def cgroup_name(nonce_hex: str) -> str:
    """mission nonce (小文字 32 hex) から mission cgroup 名を導く。"""
    name = MISSION_PREFIX + nonce_hex
    if _MISSION_NAME.fullmatch(name) is None:
        raise ValueError(f"mission cgroup 名にできない nonce: {nonce_hex!r}")
    return name


def _supervisor_parent(proc_self_cgroup: str) -> str:
    """`/proc/self/cgroup` から unit root の相対 path を返す。`supervisor/` に居なければ失敗。"""
    lines = [line for line in proc_self_cgroup.split("\n") if line]
    if len(lines) != 1 or not lines[0].startswith("0::/"):
        raise MissionCgroupError(REASON_TOPOLOGY_UNSUPPORTED, "cgroup v2 の単一階層でない")
    rel = lines[0][len("0::"):]
    head, _, last = rel.rpartition("/")
    if last != SUPERVISOR or not head:
        raise MissionCgroupError(REASON_TOPOLOGY_UNSUPPORTED,
                                 f"自分の cgroup {rel!r} が <unit>/supervisor でない")
    return head


def _close_quietly(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


class MissionCgroup:
    """1 mission の cgroup と、その interface fd (worker が mission の終わりまで保持)。"""

    def __init__(self, name: str, rel: str, procs_fd: int, kill_fd: int,
                 events_fd: int, procs_read_fd: int) -> None:
        self.name = name
        #: cgroup v2 の相対 path (`/proc/<pid>/cgroup` の `0::` の後ろと同じ形)
        self.rel = rel
        self._procs_fd = procs_fd
        self._kill_fd = kill_fd
        self._events_fd = events_fd
        #: join の読み戻し用。/proc は outer Landlock 後に読めないので、保持した fd で見る
        self._procs_read_fd = procs_read_fd
        self._closed = False

    @classmethod
    def create(cls, nonce_hex: str, *, proc_self_cgroup: str | None = None,
               mount: str = CGROUP_MOUNT) -> "MissionCgroup":
        """`<unit root>/m-<nonce>/` を新設して上限を書き、interface fd を開く。

        自己位置の不一致は `cgroup_topology_unsupported`、mkdir (EEXIST を含む)・上限の
        書き込み・fd の open の失敗は `cgroup_create_failed`。作った後で失敗したときは
        空の cgroup を rmdir してから送出する (process はまだ入っていない)。"""
        name = cgroup_name(nonce_hex)
        if proc_self_cgroup is None:
            try:
                with open("/proc/self/cgroup") as f:
                    proc_self_cgroup = f.read()
            except OSError as e:
                raise MissionCgroupError(REASON_TOPOLOGY_UNSUPPORTED,
                                         f"/proc/self/cgroup を読めない ({e.strerror})") from None
        root_rel = _supervisor_parent(proc_self_cgroup)
        rel = f"{root_rel}/{name}"
        path = Path(mount + rel)
        try:
            os.mkdir(path, 0o755)
        except OSError as e:
            raise MissionCgroupError(REASON_CREATE_FAILED,
                                     f"{name} を作れない ({e.strerror})") from None
        fds: list[int] = []
        key = ""
        try:
            for key, value in (("pids.max", str(PIDS_MAX)), ("memory.max", str(MEMORY_MAX)),
                               ("memory.swap.max", "0")):
                fd = os.open(path / key, os.O_WRONLY | os.O_CLOEXEC)
                try:
                    os.write(fd, value.encode())
                finally:
                    os.close(fd)
            for key, flags in (("cgroup.procs", os.O_WRONLY), ("cgroup.kill", os.O_WRONLY),
                               ("cgroup.events", os.O_RDONLY), ("cgroup.procs", os.O_RDONLY)):
                fds.append(os.open(path / key, flags | os.O_CLOEXEC))
        except OSError as e:
            for fd in fds:
                _close_quietly(fd)
            try:
                os.rmdir(path)
            except OSError:
                pass
            raise MissionCgroupError(REASON_CREATE_FAILED,
                                     f"{name}/{key} を用意できない ({e.strerror})") from None
        return cls(name, rel, *fds)

    # --- launch_cli に渡す関数 ------------------------------------------------

    def join(self, pid: int) -> None:
        """pid を保持 fd の `cgroup.procs` に書き、読み取り用に保持した別の fd で pread して
        pid が載ったことを確かめる (`/proc` は読まない)。

        失敗は `cgroup_join_failed` (launch_cli がそれを handoff 前の失敗に写す)。"""
        try:
            os.write(self._procs_fd, str(pid).encode())
        except OSError as e:
            raise MissionCgroupError(REASON_JOIN_FAILED,
                                     f"pid {pid} を移せない ({e.strerror})") from None
        try:
            now = os.pread(self._procs_read_fd, _PROCS_READ_MAX, 0).decode()
        except OSError as e:
            raise MissionCgroupError(REASON_JOIN_FAILED,
                                     f"cgroup.procs を読み戻せない ({e.strerror})") from None
        if str(pid) not in now.split():
            raise MissionCgroupError(REASON_JOIN_FAILED,
                                     f"pid {pid} が cgroup.procs の読み戻しに無い")

    def kill(self) -> None:
        """保持 fd の `cgroup.kill` に書く (cgroup の中の全 process に SIGKILL)。"""
        os.write(self._kill_fd, b"1")

    def kill_quietly(self) -> bool:
        try:
            self.kill()
        except OSError:
            return False
        return True

    # --- populated ------------------------------------------------------------

    def populated(self) -> bool:
        data = os.pread(self._events_fd, _EVENTS_READ_MAX, 0).decode()
        for line in data.split("\n"):
            key, _, value = line.partition(" ")
            if key == "populated":
                return value.strip() != "0"
        raise OSError(_errno.EIO, "cgroup.events に populated が無い")

    def wait_unpopulated(self, deadline: float) -> bool:
        """`populated 0` を deadline (monotonic の絶対時刻) まで待つ。読めなければ False。"""
        poller = select.poll()
        poller.register(self._events_fd, select.POLLPRI)
        while True:
            try:
                if not self.populated():
                    return True
            except OSError:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            poller.poll(max(1, int(min(_POLL_SEC, remaining) * 1000)))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for fd in (self._procs_fd, self._kill_fd, self._events_fd, self._procs_read_fd):
            _close_quietly(fd)

    def fds(self) -> tuple[int, int, int, int]:
        return (self._procs_fd, self._kill_fd, self._events_fd, self._procs_read_fd)


def launcher_kill(cgroup: MissionCgroup) -> Callable[[], None]:
    """`launch_cli(kill=...)` に渡す停止。`cgroup.kill` を書く (launcher pid 宛の SIGKILL は
    launch_cli が続けて必ず送る。JOINED より前の launcher は cgroup に居ないので)。"""

    def _kill() -> None:
        cgroup.kill_quietly()

    return _kill


__all__ = [
    "MEMORY_MAX", "MissionCgroup", "MissionCgroupError", "PIDS_MAX", "REASON_CREATE_FAILED",
    "REASON_JOIN_FAILED", "REASON_TOPOLOGY_UNSUPPORTED", "cgroup_name", "launcher_kill",
]
