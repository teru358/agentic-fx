"""Shared deploy-operation contracts."""
from __future__ import annotations

import contextlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.plugin import deploy_ops
from agentic_fx.plugin import switch
from agentic_fx.config import load_settings
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import db as db_store
from agentic_fx.store import plugin_deploy_ops as deploy_store
from agentic_fx.store import signals as signals_store


NOW = datetime(2026, 10, 16, 3, 0, tzinfo=timezone.utc)


def _intent(conn, *, name="sma", subject_content_hash="content-a",
            subject_artifact_hash="artifact-a", to_target=None, revoke=False,
            force=False, op_kind="disable_symlink"):
    return deploy_store.insert_intent(
        conn, op_kind=op_kind, name=name, from_kind="symlink", to_kind="absent",
        from_target=".versions/sma/artifact-a", to_target=to_target,
        retired_path=None, subject_content_hash=subject_content_hash,
        subject_artifact_hash=subject_artifact_hash, target_content_hash=None,
        target_artifact_hash=None, approval_id=None, revoke=revoke,
        revoke_row_id=None, force=force, actor="human_cli", now=NOW)


@pytest.fixture
def conn(tmp_path):
    connection = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(connection)
    return connection


@pytest.mark.parametrize(
    "requested",
    [
        {"op_kind": "rollback_symlink"},
        {"name": "ema"},
        {"subject_content_hash": "content-b"},
        {"subject_artifact_hash": "artifact-b"},
        {"to_target": ".versions/sma/other"},
        {"revoke": True},
        {"force": True},
    ],
)
def test_match_or_reject_rejects_each_immutable_signature_difference(conn, requested):
    """A different immutable operation must not inherit an open intent."""
    op_id = _intent(conn)
    row = deploy_store.get_open_by_name(conn, "sma")
    values = {
        "op_kind": "disable_symlink", "name": "sma",
        "subject_content_hash": "content-a", "subject_artifact_hash": "artifact-a",
        "to_target": None, "revoke": False, "force": False,
    }
    values.update(requested)

    with pytest.raises(deploy_ops.SignatureMismatchError, match=rf"op_id={op_id}"):
        deploy_ops.match_or_reject(
            row, deploy_ops.Signature(**values),
            deploy_store.LiveIdentity("symlink", "content-a", "artifact-a"),
        )


def test_match_or_reject_resets_syscall_failed_when_live_is_from(conn):
    """Retrying the same intent reopens it and clears its notification claim."""
    op_id = _intent(conn)
    deploy_store.set_phase(conn, op_id, "needs_retry", now=NOW,
                           fail_reason="syscall_failed", notify_attempted=True)
    row = deploy_store.get_open_by_name(conn, "sma")
    requested = deploy_ops.row_signature(row)

    assert deploy_ops.match_or_reject(
        row, requested,
        deploy_store.LiveIdentity("symlink", "content-a", "artifact-a"),
        conn=conn, now=NOW,
    ) == op_id
    reset = deploy_store.get_open_by_name(conn, "sma")
    assert reset["phase"] == "intent"
    assert reset["notify_attempted"] == 0


def test_match_or_reject_rejects_matching_signature_when_live_is_neither_end(conn):
    """An unknown live identity is fail-closed even for the same request."""
    _intent(conn)
    row = deploy_store.get_open_by_name(conn, "sma")

    with pytest.raises(deploy_ops.SignatureMismatchError, match="live identity"):
        deploy_ops.match_or_reject(
            row, deploy_ops.row_signature(row),
            deploy_store.LiveIdentity("symlink", "foreign", "foreign"),
        )


