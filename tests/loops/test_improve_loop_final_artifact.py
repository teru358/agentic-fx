"""最終 artifact の材料と提出 tx まわりの境界を固定する。"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from agentic_fx.loops.improve_loop import ImproveLoop

from tests.loops.test_improve_candidate_ledger import (
    _PASSING_INDICATOR_CONFIG, _PASSING_INDICATOR_PY,
    _PASSING_INDICATOR_TEST, _expect_rows, _indicator, _plugin_artifact,
    _run, improve_env)


def test_candidate_without_computable_identity_is_gate_failed(improve_env):
    """ロジック hash を計算できない候補は承認に進めず、gate_failed で終端する。"""
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
    assert app.conn_core.execute(
        "SELECT COUNT(*) FROM approval_requests").fetchone()[0] == 0
    extra = _expect_rows(app.conn_core, ctx, seeded, "gate_failed")
    assert extra["logic_hash"] is None
    last = app.conn_core.execute(
        "SELECT last_result FROM improvement_backlog ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    assert last == "gate_failed:identity_unavailable"


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
    relative, _ = loop._snapshot_final_archive(
        candidate, mission_id=7, now=NOW)
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
    relative, _ = loop._snapshot_final_archive(
        candidate, mission_id=7, now=NOW)
    assert relative is not None
    assert _verified(final_path, candidate.artifact_hash)
    assert (final_path / "meta.json").is_file()


def test_snapshot_reuses_a_valid_same_name_dir(improve_env, tmp_path):
    from tests.loops.test_improve_e2e import NOW
    loop, candidate, final_path = _snapshot_setup(improve_env, tmp_path)
    assert loop._snapshot_final_archive(
        candidate, mission_id=7, now=NOW) == (
            final_path.relative_to(loop._root).as_posix(), True)
    inode = final_path.stat().st_ino
    assert loop._snapshot_final_archive(
        candidate, mission_id=7, now=NOW)[1] is False
    assert final_path.stat().st_ino == inode
    assert not [p for p in final_path.parent.iterdir()
                if p.name.startswith(".stale-")]


def test_archive_is_made_before_the_tx_and_removed_on_rollback(improve_env):
    """archive の作成は書き込み tx の外。tx が rollback したら、この呼び出しが
    作った dir は消え、台帳の行も残らない。"""
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
    assert finals == []
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


def _finals(root, mission_id):
    archive_root = root / "plugins" / "_archive" / str(mission_id)
    if not archive_root.exists():
        return []
    return [p for p in archive_root.iterdir() if not p.name.startswith(".")]


def test_duplicate_metrics_demotion_leaves_no_orphan_archive_dir(improve_env):
    app, root = improve_env
    metrics = {"trades": 194, "pf": 1.4981, "win_rate": 0.487, "avg_r": 0.188,
               "max_drawdown": 0.0373, "total_pnl": 100.0}
    hold = {"trades": 53, "pf": 1.1, "win_rate": 0.3, "avg_r": 0.037,
            "max_drawdown": 0.0392, "total_pnl": 10.0}
    from tests.loops.test_improve_candidate_ledger import (
        _PASSING_STRATEGY_CONFIG, _PASSING_STRATEGY_PY,
        _PASSING_STRATEGY_TEST)
    from tests.loops.test_improve_loop_duplicate_metrics import (
        _fake_holdout_full_metrics, _fake_in_sample_full_metrics)
    runs = []
    for name in ("conv_a", "conv_b"):
        runs.append(_run(
            app, root, _plugin_artifact(name, kind="strategy"),
            files=(name, f'"""{name}"""\n' + _PASSING_STRATEGY_PY,
                   _PASSING_STRATEGY_CONFIG, _PASSING_STRATEGY_TEST),
            in_sample=_fake_in_sample_full_metrics(metrics),
            holdout_fn=_fake_holdout_full_metrics(hold)))
    (_, ctx_a, _), (_, ctx_b, seeded_b) = runs
    _expect_rows(app.conn_core, ctx_b, seeded_b, "duplicate_metrics")
    assert len(_finals(root, ctx_a.mission_id)) == 1
    assert _finals(root, ctx_b.mission_id) == []


def test_rollback_keeps_an_archive_dir_that_already_existed(improve_env):
    from agentic_fx.plugin.gate_pytest import hashes_of
    app, root = improve_env
    from tests.loops.test_improve_e2e import NOW
    pre = []

    def make_existing(loop, ctx):
        directory = ctx.staging_dir / "cand"
        content_hash, artifact_hash = hashes_of(directory)
        candidate = loop._final_candidate(
            directory, name="cand", content_hash=content_hash,
            artifact_hash=artifact_hash)
        assert loop._snapshot_final_archive(
            candidate, mission_id=ctx.mission_id, now=NOW)[0] is not None
        pre.append(artifact_hash)

    loop, ctx, seeded = _run(
        app, root, _plugin_artifact("cand"), files=_indicator(),
        before_commit=make_existing,
        extra=[patch("agentic_fx.store.candidate_archives.settle",
                     lambda conn, archive_id, outcome: False)])
    finals = _finals(root, ctx.mission_id)
    assert [p.name for p in finals] == pre


def test_orphan_cleanup_refuses_paths_outside_the_archive_root(
        improve_env, tmp_path):
    app, root = improve_env
    from tests.loops.test_improve_candidate_ledger import _loop
    loop = _loop(app, root)
    outside = tmp_path / "victim"
    outside.mkdir()
    (outside / "keep").write_text("x")
    loop._remove_created_archive(str(outside))
    assert (outside / "keep").exists()
    archive_dir = root / "plugins" / "_archive" / "9"
    archive_dir.mkdir(parents=True)
    link = archive_dir / "h"
    link.symlink_to(outside)
    loop._remove_created_archive("plugins/_archive/9/h")
    assert (outside / "keep").exists()
    assert link.is_symlink()


def _race_before_publish(final_path, make):
    """公開 helper の最初の呼び出しの直前に、別の実行者が宛先へ `make` を行う。"""
    real = ImproveLoop._publish_dir_noreplace
    ran = []

    def racing(src, dst):
        if str(dst) == str(final_path) and not ran:
            ran.append(True)
            make(src, dst)
        return real(src, dst)

    return racing, ran


def test_snapshot_does_not_claim_a_dir_a_racer_published_before_the_rename(
        improve_env, tmp_path):
    """公開の直前に別の実行者が同じ宛先へ正当な archive を作ると、
    呼び出しは作成者として扱われず、降格時の削除で他者の dir が残る。"""
    import shutil
    from tests.loops.test_improve_e2e import NOW
    loop, candidate, final_path = _snapshot_setup(improve_env, tmp_path)
    racing, ran = _race_before_publish(
        final_path, lambda src, dst: shutil.copytree(src, dst))
    with patch.object(ImproveLoop, "_publish_dir_noreplace",
                      staticmethod(racing)):
        relative, created = loop._snapshot_final_archive(
            candidate, mission_id=7, now=NOW)
    assert ran == [True]
    assert relative is not None and created is False
    assert _verified(final_path, candidate.artifact_hash)
    loop._remove_created_archive(relative if created else None)
    assert final_path.is_dir()
    assert not [p for p in final_path.parent.iterdir()
                if p.name.startswith(".tmp-")]


def test_snapshot_does_not_replace_an_empty_dir_a_racer_placed(
        improve_env, tmp_path):
    """宛先に他者の空 dir があっても rename で置換せず、壊れた先着として
    退避してから作り直す。退避した空 dir は残る。"""
    from tests.loops.test_improve_e2e import NOW
    loop, candidate, final_path = _snapshot_setup(improve_env, tmp_path)
    racing, ran = _race_before_publish(
        final_path, lambda src, dst: dst.mkdir())
    with patch.object(ImproveLoop, "_publish_dir_noreplace",
                      staticmethod(racing)):
        relative, created = loop._snapshot_final_archive(
            candidate, mission_id=7, now=NOW)
    assert ran == [True]
    assert relative is not None and created is True
    assert _verified(final_path, candidate.artifact_hash)
    stale = [p for p in final_path.parent.iterdir()
             if p.name.startswith(".stale-")]
    assert len(stale) == 1 and stale[0].is_dir()
    assert list(stale[0].iterdir()) == []
    assert not [p for p in final_path.parent.iterdir()
                if p.name.startswith(".tmp-")]


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_snapshot_moves_aside_a_file_or_symlink_a_racer_placed(
        improve_env, tmp_path, kind):
    from tests.loops.test_improve_e2e import NOW
    loop, candidate, final_path = _snapshot_setup(improve_env, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x")

    def make(src, dst):
        if kind == "file":
            dst.write_text("junk")
        else:
            dst.symlink_to(outside)

    racing, ran = _race_before_publish(final_path, make)
    with patch.object(ImproveLoop, "_publish_dir_noreplace",
                      staticmethod(racing)):
        relative, created = loop._snapshot_final_archive(
            candidate, mission_id=7, now=NOW)
    assert ran == [True]
    assert relative is not None and created is True
    assert _verified(final_path, candidate.artifact_hash)
    assert (outside / "keep").read_text() == "x"
    stale = [p for p in final_path.parent.iterdir()
             if p.name.startswith(".stale-")]
    assert len(stale) == 1


def test_snapshot_fails_closed_when_renameat2_is_unavailable(
        improve_env, tmp_path):
    from tests.loops.test_improve_e2e import NOW

    class _NoRenameat2:
        def __init__(self, *a, **k):
            pass

        def __getattr__(self, name):
            raise AttributeError(name)

    loop, candidate, final_path = _snapshot_setup(improve_env, tmp_path)
    with patch("agentic_fx.loops.improve_loop.ctypes.CDLL", _NoRenameat2):
        relative, created = loop._snapshot_final_archive(
            candidate, mission_id=7, now=NOW)
    assert (relative, created) == (None, False)
    assert not final_path.exists()
    assert not [p for p in final_path.parent.iterdir()
                if p.name.startswith(".tmp-")]


def test_publish_archives_goes_through_the_noreplace_helper(
        improve_env, tmp_path):
    import inspect
    src = inspect.getsource(ImproveLoop._publish_archives)
    assert "_publish_dir_noreplace" in src
    assert "os.rename" not in src
    src = inspect.getsource(ImproveLoop._snapshot_final_archive)
    assert "_publish_dir_noreplace" in src
    assert "os.rename(archive_tmp" not in src


def test_snapshot_reports_creation_when_nobody_else_published(
        improve_env, tmp_path):
    from tests.loops.test_improve_e2e import NOW
    loop, candidate, final_path = _snapshot_setup(improve_env, tmp_path)
    relative, created = loop._snapshot_final_archive(
        candidate, mission_id=7, now=NOW)
    assert created is True and relative is not None


def _uuid_sequence(values):
    import uuid
    it = iter(values)
    last = [None]

    def fake():
        try:
            last[0] = next(it)
        except StopIteration:
            pass
        return uuid.UUID(int=last[0])

    return fake


@pytest.mark.parametrize("kind", ["file", "empty_dir"])
def test_stale_aside_name_collision_is_not_replaced(
        improve_env, tmp_path, kind):
    """退避先の名前に先客 (ファイル・空 dir) がいても置換せず、別名で退避する。"""
    import uuid
    from tests.loops.test_improve_e2e import NOW
    loop, candidate, final_path = _snapshot_setup(improve_env, tmp_path)
    final_path.mkdir(parents=True)
    (final_path / "plugin.py").write_text("partial\n")
    taken = final_path.parent / (
        f".stale-{candidate.artifact_hash}-{uuid.UUID(int=2)}")
    if kind == "file":
        taken.write_text("occupant")
    else:
        taken.mkdir()
    with patch("agentic_fx.loops.improve_loop.uuid.uuid4",
               _uuid_sequence([1, 2, 3, 4])):
        relative, created = loop._snapshot_final_archive(
            candidate, mission_id=7, now=NOW)
    assert relative is not None and created is True
    assert _verified(final_path, candidate.artifact_hash)
    if kind == "file":
        assert taken.read_text() == "occupant"
    else:
        assert taken.is_dir() and list(taken.iterdir()) == []
    stale = [p for p in final_path.parent.iterdir()
             if p.name.startswith(".stale-")]
    assert len(stale) == 2


def test_stale_aside_gives_up_after_three_collisions(improve_env, tmp_path):
    import uuid
    from tests.loops.test_improve_e2e import NOW
    loop, candidate, final_path = _snapshot_setup(improve_env, tmp_path)
    final_path.mkdir(parents=True)
    (final_path / "plugin.py").write_text("partial\n")
    taken = final_path.parent / (
        f".stale-{candidate.artifact_hash}-{uuid.UUID(int=2)}")
    taken.write_text("occupant")
    with patch("agentic_fx.loops.improve_loop.uuid.uuid4",
               _uuid_sequence([1, 2])):
        relative, created = loop._snapshot_final_archive(
            candidate, mission_id=7, now=NOW)
    assert (relative, created) == (None, False)
    assert taken.read_text() == "occupant"
    assert (final_path / "plugin.py").read_text() == "partial\n"


def test_final_archive_paths_never_call_a_replacing_rename():
    import inspect
    assert "os.rename(" not in inspect.getsource(
        ImproveLoop._snapshot_final_archive)
    assert "os.rename(" not in inspect.getsource(
        ImproveLoop._publish_archives)
    assert "os.rename(" not in inspect.getsource(
        ImproveLoop._set_aside_stale)
