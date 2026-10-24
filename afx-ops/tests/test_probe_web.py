"""`afx-ops probe web`: 決定木の順、起動との reason 一致、何も残さないこと。"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import types
from pathlib import Path

import pytest

from afx_ops import cli, interfaces, probe
from afx_ops.config import ConfigError, read_config
from afx_ops.interfaces import Candidate, InterfaceLookupError
from afx_ops.isolation import accept_loop, startup
from afx_ops.isolation.accept_loop import ListenerError, check_listener, prebind_listener

SCRIPT = Path(sys.executable).parent / "afx-ops"
LAN = "192.168.1.10"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_DIRS: dict[str, object] = {}


@pytest.fixture(autouse=True)
def _web_dirs(ops_dirs):
    _DIRS["web"] = ops_dirs("web")
    yield
    _DIRS.clear()


def _install(extra: str = "", *, port: int | None = None, raw: str | None = None) -> Path:
    dirs = _DIRS["web"]
    dirs.config.mkdir(parents=True, mode=0o700)
    dirs.config.chmod(0o700)
    text = ('instance_root = "/srv/afx-probe"\nkey_dir = "/srv/afx-probe-keys"\n'
            f"port = {port if port is not None else _free_port()}\n")
    path = dirs.config / "ops.toml"
    path.write_text(raw if raw is not None else text + extra, encoding="utf-8")
    path.chmod(0o600)
    return path


def _secret_dir(*, mode: int = 0o700) -> Path:
    path = _DIRS["web"].secret
    path.mkdir(parents=True, mode=mode, exist_ok=True)
    path.chmod(mode)
    return path


def _run(capsys) -> tuple[int, str, str]:
    rc = probe.main("web")
    out, err = capsys.readouterr()
    return rc, out, err


def _tree(home: Path):
    return sorted((str(p.relative_to(home)), p.lstat().st_mtime_ns) for p in home.rglob("*"))


class Lookup:
    def __init__(self, *addresses: str, fail: bool = False) -> None:
        self.addresses, self.fail, self.calls = addresses, fail, 0

    def __call__(self):
        self.calls += 1
        if self.fail:
            raise InterfaceLookupError
        return [Candidate(address, 2, "global") for address in self.addresses]


@pytest.fixture
def lan_present(monkeypatch):
    lookup = Lookup(LAN, "192.168.1.11")
    monkeypatch.setattr(interfaces, "list_interfaces", lookup)
    return lookup


# --- 成功と決定木の順 -----------------------------------------------------------------

def test_the_default_localhost_configuration_passes_silently(home, capsys):
    _install()
    assert _run(capsys) == (0, "", "")


def test_a_non_default_loopback_bind_passes_without_enumerating_interfaces(home, capsys,
                                                                          monkeypatch):
    lookup = Lookup(fail=True)
    monkeypatch.setattr(interfaces, "list_interfaces", lookup)
    _install('bind = "127.0.0.2"\nallow = ["127.0.0.1/32"]\n')
    assert _run(capsys) == (0, "", "")
    assert lookup.calls == 0


def test_a_real_lan_address_and_a_limited_allow_pass_and_the_port_is_released(home, capsys):
    found = interfaces.list_interfaces()
    if not found:
        pytest.skip("この host に global / link の IPv4 が無い")
    port = _free_port()
    _install(f'bind = "{found[0].address}"\nallow = ["{found[0].address}/32"]\n', port=port)
    assert _run(capsys) == (0, "", "")
    with socket.socket() as s:
        s.bind((found[0].address, port))


def test_a_secondary_address_passes(home, capsys, lan_present):
    _install('bind = "192.168.1.11"\nallow = ["192.168.1.0/24"]\n', port=_free_port())
    assert _run(capsys)[0] == 73  # 実 bind は本機に無い address で失敗するが、検査の段は通る
    assert lan_present.calls == 1


@pytest.mark.parametrize(("extra", "reason", "rc"), [
    ('bind = "192.168.1.10"\n', "allow_required", 71),
    ('allow = ["0.0.0.0/0"]\n', "unrestricted_not_supported", 71),
    ('bind = "192.168.1.10"\nallow = ["0.0.0.0/0"]\n', "unrestricted_not_supported", 71),
    ('allow = ["127.0.0.1"]\nauth = "basic"\n', "auth_not_supported", 71),
    ('allow = ["0.0.0.0/0", "10.0.0.0/8"]\n', "config_invalid", 71),
    ('bind = "192.168.77.5"\nallow = ["10.0.0.0/8"]\n', "bind_not_local", 71),
    ('bind = "0.0.0.0"\nallow = ["10.0.0.0/8"]\n', "config_invalid", 71),
    ('auth = "Basic"\n', "config_invalid", 71),
])
def test_each_rejection_is_one_fixed_line_with_the_documented_rc(home, capsys, lan_present,
                                                                   extra, reason, rc):
    _install(extra)
    assert _run(capsys) == (rc, "", f"afx-ops: {reason}\n")


def test_a_failed_interface_lookup_is_distinct_from_an_absent_address(home, capsys, monkeypatch):
    monkeypatch.setattr(interfaces, "list_interfaces", Lookup(fail=True))
    _install(f'bind = "{LAN}"\nallow = ["10.0.0.0/8"]\n')
    assert _run(capsys) == (71, "", "afx-ops: interface_lookup_failed\n")


def test_the_precedence_follows_the_decision_tree(home, capsys, lan_present):
    # allow_required (3) は capability (4)・interface (5)・secret (6) より先
    _secret_dir().joinpath("stray").write_text("x")
    _install(f'bind = "{LAN}"\nauth = "basic"\n')
    assert _run(capsys)[2] == "afx-ops: allow_required\n"


def test_the_secret_state_is_judged_after_the_interfaces(home, capsys, lan_present):
    _secret_dir().joinpath("stray").write_text("x")
    _install(f'bind = "{LAN}"\nallow = ["10.0.0.0/8"]\n')
    assert _run(capsys)[2] == "afx-ops: secret_invalid\n"
    lan_present.fail = True
    assert _run(capsys)[2] == "afx-ops: interface_lookup_failed\n"


def test_secret_residue_is_secret_unexpected_and_nothing_is_removed(home, capsys):
    directory = _secret_dir()
    password = directory / "web.password"
    password.write_bytes(b"A" * 22)
    password.chmod(0o600)
    _install()
    assert _run(capsys) == (71, "", "afx-ops: secret_unexpected\n")
    assert password.read_bytes() == b"A" * 22
    (directory / ".web.password.0123.tmp").write_text("x")
    assert _run(capsys)[2] == "afx-ops: secret_invalid\n"
    assert (directory / ".web.password.0123.tmp").exists()


def test_a_missing_secret_dir_is_allowed_and_is_not_created(home, capsys):
    _install()
    assert _run(capsys)[0] == 0
    assert not _DIRS["web"].secret.exists()


# --- 実 bind -----------------------------------------------------------------------

@pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0"])
def test_a_busy_port_is_probe_bind_failed_rc_73(home, capsys, host):
    holder = socket.socket()
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind((host, 0))
    holder.listen(1)
    try:
        _install(port=holder.getsockname()[1])
        assert _run(capsys) == (73, "", "afx-ops: probe_bind_failed\n")
    finally:
        holder.close()


def test_a_getsockname_that_differs_from_the_setting_is_probe_bind_failed(home, capsys,
                                                                          monkeypatch):
    """M-T1-LISTENER-ADDRESS / M-T1-LISTENER-PORT: 不一致を見逃す変異は落ちる。"""
    import shutil
    _install()
    for lie in (("127.0.0.9", None), ("127.0.0.1", 1)):
        shim = types.SimpleNamespace(**vars(socket))

        class Lying(socket.socket):
            def getsockname(self, _lie=lie):
                host, port = super().getsockname()
                return (_lie[0], _lie[1] or port)

        shim.socket = Lying
        with monkeypatch.context() as scoped:
            scoped.setattr(accept_loop, "socket", shim)
            assert _run(capsys) == (73, "", "afx-ops: probe_bind_failed\n")
    config_dir = _DIRS["web"].config
    assert config_dir.is_relative_to(home)
    shutil.rmtree(config_dir)


def test_check_listener_compares_both_the_address_and_the_port():
    sock = prebind_listener(0)
    try:
        host, port = sock.getsockname()
        check_listener(sock, port, host)
        for wrong_port, wrong_host in ((port + 1, host), (port, "127.0.0.2"), (port, LAN)):
            with pytest.raises(ListenerError) as info:
                check_listener(sock, wrong_port, wrong_host)
            assert info.value.reason == "listener_invalid"
    finally:
        sock.close()


def test_production_start_maps_a_listener_failure_to_listener_invalid(tmp_path, monkeypatch):
    """起動は同じ関数の失敗を `listener_invalid` にする (probe は `probe_bind_failed`)。"""
    plan = startup.StartupPlan("web", tmp_path / "config", tmp_path / "secret", (),
                               tmp_path / "state", 18080, (), False, False)
    from afx_ops.config import OpsConfig
    config = OpsConfig(tmp_path / "root", tmp_path / "keys", None, port=18080)

    class _Filter:
        compiled = object()

    monkeypatch.setattr(startup, "connect_notify", lambda *args, **kwargs: None)
    monkeypatch.setattr(startup, "check_preconditions", lambda *args: None)
    monkeypatch.setattr(startup, "prepare_dirs", lambda *args, **kwargs: startup.PreparedDirs(-1, -1))
    monkeypatch.setattr(startup, "feature_filter", lambda *args: _Filter())
    monkeypatch.setattr(startup, "read_api_token", lambda *args: b"token")

    def mismatch(port, *, bind):
        raise ListenerError("address")

    monkeypatch.setattr(startup, "prebind_listener", mismatch)
    with pytest.raises(startup.StartupError) as info:
        startup.isolate(plan, config, plan_check=lambda value: None)
    assert info.value.reason == "listener_invalid"


# --- AC-11: 何も作らない -----------------------------------------------------------------

@pytest.mark.parametrize("extra", [
    "", 'bind = "127.0.0.2"\nallow = ["127.0.0.1/32"]\n', 'allow = ["0.0.0.0/0"]\n',
    f'bind = "{LAN}"\nallow = ["10.0.0.0/8"]\n', 'auth = "Basic"\n',
])
def test_success_and_failure_leave_no_state_stage_file_listener_or_fd(home, capsys, lan_present,
                                                                      extra):
    port = _free_port()
    _install(extra, port=port)
    before_tree, before_fds = _tree(home), set(os.listdir("/proc/self/fd"))
    probe.main("web")
    capsys.readouterr()
    assert _tree(home) == before_tree
    assert set(os.listdir("/proc/self/fd")) == before_fds
    assert not (home / ".local").exists()
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))


def test_a_config_error_leaves_no_fd_behind(home, capsys):
    _install(raw="not toml = =")
    before = set(os.listdir("/proc/self/fd"))
    assert _run(capsys)[0] == 71
    assert set(os.listdir("/proc/self/fd")) == before


def test_the_real_command_line_probe_prints_nothing_on_success(home, child_env):
    _install()
    done = subprocess.run([str(SCRIPT), "probe", "web"], capture_output=True, text=True,
                          timeout=60, env=child_env, cwd=home, stdin=subprocess.DEVNULL)
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    assert not (home / ".local").exists()


def test_the_real_command_line_probe_reports_a_missing_config_dir(home, child_env):
    done = subprocess.run([str(SCRIPT), "probe", "web"], capture_output=True, text=True,
                          timeout=60, env=child_env, cwd=home, stdin=subprocess.DEVNULL)
    assert (done.returncode, done.stdout, done.stderr) == (71, "", "afx-ops: config_dir_missing\n")


# --- 起動と probe は 1〜6 を同じ reason で返す ----------------------------------------------

def _production_reason(lookup=None) -> str | None:
    """production の起動口が probe と同じ入力で最初に返す reason (listener の前まで)。"""
    try:
        config = read_config("web")
    except ConfigError as exc:
        return exc.reason
    try:
        plan = startup.StartupPlan.for_feature("web", web_port=config.port,
                                               web_bind=config.web_bind, web_auth=config.auth)
        startup.check_web_access(plan, config, lookup=lookup)
    except startup.StartupError as exc:
        return exc.reason
    finally:
        os.close(config.config_dir_fd)
    return None


@pytest.mark.parametrize("extra", [
    "", 'bind = "192.168.1.10"\n', 'allow = ["0.0.0.0/0"]\n', 'auth = "basic"\n',
    'bind = "192.168.77.5"\nallow = ["10.0.0.0/8"]\n', f'bind = "{LAN}"\nallow = ["10.0.0.0/8"]\n',
    'bind = "x"\n',
])
def test_production_and_probe_agree_on_the_reason(home, capsys, lan_present, extra):
    _install(extra, port=_free_port())
    expected = _production_reason()
    rc, _out, err = _run(capsys)
    if expected is None:
        assert rc in (0, 73)
    else:
        assert err == f"afx-ops: {expected}\n" and rc == 71


def test_the_probe_calls_the_same_checking_function_as_production():
    assert probe.startup.check_web_access is startup.check_web_access
    source = Path(startup.__file__).read_text(encoding="utf-8")
    assert "check_web_access(plan, config)" in source
    assert cli.startup is startup
