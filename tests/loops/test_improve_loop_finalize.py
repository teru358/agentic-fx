# tests/loops/test_improve_loop_finalize.py
"""ImproveLoop.commit 統合 (設計書 §4.2、プラン 10.11〜10.13)。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.loops.improve_run_context import ImproveRunContext
from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
from agentic_fx.runners.base import Mission, MissionResult


def _write_candidate(staging_dir: Path, name: str) -> None:
    """candidate plugin directory を staging_dir/<name>/ へ作成し、
    基本的な plugin.py + config.yaml + test_plugin.py を書く (happy path 用)。
    プラン実装時に、より詳細な plugin 内容が必要な場合は、
    tests/plugin/test_switch_paths.py::_write_candidate を参照する。"""
    candidate_dir = staging_dir / name
    candidate_dir.mkdir(parents=True, exist_ok=True)
    (candidate_dir / "plugin.py").write_text(
        "def compute(df, params):\n    return {'v': 1.0}\n")
    (candidate_dir / "config.yaml").write_text(
        "kind: indicator\npairs: ['USDJPY']\ntimeframe: '1h'\n")
    (candidate_dir / "test_plugin.py").write_text(
        "def test_x():\n    pass\n")


def test_commit_runs_all_nine_steps_in_order_for_happy_path_plugin(
        loop_full, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    """End-to-end (fake CLI 出力を使う) — 発見→選択→plugin ゲート合格→
    承認申請→レポート準備→Tx-2→finish、の一連が 1 回の commit() 呼び出し
    で完了することを確認する (E2E は Task 12 で更に厚く検証されるが、
    Task 10 単体でも happy path を通す)。

    **レビュー1周目 I4**: 最終状態 (`missions.status`/`improvement_runs.result`)
    だけを見る旧版は、`_freeze_ledger` を Tx-1 の後へ移す・approval を
    gate row 保存前へ移す等の順序退行があっても最終 2 列が同じなら通って
    しまう恒真に近いテストだった。以下は各 private step メソッドに
    recorder を monkeypatch で注入し、呼び出し**順序**そのものを設計書
    §4.2 の手順 0〜9 の期待列と完全一致で assert する。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    staging_dir = tmp_path / "staging"
    _write_candidate(staging_dir, "myind")

    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"analyze_corr": 60.0,
                                                        "run_backtest": 600.0})
    ctx = ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging_dir,
        source_snapshot_dir=tmp_path / "source",
        allowed_backlog_ids=None, slot_key=None, ledger=ledger,
        rpc_handlers={})
    mission = Mission(prompt="x", tools=[], output_schema={}, max_turns=10,
                      timeout_sec=60)
    result = MissionResult(status="completed", output={
        "discoveries": [], "selected": {"backlog_id": backlog_id, "idea": "x"},
        "artifact": {"type": "plugin", "name": "myind", "kind": "indicator",
                    "self_test": "passed", "summary": "s"},
        "selection_rationale": "r"}, transcript=[])

    # §4.2 手順 0〜9 のうち happy path (plugin, indicator, ゲート全通過) が
    # 実際に到達する private メソッド名の期待列 (手順4 の strategy ゲートは
    # kind="indicator" なので到達しない — スキップは意図どおり)。
    _EXPECTED_ORDER = [
        "_freeze_ledger",        # 手順0
        "_inspect_output",       # 手順1
        "_select_and_bind",      # 手順2
        "_run_plugin_gate",      # 手順3
        "_build_approval_payload",  # 手順5
        "_finalize_success",     # 手順7-9 (Tx-2 組み立て + finish_improve_mission)
    ]
    call_order: list[str] = []

    def _make_recorder(name):
        original = getattr(loop_full, name)

        def _recorder(*a, **kw):
            call_order.append(name)
            return original(*a, **kw)
        return _recorder

    for name in _EXPECTED_ORDER:
        monkeypatch.setattr(loop_full, name, _make_recorder(name))

    loop_full.commit(mission=mission, ctx=ctx, result=result,
                     now=datetime(2026, 8, 22))

    assert call_order == _EXPECTED_ORDER

    m = conn.execute("SELECT status FROM missions WHERE id=?",
                     (mission_id,)).fetchone()
    assert m["status"] == "completed"
    r = conn.execute("SELECT result FROM improvement_runs WHERE id=?",
                     (run_id,)).fetchone()
    assert r["result"] == "approval"


