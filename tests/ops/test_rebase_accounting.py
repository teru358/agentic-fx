import math
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import agentic_fx.ops.rebase_accounting as accounting
from agentic_fx.config import PaperSettings
from agentic_fx.core.accounting import daily_start_equity, record_snapshot
from agentic_fx.ops.rebase_accounting import (
    RebaseOutcomeUnknown, apply_rebase, paper_equity, read_rebase_totals,
)
from agentic_fx.store import snapshots
from agentic_fx.store.db import connect, init_db


class Clock:
    def now(self):
        return datetime(2026, 11, 14, 12, tzinfo=timezone.utc)


def _accepted(conn, n):
    cur = conn.execute(
        "INSERT INTO ops_requests "
        "(request_id,endpoint,authenticated_principal,body_sha256,phase,created_at) "
        "VALUES (?,?,?,?,?,?)",
        (f"request-{n}", "killswitch_rebase", "approver", "digest",
         "accepted", "2026-11-14T00:00:00+00:00"))
    conn.commit()
    return cur.lastrowid


def _insert_closed_order(conn, now, pnl):
    conn.execute(
        "INSERT INTO orders(pair,direction,entry_type,horizon,status,created_at,updated_at,"
        "quantity,avg_fill_price,close_price,realized_pnl,fees_swap) "
        "VALUES('USDJPY','long','market','day','closed',?,?,0.01,148.123,148.456,?,0)",
        (now.isoformat(), now.isoformat(), pnl))
    conn.commit()


def _apply(conn, *, source_id, audit_id, now, hwm, equity, balance,
           validate=None, applied_at=None):
    cut = hwm - equity
    pct = cut / hwm * 100
    return apply_rebase(
        conn, source_snapshot_id=source_id, accepted_audit_id=audit_id,
        applied_at=applied_at or now, generation=7, latched_at=None, digest_version=1,
        preview_token=f"token-{audit_id}", hwm_before=hwm, equity=equity,
        balance=balance, drawdown_pct=pct, threshold_pct=pct, cut=cut,
        cut_pct=pct, additional_headroom=equity * pct / 100, now=now,
        validate=validate or (lambda db: None))


def test_read_rebase_totals_accepts_plain_sqlite_connection(tmp_path):
    path = tmp_path / "plain.db"
    initialized = connect(path)
    init_db(initialized)
    initialized.close()
    conn = sqlite3.connect(path)
    assert read_rebase_totals(conn) == {
        "cut": 0.0, "cut_pct": 0.0, "count": 0, "last_id": None}
    assert not conn.in_transaction


@pytest.mark.parametrize("field,value", [
    ("hwm_before", float("inf")),
    ("equity", float("nan")),
    ("balance", float("-inf")),
    ("drawdown_pct", float("inf")),
    ("threshold_pct", float("nan")),
    ("cut", float("inf")),
    ("cut_pct", float("inf")),
    ("additional_headroom", float("inf")),
    ("hwm_before", 1001.0),
    ("cut", 99.0),
    ("cut", 0.0),
    ("cut_pct", 9.0),
    ("drawdown_pct", 9.0),
    ("additional_headroom", -1.0),
])
def test_invalid_ledger_values_are_rejected_before_writes(tmp_path, field, value):
    conn, now, source_id, audit_id = _commit_fault_db(tmp_path / f"invalid-{field}.db")
    kwargs = {"hwm": 1000.0, "equity": 900.0, "balance": 900.0}
    if field == "hwm_before":
        kwargs["hwm"] = value
    with pytest.raises((ValueError, TypeError)):
        apply_values = {
            "hwm_before": kwargs["hwm"], "equity": kwargs["equity"],
            "balance": kwargs["balance"], "drawdown_pct": 10.0,
            "threshold_pct": 10.0, "cut": 100.0,
            "cut_pct": 10.0, "additional_headroom": 90.0,
        }
        apply_values[field] = value
        if field == "hwm_before" and math.isfinite(value):
            apply_values["cut"] = value - apply_values["equity"]
            apply_values["cut_pct"] = apply_values["cut"] / value * 100
        apply_rebase(
            conn, source_snapshot_id=source_id, accepted_audit_id=audit_id,
            applied_at=now, generation=7, latched_at=None, digest_version=1,
            preview_token="bad-value", now=now, validate=lambda db: None,
            **apply_values)
    assert _counts(tmp_path / f"invalid-{field}.db") == (0, 1)


