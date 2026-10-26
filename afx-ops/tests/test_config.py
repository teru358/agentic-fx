"""機能ごとの ``ops.toml`` を隔離前に安全に読む。"""
from __future__ import annotations

import dataclasses
import os
import sysconfig
from pathlib import Path

import pytest

from afx_ops import config as config_module
from afx_ops.config import ConfigError, read_config
from afx_ops.isolation import startup
from afx_ops.isolation.startup import feature_dirs


def _write_config(monkeypatch, tmp_path: Path, feature: str, text: str) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    path = feature_dirs(feature).config / "ops.toml"
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.mark.parametrize(("feature", "extra", "expected"), [
    ("web", "port = 8080\n", {"port": 8080}),
    ("bot", "guild_id = 12345678901234567\nchannel_id = 12345678901234567890\n"
     "allowlist_user_ids = [12345678901234567]\nmention_role_id = 12345678901234568\n"
     "mention_user_ids = [12345678901234569]\n",
     {"guild_id": 12345678901234567, "channel_id": 12345678901234567890,
      "allowlist_user_ids": (12345678901234567,), "mention_role_id": 12345678901234568,
      "mention_user_ids": (12345678901234569,)}),
    ("notify", "", {}),
])
def test_reads_each_feature_schema(monkeypatch, tmp_path, feature, extra, expected):
    _write_config(monkeypatch, tmp_path, feature,
                  'instance_root = "/srv/afx"\nkey_dir = "/srv/keys"\n' + extra)

    config = read_config(feature)
    try:
        assert config.instance_root == Path("/srv/afx")
        assert config.key_dir == Path("/srv/keys")
        for key, value in expected.items():
            assert getattr(config, key) == value
    finally:
        os.close(config.config_dir_fd)


def test_keeps_common_paths_and_the_checked_config_dir_fd(monkeypatch, tmp_path):
    path = _write_config(monkeypatch, tmp_path, "notify", '''
instance_root = "/srv/afx"
key_dir = "/srv/keys"
''')

    config = read_config("notify")
    try:
        assert config.instance_root == Path("/srv/afx")
        assert config.key_dir == Path("/srv/keys")
        assert os.fstat(config.config_dir_fd).st_ino == path.parent.stat().st_ino
    finally:
        os.close(config.config_dir_fd)


def test_reads_from_the_checked_dir_when_the_path_is_replaced_by_a_symlink(
        monkeypatch, tmp_path):
    path = _write_config(monkeypatch, tmp_path, "notify", '''
instance_root = "/srv/trusted"
key_dir = "/srv/trusted-keys"
''')
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    (attacker / "ops.toml").write_text('instance_root = "/srv/attacker"\nkey_dir = "/srv/keys"\n',
                                         encoding="utf-8")
    original = config_module.open_config_dir

    def open_then_replace(directory):
        fd = original(directory)
        saved = directory.with_name("config-before-swap")
        directory.rename(saved)
        directory.symlink_to(attacker, target_is_directory=True)
        return fd

    monkeypatch.setattr(config_module, "open_config_dir", open_then_replace)
    config = read_config("notify")
    try:
        assert config.instance_root == Path("/srv/trusted")
        assert config.key_dir == Path("/srv/trusted-keys")
        assert os.fstat(config.config_dir_fd).st_ino == (path.parent.with_name(
            "config-before-swap")).stat().st_ino
    finally:
        os.close(config.config_dir_fd)


def test_rejects_an_unsafe_config_dir_as_config_invalid(monkeypatch, tmp_path):
    path = _write_config(monkeypatch, tmp_path, "notify", '''
instance_root = "/srv/afx"
key_dir = "/srv/keys"
''')
    path.parent.chmod(0o777)
    try:
        with pytest.raises(ConfigError) as info:
            read_config("notify")
    finally:
        path.parent.chmod(0o700)
    assert info.value.reason == "config_invalid"