def test_commit_rolls_back_tx2_on_db_fault_between_gate_rows_and_approval(
        loop_full, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    """レビュー1周目 I4: `_finalize_success` の Tx-2 本体 (台帳→gate rows→
    approval→finish の単一 commit) に DB fault を注入し、境界の一つでも
    落ちれば同一 Tx-2 が丸ごと rollback することを確認する
    (`test_tx2_commit_failure_rolls_back_and_compensation_finalizes`
    (10.10 節) は `_finalize_success` を直接呼ぶ単体テストだったが、本テストは
    `commit()` の統合本体を通した happy path 経路の途中で fault を注入する
    ことで、境界の位置がずれても検出できることを狙う)。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    staging_dir = tmp_path / "staging"
    _write_candidate(staging_dir, "myind")

    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"analyze_corr": 60.0,
                                                        "run_backtest": 600.0})
    ctx = ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging_dir,
        source_snapshot_dir=tmp_path / "source",
        allowed_backlog_ids=None, slot_key=None, ledger=ledger,
        rpc_handlers={})
    mission = Mission(prompt="x", tools=[], output_schema={}, max_turns=10,
                      timeout_sec=60)
    result = MissionResult(status="completed", output={
        "discoveries": [], "selected": {"backlog_id": backlog_id, "idea": "x"},
        "artifact": {"type": "plugin", "name": "myind", "kind": "indicator",
                    "self_test": "passed", "summary": "s"},
        "selection_rationale": "r"}, transcript=[])

    def _boom(*a, **kw):
        raise RuntimeError("simulated fault between gate rows and approval")

    from agentic_fx.store import approvals as approvals_store
    monkeypatch.setattr(approvals_store, "create", _boom)

    loop_full.commit(mission=mission, ctx=ctx, result=result,
                     now=datetime(2026, 8, 22))

    # 補償側 (§4.2 の Tx-2 失敗時の意味論) — approval は作られておらず、
    # mission/run は failed で終端する (10.10 節の補償経路と同じ契約)。
    a = conn.execute(
        "SELECT COUNT(*) FROM approval_requests WHERE kind='plugin'"
    ).fetchone()[0]
    assert a == 0
    m = conn.execute("SELECT status FROM missions WHERE id=?",
                     (mission_id,)).fetchone()
    assert m["status"] == "failed"


def test_finalize_success_tx2_internal_seams_run_ledger_then_gate_then_approval_then_finish(
        loop_min, conn, mission_and_run_fixture, monkeypatch):
    """レビュー2周目 Important 2: `test_tx2_writes_ledger_rows_then_gate_rows_then_approval_then_finish`
    (10.10節) と `test_commit_runs_all_nine_steps_in_order_for_happy_path_plugin`
    (本節) はどちらも `_finalize_success` を 1 呼出しとしてしか観測しない —
    設計書 §4.1 が要求する Tx-2 **内部**の「台帳保存→親ゲート行保存→
    approval→finish」の順序は、それらのテストでは検出できない (例えば
    approval を gate row 保存より前へ移す変異を入れても、両テストは green
    のままになる)。本テストは `_finalize_success` が呼ぶ 4 seam
    (`_persist_ledger_rows` / `_persist_gate_rows` /
    `approvals_store.create` / `missions_store.finish_improve_mission`)
    それぞれに recorder を注入し、Tx-2 内部の呼び出し列そのものを完全一致で
    assert する (10.10節の外側期待列テストとは別に、内部列を独立に固定する)。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture

    from agentic_fx.loops import improve_loop as improve_loop_mod

    call_order: list[str] = []

    orig_persist_ledger_rows = loop_min._persist_ledger_rows
    orig_persist_gate_rows = loop_min._persist_gate_rows
    orig_approvals_create = improve_loop_mod.approvals_store.create
    orig_finish_improve_mission = (
        improve_loop_mod.missions_store.finish_improve_mission)

    def _persist_ledger_rows_recorder(*a, **kw):
        call_order.append("_persist_ledger_rows")
        return orig_persist_ledger_rows(*a, **kw)

    def _persist_gate_rows_recorder(*a, **kw):
        call_order.append("_persist_gate_rows")
        return orig_persist_gate_rows(*a, **kw)

    def _approvals_create_recorder(*a, **kw):
        call_order.append("approvals_store.create")
        return orig_approvals_create(*a, **kw)

    def _finish_improve_mission_recorder(*a, **kw):
        call_order.append("missions_store.finish_improve_mission")
        return orig_finish_improve_mission(*a, **kw)

    monkeypatch.setattr(
        loop_min, "_persist_ledger_rows", _persist_ledger_rows_recorder)
    monkeypatch.setattr(
        loop_min, "_persist_gate_rows", _persist_gate_rows_recorder)
    monkeypatch.setattr(
        improve_loop_mod.approvals_store, "create", _approvals_create_recorder)
    monkeypatch.setattr(
        improve_loop_mod.missions_store, "finish_improve_mission",
        _finish_improve_mission_recorder)

    loop_min._finalize_success(
        conn, mission_id=mission_id, run_id=run_id, backlog_id=backlog_id,
        slot_key=None, approval_payload={"name": "x", "kind": "indicator"},
        now=datetime(2026, 8, 22))

    assert call_order == [
        "_persist_ledger_rows",
        "_persist_gate_rows",
        "approvals_store.create",
        "missions_store.finish_improve_mission",
    ]


def test_finalize_success_persists_ledger_and_gate_rows_in_tx2(
        loop_min, conn, mission_and_run_fixture):
    """3 周目レビュー Important-2: `ledger_entries`/`gate_rows` に実データを
    渡すと `analysis_runs`/`backtest_runs` へ実際に行が保存されることを
    確認する — これまでは両方とも既定値 `()` のまま呼ばれ、
    `_persist_ledger_rows`/`_persist_gate_rows` は恒久的に no-op だった
    (設計書 §4.1 Tx-2「台帳の backtest_runs/analysis_runs 行」「親ゲートの
    backtest_runs 行」が実装されない欠落)。ここでは `_finalize_success` を
    直接呼び、`ctx.ledger.entries()`/`_run_strategy_gate` の `record_fn`
    sink が実運用で積む形と同じ shape の dict を渡す (`_build_rpc_handlers`
    (10.9 節) が RPC handler の戻り値としてこの shape を返す契約 — 本テスト
    はその契約に対する pin)。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    in_sample_period = (datetime(2026, 1, 1, tzinfo=timezone.utc),
                        datetime(2026, 2, 1, tzinfo=timezone.utc))
    holdout_period = (datetime(2026, 2, 1, tzinfo=timezone.utc),
                      datetime(2026, 3, 1, tzinfo=timezone.utc))
    ledger_entries = (
        {"opaque_ref": "run_backtest:myst:USDJPY", "kind": "run_backtest",
         "params": {"name": "myst", "pair": "USDJPY"}, "trial_count": 1,
         "result_summary": {
             "scope": "in_sample", "plugin_ref": "plugins/myst",
             "content_hash": "h1", "kind": "strategy", "pair": "USDJPY",
             "timeframe": "1h", "source": "dukascopy", "period": in_sample_period,
             "metrics": {"pf": 1.2}, "settings_hash": "sh1",
             "core_commit": "c1", "initial_balance": 10000.0,
             "now": in_sample_period[0]}},
        {"opaque_ref": "analyze_corr:1", "kind": "analyze_corr",
         "params": {"pairs": ["USDJPY"]}, "trial_count": 3,
         "result_summary": {
             "params": {"request": {"pairs": ["USDJPY"]}},
             "trial_count": 3, "source": "improve_agent"}},
    )
    gate_rows = (
        {"scope": "holdout_gate", "plugin_ref": "plugins/myst",
         "content_hash": "h1", "kind": "strategy", "pair": "USDJPY",
         "timeframe": "1h", "source": "dukascopy", "period": holdout_period,
         "metrics": {"pf": 1.1}, "settings_hash": "sh1",
         "core_commit": "c1", "initial_balance": 10000.0,
         "now": holdout_period[1]},
    )

    loop_min._finalize_success(
        conn, mission_id=mission_id, run_id=run_id, backlog_id=backlog_id,
        slot_key=None, approval_payload={"name": "myst", "kind": "strategy"},
        now=datetime(2026, 8, 22, tzinfo=timezone.utc),
        ledger_entries=ledger_entries, gate_rows=gate_rows)

    bt_rows = conn.execute(
        "SELECT scope, pair, content_hash, variant FROM backtest_runs "
        "ORDER BY id").fetchall()
    assert [(r["scope"], r["pair"], r["content_hash"], r["variant"])
            for r in bt_rows] == [
        ("in_sample", "USDJPY", "h1", "candidate"),
        ("holdout_gate", "USDJPY", "h1", "candidate")]
    an_rows = conn.execute(
        "SELECT trial_count, source FROM analysis_runs").fetchall()
    assert [(r["trial_count"], r["source"]) for r in an_rows] == [
        (3, "improve_agent")]


def test_finalize_success_overwrites_payload_analysis_run_ids_with_real_ids(
        loop_min, conn, mission_and_run_fixture):
    """直前修正の申し送り③ (§8.1-16): `_build_approval_payload` (10.8節) が
    置いた placeholder `analysis_run_ids: []` を、`_finalize_success` が
    `_persist_ledger_rows` で実際に永続化した `analysis_runs` の行 id で
    上書きしてから `approvals_store.create` へ渡すことを確認する — 2 件の
    analyze_corr entry を渡し、payload の analysis_run_ids がその 2 件の
    実 rowid と一致することを pin する。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    ledger_entries = (
        {"opaque_ref": "analyze_corr:1", "kind": "analyze_corr",
         "params": {}, "trial_count": 10,
         "result_summary": {"params": {}, "trial_count": 10,
                            "source": "improve_agent"}},
        {"opaque_ref": "analyze_corr:2", "kind": "analyze_corr",
         "params": {}, "trial_count": 5,
         "result_summary": {"params": {}, "trial_count": 5,
                            "source": "improve_agent"}},
    )
    loop_min._finalize_success(
        conn, mission_id=mission_id, run_id=run_id, backlog_id=backlog_id,
        slot_key=None,
        approval_payload={"name": "myind", "kind": "indicator",
                          "analysis_run_ids": []},
        now=datetime(2026, 8, 22, tzinfo=timezone.utc),
        ledger_entries=ledger_entries)

    real_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM analysis_runs ORDER BY id").fetchall()]
    assert len(real_ids) == 2
    import json as _json
    row = conn.execute(
        "SELECT payload_json FROM approval_requests WHERE kind='plugin' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    payload = _json.loads(row["payload_json"])
    assert payload["analysis_run_ids"] == real_ids
    assert payload["analysis_call_count"] == 2
    assert payload["trial_count"] == 15


def test_commit_terminates_scheduler_wave_slot_via_ctx_slot_key(
        loop_full, conn, mission_and_run_fixture_with_slot, tmp_path):
    """レビュー1周目 C2 の protocol test: scheduler wave 起動 (`ctx.slot_key`
    が非 None) で prepare → commit を通したとき、`commit()` が固定の
    `slot_key=None` にすり替えず `ctx.slot_key` をそのまま
    `finish_improve_mission` へ運び、slot が `running→done` で終端する
    ことを確認する (敗者経路のような失敗終端でも同じ配線で `failed` に
    終端するのが本来の契約だが、本テストは happy path 成功終端のみを
    固定する — 失敗系の変種は実装者が追加すること)。"""
    mission_id, run_id, backlog_id, slot_key = mission_and_run_fixture_with_slot
    staging_dir = tmp_path / "staging"
    _write_candidate(staging_dir, "myind")

    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"analyze_corr": 60.0,
                                                        "run_backtest": 600.0})
    ctx = ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging_dir,
        source_snapshot_dir=tmp_path / "source",
        allowed_backlog_ids=None, slot_key=slot_key, ledger=ledger,
        rpc_handlers={})
    mission = Mission(prompt="x", tools=[], output_schema={}, max_turns=10,
                      timeout_sec=60)
    result = MissionResult(status="completed", output={
        "discoveries": [], "selected": {"backlog_id": backlog_id, "idea": "x"},
        "artifact": {"type": "plugin", "name": "myind", "kind": "indicator",
                    "self_test": "passed", "summary": "s"},
        "selection_rationale": "r"}, transcript=[])

    loop_full.commit(mission=mission, ctx=ctx, result=result,
                     now=datetime(2026, 8, 22))

    period_key, k = slot_key
    slot = conn.execute(
        "SELECT status FROM improve_wave_slots WHERE wave_period_key=? "
        "AND k=?", (period_key, k)).fetchone()
    assert slot["status"] == "done"


