import json

from agentic_fx.runners import turn_budget as tbm
from agentic_fx.tools.registry import ToolDef, ToolRegistry


def _registry(**hooks):
    calls = []
    reg = ToolRegistry(**hooks)

    def search(query):
        calls.append(("search_news", query))
        return [{"title": query}]

    def boom():
        calls.append(("boom",))
        raise RuntimeError("x")

    def errdict():
        calls.append(("errdict",))
        return {"error": "nope"}

    reg.register(ToolDef("search_news", "s", {
        "type": "object", "properties": {"query": {"type": "string"}},
        "required": ["query"]}, search))
    reg.register(ToolDef("boom", "b", {"type": "object"}, boom))
    reg.register(ToolDef("errdict", "e", {"type": "object"}, errdict))
    return reg, calls


def _guard(policy="signal", max_turns=6, turn=1):
    guard = tbm.TurnGuard(policy, max_turns)
    guard.begin_request(turn)
    return guard


ALLOWED = ["search_news", "boom", "errdict"]


def test_without_a_guard_execute_is_unchanged():
    reg, calls = _registry()
    first = reg.execute("search_news", {"query": "a"}, ALLOWED)
    second = reg.execute("search_news", {"query": "a"}, ALLOWED)
    assert first == second and len(calls) == 2


def test_guard_suppresses_the_second_identical_call_without_running_it():
    reg, calls = _registry()
    guard = _guard()
    reg.execute("search_news", {"query": "a"}, ALLOWED, guard=guard)
    out = reg.execute("search_news", {"query": "a"}, ALLOWED, guard=guard)
    assert json.loads(out)["error"] == "duplicate_call"
    assert len(calls) == 1
    assert guard.search_succeeded == 1 and guard.search_duplicate == 1


def test_validation_runs_before_the_guard_and_is_counted_as_invalid():
    reg, calls = _registry()
    guard = _guard()
    out = reg.execute("search_news", {"query": 5}, ALLOWED, guard=guard)
    assert "invalid arguments" in json.loads(out)["error"]
    assert calls == []
    assert guard.search_invalid == 1 and guard.search_executed == 0


def test_not_allowed_name_is_counted_as_invalid_for_search():
    reg, _ = _registry()
    guard = _guard()
    out = reg.execute("search_news", {"query": "a"}, ["boom"], guard=guard)
    assert "not allowed" in json.loads(out)["error"]
    assert guard.search_invalid == 1


def test_final_stage_stops_a_valid_call_but_not_the_validation_error():
    reg, calls = _registry()
    guard = _guard(turn=5)
    stop = reg.execute("search_news", {"query": "a"}, ALLOWED, guard=guard)
    err = reg.execute("search_news", {"query": 1}, ALLOWED, guard=guard)
    assert json.loads(stop)["error"] == "final_answer_only"
    assert "invalid arguments" in json.loads(err)["error"]
    assert calls == []
    assert guard.final_stage_suppressed == 1


def _positions_registry(func):
    reg = ToolRegistry()
    calls = []

    def wrapped():
        calls.append(1)
        return func()

    reg.register(ToolDef("get_positions", "p", {"type": "object"}, wrapped))
    return reg, calls


def _raise():
    raise RuntimeError("x")


def test_exception_and_error_dict_results_count_as_failures():
    for func in (_raise, lambda: {"error": "nope"}):
        reg, calls = _positions_registry(func)
        guard = _guard()
        for _ in range(2):
            reg.execute("get_positions", {}, ["get_positions"], guard=guard)
        third = reg.execute("get_positions", {}, ["get_positions"], guard=guard)
        assert json.loads(third)["error"] == "duplicate_call"
        assert len(calls) == 2


def test_tools_outside_the_allowlist_are_never_suppressed():
    reg, calls = _registry()
    guard = _guard()
    for _ in range(3):
        reg.execute("errdict", {}, ALLOWED, guard=guard)
        reg.execute("boom", {}, ALLOWED, guard=guard)
    assert len(calls) == 6
    assert guard.duplicate_suppressed == 0


def test_suppressed_calls_do_not_fire_on_execute_or_on_result():
    seen = {"exec": 0, "result": []}
    reg, _ = _registry(on_execute=lambda: seen.__setitem__("exec", seen["exec"] + 1),
                       on_result=lambda n, ok, e: seen["result"].append((n, ok)))
    guard = _guard()
    reg.execute("search_news", {"query": "a"}, ALLOWED, guard=guard)
    reg.execute("search_news", {"query": "a"}, ALLOWED, guard=guard)
    assert seen["exec"] == 1
    assert seen["result"] == [("search_news", True)]


def test_validation_errors_still_fire_on_execute_with_a_guard():
    seen = {"exec": 0}
    reg, _ = _registry(on_execute=lambda: seen.__setitem__("exec", seen["exec"] + 1))
    reg.execute("search_news", {"query": 1}, ALLOWED, guard=_guard())
    assert seen["exec"] == 1


def test_on_result_still_reports_invalid_args_with_a_guard():
    seen = []
    reg, _ = _registry(on_result=lambda n, ok, e: seen.append((n, ok, e)))
    reg.execute("search_news", {"query": 1}, ALLOWED, guard=_guard())
    assert seen == [("search_news", False, "invalid_args")]
