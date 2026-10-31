"""server 認証: 鍵を送る前に接続先が本物の afx daemon か確かめる。

二層ある。

- 共有 verifier (``check_socket_path`` → 接続 → ``verify_peer``): 本体 client と同じ
  検査順・同じ reason 語彙の契約を独立に実装したもの。``connect_verified`` がこの
  連鎖の入口で、試験用 probe はこれだけを呼ぶ。
- afx-ops 固有 admission (``fetch_pidfd`` + ``check_pidfd`` + ``check_lock_device``): 共有 verifier の接続後の検査の前段に置く、より
  厳しい検査。``SO_PEERPIDFD`` を必須にし、instance lock の装置番号の食い違いを
  readlink で救済しない。要求経路 (api_client) が接続ごとに使う。

どの検査も確定できなければ ``ServerAuthError`` を投げ、呼び出し側は鍵を一切送らない。
``/proc/self`` は使わない (隔離後は自分の pid の ``/proc/<pid>`` だけが読めるため)。
"""
from __future__ import annotations

import errno
import os
import select
import socket
import stat
import struct
from pathlib import Path

# 共有契約の語彙 (本体 client の ServerAuthError.reason と同じ)。
SHARED_REASONS = ("socket_dir", "socket_file", "peer_uid", "peer_gone", "peer_check",
                  "not_lock_holder", "holder_gone")
# afx-ops 固有 admission だけが使う語。
ADMISSION_REASONS = ("ops_requires_so_peerpidfd", "instance_lock_dev_mismatch",
                     "pidfd_transient")
REASONS = SHARED_REASONS + ADMISSION_REASONS

# stdlib に無い kernel 定数 (Linux 6.5 以降の getsockopt optname)。
SO_PEERPIDFD = 77
PROC_LOCKS = Path("/proc/locks")
_UCRED = struct.Struct("3i")
_TIMEVAL = struct.Struct("ll")
# pidfd が使えないと分かる errno。それ以外 (ESRCH 以外) も共有側では省略扱いにする。
_PIDFD_UNSUPPORTED = (errno.ENOPROTOOPT, errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP)
# pidfd の fd を作れなかっただけの errno (fd・memory の一時的な枯渇)。kernel 非対応ではない。
_PIDFD_TRANSIENT = (errno.EMFILE, errno.ENFILE, errno.ENOBUFS, errno.ENOMEM)
# 接続の相手が既に居ないと分かる errno (kernel により ESRCH か ENODATA)。
_PIDFD_PEER_GONE = (errno.ESRCH, errno.ENODATA)


