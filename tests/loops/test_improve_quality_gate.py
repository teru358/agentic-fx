"""質検査 (段 L・F・M) を `commit()` の終端まで通した振る舞い。

優先順位 (noop_copy > 床 > duplicate_logic > 仮説族 > duplicate_metrics)、
一般更新と pin 再ロックの区別、仮説族の検出属性、失敗の 3 分類を見る。
E2E の足場は `test_improve_candidate_ledger` を再利用する。"""
from __future__ import annotations

import dataclasses
import shutil
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from agentic_fx.loops import improve_quality as quality
from agentic_fx.loops.improve_loop import ImproveLoop
from agentic_fx.plugin.logic_identity import logic_hash_of
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import candidate_archives as archives

from tests.loops.test_improve_candidate_ledger import (
    _PASSING_INDICATOR_CONFIG,
    _PASSING_INDICATOR_PY,
    _PASSING_INDICATOR_TEST,
    _PASSING_STRATEGY_CONFIG,
    _PASSING_STRATEGY_PY,
    _PASSING_STRATEGY_TEST,
    _expect_rows,
    _payload,
    _plugin_artifact,
    _rows,
    _run,
    _seed,
)
from tests.loops.test_improve_e2e import NOW, improve_env  # noqa: F401
from tests.loops.test_improve_loop_duplicate_metrics import (
    _fake_holdout_full_metrics,
    _fake_in_sample_full_metrics,
)

NOTICE = quality.QUALITY_NOTICE
OTHER_MISSION = 9001

METRICS_1 = {"trades": 194, "pf": 1.4981, "win_rate": 0.487, "avg_r": 0.188,
             "max_drawdown": 0.0373, "total_pnl": 100.0}
METRICS_2 = {"trades": 120, "pf": 1.31, "win_rate": 0.45, "avg_r": 0.11,
             "max_drawdown": 0.03, "total_pnl": 80.0}
HOLDOUT = {"trades": 53, "pf": 1.1, "win_rate": 0.3, "avg_r": 0.037,
           "max_drawdown": 0.0392, "total_pnl": 10.0}
UNPROFITABLE = {"trades": 60, "pf": 0.3, "win_rate": 0.2, "avg_r": -0.1,
                "max_drawdown": 0.2, "total_pnl": -50.0}

# 構造の違う戦略本体 (docstring・数値・rationale を変えても ロジック hash は
# 変わらないので、別物にするには構造を変える)
PY_X = _PASSING_STRATEGY_PY
PY_Y = _PASSING_STRATEGY_PY + '\n_VARIANT = "y"\n'
PY_Z = _PASSING_STRATEGY_PY + '\n_VARIANT = "z"\n_OTHER = 1\n'
FAMILY = "strategy:bollinger,rsi@1h@USDJPY"


def _files(name, py=PY_X, config=_PASSING_STRATEGY_CONFIG):
    return (name, py, config, _PASSING_STRATEGY_TEST)


def _artifact(name, idea=None):
    output = _plugin_artifact(name, kind="strategy")
    if idea is not None:
        output["selected"]["idea"] = idea
        output["discoveries"] = []
    return output


def _run_strategy(app, root, name, *, py=PY_X, metrics=METRICS_1,
                  holdout=HOLDOUT, extra=(), seed=False, idea=None,
                  before_commit=None):
    return _run(
        app, root, _artifact(name, idea),
        files=_files(name, py), in_sample=_fake_in_sample_full_metrics(metrics),
        holdout_fn=_fake_holdout_full_metrics(holdout), extra=extra, seed=seed,
        before_commit=before_commit)


def _decided_row(conn, *, name, logic_hash, outcome, mission_id=OTHER_MISSION,
                 family_key=None):
    """他 mission の行を、許される遷移だけで `outcome` まで進めて作る。"""
    row_id = archives.insert(
        conn, mission_id=mission_id, name=name, content_hash=f"c-{name}",
        artifact_hash=f"a-{name}-{outcome}", archive_path=None,
        pair="USDJPY", metrics={}, now=NOW, logic_hash=logic_hash,
        family_key=family_key, commit=True)
    path = {
        "rejected_by_human": ["pending", "rejected_by_human"],
        "approved": ["pending", "approved"],
        "duplicate_logic": ["duplicate_logic"],
        "duplicate_metrics": ["duplicate_metrics"],
        "family_redirect": ["family_redirect"],
        "not_submitted": [],
    }[outcome]
    current = "not_submitted"
    for step in path:
        assert archives.settle(conn, row_id, step, expected_from=current,
                               commit=True)
        current = step
    return row_id


def _hash_of(py, config=_PASSING_STRATEGY_CONFIG):
    value = logic_hash_of(py, config)
    assert value is not None
    return value


def _deployed(name, *, logic_hash="v1:" + "9" * 64, family=None,
              kind="strategy"):
    return quality.IdentityEntry("deployed", name, kind, logic_hash, family)


def _population_patch(deployed=(), examples=()):
    def build(self, ctx):
        return quality.FilePopulation(
            deployed=quality.Population(
                entries=list(deployed), attempted=len(deployed)),
            examples=quality.Population(
                entries=list(examples), attempted=len(examples)))
    return patch.object(ImproveLoop, "_load_quality_population", build)


def _family_patch(family_key):
    original = ImproveLoop._final_candidate

    def wrapped(self, *a, **kw):
        candidate = original(self, *a, **kw)
        return (None if candidate is None
                else dataclasses.replace(candidate, family_key=family_key))
    return patch.object(ImproveLoop, "_final_candidate", wrapped)


def _identity_patch(**changes):
    original = ImproveLoop._final_candidate

    def wrapped(self, *a, **kw):
        candidate = original(self, *a, **kw)
        return (None if candidate is None
                else dataclasses.replace(candidate, **changes))
    return patch.object(ImproveLoop, "_final_candidate", wrapped)


