"""improve registry の候補置き場ツール (設計書 §3.4/§2.3)。"""
from __future__ import annotations

import json
import subprocess
import shutil
from pathlib import Path

import pytest

from agentic_fx.config import ImproveToolBudgetSettings
from agentic_fx.tools.improve_staging_tools import (
    BUDGET_EXHAUSTED_DIRECTIVE,
    _failure_signature, build_improve_staging_tooldefs,
)
from agentic_fx.tools.mission_counters import MissionToolCounters
from agentic_fx.tools.registry import ToolRegistry


def _build(tmp_path):
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    tools = build_improve_staging_tooldefs(
        staging_dir=staging_dir, source_snapshot_dir=source_dir)
    return {t.name: t for t in tools}, staging_dir, source_dir


def test_write_then_read_staging_file_roundtrips(tmp_path):
    tools, staging_dir, _ = _build(tmp_path)
    out = tools["write_staging_file"].func(
        name="rsi_v2", rel="plugin.py", content="x = 1\n")
    assert "error" not in out
    got = tools["read_staging_file"].func(name="rsi_v2", rel="plugin.py")
    assert got["content"] == "x = 1\n"
    # L18: 生成先が「正しい場所」であることを assert する (単に外部に
    # ファイルが無いだけでなく、staging_dir/name/rel に実在すること)。
    written = staging_dir / "rsi_v2" / "plugin.py"
    assert written.read_text() == "x = 1\n"
    assert written.resolve().parent.parent == staging_dir.resolve()


def test_list_staging_reports_names_and_files(tmp_path):
    tools, _, _ = _build(tmp_path)
    tools["write_staging_file"].func(name="a", rel="plugin.py", content="1")
    tools["write_staging_file"].func(name="a", rel="config.yaml", content="2")
    out = tools["list_staging"].func()
    assert out == {"candidates": [{"name": "a", "files": ["config.yaml", "plugin.py"]}]}


def test_list_staging_returns_candidates_in_sorted_order(tmp_path):
    """L19: 候補 1 件のみのテストしか無いため `sorted` 削除が検出できない
    — 候補 3 件を逆順に作り、返却順が昇順であることを確認する。"""
    tools, _, _ = _build(tmp_path)
    for name in ("zebra", "mango", "apple"):
        tools["write_staging_file"].func(name=name, rel="plugin.py", content="x")
    out = tools["list_staging"].func()
    assert [c["name"] for c in out["candidates"]] == ["apple", "mango", "zebra"]


def test_list_staging_excludes_names_unreadable_by_tools(tmp_path):
    """opencode E2E m11 実測 (2026-08-30): snapshot は staging_dir 直下の
    `_snapshot_src/` に実体化されるため、list_staging が候補として返して
    いた。`_NAME_RE` は先頭 `_` を弾くので、モデルはその「候補」を
    read_staging_file で読もうとして必ず not found になる — 読めない名前は
    列挙しない。"""
    tools, staging_dir, _ = _build(tmp_path)
    tools["write_staging_file"].func(name="a", rel="plugin.py", content="1")
    (staging_dir / "_snapshot_src" / "rsi").mkdir(parents=True)
    out = tools["list_staging"].func()
    assert [c["name"] for c in out["candidates"]] == ["a"]


def test_safe_join_rejects_circular_symlink(tmp_path):
    """L17: `_safe_join` の `except (OSError, RuntimeError)` (resolve 失敗)
    経路が未テスト — 循環 symlink を張って error になることを確認する。"""
    staging_dir = tmp_path / "staging"; staging_dir.mkdir()
    source_dir = tmp_path / "source"; source_dir.mkdir()
    loop_dir = staging_dir / "loopy"
    loop_dir.symlink_to(loop_dir, target_is_directory=True)  # 自己参照
    tools = {t.name: t for t in build_improve_staging_tooldefs(
        staging_dir=staging_dir, source_snapshot_dir=source_dir)}
    out = tools["read_staging_file"].func(name="loopy", rel="plugin.py")
    assert "error" in out


