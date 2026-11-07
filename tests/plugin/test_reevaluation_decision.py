"""Approving, rejecting and retrying a re-evaluation approval request.

A re-evaluation request replaces the evidence for the version that is already live.
Approving it changes nothing on disk; rejecting it stops the deployment the way
``afx plugin disable`` does.
"""
from __future__ import annotations

import json
import logging
import os

import pytest

from agentic_fx.plugin import deploy_ops, loader, switch
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import plugin_deploy_ops as deploy_store
from agentic_fx.store import plugin_switch_journal as journal_store
from tests.plugin.conftest import SETTINGS
from tests.plugin.deploy_world import World, at

NAME = "sma"
EPOCH = "time_repair_dst_v1"


class Recorder:
    def __init__(self):
        self.events = []

    def write(self, category, event, summary, **_kwargs):
        self.events.append((event, summary))

    def names(self):
        return [event for event, _summary in self.events]


class Case:
    """A deployed strategy with a pending re-evaluation request."""

    def __init__(self, tmp_path, *, indicator: bool = False, epoch: bool = True):
        self.world = World(tmp_path)
        self.conn = self.world.conn
        self.root = self.world.root
        if epoch:
            self.conn.execute(
                "INSERT INTO data_migrations(name, applied_at) VALUES (?, ?)",
                (EPOCH, at(0).isoformat()))
            self.conn.commit()
        self.indicator = None
        if indicator:
            self.indicator = self.world.version("ind", "i", kind="indicator")
            self.world.deploy("ind", self.indicator, None, 1)
        self.version = self.world.version(
            NAME, "a", kind="strategy", indicator="ind" if indicator else None)
        self.basis_id = self.world.deploy(NAME, self.version, None, 2)
        self.content = loader.content_hash(self.version)
        self.artifact = self.version.name
        self.activity = Recorder()
        self.approval_id = self.pending()

    def payload(self, **override) -> dict:
        payload = {
            "name": NAME, "kind": "strategy", "content_hash": self.content,
            "artifact_hash": self.artifact, "candidate_origin": "reevaluation",
            "candidate_path": f"plugins/.versions/{NAME}/{self.artifact}",
            "in_sample": {}, "holdout": {}, "run_ids": {},
            "history_epoch": switch.history_epoch(self.conn),
            "eval_now": at(30).isoformat(),
            "reevaluates_approval_id": self.basis_id,
            "basis_decision_id": self.basis_id,
            "deploy_generation": switch.deploy_generation(self.conn, NAME),
            "previous": {}, "reason": "history_time_repaired",
            "indicator_deps": ({"fast": {
                "plugin": "ind", "content_hash": loader.content_hash(self.indicator),
                "params": {}}} if self.indicator is not None else {}),
        }
        payload.update(override)
        return payload

    def pending(self, **override) -> int:
        return approvals_store.create(self.conn, "plugin", self.payload(**override),
                                      at(40))

    def row(self, approval_id=None):
        return dict(self.conn.execute(
            "SELECT * FROM approval_requests WHERE id=?",
            (approval_id or self.approval_id,)).fetchone())

    def approve(self, **kwargs):
        return switch.approve_candidate(
            self.conn, self.approval_id, decided_by="human", now=at(50),
            plugins_root=self.root, settings=SETTINGS, activity=self.activity, **kwargs)

    def reject(self, reason="no", **kwargs):
        return switch.reject_candidate(
            self.conn, self.approval_id, decided_by="human", reason=reason, now=at(50),
            plugins_root=self.root, activity=self.activity, **kwargs)

    def retry(self, **kwargs):
        return switch.retry_approval(
            self.conn, self.approval_id, decided_by="human", now=at(50),
            plugins_root=self.root, settings=SETTINGS, activity=self.activity, **kwargs)

    def live_target(self):
        live = self.root / NAME
        return os.readlink(live) if live.is_symlink() else None

    def decision(self):
        decision = approvals_store.latest_decision_for_identity(
            self.conn, NAME, self.content)
        return None if decision is None else (decision["id"], decision["status"])

    def state(self):
        return (self.live_target(), self.decision(), self.row()["status"],
                [tuple(r) for r in self.conn.execute("SELECT * FROM plugin_deploy_ops")],
                [tuple(r) for r in self.conn.execute(
                    "SELECT op_id, phase FROM plugin_switch_journal")],
                sorted(p.name for p in (self.root / ".versions" / NAME).iterdir()),
                [tuple(r) for r in self.conn.execute("SELECT id, status FROM signals")])