def _last_result(conn):
    return conn.execute(
        "SELECT last_result FROM improvement_backlog ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]


def _approval_count(conn):
    return conn.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0]


def _activity(root):
    return (root / "logs" / "activity.log").read_text(encoding="utf-8")


def _final(conn, ctx, seeded):
    rows = [r for r in _rows(conn, ctx.mission_id) if r["id"] not in seeded]
    assert len(rows) == 1
    return rows[0]


# --- AC-8 優先順位 -----------------------------------------------------------

def test_ac8_i_noop_copy_wins_over_logic_and_family_matches(improve_env):
    """完全コピーで、ロジックにも仮説族にも一致する候補。最終 outcome は
    noop_copy で、段 L・F には到達せず quality_flags は空のまま。"""
    app, root = improve_env
    conn = app.conn_core
    example = root / "docs" / "examples" / "plugins" / "ind_example"
    example.mkdir(parents=True)
    (example / "plugin.py").write_text(_PASSING_INDICATOR_PY)
    (example / "config.yaml").write_text(_PASSING_INDICATOR_CONFIG)
    candidate_hash = logic_hash_of(
        _PASSING_INDICATOR_PY, _PASSING_INDICATOR_CONFIG)
    _decided_row(conn, name="past", logic_hash=candidate_hash,
                 outcome="rejected_by_human")
    calls = []
    original = ImproveLoop._check_candidate_quality

    def spy(self, *a, **kw):
        calls.append(1)
        return original(self, *a, **kw)

    deployed = _deployed("other", family="indicator:cand@1h",
                         kind="indicator", logic_hash=candidate_hash)
    loop, ctx, seeded = _run(
        app, root, _plugin_artifact("cand"),
        files=("cand", _PASSING_INDICATOR_PY, _PASSING_INDICATOR_CONFIG,
               _PASSING_INDICATOR_TEST),
        extra=[patch.object(ImproveLoop, "_check_candidate_quality", spy),
               _population_patch(deployed=[deployed])])
    row = _expect_rows(conn, ctx, seeded, "noop_copy")
    assert row["quality_flags"] == []
    assert calls == []
    assert _approval_count(conn) == 0


@pytest.mark.parametrize("outcome", [
    "rejected_by_human", "duplicate_metrics", "duplicate_logic",
    "family_redirect"])
def test_ac8_iii_ac12_logic_match_with_a_past_candidate_is_rejected_whatever_the_metrics(
        improve_env, outcome):
    app, root = improve_env
    conn = app.conn_core
    _decided_row(conn, name="past", logic_hash=_hash_of(PY_X), outcome=outcome)
    loop, ctx, seeded = _run_strategy(
        app, root, "cand", metrics=METRICS_2, extra=[])
    row = _expect_rows(conn, ctx, seeded, "duplicate_logic")
    assert row["archive_path"] is None and row["quality_flags"] == []
    assert _approval_count(conn) == 0
    assert _last_result(conn) == f"observation:{NOTICE}"


def test_ac8_iii_logic_match_with_an_example_is_rejected(improve_env):
    app, root = improve_env
    conn = app.conn_core
    example = root / "docs" / "examples" / "plugins" / "hold_example"
    example.mkdir(parents=True)
    # 説明文字列だけが違う (完全コピーではないがロジックは等価)
    (example / "plugin.py").write_text(
        PY_X.replace("e2e fixture always holds", "another explanation"))
    (example / "config.yaml").write_text(_PASSING_STRATEGY_CONFIG)
    loop, ctx, seeded = _run_strategy(app, root, "cand")
    _expect_rows(conn, ctx, seeded, "duplicate_logic")
    assert _last_result(conn) == f"observation:{NOTICE}"


def test_ac8_iii_logic_match_with_a_deployed_other_name_is_rejected(improve_env):
    app, root = improve_env
    conn = app.conn_core
    deployed = _deployed("alpha", logic_hash=_hash_of(PY_X))
    loop, ctx, seeded = _run_strategy(
        app, root, "cand", extra=[_population_patch(deployed=[deployed])])
    _expect_rows(conn, ctx, seeded, "duplicate_logic")
    assert "matched=deployed:alpha" in _activity(root)


def test_ac8_iv_duplicate_logic_wins_over_family(improve_env):
    app, root = improve_env
    conn = app.conn_core
    deployed = _deployed("alpha", logic_hash=_hash_of(PY_X), family=FAMILY)
    loop, ctx, seeded = _run_strategy(
        app, root, "cand",
        extra=[_population_patch(deployed=[deployed]), _family_patch(FAMILY)])
    row = _expect_rows(conn, ctx, seeded, "duplicate_logic")
    assert row["quality_flags"] == []


def test_ac8_v_unprofitable_wins_over_duplicate_logic(improve_env):
    app, root = improve_env
    conn = app.conn_core
    _decided_row(conn, name="past", logic_hash=_hash_of(PY_X),
                 outcome="rejected_by_human")
    loop, ctx, seeded = _run_strategy(
        app, root, "cand", metrics=UNPROFITABLE)
    _expect_rows(conn, ctx, seeded, "unprofitable")


def test_ac8_vi_same_metrics_with_a_different_structure_is_duplicate_metrics(
        improve_env):
    app, root = improve_env
    conn = app.conn_core
    _run_strategy(app, root, "conv_a", py=PY_X)
    loop, ctx, seeded = _run_strategy(app, root, "conv_b", py=PY_Y)
    _expect_rows(conn, ctx, seeded, "duplicate_metrics")
    assert _approval_count(conn) == 1