def test_safe_join_rejects_name_with_trailing_newline(tmp_path):
    """round2 D3 是正 (検収 acceptance-round2.md D3): `_safe_join`
    (improve_staging_tools.py:24) の `.fullmatch()` 化が実際にツール経由で
    観測されていなかった (`.match` へ戻す変異が全スイート生存)。
    末尾改行付きの name (`.match()` + `$` は受理してしまう — probe 実測と
    同型) を `write_staging_file`/`read_staging_file` へ渡し、reject
    されることを確認する。"""
    tools, staging_dir, _ = _build(tmp_path)
    out = tools["write_staging_file"].func(
        name="rsi_v2\n", rel="plugin.py", content="x = 1\n")
    assert "error" in out
    # 正規形でない名前のディレクトリが staging_dir 配下に作られていないこと
    assert not any(p.name.endswith("\n") for p in staging_dir.iterdir())


def test_write_staging_file_rejects_rel_outside_allowed_set(tmp_path):
    tools, _, _ = _build(tmp_path)
    out = tools["write_staging_file"].func(
        name="a", rel="not_allowed.txt", content="x")
    assert "error" in out


@pytest.mark.parametrize("evil_rel", ["../../etc/passwd", "/etc/passwd",
                                       "..\\..\\etc\\passwd"])
def test_write_staging_file_rejects_path_traversal_in_rel(tmp_path, evil_rel):
    tools, staging_dir, _ = _build(tmp_path)
    out = tools["write_staging_file"].func(
        name="a", rel=evil_rel, content="x")
    assert "error" in out
    assert not (staging_dir.parent / "etc").exists()


@pytest.mark.parametrize("evil_name", ["../a", "a/b", "A", "1a", ""])
def test_write_staging_file_rejects_non_canonical_name(tmp_path, evil_name):
    tools, _, _ = _build(tmp_path)
    out = tools["write_staging_file"].func(
        name=evil_name, rel="plugin.py", content="x")
    assert "error" in out


def test_read_plugin_source_reads_from_source_snapshot_dir_only(tmp_path):
    tools, _, source_dir = _build(tmp_path)
    (source_dir / "rsi_v1").mkdir()
    (source_dir / "rsi_v1" / "plugin.py").write_text("y = 2\n")
    out = tools["read_plugin_source"].func(name="rsi_v1")
    assert out["plugin.py"] == "y = 2\n"


def test_read_plugin_source_absent_name_returns_error_not_raise(tmp_path):
    tools, _, _ = _build(tmp_path)
    out = tools["read_plugin_source"].func(name="does_not_exist")
    assert out["error"] == "not found"
    assert "list_examples / read_example_plugin" in out["hint"]
    assert "list_staging / read_staging_file" in out["hint"]


def test_read_staging_file_absent_path_returns_guidance(tmp_path):
    tools, _, _ = _build(tmp_path)
    out = tools["read_staging_file"].func(name="does_not_exist", rel="plugin.py")
    assert out["error"] == "not found"
    assert "list_staging" in out["hint"]
    assert "write_staging_file" in out["hint"]


def test_read_example_plugin_reads_all_allowed_files(tmp_path):
    tools, _, source_dir = _build(tmp_path)
    example = source_dir / "_examples" / "rsi_indicator"
    example.mkdir(parents=True)
    expected = {
        "plugin.py": "def compute():\n    return 42\n",
        "config.yaml": "kind: indicator\n",
        "test_plugin.py": "def test_compute():\n    assert True\n",
    }
    for rel, content in expected.items():
        (example / rel).write_text(content)

    assert tools["read_example_plugin"].func(name="rsi_indicator") == expected


