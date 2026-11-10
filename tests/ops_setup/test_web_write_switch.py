"""`afx ops web enable --write` / `--no-write` と、書込みが有効な間の reconfigure。

systemd と鍵 dir は fake / tmp を使い、本番の unit・`~/.config/agentic-fx/`・`data/` には触れない。
"""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from agentic_fx.entry import main
from agentic_fx.ops.contracts import Principal
from agentic_fx.ops.keys import enrolled_key_matches, key_dir, key_file_name, principal_entry
from agentic_fx.ops_setup.feature_config import web_access_check, web_live_config
from agentic_fx.ops_setup.interactive import interactive_enable
from agentic_fx.ops_setup.reconfigure import run_ops_reconfigure
from agentic_fx.ops_setup.stages import InvalidState, read_marker, read_stage
from agentic_fx.ops_setup.web_setup import SecretPlan, WebOptions, access_line, apply_secret
from web_support import (ACCESS_Q, AUTH_Q, CANDIDATES, KEEP_BASIC_Q, LAN, PASSWORD_Q, SCOPE_Q, USER_Q, ScriptIO,
                         make_bundle, tree)

PW = "AAECAwQFBgcICQoLDA0ODw"
WRITE = Principal.OPS_WEB_WRITE
READ = Principal.OPS_WEB
BASIC = {"port": 8788, "auth": "basic"}


class Crash(BaseException):
    pass


def make(tmp_path, monkeypatch, *, afx_active=True, values=None):
    """loopback + Basic 認証で enable 済みの web (afx の稼働は選べる)。"""
    bundle = make_bundle(tmp_path, monkeypatch, enable_web=False, web_values=values or BASIC,
                         afx_active=afx_active)
    if not afx_active:
        bundle.systemd.afx_state = "inactive"
    apply_secret(bundle.root, SecretPlan("install", PW))
    assert bundle.setup.enable("web").stage == "enabled"
    bundle.systemd.calls.clear()
    return bundle


def toml_of(bundle) -> dict:
    return tomllib.loads((bundle.xdg / "config" / "ops.toml").read_text())


def entry(bundle, principal=WRITE):
    return principal_entry(key_dir(bundle.root), principal)


def run(bundle, write, *, io=None, hook=None):
    io = io or ScriptIO(answers=["yes"])
    if hook is not None:
        bundle.setup.hook = hook
    result = interactive_enable(bundle.root, "web", io=io, options=WebOptions(write=write),
                                setup_factory=lambda _root: bundle.setup)
    return result, io


def afx_calls(bundle, name):
    return [c for c in bundle.systemd.calls if c[0] == name and c[1] == bundle.paths.afx_unit]


def world(bundle) -> dict:
    """鍵・stage・marker・web の設定と secret の byte 内容 (拒否が何も変えないことの確認用)。"""
    return {**bundle.snapshot(), **tree(bundle.xdg)}


# --- reader と status ------------------------------------------------------------------------

def test_the_reader_exposes_write_and_status_shows_it_in_one_word(tmp_path, monkeypatch):
    bundle = make(tmp_path, monkeypatch)
    stage = read_stage(bundle.root, "web")
    assert access_line(bundle.root, stage).endswith(" write=off")
    assert run(bundle, True)[0].rc == 0
    assert web_live_config(bundle.root)["write"] is True
    assert web_access_check(bundle.root).values["write"] is True
    assert access_line(bundle.root, read_stage(bundle.root, "web")).endswith("auth=basic user=afx write=on")


@pytest.mark.parametrize("values, reason", [
    ({"port": 8788, "write": True}, "write_requires_auth"),
    ({"port": 8788, "bind": LAN, "allow": ["192.168.1.0/24"], "auth": "basic", "write": True}, "write_requires_loopback"),
])
def test_the_reader_names_the_write_reason_on_the_live_file(tmp_path, monkeypatch, values, reason):
    bundle = make(tmp_path, monkeypatch)
    from agentic_fx.ops_setup.feature_config import write_web_config
    write_web_config(bundle.root, values)
    assert web_access_check(bundle.root, interfaces=lambda: [LAN]).reason == reason
    assert access_line(bundle.root, read_stage(bundle.root, "web"),
                       interfaces=lambda: [LAN]) == f"web-access: unavailable reason={reason}"


# --- 有効 → 無効 → 再有効で鍵が回る ---

def test_enable_disable_and_enable_again_rotates_the_revoked_key(tmp_path, monkeypatch):
    bundle = make(tmp_path, monkeypatch)
    assert entry(bundle) is None
    result, io = run(bundle, True)
    assert result.rc == 0 and result.reason_code == "ok"
    assert entry(bundle) == ("active", None) and enrolled_key_matches(key_dir(bundle.root), WRITE)
    assert toml_of(bundle)["write"] is True and toml_of(bundle)["auth"] == "basic"
    stage = read_stage(bundle.root, "web")
    assert (stage["operation"], stage["stage"]) == ("enable", "enabled")
    assert not any(key in stage for key in ("write_target", "write_principal", "write_step"))
    assert read_marker(bundle.root) is None
    assert enrolled_key_matches(key_dir(bundle.root), READ)

    result, _ = run(bundle, False)
    assert result.rc == 0
    assert entry(bundle) == ("revoked", None)
    assert "write" not in toml_of(bundle)
    assert enrolled_key_matches(key_dir(bundle.root), READ)
    assert (key_dir(bundle.root) / key_file_name(WRITE)).read_bytes() == b"revoked\n"

    result, _ = run(bundle, True)
    assert result.rc == 0
    assert entry(bundle) == ("active", None) and enrolled_key_matches(key_dir(bundle.root), WRITE)
    ready = json.loads((key_dir(bundle.root) / ".ready").read_text())
    assert ready["principals"][WRITE.value]["generation"] == 3
    assert toml_of(bundle)["write"] is True


def test_the_running_afx_is_announced_confirmed_stopped_and_started_again(tmp_path, monkeypatch):
    bundle = make(tmp_path, monkeypatch)
    result, io = run(bundle, True)
    assert result.rc == 0
    assert any("afx を一時的に止めます" in text for text in io.out)
    assert any("yes" in label for label in io.asked)
    assert len(afx_calls(bundle, "stop")) >= 1 and len(afx_calls(bundle, "start")) == 1
    assert bundle.systemd.afx_state == "active"
    assert any("password" in text and "変える" in text for text in io.out)
    assert bundle.systemd.names().index("stop") < bundle.systemd.names().index("start")


