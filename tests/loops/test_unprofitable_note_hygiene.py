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


def _write_indicator_candidate(staging_dir, name: str) -> None:
    """`tests/loops/test_improve_loop_finalize.py::_write_candidate` と
    同型 (indicator gate は strategy gate と異なり `evaluate` を要求
    しない `compute` のみの plugin.py で合格する)。"""
    candidate_dir = staging_dir / name
    candidate_dir.mkdir(parents=True, exist_ok=True)
    (candidate_dir / "plugin.py").write_text(
        "def compute(df, params):\n    return {'v': 1.0}\n")
    (candidate_dir / "config.yaml").write_text(
        "kind: indicator\npairs: ['USDJPY']\ntimeframe: '1h'\n")
    (candidate_dir / "test_plugin.py").write_text(
        "def test_x():\n    pass\n"
        "def test_y():\n    pass\n"
        "def test_z():\n    pass\n")


def _commit_terminal_with_discoveries(
        loop, conn, mission_and_run_fixture, tmp_path, *, artifact,
        note_idea: str, task_idea: str, write_plugin: bool = False):
    """discovery の note (`kind=fact`) と task (`kind=task`) を持つ
    commit-level fixture で、任意の終端 `artifact` を通す (codex r1
    是正、N2 Important: report/observation/approval の 3 終端で
    `inserted_ids` が注記されないことを pin するための共有 helper)。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    staging_dir = tmp_path / "staging"
    if write_plugin:
        _write_indicator_candidate(staging_dir, artifact["name"])
    else:
        staging_dir.mkdir(parents=True, exist_ok=True)
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={})
    ctx = ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging_dir,
        source_snapshot_dir=tmp_path / "source", allowed_backlog_ids=None,
        slot_key=None, ledger=ledger, rpc_handlers={})
    mission = Mission(prompt="x", tools=[], output_schema={}, max_turns=10,
                      timeout_sec=60)
    result = MissionResult(status="completed", output={
        "discoveries": [
            {"idea": note_idea, "source": "agent", "evidence": "e",
             "kind": "fact"},
            {"idea": task_idea, "source": "agent", "evidence": "e",
             "kind": "task"},
        ],
        "selected": {"backlog_id": backlog_id, "idea": "x"},
        "artifact": artifact,
        "selection_rationale": "r"}, transcript=[])

    loop.commit(mission=mission, ctx=ctx, result=result, now=_NOW)
    return conn


def test_n2_report_outcome_leaves_inserted_rows_untouched(
        loop_full, conn, mission_and_run_fixture, tmp_path):
    """codex r1 Important: report 終端 (`_finalize_report_or_observation`
    経由、`artifact.type=='report'`) を discovery の note/task を持つ
    commit-level fixture で実際に通し、両行の `last_result` が NULL の
    ままであることを pin する。"""
    _commit_terminal_with_discoveries(
        loop_full, conn, mission_and_run_fixture, tmp_path,
        artifact={"type": "report", "proposal_kind": "core",
                  "title": "report terminal", "body_md": "body"},
        note_idea="report 終端の note", task_idea="report 終端の task")

    assert _idea_last_result(conn, "report 終端の note") is None
    assert _idea_last_result(conn, "report 終端の task") is None


def test_n2_observation_outcome_leaves_inserted_rows_untouched(
        loop_full, conn, mission_and_run_fixture, tmp_path):
    """codex r1 Important: observation 終端 (`_finalize_report_or_
    observation` 経由、`artifact.type=='observation'`) を discovery の
    note/task を持つ commit-level fixture で実際に通し、両行の
    `last_result` が NULL のままであることを pin する。"""
    _commit_terminal_with_discoveries(
        loop_full, conn, mission_and_run_fixture, tmp_path,
        artifact={"type": "observation", "reason": "no candidate"},
        note_idea="observation 終端の note", task_idea="observation 終端の task")

    assert _idea_last_result(conn, "observation 終端の note") is None
    assert _idea_last_result(conn, "observation 終端の task") is None


def test_n2_approval_outcome_leaves_inserted_rows_untouched(
        loop_full, conn, mission_and_run_fixture, tmp_path):
    """codex r1 Important: approval 終端 (`_finalize_success` 経由、
    plugin/indicator がゲート合格し承認申請を出す経路) を discovery の
    note/task を持つ commit-level fixture で実際に通し、両行の
    `last_result` が NULL のままであることを pin する (indicator は
    strategy gate — 収益性フロア判定 — を経由しないため、この終端が
    `unprofitable` 分岐へ誤って迷い込まないことも合わせて確認する)。"""
    conn = _commit_terminal_with_discoveries(
        loop_full, conn, mission_and_run_fixture, tmp_path,
        artifact={"type": "plugin", "name": "myind", "kind": "indicator",
                  "self_test": "passed", "summary": "s"},
        note_idea="approval 終端の note", task_idea="approval 終端の task",
        write_plugin=True)

    run_id = mission_and_run_fixture[1]
    run = conn.execute(
        "SELECT result FROM improvement_runs WHERE id=?",
        (run_id,)).fetchone()
    assert run["result"] == "approval"
    assert _idea_last_result(conn, "approval 終端の note") is None
    assert _idea_last_result(conn, "approval 終端の task") is None


# ---------------------------------------------------------------------------
# N3: 既存行の再利用 (existing / promoted) は注記されない
# ---------------------------------------------------------------------------

def test_n3_selected_existing_task_reuses_without_inserting(
        loop_and_ctx_with_open_backlog):
    """codex r1 Minor: discoveries 側の重複は `_upsert_backlog_idea` より
    前の `continue` で除外されるため existing 戻り値を実際には通らない
    (段0 M12)。ここでは `selected.backlog_id` を省略し `selected.idea` を
    既存 task の idea と一致させ、`_upsert_backlog_idea(...) -> "existing"`
    を実際に通す経路 (`_select_and_bind` の `selected` 分岐) を pin する。
    その ID が `inserted_ids` に無いことを確認する。"""
    loop, ctx, conn, _backlog_id = loop_and_ctx_with_open_backlog
    existing_id = backlog_store.add(conn, "already known idea", "user", _NOW)

    output = {
        "discoveries": [],
        "selected": {"idea": "already known idea", "source": "agent"},
        "artifact": {"type": "observation", "reason": "x"},
        "selection_rationale": "x"}

    outcome = loop._select_and_bind(conn, output, ctx, now=_NOW)

    assert outcome.backlog_id == existing_id
    assert existing_id not in outcome.inserted_ids


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
    """codex r1 Minor (N4): report part 作成失敗は注記 tx より前 (注記を
    一度も書かない) のため atomicity の probe にならない — その分岐は N2
    の非書込み基準 (`test_n2_report_failed_branch_leaves_inserted_row_
    untouched`) 側で扱う。ここで検査するのは、正常分岐 tx の注記後に
    後段 DB 処理 (`finish_improve_mission`) が失敗した場合の rollback —
    `_finalize_gate_failed` の `mission_outcome=="unprofitable"` UPDATE 後、
    同一 tx 内の `finish_improve_mission` が例外を送出すると、先に書いた
    `origin:unprofitable` も rollback で消えることを pin する。"""
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
    """codex r1 Minor (N5): task 行だけを検査すると、UPDATE に
    `status='open'` を混ぜる変異 (note の `status` を `note` から `open`
    へ壊す) は task 側の期待値 (既定 `open`) を変えないため全 pin が緑の
    まま生存する。note 行 (`kind=fact` の discovery) についても
    `idea`/`idea_norm`/`status=='note'`/機械注記を検査する。"""
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

    note_row = conn.execute(
        "SELECT idea, idea_norm, status, last_result FROM improvement_backlog "
        "WHERE idea=?", ("fast=10/slow=30 は提出条件を満たした",)).fetchone()
    assert note_row["idea"] == "fast=10/slow=30 は提出条件を満たした"
    assert (note_row["idea_norm"]
            == "fast=10/slow=30 は提出条件を満たした".strip().lower())
    assert note_row["status"] == "note"  # kind=fact の既定 status のまま
    assert note_row["last_result"] == "origin:unprofitable"


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
            # 段0 S1 の囮: ゲート終端が書く実際の `last_result`
            # (`unprofitable` — フロア不合格候補そのものの行、`observation`
            # で `list_open` に残る)。`origin` 列は **機械注記の完全一致**
            # だけを映す契約なので、この行は空欄でなければならない。
            {"id": 6, "idea": "the candidate row itself", "status": "observation",
             "attempts": 2, "assigned": False, "last_result": "unprofitable"},
        ], "notes": [
            {"id": 4, "idea": "clean note", "status": "note", "attempts": 0,
             "last_result": None},
            {"id": 5, "idea": "flagged note", "status": "note", "attempts": 0,
             "last_result": "origin:unprofitable"},
            # 段0 S1 の囮 (notes 側): `promoted_from_note` など別語彙。
            {"id": 7, "idea": "other machine note", "status": "note",
             "attempts": 0, "last_result": "promoted_from_note"},
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


# ---------------------------------------------------------------------------
# 段0 (指揮者変異スイープ 2026-09-13) で SURVIVED した次元の pin。
# `tmp/review-20260913-nh/stage0.md` の S1〜S5。
# ---------------------------------------------------------------------------

def _origin_cell(text: str, row_id: int) -> str:
    """レンダ済み prompt から `| <id> | ... |` 行を拾い、末尾の
    `origin` セルを返す。"""
    line = next(l for l in text.splitlines() if l.startswith(f"| {row_id} |"))
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return cells[-1]


def test_s1_origin_cell_is_exact_match_not_substring(tmp_path):
    """段0 M1: `_backlog_table` の origin 判定を部分一致
    (`'unprofitable' in str(last_result)`) に緩めると、ゲート終端が書く
    実際の `last_result='unprofitable'` (フロアで落ちた候補行そのもの、
    `observation` として `list_open` に残る) まで `origin` 列で
    `unprofitable` と誤表示される — 機械注記でない行が「前 mission が
    起票した note/task」に見えてしまい、規律 5 の意味が壊れる。
    完全一致 (`== 'origin:unprofitable'`) を pin する。"""
    text = _render(tmp_path)

    assert _origin_cell(text, 3) == "unprofitable"   # 機械注記
    assert _origin_cell(text, 5) == "unprofitable"   # 機械注記 (note)
    assert _origin_cell(text, 2) == ""               # last_result なし
    assert _origin_cell(text, 6) == ""               # 終端理由 'unprofitable'
    assert _origin_cell(text, 7) == ""               # 'promoted_from_note'


def test_s2_build_improve_context_carries_note_last_result_to_origin_column(
        tmp_path):
    """段0 M3 (配線): N6 は `ctx_data` を手書きするため、
    `improve_context._backlog_section` が **notes** 行に `last_result` を
    載せている配線を誰も検証していなかった (items 側は
    `test_improve_context.py` の既存 pin が守っている)。実 DB →
    `build_improve_context` → `_render_improve_mission_prompt` を通し、
    機械注記した note が note 表の `origin` 列に出ることを pin する。"""
    from agentic_fx.loops.improve_context import build_improve_context
    from agentic_fx.loops.improve_loop import ImproveLoop
    from agentic_fx.store.db import connect, init_db
    from tests.loops.conftest import SETTINGS

    c = connect(tmp_path / "ctx.db")
    init_db(c)
    flagged = backlog_store.upsert_system_note(
        c, idea="flagged wired note", last_result="origin:unprofitable",
        now=_NOW)
    clean = backlog_store.upsert_system_note(
        c, idea="clean wired note", last_result="human_noted", now=_NOW)
    c.commit()

    ctx_data = build_improve_context(
        c, settings=SETTINGS, now=_NOW, root=tmp_path,
        allowed_backlog_ids=None)
    loop = ImproveLoop.__new__(ImproveLoop)
    loop._settings = SETTINGS
    text = loop._render_improve_mission_prompt(
        ctx_data, ctx=_FakeRunContext(tmp_path / "staging",
                                      tmp_path / "source"))
    c.close()

    assert _origin_cell(text, flagged) == "unprofitable"
    assert _origin_cell(text, clean) == ""


def test_s3_origin_marker_updates_updated_at_to_now(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    """段0 M4: UPDATE から `updated_at=?` を落としても既存 pin は全て緑
    (どのテストも INSERT と終端で同じ `now` を使っていた)。設計書 §2-2 の
    `updated_at=?` を、終端時刻が起票時刻と異なる条件で pin する。"""
    from datetime import timedelta

    mission_id, run_id, backlog_id = mission_and_run_fixture
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    note_id = backlog_store.add(conn, "stale note", "agent", _NOW)
    later = _NOW + timedelta(hours=3)
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={})
    ledger.freeze()
    ctx = ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging_dir,
        source_snapshot_dir=tmp_path / "source", allowed_backlog_ids=None,
        slot_key=None, ledger=ledger, rpc_handlers={})

    loop_min._finalize_gate_failed(
        conn, ctx=ctx, backlog_id=backlog_id, reason="unprofitable",
        now=later, mission_outcome="unprofitable", inserted_ids=(note_id,))

    row = conn.execute(
        "SELECT last_result, updated_at FROM improvement_backlog WHERE id=?",
        (note_id,)).fetchone()
    assert row["last_result"] == "origin:unprofitable"
    assert row["updated_at"] == later.isoformat()


def test_s4_origin_marker_touches_only_listed_ids(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    """段0 M5: `WHERE id IN (...)` を `WHERE id >= min(inserted_ids)` に
    緩めても既存 pin は全て緑だった (既存テストでは起票行が常に最大 id)。
    `inserted_ids` に無い、かつ id がより大きい行が注記されないことを
    pin する (無関係な backlog 行の `last_result` を機械注記で潰すと、
    その行の実際の終端理由が失われる)。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    marked_id = backlog_store.add(conn, "inserted by this mission", "agent", _NOW)
    bystander_id = backlog_store.add(conn, "unrelated newer row", "user", _NOW)
    assert bystander_id > marked_id
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={})
    ledger.freeze()
    ctx = ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging_dir,
        source_snapshot_dir=tmp_path / "source", allowed_backlog_ids=None,
        slot_key=None, ledger=ledger, rpc_handlers={})

    loop_min._finalize_gate_failed(
        conn, ctx=ctx, backlog_id=backlog_id, reason="unprofitable",
        now=_NOW, mission_outcome="unprofitable", inserted_ids=(marked_id,))

    rows = {r["id"]: r["last_result"] for r in conn.execute(
        "SELECT id, last_result FROM improvement_backlog")}
    assert rows[marked_id] == "origin:unprofitable"
    assert rows[bystander_id] is None