def test_acquire_op_locks_releases_and_retries_when_dependents_expand(tmp_path, monkeypatch):
    """Each growing snapshot releases its flocks and the third stable one is kept."""
    from agentic_fx.plugin import switch

    real_locks = switch._plugin_locks
    events: list[tuple[str, tuple[str, ...]]] = []

    @contextlib.contextmanager
    def spy_locks(root, names, **kwargs):
        ordered = tuple(sorted(set(names)))
        with real_locks(root, names, **kwargs):
            events.append(("acquire", ordered))
            try:
                yield
            finally:
                events.append(("release", ordered))

    values = iter(({"a"}, {"a", "b"}, {"a", "b"}, {"a", "b", "c"},
                   {"a", "b", "c"}, {"a", "b", "c"}))
    monkeypatch.setattr(switch, "_plugin_locks", spy_locks)
    plugins_root = tmp_path / "plugins"
    plugins_root.mkdir()
    with deploy_ops.acquire_op_locks(
            plugins_root, "s", enumerate_dependents=lambda: next(values)) as locked:
        assert locked == {"s", "a", "b", "c"}
        assert events[-1] == ("acquire", ("a", "b", "c", "s"))
    assert events == [
        ("acquire", ("a", "s")), ("release", ("a", "s")),
        ("acquire", ("a", "b", "s")), ("release", ("a", "b", "s")),
        ("acquire", ("a", "b", "c", "s")), ("release", ("a", "b", "c", "s")),
    ]


def test_acquire_op_locks_refuses_after_three_expanding_snapshots(tmp_path, monkeypatch):
    """Continuously changing dependents never run without a stable lock set."""
    from agentic_fx.plugin import switch

    @contextlib.contextmanager
    def fake_locks(root, names, **kwargs):
        yield

    snapshots = iter((set(), {"a"}, {"a"}, {"b"}, {"b"}, {"c"}))
    monkeypatch.setattr(switch, "_plugin_locks", fake_locks)
    with pytest.raises(deploy_ops.DependentsChangedError):
        with deploy_ops.acquire_op_locks(
                tmp_path / "plugins", "s", enumerate_dependents=lambda: next(snapshots)):
            pass


def test_check_can_start_op_rejects_open_journal_for_a_dependent(conn):
    """An indicator operation cannot start while a dependent switch is unfinished."""
    from agentic_fx.store import plugin_switch_journal as journal_store

    journal_store.insert(
        conn, kind="approve", approval_id=1, name="strategy", old_kind="absent",
        old_target=None, new_target=".versions/strategy/a", switch_required=True,
        temp_path=".tmp/strategy", actor="human", now=NOW)
    with pytest.raises(deploy_ops.DeployOpBlockedError, match="strategy.*op_id=1"):
        deploy_ops.check_can_start_op(conn, "indicator", dependents={"strategy"})


def test_closed_verify_failed_deploy_op_does_not_block_a_switch_entry(conn):
    """Only syscall failures remain open; verify failures are intentionally closed."""
    op_id = deploy_store.insert_intent(
        conn, op_kind="rollback_symlink", name="indicator", from_kind="absent",
        to_kind="symlink", from_target=None,
        to_target=".versions/indicator/target-artifact", retired_path=None,
        subject_content_hash=None, subject_artifact_hash=None,
        target_content_hash="target-content", target_artifact_hash="target-artifact",
        approval_id=None, revoke=False, revoke_row_id=None, force=False,
        actor="human_cli", now=NOW)
    deploy_store.set_phase(conn, op_id, "needs_retry", now=NOW,
                           fail_reason="verify_failed")

    assert deploy_ops.check_can_start_op(conn, "indicator", dependents=set()) is None


