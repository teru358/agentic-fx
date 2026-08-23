"""process_expired_approvals の意味論 (プラン 10 Task 11g、統合裁定 R-i8、
設計書 §4.3・裁定1)。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_fx.plugin import switch
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import db as db_store

NOW = datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)
PAST = NOW - timedelta(hours=1)


@pytest.fixture
def env(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / ".locks").mkdir()
    conn = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(conn)
    return tmp_path, plugins_dir, conn


def test_expired_plugin_pending_with_unfinished_journal_is_skipped(env):
    """期限到来 + 未完ジャーナル有り → スキップ (今回は expired にしない、
    次回再試行)。"""
    tmp_path, plugins_dir, conn = env
    approval_id = approvals_store.create(
        conn, kind="plugin",
        payload={"name": "sma", "content_hash": "h1", "artifact_hash": "a1",
                 "candidate_origin": "staging",
                 "candidate_path": "plugins/_staging/1/sma"},
        now=PAST, expires_at=PAST)
    switch.begin_switch_journal(
        conn, kind="approve", approval_id=approval_id, name="sma",
        old_kind="absent", old_target=None,
        new_target=f".versions/sma/{'a' * 64}", switch_required=True,
        actor="human", now=PAST, commit=True)

    switch.process_expired_approvals(conn, plugins_root=plugins_dir, now=NOW)

    row = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    assert row["status"] == "pending"  # スキップされ expired にならない


def test_expired_plugin_pending_without_journal_becomes_expired(env):
    """期限到来 + 未完ジャーナル無し → apply_decision(expired) で確定。"""
    tmp_path, plugins_dir, conn = env
    approval_id = approvals_store.create(
        conn, kind="plugin",
        payload={"name": "sma", "content_hash": "h1", "artifact_hash": "a1",
                 "candidate_origin": "staging",
                 "candidate_path": "plugins/_staging/1/sma"},
        now=PAST, expires_at=PAST)

    switch.process_expired_approvals(conn, plugins_root=plugins_dir, now=NOW)

    row = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    assert row["status"] == "expired"


def test_expired_non_plugin_kind_uses_direct_expire_due_path(env):
    """非 plugin kind (例: mission) は flock を経由せず、従来どおり
    `expire_due` の直接 expired 化で処理される。"""
    tmp_path, plugins_dir, conn = env
    approval_id = approvals_store.create(
        conn, kind="mission", payload={"reason": "weekly review"},
        now=PAST, expires_at=PAST)

    switch.process_expired_approvals(conn, plugins_root=plugins_dir, now=NOW)

    row = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    assert row["status"] == "expired"
