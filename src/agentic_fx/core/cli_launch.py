"""改善 mission の worker 側: launcher を起こし、status protocol で setup を証明する。

worker は CLI の実行 artifact (x86_64 の 64 bit ELF に限る) を検査してから
`SOCK_SEQPACKET` の socketpair を作り、子端と CLI
binary の fd だけを launcher に渡す。launcher は mission cgroup に入った後でその bytes を封印した
memfd に写し、admission の digest と照合してから memfd を exec する (memfd の page は mission
cgroup に課金される)。`launcher_proc` は worker の direct child で、
wait / reap するのは worker だけ。setup の成否は frame だけで決め、launcher の
stderr や exit code は見ない。

state: wait_hello → (cgroup join) → JOINED → wait_prepared → (receiver へ handoff)
→ GO → wait_exec → `cli_started` / `cli_exec_unproven`。

close の所有:

| fd | 閉じる者 |
|---|---|
| status 子端 | launcher (CLOEXEC / `_exit`)。worker は Popen 直後に自分の copy を閉じる |
| status 親端 | worker が結果の確定時に閉じる |
| listener (handoff 前) | 受信した worker が、PREPARED が不正なら閉じる |
| listener (handoff 後) | receiver だけ。worker は参照を持たない |
| CLI binary fd | worker は Popen 直後に閉じる、launcher は memfd に写した後で閉じる |
| 封印済み memfd | launcher が作り、exec で閉じる (CLOEXEC) |

cgroup の join と receiver への handoff は呼び出し側が関数で渡す (この module は
cgroup も receiver も持たない)。
"""
from __future__ import annotations

import errno as _errno
import fcntl
import json
import math
import os
import select
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from agentic_fx.core import cli_artifact_copy as AC
from agentic_fx.core import cli_launch_protocol as P
from agentic_fx.core import landlock_nested
from agentic_fx.core import seccomp_cli_bpf

BEFORE_HANDOFF = "before_handoff"
AFTER_HANDOFF = "after_handoff"

SANDBOX_UNAVAILABLE = "sandbox_unavailable"
FAILED = "failed"

LAUNCHER_MODULE = "agentic_fx.core.cli_launcher"

# seccomp notify fd の判定。readlink の表記と、kernel が notify fd にだけ返す
# ID_VALID の ENOENT の両方を見る。
SECCOMP_NOTIFY_LINK = "anon_inode:seccomp notify"
_SECCOMP_IOCTL_NOTIF_ID_VALID = 0x40082102

# 失敗後の reap に使う既定の猶予 (呼び出し側が cleanup deadline を渡さないとき)
_DEFAULT_REAP_GRACE_SEC = 2.0


@dataclass(frozen=True)
class ExpectedCredentials:
    uid: int
    gid: int
    groups: tuple[int, ...]


def current_credentials() -> ExpectedCredentials:
    """worker 自身の uid / gid / supplementary group (launcher はこれと一致を要求する)。"""
    return ExpectedCredentials(os.getuid(), os.getgid(), tuple(sorted(set(os.getgroups()))))


def expected_prepared() -> P.Prepared:
    """worker 側で compile した値と実 Landlock ABI。PREPARED はこれと一致を要求する。"""
    c = seccomp_cli_bpf.compile_policy()
    return P.Prepared(c.profile_id, c.policy_hash, c.bpf_hash, c.filter_flags,
                      landlock_nested.probe_abi(), subreaper=1)


def open_cli_binary(path: str) -> int:
    """CLI binary を `O_RDONLY|O_CLOEXEC` で開く。同じ fd を hash して launcher に
    渡し、path を再 open しない。"""
    return os.open(path, os.O_RDONLY | os.O_CLOEXEC)


#: 開いた fd を x86_64 の 64 bit ELF (EXEC / DYN) として検査する (launcher も同じ関数で memfd を
#: 検査し直す)
inspect_cli_artifact = AC.inspect_cli_artifact