def test_record_intent_commits_op_and_revocation_together(conn):
    """A revoking intent references the rejected approval in the same transaction."""
    from agentic_fx.store import approvals as approvals_store

    approval_id = approvals_store.create(
        conn, "plugin", {"name": "sma", "content_hash": "content-a",
                           "artifact_hash": "artifact-a"}, NOW)
    approvals_store.apply_decision(conn, approval_id, "approved", decided_by="h", now=NOW)
    signal_id = signals_store.add(
        conn, plugin="sma", content_hash="content-a", pair="USDJPY",
        timeframe="1h", bar_ts=NOW.isoformat(), kind="strategy",
        payload={"direction": "long"}, now=NOW)
    op_id = deploy_ops.record_intent(
        conn,
        intent_row={
            "op_kind": "disable_symlink", "name": "sma", "from_kind": "symlink",
            "to_kind": "absent", "from_target": ".versions/sma/artifact-a",
            "to_target": None, "retired_path": None,
            "subject_content_hash": "content-a", "subject_artifact_hash": "artifact-a",
            "target_content_hash": None, "target_artifact_hash": None,
            "approval_id": None, "force": False,
        },
        revoke=deploy_ops.RevokeSpec("sma", "content-a"),
        cascade_identities=[("sma", "content-a")],
        now=NOW, actor="human_cli")
    row = conn.execute("SELECT approval_id,revoke,revoke_row_id FROM plugin_deploy_ops WHERE op_id=?", (op_id,)).fetchone()
    assert (row["approval_id"], row["revoke"], row["revoke_row_id"]) == (approval_id, 1, approval_id + 1)
    assert conn.execute("SELECT status FROM approval_requests WHERE id=?", (approval_id + 1,)).fetchone()[0] == "rejected"
    assert conn.execute("SELECT status FROM signals WHERE id=?", (signal_id,)).fetchone()[0] == "abandoned"
    assert conn.execute("SELECT signal_id FROM plugin_deploy_op_signals WHERE op_id=?", (op_id,)).fetchone()[0] == signal_id


def test_record_intent_rolls_back_every_table_when_signal_linking_fails(conn, monkeypatch):
    """A failure while the signal store links an op leaves no partial intent."""
    signals_store.add(
        conn, plugin="sma", content_hash="content-a", pair="USDJPY",
        timeframe="1h", bar_ts=NOW.isoformat(), kind="strategy",
        payload={"direction": "long"}, now=NOW)

    def fail_link(*args, **kwargs):
        raise sqlite3.OperationalError("link failed")

    monkeypatch.setattr(deploy_ops.signals_store, "abandon_pending_for_identities", fail_link)
    with pytest.raises(sqlite3.OperationalError, match="link failed"):
        deploy_ops.record_intent(
            conn,
            intent_row={
                "op_kind": "disable_symlink", "name": "sma", "from_kind": "symlink",
                "to_kind": "absent", "from_target": ".versions/sma/artifact-a",
                "to_target": None, "retired_path": None,
                "subject_content_hash": "content-a", "subject_artifact_hash": "artifact-a",
                "target_content_hash": None, "target_artifact_hash": None,
                "approval_id": None, "force": False,
            },
            revoke=None, cascade_identities=[("sma", "content-a")],
            now=NOW, actor="human_cli")
    assert conn.execute("SELECT COUNT(*) FROM plugin_deploy_ops").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM plugin_deploy_op_signals").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM signals").fetchone()[0] == "pending"


def test_record_intent_rolls_back_when_after_insert_fails(conn):
    """A destination-update failure leaves neither intent nor signal effects."""
    signal_id = signals_store.add(
        conn, plugin="sma", content_hash="content-a", pair="USDJPY",
        timeframe="1h", bar_ts=NOW.isoformat(), kind="strategy",
        payload={"direction": "long"}, now=NOW)

    def fail_after_insert(op_id):
        raise RuntimeError(f"destination failed for {op_id}")

    with pytest.raises(RuntimeError, match="destination failed"):
        deploy_ops.record_intent(
            conn,
            intent_row={
                "op_kind": "retire_plain", "name": "sma", "from_kind": "plain",
                "to_kind": "retired", "from_target": None, "to_target": None,
                "retired_path": "_retired/.pending", "subject_content_hash": "content-a",
                "subject_artifact_hash": None, "target_content_hash": None,
                "target_artifact_hash": None, "approval_id": None, "force": False,
            },
            revoke=None, cascade_identities=[("sma", "content-a")], now=NOW,
            actor="human_cli", after_insert=fail_after_insert)

    assert conn.execute("SELECT COUNT(*) FROM plugin_deploy_ops").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM plugin_deploy_op_signals").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM signals WHERE id=?", (signal_id,)).fetchone()[0] == "pending"


