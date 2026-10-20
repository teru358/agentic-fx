"""最終 artifact の材料と gate の最終 hash の境界を 1 つずつ固定する。"""
from __future__ import annotations

from unittest.mock import patch

from agentic_fx.loops.improve_loop import ImproveLoop
from agentic_fx.plugin.gate_pytest import GateResult, hashes_of

from tests.loops.test_improve_candidate_ledger import (
    _assert_terminal, _expect_rows, _gate_dir, _indicator, _loop,
    _plugin_artifact, _run, improve_env)

_STRATEGY_CONFIG = """
kind: strategy
pairs: [USDJPY, EURUSD]
timeframe: 1h
exit_mode: levels
params: {}
"""


def test_final_candidate_takes_the_first_pair_of_a_strategy(
        improve_env, tmp_path):
    app, root = improve_env
    loop = _loop(app, root)
    directory = _gate_dir(tmp_path)
    (directory / "config.yaml").write_text(_STRATEGY_CONFIG, encoding="utf-8")
    content_hash, artifact_hash = hashes_of(directory)
    candidate = loop._final_candidate(
        directory, name="cand", content_hash=content_hash,
        artifact_hash=artifact_hash)
    assert candidate.pair == "USDJPY"


def test_final_candidate_is_none_without_a_final_hash(improve_env, tmp_path):
    app, root = improve_env
    loop = _loop(app, root)
    directory = _gate_dir(tmp_path)
    content_hash, artifact_hash = hashes_of(directory)
    assert loop._final_candidate(
        directory, name="cand", content_hash=None,
        artifact_hash=artifact_hash) is None
    assert loop._final_candidate(
        directory, name="cand", content_hash=content_hash,
        artifact_hash=None) is None
    assert loop._final_candidate(
        directory, name="cand", content_hash="", artifact_hash="") is None


def test_gate_fails_when_only_the_self_test_changes_during_a_passing_run(
        improve_env, tmp_path, monkeypatch):
    """pytest が通っても、実行中に self-test (artifact_hash だけに入る) が
    書き換えられたなら不合格で、verdict は書き換え後の hash を持つ。"""
    app, root = improve_env
    loop = _loop(app, root)
    directory = _gate_dir(tmp_path)
    before = hashes_of(directory)

    def fake(plugin_dir, *, settings):
        (plugin_dir / "test_plugin.py").write_text(
            (plugin_dir / "test_plugin.py").read_text() + "\n# edited\n",
            encoding="utf-8")
        return GateResult(True, 0, "99 passed in 0.1s", 0.0)

    monkeypatch.setattr("agentic_fx.loops.improve_loop.run_gate_pytest", fake)
    verdict = loop._run_plugin_gate(
        directory, name="cand", source_snapshot_dir=tmp_path / "snap")
    assert verdict.passed is False
    assert verdict.reason.startswith("hash changed after pytest")
    assert verdict.content_hash == before[0]
    assert verdict.artifact_hash == hashes_of(directory)[1] != before[1]


def test_a_failing_ledger_write_does_not_change_the_missions_verdict(
        improve_env):
    """提出に至らない終端で最終行の作成が失敗しても、mission の裁定 (gate 不合格
    の終端と backlog の遷移) は落ちず、行は作られず、activity に残る。"""
    app, root = improve_env

    def boom(self, *args, **kwargs):
        raise RuntimeError("injected ledger failure")

    loop, ctx, seeded = _run(
        app, root, _plugin_artifact("cand"), files=_indicator(),
        extra=[patch("agentic_fx.loops.improve_loop.find_noop_copy",
                     lambda *a, **k: "_examples/rsi"),
               patch.object(ImproveLoop, "_ensure_final_archive_row", boom)])
    _expect_rows(app.conn_core, ctx, seeded, None)
    _assert_terminal(app.conn_core, ctx)
    assert app.conn_core.execute(
        "SELECT status FROM missions WHERE id=?",
        (ctx.mission_id,)).fetchone()[0] == "completed"
    assert any("candidate_settle_failed" in line
               for line in app.activity.tail(10_000))


def test_reusing_an_existing_row_does_not_create_a_snapshot_directory(
        improve_env):
    """同じ artifact の行が既にあれば再利用するだけで、archive を新たに作らない
    (行の archive_path は変えないので、作ると台帳の外に孤立した dir が残る)。"""
    from datetime import datetime, timezone
    from agentic_fx.store import candidate_archives as archives
    from tests.loops.test_improve_candidate_ledger import (
        _fake_holdout_metrics, _fake_in_sample_metrics, _strategy)
    app, root = improve_env
    conn = app.conn_core
    seeded_box = []

    def seed_exact(loop, ctx):
        _, artifact_hash = hashes_of(ctx.staging_dir / "cand")
        seeded_box.append(artifact_hash)
        archives.insert(
            conn, mission_id=ctx.mission_id, name="cand",
            content_hash="c-exact", artifact_hash=artifact_hash,
            archive_path=None, pair="USDJPY", metrics={"pf": 1.4},
            now=datetime(2026, 10, 19, tzinfo=timezone.utc), commit=True)

    loop, ctx, seeded = _run(
        app, root, _plugin_artifact("cand", kind="strategy"),
        files=_strategy(), in_sample=_fake_in_sample_metrics(40),
        holdout_fn=_fake_holdout_metrics(), before_commit=seed_exact)
    assert not (root / "plugins" / "_archive" / str(ctx.mission_id)
                / seeded_box[0]).exists()