# --- 10.11 節「未執筆メソッド」の回収 (前任 haiku が stub のまま残した
# _finalize_failed_mission/_finalize_output_invalid/_finalize_loser/
# _finalize_gate_failed の TDD 実装) ---
# 前任 stub は `run_result="failed"`/`"observation"`/`"duplicate"` を
# `finish_improve_mission` へ渡していたが、`improvement_runs.result` は
# `CHECK (result IN ('approval','report'))` (db.py:50) — NULL 以外の
# 未知値は sqlite3.IntegrityError で red になる。以下はこの契約を pin する。


def test_finalize_failed_mission_terminates_failed_with_null_result_and_deletes_staging(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    """§3.6: timeout/failed/max_turns → missions は failed、
    improvement_runs.result は NULL のまま (CHECK は approval|report のみ
    許す)、staging は削除、台帳は DISCARDED。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    (staging_dir / "marker").write_text("x")

    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"analyze_corr": 60.0,
                                                        "run_backtest": 600.0})
    ledger.freeze()
    ctx = ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging_dir,
        source_snapshot_dir=tmp_path / "source",
        allowed_backlog_ids=None, slot_key=None, ledger=ledger,
        rpc_handlers={})
    result = MissionResult(status="timeout", output=None, transcript=[])

    loop_min._finalize_failed_mission(conn, ctx=ctx, result=result,
                                      now=datetime(2026, 8, 22))

    m = conn.execute("SELECT status FROM missions WHERE id=?",
                     (mission_id,)).fetchone()
    assert m["status"] == "failed"
    r = conn.execute("SELECT result FROM improvement_runs WHERE id=?",
                     (run_id,)).fetchone()
    assert r["result"] is None
    assert not staging_dir.exists()
    assert ledger._state == "DISCARDED"


def test_finalize_failed_mission_terminates_scheduler_wave_slot_as_failed(
        loop_min, conn, mission_and_run_fixture_with_slot, tmp_path):
    """レビュー1周目 C2 と同型の pin: 失敗系終端でも `ctx.slot_key` を
    固定値にすり替えず運び、slot が `running→failed` で終端すること。"""
    mission_id, run_id, backlog_id, slot_key = mission_and_run_fixture_with_slot
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()

    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"analyze_corr": 60.0,
                                                        "run_backtest": 600.0})
    ledger.freeze()
    ctx = ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging_dir,
        source_snapshot_dir=tmp_path / "source",
        allowed_backlog_ids=None, slot_key=slot_key, ledger=ledger,
        rpc_handlers={})
    result = MissionResult(status="failed", output=None, transcript=[])

    loop_min._finalize_failed_mission(conn, ctx=ctx, result=result,
                                      now=datetime(2026, 8, 22))

    period_key, k = slot_key
    slot = conn.execute(
        "SELECT status FROM improve_wave_slots WHERE wave_period_key=? "
        "AND k=?", (period_key, k)).fetchone()
    assert slot["status"] == "failed"


def test_finalize_output_invalid_terminates_failed_with_null_result_and_deletes_staging(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    """§4.2 手順1 不合格: Mission failed、improvement_runs.result は NULL、
    staging 削除、台帳 DISCARDED。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()

    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"analyze_corr": 60.0,
                                                        "run_backtest": 600.0})
    ledger.freeze()
    ctx = ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging_dir,
        source_snapshot_dir=tmp_path / "source",
        allowed_backlog_ids=None, slot_key=None, ledger=ledger,
        rpc_handlers={})

    loop_min._finalize_output_invalid(
        conn, ctx=ctx, reason="artifact.name is not canonical",
        now=datetime(2026, 8, 22))

    m = conn.execute("SELECT status FROM missions WHERE id=?",
                     (mission_id,)).fetchone()
    assert m["status"] == "failed"
    r = conn.execute("SELECT result FROM improvement_runs WHERE id=?",
                     (run_id,)).fetchone()
    assert r["result"] is None
    assert not staging_dir.exists()
    assert ledger._state == "DISCARDED"


