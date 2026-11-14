"""shell の kill switch reset / reconcile は直接 mutation のまま、前後に公開 wrapper を 1 回ずつ呼ぶ。"""
from __future__ import annotations

import json
import logging
import os

import pytest

from agentic_fx.activity import Category
from agentic_fx.ops.contracts import ErrorCode, OpsError
from agentic_fx.store.state import AppState

from tests.test_commands import _commands as _build


def _commands(path):
    path.mkdir(parents=True, exist_ok=True)
    return _build(path)


class _Wrapper:
    def __init__(self, error: BaseException | None = None) -> None:
        self.calls: list[AppState | None] = []
        self.error = error

    def emit_pending_guard_notices(self, after=None, **kwargs):
        self.calls.append(after)
        if self.error is not None:
            raise self.error
        return 0


def _plain_run(tmp_path, command):
    conn, state, activity, cmds = _commands(tmp_path)
    state.update(kill_switch_latched=True)
    gen = state.load().kill_switch_generation
    out = cmds.dispatch(command.format(gen=gen))
    return out, [r for r in activity.tail(20, Category.SYSTEM)], state


def _fail_replace(monkeypatch):
    def boom(a, b):
        raise OSError(28, "injected")
    monkeypatch.setattr(os, "replace", boom)


def test_success_calls_the_wrapper_once_with_the_returned_state(tmp_path):
    base_out, base_acts, _ = _plain_run(tmp_path / "a", "killswitch reset {gen}")
    conn, state, activity, cmds = _commands(tmp_path / "b")
    wrapper = _Wrapper()
    cmds.ops_service = wrapper
    state.update(kill_switch_latched=True)
    gen = state.load().kill_switch_generation
    out = cmds.dispatch(f"killswitch reset {gen}")
    assert out == base_out == "kill switch ラッチを解除しました"
    assert [a[a.index("kill_switch_reset"):][:17] for a in activity.tail(20, Category.SYSTEM)
            if "kill_switch_reset" in a] == ["kill_switch_reset"]
    assert len(wrapper.calls) == 2
    assert wrapper.calls[0] is None
    assert wrapper.calls[1].kill_switch_latched is False
    assert wrapper.calls[1].kill_switch_generation == gen


def test_generation_mismatch_text_activity_are_unchanged_and_no_wrapper_call(tmp_path):
    base_out, base_acts, _ = _plain_run(tmp_path / "a", "killswitch reset 99")
    conn, state, activity, cmds = _commands(tmp_path / "b")
    wrapper = _Wrapper()
    cmds.ops_service = wrapper
    state.update(kill_switch_latched=True)
    out = cmds.dispatch("killswitch reset 99")
    assert out == base_out and "更新された" in out
    assert any("generation mismatch" in r for r in activity.tail(20, Category.SYSTEM))
    assert wrapper.calls == [None]


def test_reset_not_applied_with_marker_calls_the_wrapper_once_with_projected_state(
        tmp_path, monkeypatch):
    conn0, state0, activity0, cmds0 = _commands(tmp_path / "a")
    state0.update(kill_switch_latched=True)
    with monkeypatch.context() as patch:
        _fail_replace(patch)
        base_out = cmds0.dispatch(f"killswitch reset {state0.load().kill_switch_generation}")
    conn, state, activity, cmds = _commands(tmp_path / "b")
    wrapper = _Wrapper()
    cmds.ops_service = wrapper
    state.update(kill_switch_latched=True)
    gen = state.load().kill_switch_generation
    with monkeypatch.context() as patch:
        _fail_replace(patch)
        out = cmds.dispatch(f"killswitch reset {gen}")
    assert out == base_out and "killswitch reconcile" in out
    assert any("killswitch_release_not_applied" in r for r in activity.tail(20, Category.SYSTEM))
    assert len(wrapper.calls) == 2 and wrapper.calls[0] is None
    assert wrapper.calls[1].kill_switch_latched is True  # marker を投影した安全側の state


def test_reconcile_confirm_calls_the_wrapper_once(tmp_path):
    conn, state, activity, cmds = _commands(tmp_path)
    wrapper = _Wrapper()
    cmds.ops_service = wrapper
    state.update(kill_switch_latched=True)
    marker = tmp_path / "s.json.reset-in-progress"
    marker.write_text(json.dumps({"kind": "kill_switch_reset", "requested_generation": 1,
                                  "started_at": "T"}))
    out = cmds.dispatch("killswitch reconcile confirm")
    assert "ラッチ中として確定しました" in out
    assert len(wrapper.calls) == 2 and wrapper.calls[0] is None
    assert wrapper.calls[1].kill_switch_latched is True


def test_reconcile_view_without_confirm_does_not_call_the_wrapper(tmp_path):
    conn, state, activity, cmds = _commands(tmp_path)
    wrapper = _Wrapper()
    cmds.ops_service = wrapper
    state.update(kill_switch_latched=True)
    (tmp_path / "s.json.reset-in-progress").write_text(
        json.dumps({"kind": "kill_switch_reset", "requested_generation": 1, "started_at": "T"}))
    cmds.dispatch("killswitch reconcile")
    assert wrapper.calls == []


def test_reset_without_generation_does_not_call_the_wrapper(tmp_path):
    conn, state, activity, cmds = _commands(tmp_path)
    wrapper = _Wrapper()
    cmds.ops_service = wrapper
    state.update(kill_switch_latched=True)
    cmds.dispatch("killswitch reset")
    assert wrapper.calls == []


@pytest.mark.parametrize("error", [OpsError(ErrorCode.REQUEST_TIMEOUT), RuntimeError("secret-x"),
                                   OSError("secret-y")])
def test_wrapper_failure_is_swallowed_with_a_fixed_line(tmp_path, caplog, error):
    conn, state, activity, cmds = _commands(tmp_path)
    cmds.ops_service = _Wrapper(error)
    state.update(kill_switch_latched=True)
    gen = state.load().kill_switch_generation
    with caplog.at_level(logging.WARNING):
        out = cmds.dispatch(f"killswitch reset {gen}")
    assert out == "kill switch ラッチを解除しました"
    assert [r.getMessage() for r in caplog.records] == ["guard_notice_deferred"] * 2
    assert all(r.exc_info is None for r in caplog.records)


def test_no_ops_service_means_no_notice_call_and_normal_behaviour(tmp_path):
    conn, state, activity, cmds = _commands(tmp_path)
    assert cmds.ops_service is None
    state.update(kill_switch_latched=True)
    out = cmds.dispatch(f"killswitch reset {state.load().kill_switch_generation}")
    assert out == "kill switch ラッチを解除しました"