def test_an_afx_that_was_stopped_is_not_started(tmp_path, monkeypatch):
    bundle = make(tmp_path, monkeypatch, afx_active=False)
    io = ScriptIO(answers=[])
    result, _ = run(bundle, True, io=io)
    assert result.rc == 0 and io.asked == []
    assert afx_calls(bundle, "start") == [] and bundle.systemd.afx_state == "inactive"
    assert toml_of(bundle)["write"] is True and entry(bundle) == ("active", None)
    result, _ = run(bundle, False, io=ScriptIO(answers=[]))
    assert result.rc == 0 and afx_calls(bundle, "start") == []
    assert read_stage(bundle.root, "web")["afx_before_stop"] == "inactive"


def test_the_target_already_reached_changes_nothing(tmp_path, monkeypatch):
    bundle = make(tmp_path, monkeypatch)
    before = world(bundle)
    result, _ = run(bundle, False)
    assert result.rc == 0 and world(bundle) == before
    assert afx_calls(bundle, "stop") == [] and afx_calls(bundle, "start") == []
    assert run(bundle, True)[0].rc == 0
    before = world(bundle)
    bundle.systemd.calls.clear()
    result, _ = run(bundle, True)
    assert result.rc == 0 and world(bundle) == before
    assert afx_calls(bundle, "stop") == [] and afx_calls(bundle, "start") == []


# --- 構成の検査 (鍵に触れない) -----------------------------------------------------------------

@pytest.mark.parametrize("values, reason", [
    ({"port": 8788}, "write_requires_auth"),
    ({"port": 8788, "bind": LAN, "allow": ["192.168.1.0/24"], "auth": "none"}, "write_requires_auth"),
    ({"port": 8788, "bind": LAN, "allow": ["192.168.1.0/24"], "auth": "basic"}, "write_requires_loopback"),
])
def test_a_configuration_that_does_not_qualify_never_touches_the_keys(tmp_path, monkeypatch, values, reason):
    bundle = make_bundle(tmp_path, monkeypatch, enable_web=False, web_values=values)
    if values.get("auth") == "basic":
        apply_secret(bundle.root, SecretPlan("install", PW))
    bundle.setup.access_check = lambda: None
    assert bundle.setup.enable("web").stage == "enabled"
    bundle.systemd.calls.clear()
    before = world(bundle)
    result, io = run(bundle, True, io=ScriptIO(answers=[]))
    assert (result.rc, result.reason_code) == (1, reason)
    assert world(bundle) == before and all(call[0] == "show" for call in bundle.systemd.calls)
    assert entry(bundle) is None and io.asked == []
    assert any("afx ops web reconfigure" in text for text in io.err)


def test_a_web_that_is_not_enabled_is_refused(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch, enable_web=False, web_values=BASIC)
    before = world(bundle)
    result, _ = run(bundle, True, io=ScriptIO(answers=[]))
    assert (result.rc, result.reason_code) == (1, "not_enabled")
    assert world(bundle) == before and entry(bundle) is None


def test_no_write_refuses_when_the_key_to_return_to_is_not_active(tmp_path, monkeypatch):
    bundle = make(tmp_path, monkeypatch)
    assert run(bundle, True)[0].rc == 0
    (key_dir(bundle.root) / key_file_name(READ)).write_text("0" * 64 + "\n")
    (key_dir(bundle.root) / key_file_name(READ)).chmod(0o600)
    result, _ = run(bundle, False)
    assert (result.rc, result.reason_code) == (1, "principal_key_mismatch")
    assert entry(bundle) == ("active", None) and toml_of(bundle)["write"] is True
    assert (key_dir(bundle.root) / key_file_name(WRITE)).read_bytes() != b"revoked\n"


def test_a_write_key_that_is_active_but_does_not_match_is_refused(tmp_path, monkeypatch):
    bundle = make(tmp_path, monkeypatch)
    assert run(bundle, True)[0].rc == 0
    assert run(bundle, False)[0].rc == 0
    assert run(bundle, True)[0].rc == 0
    (key_dir(bundle.root) / key_file_name(WRITE)).write_text("1" * 64 + "\n")
    (key_dir(bundle.root) / key_file_name(WRITE)).chmod(0o600)
    assert run(bundle, False)[0].rc == 0
    assert entry(bundle) == ("revoked", None)  # 無効化は鍵が壊れていても revoke できる
    (key_dir(bundle.root) / key_file_name(WRITE)).write_bytes(b"revoked\n")


def test_declining_the_confirmation_changes_nothing_and_a_non_tty_cannot_confirm(tmp_path, monkeypatch):
    bundle = make(tmp_path, monkeypatch)
    before = world(bundle)
    result, _ = run(bundle, True, io=ScriptIO(answers=["no"]))
    assert (result.rc, result.reason_code) == (1, "setup_aborted") and world(bundle) == before
    assert afx_calls(bundle, "stop") == [] and entry(bundle) is None
    result, _ = run(bundle, True, io=ScriptIO(answers=[], tty=False))
    assert (result.rc, result.reason_code) == (2, "setup_input_required") and world(bundle) == before
    assert afx_calls(bundle, "stop") == []


# --- どの段で中断しても再実行で収束する ---

def _hook_names(tmp_path, monkeypatch, write, *, prepare=()):
    bundle = make(tmp_path, monkeypatch)
    for step in prepare:
        assert run(bundle, step)[0].rc == 0
    names: list[str] = []
    assert run(bundle, write, hook=names.append)[0].rc == 0
    return names


SCENARIOS = [("enable", (), True), ("disable", (True,), False), ("rotate", (True, False), True)]


@pytest.mark.parametrize("name, prepare, target", SCENARIOS)
def test_an_interruption_after_any_step_converges_on_rerun(tmp_path_factory, monkeypatch, name, prepare, target):
    names = _hook_names(tmp_path_factory.mktemp("probe"), monkeypatch, target, prepare=prepare)
    assert any(n.startswith("after_stage_write_") for n in names) and "after_keys" in names
    for index in range(len(names)):
        bundle = make(tmp_path_factory.mktemp(f"crash{index}"), monkeypatch)
        for step in prepare:
            assert run(bundle, step)[0].rc == 0
        count = 0

        def hook(point, index=index):
            nonlocal count
            if count == index:
                count += 1
                raise Crash(point)
            count += 1

        with pytest.raises(Crash):
            run(bundle, target, hook=hook)
        bundle.setup.hook = lambda _name: None
        result, _ = run(bundle, target, io=ScriptIO(answers=["yes"]))
        assert result.rc == 0, (index, result)
        assert toml_of(bundle).get("write", False) is target, index
        assert entry(bundle)[0] == ("active" if target else "revoked"), index
        assert enrolled_key_matches(key_dir(bundle.root), READ), index
        stage = read_stage(bundle.root, "web")
        assert stage["stage"] == "enabled" and "write_step" not in stage, index
        assert read_marker(bundle.root) is None, index
        assert bundle.systemd.afx_state == "active", index