def test_read_example_plugin_absent_name_lists_available_examples(tmp_path):
    tools, _, source_dir = _build(tmp_path)
    (source_dir / "_examples" / "rsi_indicator").mkdir(parents=True)

    out = tools["read_example_plugin"].func(name="does_not_exist")

    assert out["error"] == "not found"
    assert out["available"] == ["rsi_indicator"]


def test_list_examples_returns_canonical_directories_in_sorted_order(tmp_path):
    tools, _, source_dir = _build(tmp_path)
    examples = source_dir / "_examples"
    for name in ("sma_cross", "rsi_indicator", "_private"):
        (examples / name).mkdir(parents=True)

    assert tools["list_examples"].func() == {
        "examples": ["rsi_indicator", "sma_cross"]}


@pytest.mark.parametrize("invalid_name", ["../x", "_examples"])
def test_read_example_plugin_rejects_non_canonical_name(tmp_path, invalid_name):
    tools, _, _ = _build(tmp_path)

    out = tools["read_example_plugin"].func(name=invalid_name)

    assert out["error"] == "not found"
    assert out["available"] == []


def test_example_tools_are_registered(tmp_path):
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    registry = ToolRegistry()
    registry.register_all(build_improve_staging_tooldefs(
        staging_dir=staging_dir, source_snapshot_dir=source_dir))

    assert "list_examples" in registry.names()
    assert "read_example_plugin" in registry.names()


def test_run_plugin_tests_reports_participant_result(tmp_path):
    tools, staging_dir, _ = _build(tmp_path)
    (staging_dir / "ok_case").mkdir()
    (staging_dir / "ok_case" / "test_plugin.py").write_text(
        "def test_x():\n    assert 1 == 1\n")
    out = tools["run_plugin_tests"].func(name="ok_case")
    assert out["passed"] is True


def test_run_plugin_tests_ignores_ancestor_pytest_config(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        'addopts = "--this-flag-does-not-exist"\n')
    tools, staging_dir, _ = _build(tmp_path)
    (staging_dir / "isolated_case").mkdir()
    (staging_dir / "isolated_case" / "test_plugin.py").write_text(
        "def test_x():\n    assert True\n")

    out = tools["run_plugin_tests"].func(name="isolated_case")

    assert out["passed"] is True, out["stdout_tail"]


def test_run_plugin_tests_reports_failure_without_raising(tmp_path):
    tools, staging_dir, _ = _build(tmp_path)
    (staging_dir / "bad_case").mkdir()
    (staging_dir / "bad_case" / "test_plugin.py").write_text(
        "def test_x():\n    assert 1 == 2\n")
    out = tools["run_plugin_tests"].func(name="bad_case")
    assert out["passed"] is False


def test_run_plugin_tests_truncates_stdout_to_2000_chars(tmp_path):
    """L20: `run_plugin_tests` の `stdout[-2000:]` 切り詰めが未検証。
    2000 文字超を出す test_plugin.py で `len(stdout_tail) == 2000` を確認。"""
    tools, staging_dir, _ = _build(tmp_path)
    (staging_dir / "verbose_case").mkdir()
    body = "\n".join(
        f"def test_x{i}():\n    assert 'x' * 200 == ''" for i in range(20))
    (staging_dir / "verbose_case" / "test_plugin.py").write_text(body + "\n")
    out = tools["run_plugin_tests"].func(name="verbose_case")
    assert len(out["stdout_tail"]) == 2000


