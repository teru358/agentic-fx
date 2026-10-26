"""Feature-private input files used before durable enable."""
from __future__ import annotations

import errno
import ipaddress
import os
import re
import secrets
import stat
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from agentic_fx.ops.keys import key_dir

_SECRET_NAME = {"bot": "discord.token", "notify": "webhook.url"}
_FIELDS = {"bot": ("guild_id", "channel_id", "allowlist_user_ids", "mention_user_ids", "token"), "web": ("port",), "notify": ("webhook_url",)}
_COMMON = ("instance_root", "key_dir")
# web の公開範囲の key。port と違って省略でき、省略は「自機のみ・allow 無し・認証無し・ユーザー名 afx」を表す
_WEB_ACCESS = ("bind", "allow", "auth", "user")
DEFAULT_USER = "afx"
USER_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,31}")
# web.password の size (byte)。起動側の検査と同じ範囲
PASSWORD_MIN, PASSWORD_MAX = 8, 32
_OPTIONAL = {"bot": ("mention_role_id", "trade_notices", "trade_amounts", "ops_notices",
                     "improve_notices"),
             "web": _WEB_ACCESS, "notify": ("deliver_trade_events", "deliver_improve_events")}
UNRESTRICTED = "0.0.0.0/0"
LOOPBACK = ipaddress.IPv4Network("127.0.0.0/8")


def is_loopback(values: dict[str, Any]) -> bool:
    """bind (省略時 127.0.0.1) が 127.0.0.0/8 にあるか。"""
    return ipaddress.IPv4Address(values.get("bind", "127.0.0.1")) in LOOPBACK
_WEB_PASSWORD = "web.password"
# 取引通知の出し方 (afx-ops の ops.toml の optional key。値の集合は afx-ops と同じ)
_TRADE_KEYS = {"bot": ("trade_notices", "trade_amounts", "ops_notices", "improve_notices"), "web": (),
               "notify": ("deliver_trade_events", "deliver_improve_events")}
TRADE_NOTICES = ("all", "results", "fills_closes", "off")
TRADE_AMOUNTS = ("absolute", "relative")
OPS_NOTICES = ("default", "all")
IMPROVE_NOTICES = ("all", "results", "off")
_BOOLEAN_KEYS = frozenset({"deliver_trade_events", "deliver_improve_events"})
_DIR_FLAGS = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

class InputError(ValueError): pass


class UnsafeConfigError(OSError):
    def __init__(self, component: str):
        super().__init__(f"unsafe {component}")
        self.component = component

@dataclass(frozen=True)
class Inputs:
    values: dict[str, Any]
    missing: tuple[str, ...]
    @property
    def complete(self) -> bool: return not self.missing

def _feature_base(feature: str) -> Path:
    if feature not in _FIELDS: raise ValueError("unknown_feature")
    xdg = os.environ.get("XDG_CONFIG_HOME")
    home = Path(os.environ.get("HOME", str(Path.home())))
    return (Path(xdg) if xdg and os.path.isabs(xdg) else home / ".config") / "agentic-fx" / "ops" / feature

def _discord_id(value: object, name: str) -> int:
    if type(value) is not int or not 10**16 <= value < 10**20: raise InputError(name)
    return value

def _discord_ids(value: object, name: str) -> list[int]:
    if type(value) is not list: raise InputError(name)
    result = [_discord_id(item, name) for item in value]
    if len(result) != len(set(result)): raise InputError(name)
    return result

def _trade_options(feature: str, values: dict[str, Any]) -> dict[str, Any]:
    """値が渡された取引通知の optional key だけを検査して返す (無いものは既定値に任せる)。"""
    result: dict[str, Any] = {}
    for name in _TRADE_KEYS[feature]:
        if name not in values: continue
        value = values[name]
        if name in _BOOLEAN_KEYS:
            if type(value) is not bool: raise InputError(name)
        elif type(value) is not str or value not in {"trade_notices": TRADE_NOTICES,
                                                       "trade_amounts": TRADE_AMOUNTS,
                                                       "ops_notices": OPS_NOTICES,
                                                       "improve_notices": IMPROVE_NOTICES}[name]:
            raise InputError(name)
        result[name] = value
    return result