def test_a_stopped_afx_stays_stopped_across_an_interruption(tmp_path, monkeypatch):
    bundle = make(tmp_path, monkeypatch, afx_active=False)

    def hook(point):
        if point == "after_keys":
            raise Crash(point)

    with pytest.raises(Crash):
        run(bundle, True, hook=hook)
    assert read_stage(bundle.root, "web")["write_step"] == "stopped"
    bundle.setup.hook = lambda _name: None
    assert run(bundle, True, io=ScriptIO(answers=[]))[0].rc == 0
    assert afx_calls(bundle, "start") == [] and entry(bundle) == ("active", None)


def test_a_different_target_on_rerun_replaces_the_recorded_one(tmp_path, monkeypatch):
    bundle = make(tmp_path, monkeypatch)

    def hook(point):
        if point == "after_keys":
            raise Crash(point)

    with pytest.raises(Crash):
        run(bundle, True, hook=hook)
    assert entry(bundle) == ("active", None) and "write" not in toml_of(bundle)
    bundle.setup.hook = lambda _name: None
    assert run(bundle, False)[0].rc == 0
    assert entry(bundle) == ("revoked", None) and "write" not in toml_of(bundle)
    assert "write_step" not in read_stage(bundle.root, "web")


def test_the_recorded_goal_names_the_principal_and_status_reports_the_pending_switch(tmp_path, monkeypatch):
    bundle = make(tmp_path, monkeypatch)

    def hook(point):
        if point == "after_stage_write_stopped":
            raise Crash(point)

    with pytest.raises(Crash):
        run(bundle, True, hook=hook)
    stage = read_stage(bundle.root, "web")
    assert (stage["write_target"], stage["write_principal"], stage["write_step"]) == (True, "ops-web-write", "stopped")
    assert "write_switch=stopped target=on" in bundle.setup.status_line("web")
    assert read_marker(bundle.root) == {**read_marker(bundle.root), "feature": "web"}


def test_a_hand_edited_switch_record_is_rejected(tmp_path, monkeypatch):
    bundle = make(tmp_path, monkeypatch)
    path = bundle.stage_file
    for patch in ({"write_target": True}, {"write_target": True, "write_principal": "ops-web", "write_step": "staged"},
                  {"write_target": True, "write_principal": "ops-web-write", "write_step": "nope"},
                  {"write_target": "yes", "write_principal": "ops-web-write", "write_step": "staged"}):
        state = json.loads(path.read_text())
        path.write_text(json.dumps({**state, **patch}))
        with pytest.raises(InvalidState) as caught:
            read_stage(bundle.root, "web")
        assert caught.value.reason == "invalid_stage"
        path.write_text(json.dumps(state))


def _leave_pending(bundle):
    def hook(point):
        if point == "after_stage_write_stopped":
            raise Crash(point)

    with pytest.raises(Crash):
        run(bundle, True, hook=hook)
    bundle.setup.hook = lambda _name: None


def test_a_pending_switch_blocks_plain_enable_and_reconfigure(tmp_path, monkeypatch, capsys):
    bundle = make(tmp_path, monkeypatch)
    _leave_pending(bundle)
    before = world(bundle)
    with pytest.raises(InvalidState) as caught:
        bundle.setup.enable("web")
    assert caught.value.reason == "write_switch_pending"
    io = ScriptIO(tty=False)
    options = WebOptions(non_interactive=True, config_file=_config(tmp_path, 'port = 9000\nauth = "basic"\n'),
                         secret_action="keep")
    rc = run_ops_reconfigure(bundle.root, "web", options=options, io=io, setup_factory=lambda _r: bundle.setup,
                             probe_runner=lambda _f: (0, ""), candidates=lambda: CANDIDATES)
    assert rc == 1 and "write_switch_pending" in capsys.readouterr().err
    assert world(bundle) == before


@pytest.mark.parametrize("feature", ["notify", "bot", "web"])
def test_a_pending_switch_refuses_enable_before_any_input_is_collected_or_saved(tmp_path, monkeypatch, feature):
    """入口の対話・設定と secret の保存・probe より前に、読取専用の検査で拒否する。"""
    bundle = make(tmp_path, monkeypatch)
    _crash_at(bundle, "after_stage_write_stopped")
    before = world(bundle)
    probes: list[str] = []
    io = ScriptIO(answers=["x"] * 8, secret_answers=["x"] * 4)
    result = interactive_enable(bundle.root, feature, io=io, setup_factory=lambda _root: bundle.setup,
                                probe_runner=lambda name: probes.append(name) or (0, ""),
                                candidates=lambda: CANDIDATES)
    assert (result.rc, result.reason_code) == (1, "write_switch_pending")
    assert io.asked == [] and io.of("secret") == [] and probes == []
    assert world(bundle) == before


# --- 書込みが有効な間の reconfigure ---

def _config(tmp_path: Path, text: str, name: str = "t.toml") -> str:
    path = tmp_path / name
    path.write_text(text)
    path.chmod(0o600)
    return str(path)


def _reconf(bundle, options, *, io=None, probe=lambda _f: (0, "")):
    io = io or ScriptIO(tty=not options.non_interactive)
    rc = run_ops_reconfigure(bundle.root, "web", options=options, io=io, setup_factory=lambda _r: bundle.setup,
                             probe_runner=probe, candidates=lambda: CANDIDATES)
    return rc, io


@pytest.fixture
def writing(tmp_path, monkeypatch):
    bundle = make(tmp_path, monkeypatch)
    assert run(bundle, True)[0].rc == 0
    bundle.systemd.calls.clear()
    return bundle


