# tests/loops/test_improve_loop_finalize.py
"""ImproveLoop.commit 統合 (設計書 §4.2、プラン 10.11〜10.13)。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_fx.backtest.holdout import NoHistoryError
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
        "def test_x():\n    pass\n"
        "def test_y():\n    pass\n"
        "def test_z():\n    pass\n")


def test_delete_staging_does_not_chmod_file_symlink_target(
        loop_min, tmp_path):
    external = tmp_path / "external.txt"
    external.write_text("outside")
    external.chmod(0o400)
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    (staging_dir / "external-link").symlink_to(external)
    ctx = SimpleNamespace(staging_dir=staging_dir)

    loop_min._delete_staging(ctx)

    assert external.stat().st_mode & 0o777 == 0o400
    assert not staging_dir.exists()


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
    assert ctx.ledger._state == "PERSISTED"

    m = conn.execute("SELECT status FROM missions WHERE id=?",
                     (mission_id,)).fetchone()
    assert m["status"] == "completed"
    r = conn.execute("SELECT result FROM improvement_runs WHERE id=?",
                     (run_id,)).fetchone()
    assert r["result"] == "approval"


def test_commit_loader_rejection_becomes_gate_failed(
        loop_full, conn, mission_and_run_fixture, tmp_path):
    mission_id, run_id, backlog_id = mission_and_run_fixture
    staging_dir = tmp_path / "staging"
    _write_candidate(staging_dir, "myind")
    candidate_config = staging_dir / "myind" / "config.yaml"
    candidate_config.write_text(candidate_config.read_text() + "warmup_bars: 5\n")

    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={
        "analyze_corr": 60.0, "run_backtest": 600.0})
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

    loop_full.commit(mission=mission, ctx=ctx, result=result,
                     now=datetime(2026, 8, 22))

    backlog = conn.execute(
        "SELECT status, last_result FROM improvement_backlog WHERE id=?",
        (backlog_id,)).fetchone()
    assert backlog["status"] == "observation"
    assert backlog["last_result"] == (
        "gate_failed:loader_rejected: unknown config keys: ['warmup_bars']")
    mission_row = conn.execute(
        "SELECT status FROM missions WHERE id=?", (mission_id,)).fetchone()
    assert mission_row["status"] == "completed"


def test_commit_signal_candidate_is_gate_failed_without_approval(
        loop_full, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    mission_id, run_id, backlog_id = mission_and_run_fixture
    staging_dir = tmp_path / "staging"
    _write_candidate(staging_dir, "mysignal")
    candidate_dir = staging_dir / "mysignal"
    (candidate_dir / "plugin.py").write_text(
        "def detect(df, params):\n    return []\n")
    (candidate_dir / "config.yaml").write_text(
        "kind: signal\npairs: ['USDJPY']\ntimeframe: '1h'\n")

    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={})
    ctx = ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging_dir,
        source_snapshot_dir=tmp_path / "source", allowed_backlog_ids=None,
        slot_key=None, ledger=ledger, rpc_handlers={})
    mission = Mission(prompt="x", tools=[], output_schema={}, max_turns=10,
                      timeout_sec=60)
    result = MissionResult(status="completed", output={
        "discoveries": [],
        "selected": {"backlog_id": backlog_id, "idea": "x"},
        "artifact": {"type": "plugin", "name": "mysignal", "kind": "signal",
                     "self_test": "passed", "summary": "s"},
        "selection_rationale": "r"}, transcript=[])
    gate_verdict = SimpleNamespace(
        passed=True, content_hash="c" * 64, artifact_hash="a" * 64)
    candidate_meta = SimpleNamespace(max_bars=100)
    monkeypatch.setattr(
        loop_full, "_run_plugin_gate", lambda *a, **kw: gate_verdict)
    monkeypatch.setattr(
        "agentic_fx.plugin.loader._discover_one",
        lambda *a, **kw: candidate_meta)

    loop_full.commit(mission=mission, ctx=ctx, result=result,
                     now=datetime(2026, 8, 22, tzinfo=timezone.utc))

    assert conn.execute(
        "SELECT COUNT(*) FROM approval_requests WHERE kind='plugin'"
    ).fetchone()[0] == 0
    backlog = conn.execute(
        "SELECT status, last_result FROM improvement_backlog WHERE id=?",
        (backlog_id,)).fetchone()
    assert backlog["status"] == "observation"
    assert backlog["last_result"] == "gate_failed:kind_unsupported:signal"


def test_commit_strategy_missing_history_becomes_gate_failed(
        loop_full, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    mission_id, run_id, backlog_id = mission_and_run_fixture
    staging_dir = tmp_path / "staging"
    _write_candidate(staging_dir, "myst")
    (staging_dir / "myst" / "config.yaml").write_text(
        "kind: strategy\npairs: [EURUSD]\ntimeframe: 1h\n"
        "exit_mode: levels\nparams: {}\n")

    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={})
    ctx = ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging_dir,
        source_snapshot_dir=tmp_path / "source", allowed_backlog_ids=None,
        slot_key=None, ledger=ledger, rpc_handlers={})
    mission = Mission(prompt="x", tools=[], output_schema={}, max_turns=10,
                      timeout_sec=60)
    result = MissionResult(status="completed", output={
        "discoveries": [],
        "selected": {"backlog_id": backlog_id, "idea": "x"},
        "artifact": {"type": "plugin", "name": "myst", "kind": "strategy",
                     "self_test": "passed", "summary": "s"},
        "selection_rationale": "r"}, transcript=[])
    gate_verdict = SimpleNamespace(
        passed=True, content_hash="c" * 64, artifact_hash="a" * 64)
    meta = SimpleNamespace(max_bars=100)
    monkeypatch.setattr(
        loop_full, "_run_plugin_gate", lambda *a, **kw: gate_verdict)
    monkeypatch.setattr(
        "agentic_fx.plugin.loader._discover_one", lambda *a, **kw: meta)
    def fake_strategy_gate(*a, **kw):
        kw["record_fn"]({
            "scope": "holdout_gate", "plugin_ref": "plugins/myst",
            "content_hash": "c" * 64, "kind": "strategy",
            "pair": "USDJPY", "timeframe": "1h", "source": "dukascopy",
            "base_interval": "1m", "params": {},
            "period": (datetime(2026, 1, 1, tzinfo=timezone.utc),
                       datetime(2026, 2, 1, tzinfo=timezone.utc)),
            "metrics": {"pf": 1.1}, "settings_hash": "settings-hash",
            "core_commit": "core", "initial_balance": 10000.0,
            "now": datetime(2026, 2, 1, tzinfo=timezone.utc),
        })
        raise NoHistoryError(
            "no 1m history for symbol='EURUSD' source='dukascopy' "
            "(cannot determine in-sample start)")

    monkeypatch.setattr(loop_full, "_run_strategy_gate", fake_strategy_gate)

    loop_full.commit(mission=mission, ctx=ctx, result=result,
                     now=datetime(2026, 8, 22, tzinfo=timezone.utc))

    mission_row = conn.execute(
        "SELECT status FROM missions WHERE id=?", (mission_id,)).fetchone()
    run_row = conn.execute(
        "SELECT result, finished_at FROM improvement_runs WHERE id=?",
        (run_id,)).fetchone()
    backlog_row = conn.execute(
        "SELECT status, last_result FROM improvement_backlog WHERE id=?",
        (backlog_id,)).fetchone()
    assert mission_row["status"] == "completed"
    assert run_row["result"] == "report"
    assert run_row["finished_at"] is not None
    assert backlog_row["status"] == "observation"
    assert backlog_row["last_result"] == (
        "gate_failed:backtest_data_unavailable:no 1m history for "
        "symbol='EURUSD' source='dukascopy' "
        "(cannot determine in-sample start)")
    activity = (tmp_path / "activity.log").read_text()
    assert "gate_failed" in activity
    assert (
        "backtest_data_unavailable:no 1m history for symbol='EURUSD'"
        in activity)
    assert conn.execute(
        "SELECT COUNT(*) FROM backtest_runs WHERE mission_id=?",
        (mission_id,)).fetchone()[0] == 1


def test_finalize_gate_failed_persists_gate_rows_when_report_write_raises_oserror(
        loop_min, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    """L05: report part 作成失敗の補償 Tx にも親 gate 行を残す。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={})
    ledger.freeze()
    ctx = ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging_dir,
        source_snapshot_dir=tmp_path / "source", allowed_backlog_ids=None,
        slot_key=None, ledger=ledger, rpc_handlers={})
    gate_rows = ({
        "scope": "holdout_gate", "plugin_ref": "plugins/myst",
        "content_hash": "c" * 64, "kind": "strategy", "pair": "USDJPY",
        "timeframe": "1h", "source": "dukascopy",
        "base_interval": "1m", "params": {},
        "period": (datetime(2026, 1, 1, tzinfo=timezone.utc),
                   datetime(2026, 2, 1, tzinfo=timezone.utc)),
        "metrics": {"pf": 1.1}, "settings_hash": "settings-hash",
        "core_commit": "core", "initial_balance": 10000.0,
        "now": datetime(2026, 2, 1, tzinfo=timezone.utc),
    },)
    monkeypatch.setattr(
        loop_min, "_write_report_part",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("write failed")))

    loop_min._finalize_gate_failed(
        conn, ctx=ctx, backlog_id=backlog_id, reason="gate_failed:test",
        now=datetime(2026, 8, 22, tzinfo=timezone.utc), gate_rows=gate_rows)

    assert conn.execute(
        "SELECT COUNT(*) FROM backtest_runs WHERE mission_id=?",
        (mission_id,)).fetchone()[0] == 1


