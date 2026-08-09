from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.commands import Commands
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock
from agentic_fx.core.health_latch import HealthLatch
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.store import approvals
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.state import StateStore

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[1] / "config" / "settings.yaml.example")


def _commands(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    state = StateStore(tmp_path / "s.json")
    activity = ActivityLog(tmp_path / "logs" / "activity.log")
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "logs" / "agentic.log").write_text(
        "line1\nline2\nline3\n", encoding="utf-8")
    trade_loop = MagicMock()
    trade_loop.ask_once.return_value = "回答です"
    health_latch = HealthLatch()
    cmds = Commands(conn=conn, state_store=state,
                    broker=PaperBroker(conn, SETTINGS, FixedClock(NOW)),
                    trade_loop=trade_loop, activity=activity,
                    log_dir=tmp_path / "logs", clock=FixedClock(NOW),
                    health_latch=health_latch)
    return conn, state, activity, cmds


def test_status(tmp_path):
    _, _, _, cmds = _commands(tmp_path)
    out = cmds.dispatch("status")
    assert "learning" in out and "1,000,000" in out
    # F3: kill switch と orders 行の表示確認
    assert "アクティブ orders: 0" in out
    assert "ok" in out  # kill_switch_latched=False


def test_status_latched(tmp_path):
    """F3: kill_switch_latched=True の表示確認。"""
    _, state, _, cmds = _commands(tmp_path)
    state.update(kill_switch_latched=True)
    out = cmds.dispatch("status")
    assert "LATCHED" in out


def test_status_shows_process_health_latch_reasons(tmp_path):
    _, _, _, cmds = _commands(tmp_path)
    cmds.health_latch.record_failure("disk full")

    out = cmds.dispatch("status")

    assert "health: LATCHED (disk full)" in out


def test_log_tail(tmp_path):
    _, _, _, cmds = _commands(tmp_path)
    out = cmds.dispatch("log 2")
    assert "line2" in out and "line1" not in out


def test_log_zero_returns_empty(tmp_path):
    """W5: `log 0` は全ログを流さず空文字を返す。"""
    _, _, _, cmds = _commands(tmp_path)
    out = cmds.dispatch("log 0")
    assert out == ""


def test_log_negative_returns_empty(tmp_path):
    """W5: 負の n も同じガードで空文字を返す。"""
    _, _, _, cmds = _commands(tmp_path)
    out = cmds.dispatch("log -1")
    assert out == ""


def test_activity_filter(tmp_path):
    _, _, activity, cmds = _commands(tmp_path)
    activity.write(Category.TRADE, "order_opened", "x")
    activity.write(Category.NEWS, "collected", "y")
    out = cmds.dispatch("activity 10 TRADE")
    assert "order_opened" in out and "collected" not in out


def test_ask_delegates(tmp_path):
    _, _, _, cmds = _commands(tmp_path)
    out = cmds.dispatch("ask 今の相場は？")
    assert "回答です" in out
    # F3: 質問文が正しく渡されることを確認
    cmds.trade_loop.ask_once.assert_called_once_with("今の相場は？")


def test_approve(tmp_path):
    """F2: approve は DB 状態と activity 記録を検証。"""
    conn, _, activity, cmds = _commands(tmp_path)
    aid = approvals.create(conn, "tech_plugin", {}, NOW)
    out = cmds.dispatch(f"approve {aid}")
    assert "approved" in out
    # DB に status="approved" が記録されること
    row = conn.execute(
        "SELECT status FROM approval_requests WHERE id=?", (aid,)).fetchone()
    assert row["status"] == "approved"
    # activity に APPROVAL レコードが記録されること
    records = activity.tail(10, Category.APPROVAL)
    assert any("approved" in r for r in records)


def test_reject(tmp_path):
    """F2: reject は理由を保存し activity に記録。"""
    conn, _, activity, cmds = _commands(tmp_path)
    aid = approvals.create(conn, "tech_plugin", {}, NOW)
    reason_text = "テスト理由"
    out = cmds.dispatch(f"reject {aid} {reason_text}")
    assert "rejected" in out
    # DB に status="rejected" と reason が保存されること
    row = conn.execute(
        "SELECT status, reason FROM approval_requests WHERE id=?", (aid,)).fetchone()
    assert row["status"] == "rejected"
    assert row["reason"] == reason_text
    # activity に APPROVAL レコードが記録されること
    records = activity.tail(10, Category.APPROVAL)
    assert any("rejected" in r for r in records)


