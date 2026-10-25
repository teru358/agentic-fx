"""終端の全 site が分岐ごとの固定語を渡すことを、ソースの構造 (AST) で確かめる。

行番号は固定せず、関数名と渡す式で識別する。"""
from __future__ import annotations

import ast
import collections
from pathlib import Path

import pytest

import agentic_fx
from agentic_fx.loops import improve_loop
from agentic_fx.loops import improve_notice_codes as codes

SRC = Path(agentic_fx.__file__).parent
LOOP_PATH = Path(improve_loop.__file__)
TREE = ast.parse(LOOP_PATH.read_text(encoding="utf-8"))

_PARENT: dict[ast.AST, ast.AST] = {}
for _node in ast.walk(TREE):
    for _child in ast.iter_child_nodes(_node):
        _PARENT[_child] = _node


def _func_of(node: ast.AST) -> str:
    cur = node
    while cur in _PARENT:
        cur = _PARENT[cur]
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur.name
    return "<module>"


def _call_name(call: ast.Call) -> str | None:
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)


def _calls(name: str) -> list[ast.Call]:
    return [n for n in ast.walk(TREE)
            if isinstance(n, ast.Call) and _call_name(n) == name]


def _kw(call: ast.Call, key: str) -> ast.expr | None:
    for k in call.keywords:
        if k.arg == key:
            return k.value
    return None


