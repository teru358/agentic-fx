"""web の公開範囲の組み合わせ (allow の必須、capability、interface の実在、secret dir)。"""
from __future__ import annotations

import dataclasses
import ipaddress
import os
import stat
from pathlib import Path

import pytest

from afx_ops import interfaces
from afx_ops.config import WEB_UNRESTRICTED, OpsConfig
from afx_ops.interfaces import Candidate, InterfaceLookupError
from afx_ops.isolation import startup

NET = ipaddress.IPv4Network
PASSWORD = b"A" * 22
LAN = "192.168.1.10"


def _config(bind: str | None = None, allow=None, auth: str = "none") -> OpsConfig:
    return OpsConfig(instance_root=Path("/srv/afx"), key_dir=Path("/srv/keys"),
                     config_dir_fd=None, port=18080, bind=bind, auth=auth,
                     allow=None if allow is None else tuple(NET(item) for item in allow))


def _plan(tmp_path: Path, config: OpsConfig) -> startup.StartupPlan:
    plan = startup.StartupPlan.for_feature("web", web_port=config.port,
                                           web_bind=config.web_bind, web_auth=config.auth)
    return dataclasses.replace(plan, secret_dir=tmp_path / "ops" / "secret")


class Lookup:
    def __init__(self, *addresses: str, fail: bool = False) -> None:
        self.addresses, self.fail, self.calls = addresses, fail, 0

    def __call__(self):
        self.calls += 1
        if self.fail:
            raise InterfaceLookupError
        return [Candidate(a, 2, "global") for a in self.addresses]


def _check(tmp_path, config: OpsConfig, lookup=None) -> str | None:
    try:
        startup.check_web_access(_plan(tmp_path, config), config,
                                 lookup=lookup if lookup is not None else Lookup(LAN))
    except startup.StartupError as exc:
        return exc.reason
    return None


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


def _fds() -> set[str]:
    return set(os.listdir("/proc/self/fd"))


# --- AC-4: allow の必須 -------------------------------------------------------------

def test_loopback_runs_with_allow_omitted_limited_and_without_an_interface_lookup(tmp_path):
    lookup = Lookup(fail=True)
    for allow in (None, ["127.0.0.0/8"], ["10.0.0.0/8"]):
        assert _check(tmp_path, _config(allow=allow), lookup) is None
        assert _check(tmp_path, _config(bind="127.0.0.2", allow=allow), lookup) is None
    assert lookup.calls == 0


def test_a_non_loopback_bind_without_allow_is_allow_required_before_any_lookup(tmp_path):
    """M-T1-ALLOW-MISSING: 非 loopback の allow 省略を通す変異は落ちる。"""
    lookup = Lookup(fail=True)
    assert _check(tmp_path, _config(bind=LAN), lookup) == "allow_required"
    assert lookup.calls == 0


def test_a_non_loopback_bind_with_a_limited_allow_runs(tmp_path):
    assert _check(tmp_path, _config(bind=LAN, allow=["192.168.1.0/24"])) is None


# --- capability の優先順位 --------------------------------------------------------------

@pytest.mark.parametrize("bind", [None, LAN])
def test_a_singleton_unrestricted_allow_is_not_supported(tmp_path, bind):
    """M-T1-CAP-UNRESTRICTED: T1 で singleton unrestricted を通す変異は落ちる。"""
    lookup = Lookup(fail=True)
    assert _check(tmp_path, _config(bind=bind, allow=["0.0.0.0/0"]), lookup) == (
        "unrestricted_not_supported")
    assert lookup.calls == 0


@pytest.mark.parametrize("bind", [None, LAN])
def test_basic_auth_is_not_supported(tmp_path, bind):
    """M-T1-CAP-BASIC: T1 で basic を通す変異は落ちる。"""
    lookup = Lookup(fail=True)
    assert _check(tmp_path, _config(bind=bind, allow=["192.168.1.0/24"], auth="basic"),
                  lookup) == "auth_not_supported"
    assert lookup.calls == 0


