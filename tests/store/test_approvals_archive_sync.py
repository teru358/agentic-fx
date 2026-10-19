"""承認の決定・撤回と候補台帳 (outcome_class) の同一 tx 更新。"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.store import approvals, candidate_archives as archives
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 10, 19, 9, tzinfo=timezone.utc)


def _db(tmp_path):
    conn = connect(tmp_path / "sync.db")
    init_db(conn)
    return conn


def _row(conn, mission_id=3, artifact="A1", content="C1", name="rsi_v2"):
    return archives.insert(
        conn, mission_id=mission_id, name=name, content_hash=content,
        artifact_hash=artifact, archive_path=None, pair="USDJPY",
        metrics={"pf": 1.3}, now=NOW, commit=True)


def _submit(conn, archive_id, *, mission_id=3, artifact="A1", content="C1",
            name="rsi_v2", with_id=True):
    payload = {"name": name, "content_hash": content,
               "artifact_hash": artifact, "mission_id": mission_id}
    if with_id:
        payload["candidate_archive_id"] = archive_id
    approval_id = approvals.create(conn, kind="plugin", payload=payload,
                                   now=NOW, commit=True)
    assert archives.settle(conn, archive_id, "pending", commit=True)
    return approval_id


def _outcome(conn, archive_id):
    return archives.get(conn, archive_id)["outcome_class"]


@pytest.mark.parametrize("status,expected", [
    ("approved", "approved"), ("rejected", "rejected_by_human"),
    ("expired", "expired"), ("invalidated", "invalidated")])
def test_ac15_each_decision_moves_the_submitted_row(tmp_path, status, expected):
    conn = _db(tmp_path)
    archive_id = _row(conn)
    other = _row(conn, artifact="A9", content="C9")
    approval_id = _submit(conn, archive_id)
    approvals.apply_decision(conn, approval_id, status, decided_by="t", now=NOW)
    assert _outcome(conn, archive_id) == expected
    assert _outcome(conn, other) == "not_submitted"


def test_ac15_rewritten_selftest_updates_candidate_archive_id_row(tmp_path):
    conn = _db(tmp_path)
    backtest_row = _row(conn, artifact="A-backtest")
    submitted_row = _row(conn, artifact="A-submitted")  # 提出直前に作った行
    archives.settle(conn, backtest_row, "pending", commit=True)  # 巻き込み確認用
    approval_id = approvals.create(
        conn, kind="plugin", payload={
            "name": "rsi_v2", "content_hash": "C1",
            "artifact_hash": "A-submitted", "mission_id": 3,
            "candidate_archive_id": submitted_row}, now=NOW, commit=True)
    archives.settle(conn, submitted_row, "pending", commit=True)
    approvals.apply_decision(conn, approval_id, "approved", decided_by="t",
                             now=NOW)
    assert _outcome(conn, submitted_row) == "approved"
    assert _outcome(conn, backtest_row) == "pending"


def test_ac15_cas_failure_leaves_archive_untouched(tmp_path):
    conn = _db(tmp_path)
    archive_id = _row(conn)
    approval_id = _submit(conn, archive_id)
    approvals.apply_decision(conn, approval_id, "approved", decided_by="t",
                             now=NOW)
    with pytest.raises(approvals.AlreadyDecidedError):
        approvals.apply_decision(conn, approval_id, "rejected", decided_by="t",
                                 now=NOW)
    assert _outcome(conn, archive_id) == "approved"


def test_ac15_missing_row_still_commits_decision_with_warning(tmp_path, caplog):
    conn = _db(tmp_path)
    approval_id = approvals.create(
        conn, kind="plugin", payload={
            "name": "n", "content_hash": "c", "artifact_hash": "nope",
            "mission_id": 99, "candidate_archive_id": 12345},
        now=NOW, commit=True)
    with caplog.at_level(logging.WARNING, logger="agentic_fx.store.approvals"):
        approvals.apply_decision(conn, approval_id, "approved", decided_by="t",
                                 now=NOW)
    assert conn.execute("SELECT status FROM approval_requests WHERE id=?",
                        (approval_id,)).fetchone()[0] == "approved"
    assert any("no candidate archive row" in r.message for r in caplog.records)


def test_ac15_row_not_pending_skips_update_with_warning(tmp_path, caplog):
    conn = _db(tmp_path)
    archive_id = _row(conn)  # not_submitted のまま (pending でない)
    approval_id = approvals.create(
        conn, kind="plugin", payload={
            "name": "n", "content_hash": "C1", "artifact_hash": "A1",
            "mission_id": 3, "candidate_archive_id": archive_id},
        now=NOW, commit=True)
    with caplog.at_level(logging.WARNING, logger="agentic_fx.store.approvals"):
        approvals.apply_decision(conn, approval_id, "approved", decided_by="t",
                                 now=NOW)
    assert _outcome(conn, archive_id) == "not_submitted"
    assert conn.execute("SELECT status FROM approval_requests WHERE id=?",
                        (approval_id,)).fetchone()[0] == "approved"
    assert any("is not pending" in r.message for r in caplog.records)


def test_ac15_legacy_payload_without_candidate_id_uses_resolve(tmp_path):
    conn = _db(tmp_path)
    archive_id = _row(conn)
    approval_id = _submit(conn, archive_id, with_id=False)
    approvals.apply_decision(conn, approval_id, "rejected", decided_by="t",
                             now=NOW)
    assert _outcome(conn, archive_id) == "rejected_by_human"


def test_ac15_failure_after_approval_update_leaves_nothing_half_applied(
        tmp_path, monkeypatch):
    path = tmp_path / "sync.db"
    conn = connect(path)
    init_db(conn)
    archive_id = _row(conn)
    approval_id = _submit(conn, archive_id)

    def boom(*args, **kwargs):
        raise RuntimeError("injected")

    monkeypatch.setattr(archives, "settle", boom)
    with pytest.raises(RuntimeError):
        approvals.apply_decision(conn, approval_id, "approved", decided_by="t",
                                 now=NOW)
    monkeypatch.undo()
    other = connect(path)
    assert other.execute("SELECT status FROM approval_requests WHERE id=?",
                         (approval_id,)).fetchone()[0] == "pending"
    assert _outcome(other, archive_id) == "pending"
    assert conn.execute("SELECT status FROM approval_requests WHERE id=?",
                        (approval_id,)).fetchone()[0] == "pending"


def test_ac15_non_plugin_kind_does_not_touch_archives(tmp_path):
    conn = _db(tmp_path)
    archive_id = _row(conn)
    archives.settle(conn, archive_id, "pending", commit=True)
    approval_id = approvals.create(
        conn, kind="live_trade", payload={
            "mission_id": 3, "artifact_hash": "A1", "content_hash": "C1"},
        now=NOW, commit=True)
    approvals.apply_decision(conn, approval_id, "approved", decided_by="t",
                             now=NOW)
    assert _outcome(conn, archive_id) == "pending"


# --- AC-21 (store 層) ------------------------------------------------------

def _approved(conn, archive_id, *, decided_at, **kwargs):
    approval_id = _submit(conn, archive_id, **kwargs)
    approvals.apply_decision(conn, approval_id, "approved", decided_by="t",
                             now=decided_at)
    return approval_id


def test_ac21_revocation_moves_approved_row_to_revoked(tmp_path):
    conn = _db(tmp_path)
    archive_id = _row(conn)
    _approved(conn, archive_id, decided_at=NOW)
    revoke_id = approvals.append_revocation(
        conn, "rsi_v2", "C1", now=NOW + timedelta(hours=1))
    assert revoke_id is not None
    assert _outcome(conn, archive_id) == "revoked"


def test_ac21_revocation_returning_none_leaves_row(tmp_path):
    conn = _db(tmp_path)
    archive_id = _row(conn)
    _approved(conn, archive_id, decided_at=NOW)
    approvals.append_revocation(conn, "rsi_v2", "C1",
                                now=NOW + timedelta(hours=1))
    assert approvals.append_revocation(
        conn, "rsi_v2", "C1", now=NOW + timedelta(hours=2)) is None
    assert _outcome(conn, archive_id) == "revoked"


def test_ac21_only_the_targeted_approval_row_is_revoked(tmp_path):
    conn = _db(tmp_path)
    first = _row(conn, artifact="A1")
    second = _row(conn, mission_id=4, artifact="A2")
    _approved(conn, first, decided_at=NOW)
    _approved(conn, second, decided_at=NOW + timedelta(hours=1),
              mission_id=4, artifact="A2")
    approvals.append_revocation(conn, "rsi_v2", "C1",
                                now=NOW + timedelta(hours=2))
    assert _outcome(conn, second) == "revoked"
    assert _outcome(conn, first) == "approved"


def test_ac21_failure_leaves_no_revocation_row(tmp_path, monkeypatch):
    path = tmp_path / "sync.db"
    conn = connect(path)
    init_db(conn)
    archive_id = _row(conn)
    _approved(conn, archive_id, decided_at=NOW)

    def boom(*args, **kwargs):
        raise RuntimeError("injected")

    monkeypatch.setattr(archives, "settle", boom)
    with pytest.raises(RuntimeError):
        approvals.append_revocation(conn, "rsi_v2", "C1",
                                    now=NOW + timedelta(hours=1))
    monkeypatch.undo()
    other = connect(path)
    assert other.execute(
        "SELECT COUNT(*) FROM approval_requests WHERE status='rejected'"
    ).fetchone()[0] == 0
    assert _outcome(other, archive_id) == "approved"


def test_ac21_commit_false_failure_does_not_commit_for_caller(tmp_path,
                                                              monkeypatch):
    conn = _db(tmp_path)
    archive_id = _row(conn)
    _approved(conn, archive_id, decided_at=NOW)
    conn.execute("BEGIN IMMEDIATE")
    revoke_id = approvals.append_revocation(
        conn, "rsi_v2", "C1", now=NOW + timedelta(hours=1), commit=False)
    assert revoke_id is not None
    assert _outcome(conn, archive_id) == "revoked"   # 同じ tx の中で更新済み
    conn.rollback()
    assert _outcome(conn, archive_id) == "approved"  # 呼び出し元の rollback で戻る
    assert conn.execute("SELECT COUNT(*) FROM approval_requests WHERE "
                        "status='rejected'").fetchone()[0] == 0
