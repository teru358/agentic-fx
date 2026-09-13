# tests/loops/test_unprofitable_note_hygiene.py
"""[unprofitable-note-hygiene] 設計書 v1.0 §4 (N1〜N8) の pin。

`docs/superpowers/specs/2026-09-13-unprofitable-note-hygiene-design.md`。
A4 run19 観測 D: mission が収益性フロアで `unprofitable` に落ちたとき、
同じ mission が起票した note/task backlog 行 (agent 自筆・in_sample 語彙)
に機械注記 `origin:unprofitable` を付け、次 mission の同型再提出を
prompt 上で抑止する。fixture 流儀は
`tests/loops/test_improve_loop_finalize.py`/`test_improve_loop_tx1_selection.py`/
`tests/loops/test_improve_loop_prepare.py` に倣う。
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agentic_fx.loops.improve_run_context import ImproveRunContext
from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
from agentic_fx.runners.base import Mission, MissionResult
from agentic_fx.store import backlog as backlog_store
from agentic_fx.store import missions as missions_store

_NOW = datetime(2026, 9, 13, tzinfo=timezone.utc)


def _write_candidate(staging_dir, name: str) -> None:
    candidate_dir = staging_dir / name
    candidate_dir.mkdir(parents=True, exist_ok=True)
    (candidate_dir / "plugin.py").write_text(
        "def compute(df, params):\n    return {'v': 1.0}\n")
    (candidate_dir / "config.yaml").write_text(
        "kind: strategy\npairs: ['USDJPY']\ntimeframe: '1h'\n"
        "exit_mode: levels\nparams: {}\n")
    (candidate_dir / "test_plugin.py").write_text(
        "def test_x():\n    pass\n"
        "def test_y():\n    pass\n"
        "def test_z():\n    pass\n")


def _floor_metrics(pf: float) -> dict:
    """§4.2 フロア判定 (`_floor_fail_items`) が読む 8 指標をすべて埋める
    (`trades`/`pf`/`win_rate`/`avg_r`/`max_drawdown`/`total_pnl`/
    `kill_switch_latches`/`evaluable`)。`pf < min_pf` (既定 1.2 相当) で
    不合格になるよう `trades>0`/`evaluable=True` にしておく。"""
    return {"trades": 5, "pf": pf, "win_rate": 0.4, "avg_r": 0.1,
            "max_drawdown": 100.0, "total_pnl": -10.0,
            "kill_switch_latches": 0, "evaluable": True}


def _gate_row(*, scope: str, pf: float) -> dict:
    return {
        "scope": scope, "plugin_ref": "plugins/myst",
        "content_hash": "c" * 64, "kind": "strategy", "pair": "USDJPY",
        "timeframe": "1h", "source": "dukascopy", "base_interval": "1m",
        "params": {},
        "period": (datetime(2026, 1, 1, tzinfo=timezone.utc),
                   datetime(2026, 2, 1, tzinfo=timezone.utc)),
        "metrics": _floor_metrics(pf), "settings_hash": "settings-hash",
        "core_commit": "core", "initial_balance": 10000.0,
        "now": _NOW,
    }


def _install_floor_strategy_gate(monkeypatch, loop, *, holdout_metrics):
    """`_run_strategy_gate` を、フロア不合格 (`floor_reason` あり) の
    `StrategyGateVerdict` を返すよう差し替える。`holdout_metrics=None` で
    in_sample 段落ち、dict で holdout 段落ちを模す (どちらも
    `commit()` 側の到達コードは同じ — §4.2 参照)。"""
    monkeypatch.setattr(
        loop, "_run_plugin_gate",
        lambda *a, **kw: SimpleNamespace(
            passed=True, content_hash="c" * 64, artifact_hash="a" * 64))
    monkeypatch.setattr(
        "agentic_fx.plugin.loader._discover_one",
        lambda *a, **kw: SimpleNamespace(max_bars=100))

    def fake_strategy_gate(*a, **kw):
        kw["record_fn"](_gate_row(scope="in_sample", pf=0.5))
        if holdout_metrics is not None:
            kw["record_fn"](_gate_row(scope="holdout_gate", pf=0.5))
        return SimpleNamespace(
            evaluable=True, floor_reason="unprofitable",
            observation_reason="",
            candidate_metrics={"USDJPY": _floor_metrics(0.5)},
            holdout_metrics=holdout_metrics,
            baseline_row=None)

    monkeypatch.setattr(loop, "_run_strategy_gate", fake_strategy_gate)


def _commit_unprofitable_mission(
        loop_full, conn, mission_and_run_fixture, tmp_path, monkeypatch, *,
        holdout_metrics):
    mission_id, run_id, backlog_id = mission_and_run_fixture
    staging_dir = tmp_path / "staging"
    _write_candidate(staging_dir, "myst")
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={})
    ctx = ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging_dir,
        source_snapshot_dir=tmp_path / "source", allowed_backlog_ids=None,
        slot_key=None, ledger=ledger, rpc_handlers={})
    mission = Mission(prompt="x", tools=[], output_schema={}, max_turns=10,
                      timeout_sec=60)
    result = MissionResult(status="completed", output={
        "discoveries": [
            {"idea": "fast=10/slow=30 は提出条件を満たした", "source": "agent",
             "evidence": "e", "kind": "fact"},
            {"idea": "次は fast=12/slow=28 を試す", "source": "agent",
             "evidence": "e", "kind": "task"},
        ],
        "selected": {"backlog_id": backlog_id, "idea": "x"},
        "artifact": {"type": "plugin", "name": "myst", "kind": "strategy",
                    "self_test": "passed", "summary": "s"},
        "selection_rationale": "r"}, transcript=[])
    _install_floor_strategy_gate(monkeypatch, loop_full, holdout_metrics=holdout_metrics)

    loop_full.commit(mission=mission, ctx=ctx, result=result, now=_NOW)
    return conn


def _idea_last_result(conn, idea: str):
    row = conn.execute(
        "SELECT last_result FROM improvement_backlog WHERE idea=?",
        (idea,)).fetchone()
    assert row is not None, f"backlog row not found for idea={idea!r}"
    return row["last_result"]


# ---------------------------------------------------------------------------
# N1: フロア不合格 (in_sample 段 / holdout 段の両方) で終わった mission が
# INSERT した note と task の last_result が origin:unprofitable
# ---------------------------------------------------------------------------

def test_n1_in_sample_floor_annotates_inserted_note_and_task(
        loop_full, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    _commit_unprofitable_mission(
        loop_full, conn, mission_and_run_fixture, tmp_path, monkeypatch,
        holdout_metrics=None)

    assert _idea_last_result(
        conn, "fast=10/slow=30 は提出条件を満たした") == "origin:unprofitable"
    assert _idea_last_result(
        conn, "次は fast=12/slow=28 を試す") == "origin:unprofitable"


def test_n1_holdout_floor_annotates_inserted_note_and_task(
        loop_full, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    _commit_unprofitable_mission(
        loop_full, conn, mission_and_run_fixture, tmp_path, monkeypatch,
        holdout_metrics={"USDJPY": _floor_metrics(0.5)})

    assert _idea_last_result(
        conn, "fast=10/slow=30 は提出条件を満たした") == "origin:unprofitable"
    assert _idea_last_result(
        conn, "次は fast=12/slow=28 を試す") == "origin:unprofitable"


# ---------------------------------------------------------------------------
# N2: gate_failed:* / insufficient_trades / report_failed 分岐では
# last_result が NULL のまま (逆変異: 条件を外すと red)
# ---------------------------------------------------------------------------

def test_n2_default_gate_failed_outcome_leaves_inserted_row_untouched(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    """`mission_outcome` 既定 (`gate_failed`) で `_finalize_gate_failed` を
    呼んでも `inserted_ids` は注記されない。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    note_id = backlog_store.add(conn, "insufficient trades note", "agent", _NOW)
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={})
    ledger.freeze()
    ctx = ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging_dir,
        source_snapshot_dir=tmp_path / "source", allowed_backlog_ids=None,
        slot_key=None, ledger=ledger, rpc_handlers={})

    loop_min._finalize_gate_failed(
        conn, ctx=ctx, backlog_id=backlog_id, reason="insufficient_trades:5",
        now=_NOW, inserted_ids=(note_id,))

    row = conn.execute(
        "SELECT last_result FROM improvement_backlog WHERE id=?",
        (note_id,)).fetchone()
    assert row["last_result"] is None