def test_unrestricted_comes_before_basic(tmp_path):
    config = _config(bind=LAN, allow=["0.0.0.0/0"], auth="basic")
    assert _check(tmp_path, config) == "unrestricted_not_supported"


def test_allow_required_comes_before_the_capability_checks(tmp_path):
    assert _check(tmp_path, _config(bind=LAN, auth="basic")) == "allow_required"


def test_capability_checks_come_before_the_secret_check(tmp_path):
    _password(_secret_dir(tmp_path))
    (tmp_path / "ops" / "secret" / "stray").write_text("x")
    assert _check(tmp_path, _config(allow=["0.0.0.0/0"])) == "unrestricted_not_supported"
    assert _check(tmp_path, _config(auth="basic")) == "auth_not_supported"


# --- AC-1: interface の実在 --------------------------------------------------------------

def test_a_bind_that_exists_on_an_interface_is_accepted(tmp_path):
    assert _check(tmp_path, _config(bind=LAN, allow=["10.0.0.0/8"]), Lookup(LAN)) is None


def test_a_secondary_address_is_accepted_as_a_bind(tmp_path):
    lookup = Lookup("192.168.1.10", "192.168.1.11")
    assert _check(tmp_path, _config(bind="192.168.1.11", allow=["10.0.0.0/8"]), lookup) is None


def test_a_bind_missing_after_a_successful_lookup_is_bind_not_local(tmp_path):
    """M-T1-BIND-LOCAL: 列挙成功後に存在しない bind を受理する変異は落ちる。"""
    config = _config(bind="192.168.77.5", allow=["10.0.0.0/8"])
    assert _check(tmp_path, config, Lookup(LAN)) == "bind_not_local"
    assert _check(tmp_path, config, Lookup()) == "bind_not_local"


def test_a_failed_lookup_is_interface_lookup_failed_and_distinct_from_absence(tmp_path):
    config = _config(bind=LAN, allow=["10.0.0.0/8"])
    assert _check(tmp_path, config, Lookup(fail=True)) == "interface_lookup_failed"


