"""`afx check` と起動前検査の分類 (fatal / warning)。"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest
import yaml

import agentic_fx.service as service_mod
from agentic_fx.entry import main
from agentic_fx.setup import preflight
from agentic_fx.setup.preflight import WARNING_CODES, Finding, classify, run_check
from agentic_fx.store.setup_record import SetupRecord, write_setup_record

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"
TRUE_BIN = shutil.which("true")


def _deep(d, u):
    for k, v in u.items():
        if isinstance(v, dict) and isinstance(d.get(k), dict):
            _deep(d[k], v)
        else:
            d[k] = v


def make_root(tmp_path, **overrides) -> Path:
    (tmp_path / "config").mkdir(exist_ok=True)
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    _deep(raw, overrides)
    (tmp_path / "config" / "settings.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    return tmp_path


def record(**updates) -> SetupRecord:
    data = {
        "version": 1, "completed_at": "2026-10-12T00:00:00Z", "mode": "interactive",
        "settings_schema_version": 1,
        "price": {"result": "ok"}, "llm_trade": {"result": "ok"}, "llm_improve": {"result": "ok"},
        "bridge_plan": "connected", "history": {"source": "mt5"}, "indicators": {"installed": 9},
        "unit": "installed", "api": {"enabled": True, "result": "ok"}, "ops": {},
        "isolation": {"admitted": "yes"}, "linger": {"state": "already_enabled"},
        "start": {"result": "started"},
    }
    data.update(updates)
    return SetupRecord(data)


def claude_runner(bin_path: str, creds: str) -> dict:
    return {"runner": {"trade": {"backend": "claude", "model": "m"},
                       "improve": {"backend": "claude", "model": "m"},
                       "claude": {"bin": bin_path, "credentials_file": creds}}}


_REAL_SECRET_CHECK = service_mod._check_service_initial_env_has_no_secrets


def set_initial_env(monkeypatch, names):
    def check(settings, **kwargs):
        return _REAL_SECRET_CHECK(settings, read_initial_env_names=lambda: set(names), **kwargs)

    monkeypatch.setattr(service_mod, "_check_service_initial_env_has_no_secrets", check)


@pytest.fixture(autouse=True)
def clean_initial_env(monkeypatch):
    set_initial_env(monkeypatch, {"HOME", "PATH"})


def test_example_alone_has_no_fatal_and_no_warning(tmp_path):
    report = run_check(make_root(tmp_path))
    assert report.fatal == [] and report.warning == []


# --- fatal 4 種 (settings 構文・mission ceiling・ELF・秘密 env) + cache retention ---

def test_fatal_broken_settings_syntax(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text("pairs: [\n", encoding="utf-8")
    assert run_check(tmp_path).fatal == ["settings_invalid"]


def test_fatal_mission_ceiling_violation(tmp_path):
    root = make_root(tmp_path, llama_swap={"timeout_sec": 5000})
    assert run_check(root).fatal == ["mission_ceiling_violation"]


def test_fatal_cache_retention_inconsistent(tmp_path):
    root = make_root(tmp_path, datafeed={"cache_retention_days": 1})
    assert "cache_retention_inconsistent" in run_check(root).fatal


def test_fatal_codex_not_elf(tmp_path):
    wrapper = tmp_path / "codex.js"
    wrapper.write_text("#!/bin/sh\n")
    wrapper.chmod(0o755)
    root = make_root(tmp_path, runner={"improve": {"backend": "codex", "model": "m"},
                                       "codex": {"bin": str(wrapper)}})
    assert run_check(root).fatal == ["cli_not_elf"]


def test_fatal_secret_env_in_initial_environment(tmp_path, monkeypatch):
    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    root = make_root(tmp_path, **claude_runner(TRUE_BIN, str(creds)))
    set_initial_env(monkeypatch, {"HOME", "MY_API_KEY"})
    assert "secret_env_present" in run_check(root, unit_env=True).fatal


def test_manual_check_does_not_fail_on_a_secret_in_the_shell_env(tmp_path, monkeypatch, capsys):
    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    root = make_root(tmp_path, **claude_runner(TRUE_BIN, str(creds)))
    set_initial_env(monkeypatch, {"HOME", "MY_API_KEY"})
    monkeypatch.chdir(root)
    assert main(["check"]) == 0
    out = capsys.readouterr().out
    assert "secret_env_present" not in out and "info:" in out and "unit には渡りません" in out
    assert "MY_API_KEY" not in out


def test_check_in_the_unit_environment_fails_on_a_secret_env(tmp_path, monkeypatch, capsys):
    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    root = make_root(tmp_path, **claude_runner(TRUE_BIN, str(creds)))
    set_initial_env(monkeypatch, {"HOME", "MY_API_KEY"})
    monkeypatch.chdir(root)
    assert main(["check", "--unit-env"]) == 1
    assert "secret_env_present" in capsys.readouterr().out


# --- warning 5 種 ---

def test_warning_bin_not_found(tmp_path):
    root = make_root(tmp_path, **claude_runner(str(tmp_path / "no-claude"), str(tmp_path / "c")))
    report = run_check(root)
    assert report.fatal == [] and report.warning == ["bin_not_found"]


def test_warning_credentials_missing(tmp_path):
    root = make_root(tmp_path, **claude_runner(TRUE_BIN, str(tmp_path / "absent.json")))
    report = run_check(root)
    assert report.fatal == [] and report.warning == ["credentials_missing"]


def test_warning_version_check_failed(tmp_path):
    failing = tmp_path / "claude"
    failing.write_text("#!/bin/sh\nexit 1\n")
    failing.chmod(0o755)
    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    root = make_root(tmp_path, **claude_runner(str(failing), str(creds)))
    report = run_check(root)
    assert report.fatal == [] and report.warning == ["version_check_failed"]


@pytest.mark.parametrize("reason", ["deferred_by_user", "user_declined_probe"])
def test_warning_from_unverified_llm_record(tmp_path, reason):
    root = make_root(tmp_path)
    write_setup_record(root, record(llm_trade={"result": "unverified", "reason_code": reason}))
    report = run_check(root)
    assert report.fatal == [] and report.warning == [reason]


@pytest.mark.parametrize("reason", ["bin_not_found", "credentials_missing", "version_check_failed",
                                    "model_not_found", "connect_error"])
def test_past_failure_reasons_in_the_record_are_not_warnings(tmp_path, reason):
    root = make_root(tmp_path)
    write_setup_record(root, record(llm_trade={"result": "unverified", "reason_code": reason},
                                    llm_improve={"result": "failed", "reason_code": reason}))
    report = run_check(root)
    assert report.fatal == [] and report.warning == []


def test_recorded_missing_bin_does_not_warn_when_the_bin_exists_now(tmp_path):
    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    root = make_root(tmp_path, **claude_runner(TRUE_BIN, str(creds)))
    write_setup_record(root, record(
        llm_trade={"result": "unverified", "reason_code": "bin_not_found"},
        llm_improve={"result": "unverified", "reason_code": "bin_not_found"}))
    report = run_check(root)
    assert report.fatal == [] and report.warning == []


def test_recorded_deferral_is_dropped_for_a_backend_that_checks_ok_now(tmp_path):
    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    root = make_root(tmp_path, **claude_runner(TRUE_BIN, str(creds)))
    write_setup_record(root, record(
        llm_trade={"result": "unverified", "reason_code": "deferred_by_user"}))
    assert run_check(root).warning == []


def test_recorded_deferral_stays_for_a_backend_that_does_not_check_ok(tmp_path):
    root = make_root(tmp_path, **claude_runner(str(tmp_path / "no-claude"), str(tmp_path / "c")))
    write_setup_record(root, record(
        llm_trade={"result": "unverified", "reason_code": "deferred_by_user"}))
    assert sorted(run_check(root).warning) == ["bin_not_found", "deferred_by_user"]


def test_deferred_llm_keeps_example_model_names_and_settings_validate(tmp_path):
    root = make_root(tmp_path)
    before = (root / "config" / "settings.yaml").read_bytes()
    write_setup_record(root, record(
        llm_trade={"result": "unverified", "reason_code": "deferred_by_user"},
        llm_improve={"result": "unverified", "reason_code": "deferred_by_user"}))
    report = run_check(root)
    assert (root / "config" / "settings.yaml").read_bytes() == before
    assert report.warning == ["deferred_by_user"] and report.fatal == []


# --- 分類は 1 箇所 ---

def test_is_warning_is_the_function_used_by_service_and_check():
    assert service_mod.is_warning is preflight.is_warning
    assert preflight.is_warning("bin_not_found") and not preflight.is_warning("cli_not_elf")
    assert not preflight.is_warning("something_new")


def test_classify_splits_codes_and_unknown_is_fatal():
    fatal, warning = classify([Finding("bin_not_found"), Finding("cli_not_elf"),
                               Finding("something_new"), Finding("bin_not_found")])
    assert fatal == ["cli_not_elf", "something_new"]
    assert warning == ["bin_not_found"]


@pytest.mark.parametrize("code", sorted(preflight.WARNING_CODES))
def test_no_code_is_in_both_tables(code):
    assert code not in preflight.FATAL_CODES


# --- 終了コード・出力 ---

def test_check_command_returns_zero_for_warning_only(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path, **claude_runner(str(tmp_path / "no-claude"), str(tmp_path / "c")))
    monkeypatch.chdir(root)
    assert main(["check"]) == 0
    assert "warning: bin_not_found" in capsys.readouterr().out


def test_check_command_returns_zero_for_version_check_failure(tmp_path, monkeypatch, capsys):
    failing = tmp_path / "claude"
    failing.write_text("#!/bin/sh\nexit 1\n")
    failing.chmod(0o755)
    root = make_root(tmp_path, **claude_runner(str(failing), str(tmp_path / "c")))
    monkeypatch.chdir(root)
    assert main(["check"]) == 0
    assert "warning: version_check_failed" in capsys.readouterr().out


def test_check_command_returns_nonzero_for_fatal(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path, llama_swap={"timeout_sec": 5000})
    monkeypatch.chdir(root)
    assert main(["check"]) == 1
    assert "fatal: mission_ceiling_violation" in capsys.readouterr().out


def test_check_json_carries_the_report(tmp_path, monkeypatch, capsys):
    import json

    root = make_root(tmp_path, llama_swap={"timeout_sec": 5000},
                     **claude_runner(str(tmp_path / "no-claude"), str(tmp_path / "c")))
    monkeypatch.chdir(root)
    assert main(["check", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data == {"fatal": ["mission_ceiling_violation"], "warning": ["bin_not_found"]}


# --- 読み取りのみ ---

def _snapshot(root: Path):
    return sorted((str(p.relative_to(root)), p.stat().st_mtime_ns)
                  for p in root.rglob("*"))


def test_check_does_not_write_data_or_lock(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    data = root / "data" / "state"
    data.mkdir(parents=True)
    write_setup_record(root, record(llm_trade={"result": "unverified", "reason_code": "deferred_by_user"}))
    before = _snapshot(root)
    monkeypatch.chdir(root)
    assert main(["check"]) == 0
    assert _snapshot(root) == before
    assert not (root / "data" / "instance.lock").exists()
    assert not (root / "data" / "agentic.db").exists()


# --- service の起動前検査 (結合) ---

def test_service_start_checks_do_not_refuse_on_warning_and_status_shows_unverified(tmp_path, monkeypatch):
    from agentic_fx.core.contracts import FixedClock
    from agentic_fx.runners.fake_runner import FakeRunner
    from agentic_fx.service import build_app, build_splash
    from tests.test_service_app import NOW, _root_with_settings

    root = _root_with_settings(tmp_path, **claude_runner(str(tmp_path / "no-claude"), str(tmp_path / "c")))
    app = build_app(root, runner=FakeRunner([]), clock=FixedClock(NOW))
    try:
        # bin は解決できないまま (mission は実行時に fail closed で止まる)
        assert app.settings.runner.claude.bin == str(tmp_path / "no-claude")
        write_setup_record(root, record(
            llm_trade={"result": "unverified", "reason_code": "bin_not_found"}))
        assert "LLM は未確認です (bin_not_found)" in build_splash(app)
    finally:
        app.close()


def test_service_start_still_refuses_on_fatal(tmp_path):
    from agentic_fx.core.contracts import FixedClock
    from agentic_fx.runners.fake_runner import FakeRunner
    from agentic_fx.service import build_app
    from tests.test_service_app import NOW, _root_with_settings

    wrapper = tmp_path / "codex.js"
    wrapper.write_text("#!/bin/sh\n")
    wrapper.chmod(0o755)
    root = _root_with_settings(tmp_path, runner={"improve": {"backend": "codex", "model": "m"},
                                                  "codex": {"bin": str(wrapper)}})
    with pytest.raises(RuntimeError, match="ELF"):
        build_app(root, runner=FakeRunner([]), clock=FixedClock(NOW))


def test_check_cli_backend_returns_settings_and_logs_on_warning(tmp_path, caplog):
    from agentic_fx.config import load_settings

    root = make_root(tmp_path, **claude_runner(str(tmp_path / "no-claude"), str(tmp_path / "c")))
    settings = load_settings(root / "config" / "settings.yaml")
    findings: list[Finding] = []
    caplog.set_level("WARNING", logger="agentic_fx.service")
    out = service_mod._check_cli_backend(settings, which="trade", findings=findings)
    assert out is settings
    assert [f.code for f in findings] == ["bin_not_found"]
    assert "bin_not_found" in caplog.text


# --- 起動前検査の一覧は service と check で同じ ---

def _recording_checks(monkeypatch):
    calls = []

    def wrap(check):
        def recorded(settings, ctx):
            calls.append(check)
            return settings
        return recorded

    monkeypatch.setattr(service_mod, "STARTUP_CHECKS",
                        tuple(wrap(check) for check in service_mod.STARTUP_CHECKS))
    return calls


def test_service_and_check_run_the_same_startup_checks(tmp_path, monkeypatch):
    from agentic_fx.core.contracts import FixedClock
    from agentic_fx.runners.fake_runner import FakeRunner
    from tests.test_service_app import NOW, _root_with_settings

    original = service_mod.STARTUP_CHECKS
    calls = _recording_checks(monkeypatch)
    (tmp_path / "svc").mkdir()
    (tmp_path / "chk").mkdir()
    app = service_mod.build_app(_root_with_settings(tmp_path / "svc"), runner=FakeRunner([]),
                                clock=FixedClock(NOW))
    app.close()
    service_calls, calls[:] = list(calls), []
    run_check(make_root(tmp_path / "chk"))
    check_calls = list(calls)
    assert len(service_calls) == len(original)
    assert all(a is b for a, b in zip(service_calls, original))
    assert len(check_calls) == len(service_calls)
    assert all(a is b for a, b in zip(check_calls, service_calls))


def test_check_shows_the_reason_for_an_empty_pairs_list(tmp_path):
    report = run_check(make_root(tmp_path, pairs=[]))
    assert report.fatal == ["pairs_empty"]
    assert "pairs:" in report.messages["pairs_empty"]
    assert "pairs_empty: " in preflight.format_report(report)


def test_pairs_of_the_wrong_type_is_not_reported_as_empty(tmp_path):
    report = run_check(make_root(tmp_path, pairs="EURUSD"))
    assert report.fatal == ["settings_invalid"]
    assert "pairs" in report.messages["settings_invalid"]
    assert "空" not in report.messages["settings_invalid"]


def test_check_shows_the_reason_for_an_unknown_primary_source(tmp_path):
    report = run_check(make_root(tmp_path, datafeed={"primary": "typo-source"}))
    assert report.fatal == ["datafeed_primary_unknown"]
    assert "datafeed.primary" in report.messages["datafeed_primary_unknown"]
    assert "fatal: datafeed_primary_unknown: " in preflight.format_report(report)


def test_check_reports_hard_plugin_retention_shortfall(tmp_path, monkeypatch):
    def refuse(settings, plugins, activity):
        raise preflight.PreflightFailure("plugin_retention_short", "短い")

    monkeypatch.setattr(service_mod, "_startup_plugin_dispositions", refuse)
    report = run_check(make_root(tmp_path))
    assert report.fatal == ["plugin_retention_short"]
    assert report.messages["plugin_retention_short"].endswith("短い")


def test_check_feeds_the_approved_plugins_in_the_database_to_the_retention_check(tmp_path, monkeypatch):
    from agentic_fx.store.db import connect, init_db
    from agentic_fx.tools import plugin_loader

    root = make_root(tmp_path)
    (root / "data").mkdir()
    conn = connect(root / "data" / "agentic.db")
    init_db(conn)
    conn.close()
    sentinel = object()
    opened = []

    class Inventory:
        metas = (sentinel,)

    class Result:
        inventory = Inventory()

    def approved(conn, plugins_dir, *, settings):
        opened.append(conn.execute("PRAGMA query_only").fetchone()[0])
        try:
            conn.execute("CREATE TABLE should_not_exist (x)")
        except Exception as exc:  # noqa: BLE001
            opened.append(type(exc).__name__)
        return Result()

    seen = []

    def spy(settings, plugins, activity):
        seen.append(list(plugins))
        raise preflight.PreflightFailure("plugin_retention_short", "短い")

    monkeypatch.setattr(plugin_loader, "approved_plugins", approved)
    monkeypatch.setattr(service_mod, "_startup_plugin_dispositions", spy)
    report = run_check(root)
    assert seen == [[sentinel]]
    assert report.fatal == ["plugin_retention_short"]
    assert opened[-1] == "OperationalError"


def test_check_without_a_database_checks_only_the_core_requirements(tmp_path, monkeypatch):
    seen = []
    real = service_mod._startup_plugin_dispositions

    def spy(settings, plugins, activity):
        seen.append(list(plugins))
        return real(settings, plugins, activity)

    monkeypatch.setattr(service_mod, "_startup_plugin_dispositions", spy)
    run_check(make_root(tmp_path))
    assert seen == [[]]
    assert not (tmp_path / "data").exists()


def test_corrupt_database_is_fatal_not_silently_skipped(tmp_path):
    root = make_root(tmp_path)
    (root / "data").mkdir()
    (root / "plugins").mkdir()
    (root / "data" / "agentic.db").write_bytes(b"this is not a sqlite database" * 50)
    report = run_check(root)
    assert "db_unreadable" in report.fatal
    assert "this is not" not in report.messages["db_unreadable"]


def test_database_with_an_old_schema_is_a_warning_and_does_not_stop_the_unit(tmp_path):
    import sqlite3

    root = make_root(tmp_path)
    (root / "data").mkdir()
    (root / "plugins").mkdir()
    conn = sqlite3.connect(root / "data" / "agentic.db")
    conn.execute("CREATE TABLE unrelated (x)")
    conn.commit()
    conn.close()
    report = run_check(root)
    assert report.fatal == []
    assert "plugin_list_unavailable" in report.warning
    assert "migration" in report.messages["plugin_list_unavailable"]


def test_database_that_cannot_be_read_for_a_reason_other_than_schema_is_fatal(
        tmp_path, monkeypatch):
    import sqlite3

    from agentic_fx.tools import plugin_loader

    root = make_root(tmp_path)
    (root / "data").mkdir()
    (root / "plugins").mkdir()
    conn = sqlite3.connect(root / "data" / "agentic.db")
    conn.execute("CREATE TABLE unrelated (x)")
    conn.commit()
    conn.close()

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(plugin_loader, "approved_plugins", locked)
    report = run_check(root)
    assert "db_unreadable" in report.fatal
    assert "plugin_list_unavailable" not in report.warning


def test_all_failures_after_the_bin_resolves_are_reported_together(tmp_path, monkeypatch):
    root = make_root(tmp_path, **claude_runner(TRUE_BIN, str(tmp_path / "absent.json")))

    def bad_version(bin_path):
        raise preflight.PreflightFailure("version_check_failed", "boom")

    monkeypatch.setattr(service_mod, "_check_cli_version", bad_version)
    report = run_check(root)
    assert report.fatal == []
    assert set(report.warning) == {"version_check_failed", "credentials_missing"}


def test_check_and_service_loop_over_the_same_checks_tuple(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(service_mod, "STARTUP_CHECKS",
                        (lambda settings, ctx: seen.append("x") or settings,))
    run_check(make_root(tmp_path))
    assert seen == ["x"]


def test_unexpected_exception_is_fatal_with_only_the_type_name(tmp_path, monkeypatch):
    def deny(bin_value, *, require_elf):
        raise PermissionError("/secret/path/claude")

    monkeypatch.setattr(service_mod, "_resolve_cli_bin", deny)
    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    root = make_root(tmp_path, **claude_runner(TRUE_BIN, str(creds)))
    report = run_check(root)
    assert report.fatal == ["startup_check_failed"]
    assert "PermissionError" in report.messages["startup_check_failed"]
    assert "/secret/path" not in report.messages["startup_check_failed"]


def test_one_failing_check_does_not_hide_the_others(tmp_path, monkeypatch):
    monkeypatch.setattr(service_mod, "_resolve_cli_bin",
                        lambda *a, **k: (_ for _ in ()).throw(PermissionError("x")))
    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    root = make_root(tmp_path, llama_swap={"timeout_sec": 5000}, **claude_runner(TRUE_BIN, str(creds)))
    assert set(run_check(root).fatal) == {"mission_ceiling_violation", "startup_check_failed"}


def test_version_is_checked_for_each_of_trade_and_improve(tmp_path, monkeypatch):
    runs = []
    real = service_mod._check_cli_version
    monkeypatch.setattr(service_mod, "_check_cli_version", lambda p: runs.append(p) or real(p))
    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    root = make_root(tmp_path, **claude_runner(TRUE_BIN, str(creds)))
    assert run_check(root).warning == []
    assert len(runs) == 2


def test_cli_check_results_are_cached_per_backend_and_which(tmp_path):
    from agentic_fx.config import load_settings

    root = make_root(tmp_path, **claude_runner(str(tmp_path / "no-claude"), str(tmp_path / "c")))
    settings = load_settings(root / "config" / "settings.yaml")
    findings: list[Finding] = []
    verified: dict = {}
    for which in ("trade", "improve"):
        service_mod._check_cli_backend(settings, which=which, findings=findings, verified=verified)
    assert set(verified) == {("claude", "trade"), ("claude", "improve")}
    assert [f.code for f in findings] == ["bin_not_found", "bin_not_found"]


def test_check_hands_the_resolved_settings_to_the_following_checks(tmp_path, monkeypatch):
    seen = []

    def first(settings, ctx):
        return settings.model_copy(update={"pairs": ["SENTINEL"]})

    def second(settings, ctx):
        seen.append(list(settings.pairs))
        return settings

    monkeypatch.setattr(service_mod, "STARTUP_CHECKS", (first, second))
    run_check(make_root(tmp_path))
    assert seen == [["SENTINEL"]]


def test_shell_secret_note_checks_every_backend_with_its_own_label(tmp_path, monkeypatch):
    from agentic_fx.config import load_settings

    root = make_root(tmp_path, runner={"trade": {"backend": "claude", "model": "m"},
                                       "improve": {"backend": "codex", "model": "m"}})
    settings = load_settings(root / "config" / "settings.yaml")
    calls = []

    def check(settings, *, which, backend):
        calls.append((which, backend))

    monkeypatch.setattr(service_mod, "_check_service_initial_env_has_no_secrets", check)
    assert preflight._shell_secret_note(settings) == []
    assert calls == [("trade", "claude"), ("improve", "codex")]


def test_failing_version_is_reported_once_for_a_shared_backend(tmp_path):
    failing = tmp_path / "claude"
    failing.write_text("#!/bin/sh\nexit 1\n")
    failing.chmod(0o755)
    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    root = make_root(tmp_path, **claude_runner(str(failing), str(creds)))
    assert run_check(root).warning == ["version_check_failed"]


def test_resolved_bin_is_written_back_even_without_credentials(tmp_path):
    from agentic_fx.config import load_settings

    root = make_root(tmp_path, **claude_runner("true", str(tmp_path / "absent.json")))
    settings = load_settings(root / "config" / "settings.yaml")
    findings: list[Finding] = []
    out = service_mod._check_cli_backend(settings, which="trade", findings=findings)
    assert [f.code for f in findings] == ["credentials_missing"]
    assert out.runner.claude.bin == str(Path(TRUE_BIN).resolve())
    assert settings.runner.claude.bin == "true"


def test_resolved_bin_is_written_back_when_version_fails(tmp_path):
    from agentic_fx.config import load_settings

    failing = tmp_path / "claude"
    failing.write_text("#!/bin/sh\nexit 1\n")
    failing.chmod(0o755)
    root = make_root(tmp_path, **claude_runner(str(failing), str(tmp_path / "c")))
    settings = load_settings(root / "config" / "settings.yaml")
    out = service_mod._check_cli_backend(settings, which="trade", findings=[])
    assert out.runner.claude.bin == str(failing.resolve())


def test_startup_warnings_are_recorded_in_the_activity_log_by_code_only(tmp_path):
    from agentic_fx.core.contracts import FixedClock
    from agentic_fx.runners.fake_runner import FakeRunner
    from tests.test_service_app import NOW, _root_with_settings

    root = _root_with_settings(tmp_path, **claude_runner(str(tmp_path / "no-claude"), str(tmp_path / "c")))
    app = service_mod.build_app(root, runner=FakeRunner([]), clock=FixedClock(NOW))
    try:
        text = app.activity._path.read_text(encoding="utf-8")
    finally:
        app.close()
    lines = [line.split("\t") for line in text.splitlines() if "\tstartup_warning\t" in line]
    assert lines and all(cols[3] in WARNING_CODES for cols in lines)
    assert str(tmp_path / "no-claude") not in text
