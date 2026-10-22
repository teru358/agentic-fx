from __future__ import annotations

import json
import importlib.util
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts" / "reevaluate_approvals.py"
_SPEC = importlib.util.spec_from_file_location(
    "approval_fixture", REPOSITORY / "tests" / "ops" / "_change_fixtures.py")
assert _SPEC is not None and _SPEC.loader is not None
_FIXTURE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_FIXTURE)


def _payload(name: str, *, in_pf: float = 1.2, holdout_pf: float = 1.2,
             holdout_trades: int = 30) -> dict:
    return _FIXTURE.approval_payload(
        name=name,
        in_sample={"USDJPY": _FIXTURE.metrics(40, in_pf, 0.12)},
        holdout={"USDJPY": _FIXTURE.metrics(holdout_trades, holdout_pf, 0.08)},
    )


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "approvals.db"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE approval_requests (
        id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
        status TEXT NOT NULL, created_at TEXT NOT NULL)""")
    rows = [
        (21, _payload("passing")),
        (24, _payload("low-pf", in_pf=0.9)),
        (25, _payload("too-small", holdout_trades=4)),
        (26, {"name": "legacy", "content_hash": "d" * 64}),
    ]
    conn.executemany(
        "INSERT INTO approval_requests(id, kind, payload_json, status, created_at) "
        "VALUES (?, 'plugin', ?, 'approved', '2026-10-05T00:00:00+00:00')",
        [(approval_id, json.dumps(payload, ensure_ascii=False))
         for approval_id, payload in rows],
    )
    conn.commit()
    conn.close()
    return path


def _real_shape_database(tmp_path: Path) -> Path:
    path = tmp_path / "real-shape-approvals.db"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE approval_requests (
        id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
        status TEXT NOT NULL, created_at TEXT NOT NULL)""")
    conn.executemany(
        "INSERT INTO approval_requests(id, kind, payload_json, status, created_at) "
        "VALUES (?, 'plugin', ?, 'approved', '2026-10-05T00:00:00+00:00')",
        [(approval_id, json.dumps(payload, ensure_ascii=False))
         for approval_id, payload in _FIXTURE.reevaluation_real_payloads().items()],
    )
    conn.commit()
    conn.close()
    return path


def _run(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(path), *args],
        cwd=REPOSITORY, text=True, capture_output=True, check=False,
    )


def test_prints_saved_fixture_shape_under_fixed_floor(tmp_path: Path):
    completed = _run(_database(tmp_path), "--ids", "21,24,25")

    assert completed.returncode == 0
    lines = completed.stdout.splitlines()
    assert len(lines) == 4
    assert "21" in lines[1] and "passing@cccccccccccc" in lines[1] and "合格" in lines[1]
    assert "24" in lines[2] and "PF 不足" in lines[2]
    assert "25" in lines[3] and "holdout 評価不能" in lines[3]


def test_prints_real_flat_holdout_shape_under_fixed_floor(tmp_path: Path):
    """平坦 holdout を捨てる変異を殺す: #21/#24/#25 の実保存形状。"""
    completed = _run(_real_shape_database(tmp_path), "--ids", "21,24,25")

    assert completed.returncode == 0
    lines = completed.stdout.splitlines()
    assert len(lines) == 4
    assert lines[1] == (
        "21 | real-21@cccccccccccc | pf=USDJPY:1.4440572139303938 / "
        "avg_r=USDJPY:0.18095898178667616 | pf=USDJPY:0.7624153836938286 / "
        "trades=USDJPY:48 | PF 不足")
    assert lines[2] == (
        "24 | real-24@cccccccccccc | pf=USDJPY:1.0699268738574357 / "
        "avg_r=USDJPY:0.04121428459537167 | pf=USDJPY:3.3747323340475135 / "
        "trades=USDJPY:4 | holdout 評価不能")
    assert lines[3] == (
        "25 | real-25@cccccccccccc | pf=USDJPY:1.1193967065100916 / "
        "avg_r=USDJPY:0.0622021839480993 | pf=USDJPY:4.617164898746958 / "
        "trades=USDJPY:4 | holdout 評価不能")


def test_missing_saved_values_are_reported_without_failure(tmp_path: Path):
    completed = _run(_database(tmp_path), "--ids", "26")

    assert completed.returncode == 0
    assert "26" in completed.stdout
    assert "評価不能 (値なし)" in completed.stdout


def test_ids_limits_the_rows(tmp_path: Path):
    completed = _run(_database(tmp_path), "--ids", "24")

    assert completed.returncode == 0
    assert "24" in completed.stdout
    assert "21" not in completed.stdout
    assert "25" not in completed.stdout


def test_read_only_execution_leaves_database_bytes_and_mtime_unchanged(tmp_path: Path):
    path = _database(tmp_path)
    before = (path.read_bytes(), path.stat().st_mtime_ns)

    completed = _run(path)

    assert completed.returncode == 0
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_query_only_connection_rejects_writes(tmp_path: Path):
    sys.path.insert(0, str(REPOSITORY))
    try:
        from scripts.reevaluate_approvals import open_readonly
    finally:
        sys.path.pop(0)

    conn = open_readonly(_database(tmp_path))
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM approval_requests")
    finally:
        conn.close()
