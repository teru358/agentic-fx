"""web の公開範囲の設定作業。enable の前処理と reconfigure が共有する。

- 入力 file (`--config-file` / `--password-file`) の有界な読取り
- password の文法と生成
- 前処理 (0) 一時 file の除去、(a) secret、(b) config の atomic な反映
- 要約 1 行と status の公開範囲 1 行

ここは対話も systemd も知らない。固定 reason は `WebSetupError.reason` で返す。
"""
from __future__ import annotations

import base64
import os
import re
import secrets
import stat
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentic_fx.ops_setup.feature_config import (
    DEFAULT_USER,
    PASSWORD_MAX,
    PASSWORD_MIN,
    UNRESTRICTED,
    InputError,
    InterfaceLookupFailed,
    UnsafeConfigError,
    atomic,
    is_loopback,
    open_child,
    open_feature,
    validate,
    web_access_check,
    web_secret_state,
    write_web_config,
)

PASSWORD_NAME = "web.password"
SECRET_GUIDANCE_TEMPLATE = "secret の所有者・mode・型を直すか、停止中に対象を退避してから同じ {verb} を再実行する"
SECRET_ACTIONS = ("remove", "keep", "install", "absent")
CONFIG_FILE_MAX = 65536
_TEMP_PASSWORD = re.compile(r"\.web\.password\.[0-9a-f]{32}\.tmp\Z")
_TEMP_CONFIG = re.compile(r"\.ops\.toml\.[0-9a-f]{32}\.tmp\Z")
Hook = Callable[[str], None]


class WebSetupError(Exception):
    """固定 reason と rc を持つ失敗。診断文は持たない。"""

    def __init__(self, reason: str, rc: int = 1, guidance: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.rc = rc
        self.guidance = guidance


@dataclass(frozen=True)
class WebOptions:
    """`afx ops web enable` / `reconfigure` の option。重複は呼び出し側が数えて渡す。"""

    ask: bool = False
    non_interactive: bool = False
    config_file: str | None = None
    password_file: str | None = None
    secret_action: str | None = None
    duplicated: bool = False

    @property
    def flagless(self) -> bool:
        return not (self.ask or self.non_interactive)


def options_problem(options: WebOptions, *, reconfigure: bool) -> str | None:
    """表外の組み合わせなら固定 reason `setup_input_required`、許されるなら None。"""
    if options.duplicated:
        return "setup_input_required"
    if options.ask and (reconfigure or options.non_interactive or options.config_file is not None
                        or options.password_file is not None or options.secret_action is not None):
        return "setup_input_required"
    if not options.non_interactive:
        if (options.config_file, options.password_file, options.secret_action) != (None, None, None):
            return "setup_input_required"
        return None
    if options.config_file is None or options.secret_action not in SECRET_ACTIONS:
        return "setup_input_required"
    if (options.secret_action == "install") != (options.password_file is not None):
        return "setup_input_required"
    return None


# --- password -------------------------------------------------------------------------------

def check_password(value: bytes | str) -> str:
    """visible ASCII 8〜32 byte (`:` 可)。満たさなければ `secret_invalid`。"""
    raw = value.encode("utf-8", "surrogateescape") if isinstance(value, str) else value
    if not PASSWORD_MIN <= len(raw) <= PASSWORD_MAX or any(not 0x21 <= byte <= 0x7E for byte in raw):
        raise WebSetupError("secret_invalid")
    return raw.decode("ascii")


def generated_password_line(user: str, password: str) -> str:
    """生成した password の表示行。ユーザー名を添える (表示は 1 回だけ)。"""
    return f"ユーザー名: {user} / 生成した password (この表示は 1 回だけです): {password}"


def generate_password(token_bytes: Callable[[int], bytes] = secrets.token_bytes) -> str:
    """CSPRNG の 16 byte を padding なしの base64url (22 byte) にする。"""
    return base64.urlsafe_b64encode(token_bytes(16)).rstrip(b"=").decode("ascii")


# --- 入力 file --------------------------------------------------------------------------------

def _same_file(before: os.stat_result, after: os.stat_result) -> bool:
    return all(getattr(before, name) == getattr(after, name)
               for name in ("st_dev", "st_ino", "st_size", "st_uid", "st_mode"))


def _read_bounded(path: str, *, limit: int, reason: str, rc: int, accept_mode: Callable[[int], bool],
                  size_ok: Callable[[int], bool], after_open: Hook | None) -> bytes:
    """O_NOFOLLOW で 1 回だけ open し、fd の fstat と有界な read 1 回で読む。path は再 open しない。"""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY)
    except (OSError, ValueError):
        raise WebSetupError(reason, rc) from None
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or not accept_mode(stat.S_IMODE(before.st_mode)) or not size_ok(before.st_size)):
            raise WebSetupError(reason, rc)
        if after_open is not None:
            after_open("input_opened")
        data = os.read(fd, limit)
        after = os.fstat(fd)
        if len(data) != before.st_size or not _same_file(before, after):
            raise WebSetupError(reason, rc)
        return data
    except OSError:
        raise WebSetupError(reason, rc) from None
    finally:
        os.close(fd)


