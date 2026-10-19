"""候補台帳への最終 artifact の行の作成 (提出と提出に至らない終端)、gate の
最終 hash、全終端の写像、launch 失敗の補償。

E2E インフラは `tests/loops/test_improve_e2e.py` (FakeImproveWorkerRunner /
improve_env / fake の in_sample / holdout) をそのまま再利用する。"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from agentic_fx.backtest import holdout
from agentic_fx.core.contracts import FixedClock
from agentic_fx.core.improve_supervisor import ImproveSupervisor
from agentic_fx.loops.improve_loop import ImproveLoop
from agentic_fx.plugin.gate_pytest import GateResult, hashes_of
from agentic_fx.plugin.sandbox import SandboxError
from agentic_fx.runners.base import MissionResult
from agentic_fx.store import backlog as backlog_store
from agentic_fx.store import candidate_archives as archives
from agentic_fx.store import improve_waves
from agentic_fx.store.db import connect, connect_readonly

from tests.loops.test_improve_e2e import (
    NOW,
    FakeImproveWorkerRunner,
    _FAILING_INDICATOR_TEST,
    _PASSING_INDICATOR_CONFIG,
    _PASSING_INDICATOR_PY,
    _PASSING_INDICATOR_TEST,
    _PASSING_STRATEGY_CONFIG,
    _PASSING_STRATEGY_PY,
    _PASSING_STRATEGY_TEST,
    _fake_holdout_metrics,
    _fake_in_sample_metrics,
    _plugin_artifact,
    _report_artifact,
    _write_staging_plugin,
    improve_env,
)


def _loop(app, root) -> ImproveLoop:
    return ImproveLoop(
        root=root, settings=app.settings, clock=FixedClock(NOW),
        db_write_conn_factory=lambda: connect(root / "data" / "agentic.db"),
        db_readonly_conn_factory=lambda: connect_readonly(
            root / "data" / "agentic.db"),
        activity=app.activity, rag=app.rag)


def _seed(conn, mission_id) -> list[int]:
    """run_backtest が作る行を模した、成績つきの 2 行。"""
    return [archives.insert(
        conn, mission_id=mission_id, name=f"seed_{tag}",
        content_hash=f"c-{tag}", artifact_hash=f"h-{tag}",
        archive_path=None, pair="USDJPY",
        metrics={"pf": 1.1, "trades": 40}, now=NOW, commit=True)
        for tag in ("a", "b")]


def _rows(conn, mission_id):
    return archives.list_by_mission(conn, mission_id)


def _assert_terminal(conn, ctx):
    status = conn.execute("SELECT status FROM missions WHERE id=?",
                          (ctx.mission_id,)).fetchone()[0]
    assert status != "running"
    assert not ctx.staging_dir.exists()


def _expect_rows(conn, ctx, seeded, final_outcome):
    """seed 以外の行が最終行 1 本 (または 0 本) で、最終行以外は not_submitted。"""
    rows = _rows(conn, ctx.mission_id)
    by_id = {r["id"]: r for r in rows}
    for seed_id in seeded:
        assert by_id[seed_id]["outcome_class"] == "not_submitted"
    extra = [r for r in rows if r["id"] not in seeded]
    if final_outcome is None:
        assert extra == []
        return None
    assert len(extra) == 1
    assert extra[0]["outcome_class"] == final_outcome
    return extra[0]


def _run(app, root, output, *, files=None, status="completed",
         in_sample=None, holdout_fn=None, extra=(), before_commit=None,
         result_kwargs=None, seed=True):
    """prepare → (行の seed、staging の配置) → commit。ctx と seed の id を返す。"""
    loop = _loop(app, root)
    result = MissionResult(status=status, output=output, transcript=[],
                           **(result_kwargs or {}))
    patches = [patch("agentic_fx.runners.worker_runner.WorkerRunner",
                     lambda **kw: FakeImproveWorkerRunner(result=result, **kw))]
    if in_sample is not None:
        patches.append(patch(
            "agentic_fx.loops.improve_loop.holdout.run_in_sample", in_sample))
    if holdout_fn is not None:
        patches.append(patch(
            "agentic_fx.loops.improve_loop.holdout.run_holdout_gate",
            holdout_fn))
    patches.extend(extra)
    for p in patches:
        p.start()
    try:
        mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
        seeded = _seed(app.conn_core, ctx.mission_id) if seed else []
        if files is not None:
            name, plugin_py, config, test_py = files
            _write_staging_plugin(ctx.staging_dir, name, plugin_py, config,
                                  test_py)
        if before_commit is not None:
            before_commit(loop, ctx)
        loop.commit(mission=mission, ctx=ctx, result=worker.run(mission),
                    now=NOW)
    finally:
        for p in reversed(patches):
            p.stop()
    return loop, ctx, seeded


_STRATEGY_FILES = ("cand", _PASSING_STRATEGY_PY, _PASSING_STRATEGY_CONFIG,
                   _PASSING_STRATEGY_TEST)
_INDICATOR_FILES = ("cand", _PASSING_INDICATOR_PY, _PASSING_INDICATOR_CONFIG,
                    _PASSING_INDICATOR_TEST)


def _strategy(name="cand"):
    return (name,) + _STRATEGY_FILES[1:]


def _indicator(name="cand"):
    return (name,) + _INDICATOR_FILES[1:]


def _payload(conn):
    return json.loads(conn.execute(
        "SELECT payload_json FROM approval_requests WHERE kind='plugin' "
        "ORDER BY id DESC").fetchone()[0])


# --- AC-22 -----------------------------------------------------------------

def test_ac22_i_rewritten_selftest_creates_new_row_and_payload_points_to_it(
        improve_env):
    """run_backtest 時の artifact_hash の行 (seed) と提出時の最終 bytes の
    hash が異なるとき、新しい行が作られ payload の candidate_archive_id が
    その行を指す。"""
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, seeded = _run(
        app, root, _plugin_artifact("cand", kind="strategy"),
        files=_strategy(), in_sample=_fake_in_sample_metrics(40),
        holdout_fn=_fake_holdout_metrics())
    extra = _expect_rows(conn, ctx, seeded, "pending")
    payload = _payload(conn)
    assert payload["candidate_archive_id"] == extra["id"]
    assert extra["artifact_hash"] == payload["artifact_hash"]
    assert extra["artifact_hash"] not in ("h-a", "h-b")
    assert extra["metrics"] == {}
    assert extra["pair"] == "USDJPY"
    assert extra["archive_path"] is not None
    assert (root / extra["archive_path"] / "plugin.py").is_file()
    assert extra["logic_hash"].startswith("v1:")
    assert extra["family_key"] is None  # indicator 依存の無い戦略


def test_ac22_ii_unchanged_submission_reuses_existing_row(improve_env):
    app, root = improve_env
    conn = app.conn_core
    seeded_box: list[int] = []

    def seed_exact(loop, ctx):
        _, artifact_hash = hashes_of(ctx.staging_dir / "cand")
        seeded_box.append(archives.insert(
            conn, mission_id=ctx.mission_id, name="cand",
            content_hash="c-exact", artifact_hash=artifact_hash,
            archive_path="plugins/_archive/x/exact", pair="USDJPY",
            metrics={"pf": 1.4, "trades": 40}, now=NOW, commit=True))

    loop, ctx, seeded = _run(
        app, root, _plugin_artifact("cand", kind="strategy"),
        files=_strategy(), in_sample=_fake_in_sample_metrics(40),
        holdout_fn=_fake_holdout_metrics(), before_commit=seed_exact)
    rows = _rows(conn, ctx.mission_id)
    assert len(rows) == 3  # seed 2 + 事前に作った 1 (増えない)
    reused = [r for r in rows if r["id"] == seeded_box[0]][0]
    assert reused["outcome_class"] == "pending"
    assert _payload(conn)["candidate_archive_id"] == seeded_box[0]


def test_ac22_vi_indicator_submission_has_null_pair_and_points_payload(
        improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, seeded = _run(
        app, root, _plugin_artifact("cand", kind="indicator"),
        files=_indicator())
    extra = _expect_rows(conn, ctx, seeded, "pending")
    assert extra["pair"] is None
    assert extra["family_key"] == "indicator:cand@1h"
    assert _payload(conn)["candidate_archive_id"] == extra["id"]


def _rewriting_gate(new_source, *, passed=False, stdout="boom",
                    delete_test=False):
    def fake(plugin_dir, *, settings):
        (plugin_dir / "plugin.py").write_text(new_source, encoding="utf-8")
        if delete_test:
            (plugin_dir / "test_plugin.py").unlink()
        return GateResult(passed=passed, returncode=0 if passed else 1,
                          stdout_tail=stdout, duration_sec=0.0)
    return fake


_REWRITTEN = 'def compute(df, params):\n    return {"value": 123}\n'


def test_ac22_iii_v_selftest_rewrites_plugin_then_pytest_fails(improve_env):
    """self-test が plugin.py を書き換えてから pytest が失敗する候補。
    終端の最終行の artifact_hash は書き換え後の staging の bytes の hash で、
    gate 開始時に取った値とは異なる。"""
    app, root = improve_env
    conn = app.conn_core
    before: list[str] = []

    def remember(loop, ctx):
        before.append(hashes_of(ctx.staging_dir / "cand")[1])

    loop, ctx, seeded = _run(
        app, root, _plugin_artifact("cand"), files=_indicator(),
        before_commit=remember,
        extra=[patch("agentic_fx.loops.improve_loop.run_gate_pytest",
                     _rewriting_gate(_REWRITTEN))])
    extra = _expect_rows(conn, ctx, seeded, "gate_failed")
    assert extra["archive_path"] is None
    assert extra["artifact_hash"] != before[0]
    from agentic_fx.plugin.version_store import artifact_hash_bytes
    rewritten_hash = artifact_hash_bytes(
        _REWRITTEN.encode(), _PASSING_INDICATOR_CONFIG.encode(),
        _PASSING_INDICATOR_TEST.encode())
    assert extra["artifact_hash"] == rewritten_hash
    assert conn.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0] == 0


def test_ac22_v_hash_unavailable_after_pytest_creates_no_row(improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, seeded = _run(
        app, root, _plugin_artifact("cand"), files=_indicator(),
        extra=[patch("agentic_fx.loops.improve_loop.run_gate_pytest",
                     _rewriting_gate(_REWRITTEN, delete_test=True))])
    _expect_rows(conn, ctx, seeded, None)
    backlog = conn.execute(
        "SELECT last_result FROM improvement_backlog ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    assert backlog.startswith("gate_failed:")  # 候補の判定は変わらない


def _gate_dir(tmp_path, *, test_py=_PASSING_INDICATOR_TEST,
              plugin_py=_PASSING_INDICATOR_PY):
    directory = tmp_path / "gate" / "cand"
    directory.mkdir(parents=True)
    (directory / "plugin.py").write_text(plugin_py, encoding="utf-8")
    (directory / "config.yaml").write_text(_PASSING_INDICATOR_CONFIG,
                                           encoding="utf-8")
    (directory / "test_plugin.py").write_text(test_py, encoding="utf-8")
    (tmp_path / "snap").mkdir()
    return directory


@pytest.mark.parametrize("scenario", [
    "pytest_failed", "runtime_error", "too_thin", "hash_changed_on_pass"])
def test_ac22_v_post_pytest_returns_carry_the_final_hash(
        improve_env, tmp_path, monkeypatch, scenario):
    app, root = improve_env
    loop = _loop(app, root)
    directory = _gate_dir(tmp_path)
    before = hashes_of(directory)

    def fake(plugin_dir, *, settings):
        (plugin_dir / "plugin.py").write_text(_REWRITTEN, encoding="utf-8")
        if scenario == "runtime_error":
            raise RuntimeError("landlock unavailable")
        if scenario == "pytest_failed":
            return GateResult(False, 1, "boom", 0.0)
        # 3 本以上通った体裁にして、minimum 未満 / hash 変化の分岐へ進める
        tail = "1 passed in 0.1s" if scenario == "too_thin" else "99 passed in 0.1s"
        return GateResult(True, 0, tail, 0.0)

    monkeypatch.setattr("agentic_fx.loops.improve_loop.run_gate_pytest", fake)
    verdict = loop._run_plugin_gate(
        directory, name="cand", source_snapshot_dir=tmp_path / "snap")
    assert verdict.passed is False
    assert verdict.failure_kind == "gate_failed"
    assert (verdict.content_hash, verdict.artifact_hash) == hashes_of(directory)
    assert verdict.artifact_hash != before[1]


def test_ac22_v_pre_pytest_returns_carry_the_hash_taken_before(
        improve_env, tmp_path, monkeypatch):
    app, root = improve_env
    loop = _loop(app, root)
    # self_test_missing
    directory = _gate_dir(tmp_path, test_py="x = 1\n")
    verdict = loop._run_plugin_gate(
        directory, name="cand", source_snapshot_dir=tmp_path / "snap")
    assert verdict.reason == "self_test_missing"
    assert verdict.failure_kind == "gate_failed"
    assert (verdict.content_hash, verdict.artifact_hash) == hashes_of(directory)
    # noop_copy は種別を持つ
    other = tmp_path / "other"
    other.mkdir()
    directory2 = _gate_dir(other)
    monkeypatch.setattr(
        "agentic_fx.loops.improve_loop.find_noop_copy",
        lambda *a, **k: "_examples/rsi")
    verdict = loop._run_plugin_gate(
        directory2, name="cand", source_snapshot_dir=other / "snap")
    assert verdict.failure_kind == "noop_copy"
    assert verdict.reason.startswith("noop_copy_of:")
    assert (verdict.content_hash, verdict.artifact_hash) == hashes_of(directory2)


def test_ac22_vii_indicator_family_key_has_no_pair_and_does_not_mix_with_strategy():
    from agentic_fx.plugin.logic_identity import family_key_of
    indicator = family_key_of(
        "kind: indicator\ntimeframe: 4h\npairs: [USDJPY]\noutputs: [v]\n",
        name="rsi")
    assert indicator == "indicator:rsi@4h"
    strategy = family_key_of(
        "kind: strategy\ntimeframe: 4h\npairs: [USDJPY]\nindicators:\n"
        "  a: {plugin: rsi}\n", name="rsi")
    assert strategy == "strategy:rsi@4h@USDJPY"
    assert indicator != strategy


# --- AC-23: 全終端 ---------------------------------------------------------

def test_ac23_e1_failed_mission(improve_env):
    app, root = improve_env
    loop, ctx, seeded = _run(app, root, None, status="failed")
    _expect_rows(app.conn_core, ctx, seeded, None)
    _assert_terminal(app.conn_core, ctx)


def test_ac23_e2_output_invalid(improve_env):
    app, root = improve_env
    loop, ctx, seeded = _run(app, root, {"bogus": 1})
    _expect_rows(app.conn_core, ctx, seeded, None)
    _assert_terminal(app.conn_core, ctx)


def test_ac23_e3_loser(improve_env):
    app, root = improve_env
    conn = app.conn_core
    backlog_id = backlog_store.add(conn, "shared idea", "user", NOW)
    output = _plugin_artifact("cand")
    output["selected"] = {"backlog_id": backlog_id, "idea": "shared idea"}

    def take(loop, ctx):
        conn.execute("UPDATE improvement_backlog SET status='selected' "
                     "WHERE id=?", (backlog_id,))
        conn.commit()

    loop, ctx, seeded = _run(app, root, output, files=_indicator(),
                             before_commit=take)
    _expect_rows(conn, ctx, seeded, None)
    _assert_terminal(conn, ctx)


def test_ac23_e4_recovered_report_demoted(improve_env):
    app, root = improve_env
    loop, ctx, seeded = _run(
        app, root, _report_artifact("core"),
        result_kwargs={"recovered": True})
    _expect_rows(app.conn_core, ctx, seeded, None)
    _assert_terminal(app.conn_core, ctx)


def test_ac23_e5_observation_artifact(improve_env):
    app, root = improve_env
    output = {"discoveries": [], "selected": {"backlog_id": None,
                                              "idea": "an idea"},
              "artifact": {"type": "observation", "reason": "nothing found"},
              "selection_rationale": "r"}
    loop, ctx, seeded = _run(app, root, output)
    _expect_rows(app.conn_core, ctx, seeded, None)
    _assert_terminal(app.conn_core, ctx)


def test_ac23_e6_report_write_failure_with_no_candidate(improve_env):
    app, root = improve_env

    def plant_symlink(loop, ctx):
        tmp_dir = root / "data" / "improve_reports" / ".tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        target = tmp_dir / "evil.md"
        target.write_text("x", encoding="utf-8")
        (tmp_dir / f"improve-{ctx.mission_id}.md.part").symlink_to(target)

    loop, ctx, seeded = _run(
        app, root, _report_artifact("core"), before_commit=plant_symlink)
    _expect_rows(app.conn_core, ctx, seeded, None)
    assert app.conn_core.execute(
        "SELECT report_state FROM improvement_runs WHERE id=?",
        (ctx.run_id,)).fetchone()[0] == "failed"


def test_ac23_e7_noop_copy(improve_env):
    app, root = improve_env
    loop, ctx, seeded = _run(
        app, root, _plugin_artifact("cand"), files=_indicator(),
        extra=[patch("agentic_fx.loops.improve_loop.find_noop_copy",
                     lambda *a, **k: "_examples/rsi")])
    extra = _expect_rows(app.conn_core, ctx, seeded, "noop_copy")
    assert extra["archive_path"] is None
    _assert_terminal(app.conn_core, ctx)


def test_ac23_e8_other_gate_failure(improve_env):
    app, root = improve_env
    loop, ctx, seeded = _run(
        app, root, _plugin_artifact("cand"),
        files=("cand", _PASSING_INDICATOR_PY, _PASSING_INDICATOR_CONFIG,
               _FAILING_INDICATOR_TEST))
    extra = _expect_rows(app.conn_core, ctx, seeded, "gate_failed")
    assert extra["archive_path"] is None
    _assert_terminal(app.conn_core, ctx)


def test_ac23_e8_snapshot_check_failure_has_no_final_row(improve_env):
    app, root = improve_env

    def grow(loop, ctx):
        # 提出物の中に想定外の入れ子ディレクトリを置いて snapshot 検査を落とす
        (ctx.staging_dir / "cand" / "nested").mkdir()

    loop, ctx, seeded = _run(
        app, root, _plugin_artifact("cand"), files=_indicator(),
        before_commit=grow)
    backlog = app.conn_core.execute(
        "SELECT last_result FROM improvement_backlog ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    assert backlog.startswith("gate_failed:")
    _expect_rows(app.conn_core, ctx, seeded, None)


def test_ac23_e9_oversized_max_bars(improve_env):
    app, root = improve_env
    loop, ctx, seeded = _run(
        app, root, _plugin_artifact("cand", kind="strategy"),
        files=("cand", _PASSING_STRATEGY_PY,
               _PASSING_STRATEGY_CONFIG.replace(
                   "params: {}", "max_bars: 999999\nparams: {}"),
               _PASSING_STRATEGY_TEST))
    _expect_rows(app.conn_core, ctx, seeded, "gate_failed")


def test_ac23_e10_no_history_and_unconfigured_source(improve_env):
    app, root = improve_env

    def no_history(settings, *, history_conn, record_fn, **kwargs):
        raise holdout.NoHistoryError("no bars")

    loop, ctx, seeded = _run(
        app, root, _plugin_artifact("cand", kind="strategy"),
        files=_strategy(), in_sample=no_history)
    _expect_rows(app.conn_core, ctx, seeded, "gate_failed")


def test_ac23_e10_history_source_unconfigured(improve_env):
    from agentic_fx.plugin.strategy_gate import StrategyGateVerdict
    app, root = improve_env
    verdict = StrategyGateVerdict(
        evaluable=False, observation_reason="history_source_unconfigured",
        unevaluable_kind="history_source_unconfigured")
    loop, ctx, seeded = _run(
        app, root, _plugin_artifact("cand", kind="strategy"),
        files=_strategy(),
        extra=[patch.object(ImproveLoop, "_run_strategy_gate",
                            lambda self, *a, **k: verdict)])
    _expect_rows(app.conn_core, ctx, seeded, "gate_failed")


def test_ac23_e11_insufficient_trades(improve_env):
    app, root = improve_env
    loop, ctx, seeded = _run(
        app, root, _plugin_artifact("cand", kind="strategy"),
        files=_strategy(), in_sample=_fake_in_sample_metrics(29))
    extra = _expect_rows(app.conn_core, ctx, seeded, "insufficient_trades")
    assert extra["archive_path"] is None


def test_ac23_e12_floor_unprofitable(improve_env):
    app, root = improve_env
    loop, ctx, seeded = _run(
        app, root, _plugin_artifact("cand", kind="strategy"),
        files=_strategy(),
        in_sample=_fake_in_sample_metrics(40, pf=0.3, avg_r=-0.1),
        holdout_fn=_fake_holdout_metrics())
    _expect_rows(app.conn_core, ctx, seeded, "unprofitable")


def test_ac23_e12_report_write_failure_keeps_candidate_outcome(improve_env):
    """report_failed の終端でも候補の outcome は mission_outcome に
    引きずられない。"""
    app, root = improve_env

    def plant_symlink(loop, ctx):
        tmp_dir = root / "data" / "improve_reports" / ".tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        target = tmp_dir / "evil.md"
        target.write_text("x", encoding="utf-8")
        (tmp_dir / f"improve-{ctx.mission_id}.md.part").symlink_to(target)

    loop, ctx, seeded = _run(
        app, root, _plugin_artifact("cand", kind="strategy"),
        files=_strategy(),
        in_sample=_fake_in_sample_metrics(29), before_commit=plant_symlink)
    _expect_rows(app.conn_core, ctx, seeded, "insufficient_trades")
    assert app.conn_core.execute(
        "SELECT report_state FROM improvement_runs WHERE id=?",
        (ctx.run_id,)).fetchone()[0] == "failed"


def test_ac23_e13_sandbox_unavailable(improve_env):
    app, root = improve_env

    def unavailable(self, *a, **k):
        raise SandboxError("x", code="sandbox_unavailable",
                           sandbox_reason="host_preflight")

    loop, ctx, seeded = _run(
        app, root, _plugin_artifact("cand", kind="strategy"),
        files=_strategy(),
        extra=[patch.object(ImproveLoop, "_run_strategy_gate", unavailable)])
    _expect_rows(app.conn_core, ctx, seeded, None)
    assert app.conn_core.execute(
        "SELECT status FROM improvement_backlog ORDER BY id DESC LIMIT 1"
    ).fetchone()[0] == "open"


def test_ac23_e14_e15_duplicate_metrics_demotion_and_submission(improve_env):
    app, root = improve_env
    conn = app.conn_core
    metrics = {"trades": 194, "pf": 1.4981, "win_rate": 0.487, "avg_r": 0.188,
               "max_drawdown": 0.0373, "total_pnl": 100.0}
    hold = {"trades": 53, "pf": 1.1, "win_rate": 0.3, "avg_r": 0.037,
            "max_drawdown": 0.0392, "total_pnl": 10.0}
    from tests.loops.test_improve_loop_duplicate_metrics import (
        _fake_holdout_full_metrics, _fake_in_sample_full_metrics)
    loop, ctx_a, seeded_a = _run(
        app, root, _plugin_artifact("conv_a", kind="strategy"),
        files=("conv_a", '"""a"""\n' + _PASSING_STRATEGY_PY,
               _PASSING_STRATEGY_CONFIG, _PASSING_STRATEGY_TEST),
        in_sample=_fake_in_sample_full_metrics(metrics),
        holdout_fn=_fake_holdout_full_metrics(hold))
    first = _expect_rows(conn, ctx_a, seeded_a, "pending")  # E15
    assert _payload(conn)["candidate_archive_id"] == first["id"]
    loop, ctx_b, seeded_b = _run(
        app, root, _plugin_artifact("conv_b", kind="strategy"),
        files=("conv_b", '"""b"""\n' + _PASSING_STRATEGY_PY,
               _PASSING_STRATEGY_CONFIG, _PASSING_STRATEGY_TEST),
        in_sample=_fake_in_sample_full_metrics(metrics),
        holdout_fn=_fake_holdout_full_metrics(hold))
    extra = _expect_rows(conn, ctx_b, seeded_b, "duplicate_metrics")  # E14
    assert extra["archive_path"] is None
    assert conn.execute(
        "SELECT COUNT(*) FROM approval_requests").fetchone()[0] == 1
    _assert_terminal(conn, ctx_b)


def test_ac23_e16_finalize_success_exception(improve_env):
    app, root = improve_env

    def boom(*a, **k):
        raise RuntimeError("injected")

    loop, ctx, seeded = _run(
        app, root, _plugin_artifact("cand"), files=_indicator(),
        extra=[patch("agentic_fx.loops.improve_loop.approvals_store.create",
                     boom)])
    _expect_rows(app.conn_core, ctx, seeded, None)
    assert app.conn_core.execute(
        "SELECT COUNT(*) FROM approval_requests").fetchone()[0] == 0
    assert app.conn_core.execute(
        "SELECT status FROM missions WHERE id=?",
        (ctx.mission_id,)).fetchone()[0] == "failed"


def test_ac23_e15_settle_cas_failure_persists_no_approval(improve_env):
    """提出する行が not_submitted でなければ pending へ進められない。承認を
    書かずに Tx-2 全体を戻し、失敗の終端に落とす。"""
    app, root = improve_env
    original = ImproveLoop._ensure_final_archive_row

    def already_decided(self, conn, **kwargs):
        archive_id = original(self, conn, **kwargs)
        conn.execute("UPDATE candidate_archives SET outcome_class="
                     "'gate_failed' WHERE id=?", (archive_id,))
        return archive_id

    loop, ctx, seeded = _run(
        app, root, _plugin_artifact("cand", kind="strategy"),
        files=_strategy(), in_sample=_fake_in_sample_metrics(40),
        holdout_fn=_fake_holdout_metrics(),
        extra=[patch.object(ImproveLoop, "_ensure_final_archive_row",
                            already_decided)])
    conn = app.conn_core
    assert conn.execute(
        "SELECT COUNT(*) FROM approval_requests").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM missions WHERE id=?",
                        (ctx.mission_id,)).fetchone()[0] == "failed"
    _expect_rows(conn, ctx, seeded, None)


def test_ac23_e17_commit_exception_then_compensation(improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop = _loop(app, root)
    result = MissionResult(status="completed", output=_plugin_artifact("cand"),
                           transcript=[])
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(result=result, **kw)):
        mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
        seeded = _seed(conn, ctx.mission_id)
        mission_result = worker.run(mission)

        def boom(*a, **k):
            raise RuntimeError("injected")

        loop._inspect_output = boom
        with pytest.raises(RuntimeError):
            loop.commit(mission=mission, ctx=ctx, result=mission_result,
                        now=NOW)
        loop.compensate_commit_failure(ctx=ctx, now=NOW,
                                       exc=RuntimeError("injected"))
    _expect_rows(conn, ctx, seeded, None)
    _assert_terminal(conn, ctx)


def test_ac23_e18a_prepare_failure_leaves_no_rows(improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop = _loop(app, root)

    def boom(**kw):
        raise RuntimeError("injected")

    with patch("agentic_fx.runners.worker_runner.WorkerRunner", boom):
        with pytest.raises(RuntimeError):
            loop.prepare(slot_key=None, now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM candidate_archives"
                        ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM missions WHERE status='running'"
    ).fetchone()[0] == 0


class _RaisingRunner:
    def __init__(self, **kw):
        self.kwargs = kw

    def run(self, mission):
        raise OSError("ENOSPC injected")


def _supervisor(app, root, loop) -> ImproveSupervisor:
    sup = ImproveSupervisor(
        capacity=1, root=root, settings=app.settings,
        clock=FixedClock(NOW), db_path=root / "data" / "agentic.db",
        stop_event=threading.Event(), activity=app.activity)
    sup._improve_loop = loop
    return sup


def _running_missions(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM missions WHERE status='running'").fetchone()[0]


def test_ac23_e18b_scheduled_slot_launch_failure(improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop = _loop(app, root)
    sup = _supervisor(app, root, loop)
    backlog_store.add(conn, "a pending idea", "user", NOW)
    improve_waves.create_wave_and_slots(
        conn, period_key="2026-W34", now=NOW, expected=1, commit=True)
    with patch("agentic_fx.runners.worker_runner.WorkerRunner", _RaisingRunner):
        with pytest.raises(OSError):
            sup._launch_slot("2026-W34", 0)
    assert conn.execute("SELECT COUNT(*) FROM candidate_archives"
                        ).fetchone()[0] == 0
    assert _running_missions(conn) == 0
    assert conn.execute(
        "SELECT status FROM improve_wave_slots WHERE wave_period_key="
        "'2026-W34' AND k=0").fetchone()[0] == "failed"
    assert list((root / "plugins" / "_staging").glob("*")) == []


def test_ac23_e18c_manual_launch_failure_terminalizes_mission_and_run(
        improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop = _loop(app, root)
    sup = _supervisor(app, root, loop)
    with patch("agentic_fx.runners.worker_runner.WorkerRunner", _RaisingRunner):
        with pytest.raises(OSError):
            sup.submit_manual()
    assert conn.execute("SELECT COUNT(*) FROM candidate_archives"
                        ).fetchone()[0] == 0
    assert _running_missions(conn) == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM improvement_runs WHERE finished_at IS NULL"
    ).fetchone()[0] == 0
    staging = root / "plugins" / "_staging"
    assert not staging.exists() or list(staging.glob("*")) == []