def test_finalize_loser_writes_skip_report_and_finishes_run_as_report(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    """§4.2 手順2 敗者経路: backlog は遷移せず (Tx-1 CAS rowcount=0)、
    親が「重複のため見送り」レポートを自前で書き run を `result='report'`
    で終端する (10.9 節の outbox ヘルパを再利用、プラン10 Task10-11
    申し送り)。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()

    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"analyze_corr": 60.0,
                                                        "run_backtest": 600.0})
    ledger.freeze()
    ctx = ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging_dir,
        source_snapshot_dir=tmp_path / "source",
        allowed_backlog_ids=None, slot_key=None, ledger=ledger,
        rpc_handlers={})
    output = {"selected": {"idea": "dup idea"}}

    loop_min._finalize_loser(conn, ctx=ctx, output=output,
                             now=datetime(2026, 8, 22))

    m = conn.execute("SELECT status FROM missions WHERE id=?",
                     (mission_id,)).fetchone()
    assert m["status"] == "completed"
    r = conn.execute(
        "SELECT result, report_state, report_path FROM improvement_runs "
        "WHERE id=?", (run_id,)).fetchone()
    assert r["result"] == "report"
    assert r["report_state"] == "published"
    assert r["report_path"] is not None
    assert Path(r["report_path"]).exists()
    assert not staging_dir.exists()
    assert ledger._state == "DISCARDED"
    b = conn.execute(
        "SELECT status FROM improvement_backlog WHERE id=?",
        (backlog_id,)).fetchone()
    assert b["status"] == "open"  # 敗者経路は backlog を一切遷移させない


def test_finalize_gate_failed_sets_backlog_observation_with_reason(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    """§4.3: ゲート不合格/評価不能 → backlog は `observation`、
    `last_result` は呼び出し元が渡した reason そのまま。承認申請は出さず
    mission は `completed`/`result=NULL` で終端 (取引を止めない — R8)。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    from agentic_fx.store import backlog as backlog_store
    backlog_store.select_for_mission(conn, backlog_id,
                                     now=datetime(2026, 8, 22), commit=True)
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()

    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"analyze_corr": 60.0,
                                                        "run_backtest": 600.0})
    ledger.freeze()
    ctx = ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging_dir,
        source_snapshot_dir=tmp_path / "source",
        allowed_backlog_ids=None, slot_key=None, ledger=ledger,
        rpc_handlers={})

    loop_min._finalize_gate_failed(
        conn, ctx=ctx, backlog_id=backlog_id,
        reason="gate_failed:pytest failed: boom", now=datetime(2026, 8, 22))

    m = conn.execute("SELECT status FROM missions WHERE id=?",
                     (mission_id,)).fetchone()
    assert m["status"] == "completed"
    r = conn.execute("SELECT result FROM improvement_runs WHERE id=?",
                     (run_id,)).fetchone()
    assert r["result"] is None
    b = conn.execute(
        "SELECT status, last_result FROM improvement_backlog WHERE id=?",
        (backlog_id,)).fetchone()
    assert b["status"] == "observation"
    assert b["last_result"] == "gate_failed:pytest failed: boom"
    assert not staging_dir.exists()
    assert ledger._state == "DISCARDED"