def test_ac8_a_metrics_match_does_not_hide_a_logic_match(improve_env):
    """同じ成績かつ同じロジックなら、先に当たる段 L が決める。"""
    app, root = improve_env
    conn = app.conn_core
    _run_strategy(app, root, "conv_a", py=PY_X)
    loop, ctx, seeded = _run_strategy(app, root, "conv_b", py=PY_X)
    _expect_rows(conn, ctx, seeded, "duplicate_logic")


# --- AC-12 / AC-20 母集団 -----------------------------------------------------

def test_ac12_ac20_ii_an_earlier_iteration_of_the_same_mission_is_not_compared(
        improve_env):
    """途中の反復の行 (同じ mission) は質検査の時点で母集団に入らない。"""
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, seeded = _run(
        app, root, _plugin_artifact("cand", kind="strategy"),
        files=_files("cand"), in_sample=_fake_in_sample_full_metrics(METRICS_1),
        holdout_fn=_fake_holdout_full_metrics(HOLDOUT),
        before_commit=lambda loop, ctx: _decided_row(
            conn, name="earlier", logic_hash=_hash_of(PY_X),
            outcome="rejected_by_human", mission_id=ctx.mission_id))
    final = [r for r in _rows(conn, ctx.mission_id)
             if r["name"] == "cand"]
    assert [r["outcome_class"] for r in final] == ["pending"]


def test_ac20_i_a_structurally_different_candidate_passes_against_the_same_past(
        improve_env):
    app, root = improve_env
    conn = app.conn_core
    _decided_row(conn, name="past", logic_hash=_hash_of(PY_X),
                 outcome="rejected_by_human")
    loop, ctx, seeded = _run_strategy(app, root, "cand", py=PY_Z)
    _expect_rows(conn, ctx, seeded, "pending")


def test_ac12_a_not_submitted_row_does_not_block_a_numeric_retry(improve_env):
    app, root = improve_env
    conn = app.conn_core
    _decided_row(conn, name="past", logic_hash=_hash_of(PY_X),
                 outcome="not_submitted")
    loop, ctx, seeded = _run_strategy(app, root, "cand")
    _expect_rows(conn, ctx, seeded, "pending")


# --- AC-9 一般更新 -----------------------------------------------------------

def test_ac9_same_name_update_with_the_same_logic_is_not_a_duplicate_logic(
        improve_env):
    """配備済みの同名旧版とロジックが一致する (config 値だけが違う) 同名候補は、
    duplicate_logic でも仮説族の検出でもなく、そのまま提出される。"""
    app, root = improve_env
    conn = app.conn_core
    deployed = _deployed("cand", logic_hash=_hash_of(PY_X), family=FAMILY)
    loop, ctx, seeded = _run_strategy(
        app, root, "cand",
        extra=[_population_patch(deployed=[deployed]), _family_patch(FAMILY)])
    row = _expect_rows(conn, ctx, seeded, "pending")
    assert row["quality_flags"] == []


def test_ac9_same_name_update_with_the_same_metrics_is_duplicate_metrics(
        improve_env):
    app, root = improve_env
    conn = app.conn_core
    deployed = _deployed("cand", logic_hash=_hash_of(PY_X))
    first_loop, first_ctx, first_seeded = _run_strategy(
        app, root, "cand",
        extra=[_population_patch(deployed=[deployed])])
    first = _expect_rows(conn, first_ctx, first_seeded, "pending")
    assert archives.settle(conn, first["id"], "approved",
                           expected_from="pending", commit=True)
    loop, ctx, seeded = _run_strategy(
        app, root, "cand", idea="second idea for cand",
        extra=[_population_patch(deployed=[deployed])])
    _expect_rows(conn, ctx, seeded, "duplicate_metrics")


def test_ac9_same_name_update_with_different_metrics_reaches_approval(
        improve_env):
    app, root = improve_env
    conn = app.conn_core
    deployed = _deployed("cand", logic_hash=_hash_of(PY_X))
    first_loop, first_ctx, first_seeded = _run_strategy(
        app, root, "cand",
        extra=[_population_patch(deployed=[deployed])])
    first = _expect_rows(conn, first_ctx, first_seeded, "pending")
    assert archives.settle(conn, first["id"], "approved",
                           expected_from="pending", commit=True)
    loop, ctx, seeded = _run_strategy(
        app, root, "cand", metrics=METRICS_2, idea="second idea for cand",
        extra=[_population_patch(deployed=[deployed])])
    _expect_rows(conn, ctx, seeded, "pending")


def test_ac9_the_same_name_exception_needs_the_name_to_be_deployed(improve_env):
    app, root = improve_env
    conn = app.conn_core
    _decided_row(conn, name="cand", logic_hash=_hash_of(PY_X),
                 outcome="approved")
    loop, ctx, seeded = _run_strategy(app, root, "cand", metrics=METRICS_2)
    _expect_rows(conn, ctx, seeded, "duplicate_logic")


# --- AC-10 pin 再ロックは一般更新と別のケース --------------------------------

