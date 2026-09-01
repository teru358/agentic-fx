from datetime import datetime, timezone

import pytest

from agentic_fx.store import backlog, improve_runs
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def test_backlog_and_run(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    bid = backlog.add(c, "ATR ベースの SL 幅", "user", NOW)
    assert backlog.list_open(c)[0]["idea"].startswith("ATR")
    backlog.set_status(c, bid, "selected", NOW)
    assert backlog.list_open(c) == []
    rid = improve_runs.start(c, bid, NOW)
    improve_runs.finish(c, rid, result="report", now=NOW,
                        report_path="reports/improve-2026-07-26.md")


# round2 最終是正 A6 (2026-08-29、verified-local-round2.md A6): #8 是正の
# 「正規化は Python 側 1 箇所、全 INSERT 経路が idea_norm を書く」の
# うち CLI/手動追加経路 (`backlog.add`) だけが観測されていなかった。
def test_add_writes_python_normalized_idea_norm(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    bid = backlog.add(c, "  IMPROVE X\n", "cli", NOW)
    row = c.execute(
        "SELECT idea, idea_norm FROM improvement_backlog WHERE id=?",
        (bid,)).fetchone()
    assert row["idea"] == "  IMPROVE X\n"      # 原文は保持
    assert row["idea_norm"] == "improve x"     # 正規形は Python 側と同一


def test_finish_no_longer_accepts_pr_url(tmp_path):
    """Task 19: pr_url は improvement_runs から落ちたため finish() の
    引数からも外れている (渡すと TypeError)。"""
    c = connect(tmp_path / "t.db")
    init_db(c)
    bid = backlog.add(c, "x", "user", NOW)
    rid = improve_runs.start(c, bid, NOW)
    with pytest.raises(TypeError):
        improve_runs.finish(c, rid, result="report", now=NOW,
                            pr_url="https://example/pr/1")


def test_select_for_mission_cas_winner_gets_true_and_transitions_to_selected(tmp_path):
    """§4.1 Tx-1・裁定7: rowcount=1 の側が True。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    bid = backlog.add(c, "idea", "user", NOW)
    ok = backlog.select_for_mission(c, bid, now=NOW)
    assert ok is True
    row = c.execute("SELECT status, attempts FROM improvement_backlog WHERE id=?",
                    (bid,)).fetchone()
    assert row["status"] == "selected"
    assert row["attempts"] == 1


def test_select_for_mission_cas_loser_gets_false_and_does_not_transition(tmp_path):
    """2 接続同時選択で敗者は False・状態は変わらない (二重承認申請を防ぐ CAS の核)。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    bid = backlog.add(c, "idea", "user", NOW)
    assert backlog.select_for_mission(c, bid, now=NOW) is True
    assert backlog.select_for_mission(c, bid, now=NOW) is False
    row = c.execute("SELECT attempts FROM improvement_backlog WHERE id=?",
                    (bid,)).fetchone()
    assert row["attempts"] == 1  # 敗者側は attempts を増やさない


def test_select_for_mission_accepts_open_and_observation_not_others(tmp_path):
    """`open|observation` からのみ選択できる (§4.3 表の遷移元)。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    bid = backlog.add(c, "idea", "user", NOW)
    backlog.set_status(c, bid, "done", NOW)
    assert backlog.select_for_mission(c, bid, now=NOW) is False


def test_select_for_mission_from_observation_succeeds(tmp_path):
    c = connect(tmp_path / "t.db"); init_db(c)
    bid = backlog.add(c, "idea", "user", NOW)
    backlog.set_status(c, bid, "observation", NOW, last_result="mission_failed:timeout")
    assert backlog.select_for_mission(c, bid, now=NOW) is True


def test_list_open_includes_observation(tmp_path):
    """`list_open` = `open|observation` (R8: 失敗を「悪い」と記録しない)。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    b1 = backlog.add(c, "a", "user", NOW)
    b2 = backlog.add(c, "b", "user", NOW)
    backlog.set_status(c, b2, "observation", NOW, last_result="insufficient_trades:5")
    ids = {r["id"] for r in backlog.list_open(c)}
    assert ids == {b1, b2}


def test_list_open_excludes_selected_done_rejected(tmp_path):
    c = connect(tmp_path / "t.db"); init_db(c)
    b1 = backlog.add(c, "a", "user", NOW)
    b2 = backlog.add(c, "b", "user", NOW)
    b3 = backlog.add(c, "c", "user", NOW)
    backlog.set_status(c, b1, "selected", NOW)
    backlog.set_status(c, b2, "done", NOW, last_result="approved:1")
    backlog.set_status(c, b3, "rejected", NOW, last_result="human_rejected")
    assert backlog.list_open(c) == []


def test_note_is_not_open_or_selectable_and_is_listed_as_note(tmp_path):
    c = connect(tmp_path / "t.db"); init_db(c)
    bid = backlog.add(c, "known constraint", "agent", NOW)
    backlog.set_status(c, bid, "note", NOW)

    assert backlog.list_open(c) == []
    assert backlog.select_for_mission(c, bid, now=NOW) is False
    assert [row["id"] for row in backlog.list_notes(c)] == [bid]


def test_list_notes_filters_mixed_backlog_statuses(tmp_path):
    c = connect(tmp_path / "mixed.db"); init_db(c)
    ids = {}
    for status in ("open", "observation", "selected", "note", "rejected"):
        ids[status] = backlog.add(c, status, "agent", NOW)
        backlog.set_status(c, ids[status], status, NOW)
    assert [row["id"] for row in backlog.list_notes(c)] == [ids["note"]]


def test_set_status_writes_last_result(tmp_path):
    c = connect(tmp_path / "t.db"); init_db(c)
    bid = backlog.add(c, "a", "user", NOW)
    backlog.set_status(c, bid, "observation", NOW, last_result="report_failed:disk_full")
    row = c.execute("SELECT last_result FROM improvement_backlog WHERE id=?",
                    (bid,)).fetchone()
    assert row["last_result"] == "report_failed:disk_full"


def test_set_status_without_last_result_clears_existing_value(tmp_path):
    """M14 (段 0 Minor): `last_result=?` を `COALESCE(?, last_result)` に
    する変異 (None なら据え置き) が red になる pin — docstring m1 が明記
    する「常に上書きする。渡さない呼び出しは NULL でクリアする」非対称
    契約そのものを見る。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    bid = backlog.add(c, "a", "user", NOW)
    backlog.set_status(c, bid, "observation", NOW, last_result="report_failed:disk_full")
    backlog.set_status(c, bid, "open", NOW)  # last_result 未指定 (既定 None)
    row = c.execute("SELECT last_result FROM improvement_backlog WHERE id=?",
                    (bid,)).fetchone()
    assert row["last_result"] is None


def test_set_status_commit_false_does_not_commit(tmp_path):
    """`commit=False` は呼び出し側の tx に留める (Task 8 全体の設計不変条件)。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    bid = backlog.add(c, "a", "user", NOW)
    c.execute("BEGIN IMMEDIATE")
    backlog.set_status(c, bid, "selected", NOW, commit=False)
    c.rollback()
    row = c.execute("SELECT status FROM improvement_backlog WHERE id=?",
                    (bid,)).fetchone()
    assert row["status"] == "open"  # rollback で巻き戻る = commit していなかった証拠


@pytest.mark.parametrize("current,outcome,expected_status,expected_last_result_prefix", [
    ("selected", "done", "done", "report:"),
    ("selected", "report_failed", "observation", "report_failed:"),
    ("selected", "gate_failed", "observation", "gate_failed:"),
    ("selected", "insufficient_trades", "observation", "insufficient_trades:"),
    ("selected", "unsupported_in_plan10", "observation", "unsupported_in_plan10:"),
    ("selected", "approved", "done", "approved:"),
    ("selected", "rejected", "observation", "rejected:"),
    ("selected", "expired", "observation", "expired"),
    ("selected", "invalidated", "observation", "invalidated"),
    ("selected", "mission_failed", "observation", "mission_failed:"),
    ("selected", "commit_failed", "observation", "commit_failed"),
    ("selected", "interrupted", "observation", "interrupted"),
])
def test_apply_approval_outcome_state_machine_table(
        tmp_path, current, outcome, expected_status, expected_last_result_prefix):
    """§4.3 状態機械表を逐語でテーブル駆動テストにする (§8.1-27)。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    bid = backlog.add(c, "a", "user", NOW)
    backlog.set_status(c, bid, current, NOW)
    backlog.apply_approval_outcome(c, backlog_id=bid, outcome=outcome,
                                   reason=None, now=NOW)
    row = c.execute("SELECT status, last_result FROM improvement_backlog "
                    "WHERE id=?", (bid,)).fetchone()
    assert row["status"] == expected_status
    assert row["last_result"].startswith(expected_last_result_prefix)


def test_apply_approval_outcome_done_to_observation_only_for_report_state_failed(tmp_path):
    """§4.3: `done → observation` は `report_state` が `failed` に遷移する
    ときだけ許す (codex 6 周目 I2)。ここでは outcome='report_publish_failed'
    という専用 outcome 名で他の done→observation 遷移と区別する。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    bid = backlog.add(c, "a", "user", NOW)
    backlog.set_status(c, bid, "done", NOW, last_result="report:reports/x.md")
    backlog.apply_approval_outcome(c, backlog_id=bid,
                                   outcome="report_publish_failed",
                                   reason="disk_full", now=NOW)
    row = c.execute("SELECT status, last_result FROM improvement_backlog "
                    "WHERE id=?", (bid,)).fetchone()
    assert row["status"] == "observation"
    assert row["last_result"] == "report_failed:disk_full"


def test_apply_approval_outcome_backlog_id_none_is_noop(tmp_path):
    """legacy 行 (payload に backlog_id 無し) の invalidated 遷移は no-op。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    backlog.apply_approval_outcome(c, backlog_id=None, outcome="invalidated",
                                   reason=None, now=NOW)  # 例外にならない


# round2 #10 是正 (2026-08-29、verified-round2.md #10): `apply_approval_
# outcome` が `set_status` の bool (fail-open 防止) を捨てていた。
# `backlog.set_status` の docstring が自ら宣言する契約 (False=該当行が
# 存在しなかった) を可視化する — raise はしない (採らない形: raise だと
# apply_decision の1txがrollbackし、expire/invalidate経路まで
# pendingに固着する durable stuck を新設してしまう)。
def test_apply_approval_outcome_logs_when_backlog_row_missing(tmp_path, caplog):
    import logging
    from agentic_fx.store import approvals

    c = connect(tmp_path / "t.db")
    init_db(c)
    missing_backlog_id = 999999
    approval_id = approvals.create(
        c, kind="plugin", payload={"backlog_id": missing_backlog_id}, now=NOW)

    with caplog.at_level(logging.ERROR, logger="agentic_fx.store.backlog"):
        approvals.apply_decision(
            c, approval_id, "approved", decided_by="human", now=NOW)

    assert any(str(missing_backlog_id) in r.message for r in caplog.records)
    row = c.execute(
        "SELECT status FROM approval_requests WHERE id=?",
        (approval_id,)).fetchone()
    # 決定は止まらない (= #4 型の固着を新設していないことを固定する)。
    assert row["status"] == "approved"


def test_apply_approval_outcome_no_error_log_for_existing_backlog_row(
        tmp_path, caplog):
    import logging
    from agentic_fx.store import approvals

    c = connect(tmp_path / "t.db")
    init_db(c)
    bid = backlog.add(c, "a", "user", NOW)
    approval_id = approvals.create(
        c, kind="plugin", payload={"backlog_id": bid}, now=NOW)

    with caplog.at_level(logging.ERROR, logger="agentic_fx.store.backlog"):
        approvals.apply_decision(
            c, approval_id, "approved", decided_by="human", now=NOW)

    assert caplog.records == []
    row = c.execute(
        "SELECT status FROM approval_requests WHERE id=?",
        (approval_id,)).fetchone()
    assert row["status"] == "approved"
    b = c.execute(
        "SELECT status FROM improvement_backlog WHERE id=?", (bid,)).fetchone()
    assert b["status"] == "done"


def test_human_reject_and_reopen(tmp_path):
    """人間操作: `observation/open → rejected`、`done/rejected → open`。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    bid = backlog.add(c, "a", "user", NOW)
    backlog.set_status(c, bid, "rejected", NOW, last_result="human_rejected")
    row = c.execute("SELECT status FROM improvement_backlog WHERE id=?",
                    (bid,)).fetchone()
    assert row["status"] == "rejected"
    backlog.set_status(c, bid, "open", NOW, last_result="reopened")
    row = c.execute("SELECT status FROM improvement_backlog WHERE id=?",
                    (bid,)).fetchone()
    assert row["status"] == "open"