def test_finalize_gate_failed_terminates_scheduler_wave_slot_as_done(
        loop_min, conn, mission_and_run_fixture_with_slot, tmp_path):
    """ゲート不合格は Mission としては `completed` (承認申請は出さないが
    Mission 自体は正常終了) — slot は `done` で終端する (§4.3 の
    `mission_status="completed"` と揃える)。"""
    mission_id, run_id, backlog_id, slot_key = mission_and_run_fixture_with_slot
    from agentic_fx.store import backlog as backlog_store
    backlog_store.select_for_mission(conn, backlog_id,
                                     now=datetime(2026, 8, 22), commit=True)
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()

    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"analyze_corr": 60.0,
                                                        "run_backtest": 600.0})
    ledger.freeze()
    ctx = ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging_dir,
        source_snapshot_dir=tmp_path / "source",
        allowed_backlog_ids=None, slot_key=slot_key, ledger=ledger,
        rpc_handlers={})

    loop_min._finalize_gate_failed(
        conn, ctx=ctx, backlog_id=backlog_id,
        reason="insufficient_trades:5", now=datetime(2026, 8, 22))

    period_key, k = slot_key
    slot = conn.execute(
        "SELECT status FROM improve_wave_slots WHERE wave_period_key=? "
        "AND k=?", (period_key, k)).fetchone()
    assert slot["status"] == "done"


