"""MCP の spool (worker 側): spool root と dispatcher。

CLI が起動する MCP stdio shim と mission worker の間の受け渡しを、
同一 directory 内の file の rename で行う。shim 側の手順は
`mcp_spool_client` にある。

信頼の前提: spool に書けるのは worker と CLI の部分木 (同じ uid の一つの
principal) だけである。部分木による応答の先置きや要求の差し替えは自己妨害
として扱い、dispatcher が守るのは「同じ要求を二度実行しない」「spool の
外の file を読まない」「上限を超える入力で止まらない」「tool の実行の正本
は worker の ledger」の 4 点である。shim が読んだ応答は偽造され得るので、
実行の証明には ledger を使う。
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import select
import stat
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from agentic_fx.tools.mcp_spool_client import (
    CLAIM_PREFIX, REQ_PREFIX, RESP_PREFIX, RTMP_PREFIX, get_libc,
    is_lower_hex, is_request_id, renameat2_noreplace, write_all,
)

_log = logging.getLogger("agentic_fx.mcp_spool")

SPOOL_PARENT_NAME = "afx-spool"

# spool 内の entry 数の上限。shim の要求 1 件ごとに `claim-` と `resp-`
# の 2 entry が mission の終わりまで残る。改善 mission の tool 呼び出しは
# `max_tool_calls` (既定 300) で打ち切られ、その後の拒否も連続拒否の上限で
# 止まるので、正常な mission は 2 × (300 + 数十) 程度に収まる。上限は
# その 5 倍余りとし、超えたら異常として mission を止める。
MAX_SPOOL_ENTRIES = 4096

# 要求 1 件の読み取り上限 (bytes)。実 CLI の tools/call の引数は最大
# 2.5 KB 程度 (実 mission の transcript の実測)。plugin の source 全文を
# 書く呼び出しでも LLM の 1 回の出力 (数万 token、数百 KB) を超えないので、
# 余裕を見て 4 MiB とする。
MAX_REQUEST_BYTES = 4 * 1024 * 1024

# JSON の入れ子の深さと key の総数の上限。実 CLI の要求は深さ 4 前後、
# 1 object の key は 10 未満。
MAX_JSON_DEPTH = 32
MAX_JSON_KEYS = 4096

RESCAN_INTERVAL_SEC = 1.0

SPOOL_CREATE_FAILED = "spool_create_failed"
SPOOL_FLOOD = "spool_flood"
SPOOL_DISPATCHER_DIED = "spool_dispatcher_died"

# --- syscall 定数 (x86_64) ---
_SYS_OPENAT2 = 437
_RESOLVE_NO_XDEV = 0x01
_RESOLVE_NO_MAGICLINKS = 0x02
_RESOLVE_NO_SYMLINKS = 0x04
_RESOLVE_BENEATH = 0x08

_IN_MOVED_TO = 0x00000080
_IN_CREATE = 0x00000100
_IN_Q_OVERFLOW = 0x00004000
_IN_IGNORED = 0x00008000
_IN_ONLYDIR = 0x01000000
_IN_DONT_FOLLOW = 0x02000000
_IN_EVENT_HEADER = struct.Struct("iIII")


class _OpenHow(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_uint64), ("mode", ctypes.c_uint64),
                ("resolve", ctypes.c_uint64)]


def openat2_beneath(dirfd: int, name: str, flags: int) -> int:
    """`dirfd` の直下の `name` を symlink・mount 越え・magic link を辿らずに
    開く。失敗は errno つきの `OSError`。"""
    how = _OpenHow(flags=flags, mode=0,
                   resolve=(_RESOLVE_BENEATH | _RESOLVE_NO_SYMLINKS
                            | _RESOLVE_NO_XDEV | _RESOLVE_NO_MAGICLINKS))
    libc = get_libc()
    fd = libc.syscall(ctypes.c_long(_SYS_OPENAT2), ctypes.c_int(dirfd),
                      ctypes.c_char_p(os.fsencode(name)), ctypes.byref(how),
                      ctypes.c_size_t(ctypes.sizeof(how)))
    if fd < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), name)
    return int(fd)


class SpoolCreateFailed(RuntimeError):
    reason = SPOOL_CREATE_FAILED


def mcp_spool_dir(workdir: Path, nonce: str) -> Path:
    """spool root の path。dispatcher の作成と CLI に渡す MCP 設定の引数は
    どちらもこの 1 関数から導出する。"""
    return workdir / SPOOL_PARENT_NAME / nonce


def _is_nonce(value: str) -> bool:
    return len(value) >= 32 and is_lower_hex(value)


class SpoolRoot:
    """`workdir/afx-spool/<nonce>/` を 0700 で新設し、dirfd と inotify fd
    を mission の終わりまで保持する。"""

    def __init__(self, path: Path, dirfd: int, inotify_fd: int) -> None:
        self.path = path
        self.dirfd = dirfd
        self.inotify_fd = inotify_fd
        self._closed = False

    @classmethod
    def create(cls, workdir: Path, nonce: str) -> "SpoolRoot":
        if not _is_nonce(nonce):
            raise SpoolCreateFailed(
                f"{SPOOL_CREATE_FAILED}: nonce must be >=128 bit hex")
        path = mcp_spool_dir(workdir, nonce)
        parent = path.parent
        try:
            try:
                os.mkdir(parent, 0o700)
            except FileExistsError:
                pass
            pfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY
                          | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as e:
            raise SpoolCreateFailed(
                f"{SPOOL_CREATE_FAILED}: cannot open {parent}: {e}") from e
        try:
            _check_private_dir(os.fstat(pfd), parent)
            try:
                # 既存の path (file・symlink・directory) は全部 EEXIST に
                # なる。前の mission の残りを使い回さない。
                os.mkdir(nonce, 0o700, dir_fd=pfd)
                dirfd = os.open(nonce, os.O_RDONLY | os.O_DIRECTORY
                                | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=pfd)
            except OSError as e:
                raise SpoolCreateFailed(
                    f"{SPOOL_CREATE_FAILED}: cannot create {path}: {e}") from e
        finally:
            os.close(pfd)
        try:
            st = os.fstat(dirfd)
            _check_private_dir(st, path)
            if stat.S_IMODE(st.st_mode) != 0o700:
                raise SpoolCreateFailed(
                    f"{SPOOL_CREATE_FAILED}: {path} mode is "
                    f"{stat.S_IMODE(st.st_mode):o}, not 700")
        except BaseException:
            os.close(dirfd)
            raise
        return cls(path, dirfd, _open_inotify(path))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for fd in (self.inotify_fd, self.dirfd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _check_private_dir(st: os.stat_result, path: Path) -> None:
    if not stat.S_ISDIR(st.st_mode):
        raise SpoolCreateFailed(f"{SPOOL_CREATE_FAILED}: {path} is not a directory")
    if st.st_uid != os.getuid():
        raise SpoolCreateFailed(f"{SPOOL_CREATE_FAILED}: {path} has a foreign owner")
    if st.st_mode & 0o077:
        raise SpoolCreateFailed(f"{SPOOL_CREATE_FAILED}: {path} is not private")


def _open_inotify(path: Path) -> int:
    """起床用の inotify。作れなくても 1 秒周期の rescan が正本なので
    失敗は致命にしない (-1 を返す)。"""
    libc = get_libc()
    fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
    if fd < 0:
        _log.warning("inotify_init1 failed: %s", os.strerror(ctypes.get_errno()))
        return -1
    wd = libc.inotify_add_watch(
        fd, os.fsencode(str(path)),
        _IN_CREATE | _IN_MOVED_TO | _IN_ONLYDIR | _IN_DONT_FOLLOW)
    if wd < 0:
        _log.warning("inotify_add_watch failed: %s",
                     os.strerror(ctypes.get_errno()))
        os.close(fd)
        return -1
    return int(fd)


@dataclass(frozen=True)
class LedgerEvent:
    request_id: str
    kind: str
    detail: str = ""


class SpoolLedger:
    """dispatcher の記録。tool を実行したかの正本はここ (shim が読む応答
    ではない)。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[LedgerEvent] = []

    def record(self, request_id: str, kind: str, detail: str = "") -> None:
        with self._lock:
            self._events.append(LedgerEvent(request_id, kind, detail))

    def events(self, kind: str | None = None) -> list[LedgerEvent]:
        with self._lock:
            return [e for e in self._events if kind is None or e.kind == kind]

    def tool_executed_ids(self) -> list[str]:
        """`registry.execute()` を実際に呼んだ要求の id。"""
        return [e.request_id for e in self.events("tool_executed")]