@pytest.fixture
def case(tmp_path):
    return Case(tmp_path)


def _later_approval(case: Case, minute: int = 5) -> int:
    approval_id = approvals_store.create(
        case.conn, "plugin",
        {"name": NAME, "content_hash": case.content, "artifact_hash": case.artifact},
        at(minute))
    approvals_store.apply_decision(case.conn, approval_id, "approved",
                                   decided_by="human", now=at(minute))
    return approval_id


def _switch_live(case: Case, version, minute: int) -> None:
    case.world.link(NAME, version)
    case.world.journal(NAME, "approve", None, version, minute)


def _stale_by_live_target(case):
    case.world.link(NAME, case.world.version(NAME, "b", kind="strategy"))


def _stale_by_live_absent(case):
    (case.root / NAME).unlink()


def _stale_by_version_dir(case):
    plugin = case.version / "plugin.py"
    plugin.write_text(plugin.read_text() + "# edited in place\n")


def _stale_by_new_approval(case):
    _later_approval(case)


def _stale_by_revocation(case):
    approvals_store.append_revocation(case.conn, NAME, case.content, now=at(6))


def _stale_by_generation(case):
    # live returns to the evaluated version after a round trip through another one
    other = case.world.version(NAME, "b", kind="strategy")
    case.world.link(NAME, other)
    case.world.journal(NAME, "approve", case.version, other, 5)
    case.world.link(NAME, case.version)
    case.world.journal(NAME, "approve", other, case.version, 6)


def _stale_by_generation_deploy_op(case):
    case.world.intent(NAME, op_kind="rollback_symlink", phase="done",
                      from_ver=case.version, to_ver=case.version, minute=5)


STALE_CASES = {
    "live_target": _stale_by_live_target,
    "live_absent": _stale_by_live_absent,
    "version_dir_hash": _stale_by_version_dir,
    "newer_approval": _stale_by_new_approval,
    "revoked": _stale_by_revocation,
    "round_trip": _stale_by_generation,
    "deploy_op": _stale_by_generation_deploy_op,
}


# ---------------------------------------------------------------------- approve

def test_approve_records_the_new_approval_and_leaves_the_deployment_alone(case):
    before = case.state()
    versions_before = sorted(p.name for p in (case.root / ".versions").iterdir())

    outcome = case.approve()

    assert (outcome.outcome, outcome.status, outcome.name) == (
        "reevaluation_confirmed", "approved", NAME)
    row = case.row()
    assert (row["status"], row["reason"], row["decided_by"]) == (
        "approved", "reevaluation_confirmed", "human")
    assert case.decision() == (case.approval_id, "approved")
    latest = approvals_store.latest_approved_for_identity(case.conn, NAME, case.content)
    assert latest["id"] == case.approval_id
    after = case.state()
    # live, version dirs, journal and deploy ops are untouched; only the decision moved
    assert (after[0], after[3], after[4], after[5], after[6]) == (
        before[0], before[3], before[4], before[5], before[6])
    assert sorted(p.name for p in (case.root / ".versions").iterdir()) == versions_before
    assert "reevaluation_approved" in case.activity.names()
    summary = dict(case.activity.events)["reevaluation_approved"]
    for fragment in (str(case.approval_id), NAME, case.content, case.artifact,
                     str(case.basis_id)):
        assert fragment in summary


def test_approve_does_not_resolve_a_candidate_locator(case):
    # the canonical path exists on disk as a version dir, not as a staging / human candidate;
    # the unknown origin must stay rejected for every other route
    with pytest.raises(ValueError, match="unknown candidate_origin"):
        switch.resolve_candidate_dir(
            case.root, candidate_origin="reevaluation",
            candidate_path=f"plugins/.versions/{NAME}/{case.artifact}", name=NAME)
    assert case.approve().outcome == "reevaluation_confirmed"