def _funcdef(name: str) -> ast.FunctionDef:
    found = [n for n in ast.walk(TREE)
             if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(found) == 1, name
    return found[0]


def _param_has_default(func: ast.FunctionDef, param: str) -> bool:
    args = func.args
    positional = args.posonlyargs + args.args
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    for a, d in zip(positional, defaults):
        if a.arg == param:
            return d is not None
    for a, d in zip(args.kwonlyargs, args.kw_defaults):
        if a.arg == param:
            return d is not None
    raise AssertionError(f"{func.name} has no parameter {param}")


def _notice_names() -> set[str]:
    return {n for n in dir(codes) if n.startswith("NOTICE_")}


# 変換表の各 site が渡す式 (関数名, notice= の式)。
EXPECTED_FINISH_SITES = sorted([
    ("_compensate_prepare_failure", "notice"),
    ("_compensate_tx2_failure", "NOTICE_APPROVAL_TX_FAILED"),
    ("_finalize_report_or_observation", "notice"),
    ("_finalize_failed_mission", "failed_class.notice"),
    ("_finalize_gate_sandbox_unavailable", "NOTICE_SANDBOX_UNAVAILABLE"),
    ("_finalize_output_invalid", "NOTICE_OUTPUT_INVALID"),
    ("_finalize_loser", "NOTICE_DUPLICATE_SKIPPED"),
    ("_finalize_loser", "NOTICE_REPORT_WRITE_FAILED"),
    ("_finalize_gate_failed", "notice"),
    ("_finalize_gate_failed", "notice"),
    ("_finalize_success", "NOTICE_SUBMITTED"),
    ("compensate_commit_failure", "NOTICE_COMMIT_CRASHED"),
    ("_prepare_report_if_applicable", "NOTICE_REPORT_WRITE_FAILED"),
])

EXPECTED_GATE_FAILED_CALLS = sorted([
    "notice_for_plugin_gate(gate_verdict.code)",
    "NOTICE_LOADER_REJECTED",
    "NOTICE_UNVERIFIED",
    "NOTICE_MAX_BARS_LIMIT",
    "NOTICE_KIND_UNSUPPORTED",
    "NOTICE_OUTPUTS_REQUIRED",
    "NOTICE_INDICATOR_UNRESOLVED",
    "NOTICE_BACKTEST_DATA_UNAVAILABLE",
    "notice_for_unevaluable_kind(strategy_verdict.unevaluable_kind)",
    "NOTICE_UNPROFITABLE",
])
EXPECTED_REPORT_OR_OBSERVATION_CALLS = sorted([
    "NOTICE_DEMOTED_RECOVERED",
    "notice_for_quality(demotion.code)",
    "plain_notice",
])
EXPECTED_COMPENSATE_PREPARE_CALLS = sorted([
    "NOTICE_PREPARE_FAILED", "NOTICE_LAUNCH_CRASHED"])


def test_finish_improve_mission_call_sites_match_the_table_exactly():
    calls = _calls("finish_improve_mission")
    assert len(calls) == 13
    actual = sorted((_func_of(c), ast.unparse(_kw(c, "notice")))
                    for c in calls if _kw(c, "notice") is not None)
    assert actual == EXPECTED_FINISH_SITES


def test_every_finish_call_passes_notice_tool_calls_and_observation_text():
    for call in _calls("finish_improve_mission"):
        for key in ("notice", "tool_calls", "observation_text"):
            assert _kw(call, key) is not None, (_func_of(call), key)


def test_observation_text_is_non_null_only_through_the_report_or_observation_helper():
    for call in _calls("finish_improve_mission"):
        value = ast.unparse(_kw(call, "observation_text"))
        if _func_of(call) == "_finalize_report_or_observation":
            assert value == "observation_text"
        else:
            assert value == "None", _func_of(call)


def _is_allowed_notice_expr(expr: ast.expr, func_name: str) -> bool:
    text = ast.unparse(expr)
    if isinstance(expr, ast.Name) and expr.id in _notice_names():
        return True
    if isinstance(expr, ast.Call) and _call_name(expr) in {
            "notice_for_plugin_gate", "notice_for_quality",
            "notice_for_unevaluable_kind"}:
        return True
    func = _funcdef(func_name) if func_name != "<module>" else None
    if text == "notice" and func is not None and any(
            a.arg == "notice" for a in func.args.kwonlyargs + func.args.args):
        return True
    # selector / 分類関数の戻り値から取り出した式
    if isinstance(expr, ast.Attribute) and expr.attr == "notice":
        return True
    if isinstance(expr, ast.Name) and expr.id in {"plain_notice"}:
        return True
    return False


def test_every_notice_argument_is_a_constant_a_closed_lookup_or_a_selector_result():
    names = ("finish_improve_mission", "_finalize_gate_failed",
             "_finalize_report_or_observation", "_compensate_prepare_failure")
    for name in names:
        for call in _calls(name):
            expr = _kw(call, "notice")
            assert expr is not None, (name, _func_of(call), call.lineno)
            assert _is_allowed_notice_expr(expr, _func_of(call)), (
                name, ast.unparse(expr))


def test_plain_and_failed_notice_names_are_bound_from_the_single_classifiers():
    bound = {}
    for node in ast.walk(TREE):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            fn = _call_name(node.value)
            for target in node.targets:
                for name in ast.walk(target):
                    if isinstance(name, ast.Name):
                        bound.setdefault(name.id, set()).add(fn)
    assert bound["plain_notice"] == {"_plain_outcome_notice"}
    assert bound["failed_class"] == {"_classify_failed_mission"}


def test_gate_failed_helper_call_sites_each_pass_their_own_notice():
    calls = _calls("_finalize_gate_failed")
    assert {_func_of(c) for c in calls} == {"commit"}
    assert sorted(ast.unparse(_kw(c, "notice")) for c in calls) \
        == EXPECTED_GATE_FAILED_CALLS


def test_report_or_observation_helper_call_sites_each_pass_their_own_notice():
    calls = _calls("_finalize_report_or_observation")
    assert {_func_of(c) for c in calls} == {"commit"}
    assert sorted(ast.unparse(_kw(c, "notice")) for c in calls) \
        == EXPECTED_REPORT_OR_OBSERVATION_CALLS


def test_prepare_failure_compensation_call_sites_each_pass_their_own_notice():
    calls = _calls("_compensate_prepare_failure")
    assert sorted((_func_of(c), ast.unparse(_kw(c, "notice"))) for c in calls) \
        == sorted([("prepare", "NOTICE_PREPARE_FAILED"),
                   ("compensate_launch_failure", "NOTICE_LAUNCH_CRASHED")])


@pytest.mark.parametrize("helper", [
    "_finalize_gate_failed", "_finalize_report_or_observation",
    "_compensate_prepare_failure"])
def test_helper_notice_parameter_is_required(helper):
    assert not _param_has_default(_funcdef(helper), "notice")


def test_finish_improve_mission_notice_and_tool_calls_are_required():
    path = SRC / "store" / "missions.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    func = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef)
                and n.name == "finish_improve_mission")
    for a, d in zip(func.args.kwonlyargs, func.args.kw_defaults):
        if a.arg in ("notice", "tool_calls"):
            assert d is None, a.arg


