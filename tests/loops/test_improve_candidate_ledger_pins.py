"""最終 artifact の材料と gate の最終 hash の境界を 1 つずつ固定する。"""
from __future__ import annotations

from agentic_fx.plugin.gate_pytest import GateResult, hashes_of

from tests.loops.test_improve_candidate_ledger import (
    _gate_dir, _loop, improve_env)

_STRATEGY_CONFIG = """
kind: strategy
pairs: [USDJPY, EURUSD]
timeframe: 1h
exit_mode: levels
params: {}
"""


def test_final_candidate_takes_the_first_pair_of_a_strategy(
        improve_env, tmp_path):
    app, root = improve_env
    loop = _loop(app, root)
    directory = _gate_dir(tmp_path)
    (directory / "config.yaml").write_text(_STRATEGY_CONFIG, encoding="utf-8")
    content_hash, artifact_hash = hashes_of(directory)
    candidate = loop._final_candidate(
        directory, name="cand", content_hash=content_hash,
        artifact_hash=artifact_hash)
    assert candidate.pair == "USDJPY"


def test_final_candidate_is_none_without_a_final_hash(improve_env, tmp_path):
    app, root = improve_env
    loop = _loop(app, root)
    directory = _gate_dir(tmp_path)
    content_hash, artifact_hash = hashes_of(directory)
    assert loop._final_candidate(
        directory, name="cand", content_hash=None,
        artifact_hash=artifact_hash) is None
    assert loop._final_candidate(
        directory, name="cand", content_hash=content_hash,
        artifact_hash=None) is None
    assert loop._final_candidate(
        directory, name="cand", content_hash="", artifact_hash="") is None


def test_gate_fails_when_only_the_self_test_changes_during_a_passing_run(
        improve_env, tmp_path, monkeypatch):
    """pytest が通っても、実行中に self-test (artifact_hash だけに入る) が
    書き換えられたなら不合格で、verdict は書き換え後の hash を持つ。"""
    app, root = improve_env
    loop = _loop(app, root)
    directory = _gate_dir(tmp_path)
    before = hashes_of(directory)

    def fake(plugin_dir, *, settings):
        (plugin_dir / "test_plugin.py").write_text(
            (plugin_dir / "test_plugin.py").read_text() + "\n# edited\n",
            encoding="utf-8")
        return GateResult(True, 0, "99 passed in 0.1s", 0.0)

    monkeypatch.setattr("agentic_fx.loops.improve_loop.run_gate_pytest", fake)
    verdict = loop._run_plugin_gate(
        directory, name="cand", source_snapshot_dir=tmp_path / "snap")
    assert verdict.passed is False
    assert verdict.reason.startswith("hash changed after pytest")
    assert verdict.content_hash == before[0]
    assert verdict.artifact_hash == hashes_of(directory)[1] != before[1]
