"""最終 artifact の材料と提出 tx まわりの境界を固定する。"""
from __future__ import annotations

from unittest.mock import patch

from agentic_fx.loops.improve_loop import ImproveLoop

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


def _snapshot_setup(improve_env, tmp_path):
    from agentic_fx.plugin.gate_pytest import hashes_of
    from tests.loops.test_improve_candidate_ledger import _gate_dir, _loop
    app, root = improve_env
    loop = _loop(app, root)
    directory = _gate_dir(tmp_path)
    content_hash, artifact_hash = hashes_of(directory)
    candidate = loop._final_candidate(
        directory, name="cand", content_hash=content_hash,
        artifact_hash=artifact_hash)
    final_path = root / "plugins" / "_archive" / "7" / artifact_hash
    return loop, candidate, final_path


def _verified(path, artifact_hash):
    from agentic_fx.plugin.version_store import artifact_hash_bytes
    return artifact_hash_bytes(
        (path / "plugin.py").read_bytes(), (path / "config.yaml").read_bytes(),
        (path / "test_plugin.py").read_bytes()) == artifact_hash


def test_snapshot_builds_a_fresh_archive_over_an_incomplete_same_name_dir(
        improve_env, tmp_path):
    from tests.loops.test_improve_e2e import NOW
    loop, candidate, final_path = _snapshot_setup(improve_env, tmp_path)
    final_path.mkdir(parents=True)
    (final_path / "plugin.py").write_text("partial\n")
    relative = loop._snapshot_final_archive(candidate, mission_id=7, now=NOW)
    assert relative is not None
    assert _verified(final_path, candidate.artifact_hash)
    stale = [p for p in final_path.parent.iterdir()
             if p.name.startswith(".stale-")]
    assert len(stale) == 1


def test_snapshot_builds_a_fresh_archive_over_a_different_content_dir(
        improve_env, tmp_path):
    from tests.loops.test_improve_e2e import NOW
    loop, candidate, final_path = _snapshot_setup(improve_env, tmp_path)
    final_path.mkdir(parents=True)
    for name in ("plugin.py", "config.yaml", "test_plugin.py"):
        (final_path / name).write_text("other\n")
    relative = loop._snapshot_final_archive(candidate, mission_id=7, now=NOW)
    assert relative is not None
    assert _verified(final_path, candidate.artifact_hash)
    assert (final_path / "meta.json").is_file()


def test_snapshot_reuses_a_valid_same_name_dir(improve_env, tmp_path):
    from tests.loops.test_improve_e2e import NOW
    loop, candidate, final_path = _snapshot_setup(improve_env, tmp_path)
    assert loop._snapshot_final_archive(
        candidate, mission_id=7, now=NOW) is not None
    inode = final_path.stat().st_ino
    assert loop._snapshot_final_archive(
        candidate, mission_id=7, now=NOW) is not None
    assert final_path.stat().st_ino == inode
    assert not [p for p in final_path.parent.iterdir()
                if p.name.startswith(".stale-")]


def test_archive_is_made_before_the_tx_and_survives_a_rollback(improve_env):
    """archive の作成は書き込み tx の外。tx が rollback しても dir は残り
    (孤立 dir は既存の sweep が回収する)、台帳の行は残らない。"""
    import sqlite3
    from agentic_fx.store.db import connect
    app, root = improve_env
    write_lock_free = []
    real = ImproveLoop._snapshot_final_archive

    def spy(self, candidate, **kwargs):
        probe = connect(root / "data" / "agentic.db")
        try:
            probe.execute("PRAGMA busy_timeout=0")
            probe.execute("BEGIN IMMEDIATE")
            probe.execute("ROLLBACK")
            write_lock_free.append(True)
        except sqlite3.OperationalError:
            write_lock_free.append(False)
        finally:
            probe.close()
        return real(self, candidate, **kwargs)

    loop, ctx, seeded = _run(
        app, root, _plugin_artifact("cand"), files=_indicator(),
        extra=[patch.object(ImproveLoop, "_snapshot_final_archive", spy),
               patch("agentic_fx.store.candidate_archives.settle",
                     lambda conn, archive_id, outcome: False)])
    assert write_lock_free == [True]
    archive_root = root / "plugins" / "_archive" / str(ctx.mission_id)
    finals = [p for p in archive_root.iterdir() if not p.name.startswith(".")]
    assert len(finals) == 1
    rows = app.conn_core.execute(
        "SELECT id FROM candidate_archives WHERE mission_id=? AND "
        "archive_path IS NOT NULL", (ctx.mission_id,)).fetchall()
    assert rows == []


def test_archive_tmp_is_written_by_one_helper_only():
    """tmp への書き込み手順 (4 ファイル・fsync・ro 化) は 1 つの関数だけが持つ。"""
    import inspect
    from agentic_fx.loops import improve_loop
    source = inspect.getsource(improve_loop)
    helper = inspect.getsource(improve_loop.ImproveLoop._write_archive_tmp)
    assert source.count("_write_ro_file(") == helper.count("_write_ro_file(")


def test_snapshot_goes_through_the_shared_tmp_writer(improve_env, tmp_path):
    from tests.loops.test_improve_e2e import NOW
    loop, candidate, final_path = _snapshot_setup(improve_env, tmp_path)
    calls = []
    real = ImproveLoop._write_archive_tmp

    def spy(self, *args, **kwargs):
        calls.append(1)
        return real(self, *args, **kwargs)

    with patch.object(ImproveLoop, "_write_archive_tmp", spy):
        loop._snapshot_final_archive(candidate, mission_id=7, now=NOW)
    assert calls == [1]
