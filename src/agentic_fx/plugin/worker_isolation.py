"""plugin worker の隔離段と source-only loader。

worker は plugin を 1 行も読む前に、この module の手順で自分自身を隔離する。

1. `prepare_isolation()`: bytecode の書き出しを止め、protocol 用に stdout を退避して
   fd 1 を stderr へ向け、失敗応答の行をすべて bytes にしておく。隔離段が使う module は
   この module の import 時点で読み込み済みになる (rlimit 後に遅延 import させない)。
2. handshake を読む (呼び出し側)。
3. `isolate(prepared, handshake, main_dir)`: rlimit → 継承 seccomp filter の検査 →
   Landlock ABI と匿名 session keyring → allowlist の組み立てと自己検査 → Landlock →
   seccomp `allow/6` → 規則用 fd の close → sandbox 下の runtime import 自己試験。

隔離段の失敗は、保持 fd を閉じ、traceback と 1 行の要約を stderr に書き、事前に作った
`sandbox_ready ok:false` の行を `os.write` で protocol fd に書いて `os._exit(0)` する。
RLIMIT_AS が極小でも応答できるよう、失敗経路では JSON 整形も import もしない。

`load_plugin_module()` は検査済みの plugin dir fd から `plugin.py` と `config.yaml` だけを
有界に読み、hash を照合した同じ bytes を compile / exec する (pyc は読まない・書かない)。

**この module の関数は呼んだ process を不可逆に隔離する。** テストでは必ず使い捨ての子
process の中で呼ぶ。

この module は import 時に numpy / pandas を読まない (隔離前に plugin の依存を読ませない)。
"""
from __future__ import annotations

import ctypes
import errno
import json
import linecache
import os
import resource
import stat as _stat
import sys
import tokenize  # noqa: F401  (traceback 整形が使う。rlimit 後に遅延 import させない)
import traceback
import types
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib.machinery import ModuleSpec
from pathlib import Path

from agentic_fx.core import landlock, seccomp
from agentic_fx.core.plugin_files import MAX_PLUGIN_FILE_BYTES as _SHARED_MAX_PLUGIN_FILE_BYTES
from agentic_fx.core.plugin_files import read_fd_up_to, write_all as _write_all
from agentic_fx.core.worker_limits import NPROC_CAP as _NPROC_CAP
from agentic_fx.core.runtime_fingerprint import SANDBOX_PROFILE_VERSION
from agentic_fx.plugin import version_store

#: worker の隔離段が返し得る固定 reason (親は `sandbox_setup_failed:<reason>` に写す。
#: `inherited_seccomp_filter` だけは親でも同名)
WORKER_SANDBOX_REASONS = (
    "rlimit_failed", "inherited_seccomp_filter", "landlock_abi_too_old",
    "keyring_join_failed", "landlock_task_inspection_failed", "landlock_multithreaded",
    "runtime_root_too_wide", "fd_open_failed", "allowlist_guarded", "allowlist_not_leaf",
    "plugin_file_invalid", "landlock_create_failed", "landlock_add_rule_failed",
    "no_new_privs_failed", "landlock_restrict_failed", "seccomp_failed",
    "isolation_unexpected_error", "runtime_import_failed",
)

#: `WORKER_SANDBOX_REASONS` のうち、候補の `plugin.py` / `config.yaml` が原因のもの
#: (symlink・非通常ファイル・guarded dir)。環境障害ではなく候補の責任なので、親は
#: これらを `plugin_error` に分類する (`started:true`、候補枠を消費、agent に修正を促す)。
#: 残りは環境側で `sandbox_unavailable`。
CANDIDATE_ISOLATION_REASONS = (
    "plugin_file_invalid", "allowlist_not_leaf", "allowlist_guarded",
)

_UNEXPECTED = "isolation_unexpected_error"

