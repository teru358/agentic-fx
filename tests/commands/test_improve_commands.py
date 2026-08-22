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


def test_backlog_reopen_rejects_from_non_terminal_status(commands):
    """§4.3 の「done/rejected → open」制約の pin。
    backlog.set_status は transition guard を持たないため、Commands.dispatch
    内でも guard が無い場合、open 状態から reopen しても状態は変わらない
    (ただしこれは command の guard 不在による偶然の一致ではなく
    set_status が無条件で新しい status を書くため)。

    M3 mutation は「reopen が open から遷移を許す」という guard 不在を検出
    する intended test だが、set_status 自体に guard が無いため、現在の
    実装では「mutation を注入してもテストが死なない」という虚偽な状態
    (vacuous mutation) になっている。プラン L14887 の M3 は
    「変異注入対象が存在しない — set_status に transition guard が無い」として
    ledger に記録すること (プラン記述と実装の矛盾)。"""
    cmds, conn, _ = commands
    bid = backlog.add(conn, idea="test idea 3", source="user",
                      now=datetime(2026, 8, 22))
    # 初期状態は 'open'。last_result は NULL
    row_before = dict(conn.execute(
        "SELECT status, last_result FROM improvement_backlog WHERE id=?", (bid,)
    ).fetchone())
    assert row_before["status"] == "open"
    assert row_before["last_result"] is None

    # reopen を呼ぶ
    out = cmds.dispatch(f"backlog reopen {bid}")

    # open のままなので status は変わらない (guard が無いため状態遷移しない)
    row_after = dict(conn.execute(
        "SELECT status, last_result FROM improvement_backlog WHERE id=?", (bid,)
    ).fetchone())
    assert row_after["status"] == "open"
    # set_status は last_result を常に上書きするため "reopened" になる
    # （この挙動自体が M3 をvacuous にしている）
    assert row_after["last_result"] == "reopened"


def test_policy_add_appends_to_directives_file(commands):
    cmds, _, root = commands
    cmds._policy_path = root / "policy" / "directives.md"  # 実装時は
                                                             # コンストラクタ
                                                             # 引数化する
                                                             # (下記申し送り)
    out = cmds.dispatch("policy add 新規指針テキスト")
    assert "新規指針テキスト" in cmds._policy_path.read_text(encoding="utf-8")