def test_apply_rebase_rejects_naive_applied_at(tmp_path):
    conn, now, source_id, audit_id = _commit_fault_db(tmp_path / "naive.db")
    with pytest.raises(ValueError):
        _apply(conn, source_id=source_id, audit_id=audit_id,
               now=now, hwm=1000, equity=900, balance=900,
               applied_at=datetime(2026, 11, 14, 12))
    assert _counts(tmp_path / "naive.db") == (0, 1)


def test_apply_rebase_normalizes_applied_at_to_utc(tmp_path):
    conn, now, source_id, audit_id = _commit_fault_db(tmp_path / "offset.db")
    offset_time = datetime(2026, 11, 14, 15, tzinfo=timezone(timedelta(hours=3)))
    _apply(conn, source_id=source_id, audit_id=audit_id, now=now,
           hwm=1000, equity=900, balance=900, applied_at=offset_time)
    stored = conn.execute(
        "SELECT applied_at FROM kill_switch_hwm_rebases").fetchone()[0]
    assert stored == now.isoformat()


def test_repeated_rebases_sum_paperbroker_equity_values(tmp_path):
    conn = connect(tmp_path / "repeat.db")
    init_db(conn)
    now = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
    settings = SimpleNamespace(paper=PaperSettings(starting_balance=1000.0))
    _insert_closed_order(conn, now, -0.1)
    _insert_closed_order(conn, now, -0.2)
    _insert_closed_order(conn, now, -0.3)
    initial_balance, initial_equity = paper_equity(conn, settings, Clock())
    assert initial_balance == pytest.approx(999.4)
    assert initial_equity == initial_balance
    conn.execute("INSERT INTO account_snapshots(ts,balance,equity,hwm,cashflow,source) "
                 "VALUES(?,?,?,?,0,'paper')",
                 ((now - timedelta(hours=1)).isoformat(), initial_balance,
                  initial_equity, initial_equity + 10.0))
    conn.commit()
    daily_before = daily_start_equity(conn, now)

    expected_cut = expected_pct = 0.0
    for n in range(5):
        _insert_closed_order(conn, now + timedelta(seconds=n), -(0.07 + n / 1000))
        balance, equity = paper_equity(conn, settings, Clock())
        assert balance == pytest.approx(999.4 - sum(0.07 + i / 1000 for i in range(n + 1)))
        hwm = equity + 10.123456789 + n * 0.0314159265
        source = conn.execute(
            "INSERT INTO account_snapshots(ts,balance,equity,hwm,cashflow,source) "
            "VALUES(?,?,?,?,0,'paper')",
            ((now + timedelta(minutes=n)).isoformat(), balance, equity, hwm))
        conn.commit()
        cut, pct = hwm - equity, (hwm - equity) / hwm * 100.0
        expected_cut += cut
        expected_pct += pct
        result = _apply(conn, source_id=source.lastrowid,
                        audit_id=_accepted(conn, n), now=now, hwm=hwm,
                        equity=equity, balance=balance)
        current = conn.execute("SELECT * FROM account_snapshots WHERE id=?",
                               (result["snapshot_id"],)).fetchone()
        assert current["balance"] == balance
        assert current["equity"] == equity
        assert current["hwm"] == equity
        ledger_values = conn.execute(
            "SELECT hwm_after,equity FROM kill_switch_hwm_rebases WHERE id=?",
            (result["rebase_id"],)).fetchone()
        assert ledger_values["hwm_after"] == ledger_values["equity"] == equity
        assert current["ts"] == (now + timedelta(minutes=n)).isoformat()
        assert daily_start_equity(conn, now) == daily_before

    totals = read_rebase_totals(conn)
    assert totals["count"] == 5
    assert totals["cut"] == pytest.approx(expected_cut, rel=1e-14)
    assert totals["cut_pct"] == pytest.approx(expected_pct, rel=1e-14)
    assert totals["last_id"] == 5
    columns = {r["name"] for r in conn.execute(
        "PRAGMA table_info(kill_switch_hwm_rebases)")}
    assert not {"cumulative_cut", "cumulative_cut_pct", "count"} & columns


