from __future__ import annotations

from datetime import datetime, timezone
import os
import threading
from types import SimpleNamespace

import pytest

from agentic_fx.setup.pending_steps import (
    PendingFacts, PendingStep, pending_steps, render_pending_step,
)
from agentic_fx.store.setup_record import SetupRecord, _safe_value, read_setup_record, write_setup_record


NOW = datetime(2026, 10, 12, 3, 4, tzinfo=timezone.utc)


def _record(**updates):
    data = {
        "version": 1, "completed_at": "2026-10-12T00:00:00Z", "mode": "interactive",
        "settings_schema_version": 1,
        "price": {"result": "ok", "at": "2026-10-12T00:00:00Z", "reason_code": "ok"},
        "llm_trade": {"result": "ok", "at": "2026-10-12T00:00:00Z", "reason_code": "ok"},
        "llm_improve": {"result": "ok", "at": "2026-10-12T00:00:00Z", "reason_code": "ok"},
        "bridge_plan": "connected", "history": {"source": "mt5"},
        "unit": "installed",
        "indicators": {"installed": 9}, "api": {"enabled": True, "result": "ok"},
        "ops": {}, "isolation": {"admitted": "yes"},
        "linger": {"state": "already_enabled"},
        "start": {"result": "started"},
    }
    data.update(updates)
    return SetupRecord(data)


def test_record_is_atomic_and_corrupt_is_fail_soft(tmp_path):
    write_setup_record(tmp_path, _record())
    loaded = read_setup_record(tmp_path)
    assert loaded.state == "ok"
    assert loaded.record.data["mode"] == "interactive"

    path = tmp_path / "data" / "state" / "setup.json"
    path.write_text("{not json", encoding="utf-8")
    assert read_setup_record(tmp_path).state == "corrupt"


def test_all_pending_conditions_are_derived_from_injected_facts():
    record = _record(
        llm_trade={"result": "failed", "at": "2026-10-12T00:00:00Z", "reason_code": "bin_not_found"},
        llm_improve={"result": "unverified", "at": "2026-10-12T00:00:00Z", "reason_code": "connect_error"},
        bridge_plan="planned", indicators={"installed": 6},
        history={"source": "none"}, api={"enabled": False, "result": "failed", "reason_code": "connect_error"},
        ops={"bot": {"result": "failed", "stage": "secret", "reason_code": "credentials_missing", "ready": "failed"}},
        isolation={"admitted": "no", "reason_code": "landlock_abi_too_old"},
        linger={"state": "declined"}, start={"result": "pending"},
    )
    facts = PendingFacts(record_state="ok", record=record, now=NOW,
                         settings={"improve_backend": "opencode", "llama_swap_verified": False,
                                   "eval_source": "none", "price_source": "yfinance"},
                         env_present_keys=frozenset(), required_env_keys=("DISCORD_WEBHOOK_URL",),
                         deployed_plugins=(("a", "indicator"),), history_counts={"USDJPY": 0},
                         approval_ids=(12,), landlock_available=False, service_active=False)
    codes = [step.code for step in pending_steps(facts)]
    assert codes == [
        "llm_unverified", "llm_unverified", "opencode_unverified", "bridge_not_connected",
        "env_key_missing", "indicators_missing", "no_strategy", "history_source_unconfigured", "history_missing",
        "approval_stalled", "landlock_unavailable", "yfinance_weekend_gap", "ops_incomplete",
        "isolation_not_admitted", "api_disabled", "api_start_failed", "linger_off", "start_pending",
        "ops_not_ready",
    ]


def test_old_landlock_abi_adds_a_distinct_improve_isolation_warning():
    record = _record(landlock={"result": "ok", "reason_code": "landlock_abi_too_old"})
    steps = pending_steps(PendingFacts(record_state="ok", record=record, now=NOW))

    step = next(step for step in steps if step.code == "improve_isolation_unavailable")
    assert step.severity == "warn"
    assert render_pending_step(step) == (
        "この kernel では改善ループの隔離が使えません (Landlock ABI 8 以上、Linux 6.18 以降が必要)。kernel を更新してください")


