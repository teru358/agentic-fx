from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.store import approvals, backlog
from agentic_fx.store.approvals import AlreadyDecidedError
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def _conn(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    return c


def test_create_and_decide(tmp_path):
    c = _conn(tmp_path)
    aid = approvals.create(c, "tech_plugin", {"path": "plugins/tech/rsi"}, NOW)
    assert len(approvals.pending(c)) == 1
    approvals.decide(c, aid, status="approved", decided_by="shell", now=NOW)
    assert approvals.pending(c) == []


def test_double_decide_rejected(tmp_path):
    c = _conn(tmp_path)
    aid = approvals.create(c, "news_source", {"url": "https://x"}, NOW)
    approvals.decide(c, aid, status="rejected", decided_by="shell", now=NOW,
                     reason="低品質")
    with pytest.raises(AlreadyDecidedError):
        approvals.decide(c, aid, status="approved", decided_by="shell", now=NOW)


def test_expire_due(tmp_path):
    c = _conn(tmp_path)
    approvals.create(c, "live_trade", {"pair": "USDJPY"}, NOW,
                     expires_at=NOW + timedelta(minutes=15))
    n = approvals.expire_due(c, NOW + timedelta(minutes=16))
    assert n == 1
    assert approvals.pending(c) == []


def test_pending_filter_by_kind(tmp_path):
    c = _conn(tmp_path)
    approvals.create(c, "tech_plugin", {}, NOW)
    approvals.create(c, "news_source", {}, NOW)
    assert len(approvals.pending(c, kind="tech_plugin")) == 1


def test_decide_rejects_expired_approval_and_marks_expired(tmp_path):
    c = _conn(tmp_path)
    aid = approvals.create(c, "live_trade", {"pair": "USDJPY"}, NOW,
                           expires_at=NOW + timedelta(minutes=15))
    later = NOW + timedelta(minutes=16)
    with pytest.raises(AlreadyDecidedError):
        approvals.decide(c, aid, status="approved", decided_by="shell", now=later)
    row = c.execute("SELECT status FROM approval_requests WHERE id=?", (aid,)).fetchone()
    assert row["status"] == "expired"


def test_decide_boundary_expires_at_equal_now_is_still_valid(tmp_path):
    c = _conn(tmp_path)
    aid = approvals.create(c, "live_trade", {"pair": "USDJPY"}, NOW,
                           expires_at=NOW + timedelta(minutes=15))
    boundary = NOW + timedelta(minutes=15)
    approvals.decide(c, aid, status="approved", decided_by="shell", now=boundary)
    row = c.execute("SELECT status FROM approval_requests WHERE id=?", (aid,)).fetchone()
    assert row["status"] == "approved"


def test_decide_rejects_pending_status_value(tmp_path):
    c = _conn(tmp_path)
    aid = approvals.create(c, "tech_plugin", {"path": "x"}, NOW)
    with pytest.raises(ValueError):
        approvals.decide(c, aid, status="pending", decided_by="shell", now=NOW)
    row = c.execute(
        "SELECT status, decided_by, decided_at FROM approval_requests WHERE id=?",
        (aid,)).fetchone()
    assert row["status"] == "pending"
    assert row["decided_by"] is None
    assert row["decided_at"] is None


def test_decide_rejects_unknown_status_value(tmp_path):
    c = _conn(tmp_path)
    aid = approvals.create(c, "tech_plugin", {"path": "x"}, NOW)
    with pytest.raises(ValueError):
        approvals.decide(c, aid, status="invalidated", decided_by="shell", now=NOW)
    row = c.execute("SELECT status FROM approval_requests WHERE id=?", (aid,)).fetchone()
    assert row["status"] == "pending"


def test_apply_decision_approved_cas_and_backlog_transition(tmp_path):
    """§4.3: approval CAS + backlog 遷移が 1 tx。"""
    c = _conn(tmp_path)
    bid = backlog.add(c, "idea", "user", NOW)
    backlog.select_for_mission(c, bid, now=NOW)
    aid = approvals.create(c, "plugin", {"backlog_id": bid, "name": "x"}, NOW)
    approvals.apply_decision(c, aid, "approved", decided_by="shell", now=NOW)
    approval = c.execute("SELECT status FROM approval_requests WHERE id=?",
                         (aid,)).fetchone()
    assert approval["status"] == "approved"
    row = c.execute("SELECT status, last_result FROM improvement_backlog "
                    "WHERE id=?", (bid,)).fetchone()
    assert row["status"] == "done"
    assert row["last_result"] == f"approved:{aid}"


def test_apply_decision_rejected_backlog_to_observation(tmp_path):
    c = _conn(tmp_path)
    bid = backlog.add(c, "idea", "user", NOW)
    backlog.select_for_mission(c, bid, now=NOW)
    aid = approvals.create(c, "plugin", {"backlog_id": bid, "name": "x"}, NOW)
    approvals.apply_decision(c, aid, "rejected", decided_by="shell", now=NOW,
                             reason="not useful")
    row = c.execute("SELECT status, last_result FROM improvement_backlog "
                    "WHERE id=?", (bid,)).fetchone()
    assert row["status"] == "observation"
    assert row["last_result"] == "rejected:not useful"


def test_apply_decision_non_pending_cas_rowcount_zero_raises_with_zero_side_effect(tmp_path):
    """裁定1: CAS が rowcount=0 なら副作用ゼロで例外 (先に expire_due を
    単独 tx で呼ぶ運用と対になる — apply_decision 自身は expired 化しない)。"""
    c = _conn(tmp_path)
    bid = backlog.add(c, "idea", "user", NOW)
    backlog.select_for_mission(c, bid, now=NOW)
    aid = approvals.create(c, "plugin", {"backlog_id": bid, "name": "x"}, NOW)
    approvals.apply_decision(c, aid, "approved", decided_by="shell", now=NOW)
    with pytest.raises(AlreadyDecidedError):
        approvals.apply_decision(c, aid, "rejected", decided_by="shell", now=NOW)
    # 副作用ゼロ: backlog は最初の決定 (approved -> done) のまま変化しない
    row = c.execute("SELECT status FROM improvement_backlog WHERE id=?",
                    (bid,)).fetchone()
    assert row["status"] == "done"


def test_apply_decision_backlog_id_absent_from_payload_is_noop_for_backlog(tmp_path):
    """payload に backlog_id が無い承認 (legacy 行・非 plugin kind 由来) は
    approval CAS だけ成立し backlog には触れない。"""
    c = _conn(tmp_path)
    aid = approvals.create(c, "plugin", {"name": "x"}, NOW)  # backlog_id なし
    approvals.apply_decision(c, aid, "approved", decided_by="shell", now=NOW)
    approval = c.execute("SELECT status FROM approval_requests WHERE id=?",
                         (aid,)).fetchone()
    assert approval["status"] == "approved"


def test_apply_decision_commit_false_leaves_transaction_open(tmp_path):
    c = _conn(tmp_path)
    bid = backlog.add(c, "idea", "user", NOW)
    backlog.select_for_mission(c, bid, now=NOW)
    aid = approvals.create(c, "plugin", {"backlog_id": bid, "name": "x"}, NOW)
    c.execute("BEGIN IMMEDIATE")
    approvals.apply_decision(c, aid, "approved", decided_by="shell", now=NOW,
                             commit=False)
    c.rollback()
    approval = c.execute("SELECT status FROM approval_requests WHERE id=?",
                         (aid,)).fetchone()
    assert approval["status"] == "pending"


def test_create_commit_false_leaves_transaction_open(tmp_path):
    c = _conn(tmp_path)
    c.execute("BEGIN IMMEDIATE")
    aid = approvals.create(c, "plugin", {"x": 1}, NOW, commit=False)
    c.rollback()
    row = c.execute("SELECT * FROM approval_requests WHERE id=?", (aid,)).fetchone()
    assert row is None


def test_expire_due_commit_false_expires_non_plugin_kind_individually(tmp_path):
    """expire_due の commit=False 版は行を 1 件ずつ列挙して処理する
    (呼び出し元 tx に混ぜられる形。戻り値の意味は既存の一括 UPDATE 版と
    同じ = 直接 expired 化した件数)。R-i8 (統合裁定): kind='plugin' は
    ここでは対象外 (列挙のみ) なので、非 plugin kind (tech_plugin/
    news_source/live_trade) の 2 行だけを直接 expired 化する。"""
    c = _conn(tmp_path)
    aid1 = approvals.create(c, "tech_plugin", {"x": 1}, NOW, expires_at=NOW)
    aid2 = approvals.create(c, "news_source", {"x": 2}, NOW, expires_at=NOW)
    later = NOW + timedelta(hours=1)  # m4: fragile hour-wrap pattern を timedelta に統一
    n = approvals.expire_due(c, later, commit=False)
    c.commit()
    assert n == 2
    for aid in (aid1, aid2):
        row = c.execute("SELECT status FROM approval_requests WHERE id=?",
                        (aid,)).fetchone()
        assert row["status"] == "expired"


def test_list_due_for_expiry_enumerates_without_state_change(tmp_path):
    """R-i8: 列挙のみ変種は行を返すだけで status を変えない (E 束の
    process_expired_approvals が依存する契約)。"""
    c = _conn(tmp_path)
    approvals.create(c, "plugin", {"name": "x"}, NOW, expires_at=NOW)
    approvals.create(c, "tech_plugin", {"x": 1}, NOW, expires_at=NOW)
    later = NOW + timedelta(hours=1)  # m4: fragile hour-wrap pattern を timedelta に統一
    rows = approvals.list_due_for_expiry(c, now=later, kind="plugin")
    assert [r["kind"] for r in rows] == ["plugin"]
    statuses = {r["status"] for r in c.execute(
        "SELECT status FROM approval_requests").fetchall()}
    assert statuses == {"pending"}   # 状態は一切変わらない


# precheck 2026-08-22: T8-B11 (R9)
def test_apply_decision_approve_on_expired_pending_is_rejected(tmp_path):
    """R9: 既存 decide の expires_at 述語を維持する (fail closed)。期限切れ
    pending への approve/reject は AlreadyDecidedError で拒否し、副作用ゼロ
    (status は pending のまま — apply_decision 自身は expired 化しない)。"""
    c = _conn(tmp_path)
    aid = approvals.create(c, "plugin", {"name": "x"}, NOW, expires_at=NOW)
    later = NOW + timedelta(hours=1)  # m4: fragile hour-wrap pattern を timedelta に統一
    with pytest.raises(AlreadyDecidedError):
        approvals.apply_decision(c, aid, "approved", decided_by="shell", now=later)
    row = c.execute("SELECT status FROM approval_requests WHERE id=?",
                    (aid,)).fetchone()
    assert row["status"] == "pending"


def test_apply_decision_status_expired_succeeds_on_already_expired_pending(tmp_path):
    """`status='expired'` はクローズ系の遷移であり expires_at 述語の対象外
    (B5): 期限切れ pending でも apply_decision(status='expired') は成功する
    — Task 11 process_expired_approvals がこの経路で「本物の expired 行」を
    作る唯一の手段になる。"""
    c = _conn(tmp_path)
    aid = approvals.create(c, "plugin", {"name": "x"}, NOW, expires_at=NOW)
    later = NOW + timedelta(hours=1)  # m4: fragile hour-wrap pattern を timedelta に統一
    approvals.apply_decision(c, aid, "expired", decided_by="system", now=later)
    row = c.execute("SELECT status FROM approval_requests WHERE id=?",
                    (aid,)).fetchone()
    assert row["status"] == "expired"


def test_expire_due_skips_plugin_kind_pending_rows(tmp_path):
    """R-i8 (統合裁定): kind='plugin' の期限到来 pending は expire_due が
    直接 expired 化しない (列挙のみの列挙変種) — pending のまま残る。
    plugin kind の expired 化は Task 11 の `process_expired_approvals`
    (name ごとの plugin flock 下で「未完ジャーナル無し」を確認してから
    `apply_decision(status='expired')` を呼ぶ。未完ジャーナルがある行は
    次回再試行のためスキップされる) の責務。"""
    c = _conn(tmp_path)
    aid = approvals.create(c, "plugin", {"name": "x"}, NOW, expires_at=NOW)
    later = NOW + timedelta(hours=1)  # m4: fragile hour-wrap pattern を timedelta に統一
    n = approvals.expire_due(c, later, commit=False)
    c.commit()
    assert n == 0
    row = c.execute("SELECT status FROM approval_requests WHERE id=?",
                    (aid,)).fetchone()
    assert row["status"] == "pending"
