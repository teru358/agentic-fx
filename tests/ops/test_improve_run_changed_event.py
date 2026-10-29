"""improve_run_changed event の専用検査。"""
from __future__ import annotations

import pytest

from agentic_fx import improve_notice_codes as codes
from agentic_fx.ops.events import FIELDS, EventSchemaError, EventStore, validate_fields

CODE = "improve_run_changed"
REF = "improve:2043"


def _started(trigger="manual"):
    return {"state": "started", "trigger": trigger, "result_code": None,
            "reason_code": None, "tool_calls": None}


def _finished(result="gate_failed", reason="unprofitable", trigger="scheduled",
              tool_calls=10):
    return {"state": "finished", "trigger": trigger, "result_code": result,
            "reason_code": reason, "tool_calls": tool_calls}


def test_field_set_is_fixed():
    assert FIELDS[CODE] == frozenset(
        {"state", "trigger", "result_code", "reason_code", "tool_calls",
         "repeat_run", "repeat_max_runs", "repeat_elapsed_sec", "repeat_duration_sec"})


@pytest.mark.parametrize("trigger", codes.TRIGGERS)
def test_started_is_accepted(trigger):
    validate_fields(CODE, _started(trigger), REF)


@pytest.mark.parametrize("pair", sorted(codes.ALLOWED_PAIRS, key=str))
@pytest.mark.parametrize("tool_calls", [None, 0, 14, codes.TOOL_CALLS_MAX])
def test_every_allowed_pair_is_accepted_when_finished(pair, tool_calls):
    validate_fields(CODE, _finished(*pair, tool_calls=tool_calls), REF)


def _bad(**override):
    fields = _finished()
    fields.update(override)
    return fields


@pytest.mark.parametrize("fields", [
    _bad(reason_code="pf:7.7777"),
    _bad(reason_code=":"),
    _bad(reason_code="insufficient_trades:16"),
    _bad(reason_code="unprofitable7"),
    _bad(reason_code="OSError:boom"),
    _bad(reason_code="not_in_the_table"),
    _bad(reason_code="gate_other"),
    _bad(result_code="report", reason_code="unprofitable"),
    _bad(result_code="submitted", reason_code="unprofitable"),
    _bad(result_code="zzz", reason_code=None),
    _bad(result_code=None, reason_code=None),
    _bad(result_code=7),
    _bad(state="running"),
    _bad(state=None),
    _bad(state=1),
    _bad(trigger="cron"),
    _bad(trigger=None),
    _bad(tool_calls=-1),
    _bad(tool_calls=True),
    _bad(tool_calls=False),
    _bad(tool_calls=1.5),
    _bad(tool_calls="12"),
    _bad(tool_calls=codes.TOOL_CALLS_MAX + 1),
])
def test_invalid_finished_fields_are_rejected(fields):
    with pytest.raises(EventSchemaError):
        validate_fields(CODE, fields, REF)


@pytest.mark.parametrize("override", [
    {"result_code": "failed"}, {"reason_code": "timeout"}, {"tool_calls": 3},
    {"tool_calls": 0}])
def test_started_carries_no_result(override):
    fields = _started()
    fields.update(override)
    with pytest.raises(EventSchemaError):
        validate_fields(CODE, fields, REF)


def test_extra_and_missing_fields_are_rejected():
    with pytest.raises(EventSchemaError):
        validate_fields(CODE, {**_started(), "extra": 1}, REF)
    fields = _started()
    fields.pop("tool_calls")
    with pytest.raises(EventSchemaError):
        validate_fields(CODE, fields, REF)


@pytest.mark.parametrize("ref", [None, "", "improve:", "improve:abc", "improve:-1",
                                 "improve:1234567890123456789", "approval:1",
                                 "improve:1 ", " improve:1", "improve:1\n"])
def test_ref_is_required_and_strict(ref):
    with pytest.raises(EventSchemaError):
        validate_fields(CODE, _started(), ref)


def test_ref_boundaries_are_accepted():
    validate_fields(CODE, _started(), "improve:1")
    validate_fields(CODE, _started(), "improve:" + "9" * 18)


@pytest.mark.parametrize("repeat", [
    {"repeat_run": 1, "repeat_max_runs": 1,
     "repeat_elapsed_sec": 0, "repeat_duration_sec": 60},
    {"repeat_run": 2, "repeat_max_runs": 10,
     "repeat_elapsed_sec": 305, "repeat_duration_sec": 1800},
])
def test_complete_repeat_context_with_daemon_bounds_is_accepted(repeat):
    validate_fields(CODE, {**_started(), **repeat}, REF)


@pytest.mark.parametrize("repeat", [
    {"repeat_run": 1},
    {"repeat_run": 0, "repeat_max_runs": 0,
     "repeat_elapsed_sec": 0, "repeat_duration_sec": 0},
    {"repeat_run": -1, "repeat_max_runs": 1,
     "repeat_elapsed_sec": 0, "repeat_duration_sec": 60},
    {"repeat_run": 1, "repeat_max_runs": -1,
     "repeat_elapsed_sec": 0, "repeat_duration_sec": 60},
    {"repeat_run": 1, "repeat_max_runs": 1,
     "repeat_elapsed_sec": -1, "repeat_duration_sec": 60},
    {"repeat_run": 1, "repeat_max_runs": 1,
     "repeat_elapsed_sec": 0, "repeat_duration_sec": -1},
    {"repeat_run": 3, "repeat_max_runs": 2,
     "repeat_elapsed_sec": 0, "repeat_duration_sec": 60},
    {"repeat_run": 1, "repeat_max_runs": 1,
     "repeat_elapsed_sec": 0, "repeat_duration_sec": 59},
])
def test_repeat_context_outside_the_daemon_contract_is_rejected(repeat):
    with pytest.raises(EventSchemaError):
        validate_fields(CODE, {**_started(), **repeat}, REF)


def test_event_schema_error_surfaces_from_both_emit_paths(ops_env):
    store = EventStore(ops_env.conn, wall_clock=ops_env.wall)
    bad = _bad(reason_code="pf:7.7777")
    with pytest.raises(EventSchemaError):
        store.emit(CODE, REF, bad)
    with pytest.raises(EventSchemaError):
        store.emit_best_effort(CODE, REF, bad)
    assert ops_env.conn.execute("SELECT count(*) c FROM ops_events").fetchone()["c"] == 0


def test_valid_event_is_stored_with_sorted_json(ops_env):
    store = EventStore(ops_env.conn, wall_clock=ops_env.wall)
    event_id = store.emit_best_effort(CODE, REF, _finished())
    row = ops_env.conn.execute("SELECT * FROM ops_events WHERE id=?",
                               (event_id,)).fetchone()
    assert (row["code"], row["ref"]) == (CODE, REF)
    assert row["fields_json"] == (
        '{"reason_code":"unprofitable","result_code":"gate_failed",'
        '"state":"finished","tool_calls":10,"trigger":"scheduled"}')


def test_events_module_does_not_pull_numeric_report_terms():
    import inspect

    import agentic_fx.ops.events as events
    source = inspect.getsource(events)
    assert "holdout" not in source and "avg_r" not in source
