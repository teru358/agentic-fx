"""lock 順の source scan。範囲は src/agentic_fx 全体の core lock 取得箇所と、そこから (または
state の排他保持から) 推移的に届く辺。

core lock は ops lane の内側でだけ取る。core 保持から ops lane / OpsService の lane 取得へ届く辺と、
state の排他保持から core 取得へ届く辺を禁じる。取得箇所の一覧は固定し、増減したら赤にする。
解決は静的な近似で、実行時の分岐までは証明しない (scan の方式は tests/ops/_lock_scan.py)。"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.ops import _lock_scan as scan

SRC = Path(__file__).resolve().parents[2] / "src" / "agentic_fx"
STATE_LOCKING_CALLS = frozenset({
    "load", "autopilot_at_start", "file_value", "_exclusive", "_shared",
    "reset_kill_switch", "confirm_latched", "shared_guard", "_current_state",
    "_guard_step", "_sweep_kill_switch", "emit_pending_guard_notices",
    "emit_pending_notices", "_ops_lane"})

# core lock を取る関数 (with / acquire / 補助関数への受け渡し)。増減は意図した変更だけ。
CORE_ACQUISITION_SITES = [
    "core/econ_slice_worker.py:EconSliceWorker._write",
    "loops/reflection_cycle.py:ReflectionCycle._consume_attempt",
    "loops/reflection_cycle.py:ReflectionCycle._reflect_one",
    "loops/reflection_cycle.py:ReflectionCycle.run_pending",
    "loops/trade_loop.py:TradeLoop._ask_once_impl",
    "loops/trade_loop.py:TradeLoop._notify_gate_reject_streak",
    "loops/trade_loop.py:TradeLoop._run_once_impl",
    "loops/trade_loop.py:TradeLoop._run_signal_mission",
    "ops/service.py:OpsService._rebase_under_core",
    "service.py:_scheduler_tick_body",
]


@pytest.fixture(scope="module")
def index():
    return scan.Index.from_tree(SRC)


def _synthetic(*sources: str) -> scan.Index:
    built = scan.Index()
    for number, source in enumerate(sources):
        built.add_source(f"synthetic{number}.py", source)
    return built


# ------------------------------------------------------------ 実コードの scan

def test_core_acquisition_sites_are_pinned(index):
    assert scan.acquisition_sites(index) == CORE_ACQUISITION_SITES


def test_no_core_lock_holder_reaches_the_ops_lane(index):
    assert scan.core_to_ops(index) == []


def test_no_state_exclusive_holder_reaches_a_core_acquisition(index):
    assert scan.state_exclusive_to_core(index) == []


def test_the_real_tree_walk_actually_follows_calls(index):
    # 推移の解決が働いていること: scheduler tick の core 保持区間から Scheduler.tick へ届く。
    holder = next(f for f in index.funcs if f.qualname == "_scheduler_tick_body")
    (_form, stmts), = scan.hold_regions(holder.node, "core_lock")
    reached = {f.qualname for f in scan._reached(index, holder, stmts)}
    assert "Scheduler.tick" in reached


# ------------------------------------------------------------ 合成の悪い例

CORE_TO_OPS_BAD = {
    "with + direct": "def f(self):\n    with self._core_lock:\n        self.ops.status(1)\n",
    "acquire/release": (
        "def f(self):\n    self._core_lock.acquire()\n    try:\n"
        "        self.ops.status(1)\n    finally:\n        self._core_lock.release()\n"),
    "alias": "def f(self):\n    lock = self._core_lock\n    with lock:\n"
             "        self._ops_lane(1)\n",
    "alias chain": "def f(self):\n    a = self._core_lock\n    b = a\n    with b:\n"
                   "        self._ops_lane(1)\n",
    "async with": "async def f(self):\n    async with self._core_lock:\n"
                  "        self.ops.status(1)\n",
    "if not acquire": "def f(self):\n    if not self._core_lock.acquire(blocking=False):\n"
                      "        return\n    self.ops.status(1)\n",
    "enter_context": "def f(self, stack):\n    stack.enter_context(self._core_lock)\n"
                     "    self.ops.status(1)\n",
    "helper": ("def helper(self):\n    with self._ops_lock:\n        pass\n"
               "def middle(self):\n    self.helper()\n"
               "def f(self):\n    with self._core_lock:\n        self.middle()\n"),
}


@pytest.mark.parametrize("name", list(CORE_TO_OPS_BAD))
def test_core_to_ops_detects_each_acquisition_form(name):
    assert scan.core_to_ops(_synthetic(CORE_TO_OPS_BAD[name])) != []


def test_core_to_ops_follows_an_imported_function_across_files():
    helper = "def reach(x):\n    with x._ops_lock:\n        pass\n"
    holder = ("from pkg.helper import reach\n"
              "def f(self):\n    with self._core_lock:\n        reach(self)\n")
    index = _synthetic(holder, helper)
    index.imports["synthetic0.py"]["reach"] = "pkg.synthetic1"
    assert scan.core_to_ops(index) != []


def test_core_to_ops_ignores_benign_shapes():
    ordered = ("def f(self):\n    with self._ops_lock:\n        with self._core_lock:\n"
               "            self.helper()\n"
               "def helper(self):\n    return 1\n")
    after = ("def f(self):\n    with self._core_lock:\n        pass\n"
             "    self.ops.status(1)\n")
    released = ("def f(self):\n    self._core_lock.acquire()\n    self._core_lock.release()\n"
                "    self.ops.status(1)\n")
    for source in (ordered, after, released):
        assert scan.core_to_ops(_synthetic(source)) == [], source


def test_state_exclusive_to_core_detects_direct_and_helper_routes():
    direct = "def f(store):\n    with store._exclusive():\n        with core_lock:\n            pass\n"
    via_helper = ("def takes(self):\n    with self._core_lock:\n        pass\n"
                  "def mid(self):\n    self.takes()\n"
                  "def f(store):\n    with store._exclusive():\n        mid(store)\n")
    acquire = ("def f(store):\n    with store._exclusive():\n"
               "        core_lock.acquire()\n")
    for source in (direct, via_helper):
        assert scan.state_exclusive_to_core(_synthetic(source)) != [], source
    assert scan.hold_regions(ast.parse(acquire).body[0], "core_lock") != []


def test_acquisition_sites_list_every_form():
    index = _synthetic(
        "def a(self):\n    with self._core_lock:\n        pass\n"
        "def b(self):\n    self._core_lock.acquire()\n"
        "async def c(self):\n    async with self._core_lock:\n        pass\n"
        "def d(self):\n    lock = self._core_lock\n    with lock:\n        pass\n"
        "def e(self, s):\n    self._acquire(self._core_lock, 1, 2)\n"
        "def not_one(self):\n    self.x = self._core_lock\n")
    assert scan.acquisition_sites(index) == [
        "synthetic0.py:a", "synthetic0.py:b", "synthetic0.py:c", "synthetic0.py:d",
        "synthetic0.py:e"]


# ------------------------------------------------------------ rebase 区間

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
