"""全 terminal decision が同じ plugin flock を通ることの multi-process test
(プラン 10 Task 11g、§8.1-32、設計書 §5 変異リスト「flock を落とす」の killer)。"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.config import load_settings
from agentic_fx.plugin import switch
from agentic_fx.store import db as db_store

NOW = datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)


def _fake_pytest_ok(plugin_dir, *, settings):
    from agentic_fx.plugin.gate_pytest import GateResult
    return GateResult(passed=True, returncode=0, stdout_tail="ok", duration_sec=0.1)


def test_reject_waits_for_approve_flock_and_then_gets_already_decided(tmp_path, monkeypatch):
    """killer (§5 変異リスト逐語): 「別プロセスの bless/approve と同時に
    走らせ、両者が互いの版を壊さないこと」+「reject/expire が plugin flock
    を取らない (killer = approve の切替直後に reject を差し込み、DB
    rejected と live 新版が食い違わないこと = reject が lock 待ちで approve
    完了後に AlreadyDecidedError になる)」。"""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / ".locks").mkdir()
    (plugins_dir / "_staging" / "1" / "sma").mkdir(parents=True)
    (plugins_dir / "_staging" / "1" / "sma" / "plugin.py").write_text(
        "def compute(df, params):\n    return {'v': 1.0}\n")
    (plugins_dir / "_staging" / "1" / "sma" / "config.yaml").write_text("kind: indicator\noutputs: [v]\n")
    (plugins_dir / "_staging" / "1" / "sma" / "test_plugin.py").write_text(
        "def test_x():\n    pass\n")

    db_path = tmp_path / "agentic.db"
    conn = db_store.connect(db_path)
    db_store.init_db(conn)
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    approval_id = switch.submit_candidate(
        conn, name="sma", staging_dir=plugins_dir / "_staging" / "1",
        candidate_origin="staging", mission_id=1, backlog_id=None,
        settings=load_settings(Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"), now=NOW)
    conn.close()

    barrier_file = tmp_path / "barrier"
    barrier_file.write_text("init")
    worker = Path(__file__).parent / "_flock_worker.py"

    p_approve = subprocess.Popen(
        [sys.executable, str(worker), str(db_path), str(plugins_dir),
         "approve", str(approval_id), str(barrier_file)],
        stdin=subprocess.DEVNULL)
    p_reject = subprocess.Popen(
        [sys.executable, str(worker), str(db_path), str(plugins_dir),
         "reject", str(approval_id), str(barrier_file)],
        stdin=subprocess.DEVNULL)

    p_approve.wait(timeout=15)
    p_reject.wait(timeout=15)
    assert p_approve.returncode == 0
    assert p_reject.returncode == 0

    result_file = Path(str(barrier_file) + ".reject_result")
    assert result_file.read_text() == "AlreadyDecidedError"

    conn2 = db_store.connect(db_path)
    row = conn2.execute("SELECT status FROM approval_requests WHERE id=?",
                        (approval_id,)).fetchone()
    assert row["status"] == "approved"  # CAS 敗者 (reject) が live/DB を変えない
    live = plugins_dir / "sma"
    assert live.is_symlink()  # approve が完遂している


def test_same_name_different_content_hash_approvals_are_independent(tmp_path, monkeypatch):
    """killer (§5 変異リスト逐語、§8.1-39): 後発決定 key を `name` だけに
    する変異を殺す — 同名 (`sma`) 別 `content_hash` の 2 pending approval
    が互いに干渉しない (`reject_candidate` で片方を決定してもう片方は
    pending のまま)。"""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / ".locks").mkdir()
    db_path = tmp_path / "agentic.db"
    conn = db_store.connect(db_path)
    db_store.init_db(conn)
    settings = load_settings(Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)

    # B: plugin.py の中身 v1 (content_hash=h1)
    dir_b = plugins_dir / "_staging" / "1" / "sma"
    dir_b.mkdir(parents=True)
    (dir_b / "plugin.py").write_text(
        "def compute(df, params):\n    return {'v': 1.0}\n")
    (dir_b / "config.yaml").write_text("kind: indicator\noutputs: [v]\n")
    (dir_b / "test_plugin.py").write_text("def test_x():\n    pass\n")
    approval_b = switch.submit_candidate(
        conn, name="sma", staging_dir=plugins_dir / "_staging" / "1",
        candidate_origin="staging", mission_id=1, backlog_id=None,
        settings=settings, now=NOW)

    # C: plugin.py の中身 v2 (content_hash=h2 — B とは異なる、別 mission の staging)
    dir_c = plugins_dir / "_staging" / "2" / "sma"
    dir_c.mkdir(parents=True)
    (dir_c / "plugin.py").write_text(
        "def compute(df, params):\n    return {'v': 2.0}\n")
    (dir_c / "config.yaml").write_text("kind: indicator\noutputs: [v]\n")
    (dir_c / "test_plugin.py").write_text("def test_x():\n    pass\n")
    approval_c = switch.submit_candidate(
        conn, name="sma", staging_dir=plugins_dir / "_staging" / "2",
        candidate_origin="staging", mission_id=2, backlog_id=None,
        settings=settings, now=NOW)

    import json as _json
    row_b = conn.execute("SELECT payload_json FROM approval_requests WHERE id=?",
                         (approval_b,)).fetchone()
    row_c = conn.execute("SELECT payload_json FROM approval_requests WHERE id=?",
                         (approval_c,)).fetchone()
    hash_b = _json.loads(row_b["payload_json"])["content_hash"]
    hash_c = _json.loads(row_c["payload_json"])["content_hash"]
    assert hash_b != hash_c  # 前提: 別 content_hash であること

    switch.reject_candidate(conn, approval_c, decided_by="human",
                            reason="not needed", now=NOW,
                            plugins_root=plugins_dir)  # B-1

    status_b = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                            (approval_b,)).fetchone()["status"]
    status_c = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                            (approval_c,)).fetchone()["status"]
    assert status_c == "rejected"
    assert status_b == "pending"  # 別 content_hash の B は無関係のまま (D4 の pin)
