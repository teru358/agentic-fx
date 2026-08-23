"""ImproveLoop.prepare (設計書 §4 冒頭, プラン §8.1-6/11/23)。"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from agentic_fx.loops.improve_run_context import ImproveRunContext
from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger


def test_improve_run_context_is_frozen_dataclass():
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"run_backtest": 600.0})
    ctx = ImproveRunContext(
        mission_id=1, run_id=2, staging_dir=Path("/tmp/staging"),
        source_snapshot_dir=Path("/tmp/source"),
        allowed_backlog_ids=frozenset({1, 2}), slot_key=("2026-W34", 0),
        ledger=ledger, rpc_handlers={})
    assert ctx.mission_id == 1
    with pytest.raises(AttributeError):
        ctx.mission_id = 99  # frozen


def test_tx0_mission_id_unique_partial_index_on_improvement_runs(conn):
    """`improvement_runs.mission_id` に対する部分 UNIQUE の migration が
    効いていること (2 回目の INSERT が同じ mission_id を指せば
    IntegrityError)。新規行についてのみ強制 — 既存 NULL 行は許容する。"""
    now = datetime(2026, 8, 22, 3, 0)
    conn.execute(
        "INSERT INTO improvement_runs (backlog_id, mission_id, started_at) "
        "VALUES (NULL, 555, ?)", (now.isoformat(),))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO improvement_runs (backlog_id, mission_id, started_at) "
            "VALUES (NULL, 555, ?)", (now.isoformat(),))


def test_tx0_crash_between_mission_insert_and_run_insert_leaves_no_orphan(
        loop_min, conn, monkeypatch):
    """Tx-0 の途中 (missions INSERT 後・run INSERT 前) で例外が起きたら
    ロールバックし、mission 行も残らない (単一 tx pin)。"""
    now = datetime(2026, 8, 22, 3, 0)

    from agentic_fx.store import improve_runs as improve_runs_store

    def _boom(*a, **kw):
        raise RuntimeError("simulated crash between INSERTs")

    monkeypatch.setattr(improve_runs_store, "start", _boom)

    with pytest.raises(RuntimeError):
        loop_min.prepare(slot_key=None, now=now)

    n = conn.execute("SELECT count(*) c FROM missions").fetchone()["c"]
    assert n == 0


def _sample_ctx_data():
    return {
        "performance_report": {
            # precheck 2026-08-22 wave2: T10-M2 — improve_context.py:62 の
            # 実shape は [30, 90] (複数窓)。単一 int ではない
            "window_days": [30, 90], "win_rate": 0.5, "profit_factor": 1.2,
            "by_pair": {"USDJPY": 0.4}, "by_hour": {"9": 0.3},
            "reject_breakdown": {"spread": 2}, "hold_rate": 0.1},
        "improvement_history": {"recent_runs": [
            {"id": 1, "backlog_id": 2, "idea": "x", "result": "approved",
             "attempts": 1, "last_result": None}]},
        # precheck 2026-08-22 wave2: T10-B4 — improve_context.py:86-92 の
        # 実shape (list[dict], list[str] ではない) に合わせる。文字列
        # リストのままだと `", ".join(list[str])` は緑のまま通ってしまい
        # 本番の `", ".join(list[dict])` (TypeError) を検出できない。
        "current_inventory": {
            "approved_plugins": [{"name": "p1", "kind": "indicator",
                                  "pairs": ["USDJPY"]}],
            "news_sources": [{"name": "s1", "enabled": True}],
            "risk_gate": {"max_positions": 3}},
        "backlog": {"items": [
            {"id": 2, "idea": "x", "status": "open", "attempts": 1,
             "assigned": True}]},
        "user_policy": {"tail": "方針テキスト"},
        "references": {"plugin_name_pattern": "^[a-z][a-z0-9_]{0,63}$",
                       "plugin_contract_summary": "契約要約"},
    }


class _FakeRunContext:
    def __init__(self, staging_dir, source_snapshot_dir):
        self.staging_dir = staging_dir
        self.source_snapshot_dir = source_snapshot_dir


def test_render_improve_mission_prompt_fills_all_placeholders(tmp_path):
    """RB4: 8-I の対応表の全 17 キーが埋まり、テンプレートに未展開の
    `{...}` プレースホルダが残らない。"""
    from agentic_fx.loops.improve_loop import ImproveLoop

    loop = ImproveLoop.__new__(ImproveLoop)
    ctx = _FakeRunContext(tmp_path / "staging", tmp_path / "source")
    text = loop._render_improve_mission_prompt(_sample_ctx_data(), ctx=ctx)

    assert isinstance(text, str)
    # テンプレートの 17 プレースホルダが 1 つも未展開のまま残っていない
    # ことを確認する (`{plugin_name_pattern}` の値自体は正規表現なので
    # `{`/`}` を含みうる — ブランケットで全体を検査すると恒真になるため、
    # プレースホルダ名そのものが残っていないかを個別に見る)。
    for placeholder in (
            "{performance_window_days}", "{win_rate}", "{profit_factor}",
            "{by_pair}", "{by_hour}", "{reject_breakdown}", "{hold_rate}",
            "{improvement_history_table}", "{approved_plugins}",
            "{news_sources}", "{risk_gate_summary}", "{backlog_table}",
            "{user_policy_tail}", "{plugin_name_pattern}",
            "{plugin_contract_summary}", "{staging_dir}",
            "{source_snapshot_dir}"):
        assert placeholder not in text, f"未展開のプレースホルダ: {placeholder}"
    assert str(tmp_path / "staging") in text
    assert str(tmp_path / "source") in text


def test_render_improve_mission_prompt_fails_closed_on_missing_key(tmp_path):
    """RB4: 対応表のキーが 1 つでも欠けたら (`references` セクション欠落
    などの上流バグ) `KeyError` で fail closed する — 空文字列で握り潰し
    て公開しない。"""
    from agentic_fx.loops.improve_loop import ImproveLoop

    loop = ImproveLoop.__new__(ImproveLoop)
    ctx = _FakeRunContext(tmp_path / "staging", tmp_path / "source")
    bad_ctx_data = _sample_ctx_data()
    del bad_ctx_data["references"]
    with pytest.raises(KeyError):
        loop._render_improve_mission_prompt(bad_ctx_data, ctx=ctx)


def test_tx0_mutation_M2_system_exit_without_rollback(loop_min, conn, monkeypatch):
    """M2: BaseException ではなく Exception を catch することで、
    SystemExit でロールバックしない退行を検出する。"""
    now = datetime(2026, 8, 22, 3, 0)
    from agentic_fx.store import improve_runs as improve_runs_store

    def _sys_exit(*a, **kw):
        raise SystemExit("simulated system exit")

    monkeypatch.setattr(improve_runs_store, "start", _sys_exit)

    with pytest.raises(SystemExit):
        loop_min.prepare(slot_key=None, now=now)

    # SystemExit が正しくロールバックされれば、mission 行は 0 件
    n = conn.execute("SELECT count(*) c FROM missions").fetchone()["c"]
    assert n == 0, "SystemExit should trigger rollback with BaseException handler"


def test_tx0_mutation_M3_null_mission_id_rows_allowed(conn):
    """M3: 部分 UNIQUE index の WHERE clause が無いと、NULL 行同士が衝突。
    逆に WHERE mission_id IS NOT NULL があれば、複数の NULL 行が共存できる。"""
    now = datetime(2026, 8, 22, 3, 0)
    # 2 つの NULL mission_id 行を挿入
    conn.execute(
        "INSERT INTO improvement_runs (backlog_id, mission_id, started_at) "
        "VALUES (NULL, NULL, ?)", (now.isoformat(),))
    # 部分 UNIQUE があれば、これも成功する (NULL は UNIQUE 制約の対象外)
    conn.execute(
        "INSERT INTO improvement_runs (backlog_id, mission_id, started_at) "
        "VALUES (NULL, NULL, ?)", (now.isoformat(),))
    # 3 つ目も成功
    conn.execute(
        "INSERT INTO improvement_runs (backlog_id, mission_id, started_at) "
        "VALUES (NULL, NULL, ?)", (now.isoformat(),))
    rows = conn.execute("SELECT count(*) c FROM improvement_runs "
                        "WHERE mission_id IS NULL").fetchone()["c"]
    assert rows == 3
