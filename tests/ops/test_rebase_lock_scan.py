"""HWM 付け直しの lock 順の source scan (範囲は core 取得箇所と state 排他からの逆辺だけ)。

core lock は ops lane の内側でだけ取る。core を持ったまま ops lane / OpsService を呼ぶ辺、
state の排他保持から core を取る辺、rebase の共有 guard 内で state の lock を取り直す辺を禁じる。"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "agentic_fx"
OPS_ATTRS = frozenset({"_ops_lock", "_ops_lane", "_mutate"})
OPS_RECEIVERS = frozenset({"ops", "ops_service", "self.ops", "self._ops", "app.ops",
                           "self.ops_service", "self._ops_service"})
STATE_LOCKING_CALLS = frozenset({
    "load", "autopilot_at_start", "file_value", "_exclusive", "_shared",
    "reset_kill_switch", "confirm_latched", "shared_guard", "_current_state",
    "_guard_step", "_sweep_kill_switch", "emit_pending_guard_notices",
    "emit_pending_notices", "_ops_lane"})


def _with_blocks(tree: ast.AST, marker: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.With) and any(
                marker in ast.unparse(item.context_expr) for item in node.items):
            yield node


def _body_nodes(node: ast.With):
    return ast.walk(ast.Module(body=node.body, type_ignores=[]))


def core_to_ops_edges(source: str) -> list[int]:
    hits = []
    for block in _with_blocks(ast.parse(source), "core_lock"):
        for sub in _body_nodes(block):
            if isinstance(sub, ast.Attribute) and sub.attr in OPS_ATTRS:
                hits.append(sub.lineno)
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                    and ast.unparse(sub.func.value) in OPS_RECEIVERS):
                hits.append(sub.lineno)
    return hits


def state_exclusive_to_core_edges(source: str) -> list[int]:
    hits = []
    for block in _with_blocks(ast.parse(source), "_exclusive"):
        for sub in _body_nodes(block):
            if isinstance(sub, (ast.Name, ast.Attribute)) and "core_lock" in ast.unparse(sub):
                hits.append(sub.lineno)
    return hits


def _function(source: str, name: str) -> ast.FunctionDef:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(name)


def locking_calls_in(source: str, function: str) -> set[str]:
    calls = set()
    for sub in ast.walk(_function(source, function)):
        if not (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)):
            continue
        # state store の書込み (update / save) は dict.update と区別して受け手の名前で見る。
        if sub.func.attr in STATE_LOCKING_CALLS or (
                sub.func.attr in ("update", "save") and "state" in ast.unparse(sub.func.value)):
            calls.add(sub.func.attr)
    return calls


def test_scanners_detect_the_forbidden_edges_in_synthetic_source():
    assert core_to_ops_edges("with self._core_lock:\n    app.ops.status(1)\n") == [2]
    assert core_to_ops_edges("with core_lock:\n    x = self._ops_lane(d)\n") == [2]
    assert core_to_ops_edges("with core_lock:\n    pass\napp.ops.status(1)\n") == []
    assert state_exclusive_to_core_edges(
        "with store._exclusive():\n    with core_lock:\n        pass\n") != []
    assert locking_calls_in("def f(guard):\n    store.load()\n", "f") == {"load"}
    assert locking_calls_in("def f(s):\n    s._state_store.update(x=1)\n", "f") == {"update"}
    assert locking_calls_in("def f(s):\n    fields.update(x=1)\n", "f") == set()


def test_no_core_lock_holder_reaches_the_ops_lane_or_ops_service():
    offenders = {}
    for path in sorted(SRC.rglob("*.py")):
        hits = core_to_ops_edges(path.read_text(encoding="utf-8"))
        if hits:
            offenders[str(path.relative_to(SRC))] = hits
    assert offenders == {}


def test_no_state_exclusive_holder_takes_the_core_lock():
    offenders = {}
    for path in sorted(SRC.rglob("*.py")):
        hits = state_exclusive_to_core_edges(path.read_text(encoding="utf-8"))
        if hits:
            offenders[str(path.relative_to(SRC))] = hits
    assert offenders == {}


def test_rebase_critical_section_takes_no_state_lock_or_sweep():
    source = (SRC / "ops" / "service.py").read_text(encoding="utf-8")
    # core 取得 → 共有 guard → 書込みの区間と、その中で呼ぶ関数。
    assert locking_calls_in(source, "_rebase_commit") == set()
    under_core = locking_calls_in(source, "_rebase_under_core")
    assert under_core == {"shared_guard"}  # 共有 guard を取るのはここだけ
    evaluator = (SRC / "ops" / "rebase_service.py").read_text(encoding="utf-8")
    assert locking_calls_in(evaluator, "evaluate_rebase") == set()


def test_ops_lane_is_taken_before_core_in_the_rebase_post():
    source = (SRC / "ops" / "service.py").read_text(encoding="utf-8")
    method = ast.unparse(_function(source, "rebase_kill_switch"))
    assert "_mutate" in method and "core_lock" not in method  # core は ops lane の内側の action だけ
    under_core = ast.unparse(_function(source, "_rebase_under_core"))
    assert under_core.index("core_lock") < under_core.index("shared_guard")


def test_preview_never_takes_the_core_lock():
    source = (SRC / "ops" / "service.py").read_text(encoding="utf-8")
    assert "core_lock" not in ast.unparse(_function(source, "rebase_preview"))
    assert "core_lock" not in ast.unparse(_function(source, "_rebase_preview_state"))
