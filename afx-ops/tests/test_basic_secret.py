"""`auth = "basic"` の `web.password`: 起動と probe が同じ関数で読み、固定 reason だけで止まる。"""
from __future__ import annotations

import dataclasses
import os
import socket
import stat
from pathlib import Path

import pytest

from afx_ops import probe
from afx_ops.config import OpsConfig
from afx_ops.isolation import startup

PASSWORD = b"Zx9:colon:rich:pw-22"


def _config(auth: str = "basic") -> OpsConfig:
    return OpsConfig(instance_root=Path("/srv/afx"), key_dir=Path("/srv/keys"),
                     config_dir_fd=None, port=18080, auth=auth)


def _plan(tmp_path: Path, auth: str = "basic") -> startup.StartupPlan:
    plan = startup.StartupPlan.for_feature("web", web_port=18080, web_auth=auth)
    return dataclasses.replace(plan, secret_dir=tmp_path / "ops" / "secret")


def _secret_dir(tmp_path: Path, mode: int = 0o700) -> Path:
    path = tmp_path / "ops" / "secret"
    path.mkdir(parents=True, mode=mode, exist_ok=True)
    path.chmod(mode)
    return path


def _password(directory: Path, data: bytes = PASSWORD, mode: int = 0o600) -> Path:
    path = directory / "web.password"
    path.write_bytes(data)
    path.chmod(mode)
    return path


def _check(tmp_path: Path, auth: str = "basic") -> str | None:
    try:
        startup.check_web_access(_plan(tmp_path, auth), _config(auth))
    except startup.StartupError as exc:
        return exc.reason
    return None


def _read(tmp_path: Path) -> str | bytes:
    try:
        return startup.read_secrets(_plan(tmp_path))["web.password"]
    except startup.StartupError as exc:
        return exc.reason


def _fds() -> set[str]:
    return set(os.listdir("/proc/self/fd"))


# --- 必須集合は検証済み auth から決まる -----------------------------------------------------

def test_basic_requires_the_password_and_none_requires_nothing():
    basic = startup.StartupPlan.for_feature("web", web_port=1, web_auth="basic")
    none = startup.StartupPlan.for_feature("web", web_port=1, web_auth="none")
    assert basic.secret_files == ("web.password",)
    assert none.secret_files == ()


# --- 読む -------------------------------------------------------------------------------------

def test_the_safe_password_is_returned_once_as_bytes_with_colons_kept(tmp_path):
    _password(_secret_dir(tmp_path))
    assert _check(tmp_path) is None
    assert _read(tmp_path) == PASSWORD


def test_none_never_reads_the_password(tmp_path):
    _password(_secret_dir(tmp_path))
    assert startup.read_secrets(_plan(tmp_path, "none")) == {}


# --- AC-34: 7 / 8 / 32 / 33 と文字 class -----------------------------------------------------

@pytest.mark.parametrize(("size", "reason"), [(7, "secret_invalid"), (8, None), (32, None),
                                              (33, "secret_invalid"), (0, "secret_invalid"),
                                              (16, None), (128, "secret_invalid")])
def test_password_size_boundaries_for_basic(tmp_path, size, reason):
    _password(_secret_dir(tmp_path), b"a" * size)
    assert _check(tmp_path) == reason
    assert _read(tmp_path) == (b"a" * size if reason is None else reason)


@pytest.mark.parametrize("data", [
    b"a" * 15 + b" ", b"a" * 15 + b"\n", b"a" * 15 + b"\t", b"a" * 15 + b"\x7f",
    b"a" * 15 + b"\x00", b"a" * 15 + "é".encode(), b"a" * 14 + b"\r\n", b"a" * 14 + b"\xff\xfe",
    b"a" * 8 + b" ", b"a" * 7 + b"\n",
])
def test_password_byte_grammar_for_basic(tmp_path, data):
    _password(_secret_dir(tmp_path), data)
    assert _check(tmp_path) == "secret_invalid"
    assert _read(tmp_path) == "secret_invalid"


def test_every_visible_ascii_byte_is_allowed(tmp_path):
    data = bytes(range(0x21, 0x7f))[:32]
    _password(_secret_dir(tmp_path), data)
    assert _check(tmp_path) is None
    assert _read(tmp_path) == data