def test_unavailable_landlock_suppresses_the_old_abi_warning():
    record = _record(landlock={"result": "ok", "reason_code": "landlock_abi_too_old"})
    steps = pending_steps(PendingFacts(record_state="ok", record=record, now=NOW,
                                       landlock_available=False))

    assert [step.code for step in steps if "landlock" in step.code or "isolation" in step.code] == [
        "landlock_unavailable"]


def _real_settings(**backtest):
    from pathlib import Path
    from agentic_fx.config import load_settings
    base = load_settings(Path("config/settings.yaml.example"))
    return base.model_copy(update={
        "runner": base.runner.model_copy(update={
            "improve": base.runner.improve.model_copy(update={"backend": "opencode"})}),
        "improve": base.improve.model_copy(update={"llama_swap_verified": False}),
        "backtest": base.backtest.model_copy(update={"eval_source": "none", **backtest}),
        "datafeed": base.datafeed.model_copy(update={"primary": "yfinance"}),
    })


def test_pending_steps_uses_nested_settings_and_does_not_hide_zero_history():
    steps = pending_steps(PendingFacts(
        record_state="ok", record=_record(), settings=_real_settings(),
        history_counts={"USDJPY": 0, "EURUSD": 0}, service_active=True,
    ))
    codes = [step.code for step in steps]
    assert "opencode_unverified" in codes
    assert [code for code in codes if code.startswith("history_")] == [
        "history_source_unconfigured", "history_missing", "history_missing"]
    assert "start_pending" not in codes


@pytest.mark.parametrize("value", ["https:", "tmp.file", "secretvalue"])
def test_record_rejects_free_text_fragments(value, tmp_path):
    record = _record()
    record.data["price"]["reason_code"] = value
    with pytest.raises(ValueError):
        write_setup_record(tmp_path, record)


def test_record_accepts_utc_offsets_and_rejects_unknown_schema_keys(tmp_path):
    record = _record(completed_at="2026-10-12T00:00:00+00:00")
    record.data["price"]["at"] = "2026-10-12T00:00:00+00:00"
    write_setup_record(tmp_path, record)
    assert read_setup_record(tmp_path).state == "ok"
    record.data["completed_at"] = "2026-10-12T09:00:00+09:00"
    write_setup_record(tmp_path, record)
    record.data["unexpected"] = "value"
    with pytest.raises(ValueError):
        write_setup_record(tmp_path, record)


@pytest.mark.parametrize("field", ["completed_at", "price.at"])
def test_record_rejects_timestamps_without_timezone(field, tmp_path):
    record = _record()
    if field == "completed_at":
        record.data["completed_at"] = "2026-10-12T00:00:00"
    else:
        record.data["price"]["at"] = "2026-10-12T00:00:00"
    with pytest.raises(ValueError):
        write_setup_record(tmp_path, record)


@pytest.mark.parametrize("reason", ["invalid_response", "bridge_not_connected", "version_check_failed", "auth_unsupported", "server_time_unknown"])
def test_record_accepts_check_vocabulary_reasons(reason, tmp_path):
    record = _record()
    record.data["price"] = {"result": "failed", "at": "2026-10-12T00:00:00Z", "reason_code": reason}
    write_setup_record(tmp_path, record)
    assert read_setup_record(tmp_path).state == "ok"


def test_reason_codes_cover_the_vocabulary_named_in_the_design():
    import re
    from pathlib import Path
    from agentic_fx.store.setup_record import REASON_CODES
    text = Path("docs/superpowers/specs/2026-10-12-first-run-wizard-design.md").read_text(encoding="utf-8")
    start = text.index("### 段 3: 価格源")
    end = text.index("### 段 5: 判断足")
    stages = text[start:end]
    named = set(re.findall(r"`([a-z]+(?:_[a-z]+)+)`", stages))
    named |= set(re.findall(r"`([a-z]+(?:_[a-z]+)+)`", text[text.index("失敗理由は固定の分類"):][:400]))
    not_reasons = {"bridge_url", "context_limit", "llm_improve", "llm_trade", "ready_confirm_ticks",
                   "safe_error_text", "state_fn"}
    assert {"auth_unsupported", "sandbox_unavailable", "server_time_unknown", "bin_not_found"} <= named
    assert named - not_reasons <= REASON_CODES
    assert {"invalid_response", "bridge_not_connected", "version_check_failed"} <= REASON_CODES


