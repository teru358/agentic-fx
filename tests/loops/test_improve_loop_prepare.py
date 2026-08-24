"""ImproveLoop.prepare (設計書 §4 冒頭, プラン §8.1-6/11/23)。"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from agentic_fx.loops.improve_run_context import ImproveRunContext
from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger

from tests.loops.conftest import _prepare_wave_slot


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


def test_tx0_creates_mission_and_run_atomically_scheduler_wave(loop_full, conn, clock):
    """scheduler wave (slot_key あり) のとき、missions/run/slot が
    1 commit で現れる (部分状態が観測できない)。

    プラン L16050-16069 の逐語 (D-2)。**未申告の適応**: `_build_loop(conn)`
    (プランの想定シグネチャ、tmp_path/clock 引数なし) は現物の
    `tests/loops/conftest.py::_build_loop(conn, tmp_path, *, clock=None)`
    と一致しないため、共有 fixture `loop_full` (= `_conn_for_test` シーム
    付き。D-10 是正で `prepare()` にもこのシームを対称実装したため、
    `prepare()` 完了後も同一 `conn` で状態確認できる) を使う。
    `_prepare_wave_slot` はキーワード専用シグネチャに合わせて呼ぶ。"""
    now = clock.now()
    _prepare_wave_slot(conn, period_key="2026-W34", k=0, now=now)

    mission, ctx, runner = loop_full.prepare(slot_key=("2026-W34", 0), now=now)

    m = conn.execute("SELECT * FROM missions WHERE id=?", (ctx.mission_id,)).fetchone()
    assert m["status"] == "running"
    assert m["loop"] == "improve"
    r = conn.execute("SELECT * FROM improvement_runs WHERE id=?", (ctx.run_id,)).fetchone()
    assert r["backlog_id"] is None
    assert r["mission_id"] == ctx.mission_id
    slot = conn.execute(
        "SELECT status, mission_id FROM improve_wave_slots "
        "WHERE wave_period_key='2026-W34' AND k=0").fetchone()
    assert slot["status"] == "claimed"
    assert slot["mission_id"] == ctx.mission_id


def test_tx0_manual_one_shot_has_no_slot_claim(loop_full, conn, clock):
    """手動 one-shot (slot_key=None) は slot claim を行わない。

    プラン L16072-16079 の逐語 (D-2)。適応は上記と同様 (`loop_full` を使う)。"""
    now = clock.now()
    mission, ctx, runner = loop_full.prepare(slot_key=None, now=now)
    assert ctx.allowed_backlog_ids is None  # 全バックログ担当 (印なし)
    n = conn.execute("SELECT count(*) c FROM improve_wave_slots").fetchone()["c"]
    assert n == 0


def test_materialize_workspace_copies_approved_plugin_source_into_snapshot(
        loop_min, conn, clock):
    """D-8 pin: `_materialize_workspace` は R-D3 が要求する
    「discover 済み plugin metas をコピーする」を満たす — 空リストへ
    `copy_source_snapshot([])` の退行 (検収 D-8 で検出) を検出する。"""
    from agentic_fx.plugin.loader import content_hash
    from agentic_fx.store import approvals as approvals_store

    plugins_dir = loop_min._root / "plugins"
    plugin_dir = plugins_dir / "sample_ind"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text("def compute(df, params):\n    return {}\n")
    (plugin_dir / "config.yaml").write_text("kind: indicator\n")
    (plugin_dir / "test_plugin.py").write_text("def test_x():\n    pass\n")
    ch = content_hash(plugin_dir)
    aid = approvals_store.create(
        conn, "plugin", {"name": "sample_ind", "content_hash": ch}, clock.now())
    approvals_store.apply_decision(
        conn, aid, status="approved", decided_by="test", now=clock.now())

    staging_dir, source_snapshot_dir = loop_min._materialize_workspace(
        conn, 42, None)

    copied = source_snapshot_dir / "sample_ind"
    assert (copied / "plugin.py").is_file()
    assert (copied / "config.yaml").is_file()
    assert (copied / "test_plugin.py").is_file()
    assert (copied / "plugin.py").read_text() == "def compute(df, params):\n    return {}\n"


def test_compute_partition_hint_disjoint_covers_open_backlog_across_slots(
        loop_min, conn, clock):
    """D-1 pin: scheduler wave (expected=2) は open backlog を
    `id % expected == k` で互いに素な分割にする — `_compute_partition_hint`
    が常に `None` (全担当) を返す退行や `%`/`==` の演算子退行を検出する。"""
    from agentic_fx.store import backlog as backlog_store
    from agentic_fx.store import improve_waves

    ids = [backlog_store.add(conn, f"idea-{i}", "user", clock.now())
          for i in range(4)]
    improve_waves.create_wave_and_slots(
        conn, period_key="2026-W40", now=clock.now(), expected=2)

    hint0 = loop_min._compute_partition_hint(conn, ("2026-W40", 0))
    hint1 = loop_min._compute_partition_hint(conn, ("2026-W40", 1))

    assert hint0 == frozenset(i for i in ids if i % 2 == 0)
    assert hint1 == frozenset(i for i in ids if i % 2 == 1)
    # 全体をちょうど 1 回ずつ担当する (互いに素・網羅)
    assert hint0 | hint1 == frozenset(ids)
    assert hint0 & hint1 == frozenset()

    # slot_key=None は全バックログ担当 (印なし)
    assert loop_min._compute_partition_hint(conn, None) is None


def test_tx0_mission_id_unique_partial_index_on_improvement_runs(conn):
    """`improvement_runs.mission_id` に対する部分 UNIQUE の migration が
    効いていること (2 回目の INSERT が同じ mission_id を指せば
    IntegrityError)。新規行についてのみ強制 — 既存 NULL 行は許容する。
    merge (main 649a811 束 C 裁定 D1) が improvement_runs.mission_id に
    FK を付けたため、実在しない mission_id の直挿しは FK 違反で拒否される。
    UNIQUE 索引を検証するには実在する mission 行を先に作る必要がある。"""
    now = datetime(2026, 8, 22, 3, 0)
    mission_id = conn.execute(
        "INSERT INTO missions (loop, runner, model, status, started_at) "
        "VALUES ('improve', 'local', 'x', 'running', ?)",
        (now.isoformat(),)).lastrowid
    conn.execute(
        "INSERT INTO improvement_runs (backlog_id, mission_id, started_at) "
        "VALUES (NULL, ?, ?)", (mission_id, now.isoformat()))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO improvement_runs (backlog_id, mission_id, started_at) "
            "VALUES (NULL, ?, ?)", (mission_id, now.isoformat()))


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


def test_build_mission_tools_matches_child_registry_names(loop_full, tmp_path):
    """B12/B17: 親が Mission.tools へ埋める名前集合が、子
    (`build_mission_registry("improve", ...)`) が実際に登録する名前集合と
    一致する — 「エージェントに見せる」と「子で実行できる」の食い違いを
    構造的に防ぐ pin。"""
    from agentic_fx.tools.mission_registry import build_mission_registry

    staging_dir, source_snapshot_dir = tmp_path / "s", tmp_path / "src"
    staging_dir.mkdir()
    source_snapshot_dir.mkdir()
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"run_backtest": 1.0,
                                                        "analyze_corr": 1.0})
    rpc_handlers = {"run_backtest": lambda a: {}, "analyze_corr": lambda a: {}}

    tools = loop_full._build_mission_tools(
        staging_dir=staging_dir, source_snapshot_dir=source_snapshot_dir,
        ledger=ledger, rpc_handlers=rpc_handlers)
    tool_names = {t["function"]["name"] for t in tools}

    child_registry = build_mission_registry(
        "improve", None, loop_full._settings, None, None, activity=None,
        staging_dir=staging_dir, source_snapshot_dir=source_snapshot_dir,
        ledger=ledger, rpc_handlers=rpc_handlers)
    assert tool_names == set(child_registry.names())

    # signal_tools.IMPROVE_FORBIDDEN との非交差 (tests/loops/test_improve_forbidden.py
    # と同型の pin — B17 申し送り)
    from agentic_fx.tools import signal_tools
    assert not (tool_names & signal_tools.IMPROVE_FORBIDDEN)


def test_build_worker_runner_passes_ctx_as_run_context(loop_full, conn):
    """`_build_worker_runner` が `ImproveRunContext` をそのまま
    `WorkerRunner(run_context=ctx)` へ渡すことの契約テスト (D-4 是正、
    プラン L18265 逐語 — 検収で「欠落 (10.9 Step7 M6/M7 を殺す唯一の
    pin)」と指摘された)。"""
    ctx = ImproveRunContext(
        mission_id=1, run_id=1, staging_dir=Path("/tmp/x"),
        source_snapshot_dir=Path("/tmp/y"), allowed_backlog_ids=None,
        slot_key=None, ledger=ImproveRpcLedger(rpc_timeout_sec_by_kind={}),
        rpc_handlers={})
    runner = loop_full._build_worker_runner(ctx)
    assert runner._run_context is ctx
    assert runner._worker_profile == "improve"