def test_finalize_success_writes_mission_id_on_ledger_and_gate_rows(
        loop_min, conn, mission_and_run_fixture):
    """RW4 pin — Task 12 の `SELECT ... WHERE mission_id=?` assert が成立
    するための検査。台帳 (`ledger_entries`) 由来の `analysis_runs` 行と、
    親ゲート (`gate_rows`) 由来の `backtest_runs` 行 (no_strategy baseline
    等) の**両方**に `mission_id` が書かれることを確認する
    (`test_strategy_baseline_falls_back_to_no_strategy_row` (Task 12) の
    baseline 行は gate_rows 経由で書かれるため、`_persist_ledger_rows` だけ
    直しても不十分)。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    period = (datetime(2026, 1, 1, tzinfo=timezone.utc),
              datetime(2026, 1, 2, tzinfo=timezone.utc))
    ledger_entries = [{"kind": "analyze_corr", "trial_count": 1,
                       "result_summary": {"params": {}, "trial_count": 1,
                                         "source": "rpc"}}]
    gate_rows = [{
        "scope": "in_sample", "plugin_ref": "no_strategy:x",
        "content_hash": "h" * 8, "kind": "strategy", "pair": "USDJPY",
        "timeframe": "1h", "source": "dukascopy", "period": period,
        "metrics": {}, "settings_hash": "s", "core_commit": "c",
        "initial_balance": 10000.0, "now": now, "variant": "no_strategy"}]

    loop_min._finalize_success(
        conn, mission_id=mission_id, run_id=run_id, backlog_id=backlog_id,
        slot_key=None, approval_payload={"name": "x", "kind": "indicator"},
        now=now, ledger_entries=ledger_entries, gate_rows=gate_rows)

    an_row = conn.execute(
        "SELECT mission_id FROM analysis_runs WHERE mission_id=?",
        (mission_id,)).fetchone()
    assert an_row is not None
    bt_row = conn.execute(
        "SELECT mission_id FROM backtest_runs WHERE mission_id=? "
        "AND variant='no_strategy'", (mission_id,)).fetchone()
    assert bt_row is not None
