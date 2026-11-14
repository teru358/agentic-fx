"""guard 通知の周辺の整理: 単一の例外境界、emit の引数、status の補助表示の log。"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from agentic_fx import service as agentic_service
from agentic_fx.ops import service as service_mod
from agentic_fx.ops.events import EventStore
from agentic_fx.ops.service import OpsService


def test_lane_wrapper_sums_the_counts_returned_by_guard_step(monkeypatch):
    calls = []

    def fake_step(self, run, get_state):
        calls.append(get_state)
        return 2

    monkeypatch.setattr(OpsService, "_guard_step", fake_step)
    svc = OpsService.__new__(OpsService)
    svc._lane_owned = lambda: True
    svc._state_store = object()
    svc._finish_notices = lambda run: None
    assert svc._guard_notices_in_lane(after=object()) == 4
    assert len(calls) == 2


def test_guard_step_returns_zero_and_marks_failure(monkeypatch):
    svc = OpsService.__new__(OpsService)
    svc._conn = SimpleNamespace(rollback=lambda: None)
    run = service_mod._NoticeRun()

    def boom(state):
        raise RuntimeError("x")

    svc._sweep_kill_switch = boom
    assert svc._guard_step(run, lambda: None) == 0
    assert run.failed is True
    svc._sweep_kill_switch = lambda state: 1
    assert svc._guard_step(service_mod._NoticeRun(), lambda: None) == 1


@pytest.mark.parametrize("not_before", [None, "floor"])
def test_emit_best_effort_always_passes_not_before(monkeypatch, not_before):
    store = EventStore.__new__(EventStore)
    seen = {}

    def fake_emit(code, ref, fields, **kwargs):
        seen.update(kwargs)
        return 1

    store.emit = fake_emit
    store._check_floor = lambda value: None
    store._failures_lock = __import__("threading").Lock()
    import agentic_fx.ops.events as events_mod
    store._failures = events_mod._Failures()
    monkeypatch.setattr(events_mod, "validate_fields", lambda *a, **k: None)
    store.emit_best_effort("c", "r", {}, not_before=not_before)
    assert seen == {"not_before": not_before}


class _State:
    def __init__(self):
        self.fail = True

    def load(self, **kwargs):
        if self.fail:
            raise OSError("busy")
        return SimpleNamespace(kill_switch_generation=0, kill_switch_latched_at=None,
                               kill_switch_latched=False)


def test_status_probe_warns_only_on_transition_to_failure(caplog):
    probe = agentic_service._GuardNoticeProbe()
    state = _State()
    with caplog.at_level(logging.WARNING, logger=agentic_service._log.name):
        for _ in range(5):
            assert probe(None, state) is None
        assert len([r for r in caplog.records
                    if "status guard notice unavailable" in r.getMessage()]) == 1
        state.fail = False
        assert probe(None, state) == (False, None)
        state.fail = True
        probe(None, state)
        probe(None, state)
    assert len([r for r in caplog.records
                if "status guard notice unavailable" in r.getMessage()]) == 2
