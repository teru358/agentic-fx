from __future__ import annotations

from dataclasses import replace
import pytest

from agentic_fx import service
from agentic_fx.config import load_settings_text
from agentic_fx.setup import wizard
from agentic_fx.setup.preflight import PreflightFailure
from agentic_fx.setup.wizard import run_wizard

from tests.setup.test_wizard_models import claude_world, codex_world, home, record_of, settings_of  # noqa: F401
from tests.setup.test_wizard_rerun import installed_root
from tests.setup.wizard_support import (
    FakeResponse, LOCK_FILES, ScriptedWizardIO, codex_model_list, fresh_responder, make_deps, make_root, snapshot,
)

KEY = "KEYVALUE-1"


def bridge_headers(fetch):
    return [kwargs.get("headers") for url, kwargs in fetch.calls if url.endswith("/health")]


@pytest.mark.parametrize("url", ["https://bridge.example:8812", "http://127.0.0.1:8812",
                                 "http://localhost:8812", "http://[::1]:8812"])
def test_the_bridge_key_goes_only_to_https_or_this_machine(tmp_path, url):
    root = make_root(tmp_path)
    deps, calls, fetch, run = make_deps(tmp_path, environ={"MT5_BRIDGE_API_KEY": KEY})
    io = ScriptedWizardIO(responder=fresh_responder(("bridge の URL", url), ("bridge 側で API キー", "1")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert bridge_headers(fetch)[0] == {"X-Bridge-Api-Key": KEY}
    assert KEY not in io.text


@pytest.mark.parametrize("url", ["http://bridge.example:8812", "http://localhost.example.com:8812",
                                 "http://192.168.0.5:8812"])
def test_the_bridge_key_is_never_sent_over_plain_http_to_another_host(tmp_path, url):
    root = make_root(tmp_path)
    deps, calls, fetch, run = make_deps(tmp_path, environ={"MT5_BRIDGE_API_KEY": KEY})
    io = ScriptedWizardIO(responder=fresh_responder(("bridge の URL", url), ("bridge 側で API キー", "1")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert bridge_headers(fetch)[0] == {}
    assert "平文 http には鍵を送りません" in io.text


def test_plain_http_auth_is_left_unverified_without_a_retry_question(tmp_path):
    root = make_root(tmp_path)
    calls = []
    deps, _, default_fetch, _ = make_deps(tmp_path, environ={"MT5_BRIDGE_API_KEY": KEY})

    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/health"):
            return FakeResponse({"mt5_connected": True})
        if "/quote/" in url:
            return FakeResponse({}, 401)
        return default_fetch(url, **kwargs)

    deps = replace(deps, fetch=fetch)
    io = ScriptedWizardIO(responder=fresh_responder(
        ("bridge の URL", "http://bridge.example:8812"), ("bridge 側で API キー", "1"),
        ("[r] 再試行", "2")))
    assert run_wizard(root, io, deps) == 0, io.text
    price = record_of(root)["price"]
    assert price["result"] == "unverified" and price["reason_code"] == "key_withheld_plain_http"
    assert not io.asked_with("[r] 再試行")
    assert "daemon は `.env` の鍵で接続します" in io.text
    assert all(kwargs.get("headers") == {} for url, kwargs in calls if "bridge.example" in url)


def test_https_auth_failure_keeps_the_retry_question(tmp_path):
    root = make_root(tmp_path)
    deps, _, default_fetch, _ = make_deps(tmp_path, environ={"MT5_BRIDGE_API_KEY": KEY})

    def fetch(url, **kwargs):
        if url.endswith("/health"):
            return FakeResponse({"mt5_connected": True})
        if "/quote/" in url:
            return FakeResponse({}, 401)
        return default_fetch(url, **kwargs)

    deps = replace(deps, fetch=fetch)
    io = ScriptedWizardIO(responder=fresh_responder(
        ("bridge の URL", "https://bridge.example:8812"), ("bridge 側で API キー", "1"),
        ("[r] 再試行", "2")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert record_of(root)["price"]["result"] == "planned"
    assert io.asked_with("[r] 再試行")
    assert "MT5_BRIDGE_API_KEY を確かめてください" in io.text


def test_summary_hides_setting_keys_and_reopens_after_showing_the_diff(tmp_path):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(
        ("この内容で書き込みます", ("d", "y"))))
    assert run_wizard(root, io, deps) == 0, io.text
    summary, diff = io.text.split("settings.yaml の変更点", 1)
    assert "datafeed." not in summary and "確定 1" not in summary and "planned" not in summary
    assert "現在値 → 新しい値" in diff
    assert len(io.asked_with("この内容で書き込みます")) == 2


def test_summary_decline_writes_no_setup_files(tmp_path):
    root = make_root(tmp_path)
    before = snapshot(root)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(("この内容で書き込みます", "n")))
    assert run_wizard(root, io, deps) == 1, io.text
    after = snapshot(root)
    assert {key: value for key, value in after.items() if key in before} == before
    assert set(after) - set(before) <= LOCK_FILES


def test_example_shortcut_adds_all_examples_to_policy_and_backlog(tmp_path):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(
        ("方針文", ("e", "")), ("最初の課題", ("e", ""))))
    assert run_wizard(root, io, deps) == 0, io.text
    log = (root / "logs" / "activity.log").read_text(encoding="utf-8")
    assert log.count("\tpolicy_added\t") == 3
    assert log.count("\tbacklog_added\t") == 1


@pytest.mark.parametrize("answer", ["2", "3", ""])
def test_the_bridge_key_is_not_sent_unless_the_user_said_one_is_set(tmp_path, answer):
    root = make_root(tmp_path)
    deps, calls, fetch, run = make_deps(tmp_path, environ={"MT5_BRIDGE_API_KEY": KEY})
    io = ScriptedWizardIO(responder=fresh_responder(("bridge 側で API キー", answer)))
    assert run_wizard(root, io, deps) == 0, io.text
    assert bridge_headers(fetch)[0] == {}


def lease_too_short(text: str) -> str:
    assert "signal_lease_min: 15 " in text
    return text.replace("signal_lease_min: 15 ", "signal_lease_min: 1  ")


def test_a_value_the_questions_cannot_fix_offers_to_stop_without_writing(tmp_path):
    root, _ = installed_root(tmp_path, mutate=lease_too_short)
    before = snapshot(root)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(("[a] 中止して", "a")))
    assert run_wizard(root, io, deps) == 1, io.text
    assert "signal_lease_min" in io.text and "何も書いていません" in io.text
    assert len(io.asked_with("判断足")) == 1
    assert set(snapshot(root)) - set(before) <= LOCK_FILES
    assert {k: v for k, v in snapshot(root).items() if k in before} == before


def test_a_value_the_questions_cannot_fix_can_restart_from_the_first_question(tmp_path):
    root, _ = installed_root(tmp_path, mutate=lease_too_short)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(("[a] 中止して", ("r", "a"))))
    assert run_wizard(root, io, deps) == 1, io.text
    assert len(io.asked_with("表示タイムゾーン (例")) == 2


def test_a_failed_check_naming_the_improve_schedule_returns_to_the_schedule_question(tmp_path, monkeypatch):
    calls = {"n": 0}
    real = wizard.service._check_mission_ceilings

    def fail_once(settings, activity):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PreflightFailure("mission_ceiling_violation", "schedule.improve_at が許容範囲外です")
        return real(settings, activity)

    monkeypatch.setattr(wizard.service, "_check_mission_ceilings", fail_once)
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder())
    assert run_wizard(root, io, deps) == 0, io.text
    assert "改善周期の段へ戻ります" in io.text
    assert len(io.asked_with("改善の周期")) == 2 and len(io.asked_with("判断足")) == 1