def test_commit_real_strategy_gate_missing_history_becomes_gate_failed(
        loop_full, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    """実 strategy gate/holdout の空履歴例外を commit が安全に終端する。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    staging_dir = tmp_path / "staging"
    _write_candidate(staging_dir, "myst")
    (staging_dir / "myst" / "config.yaml").write_text(
        "kind: strategy\npairs: [EURUSD]\ntimeframe: 1h\n"
        "exit_mode: levels\nparams: {}\n")

    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={})
    ctx = ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging_dir,
        source_snapshot_dir=tmp_path / "source", allowed_backlog_ids=None,
        slot_key=None, ledger=ledger, rpc_handlers={})
    mission = Mission(prompt="x", tools=[], output_schema={}, max_turns=10,
                      timeout_sec=60)
    result = MissionResult(status="completed", output={
        "discoveries": [],
        "selected": {"backlog_id": backlog_id, "idea": "x"},
        "artifact": {"type": "plugin", "name": "myst", "kind": "strategy",
                     "self_test": "passed", "summary": "s"},
        "selection_rationale": "r"}, transcript=[])
    gate_verdict = SimpleNamespace(
        passed=True, content_hash="c" * 64, artifact_hash="a" * 64)
    meta = SimpleNamespace(
        name="myst", kind="strategy", timeframe="1h",
        content_hash="c" * 64, pairs=("EURUSD",), max_bars=100)
    monkeypatch.setattr(
        loop_full, "_run_plugin_gate", lambda *a, **kw: gate_verdict)
    monkeypatch.setattr(
        "agentic_fx.plugin.loader._discover_one", lambda *a, **kw: meta)
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_adapter.build_intent_source",
        lambda *a, **kw: SimpleNamespace(close=lambda: None))

    loop_full.commit(mission=mission, ctx=ctx, result=result,
                     now=datetime(2026, 8, 22, tzinfo=timezone.utc))

    backlog_row = conn.execute(
        "SELECT status, last_result FROM improvement_backlog WHERE id=?",
        (backlog_id,)).fetchone()
    assert backlog_row["status"] == "observation"
    assert backlog_row["last_result"].startswith(
        "gate_failed:backtest_data_unavailable:")


def test_commit_strategy_other_valueerror_still_propagates(
        loop_full, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    """段 0 変異 M1 (2026-09-03) の pin: gate の ValueError を gate_failed に
    変換するのは「no 1m history for symbol=」で始まるデータ欠損だけ。
    それ以外の ValueError はプログラム欠陥の可能性があるため素通しで
    commit を中断させる (黙って observation に落とすと欠陥が隠れる)。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    staging_dir = tmp_path / "staging"
    _write_candidate(staging_dir, "myst")
    (staging_dir / "myst" / "config.yaml").write_text(
        "kind: strategy\npairs: [EURUSD]\ntimeframe: 1h\n"
        "exit_mode: levels\nparams: {}\n")

    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={})
    ctx = ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging_dir,
        source_snapshot_dir=tmp_path / "source", allowed_backlog_ids=None,
        slot_key=None, ledger=ledger, rpc_handlers={})
    mission = Mission(prompt="x", tools=[], output_schema={}, max_turns=10,
                      timeout_sec=60)
    result = MissionResult(status="completed", output={
        "discoveries": [],
        "selected": {"backlog_id": backlog_id, "idea": "x"},
        "artifact": {"type": "plugin", "name": "myst", "kind": "strategy",
                     "self_test": "passed", "summary": "s"},
        "selection_rationale": "r"}, transcript=[])
    gate_verdict = SimpleNamespace(
        passed=True, content_hash="c" * 64, artifact_hash="a" * 64)
    meta = SimpleNamespace(max_bars=100)
    monkeypatch.setattr(
        loop_full, "_run_plugin_gate", lambda *a, **kw: gate_verdict)
    monkeypatch.setattr(
        "agentic_fx.plugin.loader._discover_one", lambda *a, **kw: meta)
    monkeypatch.setattr(
        loop_full, "_run_strategy_gate",
        lambda *a, **kw: (_ for _ in ()).throw(ValueError(
            "pair 'USDJPY' is not in plugin 'myst's declared pairs")))

    with pytest.raises(ValueError, match="declared pairs"):
        loop_full.commit(mission=mission, ctx=ctx, result=result,
                         now=datetime(2026, 8, 22, tzinfo=timezone.utc))

    backlog_row = conn.execute(
        "SELECT status FROM improvement_backlog WHERE id=?",
        (backlog_id,)).fetchone()
    assert backlog_row["status"] != "observation"
    assert staging_dir.exists()


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
    approval_id = conn.execute(
        "SELECT id FROM approval_requests WHERE kind='plugin'"
    ).fetchone()[0]
    activity_text = (loop_min._activity._path).read_text()
    assert "\tIMPROVE\tapproval_requested\t" in activity_text
    assert (f"mission={mission_id} backlog={backlog_id} plugin=x "
            f"approval={approval_id}\t{mission_id}") in activity_text


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
             "timeframe": "1h", "source": "dukascopy", "base_interval": "1m",
             "params": {}, "period": in_sample_period,
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
         "timeframe": "1h", "source": "dukascopy", "base_interval": "1m",
         "params": {}, "period": holdout_period,
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
        "SELECT scope, pair, content_hash, variant, base_interval "
        "FROM backtest_runs ORDER BY id").fetchall()
    assert [(r["scope"], r["pair"], r["content_hash"], r["variant"])
            for r in bt_rows] == [
        ("in_sample", "USDJPY", "h1", "candidate"),
        ("holdout_gate", "USDJPY", "h1", "candidate")]
    # A6 (v3 設計): 台帳経路 (in_sample) と親ゲート経路 (holdout_gate) の
    # 双方で base_interval が実際に永続化されることを直接検査する。
    assert [r["base_interval"] for r in bt_rows] == ["1m", "1m"]
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


