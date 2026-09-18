"""improve registry の候補置き場ツール (設計書 §3.4/§2.3)。"""
from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
import shutil
from pathlib import Path

import pytest

from agentic_fx.config import ImproveToolBudgetSettings
from agentic_fx.tools import improve_staging_tools
from agentic_fx.tools.improve_staging_tools import (
    BUDGET_EXHAUSTED_DIRECTIVE, NO_TESTS_SIGNATURE, TIMEOUT_SIGNATURE,
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
        (fixtures / "m58_run_plugin_tests_1.txt").read_text()) == NO_TESTS_SIGNATURE


@pytest.mark.parametrize("output", ["", "Traceback\nImportError: broken\n"])
def test_failure_signature_without_failed_lines_is_stable(output):
    assert _failure_signature(output) == NO_TESTS_SIGNATURE


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
        "passed": False, "stdout_tail": "pytest timeout (120s)",
        "remaining_budget": {"self_tests": 11}}
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
    assert out["repeated_failure"]["failed_tests"] == ["<timeout>"]
    # /code-review 2 周目 (2026-09-07): 実行不能 (timeout / 収集エラー) は assert
    # 失敗向けの directive を出さない
    assert "実行できていません" in out["repeated_failure"]["directive"]
    assert "最終バー" not in out["repeated_failure"]["directive"]


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
    counters.record_backtest_result("candidate", ok=False)
    assert tools["run_plugin_tests"].func("candidate")["error"] == \
        "run_backtest_required_first"
    counters.record_backtest_result("candidate", ok=True)
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
    assert "2 回続けて" in second["repeated_failure"]["directive"]
    assert "3 回" not in second["repeated_failure"]["directive"]


# --- ローカル 1 周目 pin (2026-09-07、tmp/review-20260907-st/verified-round1-local.md) ---

# 追加 2: 挿入先: 新規関数 `test_repeated_failure_last_values_are_capped_at_five`。
# ファイル先頭に追加: from types import SimpleNamespace
# 以下 2 定数はモジュールレベル
_MANY_ASSERTS = "".join(
    f"E       AssertionError: assert {i} == 0\n" for i in range(1, 8)
) + "FAILED test_plugin.py::test_x\n"


