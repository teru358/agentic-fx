"""候補台帳の backfill の選び方と、archive_path の信頼境界を固定する。"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from agentic_fx.store.db import connect, init_db
from tests.store.test_candidate_archives_migration_pins import (
    NOW, _approval, _legacy, _old_row)

_SMA = (Path(__file__).resolve().parents[2] / "docs" / "examples" / "plugins"
        / "sma_cross")
_PAYLOAD = {"mission_id": 1, "artifact_hash": "h1", "content_hash": "c1"}


@pytest.mark.parametrize("older,newer", [
    ("expired", "approved"), ("approved", "expired"),
    ("rejected", "approved"), ("approved", "rejected")])
def test_the_latest_approval_of_a_row_decides_its_outcome(
        tmp_path, older, newer):
    conn, path = _legacy(tmp_path)
    _old_row(conn, 1, "h1", "c1")
    _approval(conn, older, dict(_PAYLOAD))
    _approval(conn, newer, dict(_PAYLOAD))
    conn.commit()
    conn.close()
    migrated = connect(path)
    init_db(migrated)
    expected = {"approved": "approved", "expired": "expired",
                "rejected": "rejected_by_human"}[newer]
    assert migrated.execute("SELECT outcome_class FROM candidate_archives"
                            ).fetchone()[0] == expected


def _row_with_path(conn, archive_path):
    conn.execute(
        "INSERT INTO candidate_archives (mission_id,name,content_hash,"
        "artifact_hash,archive_path,pair,metrics_json,created_at) VALUES "
        "(1,'sma_cross','c1','h1',?,'USDJPY','{}',?)",
        (archive_path, NOW.isoformat()))
    conn.commit()


def _identity_after_backfill(tmp_path, archive_path, *, prepare):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    shutil.copytree(_SMA, outside / "h1")
    prepare(root, outside)
    conn, path = _legacy(tmp_path)
    _row_with_path(conn, archive_path.format(outside=outside))
    conn.close()
    migrated = connect(path)
    init_db(migrated, root=root)
    return migrated.execute(
        "SELECT logic_hash, family_key FROM candidate_archives").fetchone()


def test_identity_backfill_reads_a_row_inside_the_root(tmp_path):
    def inside(root, outside):
        shutil.copytree(_SMA, root / "plugins" / "_archive" / "1" / "h1")
    row = _identity_after_backfill(
        tmp_path, "plugins/_archive/1/h1", prepare=inside)
    assert row[0] is not None


@pytest.mark.parametrize("archive_path", [
    "{outside}/h1", "../outside/h1", "plugins/../../outside/h1"])
def test_identity_backfill_does_not_read_outside_the_root(
        tmp_path, archive_path):
    row = _identity_after_backfill(
        tmp_path, archive_path, prepare=lambda root, outside: None)
    assert (row[0], row[1]) == (None, None)


def test_identity_backfill_does_not_follow_a_symlinked_parent(tmp_path):
    def link(root, outside):
        (root / "plugins" / "_archive").mkdir(parents=True)
        os.symlink(outside, root / "plugins" / "_archive" / "1")
    row = _identity_after_backfill(
        tmp_path, "plugins/_archive/1/h1", prepare=link)
    assert (row[0], row[1]) == (None, None)