def test_ac10_relock_resubmission_passes_l_and_f_by_the_ordinary_rule_and_m_keeps_the_relock_exception(
        tmp_path):
    """段 L・F は `is_relock_transition` を呼ばない (呼ぶのは段 M の既存の
    再ロック例外だけ)。同名の再ロックはロジックが変わらないので、一般更新の
    規則で L・F を通り、M では自分の旧版との成績一致が再ロックとして許される。
    別名で同じ中身を出すと、再ロックの例外を流用せず duplicate_logic。"""
    import sys

    from tests.fixtures import indicator_wiring as fx
    from tests.fixtures.wiring_envs import loop_env as _loop_env
    from tests.loops.test_improve_loop_duplicate_metrics import (
        _approve_plain_strategy, _inventory_with_phase1, _seed_approved_candidate_metrics,
        loop_staging)
    loop, conn, plugins_root = _loop_env(tmp_path)
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    fx.write_rsi_pullback(plugins_root, pins={"rsi": "a" * 64})
    _approve_plain_strategy(conn, plugins_root, "rsi_pullback")
    staging = loop_staging(loop)
    candidate_dir = fx.write_rsi_pullback(staging, pins={"rsi": hashes["rsi"]})
    inventory = _inventory_with_phase1(conn, plugins_root)
    _seed_approved_candidate_metrics(conn, pair="USDJPY", trades=40, pf=1.5,
                                     avg_r=0.2)
    payload = {"kind": "strategy", "name": "rsi_pullback",
               "content_hash": "c" * 64, "eval_source": "dukascopy",
               "base_interval": "5m",
               "in_sample": {"USDJPY": {"trades": 40, "pf": 1.5, "avg_r": 0.2}}}
    file_population = quality.load_file_population(plugins_root, None)
    assert file_population.unavailable is None
    callers = []
    import agentic_fx.loops.improve_loop as improve_loop_module
    real = improve_loop_module.is_relock_transition

    def spy(*a, **kw):
        callers.append(sys._getframe(1).f_code.co_name)
        return real(*a, **kw)

    def candidate(name):
        return loop._final_candidate(
            staging / "rsi_pullback", name=name, content_hash="c" * 64,
            artifact_hash="d" * 64)

    with patch.object(improve_loop_module, "is_relock_transition", spy):
        general = loop._check_candidate_quality(
            conn, payload, candidate=candidate("rsi_pullback"), mission_id=1,
            file_population=file_population, inventory=inventory,
            staging_dir=staging)
        assert general.outcome is None and general.flags == ()
        renamed = loop._check_candidate_quality(
            conn, dict(payload, name="rsi_pullback_copy"),
            candidate=candidate("rsi_pullback_copy"), mission_id=1,
            file_population=file_population, inventory=inventory,
            staging_dir=staging)
    assert renamed.outcome == "duplicate_logic"
    assert set(callers) == {"_check_duplicate_metrics_for_approval"}
    assert candidate_dir.is_dir()


# --- AC-11 仮説族 ------------------------------------------------------------

def test_ac11_family_of_a_deployed_other_name_is_flagged_and_still_submitted(
        improve_env):
    app, root = improve_env
    conn = app.conn_core
    deployed = _deployed("alpha", family=FAMILY)
    loop, ctx, seeded = _run_strategy(
        app, root, "cand",
        extra=[_population_patch(deployed=[deployed]), _family_patch(FAMILY)])
    row = _expect_rows(conn, ctx, seeded, "pending")
    assert row["quality_flags"] == ["family_detected"]
    approval_id = conn.execute(
        "SELECT id FROM approval_requests").fetchone()[0]
    assert _payload(conn)["candidate_archive_id"] == row["id"]
    approvals_store.apply_decision(
        conn, approval_id, status="approved", decided_by="t", now=NOW)
    decided = archives.get(conn, row["id"])
    assert decided["outcome_class"] == "approved"
    assert decided["quality_flags"] == ["family_detected"]


def test_ac11_a_rejected_family_is_not_detected(improve_env):
    app, root = improve_env
    conn = app.conn_core
    _decided_row(conn, name="past", logic_hash="v1:" + "7" * 64,
                 outcome="rejected_by_human", family_key=FAMILY)
    loop, ctx, seeded = _run_strategy(
        app, root, "cand", extra=[_family_patch(FAMILY)])
    row = _expect_rows(conn, ctx, seeded, "pending")
    assert row["quality_flags"] == []


def test_ac11_a_family_on_a_different_key_is_not_detected(improve_env):
    app, root = improve_env
    conn = app.conn_core
    deployed = _deployed("alpha", family=FAMILY)
    loop, ctx, seeded = _run_strategy(
        app, root, "cand",
        extra=[_population_patch(deployed=[deployed]),
               _family_patch("strategy:ema@1h@USDJPY")])
    row = _expect_rows(conn, ctx, seeded, "pending")
    assert row["quality_flags"] == []


# --- AC-18 失敗の 3 分類 -----------------------------------------------------

def test_ac18_the_candidates_own_identity_failure_is_rejected_before_the_tx(
        improve_env):
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, seeded = _run_strategy(
        app, root, "cand", extra=[_identity_patch(logic_hash=None)])
    row = _expect_rows(conn, ctx, seeded, "gate_failed")
    assert row["quality_flags"] == []
    assert _approval_count(conn) == 0
    # agent 面は原因を示さない固定の理由語だけ。原因は activity にだけ残る。
    result = _last_result(conn)
    assert result == "gate_failed:unverified"
    assert "identity" not in result
    assert "candidate_identity_unavailable" in _activity(root)


def test_ac18_the_in_tx_check_also_rejects_a_candidate_without_identity(
        improve_env):
    """到達しないはずの経路でも fail closed: 段 L・F を skip して通さない。"""
    app, root = improve_env
    conn = app.conn_core
    from agentic_fx.loops.improve_loop import _FinalCandidate
    loop = ImproveLoop(
        root=root, settings=app.settings, clock=app.clock,
        db_write_conn_factory=lambda: conn,
        db_readonly_conn_factory=lambda: conn,
        activity=app.activity, rag=app.rag)
    candidate = _FinalCandidate(
        name="cand", content_hash="c" * 64, artifact_hash="d" * 64,
        pair="USDJPY", logic_hash=None, family_key=None,
        candidate_dir=root)
    verdict = loop._check_candidate_quality(
        conn, {"kind": "strategy", "name": "cand"}, candidate=candidate,
        mission_id=1, inventory=None, staging_dir=None,
        file_population=quality.FilePopulation(
            deployed=quality.Population(), examples=quality.Population()))
    assert verdict.outcome == "gate_failed"
    assert verdict.reason == NOTICE


