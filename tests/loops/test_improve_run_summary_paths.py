"""終端の各経路で、要約の行がちょうど 1 行、経路の表どおりの項目だけで保存される。

足場は `test_improve_notice_paths` と同じ駆動ヘルパ (prepare → staging 配置 →
実際の `commit()` / 補償) を使う。"""
from __future__ import annotations

import json
import sqlite3
from unittest.mock import patch

import pytest

from agentic_fx import improve_notice_codes as codes
from agentic_fx.backtest import holdout
from agentic_fx.loops import improve_run_summary as run_summary
from agentic_fx.loops.improve_loop import ImproveLoop
from agentic_fx.plugin.sandbox import SandboxError
from agentic_fx.runners.base import MissionResult
from agentic_fx.store import backlog as backlog_store
from agentic_fx.store import candidate_archives as archives

from tests.loops.test_improve_candidate_ledger import (
    _indicator,
    _loop,
    _plugin_artifact,
    _report_artifact,
    _run,
    _strategy,
)
from tests.loops.test_improve_e2e import (
    NOW,
    FakeImproveWorkerRunner,
    _fake_holdout_metrics,
    _fake_in_sample_metrics,
    improve_env,  # noqa: F401
)
from tests.loops.test_improve_notice_paths import (
    TOKEN,
    _crash_commit_then_compensate,
    _observation_output,
    _report_symlink,
)
from tests.loops.test_improve_quality_gate import (
    FAMILY,
    METRICS_2,
    PY_X,
    _decided_row,
    _family_patch,
    _hash_of,
    _run_strategy,
)


def _summary(conn, mission_id):
    """(state, items) を返す。行が 1 行だけであることも確かめる。"""
    rows = conn.execute(
        "SELECT * FROM improve_run_summaries WHERE mission_id=?",
        (mission_id,)).fetchall()
    assert len(rows) == 1, f"summary rows for {mission_id}: {len(rows)}"
    row = rows[0]
    assert row["summary_version"] == 1
    items = json.loads(row["summary_json"])
    if row["state"] == "ok":
        import jsonschema
        jsonschema.validate(items, run_summary.ITEMS_SCHEMA)
    else:
        assert items == {}
    return row["state"], items


def _output_json(conn, mission_id):
    row = conn.execute("SELECT output_json, transcript_json FROM missions "
                       "WHERE id=?", (mission_id,)).fetchone()
    assert row["transcript_json"] == "[]"
    return None if row["output_json"] is None else json.loads(row["output_json"])


def _keys(items):
    return set(items)


def _unprofitable(app, root, **kwargs):
    kwargs.setdefault("extra", [_family_patch(FAMILY)])
    return _run(
        app, root, _plugin_artifact("cand", kind="strategy"),
        files=_strategy(),
        in_sample=_fake_in_sample_metrics(40, pf=0.3, avg_r=-0.1),
        holdout_fn=_fake_holdout_metrics(), **kwargs)


# --- 提出・観察・報告 ---------------------------------------------------------

