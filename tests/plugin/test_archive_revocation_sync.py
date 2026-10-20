"""撤回の2経路 (disable --revoke、revoke_version) と候補台帳の同じ tx 更新。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agentic_fx.plugin import deploy_disable
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import candidate_archives as archives
from agentic_fx.store import db as db_store
from tests.plugin.conftest import SETTINGS
from tests.plugin.test_deploy_disable import (
    NOW, _link_live, _version_with_kind)
from agentic_fx.plugin import loader


def _approved_with_row(conn, name, version):
    content = loader.content_hash(version)
    artifact = version.name
    archive_id = archives.insert(
        conn, mission_id=5, name=name, content_hash=content,
        artifact_hash=artifact, archive_path=None, pair=None, metrics={},
        now=NOW, commit=True)
    approval_id = approvals_store.create(
        conn, "plugin", {"name": name, "content_hash": content,
                         "artifact_hash": artifact, "mission_id": 5,
                         "candidate_archive_id": archive_id}, NOW)
    assert archives.settle(conn, archive_id, "pending", commit=True)
    approvals_store.apply_decision(conn, approval_id, "approved",
                                   decided_by="human", now=NOW)
    return content, archive_id


def _outcome(conn, archive_id):
    return archives.get(conn, archive_id)["outcome_class"]


def _world(tmp_path):
    conn = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(conn)
    root = tmp_path / "plugins"
    root.mkdir()
    return conn, root


def test_ac21_disable_revoke_moves_row_to_revoked(tmp_path, monkeypatch):
    conn, root = _world(tmp_path)
    version = _version_with_kind(root, "sma", "a" * 64, kind="indicator")
    _, archive_id = _approved_with_row(conn, "sma", version)
    _link_live(root, "sma", version)
    monkeypatch.setattr(deploy_disable, "enumerate_dependents",
                        lambda *a, **k: deploy_disable.DependentsReport())
    assert _outcome(conn, archive_id) == "approved"
    outcome = deploy_disable.disable_plugin(
        conn, root, "sma", revoke=True, force=False, now=NOW, settings=SETTINGS)
    assert outcome.outcome == "disabled"
    assert _outcome(conn, archive_id) == "revoked"


def test_ac21_disable_without_revoke_keeps_row_approved(tmp_path, monkeypatch):
    conn, root = _world(tmp_path)
    version = _version_with_kind(root, "sma", "a" * 64, kind="indicator")
    _, archive_id = _approved_with_row(conn, "sma", version)
    _link_live(root, "sma", version)
    monkeypatch.setattr(deploy_disable, "enumerate_dependents",
                        lambda *a, **k: deploy_disable.DependentsReport())
    deploy_disable.disable_plugin(
        conn, root, "sma", revoke=False, force=False, now=NOW,
        settings=SETTINGS)
    assert _outcome(conn, archive_id) == "approved"


def test_ac21_revoke_version_moves_row_to_revoked_and_second_call_is_noop(
        tmp_path):
    conn, root = _world(tmp_path)
    version = _version_with_kind(root, "sma", "a" * 64, kind="indicator")
    content, archive_id = _approved_with_row(conn, "sma", version)
    first = deploy_disable.revoke_version(conn, root, "sma",
                                          content_hash=content, now=NOW)
    assert first.outcome == "revoked"
    assert _outcome(conn, archive_id) == "revoked"
    second = deploy_disable.revoke_version(conn, root, "sma",
                                           content_hash=content, now=NOW)
    assert second.outcome == "already_revoked"
    assert _outcome(conn, archive_id) == "revoked"


def test_ac21_revoke_version_failure_rolls_back_archive_and_revocation(
        tmp_path, monkeypatch):
    conn, root = _world(tmp_path)
    version = _version_with_kind(root, "sma", "a" * 64, kind="indicator")
    content, archive_id = _approved_with_row(conn, "sma", version)
    from agentic_fx.store import plugin_deploy_ops as deploy_store

    def boom(*args, **kwargs):
        raise RuntimeError("injected")

    monkeypatch.setattr(deploy_store, "insert_done_revoke", boom)
    with pytest.raises(RuntimeError):
        deploy_disable.revoke_version(conn, root, "sma",
                                      content_hash=content, now=NOW)
    assert _outcome(conn, archive_id) == "approved"
    assert conn.execute("SELECT COUNT(*) FROM approval_requests WHERE "
                        "status='rejected'").fetchone()[0] == 0


def test_ac21_disable_revoke_failure_after_revocation_rolls_back_row(
        tmp_path, monkeypatch):
    conn, root = _world(tmp_path)
    version = _version_with_kind(root, "sma", "a" * 64, kind="indicator")
    _, archive_id = _approved_with_row(conn, "sma", version)
    _link_live(root, "sma", version)
    monkeypatch.setattr(deploy_disable, "enumerate_dependents",
                        lambda *a, **k: deploy_disable.DependentsReport())
    from agentic_fx.store import plugin_deploy_ops as deploy_store

    def boom(*args, **kwargs):
        raise RuntimeError("injected")

    monkeypatch.setattr(deploy_store, "insert_intent", boom)
    with pytest.raises(RuntimeError):
        deploy_disable.disable_plugin(
            conn, root, "sma", revoke=True, force=False, now=NOW,
            settings=SETTINGS)
    assert _outcome(conn, archive_id) == "approved"
    assert conn.execute("SELECT COUNT(*) FROM approval_requests WHERE "
                        "status='rejected'").fetchone()[0] == 0
