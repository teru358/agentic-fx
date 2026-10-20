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
                  holdout=HOLDOUT, extra=(), seed=False, idea=None):
    return _run(
        app, root, _artifact(name, idea),
        files=_files(name, py), in_sample=_fake_in_sample_full_metrics(metrics),
        holdout_fn=_fake_holdout_full_metrics(holdout), extra=extra, seed=seed)


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
    file_population = quality.FilePopulation(
        deployed=quality.load_deployed(inventory.phase1_metas),
        examples=quality.Population())
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
