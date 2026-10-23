"""Feature-private input files used before durable enable."""
from __future__ import annotations

import errno
import os
import secrets
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from agentic_fx.ops.keys import key_dir

_SECRET_NAME = {"bot": "discord.token", "notify": "webhook.url"}
_FIELDS = {"bot": ("guild_id", "channel_id", "allowlist_user_ids", "mention_user_ids", "token"), "web": ("port",), "notify": ("webhook_url",)}
_COMMON = ("instance_root", "key_dir")
_OPTIONAL = {"bot": ("mention_role_id", "trade_notices", "trade_amounts", "ops_notices"), "web": (),
             "notify": ("deliver_trade_events",)}
# 取引通知の出し方 (afx-ops の ops.toml の optional key。値の集合は afx-ops と同じ)
_TRADE_KEYS = {"bot": ("trade_notices", "trade_amounts", "ops_notices"), "web": (), "notify": ("deliver_trade_events",)}
TRADE_NOTICES = ("all", "results", "fills_closes", "off")
TRADE_AMOUNTS = ("absolute", "relative")
OPS_NOTICES = ("default", "all")
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
        if name == "deliver_trade_events":
            if type(value) is not bool: raise InputError(name)
        elif type(value) is not str or value not in {"trade_notices": TRADE_NOTICES,
                                                       "trade_amounts": TRADE_AMOUNTS,
                                                       "ops_notices": OPS_NOTICES}[name]:
            raise InputError(name)
        result[name] = value
    return result

def validate(feature: str, values: dict[str, Any]) -> dict[str, Any]:
    if feature == "web":
        port = values.get("port")
        if type(port) is not int or not 1 <= port <= 65535: raise InputError("port")
        return {"port": port}
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


def _open_child(parent_fd: int, name: str, *, create: bool = False, mode: int = 0o700, exact: int | None = None,
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
            next_fd = _open_child(fd, part, create=create, checked=index == len(path.parts) - 2)
            os.close(fd); fd = next_fd
        _owned_dir(fd, component="parent_dir")
        return fd
    except BaseException:
        os.close(fd); raise

def _open_feature(feature: str, *, create: bool = False) -> int:
    fd = _open_config_home(create=create)
    try:
        for name, exact, component, other_writable in (("agentic-fx", None, "parent_dir", True),
                                                        ("ops", None, "parent_dir", True),
                                                        (feature, 0o700, "feature_dir", False)):
            next_fd = _open_child(fd, name, create=create, exact=exact, component=component,
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
    base_fd = _open_feature(feature)
    try: config_fd = _open_child(base_fd, "config", readable=True, component="config_dir")
    finally: os.close(base_fd)
    try: parsed = tomllib.loads(_read_regular(config_fd, "ops.toml").decode("utf-8"))
    finally: os.close(config_fd)
    if not isinstance(parsed, dict): raise ValueError("invalid config")
    required = _COMMON + tuple(name for name in _FIELDS[feature] if name not in {"token", "webhook_url"})
    if any(name not in parsed for name in required) or any(name not in set(required + _OPTIONAL[feature]) for name in parsed): raise ValueError("invalid schema")
    if parsed.get("instance_root") != str(Path(root).resolve()) or parsed.get("key_dir") != str(key_dir(root)): raise ValueError("wrong instance")
    public = {name: parsed[name] for name in _FIELDS[feature] if name in parsed}
    options = {name: parsed[name] for name in _TRADE_KEYS[feature] if name in parsed}
    if feature != "notify": validate(feature, public | options | ({"token": "present"} if feature == "bot" else {}))
    else: _trade_options(feature, options)
    return public | options

def _secret_present(feature: str) -> bool:
    name = _SECRET_NAME.get(feature)
    if name is None: return False
    base_fd = _open_feature(feature)
    try: secret_fd = _open_child(base_fd, "secret", exact=0o700, readable=True, component="secret_dir")
    finally: os.close(base_fd)
    try: _read_regular(secret_fd, name, secret=True); return True
    finally: os.close(secret_fd)

def unsafe_reason(root: Path, feature: str) -> str | None:
    try: base_fd = _open_feature(feature)
    except FileNotFoundError: return None
    except UnsafeConfigError as error: return error.component
    except OSError: return "parent_dir"
    try:
        try: config_fd = _open_child(base_fd, "config", readable=True, component="config_dir")
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
            try: secret_fd = _open_child(base_fd, "secret", exact=0o700, readable=True, component="secret_dir")
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

def _toml_value(value: Any) -> str:
    if isinstance(value, bool): return "true" if value else "false"
    if isinstance(value, int): return str(value)
    if isinstance(value, list): return "[" + ", ".join(str(item) for item in value) + "]"
    if isinstance(value, str): return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    raise TypeError(value)

def _atomic(directory_fd: int, name: str, data: bytes, mode: int, *, secret: bool = False) -> None:
    try:
        if _read_regular(directory_fd, name, secret=secret) == data: return
    except FileNotFoundError: pass
    temporary = f".{name}.{secrets.token_hex(16)}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=directory_fd)
    try:
        os.fchmod(fd, mode); offset = 0
        while offset < len(data):
            written = os.write(fd, data[offset:])
            if written <= 0: raise OSError(errno.EIO, "short write")
            offset += written
        os.fsync(fd)
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(fd)
        try: os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError: pass

def write_inputs(root: Path, feature: str, values: dict[str, Any]) -> None:
    values = validate(feature, values)
    if is_unsafe(root, feature): raise OSError("unsafe existing configuration")
    base_fd = _open_feature(feature, create=True)
    try:
        config_fd = _open_child(base_fd, "config", create=True, mode=0o755, readable=True, component="config_dir")
        secret_fd = _open_child(base_fd, "secret", create=True, mode=0o700, exact=0o700, readable=True, component="secret_dir")
    finally: os.close(base_fd)
    config: dict[str, Any] = {"instance_root": str(Path(root).resolve()), "key_dir": str(key_dir(root))}
    if feature == "bot":
        config.update({name: values[name] for name in ("guild_id", "channel_id", "allowlist_user_ids", "mention_user_ids")})
        if "mention_role_id" in values: config["mention_role_id"] = values["mention_role_id"]
    elif feature == "web": config["port"] = values["port"]
    config.update({name: values[name] for name in _TRADE_KEYS[feature] if name in values})
    try:
        _atomic(config_fd, "ops.toml", "".join(f"{key} = {_toml_value(value)}\n" for key, value in config.items()).encode(), 0o644)
        if feature == "bot" and values["token"] != "present": _atomic(secret_fd, _SECRET_NAME[feature], values["token"].encode(), 0o600, secret=True)
        if feature == "notify" and values["webhook_url"] != "present": _atomic(secret_fd, _SECRET_NAME[feature], values["webhook_url"].encode(), 0o600, secret=True)
    finally: os.close(config_fd); os.close(secret_fd)

def redact(feature: str, value: str) -> str:
    return (urlsplit(value).hostname or "<invalid>") if feature == "notify" else "<redacted>"
