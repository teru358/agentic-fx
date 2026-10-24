"""`afx ops web status` の公開範囲行、terminal enabled の設定検査、disable の setup lock。"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agentic_fx.entry import main
from agentic_fx.ops_setup import interactive
from agentic_fx.ops_setup.feature_config import write_inputs
from agentic_fx.ops_setup.interfaces import Candidate
from agentic_fx.ops_setup.stages import InvalidState, OpsSetup, read_stage, run_ops_command
from agentic_fx.ops_setup.systemd import Systemd
from agentic_fx.ops_setup.web_setup import SecretPlan, WebOptions, apply_target
from web_support import LAN, ScriptIO, make_bundle, stage_file, tree

PW = "E" * 22
GUIDANCE = "afx ops web reconfigure を再実行する"


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    return tmp_path / "c" / "agentic-fx" / "ops" / "web"


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "instance"
    path.mkdir()
    monkeypatch.chdir(path)
    return path


@pytest.fixture
def quiet_manager(monkeypatch):
    """status の unit 行が実 systemctl を呼ばないようにし、interface 列挙を固定する。"""
    monkeypatch.setattr(Systemd, "show", lambda self, unit, *props: {p: "inactive" for p in props})
    monkeypatch.setattr("agentic_fx.ops_setup.stages.resolve_ops_exec",
                        lambda paths: replace(paths, ops_exec="/bin/true"))
    monkeypatch.setattr("agentic_fx.ops_setup.stages.interface_listing.list_interfaces",
                        lambda _exec: (Candidate(LAN, 2, "global", "enp6s0"),))


def _status(capsys) -> list[str]:
    assert main(["ops", "web", "status"]) == 0
    return capsys.readouterr().out.splitlines()


def _access(capsys) -> str:
    lines = _status(capsys)
    assert lines[0].startswith("web: ") and lines[2:] == [] or len(lines) == 2
    return lines[1]


# --- status の公開範囲行 -----------------------------------------------------------------------------------

@pytest.mark.parametrize("values, secret, line", [
    ({"port": 8788}, False, "web-access: bind=127.0.0.1:8788 allow=loopback-default auth=none"),
    ({"port": 8788, "bind": "127.0.0.1", "allow": ["203.0.113.5", "198.51.100.0/24"], "auth": "none"}, False,
     "web-access: bind=127.0.0.1:8788 allow=restricted auth=none"),
    ({"port": 8788, "bind": LAN, "allow": ["203.0.113.5", "198.51.100.0/24"], "auth": "none"}, False,
     f"web-access: bind={LAN}:8788 allow=restricted auth=none"),
    ({"port": 8788, "bind": LAN, "allow": ["0.0.0.0/0"], "auth": "none"}, False,
     f"web-access: bind={LAN}:8788 allow=unrestricted auth=none"),
    ({"port": 8788, "bind": LAN, "allow": ["0.0.0.0/0"], "auth": "basic"}, True,
     f"web-access: bind={LAN}:8788 allow=unrestricted auth=basic user=afx"),
    ({"port": 8788, "bind": LAN, "allow": ["10.0.0.0/8"], "auth": "basic"}, True,
     f"web-access: bind={LAN}:8788 allow=restricted auth=basic user=afx"),
    ({"port": 8788, "bind": LAN, "allow": ["10.0.0.0/8"], "auth": "basic", "user": "ops-web"}, True,
     f"web-access: bind={LAN}:8788 allow=restricted auth=basic user=ops-web"),
    ({"port": 8788, "bind": "127.0.0.1", "auth": "basic", "user": "a" * 32}, True,
     f"web-access: bind=127.0.0.1:8788 allow=loopback-default auth=basic user={'a' * 32}"),
])
def test_status_prints_the_stage_line_then_one_access_line(root, xdg, capsys, quiet_manager, values, secret, line):
    apply_target(root, values, SecretPlan("install", PW) if secret else SecretPlan("absent"))
    stage_file(root, "enable", "enabled")
    lines = _status(capsys)
    assert len(lines) == 2 and lines[0].startswith("web: enable enabled") and lines[1] == line
    assert "203.0.113" not in lines[1] and "198.51.100" not in lines[1] and PW not in "\n".join(lines)


@pytest.mark.parametrize("stage", [None, ("disable", "disabled")])
def test_status_without_a_stage_or_after_disable_is_not_configured_whatever_the_files_say(
        root, xdg, capsys, quiet_manager, stage):
    if stage:
        stage_file(root, *stage)
    for prepare in (lambda: None, lambda: write_inputs(root, "web", {"port": 8788}),
                    lambda: (xdg / "config" / "ops.toml").write_text("not = [toml"),
                    lambda: (xdg / "config" / "ops.toml").chmod(0o666)):
        prepare()
        assert _access(capsys) == "web-access: unavailable reason=not_configured"


def test_status_for_an_enabled_stage_reports_every_reader_reason_in_the_fixed_form(root, xdg, capsys, quiet_manager):
    stage_file(root, "enable", "enabled")
    assert _access(capsys) == "web-access: unavailable reason=config_dir_missing"
    write_inputs(root, "web", {"port": 8788})
    config = xdg / "config" / "ops.toml"
    original = config.read_text()
    config.write_text("not = [toml")
    assert _access(capsys) == "web-access: unavailable reason=config_invalid"
    config.write_text(original + 'auth = "Basic"\n')
    assert _access(capsys) == "web-access: unavailable reason=config_invalid"
    config.write_text(original)
    config.chmod(0o666)
    assert _access(capsys) == "web-access: unavailable reason=config_dir_invalid"
    config.unlink()
    config.symlink_to(xdg / "elsewhere")
    assert _access(capsys) == "web-access: unavailable reason=config_dir_invalid"
    config.unlink()
    write_inputs(root, "web", {"port": 8788, "bind": LAN})
    assert _access(capsys) == "web-access: unavailable reason=allow_required"
    write_inputs(root, "web", {"port": 8788, "auth": "basic"})
    assert _access(capsys) == "web-access: unavailable reason=secret_missing"
    write_inputs(root, "web", {"port": 8788})
    (xdg / "secret" / "web.password").write_text(PW)
    (xdg / "secret" / "web.password").chmod(0o600)
    assert _access(capsys) == "web-access: unavailable reason=secret_unexpected"
    (xdg / "secret" / "stray").write_text("x")
    assert _access(capsys) == "web-access: unavailable reason=secret_invalid"


def test_status_unavailable_lines_never_carry_values_or_paths(root, xdg, capsys, quiet_manager):
    stage_file(root, "enable", "enabled")
    write_inputs(root, "web", {"port": 8788, "bind": LAN, "allow": ["203.0.113.5"]})
    (xdg / "secret" / "stray").write_text("x")
    line = _access(capsys)
    assert line == "web-access: unavailable reason=secret_invalid"
    for needle in (str(root), str(xdg), "203.0.113", LAN, "8788", "stray"):
        assert needle not in line


def test_status_for_a_non_loopback_bind_uses_the_interface_listing(root, xdg, capsys, quiet_manager, monkeypatch):
    stage_file(root, "enable", "enabled")
    write_inputs(root, "web", {"port": 8788, "bind": "192.168.9.9", "allow": ["10.0.0.0/8"]})
    assert _access(capsys) == "web-access: unavailable reason=bind_not_local"

    def failing(_exec):
        from agentic_fx.ops_setup.feature_config import InterfaceLookupFailed
        raise InterfaceLookupFailed

    monkeypatch.setattr("agentic_fx.ops_setup.stages.interface_listing.list_interfaces", failing)
    assert _access(capsys) == "web-access: unavailable reason=interface_lookup_failed"


def test_status_never_binds_a_port_or_reads_the_password_value(root, xdg, capsys, quiet_manager, monkeypatch):
    import socket
    stage_file(root, "enable", "enabled")
    apply_target(root, {"port": 8788, "bind": LAN, "allow": ["10.0.0.0/8"], "auth": "basic"}, SecretPlan("install", PW))
    monkeypatch.setattr(socket.socket, "bind", lambda *a, **k: (_ for _ in ()).throw(AssertionError("bind")))
    monkeypatch.setattr(socket.socket, "listen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("listen")))
    assert "auth=basic" in _access(capsys)


def test_status_for_the_other_features_prints_no_access_line(root, xdg, capsys, quiet_manager):
    (root / "data" / "ops").mkdir(parents=True)
    (root / "data" / "ops" / "bot.stage.json").write_text(json.dumps(
        {"feature": "bot", "operation": "enable", "stage": "enabled"}))
    assert main(["ops", "bot", "status"]) == 0
    assert "web-access" not in capsys.readouterr().out


def test_status_keeps_the_open_operations_line_after_the_access_line(root, xdg, capsys, quiet_manager, monkeypatch):
    monkeypatch.setattr("agentic_fx.ops_setup.stages.deploy_store.count_open_readonly", lambda _db: 2)
    stage_file(root, "enable", "enabled")
    write_inputs(root, "web", {"port": 8788})
    assert main(["ops", "web", "status"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].startswith("web-access:") and lines[2].startswith("配備操作が未完了: 2 件")


# --- terminal enabled の実物照合の前の設定検査 ----------------------------------------------------

def _gate_bundle(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch)
    bundle.setup.access_check = bundle.setup._web_access_reason      # 実 reader
    bundle.systemd.active.discard(bundle.web_unit)                   # afx は稼働中・ops-web は inactive
    return bundle


def _break(bundle, kind):
    xdg = bundle.xdg
    if kind == "secret_unexpected":
        (xdg / "secret").mkdir(exist_ok=True)
        (xdg / "secret" / "web.password").write_text(PW)
        (xdg / "secret" / "web.password").chmod(0o600)
    elif kind == "secret_missing":
        text = (xdg / "config" / "ops.toml").read_text()
        (xdg / "config" / "ops.toml").write_text(text + 'auth = "basic"\n')
    elif kind == "grammar":
        text = (xdg / "config" / "ops.toml").read_text()
        (xdg / "config" / "ops.toml").write_text(text + 'auth = "Basic"\n')
    elif kind == "unsafe_secret":
        (xdg / "secret" / "stray").write_text("x")


@pytest.mark.parametrize("kind", ["secret_unexpected", "secret_missing", "grammar", "unsafe_secret"])
def test_an_intermediate_or_broken_config_stops_the_enable_without_touching_afx_or_the_stage(
        tmp_path, monkeypatch, kind):
    bundle = _gate_bundle(tmp_path, monkeypatch)
    stage_before = bundle.stage_file.read_bytes()
    _break(bundle, kind)
    probes: list = []
    bundle.setup.probe = lambda *a: probes.append(a) or (0, "")
    bundle.systemd.calls.clear()
    with pytest.raises(InvalidState) as raised:
        bundle.setup.enable("web")
    assert (raised.value.reason, raised.value.detail) == ("invalid_state", GUIDANCE)
    names = bundle.systemd.names()
    assert not {"stop", "start", "ensure_active", "reset_failed"} & set(names)
    assert probes == [] and bundle.stage_file.read_bytes() == stage_before
    assert "stage_mismatch" not in bundle.stage_file.read_text() and "failure" not in bundle.stage_file.read_text()


@pytest.mark.parametrize("kind", ["secret_unexpected", "secret_missing", "grammar"])
def test_the_cli_enable_reports_the_guidance_and_asks_nothing(tmp_path, monkeypatch, kind):
    bundle = _gate_bundle(tmp_path, monkeypatch)
    _break(bundle, kind)
    io = ScriptIO()
    before = bundle.stage_file.read_bytes()
    result = interactive.interactive_enable(bundle.root, "web", io=io, probe_runner=lambda _f: (_ for _ in ()).throw(
        AssertionError("probe")), setup_factory=lambda _root: bundle.setup)
    assert (result.rc, result.reason_code) == (1, "invalid_state")
    assert io.err == [f"afx ops: invalid_state ({GUIDANCE})"] and io.asked == []
    assert bundle.stage_file.read_bytes() == before
    assert not {"stop", "start", "ensure_active"} & set(bundle.systemd.names())


def test_an_allowed_config_restarts_ops_from_installed_as_before(tmp_path, monkeypatch):
    bundle = _gate_bundle(tmp_path, monkeypatch)
    assert bundle.setup.enable("web").stage == "enabled"
    assert "ensure_active" in bundle.systemd.names() and bundle.web_unit in bundle.systemd.active


def test_the_check_is_skipped_when_afx_is_not_running_or_ops_is_active(tmp_path, monkeypatch):
    calls: list = []
    bundle = make_bundle(tmp_path, monkeypatch, access_check=lambda: calls.append(1) or "secret_missing")
    bundle.systemd.active.add(bundle.web_unit)                       # ops active
    before = bundle.stage_file.read_bytes()
    assert bundle.setup.enable("web").stage == "enabled"
    bundle.systemd.active.discard(bundle.web_unit)
    bundle.systemd.afx_state = "inactive"                             # afx 停止中
    bundle.systemd.active.discard(bundle.paths.afx_unit)
    assert bundle.setup.enable("web").stage == "enabled"
    assert calls == [] and bundle.stage_file.read_bytes() == before
    assert not {"stop", "start", "ensure_active"} & set(bundle.systemd.names())


def test_only_web_is_checked(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch, access_check=lambda: "secret_missing")
    from agentic_fx.ops_setup.feature_config import write_inputs as write
    write(bundle.root, "notify", {"webhook_url": "https://hooks.example.com/api/webhooks/1/x"})
    assert bundle.setup.enable("notify").stage == "enabled"
    bundle.systemd.active.discard(bundle.paths.feature_unit("notify"))
    assert bundle.setup.enable("notify").stage == "enabled"


def test_an_interface_listing_failure_for_a_lan_config_is_also_a_stop(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch, web_values={"port": 8788, "bind": LAN, "allow": ["10.0.0.0/8"]},
                         access_check=lambda: None)
    bundle.setup.access_check = bundle.setup._web_access_reason
    bundle.systemd.active.discard(bundle.web_unit)
    monkeypatch.setattr("agentic_fx.ops_setup.stages.interface_listing.list_interfaces",
                        lambda _exec: (_ for _ in ()).throw(__import__("agentic_fx.ops_setup.feature_config",
                                                                      fromlist=["x"]).InterfaceLookupFailed()))
    with pytest.raises(InvalidState):
        bundle.setup.enable("web")
    assert not {"stop", "start", "ensure_active"} & set(bundle.systemd.names())


# --- disable の setup lock (M-T2-DISABLE-NO-SETUP-LOCK) -----------------------------------------------

def _hold_lock(root: Path):
    lock = root / "data" / "ops" / "setup.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [sys.executable, "-I", "-c",
         "import fcntl,sys\nf=open(sys.argv[1],'a+b')\nfcntl.flock(f,fcntl.LOCK_EX)\nprint('held',flush=True)\nsys.stdin.readline()",
         str(lock)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "held"
    return holder


def _release(holder):
    holder.stdin.write("\n")
    holder.stdin.flush()
    holder.wait(timeout=10)


@pytest.mark.parametrize("feature", ["bot", "web", "notify"])
def test_disable_is_setup_busy_while_another_process_holds_the_lock_and_changes_nothing(
        root, monkeypatch, capsys, feature):
    called: list = []
    monkeypatch.setattr("agentic_fx.ops_setup.stages.run_ops_command",
                        lambda *a: called.append(a) or 0)
    holder = _hold_lock(root)
    try:
        assert main(["ops", feature, "disable"]) == 1
    finally:
        _release(holder)
    captured = capsys.readouterr()
    assert captured.err == "afx ops: setup_busy\n" and captured.out == "" and called == []
    assert main(["ops", feature, "disable"]) == 0 and len(called) == 1


def test_disable_with_an_unsafe_lock_directory_is_setup_lock_unsafe(root, monkeypatch, capsys):
    called: list = []
    monkeypatch.setattr("agentic_fx.ops_setup.stages.run_ops_command", lambda *a: called.append(a) or 0)
    lock_dir = root / "data" / "ops"
    lock_dir.mkdir(parents=True)
    lock_dir.chmod(0o777)
    assert main(["ops", "web", "disable"]) == 1
    assert capsys.readouterr().err == "afx ops: setup_lock_unsafe\n" and called == []


def test_disable_holds_the_lock_for_the_whole_operation_and_releases_it_after(root, monkeypatch):
    probe_code = ("import errno,fcntl,sys\nf=open(sys.argv[1],'a+b')\n"
                  "try: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                  "except BlockingIOError as e: raise SystemExit(7 if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK) else 8)\n"
                  "raise SystemExit(9)")
    lock = root / "data" / "ops" / "setup.lock"
    seen: list[int] = []

    def during(*_args):
        seen.append(subprocess.run([sys.executable, "-I", "-c", probe_code, str(lock)], timeout=30).returncode)
        return 0

    monkeypatch.setattr("agentic_fx.ops_setup.stages.run_ops_command", during)
    assert main(["ops", "web", "disable"]) == 0
    assert seen == [7]
    assert subprocess.run([sys.executable, "-I", "-c", probe_code, str(lock)], timeout=30).returncode == 9


def test_disable_releases_the_lock_when_the_operation_raises(root, monkeypatch):
    def boom(*_args):
        raise RuntimeError("x")

    monkeypatch.setattr("agentic_fx.ops_setup.stages.run_ops_command", boom)
    with pytest.raises(RuntimeError):
        interactive.run_ops_disable(root, "web")
    holder = _hold_lock(root)   # 取れる (= 解放済み) ことの確認: holder が flock を得られれば解放されている
    _release(holder)


def test_disable_waits_for_no_one_and_does_not_touch_the_instance_lock(root, monkeypatch):
    monkeypatch.setattr("agentic_fx.ops_setup.stages.run_ops_command", lambda *a: 0)
    assert main(["ops", "web", "disable"]) == 0
    assert not (root / "data" / "instance.lock").exists()