def test_approve_already_decided(tmp_path):
    """F2: 決定済み approval への二重決定は例外でなくメッセージ。"""
    conn, _, _, cmds = _commands(tmp_path)
    aid = approvals.create(conn, "tech_plugin", {}, NOW)
    # 1 度目の approve
    cmds.dispatch(f"approve {aid}")
    # 2 度目は決定済みメッセージ
    out = cmds.dispatch(f"reject {aid} 理由")
    assert "決定済み" in out or "already" in out.lower()


def test_approve_nonexistent(tmp_path):
    """F2: 存在しない id への approve は例外でなくメッセージを返す。"""
    _, _, _, cmds = _commands(tmp_path)
    out = cmds.dispatch("approve 9999")
    # 例外を投げずに文字列を返すこと（AlreadyDecidedError でメッセージが返される）
    assert isinstance(out, str)
    assert "決定済み" in out


def test_killswitch_reset(tmp_path):
    """F2: killswitch reset で activity 記録を確認。"""
    _, state, activity, cmds = _commands(tmp_path)
    # Set non-default state
    state.update(kill_switch_latched=True, autopilot=True)
    out = cmds.dispatch("killswitch reset")
    # kill_switch_latched=False に変更されること
    assert state.load().kill_switch_latched is False
    # 他の状態は保持されること
    assert state.load().autopilot is True
    assert "解除" in out
    # F2: activity に SYSTEM レコードが記録されること
    records = activity.tail(10, Category.SYSTEM)
    assert any("kill_switch_reset" in r for r in records)


def test_unknown_shows_help(tmp_path):
    _, _, _, cmds = _commands(tmp_path)
    assert "status" in cmds.dispatch("nonsense")


def test_transcript_json_not_leaked(tmp_path):
    """Regression: transcript_json (sensitive) must not appear in dispatch output."""
    conn, _, _, cmds = _commands(tmp_path)
    # Insert a mission with sensitive sentinel in transcript_json
    sentinel = "SECRET_TRANSCRIPT_MARKER"
    conn.execute(
        """INSERT INTO missions
        (loop, runner, model, status, started_at, transcript_json)
        VALUES (?, ?, ?, ?, ?, ?)""",
        ("trade", "local", "test-model", "completed", NOW.isoformat(), f'{{"{sentinel}"}}')
    )
    conn.commit()

    out = cmds.dispatch("status")
    # Sentinel must NOT appear, but loop name must
    assert sentinel not in out
    assert "trade" in out


def test_ask_with_exception(tmp_path):
    """F1: trade_loop.ask_once が例外を投げても dispatch は文字列を返す。"""
    conn, state, activity, _ = _commands(tmp_path)
    # trade_loop を例外を投げるモックに置き換える
    cmds = Commands(conn=conn, state_store=state, activity=activity,
                    broker=PaperBroker(conn, SETTINGS, FixedClock(NOW)),
                    trade_loop=MagicMock(ask_once=MagicMock(
                        side_effect=RuntimeError("Test error"))),
                    log_dir=tmp_path / "logs", clock=FixedClock(NOW))
    out = cmds.dispatch("ask 質問")
    # 例外を投げずに文字列を返すこと
    assert isinstance(out, str)
    assert "エラー" in out


def test_status_with_broken_state_json(tmp_path):
    """F1: state.json が破損しても dispatch は文字列を返す。"""
    # state.json に不正 JSON を書き込む
    (tmp_path / "s.json").write_text("{invalid json", encoding="utf-8")
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    state = StateStore(tmp_path / "s.json")
    activity = ActivityLog(tmp_path / "logs" / "activity.log")
    (tmp_path / "logs").mkdir(exist_ok=True)
    cmds = Commands(conn=conn, state_store=state, activity=activity,
                    broker=PaperBroker(conn, SETTINGS, FixedClock(NOW)),
                    trade_loop=MagicMock(),
                    log_dir=tmp_path / "logs", clock=FixedClock(NOW))
    out = cmds.dispatch("status")
    # 例外を投げずに文字列を返すこと
    assert isinstance(out, str)
    assert "エラー" in out
