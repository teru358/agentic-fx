# tests/loops/test_unprofitable_note_hygiene.py
"""[unprofitable-note-hygiene] 設計書 v2.0 §4 (N1〜N12) の pin。

`docs/superpowers/specs/2026-09-13-unprofitable-note-hygiene-design.md`。
A4 run19 観測 D: mission が収益性フロアで `unprofitable` に落ちたとき、
同じ mission が起票した note/task backlog 行 (agent 自筆・in_sample 語彙)
に系譜 (`origin_mission_id`) と終端結果 (`origin_outcome`) を専用列で
持たせ、次 mission の同型再提出を prompt 上で抑止する。

v2.0 は `last_result` 相乗り方式 (v1.x) を撤回した作り直し
(`/code-review high` 5 件、設計書 §2「なぜ作り直すか」)。専用列は他の
書き手 (終端・note 昇格・人間コマンド) と干渉しないため、v1.x の
CAS (`AND last_result IS NULL`)・昇格時の保持分岐・`inserted_ids` は
すべて撤回。fixture 流儀は `tests/loops/test_improve_loop_finalize.py`/
`tests/loops/test_improve_loop_tx1_selection.py`/
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


def _mk_ctx(mission_id, run_id, staging_dir, source_snapshot_dir, *,
           frozen=False):
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={})
    if frozen:
        ledger.freeze()
    return ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging_dir,
        source_snapshot_dir=source_snapshot_dir, allowed_backlog_ids=None,
        slot_key=None, ledger=ledger, rpc_handlers={})


def _commit_unprofitable_mission(
        loop_full, conn, mission_and_run_fixture, tmp_path, monkeypatch, *,
        holdout_metrics, extra_discoveries: list[dict] | None = None):
    mission_id, run_id, backlog_id = mission_and_run_fixture
    staging_dir = tmp_path / "staging"
    _write_candidate(staging_dir, "myst")
    ctx = _mk_ctx(mission_id, run_id, staging_dir, tmp_path / "source")
    mission = Mission(prompt="x", tools=[], output_schema={}, max_turns=10,
                      timeout_sec=60)
    discoveries = [
        {"idea": "fast=10/slow=30 は提出条件を満たした", "source": "agent",
         "evidence": "e", "kind": "fact"},
        {"idea": "次は fast=12/slow=28 を試す", "source": "agent",
         "evidence": "e", "kind": "task"},
    ] + (extra_discoveries or [])
    selected = {"backlog_id": backlog_id, "idea": "x"}
    result = MissionResult(status="completed", output={
        "discoveries": discoveries,
        "selected": selected,
        "artifact": {"type": "plugin", "name": "myst", "kind": "strategy",
                    "self_test": "passed", "summary": "s"},
        "selection_rationale": "r"}, transcript=[])
    _install_floor_strategy_gate(monkeypatch, loop_full, holdout_metrics=holdout_metrics)

    loop_full.commit(mission=mission, ctx=ctx, result=result, now=_NOW)
    return conn


def _idea_row(conn, idea: str):
    row = conn.execute(
        "SELECT id, idea, idea_norm, status, last_result, origin_mission_id, "
        "origin_outcome FROM improvement_backlog WHERE idea=?",
        (idea,)).fetchone()
    assert row is not None, f"backlog row not found for idea={idea!r}"
    return row


def _row_by_id(conn, backlog_id: int):
    row = conn.execute(
        "SELECT id, idea, idea_norm, status, last_result, origin_mission_id, "
        "origin_outcome FROM improvement_backlog WHERE id=?",
        (backlog_id,)).fetchone()
    assert row is not None
    return row


# ---------------------------------------------------------------------------
# N1: フロア不合格 (in_sample 段 / holdout 段の両方) で終わった mission が
# INSERT した note と task の origin_outcome == 'unprofitable',
# origin_mission_id == mission_id
# ---------------------------------------------------------------------------

def test_n1_in_sample_floor_annotates_inserted_note_and_task(
        loop_full, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    mission_id, _run_id, _backlog_id = mission_and_run_fixture
    _commit_unprofitable_mission(
        loop_full, conn, mission_and_run_fixture, tmp_path, monkeypatch,
        holdout_metrics=None)

    task_row = _idea_row(conn, "次は fast=12/slow=28 を試す")
    assert task_row["origin_outcome"] == "unprofitable"
    assert task_row["origin_mission_id"] == mission_id
    note_row = _idea_row(conn, "fast=10/slow=30 は提出条件を満たした")
    assert note_row["origin_outcome"] == "unprofitable"
    assert note_row["origin_mission_id"] == mission_id


def test_n1_holdout_floor_annotates_inserted_note_and_task(
        loop_full, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    mission_id, _run_id, _backlog_id = mission_and_run_fixture
    _commit_unprofitable_mission(
        loop_full, conn, mission_and_run_fixture, tmp_path, monkeypatch,
        holdout_metrics={"USDJPY": _floor_metrics(0.5)})

    task_row = _idea_row(conn, "次は fast=12/slow=28 を試す")
    assert task_row["origin_outcome"] == "unprofitable"
    assert task_row["origin_mission_id"] == mission_id
    note_row = _idea_row(conn, "fast=10/slow=30 は提出条件を満たした")
    assert note_row["origin_outcome"] == "unprofitable"
    assert note_row["origin_mission_id"] == mission_id


# ---------------------------------------------------------------------------
# N2: gate_failed:* / insufficient_trades / report / observation / approval /
# report_failed 分岐では origin_outcome が NULL (逆変異: 条件を外すと red)
# ---------------------------------------------------------------------------

def test_n2_default_gate_failed_outcome_leaves_origin_outcome_null(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    """`mission_outcome` 既定 (`gate_failed`) で `_finalize_gate_failed` を
    呼んでも `origin_mission_id=mission_id` の行の `origin_outcome` は
    書かれない。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    note_id, _ = loop_min._upsert_backlog_idea(
        conn, "insufficient trades note", "agent", "fact", _NOW,
        mission_id=mission_id)
    conn.commit()
    ctx = _mk_ctx(mission_id, run_id, staging_dir, tmp_path / "source",
                  frozen=True)

    loop_min._finalize_gate_failed(
        conn, ctx=ctx, backlog_id=backlog_id, reason="insufficient_trades:5",
        now=_NOW)

    row = _row_by_id(conn, note_id)
    assert row["origin_outcome"] is None


