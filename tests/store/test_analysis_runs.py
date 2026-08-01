"""analysis_runs のテスト (プラン 6 Task 10)。

tests/store/ には共有 conftest が無い規約 — ローカル _conn(tmp_path)
(tests/store/test_backtest_runs.py に倣う)。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agentic_fx.store import analysis_runs
from agentic_fx.store.db import connect, init_db

H = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def _conn(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def test_save_persists_row_and_returns_id(tmp_path):
    conn = _conn(tmp_path)
    run_id = analysis_runs.save(
        conn, params={"request": {"kind": "lead_lag"}, "in_sample_until": "x"},
        trial_count=25, source="dukascopy", now=H)
    row = conn.execute(
        "SELECT trial_count, source, created_at, params_json "
        "FROM analysis_runs WHERE id=?", (run_id,)).fetchone()
    assert row["trial_count"] == 25
    assert row["source"] == "dukascopy"
    assert row["created_at"] == H.isoformat()
    assert row["params_json"] == \
        '{"in_sample_until": "x", "request": {"kind": "lead_lag"}}'


def test_save_rejects_naive_now(tmp_path):
    conn = _conn(tmp_path)
    naive = datetime(2026, 7, 22, 12, 0)
    with pytest.raises(ValueError):
        analysis_runs.save(conn, params={}, trial_count=1, source="dukascopy",
                           now=naive)


def test_save_normalizes_non_utc_now_to_utc(tmp_path):
    from datetime import timedelta

    conn = _conn(tmp_path)
    tokyo = timezone(timedelta(hours=9))
    aware_non_utc = datetime(2026, 7, 22, 21, 0, tzinfo=tokyo)  # == H
    run_id = analysis_runs.save(
        conn, params={}, trial_count=1, source="dukascopy",
        now=aware_non_utc)
    row = conn.execute("SELECT created_at FROM analysis_runs WHERE id=?",
                       (run_id,)).fetchone()
    assert row["created_at"] == H.isoformat()


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "1"])
def test_save_rejects_invalid_trial_count(tmp_path, bad):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError):
        analysis_runs.save(conn, params={}, trial_count=bad, source="dukascopy",
                           now=H)


def test_save_multiple_rows_get_distinct_ids(tmp_path):
    conn = _conn(tmp_path)
    id1 = analysis_runs.save(conn, params={"a": 1}, trial_count=1,
                             source="dukascopy", now=H)
    id2 = analysis_runs.save(conn, params={"a": 2}, trial_count=2,
                             source="dukascopy", now=H)
    assert id1 != id2
    assert conn.execute(
        "SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 2
