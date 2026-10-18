from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.store import approvals, missions, plugin_deploy_ops as deploy_ops, signals
from agentic_fx.store import plugin_switch_journal as switch_journal
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def _conn(tmp_path):
    conn = connect(tmp_path / "ops.db")
    init_db(conn)
    return conn


def _disable_values(**changes):
    values = {
        "op_kind": "disable_symlink", "name": "alpha",
        "from_kind": "symlink", "to_kind": "absent",
        "from_target": ".versions/alpha/old", "to_target": None,
        "retired_path": None, "subject_content_hash": "content-old",
        "subject_artifact_hash": "artifact-old", "target_content_hash": None,
        "target_artifact_hash": None, "approval_id": 1, "revoke": 0,
        "revoke_row_id": None, "force": 0, "phase": "intent",
        "fail_reason": None, "notify_attempted": 0, "actor": "human_cli",
        "created_at": NOW.isoformat(), "updated_at": NOW.isoformat(),
    }
    values.update(changes)
    return values


def _insert_direct(conn, **changes):
    values = _disable_values(**changes)
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    return conn.execute(
        f"INSERT INTO plugin_deploy_ops ({columns}) VALUES ({placeholders})",
        tuple(values.values()),
    ).lastrowid


def _rollback_changes(**changes):
    values = {
        "op_kind": "rollback_symlink", "from_kind": "symlink",
        "to_kind": "symlink", "to_target": ".versions/alpha/new",
        "target_content_hash": "content-new", "target_artifact_hash": "artifact-new",
    }
    values.update(changes)
    return values


def _insert_intent(conn, **changes):
    values = _disable_values(**changes)
    return deploy_ops.insert_intent(
        conn,
        **{key: values[key] for key in (
            "op_kind", "name", "from_kind", "to_kind", "from_target", "to_target",
            "retired_path", "subject_content_hash", "subject_artifact_hash",
            "target_content_hash", "target_artifact_hash", "approval_id", "revoke",
            "revoke_row_id", "force", "actor")},
        now=NOW,
    )


def test_schema_accepts_valid_disable_row(tmp_path):
    conn = _conn(tmp_path)
    op_id = _insert_direct(conn)
    assert conn.execute("SELECT op_kind FROM plugin_deploy_ops WHERE op_id=?", (op_id,)).fetchone()[0] == "disable_symlink"


def test_signal_link_index_is_present_and_selected(tmp_path):
    conn = _conn(tmp_path)
    indexes = {row["name"] for row in conn.execute(
        "PRAGMA index_list(plugin_deploy_op_signals)")}
    assert "plugin_deploy_op_signals_signal_id" in indexes
    plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT 1 FROM plugin_deploy_op_signals "
        "WHERE signal_id=1").fetchall()
    assert "plugin_deploy_op_signals_signal_id" in " ".join(row[3] for row in plan)


