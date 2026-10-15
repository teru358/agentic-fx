from __future__ import annotations

import pytest

from agentic_fx.config import load_settings_text
from agentic_fx.service import ensure_initialized
from agentic_fx.setup.wizard import run_wizard
from agentic_fx.store.setup_marker import setup_marker_present
from agentic_fx.store.setup_record import read_setup_record

from tests.setup.wizard_support import (
    Rules, ScriptedWizardIO, fresh_responder, make_deps, make_root,
)


def changed_keys(root):
    """example と書き出した settings の、値が違う行のキー名 (行頭の key) の集合。"""
    example = (root / "config" / "settings.yaml.example").read_text().splitlines()
    actual = (root / "config" / "settings.yaml").read_text().splitlines()
    assert len(example) == len(actual)
    return {actual[i].split(":")[0].strip() for i in range(len(actual)) if example[i] != actual[i]}


def test_fresh_root_with_a_connected_bridge_completes_with_only_the_answered_lines_changed(tmp_path):
    root = make_root(tmp_path)
    deps, calls, fetch, run = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder())
    assert run_wizard(root, io, deps) == 0, io.text
    settings = load_settings_text((root / "config" / "settings.yaml").read_text())
    assert settings.datafeed.primary == "mt5"
    assert settings.datafeed.mt5.enabled and not settings.datafeed.yfinance.enabled
    assert not settings.datafeed.twelvedata.enabled
    assert settings.backtest.eval_source == "mt5"
    assert changed_keys(root) == {"yfinance", "mt5", "eval_source", "base_interval", "enabled"}
    ensure_initialized(root)
    record = read_setup_record(root).record.data
    assert record["price"]["result"] == "ok" and record["bridge_plan"] == "connected"
    assert record["mode"] == "interactive" and record["linger"] == {"state": "not_checked"}
    assert record["indicators"] == {"installed": 0} and record["unit"] == "not_installed"
    assert not setup_marker_present(root)


def test_questions_are_asked_in_the_designed_order(tmp_path):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    answers = ["", "", "", "", "", "1", "", "", "", "", "", "", "", "", "", "", "", "n", "y", "", ""]
    io = ScriptedWizardIO(answers)
    assert run_wizard(root, io, deps) == 0, io.text
    io.assert_consumed()
    topics = ["読みました", "表示タイムゾーン (例", "口座通貨", "対象ペア", "初期残高", "価格源", "bridge の URL",
              "bridge 側で API キー", "LLM の構成", "取引判断のモデルの番号", "改善の backend",
              "改善のモデルの番号", "判断足", "backtest の基底足", "改善の周期", "改善の時刻", "最初の 1 本",
              "afx を常駐サービス", "この内容で書き込みますか", "方針文", "最初の課題"]
    assert len(io.asked) == len(topics)
    for prompt, topic in zip(io.asked, topics):
        assert topic in prompt, (topic, prompt)


def test_the_price_source_has_no_default_and_is_not_skipped_by_enter(tmp_path):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=Rules(("価格源", ("", "", "1")), ("書き込みますか", "n")))
    assert run_wizard(root, io, deps) == 1
    assert len(io.asked_with("価格源")) == 3
    assert "のどれかで答えてください" in io.text


def test_planned_bridge_branch_never_contacts_the_bridge(tmp_path):
    root = make_root(tmp_path)
    deps, calls, fetch, run = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(("価格源", "2")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert fetch.urls("/health") == [] and not [u for u, _ in fetch.calls if "/quote/" in u]
    record = read_setup_record(root).record.data
    assert record["price"]["result"] == "planned" and record["bridge_plan"] == "planned"
    settings = load_settings_text((root / "config" / "settings.yaml").read_text())
    assert settings.datafeed.primary == "mt5" and settings.backtest.eval_source == "mt5"
    assert "mt5_bridge/" in io.text and "BRIDGE_API_KEY" in io.text


def test_without_a_bridge_yfinance_is_confirmed_once_and_history_is_disabled(tmp_path):
    root = make_root(tmp_path)
    deps, calls, fetch, run = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(("価格源", ("3", "1"))))
    assert run_wizard(root, io, deps) == 0, io.text
    assert calls.yf == ["USDJPY"]
    settings = load_settings_text((root / "config" / "settings.yaml").read_text())
    assert settings.datafeed.primary == "yfinance"
    assert not settings.datafeed.mt5.enabled and not settings.datafeed.twelvedata.enabled
    assert settings.backtest.eval_source == "none"
    assert settings.backtest.base_interval == "1m"
    record = read_setup_record(root).record.data
    assert record["price"]["result"] == "ok" and record["bridge_plan"] == "unused"
    assert record["history"] == {"source": "none"}
    assert "週明け" in io.text