def test_repeated_failure_last_values_are_capped_at_five(monkeypatch, tmp_path):
    """ローカル 1 周目 (ornith c1): `last_values` の 5 件上限を撤去する変異が
    緑で生存していた — 既存 fixture は assert 失敗行が 1 本しかなく上限を
    踏まない。失敗 assert が多い候補で tool 応答が肥大するのを防ぐ枠なので
    上限そのものを pin する。"""
    staging = tmp_path / "staging"; source = tmp_path / "source"
    staging.mkdir(); source.mkdir(); (staging / "a").mkdir()
    (staging / "a" / "test_plugin.py").write_text("def test_x(): assert 0\n")
    counters = MissionToolCounters()
    tools = {t.name: t for t in build_improve_staging_tooldefs(
        staging_dir=staging, source_snapshot_dir=source, counters=counters,
        budget=ImproveToolBudgetSettings(self_test_warn_after=1))}
    monkeypatch.setattr(
        "agentic_fx.tools.improve_staging_tools.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout=_MANY_ASSERTS, stderr=""))

    out = tools["run_plugin_tests"].func("a")
    assert out["passed"] is False
    assert out["repeated_failure"]["last_values"] == [
        f"assert {i} == 0" for i in range(1, 6)]

# 追加 3: 挿入先: 新規関数 `test_failure_signature_distinguishes_assert_source_expression`。
# ファイル先頭 (import 群の下、モジュールレベル) に置く
_SIG_HEAD = """=================================== FAILURES ===================================
_____________________________ test_cross_signal ______________________________
"""
_SIG_TAIL = """E       AssertionError: assert 'hold' == 'open'
=========================== short test summary info ============================
FAILED test_plugin.py::test_cross_signal - AssertionError: assert 'hold' == 'o'
"""


def test_failure_signature_distinguishes_assert_source_expression():
    """ローカル 1 周目 (qwen c1): 署名 3 成分のうち `assertions` (`>` 行) だけを
    落とす変異が緑で生存していた。node id と例外型が同じで `>` 行の式だけが違う
    2 出力で署名が異なることを pin する — テスト側を書き換えて別の assert に
    したのに「同じ失敗が続いている」と誤警告する退行を防ぐ。"""
    a = _SIG_HEAD + ">       assert signal == 'open'\n" + _SIG_TAIL
    b = _SIG_HEAD + ">       assert last_bar_signal(df) == 'open'\n" + _SIG_TAIL
    sig_a, sig_b = _failure_signature(a), _failure_signature(b)
    assert sig_a[0] == sig_b[0]                          # 失敗 node id は同じ
    assert sig_a[1] == sig_b[1] == ("AssertionError",)   # 例外型も同じ
    assert sig_a[2] != sig_b[2]                          # assert ソース式だけが違う
    assert sig_a != sig_b


@pytest.mark.parametrize("kwargs", [
    {"counters": MissionToolCounters()},
    {"budget": ImproveToolBudgetSettings()},
])
def test_staging_builder_rejects_half_wiring(tmp_path, kwargs):
    """codex 2 周目 (2026-09-07): counters と budget は対。片方だけは予算が
    静かに無効化される配線ミスなので ValueError (fail closed)。"""
    with pytest.raises(ValueError):
        build_improve_staging_tooldefs(
            staging_dir=tmp_path, source_snapshot_dir=tmp_path, **kwargs)



# --- /code-review high 2 周目 (2026-09-07、tmp/review-20260907-st-r2/code-review-high.md) の pin ---

def test_collection_error_repeated_failure_lists_sentinel_not_characters(monkeypatch, tmp_path):
    """#1: FAILED 行の無い失敗 (収集エラー) の repeated_failure.failed_tests は
    ['<no-tests>'] であって文字分割 ['<','n','o',...] ではない。directive は
    実行不能向け。"""
    staging = tmp_path / "staging"; source = tmp_path / "source"
    staging.mkdir(); source.mkdir(); (staging / "a").mkdir()
    (staging / "a" / "test_plugin.py").write_text("import nonexistent_module_xyz\n")
    counters = MissionToolCounters()
    tools = {t.name: t for t in build_improve_staging_tooldefs(
        staging_dir=staging, source_snapshot_dir=source, counters=counters,
        budget=ImproveToolBudgetSettings(self_test_warn_after=2))}
    tools["run_plugin_tests"].func("a")
    out = tools["run_plugin_tests"].func("a")
    assert out["passed"] is False
    assert out["repeated_failure"]["failed_tests"] == ["<no-tests>"]
    assert "実行できていません" in out["repeated_failure"]["directive"]


def test_timeout_and_collection_error_are_distinct_signatures(monkeypatch, tmp_path):
    """#3: timeout と収集エラーは別署名 — 交互に起きても連続回数が積み上がらない。"""
    staging = tmp_path / "staging"; source = tmp_path / "source"
    staging.mkdir(); source.mkdir(); (staging / "a").mkdir()
    (staging / "a" / "test_plugin.py").write_text("import nonexistent_module_xyz\n")
    counters = MissionToolCounters()
    tools = {t.name: t for t in build_improve_staging_tooldefs(
        staging_dir=staging, source_snapshot_dir=source, counters=counters,
        budget=ImproveToolBudgetSettings(self_test_warn_after=2))}
    real_run = subprocess.run
    tools["run_plugin_tests"].func("a")                      # 収集エラー (1)
    monkeypatch.setattr(
        "agentic_fx.tools.improve_staging_tools.subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("pytest", 120)))
    out = tools["run_plugin_tests"].func("a")                # timeout (1)
    assert "repeated_failure" not in out
    monkeypatch.setattr("agentic_fx.tools.improve_staging_tools.subprocess.run", real_run)
    out = tools["run_plugin_tests"].func("a")                # 収集エラー (1)
    assert "repeated_failure" not in out
    assert counters.self_test_runs == 3


def test_oserror_from_pytest_launch_is_a_delivered_failure(monkeypatch, tmp_path):
    """#2: subprocess.run が OSError を投げても、予約を消費した 1 回として
    通常の失敗応答 (passed=False + stdout_tail) を返す — registry の汎用
    error で結果が届かないまま予約だけ燃える形にしない。"""
    staging = tmp_path / "staging"; source = tmp_path / "source"
    staging.mkdir(); source.mkdir(); (staging / "a").mkdir()
    (staging / "a" / "test_plugin.py").write_text("def test_x(): pass\n")
    counters = MissionToolCounters()
    tools = {t.name: t for t in build_improve_staging_tooldefs(
        staging_dir=staging, source_snapshot_dir=source, counters=counters,
        budget=ImproveToolBudgetSettings())}
    monkeypatch.setattr(
        "agentic_fx.tools.improve_staging_tools.subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(OSError(12, "Cannot allocate memory")))
    out = tools["run_plugin_tests"].func("a")
    assert out["passed"] is False
    assert "could not start" in out["stdout_tail"]
    assert counters.self_test_runs == 1


def test_write_budget_refusal_counts_toward_terminal_streak(tmp_path):
    """段 0 pin A17 (2026-09-08): write の budget exhausted も terminal 拒否として
    streak に積む (self-test / backtest と同じ扱い)。"""
    staging = tmp_path / "staging"; source = tmp_path / "source"
    staging.mkdir(); source.mkdir()
    budget = ImproveToolBudgetSettings(max_writes=1, max_refusal_streak=3)
    counters = MissionToolCounters(budget=budget)
    tools = {t.name: t for t in build_improve_staging_tooldefs(
        staging_dir=staging, source_snapshot_dir=source,
        counters=counters, budget=budget)}
    assert tools["write_staging_file"].func("a", "plugin.py", "x") == {"ok": True}
    for _ in range(3):
        assert tools["write_staging_file"].func("a", "plugin.py", "y")["error"] == "budget exhausted"
    assert counters.terminal_refusal_streak == 3
    assert counters.abort_pending is True
    assert counters.abort_trigger == "terminal_refusals"


# --- ローカル 1 周目 pin (2026-09-08、tmp/review-20260908-ma/verified-round1-local.md) ---

# P3 / P4
def test_pre_backtest_rejection_feeds_the_recoverable_streak(tmp_path):
    """ローカル 1 周目 #3 (muse c4 / qwen c4): `run_backtest_required_first`
    の拒否が recoverable streak に積まれ、閾値で abort に至る唯一の経路。
    `counters.record_recoverable_refusal(...)` の 2 行を削除する変異が全
    スイート green で生存していた (拒否経路自体は既存テストが踏むが、
    streak を誰も見ていなかった) — これが無いと設計が v1 の穴と呼ぶ
    「self-test 拒否だけを叩き続けるループ」が timeout まで止まらない。"""
    staging = tmp_path / "staging"; staging.mkdir()
    source = tmp_path / "source"; source.mkdir()
    example = Path(__file__).resolve().parents[2] / "docs/examples/plugins/sma_cross"
    shutil.copytree(example, staging / "cand")
    budget = ImproveToolBudgetSettings(max_refusal_streak=3)
    counters = MissionToolCounters(budget=budget)
    tools = {t.name: t for t in build_improve_staging_tooldefs(
        staging_dir=staging, source_snapshot_dir=source,
        counters=counters, budget=budget)}
    assert "error" not in tools["run_plugin_tests"].func("cand")
    key = ("cand", "run_backtest_required_first")
    for n in (1, 2):
        assert tools["run_plugin_tests"].func("cand")["error"] == \
            "run_backtest_required_first"
        assert counters.recoverable_refusal_streak[key] == n
        assert not counters.abort_pending
    assert tools["run_plugin_tests"].func("cand")["error"] == \
        "run_backtest_required_first"
    assert counters.recoverable_refusal_streak[key] == 3
    assert counters.abort_pending
    assert counters.abort_trigger == "recoverable_refusals:cand"


@pytest.mark.parametrize("exc", [
    subprocess.TimeoutExpired("pytest", 120),
    OSError(12, "Cannot allocate memory"),
])
def test_unrun_self_test_does_not_reset_the_terminal_refusal_streak(
        monkeypatch, tmp_path, exc):
    """ローカル 1 周目 #4 (muse c4 / qwen c4): terminal streak のリセットは
    「pytest が実際に走った」応答のみ (設計 §1)。`if 'result' in locals():`
    を外して無条件に `record_progress` する変異が全スイート green で生存
    していた — 既存 2 テストは戻り値の辞書等値比較しか見ていない。無条件化
    すると timeout ループが毎回 streak を 0 に戻し abort が永久に発火しない。"""
    staging = tmp_path / "staging"; staging.mkdir()
    source = tmp_path / "source"; source.mkdir()
    (staging / "a").mkdir()
    (staging / "a" / "test_plugin.py").write_text("def test_x(): pass\n")
    budget = ImproveToolBudgetSettings(max_writes=1, max_refusal_streak=10)
    counters = MissionToolCounters(budget=budget)
    tools = {t.name: t for t in build_improve_staging_tooldefs(
        staging_dir=staging, source_snapshot_dir=source,
        counters=counters, budget=budget)}
    assert tools["write_staging_file"].func("a", "plugin.py", "x") == {"ok": True}
    assert tools["write_staging_file"].func("a", "plugin.py", "y")["error"] == \
        "budget exhausted"
    assert counters.terminal_refusal_streak == 1
    monkeypatch.setattr(
        "agentic_fx.tools.improve_staging_tools.subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(exc))
    assert tools["run_plugin_tests"].func("a")["passed"] is False
    assert counters.terminal_refusal_streak == 1



def test_tier_b_stays_armed_after_unevaluable_backtest(tmp_path):
    """run7 欠陥 A の E2E (staging tool 実物): 空振り backtest (evaluable=false) の後も
    strategy 候補の 2 回目 self-test は run_backtest_required_first で拒否される。
    trade が 1 本でも出た backtest の後は許可される (evaluable=False でも)。"""
    from agentic_fx.tools.improve_rpc_tools import build_improve_rpc_tooldefs
    from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
    staging = tmp_path / "staging"; staging.mkdir()
    source = tmp_path / "source"; source.mkdir()
    example = Path(__file__).resolve().parents[2] / "docs/examples/plugins/sma_cross"
    shutil.copytree(example, staging / "cand")
    budget = ImproveToolBudgetSettings()
    counters = MissionToolCounters(budget=budget)
    st = {t.name: t for t in build_improve_staging_tooldefs(
        staging_dir=staging, source_snapshot_dir=source, counters=counters, budget=budget)}
    replies = [{"metrics": {"trades": 0, "evaluable": False}},
               {"metrics": {"trades": 4, "evaluable": False}}]   # 4 本 = 低頻度でも実行された
    rpc = {t.name: t for t in build_improve_rpc_tooldefs(
        ledger=ImproveRpcLedger(rpc_timeout_sec_by_kind={"run_backtest": 600.0}),
        staging_dir=staging, run_backtest_handler=lambda args: replies.pop(0),
        analyze_corr_handler=lambda args: {}, counters=counters, budget=budget)}
    assert "error" not in st["run_plugin_tests"].func("cand")          # 1 回目は許可
    assert st["run_plugin_tests"].func("cand")["error"] == "run_backtest_required_first"
    rpc["run_backtest"].func("cand", "USDJPY")                           # 空振り
    assert st["run_plugin_tests"].func("cand")["error"] == "run_backtest_required_first"
    rpc["run_backtest"].func("cand", "USDJPY")                           # evaluable
    assert "error" not in st["run_plugin_tests"].func("cand")


# --- [indicator-consumption-wiring] T5a Step 5-2: 露出 tool ------------------------

_VIEW = {
    "plugins": [
        {"name": "rsi", "kind": "indicator", "pairs": [],
         "params": {"period": 14}, "outputs": ["rsi"], "content_hash": "a" * 64},
        {"name": "legacy", "kind": "indicator", "pairs": [],
         "params": {"period": 14}, "outputs": None, "content_hash": "b" * 64},
    ],
    "pin_broken_strategies": [
        {"name": "s_old", "alias": "rsi", "reason": "pin_mismatch"}],
}


def _tools(tmp_path, view=_VIEW):
    defs = improve_staging_tools.build_improve_staging_tooldefs(
        staging_dir=tmp_path / "staging",
        source_snapshot_dir=tmp_path / "snap", inventory_view=view)
    return {d.name: d.func for d in defs}


def _field_names(value, *, skip_keys=("params",)):
    """`value` 以下に現れる **辞書のキー名**を再帰的に集める。
    `skip_keys` に挙げたキーの**配下は降りない** (plugin 作者が決める
    自由な名前空間なので、遮断 8 の語彙表と衝突しうる)。"""
    names = set()
    if isinstance(value, dict):
        for k, v in value.items():
            names.add(k)
            if k in skip_keys:
                continue
            names |= _field_names(v, skip_keys=skip_keys)
    elif isinstance(value, list):
        for item in value:
            names |= _field_names(item, skip_keys=skip_keys)
    return names


def test_list_deployed_plugins_returns_the_view_verbatim(tmp_path):
    out = _tools(tmp_path)["list_deployed_plugins"]()
    assert out["plugins"] == _VIEW["plugins"]
    assert out["pin_broken_strategies"] == _VIEW["pin_broken_strategies"]
    # 遮断 8: 成績・期間・段の**フィールド名**が view に現れないこと。
    #
    # **codex plan r1 C5 是正**: v1.2 は `json.dumps(out)` の全文に
    # `"period"` が含まれないことを assert していたが、同じ `_VIEW` が
    # `params: {"period": 14}` を持っており **正しい出力でも必ず落ちる**
    # (判別力ゼロ)。設計書 §2.9b / §5 は **`params` の露出を明示的に許可**
    # しているので、これは遮断 8 との混同でもある。検査対象を
    # **トップレベルのフィールド名 (`params` 配下は除外)** に絞る。
    forbidden = {"pf", "avg_r", "win_rate", "max_drawdown", "total_pnl",
                 "trades", "in_sample", "holdout", "scope", "period",
                 "period_start", "period_end", "baseline", "metrics"}
    assert _field_names(out) & forbidden == set()
    # `params` 配下の `"period"` は許可されている (plugin 作者の名前空間)
    assert out["plugins"][0]["params"] == {"period": 14}


def test_list_deployed_plugins_marks_outputs_none_as_not_dependable(tmp_path):
    """U4: outputs なし = 依存先にできない — その事実が view から読める。"""
    out = _tools(tmp_path)["list_deployed_plugins"]()
    legacy = next(p for p in out["plugins"] if p["name"] == "legacy")
    assert legacy["outputs"] is None


def test_lock_staging_deps_writes_pins_from_the_view(tmp_path):
    """P1 (改善経路): view の content_hash を pin に書く。
    staging・examples は参照しない。"""
    from tests.fixtures import indicator_wiring as fx
    staging = tmp_path / "staging"
    fx.write_rsi_pullback(staging, pins=None)
    view = {"plugins": [{"name": "rsi", "kind": "indicator", "pairs": [],
                         "params": {"period": 14}, "outputs": ["rsi"],
                         "content_hash": "c" * 64}],
            "pin_broken_strategies": []}
    out = _tools(tmp_path, view)["lock_staging_deps"]("rsi_pullback")
    assert out["ok"] is True
    assert out["pins"] == {"rsi": "c" * 64}
    assert out["changed"] is True
    # ユーザー裁定 2026-09-14 ⑥: 返り値の content_hash は lock 後に disk から
    # 再計算した snapshot 値 (`check_candidate_snapshot` はこれを保証しない)。
    from agentic_fx.plugin.loader import content_hash as _content_hash
    assert out["content_hash"] == _content_hash(staging / "rsi_pullback")
    import yaml
    cfg = yaml.safe_load((staging / "rsi_pullback" / "config.yaml").read_text())
    assert cfg["indicators"]["rsi"]["pin"] == "c" * 64


def test_lock_staging_deps_is_idempotent(tmp_path):
    from tests.fixtures import indicator_wiring as fx
    staging = tmp_path / "staging"
    fx.write_rsi_pullback(staging, pins={"rsi": "c" * 64})
    view = {"plugins": [{"name": "rsi", "kind": "indicator", "pairs": [],
                         "params": {"period": 14}, "outputs": ["rsi"],
                         "content_hash": "c" * 64}],
            "pin_broken_strategies": []}
    out = _tools(tmp_path, view)["lock_staging_deps"]("rsi_pullback")
    assert out["ok"] is True and out["changed"] is False


def test_lock_staging_deps_refuses_unknown_dependency(tmp_path):
    from tests.fixtures import indicator_wiring as fx
    staging = tmp_path / "staging"
    fx.write_rsi_pullback(staging, pins=None)
    view = {"plugins": [], "pin_broken_strategies": []}
    out = _tools(tmp_path, view)["lock_staging_deps"]("rsi_pullback")
    assert out["error"] == "indicator_unresolved"
    assert out["alias"] == "rsi" and out["reason"] == "not_found"
    assert out["available"] == []


def test_lock_staging_deps_refuses_outputs_undeclared_dependency(tmp_path):
    """codex plan r2 束4 Important 是正: `_tools(tmp_path)` の**既定引数**
    (`view=_VIEW`) に頼ると、`_VIEW` の中身が別の理由で変わったときに
    この受入がこっそり `not_found` へ後退しても誰も気づけない
    (`_VIEW` はモジュールレベル共有 fixture — `test_list_deployed_plugins_*`
    等、他の複数テストとも共用している)。**このテストだけが読む
    `inventory_view` を明示的にローカルで組み立て**、U4b
    (`outputs_undeclared`) 分岐に実際に到達することを自己完結で保証する。"""
    from tests.fixtures import indicator_wiring as fx
    staging = tmp_path / "staging"
    d = fx.write_rsi_pullback(staging, pins=None)
    import yaml
    cfg = yaml.safe_load((d / "config.yaml").read_text())
    cfg["indicators"]["rsi"]["plugin"] = "legacy"
    (d / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    view = {"plugins": [
                {"name": "legacy", "kind": "indicator", "pairs": [],
                 "params": {"period": 14}, "outputs": None,
                 "content_hash": "b" * 64}],
            "pin_broken_strategies": []}
    out = _tools(tmp_path, view)["lock_staging_deps"]("rsi_pullback")
    assert out["reason"] == "outputs_undeclared"


def test_lock_staging_deps_refuses_non_indicator_dependency(tmp_path):
    """1 周目 ローカル LLM (c15 muse): `not_indicator` 分岐に到達する受入。

    既存は `not_found` (依存が view に無い) と `outputs_undeclared`
    (`outputs is None`) しか踏まないため、`if dep["kind"] != "indicator":`
    の分岐を丸ごと削除する変異が生存した (実測: tests/tools/
    test_improve_staging_tools.py + tests/loops/test_improve_loop_source_snapshot.py
    + tests/loops/test_improve_e2e.py で 117 passed)。この変異下では
    **strategy を依存先に指した候補に pin が書かれてしまい**、
    `lock_staging_deps` が ok を返す。`available` が indicator だけを
    挙げることも併せて pin する (agent への誘導が壊れると kind 違いの
    依存を書き直させられない)。
    """
    from tests.fixtures import indicator_wiring as fx
    staging = tmp_path / "staging"
    d = fx.write_rsi_pullback(staging, pins=None)
    import yaml
    cfg = yaml.safe_load((d / "config.yaml").read_text())
    cfg["indicators"]["rsi"]["plugin"] = "other_strategy"
    (d / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    view = {"plugins": [
                {"name": "other_strategy", "kind": "strategy",
                 "pairs": ["USDJPY"], "params": {}, "outputs": ["x"],
                 "content_hash": "d" * 64},
                {"name": "rsi", "kind": "indicator", "pairs": [],
                 "params": {"period": 14}, "outputs": ["rsi"],
                 "content_hash": "c" * 64}],
            "pin_broken_strategies": []}
    out = _tools(tmp_path, view)["lock_staging_deps"]("rsi_pullback")
    assert out["error"] == "indicator_unresolved"
    assert out["alias"] == "rsi" and out["reason"] == "not_indicator"
    assert out["available"] == ["rsi"]
    assert "ok" not in out and "pins" not in out


def test_lock_staging_deps_rejects_a_non_strategy_candidate(tmp_path):
    """1 周目 ローカル LLM (c15 qwen Minor): 候補自身の kind ガード。

    既存は依存側 (`dep["kind"]`) しか踏まないため、候補自身の
    `if meta.kind != "strategy":` を丸ごと削除する変異が 105 passed で
    生存した。indicator 候補は `meta.indicators` が空なので、この変異下では
    ループを 0 周して `lock_config(candidate_dir, {})` に落ち、
    **`{"ok": True, "pins": {}, "changed": False}` を返してしまう** —
    agent は「indicator にも pin を打てた」と誤解する。
    """
    from tests.fixtures import indicator_wiring as fx
    staging = tmp_path / "staging"
    fx.write_indicator(staging, "rsi")
    out = _tools(tmp_path, _VIEW)["lock_staging_deps"]("rsi")
    assert out["error"] == ("lock_staging_deps is only for kind=strategy "
                            "candidates")
    assert out["candidate_kind"] == "indicator"
    assert "ok" not in out and "pins" not in out


def test_lock_staging_deps_rejects_an_unknown_or_unsafe_candidate_name(tmp_path):
    """1 周目 ローカル LLM (c15 qwen Minor): 候補ディレクトリのガード。

    `if candidate_dir is None or not candidate_dir.is_dir():` を削除する
    変異が 67 passed で生存した。`_safe_join` が `None` を返す名前
    (traversal 形) では、この変異下で `discover_one_with_reason(None, ...)`
    に `None` が渡り**ツールが例外で落ちる** (error 辞書を返す契約が壊れ、
    agent が回復できない)。存在しない名前と traversal 形の両方を踏む。
    """
    tools = _tools(tmp_path, _VIEW)
    for name in ("does_not_exist", "../escape"):
        out = tools["lock_staging_deps"](name)
        assert out["error"] == "not found"
        assert "list_staging" in out["hint"]


def test_lock_staging_deps_keeps_the_candidate_discoverable(tmp_path):
    from agentic_fx.plugin.loader import discover_one_with_reason
    from tests.fixtures import indicator_wiring as fx
    staging = tmp_path / "staging"
    fx.write_rsi_pullback(staging, pins=None)
    view = {"plugins": [{"name": "rsi", "kind": "indicator", "pairs": [],
                         "params": {"period": 14}, "outputs": ["rsi"],
                         "content_hash": "c" * 64}],
            "pin_broken_strategies": []}
    _tools(tmp_path, view)["lock_staging_deps"]("rsi_pullback")
    meta, reason = discover_one_with_reason(staging / "rsi_pullback",
                                            "rsi_pullback")
    assert reason is None and meta.indicators[0].pin == "c" * 64


def test_lock_staging_deps_rolls_back_when_the_locked_config_stops_loading(
        tmp_path, monkeypatch):
    """[indicator-consumption-wiring] 段 0 束 3 M8: `lock_staging_deps` は
    pin を書いた**後**に `discover_one_with_reason` を通し、通らなければ
    `config.yaml` を書き戻して `loader_rejected_after_lock` を返す。

    この再検査ブロック (と `content_hash` 一致 assert) を丸ごと削る変異は
    判定 suite 全体 (1355 passed) が green のままだった (実測) —
    `test_lock_staging_deps_keeps_the_candidate_discoverable` は **tool の
    外で** discover し直すだけで、tool 自身が再検査しているかは見ていない。
    ユーザー裁定 2026-09-14 ⑥ (「lock 後に無条件で submit が通ると決め打ち
    しない」) が構造的に守られていることを pin する。"""
    from agentic_fx.plugin import loader as plugin_loader
    from tests.fixtures import indicator_wiring as fx
    staging = tmp_path / "staging"
    cand = fx.write_rsi_pullback(staging, pins=None)
    before = (cand / "config.yaml").read_text(encoding="utf-8")

    real = plugin_loader.discover_one_with_reason
    calls = {"n": 0}

    def _fail_on_second(plugin_dir, name):
        calls["n"] += 1
        if calls["n"] >= 2:
            return None, "synthetic post-lock failure"
        return real(plugin_dir, name)

    monkeypatch.setattr(improve_staging_tools.plugin_loader,
                        "discover_one_with_reason", _fail_on_second)
    out = _tools(tmp_path)["lock_staging_deps"]("rsi_pullback")
    assert out.get("error", "").startswith("loader_rejected_after_lock:"), out
    assert (cand / "config.yaml").read_text(encoding="utf-8") == before


def test_lock_staging_deps_rolls_back_when_the_relocked_hash_disagrees(
        tmp_path, monkeypatch):
    """1 周目 ローカル LLM (c15 ornith Important、指揮者裁定 2026-09-18 で
    採用): `lock_config` が返す `new_hash` と `discover` の再取得値の
    食い違いは **`assert` ではなく明示チェック**で扱う。

    `assert relocked.content_hash == new_hash` は `python -O` (最適化
    起動) で**消える**ので、防御として数えられない。両者は書き込み後の
    disk を独立に読むので、食い違い = 書き込みと再読取のあいだに何かが
    起きた (TOCTOU) ということであり、pin を書いたまま先へ進めてはいけない。
    段 0 束 3 M8 の `..._rolls_back_when_the_locked_config_stops_loading`
    と同じ形 — `config.yaml` を書き戻し、error 辞書を返す — に写像する。
    """
    from agentic_fx.plugin import loader as plugin_loader
    from tests.fixtures import indicator_wiring as fx
    staging = tmp_path / "staging"
    cand = fx.write_rsi_pullback(staging, pins=None)
    before = (cand / "config.yaml").read_text(encoding="utf-8")

    real = plugin_loader.discover_one_with_reason
    calls = {"n": 0}

    import dataclasses

    def _skew_hash_on_second(plugin_dir, name):
        calls["n"] += 1
        meta, reason = real(plugin_dir, name)
        if calls["n"] >= 2 and meta is not None:
            meta = dataclasses.replace(meta, content_hash="f" * 64)
        return meta, reason

    monkeypatch.setattr(improve_staging_tools.plugin_loader,
                        "discover_one_with_reason", _skew_hash_on_second)
    out = _tools(tmp_path)["lock_staging_deps"]("rsi_pullback")
    assert out.get("error", "").startswith("lock_hash_mismatch:"), out
    assert "ok" not in out and "pins" not in out
    assert (cand / "config.yaml").read_text(encoding="utf-8") == before
