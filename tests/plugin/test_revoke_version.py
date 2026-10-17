from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.plugin import deploy_disable
from agentic_fx.plugin import loader
from agentic_fx.plugin import switch
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import db as db_store
from agentic_fx.store import plugin_deploy_ops as deploy_store
from agentic_fx.store import signals as signals_store
from agentic_fx.store import missions as missions_store
from agentic_fx.tools import plugin_loader
from tests.plugin.conftest import SETTINGS


NOW = datetime(2026, 10, 16, 3, 0, tzinfo=timezone.utc)


def _version(root, name: str, *, value: str):
    initial = root / ".versions" / name / ("0" * 64)
    initial.mkdir(parents=True)
    (initial / "plugin.py").write_text(
        f"def compute(df, params):\n    return {{'value': {value}}}\n")
    (initial / "config.yaml").write_text("kind: indicator\noutputs: [value]\n")
    (initial / "test_plugin.py").write_text("def test_plugin():\n    pass\n")
    artifact = loader.artifact_hash_bytes(*(p.read_bytes() for p in
        (initial / "plugin.py", initial / "config.yaml", initial / "test_plugin.py")))
    version = initial.with_name(artifact)
    initial.rename(version)
    return version


def _approve(conn, name, version, *, now=NOW):
    content = loader.content_hash(version)
    artifact = loader.artifact_hash_bytes(*(p.read_bytes() for p in
        (version / "plugin.py", version / "config.yaml", version / "test_plugin.py")))
    approval_id = approvals_store.create(
        conn, "plugin", {"name": name, "content_hash": content,
                         "artifact_hash": artifact}, now)
    approvals_store.apply_decision(conn, approval_id, "approved", decided_by="human", now=now)
    return content, artifact, approval_id


def _signal(conn, name, content, *, claimed=False):
    signal_id = signals_store.add(
        conn, plugin=name, content_hash=content, pair="USDJPY", timeframe="1h",
        bar_ts=(NOW + timedelta(minutes=1) if claimed else NOW).isoformat(),
        kind="signal", payload={}, now=NOW)
    if claimed:
        conn.execute("UPDATE signals SET status='claimed' WHERE id=?", (signal_id,))
        conn.commit()
    return signal_id


def test_revoke_is_idempotent_after_rejected_decision(tmp_path):
    conn = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(conn)
    root = tmp_path / "plugins"
    root.mkdir()
    name = "sma"
    version = root / ".versions" / name / ("a" * 64)
    version.mkdir(parents=True)
    (version / "plugin.py").write_text("x")
    (version / "config.yaml").write_text("y")
    (version / "test_plugin.py").write_text("z")
    content = loader.content_hash(version)
    artifact = loader.artifact_hash_bytes(
        (version / "plugin.py").read_bytes(), (version / "config.yaml").read_bytes(),
        (version / "test_plugin.py").read_bytes())
    aid = approvals_store.create(conn, "plugin", {"name": name, "content_hash": content,
                                                     "artifact_hash": artifact}, NOW)
    approvals_store.apply_decision(conn, aid, "approved", decided_by="human", now=NOW)
    signal_id = signals_store.add(
        conn, plugin=name, content_hash=content, pair="USDJPY", timeframe="1h",
        bar_ts=NOW.isoformat(), kind="strategy", payload={"direction": "long"}, now=NOW)

    first = deploy_disable.revoke_version(conn, root, name, content_hash=content, now=NOW)
    second = deploy_disable.revoke_version(conn, root, name, content_hash=content, now=NOW)

    assert first.outcome == "revoked"
    assert second.outcome == "already_revoked"
    assert len(deploy_store.list_by_name(conn, name)) == 1
    assert conn.execute("SELECT status FROM signals WHERE id=?", (signal_id,)).fetchone()[0] == "abandoned"
    assert conn.execute("SELECT signal_id FROM plugin_deploy_op_signals").fetchone()[0] == signal_id