@dataclass
class LaunchResult:
    """`started` が真なら `launcher_proc` は CLI そのもの (未 reap)。

    偽なら `reason` は固定 reason、`series` は cleanup の系列 (handoff 前 / 後)。
    handoff 前の失敗は返す前に kill と reap を済ませる (`reaped`)。handoff 後の失敗は
    receiver の停止が先なので reap せずに返し、呼び出し側が `reap_launcher` する。

    `untransferred_fds` は呼び出し側が閉じる fd。handoff 前の listener と status 親端は
    `launch_cli` 自身が閉じてから返すので常に空 (閉じる所有者を 1 つにする)。

    `exec_identity` は launcher が PREPARED で申告した、exec する memfd の (st_dev, st_ino)。"""

    started: bool
    reason: str | None
    series: str | None
    classification: str | None
    launcher_proc: subprocess.Popen | None
    reaped: bool
    detail: str = ""
    setup_detail: int | None = None
    untransferred_fds: tuple[int, ...] = ()
    exec_identity: tuple[int, int] | None = None


class LaunchError(Exception):
    """`launch_cli` の中の想定外の例外 (`__cause__` に元の例外) を包む。

    launcher は起動済みで未 reap のまま渡すので、呼び出し側が `series` に従って
    停止と reap をする (handoff 前なら kill → reap、handoff 後なら receiver の停止が
    先)。包むのは `Exception` 系だけで、KeyboardInterrupt / SystemExit /
    GeneratorExit は呼び出し側の `except Exception` に捕まらないよう包まない。
    handoff 前の listener と status 親端は送出の前に閉じてあり、`untransferred_fds` は空。"""

    untransferred_fds: tuple[int, ...] = ()

    def __init__(self, launcher_proc: subprocess.Popen, series: str) -> None:
        super().__init__(f"unexpected error in launch_cli ({series})")
        self.launcher_proc = launcher_proc
        self.series = series


class LaunchInterrupted(BaseException):
    """`launch_cli` の中で KeyboardInterrupt / SystemExit / GeneratorExit を受けた。

    `Exception` を継承しないので呼び出し側の `except Exception` には捕まらず、中断の
    要求は通る。元の例外は `__cause__`。launcher は起動済みで未 reap のまま渡すので、
    呼び出し側が `series` に従って停止と reap をする。handoff 前なら受け取り途中の
    listener と status 親端はここで閉じてあり、`untransferred_fds` は空。"""

    untransferred_fds: tuple[int, ...] = ()

    def __init__(self, launcher_proc: subprocess.Popen, series: str) -> None:
        super().__init__(f"launch_cli interrupted ({series})")
        self.launcher_proc = launcher_proc
        self.series = series


def reap_launcher(proc: subprocess.Popen, *, deadline: float,
                  kill: Callable[[], None] | None = None) -> bool:
    """kill (cgroup.kill など) の後に必ず launcher pid への SIGKILL も送り、deadline までに
    reap する。

    JOINED より前に止まった・死んだ launcher は mission cgroup に居ないので、cgroup.kill
    だけでは届かない。未 reap の pid は再利用されないので、pid 指定の kill はこの process に
    届く。"""
    if proc.returncode is None:
        if kill is not None:
            try:
                kill()
            except Exception:  # noqa: BLE001  kill の失敗でも pid 宛の SIGKILL は送る
                pass
        try:
            proc.kill()
        except (OSError, ProcessLookupError):
            pass
    try:
        proc.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        return False
    return True


def is_seccomp_notify_fd(fd: int) -> bool:
    try:
        link = os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return False
    if link != SECCOMP_NOTIFY_LINK:
        return False
    try:
        fcntl.ioctl(fd, _SECCOMP_IOCTL_NOTIF_ID_VALID, struct.pack("<Q", 0))
    except OSError as e:
        return e.errno == _errno.ENOENT
    return False


# --- 受信 -------------------------------------------------------------------------