def _web_bind(value: object) -> str:
    """十進 dotted-quad の canonical な数値 IPv4 (`0.0.0.0`・先頭ゼロ・空白付きは不可)。"""
    if type(value) is not str: raise InputError("bind")
    try: address = ipaddress.IPv4Address(value)
    except ValueError: raise InputError("bind") from None
    if str(address) != value or address.is_unspecified: raise InputError("bind")
    return value


def _web_allow(value: object) -> list[str]:
    """canonical な数値 IPv4 / IPv4 CIDR の非空 list。書かれた表記と順序をそのまま返す。"""
    if type(value) is not list or not value: raise InputError("allow")
    networks = []
    for item in value:
        if type(item) is not str: raise InputError("allow")
        try:
            if "/" in item:
                network = ipaddress.IPv4Network(item, strict=True)
                if str(network) != item: raise InputError("allow")
            else:
                address = ipaddress.IPv4Address(item)
                if str(address) != item: raise InputError("allow")
                network = ipaddress.IPv4Network(address)
        except ValueError: raise InputError("allow") from None
        networks.append(network)
    if len(set(networks)) != len(networks): raise InputError("allow")
    if ipaddress.IPv4Network(UNRESTRICTED) in networks and len(networks) != 1: raise InputError("allow")
    return list(value)


def _web_auth(value: object) -> str:
    if type(value) is not str or value not in ("none", "basic"): raise InputError("auth")
    return value


def _web_user(value: object) -> str:
    """Basic 認証のユーザー名。英小文字で始まる 1〜32 byte の ASCII (`re.fullmatch`)。"""
    if type(value) is not str or USER_PATTERN.fullmatch(value) is None: raise InputError("user")
    return value


def _web_values(values: dict[str, Any]) -> dict[str, Any]:
    """web の 5 key (port・bind・allow・auth・user) の単独の文法。bind / allow / auth / user は省略できる。"""
    if any(name in values for name in _COMMON): raise InputError("common_key")
    port = values.get("port")
    if type(port) is not int or not 1 <= port <= 65535: raise InputError("port")
    result: dict[str, Any] = {"port": port}
    for name, check in (("bind", _web_bind), ("allow", _web_allow), ("auth", _web_auth), ("user", _web_user)):
        if name in values: result[name] = check(values[name])
    return result


def web_written_values(values: dict[str, Any]) -> dict[str, Any]:
    """writer が ops.toml に書く web の key。user は auth が basic のときだけ書き、値が既定でも省略しない。"""
    result = {name: values[name] for name in ("port", "bind", "allow", "auth") if name in values}
    if values.get("auth") == "basic": result["user"] = values.get("user", DEFAULT_USER)
    return result


def validate(feature: str, values: dict[str, Any]) -> dict[str, Any]:
    if feature == "web":
        return _web_values(values)
    if feature == "notify":
        url = values.get("webhook_url")
        try:
            p = urlsplit(url) if isinstance(url, str) else None
            valid = p and p.scheme == "https" and p.hostname and p.username is None and p.password is None and not p.fragment and p.port in (None, 443)
        except ValueError: valid = False
        if not valid: raise InputError("webhook_url")
        return {"webhook_url": url} | _trade_options("notify", values)
    if feature == "bot":
        token = values.get("token")
        if not isinstance(token, str) or not token: raise InputError("token")
        allow = _discord_ids(values.get("allowlist_user_ids"), "allowlist_user_ids")
        mention = values.get("mention_user_ids", allow)
        result = {"guild_id": _discord_id(values.get("guild_id"), "guild_id"), "channel_id": _discord_id(values.get("channel_id"), "channel_id"), "allowlist_user_ids": allow, "mention_user_ids": _discord_ids(mention, "mention_user_ids"), "token": token}
        if values.get("mention_role_id") is not None: result["mention_role_id"] = _discord_id(values["mention_role_id"], "mention_role_id")
        return result | _trade_options("bot", values)
    raise ValueError("unknown_feature")

def _owned_dir(fd: int, *, component: str, exact: int | None = None, other_writable: bool = False) -> None:
    st = os.fstat(fd); mode = stat.S_IMODE(st.st_mode)
    if not stat.S_ISDIR(st.st_mode): raise UnsafeConfigError(component)
    if st.st_uid != os.getuid(): raise UnsafeConfigError("owner")
    if exact is not None and mode != exact: raise UnsafeConfigError(component)
    if exact is None and mode & (0o002 if other_writable else 0o022): raise UnsafeConfigError(component)


