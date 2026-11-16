"""clear から latched へ変える書き手と、その呼出し元の全数 pin。

clear→latched の writer は core_lock 下か replay の単一 thread だけ。
新しい書き手や呼出し元が増えたら、ここが赤になる。
"""
import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SOURCE_DIRS = ("src/agentic_fx", "afx-ops/afx_ops", "mt5_bridge")


def _files():
    for d in SOURCE_DIRS:
        root = REPO / d
        if root.exists():
            yield from sorted(root.rglob("*.py"))


class _Scan(ast.NodeVisitor):
    """各 Call を (相対 path, 外側の qualname, node, 祖先の With 式) で集める。"""

    def __init__(self, rel):
        self.rel = rel
        self.names = []
        self.withs = []
        self.calls = []

    def _scope(self, node):
        self.names.append(node.name)
        self.generic_visit(node)
        self.names.pop()

    visit_FunctionDef = _scope
    visit_AsyncFunctionDef = _scope
    visit_ClassDef = _scope

    def visit_With(self, node):
        self.withs.append([ast.unparse(i.context_expr) for i in node.items])
        for child in node.body:
            self.visit(child)
        self.withs.pop()

    def visit_Call(self, node):
        self.calls.append((self.rel, ".".join(self.names), node,
                           [w for ws in self.withs for w in ws]))
        self.generic_visit(node)


def _calls():
    out = []
    for path in _files():
        scan = _Scan(path.relative_to(REPO).as_posix())
        scan.visit(ast.parse(path.read_text(encoding="utf-8")))
        out.extend(scan.calls)
    return out


def _is_false(node):
    return isinstance(node, ast.Constant) and node.value is False


def _attr(node):
    return node.func.attr if isinstance(node.func, ast.Attribute) else None


def test_every_call_that_can_set_latched_true_is_enumerated():
    found = set()
    for rel, scope, node, _ in _calls():
        for kw in node.keywords:
            if kw.arg == "kill_switch_latched" and not _is_false(kw.value):
                found.add((rel, scope))
    assert found == {
        # 通常・signal・replay の open が DD で発動する唯一の executor 経路
        ("src/agentic_fx/core/executor.py", "Executor._evaluate_and_execute_open"),
        # gate の入力へ現在の値を写す読取
        ("src/agentic_fx/core/executor.py", "Executor._open"),
        ("src/agentic_fx/core/executor.py", "Executor.open_from_snapshot"),
        # replay の解除失敗時の補償 (単一 thread)
        ("src/agentic_fx/backtest/runner.py", "_auto_release_kill_switch"),
        # marker の投影 (読取) と marker 中の確定
        ("src/agentic_fx/store/state.py", "GuardedState.projected"),
        ("src/agentic_fx/store/state.py", "StateStore.load"),
        ("src/agentic_fx/store/state.py", "StateStore.confirm_latched"),
        # 文書の値をそのまま写す構築
        ("src/agentic_fx/store/state.py", "parse_state"),
    }


def test_reset_marker_has_a_single_writer_and_only_reset_calls_it():
    found = {(rel, scope) for rel, scope, node, _ in _calls()
             if _attr(node) == "_write_marker"}
    assert found == {("src/agentic_fx/store/state.py",
                      "StateStore.reset_kill_switch")}


def test_reset_and_confirm_have_only_the_known_callers():
    names = {"reset_kill_switch", "confirm_latched"}
    found = {(rel, scope, _attr(node)) for rel, scope, node, _ in _calls()
             if _attr(node) in names}
    assert found == {
        ("src/agentic_fx/commands.py", "Commands._dispatch",
         "reset_kill_switch"),
        ("src/agentic_fx/commands.py", "Commands._killswitch_reconcile",
         "confirm_latched"),
        ("src/agentic_fx/ops/api_server.py", "_call", "reset_kill_switch"),
        ("src/agentic_fx/ops/service.py", "OpsService.reset_kill_switch.action",
         "reset_kill_switch"),
        ("src/agentic_fx/ops/service.py",
         "OpsService.reconcile_kill_switch.action", "confirm_latched"),
    }