def test_reconfigure_keeps_write_and_the_principal_when_port_and_password_change(writing, tmp_path, capsys):
    bundle = writing
    keys_before = {p: (key_dir(bundle.root) / p).read_bytes() for p in (".ready", key_file_name(WRITE), key_file_name(READ))}
    stage_before = bundle.stage_file.read_bytes()
    options = WebOptions(non_interactive=True, config_file=_config(tmp_path, 'port = 9000\nauth = "basic"\nuser = "ops"\n'),
                         secret_action="install", password_file=_config(tmp_path, "N" * 12, "pw"))
    rc, _ = _reconf(bundle, options)
    assert rc == 0, capsys.readouterr().err
    assert toml_of(bundle) == {**toml_of(bundle), "port": 9000, "user": "ops", "write": True}
    assert (bundle.xdg / "secret" / "web.password").read_text() == "N" * 12
    assert {p: (key_dir(bundle.root) / p).read_bytes() for p in keys_before} == keys_before
    assert bundle.stage_file.read_bytes() == stage_before
    assert web_access_check(bundle.root).values["write"] is True
    assert access_line(bundle.root, read_stage(bundle.root, "web")).endswith("write=on")


def test_reconfigure_checks_the_principal_that_matches_the_current_write(writing, tmp_path, capsys):
    bundle = writing
    options = lambda: WebOptions(non_interactive=True,  # noqa: E731
                                 config_file=_config(tmp_path, 'port = 9000\nauth = "basic"\n'), secret_action="keep")
    (key_dir(bundle.root) / key_file_name(READ)).write_text("0" * 64 + "\n")
    (key_dir(bundle.root) / key_file_name(READ)).chmod(0o600)
    rc, _ = _reconf(bundle, options())
    assert rc == 0, capsys.readouterr().err  # 書込みが有効な間は ops-web の鍵を見ない
    (key_dir(bundle.root) / key_file_name(WRITE)).write_text("0" * 64 + "\n")
    (key_dir(bundle.root) / key_file_name(WRITE)).chmod(0o600)
    capsys.readouterr()
    rc, _ = _reconf(bundle, options())
    assert rc == 1
    assert "principal_key_mismatch" in capsys.readouterr().err


def test_reconfigure_still_checks_ops_web_when_write_is_off(tmp_path, monkeypatch, capsys):
    bundle = make(tmp_path, monkeypatch)
    (key_dir(bundle.root) / key_file_name(READ)).write_text("0" * 64 + "\n")
    (key_dir(bundle.root) / key_file_name(READ)).chmod(0o600)
    options = WebOptions(non_interactive=True, config_file=_config(tmp_path, 'port = 9000\nauth = "basic"\n'),
                         secret_action="keep")
    rc, _ = _reconf(bundle, options)
    assert rc == 1 and "afx keys rotate ops-web を実行し" in capsys.readouterr().err


@pytest.mark.parametrize("text, action, reason", [
    ('port = 9000\nauth = "none"\n', "remove", "write_requires_auth"),
    ('port = 9000\n', "remove", "write_requires_auth"),
    (f'port = 9000\nbind = "{LAN}"\nallow = ["192.168.1.0/24"]\nauth = "basic"\n', "keep", "write_requires_loopback"),
])
def test_reconfigure_refuses_a_target_that_write_forbids_before_changing_anything(writing, tmp_path, capsys, text, action, reason):
    bundle = writing
    before = world(bundle)
    rc, _ = _reconf(bundle, WebOptions(non_interactive=True, config_file=_config(tmp_path, text), secret_action=action))
    assert rc == 1 and f"afx ops: {reason}" in capsys.readouterr().err
    assert world(bundle) == before
    assert bundle.systemd.calls and all(call[0] in ("show", "is_active", "is_stopped") for call in bundle.systemd.calls)


def test_reconfigure_refuses_a_write_change_before_changing_anything(writing, tmp_path, capsys):
    bundle = writing
    before = world(bundle)
    rc, _ = _reconf(bundle, WebOptions(write=False))
    assert rc == 2 and "write_change_refused" in capsys.readouterr().err
    rc, _ = _reconf(bundle, WebOptions(non_interactive=True, secret_action="keep",
                                       config_file=_config(tmp_path, 'port = 9000\nauth = "basic"\nwrite = false\n')))
    err = capsys.readouterr().err
    assert rc == 2 and "write_change_refused" in err and "enable --write / --no-write" in err
    assert world(bundle) == before and all(c[0] in ("show", "is_active", "is_stopped") for c in bundle.systemd.calls)


@pytest.mark.parametrize("answers, reason", [
    (["", "2", "1", "1", "10.0.0.0/8", "1"], "write_requires_auth"),
    (["", "2", "1", "2", "1"], "write_requires_loopback"),
])
def test_interactive_reconfigure_that_gives_up_loopback_or_basic_is_refused_before_any_write(
        writing, capsys, answers, reason):
    bundle = writing
    before = world(bundle)
    rc, _ = _reconf(bundle, WebOptions(), io=ScriptIO(answers=list(answers)))
    err = capsys.readouterr().err
    assert rc == 1 and f"afx ops: {reason}" in err and "--non-interactive --config-file" in err
    assert world(bundle) == before
    assert all(call[0] in ("show", "is_active", "is_stopped") for call in bundle.systemd.calls)


# --- CLI の入口 ------------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["--write", "--no-write"], ["--write", "--write"], ["--write", "--ask"], ["--no-write", "--non-interactive"],
    ["--write", "--secret-action", "keep"],
])
def test_write_flags_that_do_not_form_one_request_are_input_errors(tmp_path, monkeypatch, capsys, argv):
    bundle = make(tmp_path, monkeypatch)
    before = world(bundle)
    rc = main(["--root", str(bundle.root), "ops", "web", "enable", *argv])
    assert rc == 2 and "setup_input_required" in capsys.readouterr().err
    assert world(bundle) == before


def test_reconfigure_rejects_the_write_flags_from_the_command_line(tmp_path, monkeypatch, capsys):
    bundle = make(tmp_path, monkeypatch)
    before = world(bundle)
    rc = main(["--root", str(bundle.root), "ops", "web", "reconfigure", "--write"])
    assert rc == 2 and "write_change_refused" in capsys.readouterr().err
    assert world(bundle) == before


# --- 切替の途中の再開と、途中の操作の拒否 -------------------------------------------------------------

def _crash_at(bundle, point, target=True):
    def hook(name):
        if name == point:
            raise Crash(name)

    with pytest.raises(Crash):
        run(bundle, target, hook=hook)
    bundle.setup.hook = lambda _name: None


def _start_from_outside(bundle):
    """afx と web を、marker の有無にかかわらず外から起こす (旧設定のまま)。"""
    bundle.systemd.afx_state = "active"
    bundle.systemd.active.update({bundle.paths.afx_unit, bundle.web_unit})
    bundle.systemd.calls.clear()


