"""tests/loops/test_improve_loop_*.py 共有 fixture (プラン10 Task10、B3)。

`tests/loops/test_trade_loop.py` の `connect(tmp_path/"t.db")` +
`init_db(conn)` パターンを踏襲する。"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock
from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
from agentic_fx.loops.improve_run_context import ImproveRunContext
from agentic_fx.store import backlog as backlog_store
from agentic_fx.store import improve_runs as improve_runs_store
from agentic_fx.store import improve_waves
from agentic_fx.store import missions as missions_store
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


class _FakeRag:
    """`ImproveLoop.__init__(rag=...)` (T10-B10) を満たすだけの no-op —
    本節の private メソッドテストは Rag を経由しない (RAG ツールは
    research_tools 経由、10.9 節 B17 の registry 配線テストで別途扱う)。"""


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    yield c
    c.close()


@pytest.fixture
def clock():
    return FixedClock(NOW)


def _build_loop(conn, tmp_path, *, clock=None):
    from agentic_fx.loops.improve_loop import ImproveLoop

    return ImproveLoop(
        root=tmp_path, settings=SETTINGS, clock=clock or FixedClock(NOW),
        db_write_conn_factory=lambda: conn,
        db_readonly_conn_factory=lambda: conn,
        activity=ActivityLog(tmp_path / "activity.log"), rag=_FakeRag())


@pytest.fixture
def loop_min(conn, tmp_path, clock):
    return _build_loop(conn, tmp_path, clock=clock)


@pytest.fixture
def loop_full(conn, tmp_path, clock):
    # precheck 2026-08-22 wave2: T10-B3 — loop_min と同一構築 (上記説明参照)
    loop = _build_loop(conn, tmp_path, clock=clock)
    loop._conn_for_test = conn  # テストシーム：conn close を回避
    return loop


@pytest.fixture
def loop_and_ctx(loop_min, conn, tmp_path):
    """`ImproveRunContext` を直接構築する軽量 fixture — `prepare()` の
    Tx-0/workdir 作成を経由せず、`commit()` 経由メソッド (`_select_and_bind`
    等) の単体テストに使う。"""
    staging_dir = tmp_path / "staging"
    source_snapshot_dir = tmp_path / "source"
    staging_dir.mkdir(parents=True, exist_ok=True)
    source_snapshot_dir.mkdir(parents=True, exist_ok=True)

    # 10.4+ 節のテスト用に backlog 行を作っておく
    # (test_out_of_partition_selection_is_not_rejected_only_logged が需要)
    # ID 1, 2 は allowed_backlog_ids に含まれる
    # ID 999 は存在するがまたれる allowed_backlog_ids に含まれない (out_of_partition 用)
    backlog_store.add(conn, "idea-1", "user", NOW)
    backlog_store.add(conn, "idea-2", "user", NOW)
    # sqlite3 の AUTOINCREMENT は挙動が複雑なため、999 を直接挿入
    conn.execute(
        "INSERT INTO improvement_backlog (id, idea, source, status, created_at, "
        "updated_at) VALUES (999, 'out-of-partition-idea', 'user', 'open', ?, ?)",
        (NOW.isoformat(),) * 2)
    conn.commit()

    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={
        "run_backtest": 600.0, "analyze_corr": 600.0})
    ctx = ImproveRunContext(
        mission_id=1, run_id=1, staging_dir=staging_dir,
        source_snapshot_dir=source_snapshot_dir,
        allowed_backlog_ids=frozenset({1, 2}),  # 10.4 用に設定
        slot_key=None, ledger=ledger, rpc_handlers={})
    return loop_min, ctx, conn


@pytest.fixture
def loop_and_ctx_with_open_backlog(loop_min, conn, tmp_path, clock):
    """`loop_and_ctx` を拡張し、open status の backlog 行と実際の run 行を
    1 件ずつ作成して返す (10.5 の _select_and_bind テスト用)。"""
    staging_dir = tmp_path / "staging"
    source_snapshot_dir = tmp_path / "source"
    staging_dir.mkdir(parents=True, exist_ok=True)
    source_snapshot_dir.mkdir(parents=True, exist_ok=True)

    # backlog 行を複数作成 (パターンマッチに引っかからないもので準備)
    conn.execute(
        "INSERT INTO improvement_backlog (id, idea, source, status, created_at, "
        "updated_at) VALUES (999, 'out-of-partition-idea', 'user', 'open', ?, ?)",
        (NOW.isoformat(),) * 2)
    # open status の backlog を 1 件追加 (テスト用に使う)
    backlog_id = backlog_store.add(conn, "selectable-idea", "user", NOW)

    # 実際の mission/run を作成
    mission_id = missions_store.start(
        conn, "improve", loop_min._settings.runner.improve.backend,
        loop_min._settings.runner.improve.model, now=NOW, commit=False)
    run_id = improve_runs_store.start(
        conn, backlog_id=None, mission_id=mission_id, now=NOW, commit=False)
    conn.commit()

    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={
        "run_backtest": 600.0, "analyze_corr": 600.0})
    ctx = ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging_dir,
        source_snapshot_dir=source_snapshot_dir,
        allowed_backlog_ids=frozenset({1, 2}),
        slot_key=None, ledger=ledger, rpc_handlers={})
    return loop_min, ctx, conn, backlog_id


@pytest.fixture
def mission_and_run_fixture(conn, clock):
    """`missions`+`improvement_backlog`+`improvement_runs` の最小行を作り
    `(mission_id, run_id, backlog_id)` を返す (slot 無し = 手動 one-shot 相当)。

    D-10 是正 (M7 pin 案、検収 3-b): 素朴に 1 件ずつ作ると
    `mission_id == run_id == 1` に縮退し、`reconcile_report_outbox` の
    part path キーが `row['mission_id']` か `row['id']` (run id) かを
    区別できなくなる (10.9 M7 が SURVIVED した根本原因)。捨て mission を
    1 件先に `missions` へ挿入して mission_id をずらす —
    `improvement_runs` は別テーブルの自動採番なので run_id には影響しない
    (`mission_id=2, run_id=1` になる)。"""
    _ = missions_store.start(
        conn, "improve", "codex", "gpt-5", clock.now(), commit=False)
    mission_id = missions_store.start(
        conn, "improve", "codex", "gpt-5", clock.now(), commit=False)
    backlog_id = backlog_store.add(conn, "idea", "user", clock.now())
    run_id = improve_runs_store.start(
        conn, None, clock.now(), mission_id=mission_id, commit=False)
    conn.commit()
    return mission_id, run_id, backlog_id


@pytest.fixture
def mission_and_run_fixture_with_slot(conn, clock):
    """`mission_and_run_fixture` に加え、wave/slot を作って claim 済みに
    した `(mission_id, run_id, backlog_id, slot_key)` を返す。"""
    period_key = "2026-W34"
    improve_waves.create_wave_and_slots(
        conn, period_key=period_key, now=clock.now(), expected=1, commit=False)
    mission_id = missions_store.start(
        conn, "improve", "codex", "gpt-5", clock.now(), commit=False)
    claimed = improve_waves.claim_slot(
        conn, period_key=period_key, k=0, mission_id=mission_id,
        now=clock.now(), commit=False)
    assert claimed
    backlog_id = backlog_store.add(conn, "idea", "user", clock.now())
    run_id = improve_runs_store.start(
        conn, None, clock.now(), mission_id=mission_id, commit=False)
    conn.commit()
    return mission_id, run_id, backlog_id, (period_key, 0)


@pytest.fixture
def conn_with_approved_strategy(conn, clock):
    """10.7 節の baseline 照合 (`approval_requests.status='approved'` の
    最新行での近似、統合裁定 R-i12) が読む承認済み plugin 行を 1 件作る。
    `approvals` テーブルの列名は Task 8 の詳細節が正 — ここでは
    `store/approvals.py::create` をそのまま使う。"""
    from agentic_fx.store import approvals as approvals_store

    approvals_store.create(
        conn, kind="plugin",
        payload={"name": "baseline_strategy", "kind": "strategy",
                 "content_hash": "baseline-hash"},
        now=clock.now(), commit=False)
    conn.execute(
        "UPDATE approval_requests SET status='approved' WHERE id="
        "(SELECT id FROM approval_requests ORDER BY id DESC LIMIT 1)")
    conn.commit()
    return conn


def _prepare_wave_slot(conn, *, period_key: str, k: int, now, expected: int = 1):
    """wave+slot を `reserved` のまま用意する (claim 前状態が要るテスト用)。"""
    improve_waves.create_wave_and_slots(
        conn, period_key=period_key, now=now, expected=expected, commit=True)
    return period_key, k