def test_every_notice_constant_is_placed_by_a_site_or_a_closed_dictionary():
    missions_tree = ast.parse(
        (SRC / "store" / "missions.py").read_text(encoding="utf-8"))
    referenced = {
        n.id for tree in (TREE,) for n in ast.walk(tree)
        if isinstance(n, ast.Name) and n.id in _notice_names()}
    referenced |= {
        n.attr for n in ast.walk(missions_tree)
        if isinstance(n, ast.Attribute) and n.attr in _notice_names()}
    dictionary_values = (set(codes._PLUGIN_GATE_NOTICES.values())
                         | set(codes._QUALITY_NOTICES.values())
                         | set(codes._UNEVALUABLE_NOTICES.values()))
    unplaced = {name for name in _notice_names() - referenced
                if getattr(codes, name) not in dictionary_values}
    assert unplaced == set()


# --- plugin gate / 質検査の固定語 -------------------------------------------

def test_plugin_gate_failures_carry_a_distinct_fixed_code_per_semantic_branch():
    gate = _funcdef("_run_plugin_gate")
    seen: list[str] = []
    for call in ast.walk(gate):
        if not isinstance(call, ast.Call):
            continue
        name = _call_name(call)
        if name == "_PluginGateVerdict":
            passed = _kw(call, "passed")
            if isinstance(passed, ast.Constant) and passed.value is True:
                assert _kw(call, "code") is None
                continue
            if _func_of(call) != "_run_plugin_gate":
                continue
            value = _kw(call, "code")
        elif name in ("_failed_before_pytest", "_failed_after_pytest"):
            value = _kw(call, "code")
        else:
            continue
        assert isinstance(value, ast.Constant) and isinstance(value.value, str)
        seen.append(value.value)
    # 不合格 return は 10 か所、意味分岐ごとに別の語
    assert sorted(seen) == sorted(codes.PLUGIN_GATE_CODES)
    assert len(seen) == 10


def test_plugin_gate_wrappers_take_code_without_a_default():
    gate = _funcdef("_run_plugin_gate")
    wrappers = {n.name: n for n in ast.walk(gate)
                if isinstance(n, ast.FunctionDef)
                and n.name in ("_failed_before_pytest", "_failed_after_pytest")}
    assert set(wrappers) == {"_failed_before_pytest", "_failed_after_pytest"}
    for wrapper in wrappers.values():
        assert not _param_has_default(wrapper, "code")


def test_plugin_gate_every_return_is_a_verdict_expression():
    gate = _funcdef("_run_plugin_gate")
    for ret in (n for n in ast.walk(gate) if isinstance(n, ast.Return)
                and _func_of(n) == "_run_plugin_gate"):
        assert isinstance(ret.value, ast.Call)
        assert _call_name(ret.value) in (
            "_PluginGateVerdict", "_failed_before_pytest", "_failed_after_pytest")


def test_quality_rejections_pass_a_fixed_code_and_wrappers_require_it():
    for wrapper in ("_quality_rejection", "_quality_unavailable"):
        assert not _param_has_default(_funcdef(wrapper), "code"), wrapper
    used = collections.Counter()
    for name in ("_quality_rejection", "_quality_unavailable"):
        for call in _calls(name):
            value = _kw(call, "code")
            assert value is not None, (name, call.lineno)
            if _func_of(call) == "_quality_unavailable":
                assert ast.unparse(value) == "code"
                continue
            assert isinstance(value, ast.Constant), ast.unparse(call)
            assert value.value in codes.QUALITY_CODES
            used[value.value] += 1
    assert set(used) == codes.QUALITY_CODES
    verdicts = [c for c in _calls("_QualityVerdict")]
    for call in verdicts:
        if call.keywords or call.args:
            assert _kw(call, "outcome") is not None and _kw(call, "code") is not None
        else:
            assert _func_of(call) == "_check_candidate_quality"