# --- M4: resume 追撃回収 (result.recovered) の report 降格 ------------
# 背景 (codex レビュー Major 4、2026-08-30): plugin artifact は既存の
# 決定論 gate (staging 実体・pytest・hash) で虚偽 completed を防げるが、
# report artifact はモデルの body_md を実体突合なしでファイル公開し得る。
# `result.recovered=True` (段B の resume 追撃で回収した出力) かつ
# `artifact.type=='report'` のときは report を公開せず、既存の
# observation 終端経路 (`_finalize_report_or_observation`) を再利用して
# 降格する。


def test_commit_demotes_recovered_report_to_observation_without_publishing(
        loop_full, conn, mission_and_run_fixture, tmp_path):
    """recovered=True + report artifact → report ファイルは作られず
    observation として終端し、activity に report_demoted_recovered が
    書かれる。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()

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
        "artifact": {"type": "report", "proposal_kind": "core",
                    "title": "Suspicious Recovered Report", "body_md": "body"},
        "selection_rationale": "r"}, transcript=[], recovered=True)

    loop_full.commit(mission=mission, ctx=ctx, result=result,
                     now=datetime(2026, 8, 22))

    reports_dir = tmp_path / "data" / "improve_reports"
    published = [p for p in reports_dir.glob("*.md")] if reports_dir.exists() else []
    assert published == [], "recovered report artifact must not be published"

    run = conn.execute(
        "SELECT result, report_state FROM improvement_runs WHERE id=?",
        (run_id,)).fetchone()
    assert run["result"] is None
    assert run["report_state"] == "none"

    backlog = conn.execute(
        "SELECT status, last_result FROM improvement_backlog WHERE id=?",
        (backlog_id,)).fetchone()
    assert backlog["status"] == "observation"
    assert "Suspicious Recovered Report" in backlog["last_result"]
    assert "resume 回収経由のため降格" in backlog["last_result"]

    activity_text = (tmp_path / "activity.log").read_text()
    assert "report_demoted_recovered" in activity_text
    assert f"mission={mission_id}" in activity_text


def test_commit_publishes_report_normally_when_not_recovered(
        loop_full, conn, mission_and_run_fixture, tmp_path):
    """対照: recovered=False (通常経路) の report artifact は従来どおり
    公開される — 降格分岐を常に通す変異を殺す。

    report-tx-crash 是正 (2026-08-30): 従来は `_prepare_report_if_
    applicable` の実ファイル書込 + Tx-2 更新を monkeypatch で回避して
    いたが、これは本経路の `sqlite3.OperationalError: cannot start a
    transaction within a transaction` (`_prepare_report_if_applicable`
    の裸 `conn.execute(UPDATE ...)` が既定 isolation_level 下で暗黙
    transaction を開いたまま `_finalize_report_or_observation` の
    `BEGIN IMMEDIATE` に突入していた) を隠していた。ここでは
    `_prepare_report_if_applicable` を実経路のまま (real write `conn`
    = `connect()`) 駆動し、report 準備 → Tx-2 finalize の連続呼び出しが
    例外なく完走し、以降の公開が従来どおり完了することを検証する。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()

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
        "artifact": {"type": "report", "proposal_kind": "core",
                    "title": "Normal Report", "body_md": "body"},
        "selection_rationale": "r"}, transcript=[])

    loop_full.commit(mission=mission, ctx=ctx, result=result,
                     now=datetime(2026, 8, 22))

    run = conn.execute(
        "SELECT result, report_state FROM improvement_runs WHERE id=?",
        (run_id,)).fetchone()
    assert run["result"] == "report"
    assert run["report_state"] == "published"

    backlog = conn.execute(
        "SELECT status, last_result FROM improvement_backlog WHERE id=?",
        (backlog_id,)).fetchone()
    assert backlog["status"] == "done"
    assert backlog["last_result"] == "reported"

    activity_path = tmp_path / "activity.log"
    activity_text = activity_path.read_text() if activity_path.exists() else ""
    assert "report_demoted_recovered" not in activity_text


