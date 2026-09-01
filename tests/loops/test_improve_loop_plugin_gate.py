"""commit() 手順3: plugin ゲート (設計書 §4.2-3、プラン §8.1-9/10 参照)。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentic_fx.plugin.gate_pytest import GateResult
from agentic_fx.plugin.noop_gate import find_noop_copy, normalized_plugin_ast


def _write_candidate(staging_dir: Path, name: str, *,
                     plugin_py=b"def compute(df, params):\n    return {}\n",
                     config_yaml=b"kind: indicator\npairs: []\ntimeframe: 1h\n",
                     test_plugin=b"def test_x(): pass\n"):
    d = staging_dir / name
    d.mkdir(parents=True)
    (d / "plugin.py").write_bytes(plugin_py)
    (d / "config.yaml").write_bytes(config_yaml)
    (d / "test_plugin.py").write_bytes(test_plugin)
    return d


def _source_snapshot(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    return source


def test_normalized_plugin_ast_ignores_docstrings_comments_and_blank_lines(tmp_path):
    left = tmp_path / "left.py"
    right = tmp_path / "right.py"
    left.write_text('''"""module docs"""\n# comment\ndef compute(x):\n    """docs"""\n    return x + 1\n''')
    right.write_text("def compute(x):\n\n    # another comment\n    return x + 1\n")

    assert normalized_plugin_ast(left) == normalized_plugin_ast(right)


def test_normalized_plugin_ast_detects_logic_change(tmp_path):
    left = tmp_path / "left.py"
    right = tmp_path / "right.py"
    left.write_text("def compute(x):\n    return x + 1\n")
    right.write_text("def compute(x):\n    return x + 2\n")

    assert normalized_plugin_ast(left) != normalized_plugin_ast(right)


def test_find_noop_copy_requires_both_code_and_config_to_match(tmp_path):
    source = _source_snapshot(tmp_path)
    example = _write_candidate(source / "_examples", "rsi_indicator")
    candidate = _write_candidate(tmp_path / "staging", "candidate")
    (candidate / "config.yaml").write_text(
        "kind: indicator\npairs: [USDJPY]\ntimeframe: 1h\n")

    assert normalized_plugin_ast(candidate / "plugin.py") == normalized_plugin_ast(
        example / "plugin.py")
    assert find_noop_copy(candidate, source_snapshot_dir=source,
                          name="candidate") is None


def test_find_noop_copy_combines_ast_normalization_with_example_search(tmp_path):
    source = _source_snapshot(tmp_path)
    example = _write_candidate(
        source / "_examples", "rsi_indicator",
        plugin_py=b'"""example docs"""\n# example comment\ndef compute(df, params):\n    return {}\n')
    candidate = _write_candidate(
        tmp_path / "staging", "candidate",
        plugin_py=b"# candidate comment\n\ndef compute(df, params):\n    \"\"\"candidate docs\"\"\"\n    return {}\n")

    assert find_noop_copy(candidate, source_snapshot_dir=source,
                          name="candidate") == "_examples/rsi_indicator"


def test_gate_rejects_example_copy_before_pytest(tmp_path, loop_min, monkeypatch):
    source = _source_snapshot(tmp_path)
    _write_candidate(source / "_examples", "rsi_indicator")
    candidate = _write_candidate(tmp_path / "staging", "candidate")
    pytest_gate = MagicMock()
    monkeypatch.setattr("agentic_fx.loops.improve_loop.run_gate_pytest", pytest_gate)

    verdict = loop_min._run_plugin_gate(
        candidate, name="candidate", source_snapshot_dir=source)

    assert verdict.reason == "noop_copy_of:_examples/rsi_indicator"
    pytest_gate.assert_not_called()


def test_gate_rejects_too_few_self_tests_before_pytest(tmp_path, loop_min, monkeypatch):
    source = _source_snapshot(tmp_path)
    candidate = _write_candidate(tmp_path / "staging", "candidate")
    pytest_gate = MagicMock()
    monkeypatch.setattr("agentic_fx.loops.improve_loop.run_gate_pytest", pytest_gate)

    verdict = loop_min._run_plugin_gate(
        candidate, name="candidate", source_snapshot_dir=source)

    assert verdict.reason == "self_test_too_thin:1<3"
    pytest_gate.assert_not_called()


def test_gate_with_five_tests_and_material_change_reaches_pytest(
        tmp_path, loop_min, monkeypatch):
    source = _source_snapshot(tmp_path)
    candidate = _write_candidate(
        tmp_path / "staging", "candidate",
        test_plugin=b"\n".join(f"def test_{i}(): pass".encode() for i in range(5)) + b"\n")
    pytest_gate = MagicMock(return_value=GateResult(
        passed=True, returncode=0, stdout_tail="ok", duration_sec=0.1))
    monkeypatch.setattr("agentic_fx.loops.improve_loop.run_gate_pytest", pytest_gate)

    verdict = loop_min._run_plugin_gate(
        candidate, name="candidate", source_snapshot_dir=source)

    assert verdict.passed is True
    pytest_gate.assert_called_once()


def test_loader_rejection_short_circuits_before_pytest(
        tmp_path, loop_min, monkeypatch):
    d = _write_candidate(
        tmp_path,
        "myind",
        config_yaml=(b"kind: indicator\npairs: []\ntimeframe: 1h\n"
                     b"warmup_bars: 5\n"),
    )
    pytest_gate = MagicMock()
    monkeypatch.setattr(
        "agentic_fx.loops.improve_loop.run_gate_pytest", pytest_gate)

    verdict = loop_min._run_plugin_gate(
        d, name="myind", source_snapshot_dir=_source_snapshot(tmp_path))

    assert verdict.passed is False
    assert verdict.reason.startswith("loader_rejected: unknown config keys")
    pytest_gate.assert_not_called()


def test_snapshot_check_rejects_extra_file(tmp_path, loop_min):
    d = _write_candidate(tmp_path, "myind")
    (d / "extra.txt").write_text("x")
    verdict = loop_min._run_plugin_gate(
        d, name="myind", source_snapshot_dir=_source_snapshot(tmp_path))
    assert verdict.passed is False
    assert "extra" in verdict.reason.lower() or "3" in verdict.reason


def test_snapshot_check_rejects_symlink(tmp_path, loop_min):
    d = _write_candidate(tmp_path, "myind")
    (d / "plugin.py").unlink()
    (tmp_path / "elsewhere.py").write_text("evil")
    (d / "plugin.py").symlink_to(tmp_path / "elsewhere.py")
    verdict = loop_min._run_plugin_gate(
        d, name="myind", source_snapshot_dir=_source_snapshot(tmp_path))
    assert verdict.passed is False


def test_gate_pytest_failure_produces_observation_not_approval(
        tmp_path, loop_min, monkeypatch):
    d = _write_candidate(tmp_path, "myind")
    monkeypatch.setattr(
        "agentic_fx.loops.improve_loop.run_gate_pytest",
        lambda plugin_dir, *, settings: GateResult(
            passed=False, returncode=1, stdout_tail="FAILED", duration_sec=0.1))
    verdict = loop_min._run_plugin_gate(
        d, name="myind", source_snapshot_dir=_source_snapshot(tmp_path))
    assert verdict.passed is False


def test_hash_changed_after_pytest_is_rejected_even_if_pytest_passed(
        tmp_path, loop_min, monkeypatch):
    """副 pin: 親側 fault injection で pytest 実行後にファイル内容を
    書き換え、hash 不一致で承認申請が出ないことを確認する。"""
    d = _write_candidate(
        tmp_path, "myind",
        test_plugin=b"def test_a(): pass\ndef test_b(): pass\ndef test_c(): pass\n")

    def _fake_gate(plugin_dir, *, settings):
        (plugin_dir / "plugin.py").write_bytes(b"MUTATED")  # 親側 fault injection
        return GateResult(passed=True, returncode=0, stdout_tail="ok",
                          duration_sec=0.1)

    monkeypatch.setattr(
        "agentic_fx.loops.improve_loop.run_gate_pytest", _fake_gate)
    verdict = loop_min._run_plugin_gate(
        d, name="myind", source_snapshot_dir=_source_snapshot(tmp_path))
    assert verdict.passed is False
    assert "hash" in verdict.reason.lower()