def test_ac18_one_corrupted_row_is_skipped_with_a_warning_and_others_compared(
        improve_env):
    app, root = improve_env
    conn = app.conn_core
    bad = _decided_row(conn, name="broken", logic_hash="garbage",
                       outcome="rejected_by_human")
    _decided_row(conn, name="good", logic_hash=_hash_of(PY_X),
                 outcome="rejected_by_human")
    loop, ctx, seeded = _run_strategy(app, root, "cand")
    _expect_rows(conn, ctx, seeded, "duplicate_logic")
    assert f"quality_row_skipped\tmission={ctx.mission_id} row=archive:{bad}" \
        in _activity(root).replace(" ", "\t", 0) or \
        f"row=archive:{bad}" in _activity(root)


def test_ac18_a_corrupted_row_alone_with_a_usable_base_does_not_block(
        improve_env):
    app, root = improve_env
    conn = app.conn_core
    _decided_row(conn, name="broken", logic_hash="garbage",
                 outcome="rejected_by_human")
    other = _deployed("alpha", logic_hash="v1:" + "5" * 64)
    loop, ctx, seeded = _run_strategy(
        app, root, "cand", extra=[_population_patch(deployed=[other])])
    _expect_rows(conn, ctx, seeded, "pending")
    assert "quality_row_skipped" in _activity(root)


def test_ac18_when_no_comparison_row_is_usable_the_candidate_is_gate_failed_and_reported(
        improve_env):
    app, root = improve_env
    conn = app.conn_core
    _decided_row(conn, name="broken", logic_hash="garbage",
                 outcome="rejected_by_human")
    loop, ctx, seeded = _run_strategy(app, root, "cand")
    _expect_rows(conn, ctx, seeded, "gate_failed")
    assert _approval_count(conn) == 0
    assert _last_result(conn) == f"observation:{NOTICE}"
    assert "quality_check_unavailable" in _activity(root)


def test_ac18_a_database_read_failure_is_gate_failed_and_reported(improve_env):
    app, root = improve_env
    conn = app.conn_core

    def broken(conn_, *, mission_id):
        raise sqlite3.OperationalError("disk I/O error")

    loop, ctx, seeded = _run_strategy(
        app, root, "cand",
        extra=[patch.object(quality, "load_archive", broken)])
    _expect_rows(conn, ctx, seeded, "gate_failed")
    assert _approval_count(conn) == 0
    assert "archive_read_failed" in _activity(root)


def test_ac18_an_empty_population_is_not_a_failure(improve_env):
    """初回起動 (配備済み・example・台帳の行がどれも無い) は通る。"""
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, seeded = _run_strategy(app, root, "cand")
    _expect_rows(conn, ctx, seeded, "pending")


def test_demotion_details_never_reach_the_agent_surface(improve_env):
    """降格の通知 (backlog の last_result) は固定文言だけ。他候補の名前・hash
    は activity にだけ残る。"""
    app, root = improve_env
    conn = app.conn_core
    past_hash = _hash_of(PY_X)
    _decided_row(conn, name="secret_past_name", logic_hash=past_hash,
                 outcome="rejected_by_human")
    loop, ctx, seeded = _run_strategy(app, root, "cand")
    result = _last_result(conn)
    assert result == f"observation:{NOTICE}"
    assert "secret_past_name" not in result and past_hash not in result
    log = _activity(root)
    assert "quality_demoted" in log and "secret_past_name" in log


def test_duplicate_metrics_details_never_reach_the_agent_surface(improve_env):
    """成績一致の降格も固定文言だけ。一致した版の hash と pair は activity
    にだけ残る。"""
    app, root = improve_env
    conn = app.conn_core
    _run_strategy(app, root, "conv_a", py=PY_X)
    approved_hash = conn.execute(
        "SELECT content_hash FROM backtest_runs "
        "WHERE scope='in_sample' AND variant='candidate' "
        "ORDER BY id LIMIT 1").fetchone()[0]
    loop, ctx, seeded = _run_strategy(app, root, "conv_b", py=PY_Y)
    _expect_rows(conn, ctx, seeded, "duplicate_metrics")
    result = _last_result(conn)
    assert result == f"observation:{NOTICE}"
    assert approved_hash not in result
    assert "pair=" not in result and "duplicate_metrics_of" not in result
    log = _activity(root)
    assert "quality_demoted" in log
    assert f"duplicate_metrics_of:{approved_hash}" in log


# --- 配備切替との順序 (配備済みの母集団は lock 下の live のファイルが正) -------

def _record_disable(conn, name):
    """配備の無効化を DB に完了済みで記録する (配備切替の journal と同じ DB)。"""
    from agentic_fx.store import plugin_deploy_ops as deploy_ops
    op_id = deploy_ops.insert_intent(
        conn, op_kind="disable_symlink", name=name, from_kind="symlink",
        to_kind="absent", from_target=f".versions/{name}/{'b' * 64}",
        to_target=None, retired_path=None, subject_content_hash="c" * 64,
        subject_artifact_hash="a" * 64, target_content_hash=None,
        target_artifact_hash=None, approval_id=None, revoke=False,
        revoke_row_id=None, force=False, actor="t", now=NOW)
    assert deploy_ops.set_phase(conn, op_id, "done", now=NOW)
    return op_id


def _place_link(root, name, *, py, config=_PASSING_STRATEGY_CONFIG,
                version="b"):
    """版 dir を置いて live を symlink にする (DB には触れない)。"""
    target = f".versions/{name}/{version * 64}"
    version_dir = root / "plugins" / target
    version_dir.mkdir(parents=True)
    (version_dir / "plugin.py").write_text(py)
    (version_dir / "config.yaml").write_text(config)
    (root / "plugins" / name).symlink_to(target)
    return target