@pytest.mark.parametrize("changes", [
    {"to_target": ".versions/alpha/x"},
    {"op_kind": "rollback_symlink", "to_kind": "symlink", "to_target": ".versions/alpha/new", "target_content_hash": None, "target_artifact_hash": None},
    {"op_kind": "retire_plain", "from_kind": "plain", "to_kind": "retired", "from_target": None, "subject_content_hash": "content-old", "subject_artifact_hash": "artifact-old", "retired_path": "_retired/alpha", "revoke": 1, "revoke_row_id": 1},
    {"op_kind": "revoke", "from_kind": "none", "to_kind": "none", "from_target": None, "subject_content_hash": "content-old", "subject_artifact_hash": "artifact-old", "revoke": 1, "revoke_row_id": 1, "force": 0, "phase": "intent"},
    {"revoke": 1, "revoke_row_id": None},
    {"phase": "needs_retry", "fail_reason": None},
    {"op_kind": "disable_symlink", "phase": "needs_retry", "fail_reason": "verify_failed"},
    {"op_kind": "revoke", "from_kind": "none", "to_kind": "none", "from_target": None, "subject_content_hash": "content-old", "subject_artifact_hash": "artifact-old", "revoke": 1, "revoke_row_id": 1, "force": 0, "phase": "needs_retry", "fail_reason": "syscall_failed"},
    {"notify_attempted": 1},
    {"revoke": 1, "revoke_row_id": 1, "approval_id": None},
])
def test_schema_rejects_cross_column_mismatches(tmp_path, changes):
    conn = _conn(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_direct(conn, **changes)


def test_schema_rejects_invalid_update(tmp_path):
    conn = _conn(tmp_path)
    op_id = _insert_direct(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE plugin_deploy_ops SET to_target='bad' WHERE op_id=?", (op_id,))


def test_open_unique_and_predicate_are_aligned(tmp_path):
    conn = _conn(tmp_path)
    _insert_direct(conn, **_rollback_changes())
    with pytest.raises(sqlite3.IntegrityError):
        _insert_direct(conn)
    conn.execute("UPDATE plugin_deploy_ops SET phase='needs_retry', fail_reason='syscall_failed' WHERE name='alpha'")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_direct(conn)
    conn.execute("UPDATE plugin_deploy_ops SET phase='needs_retry', fail_reason='verify_failed' WHERE name='alpha'")
    _insert_direct(conn)
    conn.execute("UPDATE plugin_deploy_ops SET phase='done', fail_reason=NULL WHERE op_id=2")
    _insert_direct(conn)
    conn.execute("UPDATE plugin_deploy_ops SET phase='foreign', fail_reason=NULL WHERE op_id=3")
    _insert_direct(conn)

    for phase in ("intent", "done", "needs_retry", "foreign"):
        for fail_reason in (None, "syscall_failed", "verify_failed"):
            row = {"phase": phase, "fail_reason": fail_reason}
            expected = phase == "intent" or (
                phase == "needs_retry" and fail_reason == "syscall_failed")
            assert deploy_ops.is_open(row) is expected
            assert deploy_ops.is_closed(row) is not expected


def test_insert_and_set_phase_keep_updated_at_after_all_name_events(tmp_path):
    conn = _conn(tmp_path)
    switch_journal.insert(
        conn, kind="approve", approval_id=1, name="alpha", old_kind="absent",
        old_target=None, temp_path="tmp", new_target=".versions/alpha/old",
        switch_required=True, actor="human_cli", now=NOW + timedelta(seconds=10))
    op_id = _insert_intent(conn)
    row = deploy_ops.get_open_by_name(conn, "alpha")
    assert row["op_id"] == op_id
    assert datetime.fromisoformat(row["updated_at"]) > NOW + timedelta(seconds=10)
    deploy_ops.set_phase(conn, op_id, "done", now=NOW, fail_reason=None)
    assert deploy_ops.get_open_by_name(conn, "alpha") is None
    assert deploy_ops.list_by_name(conn, "alpha")[0]["phase"] == "done"


def test_insert_done_revoke_satisfies_revoke_check_constraints(tmp_path):
    conn = _conn(tmp_path)

    op_id = deploy_ops.insert_done_revoke(
        conn, name="alpha", subject_content_hash="content-old",
        subject_artifact_hash="artifact-old", approval_id=7, revoke_row_id=8,
        actor="human_cli", now=NOW)

    row = conn.execute("SELECT * FROM plugin_deploy_ops WHERE op_id=?", (op_id,)).fetchone()
    assert row["op_kind"] == "revoke"
    assert row["from_kind"] == row["to_kind"] == "none"
    assert row["phase"] == "done"
    assert row["revoke"] == 1
    assert row["force"] == row["notify_attempted"] == 0


def test_insert_done_revoke_updated_at_is_monotonic_for_name(tmp_path):
    conn = _conn(tmp_path)
    first = deploy_ops.insert_done_revoke(
        conn, name="alpha", subject_content_hash="content-old",
        subject_artifact_hash="artifact-old", approval_id=7, revoke_row_id=8,
        actor="human_cli", now=NOW)
    second = deploy_ops.insert_done_revoke(
        conn, name="alpha", subject_content_hash="content-new",
        subject_artifact_hash="artifact-new", approval_id=9, revoke_row_id=10,
        actor="human_cli", now=NOW)

    rows = conn.execute(
        "SELECT updated_at FROM plugin_deploy_ops WHERE op_id IN (?,?) ORDER BY op_id",
        (first, second)).fetchall()
    assert datetime.fromisoformat(rows[1]["updated_at"]) > datetime.fromisoformat(rows[0]["updated_at"])


def test_live_history_merges_sources_and_normalizes_event_time(tmp_path):
    conn = _conn(tmp_path)
    switch_journal.insert(conn, kind="approve", approval_id=1, name="alpha",
                          old_kind="absent", old_target=None, temp_path="tmp1",
                          new_target=".versions/alpha/a", switch_required=True,
                          actor="human_cli", now=NOW + timedelta(seconds=30))
    switch_journal.set_phase(conn, 1, "decided", now=NOW + timedelta(seconds=30))
    _insert_direct(conn, phase="done", updated_at=(NOW + timedelta(seconds=20)).isoformat())
    _insert_direct(conn, op_kind="rollback_symlink", name="alpha", from_kind="absent",
                   to_kind="symlink", from_target=None, to_target=".versions/alpha/b",
                   subject_content_hash=None, subject_artifact_hash=None,
                   target_content_hash="content-b", target_artifact_hash="artifact-b",
                   approval_id=None, phase="done", updated_at=(NOW + timedelta(seconds=10)).isoformat())
    history = deploy_ops.plugin_live_history(conn, "alpha")
    assert [event["to"] for event in history] == [".versions/alpha/b", "absent", ".versions/alpha/a"]
    assert [event["effective_at"] for event in history] == sorted(event["effective_at"] for event in history)


def test_live_history_verify_failure_is_absent_and_ignores_non_events(tmp_path):
    conn = _conn(tmp_path)
    _insert_direct(conn, op_kind="rollback_symlink", from_kind="symlink",
                   to_kind="symlink", to_target=".versions/alpha/b",
                   target_content_hash="content-b", target_artifact_hash="artifact-b",
                   phase="needs_retry", fail_reason="verify_failed")
    _insert_direct(conn, name="other", phase="needs_retry", fail_reason="syscall_failed")
    revoke_values = _disable_values(op_kind="revoke", name="alpha", from_kind="none",
                                    to_kind="none", from_target=None,
                                    subject_content_hash="content-r", subject_artifact_hash="artifact-r",
                                    revoke=1, revoke_row_id=1, force=0, phase="done")
    _insert_direct(conn, **revoke_values)
    events = deploy_ops.plugin_live_history(conn, "alpha")
    assert len(events) == 1
    assert events[0]["to"] == "absent"


def test_append_revocation_is_ordered_idempotent_and_has_only_audit_payload(tmp_path):
    conn = _conn(tmp_path)
    approval_id = approvals.create(
        conn, "plugin", {"name": "alpha", "content_hash": "content-a", "artifact_hash": "artifact-a", "extra": 1}, NOW)
    approvals.apply_decision(conn, approval_id, "approved", decided_by="human", now=NOW)
    result = approvals.append_revocation(conn, "alpha", "content-a", now=NOW)
    assert result is not None
    row = conn.execute("SELECT * FROM approval_requests WHERE id=?", (result,)).fetchone()
    assert set(json.loads(row["payload_json"])) == {"name", "content_hash", "artifact_hash", "revokes_approval_id"}
    assert row["reason"] == "revoked_by_operator"
    assert datetime.fromisoformat(row["decided_at"]) > NOW
    assert approvals.append_revocation(conn, "alpha", "content-a", now=NOW) is None
    with pytest.raises(ValueError, match="approved"):
        approvals.append_revocation(conn, "missing", "content", now=NOW)


def test_linked_signals_are_excluded_and_pending_requires_freshness(tmp_path):
    conn = _conn(tmp_path)
    pending = signals.add(conn, plugin="alpha", content_hash="a", pair="USDJPY", timeframe="1h", bar_ts=NOW.isoformat(), kind="signal", payload={}, now=NOW)
    claimed = signals.add(conn, plugin="alpha", content_hash="b", pair="USDJPY", timeframe="1h", bar_ts=(NOW + timedelta(minutes=1)).isoformat(), kind="signal", payload={}, now=NOW)
    stale = signals.add(conn, plugin="alpha", content_hash="c", pair="USDJPY", timeframe="1h", bar_ts=(NOW - timedelta(hours=3)).isoformat(), kind="signal", payload={}, now=NOW)
    mission_id = missions.start(conn, "trade", "local", "model", NOW)
    second_mission_id = missions.start(conn, "trade", "local", "model", NOW)
    claim = signals.claim_oldest(conn, mission_id=mission_id, now=NOW, freshness_bars=1)
    assert claim["id"] == pending
    op_id = _insert_direct(conn, phase="done")
    deploy_ops.link_signals(conn, op_id, [(pending, "pending"), (claimed, "claimed")])
    assert signals.claim_oldest(conn, mission_id=second_mission_id, now=NOW, freshness_bars=1) is None
    assert signals.pending_exists(conn, now=NOW, freshness_bars=1) is False
    assert {row["id"] for row in signals.recent(conn, "USDJPY", since=NOW - timedelta(days=1))} == {pending, claimed, stale}
    assert {row["id"] for row in signals.recent(conn, "USDJPY", since=NOW - timedelta(days=1), exclude_deploy_linked=True)} == {stale}


def test_abandon_pending_for_identities_links_pending_and_claimed(tmp_path):
    conn = _conn(tmp_path)
    pending = signals.add(conn, plugin="alpha", content_hash="a", pair="USDJPY", timeframe="1h", bar_ts=NOW.isoformat(), kind="signal", payload={}, now=NOW)
    claimed = signals.add(conn, plugin="alpha", content_hash="a", pair="USDJPY", timeframe="1h", bar_ts=(NOW + timedelta(minutes=1)).isoformat(), kind="signal", payload={}, now=NOW)
    conn.execute("UPDATE signals SET status='claimed' WHERE id=?", (claimed,))
    op_id = _insert_direct(conn, phase="done")
    signals.abandon_pending_for_identities(conn, [("alpha", "a")], op_id=op_id, now=NOW)
    statuses = {row["id"]: row["status"] for row in conn.execute("SELECT id, status FROM signals")}
    assert statuses == {pending: "abandoned", claimed: "claimed"}
    assert deploy_ops.linked_signal_ids(conn, op_id) == {pending, claimed}


def _retire_values(**changes):
    values = {
        "op_kind": "retire_plain", "from_kind": "plain", "to_kind": "retired",
        "from_target": None, "retired_path": "_retired/alpha",
    }
    values.update(changes)
    return values


def _revoke_values(**changes):
    values = {
        "op_kind": "revoke", "from_kind": "none", "to_kind": "none",
        "from_target": None, "revoke": 1, "revoke_row_id": 1, "force": 0,
        "phase": "done",
    }
    values.update(changes)
    return values


def _rollback_absent_values(**changes):
    return _rollback_changes(
        from_kind="absent", from_target=None, subject_content_hash=None,
        subject_artifact_hash=None, **changes)


_BASES = {
    "disable": {},
    "rollback_symlink": _rollback_changes(),
    "rollback_absent": _rollback_absent_values(),
    "retire": _retire_values(),
    "revoke": _revoke_values(),
}


@pytest.mark.parametrize("base", sorted(_BASES))
def test_schema_accepts_each_valid_base_row(tmp_path, base):
    conn = _conn(tmp_path)
    _insert_direct(conn, **_BASES[base])


@pytest.mark.parametrize("base,change", [
    ("disable", {"from_kind": "plain"}),
    ("disable", {"from_kind": "absent"}),
    ("disable", {"to_kind": "symlink"}),
    ("disable", {"from_target": None}),
    ("disable", {"to_target": "x"}),
    ("disable", {"retired_path": "x"}),
    ("disable", {"subject_content_hash": None}),
    ("disable", {"subject_artifact_hash": None}),
    ("disable", {"target_content_hash": "x"}),
    ("disable", {"target_artifact_hash": "x"}),
    ("rollback_symlink", {"to_kind": "absent"}),
    ("rollback_symlink", {"to_target": None}),
    ("rollback_symlink", {"retired_path": "x"}),
    ("rollback_symlink", {"target_content_hash": None}),
    ("rollback_symlink", {"target_artifact_hash": None}),
    ("rollback_symlink", {"from_kind": "plain"}),
    ("rollback_symlink", {"from_target": None}),
    ("rollback_symlink", {"subject_content_hash": None}),
    ("rollback_symlink", {"subject_artifact_hash": None}),
    ("rollback_absent", {"from_target": "x"}),
    ("rollback_absent", {"subject_content_hash": "x"}),
    ("rollback_absent", {"subject_artifact_hash": "x"}),
    ("rollback_absent", {"revoke": 1, "revoke_row_id": 1}),
    ("rollback_absent", {"retired_path": "x"}),
    ("retire", {"from_kind": "symlink"}),
    ("retire", {"to_kind": "absent"}),
    ("retire", {"retired_path": None}),
    ("retire", {"from_target": "x"}),
    ("retire", {"to_target": "x"}),
    ("retire", {"target_content_hash": "x"}),
    ("retire", {"target_artifact_hash": "x"}),
    ("retire", {"revoke": 1, "revoke_row_id": 1}),
    ("revoke", {"from_kind": "symlink"}),
    ("revoke", {"to_kind": "absent"}),
    ("revoke", {"from_target": "x"}),
    ("revoke", {"to_target": "x"}),
    ("revoke", {"retired_path": "x"}),
    ("revoke", {"subject_content_hash": None}),
    ("revoke", {"subject_artifact_hash": None}),
    ("revoke", {"target_content_hash": "x"}),
    ("revoke", {"target_artifact_hash": "x"}),
    ("revoke", {"approval_id": None}),
    ("revoke", {"revoke": 0, "revoke_row_id": None}),
    ("revoke", {"revoke_row_id": None}),
    ("revoke", {"force": 1}),
    ("revoke", {"phase": "intent"}),
    ("disable", {"revoke": 0, "revoke_row_id": 1}),
    ("disable", {"revoke": 1, "revoke_row_id": None}),
    ("disable", {"revoke": 1, "revoke_row_id": 1, "approval_id": None}),
    ("disable", {"phase": "needs_retry", "fail_reason": None}),
    ("disable", {"phase": "done", "fail_reason": "syscall_failed"}),
    ("disable", {"phase": "intent", "fail_reason": "verify_failed"}),
    ("disable", {"phase": "needs_retry", "fail_reason": "verify_failed"}),
    ("revoke", {"phase": "needs_retry", "fail_reason": "syscall_failed"}),
    ("disable", {"notify_attempted": 1}),
    ("disable", {"op_kind": "x"}),
    ("disable", {"phase": "x"}),
    ("disable", {"fail_reason": "x", "phase": "needs_retry"}),
    ("disable", {"revoke": 2}),
    ("disable", {"force": 2}),
    ("disable", {"notify_attempted": 2}),
    ("disable", {"from_kind": "x"}),
    ("disable", {"to_kind": "x"}),
    ("disable", {"op_kind": None}),
    ("disable", {"name": None}),
    ("disable", {"from_kind": None}),
    ("disable", {"to_kind": None}),
    ("disable", {"revoke": None}),
    ("disable", {"force": None}),
    ("disable", {"phase": None}),
    ("disable", {"notify_attempted": None}),
    ("disable", {"actor": None}),
    ("disable", {"created_at": None}),
    ("disable", {"updated_at": None}),
])
def test_schema_rejects_single_column_violation_from_valid_base(tmp_path, base, change):
    conn = _conn(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_direct(conn, **{**_BASES[base], **change})


def test_schema_rejects_returning_to_intent_with_notify_attempted(tmp_path):
    conn = _conn(tmp_path)
    op_id = _insert_direct(
        conn, phase="needs_retry", fail_reason="syscall_failed", notify_attempted=1)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE plugin_deploy_ops SET phase='intent', fail_reason=NULL WHERE op_id=?",
            (op_id,))


def test_deploy_op_signals_enforce_foreign_keys(tmp_path):
    conn = _conn(tmp_path)
    signal_id = signals.add(
        conn, plugin="alpha", content_hash="a", pair="USDJPY", timeframe="1h",
        bar_ts=NOW.isoformat(), kind="signal", payload={}, now=NOW)
    op_id = _insert_direct(conn, phase="done")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO plugin_deploy_op_signals (op_id,signal_id,status_at_link) "
            "VALUES (999,?, 'pending')", (signal_id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO plugin_deploy_op_signals (op_id,signal_id,status_at_link) "
            "VALUES (?,999,'pending')", (op_id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO plugin_deploy_op_signals (op_id,signal_id,status_at_link) "
            "VALUES (999,999,'pending')")
    conn.execute(
        "INSERT INTO plugin_deploy_op_signals (op_id,signal_id,status_at_link) "
        "VALUES (?,?,'pending')", (op_id, signal_id))


def _approved_identity(conn):
    approval_id = approvals.create(
        conn, "plugin", {"name": "alpha", "content_hash": "content-a",
                         "artifact_hash": "artifact-a"}, NOW)
    approvals.apply_decision(conn, approval_id, "approved", decided_by="human", now=NOW)
    return approval_id


def _count_approvals(conn):
    return conn.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0]


@pytest.mark.parametrize("closing_status", ["invalidated", "expired"])
def test_append_revocation_ignores_statuses_the_loader_does_not_fold(tmp_path, closing_status):
    conn = _conn(tmp_path)
    approval_id = _approved_identity(conn)
    revoked = approvals.append_revocation(
        conn, "alpha", "content-a", now=NOW + timedelta(seconds=1))
    assert revoked is not None
    later = approvals.create(
        conn, "plugin", {"name": "alpha", "content_hash": "content-a",
                         "artifact_hash": "artifact-a"}, NOW + timedelta(seconds=2))
    conn.execute(
        "UPDATE approval_requests SET status=?, decided_at=?, decided_by='system' "
        "WHERE id=?", (closing_status, (NOW + timedelta(seconds=5)).isoformat(), later))
    conn.commit()
    before = _count_approvals(conn)
    assert approvals.append_revocation(
        conn, "alpha", "content-a", now=NOW + timedelta(seconds=10)) is None
    assert _count_approvals(conn) == before
    assert approval_id != revoked


def test_append_revocation_after_invalidated_only_identity_has_nothing_to_revoke(tmp_path):
    conn = _conn(tmp_path)
    approval_id = approvals.create(
        conn, "plugin", {"name": "alpha", "content_hash": "content-a",
                         "artifact_hash": "artifact-a"}, NOW)
    conn.execute(
        "UPDATE approval_requests SET status='invalidated', decided_at=?, "
        "decided_by='system' WHERE id=?", (NOW.isoformat(), approval_id))
    conn.commit()
    with pytest.raises(ValueError, match="approved"):
        approvals.append_revocation(conn, "alpha", "content-a", now=NOW)


def _journal_decided(conn, name, target, at):
    op_id = switch_journal.insert(
        conn, kind="approve", approval_id=1, name=name, old_kind="absent",
        old_target=None, temp_path=f"tmp-{target}", new_target=target,
        switch_required=True, actor="human_cli", now=at)
    switch_journal.set_phase(conn, op_id, "decided", now=at)
    return op_id


def _ops_done(conn, at, **changes):
    return _insert_direct(
        conn, phase="done", created_at=at.isoformat(), updated_at=at.isoformat(), **changes)


def test_live_history_equal_instants_order_by_source_then_id_with_microsecond_fold(tmp_path):
    conn = _conn(tmp_path)
    journal_id = _journal_decided(conn, "alpha", ".versions/alpha/j", NOW)
    first = _ops_done(conn, NOW)
    second = _ops_done(conn, NOW, op_kind="rollback_symlink", to_kind="symlink",
                       to_target=".versions/alpha/r", target_content_hash="c",
                       target_artifact_hash="a")
    history = deploy_ops.plugin_live_history(conn, "alpha")
    assert [(e["source"], e["id"]) for e in history] == [
        ("plugin_deploy_ops", first), ("plugin_deploy_ops", second),
        ("plugin_switch_journal", journal_id)]
    assert [e["effective_at"] for e in history] == [
        NOW.isoformat(), (NOW + timedelta(microseconds=1)).isoformat(),
        (NOW + timedelta(microseconds=2)).isoformat()]
    assert [e["to"] for e in history] == ["absent", ".versions/alpha/r", ".versions/alpha/j"]
    assert [e["from"] for e in history][1:] == [".versions/alpha/old", "absent"]


def test_live_history_journal_clock_regression_is_ordered_by_instant_and_strictly_monotone(tmp_path):
    conn = _conn(tmp_path)
    later_clock = _journal_decided(conn, "alpha", ".versions/alpha/one", NOW + timedelta(seconds=10))
    earlier_clock = _journal_decided(conn, "alpha", ".versions/alpha/two", NOW + timedelta(seconds=5))
    tie = _ops_done(conn, NOW + timedelta(seconds=5))
    third = _journal_decided(conn, "alpha", ".versions/alpha/three", NOW + timedelta(seconds=10))
    assert later_clock < earlier_clock < third
    history = deploy_ops.plugin_live_history(conn, "alpha")
    assert [(e["source"], e["id"]) for e in history] == [
        ("plugin_deploy_ops", tie), ("plugin_switch_journal", earlier_clock),
        ("plugin_switch_journal", later_clock), ("plugin_switch_journal", third)]
    effective = [datetime.fromisoformat(e["effective_at"]) for e in history]
    assert all(a < b for a, b in zip(effective, effective[1:]))
    assert effective == [
        NOW + timedelta(seconds=5), NOW + timedelta(seconds=5, microseconds=1),
        NOW + timedelta(seconds=10), NOW + timedelta(seconds=10, microseconds=1)]
    assert [e["to"] for e in history] == [
        "absent", ".versions/alpha/two", ".versions/alpha/one", ".versions/alpha/three"]
    live_before_last = history[-2]["to"]
    assert live_before_last == ".versions/alpha/one"


def _ops_done_via_store(conn, at):
    values = {key: value for key, value in _disable_values().items()
              if key not in ("phase", "created_at", "updated_at", "fail_reason",
                             "notify_attempted", "actor")}
    op_id = deploy_ops.insert_intent(conn, actor="human_cli", now=at, **values)
    deploy_ops.set_phase(conn, op_id, "done", now=at)
    return op_id


def test_deploy_op_written_after_a_journal_clock_regression_still_sorts_after_the_journal(tmp_path):
    conn = _conn(tmp_path)
    journal_at = NOW + timedelta(seconds=30)
    journal_id = _journal_decided(conn, "alpha", ".versions/alpha/j", journal_at)
    first = _ops_done_via_store(conn, NOW)
    second = _ops_done_via_store(conn, NOW)

    assert datetime.fromisoformat(deploy_ops.get_by_id(conn, first)["updated_at"]) > journal_at
    history = deploy_ops.plugin_live_history(conn, "alpha")
    assert [(e["source"], e["id"]) for e in history] == [
        ("plugin_switch_journal", journal_id), ("plugin_deploy_ops", first),
        ("plugin_deploy_ops", second)]
    raw = [datetime.fromisoformat(e["at"]) for e in history]
    assert raw == sorted(raw) and len(set(raw)) == 3
    effective = [datetime.fromisoformat(e["effective_at"]) for e in history]
    assert all(a < b for a, b in zip(effective, effective[1:]))


def test_set_phase_keeps_notify_attempted_unless_returning_to_intent(tmp_path):
    conn = _conn(tmp_path)
    op_id = _insert_direct(
        conn, phase="needs_retry", fail_reason="syscall_failed")
    assert deploy_ops.mark_notify_attempted(conn, op_id)
    deploy_ops.set_phase(conn, op_id, "done", now=NOW)
    assert deploy_ops.list_by_name(conn, "alpha")[0]["notify_attempted"] == 1

    retry_id = _insert_direct(
        conn, name="beta", phase="needs_retry", fail_reason="syscall_failed",
        notify_attempted=1)
    deploy_ops.set_phase(conn, retry_id, "intent", now=NOW)
    row = deploy_ops.get_open_by_name(conn, "beta")
    assert row["phase"] == "intent"
    assert row["notify_attempted"] == 0


def _decided_plugin_approval(conn, status, decided_at, created_at=NOW):
    approval_id = approvals.create(
        conn, "plugin", {"name": "alpha", "content_hash": "content-a",
                         "artifact_hash": "artifact-a"}, created_at)
    conn.execute(
        "UPDATE approval_requests SET status=?, decided_at=?, decided_by='human' "
        "WHERE id=?", (status, decided_at, approval_id))
    conn.commit()
    return approval_id


def test_append_revocation_compares_decided_at_as_instants(tmp_path):
    conn = _conn(tmp_path)
    approved = _decided_plugin_approval(conn, "approved", "2026-10-16T12:00:01+00:00")
    _decided_plugin_approval(conn, "rejected", "2026-10-16T21:00:00+09:00")
    revoked = approvals.append_revocation(
        conn, "alpha", "content-a", now=NOW + timedelta(seconds=10))
    assert revoked is not None
    payload = json.loads(conn.execute(
        "SELECT payload_json FROM approval_requests WHERE id=?", (revoked,)
    ).fetchone()[0])
    assert payload["revokes_approval_id"] == approved


def test_append_revocation_orders_rows_with_and_without_microseconds(tmp_path):
    conn = _conn(tmp_path)
    _decided_plugin_approval(conn, "approved", "2026-10-16T12:00:00+00:00")
    _decided_plugin_approval(conn, "rejected", "2026-10-16T12:00:00.000001+00:00")
    assert approvals.append_revocation(conn, "alpha", "content-a", now=NOW) is None


def test_append_revocation_rejects_naive_decided_at(tmp_path):
    conn = _conn(tmp_path)
    _decided_plugin_approval(conn, "approved", "2026-10-16T12:00:00")
    with pytest.raises(ValueError, match="naive"):
        approvals.append_revocation(conn, "alpha", "content-a", now=NOW)


def _linked_claimed(conn, mission_id, *, link):
    signal_id = signals.add(
        conn, plugin="alpha", content_hash="a", pair="USDJPY", timeframe="1h",
        bar_ts=(NOW + timedelta(minutes=1 if link else 2)).isoformat(),
        kind="signal", payload={}, now=NOW)
    conn.execute(
        "UPDATE signals SET status='claimed', claimed_by_mission_id=?, claimed_at=? "
        "WHERE id=?", (mission_id, NOW.isoformat(), signal_id))
    if link:
        deploy_ops.link_signals(
            conn, _insert_direct(conn, phase="done"), [(signal_id, "claimed")],
            commit=False)
    conn.commit()
    return signal_id


def _status(conn, signal_id):
    return conn.execute("SELECT status FROM signals WHERE id=?", (signal_id,)).fetchone()[0]


def test_requeue_abandons_linked_claimed_signal_and_keeps_unlinked_pending(tmp_path):
    conn = _conn(tmp_path)
    mission_id = missions.start(conn, "trade", "local", "model", NOW)
    linked = _linked_claimed(conn, mission_id, link=True)
    assert signals.requeue(conn, linked, now=NOW, max_requeue=3) == "abandoned"
    assert _status(conn, linked) == "abandoned"
    plain = _linked_claimed(conn, mission_id, link=False)
    assert signals.requeue(conn, plain, now=NOW, max_requeue=3) == "pending"
    assert _status(conn, plain) == "pending"


def test_reclaim_expired_abandons_linked_claimed_signal_and_keeps_unlinked_pending(tmp_path):
    conn = _conn(tmp_path)
    mission_id = missions.start(conn, "trade", "local", "model", NOW)
    linked = _linked_claimed(conn, mission_id, link=True)
    plain = _linked_claimed(conn, mission_id, link=False)
    statuses = signals.reclaim_expired(
        conn, now=NOW + timedelta(hours=1), lease_min=10, max_requeue=3)
    assert sorted(statuses) == ["abandoned", "pending"]
    assert _status(conn, linked) == "abandoned"
    assert _status(conn, plain) == "pending"


def test_startup_recovery_abandons_a_linked_signal_so_it_cannot_be_claimed(tmp_path):
    """A deploy-linked claim of an interrupted mission is terminalized, never hidden as pending."""
    conn = _conn(tmp_path)
    mission_id = missions.start(conn, "trade", "local", "model", NOW)
    linked = _linked_claimed(conn, mission_id, link=True)

    result = missions.recover_interrupted(conn, now=NOW, max_requeue=3)

    assert (result["signals_requeued"], result["signals_abandoned"]) == (0, 1)
    assert _status(conn, linked) == "abandoned"
    next_mission = missions.start(conn, "trade", "local", "model", NOW)
    assert signals.claim_oldest(
        conn, mission_id=next_mission, now=NOW, freshness_bars=2) is None


def test_lease_recovery_linked_signal_cannot_be_claimed_again(tmp_path):
    """Lease expiry terminalizes a deploy-linked claim before another mission sees it."""
    conn = _conn(tmp_path)
    mission_id = missions.start(conn, "trade", "local", "model", NOW)
    linked = _linked_claimed(conn, mission_id, link=True)

    statuses = signals.reclaim_expired(
        conn, now=NOW + timedelta(hours=1), lease_min=10, max_requeue=3)

    assert statuses == ["abandoned"]
    assert _status(conn, linked) == "abandoned"
    next_mission = missions.start(conn, "trade", "local", "model", NOW)
    assert signals.claim_oldest(
        conn, mission_id=next_mission, now=NOW, freshness_bars=2) is None


def test_expire_stale_abandons_linked_pending_signal(tmp_path):
    conn = _conn(tmp_path)
    stale = signals.add(
        conn, plugin="alpha", content_hash="a", pair="USDJPY", timeframe="1h",
        bar_ts=(NOW - timedelta(hours=5)).isoformat(), kind="signal", payload={}, now=NOW)
    deploy_ops.link_signals(conn, _insert_direct(conn, phase="done"), [(stale, "pending")])
    result = signals.expire_stale(conn, now=NOW, freshness_bars=1)
    assert result.total == 1
    assert _status(conn, stale) == "abandoned"


def test_pending_exists_is_false_when_only_stale_pending_remains(tmp_path):
    conn = _conn(tmp_path)
    signals.add(
        conn, plugin="alpha", content_hash="a", pair="USDJPY", timeframe="1h",
        bar_ts=(NOW - timedelta(hours=5)).isoformat(), kind="signal", payload={}, now=NOW)
    assert signals.pending_exists(conn, now=NOW, freshness_bars=1) is False


_LI_PLUGIN = "def compute(df, params):\n    return {'v': 1.0}\n"
_LI_CONFIG = "kind: indicator\n"
_LI_TEST = "def test_x():\n    pass\n"


def _li_write(d, *, files=("plugin.py", "config.yaml", "test_plugin.py")):
    d.mkdir(parents=True, exist_ok=True)
    contents = {"plugin.py": _LI_PLUGIN, "config.yaml": _LI_CONFIG,
                "test_plugin.py": _LI_TEST}
    for f in files:
        (d / f).write_text(contents[f])
    return d


def _li_expected(d):
    from agentic_fx.plugin import loader
    return (loader.content_hash(d), loader.artifact_hash_bytes(
        (d / "plugin.py").read_bytes(), (d / "config.yaml").read_bytes(),
        (d / "test_plugin.py").read_bytes()))


def test_live_identity_absent(tmp_path):
    assert deploy_ops.live_identity(tmp_path, "alpha") is None


def test_live_identity_symlink_uses_real_hashes_not_dir_name(tmp_path):
    vdir = _li_write(tmp_path / ".versions" / "alpha" / ("a" * 64))
    (tmp_path / "alpha").symlink_to(f".versions/alpha/{'a' * 64}")
    content, artifact = _li_expected(vdir)
    assert artifact != "a" * 64
    got = deploy_ops.live_identity(tmp_path, "alpha")
    assert got == deploy_ops.LiveIdentity("symlink", content, artifact)


def test_live_identity_dangling_symlink(tmp_path):
    (tmp_path / "alpha").symlink_to(f".versions/alpha/{'b' * 64}")
    assert deploy_ops.live_identity(tmp_path, "alpha") == \
        deploy_ops.LiveIdentity("symlink", None, None)


@pytest.mark.parametrize("target", ["../x", "/etc", f".versions/beta/{'c' * 64}"])
def test_live_identity_noncanonical_symlink_target(tmp_path, target):
    (tmp_path / "x").mkdir()
    _li_write(tmp_path / ".versions" / "beta" / ("c" * 64))
    (tmp_path / "alpha").symlink_to(target)
    assert deploy_ops.live_identity(tmp_path, "alpha") == \
        deploy_ops.LiveIdentity("symlink", None, None)


def test_live_identity_plain_dir(tmp_path):
    d = _li_write(tmp_path / "alpha")
    got = deploy_ops.live_identity(tmp_path, "alpha")
    assert got == deploy_ops.LiveIdentity("plain", _li_expected(d)[0], None)


def test_live_identity_plain_missing_required_file(tmp_path):
    _li_write(tmp_path / "alpha", files=("plugin.py", "config.yaml"))
    assert deploy_ops.live_identity(tmp_path, "alpha") == \
        deploy_ops.LiveIdentity("plain", None, None)


def test_live_identity_regular_file(tmp_path):
    (tmp_path / "alpha").write_text("x")
    assert deploy_ops.live_identity(tmp_path, "alpha") is None


@pytest.mark.parametrize("name", [
    "a/b", "..", "../alpha", "Alpha", "_alpha", "a" * 65, "", ".", "a\\b"])
def test_live_identity_rejects_noncanonical_name(tmp_path, name):
    with pytest.raises(ValueError):
        deploy_ops.live_identity(tmp_path, name)


def test_live_identity_intermediate_symlink_escape(tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()
    outside = tmp_path / "outside"
    _li_write(outside / "alpha" / ("a" * 64))
    (root / ".versions").symlink_to(outside)
    (root / "alpha").symlink_to(f".versions/alpha/{'a' * 64}")
    assert deploy_ops.live_identity(root, "alpha") == \
        deploy_ops.LiveIdentity("symlink", None, None)


def test_live_identity_inner_versions_symlink_allowed(tmp_path):
    root = tmp_path / "plugins"
    real = root / "real_versions"
    vdir = _li_write(real / "alpha" / ("a" * 64))
    (root / ".versions").symlink_to(real)
    (root / "alpha").symlink_to(f".versions/alpha/{'a' * 64}")
    content, artifact = _li_expected(vdir)
    assert deploy_ops.live_identity(root, "alpha") == \
        deploy_ops.LiveIdentity("symlink", content, artifact)


def test_get_by_id_returns_the_row_or_none(tmp_path):
    conn = _conn(tmp_path)
    op_id = _insert_intent(conn)

    assert deploy_ops.get_by_id(conn, op_id)["op_id"] == op_id
    assert deploy_ops.get_by_id(conn, op_id + 100) is None


@pytest.mark.parametrize("directory", ["fx#1", "a?b", "p%41"])
def test_a_database_under_a_path_with_uri_special_characters_opens_read_only(tmp_path, directory):
    base = tmp_path / directory
    base.mkdir()
    db_path = base / "agentic.db"
    conn = connect(db_path)
    init_db(conn)
    conn.close()

    ro = deploy_ops.open_readonly(db_path)

    assert ro is not None
    ro.close()
    assert deploy_ops.count_open_readonly(db_path) == 0
