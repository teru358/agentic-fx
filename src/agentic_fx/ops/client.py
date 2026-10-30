"""同梱 CLI client ``afx ctl``。

同じマシンの daemon の操作 API (`data/run/api.sock`) へ httpx の UDS transport で
接続する。鍵を送る前に接続先を確かめる: socket の型・所有者・親 dir 0700、接続した
socket の ``SO_PEERCRED`` uid、``data/instance.lock`` の保持者 pid と peer pid、
可能なら pidfd の pid。確かめられなければ鍵を送らず rc=3 で終える。

確認は接続ごとに行う (server は 1 接続 1 要求なので、要求ごとに新しい接続になる)。
検査は httpx が socket をつないだ直後、1 byte も書く前に走るので、検査に落ちた接続
には要求の頭も鍵も流れない。

この module は ``.env`` を読まず、鍵を生成しない。
"""
from __future__ import annotations

import errno
import os
import re
import select
import shlex
import socket
import stat
import struct
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TextIO

import httpcore
import httpx

from agentic_fx.core import improve_repeat as ir
from agentic_fx.loops import approval_facts
from agentic_fx.loops.approval_facts import display_text
from agentic_fx.cli_surface import render_ctl_help
from agentic_fx.setup.pending_text import (STEP_PARAMS, PendingStep, is_ident,
                                           parse_observed_at, render_pending_step)

from . import change_limits, keys, redactor
from .contracts import Principal
from .events import EventCursor, EventPage
from .tail import ACTIVITY_CATEGORIES
from .trade_events import DROP_REASONS

RC_OK = 0
RC_FAILED = 1
RC_USAGE = 2
RC_UNREACHABLE = 3
RC_INTERRUPTED = 130

DEFAULT_SOCKET = "data/run/api.sock"
SO_PEERPIDFD = getattr(socket, "SO_PEERPIDFD", 77)
_UCRED = struct.Struct("3i")
_HTTP_TIMEOUT = 15.0
_POLL_INTERVAL = 0.25
# job の poll 上限。server 側の job deadline に少し足した値 (待ちを無期限にしない)。
_POLL_LIMITS = {"approve": 45.0, "reject": 45.0, "retry": 45.0, "improve": 45.0,
                "ask": 905.0}
_TEXT_LIMIT = 2000
_JOB_ID = re.compile(r"[0-9a-f]{32}")
_INCIDENT = re.compile(r"[0-9a-f]{1,32}")

def _usage() -> str:
    return render_ctl_help()

STOP_GUIDANCE = ("afx ctl は stop を送りません。サービスを止めるには "
                 "`systemctl --user stop <unit>` (SIGTERM) を使ってください")

# 失敗の文面。rc=3 は「接続先へ届かない・認証できない・受け付けられない」。
MESSAGES = {
    "no_socket": "サービスに接続できません: socket がありません ({path})。"
                 "afx が稼働中で api.enabled=true か確認してください",
    "refused": "サービスに接続できません: 接続を拒否されました ({path})。"
               "afx が稼働中か確認してください",
    "no_key": "鍵を読めません ({code}): {path}。`afx keys init` の状態を確認してください",
    "server_auth": "接続先を agentic-fx の daemon と確認できないため、鍵を送りませんでした "
                   "({reason})",
    "unauthenticated": "認証に失敗しました (鍵が daemon の鍵と一致しません)。"
                       "鍵の交換後は daemon を再起動してください",
    "forbidden": "この操作の権限がありません。approver 操作は `afx ctl --as approver` で"
                 "実行してください",
    "peer_rejected": "daemon が接続元を拒否しました (サービス自身の子 process からは"
                     "使えません)",
    "autopilot_restricted": "autopilot 中はこの操作を受け付けません",
    "unavailable": "daemon が要求を受け付けられません (混雑または停止中)。"
                   "少し待ってから再実行してください",
    "lost": "応答を受け取れませんでした。結果は status / jobs get で確認してください",
    "config": "設定を読めません: {path}",
    "need_approver": "この操作は approver の権限が要ります。`afx ctl --as approver ...` で"
                     "実行してください (何も送っていません)",
    "need_terminal": "この操作は端末からの確認が要ります。stdin と /dev/tty の両方が端末の"
                     "ときだけ送ります (何も送っていません)",
    "cancelled": "取り消しました (何も送っていません)",
}

# 409 などの業務上の失敗。対話シェルと同じ言い回しにそろえる。
_ERROR_TEXT = {
    "invalid_argument": "引数が不正です",
    "not_found": "対象が存在しません",
    "request_timeout": "時間内に処理できませんでした",
    "already_decided": "その approval は決定済みです",
    "payload_changed": "表示した後に承認依頼の内容が変わったため決定しませんでした。"
                       "もう一度内容を確認してください",
    "unsupported_kind": "plugin 以外の kind のため決定できません",
    "plugin_busy": "plugin が別の処理で使用中です。少し待ってから再実行してください",
    "mission_busy": "mission が実行中のため受け付けられません",
    "improve_running": "improve が既に実行中です",
    "decision_in_progress": "同じ approval の決定が進行中です",
    "generation_mismatch": "kill switch のラッチが更新されたので status を確認してから"
                           "もう一度実行してください",
    "not_latched": "kill switch はラッチされていません",
    "invalid_state": "現在の状態ではこの操作を行えません",
    "attempt_changed": "試行記録が更新されたため戻しませんでした。もう一度実行してください",
    "idempotency_mismatch": "同じ要求 key で別の内容が送られました",
    "outcome_unknown": "以前の同じ要求の結果が不明です。対象の状態を確認してください",
    "job_running": "job は既に実行中のため取り消せません",
    "payload_too_large": "本文が大きすぎます",
    "internal": "daemon の内部エラーです",
    "database_busy": "daemon の DB が混雑しています",
}


class ClientError(Exception):
    """rc と固定の文面を持つ失敗。"""

    def __init__(self, rc: int, message: str) -> None:
        super().__init__(message)
        self.rc, self.message = rc, message


class _ResponseLost(ClientError):
    """要求を書いた後に応答を受け取れなかった。"""

    def __init__(self) -> None:
        super().__init__(RC_UNREACHABLE, MESSAGES["lost"])