def read_config_file(path: str, *, after_open: Hook | None = None) -> dict[str, Any]:
    """`--config-file`: 実効 uid 所有の regular file、group / other 書込み不可、1〜65536 byte の TOML。"""
    data = _read_bounded(path, limit=CONFIG_FILE_MAX + 1, reason="setup_input_invalid", rc=2,
                         accept_mode=lambda mode: mode & 0o022 == 0,
                         size_ok=lambda size: 1 <= size <= CONFIG_FILE_MAX, after_open=after_open)
    try:
        parsed = tomllib.loads(data.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError):
        raise WebSetupError("setup_input_invalid", 2) from None
    return parsed


def read_password_file(path: str, *, after_open: Hook | None = None) -> str:
    """`--password-file`: 実効 uid 所有の regular file、mode 0600 ちょうど。8〜32 byte、末尾 LF 1 個だけ許す。"""
    data = _read_bounded(path, limit=PASSWORD_MAX + 2, reason="secret_invalid", rc=1,
                         accept_mode=lambda mode: mode == 0o600,
                         size_ok=lambda size: PASSWORD_MIN <= size <= PASSWORD_MAX + 1, after_open=after_open)
    if data.endswith(b"\n"):
        data = data[:-1]
    return check_password(data)


def target_from_config_file(parsed: dict[str, Any]) -> dict[str, Any]:
    """目標の web 5 key (port・bind・allow・auth・user) を検査して返す。共通 key・未知 key・文法違反・allow の欠落は `setup_input_invalid`。"""
    if any(name not in ("port", "bind", "allow", "auth", "user") for name in parsed):
        raise WebSetupError("setup_input_invalid", 2)
    try:
        values = validate("web", parsed)
    except InputError:
        raise WebSetupError("setup_input_invalid", 2) from None
    if not is_loopback(values) and "allow" not in values:
        raise WebSetupError("setup_input_invalid", 2)
    return values


# --- 目標と secret の計画 ----------------------------------------------------------------------

@dataclass(frozen=True)
class SecretPlan:
    """目標の secret 操作。password は repr に出さない。"""

    action: str
    password: str | None = field(default=None, repr=False)
    generated: bool = False


def auth_of(values: dict[str, Any]) -> str:
    return values.get("auth", "none")