def _is_symlink(parent_fd: int, name: str) -> bool:
    try: return stat.S_ISLNK(os.stat(name, dir_fd=parent_fd, follow_symlinks=False).st_mode)
    except OSError: return False


def open_child(parent_fd: int, name: str, *, create: bool = False, mode: int = 0o700, exact: int | None = None,
                readable: bool = False, checked: bool = True, component: str = "parent_dir",
                other_writable: bool = False) -> int:
    created = False
    if create:
        try: os.mkdir(name, mode, dir_fd=parent_fd); created = True
        except FileExistsError: pass
    flags = _DIR_FLAGS if not readable else os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try: fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as error:
        if error.errno == errno.ELOOP or _is_symlink(parent_fd, name): raise UnsafeConfigError("symlink") from error
        raise
    try:
        if checked: _owned_dir(fd, component=component, exact=exact, other_writable=other_writable)
        if created:
            os.chmod(name, mode, dir_fd=parent_fd, follow_symlinks=False)
            if checked: _owned_dir(fd, component=component, exact=exact, other_writable=other_writable)
        return fd
    except BaseException:
        os.close(fd); raise

def _open_config_home(*, create: bool) -> int:
    path = _feature_base("web").parents[2]
    if not path.is_absolute(): raise OSError("relative config home")
    fd = os.open("/", _DIR_FLAGS)
    try:
        for index, part in enumerate(path.parts[1:]):
            next_fd = open_child(fd, part, create=create, checked=index == len(path.parts) - 2)
            os.close(fd); fd = next_fd
        _owned_dir(fd, component="parent_dir")
        return fd
    except BaseException:
        os.close(fd); raise

def open_feature(feature: str, *, create: bool = False) -> int:
    fd = _open_config_home(create=create)
    try:
        for name, exact, component, other_writable in (("agentic-fx", None, "parent_dir", True),
                                                        ("ops", None, "parent_dir", True),
                                                        (feature, 0o700, "feature_dir", False)):
            next_fd = open_child(fd, name, create=create, exact=exact, component=component,
                                  other_writable=other_writable)
            os.close(fd); fd = next_fd
        return fd
    except BaseException:
        os.close(fd); raise

def _read_regular(directory_fd: int, name: str, *, secret: bool = False) -> bytes:
    component = "secret_file" if secret else "config_file"
    try: fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory_fd)
    except OSError as error:
        if error.errno == errno.ELOOP or _is_symlink(directory_fd, name): raise UnsafeConfigError("symlink") from error
        raise
    try:
        st = os.fstat(fd); mode = stat.S_IMODE(st.st_mode)
        if not stat.S_ISREG(st.st_mode): raise UnsafeConfigError(component)
        if st.st_uid != os.getuid(): raise UnsafeConfigError("owner")
        if (mode != 0o600 if secret else mode & 0o022): raise UnsafeConfigError(component)
        chunks = []
        while chunk := os.read(fd, 4096): chunks.append(chunk)
        return b"".join(chunks)
    finally: os.close(fd)

def _parse_config(root: Path, feature: str) -> dict[str, Any]:
    base_fd = open_feature(feature)
    try: config_fd = open_child(base_fd, "config", readable=True, component="config_dir")
    finally: os.close(base_fd)
    try: parsed = tomllib.loads(_read_regular(config_fd, "ops.toml").decode("utf-8"))
    finally: os.close(config_fd)
    if not isinstance(parsed, dict): raise ValueError("invalid config")
    required = _COMMON + tuple(name for name in _FIELDS[feature] if name not in {"token", "webhook_url"})
    if any(name not in parsed for name in required) or any(name not in set(required + _OPTIONAL[feature]) for name in parsed): raise ValueError("invalid schema")
    if parsed.get("instance_root") != str(Path(root).resolve()) or parsed.get("key_dir") != str(key_dir(root)): raise ValueError("wrong instance")
    public = {name: parsed[name] for name in _FIELDS[feature] if name in parsed}
    options = {name: parsed[name] for name in _TRADE_KEYS[feature] if name in parsed}
    if feature == "web": options = {name: parsed[name] for name in _WEB_ACCESS if name in parsed}
    if feature == "web": _web_values(public | options)
    elif feature != "notify": validate(feature, public | options | ({"token": "present"} if feature == "bot" else {}))
    else: _trade_options(feature, options)
    return public | options

