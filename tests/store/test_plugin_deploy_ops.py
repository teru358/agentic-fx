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