def _watch_web_starts(bundle):
    """web を起こす瞬間の ops.toml の write と書込み鍵の状態を記録する。"""
    seen = []

    def record(systemd):
        call = systemd.calls[-1]
        if call[1] == bundle.web_unit:
            seen.append((call[0], toml_of(bundle).get("write", False), entry(bundle)[0]))

    bundle.systemd.on_call["ensure_active"] = record
    bundle.systemd.on_call["start"] = record
    return seen


@pytest.mark.parametrize("point, marker_kept", [
    ("after_stage_write_keyed", True), ("after_stage_write_configured", True), ("after_unmark", False)])
def test_a_rerun_restarts_a_web_that_was_started_from_outside_with_the_old_settings(
        tmp_path, monkeypatch, point, marker_kept):
    """marker が残る間の外部起動は止め直す。設定が書かれ marker も外れた後の起動は afx を止めず web だけ起こし直す。"""
    bundle = make(tmp_path, monkeypatch)
    _crash_at(bundle, point)
    assert (read_marker(bundle.root) is not None) is marker_kept
    _start_from_outside(bundle)
    seen = _watch_web_starts(bundle)
    result, _ = run(bundle, True)
    assert result.rc == 0
    assert toml_of(bundle)["write"] is True and entry(bundle)[0] == "active" and read_marker(bundle.root) is None
    stops = [i for i, c in enumerate(bundle.systemd.calls) if c[:2] == ("stop", bundle.web_unit)]
    assert stops, "web は止め直される"
    assert seen and all(item[1] is True and item[2] == "active" for item in seen)
    assert bundle.systemd.calls.index(("ensure_active", bundle.web_unit)) > stops[-1]
    assert bundle.systemd.afx_state == "active" and bundle.web_unit in bundle.systemd.active
    assert (("stop", bundle.paths.afx_unit) in bundle.systemd.calls) is marker_kept


def _watch_afx_start_against_the_limit(bundle):
    """afx を起こす瞬間に web が start-limit のままかを記録する (残っていれば web は起きない)。"""
    seen = []

    def record(systemd):
        if systemd.calls[-1][1] == bundle.paths.afx_unit:
            seen.append(bundle.web_unit in systemd.start_limited)

    bundle.systemd.on_call["start"] = record
    return seen


@pytest.mark.parametrize("point", ["after_stage_write_configured", "after_unmark"])
def test_a_failed_web_held_by_the_start_limit_is_reset_before_the_rerun_starts_it(tmp_path, monkeypatch, point):
    bundle = make(tmp_path, monkeypatch)
    _crash_at(bundle, point)
    bundle.systemd.afx_state = "active"
    bundle.systemd.active.discard(bundle.web_unit)
    bundle.systemd.failed.add(bundle.web_unit)
    bundle.systemd.start_limited.add(bundle.web_unit)
    bundle.systemd.calls.clear()
    seen = _watch_afx_start_against_the_limit(bundle)
    assert run(bundle, True)[0].rc == 0
    assert seen == [False]
    assert ("reset_failed", bundle.web_unit) in bundle.systemd.calls
    assert bundle.web_unit in bundle.systemd.active and bundle.web_unit not in bundle.systemd.failed


def test_a_rerun_after_an_interruption_between_the_web_stop_and_the_reset_still_resets(tmp_path, monkeypatch):
    bundle = make(tmp_path, monkeypatch)
    _crash_at(bundle, "after_unmark")
    _start_from_outside(bundle)
    bundle.systemd.start_limited.add(bundle.web_unit)

    def die(_systemd):
        raise Crash("reset_failed")

    bundle.systemd.on_call["reset_failed"] = die
    with pytest.raises(Crash):
        run(bundle, True)
    assert bundle.web_unit not in bundle.systemd.active and bundle.web_unit in bundle.systemd.start_limited
    del bundle.systemd.on_call["reset_failed"]
    bundle.systemd.calls.clear()
    seen = _watch_afx_start_against_the_limit(bundle)
    assert run(bundle, True)[0].rc == 0
    assert seen == [False]
    assert ("reset_failed", bundle.web_unit) in bundle.systemd.calls
    assert bundle.web_unit in bundle.systemd.active


def test_the_marker_and_the_lock_stay_until_the_configuration_is_written(tmp_path, monkeypatch):
    bundle = make(tmp_path, monkeypatch)
    names: list[str] = []
    marker_at: dict[str, bool] = {}

    def hook(name):
        names.append(name)
        marker_at[name] = read_marker(bundle.root) is not None

    assert run(bundle, True, hook=hook)[0].rc == 0
    for name in ("after_keys", "before_config", "after_config", "after_stage_write_configured", "before_unmark"):
        assert marker_at[name], name
    assert not marker_at["after_unmark"]
    assert names.index("after_config") < names.index("before_unmark")


def test_a_configured_record_whose_stop_no_longer_holds_goes_back_to_the_stop(tmp_path, monkeypatch):
    bundle = make(tmp_path, monkeypatch)
    _crash_at(bundle, "after_stage_write_configured")
    _start_from_outside(bundle)
    assert run(bundle, True)[0].rc == 0
    assert ("stop", bundle.paths.afx_unit) in bundle.systemd.calls
    assert read_stage(bundle.root, "web")["stage"] == "enabled"


def test_a_disable_or_another_feature_is_refused_while_a_switch_is_pending(tmp_path_factory, monkeypatch):
    names = _hook_names(tmp_path_factory.mktemp("names"), monkeypatch, True)
    for index in range(len(names)):
        bundle = make(tmp_path_factory.mktemp(f"pending{index}"), monkeypatch)
        count = 0

        def hook(point, index=index):
            nonlocal count
            if count == index:
                count += 1
                raise Crash(point)
            count += 1

        with pytest.raises(Crash):
            run(bundle, True, hook=hook)
        bundle.setup.hook = lambda _name: None
        before = world(bundle)
        calls = len(bundle.systemd.calls)
        for operation in (lambda: bundle.setup.disable("web"), lambda: bundle.setup.enable("notify")):
            with pytest.raises(InvalidState) as caught:
                operation()
            assert caught.value.reason == "write_switch_pending", (index, names[index])
            assert "enable --write" in caught.value.detail
        assert world(bundle) == before, (index, names[index])
        assert bundle.systemd.calls[calls:] == [] or all(c[0] in ("show", "is_active", "is_stopped")
                                                         for c in bundle.systemd.calls[calls:]), index
        result, _ = run(bundle, True)
        assert result.rc == 0 and toml_of(bundle)["write"] is True, index
        assert read_marker(bundle.root) is None and bundle.setup.disable("web").stage == "disabled", index


