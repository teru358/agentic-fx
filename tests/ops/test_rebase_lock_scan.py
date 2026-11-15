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
    index.imports["synthetic0.py"]["reach"] = ("pkg.synthetic1", "reach")
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


def test_acquire_success_branch_is_scanned():
    source = "def f(self):\n    if self._core_lock.acquire():\n        self.ops.status()\n"
    assert scan.core_to_ops(_synthetic(source))


def test_aliased_import_resolves_original_name():
    index = scan.Index()
    index.add_source("caller.py", "from pkg.helper import reach as alias\ndef f(self):\n    with self._core_lock:\n        alias(self)\n")
    index.add_source("helper.py", "def reach(x):\n    with x._ops_lock:\n        pass\n")
    assert scan.core_to_ops(index)


def test_bound_worker_run_is_scanned():
    source = "class Worker:\n    def run(self):\n        with self._ops_lock:\n            pass\ndef f(self):\n    worker = Worker()\n    with self._core_lock:\n        worker.run()\n"
    assert scan.core_to_ops(_synthetic(source))


def test_unresolved_calls_are_audited():
    index = _synthetic("def f(self, worker):\n    with self._core_lock:\n        worker.run()\n")
    assert scan.unresolved_calls(index) == ["synthetic0.py:f:3 worker.run() (with)"]