def test_the_default_lookup_is_the_shared_enumeration(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(interfaces, "list_interfaces",
                        lambda: seen.append(1) or [Candidate(LAN, 2, "global")])
    config = _config(bind=LAN, allow=["10.0.0.0/8"])
    startup.check_web_access(_plan(tmp_path, config), config)
    assert seen == [1]


def test_a_real_enumerated_address_is_accepted_as_a_bind(tmp_path):
    found = interfaces.list_interfaces()
    if not found:
        pytest.skip("この host に global / link の IPv4 が無い")
    config = _config(bind=found[0].address, allow=["10.0.0.0/8"])
    startup.check_web_access(_plan(tmp_path, config), config)


# --- AC-13 / AC-33: secret dir -------------------------------------------------------------

def test_a_missing_secret_dir_and_an_empty_one_are_allowed_for_none(tmp_path):
    config = _config()
    assert _check(tmp_path, config) is None
    _secret_dir(tmp_path)
    assert _check(tmp_path, config) is None


def test_a_safe_password_left_behind_is_secret_unexpected(tmp_path):
    """M-T1-NONE-RESIDUE: auth=none の安全な残骸を無視する変異は落ちる。"""
    _password(_secret_dir(tmp_path))
    assert _check(tmp_path, _config()) == "secret_unexpected"
    assert _check(tmp_path, _config(bind=LAN, allow=["10.0.0.0/8"])) == "secret_unexpected"


@pytest.mark.parametrize("name", ["token", "web.password.bak", ".web.password.0123.tmp", "x"])
def test_an_unknown_entry_is_secret_invalid(tmp_path, name):
    """M-T1-UNKNOWN-SECRET: 許可外の secret entry を通す変異は落ちる。"""
    directory = _secret_dir(tmp_path)
    (directory / name).write_text("x")
    assert _check(tmp_path, _config()) == "secret_invalid"


def test_an_unknown_entry_next_to_a_safe_password_is_secret_invalid_not_unexpected(tmp_path):
    directory = _secret_dir(tmp_path)
    _password(directory)
    (directory / "other").write_text("x")
    assert _check(tmp_path, _config()) == "secret_invalid"


def test_a_subdirectory_entry_is_secret_invalid(tmp_path):
    (_secret_dir(tmp_path) / "nested").mkdir()
    assert _check(tmp_path, _config()) == "secret_invalid"


@pytest.mark.parametrize(("size", "reason"), [
    (15, "secret_invalid"), (16, "secret_unexpected"), (128, "secret_unexpected"),
    (129, "secret_invalid"), (0, "secret_invalid"),
])
def test_password_size_boundaries(tmp_path, size, reason):
    _password(_secret_dir(tmp_path), b"a" * size)
    assert _check(tmp_path, _config()) == reason


@pytest.mark.parametrize("data", [
    b"a" * 15 + b" ", b"a" * 15 + b"\n", b"a" * 15 + b"\t", b"a" * 15 + b"\x7f",
    b"a" * 15 + b"\x00", b"a" * 15 + "é".encode(), b"a" * 15 + b"\xff", b"a" * 14 + b"\r\n",
])
def test_password_byte_grammar_rejects_space_controls_and_non_ascii(tmp_path, data):
    _password(_secret_dir(tmp_path), data)
    assert _check(tmp_path, _config()) == "secret_invalid"


def test_a_password_with_colons_and_all_visible_ascii_edges_is_grammatical(tmp_path):
    _password(_secret_dir(tmp_path), b"!~:a:b:c:d:e:f:g:h:i:j")
    assert _check(tmp_path, _config()) == "secret_unexpected"


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o660, 0o400, 0o700, 0o666])
def test_password_mode_must_be_exactly_0600(tmp_path, mode):
    _password(_secret_dir(tmp_path), mode=mode)
    assert _check(tmp_path, _config()) == "secret_invalid"


def test_a_symlinked_password_is_secret_invalid(tmp_path):
    directory = _secret_dir(tmp_path)
    target = tmp_path / "target"
    target.write_bytes(PASSWORD)
    target.chmod(0o600)
    (directory / "web.password").symlink_to(target)
    assert _check(tmp_path, _config()) == "secret_invalid"


def test_a_password_that_is_a_directory_or_fifo_is_secret_invalid(tmp_path):
    directory = _secret_dir(tmp_path)
    (directory / "web.password").mkdir()
    assert _check(tmp_path, _config()) == "secret_invalid"
    (directory / "web.password").rmdir()
    os.mkfifo(directory / "web.password", 0o600)
    assert _check(tmp_path, _config()) == "secret_invalid"


def test_a_password_owned_by_another_uid_is_secret_invalid(tmp_path, monkeypatch):
    path = _password(_secret_dir(tmp_path))
    inode = path.stat().st_ino
    real = os.fstat

    def foreign(fd):
        st = real(fd)
        if stat.S_ISREG(st.st_mode) and st.st_ino == inode:
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid + 1,
                                   st.st_gid, st.st_size, int(st.st_atime), int(st.st_mtime),
                                   int(st.st_ctime)))
        return st

    monkeypatch.setattr(os, "fstat", foreign)
    assert _check(tmp_path, _config()) == "secret_invalid"


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o770, 0o500, 0o600])
def test_the_secret_dir_must_be_0700(tmp_path, mode):
    _secret_dir(tmp_path, mode=0o700)
    (tmp_path / "ops" / "secret").chmod(mode)
    try:
        assert _check(tmp_path, _config()) == "secret_invalid"
    finally:
        (tmp_path / "ops" / "secret").chmod(0o700)


def test_a_symlinked_secret_dir_is_secret_invalid(tmp_path):
    real = tmp_path / "real-secret"
    real.mkdir(mode=0o700)
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops" / "secret").symlink_to(real)
    assert _check(tmp_path, _config()) == "secret_invalid"


