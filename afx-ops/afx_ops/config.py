"""機能ごとの ``ops.toml`` を隔離前に検証して読む。"""
from __future__ import annotations

import dataclasses
import ipaddress
import os
import re
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from afx_ops.isolation.accept_loop import LOOPBACK, LOOPBACK_NETWORK, UNRESTRICTED_NETWORK
from afx_ops.trade_events import NOTICE_KINDS
from afx_ops.isolation.startup import (
    StartupError,
    dns_tls_rules,
    feature_dirs,
    open_config_dir,
    runtime_rules,
)

REASON_CONFIG_INVALID = "config_invalid"

_COMMON_KEYS = ("instance_root", "key_dir")
_FEATURE_KEYS = MappingProxyType({
    "bot": ("guild_id", "channel_id", "allowlist_user_ids", "mention_user_ids"),
    "web": ("port",),
    "notify": (),
})
_OPTIONAL_FEATURE_KEYS = MappingProxyType({
    "bot": ("mention_role_id", "trade_notices", "trade_amounts", "ops_notices"),
    "web": ("bind", "allow", "auth", "user"),
    "notify": ("deliver_trade_events",),
})
# 取引通知の出し方。key が無い旧 ops.toml は既定値で起動する
TRADE_NOTICES = tuple(NOTICE_KINDS)
TRADE_AMOUNTS = ("absolute", "relative")
OPS_NOTICES = ("default", "all")
_SAFE_DETAIL_KEY = re.compile(r"[A-Za-z0-9_]{1,32}\Z")


WEB_LOOPBACK = LOOPBACK
WEB_UNRESTRICTED = UNRESTRICTED_NETWORK
WEB_AUTHS = ("none", "basic")
WEB_USER_DEFAULT = "afx"
WEB_USER_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,31}")


class ConfigError(Exception):
    """設定を使えない。値や path を例外へ含めない。

    `reason` は既定が `config_invalid`。web は `ops.toml` の不在を `config_invalid`、config dir
    の不在を `config_dir_missing`、config dir / `ops.toml` の不安全を `config_dir_invalid` で返す。
    """

    def __init__(self, detail: str, reason: str = REASON_CONFIG_INVALID) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class OpsConfig:
    instance_root: Path
    key_dir: Path
    config_dir_fd: int | None
    port: int | None = None
    guild_id: int | None = None
    channel_id: int | None = None
    allowlist_user_ids: tuple[int, ...] = ()
    mention_role_id: int | None = None
    mention_user_ids: tuple[int, ...] = ()
    trade_notices: str = "all"
    trade_amounts: str = "absolute"
    ops_notices: str = "default"
    deliver_trade_events: bool = False
    # web の公開範囲。bind が None は loopback の `127.0.0.1`、allow が None は key の省略
    bind: str | None = None
    allow: tuple[ipaddress.IPv4Network, ...] | None = None
    auth: str = "none"
    user: str = WEB_USER_DEFAULT

    def without_config_dir_fd(self) -> "OpsConfig":
        """config dir の fd の所有を手放した写し (fd は閉じない)。"""
        values = {f.name: getattr(self, f.name) for f in dataclasses.fields(self)
                  if f.name != "config_dir_fd"}
        return OpsConfig(config_dir_fd=None, **values)

    @property
    def web_bind(self) -> str:
        return WEB_LOOPBACK if self.bind is None else self.bind

    @property
    def web_loopback(self) -> bool:
        return ipaddress.IPv4Address(self.web_bind) in LOOPBACK_NETWORK

    def web_allow_networks(self) -> tuple[ipaddress.IPv4Network, ...]:
        """接続元の照合に使う network。loopback で allow を省略したときは `127.0.0.0/8`。"""
        return (LOOPBACK_NETWORK,) if self.allow is None else self.allow


# ops.toml の所有者として期待する uid。試験が差し替えて他 uid の file を再現する場所で、
# production は差し替えない (既定は実効 process の uid)。
_expected_owner_uid = os.getuid