def test_n2_report_failed_branch_leaves_inserted_row_untouched(
        loop_min, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    """report part 作成失敗 (`report_failed` 固定分岐) では、呼び出し元が
    `mission_outcome="unprofitable"` を渡しても注記しない
    (`test_f4_10_...` と同じ分岐だが本 pin は `inserted_ids` を見る)。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    note_id = backlog_store.add(conn, "report write failure note", "agent", _NOW)
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={})
    ledger.freeze()
    ctx = ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging_dir,
        source_snapshot_dir=tmp_path / "source", allowed_backlog_ids=None,
        slot_key=None, ledger=ledger, rpc_handlers={})
    monkeypatch.setattr(
        loop_min, "_write_report_part",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("write failed")))

    loop_min._finalize_gate_failed(
        conn, ctx=ctx, backlog_id=backlog_id, reason="unprofitable", now=_NOW,
        mission_outcome="unprofitable", inserted_ids=(note_id,))

    row = conn.execute(
        "SELECT last_result FROM improvement_backlog WHERE id=?",
        (note_id,)).fetchone()
    assert row["last_result"] is None


# ---------------------------------------------------------------------------
# N3: 既存行の再利用 (existing / promoted) は注記されない
# ---------------------------------------------------------------------------

def test_n3_select_and_bind_inserted_ids_excludes_existing_reuse(
        loop_and_ctx_with_open_backlog):
    loop, ctx, conn, backlog_id = loop_and_ctx_with_open_backlog
    existing_id = backlog_store.add(conn, "already known idea", "user", _NOW)

    output = {
        "discoveries": [
            {"idea": "already known idea", "source": "agent", "evidence": "e",
             "kind": "task"},  # existing — 再利用のみ
            {"idea": "brand new idea", "source": "agent", "evidence": "e",
             "kind": "task"},  # inserted
        ],
        "selected": {"backlog_id": backlog_id, "idea": "x"},
        "artifact": {"type": "observation", "reason": "x"},
        "selection_rationale": "x"}

    outcome = loop._select_and_bind(conn, output, ctx, now=_NOW)

    new_row = conn.execute(
        "SELECT id FROM improvement_backlog WHERE idea='brand new idea'"
    ).fetchone()
    assert existing_id not in outcome.inserted_ids
    assert new_row["id"] in outcome.inserted_ids


def test_n3_select_and_bind_inserted_ids_excludes_note_promoted_to_task(
        loop_and_ctx_with_open_backlog):
    """既存 note を `kind=task` の discovery が昇格 (`promoted`) させる
    経路も `inserted_ids` に入らない。"""
    loop, ctx, conn, backlog_id = loop_and_ctx_with_open_backlog
    note_id = backlog_store.upsert_system_note(
        conn, idea="pre-existing note", last_result="x", now=_NOW)
    conn.commit()

    output = {
        "discoveries": [
            {"idea": "pre-existing note", "source": "agent", "evidence": "e",
             "kind": "task"},
        ],
        "selected": {"backlog_id": backlog_id, "idea": "x"},
        "artifact": {"type": "observation", "reason": "x"},
        "selection_rationale": "x"}

    outcome = loop._select_and_bind(conn, output, ctx, now=_NOW)

    assert note_id not in outcome.inserted_ids
    row = conn.execute(
        "SELECT status FROM improvement_backlog WHERE id=?",
        (note_id,)).fetchone()
    assert row["status"] == "open"  # promoted


# ---------------------------------------------------------------------------
# N4: 同 tx: `_finalize_gate_failed` が rollback する経路 (report 作成
# 失敗ではなく、成功系 tx 内での後段失敗) で注記も消える
# ---------------------------------------------------------------------------

def test_n4_rollback_in_same_tx_undoes_origin_marker(
        loop_min, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    mission_id, run_id, backlog_id = mission_and_run_fixture
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    note_id = backlog_store.add(conn, "rollback candidate note", "agent", _NOW)
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={})
    ledger.freeze()
    ctx = ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging_dir,
        source_snapshot_dir=tmp_path / "source", allowed_backlog_ids=None,
        slot_key=None, ledger=ledger, rpc_handlers={})

    from agentic_fx.store import missions as missions_mod

    def _boom(*a, **kw):
        raise RuntimeError("simulated fault after origin UPDATE")

    monkeypatch.setattr(missions_mod, "finish_improve_mission", _boom)

    with pytest.raises(RuntimeError):
        loop_min._finalize_gate_failed(
            conn, ctx=ctx, backlog_id=backlog_id, reason="unprofitable",
            now=_NOW, mission_outcome="unprofitable",
            inserted_ids=(note_id,))

    row = conn.execute(
        "SELECT last_result FROM improvement_backlog WHERE id=?",
        (note_id,)).fetchone()
    assert row["last_result"] is None


# ---------------------------------------------------------------------------
# N5: idea / idea_norm / status 不変
# ---------------------------------------------------------------------------

def test_n5_origin_marker_does_not_touch_idea_idea_norm_or_status(
        loop_full, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    conn = _commit_unprofitable_mission(
        loop_full, conn, mission_and_run_fixture, tmp_path, monkeypatch,
        holdout_metrics=None)

    row = conn.execute(
        "SELECT idea, idea_norm, status, last_result FROM improvement_backlog "
        "WHERE idea=?", ("次は fast=12/slow=28 を試す",)).fetchone()
    assert row["idea"] == "次は fast=12/slow=28 を試す"
    assert row["idea_norm"] == "次は fast=12/slow=28 を試す".strip().lower()
    assert row["status"] == "open"  # kind=task の既定 status のまま
    assert row["last_result"] == "origin:unprofitable"


# ---------------------------------------------------------------------------
# N6/N7: prompt の backlog 表・note 表に origin 列、規律 5 の逐語出現
# ---------------------------------------------------------------------------

class _FakeRunContext:
    def __init__(self, staging_dir, source_snapshot_dir):
        self.staging_dir = staging_dir
        self.source_snapshot_dir = source_snapshot_dir


def _sample_ctx_data_with_origin():
    return {
        "performance_report": {
            "window_days": [30, 90], "win_rate": 0.5, "profit_factor": 1.2,
            "by_pair": {"USDJPY": 0.4}, "by_hour": {"9": 0.3},
            "reject_breakdown": {"spread": 2}, "hold_rate": 0.1},
        "improvement_history": {"recent_runs": []},
        "current_inventory": {
            "approved_plugins": [], "news_sources": [],
            "risk_gate": {"max_positions": 3}},
        "backlog": {"items": [
            {"id": 2, "idea": "clean item", "status": "open", "attempts": 1,
             "assigned": True, "last_result": None},
            {"id": 3, "idea": "flagged item", "status": "open", "attempts": 1,
             "assigned": False, "last_result": "origin:unprofitable"},
        ], "notes": [
            {"id": 4, "idea": "clean note", "status": "note", "attempts": 0,
             "last_result": None},
            {"id": 5, "idea": "flagged note", "status": "note", "attempts": 0,
             "last_result": "origin:unprofitable"},
        ]},
        "user_policy": {"tail": "方針テキスト"},
        "references": {"plugin_name_pattern": "^[a-z][a-z0-9_]{0,63}$",
                       "plugin_contract_summary": "契約要約"},
    }


def _render(tmp_path, settings=None):
    from agentic_fx.loops.improve_loop import ImproveLoop
    from tests.loops.conftest import SETTINGS

    loop = ImproveLoop.__new__(ImproveLoop)
    loop._settings = settings or SETTINGS
    ctx = _FakeRunContext(tmp_path / "staging", tmp_path / "source")
    return loop._render_improve_mission_prompt(
        _sample_ctx_data_with_origin(), ctx=ctx)


def test_n6_backlog_table_has_origin_column_flagging_only_annotated_rows(
        tmp_path):
    text = _render(tmp_path)

    selectable, notes = text.split("## 既知の事実", 1)
    # items 表: id=2 (clean) は origin 空欄、id=3 (flagged) は unprofitable
    assert "| 2 | clean item |" in selectable
    assert "| 3 | flagged item |" in selectable
    clean_item_line = next(
        l for l in selectable.splitlines() if l.startswith("| 2 |"))
    flagged_item_line = next(
        l for l in selectable.splitlines() if l.startswith("| 3 |"))
    assert "unprofitable" not in clean_item_line
    assert "unprofitable" in flagged_item_line
    # notes 表: id=4 (clean) は空欄、id=5 (flagged) は unprofitable
    clean_note_line = next(
        l for l in notes.splitlines() if l.startswith("| 4 |"))
    flagged_note_line = next(
        l for l in notes.splitlines() if l.startswith("| 5 |"))
    assert "unprofitable" not in clean_note_line
    assert "unprofitable" in flagged_note_line


def test_n7_discipline_5_appears_verbatim_and_holdout_pin_stays_green(
        tmp_path):
    text = _render(tmp_path)

    assert ("`origin` 列が `unprofitable` の課題・note は、その mission の"
           "候補が収益性フロアで落ちたときに書かれたものです。") in text
    assert "同じ指標・同じパラメータの候補を再提出しないでください。" in text
    # 既存遮断 pin (F5-2 系): holdout の数値・段名は prompt に出ない。
    assert "holdout" not in text


# ---------------------------------------------------------------------------
# N8: 当該行が後に選択されて終端すると last_result は終端の値で上書きされる
# ---------------------------------------------------------------------------

def test_n8_annotated_row_last_result_overwritten_when_later_selected(
        loop_full, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    conn = _commit_unprofitable_mission(
        loop_full, conn, mission_and_run_fixture, tmp_path, monkeypatch,
        holdout_metrics=None)
    annotated_id = conn.execute(
        "SELECT id FROM improvement_backlog WHERE idea=?",
        ("次は fast=12/slow=28 を試す",)).fetchone()["id"]
    assert conn.execute(
        "SELECT last_result FROM improvement_backlog WHERE id=?",
        (annotated_id,)).fetchone()["last_result"] == "origin:unprofitable"

    # 別 mission がこの行を選び、標本不足で終端する。
    mission_id2 = missions_store.start(
        conn, "improve", "codex", "gpt-5", _NOW, commit=True)
    from agentic_fx.store import improve_runs as improve_runs_store
    run_id2 = improve_runs_store.start(
        conn, None, _NOW, mission_id=mission_id2, commit=True)
    staging_dir2 = tmp_path / "staging2"
    staging_dir2.mkdir()
    ledger2 = ImproveRpcLedger(rpc_timeout_sec_by_kind={})
    ledger2.freeze()
    ctx2 = ImproveRunContext(
        mission_id=mission_id2, run_id=run_id2, staging_dir=staging_dir2,
        source_snapshot_dir=tmp_path / "source2", allowed_backlog_ids=None,
        slot_key=None, ledger=ledger2, rpc_handlers={})

    loop_full._finalize_gate_failed(
        conn, ctx=ctx2, backlog_id=annotated_id,
        reason="insufficient_trades:2", now=_NOW)

    row = conn.execute(
        "SELECT last_result FROM improvement_backlog WHERE id=?",
        (annotated_id,)).fetchone()
    assert row["last_result"] == "insufficient_trades:2"