def test_n2_report_failed_branch_leaves_origin_outcome_null(
        loop_min, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    """report part 作成失敗 (`report_failed` 固定分岐) では、呼び出し元が
    `mission_outcome="unprofitable"` を渡しても `origin_outcome` は
    書かれない (report 作成失敗は origin UPDATE より前の分岐)。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    note_id, _ = loop_min._upsert_backlog_idea(
        conn, "report write failure note", "agent", "fact", _NOW,
        mission_id=mission_id)
    conn.commit()
    ctx = _mk_ctx(mission_id, run_id, staging_dir, tmp_path / "source",
                  frozen=True)
    monkeypatch.setattr(
        loop_min, "_write_report_part",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("write failed")))

    loop_min._finalize_gate_failed(
        conn, ctx=ctx, backlog_id=backlog_id, reason="unprofitable", now=_NOW,
        mission_outcome="unprofitable")

    row = _row_by_id(conn, note_id)
    assert row["origin_outcome"] is None


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
    commit-level fixture で、任意の終端 `artifact` を通す (N2: report/
    observation/approval の 3 終端で `origin_outcome` が書かれないことを
    pin するための共有 helper)。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    staging_dir = tmp_path / "staging"
    if write_plugin:
        _write_indicator_candidate(staging_dir, artifact["name"])
    else:
        staging_dir.mkdir(parents=True, exist_ok=True)
    ctx = _mk_ctx(mission_id, run_id, staging_dir, tmp_path / "source")
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


def test_n2_report_outcome_leaves_origin_outcome_null(
        loop_full, conn, mission_and_run_fixture, tmp_path):
    """report 終端 (`_finalize_report_or_observation` 経由、
    `artifact.type=='report'`) を discovery の note/task を持つ
    commit-level fixture で実際に通し、両行の `origin_outcome` が NULL の
    ままであることを pin する。"""
    _commit_terminal_with_discoveries(
        loop_full, conn, mission_and_run_fixture, tmp_path,
        artifact={"type": "report", "proposal_kind": "core",
                  "title": "report terminal", "body_md": "body"},
        note_idea="report 終端の note", task_idea="report 終端の task")

    assert _idea_row(conn, "report 終端の note")["origin_outcome"] is None
    assert _idea_row(conn, "report 終端の task")["origin_outcome"] is None


def test_n2_observation_outcome_leaves_origin_outcome_null(
        loop_full, conn, mission_and_run_fixture, tmp_path):
    """observation 終端 (`_finalize_report_or_observation` 経由、
    `artifact.type=='observation'`) を discovery の note/task を持つ
    commit-level fixture で実際に通し、両行の `origin_outcome` が NULL の
    ままであることを pin する。"""
    _commit_terminal_with_discoveries(
        loop_full, conn, mission_and_run_fixture, tmp_path,
        artifact={"type": "observation", "reason": "no candidate"},
        note_idea="observation 終端の note", task_idea="observation 終端の task")

    assert _idea_row(conn, "observation 終端の note")["origin_outcome"] is None
    assert _idea_row(conn, "observation 終端の task")["origin_outcome"] is None


def test_n2_approval_outcome_leaves_origin_outcome_null(
        loop_full, conn, mission_and_run_fixture, tmp_path):
    """approval 終端 (`_finalize_success` 経由、plugin/indicator がゲート
    合格し承認申請を出す経路) を discovery の note/task を持つ commit-level
    fixture で実際に通し、両行の `origin_outcome` が NULL のままであること
    を pin する (indicator は strategy gate — 収益性フロア判定 — を経由
    しないため、この終端が `unprofitable` 分岐へ誤って迷い込まないことも
    合わせて確認する)。"""
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
    assert _idea_row(conn, "approval 終端の note")["origin_outcome"] is None
    assert _idea_row(conn, "approval 終端の task")["origin_outcome"] is None


# ---------------------------------------------------------------------------
# N3: 既存行の再利用 (existing / promoted) は origin_mission_id を
# 最初の起票者のまま保つ (書き換えない)
# ---------------------------------------------------------------------------

def test_n3_selected_existing_task_reuses_without_rewriting_origin(
        loop_and_ctx_with_open_backlog):
    """discoveries 側の重複は `_upsert_backlog_idea` より前の `continue`
    で除外されるため existing 戻り値を実際には通らない。ここでは
    `selected.backlog_id` を省略し `selected.idea` を既存 task の idea と
    一致させ、`_upsert_backlog_idea(...) -> "existing"` を実際に通す経路
    (`_select_and_bind` の `selected` 分岐) を pin する。既存行の
    `origin_mission_id` (この場合 NULL — `backlog_store.add` は書かない)
    がこの mission の id へ書き換わらないことを確認する。"""
    loop, ctx, conn, _backlog_id = loop_and_ctx_with_open_backlog
    existing_id = backlog_store.add(conn, "already known idea", "user", _NOW)

    output = {
        "discoveries": [],
        "selected": {"idea": "already known idea", "source": "agent"},
        "artifact": {"type": "observation", "reason": "x"},
        "selection_rationale": "x"}

    outcome = loop._select_and_bind(conn, output, ctx, now=_NOW)

    assert outcome.backlog_id == existing_id
    row = _row_by_id(conn, existing_id)
    assert row["origin_mission_id"] is None


def test_n3_promoted_note_to_task_keeps_original_origin_mission_id(
        loop_and_ctx_with_open_backlog):
    """既存 note を `kind=task` の discovery が昇格 (`promoted`) させる
    経路も `origin_mission_id` を書き換えない (最初の起票者のまま)。"""
    loop, ctx, conn, backlog_id = loop_and_ctx_with_open_backlog
    other_mission_id = ctx.mission_id + 999
    conn.execute(
        "INSERT INTO improvement_backlog (idea, source, status, created_at, "
        "updated_at, idea_norm, origin_mission_id) VALUES "
        "('pre-existing note','agent','note',?,?,?,?)",
        (_NOW.isoformat(), _NOW.isoformat(), "pre-existing note",
         other_mission_id))
    note_id = conn.execute(
        "SELECT id FROM improvement_backlog WHERE idea='pre-existing note'"
        ).fetchone()["id"]
    conn.commit()

    output = {
        "discoveries": [
            {"idea": "pre-existing note", "source": "agent", "evidence": "e",
             "kind": "task"},
        ],
        "selected": {"backlog_id": backlog_id, "idea": "x"},
        "artifact": {"type": "observation", "reason": "x"},
        "selection_rationale": "x"}

    loop._select_and_bind(conn, output, ctx, now=_NOW)

    row = _row_by_id(conn, note_id)
    assert row["status"] == "open"  # promoted
    assert row["origin_mission_id"] == other_mission_id  # 書き換わらない


# ---------------------------------------------------------------------------
# N3': 同一 _select_and_bind 内で note を INSERT → 同 idea の task で
# 自己昇格 → フロア不合格 → その行の origin_outcome == 'unprofitable'
# (v1.x で消えた経路、code-review #1)
# ---------------------------------------------------------------------------

def test_n3prime_same_tx_self_promotion_still_gets_annotated(
        loop_full, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    mission_id, _run_id, _backlog_id = mission_and_run_fixture
    _commit_unprofitable_mission(
        loop_full, conn, mission_and_run_fixture, tmp_path, monkeypatch,
        holdout_metrics=None,
        extra_discoveries=[
            {"idea": "dup idea self promotes", "source": "agent",
             "evidence": "e", "kind": "fact"},
            {"idea": "dup idea self promotes", "source": "agent",
             "evidence": "e", "kind": "task"},
        ])

    row = _idea_row(conn, "dup idea self promotes")
    assert row["status"] == "open"  # 昇格どおり
    assert row["last_result"] == "promoted_from_note"  # 昇格時の定数のまま
    assert row["origin_mission_id"] == mission_id  # 最初の INSERT (note) の系譜
    assert row["origin_outcome"] == "unprofitable"  # このミッション自身の終端で付く


# ---------------------------------------------------------------------------
# N4: 同 tx: `_finalize_gate_failed` が正常分岐 tx 内で後段失敗すると
# origin_outcome も rollback する
# ---------------------------------------------------------------------------

def test_n4_rollback_in_same_tx_undoes_origin_outcome(
        loop_min, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    """report part 作成失敗は注記 tx より前 (origin_outcome を一度も書か
    ない) のため atomicity の probe にならない — その分岐は N2 の非書込み
    基準側で扱う。ここで検査するのは、正常分岐 tx の origin UPDATE 後、
    同一 tx 内の `finish_improve_mission` が例外を送出すると、先に書いた
    `origin_outcome` も rollback で消えることを pin する。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    note_id, _ = loop_min._upsert_backlog_idea(
        conn, "rollback candidate note", "agent", "fact", _NOW,
        mission_id=mission_id)
    conn.commit()
    ctx = _mk_ctx(mission_id, run_id, staging_dir, tmp_path / "source",
                  frozen=True)

    from agentic_fx.store import missions as missions_mod

    def _boom(*a, **kw):
        raise RuntimeError("simulated fault after origin UPDATE")

    monkeypatch.setattr(missions_mod, "finish_improve_mission", _boom)

    with pytest.raises(RuntimeError):
        loop_min._finalize_gate_failed(
            conn, ctx=ctx, backlog_id=backlog_id, reason="unprofitable",
            now=_NOW, mission_outcome="unprofitable")

    row = _row_by_id(conn, note_id)
    assert row["origin_outcome"] is None


# ---------------------------------------------------------------------------
# N5: idea / idea_norm / status / last_result 不変 (注記は last_result を
# 一切書かない)
# ---------------------------------------------------------------------------

def test_n5_origin_annotation_does_not_touch_idea_idea_norm_status_or_last_result(
        loop_full, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    conn = _commit_unprofitable_mission(
        loop_full, conn, mission_and_run_fixture, tmp_path, monkeypatch,
        holdout_metrics=None)

    task_row = _idea_row(conn, "次は fast=12/slow=28 を試す")
    assert task_row["idea"] == "次は fast=12/slow=28 を試す"
    assert task_row["idea_norm"] == "次は fast=12/slow=28 を試す".strip().lower()
    assert task_row["status"] == "open"  # kind=task の既定 status のまま
    assert task_row["last_result"] is None  # 専用列方式: last_result は不変
    assert task_row["origin_outcome"] == "unprofitable"

    note_row = _idea_row(conn, "fast=10/slow=30 は提出条件を満たした")
    assert note_row["idea"] == "fast=10/slow=30 は提出条件を満たした"
    assert (note_row["idea_norm"]
            == "fast=10/slow=30 は提出条件を満たした".strip().lower())
    assert note_row["status"] == "note"  # kind=fact の既定 status のまま
    assert note_row["last_result"] is None
    assert note_row["origin_outcome"] == "unprofitable"


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
             "assigned": True, "last_result": None, "origin_outcome": None},
            {"id": 3, "idea": "flagged item", "status": "open", "attempts": 1,
             "assigned": False, "last_result": None,
             "origin_outcome": "unprofitable"},
            # おとり: ゲート終端が書く実際の last_result
            # (`unprofitable` — フロア不合格候補そのものの行、`observation`
            # で `list_open` に残る)。`origin_outcome` は別列 (この行の
            # 値は NULL) なので、origin 列は空欄でなければならない。
            {"id": 6, "idea": "the candidate row itself", "status": "observation",
             "attempts": 2, "assigned": False, "last_result": "unprofitable",
             "origin_outcome": None},
        ], "notes": [
            {"id": 4, "idea": "clean note", "status": "note", "attempts": 0,
             "last_result": None, "origin_outcome": None},
            {"id": 5, "idea": "flagged note", "status": "note", "attempts": 0,
             "last_result": None, "origin_outcome": "unprofitable"},
            # おとり (notes 側): 別語彙 (`promoted_from_note`)。
            {"id": 7, "idea": "other machine note", "status": "note",
             "attempts": 0, "last_result": "promoted_from_note",
             "origin_outcome": None},
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


def _origin_cell(text: str, row_id: int) -> str:
    """レンダ済み prompt から `| <id> | ... |` 行を拾い、末尾の
    `origin` セルを返す。"""
    line = next(l for l in text.splitlines() if l.startswith(f"| {row_id} |"))
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return cells[-1]


def test_n6_backlog_table_has_origin_column_flagging_only_annotated_rows(
        tmp_path):
    text = _render(tmp_path)

    selectable, notes = text.split("## 既知の事実", 1)
    # items 表: id=2 (clean) は origin 空欄、id=3 (flagged) は unprofitable
    assert "| 2 | clean item |" in selectable
    assert "| 3 | flagged item |" in selectable
    assert _origin_cell(selectable, 2) == ""
    assert _origin_cell(selectable, 3) == "unprofitable"
    # id=6 はおとり (last_result=='unprofitable' だが origin_outcome は NULL)
    assert _origin_cell(selectable, 6) == ""
    # notes 表: id=4 (clean) は空欄、id=5 (flagged) は unprofitable
    assert _origin_cell(notes, 4) == ""
    assert _origin_cell(notes, 5) == "unprofitable"
    # id=7 はおとり (別語彙の last_result、origin_outcome は NULL)
    assert _origin_cell(notes, 7) == ""


def test_n7_discipline_5_appears_verbatim_and_holdout_pin_stays_green(
        tmp_path):
    text = _render(tmp_path)

    assert ("`origin` 列が `unprofitable` の課題・note は、その mission の"
           "候補が収益性フロアで落ちたときに書かれたものです。") in text
    assert "同じ指標・同じパラメータの候補を再提出しないでください。" in text
    # 既存遮断 pin (F5-2 系): holdout の数値・段名は prompt に出ない。
    assert "holdout" not in text


def test_backlog_table_header_declares_origin_column(tmp_path):
    """ヘッダ行から `origin` 列を削っても (セルはそのまま) 緑にならない
    ことを保証する。ヘッダが欠けると markdown 表の列数がセル側と食い違い、
    規律 5 が指す「`origin` 列」を agent が同定できない。課題表・note 表の
    両方でヘッダ行と区切り行の列数を pin する。"""
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


def test_s5_discipline_5_full_block_and_renumbering(tmp_path):
    """規律 5 の第 1・2・3 文と番号 (`5.` 新設 / 既存 `4.` が `6.` へ
    繰り下げ) を逐語で pin する。"""
    text = _render(tmp_path)

    assert (
        "5. **`origin` 列が `unprofitable` の課題・note は、その mission の"
        "候補が収益性フロアで落ちたときに書かれたものです。**\n"
        "   同じ指標・同じパラメータの候補を再提出しないでください。"
        "試すなら明確にパラメータを変え、その理由を `selection_rationale` に"
        "書いてください。\n") in text
    assert "6. 出力は必ず下の「最終出力」の形式" in text


def test_s2_build_improve_context_carries_note_origin_outcome_to_column(
        tmp_path):
    """配線: N6 は `ctx_data` を手書きするため、
    `improve_context._backlog_section` が **notes** 行に `origin_outcome`
    を載せている配線を誰も検証していなかった (items 側は
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
        c, idea="flagged wired note", last_result="human_noted", now=_NOW)
    c.execute(
        "UPDATE improvement_backlog SET origin_outcome='unprofitable' "
        "WHERE id=?", (flagged,))
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


# ---------------------------------------------------------------------------
# N8: 当該行が後に選択されて終端しても origin_outcome は残る
# (last_result は終端値、origin 列は unprofitable のまま)
# ---------------------------------------------------------------------------

def test_n8_annotated_row_origin_outcome_persists_after_later_termination(
        loop_full, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    conn = _commit_unprofitable_mission(
        loop_full, conn, mission_and_run_fixture, tmp_path, monkeypatch,
        holdout_metrics=None)
    annotated_id = _idea_row(conn, "次は fast=12/slow=28 を試す")["id"]
    assert _row_by_id(conn, annotated_id)["origin_outcome"] == "unprofitable"

    # 別 mission がこの行を選び、標本不足で終端する。
    mission_id2 = missions_store.start(
        conn, "improve", "codex", "gpt-5", _NOW, commit=True)
    from agentic_fx.store import improve_runs as improve_runs_store
    run_id2 = improve_runs_store.start(
        conn, None, _NOW, mission_id=mission_id2, commit=True)
    staging_dir2 = tmp_path / "staging2"
    staging_dir2.mkdir()
    ctx2 = _mk_ctx(mission_id2, run_id2, staging_dir2, tmp_path / "source2",
                   frozen=True)

    loop_full._finalize_gate_failed(
        conn, ctx=ctx2, backlog_id=annotated_id,
        reason="insufficient_trades:2", now=_NOW)

    row = _row_by_id(conn, annotated_id)
    assert row["last_result"] == "insufficient_trades:2"
    assert row["origin_outcome"] == "unprofitable"  # 別列なので消えない


# ---------------------------------------------------------------------------
# N9: 逆順並行 — A が INSERT → B が選択・終端 → A が unprofitable 終端 →
# B の last_result は不変、origin_outcome は付く (別列なので両立)
# ---------------------------------------------------------------------------

def test_n9_out_of_order_termination_and_origin_annotation_coexist(
        loop_full, conn, mission_and_run_fixture, tmp_path):
    mission_id_a, run_id_a, _ = mission_and_run_fixture
    note_id, _ = loop_full._upsert_backlog_idea(
        conn, "concurrent note idea", "agent", "fact", _NOW,
        mission_id=mission_id_a)
    conn.commit()

    # mission B が同じ行を選び、標本不足で先に終端する。
    mission_id_b = missions_store.start(
        conn, "improve", "codex", "gpt-5", _NOW, commit=True)
    from agentic_fx.store import improve_runs as improve_runs_store
    run_id_b = improve_runs_store.start(
        conn, None, _NOW, mission_id=mission_id_b, commit=True)
    staging_dir_b = tmp_path / "staging_b"
    staging_dir_b.mkdir()
    ctx_b = _mk_ctx(mission_id_b, run_id_b, staging_dir_b,
                    tmp_path / "source_b", frozen=True)
    loop_full._finalize_gate_failed(
        conn, ctx=ctx_b, backlog_id=note_id,
        reason="insufficient_trades:2", now=_NOW)

    row = _row_by_id(conn, note_id)
    assert row["last_result"] == "insufficient_trades:2"
    assert row["origin_outcome"] is None

    # mission A がいまさら注記 tx を実行する (後発だが起票は先だった)。
    staging_dir_a = tmp_path / "staging_a"
    staging_dir_a.mkdir()
    ctx_a = _mk_ctx(mission_id_a, run_id_a, staging_dir_a,
                    tmp_path / "source_a", frozen=True)
    loop_full._finalize_gate_failed(
        conn, ctx=ctx_a, backlog_id=None, reason="unprofitable", now=_NOW,
        mission_outcome="unprofitable")

    row = _row_by_id(conn, note_id)
    # 別列なので B の終端値は潰れない。A の系譜注記も両立して付く。
    assert row["last_result"] == "insufficient_trades:2"
    assert row["origin_outcome"] == "unprofitable"


# ---------------------------------------------------------------------------
# N10: 人間コマンド backlog note / reopen / reject を注記行に打っても
# origin_* は不変 (code-review #2)
# ---------------------------------------------------------------------------
# 実装は tests/test_commands.py 側に置く (Commands 経由の統合 pin)。


# ---------------------------------------------------------------------------
# N11: 選択行自身が起票行のとき、last_result == reason かつ
# origin_outcome == 'unprofitable' (code-review #5 の順序依存が消えて
# いること)
# ---------------------------------------------------------------------------

def test_n11_selected_row_is_its_own_origin_row(
        loop_full, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    """`selected` が既存 backlog にも discoveries にも無い新規 idea の
    とき、`_select_and_bind` が INSERT した行がそのまま選択・終端の対象に
    なる — 起票行と終端行が同一行。専用列 (`origin_outcome`/
    `last_result`) は別列なので、v1.x にあった「どちらが後に書かれるか」
    という順序依存は構造的に発生しない。"""
    mission_id, run_id, _fixture_backlog_id = mission_and_run_fixture
    staging_dir = tmp_path / "staging"
    _write_candidate(staging_dir, "myst")
    ctx = _mk_ctx(mission_id, run_id, staging_dir, tmp_path / "source")
    mission = Mission(prompt="x", tools=[], output_schema={}, max_turns=10,
                      timeout_sec=60)
    result = MissionResult(status="completed", output={
        "discoveries": [],
        "selected": {"backlog_id": None, "idea": "brand new self-origin idea"},
        "artifact": {"type": "plugin", "name": "myst", "kind": "strategy",
                    "self_test": "passed", "summary": "s"},
        "selection_rationale": "r"}, transcript=[])
    _install_floor_strategy_gate(monkeypatch, loop_full, holdout_metrics=None)

    loop_full.commit(mission=mission, ctx=ctx, result=result, now=_NOW)

    row = _idea_row(conn, "brand new self-origin idea")
    assert row["origin_mission_id"] == mission_id
    assert row["last_result"] == "unprofitable"  # 終端理由 (backlog_transition)
    assert row["origin_outcome"] == "unprofitable"  # 系譜注記 (別列)


# ---------------------------------------------------------------------------
# N12: 無関係 discovery も注記される (受容トレードオフの pin、逆変異で
# 緩めても red にならない = 記録用)
# ---------------------------------------------------------------------------

def test_n12_unrelated_discovery_is_also_annotated_accepted_tradeoff(
        loop_full, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    """設計書 §2-6: `discoveries` は提出候補との紐付けを持たないため、
    同じ mission が起票した無関係な discovery にも `origin_outcome` が
    付く。これは事前に受容したトレードオフの記録用 pin であり、逆変異
    (この振る舞いを外す) で red を要求しない。"""
    _commit_unprofitable_mission(
        loop_full, conn, mission_and_run_fixture, tmp_path, monkeypatch,
        holdout_metrics=None,
        extra_discoveries=[
            {"idea": "完全に無関係な改善案", "source": "agent",
             "evidence": "e", "kind": "task"},
        ])

    row = _idea_row(conn, "完全に無関係な改善案")
    assert row["origin_outcome"] == "unprofitable"


# ---------------------------------------------------------------------------
# 段 0 v2.0 S6 (生存変異 M8): 注記 UPDATE の WHERE を
# `origin_mission_id IS NOT NULL` に広げても既存 pin は全て緑だった。
# N9 は mission B が自分の行を起票しない (A の行を選ぶだけ) ため、
# 「他 mission が起票した行まで注記が波及する」次元を誰も見ていない。
# 実装レポートは「値等値の WHERE 句は範囲を広げる変異が構造的に成立
# しない」として v1.x の S4 を割愛したが、成立する (実測)。
# ---------------------------------------------------------------------------

def test_s6_origin_update_does_not_annotate_other_missions_rows(
        loop_full, conn, mission_and_run_fixture, tmp_path):
    """mission A と mission B がそれぞれ自分の行を起票し、A だけがフロア
    不合格で終端する。A の行には `origin_outcome` が付き、B の行は NULL の
    まま — 注記は `origin_mission_id=<この mission>` の行に限定される
    (設計書 §2-2「この mission が起票した全行」)。"""
    mission_id_a, run_id_a, _backlog_id = mission_and_run_fixture
    row_a, _ = loop_full._upsert_backlog_idea(
        conn, "mission A idea", "agent", "fact", _NOW, mission_id=mission_id_a)

    mission_id_b = missions_store.start(
        conn, "improve", "codex", "gpt-5", _NOW, commit=True)
    row_b, _ = loop_full._upsert_backlog_idea(
        conn, "mission B idea", "agent", "fact", _NOW, mission_id=mission_id_b)
    # 起票者が誰でもない過去行 (§2-8 遡及しない) も置く。
    legacy = backlog_store.upsert_system_note(
        conn, idea="legacy row without origin", last_result=None, now=_NOW)
    conn.commit()

    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    ctx_a = _mk_ctx(mission_id_a, run_id_a, staging_dir, tmp_path / "source",
                    frozen=True)
    loop_full._finalize_gate_failed(
        conn, ctx=ctx_a, backlog_id=None, reason="unprofitable", now=_NOW,
        mission_outcome="unprofitable")

    assert _row_by_id(conn, row_a)["origin_outcome"] == "unprofitable"
    assert _row_by_id(conn, row_b)["origin_outcome"] is None
    assert _row_by_id(conn, row_b)["origin_mission_id"] == mission_id_b
    assert _row_by_id(conn, legacy)["origin_outcome"] is None


# ---------------------------------------------------------------------------
# 段 0 v2.0 S7 (生存変異 M10): `_backlog_table` の判定を完全一致から
# 部分一致 (`'unprofitable' in str(...)`) に緩めても既存 pin は全て緑
# だった。N6 のおとり行は `origin_outcome` が全て NULL なので、
# 「origin_outcome に別の値が入ったとき」の次元を誰も見ていない。
# 遮断 8: `origin` 列に出る語彙は固定文言 `unprofitable` のみで、
# 派生値 (段名や数値が付いたもの) をそのまま通してはいけない。
# ---------------------------------------------------------------------------

def test_s7_origin_column_matches_fixed_vocabulary_exactly(tmp_path):
    """`origin_outcome` が固定文言 `unprofitable` と完全一致する行だけ
    `unprofitable` と表示する。`unprofitable` を部分文字列として含む値
    (将来の派生値・否定形) は空欄 — 部分一致に緩めると red。"""
    from agentic_fx.loops.improve_loop import ImproveLoop
    from tests.loops.conftest import SETTINGS

    ctx_data = _sample_ctx_data_with_origin()
    ctx_data["backlog"]["items"].append(
        {"id": 8, "idea": "derived value item", "status": "open",
         "attempts": 0, "assigned": False, "last_result": None,
         "origin_outcome": "unprofitable:holdout_pf=0.53"})
    ctx_data["backlog"]["notes"].append(
        {"id": 9, "idea": "negated value note", "status": "note",
         "attempts": 0, "last_result": None,
         "origin_outcome": "not_unprofitable"})
    loop = ImproveLoop.__new__(ImproveLoop)
    loop._settings = SETTINGS
    text = loop._render_improve_mission_prompt(
        ctx_data, ctx=_FakeRunContext(tmp_path / "staging",
                                      tmp_path / "source"))

    assert _origin_cell(text, 8) == ""
    assert _origin_cell(text, 9) == ""
    # 遮断 8: 派生値の中身 (段名・数値) は prompt に漏れない。
    assert "holdout" not in text
    assert "0.53" not in text


# ---------------------------------------------------------------------------
# 段 0 v2.0 S8 (生存変異 M11): `improve_context._backlog_section` の
# **items** 側 dict から `origin_outcome` を落としても既存 pin は全て緑
# だった。実装レポートは「items 側は tests/test_improve_context.py の
# 既存 pin が守っている」と書いているが、同ファイルに `origin_outcome`
# の文字列は 1 つも無い (実測)。S2 は notes 側だけを配線検証している。
# ---------------------------------------------------------------------------

def test_s8_build_improve_context_carries_item_origin_outcome_to_column(
        tmp_path):
    """配線 (items 側): 実 DB → `build_improve_context` →
    `_render_improve_mission_prompt` を通し、機械注記した **選択可能な
    課題行** (`list_open` 側) が課題表の `origin` 列に出ることを pin する
    (S2 の items 版)。"""
    from agentic_fx.loops.improve_context import build_improve_context
    from agentic_fx.loops.improve_loop import ImproveLoop
    from agentic_fx.store.db import connect, init_db
    from tests.loops.conftest import SETTINGS

    c = connect(tmp_path / "ctx_items.db")
    init_db(c)
    flagged = c.execute(
        "INSERT INTO improvement_backlog "
        "(idea,source,status,created_at,updated_at,idea_norm,origin_outcome) "
        "VALUES ('flagged wired item','agent','open',?,?,"
        "'flagged wired item','unprofitable')",
        (_NOW.isoformat(), _NOW.isoformat())).lastrowid
    clean = c.execute(
        "INSERT INTO improvement_backlog "
        "(idea,source,status,created_at,updated_at,idea_norm) "
        "VALUES ('clean wired item','agent','open',?,?,'clean wired item')",
        (_NOW.isoformat(), _NOW.isoformat())).lastrowid
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

    selectable = text.split("## 既知の事実", 1)[0]
    assert _origin_cell(selectable, flagged) == "unprofitable"
    assert _origin_cell(selectable, clean) == ""