class ServerAuthError(Exception):
    """接続先を確かめられない。``reason`` は固定語。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _NoSocket(Exception):
    pass


# --------------------------------------------------------------- server 認証

def check_socket_path(path: Path, *, uid: int) -> None:
    """接続前の静的検査: 親 dir は同 uid の 0700 の dir、socket は同 uid の socket。"""
    # 無いことが分かった場合だけ「socket 無し」。権限などで確かめられなければ鍵を送らない。
    try:
        dir_st = os.lstat(path.parent)
    except FileNotFoundError:
        raise _NoSocket() from None
    except OSError:
        raise ServerAuthError("socket_dir") from None
    if (not stat.S_ISDIR(dir_st.st_mode) or dir_st.st_uid != uid
            or stat.S_IMODE(dir_st.st_mode) != 0o700):
        raise ServerAuthError("socket_dir")
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise _NoSocket() from None
    except OSError:
        raise ServerAuthError("socket_file") from None
    if not stat.S_ISSOCK(st.st_mode) or st.st_uid != uid:
        raise ServerAuthError("socket_file")


def lock_holders(lock_path: Path, *, proc_locks: Path = Path("/proc/locks")) -> set[int]:
    """``/proc/locks`` から lock file の FLOCK WRITE 保持者の pid を集める。

    行は ``N: FLOCK  ADVISORY  WRITE <pid> <major>:<minor>:<inode> 0 EOF`` で、
    major / minor は 16 進。``->`` の付いた行は待っている側なので数えない。

    inode の一致は必須。装置番号は btrfs・overlayfs・NFS では stat の値 (匿名 dev) と
    ``/proc/locks`` の値 (superblock の dev) が食い違うので、一致すれば速い経路で数え、
    食い違うときはその pid が lock file を fd で開いていることを確かめて数える。
    """
    try:
        st = os.stat(lock_path)
        real_path = os.path.realpath(lock_path)
    except OSError:
        return set()
    device = (os.major(st.st_dev), os.minor(st.st_dev))
    holders = set()
    try:
        text = proc_locks.read_text(encoding="ascii", errors="replace")
    except OSError:
        return set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 6 or "->" in parts:
            continue
        kind, mode, pid_text, dev_text = parts[1], parts[3], parts[4], parts[5]
        if kind != "FLOCK" or mode != "WRITE":
            continue
        fields = dev_text.split(":")
        try:
            ident = (int(fields[0], 16), int(fields[1], 16), int(fields[2]))
            pid = int(pid_text)
        except (IndexError, ValueError):
            continue
        if ident[2] != st.st_ino:
            continue
        if ident[:2] == device or _has_open_fd(pid, real_path):
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


def _pidfd_pid(pidfd: int) -> int | None:
    with open(f"/proc/self/fdinfo/{pidfd}", encoding="ascii") as file:
        for line in file:
            if line.startswith("Pid:"):
                return int(line.split()[1])
    return None


def _pidfd_exited(pidfd: int) -> bool:
    """pidfd が POLLIN (相手が終了済み。zombie を含む) なら True。"""
    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    return any(mask & (select.POLLIN | select.POLLHUP | select.POLLERR)
               for _fd, mask in poller.poll(0))


def verify_peer(sock: socket.socket, *, uid: int, lock_path: Path,
                proc_locks: Path = Path("/proc/locks")) -> int:
    """接続済み socket の相手が、同 uid で instance lock を持つ生きた process か確かめる。"""
    # 調べられないことは「接続先を確認できない」で、接続の拒否ではない (鍵は送らない)。
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size)
    except OSError:
        raise ServerAuthError("peer_check") from None
    pid, peer_uid, _gid = _UCRED.unpack(raw)
    if peer_uid != uid or pid <= 0:
        raise ServerAuthError("peer_uid")
    try:
        pidfd = sock.getsockopt(socket.SOL_SOCKET, SO_PEERPIDFD)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            raise ServerAuthError("peer_gone") from None
        pidfd = None
    try:
        if pidfd is not None:
            try:
                pidfd_pid = _pidfd_pid(pidfd)
            except OSError:
                raise ServerAuthError("peer_check") from None
            if pidfd_pid != pid:
                raise ServerAuthError("peer_gone")
        # 保持者を一意に確定できなければ送らない (弱い pid file には落とさない)。
        if lock_holders(lock_path, proc_locks=proc_locks) != {pid}:
            raise ServerAuthError("not_lock_holder")
        # 保持者 (= peer) の生存。pidfd があれば poll で見る (zombie も終了とみなせる)。
        # 無ければ /proc/<pid> の存在で見る。
        if pidfd is not None:
            if _pidfd_exited(pidfd):
                raise ServerAuthError("holder_gone")
        elif not os.path.isdir(f"/proc/{pid}"):
            raise ServerAuthError("holder_gone")
    finally:
        if pidfd is not None:
            os.close(pidfd)
    return pid


class _VerifyingBackend(httpcore.SyncBackend):
    """UDS をつないだ直後、書き込む前に接続先を検査する。"""

    def __init__(self, verify: Callable[[socket.socket], object]) -> None:
        self._verify = verify

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        stream = super().connect_unix_socket(path, timeout=timeout,
                                             socket_options=socket_options)
        try:
            self._verify(stream.get_extra_info("socket"))
        except BaseException:
            stream.close()
            raise
        return stream


# httpcore の例外を同名の httpx の例外へ写す。細かい型を先に並べ、最初に当たったものを使う。
_HTTPCORE_TO_HTTPX = tuple((getattr(httpcore, name), getattr(httpx, name)) for name in (
    "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout", "TimeoutException",
    "ConnectError", "ReadError", "WriteError", "NetworkError",
    "LocalProtocolError", "RemoteProtocolError", "ProtocolError",
    "UnsupportedProtocol", "ProxyError"))


class _MappedErrors:
    """httpcore の例外だけを httpx の例外にする。それ以外 (ServerAuthError など) はそのまま。"""

    def __enter__(self) -> None:
        return None

    def __exit__(self, _type, exc, _tb) -> bool:
        if exc is None or not isinstance(exc, Exception):
            return False
        for source, target in _HTTPCORE_TO_HTTPX:
            if isinstance(exc, source):
                # 原因の連鎖は残す (FileNotFoundError を「socket 無し」と読むため)。
                raise target(str(exc)) from exc
        return False


class _ResponseBody(httpx.SyncByteStream):
    def __init__(self, stream) -> None:
        self._stream = stream

    def __iter__(self):
        with _MappedErrors():
            yield from self._stream

    def close(self) -> None:
        close = getattr(self._stream, "close", None)
        if close is not None:
            close()


class _VerifyingTransport(httpx.BaseTransport):
    """1 接続 1 要求の UDS transport。つないだ直後に接続先を検査する backend を使う。"""

    def __init__(self, socket_path: Path, verify: Callable[[socket.socket], object]) -> None:
        self._connections = httpcore.ConnectionPool(
            uds=str(socket_path), network_backend=_VerifyingBackend(verify),
            max_connections=1, max_keepalive_connections=0, retries=0)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(scheme=request.url.raw_scheme, host=request.url.raw_host,
                             port=request.url.port, target=request.url.raw_path),
            headers=request.headers.raw, content=request.stream,
            extensions=request.extensions)
        with _MappedErrors():
            response = self._connections.handle_request(core_request)
        return httpx.Response(status_code=response.status, headers=response.headers,
                              stream=_ResponseBody(response.stream),
                              extensions=response.extensions)

    def close(self) -> None:
        self._connections.close()


def _declare_empty_body(request: httpx.Request) -> None:
    # server は本文なしの要求にも Content-Length を求める。
    if "content-length" not in request.headers:
        request.headers["Content-Length"] = "0"


def _cause_chain(exc: BaseException):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


# --------------------------------------------------------------- 要求

@dataclass
class Reply:
    status: int
    data: dict | None
    code: str | None


class ApiClient:
    def __init__(self, root: Path, principal: Principal, *, uid: int | None = None,
                 socket_path: Path | None = None) -> None:
        self.root = Path(root)
        self.principal = principal
        self.uid = os.getuid() if uid is None else uid
        self.socket_path = socket_path or self._configured_socket()
        self.lock_path = self.root / "data" / "instance.lock"
        self._token: str | None = None

    def _configured_socket(self) -> Path:
        path = self.root / "config" / "settings.yaml"
        if not path.exists():
            return self.root / DEFAULT_SOCKET
        from agentic_fx.config import ConfigError, load_settings
        try:
            return self.root / load_settings(path).api.socket_path
        except ConfigError:
            raise ClientError(RC_UNREACHABLE, MESSAGES["config"].format(path=path)) from None

    def _read_token(self) -> str:
        if self._token is None:
            directory = keys.key_dir(self.root)
            try:
                self._token = keys.read_token(directory, self.principal)
            except keys.KeySetError as err:
                raise ClientError(RC_UNREACHABLE, MESSAGES["no_key"].format(
                    code=err.code, path=directory)) from None
        return self._token

    def _verify(self, sock: socket.socket) -> None:
        verify_peer(sock, uid=self.uid, lock_path=self.lock_path)

    def request(self, method: str, path: str, *, body: dict | None = None,
                params: dict | None = None, idempotent: bool = False) -> Reply:
        token = self._read_token()
        headers = {"Authorization": f"Bearer {token}"}
        if idempotent:
            headers["Idempotency-Key"] = uuid.uuid4().hex
        # 応答を失ったときに同じ key で 1 回だけ送り直してよいのは冪等 key 付きだけ。
        attempts = 2 if idempotent else 1
        for attempt in range(attempts):
            try:
                return self._send(method, path, headers, body, params)
            except _ResponseLost:
                if attempt + 1 >= attempts:
                    raise
        raise AssertionError("unreachable")

    def _send(self, method: str, path: str, headers: dict, body: dict | None,
              params: dict | None) -> Reply:
        try:
            check_socket_path(self.socket_path, uid=self.uid)
        except _NoSocket:
            raise ClientError(RC_UNREACHABLE,
                              MESSAGES["no_socket"].format(path=self.socket_path)) from None
        except ServerAuthError as err:
            raise ClientError(RC_UNREACHABLE,
                              MESSAGES["server_auth"].format(reason=err.reason)) from None
        transport = _VerifyingTransport(self.socket_path, self._verify)
        sent = False
        try:
            with httpx.Client(transport=transport, base_url="http://afx",
                              timeout=_HTTP_TIMEOUT,
                              event_hooks={"request": [_declare_empty_body]}) as http:
                request = http.build_request(method, path, headers=headers, params=params,
                                             json=body if method == "POST" else None)
                response = http.send(request)
                sent = True
        except ServerAuthError as err:
            raise ClientError(RC_UNREACHABLE,
                              MESSAGES["server_auth"].format(reason=err.reason)) from None
        except httpx.ConnectError as err:
            for cause in _cause_chain(err):
                if isinstance(cause, FileNotFoundError):
                    raise ClientError(RC_UNREACHABLE, MESSAGES["no_socket"].format(
                        path=self.socket_path)) from None
                if isinstance(cause, ServerAuthError):
                    raise ClientError(RC_UNREACHABLE, MESSAGES["server_auth"].format(
                        reason=cause.reason)) from None
            raise ClientError(RC_UNREACHABLE,
                              MESSAGES["refused"].format(path=self.socket_path)) from None
        except httpx.HTTPError:
            raise _ResponseLost() from None
        assert sent
        return _reply(response)


def _reply(response: httpx.Response) -> Reply:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        raise ClientError(RC_FAILED, f"daemon の応答を解釈できません (HTTP {response.status_code})")
    if payload.get("ok") is True and isinstance(payload.get("data"), dict):
        return Reply(response.status_code, payload["data"], None)
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    code = error.get("code") if isinstance(error.get("code"), str) else "internal"
    reply = Reply(response.status_code, None, code)
    if response.status_code in (401, 403, 503):
        key = {401: "unauthenticated", 503: "unavailable"}.get(response.status_code)
        if response.status_code == 403:
            key = code if code in ("peer_rejected", "autopilot_restricted") else "forbidden"
        raise ClientError(RC_UNREACHABLE, MESSAGES[key])
    if code == "internal":
        incident = error.get("incident")
        suffix = (f" (incident {incident})" if isinstance(incident, str)
                  and _INCIDENT.fullmatch(incident) else "")
        raise ClientError(RC_FAILED, _ERROR_TEXT["internal"] + suffix)
    return reply


def error_text(code: str | None) -> str:
    if code in _ERROR_TEXT:
        return _ERROR_TEXT[code]
    return f"失敗しました ({display_text(code, 64)})"


def _fail(reply: Reply) -> ClientError:
    return ClientError(RC_FAILED, error_text(reply.code))


# --------------------------------------------------------------- 端末確認

class Terminal:
    """``/dev/tty`` と対話する確認口。stdin と /dev/tty の両方が端末のときだけ作れる。"""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    @classmethod
    def open(cls, stdin: TextIO) -> "Terminal | None":
        try:
            if not stdin.isatty():
                return None
        except (AttributeError, ValueError):
            return None
        try:
            fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC)
        except OSError:
            return None
        if not os.isatty(fd):
            os.close(fd)
            return None
        return cls(fd)

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def ask(self, prompt: str) -> str | None:
        os.write(self._fd, prompt.encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = os.read(self._fd, 1)
            if not chunk:
                return None
            data += chunk
        return data.decode(errors="replace").strip()

    def yes(self, prompt: str) -> bool:
        answer = self.ask(f"{prompt} [y/N]: ")
        return answer is not None and answer.lower() in ("y", "yes")


# --------------------------------------------------------------- 表示

def _ident(value) -> str:
    """世代や id。整数でなければ (欠落を含め) 無害化した文字列か `-`。"""
    if type(value) is int:
        return str(value)
    return display_text(value)


def _num(value) -> str:
    if type(value) is int or (type(value) is float and value == value):
        return f"{value:,.0f}"
    return "-"


_SETUP_SEVERITIES = frozenset({"info", "warn", "action"})


def _deploy_op_text(row: dict) -> str:
    phase = display_text(row.get("phase"))
    if row.get("fail_reason"):
        phase += "/" + display_text(row.get("fail_reason"))
    return (f"{display_text(row.get('name'))} {display_text(row.get('op_kind'))} {phase} "
            f"op_id={_ident(row.get('op_id'))}")


def _deploy_ops_lines(section) -> list[str]:
    """Open deploy ops and ops needing attention; nothing when there are none."""
    if not isinstance(section, dict):
        return []
    if section.get("unavailable") is True:
        return [f"配備操作の状態を読めませんでした ({display_text(section.get('reason'))})"]
    lines = []
    for key, label in (("open", "配備操作が未完"), ("attention", "配備操作に要注意")):
        rows = [row for row in section.get(key) or [] if isinstance(row, dict)]
        if rows:
            shown = "; ".join(_deploy_op_text(row) for row in rows[:5])
            more = f" ほか {len(rows) - 5} 件" if len(rows) > 5 else ""
            lines.append(f"{label}: {len(rows)} 件 ({shown}{more}) — `afx plugin versions <name>`")
    return lines


def format_status(data: dict) -> str:
    kill = data.get("kill_switch") if isinstance(data.get("kill_switch"), dict) else {}
    if kill.get("latched") is True:
        ks = (f"LATCHED gen={_ident(kill.get('generation'))} "
              f"since={display_text(kill.get('latched_at'))}")
    else:
        ks = "ok"
    mission = data.get("mission")
    if isinstance(mission, dict):
        last = (f"{display_text(mission.get('loop'))}:{display_text(mission.get('status'))} "
                f"({display_text(mission.get('started_at'))})")
    else:
        last = "なし"
    orders = data.get("orders") if isinstance(data.get("orders"), dict) else {}
    lines = [f"mode: {display_text(data.get('mode'))} / autopilot: "
             f"{'on' if data.get('autopilot') is True else 'off'} / kill switch: {ks}",
             f"残高: {_num(data.get('balance'))} / エクイティ: {_num(data.get('equity'))}",
             f"アクティブ orders: {_num(orders.get('active'))}",
             f"直近 mission: {last}"]
    if kill.get("reconcile_required") is True:
        lines.append("kill switch: 解除の途中で止まった印あり "
                     "(`afx ctl killswitch reconcile` で確認)")
    health = data.get("health") if isinstance(data.get("health"), dict) else {}
    if health.get("latched") is True:
        lines.append(f"health: LATCHED (failures {_num(health.get('failures'))})")
    feed = data.get("data") if isinstance(data.get("data"), dict) else None
    if feed is not None:
        lines.append(f"data: {display_text(feed.get('state'))}")
    trade_events = data.get("trade_events")
    if isinstance(trade_events, dict):
        lines.append("取引 event の欠落: " + " ".join(
            f"{name}={_num(trade_events.get(name))}" for name in DROP_REASONS))
    lines.extend(_deploy_ops_lines(data.get("plugin_deploy_ops")))
    repeat = data.get("improve_repeat")
    if isinstance(repeat, dict):
        summary = _repeat_job_summary(repeat)
        if summary is not None:
            lines.append(summary)
    setup = data.get("setup")
    record_state = setup.get("record_state") if isinstance(setup, dict) else None
    if record_state == "unknown":
        lines.append("未完の手順を確認できません")
    if isinstance(setup, dict) and isinstance(setup.get("pending_steps"), list):
        steps = []
        for item in setup["pending_steps"][:32]:
            if not isinstance(item, dict) or not isinstance(item.get("code"), str):
                continue
            code = item["code"] if is_ident(item["code"]) else "unknown"
            severity = item.get("severity")
            if not isinstance(severity, str) or severity not in _SETUP_SEVERITIES:
                severity = "info"
            raw_params = item.get("params") if isinstance(item.get("params"), dict) else {}
            allowed = STEP_PARAMS.get(code, {})
            params = {key: value for key, value in raw_params.items() if key in allowed}
            observed_at = parse_observed_at(item.get("observed_at")) or datetime.now(timezone.utc)
            try:
                text = render_pending_step(PendingStep(code, severity, observed_at, params))
            except Exception:  # noqa: BLE001 — a rendering fault must not break status
                text = "手順を表示できません。afx を更新してください"
            steps.append(text)
        if record_state == "corrupt" and not any("確認記録が読めません" in step for step in steps):
            lines.append("確認記録が読めません")
        if steps:
            lines.append(f"未完の手順 ({len(steps)} 件)")
            lines.extend(f"- {step}" for step in steps)
    return "\n".join(lines)


def _lines(data: dict) -> str:
    lines = data.get("lines") if isinstance(data.get("lines"), list) else []
    text = "\n".join(display_text(line, _TEXT_LIMIT) for line in lines) or "(なし)"
    if data.get("truncated") is True:
        text += "\n(走査上限で打ち切り)"
    return text


def format_approval_list(data: dict) -> str:
    lines = []
    for row in data.get("approvals") or []:
        if not isinstance(row, dict):
            continue
        content_hash = row.get("content_hash")
        short = content_hash[:8] if isinstance(content_hash, str) and content_hash != "-" else "-"
        lines.append(f"#{_ident(row.get('id'))} kind={display_text(row.get('kind'))} "
                     f"name={display_text(row.get('name'))} "
                     f"created_at={display_text(row.get('created_at'))} "
                     f"content_hash={display_text(short)}")
    if not lines:
        lines.append("承認待ちはありません")
    journals = [j for j in data.get("open_journals") or [] if isinstance(j, dict)]
    if journals:
        lines.append("-- 未終端の切替ジャーナル --")
        lines += [f"name={display_text(j.get('name'))} phase={display_text(j.get('phase'))} "
                  f"approval_id={_ident(j.get('approval_id'))}" for j in journals]
    return "\n".join(lines)


_HASH_HEAD = re.compile(r"[0-9a-f]{64}")


def _safe_cell(value, limit: int) -> str:
    """表示する非信頼の値。redaction → 制御文字の除去 → 表示上限、の順で 1 行にする。"""
    if not isinstance(value, str):
        return "-"
    text = display_text(redactor.redact_value(value), 1 << 20)
    return text if len(text) <= limit else text[:limit] + "…"


def _safe_text(value, limit: int) -> str:
    """既存の表示整形 (`display_text`) に redaction を前置したもの。"""
    return display_text(redactor.redact_value(value) if isinstance(value, str) else value, limit)


def _backlog_display(value) -> str:
    """Collapse already-bounded API guidance to at most 200 display characters."""
    text = _safe_text(value, 1 << 20)
    return text if len(text) <= 200 else text[:199] + "…"


_STALE_ADVICE = (
    ("unreadable", "policy/directives.md を読める状態 (権限、UTF-8) に直してから "
                   "afx ctl policy reconcile を実行してください。"),
    ("malformed", "marker を手修正してから afx ctl policy reconcile を実行してください。"),
)
_STALE_ADVICE_DEFAULT = ("Mission に入る方針が DB と一致していません。"
                         "afx ctl policy reconcile で DB に合わせられます。")


def _hash_head(value) -> str:
    return value[:12] if isinstance(value, str) and _HASH_HEAD.fullmatch(value) else "-"


def _reason_text(code) -> str:
    return change_limits.REASON_TEXT.get(code, change_limits.REASON_TEXT[change_limits.UNSAFE_SOURCE])


INVALID_CHANGE_TEXT = "変更内容を表示できません"


def _change_is_well_formed(change: dict) -> bool:
    """daemon の応答が約束の型か。外れた応答 (版の食い違い・不具合) は固定文に倒す。"""
    reason = change.get("reason")
    if not isinstance(change.get("classification"), str):
        return False
    if reason is not None and not isinstance(reason, str):
        return False
    params = change.get("params")
    if params is None:
        return True
    return isinstance(params, dict) and isinstance(params.get("items", []), list)


def format_change_lines(change) -> list[str]:
    """承認の変更要約 (diff は出さない)。固定文以外の自由文は redactor を通した値だけ。"""
    if not isinstance(change, dict):
        return []
    if not _change_is_well_formed(change):
        return ["-- 変更内容 --", INVALID_CHANGE_TEXT]
    cls = change.get("classification")
    label = change_limits.CLASS_LABELS.get(cls, change_limits.CLASS_LABELS[change_limits.CLASS_UNKNOWN])
    name = change.get("name")
    name = name if isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) else "-"
    kind = change.get("kind") if change.get("kind") in change_limits.PLUGIN_KINDS else "-"
    lines = ["-- 変更内容 --",
             f"plugin: {name} ({kind}) 分類: {label}"
             + (" (前版なし)" if cls == change_limits.CLASS_NEW else ""),
             f"候補: {_hash_head(change.get('candidate_hash'))} "
             f"比較対象: {_hash_head(change.get('baseline_hash'))} "
             f"計算時刻: {_safe_cell(change.get('computed_at'), 40)}"]
    params = change.get("params") if isinstance(change.get("params"), dict) else {}
    items = [i for i in params.get("items") or [] if isinstance(i, dict)]
    if items:
        lines.append("params の差分:")
        for item in items[:change_limits.PARAMS_DIFF_MAX]:
            old, new = item.get("old"), item.get("new")
            lines.append(
                f"  {_safe_cell(item.get('key'), change_limits.PARAM_VALUE_CHARS)}: "
                f"{'(なし)' if old is None else _safe_cell(old, change_limits.PARAM_VALUE_CHARS)}"
                f" → {'(なし)' if new is None else _safe_cell(new, change_limits.PARAM_VALUE_CHARS)}")
    extra = [("ほか", params.get("omitted")), ("形に合わない key", params.get("invalid_keys")),
             ("比較していない key", params.get("flatten_omitted"))]
    for label_, count in extra:
        if type(count) is int and count > 0:
            lines.append(f"  ({label_} {count} 件)")
    if params.get("reason") == change_limits.REDACTED_CODE:
        lines.append(_reason_text(change_limits.REDACTED_CODE))
    reason = change.get("reason")
    if reason is not None:
        lines.append(_reason_text(reason))
    return lines


def format_approval_detail(data: dict) -> str:
    # in_sample / holdout の行は対話シェルの承認詳細と同じ renderer を使う。
    from agentic_fx.commands import Commands
    lines = [f"approval #{_ident(data.get('id'))} kind={display_text(data.get('kind'))} "
             f"status={display_text(data.get('status'))}"]
    lines += Commands._metrics_lines("in_sample", data.get("in_sample"))
    lines += Commands._metrics_lines("holdout", data.get("holdout"))
    lines += Commands._baseline_comparison_lines(data.get("baseline"))
    lines += format_change_lines(data.get("change"))
    facts = data.get("facts_lines")
    if isinstance(facts, list):
        lines += [_safe_text(line, approval_facts.CLAIM_DISPLAY_LIMIT) for line in facts]
    lines.append(
        f"reason={_safe_text(data.get('reason'), approval_facts.CLAIM_DISPLAY_LIMIT)} "
        f"decided_by={display_text(data.get('decided_by'))} "
        f"decided_at={display_text(data.get('decided_at'))}")
    lines.append(f"payload_sha256={display_text(data.get('payload_sha256'))}")
    return "\n".join(lines)


def format_event(event: dict) -> str:
    fields = event.get("fields") if isinstance(event.get("fields"), dict) else {}
    cells = " ".join(f"{display_text(str(k))}={display_text(str(v))}"
                     for k, v in sorted(fields.items()))
    return (f"#{display_text(str(event.get('id')))} {display_text(event.get('ts'))} "
            f"{display_text(event.get('code'))} {display_text(event.get('ref'))} {cells}").rstrip()


def _multiline(text) -> str:
    """ask の回答のような複数行の本文。行ごとに制御文字を除いて表示する。"""
    if not isinstance(text, str):
        return "-"
    return "\n".join(display_text(line, _TEXT_LIMIT) if line.strip() else ""
                     for line in text.splitlines())


# --------------------------------------------------------------- 実行

def _int_arg(text: str) -> int:
    if not (text.isascii() and text.isdigit()) or len(text) > 18:
        raise ClientError(RC_USAGE, _usage())
    return int(text)


def _count_arg(text: str, upper: int) -> int:
    """件数の引数。範囲外は丸めず usage にする (server へ届く値と画面の指定を一致させる)。"""
    value = _int_arg(text)
    if not 1 <= value <= upper:
        raise ClientError(RC_USAGE, _usage())
    return value


def _events_cursor_path(root: Path) -> Path:
    return (Path.home() / ".local" / "state" / "agentic-fx" / keys.instance_id(root)
            / "events.cursor")


def _load_cursor_state(path: Path) -> tuple[int, int]:
    """保存した (位置, 前回見た high watermark)。位置だけの旧形式は watermark 0 と読む。"""
    try:
        parts = path.read_text(encoding="ascii").split()
    except (OSError, UnicodeDecodeError):
        return 0, 0
    if not parts or not all(p.isdigit() for p in parts[:2]):
        return 0, 0
    position = int(parts[0])
    return position, int(parts[1]) if len(parts) > 1 else 0


def _load_cursor(path: Path) -> int:
    return _load_cursor_state(path)[0]


def _save_cursor(path: Path, position: int, high_watermark: int = 0) -> None:
    # high watermark も残し、次の起動で DB の巻き戻りを検知できるようにする。
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(f"{position} {max(high_watermark, 0)}\n", encoding="ascii")
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


_APPROVER_OPS = frozenset({"approve", "reject", "retry", "killswitch_reset",
                           "killswitch_reconcile", "data_resume"})
_CONFIRM_OPS = _APPROVER_OPS | {"job_cancel"}
# approve job が done で返す outcome のうち、承認されて版が切り替わったもの。
_APPROVING_OUTCOMES = frozenset({"deployed", "deployed_after_rollback"})


@dataclass
class Op:
    name: str
    args: list
    via_alias: str | None = None


def parse(tokens: list[str]) -> Op:
    """対話シェルと同じ語順の操作を読む。読めなければ usage (rc=2)。"""
    if not tokens:
        raise ClientError(RC_USAGE, _usage())
    cmd, args = tokens[0], tokens[1:]
    n = len(args)
    if cmd == "help" and n == 0:
        return Op("help", [])
    if cmd == "stop":
        return Op("stop", [])
    if cmd in ("status", "whoami") and n == 0:
        return Op(cmd, [])
    if cmd == "log" and n <= 1:
        return Op("log", [_count_arg(args[0], 500) if args else 20])
    if cmd == "activity" and n <= 2:
        category = args[1].upper() if n == 2 else None
        if category is not None and category not in ACTIVITY_CATEGORIES:
            raise ClientError(RC_USAGE, _usage())
        return Op("activity", [_count_arg(args[0], 500) if args else 20, category])
    if cmd == "events":
        return Op("events", _parse_events(args))
    if cmd in ("approval", "approvals"):
        if n in (1, 2) and args[0] == "list":
            return Op("approval_list", [_count_arg(args[1], 200) if n == 2 else 20])
        if n == 2 and args[0] == "show":
            return Op("approval_detail", [_int_arg(args[1])])
        if cmd == "approval" and n == 2 and args[0] == "retry":
            return Op("retry", [_int_arg(args[1])])
        if cmd == "approval" and n == 1:
            return Op("approval_detail", [_int_arg(args[0])])
    if cmd == "approve" and n == 1:
        return Op("approve", [_int_arg(args[0])])
    if cmd == "retry" and n == 1:
        return Op("retry", [_int_arg(args[0])])
    if cmd == "reject" and n >= 1:
        return Op("reject", [_int_arg(args[0]), " ".join(args[1:]) or None])
    if cmd == "ask" and n >= 1:
        return Op("ask", [" ".join(args)])
    if cmd == "improve" and n == 0:
        return Op("improve", [])
    if cmd == "improve" and n >= 2 and args[0] == "add":
        if any(_is_improve_flag(token) for token in args[1:]):
            # 旧 `improve add` と繰り返しの option は併用できない。
            raise ClientError(RC_USAGE, _usage())
        return Op("backlog_add", [" ".join(args[1:])], via_alias="improve add")
    if cmd == "improve" and n >= 1 and args[0] != "add":
        return Op("improve", _parse_improve(args))
    if cmd == "backlog" and n >= 2 and args[0] == "add":
        return Op("backlog_add", [" ".join(args[1:])])
    if cmd == "backlog" and n == 2 and args[0] in ("reject", "reopen", "note"):
        return Op("backlog_transition", [args[0], _int_arg(args[1])])
    if cmd == "backlog" and args and args[0] == "list":
        return Op("backlog_list", _parse_backlog_list(args[1:]))
    if cmd == "policy" and n >= 2 and args[0] == "add":
        return Op("policy", [" ".join(args[1:])])
    if cmd == "policy" and args == ["show"]:
        return Op("policy_show", [])
    if cmd == "policy" and args == ["reconcile"]:
        return Op("policy_reconcile", [])
    if cmd == "policy" and n == 2 and args[0] == "remove":
        if re.fullmatch(r"[1-9][0-9]*", args[1]) is None:
            raise ClientError(RC_USAGE, _usage())
        return Op("policy_remove", [int(args[1])])
    if cmd == "reflect" and n == 2 and args[0] == "retry":
        return Op("reflection_retry", [_int_arg(args[1])])
    if cmd == "killswitch" and n >= 1 and args[0] == "reset":
        if n == 1:
            return Op("killswitch_show", [])
        if n == 2:
            return Op("killswitch_reset", [_int_arg(args[1])])
    if cmd == "killswitch" and args in (["reconcile"], ["reconcile", "confirm"]):
        return Op("killswitch_reconcile", [])
    if cmd == "data" and args in (["resume"], ["resume", "--acknowledge"]):
        return Op("data_resume", [n == 2])
    if cmd == "jobs" and n == 2 and args[0] in ("get", "cancel"):
        if _JOB_ID.fullmatch(args[1]) is None:
            raise ClientError(RC_USAGE, _usage())
        return Op("job_get" if args[0] == "get" else "job_cancel", [args[1]])
    raise ClientError(RC_USAGE, _usage())


_IMPROVE_FLAGS = {"-t": "duration", "--duration": "duration",
                  "-c": "comment", "--comment": "comment",
                  "-n": "max_runs", "--max-runs": "max_runs"}


def _is_improve_flag(token: str) -> bool:
    return token.split("=", 1)[0] in _IMPROVE_FLAGS if token.startswith("--") \
        else token in _IMPROVE_FLAGS


def _parse_improve(args: list[str]) -> list:
    """`improve` の option を読み、[duration_sec, max_runs, comment] を返す。

    値は次の token を無条件に取る (`-c -n` の `-n` は値)。長形式は `--x=v` も受ける。
    違反はすべて usage (鍵を読む前・接続前)。位置引数は 1 つでもあれば違反。"""
    found: dict[str, str] = {}
    rest = list(args)
    while rest:
        flag = rest.pop(0)
        value = None
        if flag.startswith("--") and "=" in flag:
            flag, value = flag.split("=", 1)
        name = _IMPROVE_FLAGS.get(flag)
        if name is None or name in found:
            raise ClientError(RC_USAGE, _usage())
        if value is None:
            if not rest:
                raise ClientError(RC_USAGE, _usage())
            value = rest.pop(0)
        found[name] = value
    try:
        duration = ir.parse_duration(found["duration"]) if "duration" in found else None
        max_runs = None
        if "max_runs" in found:
            if re.fullmatch(r"[0-9]+", found["max_runs"]) is None:
                raise ValueError("invalid max runs")
            max_runs = ir.validate_max_runs(int(found["max_runs"]))
        comment = ir.normalize_comment(found["comment"]) if "comment" in found else None
    except ValueError:
        raise ClientError(RC_USAGE, _usage()) from None
    return [duration, max_runs, comment]


def _parse_events(args: list[str]) -> list:
    after, limit, follow = None, 100, False
    rest = list(args)
    while rest:
        flag = rest.pop(0)
        if flag == "--follow":
            follow = True
        elif flag in ("--after", "--limit") and rest:
            value = _int_arg(rest.pop(0))
            if flag == "--after":
                after = value
            else:
                limit = value
        else:
            raise ClientError(RC_USAGE, _usage())
    if not 1 <= limit <= 500:
        raise ClientError(RC_USAGE, _usage())
    return [after, limit, follow]


def _parse_backlog_list(args: list[str]) -> list:
    all_statuses, limit, seen_limit = False, 100, False
    rest = list(args)
    while rest:
        flag = rest.pop(0)
        if flag == "--all" and not all_statuses:
            all_statuses = True
        elif flag == "--limit" and rest and not seen_limit:
            seen_limit = True
            limit = _int_arg(rest.pop(0))
        else:
            raise ClientError(RC_USAGE, _usage())
    if not 1 <= limit <= 500:
        raise ClientError(RC_USAGE, _usage())
    return [all_statuses, limit]


class Runner:
    def __init__(self, api: ApiClient, *, stdin: TextIO, stdout: TextIO,
                 stderr: TextIO | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.api = api
        self.stdin, self.stdout = stdin, stdout
        self.stderr = stderr if stderr is not None else sys.stderr
        self.sleep, self.monotonic = sleep, monotonic

    def say(self, text: str) -> None:
        print(text, file=self.stdout, flush=True)

    def warn(self, text: str) -> None:
        print(text, file=self.stderr, flush=True)

    def run(self, op: Op) -> int:
        if op.via_alias is not None:
            from agentic_fx.cli_surface import ALIASES
            by_alias = {item.parser_alias: item for item in ALIASES}
            alias = by_alias.get(op.via_alias)
            old = f"afx ctl {op.via_alias}"
            if alias is None:
                self.warn(f"`{old}` は旧名です。`afx ctl help` で正式名を確かめてください。")
            else:
                canonical = alias.canonical_usage.split(" <", 1)[0]
                self.warn(f"`{old}` は旧名です。`{canonical}` を使ってください。")
        if op.name == "help":
            self.say(_usage())
            return RC_OK
        if op.name == "stop":
            raise ClientError(RC_USAGE, STOP_GUIDANCE)
        if op.name in _APPROVER_OPS and self.api.principal is not Principal.APPROVER:
            raise ClientError(RC_UNREACHABLE, MESSAGES["need_approver"])
        terminal = None
        if op.name in _CONFIRM_OPS:
            # 確認が要る操作は、端末を確かめる前に何も送らない。
            terminal = Terminal.open(self.stdin)
            if terminal is None:
                raise ClientError(RC_USAGE, MESSAGES["need_terminal"])
        try:
            return getattr(self, f"_op_{op.name}")(*op.args, terminal=terminal)
        finally:
            if terminal is not None:
                terminal.close()

    def _get(self, path: str, **params) -> dict:
        reply = self.api.request("GET", path, params=params or None)
        if reply.data is None:
            raise _fail(reply)
        return reply.data

    def _post(self, path: str, body: dict | None = None, *, idempotent: bool = False) -> Reply:
        return self.api.request("POST", path, body=body or {}, idempotent=idempotent)

    # ---- 読取
    def _op_status(self, *, terminal) -> int:
        self.say(format_status(self._get("/v1/status")))
        return RC_OK

    def _op_whoami(self, *, terminal) -> int:
        data = self._get("/v1/whoami")
        scopes = data.get("scopes") if isinstance(data.get("scopes"), list) else []
        self.say(f"principal: {display_text(data.get('authenticated_principal'))}\n"
                 f"scopes: {' '.join(display_text(s) for s in scopes)}")
        return RC_OK

    def _op_log(self, n: int, *, terminal) -> int:
        self.say(_lines(self._get("/v1/log", n=n)))
        return RC_OK

    def _op_activity(self, n: int, category: str | None, *, terminal) -> int:
        params = {"n": n}
        if category is not None:
            params["category"] = category
        self.say(_lines(self._get("/v1/activity", **params)))
        return RC_OK

    def _op_approval_list(self, limit: int, *, terminal) -> int:
        self.say(format_approval_list(self._get("/v1/approvals", status="pending",
                                                limit=limit)))
        return RC_OK

    def _op_approval_detail(self, approval_id: int, *, terminal) -> int:
        self.say(format_approval_detail(self._get(f"/v1/approvals/{approval_id}")))
        return RC_OK

    def _op_job_get(self, job_id: str, *, terminal) -> int:
        self.say(self._job_line(self._get(f"/v1/jobs/{job_id}")))
        return RC_OK

    def _op_events(self, after: int | None, limit: int, follow: bool, *, terminal) -> int:
        path = _events_cursor_path(self.api.root)
        # --after の明示は過去を覗くだけの閲覧で、保存した cursor を動かさない。
        persist = after is None
        if persist:
            position, high_watermark = _load_cursor_state(path)
            cursor = EventCursor(position=position, high_watermark=high_watermark)
        else:
            cursor = EventCursor(position=after)
        saved = (cursor.position, cursor.high_watermark)
        while True:
            data = self._get("/v1/events", after=cursor.position, limit=limit)
            events = [e for e in data.get("events") or [] if isinstance(e, dict)
                      and type(e.get("id")) is int]
            high = data.get("high_watermark")
            fresh = cursor.consume(EventPage(events, high if type(high) is int else -1))
            if cursor.health_error:
                hint = (f"。復旧するには cursor file ({display_text(str(path), _TEXT_LIMIT)}) "
                        "を削除してください" if persist else "")
                raise ClientError(RC_FAILED, "event の high watermark が後退しました。"
                                  f"cursor は進めていません{hint}")
            for event in fresh:
                self.say(format_event(event))
            # 表示してから cursor を保存する (切断後は同じ位置から再開する)。
            # 位置も watermark も変わらない周回では書かない (follow の常駐中の無駄な I/O)。
            current = (cursor.position, cursor.high_watermark)
            if persist and current != saved:
                try:
                    _save_cursor(path, *current)
                except OSError:
                    # 表示は済んでいる。次の起動では同じ event を再表示し得ることを伝えて止める。
                    raise ClientError(RC_FAILED, "event は表示しましたが cursor を保存できません"
                                      f"でした ({display_text(str(path))})。次回は同じ event を"
                                      "再表示することがあります") from None
                saved = current
            if not follow:
                if not fresh:
                    self.say("(新しい event はありません)")
                return RC_OK
            if len(events) < limit:
                self.sleep(2.0)

    # ---- 日常操作
    def _op_ask(self, question: str, *, terminal) -> int:
        reply = self._post("/v1/asks", {"question": question}, idempotent=True)
        job = self._accepted(reply, "ask")
        if job is None:
            return RC_FAILED
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        self.say(_multiline(result.get("answer")))
        return RC_OK

    def _op_improve(self, duration_sec: int | None = None, max_runs: int | None = None,
                    comment: str | None = None, *, terminal) -> int:
        if duration_sec is not None or max_runs is not None:
            return self._improve_repeat(duration_sec, max_runs, comment)
        reply = self._post("/v1/improve/runs",
                           {"comment": comment} if comment is not None else None,
                           idempotent=True)
        job = self._accepted(reply, "improve")
        if job is None:
            return RC_FAILED
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        self.say(f"improve mission #{_ident(result.get('mission_id'))} を起動しました")
        return RC_OK

    def _improve_repeat(self, duration_sec: int | None, max_runs: int | None,
                        comment: str | None) -> int:
        """繰り返し job は受理で返る。受理後の待ちは表示のためだけで、rc には効かない。"""
        body: dict = {}
        if duration_sec is not None:
            body["duration_sec"] = duration_sec
        if max_runs is not None:
            body["max_runs"] = max_runs
        if comment is not None:
            body["comment"] = comment
        reply = self._post("/v1/improve/runs", body, idempotent=True)
        if reply.data is None:
            raise _fail(reply)
        job_id = reply.data.get("job_id")
        if not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None:
            raise ClientError(RC_FAILED, "daemon の応答を解釈できません (job id)")
        state = reply.data.get("state")
        self.say(f"job {job_id} を受理しました")
        span = _span_text(duration_sec if duration_sec is not None else ir.MAX_DURATION_SEC)
        give_up = self.monotonic() + _POLL_LIMITS["improve"]
        header = launched = False
        try:
            while True:
                job = self._get(f"/v1/jobs/{job_id}")
                state = job.get("state")
                result = job.get("result") if isinstance(job.get("result"), dict) else {}
                if not header:
                    header = True
                    shown_runs = result.get("max_runs")
                    self.say(
                        "improve を繰り返します: "
                        f"期限 {_format_deadline(result.get('deadline_at'))} ({span})、"
                        f"最大 {shown_runs if type(shown_runs) is int else (max_runs or ir.MAX_RUNS)} 回"
                        + ("、一時指示あり" if comment is not None else ""))
                runs, mission_id = result.get("runs"), result.get("mission_id")
                # 1 回目が始まったかは回数で決める。mission_id は表示にだけ使う
                # (mission が作られる前に失敗した回も 1 回と数える)。
                started = type(runs) is int and runs >= 1
                if started and not launched:
                    launched = True
                    self.say(f"1 回目 mission #{mission_id} を起動しました"
                             if type(mission_id) is int else "1 回目を起動しました")
                if state in _TERMINAL_JOB_STATES:
                    self.say(_improve_end_text(job_id, job))
                    return RC_OK
                if started:
                    self._improve_footer(job_id)
                    return RC_OK
                if self.monotonic() >= give_up:
                    self.say("週次の slot が空くのを待っています。1 回目は空き次第始まります")
                    self._improve_footer(job_id)
                    return RC_OK
                self.sleep(_POLL_INTERVAL)
        except KeyboardInterrupt:
            self.say(interrupt_notice(job_id, state))
            raise
        except ClientError:
            self.say(interrupt_notice(job_id, state, lost=True))
            raise

    def _improve_footer(self, job_id: str) -> None:
        self.say("各 mission の開始・終了は通知に出ます。止める: "
                 f"afx ctl jobs cancel {job_id} (実行中の mission は完走し、次を始めません)\n"
                 "daemon を再起動すると繰り返しは中断します。")

    def _op_backlog_add(self, idea: str, *, terminal) -> int:
        from agentic_fx.core.idea_check import normalize_idea_display
        reply = self._post("/v1/backlog", {"idea": idea}, idempotent=True)
        if reply.data is None:
            raise _fail(reply)
        bid = _ident(reply.data.get("id"))
        display, removed = normalize_idea_display(idea)
        text = f"backlog #{bid} を追加しました: 「{display_text(display, _TEXT_LIMIT)}」"
        if removed:
            text += f"\n表示できない文字を {removed} 個含みます"
        self.say(text)
        return RC_OK

    def _op_backlog_list(self, all_statuses: bool, limit: int, *, terminal) -> int:
        data = self._get("/v1/backlog", all="true" if all_statuses else "false", limit=limit)
        rows = data.get("items") if isinstance(data.get("items"), list) else []
        if not rows:
            self.say("backlog: 0 件")
            return RC_OK
        for row in rows:
            if not isinstance(row, dict):
                continue
            self.say(f"#{_ident(row.get('id'))} {_safe_cell(row.get('status'), 200)} "
                     f"{_safe_cell(row.get('source'), 200)} {_safe_cell(row.get('updated_at'), 200)} "
                     f"{_backlog_display(row.get('idea'))}")
            if row.get("last_result") not in (None, ""):
                self.say("  " + _backlog_display(row.get("last_result")))
        if data.get("has_more") is True:
            self.say("… 他にもあります (--limit N で増やすか --all)")
        return RC_OK

    def _op_backlog_transition(self, action: str, bid: int, *, terminal) -> int:
        reply = self._post(f"/v1/backlog/{bid}/{action}")
        if reply.data is None:
            if reply.code == "not_found":
                raise ClientError(RC_FAILED, f"backlog #{bid} は存在しません")
            if reply.code == "invalid_state":
                allowed = "done|rejected|note" if action == "reopen" else "open|observation"
                raise ClientError(RC_FAILED, f"backlog #{bid} は現在の status のため {action} "
                                  f"できません ({allowed} からのみ可)")
            raise _fail(reply)
        if action == "reopen":
            self.say(f"backlog #{bid} を open に戻しました")
        else:
            self.say(f"backlog #{bid} を {'rejected' if action == 'reject' else 'note'} にしました")
        return RC_OK

    def _op_policy(self, text: str, *, terminal) -> int:
        reply = self._post("/v1/policy", {"text": text}, idempotent=True)
        if reply.data is None:
            raise _fail(reply)
        self.say("policy に追記しました")
        return RC_OK

    def _op_policy_show(self, *, terminal) -> int:
        data = self._get("/v1/policy")
        managed = [row for row in data.get("managed") or [] if isinstance(row, dict)]
        unmanaged = [row for row in data.get("unmanaged") or [] if isinstance(row, dict)]
        self.say("管理対象:")
        if managed:
            for row in managed:
                self.say(f"  #{_ident(row.get('id'))} {_safe_cell(row.get('created_at'), 200)} "
                         f"{_backlog_display(row.get('directive'))}")
        else:
            self.say("  0 件")
        self.say("管理外 (API から削除できません):")
        if unmanaged:
            for row in unmanaged:
                self.say(f"  line {_ident(row.get('line'))} {_backlog_display(row.get('directive'))}")
        else:
            self.say("  0 件")
        projection = data.get("projection") if isinstance(data.get("projection"), dict) else {}
        status = projection.get("status")
        reasons = projection.get("reasons")
        if status == "ok":
            self.say("投影: ok")
        elif not isinstance(reasons, list) or not any(isinstance(r, str) for r in reasons):
            self.say("投影: stale (unknown)。" + _STALE_ADVICE_DEFAULT)
        else:
            reason_text = ", ".join(_safe_cell(reason, 80) for reason in reasons
                                    if isinstance(reason, str))
            advice = next((text for key, text in _STALE_ADVICE if key in reasons),
                          _STALE_ADVICE_DEFAULT)
            self.say("投影: stale (" + reason_text + ")。" + advice)
        return RC_OK

    def _op_policy_remove(self, identifier: int, *, terminal) -> int:
        reply = self._post(f"/v1/policy/{identifier}/remove", idempotent=True)
        if reply.data is None:
            if reply.code == "not_found":
                raise ClientError(RC_FAILED, f"policy #{identifier} は存在しません")
            raise _fail(reply)
        self.say(f"policy remove: #{identifier}")
        return RC_OK

    def _op_policy_reconcile(self, *, terminal) -> int:
        reply = self._post("/v1/policy/reconcile", idempotent=True)
        if reply.data is None:
            raise _fail(reply)
        self.say("policy reconcile: " + ("changed" if reply.data.get("changed") is True else "unchanged"))
        return RC_OK

    def _op_reflection_retry(self, order_id: int, *, terminal) -> int:
        reply = self.api.request("GET", f"/v1/reflections/{order_id}")
        if reply.data is None:
            if reply.code == "not_found":
                raise ClientError(RC_FAILED, f"order #{order_id} は存在しません")
            raise _fail(reply)
        seen = reply.data
        if seen.get("status") != "closed":
            raise ClientError(RC_FAILED, f"order #{order_id} は closed ではありません "
                              f"(status={display_text(seen.get('status'))})")
        if seen.get("reflected") is True:
            raise ClientError(RC_FAILED, f"order #{order_id} は既に reflection 済みです")
        posted = self._post(f"/v1/reflections/{order_id}/retry",
                            {"expected_attempts": seen.get("attempts"),
                             "expected_last_attempt_at": seen.get("last_attempt_at")},
                            idempotent=True)
        if posted.data is None:
            raise _fail(posted)
        suffix = "" if seen.get("attempts") else " (台帳に試行記録なし)"
        self.say(f"order #{order_id} を reflection 再試行対象へ戻しました{suffix}")
        return RC_OK

    def _op_killswitch_show(self, *, terminal) -> int:
        kill = _kill_switch(self._get("/v1/status"))
        if kill.get("latched") is not True:
            self.say("kill switch はラッチされていません")
            return RC_OK
        generation = _ident(kill.get("generation"))
        self.say(f"kill switch は LATCHED です (gen={generation} "
                 f"since={display_text(kill.get('latched_at'))})。解除するには "
                 f"`afx ctl --as approver killswitch reset {generation}` を実行してください")
        return RC_OK

    # ---- 確認の要る操作
    def _op_approve(self, approval_id: int, *, terminal: Terminal) -> int:
        detail = self._get(f"/v1/approvals/{approval_id}")
        self.say(format_approval_detail(detail))
        digest = detail.get("payload_sha256")
        answer = terminal.ask(f"承認するには approval id ({approval_id}) をもう一度入力して"
                              "ください: ")
        if answer != str(approval_id):
            raise ClientError(RC_FAILED, MESSAGES["cancelled"])
        # 表示した payload の digest を送る。表示後に変わっていれば server が拒否する。
        return self._decide("approve", approval_id, {"payload_sha256": digest})

    def _op_retry(self, approval_id: int, *, terminal: Terminal) -> int:
        detail = self._get(f"/v1/approvals/{approval_id}")
        self.say(format_approval_detail(detail))
        if not terminal.yes(f"approval #{approval_id} の承認手順を再試行しますか"):
            raise ClientError(RC_FAILED, MESSAGES["cancelled"])
        return self._decide("retry", approval_id,
                            {"payload_sha256": detail.get("payload_sha256")})

    def _op_reject(self, approval_id: int, reason: str | None, *, terminal: Terminal) -> int:
        # 却下は取り戻しにくい。id の打ち間違いに気付けるよう、先に対象を見せる。
        self.say(format_approval_detail(self._get(f"/v1/approvals/{approval_id}")))
        if not terminal.yes(f"approval #{approval_id} を却下しますか"):
            raise ClientError(RC_FAILED, MESSAGES["cancelled"])
        body = {} if reason is None else {"reason": reason}
        return self._decide("reject", approval_id, body)

    def _decide(self, kind: str, approval_id: int, body: dict) -> int:
        reply = self._post(f"/v1/approvals/{approval_id}/{kind}", body)
        if reply.data is None:
            if reply.code == "not_found":
                raise ClientError(RC_FAILED, f"approval #{approval_id} は存在しません")
            raise _fail(reply)
        job = self._poll(reply.data, kind)
        if job is None:
            return RC_FAILED
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        outcome = display_text(result.get("outcome"))
        status = display_text(result.get("status"))
        if job.get("state") != "done":
            code = job.get("result_code")
            if kind == "retry" and result:
                raise ClientError(RC_FAILED, f"approval #{approval_id} の再試行は完了しませんでした "
                                  f"(status={status} outcome={outcome})")
            if kind == "approve" and result:
                raise ClientError(RC_FAILED, f"approval #{approval_id} は今回の操作では承認され"
                                  f"ませんでした (status={status} outcome={outcome})")
            raise ClientError(RC_FAILED, error_text(code if isinstance(code, str) else None))
        if kind == "reject":
            self.say(f"approval #{approval_id} rejected")
        elif kind == "retry":
            self.say(f"approval #{approval_id} を再試行しました (status={status} outcome={outcome})")
        elif result.get("outcome") in _APPROVING_OUTCOMES:
            self.say(f"approval #{approval_id} approved (outcome={outcome})")
        else:
            # done でも承認に至らない終端 (invalidated など) を「approved」と言わない。
            self.say(f"approval #{approval_id} の処理が完了しました (outcome={outcome})")
        return RC_OK

    def _op_killswitch_reset(self, generation: int, *, terminal: Terminal) -> int:
        data = self._get("/v1/status")
        kill = _kill_switch(data)
        self.say(f"kill switch: {'LATCHED' if kill.get('latched') is True else '解除済み'} "
                 f"gen={_ident(kill.get('generation'))} "
                 f"発動時刻={display_text(kill.get('latched_at'))} "
                 f"エクイティ={_num(data.get('equity'))}")
        if kill.get("reconcile_required") is True:
            self.say("解除の途中で止まった印があります。先に `afx ctl --as approver killswitch "
                     "reconcile` でラッチ中として確定してください")
        if kill.get("latched") is not True:
            raise ClientError(RC_FAILED, "kill switch はラッチされていません")
        answer = terminal.ask(f"世代 {generation} のラッチを解除するには reset と入力して"
                              "ください: ")
        if answer != "reset":
            raise ClientError(RC_FAILED, MESSAGES["cancelled"])
        reply = self._post("/v1/killswitch/reset", {"expected_generation": generation})
        if reply.data is None:
            if reply.code == "invalid_state":
                raise ClientError(RC_FAILED, "kill switch の状態が不確定です (解除の途中で止まった"
                                  "印が残っています)。`afx ctl --as approver killswitch "
                                  "reconcile` で確認してください")
            raise _fail(reply)
        self.say(f"kill switch ラッチを解除しました (gen={_ident(reply.data.get('generation'))})")
        return RC_OK

    def _op_killswitch_reconcile(self, *, terminal: Terminal) -> int:
        kill = _kill_switch(self._get("/v1/status"))
        if kill.get("reconcile_required") is not True:
            self.say("確認が必要な状態はありません (解除の途中で止まった印は無し)")
            return RC_OK
        file_line = "LATCHED" if kill.get("file_latched") is True else "解除済み"
        self.say("解除の途中で止まった印があります。新規の取引は止めています。\n"
                 f"状態ファイルの値: {file_line}\n"
                 f"印の内容: requested_generation="
                 f"{_ident(kill.get('requested_generation'))} "
                 f"started_at={display_text(kill.get('started_at'))}\n"
                 "ラッチ中として確定し、印を消します (解除側には倒しません)。")
        if not terminal.yes("ラッチ中として確定しますか"):
            raise ClientError(RC_FAILED, MESSAGES["cancelled"])
        reply = self._post("/v1/killswitch/reconcile")
        if reply.data is None:
            raise _fail(reply)
        generation = _ident(reply.data.get("generation"))
        self.say(f"kill switch をラッチ中として確定しました (gen={generation})。"
                 "自動では解除しません。解除するには別に "
                 f"`afx ctl --as approver killswitch reset {generation}` を実行してください")
        return RC_OK

    def _op_data_resume(self, acknowledge: bool, *, terminal: Terminal) -> int:
        if not terminal.yes("価格源の手動復帰を要求しますか"
                            + (" (--acknowledge)" if acknowledge else "")):
            raise ClientError(RC_FAILED, MESSAGES["cancelled"])
        reply = self._post("/v1/data/resume", {"acknowledge": acknowledge})
        if reply.data is None:
            raise _fail(reply)
        unprocessed = reply.data.get("unprocessed_positions")
        self.say("resume 要求を記録しました。次の tick で判定します。"
                 + (f"未処理の足がある建玉 {_num(unprocessed)} 件" if unprocessed is not None
                    else "")
                 + (" (--acknowledge 指定あり)" if acknowledge else ""))
        return RC_OK

    def _op_job_cancel(self, job_id: str, *, terminal: Terminal) -> int:
        if not terminal.yes(f"job {job_id} を取り消しますか"):
            raise ClientError(RC_FAILED, MESSAGES["cancelled"])
        reply = self._post(f"/v1/jobs/{job_id}/cancel")
        if reply.data is None:
            raise _fail(reply)
        if reply.data.get("state") == "running" and reply.data.get("stop_requested") is True:
            self.say(f"job {job_id}: 停止を受け付けました。"
                     "実行中の mission の終了後に止まります")
            return RC_OK
        self.say(f"job {job_id}: {display_text(reply.data.get('state'))}")
        return RC_OK

    # ---- job
    @staticmethod
    def _job_line(job: dict) -> str:
        repeat = _repeat_job_summary(job)
        if repeat is not None:
            return f"job {display_text(job.get('id'))}: {repeat}"
        result = job.get("result")
        text = (f"job {display_text(job.get('id'))} kind={display_text(job.get('kind'))} "
                f"state={display_text(job.get('state'))}")
        if job.get("result_code"):
            text += f" result_code={display_text(job.get('result_code'))}"
        if isinstance(result, dict):
            cells = " ".join(f"{display_text(str(k))}={display_text(str(v))}"
                             for k, v in sorted(result.items()) if k != "answer")
            if cells:
                text += f" {cells}"
            if "answer" in result:
                text += "\n" + _multiline(result.get("answer"))
        return text

    def _accepted(self, reply: Reply, kind: str) -> dict | None:
        if reply.data is None:
            raise _fail(reply)
        job = self._poll(reply.data, kind)
        if job is not None and job.get("state") != "done":
            code = job.get("result_code")
            raise ClientError(RC_FAILED, error_text(code if isinstance(code, str) else None))
        return job

    def _poll(self, accepted: dict, kind: str) -> dict | None:
        job_id = accepted.get("job_id")
        if not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None:
            raise ClientError(RC_FAILED, "daemon の応答を解釈できません (job id)")
        state = accepted.get("state")
        self.say(f"job {job_id} を受理しました")
        deadline = self.monotonic() + _POLL_LIMITS.get(kind, 45.0)
        try:
            while True:
                job = self._get(f"/v1/jobs/{job_id}")
                state = job.get("state")
                if state in ("done", "failed", "cancelled", "shutdown"):
                    return job
                if self.monotonic() >= deadline:
                    self.say(interrupt_notice(job_id, state, timed_out=True))
                    return None
                self.sleep(_POLL_INTERVAL)
        except KeyboardInterrupt:
            self.say(interrupt_notice(job_id, state))
            raise
        except ClientError:
            # 受理の行は流れ去っているかもしれない。結果を確かめる id をここで示す。
            self.say(interrupt_notice(job_id, state, lost=True))
            raise


_TERMINAL_JOB_STATES = ("done", "failed", "cancelled", "shutdown")

_STOP_REASON_TEXT = {
    "deadline": "期限に達したため終了しました",
    "max_runs": "最大回数に達したため終了しました",
    "stopped": "停止の要求により終了しました",
    "consecutive_failures": "mission が連続して失敗したため終了しました",
    "resource_exhausted": "利用枠などの上限で mission を続けられないため終了しました",
    "autopilot": "autopilot 中のため終了しました",
    "shutdown": "daemon の停止により終了しました",
}


def _repeat_job_summary(job: dict) -> str | None:
    result = job.get("result")
    if job.get("kind") != "improve" or not isinstance(result, dict):
        return None
    runs, maximum = result.get("runs"), result.get("max_runs")
    elapsed, duration = result.get("elapsed_sec"), result.get("duration_sec")
    if (type(runs) is not int or type(maximum) is not int or type(elapsed) is not int
            or type(duration) is not int or not 0 <= runs <= maximum
            or elapsed < 0 or duration < 0):
        return None
    state = {"running": "実行中", "done": "終了", "failed": "失敗",
             "shutdown": "停止"}.get(job.get("state"), "状態不明")
    elapsed_minutes, duration_minutes = elapsed // 60, duration // 60
    terminal = job.get("state") in _TERMINAL_JOB_STATES
    text = (f"improve の繰り返し: {state}、実施 {runs} 回 / 最大 {maximum} 回、"
            f"経過 {_repeat_span(elapsed_minutes)} / {_repeat_span(duration_minutes)}")
    if not terminal:
        text += f"、残り {_repeat_span(max(duration_minutes - elapsed_minutes, 0))}"
    mission_id = result.get("mission_id")
    if type(mission_id) is int and mission_id >= 0:
        text += f"、{'最後の' if terminal else '実行中'} mission #{mission_id}"
    if result.get("stop_requested") is True:
        text += "、停止要求あり"
    reason = result.get("stop_reason")
    if job.get("state") in _TERMINAL_JOB_STATES and isinstance(reason, str):
        text += f"、{_STOP_REASON_TEXT.get(reason, display_text(reason))}"
    return text


def _span_text(seconds: int) -> str:
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours}h" * (hours > 0) + f"{minutes}m" * (minutes > 0)


def _repeat_span(minutes: int) -> str:
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours > 0 else f"{minutes}m"


def _format_deadline(value) -> str:
    """wire の ISO-8601 UTC を人向けの表記にする。読めなければ無害化して返す。"""
    if isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            return display_text(value)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return display_text(value)


def _improve_end_text(job_id: str, job: dict) -> str:
    """受理後に終端した繰り返し job の理由。rc は 0 のまま (job を作った後なので)。"""
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    state = job.get("state")
    runs = result.get("runs")
    done_runs = f" (実施 {runs} 回)" if type(runs) is int else ""
    if state == "done":
        reason = result.get("stop_reason")
        text = _STOP_REASON_TEXT.get(reason, f"終了しました ({display_text(reason)})")
        return f"job {job_id}: {text}{done_runs}"
    if state == "shutdown":
        return f"job {job_id}: {_STOP_REASON_TEXT['shutdown']}{done_runs}"
    code = job.get("result_code")
    why = (MESSAGES[code] if code in ("autopilot_restricted", "unavailable")
           else error_text(code if isinstance(code, str) else None))
    return f"job {job_id}: improve を続けられませんでした: {why}{done_runs}"


def _kill_switch(status: dict) -> dict:
    # 形の崩れた応答は「ラッチ無し」として読む (表示側で traceback にしない)。
    kill = status.get("kill_switch")
    return kill if isinstance(kill, dict) else {}


def interrupt_notice(job_id: str, state, *, timed_out: bool = False,
                     lost: bool = False) -> str:
    """poll を離れるときの案内。受理済みの job は続く。取消は queued の間だけ。"""
    head = ("待ち時間の上限に達しました。" if timed_out
            else "job の状態を取得できませんでした。" if lost else "")
    text = (f"{head}job {job_id} は受理済みで、この client を離れても続きます "
            f"(state={display_text(str(state))})。状態: `afx ctl jobs get {job_id}`")
    if state == "queued":
        text += f"\n実行前 (queued) の間だけ取り消せます: `afx ctl jobs cancel {job_id}`"
    return text


# --------------------------------------------------------------- 入口

def _split_principal(argv: list[str]) -> tuple[Principal, list[str]]:
    rest = list(argv)
    principal = Principal.OPERATOR
    if rest and rest[0].startswith("--as"):
        flag = rest.pop(0)
        if flag == "--as" and rest:
            value = rest.pop(0)
        elif flag.startswith("--as="):
            value = flag[len("--as="):]
        else:
            raise ClientError(RC_USAGE, _usage())
        try:
            principal = Principal(value)
        except ValueError:
            raise ClientError(RC_USAGE, _usage()) from None
        # 常駐 service 用の principal は人間の CLI から名乗らせない。
        if principal not in (Principal.OPERATOR, Principal.APPROVER):
            raise ClientError(RC_USAGE, _usage())
    return principal, rest


def _is_terminal(stream: TextIO) -> bool:
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def main(argv: list[str], *, root: Path, stdin: TextIO | None = None,
         stdout: TextIO | None = None, stderr: TextIO | None = None,
         uid: int | None = None, sleep: Callable[[float], None] = time.sleep) -> int:
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr
    try:
        principal, tokens = _split_principal(argv)
        if not tokens:
            if not (_is_terminal(stdin) and _is_terminal(stdout)):
                raise ClientError(RC_USAGE, _usage())
            return _interactive(root, principal, uid=uid, stdin=stdin, stdout=stdout,
                                stderr=stderr, sleep=sleep)
        op = parse(tokens)
        api = ApiClient(root, principal, uid=uid) if op.name not in ("help", "stop") else None
        return Runner(api, stdin=stdin, stdout=stdout, stderr=stderr, sleep=sleep).run(op)
    except ClientError as err:
        print(f"afx ctl: {err.message}", file=stderr, flush=True)
        return err.rc
    except KeyboardInterrupt:
        return RC_INTERRUPTED


def _interactive(root: Path, principal: Principal, *, uid, stdin, stdout, stderr,
                 sleep) -> int:
    """``afx ctl>``。approver 操作は 1 回ごとに端末で確認する (まとめて許可しない)。"""
    api = ApiClient(root, principal, uid=uid)
    runner = Runner(api, stdin=stdin, stdout=stdout, stderr=stderr, sleep=sleep)
    print(f"afx ctl ({principal.value})。help で一覧、exit で終了", file=stdout, flush=True)
    while True:
        print("afx ctl> ", end="", file=stdout, flush=True)
        try:
            line = stdin.readline()
        except KeyboardInterrupt:
            print("", file=stdout)
            return RC_OK
        if not line:
            return RC_OK
        tokens = line.split()
        if not tokens:
            continue
        if tokens[0] in ("exit", "quit") and len(tokens) == 1:
            return RC_OK
        if tokens[0] == "improve" and tokens[1:2] != ["add"]:
            # improve の option だけは引用が使える。他のコマンドの自由文は壊さない。
            try:
                tokens = shlex.split(line, posix=True)
            except ValueError:
                print("afx ctl: 引用符が閉じていません (improve の引数は shell と同じ引用が"
                      "使えます)", file=stderr, flush=True)
                continue
        try:
            runner.run(parse(tokens))
        except ClientError as err:
            print(f"afx ctl: {err.message}", file=stderr, flush=True)
        except KeyboardInterrupt:
            print("", file=stdout, flush=True)
