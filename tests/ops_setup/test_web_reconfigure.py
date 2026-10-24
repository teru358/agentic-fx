"""`afx ops web reconfigure`: 入口表、setup lock、入口照合、手順 (1)〜(4)、crash 点、afx 不接触。"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agentic_fx.entry import main
from agentic_fx.ops.contracts import Principal
from agentic_fx.ops.keys import KeySetError, key_dir, key_file_name
from agentic_fx.ops_setup import interactive
from agentic_fx.ops_setup.feature_config import web_access_reason, write_inputs
from agentic_fx.ops_setup.reconfigure import run_ops_reconfigure
from agentic_fx.ops_setup.setup_lease import acquire_setup_lease
from agentic_fx.ops_setup.systemd import SystemdError
from agentic_fx.ops_setup.web_setup import SecretPlan, WebOptions, apply_target
from agentic_fx.store.enroll_marker import enroll_marker_path
from web_support import (CANDIDATES, LAN, ONE_CANDIDATE, SCOPE_Q, ScriptIO, make_bundle, stage_file, tree,
                         who_q)

PW = "AAECAwQFBgcICQoLDA0ODw"
FIXED_RNG = lambda n: bytes(range(n))  # noqa: E731
RERUN = "afx ops web reconfigure を再実行する"
RECOVER = "afx ops web enable で状態を回復してから再実行する"
RELOAD = "systemctl --user daemon-reload の後に再実行する"
KEYS = "afx を止めて afx keys rotate ops-web を実行し、afx を起動してから afx ops web reconfigure を再実行する"
BIND_Q = f"待受アドレス [1] {LAN} (global) (既定)"
ALLOW_Q = "許可する IPv4 / CIDR (カンマ区切り)"
PASSWORD_Q = "password [1] 生成する (既定) [2] 自分で入力する"


class Crash(BaseException):
    pass


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    return make_bundle(tmp_path, monkeypatch)


def _target(tmp_path: Path, text: str = "port = 9000\n", mode: int = 0o600, name: str = "t.toml") -> str:
    path = tmp_path / name
    path.write_text(text)
    path.chmod(mode)
    return str(path)


def _pw(tmp_path: Path, value: str = PW, mode: int = 0o600, name: str = "p") -> str:
    path = tmp_path / name
    path.write_text(value)
    path.chmod(mode)
    return str(path)


def noninteractive(tmp_path, text="port = 9000\n", action="absent", password=None):
    return WebOptions(non_interactive=True, config_file=_target(tmp_path, text), secret_action=action,
                      password_file=password)


class Out:
    def __init__(self, rc, io, capsys, bundle, probes):
        self.rc, self.io, self.bundle, self.probes = rc, io, bundle, probes
        captured = capsys.readouterr()
        self.err = captured.err
        self.stdout = io.out

    @property
    def calls(self):
        return self.bundle.systemd.calls

    def names(self, unit=None):
        return [c[0] for c in self.calls if unit is None or (len(c) > 1 and c[1] == unit)]


def reconf(bundle, capsys, options=None, io=None, *, probe=None, candidates=ONE_CANDIDATE, hook=None,
           ready=None):
    io = io or ScriptIO(tty=options is None or not options.non_interactive)
    probes: list = []

    def probe_runner(feature):
        probes.append(feature)
        return probe(len(probes)) if probe is not None else (0, "")

    if ready is not None:
        bundle.setup.ready = ready
    rc = run_ops_reconfigure(bundle.root, "web", options=options or WebOptions(), io=io,
                             setup_factory=lambda _root: bundle.setup, probe_runner=probe_runner,
                             candidates=lambda: candidates, hook=hook, token_bytes=FIXED_RNG)
    return Out(rc, io, capsys, bundle, probes)


ALLOWED_AFX = {"ActiveState", "Wants", "NeedDaemonReload"}
ALLOWED_OPS = {"LoadState", "NeedDaemonReload"}


def assert_only_allowed_calls(bundle):
    afx, ops = bundle.paths.afx_unit, bundle.web_unit
    for call in bundle.systemd.calls:
        name, *rest = call
        if name == "show":
            unit, *props = rest
            assert (unit == afx and set(props) <= ALLOWED_AFX) or (unit == ops and set(props) <= ALLOWED_OPS), call
        elif name in ("stop", "is_stopped", "reset_failed", "start", "is_active"):
            assert rest == [ops], call
        elif name == "hook":
            continue
        else:
            raise AssertionError(call)


def unchanged_state(bundle):
    return bundle.snapshot()


# --- 入口: option と stage の表 -----------------------------------------------------------

@pytest.mark.parametrize("options", [
    WebOptions(ask=True), WebOptions(config_file="x"), WebOptions(non_interactive=True),
    WebOptions(duplicated=True), WebOptions(ask=True, non_interactive=True, config_file="x", secret_action="keep"),
])
@pytest.mark.parametrize("stage", [None, ("disable", "disabled"), ("enable", "staged"), ("enable", "enabled")])
def test_an_option_outside_the_table_is_input_required_and_changes_nothing(tmp_path, monkeypatch, capsys, stage,
                                                                          options):
    bundle = make_bundle(tmp_path, monkeypatch, enable_web=False)
    if stage:
        stage_file(bundle.root, *stage)
    before = tree(bundle.xdg)
    out = reconf(bundle, capsys, options)
    assert out.rc == 2 and out.err == "afx ops: setup_input_required\n"
    assert out.calls == [] and tree(bundle.xdg) == before


@pytest.mark.parametrize("tty", [True, False])
@pytest.mark.parametrize("stage, reason", [
    (None, "not_enabled"), (("disable", "disabled"), "not_enabled"),
    (("enable", "staged"), "invalid_state"), (("enable", "installed"), "invalid_state"),
    (("disable", "stopping-ops"), "invalid_state"),
])
@pytest.mark.parametrize("mode", ["flagless", "non-interactive"])
def test_the_stage_gate_refuses_everything_but_terminal_enabled(tmp_path, monkeypatch, capsys, stage, reason,
                                                                tty, mode):
    bundle = make_bundle(tmp_path, monkeypatch, enable_web=False)
    if stage:
        stage_file(bundle.root, *stage)
    options = WebOptions() if mode == "flagless" else noninteractive(tmp_path)
    before = tree(bundle.xdg)
    out = reconf(bundle, capsys, options, ScriptIO(tty=tty))
    assert out.rc == 1 and out.err == f"afx ops: {reason}\n"
    assert out.calls == [] and out.probes == [] and tree(bundle.xdg) == before


def test_flagless_without_a_tty_is_input_required_after_the_read_only_checks(bundle, capsys):
    before = unchanged_state(bundle)
    out = reconf(bundle, capsys, WebOptions(), ScriptIO(tty=False))
    assert out.rc == 2 and out.err == "afx ops: setup_input_required\n"
    assert out.probes == [] and unchanged_state(bundle) == before
    assert not {"stop", "start", "reset_failed"} & set(out.names())


def test_the_read_only_checks_outrank_input_required(bundle, capsys):
    bundle.systemd.need_reload[bundle.paths.afx_unit] = "yes"
    out = reconf(bundle, capsys, WebOptions(), ScriptIO(tty=False))
    assert out.rc == 1 and out.err == f"afx ops: invalid_state ({RELOAD})\n"


# --- setup lock (双方向) -------------------------------------------------------------------------

def _hold(root: Path):
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


def test_reconfigure_is_setup_busy_while_another_process_holds_the_lock(bundle, capsys, tmp_path):
    before = unchanged_state(bundle)
    holder = _hold(bundle.root)
    try:
        out = reconf(bundle, capsys, noninteractive(tmp_path))
    finally:
        _release(holder)
    assert out.rc == 1 and out.err == "afx ops: setup_busy\n"
    assert out.calls == [] and out.probes == [] and unchanged_state(bundle) == before


def test_reconfigure_is_blocked_by_a_held_init_lease(bundle, capsys, tmp_path):
    lease = acquire_setup_lease(bundle.root)
    try:
        out = reconf(bundle, capsys, noninteractive(tmp_path))
    finally:
        lease.close()
    assert out.rc == 1 and out.err == "afx ops: setup_busy\n" and out.calls == []


def test_an_unsafe_lock_directory_is_setup_lock_unsafe(bundle, capsys, tmp_path):
    (bundle.root / "data" / "ops").chmod(0o777)
    out = reconf(bundle, capsys, noninteractive(tmp_path))
    assert out.rc == 1 and out.err == "afx ops: setup_lock_unsafe\n" and out.calls == []


@pytest.mark.parametrize("point", ["ops_stopped", "secret_applied", "config_applied", "probe_succeeded",
                                   "ops_restopped", "ops_reset_failed", "ops_started"])
def test_the_lock_is_held_from_the_entry_to_the_end_against_enable_disable_and_reconfigure(
        bundle, capsys, tmp_path, point, monkeypatch):
    seen: dict[str, tuple] = {}

    def hook(name):
        if name != point or seen:
            return
        io = ScriptIO(tty=False)
        enable = interactive.interactive_enable(bundle.root, "web", io=io, setup_factory=lambda _r: bundle.setup,
                                                probe_runner=lambda _f: (0, ""))
        monkeypatch.setattr("agentic_fx.ops_setup.stages.run_ops_command", lambda *a: 0)
        disable_rc = interactive.run_ops_disable(bundle.root, "web")
        second = run_ops_reconfigure(bundle.root, "web", options=noninteractive(tmp_path), io=ScriptIO(),
                                     setup_factory=lambda _r: bundle.setup, probe_runner=lambda _f: (0, ""),
                                     candidates=lambda: ONE_CANDIDATE)
        seen["x"] = (enable.reason_code, disable_rc, second)

    options = noninteractive(tmp_path, f'port = 9000\nbind = "{LAN}"\nallow = ["10.0.0.0/8"]\nauth = "basic"\n',
                             "install", _pw(tmp_path))
    out = reconf(bundle, capsys, options, hook=hook)
    assert out.rc == 0 and seen["x"] == ("setup_busy", 1, 1)


# --- 他 feature・marker・起動コマンド --------------------------------------------------------------

@pytest.mark.parametrize("feature, operation, stage", [("notify", "enable", "installed"),
                                                        ("bot", "disable", "stopping-ops")])
def test_another_features_unfinished_operation_stops_with_the_owner_and_operation(
        bundle, capsys, tmp_path, feature, operation, stage):
    path = bundle.root / "data" / "ops" / f"{feature}.stage.json"
    path.write_text(json.dumps({"feature": feature, "operation": operation, "stage": stage,
                                "principal": f"ops-{feature}", "registered": False, "artifacts": [],
                                "wants": [], "ops_active": False, "committed": False}))
    before = unchanged_state(bundle)
    out = reconf(bundle, capsys, noninteractive(tmp_path))
    assert out.rc == 1
    assert out.err == f"afx ops: invalid_state (afx ops {feature} {operation} を再実行して完了させてから再実行する)\n"
    assert out.calls == [] and unchanged_state(bundle) == before


def _marker(bundle, body: str):
    path = enroll_marker_path(bundle.root)
    path.write_text(body)
    return path


def test_a_foreign_marker_is_marker_foreign(bundle, capsys, tmp_path):
    _marker(bundle, json.dumps({"feature": "bot", "started_at": "2026-10-10T00:00:00Z"}))
    before = unchanged_state(bundle)
    out = reconf(bundle, capsys, noninteractive(tmp_path))
    assert out.rc == 1 and out.err.startswith("afx ops: marker_foreign (")
    assert "afx ops bot enable" in out.err and out.calls == [] and unchanged_state(bundle) == before


@pytest.mark.parametrize("body", ["not json", "{}", json.dumps({"feature": ["x"], "started_at": "t"})])
def test_a_marker_with_an_unknown_owner_is_marker_corrupt(bundle, capsys, tmp_path, body):
    _marker(bundle, body)
    out = reconf(bundle, capsys, noninteractive(tmp_path))
    assert out.rc == 1 and out.err.startswith("afx ops: marker_corrupt (") and out.calls == []


def test_its_own_marker_points_back_to_enable(bundle, capsys, tmp_path):
    _marker(bundle, json.dumps({"feature": "web", "started_at": "2026-10-10T00:00:00Z"}))
    before = unchanged_state(bundle)
    out = reconf(bundle, capsys, noninteractive(tmp_path))
    assert out.rc == 1 and out.err == f"afx ops: invalid_state ({RECOVER})\n"
    assert out.calls == [] and unchanged_state(bundle) == before


def test_an_unresolvable_start_command_is_ops_exec_not_found_without_a_stage_write(bundle, capsys, tmp_path):
    bundle.setup.paths = replace(bundle.paths, ops_exec="/nonexistent/afx-ops")
    before = unchanged_state(bundle)
    out = reconf(bundle, capsys, noninteractive(tmp_path))
    assert out.rc == 1 and out.err == "afx ops: ops_exec_not_found\n"
    assert out.calls == [] and unchanged_state(bundle) == before


# --- 入口照合 (読み取りだけ) ---------------------------------------------------------------------

def _key_path(bundle) -> Path:
    return key_dir(bundle.root) / key_file_name(Principal.OPS_WEB)


def test_a_digest_mismatch_is_principal_key_mismatch_with_the_rotation_guidance(bundle, capsys, tmp_path):
    _key_path(bundle).write_text("0" * 64)
    before = unchanged_state(bundle)
    out = reconf(bundle, capsys, noninteractive(tmp_path))
    assert out.rc == 1 and out.err == f"afx ops: principal_key_mismatch ({KEYS})\n"
    assert out.calls == [] and unchanged_state(bundle) == before


@pytest.mark.parametrize("damage", ["missing", "garbage", "mode"])
def test_a_missing_or_malformed_key_file_is_principal_key_mismatch(bundle, capsys, tmp_path, damage):
    path = _key_path(bundle)
    if damage == "missing":
        path.unlink()
    elif damage == "garbage":
        path.write_text("not a token")
    else:
        path.chmod(0o644)
    out = reconf(bundle, capsys, noninteractive(tmp_path))
    assert out.rc == 1 and out.err == f"afx ops: principal_key_mismatch ({KEYS})\n"


@pytest.mark.parametrize("failure", ["false", "not_initialized", "key_dir_invalid"])
def test_an_unenrolled_principal_or_a_broken_key_directory_is_invalid_state(bundle, capsys, tmp_path,
                                                                           monkeypatch, failure):
    def matches(directory, principal):
        if failure == "false":
            return False
        raise KeySetError(failure)

    monkeypatch.setattr("agentic_fx.ops_setup.reconfigure.enrolled_key_matches", matches)
    out = reconf(bundle, capsys, noninteractive(tmp_path))
    assert out.rc == 1 and out.err == "afx ops: invalid_state\n" and out.calls == []


@pytest.mark.parametrize("damage", ["template", "dropin"])
def test_a_unit_file_that_differs_from_the_rendered_one_is_invalid_state(bundle, capsys, tmp_path, damage):
    path = bundle.paths.template_path if damage == "template" else bundle.paths.dropin_path
    path.write_text(path.read_text() + "# edited\n")
    before = unchanged_state(bundle)
    out = reconf(bundle, capsys, noninteractive(tmp_path))
    assert unchanged_state(bundle) == before
    assert out.rc == 1 and out.err == f"afx ops: invalid_state ({RECOVER})\n"
    assert "daemon_reload" not in out.names() and not {"stop", "start"} & set(out.names())


@pytest.mark.parametrize("which", ["afx", "ops"])
@pytest.mark.parametrize("value", ["yes", "", "maybe"])
def test_need_daemon_reload_other_than_no_asks_for_a_manual_reload(bundle, capsys, tmp_path, which, value):
    unit = bundle.paths.afx_unit if which == "afx" else bundle.web_unit
    bundle.systemd.need_reload[unit] = value
    before = unchanged_state(bundle)
    out = reconf(bundle, capsys, noninteractive(tmp_path))
    assert out.rc == 1 and out.err == f"afx ops: invalid_state ({RELOAD})\n"
    assert "daemon_reload" not in out.names() and unchanged_state(bundle) == before


def test_a_not_loaded_unit_and_a_missing_wants_entry_are_invalid_state(bundle, capsys, tmp_path):
    bundle.systemd.loaded = False
    out = reconf(bundle, capsys, noninteractive(tmp_path))
    assert out.rc == 1 and out.err == f"afx ops: invalid_state ({RECOVER})\n"
    bundle.systemd.loaded = True
    bundle.systemd.wants_override = "other.service"
    out = reconf(bundle, capsys, noninteractive(tmp_path))
    assert out.rc == 1 and out.err == f"afx ops: invalid_state ({RECOVER})\n"
    assert not {"stop", "start"} & set(out.names())


@pytest.mark.parametrize("reason", ["show", "unavailable"])
def test_a_manager_that_cannot_be_asked_stops_with_its_fixed_reason(bundle, capsys, tmp_path, reason):
    bundle.systemd.fail["show"] = SystemdError(reason)
    before = unchanged_state(bundle)
    out = reconf(bundle, capsys, noninteractive(tmp_path))
    assert out.rc == 1 and out.err == f"afx ops: {reason}\n"
    assert not {"stop", "start", "daemon_reload"} & set(out.names()) and unchanged_state(bundle) == before


AFX_STATES = ["active", "activating", "reloading", "deactivating", "inactive", "failed"]
OPS_STATES = ["active", "inactive", "failed"]


@pytest.mark.parametrize("afx_state", AFX_STATES)
@pytest.mark.parametrize("ops_state", OPS_STATES)
def test_the_entry_passes_for_every_afx_by_ops_state(bundle, capsys, tmp_path, afx_state, ops_state):
    bundle.systemd.afx_state = afx_state
    bundle.systemd.active.discard(bundle.web_unit)
    if ops_state == "active":
        bundle.systemd.active.add(bundle.web_unit)
    elif ops_state == "failed":
        bundle.systemd.failed.add(bundle.web_unit)
    out = reconf(bundle, capsys, noninteractive(tmp_path))
    assert out.rc == 0
    expected = "web: reconfigured" if afx_state in ("active", "activating", "reloading") \
        else "web: reconfigured ops=starts_with_afx"
    assert out.stdout == [expected]
    assert_only_allowed_calls(bundle)


# --- 手順と順序 --------------------------------------------------------------------------

def _log_hook(bundle):
    def hook(name):
        bundle.systemd.calls.append(("hook", name))
    return hook


def _index(calls, *needle, start=0):
    for position in range(start, len(calls)):
        if tuple(calls[position][:len(needle)]) == needle:
            return position
    raise AssertionError((needle, calls))


def test_the_steps_run_in_the_documented_order(bundle, capsys, tmp_path):
    options = noninteractive(tmp_path, f'port = 9000\nbind = "{LAN}"\nallow = ["10.0.0.0/8"]\nauth = "basic"\n',
                             "install", _pw(tmp_path))
    out = reconf(bundle, capsys, options, hook=_log_hook(bundle))
    assert out.rc == 0 and out.stdout == ["web: reconfigured"]
    calls, ops = bundle.systemd.calls, bundle.web_unit
    stop1 = _index(calls, "stop", ops)
    stopped1 = _index(calls, "is_stopped", ops, start=stop1)
    secret = _index(calls, "hook", "secret_renamed")
    config = _index(calls, "hook", "config_renamed")
    check = _index(calls, "is_stopped", ops, start=config)
    probed = _index(calls, "hook", "probe_succeeded")
    stop2 = _index(calls, "stop", ops, start=probed)
    stopped2 = _index(calls, "is_stopped", ops, start=stop2)
    reset = _index(calls, "reset_failed", ops)
    start = _index(calls, "start", ops)
    ready = _index(calls, "is_active", ops, start=start)
    assert stop1 < stopped1 < secret < config < check < probed < stop2 < stopped2 < reset < start < ready
    assert_only_allowed_calls(bundle)


def test_a_stop_that_cannot_be_confirmed_writes_nothing_and_never_starts(bundle, capsys, tmp_path):
    bundle.systemd.stuck.add(bundle.web_unit)
    bundle.systemd.active.add(bundle.web_unit)
    before_tree, before_state = tree(bundle.xdg), unchanged_state(bundle)
    out = reconf(bundle, capsys, noninteractive(tmp_path, "port = 9000\n"))
    assert out.rc == 1 and out.err == f"afx ops: ops_still_active ({RERUN})\n"
    assert tree(bundle.xdg) == before_tree and unchanged_state(bundle) == before_state
    assert "start" not in out.names() and out.probes == []


def test_an_unsafe_existing_secret_stops_before_ops_web_is_touched(bundle, capsys, tmp_path):
    (bundle.xdg / "secret" / "stray").write_text("x")
    before = tree(bundle.xdg)
    out = reconf(bundle, capsys, noninteractive(tmp_path))
    assert out.rc == 1 and out.err.startswith("afx ops: secret_invalid (")
    assert "stop" not in out.names() and tree(bundle.xdg) == before and out.probes == []


def test_an_unsafe_existing_secret_stops_the_interactive_flow_before_any_question(bundle, capsys):
    (bundle.xdg / "secret" / "stray").write_text("x")
    io = ScriptIO()
    out = reconf(bundle, capsys, WebOptions(), io)
    assert out.rc == 1 and out.err.startswith("afx ops: secret_invalid (") and io.asked == []
    assert "stop" not in out.names()


def test_an_input_file_error_is_reported_before_ops_web_is_touched(bundle, capsys, tmp_path):
    for options, expected in (
            (WebOptions(non_interactive=True, config_file=_target(tmp_path, mode=0o664, name="a.toml"),
                        secret_action="absent"), "setup_input_invalid"),
            (WebOptions(non_interactive=True, config_file=_target(tmp_path, name="b.toml"), secret_action="install",
                        password_file=_pw(tmp_path, mode=0o644)), "secret_invalid"),
            (WebOptions(non_interactive=True, config_file=_target(tmp_path, 'port = 9000\nkey_dir = "/x"\n',
                                                                  name="c.toml"),
                        secret_action="absent"), "setup_input_invalid")):
        before = tree(bundle.xdg)
        out = reconf(bundle, capsys, options)
        assert out.rc in (1, 2) and out.err.startswith(f"afx ops: {expected}")
        assert "stop" not in out.names() and tree(bundle.xdg) == before


def test_the_temporary_files_a_death_left_are_removed_first(bundle, capsys, tmp_path):
    left = bundle.xdg / "secret" / (".web.password." + "a" * 32 + ".tmp")
    left.write_text("x")
    out = reconf(bundle, capsys, noninteractive(tmp_path))
    assert out.rc == 0 and not left.exists()


def test_an_external_start_after_step_one_is_stopped_again_before_each_probe(bundle, capsys, tmp_path):
    def revive(name):
        if name in ("secret_applied", "config_applied") and ("revived" not in bundle.systemd.results):
            bundle.systemd.results["revived"] = "x"
            bundle.systemd.active.add(bundle.web_unit)

    options = noninteractive(tmp_path, f'port = 9000\nbind = "{LAN}"\nallow = ["10.0.0.0/8"]\nauth = "basic"\n',
                             "install", _pw(tmp_path))
    out = reconf(bundle, capsys, options, hook=lambda n: (revive(n), _log_hook(bundle)(n)))
    assert out.rc == 0
    calls, ops = bundle.systemd.calls, bundle.web_unit
    config = _index(calls, "hook", "config_applied")
    probe_mark = _index(calls, "hook", "probe_succeeded")
    between = [c[0] for c in calls[config:probe_mark] if len(c) > 1 and c[1] == ops]
    assert between[:3] == ["is_stopped", "stop", "is_stopped"]


def test_an_external_start_that_cannot_be_stopped_is_ops_still_active_and_starts_nothing(bundle, capsys, tmp_path):
    def revive(name):
        if name == "config_applied":
            bundle.systemd.active.add(bundle.web_unit)
            bundle.systemd.stuck.add(bundle.web_unit)

    out = reconf(bundle, capsys, noninteractive(tmp_path), hook=revive)
    assert out.rc == 1 and out.err == f"afx ops: ops_still_active ({RERUN})\n"
    assert "start" not in out.names() and out.probes == []
    assert web_access_reason(bundle.root) is None and b"port = 9000" in (bundle.xdg / "config" / "ops.toml").read_bytes()


def test_an_external_start_before_step_four_is_stopped_again_before_the_start(bundle, capsys, tmp_path):
    def revive(name):
        if name == "probe_succeeded":
            bundle.systemd.active.add(bundle.web_unit)

    out = reconf(bundle, capsys, noninteractive(tmp_path), hook=lambda n: (revive(n), _log_hook(bundle)(n)))
    assert out.rc == 0
    calls, ops = bundle.systemd.calls, bundle.web_unit
    probed = _index(calls, "hook", "probe_succeeded")
    stop = _index(calls, "stop", ops, start=probed)
    assert stop < _index(calls, "is_stopped", ops, start=stop) < _index(calls, "reset_failed", ops) \
        < _index(calls, "start", ops)


def test_a_re_stop_that_cannot_be_confirmed_before_the_start_sends_no_start(bundle, capsys, tmp_path):
    def revive(name):
        if name == "probe_succeeded":
            bundle.systemd.active.add(bundle.web_unit)
            bundle.systemd.stuck.add(bundle.web_unit)

    out = reconf(bundle, capsys, noninteractive(tmp_path), hook=revive)
    assert out.rc == 1 and out.err == f"afx ops: ops_still_active ({RERUN})\n"
    assert "start" not in out.names()


# --- 生成 password -----------------------------------------------------------------

def _interactive_basic_answers():
    return ["", "2", "", "2", ""]         # port 既定 / LAN / bind 既定 / パスワード / 生成


def test_the_interactive_flow_asks_everything_with_existing_values_and_never_the_one_question(bundle, capsys):
    io = ScriptIO(answers=[""] * 2)
    out = reconf(bundle, capsys, WebOptions(), io)
    assert out.rc == 0 and io.asked == ["port", SCOPE_Q] and io.defaults[0] == ("port", "8788")
    assert "この設定で有効化します" not in io.text()


def test_the_interactive_flow_shows_a_generated_password_once_after_the_secret_is_stored(bundle, capsys):
    io = ScriptIO(answers=_interactive_basic_answers())
    out = reconf(bundle, capsys, WebOptions(), io, hook=_log_hook(bundle))
    assert out.rc == 0
    assert (bundle.xdg / "secret" / "web.password").read_text() == PW
    assert len([text for text in io.out if PW in text]) == 1
    calls = bundle.systemd.calls
    assert _index(calls, "hook", "secret_dir_synced") < _index(calls, "hook", "config_tmp_created")
    assert PW not in out.err and PW not in "\n".join(io.err)


def test_invalid_keys_in_the_live_config_are_not_offered_as_defaults(bundle, capsys):
    config = bundle.xdg / "config" / "ops.toml"
    text = config.read_text().splitlines()[:2]
    config.write_text("\n".join(text + ['port = "oops"', 'bind = "localhost"', 'auth = "Basic"']) + "\n")
    io = ScriptIO(answers=["9000", ""])
    out = reconf(bundle, capsys, WebOptions(), io)
    assert out.rc == 0 and io.defaults[0] == ("port", "8788") and io.defaults[1][0] == SCOPE_Q


def test_an_existing_lan_config_is_offered_as_the_defaults_of_every_question(bundle, capsys):
    write_inputs(bundle.root, "web", {"port": 9100, "bind": LAN, "allow": ["10.0.0.0/8"], "auth": "none"})
    io = ScriptIO(answers=["", "", "", "", ""])
    out = reconf(bundle, capsys, WebOptions(), io)
    assert out.rc == 0
    lan_scope = "公開範囲 [1] 自機のみ [2] LAN (既定)"  # 既存が LAN なので既定は LAN 側
    assert io.asked == ["port", lan_scope, BIND_Q, who_q(1), ALLOW_Q]
    assert dict(io.defaults)["port"] == "9100" and dict(io.defaults)[ALLOW_Q] == "10.0.0.0/8"


# --- probe 失敗 ----------------------------------------------------------------------------

def test_a_failed_probe_leaves_the_target_live_and_never_starts(bundle, capsys, tmp_path):
    out = reconf(bundle, capsys, noninteractive(tmp_path), probe=lambda n: (73, "probe_bind_failed"))
    assert out.rc == 1 and out.err == f"afx ops: probe_bind_failed ({RERUN})\n"
    assert out.probes == ["web"]
    assert b"port = 9000" in (bundle.xdg / "config" / "ops.toml").read_bytes()
    assert "start" not in out.names() and "reset_failed" not in out.names()
    assert out.names(bundle.web_unit).count("stop") == 1


def test_a_failed_probe_does_not_roll_the_secret_back_either(bundle, capsys, tmp_path):
    options = noninteractive(tmp_path, f'port = 9000\nbind = "{LAN}"\nallow = ["10.0.0.0/8"]\nauth = "basic"\n',
                             "install", _pw(tmp_path))
    out = reconf(bundle, capsys, options, probe=lambda n: (71, "bind_not_local"))
    assert out.rc == 1 and out.err == f"afx ops: bind_not_local ({RERUN})\n"
    assert (bundle.xdg / "secret" / "web.password").read_text() == PW
    assert b'auth = "basic"' in (bundle.xdg / "config" / "ops.toml").read_bytes()


def test_the_interactive_flow_reasks_at_most_three_times_and_reports_the_last_reason(bundle, capsys):
    io = ScriptIO(answers=[""] * 6)
    out = reconf(bundle, capsys, WebOptions(), io, probe=lambda n: (73, "probe_bind_failed"))
    assert out.rc == 1 and len(out.probes) == 3
    assert out.err == f"afx ops: probe_bind_failed ({RERUN})\n"
    assert io.err == ["afx-ops: probe_bind_failed"] * 3
    assert "start" not in out.names()


def test_an_interactive_retry_then_success_restarts(bundle, capsys):
    io = ScriptIO(answers=["", "", "9001"])
    out = reconf(bundle, capsys, WebOptions(), io, probe=lambda n: (73, "probe_bind_failed") if n == 1 else (0, ""))
    assert out.rc == 0 and out.stdout == ["web: reconfigured"] and len(out.probes) == 2


def test_eof_during_the_questions_changes_nothing_and_never_stops_ops_web(bundle, capsys):
    before = unchanged_state(bundle), tree(bundle.xdg)
    out = reconf(bundle, capsys, WebOptions(), ScriptIO(eof_after=1, answers=[""]))
    assert out.rc == 1 and out.err == "afx ops: setup_aborted\n"
    assert (unchanged_state(bundle), tree(bundle.xdg)) == before and "stop" not in out.names()


def test_eof_while_reasking_after_a_probe_failure_is_aborted_and_keeps_the_target(bundle, capsys):
    io = ScriptIO(answers=["9000", ""], eof_after=2)
    out = reconf(bundle, capsys, WebOptions(), io, probe=lambda n: (73, "probe_bind_failed"))
    assert out.rc == 1 and out.err.endswith("afx ops: setup_aborted\n")
    assert b"port = 9000" in (bundle.xdg / "config" / "ops.toml").read_bytes() and "start" not in out.names()


# --- 手順 (4) ----------------------------------------------------------------------------

@pytest.mark.parametrize("afx_state", ["active", "activating", "reloading"])
def test_with_a_running_afx_ops_web_is_stopped_reset_and_started_and_checked(bundle, capsys, tmp_path, afx_state):
    bundle.systemd.afx_state = afx_state
    out = reconf(bundle, capsys, noninteractive(tmp_path))
    assert out.rc == 0 and out.stdout == ["web: reconfigured"] and out.err == ""
    calls = bundle.systemd.calls
    last_stop = max(i for i, c in enumerate(calls) if c[:2] == ("stop", bundle.web_unit))
    tail = [c[0] for c in calls[last_stop:] if len(c) > 1 and c[1] == bundle.web_unit]
    assert tail == ["stop", "is_stopped", "reset_failed", "start", "is_active"]
    assert bundle.web_unit in bundle.systemd.active
    assert_only_allowed_calls(bundle)


@pytest.mark.parametrize("afx_state", ["deactivating", "inactive", "failed"])
def test_with_an_afx_that_is_not_up_nothing_is_started(bundle, capsys, tmp_path, afx_state):
    bundle.systemd.afx_state = afx_state
    out = reconf(bundle, capsys, noninteractive(tmp_path))
    assert out.rc == 0 and out.stdout == ["web: reconfigured ops=starts_with_afx"]
    assert "start" not in out.names() and "reset_failed" not in out.names()
    assert bundle.web_unit not in bundle.systemd.active


def test_an_afx_state_that_cannot_be_read_leaves_ops_web_stopped_and_unstarted(bundle, capsys, tmp_path):
    def hook(name):
        if name == "probe_succeeded":
            bundle.systemd.afx_show_error = True

    out = reconf(bundle, capsys, noninteractive(tmp_path), hook=hook)
    assert out.rc == 1 and out.err == f"afx ops: show ({RERUN})\n"
    assert "start" not in out.names() and bundle.web_unit not in bundle.systemd.active


@pytest.mark.parametrize("seen, ok", [("inactive", True), ("failed", True), ("active", False),
                                      ("activating", False), ("reloading", False), ("deactivating", False)])
def test_a_start_failure_re_observes_afx_to_tell_a_stopped_afx_from_a_real_failure(
        bundle, capsys, tmp_path, seen, ok):
    bundle.systemd.fail["start"] = SystemdError("start", "rc=1")
    bundle.systemd.on_call["start"] = lambda system: setattr(system, "afx_state", seen)
    out = reconf(bundle, capsys, noninteractive(tmp_path))
    if ok:
        assert out.rc == 0 and out.stdout == ["web: reconfigured ops=starts_with_afx"]
    else:
        assert out.rc == 1 and out.err == f"afx ops: start ({RERUN})\n"
    names = out.names(bundle.paths.afx_unit)
    assert "stop" not in names and "start" not in names


def test_a_start_failure_whose_re_observation_fails_reports_the_start_failure(bundle, capsys, tmp_path):
    bundle.systemd.fail["start"] = SystemdError("start", "rc=1")
    bundle.systemd.on_call["start"] = lambda system: setattr(system, "afx_show_error", True)
    out = reconf(bundle, capsys, noninteractive(tmp_path))
    assert out.rc == 1 and out.err == f"afx ops: start ({RERUN})\n"


def test_a_stop_failure_before_the_start_is_reported_with_its_fixed_reason(bundle, capsys, tmp_path):
    seen = {"n": 0}

    def fail_second(system):
        seen["n"] += 1
        if seen["n"] == 2:
            raise SystemdError("stop", "rc=1")

    bundle.systemd.on_call["stop"] = fail_second
    out = reconf(bundle, capsys, noninteractive(tmp_path))
    assert out.rc == 1 and out.err == f"afx ops: stop ({RERUN})\n" and "start" not in out.names()


def test_an_ops_web_that_is_not_ready_is_ops_not_ready_and_is_left_to_the_unit_restart(bundle, capsys, tmp_path):
    before = unchanged_state(bundle)
    out = reconf(bundle, capsys, noninteractive(tmp_path), ready=lambda _f: False)
    assert out.rc == 1 and out.err == f"afx ops: ops_not_ready ({RERUN})\n"
    assert out.names(bundle.web_unit)[-2:] == ["start", "is_active"] or "start" in out.names()
    assert out.names(bundle.paths.afx_unit).count("stop") == 0
    assert out.names(bundle.web_unit)[out.names(bundle.web_unit).index("start"):].count("stop") == 0
    assert unchanged_state(bundle) == before


def test_a_start_limited_ops_web_starts_after_the_reset(bundle, capsys, tmp_path):
    bundle.systemd.failed.add(bundle.web_unit)
    bundle.systemd.start_limited.add(bundle.web_unit)
    out = reconf(bundle, capsys, noninteractive(tmp_path))
    assert out.rc == 0 and out.stdout == ["web: reconfigured"] and bundle.web_unit in bundle.systemd.active
    assert out.names(bundle.web_unit).index("reset_failed") < out.names(bundle.web_unit).index("start")
    assert all(c[1] == bundle.web_unit for c in bundle.systemd.calls if c[0] == "reset_failed")


# --- afx に触れない・鍵と stage は byte 不変 ----------------------------------------

@pytest.mark.parametrize("afx_state", AFX_STATES)
def test_nothing_but_the_allowed_calls_and_no_instance_lock_on_any_path(bundle, capsys, tmp_path, afx_state,
                                                                       monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("instance lock taken")

    monkeypatch.setattr("agentic_fx.store.instance_lock.acquire_instance_lock", forbidden)
    monkeypatch.setattr("agentic_fx.ops_setup.stages.acquire_instance_lock", forbidden)
    monkeypatch.setattr("agentic_fx.ops_setup.setup_lease.acquire_instance_lock", forbidden)
    bundle.systemd.afx_state = afx_state
    lock_file = bundle.root / "data" / "instance.lock"
    lock_stat = lock_file.stat().st_mtime_ns if lock_file.exists() else None
    before = unchanged_state(bundle)
    out = reconf(bundle, capsys, noninteractive(tmp_path))
    assert out.rc == 0
    assert_only_allowed_calls(bundle)
    assert (lock_file.stat().st_mtime_ns if lock_file.exists() else None) == lock_stat
    assert unchanged_state(bundle) == before


def test_the_success_path_leaves_keys_stage_and_marker_byte_identical(bundle, capsys, tmp_path):
    before = unchanged_state(bundle)
    assert reconf(bundle, capsys, noninteractive(tmp_path)).rc == 0
    assert unchanged_state(bundle) == before
    assert not enroll_marker_path(bundle.root).exists()


CRASH_POINTS = ["ops_stopped", "secret_tmp_created", "secret_renamed", "secret_dir_synced", "secret_applied",
                "config_tmp_created", "config_tmp_synced", "config_renamed", "config_dir_synced",
                "config_applied", "probe_succeeded", "ops_probed", "ops_restopped", "ops_reset_failed",
                "ops_started"]


@pytest.mark.parametrize("point", CRASH_POINTS)
def test_a_death_at_any_point_leaves_keys_stage_and_marker_untouched_and_a_rerun_completes(
        bundle, capsys, tmp_path, point):
    text = f'port = 9000\nbind = "{LAN}"\nallow = ["10.0.0.0/8"]\nauth = "basic"\n'
    options = noninteractive(tmp_path, text, "install", _pw(tmp_path))
    before = unchanged_state(bundle)

    def crash(name):
        if name == point:
            raise Crash(name)

    with pytest.raises(Crash):
        reconf(bundle, capsys, options, hook=crash)
    capsys.readouterr()
    assert unchanged_state(bundle) == before
    assert not enroll_marker_path(bundle.root).exists()
    assert_only_allowed_calls(bundle)
    bundle.systemd.calls.clear()
    out = reconf(bundle, capsys, options)
    assert out.rc == 0 and out.stdout == ["web: reconfigured"]
    assert web_access_reason(bundle.root, interfaces=lambda: [LAN]) is None
    assert unchanged_state(bundle) == before
    assert bundle.web_unit in bundle.systemd.active
    assert_only_allowed_calls(bundle)


def test_a_rerun_after_the_start_is_not_needed_but_harmless(bundle, capsys, tmp_path):
    options = noninteractive(tmp_path)
    assert reconf(bundle, capsys, options).rc == 0
    assert reconf(bundle, capsys, options).rc == 0 and bundle.web_unit in bundle.systemd.active


def test_the_intermediate_pair_left_by_a_death_is_fixed_by_the_rerun_not_by_enable(bundle, capsys, tmp_path):
    text = f'port = 9000\nbind = "{LAN}"\nallow = ["10.0.0.0/8"]\nauth = "basic"\n'

    def crash(name):
        if name == "secret_applied":
            raise Crash

    with pytest.raises(Crash):
        reconf(bundle, capsys, noninteractive(tmp_path, text, "install", _pw(tmp_path)), hook=crash)
    assert web_access_reason(bundle.root, interfaces=lambda: [LAN]) == "secret_unexpected"
    bundle.setup.access_check = bundle.setup._web_access_reason
    bundle.systemd.calls.clear()
    bundle.systemd.active.discard(bundle.web_unit)
    from agentic_fx.ops_setup.stages import InvalidState
    with pytest.raises(InvalidState) as raised:
        bundle.setup.enable("web")
    assert raised.value.detail == RERUN
    assert not {"stop", "start", "ensure_active"} & set(bundle.systemd.names())


# --- 別名 ----------------------------------------------------------------------------------

def _through_main(spelling, tmp_path, monkeypatch, capsys):
    bundle = make_bundle(tmp_path / spelling, monkeypatch) if (tmp_path / spelling).mkdir() is None else None
    writes: list[str] = []
    ios = ScriptIO(tty=False)

    def wrapper(root, feature, **kwargs):
        return run_ops_reconfigure(root, feature, io=ios, setup_factory=lambda _r: bundle.setup,
                                   probe_runner=lambda _f: (0, ""), candidates=lambda: ONE_CANDIDATE,
                                   hook=lambda name: writes.append(name), token_bytes=FIXED_RNG, **kwargs)

    monkeypatch.setattr("agentic_fx.ops_setup.reconfigure.run_ops_reconfigure", wrapper)
    monkeypatch.chdir(bundle.root)
    target = _target(tmp_path / spelling, f'port = 9000\nbind = "{LAN}"\nallow = ["10.0.0.0/8"]\nauth = "basic"\n')
    password = _pw(tmp_path / spelling)
    rc = main(["ops", "web", spelling, "--non-interactive", "--config-file", target, "--secret-action", "install",
               "--password-file", password])
    captured = capsys.readouterr()
    normalised = [tuple(str(part).replace(bundle.paths.afx_unit, "AFX").replace(str(bundle.root), "ROOT")
                        for part in call) for call in bundle.systemd.calls]
    return rc, captured.out, captured.err, normalised, writes, ios.out


def test_reconf_and_reconfigure_are_the_same_command(tmp_path, monkeypatch, capsys):
    first = _through_main("reconfigure", tmp_path, monkeypatch, capsys)
    second = _through_main("reconf", tmp_path, monkeypatch, capsys)
    assert first == second
    assert first[0] == 0 and first[5] == ["web: reconfigured"]


@pytest.mark.parametrize("spelling", ["reconfigure", "reconf"])
@pytest.mark.parametrize("argv", [["--ask"], ["--ask", "--ask"], ["--config-file", "a", "--config-file", "b"],
                                  ["--non-interactive", "--non-interactive"], ["--config-file"], ["--secret-action"],
                                  ["--bogus"], ["--password-file", "p"]])
def test_every_malformed_option_is_input_required_for_both_spellings(tmp_path, monkeypatch, capsys, spelling, argv):
    monkeypatch.chdir(tmp_path)
    assert main(["ops", "web", spelling, *argv]) == 2
    captured = capsys.readouterr()
    assert captured.err == "afx ops: setup_input_required\n" and captured.out == ""
    assert not (tmp_path / "data" / "ops" / "web.stage.json").exists()
