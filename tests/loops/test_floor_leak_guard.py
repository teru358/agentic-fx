"""[reject-reason-leak] T0 (2026-09-12): 人間の却下理由のプロンプト漏洩
是正の pin (F5-1 / F5-3 / F5-4 / F5-5)。

plan 注記どおり `tests/integration/test_improve_forbidden_regression.py`
には置かない — 同ファイル全体に Landlock skip marker があり、Landlock
非対応環境ではここに置いた純粋な store / rendering の漏洩 pin まで
全 skip されてしまう (codex I12)。ここは Landlock に依存しない。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

from agentic_fx.activity import ActivityLog
from agentic_fx.commands import Commands
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock
from agentic_fx.core.health_latch import HealthLatch
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.loops.improve_context import _improvement_history
from agentic_fx.loops.improve_loop import ImproveLoop
from agentic_fx.store import approvals
from agentic_fx.store import backlog as backlog_store
from agentic_fx.store import improve_runs as improve_runs_store
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.state import StateStore

NOW = datetime(2026, 9, 12, 12, 0)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")

REASON = "holdout pf 0.80 で負け"


def _reject_flow(conn):
    """backlog → improvement_runs → approval(pending) → 人間 reject の
    実フロー一式を作る。戻り値: (backlog_id, run_id, approval_id)。"""
    backlog_id = backlog_store.add(conn, "ema_rsi_pullback を試す", "user", NOW)
    backlog_store.select_for_mission(conn, backlog_id, now=NOW)
    run_id = improve_runs_store.start(conn, backlog_id, NOW)
    approval_id = approvals.create(
        conn, kind="plugin", payload={"backlog_id": backlog_id, "name": "x"},
        now=NOW)
    approvals.apply_decision(
        conn, approval_id, "rejected", decided_by="shell", now=NOW,
        reason=REASON)
    return backlog_id, run_id, approval_id


class _FakeRunContext:
    def __init__(self, staging_dir, source_snapshot_dir):
        self.staging_dir = staging_dir
        self.source_snapshot_dir = source_snapshot_dir


def _sample_ctx_data(recent_runs):
    return {
        "performance_report": {
            "window_days": [30, 90], "win_rate": 0.5, "profit_factor": 1.2,
            "by_pair": {"USDJPY": 0.4}, "by_hour": {"9": 0.3},
            "reject_breakdown": {"spread": 2}, "hold_rate": 0.1},
        "improvement_history": {"recent_runs": recent_runs},
        "current_inventory": {
            "approved_plugins": [], "news_sources": [],
            "risk_gate": {"max_positions": 3}},
        "backlog": {"items": [], "notes": []},
        "user_policy": {"tail": "方針テキスト"},
        "references": {"plugin_name_pattern": "^[a-z][a-z0-9_]{0,63}$",
                       "plugin_contract_summary": "契約要約"},
    }


def test_reject_reason_does_not_leak_into_rendered_mission_prompt(tmp_path):
    """F5-1: 人間が理由付きで reject した後、次 mission のレンダ済み
    プロンプト全文に理由文字列 (`holdout`/`0.80`/逐語) が現れない。"""
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    _reject_flow(conn)

    hist = _improvement_history(conn)
    # last_result は固定文言 `rejected_by_human` — 理由の欠片も混じらない
    assert hist["recent_runs"][0]["last_result"] == "rejected_by_human"

    loop = ImproveLoop.__new__(ImproveLoop)
    loop._settings = SETTINGS
    ctx = _FakeRunContext(tmp_path / "staging", tmp_path / "source")
    rendered = loop._render_improve_mission_prompt(
        _sample_ctx_data(hist["recent_runs"]), ctx=ctx)

    assert "holdout" not in rendered
    assert "0.80" not in rendered
    assert REASON not in rendered
    assert "rejected_by_human" in rendered


def test_reject_reason_survives_in_approval_requests_reason_column(tmp_path):
    """F5-3: `approval_requests.reason` には理由が残っている (人間専用の
    台帳は縮退しない — backlog へ渡らないだけ)。"""
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    _, _, approval_id = _reject_flow(conn)

    row = conn.execute(
        "SELECT reason FROM approval_requests WHERE id=?",
        (approval_id,)).fetchone()
    assert row["reason"] == REASON


def _commands(conn, tmp_path):
    state = StateStore(tmp_path / "s.json")
    activity = ActivityLog(tmp_path / "logs" / "activity.log")
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "logs" / "agentic.log").write_text("x\n", encoding="utf-8")
    trade_loop = MagicMock()
    return Commands(
        conn=conn, state_store=state,
        broker=PaperBroker(conn, SETTINGS, FixedClock(NOW)),
        trade_loop=trade_loop, activity=activity,
        log_dir=tmp_path / "logs", clock=FixedClock(NOW),
        health_latch=HealthLatch())


def test_afx_approval_detail_shows_reason_for_rejected(tmp_path):
    """F5-4: `afx> approval <id>` の出力に `reason=` (理由込み) が現れる
    — 人間向け導線 (T0 Step 0-2)。"""
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    _, _, approval_id = _reject_flow(conn)
    cmds = _commands(conn, tmp_path)

    out = cmds.dispatch(f"approval {approval_id}")

    assert f"reason={REASON}" in out
    assert "decided_by=shell" in out


def test_history_table_is_sole_renderer_of_last_result(tmp_path):
    """F5-5: `_history_table` が `last_result` をレンダする唯一の経路。
    `_backlog_table` が last_result を描き始めたら (漏洩面が広がったら)
    落ちる — backlog 側センチネルが本文に出ないことを固定する。"""
    loop = ImproveLoop.__new__(ImproveLoop)
    loop._settings = SETTINGS
    ctx = _FakeRunContext(tmp_path / "staging", tmp_path / "source")
    ctx_data = _sample_ctx_data(
        [{"id": 1, "backlog_id": 2, "idea": "x", "result": "observation",
          "attempts": 1, "last_result": "HISTORY_SENTINEL"}])
    ctx_data["backlog"] = {"items": [
        {"id": 3, "idea": "y", "status": "open", "attempts": 0,
         "assigned": True, "last_result": "BACKLOG_SENTINEL"}], "notes": []}

    rendered = loop._render_improve_mission_prompt(ctx_data, ctx=ctx)

    assert "HISTORY_SENTINEL" in rendered
    assert "BACKLOG_SENTINEL" not in rendered
