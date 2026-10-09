"""改善 mission の CLI 部分木に掛ける 2 枚目の Landlock domain (nested)。

launcher が CLI を exec する直前に、自分の process の全 thread に掛ける。
outer Landlock (FS) の内側に、network と scope だけの domain を重ねる:

- network: BIND_TCP と CONNECT_TCP を handled にして規則を 1 本も足さない。
  明示 bind と直接の TCP connect は EACCES。外向き connect は seccomp の
  user-notification で worker の supervisor が代行するので、部分木自身の
  connect が通る必要はない。bind 無しの listen は Landlock では閉じないため
  seccomp 側で拒否する。
- scope: ABSTRACT_UNIX_SOCKET と SIGNAL。部分木の外への signal と、domain の
  外が bind した abstract UDS への接続を拒否する。外側 (worker の domain) から
  部分木への signal / killpg は親 domain → 子 domain なので通る。
- filesystem: handled にしない。FS の allowlist を outer の 1 か所に保つため。

ABI 8 (restrict_self の TSYNC) 未満は fallback 無しで拒否する。thread を
1 本ずつ数えて掛ける経路を作ると、数えた後に増えた thread が domain の外に
残りうるため。

launcher は outer Landlock の下で `python -c` として動くので、本 module は
標準ライブラリだけで書き、他の agentic_fx module を import しない (import
しても source を埋め込んでも同じに動く)。`from __future__ import annotations`
は付けない: dataclass が sys.modules 上の module を引くので、登録の無い
namespace への exec で import に失敗するため。x86_64 専用で、他の arch では
ABI 0 として fail closed する。
"""
import ctypes
import hashlib
import json
import os
import platform
from dataclasses import dataclass

# x86_64 の landlock syscall 番号
_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
_PR_SET_NO_NEW_PRIVS = 38

_ACCESS_NET_BIND_TCP = 1 << 0
_ACCESS_NET_CONNECT_TCP = 1 << 1
_SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0
_SCOPE_SIGNAL = 1 << 1
_RESTRICT_SELF_TSYNC = 1 << 3

# handled_access_fs / handled_access_net / scoped の 3 欄
_ATTR_SIZE = 24

MIN_ABI = 8

# 失敗の固定 reason。plugin worker の Landlock と同じ語彙を使う。TSYNC が
# 使えない kernel は ABI 8 未満なので landlock_abi_too_old に含まれる。
NESTED_REASONS = (
    "landlock_abi_too_old",
    "landlock_create_failed",
    "no_new_privs_failed",
    "landlock_restrict_failed",
)


class NestedLandlockError(Exception):
    """nested domain の組み立て・適用の失敗。`reason` は `NESTED_REASONS` の
    どれか、`detail` は技術ログ用の自由文。"""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class NestedRuleset:
    """nested domain の正規形。`rules` は規則の列で、この domain では常に空。"""

    handled_fs: int
    handled_net: int
    handled_scope: int
    rules: tuple
    restrict_flags: int
    min_abi: int


NESTED_RULESET = NestedRuleset(
    handled_fs=0,
    handled_net=_ACCESS_NET_BIND_TCP | _ACCESS_NET_CONNECT_TCP,
    handled_scope=_SCOPE_ABSTRACT_UNIX_SOCKET | _SCOPE_SIGNAL,
    rules=(),
    restrict_flags=_RESTRICT_SELF_TSYNC,
    min_abi=MIN_ABI,
)


def ruleset_for_abi(abi: int) -> NestedRuleset:
    """ABI から ruleset を決める。8 未満は `landlock_abi_too_old`。8 を超える
    ABI でも同じ ruleset を返す (新しい bit を勝手に足すと fingerprint が
    host ごとに変わるため)。"""
    if abi < MIN_ABI:
        raise NestedLandlockError("landlock_abi_too_old", f"abi={abi}")
    return NESTED_RULESET


def canonical_form(ruleset: NestedRuleset) -> bytes:
    """fingerprint 材料の正規形 (key 順固定、空白なしの JSON)。"""
    obj = {
        "handled_fs": ruleset.handled_fs,
        "handled_net": ruleset.handled_net,
        "handled_scope": ruleset.handled_scope,
        "rules": [list(r) for r in ruleset.rules],
        "restrict_flags": ruleset.restrict_flags,
        "min_abi": ruleset.min_abi,
    }
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("ascii")


def ruleset_digest(ruleset: NestedRuleset) -> str:
    """正規形の SHA-256 (hex)。"""
    return hashlib.sha256(canonical_form(ruleset)).hexdigest()


_LIBC: ctypes.CDLL | None = None


def _libc() -> ctypes.CDLL:
    """libc は 1 度だけ開く。prctl の引数は syscall と同じく幅を明示して渡す。"""
    global _LIBC
    if _LIBC is None:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        libc.prctl.restype = ctypes.c_int
        libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                               ctypes.c_ulong, ctypes.c_ulong]
        _LIBC = libc
    return _LIBC


def probe_abi() -> int:
    """kernel の Landlock ABI 版。非 x86_64・Landlock 無効・syscall 失敗は 0。"""
    if platform.machine() != "x86_64":
        return 0
    version = _libc().syscall(
        ctypes.c_long(_SYS_LANDLOCK_CREATE_RULESET), None, ctypes.c_size_t(0),
        ctypes.c_uint32(_LANDLOCK_CREATE_RULESET_VERSION))
    return int(version) if version > 0 else 0


class _Attr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64),
                ("handled_access_net", ctypes.c_uint64),
                ("scoped", ctypes.c_uint64)]


def apply_nested_landlock() -> NestedRuleset:
    """呼んだ process の全 thread に nested domain を掛ける (不可逆、子に継承)。

    順序: 実 ABI の判定 → ruleset 作成 → no_new_privs → restrict_self(TSYNC)。
    どの段の失敗も `NestedLandlockError` で、flag を落として再試行しない。"""
    ruleset = ruleset_for_abi(probe_abi())
    libc = _libc()
    attr = _Attr(ruleset.handled_fs, ruleset.handled_net, ruleset.handled_scope)
    rfd = libc.syscall(
        ctypes.c_long(_SYS_LANDLOCK_CREATE_RULESET), ctypes.byref(attr),
        ctypes.c_size_t(_ATTR_SIZE), ctypes.c_uint32(0))
    if rfd < 0:
        raise NestedLandlockError(
            "landlock_create_failed", f"errno={ctypes.get_errno()}")
    try:
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise NestedLandlockError(
                "no_new_privs_failed", f"errno={ctypes.get_errno()}")
        rc = libc.syscall(ctypes.c_long(_SYS_LANDLOCK_RESTRICT_SELF),
                          ctypes.c_int(rfd),
                          ctypes.c_uint32(ruleset.restrict_flags))
        if rc != 0:
            raise NestedLandlockError(
                "landlock_restrict_failed",
                f"errno={ctypes.get_errno()} flags={ruleset.restrict_flags}")
    finally:
        os.close(rfd)
    return ruleset

