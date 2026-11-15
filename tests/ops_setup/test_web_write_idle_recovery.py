"""有効でない web (未 enable / disabled) の write 回復: 記録・再開・手動操作・入口・I/O 失敗。

systemd と鍵 dir は fake / tmp を使い、本番の unit・`~/.config/agentic-fx/`・`data/` には触れない。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentic_fx.ops.keys import enroll, enrolled_key_matches, key_dir, rotate
from agentic_fx.ops_setup import units
from agentic_fx.ops_setup.feature_config import write_web_config
from agentic_fx.ops_setup.interactive import interactive_enable
from agentic_fx.ops_setup.stages import (InvalidState, WRITE_SWITCH_GUIDANCE, read_marker, read_stage,
                                         read_write_recovery)
from agentic_fx.ops_setup.write_key import Consistent, check_write_key_consistency
from agentic_fx.ops_setup.web_setup import WebOptions
from test_web_write_switch import (BASIC, READ, WRITE, Crash, _crash_at, _idle_base, _reconf_to_9000,
                                   _revoke_read_key, afx_calls, apply_state, entry, run, toml_of, world)
from web_support import CANDIDATES, ScriptIO

BEFORE_STOP = "active"
COMBOS = [("disabled", "active", "ro_key_active", False), ("never", "active", "rw_unregistered", True),
          ("disabled", "inactive", "rw_unregistered", False), ("never", "inactive", "ro_key_active", True)]


def record_path(bundle) -> Path:
    return bundle.root / "data" / "ops" / "web.write-recovery.json"


def record_of(bundle):
    path = record_path(bundle)
    return json.loads(path.read_text()) if path.exists() else None


def put_record(bundle, value="active", *, raw: bytes | None = None, mode=0o600):
    path = record_path(bundle)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw if raw is not None else
                     json.dumps({"version": 1, "afx_before_stop": value}).encode() + b"\n")
    path.chmod(mode)


def set_afx(bundle, state):
    bundle.systemd.afx_state = state
    if state in ("active", "activating", "reloading"):
        bundle.systemd.active.add(bundle.paths.afx_unit)
    else:
        bundle.systemd.active.discard(bundle.paths.afx_unit)


def prepared(tmp_path, monkeypatch, kind="disabled", state="ro_key_active", afx="active"):
    bundle = _idle_base(kind)(tmp_path, monkeypatch)
    apply_state(bundle, state)
    set_afx(bundle, afx)
    bundle.systemd.calls.clear()
    return bundle


def quiet(bundle):
    bundle.setup.hook = lambda _name: None


def consistent(bundle, target):
    return check_write_key_consistency(bundle.root, mode="idle") == Consistent(target)


def starts(bundle):
    return afx_calls(bundle, "start")


# --- 記録の保存・削除の順と、完了後の姿 ---------------------------------------------------------------------

def test_the_record_is_saved_before_anything_is_touched_and_removed_last(tmp_path, monkeypatch):
    bundle = prepared(tmp_path, monkeypatch)
    stage_before = bundle.stage_file.read_bytes()
    names: list[str] = []
    seen: dict = {}

    def hook(name):
        names.append(name)
        if name == "after_recovery_record":
            seen.update(record=record_of(bundle), marker=read_marker(bundle.root),
                        calls=[c[0] for c in bundle.systemd.calls if c[0] in ("stop", "start")],
                        raw=record_path(bundle).read_bytes(), mode=record_path(bundle).stat().st_mode & 0o777)

    result, io = run(bundle, False, hook=hook)
    assert result.rc == 0 and len(io.asked) == 1
    assert seen["record"] == {"version": 1, "afx_before_stop": "active"} and seen["mode"] == 0o600
    assert seen["marker"] is None and seen["calls"] == []
    order = ["before_recovery_record", "after_recovery_record", "before_marker", "before_stop", "before_keys",
             "before_config", "before_unmark", "before_recovery_start", "after_recovery_start",
             "before_recovery_remove", "after_recovery_remove"]
    assert [n for n in names if n in order] == order and names[-1] == "after_recovery_remove"
    assert record_of(bundle) is None and read_marker(bundle.root) is None and consistent(bundle, False)
    assert len(starts(bundle)) == 1 and bundle.systemd.afx_state == "active"
    assert bundle.stage_file.read_bytes() == stage_before
    result, _ = run(bundle, False)
    assert (result.rc, result.reason_code) == (1, "not_enabled")


@pytest.mark.parametrize("kind, afx, state, target", COMBOS)
def test_an_interruption_at_any_hook_converges_on_rerun(tmp_path_factory, monkeypatch, kind, afx, state, target):
    probe = prepared(tmp_path_factory.mktemp("probe"), monkeypatch, kind, state, afx)
    names: list[str] = []
    assert run(probe, target, hook=names.append)[0].rc == 0
    assert {"after_stop", "after_config", "after_unmark"} <= set(names)
    assert ("after_recovery_start" in names) is (afx == "active")
    for index in range(len(names)):
        bundle = prepared(tmp_path_factory.mktemp(f"crash{index}"), monkeypatch, kind, state, afx)
        count = 0

        def hook(point, index=index):
            nonlocal count
            hit = count == index
            count += 1
            if hit:
                raise Crash(point)

        with pytest.raises(Crash):
            run(bundle, target, hook=hook)
        quiet(bundle)
        settled = record_of(bundle) is None and consistent(bundle, target)
        result, _ = run(bundle, target)
        if not settled:
            assert result.rc == 0, (index, names[index], result)
        else:
            assert (result.rc, result.reason_code) == (1, "not_enabled"), (index, names[index])
        assert record_of(bundle) is None and read_marker(bundle.root) is None, (index, names[index])
        assert consistent(bundle, target), (index, names[index])
        assert bundle.systemd.afx_state == afx, (index, names[index])
        if afx == "inactive":
            assert starts(bundle) == [], (index, names[index])
        assert run(bundle, target)[0].reason_code == "not_enabled"


# --- 保存前の拒否は何も残さない -----------------------------------------------------------------------------

def _break_target_config(bundle):
    write_web_config(bundle.root, {"port": 8788})


def _foreign_marker(bundle):
    bundle.setup._place_marker("bot")


def _corrupt_marker(bundle):
    from agentic_fx.store.enroll_marker import enroll_marker_path
    path = enroll_marker_path(bundle.root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")


@pytest.mark.parametrize("state, target, prep, reason", [
    ("undecided", False, None, "config_invalid"),
    ("rw_mismatch", True, None, "principal_key_mismatch"),
    ("ro_key_active", True, _break_target_config, "write_requires_auth"),
    ("ro_key_active", False, _revoke_read_key, "principal_key_mismatch"),
    ("ro_key_active", False, _foreign_marker, "marker_foreign"),
    ("ro_key_active", False, _corrupt_marker, "marker_corrupt"),
])
def test_a_refusal_before_the_record_leaves_no_record_and_the_world_unchanged(
        tmp_path, monkeypatch, state, target, prep, reason):
    bundle = prepared(tmp_path, monkeypatch, "disabled", state)
    if prep is not None:
        prep(bundle)
    bundle.systemd.calls.clear()
    before = world(bundle)
    result, io = run(bundle, target)
    assert (result.rc, result.reason_code) == (1, reason)
    assert world(bundle) == before and record_of(bundle) is None and io.asked == []
    assert [c for c in bundle.systemd.calls if c[0] in ("stop", "start")] == []


def test_a_declined_confirmation_and_a_non_tty_leave_no_record(tmp_path, monkeypatch):
    bundle = prepared(tmp_path, monkeypatch)
    before = world(bundle)
    result, _ = run(bundle, False, io=ScriptIO(answers=["no"]))
    assert (result.rc, result.reason_code) == (1, "setup_aborted")
    result, _ = run(bundle, False, io=ScriptIO(answers=[], tty=False))
    assert (result.rc, result.reason_code) == (2, "setup_input_required")
    assert world(bundle) == before and record_of(bundle) is None and afx_calls(bundle, "stop") == []


def test_a_never_enabled_web_does_not_require_the_read_key_for_no_write(tmp_path, monkeypatch):
    bundle = prepared(tmp_path, monkeypatch, "never", "ro_key_active")
    assert entry(bundle, READ) is None
    result, _ = run(bundle, False)
    assert result.rc == 0 and consistent(bundle, False) and record_of(bundle) is None
    (tmp_path / "d").mkdir()
    disabled = prepared(tmp_path / "d", monkeypatch, "disabled", "ro_key_active")
    _revoke_read_key(disabled)
    assert run(disabled, False)[0].reason_code == "principal_key_mismatch"


def test_an_absent_config_is_allowed_for_no_write_and_stays_absent(tmp_path, monkeypatch):
    import shutil
    bundle = prepared(tmp_path, monkeypatch, "disabled", "ro_key_active")
    shutil.rmtree(bundle.xdg / "config")
    result, _ = run(bundle, False)
    assert result.rc == 0 and entry(bundle) == ("revoked", None) and not (bundle.xdg / "config").exists()
    assert record_of(bundle) is None


# --- confirm と状態の分類 -----------------------------------------------------------------------------------

def test_confirm_is_asked_once_for_a_new_recovery_and_never_for_a_stopped_afx(tmp_path, monkeypatch):
    bundle = prepared(tmp_path, monkeypatch)
    result, io = run(bundle, False)
    assert result.rc == 0 and len(io.asked) == 1
    (tmp_path / "s").mkdir()
    bundle = prepared(tmp_path / "s", monkeypatch, "disabled", "ro_key_active")
    _crash_at(bundle, "after_stop", False)
    assert bundle.systemd.afx_state == "inactive"
    result, io = run(bundle, False, io=ScriptIO(answers=[], tty=False))
    assert result.rc == 0 and io.asked == []


def test_a_resumed_recovery_with_a_running_afx_asks_again_and_a_non_tty_cannot_answer(tmp_path, monkeypatch):
    bundle = prepared(tmp_path, monkeypatch)
    _crash_at(bundle, "before_stop", False)
    assert record_of(bundle) is not None and bundle.systemd.afx_state == "active"
    before = world(bundle)
    result, _ = run(bundle, False, io=ScriptIO(answers=[], tty=False))
    assert (result.rc, result.reason_code) == (2, "setup_input_required")
    result, _ = run(bundle, False, io=ScriptIO(answers=["no"]))
    assert (result.rc, result.reason_code) == (1, "setup_aborted")
    assert world(bundle) == before and afx_calls(bundle, "stop") == []
    result, io = run(bundle, False)
    assert result.rc == 0 and len(io.asked) == 1 and record_of(bundle) is None


@pytest.mark.parametrize("value, started", [("active", True), ("activating", True), ("reloading", True),
                                            ("inactive", False), ("failed", False), ("unknown", False),
                                            ("deactivating", False), ("refreshing", False), ("maintenance", False)])
def test_only_a_running_state_before_the_stop_makes_the_recovery_start_afx(tmp_path, monkeypatch, value, started):
    bundle = prepared(tmp_path, monkeypatch, "disabled", "ro_key_active", "inactive")
    put_record(bundle, value)
    result, _ = run(bundle, False)
    assert result.rc == 0, result
    assert bool(starts(bundle)) is started and record_of(bundle) is None


def test_a_start_that_does_not_end_in_a_running_state_keeps_the_record(tmp_path, monkeypatch):
    bundle = prepared(tmp_path, monkeypatch)
    bundle.systemd.on_call["show"] = (lambda s: setattr(s, "afx_state", "inactive")
                                      if any(c[0] == "start" for c in s.calls) else None)
    result, _ = run(bundle, False)
    assert (result.rc, result.reason_code) == (1, "start")
    assert record_of(bundle) == {"version": 1, "afx_before_stop": "active"} and consistent(bundle, False)
    del bundle.systemd.on_call["show"]
    result, _ = run(bundle, False)
    assert result.rc == 0 and record_of(bundle) is None and bundle.systemd.afx_state == "active"


# --- 手動操作と自動再起動 -----------------------------------------------------------------------------------

def test_an_afx_that_was_running_is_started_again_after_a_manual_stop_while_the_record_remains(tmp_path, monkeypatch):
    bundle = prepared(tmp_path, monkeypatch)
    _crash_at(bundle, "after_recovery_start", False)
    assert record_of(bundle) is not None and bundle.systemd.afx_state == "active"
    set_afx(bundle, "inactive")
    bundle.systemd.calls.clear()
    result, _ = run(bundle, False)
    assert result.rc == 0 and len(starts(bundle)) == 1 and afx_calls(bundle, "stop") == []
    assert bundle.systemd.afx_state == "active" and record_of(bundle) is None


@pytest.mark.parametrize("point", ["after_keys", "after_config"])
def test_a_manual_start_after_a_stopped_origin_is_not_stopped_and_the_guidance_names_the_unit(
        tmp_path, monkeypatch, point):
    bundle = prepared(tmp_path, monkeypatch, "disabled", "ro_key_active", "inactive")
    _crash_at(bundle, point, False)
    assert read_marker(bundle.root) is not None
    set_afx(bundle, "active")
    bundle.systemd.calls.clear()
    before = world(bundle)
    result, io = run(bundle, False)
    assert (result.rc, result.reason_code) == (1, "write_recovery_afx_running")
    unit = bundle.paths.afx_unit
    text = "".join(io.err)
    assert unit != "afx.service" and f"systemctl --user stop {unit}" in text
    assert f"systemctl --user is-active {unit}" in text and "inactive" in text
    assert afx_calls(bundle, "stop") == [] and world(bundle) == before and bundle.systemd.afx_state == "active"
    set_afx(bundle, "inactive")
    result, _ = run(bundle, False)
    assert result.rc == 0 and read_marker(bundle.root) is None and record_of(bundle) is None
    assert consistent(bundle, False) and starts(bundle) == []


@pytest.mark.parametrize("state, target", [("ro_key_active", False), ("rw_unregistered", True)])
def test_the_guidance_converges_for_both_targets(tmp_path, monkeypatch, state, target):
    bundle = prepared(tmp_path, monkeypatch, "disabled", state, "inactive")
    _crash_at(bundle, "after_stop", target)
    set_afx(bundle, "active")
    assert run(bundle, target)[0].reason_code == "write_recovery_afx_running"
    set_afx(bundle, "inactive")
    assert run(bundle, target)[0].rc == 0 and consistent(bundle, target)
    assert bundle.systemd.afx_state == "inactive"


def test_a_manual_start_with_nothing_left_to_align_is_neither_stopped_nor_started(tmp_path, monkeypatch):
    bundle = prepared(tmp_path, monkeypatch, "disabled", "ro_key_active", "inactive")
    _crash_at(bundle, "after_unmark", False)
    assert read_marker(bundle.root) is None and consistent(bundle, False) and record_of(bundle) is not None
    set_afx(bundle, "active")
    bundle.systemd.calls.clear()
    result, _ = run(bundle, False)
    assert result.rc == 0 and record_of(bundle) is None and bundle.systemd.afx_state == "active"
    assert afx_calls(bundle, "stop") == [] and starts(bundle) == []


# --- 再照合の不整合 -----------------------------------------------------------------------------------------

def _undo_the_config(bundle, hook_name):
    def hook(name):
        if name == hook_name:
            write_web_config(bundle.root, {**BASIC, "write": True})
    return hook


def test_a_recheck_that_finds_an_inconsistency_keeps_the_record_and_does_not_retry_in_the_run(tmp_path, monkeypatch):
    bundle = prepared(tmp_path, monkeypatch, "disabled", "ro_key_active", "inactive")
    names: list[str] = []
    undo = _undo_the_config(bundle, "after_config")
    result, _ = run(bundle, False, hook=lambda name: (names.append(name), undo(name)))
    assert (result.rc, result.reason_code) == (1, "invalid_state")
    assert names.count("before_keys") == 1 and record_of(bundle) is not None
    set_afx(bundle, "active")
    bundle.systemd.calls.clear()
    assert run(bundle, False)[0].reason_code == "write_recovery_afx_running"
    assert afx_calls(bundle, "stop") == []
    set_afx(bundle, "inactive")
    quiet(bundle)
    result, _ = run(bundle, False)
    assert result.rc == 0 and consistent(bundle, False) and record_of(bundle) is None


def test_a_recheck_that_finds_the_other_target_is_refused_as_a_write_change(tmp_path, monkeypatch):
    bundle = prepared(tmp_path, monkeypatch, "disabled", "ro_key_active", "inactive")

    def hook(name):
        if name == "after_config":
            rotate(key_dir(bundle.root), WRITE)
            write_web_config(bundle.root, {**BASIC, "write": True})

    result, _ = run(bundle, False, hook=hook)
    assert (result.rc, result.reason_code) == (1, "write_change_refused")
    assert record_of(bundle) is not None and read_marker(bundle.root) is not None


# --- 目標の変更 ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("state, first", [("ro_key_active", True), ("rw_unregistered", False)])
def test_a_rerun_with_the_other_target_converges_and_keeps_the_first_record(tmp_path, monkeypatch, state, first):
    bundle = prepared(tmp_path, monkeypatch, "disabled", state)
    _crash_at(bundle, "after_keys", first)
    raw = record_path(bundle).read_bytes()
    names: list[str] = []
    result, _ = run(bundle, not first, hook=names.append)
    assert result.rc == 0 and consistent(bundle, not first)
    assert "before_recovery_record" not in names and record_of(bundle) is None
    assert raw == b'{"afx_before_stop": "active", "version": 1}\n'


# --- 実行と再実行の間の直接の鍵操作 -------------------------------------------------------------------------

@pytest.mark.parametrize("target", [True, False])
def test_a_direct_key_change_between_runs_is_reconciled_against_the_real_state(tmp_path, monkeypatch, target):
    bundle = prepared(tmp_path, monkeypatch, "disabled", "rw_unregistered")
    _crash_at(bundle, "after_stop", target)
    raw = record_path(bundle).read_bytes()
    enroll(key_dir(bundle.root), WRITE)
    assert record_path(bundle).read_bytes() == raw
    result, _ = run(bundle, target)
    assert result.rc == 0 and consistent(bundle, target) and record_of(bundle) is None
    assert entry(bundle)[0] == ("active" if target else "revoked")


# --- 入口: pending と壊れた記録 -----------------------------------------------------------------------------

def _pending(tmp_path, monkeypatch, how):
    bundle = prepared(tmp_path, monkeypatch)
    if how == "recorded":
        _crash_at(bundle, "after_recovery_record", False)
        assert read_marker(bundle.root) is None
    elif how == "marked":
        _crash_at(bundle, "after_stop", False)
        assert read_marker(bundle.root) is not None
    else:
        put_record(bundle, raw=b"{not json")
    bundle.systemd.calls.clear()
    return bundle


@pytest.mark.parametrize("how, reason", [("recorded", "write_switch_pending"), ("marked", "write_switch_pending"),
                                         ("broken", "write_recovery_invalid")])
def test_every_normal_entry_is_refused_before_any_side_effect(tmp_path, monkeypatch, capsys, how, reason):
    bundle = _pending(tmp_path, monkeypatch, how)
    before = world(bundle)
    for feature, options in (("bot", WebOptions()), ("notify", WebOptions()), ("web", WebOptions()),
                             ("web", WebOptions(ask=True))):
        probes: list[str] = []
        io = ScriptIO(answers=["x"] * 8, secret_answers=["x"] * 4)
        result = interactive_enable(bundle.root, feature, io=io, options=options,
                                    setup_factory=lambda _r: bundle.setup,
                                    probe_runner=lambda name: probes.append(name) or (0, ""),
                                    candidates=lambda: CANDIDATES)
        assert (result.rc, result.reason_code) == (1, reason), feature
        text = "".join(io.err)
        assert (WRITE_SWITCH_GUIDANCE in text) if how != "broken" else ("web.write-recovery.json" in text)
        assert io.asked == [] and io.of("secret") == [] and probes == []
    for feature in ("web", "bot", "notify"):
        with pytest.raises(InvalidState) as caught:
            bundle.setup.disable(feature)
        assert caught.value.reason == reason
        with pytest.raises(InvalidState) as caught:
            bundle.setup.enable(feature)
        assert caught.value.reason == reason
    rc, _ = _reconf_to_9000(bundle, tmp_path)
    assert rc == 1 and "not_enabled" in capsys.readouterr().err
    assert world(bundle) == before
    assert [c for c in bundle.systemd.calls if c[0] in ("stop", "start")] == []


@pytest.mark.parametrize("how", ["recorded", "marked"])
def test_an_explicit_write_flag_resumes_the_pending_recovery(tmp_path, monkeypatch, how):
    bundle = _pending(tmp_path, monkeypatch, how)
    result, _ = run(bundle, False)
    assert result.rc == 0 and consistent(bundle, False) and record_of(bundle) is None


def test_a_broken_record_refuses_the_write_flag_without_change(tmp_path, monkeypatch):
    bundle = _pending(tmp_path, monkeypatch, "broken")
    before = world(bundle)
    result, io = run(bundle, False)
    assert (result.rc, result.reason_code) == (1, "write_recovery_invalid")
    assert world(bundle) == before and "web.write-recovery.json" in "".join(io.err)
    assert [c for c in bundle.systemd.calls if c[0] in ("stop", "start")] == []


def test_a_record_beside_an_enabled_or_unfinished_web_stage_is_refused_unchanged(tmp_path, monkeypatch):
    from test_web_write_switch import make
    bundle = make(tmp_path, monkeypatch)
    put_record(bundle)
    before = world(bundle)
    result, _ = run(bundle, True)
    assert (result.rc, result.reason_code) == (1, "invalid_state") and world(bundle) == before


# --- 記録の厳密な読取り -------------------------------------------------------------------------------------

VALID = b'{"version": 1, "afx_before_stop": "active"}'
BAD = [b"", b"{", b"[]", b'"x"', b"null", b'{"version": 1}', b'{"afx_before_stop": "active"}',
       b'{"version": 1, "afx_before_stop": "active", "extra": 1}',
       b'{"version": 2, "afx_before_stop": "active"}', b'{"version": true, "afx_before_stop": "active"}',
       b'{"version": 1.0, "afx_before_stop": "active"}', b'{"version": "1", "afx_before_stop": "active"}',
       b'{"version": 1, "afx_before_stop": "nope"}', b'{"version": 1, "afx_before_stop": 1}',
       b'{"version": 1, "afx_before_stop": ["active"]}',
       b'{"version": 1, "version": 1, "afx_before_stop": "active"}',
       b'{"version": 1, "afx_before_stop": "active", "afx_before_stop": "active"}',
       b"[" * 5000, b"\xff\xfe", VALID + b" " * 4097]


@pytest.mark.parametrize("raw", BAD)
def test_a_malformed_record_is_invalid(tmp_path, monkeypatch, raw):
    bundle = prepared(tmp_path, monkeypatch)
    put_record(bundle, raw=raw)
    with pytest.raises(InvalidState) as caught:
        read_write_recovery(bundle.root)
    assert caught.value.reason == "write_recovery_invalid" and "web.write-recovery.json" in caught.value.detail


@pytest.mark.parametrize("value", ["active", "reloading", "refreshing", "inactive", "failed", "unknown",
                                   "activating", "deactivating", "maintenance"])
def test_every_known_active_state_is_accepted(tmp_path, monkeypatch, value):
    bundle = prepared(tmp_path, monkeypatch)
    put_record(bundle, value)
    assert read_write_recovery(bundle.root) == {"version": 1, "afx_before_stop": value}


def test_the_size_limit_is_exactly_4096_bytes(tmp_path, monkeypatch):
    bundle = prepared(tmp_path, monkeypatch)
    put_record(bundle, raw=VALID + b" " * (4096 - len(VALID)))
    assert read_write_recovery(bundle.root)["version"] == 1
    put_record(bundle, raw=VALID + b" " * (4097 - len(VALID)))
    with pytest.raises(InvalidState):
        read_write_recovery(bundle.root)


def test_the_file_kind_owner_mode_and_symlink_are_checked(tmp_path, monkeypatch):
    bundle = prepared(tmp_path, monkeypatch)
    assert read_write_recovery(bundle.root) is None
    path = record_path(bundle)
    put_record(bundle, mode=0o644)
    with pytest.raises(InvalidState):
        read_write_recovery(bundle.root)
    path.unlink()
    target = tmp_path / "elsewhere.json"
    target.write_bytes(VALID)
    target.chmod(0o600)
    path.symlink_to(target)
    with pytest.raises(InvalidState):
        read_write_recovery(bundle.root)
    path.unlink()
    path.mkdir()
    with pytest.raises(InvalidState):
        read_write_recovery(bundle.root)
    path.rmdir()
    path.symlink_to(tmp_path / "missing")
    with pytest.raises(InvalidState):
        read_write_recovery(bundle.root)


def test_a_broken_record_is_never_acted_on_by_any_state(tmp_path, monkeypatch):
    bundle = prepared(tmp_path, monkeypatch, "disabled", "rw_clean")
    put_record(bundle, raw=b'{"version": 9}')
    before = world(bundle)
    assert run(bundle, True)[0].reason_code == "write_recovery_invalid"
    assert world(bundle) == before and bundle.systemd.afx_state == "active"


# --- I/O と systemd の失敗 ----------------------------------------------------------------------------------

def _arm(bundle, at, monkeypatch, kind):
    """hook `at` の後で、記録 file への指定の操作を 1 回だけ失敗させる。"""
    armed = {"on": False}
    real_replace, real_unlink, real_fsync = os.replace, Path.unlink, units.fsync_dir

    def replace(src, dst, *a, **k):
        if armed["on"] and kind == "replace" and Path(dst).name == record_path(bundle).name:
            armed["on"] = False
            raise OSError(5, "replace")
        return real_replace(src, dst, *a, **k)

    def unlink(self, *a, **k):
        if armed["on"] and kind == "unlink" and self.name == record_path(bundle).name:
            armed["on"] = False
            raise OSError(5, "unlink")
        return real_unlink(self, *a, **k)

    def fsync(directory):
        if armed["on"] and kind in ("fsync-save", "fsync-remove"):
            armed["on"] = False
            raise OSError(5, "fsync")
        return real_fsync(directory)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(units, "fsync_dir", fsync)

    def hook(name):
        if name == at:
            armed["on"] = True
    return hook


@pytest.mark.parametrize("kind, at, kept", [("replace", "before_recovery_record", False),
                                            ("fsync-save", "before_recovery_record", True),
                                            ("unlink", "before_recovery_remove", True),
                                            ("fsync-remove", "before_recovery_remove", False)])
def test_a_failed_save_or_removal_stops_without_advancing_and_a_rerun_converges(
        tmp_path, monkeypatch, kind, at, kept):
    bundle = prepared(tmp_path, monkeypatch)
    stage_before = bundle.stage_file.read_bytes()
    result, _ = run(bundle, False, hook=_arm(bundle, at, monkeypatch, kind))
    assert (result.rc, result.reason_code) == (1, "material_write_failed")
    if at == "before_recovery_record":
        assert afx_calls(bundle, "stop") == [] and read_marker(bundle.root) is None
    else:
        assert consistent(bundle, False)
    assert bundle.stage_file.read_bytes() == stage_before
    quiet(bundle)
    result, _ = run(bundle, False)
    if kind == "fsync-remove":
        assert (result.rc, result.reason_code) == (1, "not_enabled")
    else:
        assert result.rc == 0, (kind, result)
    assert record_of(bundle) is None and consistent(bundle, False) and bundle.systemd.afx_state == "active"
    assert bundle.stage_file.read_bytes() == stage_before


def _fail_call(name):
    from agentic_fx.ops_setup.systemd import SystemdError

    def prep(bundle, monkeypatch):
        bundle.systemd.fail[name] = SystemdError(name, "rc=1")
        return bundle.systemd.fail.clear
    return prep


def _fail_lock(bundle, monkeypatch):
    from agentic_fx.ops_setup.systemd import SystemdError

    def locked():
        raise SystemdError("afx_still_running")
    real = bundle.setup._lock
    bundle.setup._lock = locked
    return lambda: setattr(bundle.setup, "_lock", real)


def _fail_unmark(bundle, monkeypatch):
    import agentic_fx.ops_setup.web_write as web_write
    real = web_write._remove_marker

    def broken(_root):
        raise OSError(5, "unlink marker")
    web_write._remove_marker = broken
    return lambda: setattr(web_write, "_remove_marker", real)


def _fail_show(bundle, monkeypatch):
    from agentic_fx.ops_setup.systemd import SystemdError
    armed = bundle.setup.hook

    def hook(name):
        if name == "before_show":
            bundle.systemd.fail["show"] = SystemdError("show", "rc=1")
    bundle.setup.hook = hook
    return lambda: (bundle.systemd.fail.clear(), setattr(bundle.setup, "hook", armed))


@pytest.mark.parametrize("prep, reason", [(_fail_call("stop"), "stop"), (_fail_show, "show"),
                                          (_fail_lock, "afx_still_running"),
                                          (_fail_unmark, "marker_remove_failed"), (_fail_call("start"), "start")])
def test_a_systemd_or_marker_failure_keeps_the_record_and_a_rerun_converges(tmp_path, monkeypatch, prep, reason):
    bundle = prepared(tmp_path, monkeypatch)
    stage_before = bundle.stage_file.read_bytes()
    heal = prep(bundle, monkeypatch)
    result, _ = run(bundle, False)
    assert (result.rc, result.reason_code) == (1, reason)
    assert record_of(bundle) == {"version": 1, "afx_before_stop": "active"}
    assert bundle.stage_file.read_bytes() == stage_before
    heal()
    result, _ = run(bundle, False)
    assert result.rc == 0 and record_of(bundle) is None and read_marker(bundle.root) is None
    assert consistent(bundle, False) and bundle.systemd.afx_state == "active"
    assert bundle.stage_file.read_bytes() == stage_before


