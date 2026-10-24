"""web の ops.toml の writer / reader (本体側): 4 key の round-trip、共通 key、filesystem 安全性。

reader の最終 reason は afx-ops の起動 / probe と同じ決定木で、期待値は
`afx-ops/tests/fixtures/web_access_matrix.json` (afx-ops の試験が仕様の 2 表から機械生成する)
を共有する。
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from agentic_fx.ops.keys import key_dir
from agentic_fx.ops_setup.feature_config import (
    InputError,
    InterfaceLookupFailed,
    collect,
    unsafe_reason,
    validate,
    web_access_reason,
    write_inputs,
)

MATRIX = Path(__file__).resolve().parents[2] / "afx-ops" / "tests" / "fixtures" / "web_access_matrix.json"
LAN = "192.168.1.10"


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    return tmp_path / "c" / "agentic-fx" / "ops" / "web"


@pytest.fixture
def root(tmp_path) -> Path:
    path = tmp_path / "instance"
    path.mkdir()
    return path


def _ops_toml(xdg: Path) -> Path:
    return xdg / "config" / "ops.toml"


# --- AC-29: round-trip ----------------------------------------------------------------

@pytest.mark.parametrize("values", [
    {"port": 8788},
    {"port": 8788, "bind": "127.0.0.1"},
    {"port": 8788, "bind": LAN, "allow": ["192.168.1.0/24"]},
    {"port": 1, "bind": LAN, "allow": ["10.0.0.5", "192.168.1.0/24", "172.16.0.0/12"]},
    {"port": 65535, "bind": LAN, "allow": ["192.168.1.0/24", "10.0.0.5"], "auth": "basic"},
    {"port": 8788, "bind": "127.0.0.1", "allow": ["0.0.0.0/0"], "auth": "none"},
    {"port": 8788, "auth": "none"},
])
def test_write_collect_write_read_keeps_types_order_and_values(root, xdg, values):
    write_inputs(root, "web", values)
    first = _ops_toml(xdg).read_bytes()
    found = collect(root, "web")
    assert found.complete and found.values == values
    assert type(found.values["port"]) is int
    write_inputs(root, "web", found.values)
    assert _ops_toml(xdg).read_bytes() == first
    assert collect(root, "web").values == values
    for name in ("bind", "allow", "auth"):
        if name in values:
            assert type(found.values[name]) is type(values[name])


def test_allow_keeps_the_written_order_and_a_single_ipv4_text(root, xdg):
    write_inputs(root, "web", {"port": 8788, "bind": LAN, "allow": ["10.0.0.5", "192.168.1.0/24"]})
    text = _ops_toml(xdg).read_text()
    assert 'allow = ["10.0.0.5", "192.168.1.0/24"]\n' in text
    assert "10.0.0.5/32" not in text


def test_the_toml_carries_the_injected_common_keys_first_and_the_four_keys_after(root, xdg):
    write_inputs(root, "web", {"port": 8788, "bind": LAN, "allow": ["10.0.0.0/8"], "auth": "none"})
    lines = _ops_toml(xdg).read_text().splitlines()
    assert lines == [f'instance_root = "{root.resolve()}"', f'key_dir = "{key_dir(root)}"',
                     "port = 8788", f'bind = "{LAN}"', 'allow = ["10.0.0.0/8"]', 'auth = "none"']


def test_omitted_optional_keys_stay_omitted_so_old_settings_read_back_unchanged(root, xdg):
    write_inputs(root, "web", {"port": 8788})
    assert _ops_toml(xdg).read_text().splitlines()[2:] == ["port = 8788"]
    assert collect(root, "web").values == {"port": 8788}


# --- 共通 key の注入と拒否 ------------------------------------------------------------------

@pytest.mark.parametrize("extra", [{"instance_root": "/srv/x"}, {"key_dir": "/srv/k"},
                                   {"instance_root": "/srv/x", "key_dir": "/srv/k"}])
def test_common_keys_are_injected_by_the_writer_and_refused_as_input(root, xdg, extra):
    with pytest.raises(InputError):
        write_inputs(root, "web", {"port": 8788, **extra})
    assert not _ops_toml(xdg).exists()
    with pytest.raises(InputError):
        validate("web", {"port": 8788, **extra})


def test_the_writer_never_writes_values_other_than_this_instances_common_keys(root, xdg):
    write_inputs(root, "web", {"port": 8788})
    text = _ops_toml(xdg).read_text()
    assert f'instance_root = "{root.resolve()}"' in text and f'key_dir = "{key_dir(root)}"' in text


# --- 文法 -------------------------------------------------------------------------------

@pytest.mark.parametrize("values", [
    {"port": 0}, {"port": 65536}, {"port": True}, {"port": "80"},
    {"port": 80, "bind": "localhost"}, {"port": 80, "bind": "0.0.0.0"}, {"port": 80, "bind": " 127.0.0.1"},
    {"port": 80, "bind": "01.2.3.4"}, {"port": 80, "bind": "::1"}, {"port": 80, "bind": 1},
    {"port": 80, "allow": []}, {"port": 80, "allow": [""]}, {"port": 80, "allow": "10.0.0.0/8"},
    {"port": 80, "allow": ["10.0.0.1/24"]}, {"port": 80, "allow": ["fe80::1"]},
    {"port": 80, "allow": ["10.0.0.0/8", "10.0.0.0/8"]}, {"port": 80, "allow": [" 10.0.0.1"]},
    {"port": 80, "allow": ["10.0.0.1", "10.0.0.1/32"]},
    {"port": 80, "allow": ["0.0.0.0/0", "10.0.0.0/8"]}, {"port": 80, "allow": ["10.0.0.0/8", "0.0.0.0/0"]},
    {"port": 80, "auth": "Basic"}, {"port": 80, "auth": ""}, {"port": 80, "auth": 1},
])
def test_validate_rejects_malformed_web_values(values):
    with pytest.raises(InputError):
        validate("web", values)


def test_validate_accepts_the_unrestricted_singleton_and_single_ipv4(root, xdg):
    assert validate("web", {"port": 80, "allow": ["0.0.0.0/0"]})["allow"] == ["0.0.0.0/0"]
    assert validate("web", {"port": 80, "allow": ["10.0.0.1"]})["allow"] == ["10.0.0.1"]


def test_an_invalid_value_never_reaches_the_disk(root, xdg):
    with pytest.raises(InputError):
        write_inputs(root, "web", {"port": 80, "bind": "localhost"})
    assert not xdg.exists()


# --- filesystem 安全性 ----------------------------------------------------------------------

def test_the_writer_refuses_to_overwrite_an_unsafe_ops_toml(root, xdg):
    write_inputs(root, "web", {"port": 8788})
    _ops_toml(xdg).chmod(0o666)
    before = _ops_toml(xdg).read_bytes()
    with pytest.raises(OSError):
        write_inputs(root, "web", {"port": 9000})
    assert _ops_toml(xdg).read_bytes() == before


def test_the_writer_refuses_a_symlinked_ops_toml(root, xdg, tmp_path):
    write_inputs(root, "web", {"port": 8788})
    target = tmp_path / "elsewhere.toml"
    target.write_bytes(_ops_toml(xdg).read_bytes())
    _ops_toml(xdg).unlink()
    _ops_toml(xdg).symlink_to(target)
    with pytest.raises(OSError):
        write_inputs(root, "web", {"port": 9000})
    assert target.read_text().count("port = 8788") == 1


@pytest.mark.parametrize("mode", [0o664, 0o666, 0o620])
def test_unsafe_reason_flags_a_group_or_other_writable_ops_toml(root, xdg, mode):
    write_inputs(root, "web", {"port": 8788})
    _ops_toml(xdg).chmod(mode)
    assert unsafe_reason(root, "web") == "config_file"


@pytest.mark.parametrize("mode", [0o644, 0o600])
def test_a_644_or_600_ops_toml_is_safe(root, xdg, mode):
    write_inputs(root, "web", {"port": 8788})
    _ops_toml(xdg).chmod(mode)
    assert unsafe_reason(root, "web") is None


def test_unsafe_reason_flags_a_malformed_new_key(root, xdg):
    write_inputs(root, "web", {"port": 8788})
    with _ops_toml(xdg).open("a", encoding="utf-8") as f:
        f.write('allow = ["0.0.0.0/0", "10.0.0.0/8"]\n')
    assert unsafe_reason(root, "web") == "config_file"


def test_new_files_have_the_documented_modes(root, xdg):
    write_inputs(root, "web", {"port": 8788, "bind": LAN, "allow": ["10.0.0.0/8"]})
    assert stat.S_IMODE(os.stat(xdg).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(xdg / "config").st_mode) == 0o755
    assert stat.S_IMODE(os.stat(xdg / "secret").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(_ops_toml(xdg)).st_mode) == 0o644


# --- AC-12 の 4 入口目: 本体 reader ------------------------------------------------------------

def _rows():
    return json.loads(MATRIX.read_text(encoding="utf-8"))


def _lookup_for(state: str, local: str):
    if state == "na":
        def forbidden():
            raise AssertionError("loopback / 文法違反の入力で interface を列挙した")
        return forbidden
    if state == "failed":
        def failed():
            raise InterfaceLookupFailed
        return failed
    return lambda: [local if state == "local" else "192.168.1.99"]


def _build(row, root: Path, xdg: Path, monkeypatch) -> None:
    """row が表す ops.toml・config dir・secret dir を XDG の領域に置く。"""
    if row.get("config_dir") == "missing":
        return
    config = xdg / "config"
    for directory in (*reversed(xdg.parents[:4]), xdg):
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
    config.mkdir(mode=0o700)
    config.chmod(0o700)
    bind = {"loopback": "127.0.0.1", "local": LAN, "absent": "192.168.77.5",
            "failed": "192.168.77.5"}[row.get("bind", "loopback")]
    text = (row["text"].replace("{ROOT}", str(root.resolve())).replace("{KEYS}", str(key_dir(root)))
            .replace("{PORT}", "8788").replace("{BIND}", bind))
    path = config / "ops.toml"
    kind = row.get("ops_toml", "file")
    if kind in ("file", "other_uid"):
        path.write_bytes(bytes.fromhex(row["bytes_hex"]) if "bytes_hex" in row else text.encode())
        path.chmod(row.get("mode", 0o600))
    elif kind == "symlink":
        target = xdg / "elsewhere.toml"
        target.write_text(text)
        target.chmod(0o600)
        path.symlink_to(target)
    elif kind == "directory":
        path.mkdir(mode=0o700)
    if kind == "other_uid":
        inode, real = path.stat().st_ino, os.fstat

        def foreign(fd):
            st = real(fd)
            if stat.S_ISREG(st.st_mode) and st.st_ino == inode:
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink,
                                       st.st_uid + 1, st.st_gid, st.st_size, int(st.st_atime),
                                       int(st.st_mtime), int(st.st_ctime)))
            return st

        monkeypatch.setattr(os, "fstat", foreign)
    if row.get("config_dir") == "writable":
        config.chmod(0o777)
    secret = row.get("secret", "none")
    if secret != "none":
        (xdg / "secret").mkdir(mode=0o700)
        (xdg / "secret").chmod(0o700)
        if secret in ("safe", "unsafe"):
            password = xdg / "secret" / "web.password"
            password.write_bytes(b"A" * 22)
            password.chmod(0o600)
        if secret == "unsafe":
            (xdg / "secret" / "stray").write_text("x")


@pytest.mark.parametrize("row", _rows(), ids=[row["id"] for row in _rows()])
def test_the_body_reader_returns_the_same_final_reason_as_afx_ops(row, root, xdg, monkeypatch):
    _build(row, root, xdg, monkeypatch)
    got = web_access_reason(root, interfaces=_lookup_for(row["interfaces"], LAN))
    assert got == row["expected"]


def test_an_unset_interface_provider_is_a_lookup_failure_only_for_a_non_loopback_bind(root, xdg):
    write_inputs(root, "web", {"port": 8788, "bind": LAN, "allow": ["10.0.0.0/8"]})
    assert web_access_reason(root) == "interface_lookup_failed"
    write_inputs(root, "web", {"port": 8788, "bind": "127.0.0.1"})
    assert web_access_reason(root) is None


def test_the_reader_leaves_no_fd_open(root, xdg):
    write_inputs(root, "web", {"port": 8788})
    before = set(os.listdir("/proc/self/fd"))
    assert web_access_reason(root) is None
    (xdg / "secret" / "stray").write_text("x")
    assert web_access_reason(root) == "secret_invalid"
    assert set(os.listdir("/proc/self/fd")) == before


def test_a_written_configuration_is_judged_allowed_for_localhost_and_a_limited_lan(root, xdg):
    write_inputs(root, "web", {"port": 8788})
    assert web_access_reason(root) is None
    write_inputs(root, "web", {"port": 8788, "bind": LAN, "allow": ["192.168.1.0/24"]})
    assert web_access_reason(root, interfaces=lambda: [LAN, "192.168.1.11"]) is None
    assert web_access_reason(root, interfaces=lambda: ["192.168.1.11"]) == "bind_not_local"