def test_submitted_strategy_carries_every_item(improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, _ = _run(
        app, root, _plugin_artifact("cand", kind="strategy"),
        files=_strategy(), in_sample=_fake_in_sample_metrics(40),
        holdout_fn=_fake_holdout_metrics(), extra=[_family_patch(FAMILY)])
    state, items = _summary(conn, ctx.mission_id)
    assert state == "ok"
    assert _keys(items) == {"hypothesis", "explanation", "tried", "floor",
                            "handoff"}
    assert items["hypothesis"] == {"idea": "idea for cand", "backlog_id": None}
    assert items["explanation"] == {"rationale": "test rationale",
                                    "artifact_summary": "cand candidate"}
    assert items["tried"] == {"plugin": "cand", "kind": "strategy",
                              "origin": "new", "indicators": []}
    floor = items["floor"]
    assert floor["failed_stage"] is None and floor["failed_pair_count"] == 0
    assert [(s["stage"], s["state"]) for s in floor["stages"]] == [
        ("in_sample", "measured"), ("holdout", "measured")]
    assert floor["stages"][0]["pairs"][0]["pf"] == 1.5
    handoff = items["handoff"]
    assert handoff["attempts"] >= 1 and handoff["outcomes"].get("pending") == 1
    assert "observation" not in items and "parent_reason" not in items
    assert _output_json(conn, ctx.mission_id)["selection_rationale"] \
        == "test rationale"


def test_submitted_indicator_is_marked_new_without_floor(improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, _ = _run(app, root, _plugin_artifact("cand"), files=_indicator())
    state, items = _summary(conn, ctx.mission_id)
    assert state == "ok"
    assert items["tried"] == {"plugin": "cand", "kind": "indicator",
                              "origin": "new", "indicators": []}
    assert "floor" not in items and "comparison" not in items


def test_observation_keeps_hypothesis_explanation_and_the_masked_text(
        improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, _ = _run(app, root, _observation_output("nothing found"))
    state, items = _summary(conn, ctx.mission_id)
    assert (state, _keys(items)) == (
        "ok", {"hypothesis", "explanation", "observation"})
    assert items["observation"] == "nothing found"
    assert items["explanation"]["artifact_summary"] is None


@pytest.mark.parametrize("reason", ["duplicate of x", "family_redirect:abc"])
def test_masked_observation_has_no_text(improve_env, reason):
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, _ = _run(app, root, _observation_output(reason))
    _state, items = _summary(conn, ctx.mission_id)
    assert "observation" not in items
    assert reason not in json.dumps(items, ensure_ascii=False)


def test_agent_text_is_sanitized_before_it_is_stored(improve_env):
    app, root = improve_env
    conn = app.conn_core
    output = _observation_output(f"line1\nline2 {TOKEN} \x07 end")
    output["selection_rationale"] = f"why\n\n{TOKEN}  apikey=SECRET9 **bold**"
    output["selected"]["idea"] = "x" * 900
    loop, ctx, _ = _run(app, root, output)
    _state, items = _summary(conn, ctx.mission_id)
    stored = json.dumps(items, ensure_ascii=False)
    assert "SECRET9" not in stored and "leak.example" not in stored
    assert "\n" not in items["observation"] and "\x07" not in stored
    assert "\\n" not in stored
    assert len(items["hypothesis"]["idea"]) <= run_summary.TEXT_LIMIT + len(
        run_summary.TRUNCATION_SUFFIX)
    assert items["hypothesis"]["idea"].endswith(run_summary.TRUNCATION_SUFFIX)


def test_report_published_has_claims_only(improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, _ = _run(app, root, _report_artifact("core"))
    state, items = _summary(conn, ctx.mission_id)
    assert (state, _keys(items)) == ("ok", {"hypothesis", "explanation"})


def test_recovered_report_demotion_names_the_parents_reason(improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, _ = _run(app, root, _report_artifact("core"),
                        result_kwargs={"recovered": True})
    _state, items = _summary(conn, ctx.mission_id)
    assert items["parent_reason"] == "demoted_recovered"
    assert "observation" not in items
    assert _keys(items) == {"hypothesis", "explanation", "parent_reason"}


def test_report_write_failure_has_claims_only(improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, _ = _run(app, root, _report_artifact("core"),
                        before_commit=_report_symlink(root))
    state, items = _summary(conn, ctx.mission_id)
    assert (state, _keys(items)) == ("ok", {"hypothesis", "explanation"})


def test_loser_keeps_the_hypothesis_only(improve_env):
    app, root = improve_env
    conn = app.conn_core
    backlog_id = backlog_store.add(conn, "shared idea", "user", NOW)
    output = _plugin_artifact("cand")
    output["selected"] = {"backlog_id": backlog_id, "idea": "shared idea"}

    def take(loop, ctx):
        conn.execute("UPDATE improvement_backlog SET status='selected' "
                     "WHERE id=?", (backlog_id,))
        conn.commit()

    loop, ctx, _ = _run(app, root, output, files=_indicator(),
                        before_commit=take)
    state, items = _summary(conn, ctx.mission_id)
    assert (state, items) == ("ok", {"hypothesis": {
        "idea": "shared idea", "backlog_id": backlog_id}})


def test_loser_report_write_failure_keeps_the_hypothesis_only(improve_env):
    app, root = improve_env
    conn = app.conn_core
    backlog_id = backlog_store.add(conn, "shared idea", "user", NOW)
    output = _plugin_artifact("cand")
    output["selected"] = {"backlog_id": backlog_id, "idea": "shared idea"}
    plant = _report_symlink(root)

    def take_and_plant(loop, ctx):
        conn.execute("UPDATE improvement_backlog SET status='selected' "
                     "WHERE id=?", (backlog_id,))
        conn.commit()
        plant(loop, ctx)

    loop, ctx, _ = _run(app, root, output, files=_indicator(),
                        before_commit=take_and_plant)
    state, items = _summary(conn, ctx.mission_id)
    assert (state, _keys(items)) == ("ok", {"hypothesis"})


# --- runner の失敗・出力不正・sandbox -----------------------------------------

@pytest.mark.parametrize("status", ["failed", "timeout", "max_turns"])
def test_runner_failure_with_a_valid_output_keeps_claims(improve_env, status):
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, _ = _run(app, root, _plugin_artifact("cand"), status=status)
    state, items = _summary(conn, ctx.mission_id)
    assert (state, _keys(items)) == ("ok", {"hypothesis", "explanation"})


def test_runner_failure_without_output_is_empty_and_output_is_null(
        improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, _ = _run(app, root, None, status="failed")
    assert _summary(conn, ctx.mission_id) == ("empty", {})
    assert _output_json(conn, ctx.mission_id) is None


def test_runner_failure_with_a_schema_broken_output_has_no_claims(improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, _ = _run(app, root, {"selected": {"idea": "half"}},
                        status="failed")
    assert _summary(conn, ctx.mission_id) == ("empty", {})
    assert _output_json(conn, ctx.mission_id) == {"selected": {"idea": "half"}}


def test_output_invalid_is_empty_and_keeps_the_original_output(improve_env):
    app, root = improve_env
    conn = app.conn_core
    bad = _plugin_artifact("cand")
    bad["artifact"]["name"] = "Bad Name"
    loop, ctx, _ = _run(app, root, bad)
    assert conn.execute("SELECT reason_code FROM improve_run_notices "
                        "WHERE mission_id=?", (ctx.mission_id,)
                        ).fetchone()[0] == "output_invalid"
    assert _summary(conn, ctx.mission_id) == ("empty", {})
    assert _output_json(conn, ctx.mission_id) == bad


def test_sandbox_unavailable_has_claims_but_no_gate_result(improve_env):
    app, root = improve_env
    conn = app.conn_core

    def unavailable(self, *a, **k):
        raise SandboxError("x", code="sandbox_unavailable",
                           sandbox_reason="host_preflight")

    loop, ctx, _ = _run(
        app, root, _plugin_artifact("cand", kind="strategy"),
        files=_strategy(),
        extra=[patch.object(ImproveLoop, "_run_strategy_gate", unavailable)])
    state, items = _summary(conn, ctx.mission_id)
    assert (state, _keys(items)) == ("ok", {"hypothesis", "explanation"})


def test_an_oversized_output_is_replaced_by_a_truncation_mark(improve_env):
    app, root = improve_env
    conn = app.conn_core
    output = _observation_output("x" * 70_000)
    loop, ctx, _ = _run(app, root, output)
    stored = _output_json(conn, ctx.mission_id)
    assert set(stored) == {"truncated", "bytes"} and stored["truncated"] is True
    assert stored["bytes"] == len(json.dumps(output, ensure_ascii=False)
                                  .encode("utf-8"))
    state, items = _summary(conn, ctx.mission_id)
    assert state == "ok" and len(items["observation"]) < 600


def test_an_output_at_the_limit_is_kept_whole(improve_env):
    app, root = improve_env
    conn = app.conn_core
    base = _observation_output("")
    pad = run_summary.OUTPUT_JSON_MAX_BYTES - len(
        json.dumps(base, ensure_ascii=False).encode("utf-8"))
    output = _observation_output("y" * pad)
    assert len(json.dumps(output, ensure_ascii=False).encode("utf-8")) \
        == run_summary.OUTPUT_JSON_MAX_BYTES
    loop, ctx, _ = _run(app, root, output)
    assert _output_json(conn, ctx.mission_id) == output


# --- 補償 -------------------------------------------------------------------

def _crash_in(app, root, method):
    """commit の途中 (`method`) で例外にして、補償を呼ぶ。"""
    conn = app.conn_core
    loop = _loop(app, root)
    result = MissionResult(status="completed", output=_plugin_artifact("cand"),
                           transcript=[])
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(result=result, **kw)):
        mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
        mission_result = worker.run(mission)

    def boom(*a, **k):
        raise OSError("disk full")

    with patch.object(ImproveLoop, method, boom):
        with pytest.raises(OSError):
            loop.commit(mission=mission, ctx=ctx, result=mission_result,
                        now=NOW)
    loop.compensate_commit_failure(ctx=ctx, now=NOW, exc=OSError("disk full"))
    return ctx, conn


def test_commit_crash_after_the_name_check_keeps_claims_and_the_name(
        improve_env):
    app, root = improve_env
    ctx, conn = _crash_in(app, root, "_select_and_bind")
    state, items = _summary(conn, ctx.mission_id)
    assert state == "ok"
    assert items["tried"] == {"plugin": "cand", "kind": None, "origin": None,
                              "indicators": []}
    assert _keys(items) == {"hypothesis", "explanation", "tried"}
    assert _output_json(conn, ctx.mission_id)["selection_rationale"] \
        == "test rationale"


def test_commit_crash_before_the_name_check_has_claims_but_no_name(
        improve_env):
    app, root = improve_env
    loop, ctx, conn = _crash_commit_then_compensate(
        app, root, tool_calls_arg=None)
    state, items = _summary(conn, ctx.mission_id)
    assert (state, _keys(items)) == ("ok", {"hypothesis", "explanation"})


def test_commit_crash_before_the_output_is_known_is_empty(improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop = _loop(app, root)
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(
                   result=MissionResult(status="completed", output=None,
                                        transcript=[]), **kw)):
        mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
    loop.compensate_commit_failure(ctx=ctx, now=NOW, exc=RuntimeError("x"))
    assert _summary(conn, ctx.mission_id) == ("empty", {})
    assert _output_json(conn, ctx.mission_id) is None


def test_launch_failure_compensation_is_empty(improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop = _loop(app, root)
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(
                   result=MissionResult(status="completed", output=None,
                                        transcript=[]), **kw)):
        mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
    loop.compensate_launch_failure(ctx=ctx, now=NOW)
    assert _summary(conn, ctx.mission_id) == ("empty", {})


def test_prepare_failure_compensation_is_empty(improve_env):
    from agentic_fx.loops.improve_loop import ImprovePrepareFailed
    app, root = improve_env
    conn = app.conn_core
    loop = _loop(app, root)

    def boom(*a, **k):
        raise OSError("workspace")

    with patch.object(ImproveLoop, "_materialize_workspace", boom):
        with pytest.raises(ImprovePrepareFailed) as raised:
            loop.prepare(slot_key=None, now=NOW)
    assert _summary(conn, raised.value.mission_id) == ("empty", {})


def test_tx2_compensation_is_empty_and_keeps_the_output(improve_env):
    app, root = improve_env
    conn = app.conn_core

    def boom(*a, **k):
        raise RuntimeError("approval insert")

    loop, ctx, _ = _run(
        app, root, _plugin_artifact("cand"), files=_indicator(),
        extra=[patch("agentic_fx.loops.improve_loop.approvals_store.create",
                     boom)])
    assert conn.execute("SELECT reason_code FROM improve_run_notices "
                        "WHERE mission_id=?", (ctx.mission_id,)
                        ).fetchone()[0] == "approval_tx_failed"
    assert _summary(conn, ctx.mission_id) == ("empty", {})
    assert _output_json(conn, ctx.mission_id)["artifact"]["name"] == "cand"


def test_recovered_interrupted_mission_has_an_empty_row(improve_env):
    from agentic_fx.store import missions as missions_store
    app, root = improve_env
    conn = app.conn_core
    loop = _loop(app, root)
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(
                   result=MissionResult(status="completed", output=None,
                                        transcript=[]), **kw)):
        mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
    missions_store.recover_interrupted(conn, now=NOW, max_requeue=3)
    assert _summary(conn, ctx.mission_id) == ("empty", {})


# --- gate の失敗 ------------------------------------------------------------

def test_unprofitable_reports_the_candidates_own_numbers(improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, _ = _unprofitable(app, root)
    state, items = _summary(conn, ctx.mission_id)
    assert state == "ok"
    assert _keys(items) == {"hypothesis", "explanation", "tried", "floor",
                            "handoff"}
    floor = items["floor"]
    assert floor["failed_stage"] == "in_sample"
    assert floor["failed_conditions"] == ["pf"]
    assert floor["failed_pair_count"] == 1
    assert floor["thresholds"] == app.settings.improve.gate.snapshot()
    in_sample, holdout_stage = floor["stages"]
    assert in_sample["state"] == "measured"
    pair = in_sample["pairs"][0]
    assert (pair["pair"], pair["pf"], pair["avg_r"], pair["trades"],
            pair["failed"]) == ("USDJPY", 0.3, -0.1, 40, ["pf"])
    assert (holdout_stage["state"], holdout_stage["pairs"]) == ("not_run", [])
    assert items["handoff"]["outcomes"] == {"not_submitted": 1}


def test_floor_numbers_never_come_from_the_baseline_variant(improve_env):
    """床を通った候補の gate は、比較用の行 (variant=no_strategy) も同じ sink に
    流す。床との差は候補の判定値だけから出る。"""
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, _ = _run(
        app, root, _plugin_artifact("cand", kind="strategy"),
        files=_strategy(), in_sample=_fake_in_sample_metrics(40, pf=1.7),
        holdout_fn=_fake_holdout_metrics(pf=1.3), extra=[_family_patch(FAMILY)])
    _state, items = _summary(conn, ctx.mission_id)
    variants = {r["variant"] for r in conn.execute(
        "SELECT variant FROM backtest_runs WHERE mission_id=?",
        (ctx.mission_id,))}
    assert variants == {"candidate", "no_strategy"}
    in_sample, holdout_stage = items["floor"]["stages"]
    assert [p["pf"] for p in in_sample["pairs"]] == [1.7]
    assert [p["pf"] for p in holdout_stage["pairs"]] == [1.3]
    assert [p["trades"] for p in in_sample["pairs"]] == [40]


def test_settings_changed_later_do_not_change_the_stored_thresholds(
        improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, _ = _unprofitable(app, root)
    before = conn.execute("SELECT summary_json FROM improve_run_summaries "
                          "WHERE mission_id=?", (ctx.mission_id,)
                          ).fetchone()[0]
    app.settings.improve.gate.min_pf = 5.0
    after = conn.execute("SELECT summary_json FROM improve_run_summaries "
                         "WHERE mission_id=?", (ctx.mission_id,)).fetchone()[0]
    assert after == before
    assert json.loads(after)["floor"]["thresholds"]["min_pf"] == 1.0


def test_insufficient_trades_shows_reference_values_and_no_holdout(improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, _ = _run(
        app, root, _plugin_artifact("cand", kind="strategy"),
        files=_strategy(), in_sample=_fake_in_sample_metrics(16),
        extra=[_family_patch(FAMILY)])
    state, items = _summary(conn, ctx.mission_id)
    assert state == "ok"
    floor = items["floor"]
    assert floor["failed_stage"] is None
    assert [(s["stage"], s["state"]) for s in floor["stages"]] == [
        ("in_sample", "reference"), ("holdout", "not_run")]
    assert floor["stages"][0]["pairs"][0]["trades"] == 16
    assert floor["stages"][0]["pairs"][0]["failed"] == []
    assert "handoff" in items


def test_backtest_data_unavailable_keeps_what_was_obtained(improve_env):
    app, root = improve_env
    conn = app.conn_core

    def no_history(settings, *, history_conn, record_fn, **kwargs):
        raise holdout.NoHistoryError(f"no bars {TOKEN}")

    loop, ctx, _ = _run(
        app, root, _plugin_artifact("cand", kind="strategy"),
        files=_strategy(), in_sample=no_history)
    state, items = _summary(conn, ctx.mission_id)
    assert state == "ok" and "floor" not in items
    assert items["tried"]["plugin"] == "cand"
    assert "SECRET9" not in json.dumps(items)


def test_gate_failure_before_the_meta_has_the_name_only(improve_env):
    app, root = improve_env
    conn = app.conn_core

    def grow(loop, ctx):
        (ctx.staging_dir / "cand" / "nested").mkdir()

    loop, ctx, _ = _run(app, root, _plugin_artifact("cand", kind="strategy"),
                        files=_strategy(), before_commit=grow)
    state, items = _summary(conn, ctx.mission_id)
    assert state == "ok"
    assert items["tried"] == {"plugin": "cand", "kind": None, "origin": None,
                              "indicators": []}
    assert _keys(items) == {"hypothesis", "explanation", "tried"}


def test_the_agents_claimed_kind_is_not_used_as_the_parents_kind(improve_env):
    app, root = improve_env
    conn = app.conn_core

    def grow(loop, ctx):
        (ctx.staging_dir / "cand" / "nested").mkdir()

    loop, ctx, _ = _run(app, root, _plugin_artifact("cand", kind="strategy"),
                        files=_indicator(), before_commit=grow)
    _state, items = _summary(conn, ctx.mission_id)
    assert items["tried"]["kind"] is None


def test_quality_demotion_names_the_reason_without_comparison_or_handoff(
        improve_env):
    app, root = improve_env
    conn = app.conn_core
    _decided_row(conn, name="past", logic_hash=_hash_of(PY_X),
                 outcome="rejected_by_human")
    loop, ctx, _ = _run_strategy(app, root, "cand", metrics=METRICS_2)
    state, items = _summary(conn, ctx.mission_id)
    assert state == "ok"
    assert items["parent_reason"] == "duplicate_logic"
    assert _keys(items) == {"hypothesis", "explanation", "tried", "floor",
                            "parent_reason"}


# --- 失敗の扱い ---------------------------------------------------------------

def test_serialization_failure_becomes_build_failed_and_the_mission_ends(
        improve_env):
    app, root = improve_env
    conn = app.conn_core

    def boom(*a, **k):
        raise ValueError("cannot serialize")

    with patch.object(run_summary, "_dump", boom):
        loop, ctx, _ = _run(app, root, _observation_output("nothing"))
    assert _summary(conn, ctx.mission_id) == ("build_failed", {})
    assert conn.execute("SELECT status FROM missions WHERE id=?",
                        (ctx.mission_id,)).fetchone()[0] == "completed"


def test_preparation_failure_becomes_build_failed(improve_env):
    app, root = improve_env
    conn = app.conn_core

    def boom(*a, **k):
        raise RuntimeError("bad material")

    with patch.object(run_summary, "_claims", boom):
        loop, ctx, _ = _run(app, root, _observation_output("nothing"))
    assert _summary(conn, ctx.mission_id) == ("build_failed", {})
    assert _output_json(conn, ctx.mission_id)["artifact"]["reason"] == "nothing"


def test_handoff_failure_leaves_the_field_out_and_ends_the_mission(improve_env):
    app, root = improve_env
    conn = app.conn_core

    def boom(*a, **k):
        raise sqlite3.OperationalError("projection failed")

    with patch("agentic_fx.loops.improve_loop.family_tally", boom):
        loop, ctx, _ = _unprofitable(app, root)
    state, items = _summary(conn, ctx.mission_id)
    assert state == "ok" and "handoff" not in items
    assert "floor" in items
    assert conn.execute("SELECT status FROM missions WHERE id=?",
                        (ctx.mission_id,)).fetchone()[0] == "completed"
    outcome = conn.execute(
        "SELECT outcome_class FROM candidate_archives WHERE mission_id=? "
        "AND metrics_json='{}'", (ctx.mission_id,)).fetchall()
    assert [r[0] for r in outcome] in (["unprofitable"], [])


def test_handoff_counts_the_missions_own_settle(improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, _ = _unprofitable(app, root)
    _state, items = _summary(conn, ctx.mission_id)
    own = conn.execute(
        "SELECT outcome_class FROM candidate_archives WHERE mission_id=?",
        (ctx.mission_id,)).fetchall()
    assert any(r[0] == "unprofitable" for r in own)
    assert items["handoff"]["attempts"] == sum(
        items["handoff"]["outcomes"].values())


def test_summary_insert_failure_leaves_neither_settle_nor_summary(improve_env):
    app, root = improve_env
    conn = app.conn_core

    def boom(*a, **k):
        raise sqlite3.OperationalError("disk full")

    with patch("agentic_fx.store.missions.summaries_store.insert", boom):
        with pytest.raises(sqlite3.OperationalError):
            _unprofitable(app, root)
    mission_id = conn.execute("SELECT MAX(id) FROM missions").fetchone()[0]
    assert conn.execute("SELECT status FROM missions WHERE id=?",
                        (mission_id,)).fetchone()[0] == "running"
    assert conn.execute("SELECT COUNT(*) FROM improve_run_summaries"
                        ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM candidate_archives WHERE mission_id=? AND "
        "outcome_class!='not_submitted'", (mission_id,)).fetchone()[0] == 0


def test_cas_failure_leaves_neither_settle_nor_summary(improve_env):
    """mission がすでに終端していれば、後から来た終端は settle も要約も残さない。"""
    app, root = improve_env
    conn = app.conn_core

    def finish_first(loop, ctx):
        conn.execute("UPDATE missions SET status='failed' WHERE id=?",
                     (ctx.mission_id,))
        conn.commit()

    with pytest.raises(RuntimeError, match="not 'running'"):
        _unprofitable(app, root, before_commit=finish_first)
    mission_id = conn.execute("SELECT MAX(id) FROM missions").fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM improve_run_summaries"
                        ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM candidate_archives WHERE mission_id=? AND "
        "outcome_class!='not_submitted'", (mission_id,)).fetchone()[0] == 0


def test_rollback_to_savepoint_failure_propagates_and_rolls_everything_back(
        improve_env):
    app, root = improve_env
    conn = app.conn_core
    real = ImproveLoop._count_handoff

    class _Conn:
        """SAVEPOINT の巻き戻しだけを失敗させる接続の被せ物。"""
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **k):
            if sql.startswith("ROLLBACK TO summary_handoff"):
                raise sqlite3.OperationalError("rollback failed")
            return self._inner.execute(sql, *a, **k)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def wrapped(self, c, ctx, key):
        return real(self, _Conn(c), ctx, key)

    def fail_tally(*a, **k):
        raise sqlite3.OperationalError("projection")

    with patch.object(ImproveLoop, "_count_handoff", wrapped), \
            patch("agentic_fx.loops.improve_loop.family_tally", fail_tally):
        with pytest.raises(sqlite3.OperationalError, match="rollback failed"):
            _unprofitable(app, root)
    assert conn.execute("SELECT COUNT(*) FROM improve_run_summaries"
                        ).fetchone()[0] == 0


def test_handoff_counts_every_artifact_of_the_same_mission(improve_env):
    app, root = improve_env
    conn = app.conn_core

    def plant(loop, ctx):
        for tag in ("x", "y"):
            archives.insert(
                conn, mission_id=ctx.mission_id, name=f"extra_{tag}",
                content_hash=f"c-{tag}", artifact_hash=f"h-{tag}",
                archive_path=None, pair="USDJPY", metrics={"pf": 0.5},
                now=NOW, family_key=FAMILY, commit=True)

    loop, ctx, _ = _unprofitable(app, root, before_commit=plant, seed=False)
    _state, items = _summary(conn, ctx.mission_id)
    assert items["handoff"]["attempts"] == 3
    assert items["handoff"]["outcomes"] == {"not_submitted": 3}


def test_no_family_key_means_no_handoff(improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, _ = _run(
        app, root, _plugin_artifact("cand", kind="strategy"),
        files=_strategy(),
        in_sample=_fake_in_sample_metrics(40, pf=0.3, avg_r=-0.1),
        holdout_fn=_fake_holdout_metrics())
    state, items = _summary(conn, ctx.mission_id)
    assert state == "ok" and "handoff" not in items and "floor" in items


def test_failed_settle_means_no_handoff_and_the_mission_still_ends(improve_env):
    app, root = improve_env
    conn = app.conn_core

    def boom(self, *a, **k):
        raise sqlite3.OperationalError("archive write failed")

    loop, ctx, _ = _unprofitable(
        app, root, extra=[_family_patch(FAMILY),
                          patch.object(ImproveLoop, "_ensure_final_archive_row",
                                       boom)])
    state, items = _summary(conn, ctx.mission_id)
    assert state == "ok" and "handoff" not in items
    assert conn.execute("SELECT status FROM missions WHERE id=?",
                        (ctx.mission_id,)).fetchone()[0] == "completed"


def test_handoff_uses_the_inventory_pinned_at_mission_start(improve_env):
    """配備の集合は ctx.inventory。終端 tx の中で live の plugins/ を読み直さない。"""
    app, root = improve_env
    conn = app.conn_core
    seen = []
    import agentic_fx.loops.improve_loop as loop_mod
    real = loop_mod.deployed_families

    def spy(metas):
        seen.append(list(metas))
        return real(metas)

    def forbid(*a, **k):
        raise AssertionError("live plugins read inside the terminal tx")

    def start_guards(loop, ctx):
        guards = [
            patch.object(loop_mod, "deployed_families", spy),
            patch.object(loop_mod, "approved_plugins", forbid),
            patch("agentic_fx.loops.improve_context.approved_plugins", forbid)]
        for guard in guards:
            guard.start()
        ctx_holder.append((ctx, guards))

    ctx_holder = []
    try:
        loop, ctx, _ = _unprofitable(app, root, before_commit=start_guards)
    finally:
        for _ctx, guards in ctx_holder:
            for guard in reversed(guards):
                guard.stop()
    _state, items = _summary(conn, ctx.mission_id)
    assert "handoff" in items
    assert seen == [list(ctx.inventory.inventory.metas)]


# --- 材料入れは mission ごと ---------------------------------------------------

def test_summary_material_is_per_mission(improve_env):
    app, root = improve_env
    loop = _loop(app, root)
    contexts = []
    for _ in range(2):
        with patch("agentic_fx.runners.worker_runner.WorkerRunner",
                   lambda **kw: FakeImproveWorkerRunner(
                       result=MissionResult(status="completed", output=None,
                                            transcript=[]), **kw)):
            contexts.append(loop.prepare(slot_key=None, now=NOW)[1])
    first, second = contexts
    assert first.summary_material is not second.summary_material
    first.summary_material.plugin_name = "only_first"
    assert second.summary_material.plugin_name is None


def test_a_second_mission_does_not_inherit_the_first_ones_material(improve_env):
    app, root = improve_env
    conn = app.conn_core
    first_loop, first_ctx, _ = _unprofitable(app, root)
    loop, ctx, _ = _run(app, root, _observation_output("second"))
    _state, items = _summary(conn, ctx.mission_id)
    assert _keys(items) == {"hypothesis", "explanation", "observation"}


# --- prompt に流れない -------------------------------------------------------

MARKER_RATIONALE = "ZZRATIONALEMARK"
MARKER_SUMMARY = "ZZARTIFACTSUMMARYMARK"
MARKER_OBSERVATION = "ZZOBSERVATIONMARK"


def test_summary_and_output_json_never_reach_the_next_missions_prompt(
        improve_env):
    app, root = improve_env
    conn = app.conn_core
    output = _plugin_artifact("cand", kind="strategy")
    output["selection_rationale"] = MARKER_RATIONALE
    output["artifact"]["summary"] = MARKER_SUMMARY
    loop, ctx, _ = _run(
        app, root, output, files=_strategy(),
        in_sample=_fake_in_sample_metrics(40, pf=0.3, avg_r=-0.1),
        holdout_fn=_fake_holdout_metrics())
    obs_loop, obs_ctx, _ = _run(app, root,
                                _observation_output(MARKER_OBSERVATION))
    stored = conn.execute(
        "SELECT summary_json FROM improve_run_summaries WHERE mission_id=?",
        (ctx.mission_id,)).fetchone()[0]
    assert MARKER_RATIONALE in stored and MARKER_SUMMARY in stored
    assert MARKER_RATIONALE in conn.execute(
        "SELECT output_json FROM missions WHERE id=?",
        (ctx.mission_id,)).fetchone()[0]
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(
                   result=MissionResult(status="completed", output=None,
                                        transcript=[]), **kw)):
        mission, _ctx, _worker = _loop(app, root).prepare(
            slot_key=None, now=NOW)
    for marker in (MARKER_RATIONALE, MARKER_SUMMARY):
        assert marker not in mission.prompt
    # 観察文は notice にだけ残る (prompt には backlog の last_result 経由の既存の道だけ)
    assert "improve_run_summaries" not in mission.prompt


def test_only_the_summary_producers_and_readers_touch_the_table():
    """要約の表と `output_json` を読む場所を allowlist で固定する。prompt を
    作る側 (improve_context / improve_loop の prompt 組立) はここに入らない。"""
    from pathlib import Path
    import agentic_fx
    src = Path(agentic_fx.__file__).parent
    touching = sorted(
        str(p.relative_to(src)) for p in src.rglob("*.py")
        if "improve_run_summaries" in p.read_text(encoding="utf-8"))
    assert touching == [
        "loops/improve_loop.py", "loops/improve_run_summary.py",
        "ops/service.py", "store/db.py", "store/improve_run_summaries.py",
        "store/missions.py"]
    context = (src / "loops" / "improve_context.py").read_text(encoding="utf-8")
    assert "output_json" not in context and "summary_json" not in context


# --- 保存する元の出力の無害化 / 材料づくりの失敗の記録 -------------------------------

def test_output_json_never_keeps_a_keyed_url_or_token(improve_env):
    app, root = improve_env
    conn = app.conn_core
    output = _observation_output(f"see {TOKEN}")
    output["selection_rationale"] = f"why {TOKEN} apikey=SECRET9"
    output["selected"]["idea"] = f"idea {TOKEN}"
    loop, ctx, _ = _run(app, root, output)
    raw = conn.execute("SELECT output_json FROM missions WHERE id=?",
                       (ctx.mission_id,)).fetchone()[0]
    assert "SECRET9" not in raw and "leak.example" not in raw
    stored = json.loads(raw)
    assert set(stored) == set(output)
    assert stored["selected"]["backlog_id"] is None
    assert stored["artifact"]["type"] == "observation"
    assert stored["selection_rationale"].startswith("why <url>")


def test_output_json_of_a_failed_mission_is_sanitized_too(improve_env):
    app, root = improve_env
    conn = app.conn_core
    broken = {"selected": {"idea": f"half {TOKEN}"},
              "extra": {"deep": [f"k apikey=SECRET9 {TOKEN}", 3, None]}}
    loop, ctx, _ = _run(app, root, broken, status="failed")
    raw = conn.execute("SELECT output_json FROM missions WHERE id=?",
                       (ctx.mission_id,)).fetchone()[0]
    assert "SECRET9" not in raw and "leak.example" not in raw
    stored = json.loads(raw)
    assert stored["extra"]["deep"][1:] == [3, None]


def test_a_floor_material_failure_leaves_an_activity_row(improve_env):
    app, root = improve_env
    conn = app.conn_core

    def boom(*a, **k):
        raise RuntimeError("bad floor secret=SECRET9")

    with patch.object(run_summary, "floor_material", boom):
        loop, ctx, _ = _unprofitable(app, root)
    assert conn.execute("SELECT status FROM missions WHERE id=?",
                        (ctx.mission_id,)).fetchone()[0] == "completed"
    log = (root / "logs" / "activity.log").read_text()
    lines = [line for line in log.splitlines()
             if "summary_material_failed" in line]
    assert len(lines) == 1
    assert f"mission_id={ctx.mission_id}" in lines[0]
    assert "SECRET9" not in log