def test_staging_tools_return_single_layer_json_via_registry(tmp_path):
    """B1 (検収 Blocking): `ToolRegistry.execute` が `json.dumps(result, ...)`
    で自ら直列化する契約 (`registry.py`) なので、5 関数は native dict を
    返さなければならない。ここでは 5 種すべてを実 `ToolRegistry.execute`
    経由で叩き、agent が受け取る文字列が **1 段の JSON** であること
    (= `json.loads` した中身がすでに dict/list/str/... であり、それ自体が
    さらに JSON 文字列になっていないこと) を pin する。"""
    staging_dir = tmp_path / "staging"; staging_dir.mkdir()
    source_dir = tmp_path / "source"; source_dir.mkdir()
    (source_dir / "rsi_v1").mkdir()
    (source_dir / "rsi_v1" / "plugin.py").write_text("y = 2\n")

    registry = ToolRegistry()
    registry.register_all(build_improve_staging_tooldefs(
        staging_dir=staging_dir, source_snapshot_dir=source_dir))
    names = registry.names()

    calls = {
        "list_staging": {},
        "write_staging_file": {"name": "rsi_v2", "rel": "plugin.py", "content": "x = 1\n"},
        "read_staging_file": {"name": "rsi_v2", "rel": "plugin.py"},
        "read_plugin_source": {"name": "rsi_v1"},
        "run_plugin_tests": {"name": "does_not_exist"},
    }
    for name, args in calls.items():
        raw = registry.execute(name, args, names)
        loaded = json.loads(raw)
        assert not isinstance(loaded, str), (
            f"{name}: agent が受け取った値が二重エンコードされた JSON 文字列 "
            f"のまま (loaded={loaded!r})")


def test_write_staging_file_rejects_symlinked_candidate_dir(tmp_path):
    """7-B M3 (検収 問4): `_NAME_RE` が `.`/`/` を弾くため `name` への
    `../` は M1 で落ち M3 (`resolve()` 後の traversal 検査) には到達しない。
    有効な `name` だが実体が staging 外を指す symlink だけが M3 を殺せる。"""
    staging_dir = tmp_path / "staging"; staging_dir.mkdir()
    source_dir = tmp_path / "source"; source_dir.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    # 有効な name (正規形を満たす) だが実体は staging の外を指す symlink
    (staging_dir / "evil").symlink_to(outside, target_is_directory=True)

    tools = {t.name: t for t in build_improve_staging_tooldefs(
        staging_dir=staging_dir, source_snapshot_dir=source_dir)}
    out = tools["write_staging_file"].func(
        name="evil", rel="plugin.py", content="PWNED")
    assert "error" in out, out
    assert not (outside / "plugin.py").exists(), "staging 外へ書き込まれた"


def test_run_plugin_tests_includes_stderr_in_tail(tmp_path):
    """実機 E2E (2026-08-30 mission #9) 是正: pytest が収集前に死ぬと
    (Landlock 下の rootdir 遡り PermissionError 等)、死因は stderr にしか
    出ないのに旧実装は stdout だけ返し `stdout_tail:""` の盲目デバッグに
    なっていた。stderr も tail に含めること。"""
    tools, staging_dir, _ = _build(tmp_path)
    (staging_dir / "crash_case").mkdir()
    (staging_dir / "crash_case" / "test_plugin.py").write_text(
        "import nonexistent_module_xyz\n")
    out = tools["run_plugin_tests"].func(name="crash_case")
    assert out["passed"] is False
    assert "nonexistent_module_xyz" in out["stdout_tail"]


def test_run_plugin_tests_confines_pytest_to_candidate_dir(tmp_path):
    """同上: rootdir/confcutdir 未指定だと pytest が候補 dir の外
    (リポジトリ root の pyproject.toml 等、Landlock allowlist 外) へ遡って
    PermissionError で死ぬ。--rootdir と --confcutdir を候補 dir に固定し、
    cwd も候補 dir にすることを argv/cwd で pin する。"""
    import subprocess as _subprocess
    captured = {}
    orig_run = _subprocess.run

    def spy_run(argv, **kw):
        captured["argv"] = argv
        captured["cwd"] = kw.get("cwd")
        return orig_run(argv, **kw)

    tools, staging_dir, _ = _build(tmp_path)
    (staging_dir / "pin_case").mkdir()
    (staging_dir / "pin_case" / "test_plugin.py").write_text(
        "def test_x():\n    assert True\n")
    import agentic_fx.tools.improve_staging_tools as mod
    orig = mod.subprocess.run
    mod.subprocess.run = spy_run
    try:
        out = tools["run_plugin_tests"].func(name="pin_case")
    finally:
        mod.subprocess.run = orig
    assert out["passed"] is True
    joined = " ".join(map(str, captured["argv"]))
    assert "--rootdir" in joined
    assert "--confcutdir" in joined or captured["cwd"] is not None


