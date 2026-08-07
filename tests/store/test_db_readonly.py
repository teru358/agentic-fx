"""db.connect_readonly (プラン 8 worker 基盤 — codex I-7)。"""
from __future__ import annotations

import sqlite3

import pytest

from agentic_fx.store.db import connect, connect_readonly, init_db


def test_connect_readonly_can_read_existing_rows(tmp_path):
    db_path = tmp_path / "agentic.db"
    rw = connect(db_path)
    init_db(rw)
    rw.execute(
        "INSERT INTO missions (loop, runner, model, status, started_at) "
        "VALUES ('trade', 'local', 'x', 'running', '2026-08-04T00:00:00+00:00')")
    rw.commit()

    ro = connect_readonly(db_path)
    row = ro.execute("SELECT loop FROM missions").fetchone()
    assert row["loop"] == "trade"


def test_connect_readonly_rejects_write(tmp_path):
    db_path = tmp_path / "agentic.db"
    init_db(connect(db_path))
    ro = connect_readonly(db_path)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute(
            "INSERT INTO missions (loop, runner, model, status, started_at) "
            "VALUES ('trade', 'local', 'x', 'running', '2026-08-04T00:00:00+00:00')")


def test_connect_readonly_missing_db_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        connect_readonly(tmp_path / "does-not-exist.db")
