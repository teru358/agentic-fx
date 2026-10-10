"""操作 API の principal 鍵: 生成・検査・rotate / revoke と `.ready` commit marker。

鍵 dir は ``~/.config/agentic-fx/api/<instance_id>/`` (0700)、principal ごとの鍵と
``.ready`` は 0600。``.ready`` は登録済み principal ごとの generation・状態・digest を
持つ鍵集合の commit marker で、実体と一致しない状態は「どの世代も ready ではない」と
読み、API を起動しない。自動補完は初回 init (``.ready`` 未作成) だけ。

manifest は v2 (``{"version": 2, "principals": {名前: entry}}``) で、map に無い既知
principal は未登録。旧形式 v1 は固定の operator / approver だけを持つものとして読み、
書換えは最初に成功した鍵変更 (enroll / rotate / revoke) のときだけ v2 で行う。

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

from .contracts import INITIAL_PRINCIPALS, Principal

READY_NAME = ".ready"
READY_VERSION = 2
_LEGACY_READY_VERSION = 1
_KEY_OPS = ("enroll", "rotate", "revoke")
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


def check_key_dir_usable(directory: Path, *, root: Path) -> None:
    """鍵 dir が置き場所・所有者・mode の検査を通り、開けることを確かめる (中身は読まない)。"""
    check_token_dir(directory, root=root)
    os.close(_open_dir(Path(directory)))


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
    if not isinstance(ready, dict) or not isinstance(principals, dict) or not principals:
        # 登録が 1 件も無い manifest で API を開くと、誰も認証できない状態を黙って許す。
        raise KeySetError("ready_invalid")
    version = ready.get("version")
    known = {p.value for p in Principal}
    if type(version) is not int:
        raise KeySetError("ready_invalid")
    if version == _LEGACY_READY_VERSION:
        # 旧形式は enum ではなく固定集合として読む (後から増えた principal を含めない)。
        if set(principals) != {p.value for p in INITIAL_PRINCIPALS}:
            raise KeySetError("ready_invalid")
    elif version != READY_VERSION or not set(principals) <= known:
        raise KeySetError("ready_invalid")
    for entry in principals.values():
        if not _entry_valid(entry):
            raise KeySetError("ready_invalid")
    return ready


def _is_hex_digest(value: object) -> bool:
    return (isinstance(value, str) and len(value) == _TOKEN_HEX_LEN
            and all(c in "0123456789abcdef" for c in value))


def _entry_valid(entry: object) -> bool:
    """state ごとの entry の形。active は digest、revoked は null、preparing は op を持つ。"""
    if not isinstance(entry, dict) or type(entry.get("generation")) is not int:
        return False
    state = entry.get("state")
    if state == "active":
        return _is_hex_digest(entry.get("sha256"))
    if state == "revoked":
        # digest を持つ revoked は「旧鍵がまだ有効かもしれない」状態と区別できない。
        return "sha256" in entry and entry["sha256"] is None
    if state == "preparing":
        return entry.get("op") in _KEY_OPS
    return False


def _registered(ready: dict) -> list[Principal]:
    """manifest に載っている principal (enum の順)。"""
    return [p for p in Principal if p.value in ready["principals"]]


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
        for principal in _registered(ready):
            entry = ready["principals"][principal.value]
            if entry["state"] == "preparing":
                raise KeySetError("ready_not_committed")
            if entry["state"] == "revoked":
                _check_tombstone(dir_fd, principal)
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
            if not hmac.compare_digest(digest, entry["sha256"]):
                raise KeySetError("digest_mismatch")
            digests[principal] = digest
        return KeySet(digests, frozenset(disabled))
    finally:
        os.close(dir_fd)


def _check_tombstone(dir_fd: int, principal: Principal) -> None:
    """revoked の鍵 file が tombstone であることを内容で確かめる。

    manifest だけが revoked で旧鍵が残っている状態は、鍵集合と実体の不一致として扱う。
    """
    try:
        raw = _read_checked(dir_fd, key_file_name(principal))
    except FileNotFoundError:
        raise KeySetError("key_missing") from None
    except OSError:
        raise KeySetError("ready_invalid") from None
    if not hmac.compare_digest(raw, _TOMBSTONE):
        raise KeySetError("ready_invalid")


def enrolled_key_matches(directory: Path, principal: Principal) -> bool:
    """manifest が active で、鍵 file の digest がその entry と一致すれば True。

    未登録・preparing・revoked は False (enroll が完結させるか断る)。active なのに鍵 file が
    無い・検査を通らない・digest が違えば :class:`KeySetError`。active への enroll は無変更で
    成功するので、ここで止めないと認証できない鍵のまま有効化が進む。
    """
    dir_fd = _open_dir(Path(directory))
    try:
        ready = _read_ready(dir_fd)
        entry = ready["principals"].get(principal.value)
        if entry is None or entry["state"] != "active":
            return False
        try:
            raw = _read_checked(dir_fd, key_file_name(principal))
        except FileNotFoundError:
            raise KeySetError("key_missing") from None
        except OSError:
            raise KeySetError("key_file_invalid") from None
        try:
            digest = _digest(_parse_token(raw))
        except (ValueError, UnicodeDecodeError):
            raise KeySetError("key_file_invalid") from None
        finally:
            del raw
        if not hmac.compare_digest(digest, entry["sha256"]):
            raise KeySetError("digest_mismatch")
        return True
    finally:
        os.close(dir_fd)


def read_token(directory: Path, principal: Principal) -> str:
    """client が鍵を読む。同じ検査を通し、生成はしない。"""
    dir_fd = _open_dir(Path(directory))
    try:
        ready = _read_ready(dir_fd)
        entry = ready["principals"].get(principal.value)
        if entry is None:
            raise KeySetError("not_enrolled")
        if entry["state"] != "active":
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
        for principal in INITIAL_PRINCIPALS:
            name = key_file_name(principal)
            if _create_exclusive(dir_fd, name, (new_token() + "\n").encode(), hook, "key"):
                created.append(principal)
        os.fsync(dir_fd)
        hook("keys_dir_synced")
        principals = {}
        for principal in INITIAL_PRINCIPALS:
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


# op ごとに、準備状態へ進めてよい元の状態。None は未登録。
_BEGIN_FROM = {"enroll": (None,), "rotate": ("active", "revoked"), "revoke": ("active",)}
# 既にその状態なら何も書かずに成功する (同じ command の結果が既に commit 済み)。
_ALREADY_DONE = {"enroll": "active", "revoke": "revoked"}


def _begin(directory: Path, principal: Principal, op: str,
           hook: Hook) -> tuple[int, dict, int] | None:
    dir_fd = _open_dir(Path(directory))
    try:
        ready = _read_ready(dir_fd)
        principals = ready["principals"]
        # 準備状態の残骸を完結できるのは、同じ principal への同じ command だけ。
        # 別の command は残骸の上に重ねず、dir を変えずに断る。
        for name, other in principals.items():
            if other["state"] == "preparing" and (name, other["op"]) != (principal.value, op):
                raise KeySetError("invalid_state")
        entry = principals.get(principal.value)
        state = None if entry is None else entry["state"]
        if state == "preparing":
            generation = entry["generation"]
        elif op in _ALREADY_DONE and state == _ALREADY_DONE[op]:
            os.close(dir_fd)
            return None
        elif state in _BEGIN_FROM[op]:
            generation = 1 if entry is None else entry["generation"] + 1
            principals[principal.value] = {"generation": generation, "state": "preparing",
                                           "op": op}
            _replace(dir_fd, READY_NAME, _ready_bytes(principals), hook, "preparing")
        elif state is None:
            # 未登録への rotate / revoke は再実行しても通らない。enroll を促せる code にする。
            raise KeySetError("not_enrolled")
        else:
            raise KeySetError("invalid_state")
        return dir_fd, principals, generation
    except BaseException:
        os.close(dir_fd)
        raise


def _commit_new_key(dir_fd: int, principals: dict, principal: Principal, generation: int,
                    hook: Hook) -> None:
    """新鍵を一時 file → fsync → rename → dir fsync で置き、最後に ``.ready`` を commit。"""
    token = new_token()
    _replace(dir_fd, key_file_name(principal), (token + "\n").encode(), hook, "key")
    principals[principal.value] = {"generation": generation, "state": "active",
                                   "sha256": _digest(token)}
    del token
    _replace(dir_fd, READY_NAME, _ready_bytes(principals), hook, "ready")


def enroll(directory: Path, principal: Principal, *, hook: Hook = _noop) -> bool:
    """``未登録 → preparing(op=enroll, generation=1) → active(1)``。

    既に active なら何も書かずに False。revoked は invalid_state (復活は rotate)。
    途中で落ちたら同じ command で完了させる。他の principal の鍵と entry は変えない。
    """
    begun = _begin(directory, principal, "enroll", hook)
    if begun is None:
        return False
    dir_fd, principals, generation = begun
    try:
        _commit_new_key(dir_fd, principals, principal, generation, hook)
    finally:
        os.close(dir_fd)
    return True


def rotate(directory: Path, principal: Principal, *, hook: Hook = _noop) -> None:
    """``active(g) → preparing(g+1) → active(g+1)``。途中で落ちたら同じ command で完了。"""
    begun = _begin(directory, principal, "rotate", hook)
    assert begun is not None
    dir_fd, principals, generation = begun
    try:
        _commit_new_key(dir_fd, principals, principal, generation, hook)
    finally:
        os.close(dir_fd)


def revoke(directory: Path, principal: Principal, *, hook: Hook = _noop) -> None:
    """``active(g) → preparing(g+1) → revoked(g+1)``。既に revoked なら無変更。"""
    begun = _begin(directory, principal, "revoke", hook)
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
    """``afx keys init|enroll|rotate|revoke``。instance lock を取れる停止中だけ実行する。"""
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
        elif action == "enroll":
            if enroll(directory, Principal(principal)):
                print(f"{principal} の鍵を登録しました ({directory})")
            else:
                print(f"{principal} は登録済みです (無変更)")
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
        if err.code == "not_enrolled":
            print(f"afx keys: {principal} は未登録です (not_enrolled)。"
                  f"先に `afx keys enroll {principal}` を実行してください", file=sys.stderr)
            return 1
        print(f"afx keys: 失敗しました ({err.code})。同じ command の再実行で準備状態から"
              "完了させてください", file=sys.stderr)
        return 1
    finally:
        lock.close()