class ServerAuthError(Exception):
    """接続先を確かめられない。``reason`` は固定語で、この時点で鍵は送っていない。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        # 失敗した段と errno だけの固定形 (``step=<段> errno=<n>``)。stderr の診断専用
        self.detail = detail


# detail に出てよい段の全部。生成側 (ここ) と表示側 (api_client.with_detail) の両方で閉じる
DETAIL_STEPS = frozenset({"getsockopt", "fdinfo_open", "fdinfo_read", "locks_open", "locks_read",
                          "pin_locks", "pin_proc", "unknown"})


def step_detail(step: str, err: int | None) -> str:
    if step not in DETAIL_STEPS:
        step = "unknown"
    return f"step={step} errno={err if type(err) is int else 0}"


class _StepError(OSError):
    """どの段で失敗したかを持つ OSError (``detail`` の元)。"""

    def __init__(self, step: str, err: OSError) -> None:
        super().__init__(err.errno, "")
        self.step = step

    @property
    def detail(self) -> str:
        return step_detail(self.step, self.errno)


def _detail_of(exc: OSError) -> str:
    return exc.detail if isinstance(exc, _StepError) else step_detail("unknown", exc.errno)


class SocketMissing(Exception):
    """socket (又はその親 dir) がまだ無い。起動待ちが待ってよい唯一の状態。"""


# --------------------------------------------------------------- 共有 verifier

def check_socket_path(path: Path, *, uid: int) -> None:
    """接続前の静的検査: 親 dir は同 uid の 0700 の dir、socket は同 uid の socket。"""
    # 無いことが分かった場合だけ「socket 無し」。権限などで確かめられなければ鍵を送らない。
    try:
        dir_st = os.lstat(path.parent)
    except FileNotFoundError:
        raise SocketMissing() from None
    except OSError:
        raise ServerAuthError("socket_dir") from None
    if (not stat.S_ISDIR(dir_st.st_mode) or dir_st.st_uid != uid
            or stat.S_IMODE(dir_st.st_mode) != 0o700):
        raise ServerAuthError("socket_dir")
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise SocketMissing() from None
    except OSError:
        raise ServerAuthError("socket_file") from None
    if not stat.S_ISSOCK(st.st_mode) or st.st_uid != uid:
        raise ServerAuthError("socket_file")


def _flock_rows(proc_locks: Path) -> list[tuple[int, int, int, int]]:
    """``/proc/locks`` の FLOCK WRITE 保持行を (pid, major, minor, inode) で返す。

    行は ``N: FLOCK  ADVISORY  WRITE <pid> <major>:<minor>:<inode> 0 EOF`` で major /
    minor は 16 進。``->`` の付いた行は待っている側なので数えない。読めなければ段つきの
    ``_StepError`` (呼び出し側が握りつぶすか理由にするかを決める)。
    """
    try:
        with open(proc_locks, "rb") as file:
            try:
                text = file.read().decode("ascii", errors="replace")
            except OSError as exc:
                raise _StepError("locks_read", exc) from None
    except _StepError:
        raise
    except OSError as exc:
        raise _StepError("locks_open", exc) from None
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 6 or "->" in parts or parts[1] != "FLOCK" or parts[3] != "WRITE":
            continue
        fields = parts[5].split(":")
        try:
            rows.append((int(parts[4]), int(fields[0], 16), int(fields[1], 16),
                         int(fields[2])))
        except (IndexError, ValueError):
            continue
    return rows


def lock_holders(lock_path: Path, *, proc_locks: Path = PROC_LOCKS,
                 dev_fallback: bool = True) -> set[int]:
    """lock file の FLOCK WRITE 保持者の pid を集める。

    inode の一致は必須。装置番号は btrfs・overlayfs・NFS では stat の値と
    ``/proc/locks`` の値が食い違うので、共有契約では食い違うときにその pid が lock file
    を fd で開いていることを確かめて数える。``dev_fallback=False`` ではこの救済をせず
    数えない (固有 admission を通した経路は dev の一致を前提にする)。
    """
    try:
        st = os.stat(lock_path)
        real_path = os.path.realpath(lock_path)
    except OSError:
        return set()
    device = (os.major(st.st_dev), os.minor(st.st_dev))
    try:
        rows = _flock_rows(proc_locks)
    except OSError:
        return set()
    holders = set()
    for pid, major, minor, inode in rows:
        if inode != st.st_ino:
            continue
        if (major, minor) == device or (dev_fallback and _has_open_fd(pid, real_path)):
            holders.add(pid)
    return holders


def _has_open_fd(pid: int, real_path: str) -> bool:
    # 同 uid の process の fd は読める。読めなければ数えない (保持者と確かめられない)。
    fd_dir = f"/proc/{pid}/fd"
    try:
        names = os.listdir(fd_dir)
    except OSError:
        return False
    for name in names:
        try:
            if os.readlink(f"{fd_dir}/{name}") == real_path:
                return True
        except OSError:
            continue
    return False


def pidfd_pid(pidfd: int) -> int | None:
    """自分の ``/proc/<自 pid>/fdinfo/<pidfd>`` の ``Pid:``。相手が死んでいれば -1。"""
    try:
        file = open(f"/proc/{os.getpid()}/fdinfo/{pidfd}", encoding="ascii")
    except OSError as exc:
        raise _StepError("fdinfo_open", exc) from None
    with file:
        try:
            for line in file:
                if line.startswith("Pid:"):
                    return int(line.split()[1])
        except OSError as exc:
            raise _StepError("fdinfo_read", exc) from None
    return None


def pidfd_exited(pidfd: int) -> bool:
    """pidfd が POLLIN (相手が終了済み。zombie を含む) なら True。"""
    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    return any(mask & (select.POLLIN | select.POLLHUP | select.POLLERR)
               for _fd, mask in poller.poll(0))


def peer_credentials(sock: socket.socket) -> tuple[int, int]:
    """``SO_PEERCRED`` の (pid, uid)。読めなければ ``peer_check``。"""
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size)
    except OSError as exc:
        raise ServerAuthError("peer_check", step_detail("getsockopt", exc.errno)) from None
    pid, uid, _gid = _UCRED.unpack(raw)
    return pid, uid


def verify_peer(sock: socket.socket, *, uid: int, lock_path: Path,
                proc_locks: Path = PROC_LOCKS, pidfd: int | None = None,
                dev_fallback: bool = True) -> int:
    """接続済み socket の相手が、同 uid で instance lock を持つ生きた process か確かめる。

    ``pidfd`` を渡されたら (所有者は呼び出し側) それを使い、閉じない。渡されなければ
    取れる場合だけ自分で取り、ここで閉じる (共有契約では pidfd は任意)。
    """
    pid, peer_uid = peer_credentials(sock)
    if peer_uid != uid or pid <= 0:
        raise ServerAuthError("peer_uid")
    owned = None
    if pidfd is None:
        try:
            owned = sock.getsockopt(socket.SOL_SOCKET, SO_PEERPIDFD)
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                raise ServerAuthError("peer_gone") from None
        pidfd = owned
    try:
        if pidfd is not None:
            try:
                matched = pidfd_pid(pidfd)
            except OSError as exc:
                raise ServerAuthError("peer_check", _detail_of(exc)) from None
            if matched != pid:
                raise ServerAuthError("peer_gone")
        # 保持者を一意に確定できなければ送らない。
        if lock_holders(lock_path, proc_locks=proc_locks, dev_fallback=dev_fallback) != {pid}:
            raise ServerAuthError("not_lock_holder")
        # 保持者 (= peer) の生存。pidfd があれば poll で見る (zombie も終了とみなせ、
        # 隔離後に /proc/<peer-pid> を読まずに済む)。無ければ /proc/<pid> の存在で見る。
        if pidfd is not None:
            if pidfd_exited(pidfd):
                raise ServerAuthError("holder_gone")
        elif not os.path.isdir(f"/proc/{pid}"):
            raise ServerAuthError("holder_gone")
    finally:
        if owned is not None:
            os.close(owned)
    return pid


def connect_unix(path: Path, *, timeout: float | None = None) -> socket.socket:
    """pathname UDS へ新しく接続する。socket が無ければ ``SocketMissing``。

    ``timeout`` を渡すと connect・送信・受信のどれも期限付きになり、期限切れは
    ``TimeoutError``。UDS の connect は相手の backlog が埋まっていると kernel 内で待つが、
    ``settimeout`` (非 blocking) の connect は待たずに EAGAIN を返すので、connect の間は
    blocking のまま ``SO_SNDTIMEO`` で kernel の待ちに期限を付け、接続後に ``settimeout``
    で送受信に期限を付ける。
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
    try:
        if timeout is not None:
            seconds, fraction = divmod(timeout, 1.0)
            # timeval の 0 は「無期限」になるので、正の期限は最低 1 µs にする
            micros = max(int(fraction * 1_000_000), 0 if seconds else 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO,
                            _TIMEVAL.pack(int(seconds), micros))
        try:
            sock.connect(str(path))
        except BlockingIOError:
            if timeout is None:
                raise
            # SO_SNDTIMEO の期限まで待っても backlog が空かなかった
            raise TimeoutError(errno.ETIMEDOUT, "connect") from None
        if timeout is not None:
            sock.settimeout(timeout)
    except FileNotFoundError:
        sock.close()
        raise SocketMissing() from None
    except BaseException:
        sock.close()
        raise
    return sock