def test_record_intent_with_revoke_rolls_back_every_row_when_signal_linking_fails(
        conn, monkeypatch):
    """A revoking intent leaves no rejected row, op row, link, or signal change."""
    approval_id = approvals_store.create(
        conn, "plugin", {"name": "sma", "content_hash": "content-a",
                           "artifact_hash": "artifact-a"}, NOW)
    approvals_store.apply_decision(conn, approval_id, "approved", decided_by="h", now=NOW)
    signals_store.add(
        conn, plugin="sma", content_hash="content-a", pair="USDJPY",
        timeframe="1h", bar_ts=NOW.isoformat(), kind="strategy",
        payload={"direction": "long"}, now=NOW)
    before_approvals = [tuple(r) for r in conn.execute(
        "SELECT id,status FROM approval_requests ORDER BY id")]

    def fail_link(*args, **kwargs):
        raise sqlite3.OperationalError("link failed")

    monkeypatch.setattr(deploy_ops.signals_store, "abandon_pending_for_identities", fail_link)
    with pytest.raises(sqlite3.OperationalError, match="link failed"):
        deploy_ops.record_intent(
            conn,
            intent_row={
                "op_kind": "disable_symlink", "name": "sma", "from_kind": "symlink",
                "to_kind": "absent", "from_target": ".versions/sma/artifact-a",
                "to_target": None, "retired_path": None,
                "subject_content_hash": "content-a", "subject_artifact_hash": "artifact-a",
                "target_content_hash": None, "target_artifact_hash": None,
                "approval_id": None, "force": False,
            },
            revoke=deploy_ops.RevokeSpec("sma", "content-a"),
            cascade_identities=[("sma", "content-a")],
            now=NOW, actor="human_cli")
    assert not conn.in_transaction
    assert [tuple(r) for r in conn.execute(
        "SELECT id,status FROM approval_requests ORDER BY id")] == before_approvals
    assert conn.execute("SELECT COUNT(*) FROM plugin_deploy_ops").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM plugin_deploy_op_signals").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM signals").fetchone()[0] == "pending"


class _Activity:
    def __init__(self, raises=False):
        self.calls = []
        self.raises = raises

    def write(self, *args):
        self.calls.append(args)
        if self.raises:
            raise OSError("activity unavailable")


def test_try_notify_claims_after_failed_activity_write_and_skips_intent(conn):
    """Notification failure is observable only through its attempted flag."""
    op_id = _intent(conn)
    intent = deploy_store.get_open_by_name(conn, "sma")
    activity = _Activity()
    assert not deploy_ops.try_notify(conn, intent, activity, now=NOW)
    assert activity.calls == []

    deploy_store.set_phase(conn, op_id, "needs_retry", now=NOW,
                           fail_reason="syscall_failed")
    failed_activity = _Activity(raises=True)
    row = deploy_store.get_open_by_name(conn, "sma")
    assert deploy_ops.try_notify(conn, row, failed_activity, now=NOW)
    assert len(failed_activity.calls) == 1
    notified = deploy_store.get_open_by_name(conn, "sma")
    assert notified["notify_attempted"] == 1
    assert not deploy_ops.try_notify(conn, notified, failed_activity, now=NOW)


def _open_disable(conn, name):
    return deploy_store.insert_intent(
        conn, op_kind="disable_symlink", name=name, from_kind="symlink",
        to_kind="absent", from_target=f".versions/{name}/artifact-a",
        to_target=None, retired_path=None, subject_content_hash="content-a",
        subject_artifact_hash="artifact-a", target_content_hash=None,
        target_artifact_hash=None, approval_id=None, revoke=False,
        revoke_row_id=None, force=False, actor="human_cli", now=NOW)


