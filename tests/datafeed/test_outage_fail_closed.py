"""observe が失敗した tick の扱い: fail-closed の印、transaction の範囲、
プロセス内の記録は commit 後にだけ進むこと、activity は commit の後に出ること。"""
import sqlite3
from datetime import timedelta

import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.store import orders
from tests.datafeed.test_outage_transitions import (
    KEY, NOW, PAIR, _events_of, _fresh, _put_state, _report,
    _rig, _seed_bar)


def _crash_on_save(machine, monkeypatch):
    def crash(**kwargs):
        raise sqlite3.OperationalError("injected crash")
    monkeypatch.setattr(machine, "_save_state", crash)


# ---- 失敗した tick は non-ready として読まれる -------------------------------


def test_a_failed_observe_makes_the_state_read_degraded_until_the_next_one_completes(
        tmp_path, monkeypatch):
    conn, machine = _rig(tmp_path)
    _fresh(conn, NOW)
    assert machine.observe(NOW, _report()) == "ready"

    _crash_on_save(machine, monkeypatch)
    with pytest.raises(sqlite3.OperationalError):
        machine.observe(NOW + timedelta(minutes=1), _report())

    # DB の行は書けなかったので ready のまま、読み口だけが止める側に倒れる
    assert conn.execute("SELECT state FROM datafeed_outage_state").fetchone()[0] == "ready"
    assert machine.state == "degraded"
    assert machine.gap_summary(NOW)["observe_failed"] is True

    monkeypatch.undo()
    later = NOW + timedelta(minutes=2)
    _fresh(conn, later)
    assert machine.observe(later, _report()) == "ready"
    assert machine.state == "ready"
    assert machine.gap_summary(later)["observe_failed"] is False


def test_a_failure_before_the_state_is_read_also_raises_the_mark(tmp_path, monkeypatch):
    conn, machine = _rig(tmp_path)
    _fresh(conn, NOW)

    def crash(*args, **kwargs):
        raise sqlite3.OperationalError("injected crash")

    monkeypatch.setattr(machine, "_read_watermarks", crash)
    with pytest.raises(sqlite3.OperationalError):
        machine.observe(NOW, _report())

    assert machine.state == "degraded"


