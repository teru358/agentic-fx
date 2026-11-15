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
    pct = accounting.drawdown_pct(equity, hwm)
    return apply_rebase(
        conn, source_snapshot_id=source_id, accepted_audit_id=audit_id,
        applied_at=applied_at or now, generation=7, latched_at=None, digest_version=1,
        preview_token=f"token-{audit_id}", hwm_before=hwm, equity=equity,
        balance=balance, threshold_pct=pct, now=now,
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
    ("hwm_before", 0.0), ("hwm_before", -1.0),
    ("equity", 1000.0), ("equity", 1100.0),
    ("hwm_before", float("inf")), ("equity", float("nan")),
    ("balance", float("-inf")), ("threshold_pct", float("nan")),
    ("threshold_pct", 0.0), ("threshold_pct", -1.0),
    ("threshold_pct", 11.0), ("hwm_before", 1001.0),
])
def test_invalid_ledger_values_are_rejected_before_writes(tmp_path, field, value):
    path = tmp_path / "invalid.db"
    conn, now, source_id, audit_id = _commit_fault_db(path)
    values = dict(hwm_before=1000.0, equity=900.0, balance=900.0, threshold_pct=10.0)
    values[field] = value
    with pytest.raises(ValueError):
        apply_rebase(conn, source_snapshot_id=source_id, accepted_audit_id=audit_id,
                     applied_at=now, generation=7, latched_at=None, digest_version=1,
                     preview_token="bad-value", now=now, validate=lambda db: None, **values)
    assert _counts(path) == (0, 1)
    assert not conn.in_transaction


@pytest.mark.parametrize("equity,balance", [
    (0.0, 0.0), (-10.0, -10.0), (900.0, 0.0), (900.0, -1.0)])
def test_non_positive_account_values_matching_the_source_are_rejected(
        tmp_path, equity, balance):
    path = tmp_path / "non-positive.db"
    conn, now, source_id, audit_id = _commit_fault_db(path)
    conn.execute("UPDATE account_snapshots SET hwm=1000.0, equity=?, balance=? WHERE id=?",
                 (equity, balance, source_id))
    conn.commit()
    with pytest.raises(ValueError):
        apply_rebase(conn, source_snapshot_id=source_id, accepted_audit_id=audit_id,
                     applied_at=now, generation=7, latched_at=None, digest_version=1,
                     preview_token="non-positive", hwm_before=1000.0, equity=equity,
                     balance=balance, threshold_pct=10.0, now=now,
                     validate=lambda db: None)
    assert _counts(path) == (0, 1)
    assert not conn.in_transaction


def test_derived_figures_are_saved_and_returned_with_rounding(tmp_path):
    conn, now, source_id, audit_id = _commit_fault_db(tmp_path / "round.db")
    conn.execute("UPDATE account_snapshots SET hwm=0.3, equity=0.1, balance=0.1 WHERE id=?", (source_id,))
    conn.commit()
    result = apply_rebase(conn, source_snapshot_id=source_id, accepted_audit_id=audit_id,
        applied_at=now, generation=7, latched_at=None, digest_version=1,
        preview_token="round", hwm_before=0.3, equity=0.1, balance=0.1,
        threshold_pct=10.0, now=now, validate=lambda db: None)
    row = conn.execute("SELECT * FROM kill_switch_hwm_rebases").fetchone()
    expected = dict(cut=0.3 - 0.1, cut_pct=100 * (0.3 - 0.1) / 0.3,
                    drawdown_pct=accounting.drawdown_pct(0.1, 0.3), additional_headroom=0.1 * 10 / 100)
    for key, value in expected.items():
        assert row[key] == result[key] == value
    figures = accounting.rebase_figures(0.3, 0.1, 10)
    assert all(getattr(figures, key) == value for key, value in expected.items())


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


@pytest.mark.parametrize("action", ["write", "commit", "rollback", "commit_then_begin", "direct_commit", "direct_rollback"])
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
        elif action == "direct_commit":
            db.commit()
        elif action == "direct_rollback":
            db.rollback()
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


class _Budget(Exception):
    pass


@pytest.mark.parametrize("fail_at", [1, 2, 3, 4, 5])
def test_budget_is_checked_up_to_the_commit_and_expiry_rolls_back(tmp_path, fail_at):
    path = tmp_path / f"budget-{fail_at}.db"
    conn, now, source_id, audit_id = _commit_fault_db(path)
    calls, committed = [], []
    real = accounting._commit_rebase
    accounting._commit_rebase = lambda c: (committed.append(1), real(c))[1]

    def budget():
        calls.append(1)
        if len(calls) == fail_at:
            raise _Budget

    try:
        with pytest.raises(_Budget):
            apply_rebase(conn, accepted_audit_id=audit_id, applied_at=now, generation=7,
                         latched_at=None, digest_version=1, preview_token="budget",
                         threshold_pct=5.0, now=now, validate=lambda db: None,
                         check_budget=budget)
    finally:
        accounting._commit_rebase = real
    # budget は BEGIN 直後・validate 後・2 回の INSERT 後 (commit の直前) の 5 点で見る。
    assert len(calls) == fail_at
    assert committed == [] and not conn.in_transaction
    assert _counts(path) == (0, 1)


