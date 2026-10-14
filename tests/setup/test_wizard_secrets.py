from __future__ import annotations

import os
from pathlib import Path

from agentic_fx.config import load_settings_text
from agentic_fx.ops_setup.interactive import EnableResult
from agentic_fx.setup.wizard import ConsoleWizardIO, WizardIO, run_wizard
from agentic_fx.store.setup_record import read_setup_record

from tests.setup.wizard_support import (
    ScriptedWizardIO, fresh_responder, make_deps, make_root,
)

MARKS = ("SECRET-MARK-1", "SECRET-MARK-2", "SECRET-MARK-3")


def test_no_secret_value_reaches_any_output_record_file_or_child_process(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    root = make_root(tmp_path)
    env_file = root / ".env"
    env_file.write_text("MT5_BRIDGE_API_KEY=SECRET-MARK-2\nDISCORD_WEBHOOK_URL=\n")
    env_before = env_file.read_bytes()
    secret_dir = tmp_path / "ops-secret"

    def enable(root_, feature, *, lease=None, io=None, **kwargs):
        assert io is None
        secret_dir.mkdir(exist_ok=True)
        (secret_dir / feature).write_text("SECRET-MARK-3")  # what the real step keeps in its own 0600 file
        return EnableResult(0, "enabled", None)

    deps, calls, fetch, run = make_deps(tmp_path, environ={"FOO_API_KEY": "SECRET-MARK-1"}, enable=enable)
    io = ScriptedWizardIO(responder=fresh_responder(
        ("bridge 側で API キー", "1"), ("systemd のユーザーサービス", "y"), ("外から見る手段を今", "y"),
        ("有効にする手段", "bot, notify"), ("操作 API を有効", "y")))
    assert run_wizard(root, io, deps) == 0, io.text

    visible = "\n".join(io.out + io.asked)
    runs = repr(run.calls) + repr(run.kwargs)
    files = {}
    for current, _dirs, names in os.walk(root):
        for name in names:
            path = Path(current) / name
            if path != env_file:
                files[str(path)] = path.read_text(errors="ignore")
    for mark in MARKS:
        assert mark not in visible, mark
        assert mark not in runs, mark
        assert all(mark not in text for text in files.values()), mark
    # the bridge key is only sent to the bridge itself
    assert any(kwargs.get("headers", {}).get("X-Bridge-Api-Key") == "SECRET-MARK-2"
               for _url, kwargs in fetch.calls)
    # names (never values) may be shown
    assert "MT5_BRIDGE_API_KEY (あり)" in visible
    assert env_file.read_bytes() == env_before


def test_the_terminal_wrapper_has_no_way_to_ask_for_a_secret():
    assert not hasattr(ConsoleWizardIO, "ask_secret")
    assert not hasattr(WizardIO, "ask_secret")
    assert not hasattr(ScriptedWizardIO(), "ask_secret")
    assert set(n for n in vars(WizardIO) if not n.startswith("_")) == {"isatty", "ask", "say"}


def test_env_is_never_created_and_a_missing_key_is_reported_by_name_only(tmp_path):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(("bridge 側で API キー", "1")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert not (root / ".env").exists()
    assert "MT5_BRIDGE_API_KEY (なし)" in io.text
    assert (load_settings_text((root / "config" / "settings.yaml").read_text()).service.secret_env_allowlist
            == [])


def test_a_bridge_key_in_the_env_file_is_used_for_the_check_and_seen_as_present(tmp_path):
    root = make_root(tmp_path)
    (root / ".env").write_text("MT5_BRIDGE_API_KEY=SECRET-MARK-2\n")
    deps, calls, fetch, run = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(("bridge 側で API キー", "1")))
    assert run_wizard(root, io, deps) == 0, io.text
    health = [kwargs for url, kwargs in fetch.calls if url.endswith("/health")]
    assert health and health[0]["headers"] == {"X-Bridge-Api-Key": "SECRET-MARK-2"}
    assert "MT5_BRIDGE_API_KEY (あり)" in io.text
    assert "SECRET-MARK-2" not in io.text


def test_risk_settings_and_the_kill_switch_are_never_asked_or_changed(tmp_path):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder())
    assert run_wizard(root, io, deps) == 0
    example = (root / "config" / "settings.yaml.example").read_text().splitlines()
    actual = (root / "config" / "settings.yaml").read_text().splitlines()
    risk_start = next(i for i, line in enumerate(example) if line.startswith("risk:"))
    risk_end = next(i for i, line in enumerate(example) if line.startswith("runner:"))
    assert example[risk_start:risk_end] == actual[risk_start:risk_end]
    assert not any("risk" in prompt or "kill" in prompt for prompt in io.asked)


def _api_line_change(root):
    example = (root / "config" / "settings.yaml.example").read_text().splitlines()
    actual = (root / "config" / "settings.yaml").read_text().splitlines()
    return [(e, a) for e, a in zip(example, actual) if e != a and a.lstrip().startswith("enabled:")]


def test_enabling_the_operation_api_changes_one_line_and_creates_no_key(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    root = make_root(tmp_path)
    example_path = root / "config" / "settings.yaml.example"
    example_text = example_path.read_text()
    assert "  enabled: true" in example_text
    example_path.write_text(example_text.replace("  enabled: true", "  enabled: false", 1))
    deps, *_ = make_deps(tmp_path)
    assert run_wizard(root, ScriptedWizardIO(responder=fresh_responder()), deps) == 0
    changes = _api_line_change(root)
    assert len(changes) == 1 and "true" in changes[0][1]
    assert not (home / ".config").exists()
    assert not (root / "data" / "run").exists()


def test_disabling_the_operation_api_keeps_the_line_and_is_recorded_and_flagged_once_the_unit_exists(tmp_path):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(("操作 API を有効", "n"), ("systemd のユーザーサービス", "y")))
    assert run_wizard(root, io, deps) == 0, io.text
    changes = _api_line_change(root)
    assert len(changes) == 1 and "false" in changes[0][1]
    data = read_setup_record(root).record.data
    assert data["api"]["enabled"] is False and data["unit"] == "installed"
    assert "操作 API が無効です。settings.yaml の api.enabled を true にして再起動してください" in io.text
    assert "`afx ctl` と、bot / web / notify は使えません" in io.text