def test_approve_with_a_dependent_indicator_takes_its_lock_and_checks_it(tmp_path):
    case = Case(tmp_path, indicator=True)
    assert case.approve().outcome == "reevaluation_confirmed"


def test_approve_rejects_a_changed_payload_digest(case):
    before = case.state()
    with pytest.raises(switch.ApprovalPayloadChanged):
        case.approve(expected_payload_sha256="0" * 64)
    assert case.state() == before


def test_approve_refuses_a_candidate_path_that_is_not_the_canonical_form(case):
    other = "plugins/.versions/%s/%s" % (NAME, "f" * 64)
    approval_id = case.pending(candidate_path=other)
    with pytest.raises(ValueError):
        switch.approve_candidate(
            case.conn, approval_id, decided_by="human", now=at(50),
            plugins_root=case.root, settings=SETTINGS, activity=case.activity)
    assert case.row(approval_id)["status"] == "pending"


@pytest.mark.parametrize("stale", sorted(STALE_CASES))
def test_approve_of_a_stale_request_invalidates_it_and_changes_nothing_else(case, stale):
    STALE_CASES[stale](case)
    before = case.state()

    outcome = case.approve()

    assert (outcome.outcome, outcome.status, outcome.reason) == (
        "invalidated", "invalidated", "reevaluation_stale")
    row = case.row()
    assert (row["status"], row["reason"]) == ("invalidated", "reevaluation_stale")
    after = case.state()
    # the version's own latest decision is not touched: a revoked version stays revoked
    assert (after[0], after[1], after[3], after[4], after[5], after[6]) == (
        before[0], before[1], before[3], before[4], before[5], before[6])
    assert "reevaluation_invalidated" in case.activity.names()
    assert "reevaluation_approved" not in case.activity.names()


@pytest.mark.parametrize("stale", ["indicator_live", "indicator_revoked"])
def test_approve_of_a_request_whose_indicator_pin_moved_is_stale(tmp_path, stale):
    case = Case(tmp_path, indicator=True)
    if stale == "indicator_live":
        case.world.link("ind", case.world.version("ind", "j", kind="indicator"))
    else:
        approvals_store.append_revocation(
            case.conn, "ind", loader.content_hash(case.indicator), now=at(6))

    outcome = case.approve()

    assert (outcome.outcome, outcome.reason) == ("invalidated", "reevaluation_stale")
    assert case.row()["status"] == "invalidated"


def test_approve_pending_request_with_a_changed_epoch_stays_pending(case):
    case.conn.execute("INSERT INTO data_migrations(name, applied_at) VALUES (?, ?)",
                      ("time_repair_next", at(20).isoformat()))
    case.conn.commit()
    before = case.state()

    outcome = case.approve()

    assert (outcome.outcome, outcome.status) == ("history_epoch_changed", "pending")
    assert case.state() == before


def test_approve_rechecks_the_epoch_inside_the_decision_transaction(case, monkeypatch):
    real = switch.history_epoch
    calls = []

    def moving(conn):
        calls.append(1)
        return real(conn) if len(calls) == 1 else "time_repair_moved"

    monkeypatch.setattr(switch, "history_epoch", moving)
    before = case.state()

    outcome = case.approve()

    assert len(calls) >= 2
    assert (outcome.outcome, outcome.status) == ("history_epoch_changed", "pending")
    assert case.state() == before


def test_approve_refuses_while_a_deploy_op_or_journal_is_open(case):
    case.world.intent(NAME, op_kind="disable_symlink", from_ver=case.version, minute=5)
    with pytest.raises(switch.OpenDeployOpError):
        case.approve()
    assert case.row()["status"] == "pending"


def test_approve_refuses_while_a_switch_journal_is_open(case):
    other = case.world.version(NAME, "b", kind="strategy")
    journal_store.insert(
        case.conn, kind="approve", approval_id=case.basis_id, name=NAME,
        old_kind="symlink", old_target=case.world.target(NAME, case.version),
        temp_path="", new_target=case.world.target(NAME, other), switch_required=True,
        actor="human", now=at(5))
    with pytest.raises(switch.UnresolvedJournalError):
        case.approve()
    assert case.row()["status"] == "pending"