def _fails(monkeypatch, tmp_path, feature: str, text: str) -> ConfigError:
    _write_config(monkeypatch, tmp_path, feature, text)
    with pytest.raises(ConfigError) as info:
        read_config(feature)
    return info.value


def test_rejects_unknown_key_without_exposing_its_value(monkeypatch, tmp_path):
    error = _fails(monkeypatch, tmp_path, "notify", '''
instance_root = "/srv/afx"
key_dir = "/srv/keys"
unexpected = "not-to-leak"
''')
    assert error.reason == "config_invalid"
    assert error.detail == "unexpected"
    assert "not-to-leak" not in str(error)


def test_rejects_wrong_value_type(monkeypatch, tmp_path):
    error = _fails(monkeypatch, tmp_path, "web", '''
instance_root = "/srv/afx"
key_dir = "/srv/keys"
port = true
''')
    assert error.reason == "config_invalid"
    assert error.detail == "port"


def test_rejects_discord_id_with_wrong_number_of_digits(monkeypatch, tmp_path):
    error = _fails(monkeypatch, tmp_path, "bot", '''
instance_root = "/srv/afx"
key_dir = "/srv/keys"
guild_id = 1234567890123456
channel_id = 12345678901234567
allowlist_user_ids = []
mention_role_id = 12345678901234567
mention_user_ids = []
''')
    assert error.reason == "config_invalid"
    assert error.detail == "guild_id"


@pytest.mark.parametrize(("key", "value"), [
    ("allowlist_user_ids", "[12345678901234567, 12345678901234567]"),
    ("allowlist_user_ids", "[true]"),
    ("mention_role_id", "1234567890123456"),
    ("mention_user_ids", "\"12345678901234567\""),
])
def test_rejects_invalid_bot_allowlist_or_mention_ids(monkeypatch, tmp_path, key, value):
    bot_values = {
        "allowlist_user_ids": "[]",
        "mention_role_id": "12345678901234569",
        "mention_user_ids": "[]",
    }
    bot_values[key] = value
    error = _fails(monkeypatch, tmp_path, "bot", f'''\
instance_root = "/srv/afx"
key_dir = "/srv/keys"
guild_id = 12345678901234567
channel_id = 12345678901234568
allowlist_user_ids = {bot_values["allowlist_user_ids"]}
mention_role_id = {bot_values["mention_role_id"]}
mention_user_ids = {bot_values["mention_user_ids"]}
''')
    assert error.reason == "config_invalid"
    assert error.detail == key


def test_bot_mention_role_is_optional_but_the_user_mentions_are_not(monkeypatch, tmp_path):
    _write_config(monkeypatch, tmp_path, "bot", '''
instance_root = "/srv/afx"
key_dir = "/srv/keys"
guild_id = 12345678901234567
channel_id = 12345678901234568
allowlist_user_ids = []
mention_user_ids = []
''')
    config = read_config("bot")
    try:
        assert config.mention_role_id is None
    finally:
        os.close(config.config_dir_fd)


def test_rejects_web_port_zero_in_production(monkeypatch, tmp_path):
    error = _fails(monkeypatch, tmp_path, "web", '''
instance_root = "/srv/afx"
key_dir = "/srv/keys"
port = 0
''')
    assert error.reason == "config_invalid"
    assert error.detail == "port"


def test_read_config_has_no_port_zero_bypass(monkeypatch, tmp_path):
    _write_config(monkeypatch, tmp_path, "web", '''
instance_root = "/srv/afx"
key_dir = "/srv/keys"
port = 1
''')
    with pytest.raises(TypeError):
        read_config("web", allow_zero=True)


def test_refuses_a_symlinked_config_file(monkeypatch, tmp_path):
    path = _write_config(monkeypatch, tmp_path, "notify", '''
instance_root = "/srv/afx"
key_dir = "/srv/keys"
''')
    target = tmp_path / "target.toml"
    target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.unlink()
    path.symlink_to(target)

    with pytest.raises(ConfigError) as info:
        read_config("notify")

    assert info.value.reason == "config_invalid"