def _secret_present(feature: str) -> bool:
    name = _SECRET_NAME.get(feature)
    if name is None: return False
    base_fd = open_feature(feature)
    try: secret_fd = open_child(base_fd, "secret", exact=0o700, readable=True, component="secret_dir")
    finally: os.close(base_fd)
    try: _read_regular(secret_fd, name, secret=True); return True
    finally: os.close(secret_fd)

def unsafe_reason(root: Path, feature: str) -> str | None:
    try: base_fd = open_feature(feature)
    except FileNotFoundError: return None
    except UnsafeConfigError as error: return error.component
    except OSError: return "parent_dir"
    try:
        try: config_fd = open_child(base_fd, "config", readable=True, component="config_dir")
        except FileNotFoundError: config_fd = None
        if config_fd is not None:
            try:
                try: _parse_config(root, feature)
                except FileNotFoundError: pass
                except UnsafeConfigError as error: return error.component
                except (ValueError, UnicodeError, tomllib.TOMLDecodeError, InputError): return "config_file"
            finally: os.close(config_fd)
        secret = _SECRET_NAME.get(feature)
        if secret:
            try: secret_fd = open_child(base_fd, "secret", exact=0o700, readable=True, component="secret_dir")
            except FileNotFoundError: secret_fd = None
            if secret_fd is not None:
                try:
                    try: _read_regular(secret_fd, secret, secret=True)
                    except FileNotFoundError: pass
                    except UnsafeConfigError as error: return error.component
                finally: os.close(secret_fd)
        return None
    except UnsafeConfigError as error: return error.component
    except (OSError, ValueError, UnicodeError, tomllib.TOMLDecodeError, InputError): return "config_file"
    finally: os.close(base_fd)


def is_unsafe(root: Path, feature: str) -> bool:
    return unsafe_reason(root, feature) is not None

def collect(root: Path, feature: str) -> Inputs:
    values: dict[str, Any] = {}
    try: values = _parse_config(root, feature)
    except (OSError, ValueError, UnicodeError, tomllib.TOMLDecodeError, InputError): pass
    try:
        if _secret_present(feature): values["token" if feature == "bot" else "webhook_url"] = "present"
    except OSError: pass
    return Inputs(values, tuple(name for name in _FIELDS[feature] if name not in values))

class InterfaceLookupFailed(Exception):
    """本機の interface の列挙ができなかった (固定 reason `interface_lookup_failed`)。"""


def _web_password_state(secret_fd: int) -> str:
    """検査済みの secret dir fd から見た `web.password`: "absent" / "safe" / "unsafe"。"""
    try:
        with os.scandir(secret_fd) as entries: names = [entry.name for entry in entries]
    except OSError: return "unsafe"
    if any(name != _WEB_PASSWORD for name in names): return "unsafe"
    if _WEB_PASSWORD not in names: return "absent"
    try: fd = os.open(_WEB_PASSWORD, os.O_RDONLY | os.O_NOFOLLOW | os.O_NOCTTY | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=secret_fd)
    except OSError: return "unsafe"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600 or not PASSWORD_MIN <= st.st_size <= PASSWORD_MAX: return "unsafe"
        data = os.read(fd, PASSWORD_MAX + 2)
    except OSError: return "unsafe"
    finally: os.close(fd)
    if len(data) != st.st_size or any(not 0x21 <= byte <= 0x7E for byte in data): return "unsafe"
    return "safe"


def _web_secret_state(base_fd: int) -> str:
    try: secret_fd = open_child(base_fd, "secret", exact=0o700, readable=True, component="secret_dir")
    except FileNotFoundError: return "absent"
    except OSError: return "unsafe"
    try: return _web_password_state(secret_fd)
    finally: os.close(secret_fd)


def _web_parsed(root: Path, config_fd: int) -> dict[str, Any] | str:
    """ops.toml を読んで web の全 key を検査する。通らなければ最終 reason (str)。"""
    try: raw = _read_regular(config_fd, "ops.toml")
    except FileNotFoundError: return "config_invalid"
    except OSError: return "config_dir_invalid"
    try: parsed = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError): return "config_invalid"
    required = (*_COMMON, "port")
    if any(name not in parsed for name in required) or any(name not in {*required, *_WEB_ACCESS} for name in parsed): return "config_invalid"
    if parsed["instance_root"] != str(Path(root).resolve()) or parsed["key_dir"] != str(key_dir(root)): return "config_invalid"
    try: _web_values({name: parsed[name] for name in ("port", *_WEB_ACCESS) if name in parsed})
    except InputError: return "config_invalid"
    return parsed