#: 成功 `sandbox_ready` の attested field。順序も契約 (親は順序違いを拒否する)
ATTESTED_FIELDS = ("sandbox_profile_version", "landlock_fs_abi", "landlock_tsync",
                   "seccomp", "keyring", "network", "scope", "nonce")

#: `plugin.py` / `config.yaml` の読取上限。親の loader と同じ出所 (値だけを共有し、
#: dir_fd からの openat による読取はこの module で行う)
MAX_PLUGIN_FILE_BYTES = _SHARED_MAX_PLUGIN_FILE_BYTES

#: source-only loader の失敗 reason (公開分類はどれも `plugin_error`)
PLUGIN_LOAD_REASONS = ("open_failed", "not_regular_file", "file_too_large",
                       "content_hash_mismatch", "decode_failed", "exec_failed")

# x86_64 の keyctl と、その操作番号
_SYS_KEYCTL = 250
_KEYCTL_GET_KEYRING_ID = 0
_KEYCTL_JOIN_SESSION_KEYRING = 1
_KEY_SPEC_SESSION_KEYRING = -3

_HEX = frozenset("0123456789abcdef")


class IsolationStageError(Exception):
    """隔離段の失敗。`reason` は `WORKER_SANDBOX_REASONS` のどれか。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class PluginLoadError(Exception):
    """source-only loader の失敗。候補側の事象で、公開分類は `plugin_error`。
    `reason` は `PLUGIN_LOAD_REASONS` のどれか。exec の失敗は元の例外を
    `__cause__` に持つ (traceback を stderr に出すため)。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


# --- 段 1: 準備 --------------------------------------------------------------------

@dataclass
class PreparedIsolation:
    """`prepare_isolation()` の結果。失敗応答の行は reason ごとに bytes で持つ。"""

    protocol_fd: int
    pid: int
    failure_lines: dict[str, bytes]
    fallback_line: bytes
    status_fd: int
    allowlist_summary: str


def _failure_line(reason: str, pid: int) -> bytes:
    return (json.dumps({"phase": "sandbox_ready", "ok": False, "stage": "sandbox",
                        "reason": reason, "pid": pid}, separators=(",", ":"))
            + "\n").encode("ascii")


def _allowlist_summary() -> str:
    # sysconfig の設定値もここで読み込ませておく (rlimit 後の遅延読込を避ける)
    code_root = Path(landlock.__file__).resolve().parents[1]
    runtime = ",".join(str(p) for p in landlock.runtime_subtree(code_root))
    system = ",".join(str(p) for p in landlock.system_dirs())
    return f"runtime=[{runtime}] system=[{system}] device=[/dev/urandom]"


def prepare_isolation() -> PreparedIsolation:
    """隔離段の手順 1。handshake を読む前に 1 回だけ呼ぶ。

    bytecode の書き出しを止める (sandbox 下の遅延 import が `__pycache__` の mkdir で
    SIGSYS 死しないように)。元の stdout を protocol 用に複製して退避し、fd 1 を stderr
    へ向ける。失敗応答の全行と allowlist の要約を作り、`/proc/self/status` を開いて
    おく (rlimit の後に fd を新しく開かずに継承 filter を検査するため)。

    protocol fd を得た後の失敗は、予備の `isolation_unexpected_error` 行を書いて
    `os._exit(0)` する。protocol fd を得られないときは書き先が無いので、stderr に 1 行
    出して非 0 で終了する。
    """
    sys.dont_write_bytecode = True
    pid = os.getpid()
    # protocol fd を得る前に予備行を bytes にしておく (以後の失敗でも書ける)
    fallback_line = _failure_line(_UNEXPECTED, pid)
    try:
        protocol_fd = os.dup(1)
    except BaseException as exc:  # noqa: BLE001  書き先が無い。親は最初の行の前の終了として扱う
        try:
            sys.stderr.write(f"plugin worker: cannot duplicate the protocol fd: {exc!r}\n")
            sys.stderr.flush()
        except BaseException:  # noqa: BLE001
            pass
        os._exit(70)
    try:
        os.dup2(2, 1)
        lines = {r: _failure_line(r, pid) for r in WORKER_SANDBOX_REASONS}
        try:
            summary = _allowlist_summary()
        except Exception as exc:  # noqa: BLE001  要約は診断用。作れなくても隔離は止めない
            summary = f"unavailable ({type(exc).__name__})"
        try:
            status_fd = os.open("/proc/self/status", os.O_RDONLY | os.O_CLOEXEC)
        except OSError:
            status_fd = -1
        return PreparedIsolation(protocol_fd=protocol_fd, pid=pid, failure_lines=lines,
                                 fallback_line=lines[_UNEXPECTED], status_fd=status_fd,
                                 allowlist_summary=summary)
    except BaseException:  # noqa: BLE001  準備中の失敗も固定行を書いて終える
        try:
            _write_all(protocol_fd, fallback_line)
        except BaseException:  # noqa: BLE001
            pass
        os._exit(0)


