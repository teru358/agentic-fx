"""strategy の rollback は、戻り先自身の最新の承認に有効な履歴証跡があるときだけ新しく始められる。
人間だけが、完全長 hash と理由を伴って、この拒否だけを迂回できる (監査記録つき)。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from agentic_fx.activity import ActivityLog
from agentic_fx.plugin import deploy_rollback, history_reasons, loader
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import plugin_deploy_ops as deploy_store
from tests.history_support import ready_evidence
from tests.plugin.conftest import SETTINGS
from tests.plugin.deploy_world import World, at

NOW = at(60)
REFUSED = history_reasons.ROLLBACK_EVIDENCE_REQUIRED


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def _approve(world, name, version, minute, *, evidence="ready", status="approved", **extra):
    payload = {"name": name, "kind": "strategy", "content_hash": loader.content_hash(version),
               "artifact_hash": version.name, **extra}
    if evidence == "ready":
        payload["history_guard"] = ready_evidence()
    elif evidence is not None:
        payload["history_guard"] = evidence
    aid = approvals_store.create(world.conn, "plugin", payload, at(minute))
    if status != "pending":
        approvals_store.apply_decision(world.conn, aid, status, decided_by="human",
                                       now=at(minute))
    return aid


def _strategies(world, *, target_evidence="ready"):
    """a (戻り先、承認に証跡 = target_evidence) → b (配備中)。"""
    a, b = (world.version("sma", tag, kind="strategy") for tag in ("a", "b"))
    aid = _approve(world, "sma", a, 1, evidence=target_evidence)
    world.journal("sma", "approve", None, a, 1, aid)
    bid = _approve(world, "sma", b, 2)
    world.journal("sma", "approve", a, b, 2, bid)
    world.link("sma", b)
    return a, b


def _rollback(world, **kwargs):
    kwargs.setdefault("to", None)
    kwargs.setdefault("revoke", False)
    kwargs.setdefault("force", False)
    return deploy_rollback.rollback_plugin(
        world.conn, world.root, "sma", now=NOW, settings=SETTINGS, **kwargs)


def _ops(world):
    return [dict(r) for r in world.conn.execute("SELECT * FROM plugin_deploy_ops")]


def _live(world):
    live = world.root / "sma"
    return os.readlink(live) if live.is_symlink() else None


def _refused(outcome):
    return (outcome.outcome, outcome.refusal_code, outcome.reasons[0])


# ---- 通常の rollback -----------------------------------------------------------

def test_a_target_with_a_sound_evidence_rolls_back(world):
    a, b = _strategies(world)
    assert _rollback(world, to=a.name).outcome == "rolled_back"
    assert _live(world) == f".versions/sma/{a.name}"


def test_the_default_target_is_checked_too(world):
    a, b = _strategies(world, target_evidence=None)
    outcome = _rollback(world)
    assert _refused(outcome) == ("refused", history_reasons.OUTCOME, REFUSED)
    assert _live(world) == f".versions/sma/{b.name}" and _ops(world) == []


@pytest.mark.parametrize("evidence", [None, "broken"])
def test_a_target_without_a_sound_evidence_is_refused_with_nothing_started(world, evidence):
    a, b = _strategies(world, target_evidence=evidence)
    outcome = _rollback(world, to=a.name)
    assert _refused(outcome) == ("refused", history_reasons.OUTCOME, REFUSED)
    assert _live(world) == f".versions/sma/{b.name}" and _ops(world) == []


@pytest.mark.parametrize("tamper", [
    lambda e: e.update(version=0), lambda e: e.update(verdict="history_gap"),
    lambda e: e.update(calendar_version="old"),
    lambda e: e.update(dataset={"source": "none", "base_interval": "1m"}),
])
def test_an_invalid_evidence_is_refused(world, tamper):
    evidence = ready_evidence()
    tamper(evidence)
    a, b = _strategies(world, target_evidence=evidence)
    assert _rollback(world, to=a.name).refusal_code == history_reasons.OUTCOME


def test_the_dry_run_reports_the_same_refusal(world):
    a, b = _strategies(world, target_evidence=None)
    assert _rollback(world, to=a.name, dry_run=True).refusal_code == history_reasons.OUTCOME


def test_another_versions_evidence_or_a_pending_request_is_not_substituted(world):
    a, b = _strategies(world, target_evidence=None)
    _approve(world, "sma", a, 5, status="pending")           # a の新依頼 (まだ pending)
    assert _rollback(world, to=a.name).refusal_code == history_reasons.OUTCOME


def test_only_the_latest_approved_payload_of_that_version_counts(world):
    a, b = _strategies(world)
    _approve(world, "sma", a, 7, evidence=None)              # 後の承認に証跡が無い
    assert _rollback(world, to=a.name).refusal_code == history_reasons.OUTCOME


def test_an_epoch_mismatch_is_refused(world):
    a, b = _strategies(world)
    world.conn.execute("INSERT INTO data_migrations(name, applied_at) VALUES "
                       "('time_repair_dst_v1', ?)", (at(0).isoformat(),))
    world.conn.commit()
    assert _rollback(world, to=a.name).refusal_code == history_reasons.OUTCOME


def test_an_unreadable_no_bars_list_refuses(world):
    a, b = _strategies(world)
    path = world.root.parent / "config" / "history-no-bars.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("entries: [", encoding="utf-8")
    assert _rollback(world, to=a.name).refusal_code == history_reasons.OUTCOME
    path.unlink()
    assert _rollback(world, to=a.name).outcome == "rolled_back"


def test_an_indicator_is_outside_the_guard(world):
    a, b, c = world.three_generations()                       # indicator
    assert _rollback(world, to=b.name).outcome == "rolled_back"


def test_a_rollback_already_started_is_resumed_without_the_check(world):
    a, b = _strategies(world, target_evidence=None)
    op_id = world.intent("sma", from_ver=b, to_ver=a)
    world.link("sma", a)                                      # 切替は済み、op は未完
    outcome = _rollback(world, to=a.name)
    assert outcome.outcome == "rolled_back" and outcome.op_id == op_id


def test_an_open_rollback_whose_switch_is_still_to_come_is_finished_without_the_check(world):
    a, b = _strategies(world, target_evidence=None)
    op_id = world.intent("sma", from_ver=b, to_ver=a)         # intent は記録済み、symlink は未切替
    outcome = _rollback(world, to=a.name)
    assert outcome.outcome == "rolled_back" and outcome.op_id == op_id
    assert _live(world) == f".versions/sma/{a.name}"


# ---- 人間専用の強行 ------------------------------------------------------------

def _forced(world, a, *, reason="確認済み", target=None, activity=None, **kw):
    override = deploy_rollback.HistoryGuardOverride(target or a.name, reason)
    return _rollback(world, to=a.name, history_override=override,
                     activity=activity if activity is not None else _log(world), **kw)


def _log(world):
    return ActivityLog(world.root.parent / "logs" / "activity.log")


def _log_lines(world):
    path = world.root.parent / "logs" / "activity.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_a_forced_rollback_goes_past_the_history_refusal_and_is_audited(world):
    a, b = _strategies(world, target_evidence=None)
    outcome = _forced(world, a, reason="broker 休場を確認")
    assert outcome.outcome == "rolled_back"
    assert _live(world) == f".versions/sma/{a.name}"
    lines = _log_lines(world)
    first = next(line for line in lines if "\trollback_history_guard_force_started\t" in line)
    assert f"target={a.name}" in first and "actor=human_cli" in first
    assert "reason=broker 休場を確認" in first and "at=" in first
    op = next(line for line in lines if "\trollback_history_guard_forced_op\t" in line)
    assert f"op_id={outcome.op_id}" in op and f"target={a.name}" in op


@pytest.mark.parametrize("short,reason", [
    (True, "確認済み"),              # 短縮 hash
    (False, ""), (False, "   "),     # 理由なし
])
def test_a_forced_rollback_needs_the_full_hash_and_a_reason(world, short, reason):
    a, b = _strategies(world, target_evidence=None)
    target = a.name[:12] if short else a.name
    outcome = _rollback(world, to=a.name, activity=_log(world),
                        history_override=deploy_rollback.HistoryGuardOverride(target, reason))
    assert outcome.outcome == "refused" and outcome.refusal_code == history_reasons.OUTCOME
    assert _live(world) == f".versions/sma/{b.name}" and _ops(world) == []
    assert _log_lines(world) == []


def test_a_forced_rollback_must_name_the_actual_target(world):
    a, b = _strategies(world, target_evidence=None)
    outcome = _forced(world, a, target=b.name)
    assert outcome.outcome == "refused" and _ops(world) == []


def test_the_default_target_cannot_be_forced(world):
    a, b = _strategies(world, target_evidence=None)
    outcome = _rollback(world, history_override=deploy_rollback.HistoryGuardOverride(
        a.name, "x"), activity=_log(world))
    assert outcome.outcome == "refused" and _ops(world) == []


def test_without_an_audit_record_nothing_is_switched(world):
    a, b = _strategies(world, target_evidence=None)
    override = deploy_rollback.HistoryGuardOverride(a.name, "x")
    no_activity = _rollback(world, to=a.name, history_override=override, activity=None)
    assert no_activity.outcome == "refused" and _ops(world) == []
    log = world.root.parent / "logs" / "activity.log"
    activity = ActivityLog(log)
    log.mkdir()                                              # 追記できないログ
    unwritable = _rollback(world, to=a.name, activity=activity, history_override=override)
    assert unwritable.outcome == "refused" and _ops(world) == []
    assert _live(world) == f".versions/sma/{b.name}"


def test_a_forced_rollback_keeps_every_other_check(world):
    a, b = _strategies(world, target_evidence=None)
    # 戻り先の版の中身を壊す: hash 検証は強行でも迂回できない
    (world.root / ".versions" / "sma" / a.name / "plugin.py").write_text("tampered")
    outcome = _forced(world, a)
    assert outcome.outcome == "refused" and outcome.refusal_code == "target_verification_failed"
    assert _ops(world) == [] and _live(world) == f".versions/sma/{b.name}"


def test_a_forced_run_without_a_refusal_to_bypass_is_audited_not_at_all(world):
    a, b = _strategies(world)                                 # 証跡あり: 強行は不要
    assert _forced(world, a).outcome == "rolled_back"
    assert not any("history_guard_force" in line for line in _log_lines(world))


def test_a_forced_rollback_leaves_no_evidence_and_the_next_plain_one_is_refused_again(world):
    a, b = _strategies(world, target_evidence=None)
    assert _forced(world, a).outcome == "rolled_back"
    world.link("sma", b)
    assert _rollback(world, to=a.name).refusal_code == history_reasons.OUTCOME


class _FlakyAudit:
    """write_required が n 回目 (1 起点) に失敗する監査ログ。"""

    def __init__(self, inner, fail_on):
        self.inner, self.fail_on, self.calls = inner, fail_on, 0

    def write_required(self, *args, **kwargs):
        self.calls += 1
        if self.calls == self.fail_on:
            raise OSError("audit unavailable")
        return self.inner.write_required(*args, **kwargs)

    def write(self, *args, **kwargs):
        return self.inner.write(*args, **kwargs)


def test_a_forced_rollback_resumed_after_a_failed_op_audit_still_records_the_op(world):
    a, b = _strategies(world, target_evidence=None)
    flaky = _FlakyAudit(_log(world), fail_on=2)
    first = _forced(world, a, activity=flaky)
    assert first.outcome == "needs_retry" and _live(world) == f".versions/sma/{b.name}"
    assert not any("_forced_op\t" in line for line in _log_lines(world))
    resumed = _rollback(world, to=a.name, activity=_log(world))          # 強行指定なしの再実行
    assert resumed.outcome == "rolled_back" and resumed.op_id == first.op_id
    op_lines = [line for line in _log_lines(world) if "\trollback_history_guard_forced_op\t" in line]
    assert len(op_lines) == 1 and f"op_id={first.op_id}" in op_lines[0]


def test_a_resume_cannot_finish_while_the_op_audit_stays_unwritable(world):
    a, b = _strategies(world, target_evidence=None)
    assert _forced(world, a, activity=_FlakyAudit(_log(world), fail_on=2)).outcome == "needs_retry"
    stuck = _rollback(world, to=a.name, activity=_FlakyAudit(_log(world), fail_on=1))
    assert stuck.outcome == "needs_retry" and _live(world) == f".versions/sma/{b.name}"


def test_an_ordinary_open_rollback_resume_writes_no_forced_record(world):
    a, b = _strategies(world, target_evidence=None)
    world.intent("sma", from_ver=b, to_ver=a)
    assert _rollback(world, to=a.name, activity=_log(world)).outcome == "rolled_back"
    assert not any("history_guard_force" in line for line in _log_lines(world))


def test_the_first_audit_row_reads_as_an_intent_and_a_failed_intent_leaves_no_op(world, monkeypatch):
    a, b = _strategies(world, target_evidence=None)

    def boom(*args, **kwargs):
        raise RuntimeError("state changed")

    monkeypatch.setattr(deploy_rollback.deploy_ops, "record_intent", boom)
    with pytest.raises(RuntimeError):
        _forced(world, a)
    lines = _log_lines(world)
    assert _ops(world) == [] and _live(world) == f".versions/sma/{b.name}"
    assert len(lines) == 1 and "\trollback_history_guard_force_started\t" in lines[0]
    assert "_forced_op" not in lines[0]
