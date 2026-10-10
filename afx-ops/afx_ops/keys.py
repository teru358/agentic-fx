"""自分の principal の鍵だけを読む (書込みは持たない)。

鍵 file と ``.ready`` manifest の絶対 path は機能別設定から受け取る。本体の鍵 dir の
導出規則は写さない。読む順は「dir を ``O_DIRECTORY|O_NOFOLLOW`` で開いて所有者・0700 を
確かめる → manifest を読んで自分の entry が active であることを確かめる → 自分の鍵 file
を ``O_NOFOLLOW`` で開いて通常 file・所有者・0600 を確かめる → 鍵の digest を manifest
と照合する」。manifest が許さない限り鍵 file は開かない。

失敗は ``KeyReadError`` の固定 code だけで表し、鍵の内容や path を例外文に載せない。
隔離 (Landlock) の前に読み終える前提なので、隔離後に鍵 dir を開く経路は無い。
"""
from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import stat
from pathlib import Path

READY_NAME = ".ready"
READY_VERSION = 2
LEGACY_READY_VERSION = 1
# manifest に現れてよい principal (本体の認可表と同じ集合)。
KNOWN_PRINCIPALS = frozenset({"operator", "approver", "ops-bot", "ops-web", "ops-notify"})
# 旧形式 v1 が持つ固定集合。v1 には ops の principal は居ない。
LEGACY_PRINCIPALS = frozenset({"operator", "approver"})
OPS_PRINCIPALS = ("ops-bot", "ops-web", "ops-notify")
_KEY_OPS = ("enroll", "rotate", "revoke")
_TOKEN_HEX_LEN = 64
_MAX_FILE = 65536

CODES = ("not_initialized", "key_dir_invalid", "ready_invalid", "not_enrolled",
         "ready_not_committed", "revoked", "key_missing", "key_file_invalid",
         "digest_mismatch", "key_path_mismatch")


class KeyReadError(Exception):
    """鍵を使えない。``code`` は固定語で、鍵の内容は載せない。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def key_file_name(principal: str) -> str:
    return f"{principal}.token"


def _open_dir(directory: Path) -> int:
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        raise KeyReadError("not_initialized") from None
    except OSError:
        raise KeyReadError("key_dir_invalid") from None
    st = os.fstat(fd)
    if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
        os.close(fd)
        raise KeyReadError("key_dir_invalid")
    return fd


def _read_checked(dir_fd: int, name: str) -> bytes:
    """``O_NOFOLLOW`` で開き、通常 file・所有者・0600 を確かめてから読む。

    不正なら ``OSError(EPERM)``、無ければ ``FileNotFoundError``。
    """
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NOCTTY | os.O_NONBLOCK
                 | os.O_CLOEXEC, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                or stat.S_IMODE(st.st_mode) != 0o600):
            raise OSError(errno.EPERM, "file check failed")
        chunks = []
        size = 0
        while chunk := os.read(fd, 4096):
            chunks.append(chunk)
            size += len(chunk)
            if size > _MAX_FILE:
                raise OSError(errno.EFBIG, "file too large")
        return b"".join(chunks)
    finally:
        os.close(fd)


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
        return "sha256" in entry and entry["sha256"] is None
    if state == "preparing":
        return entry.get("op") in _KEY_OPS
    return False


def _read_ready(dir_fd: int) -> dict:
    try:
        raw = _read_checked(dir_fd, READY_NAME)
    except FileNotFoundError:
        raise KeyReadError("not_initialized") from None
    except OSError:
        raise KeyReadError("ready_invalid") from None
    try:
        ready = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise KeyReadError("ready_invalid") from None
    principals = ready.get("principals") if isinstance(ready, dict) else None
    if not isinstance(principals, dict) or not principals:
        raise KeyReadError("ready_invalid")
    version = ready.get("version")
    if type(version) is not int:
        raise KeyReadError("ready_invalid")
    if version == LEGACY_READY_VERSION:
        # 旧形式は固定集合として読む (後から増えた principal を含めない)。
        if set(principals) != LEGACY_PRINCIPALS:
            raise KeyReadError("ready_invalid")
    elif version != READY_VERSION or not set(principals) <= KNOWN_PRINCIPALS:
        raise KeyReadError("ready_invalid")
    if not all(_entry_valid(entry) for entry in principals.values()):
        raise KeyReadError("ready_invalid")
    return principals


def _parse_token(raw: bytes) -> str:
    text = raw.decode("ascii", errors="strict").strip()
    if len(text) != _TOKEN_HEX_LEN or any(c not in "0123456789abcdef" for c in text):
        raise ValueError("malformed")
    return text


def read_token(principal: str, *, key_file: Path, ready_file: Path) -> str:
    """``principal`` の鍵を読む。manifest が active と言い digest が一致した場合だけ返す。"""
    if principal not in OPS_PRINCIPALS:
        raise KeyReadError("key_path_mismatch")
    key_file, ready_file = Path(key_file), Path(ready_file)
    # 設定の取り違えで他 principal の鍵 file を自分の entry で読まないこと。
    if (not key_file.is_absolute() or key_file.name != key_file_name(principal)
            or ready_file.name != READY_NAME or ready_file.parent != key_file.parent):
        raise KeyReadError("key_path_mismatch")
    dir_fd = _open_dir(key_file.parent)
    try:
        entry = _read_ready(dir_fd).get(principal)
        if entry is None:
            raise KeyReadError("not_enrolled")
        if entry["state"] == "revoked":
            raise KeyReadError("revoked")
        if entry["state"] != "active":
            raise KeyReadError("ready_not_committed")
        try:
            token = _parse_token(_read_checked(dir_fd, key_file.name))
        except FileNotFoundError:
            raise KeyReadError("key_missing") from None
        except (OSError, ValueError, UnicodeDecodeError):
            raise KeyReadError("key_file_invalid") from None
        digest = hashlib.sha256(token.encode()).hexdigest()
        if not hmac.compare_digest(digest, entry["sha256"]):
            raise KeyReadError("digest_mismatch")
        return token
    finally:
        os.close(dir_fd)