# --- 段 3〜10: 隔離 ------------------------------------------------------------------

@dataclass(frozen=True)
class PluginRecord:
    """loader が読む 1 本の plugin。`dir_fd` は検査に使った dir の O_PATH fd。"""

    module_name: str
    alias: str | None
    real_dir: str
    dir_fd: int
    content_hash: str


@dataclass
class IsolatedWorker:
    """隔離に成功した worker。`attestation` は `ATTESTED_FIELDS` の順。"""

    protocol_fd: int
    attestation: dict[str, object]
    main: PluginRecord
    indicators: tuple[PluginRecord, ...]
    _closed: bool = field(default=False, repr=False)

    def records(self) -> tuple[PluginRecord, ...]:
        return (self.main, *self.indicators)

    def sandbox_ready_line(self, pid: int) -> bytes:
        """成功 `sandbox_ready` の 1 行 (envelope の phase・ok・pid の後に attested field)。"""
        obj: dict[str, object] = {"phase": "sandbox_ready", "ok": True, "pid": int(pid)}
        obj.update((k, self.attestation[k]) for k in ATTESTED_FIELDS)
        return (json.dumps(obj, separators=(",", ":")) + "\n").encode("ascii")

    def close(self) -> None:
        """保持している plugin dir fd を閉じる (同じ dir の重複は 1 回だけ)。"""
        if self._closed:
            return
        self._closed = True
        for fd in {r.dir_fd for r in self.records()}:
            _close_quietly(fd)


@dataclass(frozen=True)
class _HandshakeSpec:
    nonce: str
    cpu_sec: int
    memory_mb: int
    nofile: int
    fsize_mb: int
    main_dir: str
    main_hash: str
    indicators: tuple[tuple[str, str, str], ...]   # (alias, real_dir, content_hash)


def _require_int(hs: Mapping, key: str) -> int:
    v = hs.get(key)
    if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
        raise IsolationStageError(_UNEXPECTED, f"handshake {key} must be a positive int")
    return v


def _require_hex(value: object, length: int, what: str) -> str:
    if not isinstance(value, str) or len(value) != length or not set(value) <= _HEX:
        raise IsolationStageError(_UNEXPECTED, f"handshake {what} must be {length} hex digits")
    return value