def _place_plain(root, name, *, py, config=_PASSING_STRATEGY_CONFIG):
    """人が live に直接 plain dir を置く。"""
    live = root / "plugins" / name
    live.mkdir(parents=True)
    (live / "plugin.py").write_text(py)
    (live / "config.yaml").write_text(config)


def _record_deploy(conn, root, name, *, py, config=_PASSING_STRATEGY_CONFIG):
    """版ディレクトリと live の symlink を置き、承認の配備切替が完了した記録を
    DB に残す。"""
    from agentic_fx.store import plugin_switch_journal as journal
    target = _place_link(root, name, py=py, config=config)
    op_id = journal.insert(
        conn, kind="approve", approval_id=1, name=name, old_kind="absent",
        old_target=None, temp_path="tmp", new_target=target,
        switch_required=True, actor="t", now=NOW)
    assert journal.set_phase(conn, op_id, "decided", now=NOW)
    return op_id


def _remove_live(root, name):
    (root / "plugins" / name).unlink()


def test_a_disable_between_prepare_and_the_tx_removes_the_same_name_exception(
        improve_env):
    """mission 開始時は配備されていた同名が、書き込みロックを取る前に無効化
    された。配備済みの同名ではないので、承認済みの同名行は更新として扱われず
    duplicate_logic で拒否される。"""
    app, root = improve_env
    conn = app.conn_core
    _decided_row(conn, name="cand", logic_hash=_hash_of(PY_X),
                 outcome="approved")
    _record_deploy(conn, root, "cand", py=PY_X)

    def disable(loop, ctx):
        _remove_live(root, "cand")
        _record_disable(conn, "cand")

    loop, ctx, seeded = _run_strategy(
        app, root, "cand", before_commit=disable)
    _expect_rows(conn, ctx, seeded, "duplicate_logic")
    assert _approval_count(conn) == 0


def test_the_same_name_exception_holds_while_the_name_stays_deployed(
        improve_env):
    app, root = improve_env
    conn = app.conn_core
    _decided_row(conn, name="cand", logic_hash=_hash_of(PY_X),
                 outcome="approved")
    _record_deploy(conn, root, "cand", py=PY_X)
    loop, ctx, seeded = _run_strategy(app, root, "cand")
    _expect_rows(conn, ctx, seeded, "pending")


def test_a_deploy_between_prepare_and_the_tx_rejects_a_same_logic_candidate(
        improve_env):
    """mission 開始後に別名で配備された同ロジックを、lock 下の live で見て拒否する。"""
    app, root = improve_env
    conn = app.conn_core
    loop, ctx, seeded = _run_strategy(
        app, root, "cand", before_commit=lambda loop, ctx: _record_deploy(
            conn, root, "other", py=PY_X))
    _expect_rows(conn, ctx, seeded, "duplicate_logic")
    assert "matched=deployed:other" in _activity(root)


def test_a_plugin_placed_by_hand_after_a_disable_still_counts_in_the_population(
        improve_env):
    """disable の記録 (absent) の後に人が同名を直接置いた。実在するので別名の
    同ロジック候補は duplicate_logic になり、食い違いは activity に残る。"""
    app, root = improve_env
    conn = app.conn_core
    _record_deploy(conn, root, "other", py=PY_X)
    _remove_live(root, "other")
    _record_disable(conn, "other")
    _place_plain(root, "other", py=PY_X)
    loop, ctx, seeded = _run_strategy(app, root, "cand")
    _expect_rows(conn, ctx, seeded, "duplicate_logic")
    log = _activity(root)
    assert "matched=deployed:other" in log
    assert "quality_population_drift" in log and "name=other" in log


def test_a_recorded_plugin_replaced_by_hand_is_compared_by_its_new_version(
        improve_env):
    app, root = improve_env
    conn = app.conn_core
    _record_deploy(conn, root, "other", py=PY_Y)
    _remove_live(root, "other")
    _place_plain(root, "other", py=PY_X)
    loop, ctx, seeded = _run_strategy(app, root, "cand")
    _expect_rows(conn, ctx, seeded, "duplicate_logic")
    assert "quality_population_drift" in _activity(root)


def test_a_recorded_plugin_without_drift_leaves_no_drift_activity(
        improve_env):
    app, root = improve_env
    conn = app.conn_core
    _record_deploy(conn, root, "other", py=PY_Y)
    loop, ctx, seeded = _run_strategy(app, root, "cand")
    _expect_rows(conn, ctx, seeded, "pending")
    assert "quality_population_drift" not in _activity(root)


def test_a_deploy_operation_still_in_flight_makes_the_check_unavailable(
        improve_env):
    app, root = improve_env
    conn = app.conn_core
    from agentic_fx.store import plugin_deploy_ops as deploy_ops

    def open_op(loop, ctx):
        deploy_ops.insert_intent(
            conn, op_kind="disable_symlink", name="alpha", from_kind="symlink",
            to_kind="absent", from_target=f".versions/alpha/{'a' * 64}",
            to_target=None, retired_path=None, subject_content_hash="c" * 64,
            subject_artifact_hash="a" * 64, target_content_hash=None,
            target_artifact_hash=None, approval_id=None, revoke=False,
            revoke_row_id=None, force=False, actor="t", now=NOW)

    loop, ctx, seeded = _run_strategy(
        app, root, "cand", before_commit=open_op)
    _expect_rows(conn, ctx, seeded, "gate_failed")
    assert _approval_count(conn) == 0
    assert _last_result(conn) == f"observation:{NOTICE}"
    assert "quality_check_unavailable" in _activity(root)