def test_a_closed_market_tick_that_completes_clears_the_mark(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    conn, machine = _rig(tmp_path)
    _fresh(conn, NOW)
    machine.observe(NOW, _report())
    _crash_on_save(machine, monkeypatch)
    with pytest.raises(sqlite3.OperationalError):
        machine.observe(NOW + timedelta(minutes=1), _report())
    monkeypatch.undo()

    saturday = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    machine.observe(saturday, _report())

    assert machine.state == "ready"


# ---- exposure の読み取りと state の書き込みは同じ transaction -------------------


def test_another_connection_cannot_write_an_order_between_the_exposure_read_and_the_state_write(
        tmp_path, monkeypatch):
    from agentic_fx.datafeed import outage as outage_module
    from agentic_fx.store.db import connect

    conn, machine = _rig(tmp_path)
    _seed_bar(conn, NOW - timedelta(minutes=10))
    original = outage_module.orders.list_by_status
    attempts: list[str] = []

    def read_then_race(c, *statuses):
        result = original(c, *statuses)
        if not attempts:
            other = connect(tmp_path / "outage.db")
            other.execute("PRAGMA busy_timeout=0")
            try:
                orders.insert(other, pair=PAIR, direction="buy", entry_type="market",
                              horizon="swing", status="open", now=NOW)
                other.commit()
                attempts.append("written")
            except sqlite3.OperationalError:
                attempts.append("blocked")
            finally:
                other.close()
        return result

    monkeypatch.setattr(outage_module.orders, "list_by_status", read_then_race)
    machine.observe(NOW, _report())

    assert attempts == ["blocked"]


def test_another_connection_cannot_write_an_order_between_the_exposure_read_and_the_state_write_when_the_row_already_exists(
        tmp_path, monkeypatch):
    from agentic_fx.datafeed import outage as outage_module
    from agentic_fx.store.db import connect

    conn, machine = _rig(tmp_path)
    # row も streak リセットの書き込みも無い tick でも、読み取りの前に書き込みロックを取る
    _put_state(conn, machine, "ready")
    _seed_bar(conn, NOW - timedelta(minutes=10))
    original = outage_module.orders.list_by_status
    attempts: list[str] = []

    def read_then_race(c, *statuses):
        result = original(c, *statuses)
        if not attempts:
            other = connect(tmp_path / "outage.db")
            other.execute("PRAGMA busy_timeout=0")
            try:
                orders.insert(other, pair=PAIR, direction="buy", entry_type="market",
                              horizon="swing", status="open", now=NOW)
                other.commit()
                attempts.append("written")
            except sqlite3.OperationalError:
                attempts.append("blocked")
            finally:
                other.close()
        return result

    monkeypatch.setattr(outage_module.orders, "list_by_status", read_then_race)
    machine.observe(NOW, _report())

    assert attempts == ["blocked"]


# ---- プロセス内の記録は commit が成功した後にだけ進む --------------------------


def test_a_failed_save_leaves_the_in_process_records_as_they_were(tmp_path, monkeypatch):
    conn, machine = _rig(tmp_path)
    _seed_bar(conn, NOW - timedelta(minutes=10))
    assert machine._problem_streak == 0 and machine._last_attempt_ok == {}

    _crash_on_save(machine, monkeypatch)
    with pytest.raises(sqlite3.OperationalError):
        machine.observe(NOW, _report())

    assert machine._problem_streak == 0
    assert machine._last_attempt_ok == {}


def test_the_next_tick_does_not_count_the_observation_of_a_tick_that_failed_to_save(
        tmp_path, monkeypatch):
    conn, machine = _rig(tmp_path)
    _put_state(conn, machine, "degraded", pending=1)
    _fresh(conn, NOW)
    failing = _report(ok=(), failed=(KEY,))

    _crash_on_save(machine, monkeypatch)
    with pytest.raises(sqlite3.OperationalError):
        machine.observe(NOW, failing)
    monkeypatch.undo()
    machine.observe(NOW + timedelta(minutes=1), failing)

    # 保存された観測は 1 回だけ。2 回連続で問題を見たことにはならない
    assert machine.status()["confirmed"] == 0


# ---- resume の activity は commit の後 ------------------------------------------


def test_a_failed_commit_of_an_accepted_resume_writes_no_activity_and_keeps_everything(
        tmp_path, monkeypatch):
    log = ActivityLog(tmp_path / "activity.log")
    conn, machine = _rig(tmp_path, activity=log)
    _put_state(conn, machine, "degraded", pending=1)
    _fresh(conn, NOW)
    machine.request_resume(NOW, acknowledge=True)

    def crash():
        raise sqlite3.OperationalError("injected crash")

    monkeypatch.setattr(machine, "_clear_resume_request", crash)
    with pytest.raises(sqlite3.OperationalError):
        machine.observe(NOW, _report())

    assert "data_resume_accepted" not in _events_of(log)
    row = machine.status()
    assert row["state"] == "degraded" and row["pending_human_confirmation"] == 1
    assert row["resume_requested_at"] is not None


def test_a_failed_commit_of_a_rejected_resume_writes_no_activity_and_keeps_the_request(
        tmp_path, monkeypatch):
    log = ActivityLog(tmp_path / "activity.log")
    conn, machine = _rig(tmp_path, activity=log)
    _put_state(conn, machine, "degraded", pending=1)
    _fresh(conn, NOW)
    machine.request_resume(NOW, acknowledge=False)

    def crash():
        raise sqlite3.OperationalError("injected crash")

    monkeypatch.setattr(machine, "_clear_resume_request", crash)
    with pytest.raises(sqlite3.OperationalError):
        machine.observe(NOW, _report(ok=(), failed=(KEY,)))

    assert "data_resume_rejected" not in _events_of(log)
    assert machine.status()["resume_requested_at"] is not None


# ---- 呼び出し元の未確定の書き込みを黙って確定しない ---------------------------


def _write_without_commit(conn):
    conn.execute("INSERT INTO alert_state (key, value, updated_at) VALUES ('k', 'v', 'now')")


def _count_caller_rows(conn):
    return conn.execute("SELECT count(*) FROM alert_state").fetchone()[0]


def test_observe_refuses_to_run_inside_the_callers_transaction_and_does_not_commit_it(
        tmp_path):
    conn, machine = _rig(tmp_path)
    _fresh(conn, NOW)
    machine.observe(NOW, _report())

    with pytest.raises(RuntimeError):
        with conn:
            _write_without_commit(conn)
            machine.observe(NOW + timedelta(minutes=1), _report())

    # 外側の rollback で、先行の書き込みも消えている (黙って確定されていない)
    assert _count_caller_rows(conn) == 0
    assert machine.state == "degraded"
    assert machine.gap_summary(NOW)["observe_failed"] is True


def test_observe_completes_and_clears_the_mark_when_called_again_outside_a_transaction(
        tmp_path):
    conn, machine = _rig(tmp_path)
    _fresh(conn, NOW)
    machine.observe(NOW, _report())
    with pytest.raises(RuntimeError):
        with conn:
            _write_without_commit(conn)
            machine.observe(NOW + timedelta(minutes=1), _report())
    assert machine.state == "degraded"

    later = NOW + timedelta(minutes=2)
    _fresh(conn, later)
    assert machine.observe(later, _report()) == "ready"
    assert machine.state == "ready"
    assert machine.gap_summary(later)["observe_failed"] is False


def test_a_failure_mid_tick_rolls_back_the_row_creation_and_the_streak_reset_too(
        tmp_path, monkeypatch):
    conn, machine = _rig(tmp_path)
    _seed_bar(conn, NOW - timedelta(minutes=10))
    # 初回 (row が無い) で、起動直後の streak リセットも走る tick
    assert conn.execute("SELECT count(*) FROM datafeed_outage_state").fetchone()[0] == 0
    assert machine._reset_ready_streak is True

    original = machine._save_state

    def save_then_crash(**kwargs):
        original(**kwargs)
        raise sqlite3.OperationalError("injected crash")

    monkeypatch.setattr(machine, "_save_state", save_then_crash)
    with pytest.raises(sqlite3.OperationalError):
        machine.observe(NOW, _report())

    assert conn.execute("SELECT count(*) FROM datafeed_outage_state").fetchone()[0] == 0
    assert not conn.in_transaction
    assert machine._reset_ready_streak is True


def test_a_failed_tick_does_not_lose_the_pending_streak_reset(tmp_path, monkeypatch):
    conn, machine = _rig(tmp_path)
    _put_state(conn, machine, "restricted", deadline=NOW + timedelta(minutes=20), streak=2)
    machine._reset_ready_streak = True
    _fresh(conn, NOW)
    _crash_on_save(machine, monkeypatch)
    with pytest.raises(sqlite3.OperationalError):
        machine.observe(NOW, _report())

    # リセットも rollback されたので、次の tick でもう一度行われる
    assert conn.execute(
        "SELECT ready_streak FROM datafeed_outage_state").fetchone()[0] == 2
    assert machine._reset_ready_streak is True
