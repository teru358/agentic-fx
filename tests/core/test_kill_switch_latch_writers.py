"""clear から latched へ変える書き手と、その呼出し元の全数 pin。

clear→latched の writer は core_lock 下か replay の単一 thread だけ。
新しい書き手や呼出し元が増えたら、ここが赤になる。
"""
import ast
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SOURCE_DIRS = ("src/agentic_fx", "afx-ops/afx_ops", "mt5_bridge")


_SKIPPED_PARTS = frozenset({".venv", "site-packages", "__pycache__"})


def _python_files(root):
    for path in sorted(root.rglob("*.py")):
        if _SKIPPED_PARTS.isdisjoint(path.relative_to(root).parts):
            yield path


def _files():
    for d in SOURCE_DIRS:
        root = REPO / d
        if root.exists():
            yield from _python_files(root)


def test_scan_skips_virtualenvs_and_caches(tmp_path):
    (tmp_path / "mod.py").write_text("x = 1\n")
    for hidden in (".venv/lib/python3/site-packages/pkg", "nested/.venv/lib",
                   "site-packages/pkg", "__pycache__"):
        directory = tmp_path / hidden
        directory.mkdir(parents=True)
        (directory / "broken.py").write_text("def (:\n")
    assert [p.name for p in _python_files(tmp_path)] == ["mod.py"]


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


def _bag(items):
    """同じ呼出しが同じ scope に 2 回あっても 2 と数える多重集合。"""
    return Counter(items)


def _keyword_items(node):
    """(キー名, 値 node) の列。`**{"k": v}` の dict literal は展開して見る。

    展開できない `**x` は キー名 "**" で返す。
    """
    for kw in node.keywords:
        if kw.arg is not None:
            yield kw.arg, kw.value
        elif isinstance(kw.value, ast.Dict):
            for key, value in zip(kw.value.keys, kw.value.values):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    yield key.value, value
                else:
                    yield "**", value
        else:
            yield "**", kw.value


def test_every_call_that_can_set_latched_true_is_enumerated():
    found = []
    for rel, scope, node, _ in _calls():
        for key, value in _keyword_items(node):
            if key == "kill_switch_latched" and not _is_false(value):
                found.append((rel, scope))
    assert _bag(found) == _bag({
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
    })


def test_reset_marker_has_a_single_writer_and_only_reset_calls_it():
    found = [(rel, scope) for rel, scope, node, _ in _calls()
             if _attr(node) == "_write_marker"]
    assert _bag(found) == _bag({("src/agentic_fx/store/state.py",
                                 "StateStore.reset_kill_switch")})


def test_reset_and_confirm_have_only_the_known_callers():
    names = {"reset_kill_switch", "confirm_latched"}
    found = [(rel, scope, _attr(node)) for rel, scope, node, _ in _calls()
             if _attr(node) in names]
    assert _bag(found) == _bag({
        ("src/agentic_fx/commands.py", "Commands._dispatch",
         "reset_kill_switch"),
        ("src/agentic_fx/commands.py", "Commands._killswitch_reconcile",
         "confirm_latched"),
        ("src/agentic_fx/ops/api_server.py", "_call", "reset_kill_switch"),
        ("src/agentic_fx/ops/service.py", "OpsService.reset_kill_switch.action",
         "reset_kill_switch"),
        ("src/agentic_fx/ops/service.py",
         "OpsService.reconcile_kill_switch.action", "confirm_latched"),
    })


def test_state_updates_by_keyword_are_limited_to_known_fields():
    """receiver の名前には頼らず、キーワードを取る `update` 呼出しを全部拾う。

    StateStore.update は keyword だけを取る。位置引数の dict.update は
    知られた receiver (state の dict) の分だけ拾う。キーワード付きで拾う
    非 state の呼出しは下の一覧で許可する。
    """
    dict_receivers = {"state", "self.state", "self._state_store", "store"}
    found = []
    for rel, scope, node, _ in _calls():
        if _attr(node) != "update":
            continue
        keys = tuple(sorted(k for k, _ in _keyword_items(node)))
        if not keys and ast.unparse(node.func.value) not in dict_receivers:
            continue
        found.append((rel, scope, keys, len(node.args)))
    assert _bag(found) == _bag({
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
        # 以下は state ではない (dict / 別 store の update)
        ("src/agentic_fx/backtest/runner.py", "_RecordingStateStore.update",
         ("**",), 0),
        ("src/agentic_fx/egress/handoff.py", "SyncHandoff._judge",
         ("accepted_inode", "client_inode", "client_state", "server_inode",
          "server_state"), 0),
        ("src/agentic_fx/ops/change.py", "_shrink_to_reason_only",
         ("claims",), 0),
        ("src/agentic_fx/ops/change.py", "_shrink_to_reason_only",
         ("items",), 0),
        ("src/agentic_fx/ops/rebase_service.py", "evaluate_rebase",
         ("generation", "latched", "latched_at"), 0),
        ("src/agentic_fx/ops/rebase_service.py", "evaluate_rebase",
         ("source_snapshot_id", "source_snapshot_source",
          "source_snapshot_ts"), 0),
        ("src/agentic_fx/ops/rebase_service.py", "evaluate_rebase",
         ("balance", "equity", "hwm"), 0),
        ("src/agentic_fx/ops/rebase_service.py", "evaluate_rebase",
         ("additional_headroom", "count_after", "cumulative_cut_after",
          "cumulative_cut_pct_after", "cumulative_warning", "cut", "cut_pct",
          "new_hwm", "past_count", "past_cut", "past_cut_pct",
          "warning_threshold_pct"), 0),
        ("src/agentic_fx/plugin/deploy_ops.py", "write_intent_rows",
         ("actor", "approval_id", "commit", "now", "revoke", "revoke_row_id"),
         0),
        ("src/agentic_fx/plugin/history_audit.py", "_build_report",
         ("reason", "result"), 0),
    })