def plan_for_input(values: dict[str, Any], live: str, action: str, password: str | None) -> SecretPlan:
    """非対話の `--secret-action` を、目標の auth と live の secret の状態の表に当てる。

    不安全な live は `secret_invalid`、表外の組は `setup_input_required` (どちらも無変更)。
    """
    if live == "unsafe":
        raise WebSetupError("secret_invalid")
    auth = auth_of(values)
    if auth == "basic" and live == "safe" and action == "keep" and password is None:
        return SecretPlan("keep")
    if auth == "basic" and live in ("absent", "safe") and action == "install" and password is not None:
        return SecretPlan("install", password)
    if auth == "none" and live == "safe" and action == "remove" and password is None:
        return SecretPlan("remove")
    if auth == "none" and live == "absent" and action == "absent" and password is None:
        return SecretPlan("absent")
    raise WebSetupError("setup_input_required", 2)


# --- 前処理 (0) (a) (b) ---------------------------------------------------------------------

def _scan_temporaries(directory_fd: int, pattern: re.Pattern[str], reason: str) -> list[str]:
    """一時 file の名前に完全一致する entry を数える。実効 uid 所有の regular file だけを除去対象にする。"""
    try:
        with os.scandir(directory_fd) as entries:
            names = [entry.name for entry in entries if pattern.fullmatch(entry.name)]
    except OSError:
        raise WebSetupError(reason) from None
    for name in names:
        try:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError:
            raise WebSetupError(reason) from None
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise WebSetupError(reason)
    return names


def cleanup_temporaries(root: Path, *, hook: Hook | None = None) -> None:
    """前処理 (0): writer の一時 file 名に完全一致する自 uid の regular file だけを `unlinkat` し、dir を fsync する。

    同名の symlink・他 uid・非 regular が 1 つでもあれば、何も除去せず secret dir なら `secret_invalid`、
    config dir なら `config_dir_invalid` で止まる。
    """
    emit = hook if hook is not None else (lambda _name: None)
    try:
        base_fd = open_feature("web")
    except FileNotFoundError:
        return
    except OSError:
        raise WebSetupError("config_dir_invalid") from None
    opened: list[tuple[int, list[str]]] = []
    try:
        for name, pattern, reason in (("secret", _TEMP_PASSWORD, "secret_invalid"),
                                      ("config", _TEMP_CONFIG, "config_dir_invalid")):
            try:
                if name == "secret":
                    fd = open_child(base_fd, "secret", exact=0o700, readable=True, component="secret_dir")
                else:
                    fd = open_child(base_fd, "config", readable=True, component="config_dir")
            except FileNotFoundError:
                continue
            except OSError:
                raise WebSetupError(reason) from None
            try:
                opened.append((fd, _scan_temporaries(fd, pattern, reason)))
            except BaseException:
                os.close(fd)
                raise
        emit("cleanup_scanned")
        for fd, names in opened:
            for name in names:
                try:
                    os.unlink(name, dir_fd=fd)
                except FileNotFoundError:
                    pass
            if names:
                os.fsync(fd)
        emit("cleanup_done")
    finally:
        for fd, _names in opened:
            os.close(fd)
        os.close(base_fd)


def _secret_dir(create: bool) -> tuple[int, int]:
    base_fd = open_feature("web", create=create)
    try:
        return base_fd, open_child(base_fd, "secret", create=create, mode=0o700, exact=0o700,
                                    readable=True, component="secret_dir")
    except BaseException:
        os.close(base_fd)
        raise


def apply_secret(root: Path, plan: SecretPlan, *, hook: Hook | None = None) -> None:
    """前処理 (a): 目標の secret を live の secret dir に反映する。keep / absent は何もしない。"""
    emit = hook if hook is not None else (lambda _name: None)
    if plan.action in ("keep", "absent"):
        return
    try:
        if plan.action == "install":
            if plan.password is None:
                raise WebSetupError("secret_invalid")
            data = check_password(plan.password).encode("ascii")
            base_fd, secret_fd = _secret_dir(True)
            os.close(base_fd)
            try:
                atomic(secret_fd, PASSWORD_NAME, data, 0o600, secret=True, hook=hook, label="secret")
            finally:
                os.close(secret_fd)
        elif plan.action == "remove":
            base_fd, secret_fd = _secret_dir(False)
            os.close(base_fd)
            try:
                try:
                    os.unlink(PASSWORD_NAME, dir_fd=secret_fd)
                except FileNotFoundError:
                    pass
                emit("secret_unlinked")
                os.fsync(secret_fd)
                emit("secret_dir_synced")
            finally:
                os.close(secret_fd)
        else:
            raise WebSetupError("setup_input_required", 2)
    except UnsafeConfigError:
        raise WebSetupError("secret_invalid") from None