@dataclass
class _Event:
    kind: str                     # "frame" / "eof" / "deadline" / "malformed"
    frame: P.Frame | None = None
    fds: list[int] | None = None
    detail: str = ""


def _hup(fd: int) -> bool:
    p = select.poll()
    p.register(fd, select.POLLIN | select.POLLHUP | select.POLLRDHUP)
    return any(ev & (select.POLLHUP | select.POLLRDHUP) for _fd, ev in p.poll(0))


def _recv(sock: socket.socket, nonce: bytes, deadline: float) -> _Event:
    fd = sock.fileno()
    poller = select.poll()
    poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLRDHUP | select.POLLERR)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _Event("deadline")
        if not poller.poll(math.ceil(remaining * 1000)):
            continue
        try:
            data, anc, flags, _ = sock.recvmsg(
                P.RECV_DATA_LEN, P.RECV_ANC_LEN,
                socket.MSG_CMSG_CLOEXEC | socket.MSG_DONTWAIT)
        except (BlockingIOError, InterruptedError):
            continue
        except OSError:
            return _Event("eof")
        fds, other = P.take_fds(anc)
        if not data and not anc and not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
            # 長さ 0 の datagram と EOF は recvmsg では区別できない。HUP で分ける
            if _hup(fd):
                return _Event("eof")
            return _Event("malformed", detail="zero length frame")
        if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
            P.close_fds(fds)
            return _Event("malformed", detail=f"truncated flags={flags:#x}")
        if other:
            P.close_fds(fds)
            return _Event("malformed", detail=f"ancillary {other}")
        try:
            frame = P.decode(data, nonce)
        except P.MalformedFrame as e:
            P.close_fds(fds)
            return _Event("malformed", detail=e.detail)
        return _Event("frame", frame, fds)


# --- 本体 -------------------------------------------------------------------------