# --- 配備ガード (配備操作と母集団の確定は排他) --------------------------------

def _hold_deploy_lock(root, *, held, release, then=None):
    """配備操作の代役。plugin lock (= 共有の配備ガード) を取ったまま `release`
    が立つまで保持し、`then` を lock の中で実行してから放す。別 thread で動かす。"""
    import threading
    from agentic_fx.plugin import switch

    def run():
        with switch._plugin_lock(root / "plugins", "other"):
            held.set()
            release.wait(30)
            if then is not None:
                then()

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def test_the_quality_check_waits_for_a_running_deploy_and_sees_its_result(
        improve_env):
    """配備操作が lock を保持している間、質検査はガードの取得で待つ。配備が
    済んだ後の live (別名の同ロジック) を見て duplicate_logic になる。"""
    import threading
    import time as _time
    from agentic_fx.plugin import switch
    app, root = improve_env
    conn = app.conn_core
    held, release = threading.Event(), threading.Event()
    threads = []
    waited = {}
    real = switch.deploy_guard

    def timed_guard(*a, **kw):
        if kw.get("exclusive"):
            waited["start"] = _time.monotonic()
            threading.Timer(0.5, release.set).start()
        return real(*a, **kw)

    def start(loop, ctx):
        threads.append(_hold_deploy_lock(
            root, held=held, release=release,
            then=lambda: _place_link(root, "other", py=PY_X)))
        assert held.wait(5)

    def stamp(self, ctx):
        waited["entered"] = _time.monotonic()
        return real_load(self, ctx)

    real_load = ImproveLoop._load_quality_population
    with patch.object(switch, "deploy_guard", timed_guard), \
            patch.object(ImproveLoop, "_load_quality_population", stamp):
        loop, ctx, seeded = _run_strategy(
            app, root, "cand", before_commit=start)
    threads[0].join(10)
    assert waited["entered"] - waited["start"] >= 0.4
    _expect_rows(conn, ctx, seeded, "duplicate_logic")
    assert "matched=deployed:other" in _activity(root)


def test_a_deploy_lock_held_past_the_timeout_makes_the_check_unavailable(
        improve_env, monkeypatch):
    import threading
    app, root = improve_env
    conn = app.conn_core
    monkeypatch.setattr(quality, "DEPLOY_GUARD_TIMEOUT_S", 0.3)
    held, release = threading.Event(), threading.Event()
    threads = []

    def start(loop, ctx):
        threads.append(_hold_deploy_lock(root, held=held, release=release))
        assert held.wait(5)

    try:
        loop, ctx, seeded = _run_strategy(
            app, root, "cand", before_commit=start)
    finally:
        release.set()
    threads[0].join(10)
    _expect_rows(conn, ctx, seeded, "gate_failed")
    assert _approval_count(conn) == 0
    assert _last_result(conn) == f"observation:{NOTICE}"
    log = _activity(root)
    assert "quality_check_unavailable" in log
    assert "deploy_guard_unavailable" in log


def test_a_deploy_started_during_the_quality_check_waits_until_it_ends(
        improve_env):
    """質検査が母集団を確定してから tx を閉じるまでの間に始まった配備操作は、
    その間は lock を取れず、質検査の後に完了する。"""
    import threading
    import time as _time
    from agentic_fx.plugin import switch
    app, root = improve_env
    conn = app.conn_core
    seen = {}
    thread_box = []
    real = ImproveLoop._load_quality_population

    def load_then_start_deploy(self, ctx):
        population = real(self, ctx)

        def deploy():
            with switch._plugin_lock(root / "plugins", "other"):
                _place_link(root, "other", py=PY_X)

        thread = threading.Thread(target=deploy)
        thread.start()
        thread_box.append(thread)
        _time.sleep(0.5)
        seen["placed_during_check"] = (root / "plugins" / "other").exists()
        return population

    with patch.object(ImproveLoop, "_load_quality_population",
                      load_then_start_deploy):
        loop, ctx, seeded = _run_strategy(app, root, "cand")
    thread_box[0].join(10)
    assert seen["placed_during_check"] is False
    _expect_rows(conn, ctx, seeded, "pending")
    assert (root / "plugins" / "other").is_symlink()


def test_the_deploy_guard_is_taken_before_begin_and_released_after_the_tx(
        improve_env):
    """取得順は ガード → BEGIN IMMEDIATE、放すのは COMMIT の後 (配備操作側の
    ガード → plugin lock → DB と同じ向き)。"""
    from agentic_fx.plugin import switch
    app, root = improve_env
    events: list[str] = []
    real = switch.deploy_guard

    class Spy:
        def __init__(self, *a, **kw):
            self._cm = real(*a, **kw)
            self._exclusive = kw.get("exclusive")

        def __enter__(self):
            self._cm.__enter__()
            events.append(f"guard_enter:{self._exclusive}")

        def __exit__(self, *exc):
            events.append("guard_exit")
            return self._cm.__exit__(*exc)

    def trace(statement):
        head = statement.strip().upper()
        if head.startswith("BEGIN") or head.startswith("COMMIT"):
            events.append(head.split()[0] + (
                " IMMEDIATE" if "IMMEDIATE" in head else ""))

    original = ImproveLoop._finalize_success

    def traced(self, conn_, *a, **kw):
        conn_.set_trace_callback(trace)
        try:
            return original(self, conn_, *a, **kw)
        finally:
            conn_.set_trace_callback(None)

    with patch.object(switch, "deploy_guard", Spy), \
            patch.object(ImproveLoop, "_finalize_success", traced):
        _run_strategy(app, root, "cand")
    first_guard = events.index("guard_enter:True")
    assert "BEGIN IMMEDIATE" not in events[:first_guard]
    begin = events.index("BEGIN IMMEDIATE", first_guard)
    assert first_guard < begin
    last_commit = max(i for i, e in enumerate(events) if e == "COMMIT")
    assert events.index("guard_exit", first_guard) > last_commit


