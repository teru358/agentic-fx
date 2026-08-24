"""commit() 手順3: plugin ゲート (設計書 §4.2-3、プラン §8.1-9/10 参照)。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentic_fx.plugin.gate_pytest import GateResult


def _write_candidate(staging_dir: Path, name: str, *, plugin_py=b"P",
                     config_yaml=b"kind: indicator\npairs: []\ntimeframe: 1h\n",
                     test_plugin=b"def test_x(): pass\n"):
    d = staging_dir / name
    d.mkdir(parents=True)
    (d / "plugin.py").write_bytes(plugin_py)
    (d / "config.yaml").write_bytes(config_yaml)
    (d / "test_plugin.py").write_bytes(test_plugin)
    return d


def test_snapshot_check_rejects_extra_file(tmp_path, loop_min):
    d = _write_candidate(tmp_path, "myind")
    (d / "extra.txt").write_text("x")
    verdict = loop_min._run_plugin_gate(d, name="myind")
    assert verdict.passed is False
    assert "extra" in verdict.reason.lower() or "3" in verdict.reason


def test_snapshot_check_rejects_symlink(tmp_path, loop_min):
    d = _write_candidate(tmp_path, "myind")
    (d / "plugin.py").unlink()
    (tmp_path / "elsewhere.py").write_text("evil")
    (d / "plugin.py").symlink_to(tmp_path / "elsewhere.py")
    verdict = loop_min._run_plugin_gate(d, name="myind")
    assert verdict.passed is False


def test_gate_pytest_failure_produces_observation_not_approval(
        tmp_path, loop_min, monkeypatch):
    d = _write_candidate(tmp_path, "myind")
    monkeypatch.setattr(
        "agentic_fx.loops.improve_loop.run_gate_pytest",
        lambda plugin_dir, *, settings: GateResult(
            passed=False, returncode=1, stdout_tail="FAILED", duration_sec=0.1))
    verdict = loop_min._run_plugin_gate(d, name="myind")
    assert verdict.passed is False


def test_hash_changed_after_pytest_is_rejected_even_if_pytest_passed(
        tmp_path, loop_min, monkeypatch):
    """副 pin: 親側 fault injection で pytest 実行後にファイル内容を
    書き換え、hash 不一致で承認申請が出ないことを確認する。"""
    d = _write_candidate(tmp_path, "myind")

    def _fake_gate(plugin_dir, *, settings):
        (plugin_dir / "plugin.py").write_bytes(b"MUTATED")  # 親側 fault injection
        return GateResult(passed=True, returncode=0, stdout_tail="ok",
                          duration_sec=0.1)

    monkeypatch.setattr(
        "agentic_fx.loops.improve_loop.run_gate_pytest", _fake_gate)
    verdict = loop_min._run_plugin_gate(d, name="myind")
    assert verdict.passed is False
    assert "hash" in verdict.reason.lower()