def test_the_verdict_dataclasses_refuse_an_unlabelled_failure():
    with pytest.raises(ValueError):
        improve_loop._PluginGateVerdict(passed=False, reason="x")
    with pytest.raises(ValueError):
        improve_loop._PluginGateVerdict(passed=False, reason="x", code="bogus")
    with pytest.raises(ValueError):
        improve_loop._PluginGateVerdict(passed=True, code="pytest_failed")
    improve_loop._PluginGateVerdict(passed=True)
    with pytest.raises(ValueError):
        improve_loop._QualityVerdict(outcome="duplicate_logic")
    with pytest.raises(ValueError):
        improve_loop._QualityVerdict(outcome="duplicate_logic", code="bogus")
    with pytest.raises(ValueError):
        improve_loop._QualityVerdict(code="duplicate_logic")
    assert improve_loop._QualityVerdict().code is None


# --- 文字列から分類を作らない -------------------------------------------------

NOTICE_BUILDING_FUNCTIONS = (
    "commit", "_finalize_gate_failed", "_finalize_report_or_observation",
    "_finalize_failed_mission", "_finalize_gate_sandbox_unavailable",
    "_finalize_output_invalid", "_finalize_loser", "_finalize_success",
    "_prepare_report_if_applicable", "_compensate_prepare_failure",
    "_compensate_tx2_failure", "compensate_commit_failure",
    "compensate_launch_failure", "_plain_outcome_notice",
    "_classify_failed_mission", "_run_plugin_gate", "_check_candidate_quality",
    "_quality_rejection", "_quality_unavailable")


def test_no_string_splitting_in_the_functions_that_choose_a_notice():
    for name in NOTICE_BUILDING_FUNCTIONS:
        for call in ast.walk(_funcdef(name)):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
                assert call.func.attr not in {
                    "split", "rsplit", "partition", "rpartition", "startswith",
                    "endswith", "removeprefix", "removesuffix"}, (
                        name, call.func.attr)


def test_the_only_prefix_test_on_an_observation_reason_is_the_selector():
    hits = [c for c in ast.walk(TREE)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
            and c.func.attr == "startswith"
            and any(isinstance(a, ast.Tuple)
                    and {getattr(e, "value", None) for e in a.elts}
                    == {"family_redirect:", "duplicate"} for a in c.args)]
    assert [_func_of(h) for h in hits] == ["_select_observation"]


# --- agent から読めない -----------------------------------------------------

AGENT_REACHABLE = (
    [SRC / "loops" / name for name in (
        "improve_loop.py", "improve_context.py", "improve_rpc_ledger.py",
        "improve_run_context.py", "improve_quality.py", "approval_facts.py")]
    + [SRC / "mission_worker.py"]
    + sorted((SRC / "tools").rglob("*.py"))
    + sorted((SRC / "plugin").rglob("*.py"))
    + sorted((SRC / "runners").rglob("*.py"))
    + sorted((SRC / "backtest").rglob("*.py")))


def test_agent_reachable_modules_never_mention_the_snapshot_table():
    assert LOOP_PATH in AGENT_REACHABLE
    assert any(p.name == "improve_rpc_tools.py" for p in AGENT_REACHABLE)
    assert any(p.name == "sandbox.py" for p in AGENT_REACHABLE)
    offenders = [str(p) for p in AGENT_REACHABLE
                 if "improve_run_notices" in p.read_text(encoding="utf-8")]
    assert offenders == []


def test_the_snapshot_is_inserted_only_by_the_terminal_helper_module():
    writers = [str(p.relative_to(SRC)) for p in SRC.rglob("*.py")
               if "INTO improve_run_notices" in p.read_text(encoding="utf-8")]
    assert writers == ["store/missions.py"]