def test_commit_recovered_risk_gate_report_keeps_unsupported_label(
        loop_full, conn, mission_and_run_fixture, tmp_path):
    """recovered=True + `proposal_kind=='risk_gate'` report → 降格せず、
    設計書 §4.3 状態表の逐語ラベル `unsupported_in_plan10:risk_gate` の
    まま (risk_gate は `_prepare_report_if_applicable` が本文書込み前に
    None を返すため元々ファイルを公開しない — 降格の動機である「実体
    突合なしでファイル公開し得る」が成立しないケースを対象から外す
    実装者裁定の pin。対象を広げる変異を殺す)。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()

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
        "artifact": {"type": "report", "proposal_kind": "risk_gate",
                    "title": "widen SL", "body_md": "body"},
        "selection_rationale": "r"}, transcript=[], recovered=True)

    loop_full.commit(mission=mission, ctx=ctx, result=result,
                     now=datetime(2026, 8, 22))

    backlog = conn.execute(
        "SELECT status, last_result FROM improvement_backlog WHERE id=?",
        (backlog_id,)).fetchone()
    assert backlog["status"] == "observation"
    assert backlog["last_result"] == "unsupported_in_plan10:risk_gate"

    activity_path = tmp_path / "activity.log"
    activity_text = activity_path.read_text() if activity_path.exists() else ""
    assert "report_demoted_recovered" not in activity_text


def test_commit_recovered_plugin_still_goes_through_gate(
        loop_full, conn, mission_and_run_fixture, tmp_path):
    """recovered=True + plugin artifact → 降格せず、従来どおり plugin
    gate (staging 実体・pytest) 経路を通る (plugin は既存 gate が防衛線
    のため対象外)。"""
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
        "selection_rationale": "r"}, transcript=[], recovered=True)

    loop_full.commit(mission=mission, ctx=ctx, result=result,
                     now=datetime(2026, 8, 22))

    r = conn.execute("SELECT result FROM improvement_runs WHERE id=?",
                     (run_id,)).fetchone()
    assert r["result"] == "approval"

    activity_path = tmp_path / "activity.log"
    activity_text = activity_path.read_text() if activity_path.exists() else ""
    assert "report_demoted_recovered" not in activity_text


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
    ledger.record(opaque_ref="bt", kind="run_backtest", params={},
                  result_summary={}, trial_count=1)
    ledger.record(opaque_ref="corr", kind="analyze_corr", params={},
                  result_summary={}, trial_count=1)
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
    note = conn.execute(
        "SELECT idea, source, status, last_result FROM improvement_backlog "
        "WHERE source='system'").fetchone()
    assert note["status"] == "note"
    assert "timeout/max_turns" in note["idea"]
    assert note["last_result"] == (
        f"mission #{mission_id} status=timeout run_backtest=1 analyze_corr=1")
    # M18 pin (段 0 変異、2026-09-07): idea は固定文 — mission 番号を含めると
    # idea_norm が毎回変わり、失敗 mission の数だけ note が増殖する (codex 1 周目
    # 推奨 6)。mission 番号は last_result 側にだけ載る。
    assert str(mission_id) not in note["idea"]


def test_finalize_failed_status_does_not_write_system_note(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    mission_id, run_id, _ = mission_and_run_fixture
    staging = tmp_path / "staging"; staging.mkdir()
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={}); ledger.freeze()
    ctx = ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging,
        source_snapshot_dir=tmp_path / "source", allowed_backlog_ids=None,
        slot_key=None, ledger=ledger, rpc_handlers={})
    loop_min._finalize_failed_mission(
        conn, ctx=ctx,
        result=MissionResult(status="failed", output=None, transcript=[]),
        now=datetime(2026, 8, 22))
    assert conn.execute(
        "SELECT COUNT(*) FROM improvement_backlog WHERE source='system'").fetchone()[0] == 0


def test_finalize_timeout_continues_when_system_note_upsert_fails(
        loop_min, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    mission_id, run_id, _ = mission_and_run_fixture
    staging = tmp_path / "staging"; staging.mkdir()
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={}); ledger.freeze()
    ctx = ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging,
        source_snapshot_dir=tmp_path / "source", allowed_backlog_ids=None,
        slot_key=None, ledger=ledger, rpc_handlers={})
    monkeypatch.setattr(
        "agentic_fx.loops.improve_loop.backlog_store.upsert_system_note",
        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("boom")))
    loop_min._finalize_failed_mission(
        conn, ctx=ctx,
        result=MissionResult(status="timeout", output=None, transcript=[]),
        now=datetime(2026, 8, 22))
    assert conn.execute(
        "SELECT status FROM missions WHERE id=?", (mission_id,)).fetchone()[0] == "failed"
    assert "backlog_note_failed" in (tmp_path / "activity.log").read_text()


def test_finalize_timeout_rolls_back_note_when_finish_fails(
        loop_min, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    mission_id, run_id, _ = mission_and_run_fixture
    staging = tmp_path / "staging"; staging.mkdir()
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={}); ledger.freeze()
    ctx = ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging,
        source_snapshot_dir=tmp_path / "source", allowed_backlog_ids=None,
        slot_key=None, ledger=ledger, rpc_handlers={})
    monkeypatch.setattr(
        "agentic_fx.loops.improve_loop.missions_store.finish_improve_mission",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("finish failed")))
    with pytest.raises(RuntimeError, match="finish failed"):
        loop_min._finalize_failed_mission(
            conn, ctx=ctx,
            result=MissionResult(status="timeout", output=None, transcript=[]),
            now=datetime(2026, 8, 22))
    assert conn.execute(
        "SELECT COUNT(*) FROM improvement_backlog WHERE source='system'").fetchone()[0] == 0


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


def test_finalize_failed_mission_slot_terminalize_false_leaves_slot_untouched(
        loop_min, conn, mission_and_run_fixture_with_slot, tmp_path):
    """I2b 是正 (プラン10 束D round1、ユーザー裁定 D①、2026-08-28):
    pre-ready 失敗の巻き戻し (`_handle_pre_ready_failure` の
    `revert_to_reserved`) は `_launch_slot` が `commit()` を呼ぶより前に
    完了している。`commit()`/`_finalize_failed_mission` が無条件に
    `finish_improve_mission(slot_key=ctx.slot_key, ...)` を呼ぶと、
    `mark_terminal` の status ガード無し無条件 UPDATE が、直前に revert
    した `reserved` slot を即座に `failed` へ潰す (プラン 9.5 節 RW3 が
    「commit() 内部の reached_running 分岐」を指していたが、その分岐は
    実在しなかった — プラン文書側は本 round で訂正)。
    `slot_terminalize=False` は mission/run のみ終端し、slot には
    触れないことを pin する。"""
    from agentic_fx.store import improve_waves

    mission_id, run_id, backlog_id, slot_key = mission_and_run_fixture_with_slot
    period_key, k = slot_key
    # `_launch_slot` の pre-ready 失敗巻き戻しを模す (revert_to_reserved 済み)
    reverted = improve_waves.revert_to_reserved(
        conn, period_key=period_key, k=k, now=datetime(2026, 8, 22),
        commit=True)
    assert reverted

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
                                      now=datetime(2026, 8, 22),
                                      slot_terminalize=False)

    m = conn.execute("SELECT status FROM missions WHERE id=?",
                     (mission_id,)).fetchone()
    assert m["status"] == "failed"
    slot = conn.execute(
        "SELECT status FROM improve_wave_slots WHERE wave_period_key=? "
        "AND k=?", (period_key, k)).fetchone()
    assert slot["status"] == "reserved", (
        "slot_terminalize=False は revert 済み slot を終端してはならない")


def test_finalize_failed_mission_writes_mission_failed_activity_with_reason(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    """[fail-observability]: `_finalize_output_invalid` は activity に
    `output_invalid` イベントを書くのに `_finalize_failed_mission` は何も
    書かず非対称だった (improve mission が failed のとき死因が一切残ら
    ない)。`_finalize_failed_mission` も `mission_failed` イベントを書き、
    `result.reason` を運ぶことを pin する。"""
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
    result = MissionResult(status="timeout", output=None, transcript=[],
                           reason="worker eof")

    loop_min._finalize_failed_mission(conn, ctx=ctx, result=result,
                                      now=datetime(2026, 8, 22))

    line = next(line for line in (tmp_path / "activity.log").read_text().splitlines()
               if "mission_failed" in line)
    assert f"mission={mission_id}" in line
    assert "status=timeout" in line
    assert "reason=worker eof" in line


def test_finalize_failed_mission_writes_activity_with_dash_reason_when_none(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    """reason=None のケースでも例外を投げず、activity には `-` を書く。"""
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
    result = MissionResult(status="failed", output=None, transcript=[],
                           reason=None)

    loop_min._finalize_failed_mission(conn, ctx=ctx, result=result,
                                      now=datetime(2026, 8, 22))

    line = next(line for line in (tmp_path / "activity.log").read_text().splitlines()
               if "mission_failed" in line)
    assert f"mission={mission_id}" in line
    assert "status=failed" in line
    assert "reason=-" in line


def test_commit_slot_terminalize_false_propagates_to_finalize_failed_mission(
        loop_full, conn, mission_and_run_fixture_with_slot, tmp_path):
    """`ImproveLoop.commit(..., slot_terminalize=False)` の公開 API から
    `_finalize_failed_mission` へ実際に配線されることを pin する
    (`_finalize_failed_mission` 単体呼び出しの上のテストとは別に、
    `commit()` の分岐そのものを通す)。"""
    from agentic_fx.store import improve_waves

    mission_id, run_id, backlog_id, slot_key = mission_and_run_fixture_with_slot
    period_key, k = slot_key
    assert improve_waves.revert_to_reserved(
        conn, period_key=period_key, k=k, now=datetime(2026, 8, 22),
        commit=True)

    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"analyze_corr": 60.0,
                                                        "run_backtest": 600.0})
    ctx = ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging_dir,
        source_snapshot_dir=tmp_path / "source",
        allowed_backlog_ids=None, slot_key=slot_key, ledger=ledger,
        rpc_handlers={})
    result = MissionResult(status="failed", output=None, transcript=[])

    loop_full.commit(mission="m", ctx=ctx, result=result,
                     now=datetime(2026, 8, 22), slot_terminalize=False)

    slot = conn.execute(
        "SELECT status FROM improve_wave_slots WHERE wave_period_key=? "
        "AND k=?", (period_key, k)).fetchone()
    assert slot["status"] == "reserved"
    m = conn.execute("SELECT status FROM missions WHERE id=?",
                     (mission_id,)).fetchone()
    assert m["status"] == "failed"


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
    mission は `completed` で終端 (取引を止めない — R8)。

    逐語乖離の申告 (着手前検証、D-15 是正): 旧稿は `improvement_runs.
    result IS NULL` (report を一切書かない) を pin していたが、これは
    Task 10 の未完成時点の実装をそのまま固定した stale pin だった。
    設計書 §4.2 手順6「ゲート不合格・評価不能・observation・敗者のとき、
    reports に書く」および `tests/loops/test_improve_e2e.py` の複数の
    E2E (`test_gate_failure_stops_at_report_no_approval_request` 等、
    `_finalize_loser` と同じ outbox 経路を前提にしている) と整合させ、
    `_finalize_gate_failed` にもレポート生成を追加した (D-15 是正)。
    ここでは `result='report'`/`report_state` が `prepared`/`published`
    のいずれかになることを検証する形に更新する。"""
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
    r = conn.execute(
        "SELECT result, report_state FROM improvement_runs WHERE id=?",
        (run_id,)).fetchone()
    assert r["result"] == "report"
    assert r["report_state"] in ("prepared", "published")
    b = conn.execute(
        "SELECT status, last_result FROM improvement_backlog WHERE id=?",
        (backlog_id,)).fetchone()
    assert b["status"] == "observation"
    assert b["last_result"] == "gate_failed:pytest failed: boom"
    assert not staging_dir.exists()
    assert ledger._state == "DISCARDED"