class _Fail(Exception):
    def __init__(self, reason: str, series: str, detail: str = "",
                 setup_detail: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.series = series
        self.detail = detail
        self.setup_detail = setup_detail


def _classify(reason: str) -> str:
    return FAILED if reason == P.CLI_EXEC_UNPROVEN else SANDBOX_UNAVAILABLE


def _fail_from_event(ev: _Event, series: str, *, eof_reason: str) -> _Fail:
    if ev.kind == "deadline":
        return _Fail(P.ATTESTATION_DEADLINE, series)
    if ev.kind == "eof":
        return _Fail(eof_reason, series)
    if ev.kind == "malformed":
        return _Fail(P.ATTESTATION_MALFORMED, series, ev.detail)
    raise AssertionError(ev.kind)


def _plain_frame(ev: _Event, series: str, *, eof_reason: str) -> P.Frame:
    """ancillary の付かない frame を 1 個取り出す。付いていれば閉じて malformed。"""
    if ev.kind != "frame":
        raise _fail_from_event(ev, series, eof_reason=eof_reason)
    assert ev.frame is not None
    if ev.fds:
        P.close_fds(ev.fds)
        raise _Fail(P.ATTESTATION_MALFORMED, series,
                    f"fd on {P.TYPE_NAMES[ev.frame.type]}")
    return ev.frame


def _setup_failed(frame: P.Frame, series: str) -> _Fail:
    return _Fail(frame.reason or P.ATTESTATION_MALFORMED, series,
                 "SETUP_FAILED", setup_detail=frame.errno)


def launch_cli(
    *,
    cli_fd: int,
    argv: Sequence[str],
    env: Mapping[str, str],
    backend: str,
    join: Callable[[int], None],
    handoff: Callable[[int], None],
    deadline: float,
    kill: Callable[[], None] | None = None,
    reap_deadline: float | None = None,
    nonce: bytes | None = None,
    expected: P.Prepared | None = None,
    credentials: ExpectedCredentials | None = None,
    cwd: str | None = None,
    artifact_sha256: str | None = None,
    artifact_identity=None,
    launch_kind: str = "native_elf",
    stdin=subprocess.DEVNULL,
    stdout=None,
    stderr=None,
    launcher_cmd: Sequence[str] | None = None,
    after_spawn: Callable[[], None] | None = None,
) -> LaunchResult:
    """launcher を起こし、`cli_started` か固定 reason の失敗を返す。

    - `cli_fd`: `open_cli_binary` で開いた fd。所有権はここに移り、Popen 直後に閉じる。
      x86_64 の 64 bit ELF でなければ launcher を起こさず `cli_artifact_unsupported`。
      codex は npm の wrapper (Node の `#!` script) でなく、同梱の native ELF
      (`vendor/x86_64-unknown-linux-musl/bin/codex`) を開いて渡す。
    - `env`: CLI に渡す環境。launcher process の環境として渡し、launcher がそのまま
      `execveat` に渡す (argv に載せると cmdline から読めるため)。codex には
      wrapper が足す managed 環境変数 (`CODEX_MANAGED_PACKAGE_ROOT`、
      `CODEX_MANAGED_BY_NPM` など `CODEX_MANAGED_*`) を渡さない契約で、native ELF は
      それらが未設定でも完走する。
    - `join(pid)`: HELLO の pid を mission cgroup に入れる。例外で `cgroup_join_failed`。
    - `handoff(fd)`: listener fd の所有権を receiver へ移す。正常に戻った時点で handoff
      完了とし、worker は以後その fd に触れない。例外なら fd を保持していない約束で、
      worker が閉じて `receiver_handoff_failed` (handoff 前)。
    - `deadline`: setup 全体の monotonic absolute deadline。
    - `kill`: handoff 前の失敗で reap の前に呼ぶ停止 (cgroup.kill に差し替える)。その後に
      launcher pid への SIGKILL も必ず送る (既定は SIGKILL だけ)。
    - 想定外の例外 (`Exception` 系) は `LaunchError` (launcher の handle と系列を持つ)
      に包んで送出する。KeyboardInterrupt / SystemExit / GeneratorExit は
      `LaunchInterrupted` (`BaseException`、同じく handle と系列を持つ) で送出する。
    - 呼べるのは worker の main thread だけ。launcher の `PR_SET_PDEATHSIG` は親
      process でなく親の thread に結びつき、その thread が終わると launcher と CLI に
      SIGKILL が届く。main thread 以外からは launcher を起こさず
      `launch_thread_unsupported` で返す。
    - 残余: CLI が zombie になっていて exe を照合できないときは、証明できないので
      `cli_exec_unproven` で fail closed とする (仕様どおり)。
    - `artifact_sha256`: launcher が写した bytes に求める SHA-256 (afx が admission で見た値)。
      None なら `cli_fd` をここで読んで取る (隔離の mission は必ず admission の値を渡す)。
      違えば launcher が `artifact_digest_mismatch` で止まる。
    - `launcher_cmd`: launcher の起動 argv の前半 (config の JSON が後ろに付く)。既定は
      `python -I -m agentic_fx.core.cli_launcher`。
    - `after_spawn()`: launcher を fork した直後 (HELLO を待つ前) に worker 側で 1 回呼ぶ。
      launcher にだけ要る rlimit を worker 自身から外すのに使う。例外は `LaunchError`。
    """
    if threading.current_thread() is not threading.main_thread():
        os.close(cli_fd)
        return LaunchResult(False, P.LAUNCH_THREAD_UNSUPPORTED, BEFORE_HANDOFF,
                            SANDBOX_UNAVAILABLE, None, True, threading.current_thread().name)
    nonce = P.new_launch_nonce() if nonce is None else nonce
    expected = expected_prepared() if expected is None else expected
    creds = current_credentials() if credentials is None else credentials
    cmd = list(launcher_cmd) if launcher_cmd is not None else [
        sys.executable, "-I", "-m", LAUNCHER_MODULE]

    # ELF でなければ launcher を起こさない (launcher も写した memfd を検査し直す)
    try:
        cli_identity, why = inspect_cli_artifact(cli_fd)
        if cli_identity is not None and artifact_sha256 is None:
            artifact_sha256 = AC.sha256_fd(cli_fd)
    except OSError as e:
        cli_identity, why = None, f"inspect: {e}"
    if cli_identity is None:
        os.close(cli_fd)
        return LaunchResult(False, P.CLI_ARTIFACT_UNSUPPORTED, BEFORE_HANDOFF,
                            SANDBOX_UNAVAILABLE, None, True, why)
    if artifact_identity is None:
        from agentic_fx.core.improve_fingerprint import hash_artifact_fd
        artifact_identity = hash_artifact_fd(cli_fd)
    try:
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    except OSError as e:
        os.close(cli_fd)
        return LaunchResult(False, P.LAUNCHER_SPAWN_FAILED, BEFORE_HANDOFF,
                            SANDBOX_UNAVAILABLE, None, True, str(e))

    cfg = {
        "status_fd": child.fileno(), "cli_fd": cli_fd, "nonce": nonce.hex(),
        "expected_ppid": os.getpid(), "uid": creds.uid, "gid": creds.gid,
        "groups": list(creds.groups), "backend": backend, "argv": list(argv),
        "artifact_sha256": artifact_sha256,
        "artifact_identity": artifact_identity.as_dict(), "launch_kind": launch_kind,
    }
    try:
        proc = subprocess.Popen(
            [*cmd, json.dumps(cfg)], pass_fds=(child.fileno(), cli_fd), close_fds=True,
            env=dict(env), cwd=cwd, stdin=stdin, stdout=stdout, stderr=stderr)
    except (OSError, subprocess.SubprocessError) as e:
        child.close()
        parent.close()
        os.close(cli_fd)
        return LaunchResult(False, P.LAUNCHER_SPAWN_FAILED, BEFORE_HANDOFF,
                            SANDBOX_UNAVAILABLE, None, True, str(e))
    # 子端と CLI fd の worker 側 copy は直ちに閉じる。子端を残すと launcher の死が
    # EOF として見えない
    child.close()
    os.close(cli_fd)

    series = BEFORE_HANDOFF
    held: list[int] = []  # handoff 前に worker が持っている listener
    exec_identity: tuple[int, int] | None = None
    try:
        if after_spawn is not None:
            after_spawn()
        # wait_hello
        frame = _plain_frame(_recv(parent, nonce, deadline), series,
                             eof_reason=P.LAUNCHER_DIED)
        if frame.type == P.SETUP_FAILED:
            raise _setup_failed(frame, series)
        if frame.type != P.HELLO:
            raise _Fail(P.ATTESTATION_ORDER, series, P.TYPE_NAMES[frame.type])
        if frame.pid != proc.pid:
            raise _Fail(P.ATTESTATION_MISMATCH, series, f"hello pid {frame.pid}")
        try:
            join(proc.pid)
        except Exception as e:  # noqa: BLE001  join の失敗は種類によらず固定 reason
            raise _Fail(P.CGROUP_JOIN_FAILED, series, repr(e)) from None
        try:
            parent.sendmsg([P.encode_joined(nonce)])
        except OSError as e:
            raise _Fail(P.LAUNCHER_DIED, series, f"JOINED send: {e}") from None

        # wait_prepared
        ev = _recv(parent, nonce, deadline)
        if ev.kind != "frame":
            raise _fail_from_event(ev, series, eof_reason=P.LAUNCHER_DIED)
        assert ev.frame is not None and ev.fds is not None
        if ev.frame.type != P.PREPARED:
            frame = _plain_frame(ev, series, eof_reason=P.LAUNCHER_DIED)
            if frame.type == P.SETUP_FAILED:
                raise _setup_failed(frame, series)
            raise _Fail(P.ATTESTATION_ORDER, series, P.TYPE_NAMES[frame.type])
        fds = ev.fds
        held = list(fds)
        if len(fds) != 1 or not is_seccomp_notify_fd(fds[0]):
            _close_held(held)
            raise _Fail(P.ATTESTATION_MALFORMED, series,
                        f"PREPARED carries {len(fds)} fd(s) / not a seccomp listener")
        if ev.frame.prepared != expected:
            _close_held(held)
            raise _Fail(P.ATTESTATION_MISMATCH, series,
                        f"prepared {ev.frame.prepared} != {expected}")
        # exe の照合は launcher が申告した memfd の identity で行う
        exec_identity = ev.frame.exec_identity
        listener = fds[0]
        try:
            handoff(listener)
        except Exception as e:  # noqa: BLE001  receiver が受け取らなかった
            _close_held(held)
            raise _Fail(P.RECEIVER_HANDOFF_FAILED, series, repr(e)) from None
        # handoff 完了: worker は listener の参照を破棄する
        del listener, fds, ev
        held = []
        series = AFTER_HANDOFF
        try:
            parent.sendmsg([P.encode_go(nonce)])
        except OSError as e:
            raise _Fail(P.ATTESTATION_GO_SEND_FAILED, series, str(e)) from None

        # wait_exec
        ev = _recv(parent, nonce, deadline)
        if ev.kind == "eof":
            if exec_identity is None or _exe_identity(proc.pid) != exec_identity:
                raise _Fail(P.CLI_EXEC_UNPROVEN, series)
        else:
            frame = _plain_frame(ev, series, eof_reason=P.LAUNCHER_DIED)
            if frame.type == P.EXEC_FAILED:
                raise _Fail(P.ATTESTATION_EXEC_FAILED, series, f"errno={frame.errno}",
                            setup_detail=frame.errno)
            raise _Fail(P.ATTESTATION_ORDER, series, P.TYPE_NAMES[frame.type])
    except _Fail as f:
        parent.close()
        reaped = False
        if f.series == BEFORE_HANDOFF:
            rd = reap_deadline if reap_deadline is not None else (
                max(deadline, time.monotonic()) + _DEFAULT_REAP_GRACE_SEC)
            reaped = reap_launcher(proc, deadline=rd, kill=kill)
        return LaunchResult(False, f.reason, f.series, _classify(f.reason), proc, reaped,
                            f.detail, f.setup_detail, exec_identity=exec_identity)
    except Exception as e:
        # 想定外の例外では status 親端と handoff 前の listener を閉じ、launcher の handle と系列を付けて
        # 呼び出し側へ渡す。kill と reap の順序は handoff の前後で違うのでここでは
        # 決めない
        _close_held(held)
        parent.close()
        raise LaunchError(proc, series) from e
    except BaseException as e:
        # 中断・終了の要求は `except Exception` に捕まらない型で通すが、launcher の
        # handle は失わない。handoff 前に worker が持つ listener と status 親端は閉じる
        _close_held(held)
        parent.close()
        raise LaunchInterrupted(proc, series) from e
    parent.close()
    return LaunchResult(True, None, None, None, proc, False, exec_identity=exec_identity)


def _close_held(held: list[int]) -> None:
    """handoff 前に worker が持つ listener を閉じる (receiver にも呼び出し側にも
    渡っていないので、ここで閉じないと worker の終了まで残る)。

    close の前に `held` から外す。Linux の close は EINTR を返しても fd を解放しているので、
    失敗した番号を残すと後の経路がもう一度閉じ、再利用された別の fd を閉じ得る。"""
    while held:
        fd = held.pop()
        try:
            os.close(fd)
        except OSError:
            pass


def _exe_identity(pid: int) -> tuple[int, int] | None:
    try:
        st = os.stat(f"/proc/{pid}/exe")
    except OSError:
        return None
    return (st.st_dev, st.st_ino)
