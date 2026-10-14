"""同梱の台本を、実装のウィザードに対して問いの順に流し込んで通し切れることを確かめる。

driver と同じ規則 (入力待ちの行に expect が含まれる問いにだけ答える。optional は出なければ飛ばす) で
台本を進める。実 process・実 systemd・実ネットワークは使わず、ウィザードの入出力と外部呼び出しは
tests/setup の台本 IO と fake で差し替える。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_fx.setup import post_start
from agentic_fx.setup.indicators import list_standard_indicators, ask_selection
from agentic_fx.setup.wizard import IndicatorOutcome, _IndicatorIOAdapter, run_wizard
from agentic_fx.store.setup_record import read_setup_record

from tests.setup.wizard_support import ScriptedWizardIO, make_deps, make_executable, make_root

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPTS = Path(__file__).resolve().parent / "scripts"


class Replay:
    def __init__(self, name: str) -> None:
        self.steps = json.loads((SCRIPTS / name).read_text(encoding="utf-8"))
        self.index = 0
        self.answered: list[str] = []

    def __call__(self, prompt: str) -> str:
        for candidate in range(self.index, len(self.steps)):
            step = self.steps[candidate]
            if step["expect"] in prompt:
                self.index = candidate + 1
                self.answered.append(step["expect"])
                return step["send"]
            if not step.get("optional"):
                break
        pending = self.steps[self.index]["expect"] if self.index < len(self.steps) else "(台本の終わり)"
        raise AssertionError(f"台本に無い問い: {prompt!r} (待っていたのは {pending!r})")

    def assert_finished(self) -> None:
        left = [step["expect"] for step in self.steps[self.index:] if not step.get("optional")]
        assert not left, f"台本が余りました: {left}"


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    return path


def _indicator_questions_only(ctx) -> IndicatorOutcome:
    rows = list_standard_indicators(REPOSITORY / "docs" / "examples" / "plugins", ())
    ask_selection(_IndicatorIOAdapter(ctx.io), rows)
    return IndicatorOutcome(0)


def _enable_and_start_that_asks(root, **kwargs):
    kwargs["confirm"]("enable")
    kwargs["confirm"]("start")
    return post_start.StartOutcome("skipped", "enable_declined", kwargs["now"]())


def test_main_script_runs_the_wizard_to_the_end(tmp_path, home):
    root = make_root(tmp_path)
    binary = make_executable(tmp_path / "bin" / "claude")
    credentials = home / ".claude" / ".credentials.json"
    credentials.parent.mkdir(parents=True)
    credentials.write_text("{}")
    deps, calls, fetch, run = make_deps(
        tmp_path, which=lambda name: str(binary) if name in ("claude", str(binary)) else None,
        indicator_stage=_indicator_questions_only, enable_and_start=_enable_and_start_that_asks)
    fetch.models = {}
    run.on("node", "--version", stdout="v22\n").on(str(binary), "--version", stdout="1.0\n")
    run.on(str(binary), "-p", "ok だけ答えて", "--max-turns", "1", stdout="ok\n")
    replay = Replay("main-claude.json")
    io = ScriptedWizardIO(responder=replay)
    assert run_wizard(root, io, deps) == 0, io.text
    replay.assert_finished()
    data = read_setup_record(root).record.data
    assert data["llm_trade"]["result"] == "ok" and data["llm_improve"]["result"] == "ok"
    assert data["price"]["result"] == "ok"
    assert "llama-swap が応答しません。local は既定にしません。" in io.text
    assert calls.yf == ["USDJPY"]
    text = (root / "config" / "settings.yaml").read_text(encoding="utf-8")
    assert "backend: claude" in text


def test_cli_missing_script_completes_with_fixed_vocabulary_in_the_record(tmp_path, home):
    root = make_root(tmp_path)
    deps, calls, fetch, run = make_deps(
        tmp_path, which=lambda name: None, indicator_stage=_indicator_questions_only,
        enable_and_start=_enable_and_start_that_asks)
    fetch.models = {}
    run.on("node", "--version", raises=FileNotFoundError())
    replay = Replay("cli-missing.json")
    io = ScriptedWizardIO(responder=replay)
    assert run_wizard(root, io, deps) == 0, io.text
    replay.assert_finished()
    data = read_setup_record(root).record.data
    assert data["llm_trade"]["reason_code"] == "bin_not_found"
    assert data["llm_improve"]["reason_code"] == "bin_not_found"
    assert "導入手順は docs/operations/first-run-acceptance.md の『前提: CLI の導入』" in io.text