@dataclass(frozen=True)
class WebAccess:
    """web の設定の判定。reason が None なら許可で、values は検査済みの port / bind / allow / auth と、basic のときの user。"""
    reason: str | None
    values: dict[str, Any]


def web_access_check(root: Path, *, interfaces: Callable[[], Iterable[str]] | None = None) -> WebAccess:
    """web の設定の最終 reason と (許可のとき) その値。

    status・完全性の判定・要約・設定検査が共用する 1 実装で、§2.3 の決定木
    (config dir・ops.toml の安全性 → 文法 → allow の必須 → interface の実在 →
    secret) を同じ順で評価する。`interfaces` は本機の up 状態の interface の global / link IPv4
    を返す関数で、列挙できなければ `InterfaceLookupFailed` を上げる。非 loopback の bind で
    だけ呼び、渡されなければ列挙失敗とみなす。
    """
    try: base_fd = open_feature("web")
    except FileNotFoundError: return WebAccess("config_dir_missing", {})
    except OSError: return WebAccess("config_dir_invalid", {})
    try:
        try: config_fd = open_child(base_fd, "config", readable=True, component="config_dir")
        except FileNotFoundError: return WebAccess("config_dir_missing", {})
        except OSError: return WebAccess("config_dir_invalid", {})
        try: parsed = _web_parsed(root, config_fd)
        finally: os.close(config_fd)
        if isinstance(parsed, str): return WebAccess(parsed, {})
        bind = ipaddress.IPv4Address(parsed.get("bind", "127.0.0.1"))
        loopback, allow, auth = is_loopback(parsed), parsed.get("allow"), parsed.get("auth", "none")
        if not loopback and allow is None: return WebAccess("allow_required", {})
        if not loopback:
            try: found = {str(address) for address in interfaces()} if interfaces is not None else None
            except InterfaceLookupFailed: found = None
            if found is None: return WebAccess("interface_lookup_failed", {})
            if str(bind) not in found: return WebAccess("bind_not_local", {})
        state = _web_secret_state(base_fd)
        if state == "unsafe": return WebAccess("secret_invalid", {})
        if auth == "basic" and state == "absent": return WebAccess("secret_missing", {})
        if auth == "none" and state == "safe": return WebAccess("secret_unexpected", {})
        found = {name: parsed[name] for name in ("port", *_WEB_ACCESS) if name in parsed}
        if auth == "basic": found["user"] = parsed.get("user", DEFAULT_USER)
        else: found.pop("user", None)
        return WebAccess(None, found)
    finally: os.close(base_fd)


def web_access_reason(root: Path, *, interfaces: Callable[[], Iterable[str]] | None = None) -> str | None:
    """`web_access_check` の reason だけ。許可なら None。"""
    return web_access_check(root, interfaces=interfaces).reason


def web_secret_state(root: Path) -> str:
    """live の `web.password` の状態: "absent" / "safe" / "unsafe" (secret dir の不在は absent)。"""
    try: base_fd = open_feature("web")
    except FileNotFoundError: return "absent"
    except OSError: return "unsafe"
    try: return _web_secret_state(base_fd)
    finally: os.close(base_fd)


def web_defaults(root: Path) -> dict[str, Any]:
    """live の ops.toml のうち、単独の文法を通る port / bind / allow / auth / user だけ。質問の既定値に使う。

    読めない・安全でない・別 instance の file は空。値の組み合わせは見ない。
    """
    try: base_fd = open_feature("web")
    except OSError: return {}
    try:
        try: config_fd = open_child(base_fd, "config", readable=True, component="config_dir")
        except OSError: return {}
        try: raw = _read_regular(config_fd, "ops.toml")
        except OSError: return {}
        finally: os.close(config_fd)
    finally: os.close(base_fd)
    try: parsed = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError): return {}
    result: dict[str, Any] = {}
    for name, check in (("port", None), ("bind", _web_bind), ("allow", _web_allow), ("auth", _web_auth),
                        ("user", _web_user)):
        if name not in parsed: continue
        try:
            if check is None:
                _web_values({"port": parsed[name]})
            else:
                check(parsed[name])
        except InputError: continue
        result[name] = parsed[name]
    return result


