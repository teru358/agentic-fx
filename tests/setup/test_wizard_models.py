from __future__ import annotations

import json

import pytest

from agentic_fx.config import load_settings_text
from agentic_fx.setup.wizard import CLAUDE_MODEL_ALIASES, run_wizard
from agentic_fx.store.setup_record import read_setup_record

from tests.setup.wizard_support import (
    ScriptedWizardIO, codex_model_list, fresh_responder, make_deps, make_executable, make_root,
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    return path


def settings_of(root):
    return load_settings_text((root / "config" / "settings.yaml").read_text())


def record_of(root):
    return read_setup_record(root).record.data


def claude_world(tmp_path, home):
    binary = make_executable(tmp_path / "bin" / "claude")
    credentials = home / ".claude" / ".credentials.json"
    credentials.parent.mkdir(parents=True, exist_ok=True)
    credentials.write_text("{}")
    deps, calls, fetch, run = make_deps(
        tmp_path, which=lambda name: str(binary) if name in ("claude", str(binary)) else None)
    fetch.models = {}
    run.on("node", "--version", stdout="v22\n").on(str(binary), "--version", stdout="1.0\n")
    return deps, run


def codex_world(tmp_path, home, listing):
    deps, run = claude_world(tmp_path, home)
    elf = make_executable(tmp_path / "vendor" / "codex", b"\x7fELF" + b"\0" * 16)
    auth = home / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True, exist_ok=True)
    auth.write_text("{}")
    run.on(str(elf), "--version", stdout="0.1\n")
    if listing is not None:
        run.on(str(elf), "app-server", **listing)
    return deps, run, elf


def test_claude_models_default_to_sonnet_for_both_runners(tmp_path, home):
    root = make_root(tmp_path)
    deps, run = claude_world(tmp_path, home)
    io = ScriptedWizardIO(responder=fresh_responder(("LLM の構成", "1")))
    assert run_wizard(root, io, deps) == 0, io.text
    settings = settings_of(root)
    assert settings.runner.trade.model == "sonnet" and settings.runner.improve.model == "sonnet"
    for alias, _note in CLAUDE_MODEL_ALIASES:
        assert alias in io.text


def test_claude_models_can_be_picked_by_number_or_typed(tmp_path, home):
    root = make_root(tmp_path)
    deps, run = claude_world(tmp_path, home)
    io = ScriptedWizardIO(responder=fresh_responder(
        ("LLM の構成", "1"), ("取引判断のモデル (番号", "2"), ("改善のモデル (番号", "m"),
        ("改善のモデル名", "claude-sonnet-4-6")))
    assert run_wizard(root, io, deps) == 0, io.text
    settings = settings_of(root)
    assert settings.runner.trade.model == "opus" and settings.runner.improve.model == "claude-sonnet-4-6"


def test_a_typed_model_name_with_odd_characters_is_asked_again(tmp_path, home):
    root = make_root(tmp_path)
    deps, run = claude_world(tmp_path, home)
    io = ScriptedWizardIO(responder=fresh_responder(
        ("LLM の構成", "1"), ("改善のモデル (番号", "m"), ("改善のモデル名", ("bad name; rm", "haiku"))))
    assert run_wizard(root, io, deps) == 0, io.text
    assert settings_of(root).runner.improve.model == "haiku"
    assert "英数字と" in io.text


def test_codex_defaults_to_the_listed_terra_model_and_skips_hidden_ones(tmp_path, home):
    root = make_root(tmp_path)
    deps, run, elf = codex_world(tmp_path, home, dict(stdout=codex_model_list(
        "gpt-6.1-sol", "gpt-6.1-secret", "gpt-6.1-terra", hidden=("gpt-6.1-secret",))))
    io = ScriptedWizardIO(responder=fresh_responder(("LLM の構成", "2"), ("codex の実体", str(elf))))
    assert run_wizard(root, io, deps) == 0, io.text
    settings = settings_of(root)
    assert settings.runner.improve.backend == "codex" and settings.runner.improve.model == "gpt-6.1-terra"
    assert settings.runner.trade.model == "sonnet"
    assert "gpt-6.1-secret" not in io.text
    assert run.matching(str(elf), "app-server")


def test_rerunning_with_local_trade_and_codex_improve_keeps_runner_values_on_enter(tmp_path, home):
    root = make_root(tmp_path)
    deps, run, elf = codex_world(tmp_path, home, dict(stdout=codex_model_list("gpt-6.1-terra")))
    deps.fetch.models = {"http://localhost:8080/v1": ["qwen3.6-35b-a3b_Q4", "qwen3-coder-30b-a3b-instruct"]}
    first = ScriptedWizardIO(responder=fresh_responder(
        ("改善 backend の実往復", "n"), ("改善の backend", "4"), ("codex の実体", str(elf))))
    assert run_wizard(root, first, deps) == 0, first.text
    before = (root / "config" / "settings.yaml").read_text()

    second = ScriptedWizardIO(responder=fresh_responder())
    assert run_wizard(root, second, deps) == 0, second.text
    assert (root / "config" / "settings.yaml").read_text() == before