def test_open_entry_points_have_only_the_known_callers():
    entries = {"open_from_snapshot", "handle_intent"}
    found = [(rel, scope, _attr(node)) for rel, scope, node, _ in _calls()
             if _attr(node) in entries]
    assert _bag(found) == _bag({
        ("src/agentic_fx/loops/trade_loop.py", "TradeLoop._run_once_impl",
         "open_from_snapshot"),
        ("src/agentic_fx/loops/trade_loop.py", "TradeLoop._run_signal_mission",
         "open_from_snapshot"),
        ("src/agentic_fx/backtest/runner.py", "run_replay", "handle_intent"),
    })


def test_executor_open_evaluation_has_only_the_two_known_entry_points():
    found = [(rel, scope) for rel, scope, node, _ in _calls()
             if _attr(node) == "_evaluate_and_execute_open"]
    assert _bag(found) == _bag({
        ("src/agentic_fx/core/executor.py", "Executor._open"),
        ("src/agentic_fx/core/executor.py", "Executor.open_from_snapshot"),
    })


def test_executor_open_has_only_handle_intent_as_caller():
    found = [(rel, scope) for rel, scope, node, _ in _calls()
             if _attr(node) == "_open"]
    assert _bag(found) == _bag({("src/agentic_fx/core/executor.py",
                                 "Executor.handle_intent")})


def test_live_open_callers_are_under_the_core_lock():
    live = [(scope, withs) for rel, scope, node, withs in _calls()
            if rel == "src/agentic_fx/loops/trade_loop.py"
            and _attr(node) == "open_from_snapshot"]
    assert len(live) == 2
    for scope, withs in live:
        assert any("_core_lock" in w for w in withs), scope


def _imported_modules(tree):
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            mods.add(base)
            mods.update(f"{base}.{a.name}" for a in node.names)
    return mods


_CONCURRENCY_ROOTS = ("threading", "concurrent", "multiprocessing")


def _concurrency_imports(source):
    return sorted(m for m in _imported_modules(ast.parse(source))
                  if m.split(".")[0] in _CONCURRENCY_ROOTS)


def test_replay_open_runs_in_a_single_threaded_module():
    source = (REPO / "src/agentic_fx/backtest/runner.py").read_text(
        encoding="utf-8")
    assert _concurrency_imports(source) == []
    assert "kill_switch_hwm_rebases" not in source
    assert "rebase_accounting" not in source


def test_concurrency_import_scan_sees_imports_but_not_words():
    assert _concurrency_imports('"""threading concurrent"""\n# Thread\n') == []
    for src in ("import threading", "import concurrent.futures",
                "from concurrent.futures import ThreadPoolExecutor",
                "from concurrent import futures", "import multiprocessing as mp",
                "from threading import Thread"):
        assert _concurrency_imports(src), src


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


_WRITER_NAMES = {"update", "confirm_latched", "reset_kill_switch"}
# bound method を呼出さずに持ち回る箇所の許可一覧 (scope, 名前)。現状は無い。
_ALLOWED_WRITER_ALIASES: set = set()


class _AliasScan(ast.NodeVisitor):
    """書込みメソッドの属性参照のうち、直接の呼出しの func でないものを集める。"""

    def __init__(self, rel):
        self.rel = rel
        self.names = []
        self.found = []

    def _scope(self, node):
        self.names.append(node.name)
        self.generic_visit(node)
        self.names.pop()

    visit_FunctionDef = _scope
    visit_AsyncFunctionDef = _scope
    visit_ClassDef = _scope

    def visit_Call(self, node):
        if isinstance(node.func, ast.Attribute):
            # func 自体は呼出し位置なので別名ではない。receiver 側と引数は見る。
            self.visit(node.func.value)
        else:
            self.visit(node.func)
        for arg in node.args:
            self.visit(arg)
        for kw in node.keywords:
            self.visit(kw.value)

    def visit_Attribute(self, node):
        if node.attr in _WRITER_NAMES:
            self.found.append((self.rel, ".".join(self.names), node.attr))
        self.generic_visit(node)


def _writer_aliases(source, rel="x.py"):
    scan = _AliasScan(rel)
    scan.visit(ast.parse(source))
    return scan.found


def test_writer_methods_are_never_held_as_bound_method_aliases():
    found = []
    for path in _files():
        if not path.relative_to(REPO).as_posix().startswith("src/agentic_fx"):
            continue
        found.extend(_writer_aliases(path.read_text(encoding="utf-8"),
                                     path.relative_to(REPO).as_posix()))
    assert _bag(found) == _bag(_ALLOWED_WRITER_ALIASES)


def test_alias_scan_sees_assignment_argument_and_return_but_not_calls():
    for src in ("setter = state.update",
                "setter = a.b().update",
                "f(x.confirm_latched)",
                "def g():\n    return s.reset_kill_switch",
                "d = {'k': s.update}"):
        assert _writer_aliases(src), src
    assert _writer_aliases("state.update(a=1)") == []
    assert _writer_aliases("a.update.b(1)") != []