def _toml_value(value: Any) -> str:
    if isinstance(value, bool): return "true" if value else "false"
    if isinstance(value, int): return str(value)
    if isinstance(value, list): return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, str): return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    raise TypeError(value)

def atomic(directory_fd: int, name: str, data: bytes, mode: int, *, secret: bool = False,
            hook: Callable[[str], None] | None = None, label: str = "") -> None:
    try:
        if _read_regular(directory_fd, name, secret=secret) == data: return
    except FileNotFoundError: pass
    emit = hook if hook is not None else (lambda _name: None)
    temporary = f".{name}.{secrets.token_hex(16)}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=directory_fd)
    try:
        emit(f"{label}_tmp_created")
        os.fchmod(fd, mode); offset = 0
        while offset < len(data):
            written = os.write(fd, data[offset:])
            if written <= 0: raise OSError(errno.EIO, "short write")
            offset += written
        os.fsync(fd)
        emit(f"{label}_tmp_synced")
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        emit(f"{label}_renamed")
        os.fsync(directory_fd)
        emit(f"{label}_dir_synced")
    finally:
        os.close(fd)
        try: os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError: pass

def write_inputs(root: Path, feature: str, values: dict[str, Any]) -> None:
    values = validate(feature, values)
    if is_unsafe(root, feature): raise OSError("unsafe existing configuration")
    base_fd = open_feature(feature, create=True)
    try:
        config_fd = open_child(base_fd, "config", create=True, mode=0o755, readable=True, component="config_dir")
        secret_fd = open_child(base_fd, "secret", create=True, mode=0o700, exact=0o700, readable=True, component="secret_dir")
    finally: os.close(base_fd)
    config: dict[str, Any] = {"instance_root": str(Path(root).resolve()), "key_dir": str(key_dir(root))}
    if feature == "bot":
        config.update({name: values[name] for name in ("guild_id", "channel_id", "allowlist_user_ids", "mention_user_ids")})
        if "mention_role_id" in values: config["mention_role_id"] = values["mention_role_id"]
    elif feature == "web": config.update(web_written_values(values))
    config.update({name: values[name] for name in _TRADE_KEYS[feature] if name in values})
    try:
        atomic(config_fd, "ops.toml", "".join(f"{key} = {_toml_value(value)}\n" for key, value in config.items()).encode(), 0o644)
        if feature == "bot" and values["token"] != "present": atomic(secret_fd, _SECRET_NAME[feature], values["token"].encode(), 0o600, secret=True)
        if feature == "notify" and values["webhook_url"] != "present": atomic(secret_fd, _SECRET_NAME[feature], values["webhook_url"].encode(), 0o600, secret=True)
    finally: os.close(config_fd); os.close(secret_fd)

def _web_config_text(root: Path, values: dict[str, Any]) -> bytes:
    config: dict[str, Any] = {"instance_root": str(Path(root).resolve()), "key_dir": str(key_dir(root))}
    config.update(web_written_values(values))
    return "".join(f"{key} = {_toml_value(value)}\n" for key, value in config.items()).encode()


def write_web_config(root: Path, values: dict[str, Any], *, hook: Callable[[str], None] | None = None) -> None:
    """web の ops.toml だけを atomic に書く (内容が live と byte 一致なら書かない)。

    既存の ops.toml が文法や key の点で壊れていても置き換える。置き換えを拒むのは filesystem 上の
    安全性 (symlink・他 uid・group / other 書込み可・型) に違反する場合だけで、`UnsafeConfigError`。
    """
    values = validate("web", values)
    base_fd = open_feature("web", create=True)
    try:
        config_fd = open_child(base_fd, "config", create=True, mode=0o755, readable=True, component="config_dir")
        try: os.close(open_child(base_fd, "secret", create=True, mode=0o700, exact=0o700, readable=True, component="secret_dir"))
        except BaseException:
            os.close(config_fd); raise
    finally: os.close(base_fd)
    try: atomic(config_fd, "ops.toml", _web_config_text(root, values), 0o644, hook=hook, label="config")
    finally: os.close(config_fd)


def redact(feature: str, value: str) -> str:
    return (urlsplit(value).hostname or "<invalid>") if feature == "notify" else "<redacted>"