# --- 母集団の読込失敗 ---------------------------------------------------------

def test_a_file_population_read_failure_is_gate_failed_and_reported(
        improve_env):
    """example の列挙が OSError で失敗しても、最終 tx で gate_failed を確定
    し、固定文言と activity を残す。"""
    app, root = improve_env
    conn = app.conn_core
    real_load_examples = quality.load_examples

    def unreadable(examples_dir):
        def broken(self):
            raise PermissionError(13, "denied")
        with patch.object(Path, "iterdir", broken):
            return real_load_examples(examples_dir)

    with patch.object(quality, "load_examples", unreadable):
        loop, ctx, seeded = _run_strategy(app, root, "cand")
    _expect_rows(conn, ctx, seeded, "gate_failed")
    assert _approval_count(conn) == 0
    assert _last_result(conn) == f"observation:{NOTICE}"
    log = _activity(root)
    assert "quality_check_unavailable" in log and "PermissionError" in log


# --- 実配線 (母集団・identity・検出属性を差し替えずに通す) --------------------

def test_family_detection_runs_through_the_real_population_and_settle(
        improve_env):
    """配備済み plugin を実ファイルで置き、`phase1_metas` → 母集団 →
    `identity_of_dir` → `settle(add_flags)` を差し替えずに通す。構造の違う
    候補が同じ仮説族として検出属性を持って提出される。"""
    from tests.fixtures import indicator_wiring as fx
    from agentic_fx.plugin.logic_identity import family_key_of
    app, root = improve_env
    conn = app.conn_core
    plugins_root = root / "plugins"
    fx.write_indicator(plugins_root, "rsi")
    pin = fx.deploy_approved(conn, plugins_root, ["rsi"], now=NOW)["rsi"]
    config = (_PASSING_STRATEGY_CONFIG
              + f"indicators:\n  rsi:\n    plugin: rsi\n"
                f"    params: {{period: 14}}\n    pin: {pin}\n")
    alpha = plugins_root / "alpha"
    alpha.mkdir(parents=True)
    (alpha / "plugin.py").write_text(PY_Y)
    (alpha / "config.yaml").write_text(config)
    (alpha / "test_plugin.py").write_text(_PASSING_STRATEGY_TEST)
    fx.deploy_approved(conn, plugins_root, ["alpha"], now=NOW)
    output = _artifact("cand")
    loop, ctx, seeded = _run(
        app, root, output, files=("cand", PY_X, config,
                                  _PASSING_STRATEGY_TEST),
        in_sample=_fake_in_sample_full_metrics(METRICS_1),
        holdout_fn=_fake_holdout_full_metrics(HOLDOUT), seed=False)
    row = _expect_rows(conn, ctx, seeded, "pending")
    assert row["quality_flags"] == ["family_detected"]
    assert row["family_key"] == family_key_of(config, name="cand")
    assert row["family_key"] == "strategy:rsi@1h@USDJPY"
    assert _payload(conn)["candidate_archive_id"] == row["id"]


def test_concurrent_same_logic_candidates_only_one_reaches_approval(
        improve_env):
    """別 slot (別スレッド・別接続) で、成績が違うので段 M には掛からないが
    ロジックは同じ 2 本が同時に最終化されても、先に書き込みロックを取った
    側の pending 行を後の側が読み、後の側は duplicate_logic になる。段 L が
    書き込みロックの外で行われると 2 本とも通る。"""
    import threading
    from agentic_fx.runners.base import MissionResult
    from tests.loops.test_improve_e2e import (
        FakeImproveWorkerRunner, _write_staging_plugin)
    from tests.loops.test_improve_loop_duplicate_metrics import _make_loop

    app, root = improve_env
    conn = app.conn_core
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    names = ("race_a", "race_b")
    metrics = {"race_a": METRICS_1, "race_b": METRICS_2}
    results = {
        name: MissionResult(status="completed", output=_artifact(name),
                            transcript=[])
        for name in names}

    def runner_factory(**kw):
        return FakeImproveWorkerRunner(
            result=results[threading.current_thread().name], **kw)

    def in_sample(settings, *, history_conn, record_fn, **kwargs):
        return _fake_in_sample_full_metrics(
            metrics[threading.current_thread().name])(
                settings, history_conn=history_conn, record_fn=record_fn,
                **kwargs)

    original = ImproveLoop._finalize_success

    def synced(self, *a, **kw):
        barrier.wait(timeout=10)
        return original(self, *a, **kw)

    def work(name):
        try:
            loop = _make_loop(app, root)
            mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
            _write_staging_plugin(
                ctx.staging_dir, name, PY_X, _PASSING_STRATEGY_CONFIG,
                _PASSING_STRATEGY_TEST)
            loop.commit(mission=mission, ctx=ctx, result=worker.run(mission),
                        now=NOW)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,), name=n)
               for n in names]
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               runner_factory), \
         patch("agentic_fx.loops.improve_loop.holdout.run_in_sample",
               in_sample), \
         patch("agentic_fx.loops.improve_loop.holdout.run_holdout_gate",
               _fake_holdout_full_metrics(HOLDOUT)), \
         patch.object(ImproveLoop, "_finalize_success", synced):
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    assert errors == [], f"thread(s) raised: {errors!r}"
    assert _approval_count(conn) == 1
    outcomes = sorted(r[0] for r in conn.execute(
        "SELECT outcome_class FROM candidate_archives "
        "WHERE name IN ('race_a', 'race_b') AND outcome_class != "
        "'not_submitted'"))
    assert outcomes == ["duplicate_logic", "pending"]