def test_a_failed_improve_exchange_is_verify_failed_not_a_connection_error(tmp_path, home):
    root = make_root(tmp_path)
    deps, run = claude_world(tmp_path, home)
    run.on(__import__("sys").executable, "main.py", "improve", "verify-backend", "--backend", "claude", returncode=1)
    io = ScriptedWizardIO(responder=fresh_responder(("LLM の構成", "1"), ("改善 backend の実往復", "y")))
    assert run_wizard(root, io, deps) == 0, io.text
    improve = record_of(root)["llm_improve"]
    assert improve["result"] == "failed" and improve["reason_code"] == "verify_failed"


def test_a_state_that_is_not_initialized_is_reset_even_when_its_mode_is_trading(tmp_path):
    root = make_root(tmp_path)
    store = service._state_store(root)
    store.update(initialized=False, mode=service.Mode.TRADING, autopilot=True)
    deps, *_ = make_deps(tmp_path)
    assert run_wizard(root, ScriptedWizardIO(responder=fresh_responder()), deps) == 0
    state = store.load()
    assert state.initialized and state.mode is service.Mode.LEARNING and state.autopilot is False


def test_a_relative_codex_path_is_asked_again_and_the_current_value_is_confirmed_as_unchanged(tmp_path, home):
    root = make_root(tmp_path)
    deps, run, elf = codex_world(tmp_path, home, None)
    io = ScriptedWizardIO(responder=fresh_responder(
        ("LLM の構成", "2"), ("codex の実体", ("./codex", "~/bin/codex", str(elf)))))
    run.on(str(elf), "app-server", stdout=codex_model_list("gpt-6.1-terra"))
    assert run_wizard(root, io, deps) == 0, io.text
    assert io.text.count("絶対パス (/ で始まる) で入力してください") == 2
    assert settings_of(root).runner.codex.bin == str(elf)


