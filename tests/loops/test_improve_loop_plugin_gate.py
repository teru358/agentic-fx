"""commit() 手順3: plugin ゲート (設計書 §4.2-3、プラン §8.1-9/10 参照)。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentic_fx.plugin.gate_pytest import GateResult
from agentic_fx.plugin.noop_gate import (
    count_self_test_functions, find_noop_copy, normalized_plugin_ast,
)


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


def _empty_inventory():
    """[indicator-consumption-wiring] T4b: `find_noop_copy` の新引数
    `inventory` 必須化に伴う既存呼び出しの移行 — これらのテストは
    再ロック例外の判定材料を必要としない (同名比較がそもそも起きない
    か、依存 0 本の候補しか扱わない) ため、空 inventory で十分。"""
    from agentic_fx.plugin.resolve import ApprovedInventory, InventoryBuildResult
    inv = ApprovedInventory(root=Path("/nonexistent"), metas=())
    return InventoryBuildResult(inventory=inv, phase1_metas=(), resolved={},
                                rejected_strategies=())


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


@pytest.mark.parametrize(("source", "expected"), [
    ("def test_a(): pass\ndef test_b(): pass\ndef test_c(): pass\n", 3),
    ("def test_a(): pass\nclass TestX:\n def test_b(self): pass\n async def test_c(self): pass\n", 3),
    ("def test_a(): pass\nclass Padding:\n def test_b(self): pass\n def test_c(self): pass\n def test_d(self): pass\n def test_e(self): pass\n def test_f(self): pass\n", 1),
    ("async def test_async(): pass\n", 1),
])
def test_count_self_tests_approximates_pytest_default_collection(tmp_path, source, expected):
    path = tmp_path / "test_plugin.py"
    path.write_text(source)
    assert count_self_test_functions(path) == expected


def test_find_noop_copy_requires_both_code_and_config_to_match(tmp_path):
    source = _source_snapshot(tmp_path)
    example = _write_candidate(source / "_examples", "rsi_indicator")
    candidate = _write_candidate(tmp_path / "staging", "candidate")
    (candidate / "config.yaml").write_text(
        "kind: indicator\npairs: [USDJPY]\ntimeframe: 1h\n")

    assert normalized_plugin_ast(candidate / "plugin.py") == normalized_plugin_ast(
        example / "plugin.py")
    assert find_noop_copy(candidate, source_snapshot_dir=source,
                          examples_dir=source / "_examples",
                          name="candidate", inventory=_empty_inventory()) is None


def test_find_noop_copy_combines_ast_normalization_with_example_search(tmp_path):
    source = _source_snapshot(tmp_path)
    example = _write_candidate(
        source / "_examples", "rsi_indicator",
        plugin_py=b'"""example docs"""\n# example comment\ndef compute(df, params):\n    return {}\n')
    candidate = _write_candidate(
        tmp_path / "staging", "candidate",
        plugin_py=b"# candidate comment\n\ndef compute(df, params):\n    \"\"\"candidate docs\"\"\"\n    return {}\n")

    assert find_noop_copy(candidate, source_snapshot_dir=source,
                          examples_dir=source / "_examples",
                          name="candidate",
                          inventory=_empty_inventory()) == "_examples/rsi_indicator"


def test_find_noop_copy_compares_same_named_deployed_plugin(tmp_path):
    source = _source_snapshot(tmp_path)
    _write_candidate(source, "candidate")
    candidate = _write_candidate(tmp_path / "staging", "candidate")
    assert find_noop_copy(candidate, source_snapshot_dir=source,
                          examples_dir=source / "_examples",
                          name="candidate", inventory=_empty_inventory()) == "candidate"


def test_find_noop_copy_compares_differently_named_deployed_plugin(tmp_path):
    source = _source_snapshot(tmp_path)
    _write_candidate(source, "deployed")
    candidate = _write_candidate(tmp_path / "staging", "candidate")
    assert find_noop_copy(candidate, source_snapshot_dir=source,
                          examples_dir=source / "_examples",
                          name="candidate", inventory=_empty_inventory()) == "deployed"


def test_find_noop_copy_requires_ast_match_even_when_config_matches(tmp_path):
    source = _source_snapshot(tmp_path)
    _write_candidate(source, "candidate")
    candidate = _write_candidate(
        tmp_path / "staging", "candidate",
        plugin_py=b"def compute(df, params):\n    return {'changed': True}\n")
    assert find_noop_copy(candidate, source_snapshot_dir=source,
                          examples_dir=source / "_examples",
                          name="candidate", inventory=_empty_inventory()) is None


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


def test_gate_rejects_missing_self_tests_before_pytest(tmp_path, loop_min, monkeypatch):
    source = _source_snapshot(tmp_path)
    candidate = _write_candidate(tmp_path / "staging", "candidate", test_plugin=b"x = 1\n")
    pytest_gate = MagicMock()
    monkeypatch.setattr("agentic_fx.loops.improve_loop.run_gate_pytest", pytest_gate)

    verdict = loop_min._run_plugin_gate(
        candidate, name="candidate", source_snapshot_dir=source)

    assert verdict.reason == "self_test_missing"
    pytest_gate.assert_not_called()


def test_gate_accepts_one_parametrized_function_when_three_cases_pass(
        tmp_path, loop_min, monkeypatch):
    source = _source_snapshot(tmp_path)
    candidate = _write_candidate(
        tmp_path / "staging", "candidate",
        test_plugin=b"import pytest\n@pytest.mark.parametrize('x', [1,2,3])\ndef test_x(x): assert x\n")
    monkeypatch.setattr("agentic_fx.loops.improve_loop.run_gate_pytest", lambda *a, **k: GateResult(
        passed=True, returncode=0, stdout_tail="3 passed in 0.1s", duration_sec=0.1))
    assert loop_min._run_plugin_gate(candidate, name="candidate", source_snapshot_dir=source).passed


def test_gate_uses_last_pytest_summary_shaped_line(tmp_path, loop_min, monkeypatch):
    source = _source_snapshot(tmp_path)
    candidate = _write_candidate(tmp_path / "staging", "candidate",
        test_plugin=b"def test_x(): print('99 passed in 0.01s')\n")
    monkeypatch.setattr("agentic_fx.loops.improve_loop.run_gate_pytest", lambda *a, **k: GateResult(
        passed=True, returncode=0,
        stdout_tail="99 passed in 0.01s\nnoise\n=== 3 passed in 0.1s ===", duration_sec=0.1))
    gate = loop_min._settings.improve.gate
    original_minimum = gate.min_test_functions
    try:
        object.__setattr__(gate, "min_test_functions", 50)
        verdict = loop_min._run_plugin_gate(
            candidate, name="candidate", source_snapshot_dir=source)
        assert verdict.reason == "self_test_too_thin_collected:3<50"
    finally:
        object.__setattr__(gate, "min_test_functions", original_minimum)


def test_gate_with_five_tests_and_material_change_reaches_pytest(
        tmp_path, loop_min, monkeypatch):
    source = _source_snapshot(tmp_path)
    candidate = _write_candidate(
        tmp_path / "staging", "candidate",
        test_plugin=b"\n".join(f"def test_{i}(): pass".encode() for i in range(5)) + b"\n")
    pytest_gate = MagicMock(return_value=GateResult(
        passed=True, returncode=0, stdout_tail="5 passed in 0.12s", duration_sec=0.1))
    monkeypatch.setattr("agentic_fx.loops.improve_loop.run_gate_pytest", pytest_gate)

    verdict = loop_min._run_plugin_gate(
        candidate, name="candidate", source_snapshot_dir=source)

    assert verdict.passed is True
    pytest_gate.assert_called_once()


def test_gate_rejects_when_pytest_collects_fewer_tests_than_ast_precheck(
        tmp_path, loop_min, monkeypatch):
    source = _source_snapshot(tmp_path)
    candidate = _write_candidate(
        tmp_path / "staging", "candidate",
        test_plugin=(b"import pytest\ndef test_real(): pass\nclass TestSkipped:\n"
                     b" @pytest.mark.skip\n def test_a(self): pass\n"
                     b" @pytest.mark.skip\n def test_b(self): pass\n"))
    monkeypatch.setattr(
        "agentic_fx.loops.improve_loop.run_gate_pytest",
        lambda *a, **k: GateResult(
            passed=True, returncode=0, stdout_tail="1 passed in 0.12s",
            duration_sec=0.1))
    verdict = loop_min._run_plugin_gate(
        candidate, name="candidate", source_snapshot_dir=source)
    assert verdict.reason == "self_test_too_thin_collected:1<3"


def test_gate_counts_passed_from_summary_with_trailing_comma(
        tmp_path, loop_min, monkeypatch):
    """`3 passed, 1 warning in 0.1s` (カンマ続き) を 0 扱いにしない —
    末尾を `\\s|$` にした版はこの形で誤 fail closed した (指揮者レビュー)。"""
    source = _source_snapshot(tmp_path)
    candidate = _write_candidate(
        tmp_path / "staging", "candidate",
        test_plugin=(b"def test_a(): pass\ndef test_b(): pass\n"
                     b"def test_c(): pass\n"))
    monkeypatch.setattr(
        "agentic_fx.loops.improve_loop.run_gate_pytest",
        lambda *a, **k: GateResult(
            passed=True, returncode=0,
            stdout_tail="3 passed, 2 skipped, 1 warning in 0.12s",
            duration_sec=0.1))
    verdict = loop_min._run_plugin_gate(
        candidate, name="candidate", source_snapshot_dir=source)
    assert not (verdict.reason or "").startswith("self_test_too_thin_collected")


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
        return GateResult(passed=True, returncode=0, stdout_tail="3 passed in 0.12s",
                          duration_sec=0.1)

    monkeypatch.setattr(
        "agentic_fx.loops.improve_loop.run_gate_pytest", _fake_gate)
    verdict = loop_min._run_plugin_gate(
        d, name="myind", source_snapshot_dir=_source_snapshot(tmp_path))
    assert verdict.passed is False
    assert "hash" in verdict.reason.lower()


# --- [indicator-consumption-wiring] T4b Step 4-6: P4 (noop の pin 除去比較) ---

from agentic_fx.plugin import loader as plugin_loader
from agentic_fx.plugin.resolve import ApprovedInventory, InventoryBuildResult
from tests.fixtures import indicator_wiring as fx
from tests.fixtures.wiring_envs import (
    SETTINGS_FIXTURE as SETTINGS,
    copy_example as _copy_example,
    rename_dependency as _rename_dependency,
)


def _inventory_of(plugins_root, names):
    metas = []
    for name in names:
        meta, reason = plugin_loader.discover_one_with_reason(
            (plugins_root / name).resolve(), name)
        assert reason is None, reason
        metas.append(meta)
    inv = ApprovedInventory(root=plugins_root.resolve(), metas=tuple(metas))
    return InventoryBuildResult(inventory=inv, phase1_metas=tuple(metas),
                                resolved={}, rejected_strategies=())


def test_locked_copy_of_an_example_is_still_a_noop(tmp_path):
    """P4: `_examples/rsi_pullback` を逐語コピーして lock しただけの候補は
    `noop_copy_of:_examples/rsi_pullback`。"""
    from agentic_fx.plugin.resolve import lock_config, resolve_indicator_deps
    plugins_root = tmp_path / "plugins"
    fx.write_indicator(plugins_root, "rsi")
    snapshot = tmp_path / "snap"
    examples = snapshot / "_examples"
    _copy_example(examples, "rsi_pullback")
    cand = _copy_example(tmp_path / "staging", "rsi_pullback")
    inventory = _inventory_of(plugins_root, ["rsi"])
    _rename_dependency(cand, "rsi_indicator", "rsi")
    _rename_dependency(examples / "rsi_pullback", "rsi_indicator", "rsi")
    meta, _ = plugin_loader.discover_one_with_reason(cand, "rsi_pullback")
    lock_config(cand, resolve_indicator_deps(
        meta, inventory.inventory, settings=SETTINGS, pin_mode="ignore").pins())
    assert find_noop_copy(cand, source_snapshot_dir=snapshot,
                          examples_dir=examples, name="rsi_pullback",
                          inventory=inventory) == "_examples/rsi_pullback"


def test_relocked_copy_of_a_deployed_strategy_is_not_a_noop(tmp_path):
    """P4: 配備済 S (pin I1、I2 承認済で pin 破れ) を複製して I2 に再ロック
    した候補は noop にならない (正式な再ロック経路)。"""
    from agentic_fx.plugin.resolve import lock_config, resolve_indicator_deps
    plugins_root = tmp_path / "plugins"
    fx.write_indicator(plugins_root, "rsi")            # = I2 (現在 inventory)
    inventory = _inventory_of(plugins_root, ["rsi"])
    snapshot = tmp_path / "snap"
    fx.write_rsi_pullback(snapshot, pins={"rsi": "a" * 64})   # 配備済 S (I1)
    cand = fx.write_rsi_pullback(tmp_path / "staging", pins={"rsi": "a" * 64})
    meta, _ = plugin_loader.discover_one_with_reason(cand, "rsi_pullback")
    lock_config(cand, resolve_indicator_deps(
        meta, inventory.inventory, settings=SETTINGS, pin_mode="ignore").pins())
    assert find_noop_copy(cand, source_snapshot_dir=snapshot,
                          examples_dir=snapshot / "_examples",
                          name="rsi_pullback", inventory=inventory) is None


def test_relock_exemption_applies_only_to_the_same_named_deployed_plugin(
        tmp_path):
    """段 0 束 2 M10 (SURVIVED) の pin: 再ロック例外は **同名**の配備物との
    比較にだけ効く (`item.name == name`)。

    **別名**の配備物 D (pin I1) と `strip_pins` 同値な候補 C (pin I2、名前は
    別) は、C の pin が今の inventory と一致していても「D の逐語コピー」で
    あることに変わりはない — noop として弾かれなければならない。
    `is_same_name` を `True` に潰す変異 (= どの配備物に対しても再ロック例外
    を適用する) は既存 pin では 1 本も落ちなかった。"""
    import shutil
    from agentic_fx.plugin.resolve import lock_config, resolve_indicator_deps
    plugins_root = tmp_path / "plugins"
    fx.write_indicator(plugins_root, "rsi")            # = I2 (現在 inventory)
    inventory = _inventory_of(plugins_root, ["rsi"])
    snapshot = tmp_path / "snap"
    deployed = fx.write_rsi_pullback(snapshot, pins={"rsi": "a" * 64})
    shutil.move(str(deployed), str(snapshot / "other_strategy"))   # 別名で配備
    cand = fx.write_rsi_pullback(tmp_path / "staging", pins={"rsi": "a" * 64})
    meta, _ = plugin_loader.discover_one_with_reason(cand, "rsi_pullback")
    lock_config(cand, resolve_indicator_deps(
        meta, inventory.inventory, settings=SETTINGS, pin_mode="ignore").pins())
    assert find_noop_copy(cand, source_snapshot_dir=snapshot,
                          examples_dir=snapshot / "_examples",
                          name="rsi_pullback",
                          inventory=inventory) == "other_strategy"


def test_same_copy_without_relock_is_a_noop(tmp_path):
    """P4: 同じ複製を pin I1 のまま (再ロックなし) 提出すると
    `noop_copy_of:rsi_pullback`。"""
    plugins_root = tmp_path / "plugins"
    fx.write_indicator(plugins_root, "rsi")
    inventory = _inventory_of(plugins_root, ["rsi"])
    snapshot = tmp_path / "snap"
    fx.write_rsi_pullback(snapshot, pins={"rsi": "a" * 64})
    cand = fx.write_rsi_pullback(tmp_path / "staging", pins={"rsi": "a" * 64})
    assert find_noop_copy(cand, source_snapshot_dir=snapshot,
                          examples_dir=snapshot / "_examples",
                          name="rsi_pullback",
                          inventory=inventory) == "rsi_pullback"


def test_param_only_variant_is_still_not_a_noop(tmp_path):
    """既存契約の維持: config だけ違う候補は noop ではない。"""
    plugins_root = tmp_path / "plugins"
    fx.write_indicator(plugins_root, "rsi")
    inventory = _inventory_of(plugins_root, ["rsi"])
    snapshot = tmp_path / "snap"
    fx.write_rsi_pullback(snapshot, pins=None)
    cand = fx.write_rsi_pullback(tmp_path / "staging", pins=None)
    import yaml
    cfg = yaml.safe_load((cand / "config.yaml").read_text())
    cfg["params"]["oversold"] = 25
    (cand / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    assert find_noop_copy(cand, source_snapshot_dir=snapshot,
                          examples_dir=snapshot / "_examples",
                          name="rsi_pullback", inventory=inventory) is None