def test_failure_signature_uses_real_outputs_and_ignores_assert_values():
    fixtures = Path(__file__).resolve().parents[1] / "fixtures" / "selftest_loop"
    m60 = [_failure_signature((fixtures / f"m60_run_plugin_tests_{i}.txt").read_text())
           for i in range(28)]
    m58 = _failure_signature((fixtures / "m58_run_plugin_tests_0.txt").read_text())
    assert len(set(m60)) == 1
    assert m58 != m60[0]
    assert _failure_signature(
        (fixtures / "m58_run_plugin_tests_1.txt").read_text()) == (
            "<no-tests>", "", "")


@pytest.mark.parametrize("output", ["", "Traceback\nImportError: broken\n"])
def test_failure_signature_without_failed_lines_is_stable(output):
    assert _failure_signature(output) == ("<no-tests>", "", "")


def test_failure_signature_takes_first_custom_exception_per_failure_block():
    output = """___ test_one ___
E       Boom: first
E       ValueError: chained
___ test_two ___
E       SystemExit: second
FAILED test_plugin.py::test_one
FAILED test_plugin.py::test_two
"""
    assert _failure_signature(output)[1] == ("Boom", "SystemExit")


def test_write_staging_file_rejects_only_after_budget_is_used(tmp_path):
    staging = tmp_path / "staging"; staging.mkdir()
    source = tmp_path / "source"; source.mkdir()
    counters = MissionToolCounters()
    budget = ImproveToolBudgetSettings(max_writes=2)
    tools = {t.name: t for t in build_improve_staging_tooldefs(
        staging_dir=staging, source_snapshot_dir=source,
        counters=counters, budget=budget)}
    assert tools["write_staging_file"].func("a", "plugin.py", "x") == {"ok": True}
    assert tools["write_staging_file"].func("a", "plugin.py", "y") == {"ok": True}
    rejected = tools["write_staging_file"].func("a", "plugin.py", "z")
    assert rejected["error"] == "budget exhausted"
    assert rejected["budget"] == "max_writes"