def test_entering_the_current_codex_path_says_nothing_changed(tmp_path, home):
    root = make_root(tmp_path)
    deps, run, elf = codex_world(tmp_path, home, None)
    current = load_settings_text((root / "config" / "settings.yaml.example").read_text()).runner.codex.bin
    io = ScriptedWizardIO(responder=fresh_responder(("LLM の構成", "2"), ("codex の実体", current)))
    assert run_wizard(root, io, deps) == 0, io.text
    assert "変更なし" in io.text
    assert settings_of(root).runner.codex.bin == current


def test_policy_and_first_ideas_leave_the_same_activity_lines_as_the_shell(tmp_path):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(
        ("方針文", ("損切りを広げない", "")), ("最初の課題", ("ボリンジャーバンドの逆張りを検討する", ""))))
    assert run_wizard(root, io, deps) == 0, io.text
    log = (root / "logs" / "activity.log").read_text()
    assert "policy_added" in log and "損切りを広げない" in log
    assert "backlog_added" in log and "#1" in log


@pytest.mark.parametrize("rerun", [False, True])
def test_a_settings_file_changed_during_the_questions_stops_before_anything_is_written(tmp_path, rerun):
    if rerun:
        root, _ = installed_root(tmp_path)
    else:
        root = make_root(tmp_path)
    path = root / "config" / "settings.yaml"
    rules = fresh_responder()

    def responder(prompt):
        if "書き込みます" in prompt:
            path.write_text("display_timezone: UTC\n# edited elsewhere\n")
        return rules(prompt)

    before = snapshot(root)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=responder)
    assert run_wizard(root, io, deps) == 1, io.text
    assert "設定が別の process に変更されました" in io.text
    assert path.read_text() == "display_timezone: UTC\n# edited elsewhere\n"
    after = snapshot(root)
    assert set(after) - set(before) <= LOCK_FILES | {"config/settings.yaml"}
    assert not (root / "data" / "agentic.db").exists()
    assert not list((root / "config").glob("settings.yaml.bak-*"))
