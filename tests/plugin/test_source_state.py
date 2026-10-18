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