def test_disable_through_the_cli_is_refused_with_the_fixed_reason(tmp_path, monkeypatch, capsys):
    bundle = make(tmp_path, monkeypatch)
    _crash_at(bundle, "after_stage_write_stopped")
    before = world(bundle)
    rc = main(["--root", str(bundle.root), "ops", "web", "disable"])
    assert rc == 1 and "write_switch_pending" in capsys.readouterr().err
    assert world(bundle) == before


# --- reconfigure: 読めない現行設定と、拒否のときの不変 ------------------------------------------------

def _damage_live(bundle, how):
    path = bundle.xdg / "config" / "ops.toml"
    if how == "unknown_key":
        path.write_text(path.read_text() + 'unknown = 1\n')
    elif how == "bad_type":
        path.write_text(path.read_text().replace("port = 8788", 'port = "8788"'))
    elif how == "unreadable_mode":
        path.chmod(0o666)
    else:
        path.write_text("not = [toml")


def _readonly_with_key(tmp_path, monkeypatch, key):
    """write の無い (読取専用の) web に、書込みの鍵を指定の状態で持たせる。"""
    from agentic_fx.ops.keys import enroll
    from agentic_fx.ops_setup.feature_config import write_web_config
    bundle = make(tmp_path, monkeypatch)
    if key in ("revoked", "active"):
        assert run(bundle, True)[0].rc == 0
        if key == "revoked":
            assert run(bundle, False)[0].rc == 0
        else:
            write_web_config(bundle.root, dict(BASIC))
    elif key == "preparing":
        def stop_after_manifest(step):
            if step == "preparing:renamed":
                raise Crash(step)

        with pytest.raises(Crash):
            enroll(key_dir(bundle.root), WRITE, hook=stop_after_manifest)
    assert (entry(bundle) or (None,))[0] == {"unregistered": None}.get(key, key)
    bundle.systemd.calls.clear()
    return bundle


def _reconf_to_9000(bundle, tmp_path, interactive=False):
    if interactive:
        return _reconf(bundle, WebOptions(), io=ScriptIO(answers=["", "", "2", "1"]))
    return _reconf(bundle, WebOptions(non_interactive=True, secret_action="keep",
                                      config_file=_config(tmp_path, 'port = 9000\nauth = "basic"\n')))


def _mutations(bundle):
    return [call for call in bundle.systemd.calls if call[0] not in ("show", "is_active", "is_stopped")]


@pytest.mark.parametrize("how", ["unknown_key", "bad_type", "broken_toml"])
@pytest.mark.parametrize("key", ["unregistered", "revoked", "active", "preparing"])
def test_a_read_only_web_with_a_damaged_live_file_is_repaired_unless_a_write_key_is_live(
        tmp_path, monkeypatch, capsys, how, key):
    """write が決まらない・false と読める壊れた設定は、書込みの鍵が無い・revoked のときだけ修復できる。"""
    bundle = _readonly_with_key(tmp_path, monkeypatch, key)
    _damage_live(bundle, how)
    before = world(bundle)
    rc, _ = _reconf_to_9000(bundle, tmp_path)
    err = capsys.readouterr().err
    if key in ("active", "preparing"):
        assert rc == 1 and err.startswith("afx ops: write_state_undecided"), err
        assert "enable --write" in err and "--no-write" in err
        assert world(bundle) == before
        assert _mutations(bundle) == []
        assert entry(bundle)[0] == key
    else:
        assert rc == 0, err
        assert toml_of(bundle) == {**toml_of(bundle), "port": 9000, "auth": "basic"}
        assert "write" not in toml_of(bundle)
        assert entry(bundle) == (None if key == "unregistered" else ("revoked", None))
        assert list(bundle.xdg.rglob(".*.tmp")) == []


@pytest.mark.parametrize("how", ["unknown_key", "bad_type"])
def test_a_damaged_live_file_that_says_write_true_is_repaired_with_write_kept(writing, tmp_path, capsys, how):
    bundle = writing
    _damage_live(bundle, how)
    key = entry(bundle)
    rc, _ = _reconf_to_9000(bundle, tmp_path)
    assert rc == 0, capsys.readouterr().err
    assert toml_of(bundle)["write"] is True and toml_of(bundle)["port"] == 9000 and toml_of(bundle)["auth"] == "basic"
    assert entry(bundle) == key == ("active", None)


@pytest.mark.parametrize("how, reason", [("broken_toml", "write_state_undecided"),
                                         ("unreadable_mode", "config_dir_invalid")])
@pytest.mark.parametrize("interactive", [False, True])
def test_a_live_file_whose_write_cannot_be_decided_is_refused_while_the_write_key_is_active(
        writing, tmp_path, capsys, how, reason, interactive):
    bundle = writing
    _damage_live(bundle, how)
    before = world(bundle)
    rc, io = _reconf_to_9000(bundle, tmp_path, interactive)
    assert rc == 1 and capsys.readouterr().err.startswith(f"afx ops: {reason}")
    if interactive:
        assert io.asked == []
    assert world(bundle) == before
    assert entry(bundle)[0] == "active"
    assert _mutations(bundle) == []


def test_a_non_boolean_write_is_undecided_and_refused_while_the_write_key_is_active(writing, tmp_path, capsys):
    bundle = writing
    path = bundle.xdg / "config" / "ops.toml"
    path.write_text(path.read_text().replace("write = true", 'write = "yes"'))
    before = world(bundle)
    rc, _ = _reconf_to_9000(bundle, tmp_path)
    assert rc == 1 and capsys.readouterr().err.startswith("afx ops: write_state_undecided")
    assert world(bundle) == before and _mutations(bundle) == []


def test_write_true_without_an_active_write_key_is_refused_unchanged(writing, tmp_path, capsys):
    from agentic_fx.ops.keys import revoke
    bundle = writing
    revoke(key_dir(bundle.root), WRITE)
    before = world(bundle)
    rc, _ = _reconf_to_9000(bundle, tmp_path)
    assert rc == 1 and capsys.readouterr().err.startswith("afx ops:")
    assert world(bundle) == before and _mutations(bundle) == []


def test_an_interactive_repair_of_a_broken_file_asks_every_question_instead_of_using_its_values(
        tmp_path, monkeypatch, capsys):
    bundle = _readonly_with_key(tmp_path, monkeypatch, "unregistered")
    _damage_live(bundle, "broken_toml")
    rc, io = _reconf_to_9000(bundle, tmp_path, interactive=True)
    assert rc == 0, capsys.readouterr().err
    assert len(io.asked) >= 3
    assert "write" not in toml_of(bundle)