def test_refuses_a_config_file_writable_by_other_users(monkeypatch, tmp_path):
    path = _write_config(monkeypatch, tmp_path, "notify", '''
instance_root = "/srv/afx"
key_dir = "/srv/keys"
''')
    path.chmod(0o622)

    with pytest.raises(ConfigError) as info:
        read_config("notify")

    assert info.value.reason == "config_invalid"


def test_rejects_a_missing_required_key(monkeypatch, tmp_path):
    error = _fails(monkeypatch, tmp_path, "notify", 'instance_root = "/srv/afx"\n')
    assert error.reason == "config_invalid"
    assert error.detail == "key_dir"


@pytest.mark.parametrize(("where", "suffix", "detail"), [
    ("config", "keys", "key_dir inside config_dir"),
    ("state", ".", "key_dir inside state_dir"),
    ("secret", "keys", "key_dir inside secret_dir"),
])
def test_rejects_a_key_dir_inside_a_feature_directory(monkeypatch, tmp_path, where, suffix,
                                                       detail):
    path = _write_config(monkeypatch, tmp_path, "notify", '''
instance_root = "/srv/afx"
key_dir = "/srv/keys"
''')
    key_dir = getattr(feature_dirs("notify"), where) / suffix
    path.write_text(f'instance_root = "/srv/afx"\nkey_dir = "{key_dir}"\n', encoding="utf-8")

    with pytest.raises(ConfigError) as info:
        read_config("notify")

    assert info.value.reason == "config_invalid"
    assert info.value.detail == detail


def test_accepts_a_key_dir_separate_from_feature_directories(monkeypatch, tmp_path):
    path = _write_config(monkeypatch, tmp_path, "notify", '''
instance_root = "/srv/afx"
key_dir = "/srv/keys"
''')
    key_dir = feature_dirs("notify").config.with_name("config-keys")
    path.write_text(f'instance_root = "/srv/afx"\nkey_dir = "{key_dir}"\n', encoding="utf-8")

    config = read_config("notify")
    try:
        assert config.key_dir == key_dir
    finally:
        os.close(config.config_dir_fd)


def test_rejects_a_key_dir_symlink_resolving_inside_a_feature_directory(monkeypatch, tmp_path):
    path = _write_config(monkeypatch, tmp_path, "notify", '''
instance_root = "/srv/afx"
key_dir = "/srv/keys"
''')
    key_dir = tmp_path / "key-link"
    key_dir.symlink_to(feature_dirs("notify").config / "keys")
    path.write_text(f'instance_root = "/srv/afx"\nkey_dir = "{key_dir}"\n', encoding="utf-8")

    with pytest.raises(ConfigError) as info:
        read_config("notify")

    assert info.value.detail == "key_dir inside config_dir"


@pytest.mark.parametrize("directory", [
    lambda: Path(sysconfig.get_paths()["purelib"]),
    lambda: Path("/etc/ssl"),
])
def test_rejects_a_key_dir_inside_a_runtime_or_tls_rule(monkeypatch, tmp_path, directory):
    path = _write_config(monkeypatch, tmp_path, "notify", '''
instance_root = "/srv/afx"
key_dir = "/srv/keys"
''')
    key_dir = directory()
    path.write_text(f'instance_root = "/srv/afx"\nkey_dir = "{key_dir}"\n', encoding="utf-8")

    with pytest.raises(ConfigError) as info:
        read_config("notify")

    assert info.value.reason == "config_invalid"
    assert info.value.detail == "key_dir inside runtime_or_dns_tls"


def test_key_dir_with_a_nul_is_config_invalid(monkeypatch, tmp_path):
    _write_config(monkeypatch, tmp_path, "notify", '''
instance_root = "/srv/afx"
key_dir = "/srv/keys\\u0000suffix"
''')

    with pytest.raises(ConfigError) as info:
        read_config("notify")

    assert info.value.reason == "config_invalid"
    assert info.value.detail == "key_dir"