def test_approving_a_decided_request_reports_already_decided(case):
    case.approve()
    assert case.approve().outcome == "already_decided"


def test_approving_does_not_settle_a_candidate_archive(case, caplog):
    with caplog.at_level(logging.WARNING, logger="agentic_fx.store.approvals"):
        case.approve()
    assert not [r for r in caplog.records if "candidate archive" in r.getMessage()]
    # the same decision on an ordinary plugin approval still reaches the settle step
    plain = approvals_store.create(
        case.conn, "plugin", {"name": "other", "content_hash": "h", "artifact_hash": "a"},
        at(41))
    with caplog.at_level(logging.WARNING, logger="agentic_fx.store.approvals"):
        approvals_store.apply_decision(case.conn, plain, "approved", decided_by="h",
                                       now=at(51))
    assert [r for r in caplog.records if "candidate archive" in r.getMessage()]


# ----------------------------------------------------------- later-approval filter

def test_a_reevaluation_approval_does_not_supersede_another_candidate(case):
    other = approvals_store.create(
        case.conn, "plugin",
        {"name": NAME, "content_hash": "d" * 64, "artifact_hash": "e" * 64}, at(39))
    case.approval_id = case.pending()
    case.approve()
    row = dict(case.conn.execute("SELECT * FROM approval_requests WHERE id=?",
                                 (other,)).fetchone())
    assert switch._is_superseded(case.conn, row, json.loads(row["payload_json"])) is False


def test_an_ordinary_later_approval_still_supersedes_another_candidate(case):
    other = approvals_store.create(
        case.conn, "plugin",
        {"name": NAME, "content_hash": "d" * 64, "artifact_hash": "e" * 64}, at(1))
    _later_approval(case, minute=5)
    # same identity as the pending row, so it is not a different content_hash
    row = dict(case.conn.execute("SELECT * FROM approval_requests WHERE id=?",
                                 (other,)).fetchone())
    assert switch._is_superseded(case.conn, row, json.loads(row["payload_json"])) is True


# ----------------------------------------------------------------------- reject

def test_reject_stops_the_deployment_like_disable(case):
    pending_signal = case.world.signal(NAME, case.content)
    claimed_signal = case.world.signal(NAME, case.content, claimed=True)
    other_signal = case.world.signal("other", "x" * 8)

    outcome = case.reject("because")

    assert (outcome.outcome, outcome.status, outcome.deploy_phase) == (
        "rejected", "rejected", "done")
    row = case.row()
    assert (row["status"], row["reason"], row["decided_by"]) == (
        "rejected", "because", "human")
    assert case.decision() == (case.approval_id, "rejected")
    assert case.live_target() is None and not (case.root / NAME).exists()
    op = deploy_store.get_by_id(case.conn, outcome.op_id)
    assert (op["op_kind"], op["phase"], op["approval_id"], op["revoke"], op["force"]) == (
        "disable_symlink", "done", case.basis_id, 0, 0)
    assert (op["subject_content_hash"], op["subject_artifact_hash"]) == (
        case.content, case.artifact)
    assert op["notify_attempted"] == 1
    statuses = {r["id"]: r["status"] for r in case.conn.execute(
        "SELECT id, status FROM signals")}
    assert statuses[pending_signal] == "abandoned"
    assert statuses[claimed_signal] == "claimed"
    assert statuses[other_signal] == "pending"
    assert deploy_store.linked_signal_ids(case.conn, outcome.op_id) == {
        pending_signal, claimed_signal}
    assert "plugin_disabled" in case.activity.names()
    assert "reevaluation_rejected" in case.activity.names()
    assert "phase=done" in dict(case.activity.events)["reevaluation_rejected"]
    assert (case.root / ".versions" / NAME / case.artifact).is_dir()