def _read_toml(feature: str, *, preserve_config_dir_error: bool = False) -> tuple[dict[str, object], int]:
    web = feature == "web"
    try:
        config_dir_fd = open_config_dir(feature_dirs(feature).config)
    except StartupError as exc:
        if web:
            raise ConfigError("config dir", exc.reason) from None
        if preserve_config_dir_error:
            raise
        raise ConfigError("ops.toml") from None
    except (OSError, ValueError):
        raise ConfigError("ops.toml", "config_dir_invalid" if web else REASON_CONFIG_INVALID) from None
    try:
        try:
            fd = os.open("ops.toml", os.O_RDONLY | os.O_NOFOLLOW | os.O_NOCTTY | os.O_NONBLOCK
                         | os.O_CLOEXEC, dir_fd=config_dir_fd)
        except FileNotFoundError:
            raise ConfigError("ops.toml") from None
        except (OSError, ValueError):
            raise ConfigError("ops.toml", "config_dir_invalid" if web else REASON_CONFIG_INVALID) from None
        try:
            st = os.fstat(fd)
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != _expected_owner_uid()
                    or stat.S_IMODE(st.st_mode) & 0o022):
                raise ConfigError("ops.toml", "config_dir_invalid" if web else REASON_CONFIG_INVALID)
            chunks = []
            while chunk := os.read(fd, 4096):
                chunks.append(chunk)
        except ConfigError:
            raise
        except OSError:
            raise ConfigError("ops.toml", "config_dir_invalid" if web else REASON_CONFIG_INVALID) from None
        finally:
            os.close(fd)
        try:
            parsed = tomllib.loads(b"".join(chunks).decode("utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            raise ConfigError("ops.toml") from None
        if not isinstance(parsed, dict):
            raise ConfigError("ops.toml")
        return parsed, config_dir_fd
    except BaseException:
        os.close(config_dir_fd)
        raise


def _absolute_path(value: object, key: str) -> Path:
    if type(value) is not str or not os.path.isabs(value):
        raise ConfigError(key)
    return Path(value)


def _integer(value: object, key: str) -> int:
    if type(value) is not int:
        raise ConfigError(key)
    return value


def _discord_id(value: object, key: str) -> int:
    number = _integer(value, key)
    if not 10 ** 16 <= number < 10 ** 20:
        raise ConfigError(key)
    return number


def _discord_ids(value: object, key: str) -> tuple[int, ...]:
    if type(value) is not list:
        raise ConfigError(key)
    ids = tuple(_discord_id(item, key) for item in value)
    if len(ids) != len(set(ids)):
        raise ConfigError(key)
    return ids


def _choice(parsed: Mapping[str, object], key: str, choices: tuple[str, ...], default: str) -> str:
    if key not in parsed:
        return default
    value = parsed[key]
    if type(value) is not str or value not in choices:
        raise ConfigError(key)
    return value


def _boolean(parsed: Mapping[str, object], key: str, default: bool) -> bool:
    if key not in parsed:
        return default
    if type(parsed[key]) is not bool:
        raise ConfigError(key)
    return parsed[key]


def _safe_detail_key(key: object) -> str:
    return key if isinstance(key, str) and _SAFE_DETAIL_KEY.fullmatch(key) else "<invalid>"


def _path_is_within(path: str, directory: str) -> bool:
    return path == directory or path.startswith(directory + os.sep)


def _directory_identity(fd: int) -> tuple[int, int]:
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode):
        raise ConfigError("key_dir")
    return st.st_dev, st.st_ino


def _ancestor_identities(fd: int) -> set[tuple[int, int]]:
    flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    current = os.dup(fd)
    identities = set()
    try:
        while True:
            identity = _directory_identity(current)
            if identity in identities:
                return identities
            identities.add(identity)
            parent = -1
            try:
                parent = os.open("..", flags, dir_fd=current)
                if _directory_identity(parent) == identity:
                    return identities
                os.close(current)
                current, parent = parent, -1
            finally:
                if parent >= 0:
                    os.close(parent)
    except (OSError, ValueError):
        raise ConfigError("key_dir") from None
    finally:
        os.close(current)


def _key_dir_ancestors(key_dir: Path) -> set[tuple[int, int]]:
    flags = os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(key_dir, flags)
    except (OSError, ValueError):
        raise ConfigError("key_dir") from None
    try:
        return _ancestor_identities(fd)
    finally:
        os.close(fd)


def _runtime_and_dns_tls_rule_paths() -> tuple[str, ...]:
    return tuple(dict.fromkeys(rule.path for rule in (*runtime_rules(), *dns_tls_rules())))


def _rule_directory_identities(paths: tuple[str, ...]) -> set[tuple[int, int]]:
    flags = os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC
    identities = set()
    for path in paths:
        try:
            fd = os.open(path, flags)
        except (OSError, ValueError):
            continue
        try:
            st = os.fstat(fd)
            if stat.S_ISDIR(st.st_mode):
                identities.add((st.st_dev, st.st_ino))
        except OSError:
            raise ConfigError("key_dir") from None
        finally:
            os.close(fd)
    return identities


def validate_key_dir(key_dir: Path, *, config_dir: Path, state_dir: Path,
                     secret_dir: Path, checked_dir_fds: Mapping[str, int] | None = None) -> None:
    """鍵 dir が隔離後に許可する領域に入らないことを確かめる。"""
    try:
        normalized_key_dir = os.path.realpath(key_dir)
        runtime_and_dns_tls = tuple(os.path.realpath(path)
                                    for path in _runtime_and_dns_tls_rule_paths())
    except ValueError:
        raise ConfigError("key_dir") from None
    for name, directory in (("config_dir", config_dir), ("state_dir", state_dir),
                            ("secret_dir", secret_dir)):
        if _path_is_within(normalized_key_dir, os.path.realpath(directory)):
            raise ConfigError(f"key_dir inside {name}")
    if any(_path_is_within(normalized_key_dir, path) for path in runtime_and_dns_tls):
        raise ConfigError("key_dir inside runtime_or_dns_tls")
    if checked_dir_fds is None:
        return
    ancestors = _key_dir_ancestors(key_dir)
    for name in ("config_dir", "state_dir", "secret_dir"):
        if name in checked_dir_fds and _directory_identity(checked_dir_fds[name]) in ancestors:
            raise ConfigError(f"key_dir inside {name}")
    if _rule_directory_identities(_runtime_and_dns_tls_rule_paths()) & ancestors:
        raise ConfigError("key_dir inside runtime_or_dns_tls")


def _web_bind(value: object) -> str:
    """十進 dotted-quad の canonical な数値 IPv4。`0.0.0.0` と先頭ゼロ・空白付きは拒否。"""
    if type(value) is not str:
        raise ConfigError("bind")
    try:
        address = ipaddress.IPv4Address(value)
    except ValueError:
        raise ConfigError("bind") from None
    if str(address) != value or address.is_unspecified:
        raise ConfigError("bind")
    return value


def _web_allow(value: object) -> tuple[ipaddress.IPv4Network, ...]:
    """canonical な数値 IPv4 又は IPv4 CIDR の非空 list。単一 IPv4 は `/32` にする。"""
    if type(value) is not list or not value:
        raise ConfigError("allow")
    networks: list[ipaddress.IPv4Network] = []
    for item in value:
        if type(item) is not str:
            raise ConfigError("allow")
        try:
            if "/" in item:
                network = ipaddress.IPv4Network(item, strict=True)
                if str(network) != item:
                    raise ConfigError("allow")
            else:
                address = ipaddress.IPv4Address(item)
                if str(address) != item:
                    raise ConfigError("allow")
                network = ipaddress.IPv4Network(address)
        except ValueError:
            raise ConfigError("allow") from None
        networks.append(network)
    if len(set(networks)) != len(networks):
        raise ConfigError("allow")
    if WEB_UNRESTRICTED in networks and len(networks) != 1:
        raise ConfigError("allow")
    return tuple(networks)


def _web_auth(value: object) -> str:
    if type(value) is not str or value not in WEB_AUTHS:
        raise ConfigError("auth")
    return value


def _web_user(value: object) -> str:
    """Basic 認証のユーザー名。英小文字で始まる 1〜32 byte の ASCII (`re.fullmatch`)。"""
    if type(value) is not str or WEB_USER_PATTERN.fullmatch(value) is None:
        raise ConfigError("user")
    return value


def read_config(feature: str, *, preserve_config_dir_error: bool = False) -> OpsConfig:
    """固定 schema の設定を、検査済み config dir の fd と共に読む。"""
    if feature not in _FEATURE_KEYS:
        raise ConfigError("feature")
    parsed, config_dir_fd = _read_toml(feature, preserve_config_dir_error=preserve_config_dir_error)
    try:
        required = _COMMON_KEYS + _FEATURE_KEYS[feature]
        expected = required + _OPTIONAL_FEATURE_KEYS[feature]
        for key in required:
            if key not in parsed:
                raise ConfigError(key)
        for key in parsed:
            if key not in expected:
                raise ConfigError(_safe_detail_key(key))

        instance_root = _absolute_path(parsed["instance_root"], "instance_root")
        key_dir = _absolute_path(parsed["key_dir"], "key_dir")
        dirs = feature_dirs(feature)
        validate_key_dir(key_dir, config_dir=dirs.config, state_dir=dirs.state,
                         secret_dir=dirs.secret)
        if feature == "web":
            port = _integer(parsed["port"], "port")
            if not 1 <= port <= 65535:
                raise ConfigError("port")
            return OpsConfig(
                instance_root=instance_root, key_dir=key_dir, config_dir_fd=config_dir_fd,
                port=port,
                bind=_web_bind(parsed["bind"]) if "bind" in parsed else None,
                allow=_web_allow(parsed["allow"]) if "allow" in parsed else None,
                auth=_web_auth(parsed["auth"]) if "auth" in parsed else "none",
                user=_web_user(parsed["user"]) if "user" in parsed else WEB_USER_DEFAULT)
        if feature == "bot":
            return OpsConfig(instance_root=instance_root, key_dir=key_dir,
                             config_dir_fd=config_dir_fd,
                             guild_id=_discord_id(parsed["guild_id"], "guild_id"),
                             channel_id=_discord_id(parsed["channel_id"], "channel_id"),
                             allowlist_user_ids=_discord_ids(parsed["allowlist_user_ids"],
                                                              "allowlist_user_ids"),
                             mention_role_id=(None if "mention_role_id" not in parsed else
                                              _discord_id(parsed["mention_role_id"], "mention_role_id")),
                             mention_user_ids=_discord_ids(parsed["mention_user_ids"],
                                                           "mention_user_ids"),
                             trade_notices=_choice(parsed, "trade_notices", TRADE_NOTICES, "all"),
                             trade_amounts=_choice(parsed, "trade_amounts", TRADE_AMOUNTS,
                                                   "absolute"),
                             ops_notices=_choice(parsed, "ops_notices", OPS_NOTICES, "default"))
        return OpsConfig(instance_root=instance_root, key_dir=key_dir,
                         config_dir_fd=config_dir_fd,
                         deliver_trade_events=_boolean(parsed, "deliver_trade_events", False))
    except BaseException:
        os.close(config_dir_fd)
        raise
