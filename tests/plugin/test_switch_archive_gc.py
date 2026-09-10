"""T4 (§1 L4): `plugin.switch.sweep_orphans` の archive GC 拡張 — 起動時
限定 tmp 回収 (⑥)、バイト/mission 数上限 GC (⑦)、path 不存在の再収束
(⑦′)、孤立 final の報告 (T4 変更点6)。ブリーフ「変更点」4・6、「テスト」節。
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.plugin import switch
from agentic_fx.store import candidate_archives
from agentic_fx.store import db as db_store

NOW = datetime(2026, 9, 10, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    conn = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(conn)
    return tmp_path, plugins_dir, conn


def _make_final(plugins_dir, mission_id, artifact_hash, *, size=64,
                readonly=True):
    final = plugins_dir / "_archive" / str(mission_id) / artifact_hash
    final.mkdir(parents=True)
    data = final / "plugin.py"
    data.write_bytes(b"x" * size)
    if readonly:
        data.chmod(0o400)
        final.chmod(0o500)
    return final


def _insert_row(conn, *, mission_id, artifact_hash, archive_path, name="cand"):
    return candidate_archives.insert(
        conn, mission_id=mission_id, name=name, content_hash="c",
        artifact_hash=artifact_hash, archive_path=archive_path, pair="USDJPY",
        metrics={"pf": 1.0}, now=NOW, commit=True)


# ---------------------------------------------------------------------------
# archive GC ⑥: startup 限定 tmp 回収
# ---------------------------------------------------------------------------


def test_sweep_orphans_removes_readonly_tmp_dir(env):
    tmp_path, plugins_dir, conn = env
    tmp_dir = plugins_dir / "_archive" / "1" / ".tmp-abc-1234"
    tmp_dir.mkdir(parents=True)
    f = tmp_dir / "plugin.py"
    f.write_bytes(b"y")
    f.chmod(0o400)
    tmp_dir.chmod(0o500)

    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW)

    assert not tmp_dir.exists()


def test_sweep_orphans_tmp_symlink_is_unlinked_not_followed(env):
    tmp_path, plugins_dir, conn = env
    target = tmp_path / "outside_tmp_target"
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    mission_dir = plugins_dir / "_archive" / "1"
    mission_dir.mkdir(parents=True)
    link = mission_dir / ".tmp-link-1"
    link.symlink_to(target, target_is_directory=True)

    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW)

    assert not link.exists() and not link.is_symlink()
    assert target.exists()
    assert (target / "keep.txt").exists()


# ---------------------------------------------------------------------------
# 孤立 final の報告 (T4 変更点6)
# ---------------------------------------------------------------------------


def test_sweep_orphans_tmp_cleanup_failure_is_reported(env, monkeypatch):
    """ローカル T4 1 周目 #L12 (2026-09-10): tmp 回収 (⑥) の失敗は
    `sweep_archive_tmp_failed` activity に残す。`rmtree(ignore_errors=True)`
    が全てを飲むため、この枝に到達するのは `_chmod_tree_writable` 由来の
    `OSError` だけで、GC ⑦ の `rmtree_incomplete` (段 0 T4-7) とは別経路。
    ログを落とすと tmp が残り続けても誰も気づけない。"""
    tmp_path, plugins_dir, conn = env
    tmp_dir = plugins_dir / "_archive" / "1" / ".tmp-abc-1234"
    tmp_dir.mkdir(parents=True)
    (tmp_dir / "plugin.py").write_bytes(b"x")
    activity = ActivityLog(tmp_path / "activity.log")

    def failing_chmod_tree(path):
        raise OSError("simulated chmod failure")

    monkeypatch.setattr(switch, "_chmod_tree_writable", failing_chmod_tree)

    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW,
                         activity=activity)

    assert tmp_dir.exists()
    log = (tmp_path / "activity.log").read_text()
    assert "sweep_archive_tmp_failed" in log
    assert str(tmp_dir) in log


def test_sweep_orphans_reports_orphan_final_without_deleting(env):
    tmp_path, plugins_dir, conn = env
    activity = ActivityLog(tmp_path / "activity.log")
    final = _make_final(plugins_dir, 1, "deadbeef" * 8, readonly=False)

    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW,
                         activity=activity)

    assert final.exists()
    assert "archive_orphan_final" in (tmp_path / "activity.log").read_text()


def test_sweep_orphans_does_not_report_known_final_as_orphan(env):
    tmp_path, plugins_dir, conn = env
    activity = ActivityLog(tmp_path / "activity.log")
    artifact_hash = "cafebabe" * 8
    final = _make_final(plugins_dir, 1, artifact_hash, readonly=False)
    _insert_row(conn, mission_id=1, artifact_hash=artifact_hash,
               archive_path=f"plugins/_archive/1/{artifact_hash}")

    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW,
                         activity=activity)

    assert final.exists()
    log_path = tmp_path / "activity.log"
    assert not log_path.exists() or "archive_orphan_final" not in log_path.read_text()


# ---------------------------------------------------------------------------
# archive GC ⑦: 上限 GC
# ---------------------------------------------------------------------------


def test_sweep_orphans_archive_gc_by_bytes_boundary(env):
    tmp_path, plugins_dir, conn = env
    h1, h2 = "1" * 64, "2" * 64
    f1 = _make_final(plugins_dir, 1, h1, size=100, readonly=False)
    f2 = _make_final(plugins_dir, 2, h2, size=50, readonly=False)
    _insert_row(conn, mission_id=1, artifact_hash=h1,
               archive_path=f"plugins/_archive/1/{h1}")
    _insert_row(conn, mission_id=2, artifact_hash=h2,
               archive_path=f"plugins/_archive/2/{h2}")

    # 境界: 総バイト == 上限 (150) は削除しない
    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW,
                         archive_max_bytes=150)
    assert f1.exists() and f2.exists()

    # 境界 -1: 総バイト (150) > 上限 (149) で最も古い mission を削除
    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW,
                         archive_max_bytes=149)
    assert not f1.exists()
    assert f2.exists()
    row1 = conn.execute(
        "SELECT archive_path FROM candidate_archives WHERE mission_id=1"
    ).fetchone()
    assert row1["archive_path"] is None
    row2 = conn.execute(
        "SELECT archive_path FROM candidate_archives WHERE mission_id=2"
    ).fetchone()
    assert row2["archive_path"] is not None


def test_sweep_orphans_archive_gc_by_missions_count_boundary(env):
    tmp_path, plugins_dir, conn = env
    hashes = {}
    finals = {}
    for mission_id in (1, 2, 3):
        h = str(mission_id) * 64
        hashes[mission_id] = h
        finals[mission_id] = _make_final(
            plugins_dir, mission_id, h, size=10, readonly=False)
        _insert_row(conn, mission_id=mission_id, artifact_hash=h,
                   archive_path=f"plugins/_archive/{mission_id}/{h}")

    # 境界: mission 数 (3) == 上限 (3) は削除しない
    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW,
                         archive_max_missions=3)
    assert all(p.exists() for p in finals.values())

    # 境界 -1: mission 数 (3) > 上限 (2) で最も古い mission を削除
    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW,
                         archive_max_missions=2)
    assert not finals[1].exists()
    assert finals[2].exists() and finals[3].exists()


def test_sweep_orphans_archive_gc_none_limits_are_unlimited(env):
    tmp_path, plugins_dir, conn = env
    h = "3" * 64
    final = _make_final(plugins_dir, 1, h, size=10_000_000, readonly=False)
    _insert_row(conn, mission_id=1, artifact_hash=h,
               archive_path=f"plugins/_archive/1/{h}")

    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW,
                         archive_max_bytes=None, archive_max_missions=None)

    assert final.exists()


def test_sweep_orphans_archive_gc_does_not_follow_symlink_mission_dir(env):
    tmp_path, plugins_dir, conn = env
    archive_root = plugins_dir / "_archive"
    archive_root.mkdir(parents=True)
    outside = tmp_path / "outside_mission"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    link = archive_root / "1"
    link.symlink_to(outside, target_is_directory=True)

    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW,
                         archive_max_bytes=0, archive_max_missions=0)

    assert link.is_symlink()
    assert outside.exists()
    assert (outside / "keep.txt").exists()


def test_sweep_orphans_archive_gc_symlink_mission_dir_target_perms_untouched(env):
    """段 0 T4-8 pin (2026-09-10): symlink の mission dir は候補にも入れない。
    候補に入ると rmtree は symlink を拒むが、その前の `_chmod_tree_writable`
    が os.walk で link 先へ降りて外部の permission を書き換える。"""
    import stat
    tmp_path, plugins_dir, conn = env
    archive_root = plugins_dir / "_archive"
    archive_root.mkdir(parents=True)
    outside = tmp_path / "outside_mission"
    outside.mkdir()
    ro_file = outside / "keep.txt"
    ro_file.write_text("keep")
    ro_file.chmod(0o400)
    outside.chmod(0o500)
    (archive_root / "1").symlink_to(outside, target_is_directory=True)
    try:
        switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW,
                             archive_max_bytes=0, archive_max_missions=0)
        assert stat.S_IMODE(ro_file.stat().st_mode) == 0o400
        assert stat.S_IMODE(outside.stat().st_mode) == 0o500
    finally:
        outside.chmod(0o700)
        ro_file.chmod(0o600)


def test_sweep_orphans_archive_gc_ignores_non_numeric_dirs_and_index(env):
    tmp_path, plugins_dir, conn = env
    archive_root = plugins_dir / "_archive"
    archive_root.mkdir(parents=True)
    index_path = archive_root / "INDEX.md"
    index_path.write_text("| date | mission | status |\n")
    stray = archive_root / "not_a_mission"
    stray.mkdir()
    (stray / "f.txt").write_text("x")

    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW,
                         archive_max_bytes=0, archive_max_missions=0)

    assert index_path.exists()
    assert stray.exists()


def test_sweep_orphans_archive_gc_deletes_readonly_tree(env):
    tmp_path, plugins_dir, conn = env
    h = "4" * 64
    final = _make_final(plugins_dir, 1, h, size=10, readonly=True)
    _insert_row(conn, mission_id=1, artifact_hash=h,
               archive_path=f"plugins/_archive/1/{h}")

    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW,
                         archive_max_missions=0)

    assert not final.exists()
    assert not final.parent.exists()


def test_sweep_orphans_archive_gc_rmtree_failure_keeps_row(env, monkeypatch):
    tmp_path, plugins_dir, conn = env
    h = "5" * 64
    final = _make_final(plugins_dir, 1, h, size=10, readonly=False)
    _insert_row(conn, mission_id=1, artifact_hash=h,
               archive_path=f"plugins/_archive/1/{h}")
    activity = ActivityLog(tmp_path / "activity.log")

    def failing_chmod_tree(path):
        raise OSError("simulated chmod failure")

    monkeypatch.setattr(switch, "_chmod_tree_writable", failing_chmod_tree)

    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW,
                         activity=activity, archive_max_missions=0)

    assert final.exists()
    row = conn.execute(
        "SELECT archive_path FROM candidate_archives WHERE mission_id=1"
    ).fetchone()
    assert row["archive_path"] is not None
    assert "sweep_archive_failed" in (tmp_path / "activity.log").read_text()


def test_sweep_orphans_archive_gc_silent_rmtree_failure_keeps_row(env, monkeypatch):
    """段 0 T4-7 pin (2026-09-10): `rmtree(ignore_errors=True)` は例外を出さず
    黙って残す。削除できたこと (`not exists()`) を確認してからでないと
    `clear_path` してはならない (行の path が NULL なのに dir が残る = 孤立 final)。"""
    tmp_path, plugins_dir, conn = env
    h = "6" * 64
    final = _make_final(plugins_dir, 1, h, size=10, readonly=False)
    _insert_row(conn, mission_id=1, artifact_hash=h,
               archive_path=f"plugins/_archive/1/{h}")
    activity = ActivityLog(tmp_path / "activity.log")
    monkeypatch.setattr(switch.shutil, "rmtree",
                        lambda path, ignore_errors=False: None)

    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW,
                         activity=activity, archive_max_missions=0)

    assert final.exists()
    row = conn.execute(
        "SELECT archive_path FROM candidate_archives WHERE mission_id=1"
    ).fetchone()
    assert row["archive_path"] is not None
    assert "rmtree_incomplete" in (tmp_path / "activity.log").read_text()


# ---------------------------------------------------------------------------
# archive GC ⑦′: path 不存在の再収束
# ---------------------------------------------------------------------------


def test_sweep_orphans_reconciles_row_whose_path_is_missing_on_disk(env):
    tmp_path, plugins_dir, conn = env
    h = "6" * 64
    _insert_row(conn, mission_id=1, artifact_hash=h,
               archive_path=f"plugins/_archive/1/{h}")

    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW)

    row = conn.execute(
        "SELECT archive_path FROM candidate_archives WHERE mission_id=1"
    ).fetchone()
    assert row["archive_path"] is None


def test_sweep_orphans_does_not_touch_row_whose_path_exists(env):
    tmp_path, plugins_dir, conn = env
    h = "7" * 64
    _make_final(plugins_dir, 1, h, size=10, readonly=False)
    path = f"plugins/_archive/1/{h}"
    _insert_row(conn, mission_id=1, artifact_hash=h, archive_path=path)

    switch.sweep_orphans(conn, plugins_root=plugins_dir, now=NOW)

    row = conn.execute(
        "SELECT archive_path FROM candidate_archives WHERE mission_id=1"
    ).fetchone()
    assert row["archive_path"] == path