def test_safe_value_rejects_paths_and_unknown_free_text_but_accepts_utc():
    assert _safe_value("2026-10-12T00:00:00+00:00")
    assert _safe_value("2026-10-12T00:00:00Z")
    assert not _safe_value("https://example.test")
    assert not _safe_value("/tmp/value")
    assert not _safe_value("free text")


def test_record_concurrent_writers_leave_a_complete_record(tmp_path):
    barrier = threading.Barrier(2)
    failures = []

    def write():
        try:
            barrier.wait()
            write_setup_record(tmp_path, _record())
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=write) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert failures == []
    assert read_setup_record(tmp_path).state == "ok"


def test_missing_record_only_reports_missing_and_healthy_setup_has_no_steps():
    assert [step.code for step in pending_steps(PendingFacts(record_state="missing", now=NOW))] == ["setup_record_missing"]
    facts = PendingFacts(record_state="ok", record=_record(), now=NOW,
                         settings={"eval_source": "mt5", "price_source": "mt5"},
                         deployed_plugins=tuple((str(i), "indicator") for i in range(9)) + (("s", "strategy"),),
                         history_counts={"USDJPY": 1}, landlock_available=True)
    assert pending_steps(facts) == []


@pytest.mark.parametrize("reason", ["bin_not_found", "credentials_missing"])
def test_unverified_llm_step_points_at_the_handbook_section_when_the_cli_is_the_cause(reason):
    rendered = render_pending_step(PendingStep("llm_unverified", "warn", NOW,
                                               {"target": "trade", "reason_code": reason}))
    assert f"trade の LLM は未確認です ({reason})" in rendered
    assert "docs/operations/first-run-acceptance.md の『前提: CLI の導入』" in rendered
    assert "`afx init`" in rendered


@pytest.mark.parametrize("reason", ["user_declined_probe", "model_list_unavailable", "deferred_by_user",
                                    "auth_unsupported", "connect_error", "unverified"])
def test_unverified_llm_step_for_other_causes_only_asks_to_rerun_init(reason):
    rendered = render_pending_step(PendingStep("llm_unverified", "warn", NOW,
                                               {"target": "trade", "reason_code": reason}))
    assert f"trade の LLM は未確認です ({reason})" in rendered
    assert "afx init` を再実行して確認してください" in rendered
    assert "docs/" not in rendered and "CLI の導入" not in rendered


def test_sensitive_text_never_reaches_renderer_and_unknown_is_forward_compatible():
    step = PendingStep("ops_incomplete", "action", NOW, {"feature": "https://secret.example", "stage": "/tmp/secret", "reason_code": "boom TOKEN=super-secret"})
    rendered = render_pending_step(step)
    assert "https://secret.example" not in rendered
    assert "boom TOKEN=super-secret" not in rendered
    assert "super-secret" not in rendered
    assert "unknown" in rendered
    assert render_pending_step(PendingStep("future_code", "bad", NOW, {"secret": "x"})) == "未知の手順 (code=future_code)。afx を更新してください"