# --- 安全性 -------------------------------------------------------------------------------------

@pytest.mark.parametrize("mode", [0o644, 0o640, 0o660, 0o400, 0o700, 0o666])
def test_mode_must_be_exactly_0600(tmp_path, mode):
    _password(_secret_dir(tmp_path), mode=mode)
    assert _check(tmp_path) == "secret_invalid"
    assert _read(tmp_path) == "secret_invalid"


def test_a_loose_secret_dir_is_secret_invalid(tmp_path):
    _password(_secret_dir(tmp_path, 0o755))
    assert _check(tmp_path) == "secret_invalid"
    assert _read(tmp_path) == "secret_invalid"


def test_symlink_directory_and_fifo_are_secret_invalid(tmp_path):
    directory = _secret_dir(tmp_path)
    target = tmp_path / "target"
    target.write_bytes(PASSWORD)
    target.chmod(0o600)
    (directory / "web.password").symlink_to(target)
    assert (_check(tmp_path), _read(tmp_path)) == ("secret_invalid", "secret_invalid")
    (directory / "web.password").unlink()
    (directory / "web.password").mkdir()
    assert (_check(tmp_path), _read(tmp_path)) == ("secret_invalid", "secret_invalid")
    (directory / "web.password").rmdir()
    os.mkfifo(directory / "web.password", 0o600)
    assert (_check(tmp_path), _read(tmp_path)) == ("secret_invalid", "secret_invalid")


def test_another_uid_is_secret_invalid(tmp_path, monkeypatch):
    path = _password(_secret_dir(tmp_path))
    inode, real = path.stat().st_ino, os.fstat

    def foreign(fd):
        st = real(fd)
        if stat.S_ISREG(st.st_mode) and st.st_ino == inode:
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid + 1,
                                   st.st_gid, st.st_size, int(st.st_atime), int(st.st_mtime),
                                   int(st.st_ctime)))
        return st

    monkeypatch.setattr(os, "fstat", foreign)
    assert (_check(tmp_path), _read(tmp_path)) == ("secret_invalid", "secret_invalid")


def test_an_unknown_entry_beside_the_password_is_secret_invalid(tmp_path):
    directory = _secret_dir(tmp_path)
    _password(directory)
    (directory / ".web.password.1234.tmp").write_text("x")
    assert _check(tmp_path) == "secret_invalid"


def test_missing_dir_and_missing_file_are_secret_missing(tmp_path):
    assert _check(tmp_path) == "secret_missing"
    assert _read(tmp_path) == "secret_missing"
    _secret_dir(tmp_path)
    assert _check(tmp_path) == "secret_missing"
    assert _read(tmp_path) == "secret_missing"


def test_a_file_that_vanishes_or_degrades_after_the_check_keeps_a_fixed_reason(tmp_path):
    """検査と読取りの間に差し替わっても `secrets_failed` ではなく web の固定 reason。"""
    directory = _secret_dir(tmp_path)
    path = _password(directory)
    assert _check(tmp_path) is None
    path.unlink()
    assert _read(tmp_path) == "secret_missing"
    _password(directory, b"short")
    assert _read(tmp_path) == "secret_invalid"
    _password(directory)
    directory.chmod(0o755)
    assert _read(tmp_path) == "secret_invalid"


def test_prepare_dirs_never_reports_the_generic_secrets_failed_for_web(tmp_path):
    config_dir = tmp_path / "ops" / "config"
    config_dir.mkdir(parents=True, mode=0o700)
    plan = dataclasses.replace(_plan(tmp_path), config_dir=config_dir,
                               state_dir=tmp_path / "ops" / "state")
    with pytest.raises(startup.StartupError) as info:
        startup.prepare_dirs(plan, key_dir=tmp_path / "keys-elsewhere")
    assert info.value.reason == "secret_missing"


def test_no_fd_is_left_open_on_any_basic_outcome(tmp_path):
    before = _fds()
    directory = _secret_dir(tmp_path)
    for data in (PASSWORD, b"x", b"a" * 33):
        _password(directory, data)
        _check(tmp_path)
        _read(tmp_path)
    (directory / "stray").write_text("x")
    _check(tmp_path)
    assert _fds() == before


