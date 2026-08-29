"""ImproveLoop.prepare (設計書 §4 冒頭, プラン §8.1-6/11/23)。"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.loops.improve_run_context import ImproveRunContext
from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
from agentic_fx.store import backlog as backlog_store
from agentic_fx.store import improve_runs as improve_runs_store
from agentic_fx.store import improve_waves
from agentic_fx.store import missions as missions_store
from agentic_fx.store.db import connect, connect_readonly, init_db

from tests.loops.conftest import SETTINGS, _FakeRag, _prepare_wave_slot


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


def test_tx0_creates_mission_and_run_atomically_scheduler_wave(
        loop_full, conn, clock, tmp_path):
    """scheduler wave (slot_key あり) のとき、missions/run/slot が
    1 commit で現れる (部分状態が観測できない)。

    プラン L16050-16069 の逐語 (D-2)。**未申告の適応**: `_build_loop(conn)`
    (プランの想定シグネチャ、tmp_path/clock 引数なし) は現物の
    `tests/loops/conftest.py::_build_loop(conn, tmp_path, *, clock=None)`
    と一致しないため、共有 fixture `loop_full` (= `_conn_for_test` シーム
    付き。D-10 是正で `prepare()` にもこのシームを対称実装したため、
    `prepare()` 完了後も同一 `conn` で状態確認できる) を使う。
    `_prepare_wave_slot` はキーワード専用シグネチャに合わせて呼ぶ。

    D-2b 是正 (検収 R2): `loop_full` の write/readonly 両 factory は
    同一 `conn` オブジェクトを返すため、`prepare()` が返った後に**同じ
    `conn`** で読むと「commit 済み」と「同一 tx 内で書いただけ」を区別
    できない (`conn.commit()` を除去する変異が SURVIVED — killer は
    D-9 の `tests/test_service_app.py::
    test_submit_manual_with_real_improve_loop_prepares_without_notimplementederror`
    (別接続で DB を読む) が担っていた)。ここでは `tmp_path/"t.db"` へ
    **別の readonly 接続**を新規に開いて読み、Tx-0 が実際にディスクへ
    commit されたことをこのテスト自身で検証する。"""
    now = clock.now()
    _prepare_wave_slot(conn, period_key="2026-W34", k=0, now=now)
    # L-B13 裁定是正: expected=1 の scheduler wave は空 partition だと
    # `_compute_partition_hint` が raise するようになった。open backlog
    # を 1 件用意する (expected=1 なので id % 1 == 0 == k は常に成立)。
    backlog_store.add(conn, "idea-for-partition", "user", now)
    conn.commit()

    mission, ctx, runner = loop_full.prepare(slot_key=("2026-W34", 0), now=now)

    fresh = connect_readonly(tmp_path / "t.db")
    try:
        m = fresh.execute(
            "SELECT * FROM missions WHERE id=?", (ctx.mission_id,)).fetchone()
        assert m["status"] == "running"
        assert m["loop"] == "improve"
        r = fresh.execute(
            "SELECT * FROM improvement_runs WHERE id=?",
            (ctx.run_id,)).fetchone()
        assert r["backlog_id"] is None
        assert r["mission_id"] == ctx.mission_id
        slot = fresh.execute(
            "SELECT status, mission_id FROM improve_wave_slots "
            "WHERE wave_period_key='2026-W34' AND k=0").fetchone()
        assert slot["status"] == "claimed"
        assert slot["mission_id"] == ctx.mission_id
    finally:
        fresh.close()


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


def test_materialize_workspace_creates_staging_dir_with_mode_0700(
        loop_min, conn):
    """D13 是正 (段0 非致命だが本番破壊): `mission_worker.py` の dirfd
    再検証が mode 0700 を要求するため、`_materialize_workspace` が作る
    `staging_dir` の mode が 0700 でないと**すべての improve Mission が
    子側で起動拒否**になる (実 subprocess 経路のテストは `prepare()` を
    経由せず自前で staging を組むため、この mode は一度も実行経路に
    乗っていなかった)。`umask(0o022)` 下でも成立することを見る —
    mkdir の umask マスクがこの `chmod` の存在理由なので、umask を
    触らないと恒真になりうる。"""
    import os
    old_umask = os.umask(0o022)
    try:
        staging_dir, _source_snapshot_dir = loop_min._materialize_workspace(
            conn, 44, None)
    finally:
        os.umask(old_umask)
    assert (staging_dir.stat().st_mode & 0o777) == 0o700


def test_materialize_workspace_copies_examples_from_self_root_docs_examples_plugins(
        loop_min, conn):
    """D-12 pin (検収 R2): `_materialize_workspace` が `copy_examples_snapshot`
    へ渡す `examples_root` (`improve_loop.py:369`
    ``self._root / "docs" / "examples" / "plugins"``) を破壊する変異
    (宛先を間違えるパス退行) が 921 本 SURVIVED していた — `copy_examples_snapshot`
    は不在ディレクトリで黙って return する fail-open (improve_loop.py:125)
    なので、宛先を間違えても誰も気づかない。`loop_min._root` (= tmp_path)
    配下に `docs/examples/plugins/<name>/plugin.py` を実際に作り、
    `_materialize_workspace` の戻り値 `source_snapshot_dir` (=
    `staging_dir/_snapshot_src`) の `_examples/<name>/plugin.py` へ実在の
    サンプル plugin が届くことを直接観測する (プラン §7.1 条件 6 /
    L163 の要求)。"""
    examples_root = loop_min._root / "docs" / "examples" / "plugins"
    example_dir = examples_root / "sma_cross"
    example_dir.mkdir(parents=True)
    (example_dir / "plugin.py").write_text(
        "def compute(df, params):\n    return {}\n")
    (example_dir / "config.yaml").write_text("kind: indicator\n")

    staging_dir, source_snapshot_dir = loop_min._materialize_workspace(
        conn, 43, None)

    copied = source_snapshot_dir / "_examples" / "sma_cross"
    assert (copied / "plugin.py").is_file()
    assert (copied / "config.yaml").is_file()
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


def test_compute_partition_hint_raises_when_wave_has_no_slots(loop_min, conn):
    """D09 是正 (段0 致命2): `slot_key` が非 None なのに対応する wave の
    slot が 1 件も無いのは矛盾 (呼び出し元は claim_slot 済みのはず) —
    fail-open (`None` = 全担当) にせず `RuntimeError` で落とす、という
    コード自身のコメントが明記する契約を pin する。`return None` に
    落ちると、wave slot が引けない異常時に全 slot が全バックログを
    担当してしまう (N 個の Mission が同じ backlog を取り合い、
    CLI 実行が丸ごと無駄になる)。"""
    with pytest.raises(RuntimeError, match="no wave slots"):
        loop_min._compute_partition_hint(conn, ("2026-W99", 0))


def test_compute_partition_hint_raises_when_partition_would_be_empty(
        loop_min, conn, clock):
    """L-B13 裁定 (2026-08-28、束D検収 verified-local-round1.md §7):
    空集合 partition (open backlog が無い、または `id % expected == k` に
    一致する id が無い) は `_compute_partition_hint` で `raise` する —
    `None` (印なし = 全担当) との区別を呼び出し元に無音で失わせない。"""
    improve_waves.create_wave_and_slots(
        conn, period_key="2026-W41", now=clock.now(), expected=1)
    # open backlog を 1 件も作らない → partition は必ず空集合
    with pytest.raises(RuntimeError, match="empty partition"):
        loop_min._compute_partition_hint(conn, ("2026-W41", 0))


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
        loop_full, conn, monkeypatch):
    """Tx-0 の途中 (missions INSERT 後・run INSERT 前) で例外が起きたら
    ロールバックし、mission 行も残らない (単一 tx pin)。

    ACC-B1 是正 (束D検収): Tx-0 自体の失敗は `_conn_for_test` が無ければ
    prepare() が write conn を close するようになったため (漏れ修正)、
    是正後もテスト自身の `conn` で状態確認できる `loop_full` (シーム有り)
    を使う (`loop_min` は write conn = 検証用 `conn` そのものなので、
    是正後は prepare() 内で close されてしまい検証不能になる)。"""
    now = datetime(2026, 8, 22, 3, 0)

    from agentic_fx.store import improve_runs as improve_runs_store

    def _boom(*a, **kw):
        raise RuntimeError("simulated crash between INSERTs")

    monkeypatch.setattr(improve_runs_store, "start", _boom)

    with pytest.raises(RuntimeError):
        loop_full.prepare(slot_key=None, now=now)

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


def test_tx0_mutation_M2_system_exit_without_rollback(loop_full, conn, monkeypatch):
    """M2: BaseException ではなく Exception を catch することで、
    SystemExit でロールバックしない退行を検出する。

    ACC-B1 是正で `loop_min` → `loop_full` に適応 (理由は
    `test_tx0_crash_between_mission_insert_and_run_insert_leaves_no_orphan`
    の docstring 参照)。"""
    now = datetime(2026, 8, 22, 3, 0)
    from agentic_fx.store import improve_runs as improve_runs_store

    def _sys_exit(*a, **kw):
        raise SystemExit("simulated system exit")

    monkeypatch.setattr(improve_runs_store, "start", _sys_exit)

    with pytest.raises(SystemExit):
        loop_full.prepare(slot_key=None, now=now)

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


@pytest.mark.parametrize("injection_point", [
    "_materialize_workspace",
    "build_improve_context",
    "_build_worker_runner",
])
def test_prepare_failure_after_tx0_leaves_no_dangling_rows(
        loop_full, conn, clock, monkeypatch, injection_point):
    """I3 是正 (プラン10 束D round1 I3、ユーザー裁定 D②、2026-08-28):
    Tx-0 commit 後 (workspace/context/runner 構築) の例外は `prepare()`
    内部で自作の mission/run/slot を 1 tx で終端化してから再送出する —
    dangling `missions.status='running'` / `improvement_runs.finished_at
    IS NULL` / slot `claimed` を残さない。主害は dangling そのものより
    `_running_slot_count` (`WHERE status IN ('claimed','running')`) が
    容量を恒久的に食い潰すこと (既定 `improve.parallel=1` では改善ループ
    全停止) — fault matrix の各ケースで容量が 0 に戻ることを直接見る。"""
    from agentic_fx.loops import improve_loop as improve_loop_mod

    now = clock.now()
    _prepare_wave_slot(conn, period_key="2026-W50", k=0, now=now)
    # L-B13 裁定是正: 空 partition で _compute_partition_hint が raise
    # するようになった。partition 計算より前に fault を注入するケース
    # (injection_point の 3 種はいずれも partition 計算の後段) のみ影響
    # するので、open backlog を 1 件用意しておく。
    backlog_store.add(conn, "idea-for-partition", "user", now)
    conn.commit()

    def _boom(*a, **kw):
        raise OSError(f"simulated {injection_point} failure")

    if injection_point == "build_improve_context":
        monkeypatch.setattr(improve_loop_mod, "build_improve_context", _boom)
    else:
        monkeypatch.setattr(loop_full, injection_point, _boom)

    with pytest.raises(OSError, match=injection_point):
        loop_full.prepare(slot_key=("2026-W50", 0), now=now)

    m = conn.execute("SELECT count(*) c FROM missions "
                     "WHERE status='running'").fetchone()["c"]
    assert m == 0, "dangling running mission after prepare failure"
    r = conn.execute("SELECT count(*) c FROM improvement_runs "
                     "WHERE finished_at IS NULL").fetchone()["c"]
    assert r == 0, "dangling unfinished run after prepare failure"
    slot = conn.execute(
        "SELECT status FROM improve_wave_slots "
        "WHERE wave_period_key='2026-W50' AND k=0").fetchone()
    assert slot["status"] == "failed"
    running = conn.execute(
        "SELECT count(*) c FROM improve_wave_slots "
        "WHERE status IN ('claimed','running')").fetchone()["c"]
    assert running == 0, "capacity must recover (I3 主害: capacity 恒久消費)"


def test_prepare_failure_after_tx0_manual_one_shot_has_no_slot_to_terminalize(
        loop_full, conn, clock, monkeypatch):
    """I3 是正、`slot_key=None` (手動 one-shot、`submit_manual`) の枝:
    slot が存在しないので `finish_improve_mission(slot_key=None, ...)`
    で mission/run だけ終端化する — slot 前提のコードが無条件に
    `slot_key` を要求すると `submit_manual` 経由の prepare 失敗で
    (別の) 例外に化けて補償自体が失敗する。"""
    now = clock.now()

    def _boom(*a, **kw):
        raise OSError("simulated _materialize_workspace failure")

    monkeypatch.setattr(loop_full, "_materialize_workspace", _boom)

    with pytest.raises(OSError, match="_materialize_workspace"):
        loop_full.prepare(slot_key=None, now=now)

    m = conn.execute("SELECT count(*) c FROM missions "
                     "WHERE status='running'").fetchone()["c"]
    assert m == 0
    r = conn.execute("SELECT count(*) c FROM improvement_runs "
                     "WHERE finished_at IS NULL").fetchone()["c"]
    assert r == 0


def test_prepare_seamless_tx0_durable_commit_and_closes_write_conn(
        loop_no_seam, clock):
    """束D検収是正 (verified-local-round1.md §11 #3, A20/A35/L-B7/ACC-B2):
    `loop_full`/`loop_min` は write/readonly 両 factory に同一 conn を
    返す (または `_conn_for_test` シームを立てる) ため、Tx-0 の durable
    commit と本番 conn 経路 (`_conn_for_test` 無し = prepare() 内で自前の
    write conn を close する側) が構造的に未検証だった。`loop_no_seam`
    (毎回新規接続、`_conn_for_test` 属性なし) で実測する。"""
    loop, db_path = loop_no_seam
    now = clock.now()
    _prepare_wave_slot(conn := connect(db_path), period_key="2026-W34", k=0, now=now)
    # L-B13 裁定是正: 空 partition で raise するようになったため open
    # backlog を 1 件用意する。
    backlog_store.add(conn, "idea-for-partition", "user", now)
    conn.commit()
    conn.close()

    captured = []
    real_factory = loop._db_write_conn_factory

    def _spy():
        c = real_factory()
        captured.append(c)
        return c

    loop._db_write_conn_factory = _spy

    mission, ctx, runner = loop.prepare(slot_key=("2026-W34", 0), now=now)

    # (a) 別接続から見て commit 済み (Tx-0 の原子性)
    fresh = connect_readonly(db_path)
    try:
        m = fresh.execute(
            "SELECT status FROM missions WHERE id=?", (ctx.mission_id,)).fetchone()
        assert m["status"] == "running"
        slot = fresh.execute(
            "SELECT status, mission_id FROM improve_wave_slots "
            "WHERE wave_period_key='2026-W34' AND k=0").fetchone()
        assert slot["status"] == "claimed"
        assert slot["mission_id"] == ctx.mission_id
    finally:
        fresh.close()

    # (b) 本番経路 (`_conn_for_test` 無し) の write conn は prepare() が
    # 自前で close する (D-10 是正の対称、`improve_loop.py:283-284`)。
    assert len(captured) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        captured[0].execute("SELECT 1")


def test_prepare_real_concurrent_cas_only_one_winner(tmp_path, clock):
    """束D検収是正 (verified-local-round1.md §11 #3, A20/A35/L-B7/ACC-B2 (b)):
    Tx-1 CAS (`claim_slot`) の敗者テストは従来「事前 UPDATE で slot を
    先に埋めてから prepare を呼ぶ」形で、真の並行 CAS 競合を再現して
    いなかった。ここでは実 2 スレッド + 実 2 接続 (別々の `ImproveLoop`
    インスタンス、それぞれ自前の write_conn_factory) で同一 slot を
    同時に `prepare()` させ、勝者 1・敗者 1 になることを実測する。"""
    import threading

    from agentic_fx.loops.improve_loop import ImproveLoop

    db_path = tmp_path / "t.db"
    bootstrap = connect(db_path)
    init_db(bootstrap)
    now = clock.now()
    _prepare_wave_slot(bootstrap, period_key="2026-W60", k=0, now=now)
    # L-B13 裁定是正: 空 partition で raise するようになったため、勝者側
    # (post-Tx0 の partition 計算まで進む) 用に open backlog を用意する。
    backlog_store.add(bootstrap, "idea-for-partition", "user", now)
    bootstrap.commit()
    bootstrap.close()

    def _make_loop(root):
        root.mkdir(parents=True, exist_ok=True)
        return ImproveLoop(
            root=root, settings=SETTINGS, clock=clock,
            db_write_conn_factory=lambda: connect(db_path),
            db_readonly_conn_factory=lambda: connect_readonly(db_path),
            activity=ActivityLog(root / "activity.log"), rag=_FakeRag())

    loop_a = _make_loop(tmp_path / "a")
    loop_b = _make_loop(tmp_path / "b")

    results = {}

    def _run(name, loop):
        try:
            loop.prepare(slot_key=("2026-W60", 0), now=now)
            results[name] = "ok"
        except RuntimeError as e:
            results[name] = str(e)

    t1 = threading.Thread(target=_run, args=("a", loop_a))
    t2 = threading.Thread(target=_run, args=("b", loop_b))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    outcomes = list(results.values())
    winners = [v for v in outcomes if v == "ok"]
    losers = [v for v in outcomes if "slot claim failed" in v]
    assert len(winners) == 1, results
    assert len(losers) == 1, results

    fresh = connect_readonly(db_path)
    try:
        slot = fresh.execute(
            "SELECT status, spawn_attempts FROM improve_wave_slots "
            "WHERE wave_period_key='2026-W60' AND k=0").fetchone()
        assert slot["status"] == "claimed"
        # 敗者の CAS は rowcount=0 で spawn_attempts を増やさない -> 1
        assert slot["spawn_attempts"] == 1
        m = fresh.execute(
            "SELECT count(*) c FROM missions WHERE status='running'").fetchone()
        assert m["c"] == 1, "敗者側の Tx-0 は rollback され dangling mission が残らない"
    finally:
        fresh.close()


def test_prepare_tx0_own_cas_failure_closes_write_conn_no_dangling_rows(
        loop_no_seam, clock):
    """束D検収是正 (ACC-B1 + A21、verified-local-round1.md §11 #1/#4):
    Tx-0 *自体*の失敗 (`claim_slot` の CAS 失敗) は `except BaseException:
    conn.rollback(); raise` の後 `finally: pass` (是正前の
    `improve_loop.py:205-209`) を通り、post-Tx0 失敗経路
    (`except BaseException:` @227-273、`_conn_for_test` が None なら
    close する) には**到達しない**まま prepare() を抜けていたため write
    conn が漏れていた (`probe_leakrate.py` 実測: 1 回につき厳密に 2 fd)。
    是正後は Tx-0 自体の失敗でも write conn が close されることと、
    `if not claimed: raise RuntimeError` (A21) がバックストップとして
    機能し、mission/run が dangling で残らないことを一本で実測する。"""
    loop, db_path = loop_no_seam
    now = clock.now()
    setup = connect(db_path)
    _prepare_wave_slot(setup, period_key="2026-W61", k=0, now=now)
    # 先に別 mission で claim 済みにしておき、slot を 'reserved' 以外へ
    # 動かす (= 次の CAS は必ず rowcount=0 で失敗する)。mission_id は
    # FK 制約があるため実在させる。
    setup.execute(
        "INSERT INTO missions (id, loop, runner, model, status, started_at) "
        "VALUES (999, 'improve', 'local', 'm', 'running', ?)",
        (now.isoformat(),))
    claimed = improve_waves.claim_slot(
        setup, period_key="2026-W61", k=0, mission_id=999, now=now)
    assert claimed
    setup.close()

    captured = []
    real_factory = loop._db_write_conn_factory

    def _spy():
        c = real_factory()
        captured.append(c)
        return c

    loop._db_write_conn_factory = _spy

    with pytest.raises(RuntimeError, match="slot claim failed"):
        loop.prepare(slot_key=("2026-W61", 0), now=now)

    assert len(captured) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        captured[0].execute("SELECT 1")

    fresh = connect_readonly(db_path)
    try:
        # id=999 は setup 用の他 mission (claim 済みにするため事前挿入)。
        # prepare() が Tx-0 自体の失敗で自作した mission (id != 999) が
        # dangling で残っていないことを見る。
        m = fresh.execute(
            "SELECT count(*) c FROM missions WHERE status='running' "
            "AND id != 999").fetchone()
        assert m["c"] == 0, "Tx-0 自体の失敗で dangling running mission が残ってはいけない"
        r = fresh.execute(
            "SELECT count(*) c FROM improvement_runs "
            "WHERE finished_at IS NULL").fetchone()
        assert r["c"] == 0, "Tx-0 自体の失敗で dangling unfinished run が残ってはいけない"
    finally:
        fresh.close()


def test_prepare_compensation_failure_with_activity_none_preserves_original_exception(
        tmp_path, clock, monkeypatch):
    """ACC-B3 是正 (束D検収, acceptance-round1.md B-3 /
    verified-local-round1.md §11 #14): `_compensate_prepare_failure` の
    外側 `except Exception:` ハンドラ内の `self._activity.write(...)` が
    無ガードだった。`activity=None` で構築した `ImproveLoop` かつ補償 tx
    自体も失敗する二重障害では、`AttributeError` が元例外 (Tx-0 後の
    実失敗、ここでは OSError) を置換していた。ここでは
    `_materialize_workspace` を失敗させ (post-Tx0 失敗)、かつ
    `missions_store.finish_improve_mission` (補償 tx 本体) も失敗させて
    二重障害を再現し、`prepare()` から**元の OSError** が送出されること
    (AttributeError に置換されないこと) を assert する。"""
    from agentic_fx.loops.improve_loop import ImproveLoop
    from tests.loops.conftest import SETTINGS, _FakeRag

    db_path = tmp_path / "t.db"
    bootstrap = connect(db_path)
    init_db(bootstrap)
    bootstrap.close()
    loop = ImproveLoop(
        root=tmp_path, settings=SETTINGS, clock=clock,
        db_write_conn_factory=lambda: connect(db_path),
        db_readonly_conn_factory=lambda: connect_readonly(db_path),
        activity=None, rag=_FakeRag())

    def _boom_materialize(*a, **kw):
        raise OSError("simulated _materialize_workspace failure")
    monkeypatch.setattr(loop, "_materialize_workspace", _boom_materialize)

    from agentic_fx.loops import improve_loop as improve_loop_mod

    def _boom_compensation(*a, **kw):
        raise RuntimeError("simulated compensation tx failure")
    monkeypatch.setattr(
        improve_loop_mod.missions_store, "finish_improve_mission",
        _boom_compensation)

    with pytest.raises(OSError, match="_materialize_workspace"):
        loop.prepare(slot_key=None, now=clock.now())


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
    # D-13 是正 (検収 R2): `rag=self._rag` (逸脱申告 (4)) の pin。
    # `rag=None` への変異は上記 2 assert では SURVIVED していた —
    # `_build_worker_runner` が `Rag(self._db_readonly_conn_factory)` を
    # 独自シグネチャで new せず、`ImproveLoop.__init__` が受け取った
    # `self._rag` をそのまま WorkerRunner へ渡すことを確認する。
    assert runner._rag is loop_full._rag
    # precheck 2026-08-27 Task13 Step0 是正 (R-D2 dead code): `_build_worker_runner`
    # は `ctx.rpc_handlers` を `WorkerRunner(rpc_handlers=...)` へ渡す必要が
    # ある — 省略すると WorkerRunner.dispatcher_loop は improve profile の
    # run_backtest/analyze_corr RPC を握り潰して self._rag への
    # AttributeError に化ける (実プロセス回帰ピン:
    # tests/runners/test_worker_runner.py::
    # test_worker_runner_dispatches_improve_tool_rpc_via_rpc_handlers_not_rag)。
    assert runner._rpc_handlers is ctx.rpc_handlers


def test_read_candidate_kind_defaults_to_indicator_when_key_omitted(
        loop_min, tmp_path):
    """A5(a) 是正 (束D検収, verified-local-round1.md §11 #13):
    `_read_candidate_kind` の既定値 `"indicator"` (config.yaml に `kind`
    キーが無いとき) を変える変異 (`indicator`→`strategy`) が実測 SURVIVED
    (全スイート 2924 passed) だった — 全 fixture が config.yaml に `kind`
    を明示指定しており、既定値パス自体が未踏だった。"""
    candidate_dir = tmp_path / "candidate"
    candidate_dir.mkdir()
    (candidate_dir / "config.yaml").write_text("pairs: [USDJPY]\n")  # kind 省略

    assert loop_min._read_candidate_kind(candidate_dir) == "indicator"


def test_read_candidate_pairs_defaults_to_empty_list_when_key_omitted(
        loop_min, tmp_path):
    """A5(b) 是正: `_read_candidate_pairs` の既定値 `[]` (実測 SURVIVED)。"""
    candidate_dir = tmp_path / "candidate"
    candidate_dir.mkdir()
    (candidate_dir / "config.yaml").write_text("kind: indicator\n")  # pairs 省略

    assert loop_min._read_candidate_pairs(candidate_dir) == []


def test_read_candidate_timeframe_defaults_to_1h_when_key_omitted(
        loop_min, tmp_path):
    """A5(c) 是正: `_read_candidate_timeframe` の既定値 `"1h"` (実測 SURVIVED)。"""
    candidate_dir = tmp_path / "candidate"
    candidate_dir.mkdir()
    (candidate_dir / "config.yaml").write_text("kind: indicator\n")  # timeframe 省略

    assert loop_min._read_candidate_timeframe(candidate_dir) == "1h"


# --- round2 D2 是正 (検収 acceptance-round2.md D2): 本番
# `ImproveLoop.compensate_launch_failure` を直接叩く killer -------------
#
# D2 の指摘: `test_runner_run_exception_does_not_burn_capacity`
# (tests/core/test_improve_wave_slot_protocol.py) は `_Round2FakeImproveLoop.
# compensate_launch_failure` (テスト内で終端化を再実装した fake) しか叩か
# ず、本番 `ImproveLoop.compensate_launch_failure` の本体 (新規 write conn
# の取得 / `_compensate_prepare_failure` への委譲 / `_conn_for_test` シーム
# での close) には一度も到達していなかった。ここでは `loop_no_seam`
# (write/readonly とも「毎回 db_path へ新規接続」— 本番 service.py と同型)
# を使い、①本番メソッドを直接呼ぶ ②結果は呼び出しに使った conn ではなく
# **別の readonly 接続**で読み直す (同一 conn で読むと「commit 済み」と
# 「同一 tx 内で書いただけ」を区別できない — D-2b の教訓と同型) ことで
# 本体を pin する。

def test_compensate_launch_failure_terminalizes_mission_run_and_slot_via_fresh_conn(
        loop_no_seam):
    """本番 `ImproveLoop.compensate_launch_failure` を実 DB 上で直接呼び、
    mission/run/slot が failed 終端になったことを**別接続**で確認する。
    本体を `return` に置換する変異 (D2) は、mission/run が非終端
    (`status='running'`/`finished_at IS NULL`) のまま・slot が `claimed`
    のままになるため red になる。"""
    loop, db_path = loop_no_seam
    now = datetime(2026, 8, 22, 12, 0)
    later = datetime(2026, 8, 22, 12, 5)

    seed = connect(db_path)
    try:
        improve_waves.create_wave_and_slots(
            seed, period_key="2026-W34", now=now, expected=1, commit=False)
        mission_id = missions_store.start(
            seed, "improve", "local", "m", now=now, commit=False)
        run_id = improve_runs_store.start(
            seed, backlog_id=None, mission_id=mission_id, now=now,
            commit=False)
        assert improve_waves.claim_slot(
            seed, period_key="2026-W34", k=0, mission_id=mission_id,
            now=now, commit=False)
        seed.commit()
    finally:
        seed.close()

    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"run_backtest": 600.0})
    ctx = ImproveRunContext(
        mission_id=mission_id, run_id=run_id,
        staging_dir=Path("/tmp/staging"), source_snapshot_dir=Path("/tmp/src"),
        allowed_backlog_ids=None, slot_key=("2026-W34", 0), ledger=ledger,
        rpc_handlers={})

    loop.compensate_launch_failure(ctx=ctx, now=later)

    fresh = connect_readonly(db_path)
    try:
        m = fresh.execute(
            "SELECT status, finished_at FROM missions WHERE id=?",
            (mission_id,)).fetchone()
        assert m["status"] == "failed"
        assert m["finished_at"] is not None
        r = fresh.execute(
            "SELECT finished_at FROM improvement_runs WHERE id=?",
            (run_id,)).fetchone()
        assert r["finished_at"] is not None
        slot = fresh.execute(
            "SELECT status FROM improve_wave_slots "
            "WHERE wave_period_key='2026-W34' AND k=0").fetchone()
        assert slot["status"] != "claimed"
    finally:
        fresh.close()