def test_a_long_codex_list_is_narrowed_before_the_number_is_asked(tmp_path, home):
    ids = [f"model-{n:02d}" for n in range(10)] + ["gpt-6.1-sol", "gpt-6.1-terra"]
    root = make_root(tmp_path)
    deps, run, elf = codex_world(tmp_path, home, dict(stdout=codex_model_list(*ids)))
    io = ScriptedWizardIO(responder=fresh_responder(
        ("LLM の構成", "2"), ("codex の実体", str(elf)), ("改善のモデルの絞り込み", "GPT"), ("改善のモデル (番号", "1")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert settings_of(root).runner.improve.model == "gpt-6.1-sol"
    assert "model-03" not in io.text
    assert len(io.asked_with("絞り込み")) == 1


def test_a_filter_with_no_match_is_asked_again(tmp_path, home):
    ids = [f"model-{n:02d}" for n in range(12)]
    root = make_root(tmp_path)
    deps, run, elf = codex_world(tmp_path, home, dict(stdout=codex_model_list(*ids)))
    io = ScriptedWizardIO(responder=fresh_responder(
        ("LLM の構成", "2"), ("codex の実体", str(elf)), ("改善のモデルの絞り込み", ("zzz", "")),
        ("改善のモデル (番号", "12")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert settings_of(root).runner.improve.model == "model-11"
    assert "該当するモデルがありません" in io.text


@pytest.mark.parametrize("failure", [dict(returncode=1), dict(stdout="not json\n"), dict(raises=OSError("gone"))])
def test_when_codex_lists_nothing_the_name_is_typed_and_the_record_says_so(tmp_path, home, failure):
    root = make_root(tmp_path)
    deps, run, elf = codex_world(tmp_path, home, failure)
    io = ScriptedWizardIO(responder=fresh_responder(("LLM の構成", "2"), ("codex の実体", str(elf))))
    assert run_wizard(root, io, deps) == 0, io.text
    assert settings_of(root).runner.improve.model == "gpt-5.6-terra"
    improve = record_of(root)["llm_improve"]
    assert improve["result"] == "unverified" and improve["reason_code"] == "model_list_unavailable"
    assert "model_list_unavailable" in io.text


def test_opencode_lists_provider_headings_has_no_default_and_must_be_chosen(tmp_path, home):
    root = make_root(tmp_path)
    deps, calls, fetch, run = make_deps(tmp_path)
    run.on(str(home / ".opencode" / "bin" / "opencode"), "models",
           stdout="alpha/x\nbeta/y\nalpha/z\n")
    io = ScriptedWizardIO(responder=fresh_responder(
        ("改善の backend", "2"), ("改善のモデル (番号", ("", "2")),
        ("llama-swap に問い合わせて取得", "n"), ("context_limit", "16384")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert "[alpha]" in io.text and "[beta]" in io.text
    assert settings_of(root).runner.improve.model == "alpha/z"
    assert "モデルを選んでください" in io.text
    assert not any("[" in prompt and "]:" in prompt for prompt in io.asked_with("改善のモデル (番号"))


def test_opencode_without_a_listing_is_typed_and_recorded_as_unavailable(tmp_path, home):
    root = make_root(tmp_path)
    deps, calls, fetch, run = make_deps(tmp_path)
    run.on(str(home / ".opencode" / "bin" / "opencode"), "models", returncode=1)
    io = ScriptedWizardIO(responder=fresh_responder(
        ("改善の backend", "2"), ("改善のモデル名", "llama-swap/typed"),
        ("llama-swap に問い合わせて取得", "n"), ("context_limit", "16384")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert settings_of(root).runner.improve.model == "llama-swap/typed"
    assert record_of(root)["llm_improve"]["reason_code"] == "model_list_unavailable"


def test_local_runners_keep_choosing_from_the_llama_swap_list(tmp_path):
    root = make_root(tmp_path)
    deps, calls, fetch, run = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder())
    assert run_wizard(root, io, deps) == 0, io.text
    assert "app-server" not in repr(run.calls) and "models" not in repr(run.calls)


def test_model_names_are_written_to_settings_and_never_to_the_record(tmp_path, home):
    root = make_root(tmp_path)
    deps, run, elf = codex_world(tmp_path, home, dict(stdout=codex_model_list("gpt-6.1-terra")))
    io = ScriptedWizardIO(responder=fresh_responder(
        ("LLM の構成", "2"), ("codex の実体", str(elf)), ("取引判断のモデル (番号", "3")))
    assert run_wizard(root, io, deps) == 0, io.text
    text = json.dumps(record_of(root))
    for name in ("sonnet", "opus", "fable", "haiku", "gpt-6.1-terra", "gpt-5.6-terra"):
        assert name not in text
    settings = settings_of(root)
    assert settings.runner.trade.model == "fable" and settings.runner.improve.model == "gpt-6.1-terra"
