from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from agentic_fx.ops_setup.interactive import EnableResult
from agentic_fx.ops_setup.setup_lease import SetupLeaseBusy, acquire_setup_lease
from agentic_fx.setup import post_start, unit as unit_module, wizard
from agentic_fx.setup.wizard import IndicatorOutcome, WizardContext, run_wizard
from agentic_fx.store.instance_lock import InstanceAlreadyRunning, acquire_instance_lock
from agentic_fx.store.setup_marker import read_setup_marker, setup_marker_present
from agentic_fx.store.setup_record import read_setup_record

from tests.setup.test_wizard_commit import Crash, crash_at, state_of
from tests.setup.test_wizard_rerun import installed_root
from tests.setup.wizard_support import (
    Rules, ScriptedWizardIO, fresh_responder, make_deps, make_root,
)

UNIT_RULES = (("systemd のユーザーサービス", "y"), ("外から見る手段を今", "n"), ("enable しますか", "y"),
              ("起動しますか", "y"))


def record_of(root):
    return read_setup_record(root).record.data


def installed_rules(*extra):
    return Rules(*extra, ("価格源", "1"), ("方針文", ""), ("最初の課題", ""), ("書き込みます", "y"),
                 *UNIT_RULES)


def test_the_indicator_hook_receives_a_context_and_its_count_reaches_the_record(tmp_path):
    root = make_root(tmp_path)
    seen = []

    def stage(context: WizardContext):
        seen.append((context, context.lease.active))
        return IndicatorOutcome(3)

    deps, *_ = make_deps(tmp_path, indicator_stage=stage)
    assert run_wizard(root, ScriptedWizardIO(responder=fresh_responder()), deps) == 0
    assert len(seen) == 1 and seen[0][0].root == root.resolve() and seen[0][1]
    assert seen[0][0].settings.datafeed.primary == "mt5"
    assert record_of(root)["indicators"] == {"installed": 3}


def test_without_the_indicator_hook_the_manual_route_is_shown_and_none_are_counted(tmp_path):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path, indicator_stage=None)
    io = ScriptedWizardIO(responder=fresh_responder())
    assert run_wizard(root, io, deps) == 0
    assert "afx plugin bless <名> --from _human" in io.text
    assert record_of(root)["indicators"] == {"installed": 0}


def test_the_indicator_hook_is_not_called_without_landlock(tmp_path):
    root = make_root(tmp_path)
    called = []
    deps, *_ = make_deps(tmp_path, landlock_available=lambda: False,
                         indicator_stage=lambda context: called.append(1) or IndicatorOutcome(9))
    assert run_wizard(root, ScriptedWizardIO(responder=fresh_responder(("続けますか", "y"))), deps) == 0
    assert called == []


def test_policy_lines_and_first_ideas_are_written_one_at_a_time(tmp_path):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(
        ("方針文", ("ボラティリティ収縮の後を重視する", "損切りを広げない", "")),
        ("最初の課題", ("<placeholder>", "ボリンジャーバンドの逆張りを検討する", "")),
        ("このまま追加しますか", "n")))
    assert run_wizard(root, io, deps) == 0, io.text
    directives = (root / "policy" / "directives.md").read_text()
    assert directives == "\n- ボラティリティ収縮の後を重視する\n\n- 損切りを広げない\n"
    conn = sqlite3.connect(root / "data" / "agentic.db")
    rows = conn.execute("SELECT idea, source FROM improvement_backlog").fetchall()
    assert rows == [("ボリンジャーバンドの逆張りを検討する", "user")]
    assert "プレースホルダ" in io.text
    assert "既存の方針文: 0 文字" in io.text


def test_existing_policy_size_and_backlog_count_are_shown_before_asking(tmp_path):
    root, _ = installed_root(tmp_path)
    (root / "policy").mkdir()
    (root / "policy" / "directives.md").write_text("- 既存\n", encoding="utf-8")
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder())
    assert run_wizard(root, io, deps) == 0
    assert "既存の方針文: 5 文字" in io.text