def test_a_secret_dir_that_is_a_file_is_secret_invalid(tmp_path):
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops" / "secret").write_text("x")
    assert _check(tmp_path, _config()) == "secret_invalid"


def test_the_interface_check_comes_before_the_secret_check(tmp_path):
    (_secret_dir(tmp_path) / "stray").write_text("x")
    config = _config(bind=LAN, allow=["10.0.0.0/8"])
    assert _check(tmp_path, config, Lookup(fail=True)) == "interface_lookup_failed"
    assert _check(tmp_path, config, Lookup()) == "bind_not_local"
    assert _check(tmp_path, config, Lookup(LAN)) == "secret_invalid"


def test_password_is_read_through_the_checked_dir_fd_and_every_fd_is_closed(tmp_path, monkeypatch):
    directory = _secret_dir(tmp_path)
    _password(directory)
    opened = []
    real = os.open

    def spy(path, flags, mode=0o777, *, dir_fd=None):
        opened.append((os.fspath(path), flags, dir_fd))
        return real(path, flags, mode, dir_fd=dir_fd)

    before = _fds()
    with monkeypatch.context() as scoped:
        scoped.setattr(os, "open", spy)
        assert _check(tmp_path, _config()) == "secret_unexpected"
    assert _fds() == before
    leaf = [item for item in opened if item[0] == "web.password"]
    assert len(leaf) == 1
    assert leaf[0][1] & os.O_NOFOLLOW and leaf[0][2] is not None
    assert not any(item[0].endswith("web.password") and item[2] is None for item in opened)


def test_a_missing_secret_dir_is_never_opened_as_a_file(tmp_path, monkeypatch):
    opened = []
    real = os.open

    def spy(path, flags, mode=0o777, *, dir_fd=None):
        opened.append(os.fspath(path))
        return real(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", spy)
    assert _check(tmp_path, _config()) is None
    assert "web.password" not in opened


def test_no_fd_is_left_open_on_any_failure(tmp_path):
    directory = _secret_dir(tmp_path)
    (directory / "stray").write_text("x")
    before = _fds()
    assert _check(tmp_path, _config()) == "secret_invalid"
    assert _check(tmp_path, _config(allow=["0.0.0.0/0"])) == "unrestricted_not_supported"
    assert _fds() == before


# --- AC-33: plan の許可 secret 名と必須集合 -------------------------------------------------

def test_the_allowed_secret_names_are_fixed_and_the_required_set_follows_auth(tmp_path):
    assert startup.PLANS["web"].secret_allowed == ("web.password",)
    assert startup.PLANS["bot"].secret_allowed == ("discord.token",)
    assert startup.PLANS["notify"].secret_allowed == ("webhook.url",)
    none = startup.StartupPlan.for_feature("web", web_port=1, web_auth="none")
    basic = startup.StartupPlan.for_feature("web", web_port=1, web_auth="basic")
    assert none.secret_allowed == basic.secret_allowed == ("web.password",)
    assert none.secret_files == () and basic.secret_files == ("web.password",)


def test_check_plan_accepts_both_required_sets_and_rejects_anything_else():
    for auth in ("none", "basic"):
        startup.check_plan(startup.StartupPlan.for_feature("web", web_port=1, web_auth=auth))
    plan = startup.StartupPlan.for_feature("web", web_port=1)
    for change in ({"secret_files": ("discord.token",)},
                   {"secret_files": ("web.password", "web.password")},
                   {"secret_allowed": ("web.password", "other")}, {"secret_allowed": ()},
                   {"web_bind": "localhost"}, {"web_bind": "0.0.0.0 "}):
        with pytest.raises(startup.StartupError) as info:
            startup.check_plan(dataclasses.replace(plan, **change))
        assert info.value.reason == "plan_invalid", change


def test_the_unrestricted_marker_is_the_zero_network():
    assert str(WEB_UNRESTRICTED) == "0.0.0.0/0"