def test_state_updates_by_keyword_are_limited_to_known_fields():
    receivers = {"state", "self.state", "self._state_store", "store"}
    found = set()
    for rel, scope, node, _ in _calls():
        if _attr(node) != "update":
            continue
        if ast.unparse(node.func.value) not in receivers:
            continue
        found.add((rel, scope, tuple(sorted(k.arg or "**" for k in node.keywords)),
                   len(node.args)))
    assert found == {
        ("src/agentic_fx/service.py", "run_init", ("initialized",), 0),
        ("src/agentic_fx/service.py", "run_init",
         ("autopilot", "initialized", "mode"), 0),
        ("src/agentic_fx/ops_setup/stages.py", "OpsSetup._set_stage", (), 1),
        ("src/agentic_fx/ops_setup/web_write.py", "_switch",
         ("write_principal", "write_step", "write_target"), 0),
        ("src/agentic_fx/backtest/runner.py", "_auto_release_kill_switch",
         ("kill_switch_latched", "publish", "transition_snapshot_id"), 0),
        ("src/agentic_fx/backtest/runner.py", "_auto_release_kill_switch",
         ("kill_switch_latched", "publish"), 0),
        ("src/agentic_fx/setup/wizard.py", "_commit", ("initialized",), 0),
        ("src/agentic_fx/setup/wizard.py", "_commit",
         ("autopilot", "initialized", "mode"), 0),
        ("src/agentic_fx/core/executor.py", "Executor._evaluate_and_execute_open",
         ("kill_switch_latched",), 0),
    }


def test_open_entry_points_have_only_the_known_callers():
    entries = {"open_from_snapshot", "handle_intent"}
    found = {(rel, scope, _attr(node)) for rel, scope, node, _ in _calls()
             if _attr(node) in entries}
    assert found == {
        ("src/agentic_fx/loops/trade_loop.py", "TradeLoop._run_once_impl",
         "open_from_snapshot"),
        ("src/agentic_fx/loops/trade_loop.py", "TradeLoop._run_signal_mission",
         "open_from_snapshot"),
        ("src/agentic_fx/backtest/runner.py", "run_replay", "handle_intent"),
    }


def test_executor_open_evaluation_has_only_the_two_known_entry_points():
    found = {(rel, scope) for rel, scope, node, _ in _calls()
             if _attr(node) == "_evaluate_and_execute_open"}
    assert found == {
        ("src/agentic_fx/core/executor.py", "Executor._open"),
        ("src/agentic_fx/core/executor.py", "Executor.open_from_snapshot"),
    }


def test_executor_open_has_only_handle_intent_as_caller():
    found = {(rel, scope) for rel, scope, node, _ in _calls()
             if _attr(node) == "_open"}
    assert found == {("src/agentic_fx/core/executor.py",
                      "Executor.handle_intent")}


def test_live_open_callers_are_under_the_core_lock():
    live = [(scope, withs) for rel, scope, node, withs in _calls()
            if rel == "src/agentic_fx/loops/trade_loop.py"
            and _attr(node) == "open_from_snapshot"]
    assert len(live) == 2
    for scope, withs in live:
        assert any("_core_lock" in w for w in withs), scope


def test_replay_open_runs_in_a_single_threaded_module():
    source = (REPO / "src/agentic_fx/backtest/runner.py").read_text(
        encoding="utf-8")
    assert "threading" not in source
    assert "Thread" not in source
    assert "concurrent" not in source
    assert "kill_switch_hwm_rebases" not in source
    assert "rebase_accounting" not in source


def test_scheduler_tick_reads_state_but_never_writes_it():
    path = REPO / "src/agentic_fx/core/scheduler.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    used = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute)
                and ast.unparse(node.value) == "self.state"):
            used.add(node.attr)
        if isinstance(node, ast.Call):
            assert all(kw.arg != "kill_switch_latched" for kw in node.keywords)
            assert _attr(node) not in {
                "update", "save", "reset_kill_switch", "confirm_latched",
                "_write_marker", "publish_latched", "publish_released"} or (
                    ast.unparse(node.func.value) not in
                    {"self.state", "self.executor.state"})
    assert used <= {"load", "shared_guard", "_monotonic"}