@pytest.mark.parametrize("gate_reason", [
    "noop_copy_of:_examples/rsi_indicator",
    "self_test_too_thin:1<3",
])
def test_finalize_gate_failed_preserves_new_gate_reason(
        loop_min, conn, mission_and_run_fixture, tmp_path, gate_reason):
    mission_id, run_id, backlog_id = mission_and_run_fixture
    from agentic_fx.store import backlog as backlog_store
    backlog_store.select_for_mission(
        conn, backlog_id, now=datetime(2026, 8, 22), commit=True)
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"analyze_corr": 60.0,
                                                        "run_backtest": 600.0})
    ledger.freeze()
    ctx = ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging_dir,
        source_snapshot_dir=tmp_path / "source", allowed_backlog_ids=None,
        slot_key=None, ledger=ledger, rpc_handlers={})

    loop_min._finalize_gate_failed(
        conn, ctx=ctx, backlog_id=backlog_id,
        reason=f"gate_failed:{gate_reason}", now=datetime(2026, 8, 22))

    row = conn.execute(
        "SELECT status, last_result FROM improvement_backlog WHERE id=?",
        (backlog_id,)).fetchone()
    assert row["status"] == "observation"
    assert row["last_result"] == f"gate_failed:{gate_reason}"
    activity_text = (tmp_path / "activity.log").read_text()
    assert "gate_failed" in activity_text
    assert f"reason=gate_failed:{gate_reason}" in activity_text


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
        "timeframe": "1h", "source": "dukascopy", "base_interval": "1m",
        "params": {}, "period": period,
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


