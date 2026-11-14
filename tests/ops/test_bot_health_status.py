"""status / afx status / afx ops bot status / afx ctl status の bot_health と alternate_push。"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx import status as offline_status
from agentic_fx.ops import bot_health, client
from agentic_fx.ops_setup.stages import run_ops_command

NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=timezone.utc).timestamp()
GOLDEN = json.loads(Path(__file__).resolve().parents[2].joinpath(
    "tests", "fixtures", "bot_health_contract.json").read_text(encoding="utf-8"))
VALID = next(case for case in GOLDEN["cases"] if case["name"] == "healthy")["record"]
SECRETS = (".".join(("MTIzNDU2Nzg5MDEyMzQ1Njc4", "GabCdE", "abcdefghijklmnopqrstuvwxyz0123456789")),
           "https://discord.com/api/webhooks/123456789012345678/secret-webhook-token",
           "123456789012345678")


def _stage(root: Path, feature: str, *, operation="enable", stage="enabled") -> None:
    directory = root / "data" / "ops"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{feature}.stage.json").write_text(json.dumps(
        {"feature": feature, "operation": operation, "stage": stage}))


def _snapshot(directory: Path, record: dict | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "gateway.snapshot").write_text(json.dumps(record or VALID))
    return directory


@pytest.fixture
def state(tmp_path, monkeypatch):
    base = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(base))
    return base / "agentic-fx" / "ops" / "bot"


def test_a_bot_that_is_not_enabled_is_not_configured_whatever_the_file_says(tmp_path, state):
    _snapshot(state)
    result = bot_health.collect(tmp_path, NOW)
    assert result["state"] == "not_configured" and result["interaction"] is False


@pytest.mark.parametrize("operation,stage", [("enable", "started"), ("disable", "disabled"),
                                              ("disable", "stopping-ops")])
def test_only_a_completed_enable_counts_as_configured(tmp_path, state, operation, stage):
    _stage(tmp_path, "bot", operation=operation, stage=stage)
    _snapshot(state)
    assert bot_health.collect(tmp_path, NOW)["state"] == "not_configured"


def test_an_enabled_bot_reads_the_snapshot_at_the_resolved_path(tmp_path, state):
    _stage(tmp_path, "bot")
    _snapshot(state)
    result = bot_health.collect(tmp_path, NOW)
    assert result["state"] == "healthy" and result["interaction"] is True


def test_an_enabled_bot_without_a_file_is_unknown(tmp_path, state):
    _stage(tmp_path, "bot")
    assert bot_health.collect(tmp_path, NOW)["state"] == "unknown"


def test_an_invalid_bot_stage_is_unknown_not_a_crash(tmp_path, state):
    (tmp_path / "data" / "ops").mkdir(parents=True)
    (tmp_path / "data" / "ops" / "bot.stage.json").write_text("{not json")
    assert bot_health.collect(tmp_path, NOW)["state"] == "unknown"


def test_a_daemon_with_a_different_xdg_reads_only_its_own_path(tmp_path, monkeypatch):
    """別の XDG の環境の bot が書いた file は探さない (daemon が解決した path だけを読む)。"""
    _stage(tmp_path, "bot")
    _snapshot(tmp_path / "other" / "agentic-fx" / "ops" / "bot")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "mine"))
    assert bot_health.collect(tmp_path, NOW)["state"] == "unknown"


def test_a_stale_valid_file_at_the_daemons_path_is_still_shown(tmp_path, state):
    """環境の不一致で古い有効 snapshot が残っていれば、その値に従う (unknown は保証しない)。"""
    _stage(tmp_path, "bot")
    old = dict(VALID, observed_at="2026-11-14T09:00:00Z")
    _snapshot(state, old)
    assert bot_health.collect(tmp_path, NOW)["state"] == "stale"
    perm = dict(old, terminal="permanent", terminal_code="token_invalid")
    _snapshot(state, perm)
    assert bot_health.collect(tmp_path, NOW)["state"] == "permanent-failure"


@pytest.mark.parametrize("operation,stage,want", [("enable", "enabled", "configured"),
                                                   ("enable", "started", "none"),
                                                   ("disable", "disabled", "none")])
def test_alternate_push_follows_the_notify_enable_state_only(tmp_path, state, operation, stage, want):
    _stage(tmp_path, "notify", operation=operation, stage=stage)
    assert bot_health.collect(tmp_path, NOW)["alternate_push"] == want


def test_alternate_push_is_none_without_a_stage_or_with_a_broken_one(tmp_path, state):
    assert bot_health.collect(tmp_path, NOW)["alternate_push"] == "none"
    (tmp_path / "data" / "ops").mkdir(parents=True)
    (tmp_path / "data" / "ops" / "notify.stage.json").write_text("{broken")
    assert bot_health.collect(tmp_path, NOW)["alternate_push"] == "none"


def test_alternate_push_never_reads_the_notify_secret_or_config(tmp_path, state, monkeypatch):
    _stage(tmp_path, "notify")
    opened = []
    real = os.open

    def spy(path, *args, **kwargs):
        opened.append(str(path))
        return real(path, *args, **kwargs)
    monkeypatch.setattr(os, "open", spy)
    real_open = open

    def spy_open(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr("builtins.open", spy_open)
    assert bot_health.collect(tmp_path, NOW)["alternate_push"] == "configured"
    assert not [p for p in opened if "secret" in p or "webhook" in p or ".config" in p]


def test_collect_never_raises_when_the_reader_blows_up(tmp_path, state, monkeypatch):
    _stage(tmp_path, "bot")
    monkeypatch.setattr(bot_health, "read_health", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    assert bot_health.collect(tmp_path, NOW)["state"] == "unknown"


def test_the_collected_value_has_only_fixed_non_secret_fields(tmp_path, state):
    _stage(tmp_path, "bot")
    _snapshot(state)
    result = bot_health.collect(tmp_path, NOW)
    assert set(result) == {"state", "observed_at", "gateway_state", "delivery_state",
                           "terminal_code", "interaction", "alternate_push"}
    text = json.dumps(result)
    assert not any(secret in text for secret in SECRETS)


# ---- 表示行

def test_lines_warn_when_alternate_push_is_none():
    assert bot_health.lines({"state": "healthy", "alternate_push": "none"}) == [
        "bot health: healthy alternate_push=none", "warning: alternate_push=none"]


def test_lines_have_no_warning_when_alternate_push_is_configured():
    assert bot_health.lines({"state": "healthy", "alternate_push": "configured"}) == [
        "bot health: healthy alternate_push=configured"]


def test_lines_never_echo_unexpected_values():
    shown = bot_health.lines({"state": SECRETS[0], "alternate_push": SECRETS[1]})
    assert shown == ["bot health: unknown alternate_push=none", "warning: alternate_push=none"]
    assert bot_health.lines(None) == [] and bot_health.lines("x") == []


# ---- afx status / afx ops bot status / afx ctl status

def test_afx_status_shows_the_health_line_and_the_warning(tmp_path, state, monkeypatch):
    monkeypatch.setattr(offline_status, "default_unit_dir", lambda: tmp_path / "unit")
    _stage(tmp_path, "bot")
    _snapshot(state, dict(VALID, observed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
    data = offline_status.collect_status(
        tmp_path, run=lambda *a, **k: type("R", (), {"returncode": 1, "stdout": ""})(), instance="free")
    text = offline_status.render_status(data, root=tmp_path)
    assert "bot health: healthy alternate_push=none" in text
    assert "warning: alternate_push=none" in text


def test_afx_status_with_notify_configured_has_no_warning(tmp_path, state, monkeypatch):
    monkeypatch.setattr(offline_status, "default_unit_dir", lambda: tmp_path / "unit")
    _stage(tmp_path, "notify")
    data = offline_status.collect_status(
        tmp_path, run=lambda *a, **k: type("R", (), {"returncode": 1, "stdout": ""})(), instance="free")
    text = offline_status.render_status(data, root=tmp_path)
    assert "bot health: not_configured alternate_push=configured" in text
    assert "alternate_push=none" not in text


def test_ops_bot_status_prints_the_health_line_and_the_warning(tmp_path, state, monkeypatch, capsys):
    monkeypatch.setattr("agentic_fx.ops_setup.systemd.Systemd.show",
                        lambda self, unit, *props: {"ActiveState": "active", "Result": "success"})
    _stage(tmp_path, "bot")
    assert run_ops_command(tmp_path, "status", "bot") == 0
    lines = capsys.readouterr().out.splitlines()
    assert "bot health: unknown alternate_push=none" in lines
    assert "warning: alternate_push=none" in lines


def test_ops_web_status_does_not_print_the_bot_health(tmp_path, state, monkeypatch, capsys):
    monkeypatch.setattr("agentic_fx.ops_setup.systemd.Systemd.show",
                        lambda self, unit, *props: {"ActiveState": "active", "Result": "success"})
    _stage(tmp_path, "web")
    run_ops_command(tmp_path, "status", "web")
    assert "bot health" not in capsys.readouterr().out


def test_ctl_status_prints_the_daemon_supplied_health_without_secrets():
    health = {"state": "stalled", "alternate_push": "none", "terminal_code": SECRETS[0],
              "observed_at": SECRETS[2]}
    text = client.format_status({"bot_health": health})
    assert "bot health: stalled alternate_push=none" in text
    assert "warning: alternate_push=none" in text
    assert not any(secret in text for secret in SECRETS)


def test_ctl_status_without_bot_health_is_unchanged():
    assert "bot health" not in client.format_status({})


# ---- daemon の /v1/status への配線

def test_the_daemons_status_provider_carries_bot_health_and_never_fails_on_it(tmp_path, state, monkeypatch):
    from agentic_fx.core.contracts import FixedClock
    from agentic_fx.runners.fake_runner import FakeRunner
    from agentic_fx.service import build_app
    from tests.store.test_rag import FakeEmbedding
    from tests.test_service_app import _init

    _init(tmp_path)
    root = tmp_path
    app = build_app(root, runner=FakeRunner([]),
                    clock=FixedClock(datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)),
                    embedding_fn=FakeEmbedding())
    try:
        status = app.ops._status_provider()
        assert status["bot_health"]["state"] == "not_configured"
        assert status["bot_health"]["alternate_push"] == "none"
        # 読取りが壊れても status は応答する
        monkeypatch.setattr(bot_health, "read_health",
                            lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
        _stage(root, "bot")
        assert app.ops._status_provider()["bot_health"]["state"] == "unknown"
    finally:
        app.close()