def _strategy_candidate(path, *, plugin):
    path.mkdir(parents=True)
    (path / "plugin.py").write_text("def compute(df, params):\n    return {'action': 'hold'}\n")
    (path / "config.yaml").write_text(
        f"kind: strategy\nindicators:\n  signal:\n    plugin: {plugin}\n")
    (path / "test_plugin.py").write_text("def test_x():\n    pass\n")


def test_approve_rejects_open_deploy_op_from_payload_only_dependency(tmp_path, conn):
    """Persisted pins are checked even when mutable config no longer names the indicator."""
    plugins_root = tmp_path / "plugins"
    candidate = plugins_root / "_staging" / "1" / "strategy"
    _strategy_candidate(candidate, plugin="config_dep")
    approval_id = approvals_store.create(
        conn, "plugin", {
            "name": "strategy", "candidate_origin": "staging",
            "candidate_path": "plugins/_staging/1/strategy",
            "indicator_deps": {"signal": {"plugin": "payload_dep"}},
        }, NOW)
    _open_disable(conn, "payload_dep")
    settings = load_settings(Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")

    with pytest.raises(switch.OpenDeployOpError, match="payload_dep.*op_id=1"):
        switch.approve_candidate(
            conn, approval_id, decided_by="human", now=NOW,
            plugins_root=plugins_root, settings=settings)


def test_retry_approval_rejects_open_deploy_op(tmp_path, conn):
    """Retry delegates to the same open deploy-op gate as first approval."""
    plugins_root = tmp_path / "plugins"
    candidate = plugins_root / "_staging" / "1" / "strategy"
    _strategy_candidate(candidate, plugin="indicator")
    approval_id = approvals_store.create(
        conn, "plugin", {
            "name": "strategy", "candidate_origin": "staging",
            "candidate_path": "plugins/_staging/1/strategy",
            "content_hash": "candidate-content", "indicator_deps": {},
        }, NOW)
    _open_disable(conn, "strategy")
    settings = load_settings(Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")

    with pytest.raises(switch.OpenDeployOpError, match="strategy.*op_id=1"):
        switch.retry_approval(
            conn, approval_id, decided_by="human", now=NOW,
            plugins_root=plugins_root, settings=settings)


def test_bless_rejects_open_deploy_op_from_config_dependency(tmp_path, conn):
    """Bless has no payload yet, so it checks the candidate's config dependencies."""
    plugins_root = tmp_path / "plugins"
    candidate = plugins_root / "_human" / "strategy"
    _strategy_candidate(candidate, plugin="config_dep")
    _open_disable(conn, "config_dep")
    settings = load_settings(Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")

    with pytest.raises(switch.OpenDeployOpError, match="config_dep.*op_id=1"):
        switch.bless_candidate(
            conn, name="strategy", human_dir=candidate, settings=settings,
            now=NOW, decided_by="human")


def _settings():
    return load_settings(
        Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


def test_approve_rejects_open_deploy_op_from_config_only_dependency(tmp_path, conn):
    """A config-derived indicator is checked even when the payload pins none."""
    plugins_root = tmp_path / "plugins"
    candidate = plugins_root / "_staging" / "1" / "strategy"
    _strategy_candidate(candidate, plugin="config_dep")
    approval_id = approvals_store.create(
        conn, "plugin", {
            "name": "strategy", "candidate_origin": "staging",
            "candidate_path": "plugins/_staging/1/strategy",
        }, NOW)
    _open_disable(conn, "config_dep")

    with pytest.raises(switch.OpenDeployOpError, match="config_dep.*op_id=1"):
        switch.approve_candidate(
            conn, approval_id, decided_by="human", now=NOW,
            plugins_root=plugins_root, settings=_settings())


def test_approve_rejects_when_config_dependency_grows_while_waiting_for_lock(
        tmp_path, conn, monkeypatch):
    """A dependency added after the lock set was chosen is refused, not checked."""
    plugins_root = tmp_path / "plugins"
    candidate = plugins_root / "_staging" / "1" / "strategy"
    _strategy_candidate(candidate, plugin="first_dep")
    approval_id = approvals_store.create(
        conn, "plugin", {
            "name": "strategy", "candidate_origin": "staging",
            "candidate_path": "plugins/_staging/1/strategy",
        }, NOW)
    _open_disable(conn, "late_dep")
    real_locks = switch._plugin_locks
    locked_names: list[tuple[str, ...]] = []

    @contextlib.contextmanager
    def growing_locks(root, names, **kwargs):
        locked_names.append(tuple(sorted(names)))
        with real_locks(root, names, **kwargs):
            (candidate / "config.yaml").write_text(
                "kind: strategy\nindicators:\n  signal:\n    plugin: first_dep\n"
                "  extra:\n    plugin: late_dep\n")
            yield

    monkeypatch.setattr(switch, "_plugin_locks", growing_locks)
    with pytest.raises(ValueError, match="candidate_changed"):
        switch.approve_candidate(
            conn, approval_id, decided_by="human", now=NOW,
            plugins_root=plugins_root, settings=_settings())
    assert locked_names == [("first_dep", "strategy")]
    assert conn.execute(
        "SELECT status FROM approval_requests WHERE id=?", (approval_id,)).fetchone()[0] == "pending"


@pytest.mark.parametrize("entry", ["approve_candidate", "retry_approval"])
def test_decided_approval_returns_already_decided_despite_open_deploy_op(
        tmp_path, conn, entry):
    """A finished approval is reported as decided, not blocked by a later op."""
    plugins_root = tmp_path / "plugins"
    candidate = plugins_root / "_staging" / "1" / "strategy"
    _strategy_candidate(candidate, plugin="indicator")
    approval_id = approvals_store.create(
        conn, "plugin", {
            "name": "strategy", "candidate_origin": "staging",
            "candidate_path": "plugins/_staging/1/strategy",
        }, NOW)
    conn.execute("UPDATE approval_requests SET status='approved' WHERE id=?",
                 (approval_id,))
    conn.commit()
    _open_disable(conn, "strategy")

    outcome = getattr(switch, entry)(
        conn, approval_id, decided_by="human", now=NOW,
        plugins_root=plugins_root, settings=_settings())

    assert outcome.outcome == "already_decided"


def test_approve_rejects_candidate_that_appears_while_waiting_for_lock(
        tmp_path, conn, monkeypatch):
    """A candidate absent when the lock set was chosen must not be approved unlocked."""
    plugins_root = tmp_path / "plugins"
    candidate = plugins_root / "_staging" / "1" / "strategy"
    approval_id = approvals_store.create(
        conn, "plugin", {
            "name": "strategy", "candidate_origin": "staging",
            "candidate_path": "plugins/_staging/1/strategy",
        }, NOW)
    real_locks = switch._plugin_locks
    locked_names: list[tuple[str, ...]] = []

    @contextlib.contextmanager
    def appearing_locks(root, names, **kwargs):
        locked_names.append(tuple(sorted(names)))
        with real_locks(root, names, **kwargs):
            _strategy_candidate(candidate, plugin="late_dep")
            yield

    monkeypatch.setattr(switch, "_plugin_locks", appearing_locks)
    with pytest.raises(ValueError, match="candidate_changed"):
        switch.approve_candidate(
            conn, approval_id, decided_by="human", now=NOW,
            plugins_root=plugins_root, settings=_settings())
    assert locked_names == [("strategy",)]


def test_try_notify_logs_activity_failure_and_still_marks_attempted(conn, caplog):
    op_id = _intent(conn)
    deploy_store.set_phase(conn, op_id, "needs_retry", now=NOW,
                           fail_reason="syscall_failed")
    row = deploy_store.get_open_by_name(conn, "sma")
    with caplog.at_level("WARNING"):
        assert deploy_ops.try_notify(conn, row, _Activity(raises=True), now=NOW)
    assert f"op_id={op_id}" in caplog.text
    assert deploy_store.get_open_by_name(conn, "sma")["notify_attempted"] == 1