def test_expired_budget_after_validate_does_not_run_time_callables(tmp_path):
    path = tmp_path / "budget-callable.db"
    conn, now, _source_id, audit_id = _commit_fault_db(path)
    calls, resolved = [], []

    def budget():
        calls.append(1)
        if len(calls) == 2:
            raise _Budget

    def clock():
        resolved.append(1)
        return now

    with pytest.raises(_Budget):
        apply_rebase(conn, accepted_audit_id=audit_id, applied_at=clock, generation=7,
                     latched_at=None, digest_version=1, preview_token="budget",
                     threshold_pct=5.0, now=clock, validate=lambda db: None,
                     check_budget=budget)
    assert resolved == []
    assert not conn.in_transaction and _counts(path) == (0, 1)


def test_budget_is_checked_five_times_on_success(tmp_path):
    conn, now, _source_id, audit_id = _commit_fault_db(tmp_path / "ok.db")
    calls = []
    apply_rebase(conn, accepted_audit_id=audit_id, applied_at=now, generation=7,
                 latched_at=None, digest_version=1, preview_token="ok",
                 threshold_pct=5.0, now=now, validate=lambda db: None,
                 check_budget=lambda: calls.append(1))
    assert len(calls) == 5


def test_latest_snapshot_inside_the_transaction_is_the_source(tmp_path):
    path = tmp_path / "latest.db"
    conn, now, first_id, audit_id = _commit_fault_db(path)
    other = sqlite3.connect(path)

    def validate(db):
        # validate の直前に同値の snapshot が入っても、最新行を選び直して通る。
        pass

    other.execute("INSERT INTO account_snapshots(ts,balance,equity,hwm) VALUES(?,900,900,1000)",
                  ((now + timedelta(seconds=30)).isoformat(),))
    other.commit()
    other.close()
    result = apply_rebase(conn, accepted_audit_id=audit_id, applied_at=now, generation=7,
                          latched_at=None, digest_version=1, preview_token="latest",
                          threshold_pct=5.0, now=now, validate=validate)
    row = conn.execute("SELECT source_snapshot_id FROM kill_switch_hwm_rebases").fetchone()
    assert row[0] != first_id and result["snapshot_id"] > row[0]
    assert "latched" not in result


def test_time_callables_are_resolved_after_validate(tmp_path):
    conn, now, _source_id, audit_id = _commit_fault_db(tmp_path / "time.db")
    cell = []
    later = now + timedelta(seconds=3)
    apply_rebase(conn, accepted_audit_id=audit_id, applied_at=lambda: cell[0], generation=7,
                 latched_at=None, digest_version=1, preview_token="time",
                 threshold_pct=5.0, now=lambda: cell[0],
                 validate=lambda db: cell.append(later))
    stored = conn.execute("SELECT applied_at FROM kill_switch_hwm_rebases").fetchone()[0]
    assert stored == later.isoformat()


def test_source_values_without_a_source_id_are_rejected(tmp_path):
    path = tmp_path / "mixed.db"
    conn, now, _source_id, audit_id = _commit_fault_db(path)
    with pytest.raises(ValueError):
        apply_rebase(conn, accepted_audit_id=audit_id, applied_at=now, generation=7,
                     latched_at=None, digest_version=1, preview_token="mixed",
                     hwm_before=1000.0, threshold_pct=5.0, now=now,
                     validate=lambda db: None)
    assert _counts(path) == (0, 1)


@pytest.mark.parametrize("time_arg", ["now", "applied_at"])
@pytest.mark.parametrize("action", ["insert", "commit"])
def test_time_callable_cannot_write_or_end_transaction(tmp_path, time_arg, action):
    path = tmp_path / f"time-{time_arg}-{action}.db"
    conn, now, source_id, audit_id = _commit_fault_db(path)

    def bad_time():
        if action == "insert":
            conn.execute("INSERT INTO account_snapshots(ts,balance,equity,hwm) "
                         "VALUES('x',1,1,1)")
        else:
            conn.execute("COMMIT")
        return now

    kwargs = {"now": now, "applied_at": now}
    kwargs[time_arg] = bad_time
    with pytest.raises(sqlite3.DatabaseError):
        apply_rebase(conn, source_snapshot_id=source_id, accepted_audit_id=audit_id,
                     generation=7, latched_at=None, digest_version=1,
                     preview_token="time-guard", hwm_before=1000, equity=900,
                     balance=900, threshold_pct=5, validate=lambda db: None, **kwargs)
    conn.close()
    assert _counts(path) == (0, 1)
