"""source_state: producer と OPEN gate が共有する source の live / 承認判定。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from agentic_fx.plugin.deploy_ops import acquire_name_locks
from agentic_fx.plugin.source_state import source_state
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def _conn(tmp_path):
    conn = connect(tmp_path / "s.db")
    init_db(conn)
    return conn


def _approve(conn, name, content_hash, status="approved"):
    conn.execute(
        "INSERT INTO approval_requests (kind,status,payload_json,decided_at,"
        "created_at) VALUES ('plugin',?,?,?,?)",
        (status, json.dumps({"name": name, "content_hash": content_hash}),
         NOW.isoformat(), NOW.isoformat()))
    conn.commit()


def test_absent_source_is_not_live(tmp_path):
    conn = _conn(tmp_path)
    root = tmp_path / "plugins"
    root.mkdir()
    assert source_state(conn, root, name="s", content_hash="a" * 64,
                        artifact_hash=None, indicators=None) == "strategy_not_live"


def test_plain_source_with_matching_hash_needs_an_approval(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    root = tmp_path / "plugins"
    root.mkdir()
    ident = SimpleNamespace(kind="plain", content_hash="a" * 64, artifact_hash=None)
    monkeypatch.setattr(
        "agentic_fx.store.plugin_deploy_ops.live_identity", lambda *_a: ident)
    args = dict(name="s", content_hash="a" * 64, artifact_hash=None,
                indicators=None)

    assert source_state(conn, root, **args) == "strategy_not_approved"
    _approve(conn, "s", "a" * 64)
    assert source_state(conn, root, **args) is None
    _approve(conn, "s", "a" * 64, status="rejected")
    assert source_state(conn, root, **args) == "strategy_not_approved"


def test_acquire_name_locks_is_a_working_public_wrapper(tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()
    with acquire_name_locks(root, {"a", "b"}, deadline=None):
        pass


def _live(monkeypatch, identities):
    monkeypatch.setattr(
        "agentic_fx.store.plugin_deploy_ops.live_identity",
        lambda _root, name: identities.get(name))


def _identity(kind, content_hash):
    return SimpleNamespace(kind=kind, content_hash=content_hash, artifact_hash=None)


def _indicator_world(tmp_path, monkeypatch, *, indicator_kind="symlink",
                     indicator_live_hash="b" * 64):
    conn = _conn(tmp_path)
    root = tmp_path / "plugins"
    root.mkdir()
    _live(monkeypatch, {
        "s": _identity("plain", "a" * 64),
        "ind": _identity(indicator_kind, indicator_live_hash),
    })
    _approve(conn, "s", "a" * 64)
    items = [SimpleNamespace(plugin_name="ind", content_hash="b" * 64)]
    args = dict(name="s", content_hash="a" * 64, artifact_hash=None,
                indicators=items)
    return conn, root, args


def _open_deploy_op(conn, name):
    from agentic_fx.store import plugin_deploy_ops as deploy_store
    deploy_store.insert_intent(
        conn, op_kind="disable_symlink", name=name, from_kind="symlink",
        to_kind="absent", from_target="x/v1", to_target=None, retired_path=None,
        subject_content_hash="d" * 64, subject_artifact_hash="v1",
        target_content_hash=None, target_artifact_hash=None, approval_id=None,
        revoke=False, revoke_row_id=None, force=False, actor="test", now=NOW)


def test_symlink_indicator_with_other_hash_is_not_live(tmp_path, monkeypatch):
    conn, root, args = _indicator_world(
        tmp_path, monkeypatch, indicator_live_hash="c" * 64)
    _approve(conn, "ind", "b" * 64)

    assert source_state(conn, root, **args) == "indicator_not_live"


def test_symlink_indicator_needs_its_own_approval(tmp_path, monkeypatch):
    conn, root, args = _indicator_world(tmp_path, monkeypatch)

    assert source_state(conn, root, **args) == "indicator_not_approved"
    _approve(conn, "ind", "b" * 64, status="rejected")
    assert source_state(conn, root, **args) == "indicator_not_approved"
    _approve(conn, "ind", "b" * 64)
    assert source_state(conn, root, **args) is None


def test_open_deploy_op_on_an_indicator_blocks(tmp_path, monkeypatch):
    conn, root, args = _indicator_world(tmp_path, monkeypatch)
    _approve(conn, "ind", "b" * 64)
    assert source_state(conn, root, **args) is None

    _open_deploy_op(conn, "ind")
    assert source_state(conn, root, **args) == "open_deploy_op"


def test_open_deploy_op_on_the_source_itself_blocks(tmp_path, monkeypatch):
    conn, root, args = _indicator_world(tmp_path, monkeypatch)
    _approve(conn, "ind", "b" * 64)

    _open_deploy_op(conn, "s")
    assert source_state(conn, root, **args) == "open_deploy_op"
