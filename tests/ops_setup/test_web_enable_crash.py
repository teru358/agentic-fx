"""enable の前処理 (0) → (a) → (b) → probe の各点で process が死んだときの live の状態と再実行 (実 process)。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentic_fx.ops_setup.feature_config import web_access_reason
from agentic_fx.ops_setup.web_setup import SecretPlan, WebOptions, apply_target
from test_web_enable_flow import FIXED_RNG, run_enable  # noqa: F401  (fixtures)
from web_support import KEEP_Q, LAN, ONE_CANDIDATE, ScriptIO

CHILD = Path(__file__).with_name("_web_crash_child.py")
OLD, NEW, EXTRA = "E" * 22, "N" * 22, "M" * 22
ALLOW = ["10.0.0.0/8"]
START_NONE = {"port": 8788, "bind": LAN, "allow": ALLOW, "auth": "none"}
START_BASIC = {"port": 8788, "bind": LAN, "allow": ALLOW, "auth": "basic"}


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    return tmp_path / "c" / "agentic-fx" / "ops" / "web"


@pytest.fixture
def root(tmp_path) -> Path:
    path = tmp_path / "instance"
    path.mkdir()
    return path


def _toml(tmp_path: Path, values: dict, name: str = "t.toml") -> str:
    lines = [f"port = {values['port']}", f'bind = "{values["bind"]}"',
             "allow = [" + ", ".join(f'"{a}"' for a in values["allow"]) + "]", f'auth = "{values["auth"]}"']
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
    return str(path)


def _password(tmp_path: Path, value: str, name: str) -> str:
    path = tmp_path / name
    path.write_text(value)
    path.chmod(0o600)
    return str(path)


def _crash(root: Path, point: str, config_file: str, action: str, password_file: str | None) -> int:
    spec = {"root": str(root), "point": point, "config_file": config_file, "secret_action": action}
    if password_file:
        spec["password_file"] = password_file
    done = subprocess.run([sys.executable, str(CHILD), json.dumps(spec)], capture_output=True, text=True,
                          timeout=120, env=dict(os.environ))
    assert done.stdout == "" or "password" not in done.stdout
    return done.returncode


def _reason(root: Path) -> str | None:
    return web_access_reason(root, interfaces=lambda: [LAN])


def _tmp_left(xdg: Path) -> list[str]:
    return sorted(p.name for d in ("secret", "config") for p in (xdg / d).iterdir() if p.name.endswith(".tmp"))


def _password_state(xdg: Path) -> str | None:
    path = xdg / "secret" / "web.password"
    return path.read_text() if path.exists() else None


# 点 → (live の reason)。none → basic の途中: 新しい password と古い none の config の組は secret_unexpected。
NONE_TO_BASIC = [
    ("cleanup_scanned", None), ("cleanup_done", None),
    ("secret_tmp_created", "secret_invalid"), ("secret_tmp_synced", "secret_invalid"),
    ("secret_renamed", "secret_unexpected"), ("secret_dir_synced", "secret_unexpected"),
    ("secret_applied", "secret_unexpected"),
    ("config_tmp_created", "secret_unexpected"), ("config_tmp_synced", "secret_unexpected"),
    ("config_renamed", None), ("config_dir_synced", None), ("config_applied", None),
    ("probe_succeeded", None),
]


@pytest.mark.parametrize("point, reason", NONE_TO_BASIC)
def test_none_to_basic_state_after_a_death_and_the_rerun_with_the_same_inputs(root, xdg, tmp_path, point, reason):
    apply_target(root, START_NONE, SecretPlan("absent"))
    config = _toml(tmp_path, START_BASIC)
    pw = _password(tmp_path, NEW, "pw")
    assert _crash(root, point, config, "install", pw) == 70
    assert _reason(root) == reason
    if point in ("secret_tmp_created", "secret_tmp_synced", "config_tmp_created", "config_tmp_synced"):
        assert _tmp_left(xdg), "死んだ process は一時 file を残す"
    if point in ("secret_tmp_created", "secret_tmp_synced", "cleanup_scanned", "cleanup_done"):
        assert _password_state(xdg) is None
    options = WebOptions(non_interactive=True, config_file=config, secret_action="install", password_file=pw)
    run = run_enable(root, ScriptIO(tty=False), options, candidates=ONE_CANDIDATE)
    assert run.rc == 0 and run.setup.enabled == ["web"]
    assert _reason(root) is None and _tmp_left(xdg) == [] and _password_state(xdg) == NEW


@pytest.mark.parametrize("point", ["secret_renamed", "secret_dir_synced", "secret_applied"])
def test_a_tty_rerun_after_none_to_basic_asks_the_auth_again_and_never_reshows_a_password(root, xdg, tmp_path, point):
    apply_target(root, START_NONE, SecretPlan("absent"))
    assert _crash(root, point, _toml(tmp_path, START_BASIC), "install", _password(tmp_path, NEW, "pw")) == 70
    assert _reason(root) == "secret_unexpected"
    io = ScriptIO(answers=["2", ""])      # パスワード認証を選び、既存 password を再利用 (表示しない)
    run = run_enable(root, io, candidates=ONE_CANDIDATE)
    assert run.rc == 0 and _password_state(xdg) == NEW
    assert NEW not in io.text()
    assert _reason(root) is None


@pytest.mark.parametrize("point", ["secret_renamed", "secret_dir_synced", "secret_applied"])
def test_a_tty_rerun_after_none_to_basic_can_instead_keep_none_with_an_explicit_remove(root, xdg, tmp_path, point):
    apply_target(root, START_NONE, SecretPlan("absent"))
    assert _crash(root, point, _toml(tmp_path, START_BASIC), "install", _password(tmp_path, NEW, "pw")) == 70
    io = ScriptIO(answers=["1", "", "1"])  # 許可 IP だけ (none)・allow は既存値 → 削除して認証なしにする
    run = run_enable(root, io, candidates=ONE_CANDIDATE)
    assert run.rc == 0 and _password_state(xdg) is None and _reason(root) is None


BASIC_TO_NONE = [
    ("cleanup_done", None), ("secret_unlinked", "secret_missing"), ("secret_dir_synced", "secret_missing"),
    ("secret_applied", "secret_missing"), ("config_tmp_created", "secret_missing"),
    ("config_tmp_synced", "secret_missing"), ("config_renamed", None), ("config_applied", None),
    ("probe_succeeded", None),
]


@pytest.mark.parametrize("point, reason", BASIC_TO_NONE)
def test_basic_to_none_state_after_a_death_and_the_rerun_that_matches_the_live_secret(root, xdg, tmp_path, point, reason):
    apply_target(root, START_BASIC, SecretPlan("install", OLD))
    config = _toml(tmp_path, START_NONE)
    assert _crash(root, point, config, "remove", None) == 70
    assert _reason(root) == reason
    already_gone = _password_state(xdg) is None
    action = "absent" if already_gone else "remove"
    if already_gone:
        refused = run_enable(root, ScriptIO(tty=False),
                             WebOptions(non_interactive=True, config_file=config, secret_action="remove"),
                             candidates=ONE_CANDIDATE)
        assert (refused.rc, refused.reason) == (2, "setup_input_required")
    run = run_enable(root, ScriptIO(tty=False),
                     WebOptions(non_interactive=True, config_file=config, secret_action=action),
                     candidates=ONE_CANDIDATE)
    assert run.rc == 0 and _reason(root) is None and _tmp_left(xdg) == [] and _password_state(xdg) is None


@pytest.mark.parametrize("point", ["secret_unlinked", "secret_dir_synced", "secret_applied", "config_tmp_synced"])
def test_a_tty_rerun_after_basic_to_none_asks_the_auth_and_secret_again(root, xdg, tmp_path, point):
    apply_target(root, START_BASIC, SecretPlan("install", OLD))
    assert _crash(root, point, _toml(tmp_path, START_NONE), "remove", None) == 70
    assert _reason(root) == "secret_missing"
    io = ScriptIO(answers=["1", ""])      # 許可 IP だけ、allow は既存値 (既定) を使う
    run = run_enable(root, io, candidates=ONE_CANDIDATE)
    assert run.rc == 0 and _reason(root) is None and _password_state(xdg) is None


BASIC_TO_BASIC = [
    ("secret_tmp_created", "secret_invalid"), ("secret_renamed", None), ("secret_dir_synced", None),
    ("secret_applied", None), ("config_tmp_created", None), ("config_renamed", None), ("probe_succeeded", None),
]


@pytest.mark.parametrize("point, reason", BASIC_TO_BASIC)
def test_password_replacement_state_after_a_death_and_the_one_question_rerun(root, xdg, tmp_path, point, reason):
    apply_target(root, START_BASIC, SecretPlan("install", OLD))
    config = _toml(tmp_path, {**START_BASIC, "port": 9000})
    assert _crash(root, point, config, "install", _password(tmp_path, EXTRA, "pw")) == 70
    assert _reason(root) == reason
    io = ScriptIO(answers=[""])
    run = run_enable(root, io, candidates=ONE_CANDIDATE)
    # 一時 file だけが残る点も、(0) が除去してから完全な設定として要約と 1 問に進む
    assert run.rc == 0 and io.asked == [KEEP_Q] and _tmp_left(xdg) == []
    assert _password_state(xdg) in (OLD, EXTRA)
    assert _reason(root) is None
    assert OLD not in io.text() and EXTRA not in io.text()


def test_after_the_config_is_written_and_before_the_stage_a_non_tty_rerun_prints_the_summary_and_goes_on(
        root, xdg, tmp_path):
    apply_target(root, START_NONE, SecretPlan("absent"))
    assert _crash(root, "probe_succeeded", _toml(tmp_path, START_BASIC), "install",
                  _password(tmp_path, NEW, "pw")) == 70
    io = ScriptIO(tty=False)
    run = run_enable(root, io, candidates=ONE_CANDIDATE)
    assert run.rc == 0 and run.setup.enabled == ["web"] and io.asked == []
    assert io.err == [f"web: 待受 {LAN}:8788 / 公開 許可範囲 1 件 / 認証 basic"]


def test_the_death_never_leaves_a_partial_live_file(root, xdg, tmp_path):
    apply_target(root, START_NONE, SecretPlan("absent"))
    before = (xdg / "config" / "ops.toml").read_bytes()
    for point in ("config_tmp_created", "config_tmp_synced"):
        assert _crash(root, point, _toml(tmp_path, {**START_NONE, "port": 9100}), "absent", None) == 70
        assert (xdg / "config" / "ops.toml").read_bytes() == before
    assert _crash(root, "config_renamed", _toml(tmp_path, {**START_NONE, "port": 9100}), "absent", None) == 70
    assert b"port = 9100" in (xdg / "config" / "ops.toml").read_bytes()


def test_a_same_named_temporary_symlink_stops_every_flow_before_anything_else(root, xdg, tmp_path):
    apply_target(root, START_NONE, SecretPlan("absent"))
    (xdg / "secret" / (".web.password." + "a" * 32 + ".tmp")).symlink_to("/nonexistent")
    before = (xdg / "config" / "ops.toml").read_bytes()
    for options in (WebOptions(), WebOptions(ask=True),
                    WebOptions(non_interactive=True, config_file=_toml(tmp_path, START_NONE), secret_action="absent")):
        io = ScriptIO(answers=[""] * 4, tty=options.non_interactive is False)
        run = run_enable(root, io, options, candidates=ONE_CANDIDATE)
        assert (run.rc, run.reason) == (1, "secret_invalid")
        assert io.asked == [] and run.probes == []
    (xdg / "config" / (".ops.toml." + "b" * 32 + ".tmp")).symlink_to("/nonexistent")
    (xdg / "secret" / (".web.password." + "a" * 32 + ".tmp")).unlink()
    run = run_enable(root, ScriptIO(answers=[""]), candidates=ONE_CANDIDATE)
    assert (run.rc, run.reason) == (1, "config_dir_invalid")
    assert (xdg / "config" / "ops.toml").read_bytes() == before
