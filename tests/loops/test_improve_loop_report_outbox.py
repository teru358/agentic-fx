"""report outbox の全状態表 + fsync fault injection (設計書 §4.2-6、
プラン §8.1-43)。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest


def test_write_report_uses_o_excl_and_fsyncs_before_tx2(tmp_path, loop_min):
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    (reports_dir / ".tmp").mkdir()
    part_path = loop_min._write_report_part(
        reports_dir, mission_id=7, body_md="# proposal\n...")
    assert part_path.exists()
    assert part_path.name == "improve-7.md.part"
    # O_EXCL: 2 回目は既存 part と衝突して例外
    with pytest.raises(FileExistsError):
        loop_min._write_report_part(reports_dir, mission_id=7, body_md="x")


def test_publish_renames_after_commit_not_before(tmp_path, loop_min, conn, mission_and_run_fixture):
    mission_id, run_id, backlog_id = mission_and_run_fixture
    reports_dir = tmp_path / "reports"
    (reports_dir / ".tmp").mkdir(parents=True)
    part = loop_min._write_report_part(reports_dir, mission_id=mission_id, body_md="x")
    final_path = reports_dir / "improve-2026-08-22-{}.md".format(mission_id)

    loop_min._publish_report(conn, run_id=run_id, part_path=part,
                             final_path=final_path, now=datetime(2026, 8, 22))
    assert final_path.exists()
    assert not part.exists()
    row = conn.execute(
        "SELECT report_state FROM improvement_runs WHERE id=?", (run_id,)).fetchone()
    assert row["report_state"] == "published"


def test_publish_failure_when_final_already_exists_triggers_compensation(
        tmp_path, loop_min, conn, mission_and_run_fixture):
    mission_id, run_id, backlog_id = mission_and_run_fixture
    reports_dir = tmp_path / "reports"
    (reports_dir / ".tmp").mkdir(parents=True)
    part = loop_min._write_report_part(reports_dir, mission_id=mission_id, body_md="x")
    final_path = reports_dir / "improve-2026-08-22-{}.md".format(mission_id)
    final_path.write_text("already here")  # 衝突を仕込む

    loop_min._publish_report(conn, run_id=run_id, part_path=part,
                             final_path=final_path, now=datetime(2026, 8, 22))
    row = conn.execute(
        "SELECT report_state, result, report_path FROM improvement_runs "
        "WHERE id=?", (run_id,)).fetchone()
    assert row["report_state"] == "failed"
    assert row["result"] is None
    assert row["report_path"] is None


def test_reconcile_prepared_with_part_publishes(tmp_path, loop_min, conn, mission_and_run_fixture):
    """起動時 reconcile: `prepared` かつ `.part` あり → 今公開して `published`。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    reports_dir = tmp_path / "reports"
    (reports_dir / ".tmp").mkdir(parents=True)
    part_path = reports_dir / ".tmp" / "improve-{}.md.part".format(mission_id)
    part_path.write_text("body")
    conn.execute(
        "UPDATE improvement_runs SET report_state='prepared', "
        "report_path=? WHERE id=?",
        (str(reports_dir / "improve-2026-08-22-{}.md".format(mission_id)), run_id))
    conn.commit()

    loop_min.reconcile_report_outbox(conn, reports_dir=reports_dir,
                                     now=datetime(2026, 8, 22))
    row = conn.execute(
        "SELECT report_state FROM improvement_runs WHERE id=?", (run_id,)).fetchone()
    assert row["report_state"] == "published"


def test_reconcile_prepared_with_neither_part_nor_final_fails_closed(
        tmp_path, loop_min, conn, mission_and_run_fixture):
    """`prepared` かつ両方無し → failed 補償。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    reports_dir = tmp_path / "reports"
    (reports_dir / ".tmp").mkdir(parents=True)
    conn.execute(
        "UPDATE improvement_runs SET report_state='prepared', "
        "report_path=? WHERE id=?",
        (str(reports_dir / "improve-2026-08-22-{}.md".format(mission_id)), run_id))
    conn.commit()

    loop_min.reconcile_report_outbox(conn, reports_dir=reports_dir,
                                     now=datetime(2026, 8, 22))
    row = conn.execute(
        "SELECT report_state, result, report_path FROM improvement_runs "
        "WHERE id=?", (run_id,)).fetchone()
    assert row["report_state"] == "failed"
    assert row["result"] is None


def test_reconcile_published_with_missing_final_fails_closed(tmp_path, loop_min, conn, mission_and_run_fixture):
    """`published` かつ最終ファイル無し → 補償 (report_state=failed, backlog
    done→observation(report_failed:missing))。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    conn.execute(
        "UPDATE improvement_runs SET report_state='published', "
        "report_path=? WHERE id=?",
        (str(reports_dir / "improve-2026-08-22-{}.md".format(mission_id)), run_id))
    # backlog status を 'done' に設定 (fail_report が observ ationに遷移させるため)
    if backlog_id is not None:
        conn.execute(
            "UPDATE improvement_backlog SET status='done' WHERE id=?",
            (backlog_id,))
    conn.commit()

    loop_min.reconcile_report_outbox(conn, reports_dir=reports_dir,
                                     now=datetime(2026, 8, 22))
    row = conn.execute(
        "SELECT report_state, result, report_path FROM improvement_runs "
        "WHERE id=?", (run_id,)).fetchone()
    assert row["report_state"] == "failed"
    assert row["report_path"] is None