LEFTOVERS = {"config": ".ops.toml.{}.tmp", "secret": ".web.password.{}.tmp"}


def _leave_temporaries(bundle):
    for index, (directory, pattern) in enumerate(LEFTOVERS.items()):
        path = bundle.xdg / directory / pattern.format(f"{index:x}" * 32)
        path.write_bytes(b"left by an interrupted writer")
        path.chmod(0o600)


@pytest.mark.parametrize("text, action", [
    ('port = 9000\nauth = "basic"\nwrite = false\n', "keep"),
    ('port = 9000\nauth = "none"\n', "remove"),
    (f'port = 9000\nbind = "{LAN}"\nallow = ["192.168.1.0/24"]\nauth = "basic"\n', "keep"),
])
def test_a_refused_reconfigure_leaves_the_files_a_death_left_in_place(writing, tmp_path, capsys, text, action):
    bundle = writing
    _leave_temporaries(bundle)
    before = world(bundle)
    leftovers = sorted(p.name for d in LEFTOVERS for p in (bundle.xdg / d).glob(".*.tmp"))
    assert len(leftovers) == 2
    rc, _ = _reconf(bundle, WebOptions(non_interactive=True, config_file=_config(tmp_path, text), secret_action=action))
    assert rc in (1, 2) and capsys.readouterr().err.startswith("afx ops:")
    assert world(bundle) == before
    assert sorted(p.name for d in LEFTOVERS for p in (bundle.xdg / d).glob(".*.tmp")) == leftovers
    assert all(call[0] in ("show", "is_active", "is_stopped") for call in bundle.systemd.calls)


def test_a_refused_interactive_reconfigure_leaves_the_files_a_death_left_in_place(writing, capsys):
    bundle = writing
    _leave_temporaries(bundle)
    before = world(bundle)
    rc, _ = _reconf(bundle, WebOptions(), io=ScriptIO(answers=["", "2", "1", "1", "10.0.0.0/8", "1"]))
    assert rc == 1 and "write_requires_auth" in capsys.readouterr().err
    assert world(bundle) == before


def test_an_accepted_reconfigure_still_removes_the_files_a_death_left(writing, tmp_path, capsys):
    bundle = writing
    _leave_temporaries(bundle)
    rc, _ = _reconf(bundle, WebOptions(non_interactive=True, secret_action="keep",
                                       config_file=_config(tmp_path, 'port = 9000\nauth = "basic"\n')))
    assert rc == 0, capsys.readouterr().err
    assert list(bundle.xdg.rglob(".*.tmp")) == []
    assert toml_of(bundle)["write"] is True
    _leave_temporaries(bundle)
    rc, _ = _reconf(bundle, WebOptions(), io=ScriptIO(answers=["", "", "2", ""], secret_answers=[""]))
    assert rc == 0, capsys.readouterr().err
    assert list(bundle.xdg.rglob(".*.tmp")) == []


# --- 自機のみの対話で Basic を選べる --------------------------------------------------------------------

def test_a_local_basic_made_by_the_questions_lets_enable_write_go_through(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch, enable_web=False)
    io = ScriptIO(answers=["2", "", "", "2", ""], secret_answers=[""])
    result = interactive_enable(bundle.root, "web", io=io, setup_factory=lambda _root: bundle.setup,
                                probe_runner=lambda _f: (0, ""), candidates=lambda: CANDIDATES)
    assert result.rc == 0, io.err
    assert io.asked == ["この設定で有効化します。[1] このまま [2] 変更する", "port", SCOPE_Q, AUTH_Q, USER_Q]
    assert io.defaults[3] == (AUTH_Q, "1")
    assert toml_of(bundle)["auth"] == "basic" and "write" not in toml_of(bundle)
    assert run(bundle, True)[0].rc == 0
    assert toml_of(bundle)["write"] is True and entry(bundle)[0] == "active"


def test_a_local_web_without_auth_stays_the_default_and_cannot_write(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch, enable_web=False)
    io = ScriptIO(answers=["2", "", "", ""])
    result = interactive_enable(bundle.root, "web", io=io, setup_factory=lambda _root: bundle.setup,
                                probe_runner=lambda _f: (0, ""), candidates=lambda: CANDIDATES)
    assert result.rc == 0 and "auth" not in toml_of(bundle)
    result, _ = run(bundle, True)
    assert result.reason_code == "write_requires_auth"


def test_an_interactive_reconfigure_while_write_is_on_asks_only_the_basic_credentials(writing, capsys):
    bundle = writing
    keys = {p: (key_dir(bundle.root) / p).read_bytes() for p in (".ready", key_file_name(WRITE), key_file_name(READ))}
    io = ScriptIO(answers=["9000", "", "2", "ops"], secret_answers=[""])
    rc, _ = _reconf(bundle, WebOptions(), io=io)
    assert rc == 0, capsys.readouterr().err
    assert io.asked == ["port", SCOPE_Q, KEEP_BASIC_Q, USER_Q] and io.of("secret") == [PASSWORD_Q]
    assert AUTH_Q not in io.text()
    assert toml_of(bundle) == {**toml_of(bundle), "port": 9000, "auth": "basic", "user": "ops", "write": True}
    assert {p: (key_dir(bundle.root) / p).read_bytes() for p in keys} == keys
    assert entry(bundle)[0] == "active"


def test_an_interactive_reconfigure_while_write_is_on_keeps_the_password_when_asked_to(writing, capsys):
    bundle = writing
    password = (bundle.xdg / "secret" / "web.password").read_bytes()
    io = ScriptIO(answers=["9000", "", "1"])
    rc, _ = _reconf(bundle, WebOptions(), io=io)
    assert rc == 0, capsys.readouterr().err
    assert io.asked == ["port", SCOPE_Q, KEEP_BASIC_Q] and io.of("secret") == []
    assert (bundle.xdg / "secret" / "web.password").read_bytes() == password
    assert toml_of(bundle)["write"] is True and toml_of(bundle)["port"] == 9000


def test_a_local_reconfigure_without_write_asks_the_auth_with_the_current_one_as_default(tmp_path, monkeypatch, capsys):
    bundle = make(tmp_path, monkeypatch)
    io = ScriptIO(answers=["", "", "", "1"])
    rc, _ = _reconf(bundle, WebOptions(), io=io)
    assert rc == 0, capsys.readouterr().err
    assert io.asked == ["port", SCOPE_Q, AUTH_Q, KEEP_BASIC_Q]
    assert io.defaults[2] == (AUTH_Q, "2") and toml_of(bundle)["auth"] == "basic"


