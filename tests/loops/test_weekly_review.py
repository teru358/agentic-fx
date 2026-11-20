"""週次の確定の評価: 判定・窓終端時点の対象の復元・棚卸し・劣化・確定。"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from agentic_fx.loops import weekly_review as wk
from agentic_fx.plugin import read_model_inputs
from agentic_fx.plugin.read_model import (
    SOURCE_UNAVAILABLE, ApprovalRow, DeployOpRow, JournalRow, LiveScan, ReadInputs)
from agentic_fx.store import signal_forward as sf
from agentic_fx.store import weekly_review as store
from tests.backtest.factories import SETTINGS
from tests.plugin.read_model_world import World
from tests.store.forward_world import Maker, open_db

UTC = timezone.utc
LABEL = date(2027, 1, 29)
END = datetime(2027, 1, 29, 22, tzinfo=UTC)
NOW = datetime(2027, 1, 31, 1, tzinfo=UTC)
WEEK = timedelta(weeks=1)
H = "h" * 16
ART = "c" * 64


# ---------------------------------------------------------------- 判定 (純関数)

def summary(*, n=40, avg=0.2, first_days=60, recent=5, overdue=0, gen_weeks=20, state="ok"):
    if state != "ok":
        return {"state": state}
    return {"state": "ok",
            "first_decided_at": (END - timedelta(days=first_days)).isoformat(),
            "recent_decisions": recent,
            "generation": {"started_at": (END - gen_weeks * WEEK).isoformat()},
            "series": {"all": {"n": n, "avg_r": avg}},
            "holes": {"overdue_unterminated": overdue}}


@pytest.mark.parametrize("kw,sign", [
    (dict(n=29), "insufficient"), (dict(n=30), "above_floor"),
    (dict(first_days=27), "insufficient"), (dict(first_days=28), "above_floor"),
    (dict(avg=-0.0501), "stop_candidate"), (dict(avg=-0.05), "near_zero"),
    (dict(avg=0.0), "near_zero"), (dict(avg=0.05), "near_zero"),
    (dict(avg=0.0501), "above_floor"), (dict(state="undetermined"), "indeterminate"),
    (dict(state="failed"), "indeterminate"),
])
def test_the_sign_follows_the_count_the_days_and_the_plus_minus_005_line(kw, sign):
    assert wk.judge(summary(**kw), end=END).sign == sign


def test_an_insufficient_sample_is_not_judged_by_its_average():
    assert wk.judge(summary(n=10, avg=-3.0), end=END).sign == "insufficient"


def test_a_missing_first_decision_is_insufficient():
    s = summary()
    s["first_decided_at"] = None
    assert wk.judge(s, end=END).sign == "insufficient"


def test_no_decision_in_the_last_two_weeks_is_a_reason():
    assert wk.judge(summary(recent=0), end=END).reasons == ["no_recent_decisions"]


def test_an_overdue_unterminated_row_is_a_reason():
    assert wk.judge(summary(overdue=2), end=END).reasons == ["overdue_unterminated"]


def test_a_generation_of_twelve_weeks_with_fewer_than_thirty_is_a_reason():
    j = wk.judge(summary(n=12, gen_weeks=12), end=END)
    assert j.sign == "insufficient" and j.reasons == ["generation_aged_insufficient"]
    assert wk.judge(summary(n=12, gen_weeks=11), end=END).reasons == []
    assert wk.judge(summary(n=30, gen_weeks=30), end=END).reasons == []


def test_a_stop_candidate_keeps_its_sign_when_it_also_needs_review():
    j = wk.judge(summary(avg=-0.5, recent=0), end=END)
    assert j.sign == "stop_candidate" and j.reasons == ["no_recent_decisions"]
    assert j.needs_review and j.display == "stop_candidate"


@pytest.mark.parametrize("kw,display,review", [
    (dict(avg=0.0), "needs_review", True),
    (dict(avg=0.3), "above_floor", False),
    (dict(avg=0.3, recent=0), "needs_review", True),
    (dict(n=5, gen_weeks=5), "insufficient", False),
    (dict(n=5, gen_weeks=13), "insufficient", True),
    (dict(avg=-0.3), "stop_candidate", False),
])
def test_the_display_and_the_review_count_follow_the_sign_and_the_reasons(kw, display, review):
    j = wk.judge(summary(**kw), end=END)
    assert (j.display, j.needs_review) == (display, review)


# ---------------------------------------------------------------- 窓終端時点の対象

def approval(i, name, *, kind="strategy", content=H, artifact=ART):
    payload = json.dumps({"name": name, "kind": kind, "content_hash": content,
                          "artifact_hash": artifact})
    return ApprovalRow(i, "approved", END.isoformat(), END.isoformat(), payload)


def journal(op_id, name, approval_id, at, *, artifact=ART, kind="approve", switch=1,
            phase="decided"):
    return JournalRow(op_id, kind, approval_id, name, phase, f".versions/{name}/{artifact}",
                      switch, at if isinstance(at, str) else at.isoformat())


def op(op_id, name, kind, at, *, target=None, content=None, phase="done"):
    return DeployOpRow(op_id, kind, name, phase, None, None, content, target,
                       at.isoformat())


def inputs(**kw):
    kw.setdefault("live", LiveScan())
    return ReadInputs(**kw)


def test_a_deployed_strategy_is_a_target_with_its_generation_start():
    got = wk.targets_as_of(inputs(approvals=(approval(1, "s"),),
                                  journals=(journal(5, "s", 1, END - 10 * WEEK),)), END)
    [t] = got.targets
    assert (t.name, t.content_hash, t.generation) == ("s", H, (0, 5))
    assert t.started_at == END - 10 * WEEK and t.reason is None


def test_an_indicator_is_not_a_forward_target():
    got = wk.targets_as_of(inputs(approvals=(approval(1, "i", kind="indicator"),),
                                  journals=(journal(5, "i", 1, END - WEEK),)), END)
    assert got.targets == [] and got.stopped == []


def test_a_version_change_after_the_window_end_is_not_seen():
    old, new = "a" * 64, "b" * 64
    got = wk.targets_as_of(inputs(
        approvals=(approval(1, "s", content="1" * 16, artifact=old),
                   approval(2, "s", content="2" * 16, artifact=new)),
        journals=(journal(5, "s", 1, END - 5 * WEEK, artifact=old),
                  journal(6, "s", 2, END + timedelta(hours=1), artifact=new))), END)
    [t] = got.targets
    assert t.content_hash == "1" * 16 and t.generation == (0, 5)


def test_a_version_change_before_the_window_end_restarts_the_generation():
    old, new = "a" * 64, "b" * 64
    got = wk.targets_as_of(inputs(
        approvals=(approval(1, "s", content="1" * 16, artifact=old),
                   approval(2, "s", content="2" * 16, artifact=new)),
        journals=(journal(5, "s", 1, END - 5 * WEEK, artifact=old),
                  journal(6, "s", 2, END - WEEK, artifact=new))), END)
    [t] = got.targets
    assert t.content_hash == "2" * 16 and t.started_at == END - WEEK and t.generation == (0, 6)


def test_a_rollback_to_the_first_version_is_a_new_generation_of_it():
    old, new = "a" * 64, "b" * 64
    got = wk.targets_as_of(inputs(
        approvals=(approval(1, "s", content="1" * 16, artifact=old),
                   approval(2, "s", content="2" * 16, artifact=new)),
        journals=(journal(5, "s", 1, END - 5 * WEEK, artifact=old),
                  journal(6, "s", 2, END - 4 * WEEK, artifact=new)),
        deploy_ops=(op(9, "s", "rollback_symlink", END - 2 * WEEK, target=old,
                                       content="1" * 16),)), END)
    [t] = got.targets
    assert t.content_hash == "1" * 16 and t.generation == (1, 9)
    assert t.started_at == END - 2 * WEEK


def test_a_bless_that_changes_nothing_does_not_start_a_generation():
    got = wk.targets_as_of(inputs(
        approvals=(approval(1, "s"), approval(2, "s")),
        journals=(journal(5, "s", 1, END - 5 * WEEK),
                  journal(6, "s", 2, END - WEEK, kind="bless", switch=0))), END)
    assert got.targets[0].generation == (0, 5)


@pytest.mark.parametrize("phase", ["preparing", "versioned", "recorded", "switched", "reverted"])
def test_a_switch_that_did_not_finish_is_not_a_transition(phase):
    got = wk.targets_as_of(inputs(
        approvals=(approval(1, "s"),),
        journals=(journal(5, "s", 1, END - WEEK, phase=phase),)), END)
    assert got.targets == []


@pytest.mark.parametrize("kind", ["disable_symlink", "retire_plain"])
def test_a_strategy_stopped_before_the_window_end_is_listed_as_stopped(kind):
    got = wk.targets_as_of(inputs(
        approvals=(approval(1, "s"),),
        journals=(journal(5, "s", 1, END - 5 * WEEK),),
        deploy_ops=(op(9, "s", kind, END - WEEK),)), END)
    assert got.targets == [] and got.stopped == [{"name": "s", "kind": "strategy"}]


def test_a_stop_after_the_window_end_is_not_seen():
    got = wk.targets_as_of(inputs(
        approvals=(approval(1, "s"),),
        journals=(journal(5, "s", 1, END - 5 * WEEK),),
        deploy_ops=(op(9, "s", "disable_symlink", END + timedelta(minutes=1)),)), END)
    assert len(got.targets) == 1 and got.stopped == []


def test_a_failed_or_open_stop_is_not_a_stop():
    got = wk.targets_as_of(inputs(
        approvals=(approval(1, "s"),),
        journals=(journal(5, "s", 1, END - 5 * WEEK),),
        deploy_ops=(op(9, "s", "disable_symlink", END - WEEK, phase="needs_retry"),)), END)
    assert len(got.targets) == 1 and got.stopped == []


def test_a_restart_after_a_stop_makes_it_a_target_again():
    got = wk.targets_as_of(inputs(
        approvals=(approval(1, "s"), approval(2, "s")),
        journals=(journal(5, "s", 1, END - 6 * WEEK), journal(7, "s", 2, END - WEEK)),
        deploy_ops=(op(9, "s", "disable_symlink", END - 3 * WEEK),)), END)
    assert got.targets[0].generation == (0, 7) and got.stopped == []


def test_the_same_second_is_ordered_by_source_and_operation():
    got = wk.targets_as_of(inputs(
        approvals=(approval(1, "s"),),
        journals=(journal(5, "s", 1, END - 3 * WEEK),),
        deploy_ops=(op(9, "s", "disable_symlink", END - 3 * WEEK),)), END)
    assert got.targets == [] and len(got.stopped) == 1


def test_a_transition_time_that_cannot_be_read_leaves_the_strategy_undetermined():
    got = wk.targets_as_of(inputs(approvals=(approval(1, "s"),),
                                  journals=(journal(5, "s", 1, "not a time"),)), END)
    [t] = got.targets
    assert t.reason == "time_unreadable" and t.started_at is None


def test_a_deployment_whose_approval_cannot_be_read_is_undetermined_not_dropped():
    got = wk.targets_as_of(inputs(approvals=(), journals=(journal(5, "s", 1, END - WEEK),)),
                           END)
    [t] = got.targets
    assert t.name == "s" and t.reason is not None


def test_unreadable_journals_make_the_restoration_unavailable():
    with pytest.raises(wk.TargetsUnavailable):
        wk.targets_as_of(inputs(journal_status=SOURCE_UNAVAILABLE), END)
    with pytest.raises(wk.TargetsUnavailable):
        wk.targets_as_of(inputs(pending_status=SOURCE_UNAVAILABLE), END)


def test_targets_come_back_in_name_order():
    got = wk.targets_as_of(inputs(
        approvals=(approval(1, "b"), approval(2, "a")),
        journals=(journal(5, "b", 1, END - WEEK), journal(6, "a", 2, END - WEEK))), END)
    assert [t.name for t in got.targets] == ["a", "b"]


def test_the_approval_time_metrics_of_the_generation_are_kept_per_pair():
    payload = json.dumps({"name": "s", "kind": "strategy", "content_hash": H,
                          "artifact_hash": ART,
                          "metrics": {"USDJPY": {"trades": 40, "pf": 1.3, "avg_r": 0.2,
                                                 "extra": "x"}}})
    got = wk.targets_as_of(inputs(
        approvals=(ApprovalRow(1, "approved", END.isoformat(), END.isoformat(), payload),),
        journals=(journal(5, "s", 1, END - WEEK),)), END)
    assert got.approval_metrics[("s", H)] == {
        "pairs": {"USDJPY": {"trades": 40, "pf": 1.3, "avg_r": 0.2}}}