def test_run_plugin_tests_timeout_is_counted(monkeypatch, tmp_path):
    staging = tmp_path / "staging"; source = tmp_path / "source"
    staging.mkdir(); source.mkdir(); (staging / "a").mkdir()
    (staging / "a" / "test_plugin.py").write_text("def test_x(): pass\n")
    counters = MissionToolCounters()
    tools = {t.name: t for t in build_improve_staging_tooldefs(
        staging_dir=staging, source_snapshot_dir=source,
        counters=counters, budget=ImproveToolBudgetSettings())}
    monkeypatch.setattr(
        "agentic_fx.tools.improve_staging_tools.subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("pytest", 120)))
    assert tools["run_plugin_tests"].func("a") == {
        "passed": False, "stdout_tail": "pytest timeout (120s)"}
    assert counters.self_test_runs == 1


def test_repeated_timeouts_include_repeated_failure(monkeypatch, tmp_path):
    staging = tmp_path / "staging"; source = tmp_path / "source"
    staging.mkdir(); source.mkdir(); (staging / "broken").mkdir()
    (staging / "broken" / "test_plugin.py").write_text("def test_x(): pass\n")
    counters = MissionToolCounters()
    tools = {t.name: t for t in build_improve_staging_tooldefs(
        staging_dir=staging, source_snapshot_dir=source, counters=counters,
        budget=ImproveToolBudgetSettings(self_test_warn_after=3))}
    monkeypatch.setattr(
        "agentic_fx.tools.improve_staging_tools.subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("pytest", 120)))
    for _ in range(2):
        assert "repeated_failure" not in tools["run_plugin_tests"].func("broken")
    out = tools["run_plugin_tests"].func("broken")
    assert out["repeated_failure"]["consecutive"] == 3
    assert out["repeated_failure"]["failed_tests"] == ["<no-tests>"]


def test_strategy_self_test_requires_successful_backtest_before_repeating(tmp_path):
    staging = tmp_path / "staging"; staging.mkdir()
    source = tmp_path / "source"; source.mkdir()
    example = Path(__file__).resolve().parents[2] / "docs/examples/plugins/sma_cross"
    shutil.copytree(example, staging / "candidate")
    with (staging / "candidate" / "test_plugin.py").open("a") as f:
        f.write("\ndef test_deliberate_failure():\n    assert 'hold' == 'open'\n")
    counters = MissionToolCounters()
    tools = {t.name: t for t in build_improve_staging_tooldefs(
        staging_dir=staging, source_snapshot_dir=source, counters=counters,
        budget=ImproveToolBudgetSettings())}

    assert tools["run_plugin_tests"].func("candidate")["passed"] is False
    assert tools["run_plugin_tests"].func("candidate")["error"] == \
        "run_backtest_required_first"
    counters.record_backtest("candidate", ok=False)
    assert tools["run_plugin_tests"].func("candidate")["error"] == \
        "run_backtest_required_first"
    counters.record_backtest("candidate", ok=True)
    second = tools["run_plugin_tests"].func("candidate")
    third = tools["run_plugin_tests"].func("candidate")
    assert second["passed"] is False
    assert third["repeated_failure"]["consecutive"] == 3
    assert third["repeated_failure"]["last_values"] == [
        "assert 'hold' == 'open'"]


def test_strategy_name_rotation_cannot_evade_pre_backtest_budget(tmp_path):
    staging = tmp_path / "staging"; staging.mkdir()
    source = tmp_path / "source"; source.mkdir()
    example = Path(__file__).resolve().parents[2] / "docs/examples/plugins/sma_cross"
    counters = MissionToolCounters()
    tools = {t.name: t for t in build_improve_staging_tooldefs(
        staging_dir=staging, source_snapshot_dir=source, counters=counters,
        budget=ImproveToolBudgetSettings(max_self_tests_before_backtest=3))}
    for name in ("one", "two", "three", "four"):
        shutil.copytree(example, staging / name)

    for name in ("one", "two", "three"):
        assert "error" not in tools["run_plugin_tests"].func(name)
    assert tools["run_plugin_tests"].func("four")["error"] == \
        "run_backtest_required_first"


def test_loader_rejected_candidate_is_not_subject_to_backtest_order(tmp_path):
    staging = tmp_path / "staging"; staging.mkdir()
    source = tmp_path / "source"; source.mkdir()
    candidate = staging / "broken"; candidate.mkdir()
    (candidate / "plugin.py").write_text("this is invalid python")
    (candidate / "test_plugin.py").write_text("def test_x(): assert False\n")
    counters = MissionToolCounters()
    tools = {t.name: t for t in build_improve_staging_tooldefs(
        staging_dir=staging, source_snapshot_dir=source, counters=counters,
        budget=ImproveToolBudgetSettings())}
    assert tools["run_plugin_tests"].func("broken")["passed"] is False
    assert tools["run_plugin_tests"].func("broken")["passed"] is False


# --- 段 0 変異スイープ pin (2026-09-07、指揮者): 生存 3 件のうち 2 件 ---

def test_failure_signature_ignores_assert_actual_values():
    """M7 pin: `E   assert 'hold' == 'open'` の実値だけが変わっても署名は同一
    (設計 v4 Tier A: 実値は署名に含めない — 値が漸進するだけの無進捗ループを
    同一失敗として数えるため)。"""
    fixtures = Path(__file__).resolve().parents[1] / "fixtures" / "selftest_loop"
    base = (fixtures / "m60_run_plugin_tests_0.txt").read_text()
    assert "assert 'hold' == 'open'" in base
    varied = base.replace("assert 'hold' == 'open'", "assert 'hold' == 'flat'")
    assert varied != base
    assert _failure_signature(varied) == _failure_signature(base)


def test_run_plugin_tests_rejects_exactly_at_max_self_test_runs(monkeypatch, tmp_path):
    """M2 pin: max_self_test_runs=2 なら 3 回目が budget exhausted (off-by-one)。
    strategy でない候補 (plugin.py 無し → loader 不成立) で Tier B を外す。"""
    staging = tmp_path / "staging"; source = tmp_path / "source"
    staging.mkdir(); source.mkdir(); (staging / "a").mkdir()
    (staging / "a" / "test_plugin.py").write_text("def test_x(): pass\n")
    counters = MissionToolCounters()
    tools = {t.name: t for t in build_improve_staging_tooldefs(
        staging_dir=staging, source_snapshot_dir=source, counters=counters,
        budget=ImproveToolBudgetSettings(max_self_test_runs=2, self_test_warn_after=1,
                                         max_self_tests_before_backtest=1))}
    monkeypatch.setattr(
        "agentic_fx.tools.improve_staging_tools.subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("pytest", 120)))
    assert tools["run_plugin_tests"].func("a")["passed"] is False
    assert tools["run_plugin_tests"].func("a")["passed"] is False
    third = tools["run_plugin_tests"].func("a")
    assert third == {"error": "budget exhausted", "budget": "max_self_test_runs",
                     "directive": BUDGET_EXHAUSTED_DIRECTIVE}
    assert counters.self_test_runs == 2


def test_indicator_candidate_is_not_subject_to_backtest_order(tmp_path):
    """codex 1 周目 Important 3: loader 上 kind=indicator の候補は Tier B
    (run_backtest_required_first) の対象外 — 2 回目以降の self-test も通る。"""
    staging = tmp_path / "staging"; staging.mkdir()
    source = tmp_path / "source"; source.mkdir()
    example = Path(__file__).resolve().parents[2] / "docs/examples/plugins/rsi_indicator"
    shutil.copytree(example, staging / "rsi_v2")
    counters = MissionToolCounters()
    tools = {t.name: t for t in build_improve_staging_tooldefs(
        staging_dir=staging, source_snapshot_dir=source, counters=counters,
        budget=ImproveToolBudgetSettings(max_self_tests_before_backtest=1))}
    for _ in range(3):
        out = tools["run_plugin_tests"].func("rsi_v2")
        assert "error" not in out
        assert out["passed"] is True
    assert counters.self_tests_before_backtest == 0


def test_repeated_failure_directive_reports_configured_count(monkeypatch, tmp_path):
    """codex 1 周目 Minor 4: directive の回数は設定 (self_test_warn_after) に従い、
    固定の「3 回」ではない。"""
    staging = tmp_path / "staging"; source = tmp_path / "source"
    staging.mkdir(); source.mkdir(); (staging / "a").mkdir()
    (staging / "a" / "test_plugin.py").write_text("def test_x(): pass\n")
    counters = MissionToolCounters()
    tools = {t.name: t for t in build_improve_staging_tooldefs(
        staging_dir=staging, source_snapshot_dir=source, counters=counters,
        budget=ImproveToolBudgetSettings(self_test_warn_after=2))}
    monkeypatch.setattr(
        "agentic_fx.tools.improve_staging_tools.subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("pytest", 120)))
    tools["run_plugin_tests"].func("a")
    second = tools["run_plugin_tests"].func("a")
    assert "2 回続けて失敗" in second["repeated_failure"]["directive"]
    assert "3 回" not in second["repeated_failure"]["directive"]