# --- 無効化の後の再有効化は、保持すべき write と書込みの鍵を落とさない ------------------------------------

def _disabled_while_writing(tmp_path, monkeypatch):
    bundle = make(tmp_path, monkeypatch)
    assert run(bundle, True)[0].rc == 0
    assert bundle.setup.disable("web").stage == "disabled"
    bundle.systemd.calls.clear()
    return bundle


def _reenable(bundle, mode, tmp_path):
    options, answers = {
        "keep": (WebOptions(), [""]),
        "change": (WebOptions(), ["2", "", "", "1"]),
        "ask": (WebOptions(ask=True), ["", "", "1"]),
        "non-interactive": (WebOptions(non_interactive=True, secret_action="keep",
                                       config_file=_config(tmp_path, 'port = 9000\nauth = "basic"\n')), []),
    }[mode]
    io = ScriptIO(answers=list(answers), tty=mode != "non-interactive")
    result = interactive_enable(bundle.root, "web", io=io, options=options, setup_factory=lambda _r: bundle.setup,
                                probe_runner=lambda _f: (0, ""), candidates=lambda: CANDIDATES)
    return result, io


@pytest.mark.parametrize("mode", ["keep", "change", "ask", "non-interactive"])
def test_enable_again_after_a_disable_keeps_write_and_the_write_key(tmp_path, monkeypatch, mode):
    bundle = _disabled_while_writing(tmp_path, monkeypatch)
    keys = {p: (key_dir(bundle.root) / p).read_bytes() for p in (key_file_name(WRITE), key_file_name(READ))}
    result, io = _reenable(bundle, mode, tmp_path)
    assert result.rc == 0, io.err
    assert toml_of(bundle)["write"] is True and toml_of(bundle)["auth"] == "basic"
    assert entry(bundle) == ("active", None) and enrolled_key_matches(key_dir(bundle.root), WRITE)
    assert {p: (key_dir(bundle.root) / p).read_bytes() for p in keys} == keys


@pytest.mark.parametrize("mode", ["keep", "change", "ask", "non-interactive"])
def test_enable_again_with_an_active_write_key_and_write_false_is_refused_unchanged(tmp_path, monkeypatch, mode):
    bundle = _disabled_while_writing(tmp_path, monkeypatch)
    path = bundle.xdg / "config" / "ops.toml"
    path.write_text(path.read_text().replace("write = true\n", ""))
    before = world(bundle)
    result, io = _reenable(bundle, mode, tmp_path)
    assert (result.rc, result.reason_code) == (1, "write_state_undecided"), io.err
    assert "write = true" in "".join(io.err) and "ops.toml" in "".join(io.err)
    assert io.asked == [] and io.of("secret") == []
    assert world(bundle) == before
    assert entry(bundle) == ("active", None)


def test_enable_again_with_write_true_but_no_active_write_key_is_refused_unchanged(tmp_path, monkeypatch):
    from agentic_fx.ops.keys import revoke
    bundle = _disabled_while_writing(tmp_path, monkeypatch)
    revoke(key_dir(bundle.root), WRITE)
    before = world(bundle)
    result, _ = _reenable(bundle, "keep", tmp_path)
    assert (result.rc, result.reason_code) == (1, "invalid_state")
    assert world(bundle) == before


def test_a_first_enable_without_a_key_dir_or_a_config_still_enables_read_only(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch, enable_web=False)
    options = WebOptions(non_interactive=True, secret_action="absent", config_file=_config(tmp_path, "port = 9000\n"))
    io = ScriptIO(tty=False)
    result = interactive_enable(bundle.root, "web", io=io, options=options, setup_factory=lambda _r: bundle.setup,
                                probe_runner=lambda _f: (0, ""), candidates=lambda: CANDIDATES)
    assert result.rc == 0, io.err
    assert "write" not in toml_of(bundle) and entry(bundle) is None


# --- 判定できないときの回復の案内と、--no-write の整合の回復 ----------------------------------------

def test_the_undecided_guidance_names_the_file_the_key_and_the_order(writing, tmp_path, capsys):
    bundle = writing
    _damage_live(bundle, "broken_toml")
    rc, _ = _reconf_to_9000(bundle, tmp_path)
    err = capsys.readouterr().err
    assert rc == 1
    for text in ("ops/web/config/ops.toml", "write = true", "write = false", "構文", "未知", "型",
                 "enable --write", "--no-write"):
        assert text in err, text
    assert err.index("enable --write") < err.index("--no-write")


@pytest.mark.parametrize("how", ["broken_toml", "unknown_key"])
def test_enable_write_or_no_write_with_an_unreadable_live_file_says_how_to_fix_it(writing, how):
    bundle = writing
    _damage_live(bundle, how)
    before = world(bundle)
    for target in (True, False):
        result, io = run(bundle, target, io=ScriptIO(answers=[]))
        assert result.rc == 1 and "ops.toml" in "".join(io.err) and "write = true" in "".join(io.err)
    assert world(bundle) == before


def test_no_write_revokes_a_still_live_write_key_even_when_the_file_already_says_read_only(writing):
    from agentic_fx.ops_setup.feature_config import write_web_config
    bundle = writing
    write_web_config(bundle.root, dict(BASIC))
    assert "write" not in toml_of(bundle) and entry(bundle) == ("active", None)
    result, _ = run(bundle, False)
    assert result.rc == 0
    assert entry(bundle) == ("revoked", None) and "write" not in toml_of(bundle)
    assert enrolled_key_matches(key_dir(bundle.root), READ)


def test_no_write_on_a_consistent_read_only_web_still_changes_nothing(tmp_path, monkeypatch):
    bundle = make(tmp_path, monkeypatch)
    before = world(bundle)
    result, _ = run(bundle, False, io=ScriptIO(answers=[]))
    assert result.rc == 0 and world(bundle) == before and bundle.systemd.calls[:0] == []


def test_disabling_a_feature_that_was_never_enabled_is_still_refused_while_a_switch_is_pending(tmp_path, monkeypatch):
    bundle = make(tmp_path, monkeypatch)
    _crash_at(bundle, "after_stage_write_stopped")
    before = world(bundle)
    with pytest.raises(InvalidState) as caught:
        bundle.setup.disable("notify")
    assert caught.value.reason == "write_switch_pending"
    assert world(bundle) == before
