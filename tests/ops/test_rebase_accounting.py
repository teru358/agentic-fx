from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.ops.rebase_accounting import apply_rebase, read_rebase_totals
from agentic_fx.core.accounting import daily_start_equity
from agentic_fx.store.db import connect, init_db


def _accepted(conn, n):
    cur = conn.execute(
        "INSERT INTO ops_requests "
        "(request_id,endpoint,authenticated_principal,body_sha256,phase,created_at) "
        "VALUES (?,?,?,?,?,?)",
        (f"request-{n}", "killswitch_rebase", "approver", "digest",
         "accepted", "2026-11-14T00:00:00+00:00"))
    conn.commit()
    return cur.lastrowid


def test_rebase_repeats_sum_each_cut_and_pct_without_stored_cumulative(tmp_path):
    conn = connect(tmp_path / "repeat.db")
    init_db(conn)
    now = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
    conn.execute(
        "INSERT INTO account_snapshots(ts,balance,equity,hwm,cashflow,source) "
        "VALUES(?,?,?,?,?,?)",
        ((now - timedelta(hours=1)).isoformat(), 1000.0, 1000.0,
         1000.0, 0.0, "paper"))
    conn.commit()
    for n in range(3):
        conn.execute(
            "INSERT INTO orders(pair,direction,entry_type,horizon,status,created_at,updated_at) "
            "VALUES('USDJPY','long','market','day','closed',?,?)",
            ((now - timedelta(days=3-n)).isoformat(), now.isoformat()))
    conn.commit()
    daily_before = daily_start_equity(conn, now)

    expected_cut = expected_pct = 0.0
    for n in range(5):
        ts = now - timedelta(minutes=10 - n)
        hwm, equity = 1000.0 + n * 0.3, 900.0 - n * 0.17
        source = conn.execute(
            "INSERT INTO account_snapshots(ts,balance,equity,hwm,cashflow,source) "
            "VALUES(?,?,?,?,?,?)",
            (ts.isoformat(), equity, equity, hwm, 0.0, "paper"))
        conn.commit()
        cut, pct = hwm - equity, (hwm - equity) / hwm * 100.0
        expected_cut += cut
        expected_pct += pct
        apply_rebase(
            conn, source_snapshot_id=source.lastrowid,
            accepted_audit_id=_accepted(conn, n), applied_at=now,
            generation=7, latched_at=None, digest_version=1,
            preview_token=f"token-{n}", hwm_before=hwm, equity=equity,
            balance=equity, drawdown_pct=pct, threshold_pct=10.0,
            cut=cut, cut_pct=pct, additional_headroom=equity * .1, now=now)
        assert daily_start_equity(conn, now) == daily_before

    totals = read_rebase_totals(conn)
    assert totals["count"] == 5
    assert totals["cut"] == pytest.approx(expected_cut, rel=1e-14)
    assert totals["cut_pct"] == pytest.approx(expected_pct, rel=1e-14)
    assert totals["last_id"] == 5
    columns = {r["name"] for r in conn.execute(
        "PRAGMA table_info(kill_switch_hwm_rebases)")}
    assert not {"cumulative_cut", "cumulative_cut_pct", "count"} & columns


def test_rebase_ledger_insert_failure_rolls_back_snapshot(tmp_path):
    conn = connect(tmp_path / "fault.db")
    init_db(conn)
    now = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
    source = conn.execute(
        "INSERT INTO account_snapshots(ts,balance,equity,hwm,cashflow,source) "
        "VALUES(?,?,?,?,?,?)",
        (now.isoformat(), 900.0, 900.0, 1000.0, 0.0, "paper"))
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM account_snapshots").fetchone()[0]
    conn.execute("""CREATE TRIGGER fail_rebase_ledger BEFORE INSERT ON
        kill_switch_hwm_rebases BEGIN SELECT RAISE(ABORT,'injected failure'); END""")
    conn.commit()

    with pytest.raises(Exception, match="injected failure"):
        apply_rebase(
            conn, source_snapshot_id=source.lastrowid,
            accepted_audit_id=_accepted(conn, 1), applied_at=now,
            generation=1, latched_at=None, digest_version=1,
            preview_token="test", hwm_before=1000.0, equity=900.0,
            balance=900.0, drawdown_pct=10.0, threshold_pct=10.0,
            cut=100.0, cut_pct=10.0, additional_headroom=90.0, now=now)
    assert conn.execute("SELECT COUNT(*) FROM account_snapshots").fetchone()[0] == before
    assert conn.execute("SELECT COUNT(*) FROM kill_switch_hwm_rebases").fetchone()[0] == 0
