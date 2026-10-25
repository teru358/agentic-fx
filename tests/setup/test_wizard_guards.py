from __future__ import annotations

from dataclasses import replace
import sqlite3
import pytest

from agentic_fx import commands as commands_module
from agentic_fx import service
from agentic_fx.config import load_settings_text
from agentic_fx.policy_projection import inspect_policy_file, reconcile_policy_file, regenerate_policy_file
from agentic_fx.setup import wizard
from agentic_fx.setup.preflight import PreflightFailure
from agentic_fx.setup.wizard import run_wizard
from agentic_fx.store.db import connect, init_db

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


def test_plain_http_auth_without_a_key_explains_where_to_add_it(tmp_path):
    root = make_root(tmp_path)
    deps, _, default_fetch, _ = make_deps(tmp_path)

    def fetch(url, **kwargs):
        if url.endswith("/health"):
            return FakeResponse({"mt5_connected": True})
        if "/quote/" in url:
            return FakeResponse({}, 401)
        return default_fetch(url, **kwargs)

    io = ScriptedWizardIO(responder=fresh_responder(
        ("bridge の URL", "http://bridge.example:8812"), ("bridge 側で API キー", "1")))
    assert run_wizard(root, io, replace(deps, fetch=fetch)) == 0, io.text
    assert ".env` に MT5_BRIDGE_API_KEY がありません。段 8 で確認します" in io.text


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


def test_summary_names_unverified_llm_and_the_correct_price_check(tmp_path):
    settings = load_settings_text((make_root(tmp_path) / "config" / "settings.yaml.example").read_text())
    answers = wizard.Answers("Asia/Tokyo", "JPY", ["USDJPY"], 1_000_000,
                              source="yfinance", price={"result": "unverified", "reason_code": "network"},
                              llm_trade={"result": "unverified", "reason_code": "model_list_unavailable"})
    lines = wizard._summary_lines(answers, settings, __import__("tests.setup.wizard_support", fromlist=["NOW"]).NOW)
    assert "価格源: yfinance（価格の取得は未確認）" in lines
    assert "取引判断の LLM は未確認です (model_list_unavailable)" in lines


@pytest.mark.parametrize("source, wanted", [
    ("mt5", "価格源: MT5 bridge（URL 未設定、鍵 不明、bridge の接続は未確認）"),
    ("twelvedata", "価格源: twelvedata（価格の取得は未確認）"),
])
def test_summary_describes_the_unverified_check_for_each_price_source(tmp_path, source, wanted):
    settings = load_settings_text((make_root(tmp_path) / "config" / "settings.yaml.example").read_text())
    answers = wizard.Answers("Asia/Tokyo", "JPY", ["USDJPY"], 1_000_000,
                              source=source, price={"result": "failed", "reason_code": "network"})
    assert wanted in wizard._summary_lines(answers, settings, __import__("tests.setup.wizard_support", fromlist=["NOW"]).NOW)