UNRESOLVED_CALLS = [
    'loops/reflection_cycle.py:ReflectionCycle._consume_attempt:98 self.clock.now() (with)',
    'loops/reflection_cycle.py:ReflectionCycle._reflect_one:160 self.clock.now() (with)',
    "loops/reflection_cycle.py:ReflectionCycle._reflect_one:163 self.conn.execute('SELECT payload_json FROM trade_intents WHERE id=?', (row['intent_id'],)) (with)",
    "loops/reflection_cycle.py:ReflectionCycle._reflect_one:163 self.conn.execute('SELECT payload_json FROM trade_intents WHERE id=?', (row['intent_id'],)).fetchone() (with)",
    "loops/reflection_cycle.py:ReflectionCycle._reflect_one:166 json.loads(ir['payload_json']) (with)",
    "loops/reflection_cycle.py:ReflectionCycle._reflect_one:169 json.dumps({'order': {k: row[k] for k in ('id', 'pair', 'direction', 'horizon', 'quantity', 'avg_fill_price', 'close_price', 'realized_pnl', 'close_reason')}, 'entry_reasoning': _entry_reasoning(intent)}, ensure_ascii=False, indent=1) (with)",
    'loops/reflection_cycle.py:ReflectionCycle._reflect_one:176 Mission(prompt=prompt, tools=[], output_schema=_SCHEMA, max_turns=2, timeout_sec=self.settings.llama_swap.timeout_sec) (with)',
    "loops/reflection_cycle.py:ReflectionCycle._reflect_one:181 missions.start(self.conn, 'reflection', self.settings.runner.trade.backend, self.settings.runner.trade.model, now) (with)",
    "loops/reflection_cycle.py:ReflectionCycle._reflect_one:293 reflection_attempts.clear(self.conn, row['id']) (with)",
    "loops/reflection_cycle.py:ReflectionCycle._reflect_one:323 MissionResult('failed', None, []) (with)",
    'loops/reflection_cycle.py:ReflectionCycle.run_pending:119 self.conn.execute("SELECT o.* FROM orders o LEFT JOIN reflections r ON r.order_id=o.id LEFT JOIN reflection_attempts a ON a.order_id=o.id WHERE o.status=\'closed\' AND r.order_id IS NULL AND (a.attempts IS NULL OR a.attempts < :max) ORDER BY o.id LIMIT :limit", {\'max\': self.settings.reflection.max_attempts, \'limit\': max_items}) (with)',
    'loops/reflection_cycle.py:ReflectionCycle.run_pending:119 self.conn.execute("SELECT o.* FROM orders o LEFT JOIN reflections r ON r.order_id=o.id LEFT JOIN reflection_attempts a ON a.order_id=o.id WHERE o.status=\'closed\' AND r.order_id IS NULL AND (a.attempts IS NULL OR a.attempts < :max) ORDER BY o.id LIMIT :limit", {\'max\': self.settings.reflection.max_attempts, \'limit\': max_items}).fetchall() (with)',
    'loops/trade_loop.py:TradeLoop._ask_once_impl:925 self.clock.now() (with)',
    'loops/trade_loop.py:TradeLoop._ask_once_impl:929 Mission(prompt=prompt, tools=_ASK_TOOLS, output_schema=ANSWER_SCHEMA, max_turns=self.settings.llama_swap.max_turns, timeout_sec=self.settings.llama_swap.timeout_sec) (with)',
    "loops/trade_loop.py:TradeLoop._ask_once_impl:934 missions.start(self.conn, 'ask', self.settings.runner.trade.backend, self.settings.runner.trade.model, now) (with)",
    "loops/trade_loop.py:TradeLoop._ask_once_impl:975 MissionResult('failed', None, []) (with)",
    'loops/trade_loop.py:TradeLoop._notify_gate_reject_streak:787 alert_state.get(self.conn, alert_state.GATE_REJECT_STREAK_KEY) (with)',
    'loops/trade_loop.py:TradeLoop._notify_gate_reject_streak:795 alert_state.set(self.conn, alert_state.GATE_REJECT_STREAK_KEY, str(streak_id), now=self.clock.now()) (with)',
    'loops/trade_loop.py:TradeLoop._notify_gate_reject_streak:797 self.clock.now() (with)',
    'loops/trade_loop.py:TradeLoop._notify_gate_reject_streak:797 str(streak_id) (with)',
    "loops/trade_loop.py:TradeLoop._run_once_impl:214 missions.start(self.conn, 'trade', self.settings.runner.trade.backend, self.settings.runner.trade.model, now, trigger=trigger) (with)",
    'loops/trade_loop.py:TradeLoop._run_once_impl:317 self.clock.now() (with)',
    'loops/trade_loop.py:TradeLoop._run_once_impl:320 tuple((event.id for event in view.active)) (with)',
    'loops/trade_loop.py:TradeLoop._run_once_impl:328 isinstance(snapshot_error, (NotTradableSymbol, ExposureNotTradableSymbol)) (with)',
    'loops/trade_loop.py:TradeLoop._run_once_impl:331 isinstance(snapshot_error, ExposureNotTradableSymbol) (with)',
    "loops/trade_loop.py:TradeLoop._run_once_impl:336 self.activity.write(Category.TRADE, 'gate_rejected', reason, ref_id=str(iid)) (with)",
    'loops/trade_loop.py:TradeLoop._run_once_impl:337 str(iid) (with)',
    "loops/trade_loop.py:TradeLoop._run_once_impl:361 self.activity.write(Category.TRADE, 'gate_rejected', reasons[0], ref_id=str(iid)) (with)",
    'loops/trade_loop.py:TradeLoop._run_once_impl:362 str(iid) (with)',
    "loops/trade_loop.py:TradeLoop._run_once_impl:372 self.activity.write(Category.TRADE, 'gate_rejected', reasons[0], ref_id=str(iid)) (with)",
    'loops/trade_loop.py:TradeLoop._run_once_impl:373 str(iid) (with)',
    "loops/trade_loop.py:TradeLoop._run_once_impl:390 _log.exception('executor intent handling raised') (with)",
    "loops/trade_loop.py:TradeLoop._run_once_impl:394 self.activity.write(Category.AGGREGATE, 'intent_execution_failed', safe_error_text(e) + self._market_tool_calls_suffix(result), ref_id=str(mid)) (with)",
    'loops/trade_loop.py:TradeLoop._run_once_impl:398 str(mid) (with)',
    "loops/trade_loop.py:TradeLoop._run_once_impl:403 deferred.append(f'[agentic-fx] 注文処理失敗: {safe_error_text(e)}') (with)",
    "loops/trade_loop.py:TradeLoop._run_once_impl:440 MissionResult('failed', None, []) (with)",
    "loops/trade_loop.py:TradeLoop._run_signal_mission:454 missions.start(self.conn, 'trade', self.settings.runner.trade.backend, self.settings.runner.trade.model, now, trigger='signal') (with)",
    'loops/trade_loop.py:TradeLoop._run_signal_mission:473 Mission(prompt=prompt, tools=list(SIGNAL_DECISION_TOOLS), output_schema=DECISION_SCHEMA, max_turns=self.settings.llama_swap.max_turns, timeout_sec=self.settings.llama_swap.timeout_sec) (with)',
    'loops/trade_loop.py:TradeLoop._run_signal_mission:474 list(SIGNAL_DECISION_TOOLS) (with)',
    "loops/trade_loop.py:TradeLoop._run_signal_mission:507 self.activity.write(Category.AGGREGATE, 'signal_decision_invalid', parsed.detail, ref_id=str(claimed['id'])) (with)",
    "loops/trade_loop.py:TradeLoop._run_signal_mission:509 str(claimed['id']) (with)",
    'loops/trade_loop.py:TradeLoop._run_signal_mission:581 self.clock.now() (with)',
    'loops/trade_loop.py:TradeLoop._run_signal_mission:585 datetime.fromisoformat(fresh_until) (with)',
    'loops/trade_loop.py:TradeLoop._run_signal_mission:600 self.executor.state_fn() (with)',
    "loops/trade_loop.py:TradeLoop._run_signal_mission:619 MissionResult('failed', {'detail': 'invalid_reject_code'}, result.transcript) (with)",
    "loops/trade_loop.py:TradeLoop._run_signal_mission:624 self.activity.write(Category.AGGREGATE, 'signal_decision_invalid', 'invalid_reject_code', ref_id=str(signal_id)) (with)",
    'loops/trade_loop.py:TradeLoop._run_signal_mission:626 str(signal_id) (with)',
    'loops/trade_loop.py:TradeLoop._run_signal_mission:630 isinstance(parsed, Accept) (with)',
    "loops/trade_loop.py:TradeLoop._run_signal_mission:640 self.activity.write(Category.AGGREGATE, 'signal_filtered_econ_window', signal_decisions.SYSTEM_REJECT_CODE, ref_id=str(signal_id)) (with)",
    'loops/trade_loop.py:TradeLoop._run_signal_mission:643 str(signal_id) (with)',
    'loops/trade_loop.py:TradeLoop._run_signal_mission:646 isinstance(parsed, Accept) (with)',
    "loops/trade_loop.py:TradeLoop._run_signal_mission:654 self.activity.write(Category.AGGREGATE, 'signal_accepted', 'accept', ref_id=str(signal_id)) (with)",
    'loops/trade_loop.py:TradeLoop._run_signal_mission:656 str(signal_id) (with)',
    "loops/trade_loop.py:TradeLoop._run_signal_mission:668 self.activity.write(Category.AGGREGATE, 'intent_execution_failed', reason, ref_id=str(mid)) (with)",
    'loops/trade_loop.py:TradeLoop._run_signal_mission:670 str(mid) (with)',
    "loops/trade_loop.py:TradeLoop._run_signal_mission:671 deferred_notifications.append(f'[agentic-fx] 注文処理失敗: {reason}') (with)",
    "loops/trade_loop.py:TradeLoop._run_signal_mission:682 self.activity.write(Category.AGGREGATE, 'signal_rejected_by_llm', reject_code, ref_id=str(signal_id)) (with)",
    'loops/trade_loop.py:TradeLoop._run_signal_mission:684 str(signal_id) (with)',
    "loops/trade_loop.py:TradeLoop._run_signal_mission:710 MissionResult('failed', None, []) (with)",
    'ops/service.py:OpsService._rebase_under_core:1863 min(deadline, self._monotonic() + self._REBASE_HOLD_SECONDS) (acquire)',
    'ops/service.py:OpsService._rebase_under_core:1863 self._monotonic() (acquire)',
    'ops/service.py:OpsService._rebase_under_core:1864 stack.enter_context(self._conn.waiting_until(hold)) (acquire)',
    'ops/service.py:OpsService._rebase_under_core:1865 stack.enter_context(self._state_store.shared_guard(deadline=self._foreign(hold), stop_event=self.stop_event)) (acquire)',
    'service.py:_scheduler_tick_body:2514 app.ingest.commit(app.conn_core) (with)',
    "service.py:_scheduler_tick_body:2516 _log.warning('ingest commit failed: %s', safe_error_text(exc)) (with)",
    "service.py:_scheduler_tick_body:2520 app.activity.write(Category.SYSTEM, 'ingest_commit_failed', safe_error_text(exc)) (with)",
    "service.py:_scheduler_tick_body:2523 _log.exception('failed to record ingest commit failure') (with)",
    "service.py:_scheduler_tick_body:2528 _log.warning('outage observe failed: %s', safe_error_text(exc)) (with)",
    "service.py:_scheduler_tick_body:2530 app.activity.write(Category.SYSTEM, 'outage_observe_failed', safe_error_text(exc)) (with)",
    "service.py:_scheduler_tick_body:2533 _log.exception('failed to record outage observe failure') (with)",
    'store/state.py:StateStore.confirm_latched:466 replace(current, kill_switch_latched=True, kill_switch_generation=current.kill_switch_generation + 1, kill_switch_latched_at=self._clock.now().astimezone(timezone.utc).isoformat()) (with)',
    'store/state.py:StateStore.confirm_latched:469 self._clock.now() (with)',
    'store/state.py:StateStore.confirm_latched:469 self._clock.now().astimezone(timezone.utc) (with)',
    'store/state.py:StateStore.confirm_latched:469 self._clock.now().astimezone(timezone.utc).isoformat() (with)',
    "store/state.py:StateStore.reset_kill_switch:429 StateUncertain('a previous reset did not finish; run `killswitch reconcile`') (with)",
    'store/state.py:StateStore.reset_kill_switch:433 NotLatched() (with)',
    'store/state.py:StateStore.reset_kill_switch:435 GenerationMismatch(current.kill_switch_generation, current.kill_switch_latched_at) (with)',
    'store/state.py:StateStore.reset_kill_switch:437 replace(current, kill_switch_latched=False) (with)',
    "store/state.py:StateStore.reset_kill_switch:443 ResetNotApplied('kill switch reset did not finish; marker kept') (with)",
    "store/state.py:StateStore.reset_kill_switch:448 ResetNotApplied('kill switch reset did not finish; marker kept') (with)",
    "store/state.py:StateStore.update:362 changes.get('kill_switch_latched') (with)",
    "store/state.py:StateStore.update:364 changes.get('kill_switch_latched') (with)",
    'store/state.py:StateStore.update:370 self._clock.now() (with)',
    'store/state.py:StateStore.update:370 self._clock.now().astimezone(timezone.utc) (with)',
    'store/state.py:StateStore.update:370 self._clock.now().astimezone(timezone.utc).isoformat() (with)',
    'store/state.py:StateStore.update:372 replace(current, **changes) (with)',
]


def test_unresolved_calls_in_real_hold_regions_are_pinned(index):
    assert scan.unresolved_calls(index) == UNRESOLVED_CALLS
