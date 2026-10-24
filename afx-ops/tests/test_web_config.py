"""web の ops.toml の文法 (bind / allow / auth と ops.toml の filesystem 上の安全性)。"""
from __future__ import annotations

import ipaddress
import os
import stat
from pathlib import Path

import pytest

from afx_ops.config import WEB_UNRESTRICTED, ConfigError, OpsConfig, read_config

BASE = 'instance_root = "/srv/afx"\nkey_dir = "/srv/keys"\n'
NET = ipaddress.IPv4Network


def _install(monkeypatch, tmp_path: Path, text: str, *, mode: int = 0o600) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    path = tmp_path / "cfg" / "agentic-fx" / "ops" / "web" / "config" / "ops.toml"
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def _read(monkeypatch, tmp_path, extra: str, **kwargs) -> OpsConfig:
    _install(monkeypatch, tmp_path, BASE + extra, **kwargs)
    config = read_config("web")
    os.close(config.config_dir_fd)
    return config


def _reason(monkeypatch, tmp_path, extra: str, **kwargs) -> str:
    _install(monkeypatch, tmp_path, BASE + extra, **kwargs)
    with pytest.raises(ConfigError) as info:
        read_config("web")
    return info.value.reason


# --- AC-1 / AC-3: 文法 ---------------------------------------------------------------

def test_old_port_only_settings_read_as_a_safe_localhost_configuration(monkeypatch, tmp_path):
    config = _read(monkeypatch, tmp_path, "port = 8080\n")
    assert (config.web_bind, config.allow, config.auth) == ("127.0.0.1", None, "none")
    assert config.web_loopback and config.web_allow_networks() == (NET("127.0.0.0/8"),)


@pytest.mark.parametrize("bind", ["127.0.0.1", "127.0.0.2", "192.168.1.10", "169.254.3.4"])
def test_canonical_numeric_ipv4_binds_are_accepted_by_the_reader(monkeypatch, tmp_path, bind):
    config = _read(monkeypatch, tmp_path, f'port = 8080\nbind = "{bind}"\n')
    assert config.bind == bind and config.web_bind == bind


@pytest.mark.parametrize("bind", [
    '"localhost"', '"example.com"', '"::1"', '"::ffff:127.0.0.1"', '"0.0.0.0"', '" 127.0.0.1"',
    '"127.0.0.1 "', '"127.000.000.001"', '"01.2.3.4"', '"192.168.1.10/24"', '"192.168.1"',
    '""', "1", "true", '["127.0.0.1"]', '"127.0.0.1\\n"',
])
def test_non_canonical_binds_are_config_invalid(monkeypatch, tmp_path, bind):
    """M-T1-BIND-CANON: 先頭ゼロ付き bind の受理は落ちる。"""
    assert _reason(monkeypatch, tmp_path, f"port = 8080\nbind = {bind}\n") == "config_invalid"


@pytest.mark.parametrize(("value", "expected"), [
    ('["192.168.1.10"]', (NET("192.168.1.10/32"),)),
    ('["192.168.1.10/32"]', (NET("192.168.1.10/32"),)),
    ('["192.168.1.0/24", "10.0.0.5"]', (NET("192.168.1.0/24"), NET("10.0.0.5/32"))),
    ('["10.0.0.5", "192.168.1.0/24"]', (NET("10.0.0.5/32"), NET("192.168.1.0/24"))),
    ('["0.0.0.0/0"]', (WEB_UNRESTRICTED,)),
])
def test_allow_accepts_single_ipv4_canonical_cidr_and_the_unrestricted_singleton(
        monkeypatch, tmp_path, value, expected):
    config = _read(monkeypatch, tmp_path, f"port = 8080\nallow = {value}\n")
    assert config.allow == expected


@pytest.mark.parametrize("value", [
    "[]", '[""]', '"192.168.1.0/24"', "[1]", '["fe80::1"]', '["::ffff:10.0.0.1"]',
    '["2001:db8::/32"]', '[" 10.0.0.1"]', '["10.0.0.1 "]', '["10.0.0.0/24 "]',
    '["10.0.0.1/24"]', '["010.0.0.1"]', '["10.0.0.0/033"]', '["10.0.0.0/255.255.255.0"]',
    '["10.0.0.0/24", "10.0.0.0/24"]', '["10.0.0.1", "10.0.0.1/32"]',
    '["0.0.0.0/0", "10.0.0.0/24"]', '["10.0.0.0/24", "0.0.0.0/0"]',
    '["0.0.0.0/0", "0.0.0.0/0"]', '["10.0.0.0/33"]', '["10.0.0.0/"]',
])
def test_malformed_allow_values_are_config_invalid(monkeypatch, tmp_path, value):
    """M-T1-ALLOW-UNRESTRICTED-MIX: 無制限と限定の混在は順序によらず拒否される。"""
    assert _reason(monkeypatch, tmp_path, f"port = 8080\nallow = {value}\n") == "config_invalid"