def test_record_retries_short_writes_and_durability_order(tmp_path, monkeypatch):
    import agentic_fx.store.setup_record as record_module

    calls = []
    real_write, real_fsync, real_replace = os.write, os.fsync, os.replace

    def short_write(fd, data):
        calls.append("write")
        return real_write(fd, data[:max(1, len(data) // 2)])

    monkeypatch.setattr(record_module.os, "write", short_write)
    monkeypatch.setattr(record_module.os, "fsync", lambda fd: (calls.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(record_module.os, "replace", lambda src, dst: (calls.append("replace"), real_replace(src, dst))[1])
    write_setup_record(tmp_path, _record())
    assert calls.index("fsync") < calls.index("replace") < len(calls) - 1
    assert calls[-1] == "fsync"
    assert read_setup_record(tmp_path).state == "ok"


def test_status_omits_api_only_steps_and_limits_to_32():
    record = _record(api={"enabled": False, "result": "failed", "reason_code": "connect_error"})
    all_steps = pending_steps(PendingFacts(record_state="ok", record=record, now=NOW), for_status=False)
    status_steps = pending_steps(PendingFacts(record_state="ok", record=record, now=NOW), for_status=True)
    assert {step.code for step in all_steps} >= {"api_disabled", "api_start_failed"}
    assert not {step.code for step in status_steps} & {"api_disabled", "api_start_failed"}
    many = [PendingStep("llm_unverified", "warn", NOW, {"target": "trade"}) for _ in range(40)]
    assert len(pending_steps(PendingFacts(record_state="ok", record=_record(), now=NOW,
                                          extra_steps=many))) == 32


def test_record_accepts_the_dict_unit_and_rejects_unhashable_enum_values(tmp_path):
    write_setup_record(tmp_path, _record(unit={"installed": True, "launch": "direct"}))
    loaded = read_setup_record(tmp_path)
    assert loaded.state == "ok"
    assert loaded.record.data["unit"] == {"installed": True, "launch": "direct"}
    for bad in ({"mode": []}, {"mode": {}}, {"bridge_plan": []}, {"unit": []},
                {"linger": {"state": []}}, {"isolation": {"admitted": {}}},
                {"ops": {"x": {"result": []}}},
                {"api": {"enabled": True, "result": "failed", "reason_code": []}}):
        with pytest.raises(ValueError):
            write_setup_record(tmp_path, _record(**bad))


def test_unreadable_or_mistyped_record_reads_as_corrupt(tmp_path):
    path = tmp_path / "data" / "state" / "setup.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe\x80 not utf-8")
    assert read_setup_record(tmp_path).state == "corrupt"
    path.write_text('{"mode": []}', encoding="utf-8")
    assert read_setup_record(tmp_path).state == "corrupt"
    path.write_text("[1, 2]", encoding="utf-8")
    assert read_setup_record(tmp_path).state == "corrupt"


def test_record_reasons_include_every_check_reason():
    from agentic_fx.setup.checks import CheckReason
    from agentic_fx.store.setup_record import REASON_CODES

    assert {reason.value for reason in CheckReason} <= REASON_CODES


def _indicator_steps(record, deployed):
    steps = pending_steps(PendingFacts(
        record_state="ok", record=record, deployed_plugins=tuple(
            (f"i{n}", "indicator") for n in range(deployed)), service_active=True))
    return [step for step in steps if step.code == "indicators_missing"]


def test_indicator_target_follows_the_recorded_count():
    (step,) = _indicator_steps(_record(indicators={"installed": 3}), 2)
    assert dict(step.params) == {"installed": 2, "total": 3}
    assert _indicator_steps(_record(indicators={"installed": 0}), 0) == []
    (step,) = _indicator_steps(_record(), 4)
    assert dict(step.params) == {"installed": 4, "total": 9}
    no_record = pending_steps(PendingFacts(record_state="corrupt", record=None, service_active=True))
    assert [s.params.get("total") for s in no_record if s.code == "indicators_missing"] == [9]


def test_bool_is_not_a_number_in_the_renderer_or_the_shared_sanitizer():
    from agentic_fx.setup.pending_text import sanitize_param
    assert sanitize_param("int", True, 0) == 0
    assert sanitize_param("int", 7, 0) == 7
    rendered = render_pending_step(PendingStep("approval_stalled", "action", NOW, {"approval_id": True}))
    assert "True" not in rendered and "#0" in rendered


def test_linger_not_checked_is_a_valid_record_value_and_is_not_a_pending_step(tmp_path):
    record = _record(linger={"state": "not_checked"}, unit="installed")
    write_setup_record(tmp_path, record)
    assert read_setup_record(tmp_path).state == "ok"
    codes = [step.code for step in pending_steps(PendingFacts(
        record_state="ok", record=record, now=NOW, service_active=True))]
    assert "linger_off" not in codes
    for state in ("declined", "failed"):
        flagged = [step.code for step in pending_steps(PendingFacts(
            record_state="ok", record=_record(linger={"state": state}), now=NOW,
            service_active=True))]
        assert "linger_off" in flagged