def connect_verified(socket_path: Path, *, lock_path: Path, uid: int,
                     proc_locks: Path = PROC_LOCKS) -> socket.socket:
    """共有 verifier の連鎖 (固有 admission なし)。

    確かめられなければ ``ServerAuthError`` (接続済みの socket は閉じる)。socket が
    無ければ ``SocketMissing``、接続拒否は ``OSError``。戻り値の socket へは、この関数が
    返った後でだけ鍵を書いてよい。
    """
    check_socket_path(socket_path, uid=uid)
    sock = connect_unix(socket_path)
    try:
        verify_peer(sock, uid=uid, lock_path=lock_path, proc_locks=proc_locks)
    except BaseException:
        sock.close()
        raise
    return sock


# --------------------------------------------------------------- 固有 admission

def fetch_pidfd(sock: socket.socket) -> int:
    """``SO_PEERPIDFD`` を必ず取る。取れた fd の所有と close は呼び出し側 (要求 object)。"""
    try:
        return sock.getsockopt(socket.SOL_SOCKET, SO_PEERPIDFD)
    except OSError as exc:
        detail = step_detail("getsockopt", exc.errno)
        if exc.errno in _PIDFD_PEER_GONE:
            raise ServerAuthError("peer_gone", detail) from None
        if exc.errno in _PIDFD_TRANSIENT:
            raise ServerAuthError("pidfd_transient", detail) from None
        # 非対応 (EINVAL / ENOPROTOOPT 等) と未知の errno は pidfd を持てない扱いで閉じる
        raise ServerAuthError("ops_requires_so_peerpidfd", detail) from None


