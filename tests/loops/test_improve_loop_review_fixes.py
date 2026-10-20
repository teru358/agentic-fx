"""最終 artifact の材料と提出 tx まわりの境界を固定する。"""
from __future__ import annotations

from unittest.mock import patch

from tests.loops.test_improve_candidate_ledger import (
    _PASSING_INDICATOR_CONFIG, _PASSING_INDICATOR_PY,
    _PASSING_INDICATOR_TEST, _expect_rows, _indicator, _plugin_artifact,
    _run, improve_env)


def test_deep_expression_candidate_does_not_fail_the_mission(improve_env):
    """ロジック hash を計算できない候補は、hash なしの行として通常の終端に進む。"""
    app, root = improve_env
    deep = "_deep = " + " + ".join(["1"] * 3000) + "\n"
    loop, ctx, seeded = _run(
        app, root, _plugin_artifact("cand"),
        files=("cand", _PASSING_INDICATOR_PY + deep, _PASSING_INDICATOR_CONFIG,
               _PASSING_INDICATOR_TEST))
    status = app.conn_core.execute(
        "SELECT status FROM missions WHERE id=?", (ctx.mission_id,)
    ).fetchone()[0]
    assert status == "completed"
    extra = _expect_rows(app.conn_core, ctx, seeded, "pending")
    assert extra["logic_hash"] is None


def test_submission_reads_the_candidate_identity_once_outside_the_tx(
        improve_env):
    """最終 artifact の材料は gate 判定の直後に 1 回だけ作り、承認 tx の中で
    staging を読み直さない。"""
    import agentic_fx.plugin.logic_identity as li
    calls = []
    real = li.read_identity

    def counting(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    app, root = improve_env
    loop, ctx, seeded = _run(
        app, root, _plugin_artifact("cand"), files=_indicator(),
        extra=[patch.object(li, "read_identity", counting)])
    _expect_rows(app.conn_core, ctx, seeded, "pending")
    assert len(calls) == 1


def test_final_candidate_parses_config_yaml_once(improve_env, tmp_path):
    import yaml
    from agentic_fx.plugin.gate_pytest import hashes_of
    from tests.loops.test_improve_candidate_ledger import _gate_dir, _loop
    app, root = improve_env
    loop = _loop(app, root)
    directory = _gate_dir(tmp_path)
    content_hash, artifact_hash = hashes_of(directory)
    real = yaml.safe_load
    count = []

    def counting(*args, **kwargs):
        count.append(1)
        return real(*args, **kwargs)

    with patch.object(yaml, "safe_load", counting):
        loop._final_candidate(
            directory, name="cand", content_hash=content_hash,
            artifact_hash=artifact_hash)
    assert len(count) == 1


def test_finalize_success_takes_the_pair_from_the_given_final_candidate(
        improve_env, tmp_path):
    """tx の中は渡された最終 artifact の材料だけを使う。staging の中身が
    違っても行の pair は材料のもの。"""
    from agentic_fx.plugin.gate_pytest import hashes_of
    from tests.loops.test_improve_candidate_ledger import _gate_dir, _loop
    from tests.loops.test_improve_candidate_ledger_pins import _STRATEGY_CONFIG
    app, root = improve_env
    loop = _loop(app, root)
    directory = _gate_dir(tmp_path)
    (directory / "config.yaml").write_text(_STRATEGY_CONFIG, encoding="utf-8")
    content_hash, artifact_hash = hashes_of(directory)
    candidate = loop._final_candidate(
        directory, name="cand", content_hash=content_hash,
        artifact_hash=artifact_hash)
    assert candidate.pair == "USDJPY"
    payload = {"content_hash": content_hash, "artifact_hash": artifact_hash,
               "name": "cand"}
    ctx = type("C", (), {"staging_dir": tmp_path / "gate"})()
    with patch.object(loop, "_final_candidate",
                      side_effect=AssertionError("re-read in tx")):
        submitted = loop._submitted_candidate(payload, candidate)
    assert submitted is candidate
    fallback = loop._submitted_candidate(payload, None)
    assert (fallback.pair, fallback.logic_hash, fallback.candidate_dir) == (
        None, None, None)
    assert fallback.artifact_hash == artifact_hash
