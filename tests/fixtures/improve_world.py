"""improve の結合試験が共有する、LLM を呼ばない worker runner と候補の置き込み。

`tests/loops/test_improve_e2e.py` と比較 gate の結合試験が使う。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from agentic_fx.runners.base import Mission, MissionResult
from agentic_fx.service import run_init

_REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeImproveWorkerRunner:
    """`agentic_fx.runners.worker_runner.WorkerRunner` の差し替え double。

    `ImproveLoop.prepare()` が `WorkerRunner(root=..., settings=..., clock=...,
    worker_profile="improve", run_context=ctx, stop_event=...)` の形で構築する
    想定 (Task 10) に合わせ、同じ kwargs を受理し `.run(mission)` だけを
    実装する。"""

    def __init__(self, *, result: MissionResult, **kwargs) -> None:
        self._result = result
        self.kwargs = kwargs
        self.missions: list[Mission] = []

    def run(self, mission: Mission) -> MissionResult:
        self.missions.append(mission)
        return self._result


def plugin_artifact(name: str, *, kind: str = "indicator",
                     self_test: str = "passed") -> dict:
    return {
        "discoveries": [{"idea": f"idea for {name}", "source": "agent",
                         "evidence": "test evidence", "kind": "task"}],
        "selected": {"backlog_id": None, "idea": f"idea for {name}"},
        "artifact": {"type": "plugin", "name": name, "kind": kind,
                    "self_test": self_test, "summary": f"{name} candidate"},
        "selection_rationale": "test rationale",
    }


MIN3_TEST_PY = (
    "def test_placeholder_a():\n    pass\n\n\n"
    "def test_placeholder_b():\n    pass\n\n\n"
    "def test_placeholder_c():\n    pass\n")


def write_staging_plugin(staging_dir: Path, name: str,
                          plugin_py: str, config_yaml: str,
                          test_plugin: str) -> Path:
    """FakeImproveWorkerRunner はサブプロセスを起こさないため、`prepare()` が
    作った staging_dir へ候補 3 本を直接置く (worker が書くはずの内容を
    テストが代理で書く)。staging_dir は正規形
    `plugins/_staging/<mission_id>` (D-15 是正、設計書 §2.2/§2.3) である。"""
    candidate = staging_dir / name
    candidate.mkdir(parents=True)
    (candidate / "plugin.py").write_text(plugin_py, encoding="utf-8")
    (candidate / "config.yaml").write_text(config_yaml, encoding="utf-8")
    (candidate / "test_plugin.py").write_text(test_plugin, encoding="utf-8")
    return candidate


def install(root: Path) -> None:
    (root / "config").mkdir()
    src = (_REPO_ROOT / "config" / "settings.yaml.example").read_text(
        encoding="utf-8")
    assert 'eval_source: "none"' in src
    src = src.replace('eval_source: "none"', 'eval_source: "dukascopy"', 1)
    (root / "config" / "settings.yaml.example").write_text(src)
    with patch("agentic_fx.service.PriceProvider") as pp, \
         patch("agentic_fx.service._check_llama_swap"):
        pp.return_value.healthcheck.return_value = "yfinance"
        run_init(root)