def check_pidfd(sock: socket.socket, pidfd: int) -> None:
    """自分の fdinfo で pidfd の pid が ``SO_PEERCRED`` の pid と一致することを確かめる。"""
    pid, _uid = peer_credentials(sock)
    try:
        matched = pidfd_pid(pidfd)
    except OSError as exc:
        # fd・memory の一時的な枯渇は恒久の拒否にしない (待てる語)
        reason = ("pidfd_transient" if exc.errno in _PIDFD_TRANSIENT
                  else "ops_requires_so_peerpidfd")
        raise ServerAuthError(reason, _detail_of(exc)) from None
    if matched is None:
        raise ServerAuthError("ops_requires_so_peerpidfd")
    if matched != pid:
        # -1 は相手が終了済み、別の pid は取り違え。どちらも相手を確定できない。
        raise ServerAuthError("peer_gone")


def check_lock_device(lock_path: Path, *, proc_locks: Path = PROC_LOCKS) -> None:
    """instance lock の行を ``/proc/locks`` で (装置番号, inode) の組で見つけられること。

    inode 番号は filesystem をまたぐと重複するので、dev の違う同 inode の行は別 file の
    lock とみなす。同 inode の行があるのに期待 dev の行が 1 つも無いときだけ、stat と
    ``/proc/locks`` の dev が食い違う filesystem (btrfs・overlayfs 等) と判断し、readlink で
    救済せず拒否する。同 inode の行が無い場合の判定は後段の共有 verifier に任せる。stat や
    ``/proc/locks`` を読めなければ保持者を確定できないので共有契約と同じ語で止める。
    """
    try:
        st = os.stat(lock_path)
    except OSError:
        raise ServerAuthError("not_lock_holder") from None
    try:
        rows = _flock_rows(proc_locks)
    except OSError as exc:
        raise ServerAuthError("not_lock_holder", _detail_of(exc)) from None
    device = (os.major(st.st_dev), os.minor(st.st_dev))
    devices = {(major, minor) for _pid, major, minor, inode in rows if inode == st.st_ino}
    if devices and device not in devices:
        raise ServerAuthError("instance_lock_dev_mismatch")