def test_persist_ledger_rows_skips_error_entries(loop_min, conn):
    """D-11 (検収 R2、プラン L19627、10.11 節 M5 / T10-B20、実装者が Step 1
    で追加することが明示指定されていたが未執筆だった): `_persist_ledger_rows`
    の `if "error" in summary: continue` (improve_loop.py:778-782) を削ると、
    正常な `{"error": "insufficient_data"}` 応答が `summary[k]` の
    `KeyError` を送出し Mission 全体が補償 tx へ倒れる — 既存
    `test_persist_ledger_rows_fails_closed_when_handler_omits_save_kwargs`
    は「契約違反 (save_kwargs 欠落)」を検査するだけで「正常な error 応答」
    とは区別しないため代替にならない (プランの指摘どおり)。

    `ledger_entries` に `result_summary={"error": "insufficient_data"}` の
    analyze_corr entry を混ぜ、`_persist_ledger_rows` が `KeyError` を送出
    せず当該行を `analysis_runs`/`backtest_runs` に書かないことを assert
    する。"""
    now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    period = (datetime(2026, 1, 1, tzinfo=timezone.utc),
             datetime(2026, 1, 2, tzinfo=timezone.utc))
    ledger_entries = [
        {"kind": "run_backtest", "trial_count": 1,
         "result_summary": {
             "scope": "in_sample", "plugin_ref": "plugins/myst",
             "content_hash": "h1", "kind": "strategy", "pair": "USDJPY",
             "timeframe": "1h", "source": "dukascopy", "base_interval": "1m",
             "params": {}, "period": period,
             "metrics": {"pf": 1.2}, "settings_hash": "sh1",
             "core_commit": "c1", "initial_balance": 10000.0, "now": now}},
        # 正常な error 応答 (`analyze_corr_handler` が insufficient_data を
        # 返した場合の shape — 契約違反ではない)。
        {"kind": "analyze_corr", "trial_count": 0,
         "result_summary": {"error": "insufficient_data"}},
    ]

    before_bt = conn.execute(
        "SELECT COUNT(*) c FROM backtest_runs").fetchone()["c"]
    before_an = conn.execute(
        "SELECT COUNT(*) c FROM analysis_runs").fetchone()["c"]

    analysis_run_ids = loop_min._persist_ledger_rows(
        conn, ledger_entries=ledger_entries, now=now)

    after_bt = conn.execute(
        "SELECT COUNT(*) c FROM backtest_runs").fetchone()["c"]
    after_an = conn.execute(
        "SELECT COUNT(*) c FROM analysis_runs").fetchone()["c"]
    # run_backtest entry (error 無し) だけが書かれる。
    assert after_bt == before_bt + 1
    # error 応答の analyze_corr entry は書かれず、id も積まれない。
    assert after_an == before_an
    assert analysis_run_ids == []