def apply_target(root: Path, values: dict[str, Any], plan: SecretPlan, *, hook: Hook | None = None,
                 show_password: Callable[[str, str], None] | None = None, clean: bool = True) -> None:
    """前処理の (0) → (a) → (b)。生成した password の表示は (a) の dir fsync の完了後に 1 回だけ。"""
    emit = hook if hook is not None else (lambda _name: None)
    if clean:
        cleanup_temporaries(root, hook=hook)
    apply_secret(root, plan, hook=hook)
    if plan.generated and plan.password is not None and show_password is not None:
        show_password(plan.password, values.get("user", DEFAULT_USER))
        emit("secret_displayed")
    emit("secret_applied")
    try:
        write_web_config(root, values, hook=hook)
    except UnsafeConfigError:
        raise WebSetupError("config_dir_invalid") from None
    except InputError:
        raise WebSetupError("config_invalid") from None
    emit("config_applied")


# --- 表示 ------------------------------------------------------------------------------------

def exposure(values: dict[str, Any]) -> str:
    """要約の `<範囲>`: loopback は allow にかかわらず自機のみ、非 loopback は allow の件数か全 IPv4。"""
    if is_loopback(values):
        return "自機のみ"
    allow = values.get("allow", [])
    if allow == [UNRESTRICTED]:
        return "到達できる全 IPv4"
    return f"許可範囲 {len(allow)} 件"


def summary_line(values: dict[str, Any]) -> str:
    """`web: 待受 <bind>:<port> / 公開 <範囲> / 認証 none` 又は `認証 basic (<user>)`。allow の個別要素と secret は含まない。"""
    bind = values.get("bind", "127.0.0.1")
    auth = auth_of(values)
    shown = f"basic ({values.get('user', DEFAULT_USER)})" if auth == "basic" else auth
    return f"web: 待受 {bind}:{values['port']} / 公開 {exposure(values)} / 認証 {shown}"


def access_line(root: Path, saved_stage: dict[str, Any] | None, *,
                interfaces: Callable[[], Iterable[str]] | None = None) -> str:
    """status の公開範囲 1 行。stage が不在・terminal disabled なら reader を呼ばず `not_configured`。"""
    if saved_stage is None or (saved_stage.get("operation") == "disable" and saved_stage.get("stage") == "disabled"):
        return "web-access: unavailable reason=not_configured"
    try:
        found = web_access_check(root, interfaces=interfaces)
    except InterfaceLookupFailed:
        return "web-access: unavailable reason=interface_lookup_failed"
    if found.reason is not None:
        return f"web-access: unavailable reason={found.reason}"
    values = found.values
    allow = values.get("allow")
    if allow is None:
        kind = "loopback-default"
    elif allow == [UNRESTRICTED]:
        kind = "unrestricted"
    else:
        kind = "restricted"
    auth = auth_of(values)
    shown = f"basic user={values.get('user', DEFAULT_USER)}" if auth == "basic" else auth
    return f"web-access: bind={values.get('bind', '127.0.0.1')}:{values['port']} allow={kind} auth={shown}"


__all__ = [
    "SECRET_ACTIONS", "PASSWORD_NAME", "SecretPlan", "WebOptions", "WebSetupError", "access_line",
    "apply_secret", "apply_target", "auth_of", "check_password", "cleanup_temporaries", "exposure",
    "generate_password", "generated_password_line", "is_loopback", "options_problem", "plan_for_input", "read_config_file",
    "read_password_file", "summary_line", "target_from_config_file", "web_secret_state",
]