def _parse_handshake(hs: Mapping, main_dir: str) -> _HandshakeSpec:
    if not isinstance(hs, Mapping):
        raise IsolationStageError(_UNEXPECTED, "handshake is not an object")
    indicators = []
    raw = hs.get("indicators") or []
    if not isinstance(raw, list):
        raise IsolationStageError(_UNEXPECTED, "handshake indicators must be a list")
    for item in raw:
        if not isinstance(item, Mapping):
            raise IsolationStageError(_UNEXPECTED, "handshake indicator is not an object")
        alias, plugin_py = item.get("alias"), item.get("plugin_py")
        if not isinstance(alias, str) or not alias.isidentifier():
            raise IsolationStageError(_UNEXPECTED, "handshake indicator alias")
        if not isinstance(plugin_py, str) or os.path.basename(plugin_py) != "plugin.py" \
                or not os.path.isabs(plugin_py):
            raise IsolationStageError(_UNEXPECTED, "handshake indicator plugin_py")
        indicators.append((alias, os.path.realpath(os.path.dirname(plugin_py)),
                           _require_hex(item.get("content_hash"), 64,
                                        f"indicator {alias} content_hash")))
    if len({a for a, _d, _h in indicators}) != len(indicators):
        raise IsolationStageError(_UNEXPECTED, "handshake indicator aliases are not unique")
    if not isinstance(main_dir, str) or not os.path.isabs(main_dir):
        raise IsolationStageError(_UNEXPECTED, "main plugin dir must be absolute")
    return _HandshakeSpec(
        nonce=_require_hex(hs.get("attest_nonce"), 32, "attest_nonce"),
        cpu_sec=_require_int(hs, "cpu_sec"), memory_mb=_require_int(hs, "memory_mb"),
        nofile=_require_int(hs, "nofile"), fsize_mb=_require_int(hs, "fsize_mb"),
        main_dir=os.path.realpath(main_dir),
        main_hash=_require_hex(hs.get("content_hash"), 64, "content_hash"),
        indicators=tuple(indicators))


def _set_rlimits(spec: _HandshakeSpec) -> None:
    """CORE を最初に 0 にする (以後の失敗で core を残さない)。値は handshake のまま掛け、
    hard 上限を超える要求は失敗させる (緩めた値で黙って続行しない)。"""
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CPU, (spec.cpu_sec, spec.cpu_sec))
        mem = spec.memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_NOFILE, (spec.nofile, spec.nofile))
        fsize = spec.fsize_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
        resource.setrlimit(resource.RLIMIT_NPROC, (_NPROC_CAP, _NPROC_CAP))
    except (ValueError, OSError) as exc:
        raise IsolationStageError("rlimit_failed", f"{type(exc).__name__}: {exc}") from exc


def _check_inherited_filter(status_fd: int) -> None:
    """`Seccomp_filters` が 0 であることを確かめる。field 不在・読取不能・非 0 は拒否。
    継承した filter と合成すると attestation の `Seccomp_filters=1` が成り立たない。"""
    if status_fd < 0:
        raise IsolationStageError("inherited_seccomp_filter", "/proc/self/status unreadable")
    try:
        data = os.pread(status_fd, 8192, 0)
    except OSError as exc:
        raise IsolationStageError("inherited_seccomp_filter", f"read: {exc}") from exc
    value = seccomp.seccomp_filters_field(data)
    if value is None:
        raise IsolationStageError("inherited_seccomp_filter", "Seccomp_filters field missing")
    if value != b"0":
        raise IsolationStageError(
            "inherited_seccomp_filter", f"Seccomp_filters={value.decode('ascii', 'replace')}")


def _keyctl(op: int, *args: int) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    ctypes.set_errno(0)
    rc = libc.syscall(ctypes.c_long(_SYS_KEYCTL), ctypes.c_long(op),
                      *(ctypes.c_long(a) for a in args))
    if rc < 0:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
    return int(rc)


def join_anonymous_session_keyring() -> int:
    """新しい匿名 session keyring に切り替え、その id を返す。親の session / user keyring
    の鍵を以後この process から引けないようにする。失敗は `keyring_join_failed`。"""
    try:
        return _keyctl(_KEYCTL_JOIN_SESSION_KEYRING, 0)
    except OSError as exc:
        raise IsolationStageError("keyring_join_failed", f"errno={exc.errno}") from exc


def session_keyring_id() -> int:
    """現在の session keyring の id (診断・テスト用。seccomp 適用後は呼べない)。"""
    return _keyctl(_KEYCTL_GET_KEYRING_ID, _KEY_SPEC_SESSION_KEYRING, 0)