@pytest.mark.parametrize("value", ['"none"', '"basic"'])
def test_auth_accepts_only_the_two_lowercase_values(monkeypatch, tmp_path, value):
    assert _read(monkeypatch, tmp_path, f"port = 8080\nauth = {value}\n").auth == value.strip('"')


@pytest.mark.parametrize("value", ['"Basic"', '"NONE"', '"digest"', '""', "1", "true", "[]"])
def test_other_auth_values_are_config_invalid(monkeypatch, tmp_path, value):
    assert _reason(monkeypatch, tmp_path, f"port = 8080\nauth = {value}\n") == "config_invalid"


@pytest.mark.parametrize("port", ["0", "65536", "-1", '"8080"', "true", "8080.0"])
def test_port_must_be_an_integer_in_the_tcp_range(monkeypatch, tmp_path, port):
    assert _reason(monkeypatch, tmp_path, f"port = {port}\n") == "config_invalid"


def test_the_port_is_required(monkeypatch, tmp_path):
    assert _reason(monkeypatch, tmp_path, 'bind = "127.0.0.1"\n') == "config_invalid"


@pytest.mark.parametrize("extra", ['proxy = "x"\n', 'tls = true\n', 'trade_notices = "all"\n'])
def test_unknown_keys_are_config_invalid_for_web(monkeypatch, tmp_path, extra):
    assert _reason(monkeypatch, tmp_path, "port = 8080\n" + extra) == "config_invalid"


@pytest.mark.parametrize("text", [
    'key_dir = "/srv/keys"\nport = 8080\n',
    'instance_root = "/srv/afx"\nport = 8080\n',
    'instance_root = 1\nkey_dir = "/srv/keys"\nport = 8080\n',
    'instance_root = "rel"\nkey_dir = "/srv/keys"\nport = 8080\n',
    'instance_root = "/srv/afx"\nkey_dir = "rel"\nport = 8080\n',
    'instance_root = "/srv/afx"\nkey_dir = ["/srv/keys"]\nport = 8080\n',
])
def test_common_keys_are_required_absolute_strings(monkeypatch, tmp_path, text):
    _install(monkeypatch, tmp_path, text)
    with pytest.raises(ConfigError) as info:
        read_config("web")
    assert info.value.reason == "config_invalid"


@pytest.mark.parametrize("raw", [b"port = ", b"\xff\xfe", b"port = 8080\nport = 9\n"])
def test_unparseable_toml_is_config_invalid(monkeypatch, tmp_path, raw):
    path = _install(monkeypatch, tmp_path, "")
    path.write_bytes(raw)
    with pytest.raises(ConfigError) as info:
        read_config("web")
    assert info.value.reason == "config_invalid"


# --- G1 / G2: config dir と ops.toml の安全性 -------------------------------------------

def test_a_missing_config_dir_is_config_dir_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    with pytest.raises(ConfigError) as info:
        read_config("web")
    assert info.value.reason == "config_dir_missing"
    with pytest.raises(ConfigError) as info:
        read_config("web", preserve_config_dir_error=True)
    assert info.value.reason == "config_dir_missing"


def test_a_missing_ops_toml_is_config_invalid(monkeypatch, tmp_path):
    path = _install(monkeypatch, tmp_path, "")
    path.unlink()
    with pytest.raises(ConfigError) as info:
        read_config("web")
    assert info.value.reason == "config_invalid"


def test_an_unsafe_config_dir_is_config_dir_invalid(monkeypatch, tmp_path):
    path = _install(monkeypatch, tmp_path, BASE + "port = 8080\n")
    path.parent.chmod(0o777)
    try:
        with pytest.raises(ConfigError) as info:
            read_config("web")
    finally:
        path.parent.chmod(0o700)
    assert info.value.reason == "config_dir_invalid"


@pytest.mark.parametrize("mode", [0o664, 0o666, 0o620, 0o602])
def test_a_group_or_other_writable_ops_toml_is_config_dir_invalid(monkeypatch, tmp_path, mode):
    """M-T1-TOML-UNSAFE: group writable な `ops.toml` (0664) の受理は落ちる。"""
    assert _reason(monkeypatch, tmp_path, "port = 8080\n", mode=mode) == "config_dir_invalid"


