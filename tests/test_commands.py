from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.commands import Commands
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock
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
    cmds = Commands(conn=conn, state_store=state,
                    broker=PaperBroker(conn, SETTINGS, FixedClock(NOW)),
                    trade_loop=trade_loop, activity=activity,
                    log_dir=tmp_path / "logs", clock=FixedClock(NOW))
    return conn, state, activity, cmds


def test_status(tmp_path):
    _, _, _, cmds = _commands(tmp_path)
    out = cmds.dispatch("status")
    assert "learning" in out and "1,000,000" in out


def test_log_tail(tmp_path):
    _, _, _, cmds = _commands(tmp_path)
    out = cmds.dispatch("log 2")
    assert "line2" in out and "line1" not in out


def test_activity_filter(tmp_path):
    _, _, activity, cmds = _commands(tmp_path)
    activity.write(Category.TRADE, "order_opened", "x")
    activity.write(Category.NEWS, "collected", "y")
    out = cmds.dispatch("activity 10 TRADE")
    assert "order_opened" in out and "collected" not in out


def test_ask_delegates(tmp_path):
    _, _, _, cmds = _commands(tmp_path)
    assert "回答です" in cmds.dispatch("ask 今の相場は？")


def test_approve_reject(tmp_path):
    conn, _, _, cmds = _commands(tmp_path)
    aid = approvals.create(conn, "tech_plugin", {}, NOW)
    assert "approved" in cmds.dispatch(f"approve {aid}")
    out = cmds.dispatch(f"reject {aid} 理由")  # 二重決定
    assert "決定済み" in out or "already" in out.lower()


def test_killswitch_reset(tmp_path):
    _, state, _, cmds = _commands(tmp_path)
    state.update(kill_switch_latched=True)
    out = cmds.dispatch("killswitch reset")
    assert state.load().kill_switch_latched is False
    assert "解除" in out


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