def test_declining_the_yfinance_probe_makes_no_request_and_is_recorded(tmp_path):
    root = make_root(tmp_path)
    deps, calls, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(("価格源", ("3", "1")), ("yfinance に", "n")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert calls.yf == []
    price = read_setup_record(root).record.data["price"]
    assert price["result"] == "unverified" and price["reason_code"] == "user_declined_probe"


def test_a_failing_yfinance_probe_does_not_stop_the_wizard(tmp_path):
    root = make_root(tmp_path)

    def broken(pair):
        raise RuntimeError("token=top-secret upstream failed")

    deps, *_ = make_deps(tmp_path, yf_quote=broken)
    io = ScriptedWizardIO(responder=fresh_responder(("価格源", ("3", "1"))))
    assert run_wizard(root, io, deps) == 0, io.text
    assert read_setup_record(root).record.data["price"]["result"] == "failed"
    assert "top-secret" not in io.text


def test_twelvedata_cannot_be_chosen_without_its_key_in_the_environment_or_env_file(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(("価格源", ("3", "2", "1"))))
    assert run_wizard(root, io, deps) == 0, io.text
    assert "TWELVEDATA_API_KEY が" in io.text
    settings = load_settings_text((root / "config" / "settings.yaml").read_text())
    assert settings.datafeed.primary == "yfinance"
    # キーがあれば選べる
    root2 = make_root(tmp_path / "second")
    deps2, *_ = make_deps(tmp_path / "second", environ={"TWELVEDATA_API_KEY": "x"})
    io2 = ScriptedWizardIO(responder=fresh_responder(("価格源", ("3", "2"))))
    assert run_wizard(root2, io2, deps2) == 0, io2.text
    monkeypatch.setenv("TWELVEDATA_API_KEY", "placeholder")
    settings2 = load_settings_text((root2 / "config" / "settings.yaml").read_text())
    assert settings2.datafeed.primary == "twelvedata" and settings2.datafeed.twelvedata.enabled


@pytest.mark.parametrize("health,status,expected", [
    ({"mt5_connected": False}, 200, "MT5 端末とつながっていません"),
    ({}, 401, "鍵が一致しません"),
    ({}, 500, "接続できません")])
def test_bridge_failures_are_explained_and_can_fall_back_to_planned(tmp_path, health, status, expected):
    root = make_root(tmp_path)
    deps, calls, fetch, run = make_deps(tmp_path)
    fetch.bridge_health = health
    fetch.bridge_status = status
    io = ScriptedWizardIO(responder=fresh_responder(("[r] 再試行", "2")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert expected in io.text
    record = read_setup_record(root).record.data
    assert record["price"]["result"] == "planned" and record["bridge_plan"] == "planned"


def test_bridge_url_with_credentials_is_refused_without_echoing_it(tmp_path):
    root = make_root(tmp_path)
    deps, calls, fetch, run = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(
        ("bridge の URL", ("http://user:SECRET-MARK-U@host:8812", ""))))
    assert run_wizard(root, io, deps) == 0, io.text
    assert "認証情報を入れないでください" in io.text
    assert "SECRET-MARK-U" not in io.text
    assert all("SECRET-MARK-U" not in url for url, _ in fetch.calls)


def test_retrying_after_a_bridge_failure_reaches_the_bridge_again(tmp_path):
    root = make_root(tmp_path)
    deps, calls, fetch, run = make_deps(tmp_path)
    fetch.bridge_health = {"mt5_connected": False}
    state = {"n": 0}
    original = fetch.__call__

    def responder(prompt):
        if "[r] 再試行" in prompt:
            fetch.bridge_health = {"mt5_connected": True}
            state["n"] += 1
            return "r"
        return fresh_responder()(prompt)

    io = ScriptedWizardIO(responder=responder)
    assert run_wizard(root, io, deps) == 0, io.text
    assert state["n"] == 1
    assert read_setup_record(root).record.data["price"]["result"] == "ok"