def test_revoke_old_version_is_atomic_excludes_linked_signals_and_is_idempotent(tmp_path, monkeypatch):
    conn = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(conn)
    root = tmp_path / "plugins"
    root.mkdir()
    old = _version(root, "sma", value="1.0")
    current = _version(root, "sma", value="2.0")
    old_content, _, old_approval = _approve(conn, "sma", old)
    current_content, _, _ = _approve(conn, "sma", current, now=NOW + timedelta(seconds=1))
    live = root / "sma"
    live.symlink_to(f".versions/sma/{current.name}")
    pending, claimed = _signal(conn, "sma", old_content), _signal(conn, "sma", old_content, claimed=True)

    real_abandon = signals_store.abandon_pending_for_identities
    monkeypatch.setattr(signals_store, "abandon_pending_for_identities",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("abort")))
    with pytest.raises(RuntimeError, match="abort"):
        deploy_disable.revoke_version(conn, root, "sma", content_hash=old_content, now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM approval_requests WHERE status='rejected'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM plugin_deploy_ops").fetchone()[0] == 0
    monkeypatch.setattr(signals_store, "abandon_pending_for_identities", real_abandon)

    outcome = deploy_disable.revoke_version(conn, root, "sma", content_hash=old_content, now=NOW)
    rejected = conn.execute(
        "SELECT * FROM approval_requests WHERE status='rejected' ORDER BY id").fetchall()
    row = deploy_store.list_by_name(conn, "sma")[0]
    assert outcome.outcome == "revoked"
    assert len(rejected) == 1
    assert rejected[0]["reason"] == "revoked_by_operator"
    assert rejected[0]["decided_at"] > conn.execute(
        "SELECT decided_at FROM approval_requests WHERE id=?", (old_approval,)).fetchone()[0]
    assert json.loads(rejected[0]["payload_json"])["revokes_approval_id"] == old_approval
    assert row["op_kind"] == "revoke" and row["phase"] == "done"
    assert row["approval_id"] == old_approval and row["revoke_row_id"] == rejected[0]["id"]
    assert dict(conn.execute("SELECT id,status FROM signals").fetchall()) == {
        pending: "abandoned", claimed: "claimed"}
    assert {tuple(row) for row in conn.execute(
        "SELECT signal_id,status_at_link FROM plugin_deploy_op_signals")} == {
        (pending, "pending"), (claimed, "claimed")}
    mission = missions_store.start(conn, "trade", "local", "model", NOW)
    assert signals_store.claim_oldest(conn, mission_id=mission, now=NOW, freshness_bars=1) is None
    assert signals_store.pending_exists(conn, now=NOW, freshness_bars=1) is False
    assert signals_store.recent(conn, "USDJPY", since=NOW, exclude_deploy_linked=True) == []
    assert deploy_disable.revoke_version(conn, root, "sma", content_hash=old_content, now=NOW).outcome == "already_revoked"
    assert len(deploy_store.list_by_name(conn, "sma")) == 1
    with pytest.raises(ValueError, match="current live"):
        deploy_disable.revoke_version(conn, root, "sma", content_hash=current_content, now=NOW)


def test_revoke_hides_linked_old_version_signals_from_all_consumers(tmp_path):
    conn = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(conn)
    root = tmp_path / "plugins"
    root.mkdir()
    old = _version(root, "sma", value="1.0")
    current = _version(root, "sma", value="2.0")
    old_content, _, _ = _approve(conn, "sma", old)
    _approve(conn, "sma", current, now=NOW + timedelta(seconds=1))
    (root / "sma").symlink_to(f".versions/sma/{current.name}")
    pending = _signal(conn, "sma", old_content)
    claimed = _signal(conn, "sma", old_content, claimed=True)

    deploy_disable.revoke_version(conn, root, "sma", content_hash=old_content, now=NOW)

    assert dict(conn.execute("SELECT id,status FROM signals").fetchall()) == {
        pending: "abandoned", claimed: "claimed"}
    assert signals_store.recent(conn, "USDJPY", since=NOW, exclude_deploy_linked=True) == []
    mission = missions_store.start(conn, "trade", "local", "model", NOW)
    assert signals_store.claim_oldest(conn, mission_id=mission, now=NOW, freshness_bars=1) is None
    assert signals_store.pending_exists(conn, now=NOW, freshness_bars=1) is False


def test_revocation_excludes_loader_until_bless_restores_identity(tmp_path, monkeypatch):
    conn = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(conn)
    root = tmp_path / "plugins"
    root.mkdir()
    (root / ".locks").mkdir()
    version = _version(root, "sma", value="1.0")
    content, artifact, _ = _approve(conn, "sma", version)
    live = root / "sma"
    live.symlink_to(f".versions/sma/{version.name}")

    deploy_disable.disable_plugin(conn, root, "sma", revoke=True, force=False,
                                  now=NOW, settings=SETTINGS)
    live.symlink_to(f".versions/sma/{version.name}")
    assert plugin_loader.approved_plugins(conn, root, settings=SETTINGS).inventory.metas == ()

    human = root / "_human" / "sma"
    human.mkdir(parents=True)
    for filename in ("plugin.py", "config.yaml", "test_plugin.py"):
        (human / filename).write_bytes((version / filename).read_bytes())
    from agentic_fx.plugin.gate_pytest import GateResult
    monkeypatch.setattr(
        switch, "run_gate_pytest",
        lambda *args, **kwargs: GateResult(True, 0, "1 passed", 0.1))
    approval_id = switch.bless_candidate(
        conn, name="sma", human_dir=human, settings=SETTINGS,
        now=NOW + timedelta(seconds=2), decided_by="human")

    approved = conn.execute("SELECT * FROM approval_requests WHERE id=?", (approval_id,)).fetchone()
    assert approved["status"] == "approved"
    assert json.loads(approved["payload_json"])["content_hash"] == content
    assert loader.artifact_hash_bytes(*(p.read_bytes() for p in
        (human / "plugin.py", human / "config.yaml", human / "test_plugin.py"))) == artifact
    admitted = plugin_loader.approved_plugins(conn, root, settings=SETTINGS).inventory.metas
    assert tuple(meta.name for meta in admitted) == ("sma",)


def _prepare_old_and_live(tmp_path):
    conn = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(conn)
    root = tmp_path / "plugins"
    root.mkdir()
    old = _version(root, "sma", value="1.0")
    current = _version(root, "sma", value="2.0")
    old_content, old_artifact, _ = _approve(conn, "sma", old)
    _approve(conn, "sma", current, now=NOW + timedelta(seconds=1))
    (root / "sma").symlink_to(f".versions/sma/{current.name}")
    return conn, root, old, old_content, old_artifact


def test_revoke_records_the_approved_artifact_hash_even_if_the_directory_changed(tmp_path):
    conn, root, old, old_content, old_artifact = _prepare_old_and_live(tmp_path)
    (old / "config.yaml").write_text("kind: indicator\noutputs: [value, extra]\n")

    outcome = deploy_disable.revoke_version(conn, root, "sma", content_hash=old_content, now=NOW)

    assert outcome.outcome == "revoked"
    assert deploy_store.list_by_name(conn, "sma")[0]["subject_artifact_hash"] == old_artifact


def test_revoke_warns_when_the_directory_artifact_differs_from_the_approval(tmp_path):
    conn, root, old, old_content, old_artifact = _prepare_old_and_live(tmp_path)
    (old / "test_plugin.py").write_text("def test_plugin():\n    assert True\n")
    events = []

    class Activity:
        def write(self, category, event, summary):
            events.append(event)

    outcome = deploy_disable.revoke_version(conn, root, "sma", content_hash=old_content,
                                            now=NOW, activity=Activity())

    assert outcome.outcome == "revoked"
    assert deploy_store.list_by_name(conn, "sma")[0]["subject_artifact_hash"] == old_artifact
    assert events.count("plugin_version_dir_mismatch") == 1


def test_revoke_proceeds_without_a_version_directory(tmp_path):
    import shutil
    conn, root, old, old_content, old_artifact = _prepare_old_and_live(tmp_path)
    shutil.rmtree(old)

    outcome = deploy_disable.revoke_version(conn, root, "sma", content_hash=old_content, now=NOW)

    assert outcome.outcome == "revoked"
    assert deploy_store.list_by_name(conn, "sma")[0]["subject_artifact_hash"] == old_artifact


def test_revoke_without_recorded_artifact_hash_falls_back_to_directory_or_refuses(tmp_path):
    import shutil
    conn, root, old, old_content, old_artifact = _prepare_old_and_live(tmp_path)
    conn.execute("UPDATE approval_requests SET payload_json=? WHERE id=1",
                 (json.dumps({"name": "sma", "content_hash": old_content}),))
    conn.commit()
    shutil.copytree(old, tmp_path / "keep")
    shutil.rmtree(old)
    with pytest.raises(ValueError, match="artifact hash"):
        deploy_disable.revoke_version(conn, root, "sma", content_hash=old_content, now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM approval_requests WHERE status='rejected'").fetchone()[0] == 0
    shutil.copytree(tmp_path / "keep", old)

    outcome = deploy_disable.revoke_version(conn, root, "sma", content_hash=old_content, now=NOW)

    assert outcome.outcome == "revoked"
    assert deploy_store.list_by_name(conn, "sma")[0]["subject_artifact_hash"] == old_artifact


def test_rerun_after_crash_before_notify_recovers_revoke_notification_once(tmp_path, monkeypatch):
    conn = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(conn)
    root = tmp_path / "plugins"
    root.mkdir()
    old = _version(root, "sma", value="1.0")
    current = _version(root, "sma", value="2.0")
    old_content, _, _ = _approve(conn, "sma", old)
    _approve(conn, "sma", current, now=NOW + timedelta(seconds=1))
    (root / "sma").symlink_to(f".versions/sma/{current.name}")
    real_notify = deploy_disable.deploy_ops.try_notify
    monkeypatch.setattr(deploy_disable.deploy_ops, "try_notify",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError, match="crash"):
        deploy_disable.revoke_version(conn, root, "sma", content_hash=old_content, now=NOW)
    monkeypatch.setattr(deploy_disable.deploy_ops, "try_notify", real_notify)
    row = deploy_store.list_by_name(conn, "sma")[0]
    assert row["op_kind"] == "revoke" and row["notify_attempted"] == 0

    events = []

    class Act:
        def write(self, category, event, summary):
            events.append((event, summary))

    again = deploy_disable.revoke_version(conn, root, "sma", content_hash=old_content,
                                          now=NOW, activity=Act())
    assert again.outcome == "already_revoked"
    assert again.notified_op_ids == (row["op_id"],)
    assert [e for e, _ in events] == ["plugin_deploy_op_done"]
    assert deploy_store.list_by_name(conn, "sma")[0]["notify_attempted"] == 1
    third = deploy_disable.revoke_version(conn, root, "sma", content_hash=old_content,
                                          now=NOW, activity=Act())
    assert third.outcome == "already_revoked" and third.notified_op_ids == ()
    assert len(events) == 1