# --- 出力 ---------------------------------------------------------------------------------------

def test_a_failure_never_carries_password_bytes(tmp_path):
    _password(_secret_dir(tmp_path), b"visible-but-short")
    for call in (lambda: _check(tmp_path), lambda: startup.read_secrets(_plan(tmp_path))):
        try:
            call()
        except startup.StartupError as exc:
            assert "visible" not in str(exc) and "visible" not in repr(exc)
            assert exc.detail in ("", None) or "visible" not in exc.detail


def test_the_probe_reads_the_same_password_and_reports_missing_and_invalid(home, ops_dirs, capsys):
    dirs = ops_dirs("web")
    dirs.config.mkdir(parents=True, mode=0o700)
    dirs.config.chmod(0o700)
    toml = dirs.config / "ops.toml"
    with socket.socket() as probe_socket:
        probe_socket.bind(("127.0.0.1", 0))
        port = probe_socket.getsockname()[1]
    toml.write_text('instance_root = "/srv/afx-probe"\nkey_dir = "/srv/afx-probe-keys"\n'
                    f'port = {port}\nauth = "basic"\n', encoding="utf-8")
    toml.chmod(0o600)
    assert probe.main("web") == 71
    assert capsys.readouterr().err == "afx-ops: secret_missing\n"
    dirs.secret.mkdir(parents=True, mode=0o700)
    dirs.secret.chmod(0o700)
    _password(dirs.secret, b"short")
    assert probe.main("web") == 71
    assert capsys.readouterr().err == "afx-ops: secret_invalid\n"
    _password(dirs.secret)
    assert probe.main("web") == 0
    assert capsys.readouterr() == ("", "")


def test_an_fstat_error_on_the_secret_dir_is_secret_invalid(tmp_path, monkeypatch):
    directory = _secret_dir(tmp_path)
    _password(directory)
    inode, real = directory.stat().st_ino, os.fstat

    def failing(fd):
        st = real(fd)
        if stat.S_ISDIR(st.st_mode) and st.st_ino == inode:
            raise OSError(5, "injected")
        return st

    before = _fds()
    monkeypatch.setattr(os, "fstat", failing)
    assert (_check(tmp_path), _read(tmp_path)) == ("secret_invalid", "secret_invalid")
    assert _fds() <= before


def test_an_fstat_error_on_the_secret_dir_is_rc_71_secret_invalid_in_the_probe(
        home, ops_dirs, capsys, monkeypatch):
    dirs = ops_dirs("web")
    dirs.config.mkdir(parents=True, mode=0o700)
    dirs.config.chmod(0o700)
    with socket.socket() as probe_socket:
        probe_socket.bind(("127.0.0.1", 0))
        port = probe_socket.getsockname()[1]
    toml = dirs.config / "ops.toml"
    toml.write_text('instance_root = "/srv/afx-probe"\nkey_dir = "/srv/afx-probe-keys"\n'
                    f'port = {port}\nauth = "basic"\n', encoding="utf-8")
    toml.chmod(0o600)
    dirs.secret.mkdir(parents=True, mode=0o700)
    dirs.secret.chmod(0o700)
    _password(dirs.secret)
    inode, real = dirs.secret.stat().st_ino, os.fstat

    def failing(fd):
        st = real(fd)
        if stat.S_ISDIR(st.st_mode) and st.st_ino == inode:
            raise OSError(5, "injected")
        return st

    monkeypatch.setattr(os, "fstat", failing)
    assert probe.main("web") == 71
    assert capsys.readouterr().err == "afx-ops: secret_invalid\n"


def test_a_password_that_vanishes_after_scandir_counts_as_absent(tmp_path, monkeypatch):
    """scandir で見えた `web.password` が open の前に消えたら「無し」。`auth = "none"` では
    許可側に倒れ、`secret_missing` を名乗らない。"""
    secret_dir = _secret_dir(tmp_path)
    _password(secret_dir)
    real_read = startup._read_web_password

    def vanish_then_read(dir_fd):
        os.unlink(secret_dir / startup.WEB_PASSWORD)
        return real_read(dir_fd)

    monkeypatch.setattr(startup, "_read_web_password", vanish_then_read)
    assert startup.inspect_web_secret(secret_dir) is False
