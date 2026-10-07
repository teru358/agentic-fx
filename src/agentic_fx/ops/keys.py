"""操作 API の principal 鍵: 生成・検査・rotate / revoke と `.ready` commit marker。

鍵 dir は ``~/.config/agentic-fx/api/<instance_id>/`` (0700)、principal ごとの鍵と
``.ready`` は 0600。``.ready`` は principal ごとの generation・状態・digest を持つ
鍵集合の commit marker で、実体と一致しない状態は「どの世代も ready ではない」と
読み、API を起動しない。自動補完は初回 init (``.ready`` 未作成) だけ。

server は鍵を読んだら平文を捨てて SHA-256 digest だけを保持し、定時間比較する。
この module は ``.env`` を読まない。
"""
from __future__ import annotations

import contextlib
import errno
import hashlib
import hmac
import json
import os
import secrets
import stat
import sys
import sysconfig
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .contracts import Principal

READY_NAME = ".ready"
READY_VERSION = 1
_TOMBSTONE = b"revoked\n"
_TOKEN_HEX_LEN = 64

Hook = Callable[[str], None]


class KeySetError(Exception):
    """鍵集合を使えない。``code`` は固定語で、鍵の内容は載せない。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def key_file_name(principal: Principal) -> str:
    return f"{principal.value}.token"


def instance_id(root: Path) -> str:
    """root の実体 path から導く導入インスタンスの識別子 (root ごとに鍵を分ける)。"""
    return hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:16]


def key_dir(root: Path) -> Path:
    return Path.home() / ".config" / "agentic-fx" / "api" / instance_id(root)


def new_token() -> str:
    """256 bit の CSPRNG 出力。service の初回補完と ``afx keys`` が共有する。"""
    return secrets.token_hex(32)


def _digest(token: str | bytes) -> str:
    data = token.encode() if isinstance(token, str) else token
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------ token dir 検査

def _sandbox_reachable(root: Path) -> list[Path]:
    """隔離 worker の allowlist が覆う場所と、鍵を置いてはいけない system 領域。"""
    from agentic_fx.core import landlock
    code_root = Path(__file__).resolve().parents[1]
    paths = sysconfig.get_paths()
    candidates = [Path("/etc"), Path("/proc"), Path("/dev"), Path("/sys"),
                  Path(sys.prefix), Path(sys.base_prefix), code_root,
                  Path(root) / "plugins"]
    candidates += [Path(paths[k]) for k in ("stdlib", "platstdlib", "purelib", "platlib")
                   if k in paths]
    candidates += landlock.system_dirs()
    with contextlib.suppress(Exception):
        candidates += landlock.runtime_subtree(code_root)
    return [p.resolve() for p in candidates]


def check_token_dir(directory: Path, *, root: Path) -> None:
    """鍵 dir が隔離 worker から読める場所や system 領域にあれば拒否する。"""
    real = Path(directory).resolve()
    for forbidden in _sandbox_reachable(root):
        if real == forbidden or forbidden in real.parents:
            raise KeySetError("token_dir_in_sandbox")


# ------------------------------------------------------------ 低水準 I/O

def _noop(_step: str) -> None:
    return None


def _open_dir(directory: Path) -> int:
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        raise KeySetError("not_initialized") from None
    except OSError:
        raise KeySetError("key_dir_invalid") from None
    st = os.fstat(fd)
    if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
        os.close(fd)
        raise KeySetError("key_dir_invalid")
    return fd


def _read_checked(dir_fd: int, name: str) -> bytes:
    """``O_NOFOLLOW`` で開き、通常 file・所有者・0600 を確かめてから読む。

    不正なら ``OSError(EPERM)``、無ければ ``FileNotFoundError``。
    """
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NOCTTY | os.O_CLOEXEC,
                 dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                or stat.S_IMODE(st.st_mode) != 0o600):
            raise OSError(errno.EPERM, "key file check failed")
        chunks = []
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            chunks.append(chunk)
            if sum(map(len, chunks)) > 65536:
                raise OSError(errno.EFBIG, "key file too large")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _write_temp(dir_fd: int, name: str, data: bytes) -> str:
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                 | os.O_CLOEXEC, 0o600, dir_fd=dir_fd)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    return temporary


def _replace(dir_fd: int, name: str, data: bytes, hook: Hook, step: str) -> None:
    """一時 file + fsync → atomic rename → dir fsync。各点の後で hook を呼ぶ。"""
    temporary = _write_temp(dir_fd, name, data)
    try:
        hook(f"{step}:written")
        os.rename(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=dir_fd)
        raise
    hook(f"{step}:renamed")
    os.fsync(dir_fd)
    hook(f"{step}:dir_synced")


def _create_exclusive(dir_fd: int, name: str, data: bytes, hook: Hook, step: str) -> bool:
    """無いときだけ完全な内容で作る (link は既存を上書きしない)。作ったら True。"""
    temporary = _write_temp(dir_fd, name, data)
    try:
        hook(f"{step}_written:{name.split('.')[0]}")
        try:
            os.link(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd,
                    follow_symlinks=False)
        except FileExistsError:
            return False
        hook(f"{step}_linked:{name.split('.')[0]}")
        return True
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=dir_fd)


def _parse_token(raw: bytes) -> str:
    text = raw.decode("ascii", errors="strict").strip()
    if len(text) != _TOKEN_HEX_LEN or any(c not in "0123456789abcdef" for c in text):
        raise ValueError("malformed token")
    return text


def _read_ready(dir_fd: int) -> dict:
    try:
        raw = _read_checked(dir_fd, READY_NAME)
    except FileNotFoundError:
        raise KeySetError("not_initialized") from None
    except OSError:
        raise KeySetError("ready_invalid") from None
    try:
        ready = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise KeySetError("ready_invalid") from None
    principals = ready.get("principals") if isinstance(ready, dict) else None
    if (not isinstance(ready, dict) or ready.get("version") != READY_VERSION
            or not isinstance(principals, dict)
            or set(principals) != {p.value for p in Principal}):
        raise KeySetError("ready_invalid")
    for entry in principals.values():
        if (not isinstance(entry, dict) or type(entry.get("generation")) is not int
                or entry.get("state") not in ("active", "preparing", "revoked")):
            raise KeySetError("ready_invalid")
    return ready


def _ready_bytes(principals: dict) -> bytes:
    return (json.dumps({"version": READY_VERSION, "principals": principals},
                       sort_keys=True, indent=1) + "\n").encode()


# ------------------------------------------------------------ 読込 (server)

@dataclass(slots=True)
class KeySet:
    """digest だけを持つ鍵集合。平文は保持しない。"""
    _digests: dict = field(default_factory=dict)
    disabled: frozenset = frozenset()

    def __repr__(self) -> str:
        return f"KeySet(active={sorted(p.value for p in self._digests)})"

    def authenticate(self, token: str | bytes | None) -> Principal | None:
        """全 principal と定時間比較する (一致の有無で比較回数を変えない)。"""
        presented = _digest(token or b"\0")
        found = None
        for principal, digest in self._digests.items():
            if hmac.compare_digest(presented, digest):
                found = principal
        return found


def load_keyset(directory: Path) -> KeySet:
    """起動時の読込。``.ready`` と実体が一致しなければ :class:`KeySetError`。

    mode / 所有者 / symlink が不正な鍵 file は読まずにその principal だけ無効にする。
    """
    dir_fd = _open_dir(Path(directory))
    try:
        ready = _read_ready(dir_fd)
        digests: dict[Principal, str] = {}
        disabled: set[Principal] = set()
        for principal in Principal:
            entry = ready["principals"][principal.value]
            if entry["state"] == "preparing":
                raise KeySetError("ready_not_committed")
            if entry["state"] == "revoked":
                disabled.add(principal)
                continue
            try:
                raw = _read_checked(dir_fd, key_file_name(principal))
            except FileNotFoundError:
                raise KeySetError("key_missing") from None
            except OSError:
                disabled.add(principal)
                continue
            try:
                token = _parse_token(raw)
            except (ValueError, UnicodeDecodeError):
                raise KeySetError("digest_mismatch") from None
            digest = _digest(token)
            del token, raw
            if not hmac.compare_digest(digest, str(entry.get("sha256", ""))):
                raise KeySetError("digest_mismatch")
            digests[principal] = digest
        return KeySet(digests, frozenset(disabled))
    finally:
        os.close(dir_fd)


def read_token(directory: Path, principal: Principal) -> str:
    """client が鍵を読む。同じ検査を通し、生成はしない。"""
    dir_fd = _open_dir(Path(directory))
    try:
        ready = _read_ready(dir_fd)
        if ready["principals"][principal.value]["state"] != "active":
            raise KeySetError("ready_not_committed")
        try:
            return _parse_token(_read_checked(dir_fd, key_file_name(principal)))
        except FileNotFoundError:
            raise KeySetError("key_missing") from None
        except (OSError, ValueError, UnicodeDecodeError):
            raise KeySetError("key_file_invalid") from None
    finally:
        os.close(dir_fd)


# ------------------------------------------------------------ 変更 (停止中だけ)

def _make_dirs(directory: Path) -> None:
    """dir を 0700 で作る。既存の dir の mode は直さず、検査で落とす。"""
    missing = []
    current = directory
    while not current.exists():
        missing.append(current)
        current = current.parent
    for path in reversed(missing):
        with contextlib.suppress(FileExistsError):
            os.mkdir(path, 0o700)
            os.chmod(path, 0o700)


def initialize(directory: Path, *, root: Path, hook: Hook = _noop) -> list[Principal]:
    """初回 init。``.ready`` が無いときだけ欠けた鍵を作り、最後に ``.ready`` を commit。

    既存の鍵 file は内容も mtime も変えない。``.ready`` があれば何も作らず、
    実体と一致しなければ :class:`KeySetError`。作った principal を返す。
    """
    directory = Path(directory)
    check_token_dir(directory, root=root)
    _make_dirs(directory)
    dir_fd = _open_dir(directory)
    try:
        try:
            _read_checked(dir_fd, READY_NAME)
        except FileNotFoundError:
            pass
        except OSError:
            raise KeySetError("ready_invalid") from None
        else:
            os.close(dir_fd)
            dir_fd = -1
            load_keyset(directory)
            return []
        created = []
        for principal in Principal:
            name = key_file_name(principal)
            if _create_exclusive(dir_fd, name, (new_token() + "\n").encode(), hook, "key"):
                created.append(principal)
        os.fsync(dir_fd)
        hook("keys_dir_synced")
        principals = {}
        for principal in Principal:
            try:
                token = _parse_token(_read_checked(dir_fd, key_file_name(principal)))
            except (OSError, ValueError, UnicodeDecodeError):
                raise KeySetError("key_file_invalid") from None
            principals[principal.value] = {"generation": 1, "state": "active",
                                           "sha256": _digest(token)}
        _replace(dir_fd, READY_NAME, _ready_bytes(principals), _dotless(hook), "ready")
        return created
    finally:
        if dir_fd >= 0:
            os.close(dir_fd)


def _dotless(hook: Hook) -> Hook:
    # init の hook 名は "ready_written" 形式 (rotate / revoke は "ready:written")。
    return lambda step: hook(step.replace(":", "_"))


def ensure_initialized(directory: Path, *, root: Path) -> None:
    """service 起動時の補完。``.ready`` 未作成のときだけ init と同じ関数で作る。"""
    initialize(directory, root=root)


def _begin(directory: Path, principal: Principal, op: str, hook: Hook,
           allowed: tuple[str, ...]) -> tuple[int, dict, int] | None:
    dir_fd = _open_dir(Path(directory))
    try:
        ready = _read_ready(dir_fd)
        principals = ready["principals"]
        entry = principals[principal.value]
        state = entry["state"]
        if op == "revoke" and state == "revoked":
            os.close(dir_fd)
            return None
        if state == "preparing":
            if entry.get("op") != op and op != "revoke":
                raise KeySetError("invalid_state")
            generation = entry["generation"]
        elif state in allowed:
            generation = entry["generation"] + 1
            principals[principal.value] = {"generation": generation, "state": "preparing",
                                           "op": op}
            _replace(dir_fd, READY_NAME, _ready_bytes(principals), hook, "preparing")
        else:
            raise KeySetError("invalid_state")
        if entry.get("op") != op:
            principals[principal.value] = {"generation": generation, "state": "preparing",
                                           "op": op}
        return dir_fd, principals, generation
    except BaseException:
        os.close(dir_fd)
        raise


def rotate(directory: Path, principal: Principal, *, hook: Hook = _noop) -> None:
    """``active(g) → preparing(g+1) → active(g+1)``。途中で落ちたら同じ command で完了。"""
    begun = _begin(directory, principal, "rotate", hook, ("active", "revoked"))
    assert begun is not None
    dir_fd, principals, generation = begun
    try:
        token = new_token()
        _replace(dir_fd, key_file_name(principal), (token + "\n").encode(), hook, "key")
        principals[principal.value] = {"generation": generation, "state": "active",
                                       "sha256": _digest(token)}
        del token
        _replace(dir_fd, READY_NAME, _ready_bytes(principals), hook, "ready")
    finally:
        os.close(dir_fd)


def revoke(directory: Path, principal: Principal, *, hook: Hook = _noop) -> None:
    """``active(g) → preparing(g+1) → revoked(g+1)``。既に revoked なら無変更。"""
    begun = _begin(directory, principal, "revoke", hook, ("active",))
    if begun is None:
        return
    dir_fd, principals, generation = begun
    try:
        _replace(dir_fd, key_file_name(principal), _TOMBSTONE, hook, "tombstone")
        principals[principal.value] = {"generation": generation, "state": "revoked",
                                       "sha256": None}
        _replace(dir_fd, READY_NAME, _ready_bytes(principals), hook, "ready")
    finally:
        os.close(dir_fd)


# ------------------------------------------------------------ afx keys

def run_keys_command(root: Path, action: str, principal: str | None = None) -> int:
    """``afx keys init|rotate|revoke``。instance lock を取れる停止中だけ実行する。"""
    from agentic_fx.store.instance_lock import InstanceAlreadyRunning, acquire_instance_lock
    root = Path(root)
    directory = key_dir(root)
    try:
        check_token_dir(directory, root=root)
    except KeySetError as err:
        print(f"afx keys: 鍵 dir を使えません ({err.code}): {directory}", file=sys.stderr)
        return 1
    try:
        lock = acquire_instance_lock(root / "data")
    except InstanceAlreadyRunning:
        print("afx keys: サービスが稼働中です。停止してから実行してください (無変更)",
              file=sys.stderr)
        return 1
    try:
        if action == "init":
            created = initialize(directory, root=root)
            if created:
                print(f"鍵を作成しました: {', '.join(p.value for p in created)} ({directory})")
            else:
                print(f"鍵は初期化済みです ({directory})")
        elif action == "rotate":
            rotate(directory, Principal(principal))
            print(f"{principal} の鍵を交換しました。daemon を起動して旧鍵が拒否されることを確認してください")
        elif action == "revoke":
            revoke(directory, Principal(principal))
            print(f"{principal} の鍵を失効させました")
        else:
            return 2
        return 0
    except KeySetError as err:
        print(f"afx keys: 失敗しました ({err.code})。同じ command の再実行で準備状態から"
              "完了させてください", file=sys.stderr)
        return 1
    finally:
        lock.close()