def test_preview_reads_snapshot_orders_ledger_and_broker_in_one_read_transaction(tmp_path):
    conn = connect(tmp_path / "preview.db")
    init_db(conn)
    now = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
    _insert_closed_order(conn, now, -0.125)
    conn.execute("INSERT INTO account_snapshots(ts,balance,equity,hwm) VALUES(?,999.875,999.875,1000)",
                 (now.isoformat(),))
    conn.commit()
    conn.execute("BEGIN")
    snapshot = snapshots.latest(conn)
    order_count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    totals = read_rebase_totals(conn)
    broker_values = paper_equity(
        conn, SimpleNamespace(paper=PaperSettings(starting_balance=1000.0)), Clock())
    assert conn.in_transaction
    assert snapshot["equity"] == broker_values[1] == 999.875
    assert order_count == 1
    assert totals["count"] == 0
    conn.rollback()


@pytest.mark.parametrize("fault_point", ["validator", "snapshot", "ledger", "postcondition"])
def test_precommit_faults_leave_both_rows_unchanged(tmp_path, monkeypatch, fault_point):
    conn = connect(tmp_path / f"{fault_point}.db")
    init_db(conn)
    now = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
    source = conn.execute("INSERT INTO account_snapshots(ts,balance,equity,hwm) "
                          "VALUES(?,900,900,1000)", (now.isoformat(),))
    conn.commit()
    audit_id = _accepted(conn, 1)
    before = conn.execute("SELECT COUNT(*) FROM account_snapshots").fetchone()[0]
    def validate(db):
        assert db.in_transaction
        assert db.execute("SELECT COUNT(*) FROM account_snapshots").fetchone()[0] == before
        assert db.execute("SELECT COUNT(*) FROM kill_switch_hwm_rebases").fetchone()[0] == 0
        raise RuntimeError("validate injected")
    if fault_point == "snapshot":
        conn.execute("CREATE TRIGGER fail_snapshot BEFORE INSERT ON account_snapshots "
                     "WHEN NEW.source='manual_ks_rebase' BEGIN "
                     "SELECT RAISE(ABORT,'snapshot injected'); END")
        conn.commit()
    elif fault_point == "ledger":
        conn.execute("CREATE TRIGGER fail_ledger BEFORE INSERT ON kill_switch_hwm_rebases "
                     "BEGIN SELECT RAISE(ABORT,'ledger injected'); END")
        conn.commit()
    elif fault_point == "postcondition":
        monkeypatch.setattr(accounting, "daily_start_equity",
                            lambda db, stamp: 1000.0 if not db.execute(
                                "SELECT COUNT(*) FROM kill_switch_hwm_rebases").fetchone()[0]
                            else 999.0)
    with pytest.raises(Exception):
        _apply(conn, source_id=source.lastrowid, audit_id=audit_id, now=now,
               hwm=1000, equity=900, balance=900,
               validate=validate if fault_point == "validator" else None)
    conn.close()
    reopened = connect(tmp_path / f"{fault_point}.db")
    assert reopened.execute("SELECT COUNT(*) FROM account_snapshots").fetchone()[0] == before
    assert reopened.execute("SELECT COUNT(*) FROM kill_switch_hwm_rebases").fetchone()[0] == 0


class _CommitFaultConnection(sqlite3.Connection):
    """commit() itself fails; the module's real commit boundary runs unchanged."""
    fault = None
    rollback_calls = 0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rollback_calls = 0

    def commit(self):
        if self.fault == "after":
            super().commit()
            raise OSError("commit injected after it was durable")
        if self.fault == "before":
            raise OSError("commit injected before it was durable")
        if self.fault == "interrupt":
            raise KeyboardInterrupt
        super().commit()

    def rollback(self):
        self.rollback_calls += 1
        super().rollback()