def _lock_probe(root, log):
    def probe(label):
        lease_busy = False
        try:
            other = acquire_setup_lease(root)
        except SetupLeaseBusy:
            lease_busy = True
        else:
            other.close()
        free = True
        try:
            acquire_instance_lock(root / "data").close()
        except InstanceAlreadyRunning:
            free = False
        log.append((label, lease_busy, free, setup_marker_present(root)))
    return probe


def test_the_unit_is_placed_then_handoff_enable_then_start_in_that_order(tmp_path):
    root = make_root(tmp_path)
    resolved = root.resolve()
    seen = []
    probe = _lock_probe(resolved, seen)
    markers = []

    def enable(root_, feature, *, lease=None, **kwargs):
        calls.log.append(f"enable:{feature}")
        probe("enable")
        markers.append(read_setup_marker(resolved).phase)
        return EnableResult(0, "enabled", None)

    def enable_and_start(root_, **kwargs):
        calls.log.append("enable_and_start")
        probe("start")
        return post_start.StartOutcome("started", None, "2026-10-02T06:00:00+00:00", active="ok",
                                       api_result="ok", ops={"bot": "ok", "web": "failed"})

    deps, calls, *_ = make_deps(tmp_path, enable=enable, enable_and_start=enable_and_start)
    io = ScriptedWizardIO(responder=fresh_responder(
        ("外から見る手段を今", "y"), ("有効にする手段", "bot, web"), ("systemd のユーザーサービス", "y")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert calls.log == ["place_unit", "enable:bot", "enable:web", "enable_and_start"]
    # setup.lock is held and instance.lock is free while enabling; the marker is a handoff
    assert seen[0] == ("enable", True, True, True) and seen[1] == ("enable", True, True, True)
    assert markers == ["handoff", "handoff"]
    # at start time no marker remains and the instance lock can be taken (the wizard released it)
    assert seen[2] == ("start", True, True, False)
    assert not setup_marker_present(resolved)
    data = record_of(root)
    assert data["unit"] == "installed"
    assert data["ops"]["bot"]["result"] == "enabled" and data["ops"]["bot"]["ready"] == "ok"
    assert data["ops"]["web"]["ready"] == "failed"
    assert data["start"]["result"] == "started"


def test_later_for_outside_access_places_no_marker_and_releases_the_instance_lock_only_for_start(tmp_path):
    root = make_root(tmp_path)
    resolved = root.resolve()
    seen = []
    probe = _lock_probe(resolved, seen)
    hooks = []

    def enable_and_start(root_, **kwargs):
        probe("start")
        return post_start.StartOutcome("skipped", "start_declined", "2026-10-02T06:00:00+00:00")

    deps, calls, *_ = make_deps(tmp_path, enable_and_start=enable_and_start, hook=hooks.append)
    io = ScriptedWizardIO(responder=fresh_responder(("systemd のユーザーサービス", "y"), ("外から見る手段を今", "n")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert calls.enable == []
    assert [h for h in hooks if "marker" in h and "commit" not in h and "settings" not in h and "setup_record" not in h
            and h not in ("before_setup_marker_remove", "after_setup_marker_unlink", "after_setup_marker_remove")] \
        == ["before_setup_marker_write", "before_setup_marker_replace", "after_setup_marker_replace",
            "after_setup_marker_write"]
    assert seen == [("start", True, True, False)]
    assert record_of(root)["start"]["result"] == "skipped"


def test_when_afx_ops_cannot_be_resolved_the_features_fall_back_to_later(tmp_path):
    root = make_root(tmp_path)
    hooks = []
    deps, calls, *_ = make_deps(
        tmp_path, hook=hooks.append,
        resolve_ops=lambda root_: post_start.OpsPrecheck(False, "ops_exec_not_found", "afx-ops が見つかりません"))
    io = ScriptedWizardIO(responder=fresh_responder(
        ("systemd のユーザーサービス", "y"), ("外から見る手段を今", "y"), ("有効にする手段", "bot")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert calls.enable == []
    assert "afx-ops が見つかりません" in io.text
    assert "before_handoff_marker" not in hooks
    assert record_of(root)["ops"]["bot"] == {"result": "later", "reason_code": "ops_exec_not_found",
                                             "at": record_of(root)["ops"]["bot"]["at"], "ready": "unchecked"}
    assert calls.log[-1] == "enable_and_start"


def test_a_failed_enable_does_not_stop_the_wizard_and_is_recorded_with_its_reason(tmp_path):
    root = make_root(tmp_path)

    def enable(root_, feature, *, lease=None, **kwargs):
        return EnableResult(1, "probe", "feature_probe_failed:bot")

    deps, calls, *_ = make_deps(tmp_path, enable=enable)
    io = ScriptedWizardIO(responder=fresh_responder(
        ("systemd のユーザーサービス", "y"), ("外から見る手段を今", "y"), ("有効にする手段", "bot")))
    assert run_wizard(root, io, deps) == 0, io.text
    item = record_of(root)["ops"]["bot"]
    assert item["result"] == "failed" and item["stage"] == "probe"
    assert item["reason_code"] == "feature_probe_failed:bot"
    assert "afx ops bot enable" in io.text
    assert not setup_marker_present(root)
    assert calls.enable_and_start[0]["features"] == []


def test_the_wizard_never_hands_its_terminal_to_the_enable_step(tmp_path):
    root = make_root(tmp_path)
    deps, calls, *_ = make_deps(tmp_path)  # the fake enable fails the test when it receives io
    io = ScriptedWizardIO(responder=fresh_responder(
        ("systemd のユーザーサービス", "y"), ("外から見る手段を今", "y"), ("有効にする手段", "notify")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert [feature for feature, held in calls.enable] == ["notify"]
    assert all(held for _, held in calls.enable)


def test_start_outcomes_are_recorded_as_three_separate_observations(tmp_path):
    root = make_root(tmp_path)
    admission = []

    def read_admission(run, invocation_id, **kwargs):
        admission.append((invocation_id, kwargs))
        return post_start.IsolationObservation("no", "landlock_abi_too_old", "invocation_id")

    def enable_and_start(root_, **kwargs):
        return post_start.StartOutcome(
            "started", None, "2026-10-02T06:00:00+00:00", active="ok", api_result="failed",
            api_reason_code="socket_missing", ops={}, invocation_id="inv-1", journal_cursor="c-1")

    deps, calls, *_ = make_deps(tmp_path, enable_and_start=enable_and_start,
                                read_admission_line=read_admission)
    io = ScriptedWizardIO(responder=fresh_responder(("systemd のユーザーサービス", "y")))
    assert run_wizard(root, io, deps) == 0, io.text
    data = record_of(root)
    assert data["start"]["result"] == "started"
    assert data["api"]["result"] == "failed" and data["api"]["reason_code"] == "socket_missing"
    assert data["isolation"]["admitted"] == "unknown"
    assert admission == []
    assert data["isolation"] == {"admitted": "unknown", "reason_code": "not_cli_backend",
                                 "at": "2026-10-02T06:00:00+00:00"}
    assert "journalctl --user -u afx -n 50" in io.text


def test_a_local_improve_backend_records_that_no_admission_applies(tmp_path):
    root = make_root(tmp_path)

    def enable_and_start(root_, **kwargs):
        return post_start.StartOutcome("started", None, "2026-10-02T06:00:00+00:00", active="ok", api_result="ok")

    def forbidden_admission(*args, **kwargs):
        raise AssertionError("local backend must not inspect admission")

    deps, *_ = make_deps(tmp_path, enable_and_start=enable_and_start, read_admission_line=forbidden_admission)
    io = ScriptedWizardIO(responder=fresh_responder(("systemd のユーザーサービス", "y")))
    assert run_wizard(root, io, deps) == 0
    assert record_of(root)["isolation"]["reason_code"] == "not_cli_backend"
    assert record_of(root)["api"]["result"] == "ok"


def test_a_disabled_operation_api_is_explained_in_the_closing_text_and_recorded(tmp_path):
    root = make_root(tmp_path)
    deps, calls, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(("afx を常駐サービス", "n")))
    assert run_wizard(root, io, deps) == 0, io.text
    data = record_of(root)
    assert data["api"]["enabled"] is False and calls.enable_and_start == []
    assert "api.enabled: false" in (root / "config" / "settings.yaml").read_text() or \
        "enabled: false" in (root / "config" / "settings.yaml").read_text()
    assert "手動起動" in io.text


def test_declining_the_start_is_recorded_and_shown_as_a_pending_step(tmp_path):
    root = make_root(tmp_path)

    def enable_and_start(root_, **kwargs):
        return post_start.StartOutcome("pending", "unit_start_failed", "2026-10-02T06:00:00+00:00")

    deps, *_ = make_deps(tmp_path, enable_and_start=enable_and_start)
    io = ScriptedWizardIO(responder=fresh_responder(("systemd のユーザーサービス", "y")))
    assert run_wizard(root, io, deps) == 0
    assert record_of(root)["start"]["result"] == "pending"
    assert "未完の手順:" in io.text
    assert "systemctl --user start afx" in io.text


@pytest.mark.parametrize("state,answer,expected,reason", [
    ("Linger=yes\n", None, "already_enabled", None),
    ("Linger=no\n", "n", "declined", None),
    ("garbage\n", None, "failed", "unknown")])
def test_linger_is_changed_only_after_a_yes_and_only_when_it_is_off(tmp_path, state, answer, expected, reason):
    root = make_root(tmp_path)
    calls_linger = []

    def enable_linger(run, *, confirm):
        calls_linger.append(1)
        return unit_module.enable_linger(run, confirm=confirm, user="u")

    deps, calls, fetch, run = make_deps(tmp_path, enable_linger=enable_linger)
    run.on_prefix("loginctl", "show-user", stdout=state)
    io = ScriptedWizardIO(responder=fresh_responder(("systemd のユーザーサービス", "y"), ("enable-linger", answer or "n")))
    assert run_wizard(root, io, deps) == 0, io.text
    value = record_of(root)["linger"]
    assert value["state"] == expected and value.get("reason_code") == reason
    assert (len(calls_linger) == 1) is (state == "Linger=no\n")
    assert run.matching("loginctl", "enable-linger") == []
    if answer == "n":
        assert io.text.count("ログアウトすると afx も止まります") == 1


def test_linger_enabled_by_the_wizard_is_owned_only_after_the_state_is_read_back(tmp_path):
    root = make_root(tmp_path)
    deps, calls, fetch, run = make_deps(tmp_path, enable_linger=lambda run_, *, confirm: unit_module.enable_linger(
        run_, confirm=confirm, user="u"))
    run.on_prefix_sequence("loginctl", "show-user", replies=[(0, "Linger=no\n"), (0, "Linger=no\n"), (0, "Linger=yes\n")])
    run.on_prefix("loginctl", "enable-linger")
    io = ScriptedWizardIO(responder=fresh_responder(("systemd のユーザーサービス", "y"), ("enable-linger", "y")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert record_of(root)["linger"]["state"] == "owned_by_wizard"
    assert len(run.matching("loginctl", "enable-linger")) == 1


@pytest.mark.parametrize("state", ["blocked", "check_failed", "no_systemd", "failed"])
def test_a_unit_that_cannot_be_placed_ends_the_stage_with_a_manual_start_guide(tmp_path, state):
    root = make_root(tmp_path)

    def place_unit(root_, settings, **kwargs):
        return unit_module.PlacementResult(state, "systemd_unavailable", "手で起動してください")

    deps, calls, *_ = make_deps(tmp_path, place_unit=place_unit)
    io = ScriptedWizardIO(responder=fresh_responder(("systemd のユーザーサービス", "y")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert record_of(root)["unit"] == "not_installed"
    assert calls.enable_and_start == [] and calls.enable == []
    assert "手で起動してください" in io.text


def test_declining_the_resident_service_shows_the_manual_start_and_skips_the_rest(tmp_path):
    root = make_root(tmp_path)
    deps, calls, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder())
    assert run_wizard(root, io, deps) == 0
    assert "uv run afx" in io.text
    assert calls.log == []
    assert io.asked_with("外から見る手段") == []


def test_declining_daemon_keeps_api_disabled_and_does_not_place_a_unit(tmp_path):
    root = make_root(tmp_path)
    deps, calls, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(("afx を常駐サービス", "n")))
    assert run_wizard(root, io, deps) == 0, io.text
    data = record_of(root)
    assert data["api"]["enabled"] is False
    assert data["unit"] == "not_installed" and data["start"]["reason_code"] == "declined"
    assert calls.log == []
    assert "常駐に切り替えるときは `afx init` を再実行" in io.text


def test_accepting_daemon_enables_api_and_places_a_unit(tmp_path):
    root = make_root(tmp_path)
    deps, calls, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(("afx を常駐サービス", "y")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert record_of(root)["api"]["enabled"] is True
    assert calls.log[:1] == ["place_unit"]


def test_the_confirmation_texts_for_enable_and_start_are_asked_separately(tmp_path):
    root = make_root(tmp_path)
    answers = []

    def enable_and_start(root_, *, confirm, **kwargs):
        answers.append((confirm("enable"), confirm("start")))
        return post_start.StartOutcome("skipped", "start_declined", "2026-10-02T06:00:00+00:00")

    deps, *_ = make_deps(tmp_path, enable_and_start=enable_and_start)
    io = ScriptedWizardIO(responder=fresh_responder(
        ("systemd のユーザーサービス", "y"), ("enable しますか", "y"), ("起動しますか", "n")))
    assert run_wizard(root, io, deps) == 0
    assert answers == [(True, False)]
    assert "価格の取り込みと" in io.text


# ---------------------------------------------------------------- resume
def test_a_crash_between_unit_placement_and_handoff_leaves_no_marker_and_shows_the_unit_as_placed(tmp_path):
    root = make_root(tmp_path)
    rules = lambda: fresh_responder(("systemd のユーザーサービス", "y"), ("外から見る手段を今", "y"),  # noqa: E731
                                    ("有効にする手段", "bot"))
    deps, calls, *_ = make_deps(tmp_path, hook=crash_at("before_handoff_marker"))
    with pytest.raises(Crash):
        run_wizard(root, ScriptedWizardIO(responder=rules()), deps)
    assert not setup_marker_present(root)
    assert calls.log == ["place_unit"]

    def place_unit(root_, settings, **kwargs):
        return unit_module.PlacementResult("unchanged")

    deps2, calls2, *_ = make_deps(tmp_path, place_unit=place_unit)
    io = ScriptedWizardIO(responder=rules())
    assert run_wizard(root, io, deps2) == 0, io.text
    assert "unit は設置済みです" in io.text
    assert [feature for feature, _ in calls2.enable] == ["bot"]


def test_a_crash_inside_handoff_resumes_at_the_outside_access_question_without_touching_settings(tmp_path):
    root = make_root(tmp_path)
    rules = lambda: fresh_responder(("systemd のユーザーサービス", "y"), ("外から見る手段を今", "y"),  # noqa: E731
                                    ("有効にする手段", "bot"))
    deps, *_ = make_deps(tmp_path, hook=crash_at("after_handoff_instance_lock_release"))
    with pytest.raises(Crash):
        run_wizard(root, ScriptedWizardIO(responder=rules()), deps)
    assert read_setup_marker(root).phase == "handoff"
    before = state_of(root)
    markers_seen = []

    def enable(root_, feature, *, lease=None, **kwargs):
        markers_seen.append(read_setup_marker(root.resolve()).phase)
        return EnableResult(0, "enabled", None)

    deps2, calls2, *_ = make_deps(tmp_path, enable=enable)
    io = ScriptedWizardIO(responder=Rules(("外から見る手段を今", "y"), ("有効にする手段", "bot"),
                                          ("systemd のユーザーサービス", "y"), default=""))
    assert run_wizard(root, io, deps2) == 0, io.text
    assert io.asked_with("価格源") == [] and io.asked_with("書き込みます") == []
    assert io.asked_with("systemd のユーザーサービス") == []
    assert markers_seen == ["handoff"]
    assert not setup_marker_present(root)
    after = state_of(root)
    for key in ("settings", "app_state", "db"):
        assert after[key] == before[key]
    assert record_of(root)["ops"]["bot"]["result"] == "enabled"


def test_resuming_a_handoff_and_choosing_later_still_removes_the_marker(tmp_path):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path, hook=crash_at("after_handoff_instance_lock_release"))
    with pytest.raises(Crash):
        run_wizard(root, ScriptedWizardIO(responder=fresh_responder(
            ("systemd のユーザーサービス", "y"), ("外から見る手段を今", "y"), ("有効にする手段", "bot"))), deps)
    assert setup_marker_present(root)
    deps2, calls2, *_ = make_deps(tmp_path)
    assert run_wizard(root, ScriptedWizardIO(responder=fresh_responder(("外から見る手段を今", "n"))), deps2) == 0
    assert not setup_marker_present(root)
    assert calls2.enable == []


def test_a_crash_after_the_marker_is_gone_but_before_start_records_a_pending_start(tmp_path):
    root = make_root(tmp_path)

    def enable_and_start(root_, **kwargs):
        raise Crash("before start")

    deps, *_ = make_deps(tmp_path, enable_and_start=enable_and_start)
    with pytest.raises(Crash):
        run_wizard(root, ScriptedWizardIO(responder=fresh_responder(("systemd のユーザーサービス", "y"))), deps)
    assert not setup_marker_present(root)
    assert record_of(root)["start"]["result"] == "pending"


def test_a_local_backend_does_not_read_the_started_service_admission(tmp_path):
    root = make_root(tmp_path)

    def enable_and_start(root_, **kwargs):
        return post_start.StartOutcome("started", None, "2026-10-02T06:00:00+00:00", active="ok", api_result="ok")

    def read_admission(*args, **kwargs):
        raise Crash("observing")

    deps, *_ = make_deps(tmp_path, enable_and_start=enable_and_start, read_admission_line=read_admission)
    assert run_wizard(root, ScriptedWizardIO(responder=fresh_responder(("systemd のユーザーサービス", "y"))), deps) == 0
    assert record_of(root)["start"]["result"] == "started"


# ---------------------------------------------------------------- closing text
def test_the_closing_pending_list_uses_the_shared_renderer(tmp_path):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    fetch_models_down = deps.fetch
    fetch_models_down.models = {}
    io = ScriptedWizardIO(responder=fresh_responder(("LLM の構成", "4"), ("価格源", "2")))
    assert run_wizard(root, io, deps) == 0, io.text
    steps = io.text.split("未完の手順:")[1]
    assert "bridge" in steps.lower() or "MT5" in steps
    assert steps.count("LLM") >= 1


def test_nothing_is_printed_when_there_is_nothing_pending(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    monkeypatch.setattr(wizard, "pending_steps", lambda facts: [])
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder())
    assert run_wizard(root, io, deps) == 0
    assert "未完の手順:" not in io.text


def test_pending_step_wording_lives_only_in_the_shared_module():
    source = (Path(wizard.__file__).parent / "pending_text.py").read_text(encoding="utf-8")
    wizard_source = Path(wizard.__file__).read_text(encoding="utf-8")
    literals = [text for text in re.findall(r'"([^"\n]{12,})"', source) if re.search(r"[ぁ-んァ-ン一-龥]", text)]
    assert literals
    assert [text for text in literals if text in wizard_source] == []