def test_reject_does_not_touch_the_candidate_ledger(case):
    before = case.conn.execute("SELECT COUNT(*) FROM candidate_archives").fetchone()[0]
    case.reject()
    assert case.conn.execute("SELECT COUNT(*) FROM candidate_archives").fetchone()[0] == before


def test_reject_needs_no_digest_and_ignores_a_changed_payload(case):
    case.conn.execute(
        "UPDATE approval_requests SET payload_json=? WHERE id=?",
        (json.dumps({**case.payload(), "note": "edited"}), case.approval_id))
    case.conn.commit()
    assert case.reject().outcome == "rejected"


def test_reject_runs_the_unlink_in_the_same_lock_as_the_decision(case, monkeypatch):
    seen = []
    real_unlink = os.unlink

    def watching(path, *args, **kwargs):
        if os.path.basename(str(path)) == NAME:
            fd = os.open(case.root / ".locks" / f"{NAME}.lock", os.O_RDWR | os.O_CREAT)
            try:
                import fcntl
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    seen.append("lock free")
                except BlockingIOError:
                    seen.append("lock held")
            finally:
                os.close(fd)
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", watching)
    case.reject()
    assert seen == ["lock held"]


def test_reject_decision_intent_and_signal_cleanup_commit_together(case, monkeypatch):
    signal_id = case.world.signal(NAME, case.content)

    def failing(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(deploy_ops, "write_intent_rows", failing)
    with pytest.raises(RuntimeError):
        case.reject()
    assert case.row()["status"] == "pending"
    assert case.decision() == (case.basis_id, "approved")
    assert case.conn.execute("SELECT COUNT(*) FROM plugin_deploy_ops").fetchone()[0] == 0
    assert case.conn.execute("SELECT status FROM signals WHERE id=?",
                             (signal_id,)).fetchone()[0] == "pending"
    assert case.live_target() is not None


def test_reject_signal_cleanup_failure_rolls_back_the_decision_and_the_intent(case,
                                                                               monkeypatch):
    from agentic_fx.store import signals as signals_store

    def failing(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(signals_store, "abandon_pending_for_identities", failing)
    with pytest.raises(RuntimeError):
        case.reject()
    assert case.row()["status"] == "pending"
    assert case.conn.execute("SELECT COUNT(*) FROM plugin_deploy_ops").fetchone()[0] == 0
    assert case.live_target() is not None


def test_reject_unlink_failure_leaves_a_retryable_op_and_still_rejects(case, monkeypatch):
    real_unlink = os.unlink

    def failing(path, *args, **kwargs):
        if os.path.basename(str(path)) == NAME:
            raise OSError("denied")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", failing)
    outcome = case.reject()

    assert (outcome.outcome, outcome.status, outcome.deploy_phase) == (
        "rejected", "rejected", "needs_retry")
    assert case.decision() == (case.approval_id, "rejected")
    assert case.live_target() is not None
    op = deploy_store.get_by_id(case.conn, outcome.op_id)
    assert (op["phase"], op["fail_reason"]) == ("needs_retry", "syscall_failed")
    assert "phase=needs_retry" in dict(case.activity.events)["reevaluation_rejected"]

    monkeypatch.setattr(os, "unlink", real_unlink)
    from agentic_fx.plugin import deploy_disable
    done = deploy_disable.disable_plugin(
        case.conn, case.root, NAME, revoke=False, force=False, now=at(60),
        settings=SETTINGS, activity=case.activity)
    assert done.outcome == "disabled" and not (case.root / NAME).exists()
    assert deploy_store.get_by_id(case.conn, outcome.op_id)["phase"] == "done"


def test_reject_reports_a_foreign_live_without_unlinking(case, monkeypatch):
    monkeypatch.setattr(deploy_ops, "live_matches_from", lambda *_a, **_k: False)
    outcome = case.reject()
    assert (outcome.outcome, outcome.deploy_phase) == ("rejected", "foreign")
    assert case.decision() == (case.approval_id, "rejected")
    assert case.live_target() is not None
    assert deploy_store.get_by_id(case.conn, outcome.op_id)["phase"] == "foreign"


@pytest.mark.parametrize("stale", sorted(STALE_CASES))
def test_reject_of_a_stale_request_invalidates_it_and_stops_nothing(case, stale):
    STALE_CASES[stale](case)
    signal_id = case.world.signal(NAME, case.content)
    before = case.state()

    outcome = case.reject()

    assert (outcome.outcome, outcome.status, outcome.reason, outcome.deploy_phase) == (
        "invalidated", "invalidated", "reevaluation_stale", None)
    row = case.row()
    assert (row["status"], row["reason"]) == ("invalidated", "reevaluation_stale")
    after = case.state()
    assert (after[0], after[1], after[3], after[4], after[5]) == (
        before[0], before[1], before[3], before[4], before[5])
    assert case.conn.execute("SELECT status FROM signals WHERE id=?",
                             (signal_id,)).fetchone()[0] == "pending"
    assert "reevaluation_invalidated" in case.activity.names()
    assert "plugin_disabled" not in case.activity.names()


def test_reject_with_a_changed_epoch_stays_pending_and_stops_nothing(case):
    case.conn.execute("INSERT INTO data_migrations(name, applied_at) VALUES (?, ?)",
                      ("time_repair_next", at(20).isoformat()))
    case.conn.commit()
    before = case.state()
    outcome = case.reject()
    assert (outcome.outcome, outcome.status, outcome.deploy_phase) == (
        "history_epoch_changed", "pending", None)
    assert case.state() == before


def test_reject_rechecks_the_epoch_inside_the_decision_transaction(case, monkeypatch):
    real = switch.history_epoch
    calls = []

    def moving(conn):
        calls.append(1)
        return real(conn) if len(calls) == 1 else "time_repair_moved"

    monkeypatch.setattr(switch, "history_epoch", moving)
    before = case.state()
    outcome = case.reject()
    assert len(calls) >= 2
    assert outcome.outcome == "history_epoch_changed"
    assert case.state() == before


def test_reject_refuses_while_a_deploy_op_is_open(case):
    case.world.intent(NAME, op_kind="disable_symlink", from_ver=case.version, minute=5)
    with pytest.raises(switch.OpenDeployOpError):
        case.reject()
    assert case.row()["status"] == "pending"
    assert case.live_target() is not None


def test_rejecting_a_decided_request_raises_already_decided(case):
    case.reject()
    with pytest.raises(approvals_store.AlreadyDecidedError):
        case.reject()


def test_a_reevaluation_reject_never_takes_the_plain_reject_path(case, monkeypatch):
    def plain(*_args, **_kwargs):
        raise AssertionError("plain reject path used")

    monkeypatch.setattr(switch, "_drop_staging_candidate", plain)
    outcome = case.reject()
    assert outcome.outcome == "rejected"
    assert not (case.root / NAME).exists()


def test_a_plain_reject_still_returns_a_bool(tmp_path):
    world = World(tmp_path)
    approval_id = approvals_store.create(
        world.conn, "plugin",
        {"name": "plain", "content_hash": "h", "artifact_hash": "a",
         "candidate_origin": "human", "candidate_path": "plugins/_human/plain"}, at(1))
    result = switch.reject_candidate(
        world.conn, approval_id, decided_by="human", reason="no", now=at(2),
        plugins_root=world.root)
    assert result is True


# ------------------------------------------------------------------------ retry

def test_retry_of_a_reevaluation_request_is_refused_and_changes_nothing(case):
    before = case.state()
    outcome = case.retry()
    assert (outcome.outcome, outcome.status) == ("still_pending", "pending")
    assert outcome.reason == "reevaluation_retry_unsupported"
    assert case.state() == before
    assert "reevaluation_approved" not in case.activity.names()


def test_a_request_based_on_a_revocation_row_is_stale_even_though_the_ids_match(case):
    revoked = approvals_store.append_revocation(case.conn, NAME, case.content, now=at(6))
    case.approval_id = case.pending(basis_decision_id=revoked,
                                    deploy_generation=switch.deploy_generation(case.conn, NAME))
    outcome = case.approve()
    assert (outcome.outcome, outcome.reason) == ("invalidated", "reevaluation_stale")
    assert case.decision() == (revoked, "rejected")
