"""改善 mission の CLI launcher (worker の direct child、exec 前は自前 code)。

`python -I -m agentic_fx.core.cli_launcher '<config json>'` で worker から起こされ、
自分を隔離してから、渡された CLI binary の bytes を封印した memfd を `execveat` する。
段の並び:

- L1〜L3: status fd を CLOEXEC にする、PDEATHSIG と ppid の再照合、継承 seccomp filter が
  無いこと、capability が全部 0 で uid / gid / supplementary group が worker の期待値と
  一致すること
- L4: HELLO{pid} を送り、JOINED を厳密に検査する
- L4a: mission cgroup に入った後で、渡された fd の bytes を封印した memfd に写し (memfd の
  page は mission cgroup に課金される)、SHA-256 を afx が admission で見た digest と照合し、
  memfd を ELF として検査し直す。元の fd は閉じる
- L4b: rlimit の hard 固定 (RLIMIT_FSIZE が memfd への書込みに掛からないよう、写した後)
- L5: subreaper、no_new_privs、nested Landlock (TSYNC)
- L6〜L7: seccomp `improve-cli/1` を install して listener を得て、seccomp mode 2、
  no_new_privs 1、subreaper 1 を自己検査する
- L8: PREPARED と listener fd 1 個を送り、送信の成否によらず自分の copy を閉じる
- L8w: GO を厳密に検査する
- L9: fd 3 以上を CLOEXEC にしてから memfd を `execveat(AT_EMPTY_PATH)`

失敗の申告は SETUP_FAILED / EXEC_FAILED の frame だけで行い、stderr や exit code
には意味を持たせない。single thread の前提で書く (seccomp の TSYNC と nested
Landlock の TSYNC は thread が増えても全 thread に掛かるが、launcher 自身は thread
を作らない)。

import は標準ライブラリと、protocol・artifact の copy・nested Landlock・seccomp 表と生成器の
module だけに限る (launcher source hash の範囲を小さく保つため)。
"""
import ctypes
import errno as _errno
import json
import os
import resource
import signal
import socket
import sys

from agentic_fx.core import cli_artifact_copy as AC
from agentic_fx.core import cli_launch_protocol as P
from agentic_fx.core import landlock_nested
from agentic_fx.core import seccomp_cli_bpf
from agentic_fx.core import seccomp_cli_policy

# x86_64 の syscall 番号と定数
_SYS_SECCOMP = 317
_SYS_EXECVEAT = 322
_SYS_CLOSE_RANGE = 436
_SECCOMP_SET_MODE_FILTER = 1
_SECCOMP_GET_ACTION_AVAIL = 2
_SECCOMP_GET_NOTIF_SIZES = 3
_RET_USER_NOTIF = 0x7FC00000
_AT_EMPTY_PATH = 0x1000
_CLOSE_RANGE_CLOEXEC = 1 << 2
_PR_SET_PDEATHSIG = 1
_PR_GET_SECCOMP = 21
_PR_GET_NO_NEW_PRIVS = 39
_LINUX_CAPABILITY_VERSION_3 = 0x20080522
_PR_SET_CHILD_SUBREAPER = 36
_PR_GET_CHILD_SUBREAPER = 37
_PR_SET_NO_NEW_PRIVS = 38

_LIBC = None


def _libc():
    global _LIBC
    if _LIBC is None:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        libc.prctl.restype = ctypes.c_int
        libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                               ctypes.c_ulong, ctypes.c_ulong]
        # capget / capset は glibc の wrapper で呼ぶ (syscall 番号は arch ごとに違う)
        libc.capget.restype = ctypes.c_int
        libc.capset.restype = ctypes.c_int
        _LIBC = libc
    return _LIBC


class _SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8),
                ("jf", ctypes.c_uint8), ("k", ctypes.c_uint32)]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_uint16), ("filter", ctypes.POINTER(_SockFilter))]


# --- seccomp install と kernel 能力の probe ----------------------------------------