def _apply_sandbox(plugin_dirs: Sequence[str], *, status_fd: int,
                   repo_root: Path | None, home: Path | None,
                   progress: list[str]) -> landlock.AppliedRuleset:
    """手順 4〜9。継承 filter の検査 → ABI と匿名 keyring → Landlock → seccomp。
    規則用 fd は `apply_plugin_ruleset` が閉じ、plugin dir fd だけが残る。"""
    progress[0] = "inherited_seccomp_filter"
    _check_inherited_filter(status_fd)
    progress[0] = "landlock_abi"
    landlock.plan_for_abi(landlock.landlock_abi())
    progress[0] = "keyring"
    join_anonymous_session_keyring()
    progress[0] = "landlock"
    applied = landlock.apply_plugin_ruleset([Path(p) for p in plugin_dirs],
                                            repo_root=repo_root, home=home)
    progress[0] = "seccomp"
    try:
        seccomp.apply_allow6()
    except BaseException:
        for fd in set(applied.plugin_dir_fds.values()):
            _close_quietly(fd)
        raise
    return applied


def _reason_of(exc: BaseException, lines: Mapping[str, bytes]) -> str:
    reason = getattr(exc, "reason", None)
    if isinstance(exc, (IsolationStageError, landlock.LandlockSetupError,
                        seccomp.SeccompError)) and reason in lines:
        return reason
    return _UNEXPECTED


def _fail(prepared: PreparedIsolation, exc: BaseException, step: str,
          held: Sequence[int], plugin_dirs: Sequence[str]) -> None:
    """隔離段の失敗応答。保持 fd を閉じ、stderr に traceback と要約を書き、事前に作った
    行を protocol fd に書いて終了する。戻らない。"""
    try:
        line = prepared.failure_lines.get(_reason_of(exc, prepared.failure_lines),
                                          prepared.fallback_line)
        for fd in held:
            _close_quietly(fd)
        if prepared.status_fd >= 0:
            _close_quietly(prepared.status_fd)
        try:
            traceback.print_exception(exc, file=sys.stderr)
            sys.stderr.write(
                "plugin worker: sandbox setup failed reason="
                + _reason_of(exc, prepared.failure_lines) + " step=" + step
                + " plugin_dirs=[" + ",".join(plugin_dirs) + "] "
                + prepared.allowlist_summary + "\n")
            sys.stderr.flush()
        except BaseException:  # noqa: BLE001  診断が書けなくても応答は返す
            pass
        _write_all(prepared.protocol_fd, line)
    except BaseException:  # noqa: BLE001  応答の組み立て自体が失敗しても固定行だけは書く
        try:
            os.write(prepared.protocol_fd, prepared.fallback_line)
        except BaseException:  # noqa: BLE001
            pass
    os._exit(0)