def _commit_fault_db(path):
    conn = sqlite3.connect(path, factory=_CommitFaultConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    init_db(conn)
    now = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
    source = conn.execute("INSERT INTO account_snapshots(ts,balance,equity,hwm) "
                          "VALUES(?,900,900,1000)", (now.isoformat(),))
    conn.commit()
    return conn, now, source.lastrowid, _accepted(conn, 1)


def _counts(path):
    reopened = connect(path)
    try:
        return (reopened.execute("SELECT COUNT(*) FROM kill_switch_hwm_rebases").fetchone()[0],
                reopened.execute("SELECT COUNT(*) FROM account_snapshots").fetchone()[0])
    finally:
        reopened.close()


@pytest.mark.parametrize("fault, durable", [("before", False), ("after", True)])
def test_commit_exceptions_are_outcome_unknown_and_resolvable(tmp_path, fault, durable):
    path = tmp_path / f"commit-{fault}.db"
    conn, now, source_id, audit_id = _commit_fault_db(path)
    conn.fault = fault
    with pytest.raises(RebaseOutcomeUnknown):
        _apply(conn, source_id=source_id, audit_id=audit_id, now=now,
               hwm=1000, equity=900, balance=900)
    assert conn.rollback_calls == 1
    conn.fault = None
    conn.close()
    assert _counts(path) == (int(durable), 1 + int(durable))


def test_an_interrupt_during_commit_propagates_unchanged(tmp_path):
    path = tmp_path / "commit-interrupt.db"
    conn, now, source_id, audit_id = _commit_fault_db(path)
    conn.fault = "interrupt"
    with pytest.raises(KeyboardInterrupt):
        _apply(conn, source_id=source_id, audit_id=audit_id, now=now,
               hwm=1000, equity=900, balance=900)
    conn.fault = None
    conn.close()
    assert _counts(path) == (0, 1)


@pytest.mark.parametrize("action", ["write", "commit", "rollback", "commit_then_begin"])
def test_a_validator_cannot_write_or_end_and_reopen_transaction(tmp_path, action):
    path = tmp_path / f"validator-{action}.db"
    conn, now, source_id, audit_id = _commit_fault_db(path)
    def validate(db):
        if action == "write":
            db.execute("INSERT INTO account_snapshots(ts,balance,equity,hwm) "
                       "VALUES('x',1,1,1)")
        elif action == "commit":
            db.execute("COMMIT")
        elif action == "rollback":
            db.execute("ROLLBACK")
        else:
            db.execute("COMMIT")
            db.execute("BEGIN")
    with pytest.raises(sqlite3.DatabaseError):
        _apply(conn, source_id=source_id, audit_id=audit_id, now=now,
               hwm=1000, equity=900, balance=900,
               validate=validate)
    conn.close()
    assert _counts(path) == (0, 1)


def test_validator_authorizer_is_removed_when_it_raises(tmp_path):
    conn, now, source_id, audit_id = _commit_fault_db(tmp_path / "authorizer-cleanup.db")
    with pytest.raises(RuntimeError, match="validator failure"):
        _apply(conn, source_id=source_id, audit_id=audit_id, now=now,
               hwm=1000, equity=900, balance=900,
               validate=lambda db: (_ for _ in ()).throw(RuntimeError("validator failure")))
    conn.execute("INSERT INTO account_snapshots(ts,balance,equity,hwm) VALUES('ok',1,1,1)")
    conn.rollback()


def test_same_timestamp_rebase_is_latest_and_next_mtm_inherits_its_hwm(tmp_path):
    conn = connect(tmp_path / "same-ts.db")
    init_db(conn)
    now = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
    old_id = conn.execute("INSERT INTO account_snapshots(ts,balance,equity,hwm) "
                          "VALUES(?,900,900,1000)", (now.isoformat(),)).lastrowid
    conn.commit()
    result = _apply(conn, source_id=old_id, audit_id=_accepted(conn, 1), now=now,
                    hwm=1000, equity=900, balance=900)
    latest = snapshots.latest(conn)
    assert latest["id"] == result["snapshot_id"] > old_id
    assert latest["ts"] == now.isoformat()
    assert latest["cashflow"] == 0
    mtm = record_snapshot(conn, now=now, balance=899, equity=899)
    assert mtm["hwm"] == 900