def install_filter(program, flags: int) -> int:
    """呼んだ process に filter を掛け、listener fd を返す (不可逆)。

    flags は呼び出し側が A2 の `FILTER_FLAGS` をそのまま渡す。失敗は OSError。
    no_new_privs は呼び出し側が先に立てておく。"""
    arr = (_SockFilter * len(program))(*[_SockFilter(*ins) for ins in program])
    fprog = _SockFprog(len(program), ctypes.cast(arr, ctypes.POINTER(_SockFilter)))
    ctypes.set_errno(0)
    rc = _libc().syscall(ctypes.c_long(_SYS_SECCOMP), ctypes.c_uint(_SECCOMP_SET_MODE_FILTER),
                         ctypes.c_uint(flags), ctypes.byref(fprog))
    if rc < 0:
        err = ctypes.get_errno()
        raise OSError(err, f"seccomp install flags={flags:#x}")
    return int(rc)


def raw_flags_probe(flags: int) -> tuple[int, int]:
    """`seccomp(SET_MODE_FILTER, flags, NULL)` の (rc, errno)。

    kernel は flag を検査してから filter を copy するので、受け付ける flag なら
    NULL の copy で EFAULT、受け付けない flag なら EINVAL が返る。filter は
    掛からない。"""
    ctypes.set_errno(0)
    rc = _libc().syscall(ctypes.c_long(_SYS_SECCOMP), ctypes.c_uint(_SECCOMP_SET_MODE_FILTER),
                         ctypes.c_uint(flags), None)
    return int(rc), ctypes.get_errno() if rc < 0 else 0


def raw_user_notif_probe() -> tuple[int, int]:
    """`SECCOMP_GET_ACTION_AVAIL(USER_NOTIF)` の (rc, errno)。"""
    val = ctypes.c_uint32(_RET_USER_NOTIF)
    ctypes.set_errno(0)
    rc = _libc().syscall(ctypes.c_long(_SYS_SECCOMP), ctypes.c_uint(_SECCOMP_GET_ACTION_AVAIL),
                         ctypes.c_uint(0), ctypes.byref(val))
    return int(rc), ctypes.get_errno() if rc < 0 else 0


def raw_notif_sizes_probe() -> tuple[int, int]:
    """`SECCOMP_GET_NOTIF_SIZES` の (rc, errno)。"""
    buf = (ctypes.c_uint16 * 3)()
    ctypes.set_errno(0)
    rc = _libc().syscall(ctypes.c_long(_SYS_SECCOMP), ctypes.c_uint(_SECCOMP_GET_NOTIF_SIZES),
                         ctypes.c_uint(0), buf)
    return int(rc), ctypes.get_errno() if rc < 0 else 0


def _flag_accepted(result: tuple[int, int]) -> bool:
    rc, err = result
    return rc < 0 and err == _errno.EFAULT


def classify_probes(raw: dict) -> tuple[str, ...]:
    """probe の生の応答から、使えない能力の固定 reason を返す (空 = 全部使える)。

    raw の key: `user_notif` / `notif_sizes` (rc, errno)、`new_listener` /
    `wait_killable` / `tsync_esrch` は `raw_flags_probe` の (rc, errno)。"""
    out = []
    if (raw["user_notif"][0] != 0 or raw["notif_sizes"][0] != 0
            or not _flag_accepted(raw["new_listener"])):
        out.append(P.SECCOMP_USER_NOTIF_UNAVAILABLE)
    if not _flag_accepted(raw["wait_killable"]):
        out.append(P.SECCOMP_WAIT_KILLABLE_UNAVAILABLE)
    if not _flag_accepted(raw["tsync_esrch"]):
        out.append(P.SECCOMP_TSYNC_ESRCH_UNAVAILABLE)
    return tuple(out)


def probe_seccomp_capabilities() -> tuple[str, ...]:
    """USER_NOTIF / NEW_LISTENER、WAIT_KILLABLE_RECV、TSYNC_ESRCH を install せずに
    判定する。返り値は使えない能力の固定 reason の組 (空 = 全部使える)。"""
    F = seccomp_cli_policy
    raw = {
        "user_notif": raw_user_notif_probe(),
        "notif_sizes": raw_notif_sizes_probe(),
        "new_listener": raw_flags_probe(F.SECCOMP_FILTER_FLAG_NEW_LISTENER),
        "wait_killable": raw_flags_probe(
            F.SECCOMP_FILTER_FLAG_NEW_LISTENER | F.SECCOMP_FILTER_FLAG_WAIT_KILLABLE_RECV),
        "tsync_esrch": raw_flags_probe(
            F.SECCOMP_FILTER_FLAG_TSYNC | F.SECCOMP_FILTER_FLAG_NEW_LISTENER
            | F.SECCOMP_FILTER_FLAG_TSYNC_ESRCH),
    }
    return classify_probes(raw)


