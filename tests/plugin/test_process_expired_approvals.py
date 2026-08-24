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


def test_process_expired_approvals_takes_plugin_flock_and_blocks_until_released(env):
    """検収 m2: 11g 台帳は M7 (`process_expired_approvals` が flock を
    取らない) を「単体テストでは検出不能」として survived で確定していたが、
    `test_retire_plugin_takes_plugin_flock_and_blocks_until_released` (11e)
    とまったく同じ in-process パターンが効く — 別 fd で
    `plugins/.locks/<name>.lock` を先に握っておくと `process_expired_approvals`
    はブロックし、解放後に完了する。"""
    import fcntl
    import threading
    import time

    tmp_path, plugins_dir, conn = env
    approval_id = approvals_store.create(
        conn, kind="plugin",
        payload={"name": "sma", "content_hash": "h1", "artifact_hash": "a1",
                 "candidate_origin": "staging",
                 "candidate_path": "plugins/_staging/1/sma"},
        now=PAST, expires_at=PAST)

    lock_path = plugins_dir / ".locks" / "sma.lock"
    lock_path.parent.mkdir(exist_ok=True)
    holder = open(lock_path, "w")
    fcntl.flock(holder, fcntl.LOCK_EX)  # 先に flock を握る

    started = threading.Event()
    finished = threading.Event()

    def _run():
        started.set()
        switch.process_expired_approvals(conn, plugins_root=plugins_dir, now=NOW)
        finished.set()

    t = threading.Thread(target=_run)
    t.start()
    started.wait(timeout=2)
    time.sleep(0.4)  # flock を取っていればここではまだブロック中
    assert not finished.is_set(), (
        "process_expired_approvals が flock を取らずに進んだ (M7 の変異が生きている)")
    row = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    assert row["status"] == "pending"

    fcntl.flock(holder, fcntl.LOCK_UN)  # 解放すると進む
    holder.close()
    t.join(timeout=2)
    assert finished.is_set()

    row = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    assert row["status"] == "expired"


def test_expired_plugin_approval_deletes_staging_candidate_immediately(env):
    """codex 1 周目是正 I1 (verified-codex-round1.md): staging 候補は
    expired 化の tx 直後に削除される (§5.1 手順 3 の掃除所有表)。"""
    tmp_path, plugins_dir, conn = env
    candidate_dir = plugins_dir / "_staging" / "1" / "sma"
    candidate_dir.mkdir(parents=True)
    (candidate_dir / "plugin.py").write_text("x")
    (candidate_dir / "config.yaml").write_text("kind: indicator\n")
    (candidate_dir / "test_plugin.py").write_text("def test_x():\n    pass\n")
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
    assert not candidate_dir.exists()


def test_expired_plugin_approval_with_human_candidate_survives(env):
    """対照 pin: `candidate_origin='human'` は expired 化後も自動削除
    しない (人間所有 — §5.1 手順 3)。"""
    tmp_path, plugins_dir, conn = env
    candidate_dir = plugins_dir / "_human" / "sma"
    candidate_dir.mkdir(parents=True)
    (candidate_dir / "plugin.py").write_text("x")
    approval_id = approvals_store.create(
        conn, kind="plugin",
        payload={"name": "sma", "content_hash": "h1", "artifact_hash": "a1",
                 "candidate_origin": "human",
                 "candidate_path": "plugins/_human/sma"},
        now=PAST, expires_at=PAST)

    switch.process_expired_approvals(conn, plugins_root=plugins_dir, now=NOW)

    row = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    assert row["status"] == "expired"
    assert (candidate_dir / "plugin.py").exists()


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