@pytest.mark.parametrize("mode", [0o600, 0o644, 0o640, 0o400])
def test_owner_only_and_world_readable_modes_are_safe(monkeypatch, tmp_path, mode):
    assert _read(monkeypatch, tmp_path, "port = 8080\n", mode=mode).port == 8080


def test_a_symlinked_ops_toml_is_config_dir_invalid(monkeypatch, tmp_path):
    path = _install(monkeypatch, tmp_path, BASE + "port = 8080\n")
    target = tmp_path / "elsewhere.toml"
    target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    target.chmod(0o600)
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(ConfigError) as info:
        read_config("web")
    assert info.value.reason == "config_dir_invalid"


def test_an_ops_toml_directory_is_config_dir_invalid(monkeypatch, tmp_path):
    path = _install(monkeypatch, tmp_path, "")
    path.unlink()
    path.mkdir(mode=0o700)
    with pytest.raises(ConfigError) as info:
        read_config("web")
    assert info.value.reason == "config_dir_invalid"


def test_an_ops_toml_owned_by_another_uid_is_config_dir_invalid(monkeypatch, tmp_path):
    path = _install(monkeypatch, tmp_path, BASE + "port = 8080\n")
    real = os.fstat
    inode = path.stat().st_ino

    def foreign(fd):
        st = real(fd)
        if stat.S_ISREG(st.st_mode) and st.st_ino == inode:
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink,
                                   st.st_uid + 1, st.st_gid, st.st_size, int(st.st_atime),
                                   int(st.st_mtime), int(st.st_ctime)))
        return st

    monkeypatch.setattr(os, "fstat", foreign)
    with pytest.raises(ConfigError) as info:
        read_config("web")
    assert info.value.reason == "config_dir_invalid"


def test_other_features_keep_their_existing_reasons_for_an_unsafe_ops_toml(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    path = tmp_path / "cfg" / "agentic-fx" / "ops" / "notify" / "config" / "ops.toml"
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text(BASE, encoding="utf-8")
    path.chmod(0o666)
    with pytest.raises(ConfigError) as info:
        read_config("notify")
    assert info.value.reason == "config_invalid"


def test_web_config_errors_are_printed_as_a_fixed_reason_only(monkeypatch, tmp_path, capsys):
    from afx_ops import cli
    _install(monkeypatch, tmp_path, BASE + 'port = 8080\nbind = "not-an-ip"\n')
    assert cli.main(["web"]) == 71
    assert capsys.readouterr().err == "afx-ops: config_invalid\n"


def test_the_ops_toml_owner_check_defaults_to_the_effective_uid():
    from afx_ops import config
    assert config._expected_owner_uid is os.getuid


# --- user (AC-12 G8 / AC-34) -----------------------------------------------------------------

def test_the_user_defaults_to_afx_when_the_key_is_omitted(monkeypatch, tmp_path):
    assert _read(monkeypatch, tmp_path, "port = 8080\nauth = \"basic\"\n").user == "afx"


@pytest.mark.parametrize("user", ["afx", "a", "ops-web_1", "a" * 32, "x0", "a-", "a_"])
def test_user_accepts_the_documented_grammar(monkeypatch, tmp_path, user):
    config = _read(monkeypatch, tmp_path, f'port = 8080\nauth = "basic"\nuser = "{user}"\n')
    assert config.user == user


@pytest.mark.parametrize("value", [
    '"Afx"', '"AFX"', '"afx:user"', '"afx\\n"', '"afx\\r"', '"afx "', '" afx"', '""', '"1afx"',
    '"-afx"', '"_afx"', f'"{"a" * 33}"', '"afx\\u00e9"',
    '"afx.b"', '"afx@b"', "1", "true", '["afx"]', '"afx\\u0000"',
])
def test_user_outside_the_grammar_is_config_invalid(monkeypatch, tmp_path, value):
    """大文字・`:`・改行を含む user の受理は落ちる。"""
    assert _reason(monkeypatch, tmp_path,
                   f'port = 8080\nauth = "basic"\nuser = {value}\n') == "config_invalid"


def test_the_user_is_validated_even_when_auth_is_none(monkeypatch, tmp_path):
    assert _reason(monkeypatch, tmp_path, 'port = 8080\nuser = "Afx"\n') == "config_invalid"