def isolate(prepared: PreparedIsolation, handshake: Mapping, main_dir: str, *,
            repo_root: Path | None = None, home: Path | None = None) -> IsolatedWorker:
    """隔離段の手順 3〜10 をこの順で行い、成功時は `IsolatedWorker` を返す。

    失敗時は戻らない: 保持 fd を閉じ、traceback と要約を stderr に書き、`prepared` が
    持つ固定の `sandbox_ready ok:false` 行を protocol fd に書いて `os._exit(0)` する。
    `repo_root` と `home` は guarded root の基準 (既定は module の位置と `~`)。
    """
    progress = ["handshake"]
    held: list[int] = []
    dirs: list[str] = [str(main_dir)]
    try:
        spec = _parse_handshake(handshake, main_dir)
        dirs = [spec.main_dir, *(d for _a, d, _h in spec.indicators)]
        progress[0] = "rlimit"
        _set_rlimits(spec)
        applied = _apply_sandbox(dirs, status_fd=prepared.status_fd, repo_root=repo_root,
                                 home=home, progress=progress)
        held = list(set(applied.plugin_dir_fds.values()))
        _close_quietly(prepared.status_fd)
        prepared.status_fd = -1
        progress[0] = "runtime_import"
        try:
            import numpy  # noqa: F401
            import pandas  # noqa: F401

            import agentic_fx.core.plugin_contract  # noqa: F401
        except BaseException as exc:  # noqa: BLE001  MemoryError も含めて固定 reason にする
            raise IsolationStageError("runtime_import_failed",
                                      f"{type(exc).__name__}: {exc}") from exc
        progress[0] = "attestation"
        fds = applied.plugin_dir_fds
        main = PluginRecord("plugin", None, spec.main_dir, fds[spec.main_dir], spec.main_hash)
        indicators = tuple(PluginRecord(f"indicator_{alias}", alias, d, fds[d], h)
                           for alias, d, h in spec.indicators)
        landlock_fields = applied.attestation_fields()
        attestation: dict[str, object] = {
            "sandbox_profile_version": SANDBOX_PROFILE_VERSION,
            "landlock_fs_abi": landlock_fields["landlock_fs_abi"],
            "landlock_tsync": landlock_fields["landlock_tsync"],
            "seccomp": seccomp.SECCOMP_PROFILE,
            "keyring": "anonymous",
            "network": landlock_fields["network"],
            "scope": landlock_fields["scope"],
            "nonce": spec.nonce,
        }
    except BaseException as exc:  # noqa: BLE001  既知・未知の失敗をすべて固定応答にする
        _fail(prepared, exc, progress[0], held, dirs)
        raise AssertionError("unreachable") from None  # _fail は戻らない
    return IsolatedWorker(protocol_fd=prepared.protocol_fd, attestation=attestation,
                          main=main, indicators=indicators)


def run_isolation_stage(main_dir: str, read_handshake: Callable[[], Mapping | None], *,
                        repo_root: Path | None = None,
                        home: Path | None = None) -> IsolatedWorker | None:
    """隔離段の手順 1〜10 を順に行う (`prepare_isolation` → handshake 読取 → `isolate`)。

    handshake が EOF (`read_handshake` が None) なら plugin を読まずに None を返す。
    handshake の読取で例外が出たら `isolation_unexpected_error` の固定応答で終了する。
    """
    prepared = prepare_isolation()
    try:
        handshake = read_handshake()
    except BaseException as exc:  # noqa: BLE001
        _fail(prepared, exc, "handshake", (), [str(main_dir)])
        raise AssertionError("unreachable") from None
    if handshake is None:
        _close_quietly(prepared.status_fd)
        return None
    return isolate(prepared, handshake, main_dir, repo_root=repo_root, home=home)


def isolate_for_selftest() -> dict:
    """代表自己試験 (`run_representative_selftest`) の子が引数なしで呼ぶ隔離の入口。

    plugin を持たない runtime / system 規則だけで、本番と同じ順 (継承 filter の検査 →
    匿名 session keyring → Landlock → seccomp `allow/6`) に掛ける。rlimit は掛けない。
    返り値は `runtime_fingerprint.ATTESTATION_KEYS` の証跡。失敗は例外のまま上げる。
    """
    sys.dont_write_bytecode = True
    status_fd = os.open("/proc/self/status", os.O_RDONLY | os.O_CLOEXEC)
    try:
        applied = _apply_sandbox([], status_fd=status_fd, repo_root=None, home=None,
                                 progress=["start"])
    finally:
        _close_quietly(status_fd)
    return {**applied.attestation_fields(),
            "seccomp_profile": seccomp.SECCOMP_PROFILE,
            "sandbox_profile": SANDBOX_PROFILE_VERSION}


# --- source-only loader -------------------------------------------------------------