def test_manual_choice_preserves_an_existing_unit_in_the_record(tmp_path):
    root = make_root(tmp_path)
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    (unit_dir / "afx.service").write_text("[Service]\n")
    deps, *_ = make_deps(tmp_path, unit_dir=unit_dir)
    io = ScriptedWizardIO(responder=fresh_responder(("afx を常駐サービス", "n")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert "既存の unit は残します。止めるには `systemctl --user disable --now afx`" in io.text
    assert record_of(root)["unit"] == "installed"


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
    conn = connect(root / "data" / "agentic.db")
    assert conn.execute("SELECT COUNT(*) FROM ops_policies").fetchone()[0] == 3
    assert inspect_policy_file(conn, wizard.directives_path(root)).status == "ok"
    conn.close()


def _stage12_database(root):
    conn = connect(root / "data" / "agentic.db")
    init_db(conn)
    return conn


def test_example_shortcut_uses_answers_and_does_not_add_duplicates(tmp_path):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(["e", "e", "", ""])
    answers = wizard.Answers("Asia/Tokyo", "JPY", ["EURUSD"], 1_000_000, timeframe="30m")
    _stage12_database(root).close()
    wizard._stage12(wizard._Session(root, io, deps), answers)
    policy = wizard.directives_path(root).read_text(encoding="utf-8")
    assert policy.count("対象は EURUSD の 30m 足") == 1
    conn = connect(root / "data" / "agentic.db")
    assert inspect_policy_file(conn, wizard.directives_path(root)).status == "ok"
    conn.close()
    assert "30m 足の押し目買い" in io.text and "追加済み" in io.text


def test_example_write_failure_stops_the_policy_prompt(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(["e", "should-not-be-asked"])
    answers = wizard.Answers("Asia/Tokyo", "JPY", ["EURUSD"], 1_000_000, timeframe="30m")

    def fail(conn, policy_path, activity, text, now):
        raise OSError("read-only")

    monkeypatch.setattr(wizard, "add_policy_directive", fail)
    wizard._stage12(wizard._Session(root, io, deps), answers)
    assert "書けませんでした" in io.text
    assert io.asked == ["方針文: "]


def test_stage12_examples_create_removable_managed_policies(tmp_path):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    _stage12_database(root).close()
    io = ScriptedWizardIO(["e", "", ""])
    answers = wizard.Answers("Asia/Tokyo", "JPY", ["EURUSD"], 1_000_000, timeframe="30m")

    wizard._stage12(wizard._Session(root, io, deps), answers)

    path = wizard.directives_path(root)
    conn = connect(root / "data" / "agentic.db")
    assert conn.execute("SELECT COUNT(*) FROM ops_policies").fetchone()[0] == 3
    assert path.read_text(encoding="utf-8").count("<!-- afx:managed-policy id=") == 3
    assert inspect_policy_file(conn, path).status == "ok"
    assert (root / "logs" / "activity.log").read_text(encoding="utf-8").count("\tpolicy_added\t") == 3
    conn.execute("DELETE FROM ops_policies")
    conn.commit()
    regenerate_policy_file(conn, path)
    assert inspect_policy_file(conn, path).status == "ok"
    conn.close()


def test_stage12_insert_failure_keeps_no_policy_or_activity(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    _stage12_database(root).close()
    io = ScriptedWizardIO(["失敗する方針", "should-not-be-asked"])
    answers = wizard.Answers("Asia/Tokyo", "JPY", ["EURUSD"], 1_000_000, timeframe="30m")

    def fail_insert(conn, text, *, request_id, now):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(commands_module, "insert_policy", fail_insert)
    wizard._stage12(wizard._Session(root, io, deps), answers)

    conn = connect(root / "data" / "agentic.db")
    assert "警告: 方針文を書けませんでした (OperationalError: database is locked)。" in io.text
    assert conn.execute("SELECT COUNT(*) FROM ops_policies").fetchone()[0] == 0
    assert not (root / "logs" / "activity.log").exists()
    assert io.asked == ["方針文: "]
    conn.close()


def test_stage12_projection_failure_keeps_record_without_activity(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    _stage12_database(root).close()
    io = ScriptedWizardIO(["記録済み方針", "should-not-be-asked"])
    answers = wizard.Answers("Asia/Tokyo", "JPY", ["EURUSD"], 1_000_000, timeframe="30m")

    real_reconcile = commands_module.reconcile_policy_file
    calls = []

    def fail_second_reconcile(conn, path):
        calls.append(path)
        if len(calls) == 1:
            return real_reconcile(conn, path)
        raise OSError("read-only")

    monkeypatch.setattr(commands_module, "reconcile_policy_file", fail_second_reconcile)
    wizard._stage12(wizard._Session(root, io, deps), answers)

    conn = connect(root / "data" / "agentic.db")
    assert ("警告: 方針文は記録しましたが policy/directives.md へ反映できませんでした "
            "(OSError: read-only)。afx の次回起動時に反映されます。") in io.text
    assert conn.execute("SELECT COUNT(*) FROM ops_policies").fetchone()[0] == 1
    assert not (root / "logs" / "activity.log").exists()
    assert io.asked == ["方針文: "]
    conn.close()


def test_stage12_policy_projection_is_already_converged_after_reopen(tmp_path):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    _stage12_database(root).close()
    io = ScriptedWizardIO(["再接続を確認する方針", "", ""])
    answers = wizard.Answers("Asia/Tokyo", "JPY", ["EURUSD"], 1_000_000, timeframe="30m")

    wizard._stage12(wizard._Session(root, io, deps), answers)

    conn = connect(root / "data" / "agentic.db")
    assert reconcile_policy_file(conn, wizard.directives_path(root)) is False
    conn.close()


def test_stage12_uses_its_single_connection_for_policy_and_backlog(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    _stage12_database(root).close()
    io = ScriptedWizardIO(["", ""])
    answers = wizard.Answers("Asia/Tokyo", "JPY", ["EURUSD"], 1_000_000, timeframe="30m")
    real_connect = wizard.connect
    real_connect_readonly = wizard.connect_readonly
    calls = []
    readonly_calls = []

    def record_connect(path):
        calls.append(path)
        return real_connect(path)

    def record_readonly(path):
        readonly_calls.append(path)
        return real_connect_readonly(path)

    monkeypatch.setattr(wizard, "connect", record_connect)
    monkeypatch.setattr(wizard, "connect_readonly", record_readonly)
    wizard._stage12(wizard._Session(root, io, deps), answers)

    assert calls == [root / "data" / "agentic.db"]
    assert readonly_calls == []


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
    run.on(__import__("sys").executable, "main.py", "check", "backend", "--backend", "claude", returncode=1)
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
    conn = connect(root / "data" / "agentic.db")
    assert [row["directive"] for row in conn.execute("SELECT directive FROM ops_policies")] == ["損切りを広げない"]
    assert inspect_policy_file(conn, wizard.directives_path(root)).status == "ok"
    conn.close()


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


def _stage12_run(tmp_path, inputs, *, prepare=None):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    _stage12_database(root).close()
    if prepare is not None:
        prepare(root)
    io = ScriptedWizardIO(inputs)
    answers = wizard.Answers("Asia/Tokyo", "JPY", ["EURUSD"], 1_000_000, timeframe="30m")
    wizard._stage12(wizard._Session(root, io, deps), answers)
    return root, io


def _policy_rows(root):
    conn = connect(root / "data" / "agentic.db")
    try:
        return conn.execute("SELECT COUNT(*) FROM ops_policies").fetchone()[0]
    finally:
        conn.close()


def _malform(root):
    path = wizard.directives_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("<!-- afx:managed-policy id=1 -->\n- 孤立\n", encoding="utf-8")


def test_stage12_malformed_projection_stops_before_insert(tmp_path):
    root, io = _stage12_run(tmp_path, ["新しい方針", "should-not-be-asked"], prepare=_malform)

    assert "警告: 方針文を書けませんでした (" in io.text
    assert "記録しましたが" not in io.text
    assert _policy_rows(root) == 0
    assert io.asked == ["方針文: "]


@pytest.mark.parametrize("text", ["x" * 4001, "   \t "])
def test_stage12_rejects_oversized_or_blank_body_before_insert(tmp_path, text):
    from agentic_fx.commands import add_policy_directive
    from agentic_fx.activity import ActivityLog
    from datetime import datetime
    root = make_root(tmp_path)
    _stage12_database(root).close()
    conn = connect(root / "data" / "agentic.db")
    with pytest.raises(ValueError):
        add_policy_directive(conn, wizard.directives_path(root), ActivityLog(root / "logs" / "activity.log"),
                             text, datetime(2026, 10, 25))
    assert conn.execute("SELECT COUNT(*) FROM ops_policies").fetchone()[0] == 0
    conn.close()


def test_stage12_oversized_body_reports_first_sentence_and_adds_no_row(tmp_path):
    root, io = _stage12_run(tmp_path, ["x" * 4001, "should-not-be-asked"])

    assert "警告: 方針文を書けませんでした (" in io.text
    assert _policy_rows(root) == 0
    assert io.asked == ["方針文: "]


def test_stage12_activity_failure_keeps_row_and_reflected_file(tmp_path, monkeypatch):
    from agentic_fx.activity import ActivityLog

    def fail_write(self, *args, **kwargs):
        raise OSError("log full")

    monkeypatch.setattr(ActivityLog, "write", fail_write)
    root, io = _stage12_run(tmp_path, ["活動ログが落ちる方針", "should-not-be-asked"])

    assert "警告: 方針文を書けませんでした (OSError: log full)。" in io.text
    assert "記録しましたが" not in io.text
    conn = connect(root / "data" / "agentic.db")
    assert conn.execute("SELECT COUNT(*) FROM ops_policies").fetchone()[0] == 1
    assert inspect_policy_file(conn, wizard.directives_path(root)).status == "ok"
    assert "活動ログが落ちる方針" in wizard.directives_path(root).read_text(encoding="utf-8")
    conn.close()


def test_stage12_connect_failure_prints_fixed_sentence_and_returns(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO([])
    answers = wizard.Answers("Asia/Tokyo", "JPY", ["EURUSD"], 1_000_000, timeframe="30m")

    def fail_connect(path):
        raise sqlite3.OperationalError("unable to open\ndatabase file")

    monkeypatch.setattr(wizard, "connect", fail_connect)
    assert wizard._stage12(wizard._Session(root, io, deps), answers) is None

    lines = io.text.splitlines()
    heading = lines.index("[段 12 方針文と最初の課題]")
    assert lines[heading + 1] == (
        "警告: データベースを開けませんでした (OperationalError: unable to open database file)。"
        "方針文と最初の課題はあとから afx ctl policy add / afx ctl backlog add で足せます。")
    assert io.asked == []


def test_stage12_duplicate_is_detected_by_database_exact_match(tmp_path):
    def seed_row_only(root):
        conn = connect(root / "data" / "agentic.db")
        from agentic_fx.policy_projection import insert_policy
        insert_policy(conn, "DB にだけある方針", request_id="seed", now="2026-10-25T00:00:00+00:00")
        conn.close()

    root, io = _stage12_run(tmp_path, ["DB にだけある方針", "", ""], prepare=seed_row_only)

    assert "方針文は追加済みです。" in io.text
    assert _policy_rows(root) == 1


def test_stage12_database_duplicate_after_file_block_removed(tmp_path):
    root, io = _stage12_run(tmp_path, ["同じ方針", "", ""])
    path = wizard.directives_path(root)
    path.write_text("", encoding="utf-8")
    deps, *_ = make_deps(tmp_path)
    io2 = ScriptedWizardIO(["同じ方針", "", ""])
    wizard._stage12(wizard._Session(root, io2, deps),
                    wizard.Answers("Asia/Tokyo", "JPY", ["EURUSD"], 1_000_000, timeframe="30m"))

    assert "方針文は追加済みです。" in io2.text
    assert _policy_rows(root) == 1


@pytest.mark.parametrize("error", [ValueError("bad"), commands_module.ProjectionReconcileBlocked("blocked"),
                                   commands_module.MigrationRecordUnknown("unknown")])
def test_stage12_pre_insert_error_types_map_to_first_sentence(tmp_path, monkeypatch, error):
    def fail(conn, policy_path, activity, text, now):
        raise error

    monkeypatch.setattr(wizard, "add_policy_directive", fail)
    _root, io = _stage12_run(tmp_path, ["方針", "should-not-be-asked"])

    assert f"警告: 方針文を書けませんでした ({type(error).__name__}: {error})。" in io.text


def test_stage12_unreadable_directives_during_duplicate_check_reports_first_sentence(tmp_path):
    def make_directory(root):
        path = wizard.directives_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.mkdir()

    root, io = _stage12_run(tmp_path, ["新規方針", "should-not-be-asked"], prepare=make_directory)

    assert "警告: 方針文を書けませんでした (IsADirectoryError" in io.text
    assert io.asked == ["方針文: "]
    assert _policy_rows(root) == 0
