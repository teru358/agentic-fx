"""承認時の履歴証跡の照合 (本物の検証を通す): 通常依頼の失効、再評価の保留、journal の再開、
再評価の重複判定。検査関数だけを差し替える共通 fixture の下でも、照合は本物のまま。"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

import pytest
import yaml

from agentic_fx.backtest import history_health as hh
from agentic_fx.plugin import history_reasons, switch
from agentic_fx.store import approvals as approvals_store
from tests.history_support import ready_evidence
from tests.plugin.conftest import SETTINGS
from tests.plugin.deploy_world import at
from tests.plugin.reevaluation_world import BASE, FakeGate, SOURCE, STARTED
from tests.plugin.test_reevaluate_deployed import Deployed
from tests.plugin.test_reevaluation_decision import Case, NAME, _stage_candidate

CONFIGURED = SETTINGS.model_copy(update={
    "backtest": SETTINGS.backtest.model_copy(update={"eval_source": "mt5"})})


def _list_path(case):
    return case.root.parent / "config" / "history-no-bars.yaml"


def _write_list(case, entries):
    path = _list_path(case)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"entries": entries}), encoding="utf-8")


def _entry(day="2026-08-01", symbol="USDJPY"):
    return {"source": "mt5", "base_interval": "1m", "symbol": symbol, "date_utc": day,
            "reason": "休場", "approved_by": "operator",
            "approved_at": "2026-08-02T00:00:00+00:00"}


@pytest.fixture
def case(tmp_path):
    return Case(tmp_path)


def _submit(case, monkeypatch):
    from tests.fixtures.wiring_envs import install_gate_double
    install_gate_double(monkeypatch)
    _stage_candidate(case, "b")
    return switch.submit_candidate(
        case.conn, name=NAME, staging_dir=case.root / "_staging" / "1",
        candidate_origin="staging", mission_id=1, backlog_id=None, settings=CONFIGURED,
        now=at(20))


def _payload(case, approval_id):
    return json.loads(case.conn.execute(
        "SELECT payload_json FROM approval_requests WHERE id=?", (approval_id,)).fetchone()[0])


def _store(case, approval_id, payload):
    case.conn.execute("UPDATE approval_requests SET payload_json=? WHERE id=?",
                      (json.dumps(payload), approval_id))
    case.conn.commit()


def _approve(case, approval_id):
    return switch.approve_candidate(
        case.conn, approval_id, decided_by="human", now=at(60), plugins_root=case.root,
        settings=CONFIGURED, activity=case.activity)


def _status(case, approval_id):
    row = case.conn.execute("SELECT status, reason FROM approval_requests WHERE id=?",
                            (approval_id,)).fetchone()
    return row["status"], row["reason"]


def _versions(case):
    return sorted(p.name for p in (case.root / ".versions" / NAME).iterdir())


# ---- 通常依頼 -------------------------------------------------------------------

def test_an_untampered_request_deploys_and_carries_the_gates_evidence(case, monkeypatch):
    approval_id = _submit(case, monkeypatch)
    assert _payload(case, approval_id)["history_guard"]["verdict"] == "ready"
    assert _approve(case, approval_id).outcome == "deployed"


@pytest.mark.parametrize("tamper", [
    lambda p: p.pop("history_guard"),
    lambda p: p.update(history_guard=None),
    lambda p: p["history_guard"].update(version=0),
    lambda p: p["history_guard"].update(verdict="history_stale"),
    lambda p: p["history_guard"].update(calendar_version="old"),
    lambda p: p["history_guard"]["dataset"].update(source="dukascopy"),
    lambda p: p.update(eval_source="dukascopy"),
])
def test_a_confirmed_mismatch_invalidates_with_a_fixed_reason_and_changes_nothing(
        case, monkeypatch, tamper):
    approval_id = _submit(case, monkeypatch)
    payload = _payload(case, approval_id)
    tamper(payload)
    _store(case, approval_id, payload)
    before = (case.live_target(), _versions(case), (case.root / "_staging" / "1" / NAME).exists())
    journals = case.conn.execute("SELECT COUNT(*) FROM plugin_switch_journal").fetchone()[0]

    outcome = _approve(case, approval_id)

    assert (outcome.outcome, outcome.status, outcome.reason) == (
        history_reasons.OUTCOME, "invalidated", history_reasons.EVIDENCE_INVALIDATED)
    assert _status(case, approval_id) == ("invalidated", history_reasons.EVIDENCE_INVALIDATED)
    assert (case.live_target(), _versions(case),
            (case.root / "_staging" / "1" / NAME).exists()) == before
    assert before[2] is True                      # 候補は削除しない
    assert case.conn.execute(
        "SELECT COUNT(*) FROM plugin_switch_journal").fetchone()[0] == journals
    assert "approval_history_evidence_invalidated" in case.activity.names()


def test_filling_the_history_later_does_not_revive_a_request_without_evidence(
        case, monkeypatch):
    approval_id = _submit(case, monkeypatch)
    payload = _payload(case, approval_id)
    payload.pop("history_guard")
    _store(case, approval_id, payload)
    assert _approve(case, approval_id).status == "invalidated"
    assert _approve(case, approval_id).outcome == "already_decided"


def test_a_change_to_an_in_scope_no_bars_entry_invalidates_but_an_unrelated_one_does_not(
        case, monkeypatch):
    approval_id = _submit(case, monkeypatch)
    _write_list(case, [_entry(symbol="EURUSD")])                 # 別 pair
    assert _approve(case, approval_id).outcome == "deployed"

    other = _submit_again(case, monkeypatch)
    _write_list(case, [_entry(symbol="USDJPY", day=_in_range_day(case, other))])
    assert _approve(case, other).reason == history_reasons.EVIDENCE_INVALIDATED


def _in_range_day(case, approval_id):
    evidence = _payload(case, approval_id)["history_guard"]
    return evidence["holdout_end"][:10]


def _submit_again(case, monkeypatch):
    _stage_candidate_again(case)
    return switch.submit_candidate(
        case.conn, name=NAME, staging_dir=case.root / "_staging" / "2",
        candidate_origin="staging", mission_id=2, backlog_id=None, settings=CONFIGURED,
        now=at(70))


def _stage_candidate_again(case):
    staging = case.root / "_staging" / "2" / NAME
    staging.mkdir(parents=True)
    (staging / "plugin.py").write_text(
        "# c\ndef evaluate(df, indicators, signals, params):\n    return {'action': 'hold'}\n")
    (staging / "config.yaml").write_text(
        "kind: strategy\ntimeframe: 1h\npairs: [USDJPY]\nexit_mode: levels\n")
    (staging / "test_plugin.py").write_text("def test_plugin():\n    pass\n")


def test_an_unreadable_list_keeps_the_request_pending_and_a_repair_lets_it_through(
        case, monkeypatch):
    approval_id = _submit(case, monkeypatch)
    _list_path(case).parent.mkdir(parents=True, exist_ok=True)
    _list_path(case).write_text("entries: [", encoding="utf-8")

    outcome = _approve(case, approval_id)

    assert (outcome.outcome, outcome.status, outcome.reason) == (
        history_reasons.OUTCOME, "pending", history_reasons.CHECK_UNAVAILABLE)
    assert _status(case, approval_id) == ("pending", history_reasons.CHECK_UNAVAILABLE)
    _list_path(case).unlink()
    assert _approve(case, approval_id).outcome == "deployed"


# ---- journal の再開 ---------------------------------------------------------------

def _open_own_journal(case, approval_id, phase):
    payload = _payload(case, approval_id)
    op_id = switch.begin_switch_journal(
        case.conn, kind="approve", approval_id=approval_id, name=NAME, old_kind="symlink",
        old_target=case.live_target(),
        new_target=f".versions/{NAME}/{payload['artifact_hash']}", switch_required=True,
        actor="human", now=at(30), commit=True)
    if phase != "preparing":
        from agentic_fx.store import plugin_switch_journal as journal_store
        journal_store.set_phase(case.conn, op_id, phase, now=at(31), commit=True)
    return op_id


@pytest.mark.parametrize("phase", ["preparing", "versioned"])
def test_resuming_its_own_journal_is_not_checked_even_with_a_broken_evidence(
        case, monkeypatch, phase):
    approval_id = _submit(case, monkeypatch)
    payload = _payload(case, approval_id)
    payload.pop("history_guard")
    _store(case, approval_id, payload)
    _open_own_journal(case, approval_id, phase)

    outcome = _approve(case, approval_id)

    assert outcome.outcome != history_reasons.OUTCOME
    assert _status(case, approval_id)[0] != "invalidated"


def test_another_approvals_open_journal_keeps_its_existing_refusal(case, monkeypatch):
    first = _submit(case, monkeypatch)
    _open_own_journal(case, first, "preparing")
    second = _submit_again(case, monkeypatch)
    payload = _payload(case, second)
    payload.pop("history_guard")
    _store(case, second, payload)

    with pytest.raises(switch.UnresolvedJournalError):
        _approve(case, second)
    assert _status(case, second)[0] == "pending"


# ---- 再評価の依頼 -----------------------------------------------------------------

def _tamper_pending_reevaluation(case, approval_id, mutate):
    payload = _payload(case, approval_id)
    mutate(payload)
    _store(case, approval_id, payload)


def test_a_mismatched_reevaluation_stays_pending_and_can_still_be_rejected(case):
    reeval = case.approval_id
    _tamper_pending_reevaluation(case, reeval, lambda p: p.pop("history_guard"))

    outcome = case.approve()

    assert (outcome.outcome, outcome.status, outcome.reason) == (
        history_reasons.OUTCOME, "pending", history_reasons.EVIDENCE_REFRESH_REQUIRED)
    assert _status(case, reeval) == ("pending", history_reasons.EVIDENCE_REFRESH_REQUIRED)
    assert case.decision() == (case.basis_id, "approved")     # 基準の決定は変わらない
    rejected = case.reject()
    assert rejected.outcome == "rejected" and case.live_target() is None


def test_an_unreadable_list_holds_a_reevaluation_and_a_repair_confirms_it(case):
    _list_path(case).parent.mkdir(parents=True, exist_ok=True)
    _list_path(case).write_text("entries: 3", encoding="utf-8")
    held = case.approve()
    assert (held.status, held.reason) == ("pending", history_reasons.CHECK_UNAVAILABLE)
    _list_path(case).unlink()
    assert case.approve().outcome == "reevaluation_confirmed"


def test_a_sound_reevaluation_is_confirmed(case):
    assert case.approve().outcome == "reevaluation_confirmed"


# ---- 再評価の重複判定 ---------------------------------------------------------------

@pytest.fixture
def dep(tmp_path):
    return Deployed(tmp_path)


def _in_scope_day(dep, approval_id):
    return dep.payload(approval_id)["history_guard"]["holdout_end"][:10]


def _dep_entry(day, symbol="USDJPY"):
    return {"source": SOURCE, "base_interval": BASE, "symbol": symbol, "date_utc": day,
            "reason": "休場", "approved_by": "operator",
            "approved_at": "2026-11-09T00:00:00+00:00"}


def _dep_list(dep, entries):
    path = dep.root.parent / "config" / "history-no-bars.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"entries": entries}), encoding="utf-8")


def test_an_unchanged_sound_request_is_existing(dep):
    first = dep.reevaluate(FakeGate())
    gate = FakeGate()
    again = dep.reevaluate(gate, now=STARTED.replace(hour=9))      # 時計が進んだだけ
    assert (again.status, again.approval_id) == ("existing", first.approval_id)
    assert gate.calls == []


@pytest.mark.parametrize("mutate", [
    lambda p: p.pop("history_guard"),
    lambda p: p["history_guard"].update(version=0),
    lambda p: p["history_guard"].update(calendar_version="old"),
    lambda p: p["history_guard"]["threshold_snapshot"].update(stale_open_hours=48),
])
def test_a_request_without_a_sound_evidence_is_never_existing_and_is_superseded(dep, mutate):
    first = dep.reevaluate(FakeGate())
    payload = dep.payload(first.approval_id)
    mutate(payload)
    dep.conn.execute("UPDATE approval_requests SET payload_json=? WHERE id=?",
                     (json.dumps(payload), first.approval_id))
    dep.conn.commit()

    gate = FakeGate()
    second = dep.reevaluate(gate)

    assert second.status == "created" and len(gate.calls) == 1
    old = dep.requests()[0]
    assert (old["status"], old["reason"]) == ("invalidated", "reevaluation_superseded")
    assert [r["id"] for r in dep.requests("pending")] == [second.approval_id]


def test_an_in_scope_no_bars_change_supersedes_but_an_unrelated_one_does_not(dep):
    first = dep.reevaluate(FakeGate())
    _dep_list(dep, [_dep_entry("2026-09-01", symbol="EURUSD")])
    assert dep.reevaluate(FakeGate()).status == "existing"
    _dep_list(dep, [_dep_entry(_in_scope_day(dep, first.approval_id))])
    gate = FakeGate()
    second = dep.reevaluate(gate)
    assert second.status == "created" and len(gate.calls) == 1


def test_the_guard_version_is_part_of_both_keys(dep, monkeypatch):
    first = dep.reevaluate(FakeGate())
    payload = dep.payload(first.approval_id)
    with switch._plugin_locks(dep.root, [NAME]):
        snapshot = switch._take_reevaluation_snapshot(
            dep.conn, dep.root, NAME, dep.settings, (NAME,))
    assert switch._reevaluation_key(payload) == snapshot.key()
    monkeypatch.setattr(hh, "GUARD_VERSION", hh.GUARD_VERSION + 1)
    assert switch._reevaluation_key(payload) != snapshot.key()


def test_a_new_policy_makes_the_old_request_superseded_not_existing(dep, monkeypatch):
    first = dep.reevaluate(FakeGate())
    monkeypatch.setattr(hh, "GUARD_VERSION", hh.GUARD_VERSION + 1)
    second = dep.reevaluate(FakeGate())
    assert second.status == "created" and second.approval_id != first.approval_id
    assert dep.requests()[0]["status"] == "invalidated"
    assert dep.payload(second.approval_id)["history_guard"]["version"] == hh.GUARD_VERSION


def test_the_request_stores_the_gates_evidence_unchanged(dep):
    evidence_seen = []

    class Recording(FakeGate):
        def __call__(self, conn, snapshot, **kw):
            verdict = super().__call__(conn, snapshot, **kw)
            evidence_seen.append(verdict.history_guard)
            return verdict

    created = dep.reevaluate(Recording())
    assert dep.payload(created.approval_id)["history_guard"] == evidence_seen[0]


def test_a_switched_journal_is_recovered_even_when_the_evidence_no_longer_matches(
        case, monkeypatch):
    """symlink 切替済みで決定だけ未了の依頼は、証跡を再照合せず、既存の再検証で完了する
    (照合で止めると、新旧どちらの版も取引対象にならず strategy が消える)。"""
    approval_id = _submit(case, monkeypatch)
    real = switch._finalize_decision
    calls = []

    def crash_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("injected: crash before the decision is recorded")
        return real(*args, **kwargs)

    monkeypatch.setattr(switch, "_finalize_decision", crash_once)
    with pytest.raises(OSError):
        _approve(case, approval_id)
    journal = case.conn.execute(
        "SELECT phase FROM plugin_switch_journal WHERE approval_id=?", (approval_id,)).fetchone()
    assert journal["phase"] in ("recorded", "switched") and _status(case, approval_id)[0] == "pending"

    payload = _payload(case, approval_id)
    payload.pop("history_guard")
    _store(case, approval_id, payload)
    _list_path(case).parent.mkdir(parents=True, exist_ok=True)
    _list_path(case).write_text("entries: [", encoding="utf-8")

    outcome = _approve(case, approval_id)

    assert outcome.outcome in ("deployed", "deployed_after_rollback")
    assert _status(case, approval_id)[0] == "approved"


def test_a_success_without_the_gates_evidence_files_no_request(dep):
    class NoEvidence(FakeGate):
        def __call__(self, conn, snapshot, **kw):
            import dataclasses
            return dataclasses.replace(super().__call__(conn, snapshot, **kw), history_guard=None)

    result = dep.reevaluate(NoEvidence())
    assert (result.status, result.reason) == ("unevaluable", history_reasons.CHECK_UNAVAILABLE)
    assert dep.requests() == []