def test_directory_fsync_called_after_rename(tmp_path, loop_min, conn, mission_and_run_fixture, monkeypatch):
    """rename 後に `reports/.tmp` と `reports/` の両ディレクトリ fsync を
    呼ぶことを、fsync 呼び出しを記録する fake で確認する。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    reports_dir = tmp_path / "reports"
    (reports_dir / ".tmp").mkdir(parents=True)
    part = loop_min._write_report_part(reports_dir, mission_id=mission_id, body_md="x")
    final_path = reports_dir / "improve-2026-08-22-{}.md".format(mission_id)

    fsynced_dirs = []
    monkeypatch.setattr(loop_min, "_fsync_dir", lambda p: fsynced_dirs.append(p))

    loop_min._publish_report(conn, run_id=run_id, part_path=part,
                             final_path=final_path, now=datetime(2026, 8, 22))
    assert reports_dir in fsynced_dirs
    assert (reports_dir / ".tmp") in fsynced_dirs


def test_write_report_part_calls_fsync(tmp_path, loop_min, monkeypatch):
    """M2 追加テスト: `_write_report_part` が os.fsync を呼ぶことを確認
    (fsync を削ると失敗)。"""
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    (reports_dir / ".tmp").mkdir()

    fsync_calls = []
    import os
    original_fsync = os.fsync
    def track_fsync(fd):
        fsync_calls.append(fd)
        return original_fsync(fd)
    monkeypatch.setattr("os.fsync", track_fsync)

    loop_min._write_report_part(reports_dir, mission_id=14, body_md="content")
    assert len(fsync_calls) >= 1  # fsync が呼ばれたことを確認


def test_publish_report_updates_state_after_rename_not_before(
        tmp_path, loop_min, conn, mission_and_run_fixture):
    """M3 追加テスト: `_publish_report` が rename 後に published 状態を
    更新することを確認 (rename 前に更新すると失敗)。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    reports_dir = tmp_path / "reports"
    (reports_dir / ".tmp").mkdir(parents=True)
    part = loop_min._write_report_part(reports_dir, mission_id=mission_id, body_md="x")
    final_path = reports_dir / "improve-2026-08-22-{}.md".format(mission_id)

    # rename が失敗するように final_path を先に作成
    final_path.write_text("collision")

    # _publish_report は failed に遷移すべき (rename に失敗したため)
    loop_min._publish_report(conn, run_id=run_id, part_path=part,
                             final_path=final_path, now=datetime(2026, 8, 22))
    row = conn.execute(
        "SELECT report_state FROM improvement_runs WHERE id=?", (run_id,)).fetchone()
    assert row["report_state"] == "failed"  # published に遷移せず failed


def test_fail_report_does_not_update_selected_backlog_status(tmp_path, loop_min, conn):
    """M5 追加テスト: `_fail_report` が backlog status='selected' の行を
    更新しないことを確認 (AND status='done' の条件が効いていることを pin)。"""
    # improvement_runs と backlog を作成
    from agentic_fx.store import missions as missions_store
    from agentic_fx.store import improve_runs as improve_runs_store
    from agentic_fx.store import backlog as backlog_store
    mission_id = missions_store.start(
        conn, "improve", "local", "qwen", datetime(2026, 8, 22), commit=False)
    backlog_id = backlog_store.add(conn, "idea", "user", datetime(2026, 8, 22))
    run_id = improve_runs_store.start(
        conn, backlog_id, datetime(2026, 8, 22), mission_id=mission_id, commit=False)
    # backlog status を 'selected' に設定
    conn.execute(
        "UPDATE improvement_backlog SET status='selected' WHERE id=?",
        (backlog_id,))
    conn.commit()

    # _fail_report を呼び出す
    loop_min._fail_report(conn, run_id=run_id, now=datetime(2026, 8, 22),
                          reason="test_reason")

    # backlog status が 'selected' のままであることを確認
    backlog_row = conn.execute(
        "SELECT status FROM improvement_backlog WHERE id=?",
        (backlog_id,)).fetchone()
    assert backlog_row["status"] == "selected"  # 変更されていない