def test_compensation_failure_does_not_propagate_to_caller(
        loop_min, conn, mission_and_run_fixture, tmp_path, monkeypatch):
    """D-11 (検収 R2、プラン L19622、10.11 節 M1b / 実装本体は
    improve_loop.py:873-877 に既にあるが killer が未執筆だった):
    補償 tx (`_compensate_tx2_failure`) 自体が失敗しても、その例外を
    `_finalize_success` の外側 (`_launch_slot` の呼び出し元スレッド) へ
    漏らしてはならない — 二重障害 (Tx-2 失敗 + 補償失敗) でも Mission
    worker スレッドを巻き込んで落とさず、`tx2_compensation_failed`
    activity を書いて非終端のまま残す (次回起動時の reconcile 任せ)。

    `_compensate_tx2_failure` を monkeypatch で常に例外送出させ、
    `_finalize_success` の呼び出しが例外なく戻ることと
    `activity.write(..., "tx2_compensation_failed", ...)` が呼ばれた
    ことを assert する。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture

    # Tx-2 本体を必ず失敗させる (approvals_store.create で fault 注入 —
    # test_commit_rolls_back_tx2_on_db_fault_between_gate_rows_and_approval
    # と同じ手法)。
    from agentic_fx.store import approvals as approvals_store

    def _tx2_boom(*a, **kw):
        raise RuntimeError("simulated Tx-2 fault")
    monkeypatch.setattr(approvals_store, "create", _tx2_boom)

    def _compensation_boom(*a, **kw):
        raise RuntimeError("simulated compensation fault")
    monkeypatch.setattr(loop_min, "_compensate_tx2_failure", _compensation_boom)

    # 例外なく戻ることそのものが pin (送出されればテストは失敗する)。
    loop_min._finalize_success(
        conn, mission_id=mission_id, run_id=run_id, backlog_id=backlog_id,
        slot_key=None, approval_payload={"name": "myst", "kind": "strategy"},
        now=datetime(2026, 8, 22, tzinfo=timezone.utc))

    activity_text = (tmp_path / "activity.log").read_text()
    assert "tx2_compensation_failed" in activity_text
    assert f"mission_id={mission_id} run_id={run_id}" in activity_text


def test_compensate_tx2_failure_actually_writes_failed_mission_and_observation(
        loop_min, conn, mission_and_run_fixture, monkeypatch):
    """A10 是正 (束D検収, verified-local-round1.md §11 #8):
    `test_compensation_failure_does_not_propagate_to_caller` は
    `_compensate_tx2_failure` **自体**を monkeypatch で例外送出に
    置き換えるため、実 `_compensate_tx2_failure` の本体 (mission
    `failed` / backlog `observation` / `last_result='commit_failed'`
    への遷移) は 1 行も実行されない。ここでは `_compensate_tx2_failure`
    には触れず、Tx-2 本体 (`approvals_store.create`) だけを失敗させて
    実補償 tx を走らせ、3 列が実際に書かれることを assert する。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture

    from agentic_fx.store import approvals as approvals_store

    def _tx2_boom(*a, **kw):
        raise RuntimeError("simulated Tx-2 fault")
    monkeypatch.setattr(approvals_store, "create", _tx2_boom)

    loop_min._finalize_success(
        conn, mission_id=mission_id, run_id=run_id, backlog_id=backlog_id,
        slot_key=None, approval_payload={"name": "myst", "kind": "strategy"},
        now=datetime(2026, 8, 22, tzinfo=timezone.utc))

    mission = conn.execute(
        "SELECT status FROM missions WHERE id=?", (mission_id,)).fetchone()
    assert mission["status"] == "failed"
    backlog = conn.execute(
        "SELECT status, last_result FROM improvement_backlog WHERE id=?",
        (backlog_id,)).fetchone()
    assert backlog["status"] == "observation"
    assert backlog["last_result"] == "commit_failed"


# round2 #6/#7 是正 (2026-08-29、verified-round2.md、設計逐語違反):
# `_finalize_report_or_observation` (report_path is None の経路) は
# (a) artifact=observation のとき偽ラベル `unsupported_in_plan10:risk_gate`
#     を書いていた (設計 §4.3 の `observation:<reason>` 逐語違反)
# (b) staging を削除していなかった (設計 §4.2 手順9 逐語違反 — 他 4 終端
#     経路と非対称)。

def _finalize_report_or_observation_ctx(staging_dir, *, mission_id, run_id):
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"analyze_corr": 60.0,
                                                        "run_backtest": 600.0})
    ledger.freeze()
    return ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging_dir,
        source_snapshot_dir=staging_dir / "_snapshot_src",
        allowed_backlog_ids=None, slot_key=None, ledger=ledger,
        rpc_handlers={})


class _CommitFailingConnection:
    """Delegate SQL to a real connection but fail every commit deterministically."""
    def __init__(self, conn):
        self._conn = conn

    def execute(self, *args, **kwargs):
        return self._conn.execute(*args, **kwargs)

    def commit(self):
        raise RuntimeError("simulated commit failure")

    def rollback(self):
        return self._conn.rollback()


