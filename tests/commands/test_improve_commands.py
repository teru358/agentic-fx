"""Commands.dispatch の improve/backlog/policy コマンド (プラン10 Task9-7)。"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.commands import Commands
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.store import backlog, db as db_mod
from agentic_fx.store.state import StateStore


class _FixedClock:
    def __init__(self, t):
        self._t = t
    def now(self):
        return self._t


def _fake_settings():
    """最小限のダミー設定。"""
    class Settings:
        class Risk:
            risk_per_trade_pct = 1.0
            drawdown_kill_pct = 10.0
            daily_loss_limit_pct = 5.0
        risk = Risk()
        pairs = ["EURUSD"]
    return Settings()


@pytest.fixture
def commands(tmp_path):
    conn = db_mod.connect(tmp_path / "agentic.db")
    db_mod.init_db(conn)
    state = StateStore(tmp_path / "state.json")
    # StateStore は自動的に初期化される
    broker = PaperBroker(conn, _fake_settings(), _FixedClock(datetime(2026, 8, 22)))
    (tmp_path / "logs").mkdir(exist_ok=True)
    activity = ActivityLog(tmp_path / "logs" / "activity.log")

    class _SupervisorStub:
        def submit_manual(self):
            return 42

    cmds = Commands(conn=conn, state_store=state, broker=broker,
                    trade_loop=None, activity=activity,
                    log_dir=tmp_path / "logs", clock=_FixedClock(datetime(2026, 8, 22)))
    cmds.improve_supervisor = _SupervisorStub()  # 新規属性 (下記 Step 3)
    cmds.conn_improve = conn                      # backlog/policy 用の接続
    return cmds, conn, tmp_path


def test_improve_command_submits_manual_wave(commands):
    cmds, conn, _ = commands
    out = cmds.dispatch("improve")
    assert "42" in out


def test_improve_add_creates_open_backlog_row(commands):
    cmds, conn, _ = commands
    out = cmds.dispatch("improve add EURUSD の RSI 過熱判定を改善したい")
    assert "backlog" in out.lower() or "追加" in out
    rows = backlog.list_open(conn)
    assert any("EURUSD" in r["idea"] for r in rows)


def test_improve_add_without_text_returns_usage(commands):
    cmds, _, _ = commands
    out = cmds.dispatch("improve add")
    assert "usage" in out.lower()


def test_backlog_reject_closes_open_row(commands):
    cmds, conn, _ = commands
    bid = backlog.add(conn, idea="test idea", source="user",
                      now=datetime(2026, 8, 22))
    out = cmds.dispatch(f"backlog reject {bid}")
    row = dict(conn.execute(
        "SELECT status, last_result FROM improvement_backlog WHERE id=?", (bid,)
    ).fetchone())
    assert row["status"] == "rejected"
    assert row["last_result"] == "human_rejected"


def test_backlog_reopen_returns_done_to_open(commands):
    cmds, conn, _ = commands
    bid = backlog.add(conn, idea="test idea 2", source="user",
                      now=datetime(2026, 8, 22))
    backlog.set_status(conn, bid, "done", datetime(2026, 8, 22),
                       last_result="report:x", commit=True)
    out = cmds.dispatch(f"backlog reopen {bid}")
    row = dict(conn.execute(
        "SELECT status, last_result FROM improvement_backlog WHERE id=?", (bid,)
    ).fetchone())
    assert row["status"] == "open"
    assert row["last_result"] == "reopened"


def test_backlog_reopen_promotes_note_to_open(commands):
    cmds, conn, _ = commands
    now = datetime(2026, 8, 22)
    bid = backlog.add(conn, "fact becoming task", "agent", now)
    backlog.set_status(conn, bid, "note", now, last_result="human_noted")
    assert "open" in cmds.dispatch(f"backlog reopen {bid}")
    row = conn.execute("SELECT status,last_result FROM improvement_backlog WHERE id=?", (bid,)).fetchone()
    assert dict(row) == {"status": "open", "last_result": "human_reopened"}


def test_backlog_reopen_rejects_from_non_terminal_status(commands):
    """検収 B3 (2026-08-22): §4.3 の「done/rejected → open のみ」制約を
    commands.py 側の遷移ガードで強制する (反転 — 旧テストはガード不在を
    assert していた)。`selected` (Mission 実行中の行) から reopen を打つと
    `open` に落ち、別 Mission の `select_for_mission` CAS が成功しうる
    (§4 が挙げる二重承認申請への到達経路)。ガードは拒否メッセージを返し、
    status/last_result を不変に保つこと。"""
    cmds, conn, _ = commands
    bid = backlog.add(conn, idea="test idea 3", source="user",
                      now=datetime(2026, 8, 22))
    ok = backlog.select_for_mission(conn, bid, now=datetime(2026, 8, 22),
                                    commit=True)
    assert ok
    row_before = dict(conn.execute(
        "SELECT status, last_result FROM improvement_backlog WHERE id=?", (bid,)
    ).fetchone())
    assert row_before["status"] == "selected"

    out = cmds.dispatch(f"backlog reopen {bid}")

    assert "selected" in out
    row_after = dict(conn.execute(
        "SELECT status, last_result FROM improvement_backlog WHERE id=?", (bid,)
    ).fetchone())
    assert row_after["status"] == "selected"
    assert row_after["last_result"] == row_before["last_result"]


def test_backlog_reject_rejects_from_non_terminal_status(commands):
    """検収 B3: reject 側の同型テスト。`selected` からの reject を拒否し
    status/last_result を不変に保つ (§4.3: reject は open|observation
    からのみ)。"""
    cmds, conn, _ = commands
    bid = backlog.add(conn, idea="test idea 4", source="user",
                      now=datetime(2026, 8, 22))
    ok = backlog.select_for_mission(conn, bid, now=datetime(2026, 8, 22),
                                    commit=True)
    assert ok
    row_before = dict(conn.execute(
        "SELECT status, last_result FROM improvement_backlog WHERE id=?", (bid,)
    ).fetchone())
    assert row_before["status"] == "selected"

    out = cmds.dispatch(f"backlog reject {bid}")

    assert "selected" in out
    row_after = dict(conn.execute(
        "SELECT status, last_result FROM improvement_backlog WHERE id=?", (bid,)
    ).fetchone())
    assert row_after["status"] == "selected"
    assert row_after["last_result"] == row_before["last_result"]


def test_backlog_reject_unknown_id_returns_not_found(commands):
    """検収 M-e: 存在しない id への reject/reopen が成功メッセージを返す
    (rowcount を見ない fail-open) のを閉じる。"""
    cmds, _, _ = commands
    out = cmds.dispatch("backlog reject 99999")
    assert "存在しません" in out


def test_backlog_reopen_unknown_id_returns_not_found(commands):
    cmds, _, _ = commands
    out = cmds.dispatch("backlog reopen 99999")
    assert "存在しません" in out


def test_policy_add_appends_to_directives_file(commands):
    cmds, _, root = commands
    cmds._policy_path = root / "policy" / "directives.md"  # 実装時は
                                                             # コンストラクタ
                                                             # 引数化する
                                                             # (下記申し送り)
    out = cmds.dispatch("policy add 新規指針テキスト")
    assert "新規指針テキスト" in cmds._policy_path.read_text(encoding="utf-8")