class _Rejected(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def parse_request(raw: bytes) -> dict:
    """要求の JSON を上限つきで読む。違反は `_Rejected`。"""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _Rejected("invalid_utf8") from None
    key_count = 0

    def pairs_hook(pairs: list[tuple[str, object]]) -> dict:
        nonlocal key_count
        key_count += len(pairs)
        if key_count > MAX_JSON_KEYS:
            raise _Rejected("json_too_many_keys")
        obj: dict = {}
        for key, value in pairs:
            if key in obj:
                raise _Rejected("json_duplicate_key")
            obj[key] = value
        return obj

    try:
        value = json.loads(text, object_pairs_hook=pairs_hook)
    except _Rejected:
        raise
    except RecursionError:
        raise _Rejected("json_too_deep") from None
    except ValueError:
        raise _Rejected("not_json") from None
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            children = list(node.values())
        elif isinstance(node, list):
            children = node
        else:
            continue
        if depth > MAX_JSON_DEPTH:
            raise _Rejected("json_too_deep")
        stack.extend((child, depth + 1) for child in children)
    if not isinstance(value, dict):
        raise _Rejected("not_object")
    if "id" not in value:
        raise _Rejected("notification")
    return value


Handler = Callable[[dict, Callable[[dict], None]], None]


class SpoolDispatcher:
    """spool の `req-` を claim・検証して handler に渡し、応答を publish
    する。

    handler は `(request, publish)` を受け取り、応答 dict を `publish` に
    渡す。allowed・budget・実行・counter・`after_send` の直列化は handler
    (`McpShimDispatcher.respond`) の lock が受け持つ。dispatcher の thread は
    1 本で、要求を 1 件ずつ処理する。
    """

    def __init__(self, root: SpoolRoot, handler: Handler, *,
                 use_inotify: bool = True,
                 rescan_interval_sec: float = RESCAN_INTERVAL_SEC,
                 max_entries: int = MAX_SPOOL_ENTRIES,
                 max_request_bytes: int = MAX_REQUEST_BYTES,
                 on_fatal: Callable[[str], None] | None = None) -> None:
        self._root = root
        self._handler = handler
        self._use_inotify = use_inotify and root.inotify_fd >= 0
        self._rescan_interval = rescan_interval_sec
        self._max_entries = max_entries
        self._max_request_bytes = max_request_bytes
        self._on_fatal = on_fatal
        self.ledger = SpoolLedger()
        self._current_rid: str | None = None
        self.failure_reason: str | None = None
        self.overflow_count = 0
        self.ignored_count = 0
        self._state_lock = threading.Lock()
        self._seen: set[str] = set()
        self._stop = threading.Event()
        self._wake_r, self._wake_w = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        self._fds_closed = False
        self._thread: threading.Thread | None = None

    def record_current(self, kind: str, detail: str = "") -> None:
        """handler が処理中の要求について ledger に書く。dispatcher の
        thread は 1 本なので、処理中の要求は高々 1 件。handler の外では
        何もしない。"""
        rid = self._current_rid
        if rid is not None:
            self.ledger.record(rid, kind, detail)

    @property
    def spool_dir(self) -> Path:
        return self._root.path

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    @property
    def thread(self) -> threading.Thread | None:
        return self._thread

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="afx-mcp-spool", daemon=True)
        self._thread.start()

    def stop(self, timeout_sec: float = 5.0, *, deadline: float | None = None) -> bool:
        """以後 rescan も実行もしない。thread が期限内に終われば True。
        実行中の tool があれば、その完了までは thread が残る (fd は thread
        の終わりで閉じる)。

        `deadline` (monotonic の絶対時刻) を渡すと `timeout_sec` の代わりにそれまでしか
        待たない (mission の cleanup deadline を段ごとに足し直さないため)。"""
        if deadline is not None:
            timeout_sec = max(0.0, deadline - time.monotonic())
        self._stop.set()
        with self._state_lock:
            # 閉じた fd 番号は再利用され得るので、閉じた後は書かない。
            if not self._fds_closed:
                try:
                    os.write(self._wake_w, b"x")
                except OSError:
                    pass
        if self._thread is None:
            self._close_fds()
            return True
        self._thread.join(timeout_sec)
        return not self._thread.is_alive()

    # 呼び出し側の互換名 (mission 終了時の後始末)。
    close = stop

    def _close_fds(self) -> None:
        with self._state_lock:
            if self._fds_closed:
                return
            self._fds_closed = True
        self._root.close()
        for fd in (self._wake_r, self._wake_w):
            try:
                os.close(fd)
            except OSError:
                pass

    def _fail(self, reason: str) -> None:
        with self._state_lock:
            if self.failure_reason is not None:
                return
            self.failure_reason = reason
        self.ledger.record("", reason)
        _log.error("MCP spool failure: %s", reason)
        self._stop.set()
        if self._on_fatal is not None:
            try:
                self._on_fatal(reason)
            except Exception:  # noqa: BLE001 — 通知の失敗で thread を落とさない
                _log.exception("on_fatal callback failed")

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self.scan_once()
                if self._stop.is_set():
                    break
                self._wait_for_wake()
        except Exception:  # noqa: BLE001 — thread の死を mission failure にする
            # 想定外の例外で thread だけが静かに死ぬと、shim の要求は誰にも
            # 処理されず mission が tool 0 個で空回りする。
            _log.exception("MCP spool dispatcher crashed")
            self._fail(SPOOL_DISPATCHER_DIED)
        finally:
            self._close_fds()

    def _wait_for_wake(self) -> None:
        poller = select.poll()
        poller.register(self._wake_r, select.POLLIN)
        if self._use_inotify:
            poller.register(self._root.inotify_fd, select.POLLIN)
        ready = poller.poll(self._rescan_interval * 1000)
        for fd, _ in ready:
            if fd == self._wake_r:
                self._drain(self._wake_r)
            elif fd == self._root.inotify_fd:
                self._drain_inotify()

    @staticmethod
    def _drain(fd: int) -> None:
        try:
            while os.read(fd, 4096):
                pass
        except BlockingIOError:
            pass

    def _drain_inotify(self) -> None:
        fd = self._root.inotify_fd
        while True:
            try:
                buf = os.read(fd, 65536)
            except BlockingIOError:
                return
            if not buf:
                return
            offset = 0
            while offset + _IN_EVENT_HEADER.size <= len(buf):
                _wd, mask, _cookie, name_len = _IN_EVENT_HEADER.unpack_from(
                    buf, offset)
                offset += _IN_EVENT_HEADER.size + name_len
                if mask & _IN_Q_OVERFLOW:
                    # 取りこぼした event がある。次の scan (この直後) が
                    # 正本なので、記録だけする。
                    self.overflow_count += 1
                if mask & _IN_IGNORED:
                    # watch が外れた。以後は周期 rescan だけで動く。
                    self.ignored_count += 1
                    self._use_inotify = False

    def scan_once(self) -> None:
        """spool を 1 回走査して、未処理の `req-` を全部処理する。"""
        if self._stop.is_set():
            return
        pending: list[str] = []
        count = 0
        with os.scandir(self._root.dirfd) as it:
            for entry in it:
                count += 1
                if count > self._max_entries:
                    self._fail(SPOOL_FLOOD)
                    return
                name = entry.name
                if name.startswith(REQ_PREFIX):
                    rid = name[len(REQ_PREFIX):]
                    if is_request_id(rid):
                        pending.append(rid)
        for rid in pending:
            if self._stop.is_set():
                return
            self.claim_and_process(rid)

    def claim_and_process(self, rid: str) -> bool:
        """`req-<rid>` を claim し、成功した 1 回だけ処理する。処理した
        (handler に渡したか、検証で拒否した) なら True。"""
        if self._stop.is_set():
            return False
        req_name = REQ_PREFIX + rid
        claim_name = CLAIM_PREFIX + rid
        try:
            renameat2_noreplace(self._root.dirfd, req_name, claim_name)
        except FileNotFoundError:
            return False  # 別の claim が先に取った
        except FileExistsError:
            # 同じ id の claim が既にある。二度は実行しない。
            self._record_replay(rid, "claim_exists")
            return False
        except OSError as e:
            self.ledger.record(rid, "claim_failed", str(e))
            return False
        with self._state_lock:
            if rid in self._seen:
                replay = True
            else:
                self._seen.add(rid)
                replay = False
        if replay:
            self._record_replay(rid, "id_already_processed")
            return False
        self._process_claimed(rid)
        return True

    def _record_replay(self, rid: str, detail: str) -> None:
        # 同じ残骸を rescan ごとに記録し直さない。
        key = f"replay:{detail}:{rid}"
        with self._state_lock:
            if key in self._seen:
                return
            self._seen.add(key)
        self.ledger.record(rid, "replay", detail)

    def _open_claimed(self, rid: str) -> int:
        """claim した file を開き、中身を読む前に検証する。違反は
        `_Rejected`。返した fd は呼び出し側が閉じる。"""
        try:
            fd = openat2_beneath(
                self._root.dirfd, CLAIM_PREFIX + rid,
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as e:
            raise _Rejected(f"open_failed:{e.strerror}") from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise _Rejected("not_regular_file")
            if st.st_uid != os.getuid():
                raise _Rejected("foreign_uid")
            if st.st_nlink != 1:
                raise _Rejected("link_count")
            if st.st_mode & 0o077:
                raise _Rejected("not_private_mode")
            if st.st_size > self._max_request_bytes:
                raise _Rejected("oversized")
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _read_claimed(self, fd: int) -> bytes:
        # 読む間に伸びる file でも上限 + 1 byte で止める。
        limit = self._max_request_bytes + 1
        chunks: list[bytes] = []
        total = 0
        while total < limit:
            chunk = os.read(fd, min(65536, limit - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > self._max_request_bytes:
            raise _Rejected("oversized")
        return b"".join(chunks)

    def _process_claimed(self, rid: str) -> None:
        request: dict | None = None
        reject_reason: str | None = None
        claim_fd = -1
        try:
            claim_fd = self._open_claimed(rid)
        except _Rejected as r:
            reject_reason = r.reason

        # 応答の置き場所は実行の前に確保する。先置きされていれば実行しない。
        rtmp_name = RTMP_PREFIX + rid
        try:
            rtmp_fd = os.open(rtmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                              | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                              dir_fd=self._root.dirfd)
        except FileExistsError:
            self._close_quietly(claim_fd)
            self.ledger.record(rid, "response_tmp_preempted")
            return
        except OSError as e:
            self._close_quietly(claim_fd)
            self.ledger.record(rid, "response_tmp_failed", str(e))
            return

        # 中身の読み取りと JSON の decode は、応答の置き場所を確保して
        # (先置きされていないと確かめて) から行う。
        try:
            if claim_fd >= 0:
                try:
                    request = parse_request(self._read_claimed(claim_fd))
                except _Rejected as r:
                    reject_reason = r.reason
        finally:
            if claim_fd >= 0:
                os.close(claim_fd)

        published = False

        def publish(response: dict) -> None:
            nonlocal published
            if published:
                return
            # `_publish` は成否によらず rtmp_fd を閉じて rtmp を片づける。
            # 失敗で応答が出せなかった要求は shim が期限で error にする。
            self._publish(rid, rtmp_fd, response)
            published = True

        try:
            if reject_reason is not None:
                self.ledger.record(rid, "rejected", reject_reason)
                publish({"jsonrpc": "2.0", "id": None, "error": {
                    "code": -32600,
                    "message": f"request rejected: {reject_reason}"}})
                return
            assert request is not None
            self.ledger.record(rid, "dispatched", str(request.get("method")))
            self._current_rid = rid
            try:
                self._handler(request, publish)
            except Exception as e:  # noqa: BLE001 — 応答を返して次へ進む
                _log.exception("MCP handler failed")
                self.ledger.record(rid, "handler_error", type(e).__name__)
                publish({"jsonrpc": "2.0", "id": request.get("id"),
                         "error": {"code": -32603,
                                   "message": f"internal error: {type(e).__name__}"}})
            finally:
                self._current_rid = None
        finally:
            if not published:
                os.close(rtmp_fd)
                try:
                    os.unlink(rtmp_name, dir_fd=self._root.dirfd)
                except OSError:
                    pass

    @staticmethod
    def _close_quietly(fd: int) -> None:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass

    def _publish(self, rid: str, rtmp_fd: int, response: dict) -> None:
        """応答を rtmp に全量書いて resp へ NOREPLACE で rename する。成否に
        よらず rtmp_fd を閉じ、失敗したら rtmp を残さない。例外は外へ出さず
        ledger に書く (dispatcher の thread を落とさない)。"""
        rtmp_name = RTMP_PREFIX + rid
        outcome: tuple[str, str] | None = None
        try:
            try:
                write_all(rtmp_fd, (json.dumps(response) + "\n").encode())
                os.fsync(rtmp_fd)
            finally:
                os.close(rtmp_fd)
            renameat2_noreplace(self._root.dirfd, rtmp_name, RESP_PREFIX + rid)
        except FileExistsError:
            outcome = ("response_preempted", "")
        except Exception as e:  # noqa: BLE001 — OSError 以外 (dumps の失敗) も同じ扱い
            outcome = ("publish_failed", f"{type(e).__name__}: {e}")
        if outcome is not None:
            self.ledger.record(rid, *outcome)
            try:
                os.unlink(rtmp_name, dir_fd=self._root.dirfd)
            except OSError:
                pass