def test_key_dir_ancestor_open_error_is_config_invalid(tmp_path, monkeypatch):
    key_dir = tmp_path / "keys"
    key_dir.mkdir()
    fd = os.open(key_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    original_open = config_module.os.open

    def reject_parent(path, *args, **kwargs):
        if path == "..":
            raise PermissionError()
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(config_module.os, "open", reject_parent)
    try:
        with pytest.raises(ConfigError) as info:
            config_module._ancestor_identities(fd)
    finally:
        os.close(fd)

    assert info.value.reason == "config_invalid"
    assert info.value.detail == "key_dir"


def test_key_dir_ancestor_identity_error_closes_the_parent_fd(tmp_path, monkeypatch):
    key_dir = tmp_path / "keys"
    key_dir.mkdir()
    fd = os.open(key_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    opened = []
    original_open = config_module.os.open
    original_identity = config_module._directory_identity

    def track_parent(path, *args, **kwargs):
        result = original_open(path, *args, **kwargs)
        if path == "..":
            opened.append(result)
        return result

    calls = 0
    def fail_for_parent(candidate):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ConfigError("key_dir")
        return original_identity(candidate)

    monkeypatch.setattr(config_module.os, "open", track_parent)
    monkeypatch.setattr(config_module, "_directory_identity", fail_for_parent)
    try:
        with pytest.raises(ConfigError):
            config_module._ancestor_identities(fd)
    finally:
        os.close(fd)

    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


@pytest.mark.parametrize(("suffix", "detail"), [
    ("alias/bot/state", "key_dir inside state_dir"),
    ("bot/config/../config/", "key_dir inside config_dir"),
    ("alias/bot/secret", "key_dir inside secret_dir"),
])
def test_checked_directory_inodes_reject_key_dir_aliases_even_when_path_normalization_misses(
        monkeypatch, tmp_path, suffix, detail):
    root = tmp_path / "root"
    config_dir = root / "bot" / "config"
    secret_dir = root / "bot" / "secret"
    state_dir = root / "bot" / "state"
    for directory in (config_dir, secret_dir, state_dir):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
    alias = root / "alias"
    alias.symlink_to(root, target_is_directory=True)
    plan = startup.StartupPlan("bot", config_dir, secret_dir, ("token",), state_dir, None, (),
                               False, False)
    monkeypatch.setattr(config_module.os.path, "realpath", lambda path: str(path))

    with pytest.raises(ConfigError) as info:
        startup.prepare_dirs(plan, key_dir=root / suffix)

    assert info.value.detail == detail


@pytest.mark.skip(reason="bind mount の作成にはこの非 root test 環境にない CAP_SYS_ADMIN が要る")
def test_checked_directory_inodes_reject_a_bind_mount_alias():
    """bind mount は別の path でも同じ directory inode を返すことを実機で確認する。"""


def test_rejects_web_port_above_the_tcp_range(monkeypatch, tmp_path):
    error = _fails(monkeypatch, tmp_path, "web", '''
instance_root = "/srv/afx"
key_dir = "/srv/keys"
port = 65536
''')
    assert error.reason == "config_invalid"
    assert error.detail == "port"


def test_rejects_a_discord_id_of_twenty_one_digits(monkeypatch, tmp_path):
    error = _fails(monkeypatch, tmp_path, "bot", '''
instance_root = "/srv/afx"
key_dir = "/srv/keys"
guild_id = 12345678901234567
channel_id = 100000000000000000000
allowlist_user_ids = [12345678901234567]
mention_user_ids = []
''')
    assert error.reason == "config_invalid"
    assert error.detail == "channel_id"


def test_rejects_a_discord_id_given_as_a_string(monkeypatch, tmp_path):
    error = _fails(monkeypatch, tmp_path, "bot", '''
instance_root = "/srv/afx"
key_dir = "/srv/keys"
guild_id = "12345678901234567"
channel_id = 12345678901234567890
allowlist_user_ids = [12345678901234567]
mention_user_ids = []
''')
    assert error.reason == "config_invalid"
    assert error.detail == "guild_id"


def test_refuses_a_symlinked_config_file_whose_target_is_private(monkeypatch, tmp_path):
    path = _write_config(monkeypatch, tmp_path, "notify", '''
instance_root = "/srv/afx"
key_dir = "/srv/keys"
''')
    target = tmp_path / "target.toml"
    target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    target.chmod(0o600)
    path.unlink()
    path.symlink_to(target)

    with pytest.raises(ConfigError) as info:
        read_config("notify")

    assert info.value.reason == "config_invalid"


def test_refuses_a_config_file_writable_by_the_group(monkeypatch, tmp_path):
    path = _write_config(monkeypatch, tmp_path, "notify", '''
instance_root = "/srv/afx"
key_dir = "/srv/keys"
''')
    path.chmod(0o620)

    with pytest.raises(ConfigError) as info:
        read_config("notify")

    assert info.value.reason == "config_invalid"


_BASE = 'instance_root = "/srv/afx"\nkey_dir = "/srv/keys"\n'
_BOT = ("guild_id = 12345678901234567\nchannel_id = 12345678901234568\n"
        "allowlist_user_ids = [12345678901234567]\nmention_user_ids = []\n")


def _read_closing(feature: str):
    config = read_config(feature)
    os.close(config.config_dir_fd)
    return config


def test_ac23_old_ops_toml_without_the_trade_keys_gets_the_defaults(monkeypatch, tmp_path):
    _write_config(monkeypatch, tmp_path, "bot", _BASE + _BOT)
    config = _read_closing("bot")
    assert (config.trade_notices, config.trade_amounts, config.ops_notices) == ("all", "absolute", "default")
    _write_config(monkeypatch, tmp_path / "n", "notify", _BASE)
    assert _read_closing("notify").deliver_trade_events is False


@pytest.mark.parametrize("notices", ["all", "results", "fills_closes", "off"])
@pytest.mark.parametrize("amounts", ["absolute", "relative"])
def test_ac23_bot_reads_trade_notices_and_trade_amounts(monkeypatch, tmp_path, notices, amounts):
    _write_config(monkeypatch, tmp_path, "bot",
                  _BASE + _BOT + f'trade_notices = "{notices}"\ntrade_amounts = "{amounts}"\n')
    config = _read_closing("bot")
    assert (config.trade_notices, config.trade_amounts) == (notices, amounts)


@pytest.mark.parametrize("notices", ["default", "all"])
def test_ac18_bot_reads_ops_notices(monkeypatch, tmp_path, notices):
    _write_config(monkeypatch, tmp_path, "bot", _BASE + _BOT + f'ops_notices = "{notices}"\n')
    assert _read_closing("bot").ops_notices == notices


@pytest.mark.parametrize("line", ['trade_notices = "ALL"', 'trade_notices = ""',
                                  'trade_notices = true', 'trade_notices = 1',
                                  'trade_notices = ["all"]', 'trade_amounts = "abs"',
                                  'trade_amounts = 1', 'trade_amounts = false',
                                  'ops_notices = "everything"', 'ops_notices = 1',
                                  'ops_notices = "none"', 'ops_notices = "ALL"', 'ops_notices = ""',
                                  'ops_notices = true',
                                  'deliver_trade_events = true'])
def test_ac23_bot_rejects_invalid_values_and_notify_only_keys(monkeypatch, tmp_path, line):
    _write_config(monkeypatch, tmp_path, "bot", _BASE + _BOT + line + "\n")
    with pytest.raises(ConfigError):
        read_config("bot")


@pytest.mark.parametrize("value,expected", [("true", True), ("false", False)])
def test_ac23_notify_reads_deliver_trade_events(monkeypatch, tmp_path, value, expected):
    _write_config(monkeypatch, tmp_path, "notify", _BASE + f"deliver_trade_events = {value}\n")
    assert _read_closing("notify").deliver_trade_events is expected


@pytest.mark.parametrize("line", ['deliver_trade_events = "true"', "deliver_trade_events = 1",
                                  "deliver_trade_events = 0", 'trade_notices = "all"',
                                  'trade_amounts = "absolute"'])
def test_ac23_notify_rejects_invalid_values_and_bot_only_keys(monkeypatch, tmp_path, line):
    _write_config(monkeypatch, tmp_path, "notify", _BASE + line + "\n")
    with pytest.raises(ConfigError):
        read_config("notify")


def test_ac23_web_rejects_the_trade_keys(monkeypatch, tmp_path):
    _write_config(monkeypatch, tmp_path, "web", _BASE + 'port = 8080\ntrade_notices = "all"\n')
    with pytest.raises(ConfigError):
        read_config("web")


def test_the_ops_notices_default_holds_for_features_that_do_not_read_it(monkeypatch, tmp_path):
    _write_config(monkeypatch, tmp_path, "notify", _BASE)
    assert _read_closing("notify").ops_notices == "default"


def test_old_ops_toml_without_the_improve_keys_gets_the_defaults(monkeypatch, tmp_path):
    _write_config(monkeypatch, tmp_path, "bot", _BASE + _BOT)
    assert _read_closing("bot").improve_notices == "all"
    _write_config(monkeypatch, tmp_path / "n", "notify", _BASE)
    assert _read_closing("notify").deliver_improve_events is False


@pytest.mark.parametrize("notices", ["all", "results", "off"])
def test_bot_reads_improve_notices(monkeypatch, tmp_path, notices):
    _write_config(monkeypatch, tmp_path, "bot", _BASE + _BOT + f'improve_notices = "{notices}"\n')
    assert _read_closing("bot").improve_notices == notices


@pytest.mark.parametrize("line", ['improve_notices = "ALL"', 'improve_notices = ""',
                                  'improve_notices = true', 'improve_notices = 1',
                                  'improve_notices = ["all"]', 'improve_notices = "fills_closes"',
                                  'improve_notices = "default"', 'improve_notices = "none"',
                                  'deliver_improve_events = true'])
def test_bot_rejects_invalid_improve_values_and_notify_only_keys(monkeypatch, tmp_path, line):
    _write_config(monkeypatch, tmp_path, "bot", _BASE + _BOT + line + "\n")
    with pytest.raises(ConfigError) as info:
        read_config("bot")
    if line.startswith("improve_notices"):
        assert info.value.detail == "improve_notices"


@pytest.mark.parametrize("value,expected", [("true", True), ("false", False)])
def test_notify_reads_deliver_improve_events(monkeypatch, tmp_path, value, expected):
    _write_config(monkeypatch, tmp_path, "notify", _BASE + f"deliver_improve_events = {value}\n")
    assert _read_closing("notify").deliver_improve_events is expected


@pytest.mark.parametrize("line", ['deliver_improve_events = "true"', "deliver_improve_events = 1",
                                  "deliver_improve_events = 0", 'improve_notices = "all"'])
def test_notify_rejects_invalid_improve_values_and_bot_only_keys(monkeypatch, tmp_path, line):
    _write_config(monkeypatch, tmp_path, "notify", _BASE + line + "\n")
    with pytest.raises(ConfigError):
        read_config("notify")


@pytest.mark.parametrize("line", ['improve_notices = "all"', "deliver_improve_events = true"])
def test_web_rejects_the_improve_keys(monkeypatch, tmp_path, line):
    _write_config(monkeypatch, tmp_path, "web", _BASE + "port = 8080\n" + line + "\n")
    with pytest.raises(ConfigError):
        read_config("web")


def test_the_improve_setting_read_at_start_does_not_follow_a_later_file_change(
        monkeypatch, tmp_path):
    path = _write_config(monkeypatch, tmp_path, "bot",
                         _BASE + _BOT + 'improve_notices = "results"\n')
    config = _read_closing("bot")

    path.write_text(path.read_text(encoding="utf-8").replace("results", "off"), encoding="utf-8")

    assert config.improve_notices == "results"
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.improve_notices = "off"  # type: ignore[misc]