def test_approval_activity_is_written_only_after_successful_commit(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    mission_id, run_id, backlog_id = mission_and_run_fixture
    loop_min._finalize_success(
        _CommitFailingConnection(conn), mission_id=mission_id, run_id=run_id,
        backlog_id=backlog_id, slot_key=None,
        approval_payload={"name": "myst", "kind": "indicator"},
        now=datetime(2026, 8, 22, tzinfo=timezone.utc))
    text = ((tmp_path / "activity.log").read_text()
            if (tmp_path / "activity.log").exists() else "")
    assert "\tapproval_requested\t" not in text


@pytest.mark.parametrize(("report_path", "artifact", "forbidden_event"), [
    ("/tmp/report.md", {"type": "report"}, "report_published"),
    (None, {"type": "observation", "reason": "x"}, "mission_observation"),
])
def test_report_and_observation_activity_are_written_only_after_successful_commit(
        loop_min, conn, mission_and_run_fixture, tmp_path,
        report_path, artifact, forbidden_event):
    mission_id, run_id, backlog_id = mission_and_run_fixture
    staging_dir = tmp_path / "staging-commit-fail"
    staging_dir.mkdir()
    ctx = _finalize_report_or_observation_ctx(
        staging_dir, mission_id=mission_id, run_id=run_id)
    with pytest.raises(RuntimeError, match="simulated commit failure"):
        loop_min._finalize_report_or_observation(
            _CommitFailingConnection(conn), ctx=ctx, backlog_id=backlog_id,
            report_path=report_path, artifact=artifact,
            now=datetime(2026, 8, 22))
    text = ((tmp_path / "activity.log").read_text()
            if (tmp_path / "activity.log").exists() else "")
    assert f"\t{forbidden_event}\t" not in text


def test_observation_artifact_records_reason_not_risk_gate_label(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    mission_id, run_id, backlog_id = mission_and_run_fixture
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    ctx = _finalize_report_or_observation_ctx(
        staging_dir, mission_id=mission_id, run_id=run_id)

    loop_min._finalize_report_or_observation(
        conn, ctx=ctx, backlog_id=backlog_id, report_path=None,
        artifact={"type": "observation", "reason": "insufficient data"},
        now=datetime(2026, 8, 22))

    backlog = conn.execute(
        "SELECT status, last_result FROM improvement_backlog WHERE id=?",
        (backlog_id,)).fetchone()
    assert backlog["status"] == "observation"
    assert backlog["last_result"] == "observation:insufficient data"
    run = conn.execute(
        "SELECT result, report_state FROM improvement_runs WHERE id=?",
        (run_id,)).fetchone()
    assert run["result"] is None
    assert run["report_state"] == "none"
    activity_text = (tmp_path / "activity.log").read_text()
    assert "\tIMPROVE\tmission_observation\t" in activity_text
    assert (f"mission={mission_id} backlog={backlog_id} "
            f"reason=observation:insufficient data\t{mission_id}") in activity_text
    assert "\treport_published\t" not in activity_text


def test_observation_without_backlog_logs_dash(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    mission_id, run_id, _ = mission_and_run_fixture
    staging_dir = tmp_path / "staging-no-backlog"
    staging_dir.mkdir()
    ctx = _finalize_report_or_observation_ctx(
        staging_dir, mission_id=mission_id, run_id=run_id)
    loop_min._finalize_report_or_observation(
        conn, ctx=ctx, backlog_id=None, report_path=None,
        artifact={"type": "observation", "reason": "no candidate"},
        now=datetime(2026, 8, 22))
    activity_text = (tmp_path / "activity.log").read_text()
    assert f"mission={mission_id} backlog=- reason=observation:no candidate" in activity_text


def test_risk_gate_report_artifact_keeps_unsupported_label(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    """対照: §4.3 表の別の行 (`proposal_kind=risk_gate`) は従来どおり
    `unsupported_in_plan10:risk_gate` のままであること — 片方だけだと
    分岐を丸ごと消す変異 (常に observation ラベルにする等) を殺せない。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    ctx = _finalize_report_or_observation_ctx(
        staging_dir, mission_id=mission_id, run_id=run_id)

    loop_min._finalize_report_or_observation(
        conn, ctx=ctx, backlog_id=backlog_id, report_path=None,
        artifact={"type": "report", "proposal_kind": "risk_gate"},
        now=datetime(2026, 8, 22))

    backlog = conn.execute(
        "SELECT status, last_result FROM improvement_backlog WHERE id=?",
        (backlog_id,)).fetchone()
    assert backlog["status"] == "observation"
    assert backlog["last_result"] == "unsupported_in_plan10:risk_gate"


def test_report_path_deletes_staging_including_readonly_snapshot(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    """殺すテスト案: 0500/0400 の readonly tree を実際に作ってから回す
    (0700 のままだと `_delete_staging` を消す変異を rmtree の成否では
    殺せない)。"""
    import os
    mission_id, run_id, backlog_id = mission_and_run_fixture
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    snapshot_dir = staging_dir / "_snapshot_src"
    snapshot_dir.mkdir()
    plugin_file = snapshot_dir / "plugin.py"
    plugin_file.write_text("x = 1\n")
    os.chmod(plugin_file, 0o400)
    os.chmod(snapshot_dir, 0o500)
    ctx = _finalize_report_or_observation_ctx(
        staging_dir, mission_id=mission_id, run_id=run_id)
    report_path = tmp_path / "report.md"
    report_path.write_text("# report\n")
    part_dir = tmp_path / ".tmp"
    # _publish_report は run_id 由来の part_path
    # (reports_dir/.tmp/improve-<mission_id>.md.part) を rename するため、
    # _root を tmp_path に固定し、その配置に実体を用意する。
    loop_min._root = tmp_path
    reports_dir = tmp_path / "data" / "improve_reports"
    (reports_dir / ".tmp").mkdir(parents=True)
    part_path = reports_dir / ".tmp" / f"improve-{mission_id}.md.part"
    part_path.write_text("# report\n")
    final_path = reports_dir / f"improve-{mission_id}.md"

    loop_min._finalize_report_or_observation(
        conn, ctx=ctx, backlog_id=backlog_id, report_path=str(final_path),
        artifact={"type": "report"}, now=datetime(2026, 8, 22))

    assert not staging_dir.exists()
    run = conn.execute(
        "SELECT result, report_state FROM improvement_runs WHERE id=?",
        (run_id,)).fetchone()
    assert run["result"] == "report"
    assert run["report_state"] == "published"
    activity_text = (tmp_path / "activity.log").read_text()
    assert "\tIMPROVE\treport_published\t" in activity_text
    assert (f"mission={mission_id} backlog={backlog_id} path={final_path}"
            f"\t{mission_id}") in activity_text
    assert "\tmission_observation\t" not in activity_text


def test_report_publish_failure_does_not_log_published(
        loop_min, conn, mission_and_run_fixture, tmp_path):
    """2 周目 CR3 (実形状): `_publish_report` は rename 失敗で例外を出さず
    `_fail_report` (report_state=failed) して False を返す。その経路で
    `report_published` を書いてはならない。段 0 変異「publish 成否を無視」は
    `_publish_report` を raise する mock で書いた旧版では生存した — 実経路
    (final_path が既に存在 → RENAME_NOREPLACE が EEXIST) で踏む。"""
    mission_id, run_id, backlog_id = mission_and_run_fixture
    staging_dir = tmp_path / "staging-publish-fail"
    staging_dir.mkdir()
    ctx = _finalize_report_or_observation_ctx(
        staging_dir, mission_id=mission_id, run_id=run_id)
    loop_min._root = tmp_path
    reports_dir = tmp_path / "data" / "improve_reports"
    (reports_dir / ".tmp").mkdir(parents=True)
    (reports_dir / ".tmp" / f"improve-{mission_id}.md.part").write_text("# r\n")
    final_path = reports_dir / f"improve-{mission_id}.md"
    final_path.write_text("# already there\n")  # rename_conflict を誘発

    loop_min._finalize_report_or_observation(
        conn, ctx=ctx, backlog_id=backlog_id, report_path=str(final_path),
        artifact={"type": "report"}, now=datetime(2026, 8, 22))

    run = conn.execute(
        "SELECT report_state FROM improvement_runs WHERE id=?",
        (run_id,)).fetchone()
    assert run["report_state"] == "failed"
    activity_text = ((tmp_path / "activity.log").read_text()
                     if (tmp_path / "activity.log").exists() else "")
    assert "\treport_published\t" not in activity_text