def test_s5_discipline_5_full_block_and_renumbering(tmp_path):
    """段0 M6/M13: N7 は規律 5 の第 1・2 文しか見ておらず、第 3 文
    (「試すなら明確にパラメータを変え、その理由を `selection_rationale` に
    書いてください。」) を消しても緑、番号を `5.` から `4.` に変えて
    (既存項目 4 と重複) も緑だった。設計書 §2-4 の逐語ブロック全体と、
    実装者が申告した繰り下げ (既存の「出力は必ず」項目が `6.`) を pin
    する。"""
    text = _render(tmp_path)

    assert (
        "5. **`origin` 列が `unprofitable` の課題・note は、その mission の"
        "候補が収益性フロアで落ちたときに書かれたものです。**\n"
        "   同じ指標・同じパラメータの候補を再提出しないでください。"
        "試すなら明確にパラメータを変え、その理由を `selection_rationale` に"
        "書いてください。\n") in text
    assert "6. 出力は必ず下の「最終出力」の形式" in text


# ---------------------------------------------------------------------------
# ローカル LLM レビュー 1 周目 (2026-09-13) で採った pin (L1/L2)。
# `tmp/review-20260913-nh/local-triage.md`。
# ---------------------------------------------------------------------------

def test_l1_selected_new_idea_insert_is_tracked_in_inserted_ids(
        loop_and_ctx_with_open_backlog):
    """L1 (muse c1): `_select_and_bind` には INSERT 経路が 2 つある —
    discoveries ループの `_upsert_backlog_idea` と、`selected` が既存に
    無い新規 idea だったときの `_upsert_backlog_idea`。既存 pin
    (`test_n3_*`) は前者しか見ておらず、後者の
    `if selected_action == "inserted": inserted_ids.append(backlog_id)`
    を丸ごと削除しても `tests/loops/` 全体が緑だった (probe 実測 SURVIVED)。
    `_SelectionOutcome` の構造契約 (`_upsert_backlog_idea` が返した
    inserted 行の id を漏れなく `inserted_ids` へ伝播すること) を守る
    white-box pin (codex r1 Minor L1 是正、2026-09-13: `finish_improve_
    mission` の `backlog_transition` による上書きで最終 DB 状態は等価に
    なりうるため、利用者影響 — 機械注記欠落 — としては説明しない)。"""
    loop, ctx, conn, _backlog_id = loop_and_ctx_with_open_backlog

    output = {
        "discoveries": [],
        "selected": {"idea": "a brand new idea chosen by the agent",
                     "source": "agent"},
        "artifact": {"type": "observation", "reason": "x"},
        "selection_rationale": "x"}

    outcome = loop._select_and_bind(conn, output, ctx, now=_NOW)

    new_row = conn.execute(
        "SELECT id FROM improvement_backlog WHERE idea=?",
        ("a brand new idea chosen by the agent",)).fetchone()
    assert new_row is not None
    assert outcome.backlog_id == new_row["id"]
    assert new_row["id"] in outcome.inserted_ids


def test_l2_backlog_table_header_declares_origin_column(tmp_path):
    """L2 (muse c3 + qwen c3): `test_n6`/`test_s1` はデータ行のセルしか
    見ておらず、`_backlog_table` のヘッダ行から `origin` 列を削っても
    (セルはそのまま) `tests/loops/` 全体が緑だった (probe 実測 SURVIVED)。
    ヘッダが欠けると markdown 表の列数がセル側と食い違い、規律 5 が指す
    「`origin` 列」を agent が同定できない。課題表・note 表の両方で
    ヘッダ行と区切り行の列数を pin する。"""
    text = _render(tmp_path)

    selectable, notes = text.split("## 既知の事実", 1)
    for section in (selectable, notes):
        header = next(
            l for l in section.splitlines() if l.startswith("| id |"))
        assert header == (
            "| id | idea | status | attempts | assigned | origin |")
        separator = next(
            l for l in section.splitlines() if l.startswith("|---|"))
        assert separator.count("|") == header.count("|") == 7