def _close_quietly(fd: int) -> None:
    if fd < 0:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def read_plugin_file_bounded(dir_fd: int, name: str, *,
                             limit: int = MAX_PLUGIN_FILE_BYTES) -> bytes:
    """`dir_fd` 直下の `name` を symlink を辿らずに開き、最大 `limit + 1` bytes だけ読む。

    上限は読んだ bytes 数で判定する (fstat の size は読取中に伸び得る)。超過は
    `file_too_large`、通常ファイルでなければ `not_regular_file`、open / read の失敗は
    `open_failed`。O_NONBLOCK は FIFO に差し替えられた名前で読取が止まらないため。
    """
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:   # 名前が symlink
            raise PluginLoadError("not_regular_file", f"{name} is a symlink") from exc
        raise PluginLoadError("open_failed", f"{name}: {exc}") from exc
    try:
        if not _stat.S_ISREG(os.fstat(fd).st_mode):
            raise PluginLoadError("not_regular_file", f"{name} is not a regular file")
        try:
            data = read_fd_up_to(fd, limit)
        except OSError as exc:
            raise PluginLoadError("open_failed", f"{name}: read: {exc}") from exc
        if data is None:
            raise PluginLoadError("file_too_large", f"{name} exceeds {limit} bytes")
        return data
    finally:
        os.close(fd)


def content_hash_at(dir_fd: int) -> str:
    """`dir_fd` 直下の `plugin.py` と `config.yaml` を有界に読んだ `content_hash`。"""
    return version_store.content_hash_bytes(read_plugin_file_bounded(dir_fd, "plugin.py"),
                                            read_plugin_file_bounded(dir_fd, "config.yaml"))


def load_plugin_module(record: PluginRecord) -> types.ModuleType:
    """検査済み dir fd から plugin を source として読み、新しい module に exec して返す。

    hash を照合した同じ bytes を compile する。exec 前に decode した行を linecache に
    mtime なしで登録し、traceback がディスクを読み直さないようにする。module は
    `sys.modules` に登録しない。失敗は `PluginLoadError`。
    """
    plugin_bytes = read_plugin_file_bounded(record.dir_fd, "plugin.py")
    config_bytes = read_plugin_file_bounded(record.dir_fd, "config.yaml")
    if version_store.content_hash_bytes(plugin_bytes, config_bytes) != record.content_hash:
        raise PluginLoadError("content_hash_mismatch", record.real_dir)
    try:
        text = plugin_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PluginLoadError("decode_failed", f"{record.real_dir}: {exc}") from exc
    path = os.path.join(record.real_dir, "plugin.py")
    try:
        code = compile(plugin_bytes, path, "exec", dont_inherit=True)
    except BaseException as exc:  # noqa: BLE001  SyntaxError 等も候補側の失敗
        raise PluginLoadError("exec_failed", f"compile: {type(exc).__name__}") from exc
    linecache.cache[path] = (len(text), None, text.splitlines(True), path)
    module = types.ModuleType(record.module_name)
    spec = ModuleSpec(record.module_name, None, origin=path)
    spec.has_location = True
    module.__spec__ = spec
    module.__file__ = path
    module.__loader__ = None
    module.__package__ = ""
    module.__cached__ = None  # type: ignore[attr-defined]
    try:
        exec(code, module.__dict__)  # noqa: S102
    except BaseException as exc:  # noqa: BLE001  top-level の失敗は候補側
        raise PluginLoadError("exec_failed", f"{type(exc).__name__}: {exc}") from exc
    return module


__all__ = [
    "ATTESTED_FIELDS",
    "CANDIDATE_ISOLATION_REASONS",
    "MAX_PLUGIN_FILE_BYTES",
    "PLUGIN_LOAD_REASONS",
    "WORKER_SANDBOX_REASONS",
    "IsolatedWorker",
    "IsolationStageError",
    "PluginLoadError",
    "PluginRecord",
    "PreparedIsolation",
    "isolate",
    "isolate_for_selftest",
    "join_anonymous_session_keyring",
    "load_plugin_module",
    "prepare_isolation",
    "read_plugin_file_bounded",
    "run_isolation_stage",
]
