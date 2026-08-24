"""improvement_runs CRUD 拡張 (設計書 §4.1、裁定7)。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agentic_fx.store import backlog, improve_runs, missions
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def test_start_with_backlog_id_none_and_mission_id_for_tx0(tmp_path):
    """裁定7: Tx-0 では backlog_id=None・mission_id 必須で呼ぶ。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    mid = missions.start(c, "improve", "local", "m", now=NOW)
    rid = improve_runs.start(c, None, now=NOW, mission_id=mid)
    row = c.execute("SELECT backlog_id, mission_id FROM improvement_runs "
                    "WHERE id=?", (rid,)).fetchone()
    assert row["backlog_id"] is None
    assert row["mission_id"] == mid


def test_mission_id_unique_among_new_rows(tmp_path):
    """`improvement_runs.mission_id` は部分 UNIQUE (§4.1 Tx-0)。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    mid = missions.start(c, "improve", "local", "m", now=NOW)
    improve_runs.start(c, None, now=NOW, mission_id=mid)
    with pytest.raises(Exception):  # sqlite3.IntegrityError
        improve_runs.start(c, None, now=NOW, mission_id=mid)


def test_multiple_null_mission_id_rows_allowed(tmp_path):
    """既存移行行 (mission_id=NULL) は複数許される — 部分 UNIQUE は
    `WHERE mission_id IS NOT NULL` のみに効く。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    r1 = improve_runs.start(c, None, now=NOW)  # mission_id 省略 = None
    r2 = improve_runs.start(c, None, now=NOW)
    assert r1 != r2  # 例外にならない


def test_bind_backlog_sets_backlog_id_on_run(tmp_path):
    """裁定7: Tx-1 で CAS 勝者のみが呼ぶ。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    bid = backlog.add(c, "idea", "user", NOW)
    mid = missions.start(c, "improve", "local", "m", now=NOW)
    rid = improve_runs.start(c, None, now=NOW, mission_id=mid)
    improve_runs.bind_backlog(c, rid, bid)
    row = c.execute("SELECT backlog_id FROM improvement_runs WHERE id=?",
                    (rid,)).fetchone()
    assert row["backlog_id"] == bid


def test_bind_backlog_only_affects_targeted_run(tmp_path):
    """L07: `bind_backlog` の `WHERE id=?` を `WHERE 1=1` にしても検出
    されない — 既存テスト群は全て 1 DB あたり run を 1 本しか作らない。
    ここでは run を 2 本作り、片方だけ bind してもう片方の backlog_id が
    None のままであることを確認する。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    bid = backlog.add(c, "idea", "user", NOW)
    rid1 = improve_runs.start(c, None, now=NOW)
    rid2 = improve_runs.start(c, None, now=NOW)
    improve_runs.bind_backlog(c, rid1, bid)
    row1 = c.execute("SELECT backlog_id FROM improvement_runs WHERE id=?",
                     (rid1,)).fetchone()
    row2 = c.execute("SELECT backlog_id FROM improvement_runs WHERE id=?",
                     (rid2,)).fetchone()
    assert row1["backlog_id"] == bid
    assert row2["backlog_id"] is None


def test_bind_backlog_commit_false_leaves_transaction_open(tmp_path):
    c = connect(tmp_path / "t.db"); init_db(c)
    bid = backlog.add(c, "idea", "user", NOW)
    rid = improve_runs.start(c, None, now=NOW)
    c.execute("BEGIN IMMEDIATE")
    improve_runs.bind_backlog(c, rid, bid, commit=False)
    c.rollback()
    row = c.execute("SELECT backlog_id FROM improvement_runs WHERE id=?",
                    (rid,)).fetchone()
    assert row["backlog_id"] is None


def test_finish_writes_report_state(tmp_path):
    c = connect(tmp_path / "t.db"); init_db(c)
    rid = improve_runs.start(c, None, now=NOW)
    improve_runs.finish(c, rid, result="report", now=NOW,
                        report_path="reports/x.md", report_state="prepared")
    row = c.execute("SELECT report_state FROM improvement_runs WHERE id=?",
                    (rid,)).fetchone()
    assert row["report_state"] == "prepared"


def test_finish_default_report_state_is_none(tmp_path):
    """既存呼び出し (report_state を渡さない) は 'none' — 承認申請のみの
    Mission (report を書かない経路) の既定と一致する。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    bid = backlog.add(c, "idea", "user", NOW)
    rid = improve_runs.start(c, bid, NOW)
    improve_runs.finish(c, rid, result="approval", now=NOW, approval_id=1)
    row = c.execute("SELECT report_state FROM improvement_runs WHERE id=?",
                    (rid,)).fetchone()
    assert row["report_state"] == "none"


def test_start_commit_false_leaves_transaction_open(tmp_path):
    c = connect(tmp_path / "t.db"); init_db(c)
    c.execute("BEGIN IMMEDIATE")
    rid = improve_runs.start(c, None, now=NOW, commit=False)
    c.rollback()
    row = c.execute("SELECT * FROM improvement_runs WHERE id=?", (rid,)).fetchone()
    assert row is None


# precheck 2026-08-22: T8-m11 -- set_report_state was untested. Add a minimal pin.
def test_set_report_state_updates_state_and_path(tmp_path):
    """report_path is updated together with report_state when given."""
    c = connect(tmp_path / "t.db"); init_db(c)
    rid = improve_runs.start(c, None, now=NOW)
    improve_runs.set_report_state(c, rid, "published", report_path="reports/x.md")
    row = c.execute("SELECT report_state, report_path FROM improvement_runs "
                    "WHERE id=?", (rid,)).fetchone()
    assert row["report_state"] == "published"
    assert row["report_path"] == "reports/x.md"


def test_set_report_state_failed_clears_report_path_even_when_not_passed(tmp_path):
    """report_state='failed' updates even without an explicit report_path."""
    c = connect(tmp_path / "t.db"); init_db(c)
    rid = improve_runs.start(c, None, now=NOW)
    improve_runs.set_report_state(c, rid, "prepared", report_path="reports/x.md")
    improve_runs.set_report_state(c, rid, "failed")
    row = c.execute("SELECT report_state, report_path FROM improvement_runs "
                    "WHERE id=?", (rid,)).fetchone()
    assert row["report_state"] == "failed"
    assert row["report_path"] is None