# --- launcher 本体 ----------------------------------------------------------------

class _Abort(Exception):
    """frame を送らずに _exit する失敗 (status fd 自体が信用できない段)。"""


class _SetupFailed(Exception):
    def __init__(self, reason: str, detail: int = 0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


# 自己検査は /proc を開かずに syscall で行う。outer Landlock が /proc を許さない
# backend (codex) でも同じ判定ができるように。

def _prctl_get(option: int) -> int:
    ctypes.set_errno(0)
    rc = _libc().prctl(option, 0, 0, 0, 0)
    if rc < 0:
        raise OSError(ctypes.get_errno(), f"prctl {option}")
    return rc


def _seccomp_mode() -> int:
    return _prctl_get(_PR_GET_SECCOMP)


def _no_new_privs() -> int:
    return _prctl_get(_PR_GET_NO_NEW_PRIVS)


def _subreaper_flag() -> int:
    """`PR_GET_CHILD_SUBREAPER` の読み値。flag は exec をまたいで CLI に残る。
    seccomp の後でも読める (CLI 側で許可されているのは GET だけ)。"""
    flag = ctypes.c_int(-1)
    ctypes.set_errno(0)
    if _libc().prctl(_PR_GET_CHILD_SUBREAPER, ctypes.addressof(flag), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl PR_GET_CHILD_SUBREAPER")
    return flag.value


class _CapHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _CapData(ctypes.Structure):
    _fields_ = [("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32),
                ("inheritable", ctypes.c_uint32)]


def _capabilities() -> tuple[int, int, int]:
    """(effective, permitted, inheritable) の 64 bit 値。"""
    hdr = _CapHeader(_LINUX_CAPABILITY_VERSION_3, 0)
    data = (_CapData * 2)()
    ctypes.set_errno(0)
    if _libc().capget(ctypes.byref(hdr), data) != 0:
        raise OSError(ctypes.get_errno(), "capget")
    return tuple(getattr(data[0], k) | (getattr(data[1], k) << 32)
                 for k in ("effective", "permitted", "inheritable"))



def clear_inheritable_caps() -> None:
    """自 process の inheritable capability を空にする (effective / permitted は触らない)。

    user service の process は systemd から CapInh に CAP_WAKE_ALARM 等を受け継ぐことがあり、
    子の launcher は「effective / permitted / inheritable が全部 0」を要求するので、worker が
    launch_cli の前に呼ぶ。inheritable は非特権でも下げられる。effective / permitted が
    非ゼロの process は launcher の検査が従来どおり fail closed にする。
    空になったことは capget で確かめる (`/proc` を読まないので Landlock の後でも呼べる)。"""
    hdr = _CapHeader(_LINUX_CAPABILITY_VERSION_3, 0)
    data = (_CapData * 2)()
    ctypes.set_errno(0)
    if _libc().capget(ctypes.byref(hdr), data) != 0:
        raise OSError(ctypes.get_errno(), "capget")
    for d in data:
        d.inheritable = 0
    ctypes.set_errno(0)
    if _libc().capset(ctypes.byref(hdr), data) != 0:
        raise OSError(ctypes.get_errno(), "capset")
    if _capabilities()[2] != 0:
        raise OSError(_errno.EPERM, "inheritable capabilities remain after capset")


def _send(sock: socket.socket, data: bytes, fds=()) -> None:
    anc = []
    if fds:
        anc = [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                b"".join(int(fd).to_bytes(4, "little", signed=True) for fd in fds))]
    sock.sendmsg([data], anc)


def _expect(sock: socket.socket, nonce: bytes, ftype: int) -> None:
    """worker からの frame を 1 個受けて厳密に検査する。違えば `_Abort`。"""
    data, anc, flags, _ = sock.recvmsg(P.RECV_DATA_LEN, P.RECV_ANC_LEN,
                                       socket.MSG_CMSG_CLOEXEC)
    fds, other = P.take_fds(anc)
    if fds or other:
        P.close_fds(fds)
        raise _Abort("ancillary")
    if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
        raise _Abort("truncated")
    if not data:
        raise _Abort("eof")
    try:
        frame = P.decode(data, nonce)
    except P.MalformedFrame as e:
        raise _Abort(e.detail) from None
    if frame.type != ftype:
        raise _Abort("order")


def _check_credentials(uid: int, gid: int, groups) -> None:
    try:
        caps = _capabilities()
    except OSError:
        raise _SetupFailed("credentials_unexpected", P.CRED_DETAIL_CAPS) from None
    if any(caps):
        raise _SetupFailed("credentials_unexpected", P.CRED_DETAIL_CAPS)
    if set(os.getresuid()) != {uid}:
        raise _SetupFailed("credentials_unexpected", P.CRED_DETAIL_UID)
    if set(os.getresgid()) != {gid}:
        raise _SetupFailed("credentials_unexpected", P.CRED_DETAIL_GID)
    if sorted(set(os.getgroups())) != sorted(set(groups)):
        raise _SetupFailed("credentials_unexpected", P.CRED_DETAIL_GROUPS)


def _set_rlimits(backend: str) -> None:
    try:
        for name, (soft, hard) in P.rlimit_profile(backend).items():
            resource.setrlimit(getattr(resource, name), (soft, hard))
            if resource.getrlimit(getattr(resource, name)) != (soft, hard):
                raise _SetupFailed("rlimit_failed")
    except (OSError, ValueError, KeyError) as e:
        raise _SetupFailed("rlimit_failed", getattr(e, "errno", 0) or 0) from None


def _sealed_exec_artifact(src_fd: int, digest: str) -> int:
    """mission cgroup の中で src_fd の bytes を封印した memfd に写し、digest と ELF を検査して
    memfd を返す (src_fd はここで閉じる)。失敗は固定 reason の `_SetupFailed`。

    worker は RLIMIT_FSIZE の soft を小さく、hard を artifact の大きさ以上にして起こすので、
    memfd への書込みの前に soft を hard まで上げる (8 MiB への固定は写した後の `_set_rlimits`)。"""
    try:
        try:
            _soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
            resource.setrlimit(resource.RLIMIT_FSIZE, (hard, hard))
            memfd = AC.sealed_copy(src_fd)
        except (OSError, ValueError) as e:
            raise _SetupFailed("artifact_copy_failed", getattr(e, "errno", 0) or 0) from None
    finally:
        os.close(src_fd)
    try:
        try:
            got = AC.sha256_fd(memfd)
        except OSError as e:
            raise _SetupFailed("artifact_copy_failed", e.errno or 0) from None
        if got != digest:
            raise _SetupFailed("artifact_digest_mismatch")
        try:
            identity, _why = AC.inspect_cli_artifact(memfd)
        except OSError:
            identity = None
        if identity is None:
            raise _SetupFailed("artifact_copy_unsupported")
    except BaseException:
        os.close(memfd)
        raise
    return memfd


def _execveat(cli_fd: int, argv, envp) -> int:
    """成功すれば戻らない。失敗の errno を返す。"""
    c_argv = (ctypes.c_char_p * (len(argv) + 1))(*[a.encode() if isinstance(a, str) else a
                                                    for a in argv], None)
    c_envp = (ctypes.c_char_p * (len(envp) + 1))(*envp, None)
    ctypes.set_errno(0)
    _libc().syscall(ctypes.c_long(_SYS_EXECVEAT), ctypes.c_int(cli_fd), ctypes.c_char_p(b""),
                    c_argv, c_envp, ctypes.c_int(_AT_EMPTY_PATH))
    return ctypes.get_errno() or _errno.EIO


def _run(cfg: dict) -> None:
    libc = _libc()
    status_fd = int(cfg["status_fd"])
    src_fd = int(cfg["cli_fd"])
    digest = str(cfg["artifact_sha256"])
    nonce = bytes.fromhex(cfg["nonce"])
    if len(nonce) != P.NONCE_LEN:
        raise _Abort("nonce")

    # L1: status fd を exec で残さない。pass_fds で継承可能になっているので立て直す
    os.set_inheritable(status_fd, False)
    if os.get_inheritable(status_fd):
        raise _Abort("status cloexec")
    sock = socket.socket(fileno=status_fd)
    if sock.type != socket.SOCK_SEQPACKET or sock.family != socket.AF_UNIX:
        raise _Abort("status type")

    # L2: worker が死んだら一緒に死ぬ。設定前に親が替わっていないかを照合する
    if libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        raise _Abort("pdeathsig")
    if os.getppid() != int(cfg["expected_ppid"]):
        raise _Abort("ppid")

    try:
        # L3
        try:
            inherited = _seccomp_mode() != 0
        except OSError:
            inherited = True
        if inherited:
            raise _SetupFailed("inherited_seccomp_filter")
        _check_credentials(int(cfg["uid"]), int(cfg["gid"]), cfg["groups"])
        try:
            compiled = seccomp_cli_bpf.compile_policy()
        except Exception:
            raise _SetupFailed("policy_compile_failed") from None

        # L4
        _send(sock, P.encode_hello(nonce, os.getpid()))
        _expect(sock, nonce, P.JOINED)

        # L4a: mission cgroup に入った後で写す (memfd の page を mission cgroup に課金する)
        cli_fd = _sealed_exec_artifact(src_fd, digest)
        st = os.fstat(cli_fd)
        exec_identity = (st.st_dev, st.st_ino)
        # L4b: RLIMIT_FSIZE は memfd への書込みにも掛かるので、写した後に固定する
        _set_rlimits(cfg["backend"])

        # L5: subreaper → no_new_privs → nested Landlock
        if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
            raise _SetupFailed("subreaper_failed", ctypes.get_errno())
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise _SetupFailed("no_new_privs_failed", ctypes.get_errno())
        abi = landlock_nested.probe_abi()
        try:
            landlock_nested.apply_nested_landlock()
        except landlock_nested.NestedLandlockError as e:
            raise _SetupFailed(e.reason) from None

        # L6〜L7
        try:
            listener = install_filter(compiled.program, compiled.filter_flags)
        except OSError as e:
            raise _SetupFailed("seccomp_install_failed", e.errno or 0) from None
        try:
            try:
                ok = _seccomp_mode() == 2 and _no_new_privs() == 1
            except OSError:
                ok = False
            if not ok:
                raise _SetupFailed("seccomp_self_check_failed")
            # 孤児の引取り先が CLI になる前提。立っていなければ孤児が worker へ上がる
            try:
                subreaper = _subreaper_flag()
            except OSError:
                subreaper = 0
            if subreaper != 1:
                raise _SetupFailed("subreaper_failed")
        except BaseException:
            os.close(listener)
            raise

        # L8: 送信の成否によらず launcher の copy は閉じる
        prepared = P.Prepared(compiled.profile_id, compiled.policy_hash, compiled.bpf_hash,
                              compiled.filter_flags, abi, subreaper)
        try:
            _send(sock, P.encode_prepared(nonce, prepared, exec_identity), fds=[listener])
        finally:
            os.close(listener)
    except _SetupFailed as e:
        try:
            _send(sock, P.encode_setup_failed(nonce, e.reason, e.detail))
        except OSError:
            pass
        raise _Abort(e.reason) from None

    # L8w
    _expect(sock, nonce, P.GO)

    # L9: 0/1/2 以外は exec で閉じる。失敗時に EXEC_FAILED を送れるよう、
    # close ではなく CLOEXEC にする
    if libc.syscall(ctypes.c_long(_SYS_CLOSE_RANGE), ctypes.c_uint(3),
                    ctypes.c_uint(0xFFFFFFFF), ctypes.c_uint(_CLOSE_RANGE_CLOEXEC)) != 0:
        err = ctypes.get_errno()
    else:
        envp = [k + b"=" + v for k, v in os.environb.items()]
        err = _execveat(cli_fd, cfg["argv"], envp)
    try:
        _send(sock, P.encode_exec_failed(nonce, err))
    except OSError:
        pass
    raise _Abort("exec")


def main(argv=None) -> None:
    args = sys.argv[1:] if argv is None else argv
    try:
        if len(args) != 1:
            raise _Abort("argv")
        _run(json.loads(args[0]))
    except BaseException:  # noqa: BLE001  失敗の申告は frame で済んでいる
        pass
    os._exit(1)


if __name__ == "__main__":
    main()
