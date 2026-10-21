"""transitions の observer registry (AC-10 の登録簿、AC-11)。"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone

import pytest

from agentic_fx.core import transitions
from agentic_fx.core.contracts import OrderStatus as S
from agentic_fx.core.transitions import ObserverError, observe, transition, unobserve
from agentic_fx.store import orders
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    oid = orders.insert(conn, pair="USDJPY", direction="long", entry_type="limit",
                        horizon="day", status=S.SUBMITTING, now=NOW)
    yield conn, oid, tmp_path / "t.db"
    conn.close()


def test_observer_receives_row_from_and_to_after_commit(db):
    conn, oid, path = db
    seen = []

    def fn(row, frm, to):
        other = connect(path)
        try:
            committed = orders.get(other, oid)["status"]
        finally:
            other.close()
        seen.append((row["id"], row["status"], frm, to, committed))

    token = observe(conn, fn)
    try:
        transition(conn, oid, S.SUBMITTED, NOW)
    finally:
        unobserve(token)
    assert seen == [(oid, "submitted", "submitting", "submitted", "submitted")]


def test_observer_is_not_called_for_illegal_transition(db):
    conn, oid, _ = db
    calls = []
    token = observe(conn, lambda *a: calls.append(a))
    try:
        with pytest.raises(transitions.IllegalTransition):
            transition(conn, oid, S.OPEN, NOW)
    finally:
        unobserve(token)
    assert calls == []


def test_duplicate_registration_is_rejected_and_unobserve_releases(db):
    conn, oid, _ = db
    token = observe(conn, lambda *a: None)
    with pytest.raises(ObserverError):
        observe(conn, lambda *a: None)
    unobserve(token)
    unobserve(token)  # 解除済みは何もしない
    calls = []
    token2 = observe(conn, lambda *a: calls.append(a))
    unobserve(token2)
    transition(conn, oid, S.SUBMITTED, NOW)
    assert calls == []


def test_other_connection_has_no_observer(db):
    conn, oid, path = db
    calls = []
    token = observe(conn, lambda *a: calls.append(a))
    other = connect(path)
    try:
        transition(other, oid, S.SUBMITTED, NOW)
    finally:
        other.close()
        unobserve(token)
    assert calls == []


def test_ac11_raising_observer_does_not_change_result_or_row(db):
    conn, oid, _ = db

    def boom(*_):
        raise RuntimeError("observer exploded")

    token = observe(conn, boom)
    try:
        row = transition(conn, oid, S.SUBMITTED, NOW, broker_order_id="b1")
    finally:
        unobserve(token)
    assert row["status"] == "submitted" and row["broker_order_id"] == "b1"
    assert orders.get(conn, oid) == row


def test_register_requires_callable(db):
    conn, _, _ = db
    with pytest.raises(ObserverError):
        observe(conn, None)


def test_unobserve_unknown_token_raises():
    with pytest.raises(ObserverError):
        unobserve(object())


def test_slow_observer_is_the_only_cost_registry_lookup_is_thread_safe(db):
    conn, oid, _ = db
    token_box = []

    def worker():
        for _ in range(200):
            try:
                t = observe(conn, lambda *a: None)
            except ObserverError:
                continue
            unobserve(t)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 全員が解除し終えた後は、もう一度登録できる
    token_box.append(observe(conn, lambda *a: None))
    unobserve(token_box[0])


def test_observer_gets_a_copy_so_it_cannot_alter_the_returned_row(db):
    conn, oid, _ = db
    seen = []

    def meddle(row, frm, to):
        seen.append(row)
        row["status"] = "tampered"

    token = observe(conn, meddle)
    try:
        result = transition(conn, oid, S.SUBMITTED, NOW)
    finally:
        unobserve(token)
    assert seen[0] is not result
    assert result["status"] == S.SUBMITTED.value


def test_a_stale_token_does_not_release_a_later_registration(db):
    conn, oid, _ = db
    stale = observe(conn, lambda *a: None)
    unobserve(stale)
    calls = []
    live = observe(conn, lambda *a: calls.append(a))
    try:
        unobserve(stale)
        transition(conn, oid, S.SUBMITTED, NOW)
    finally:
        unobserve(live)
    assert len(calls) == 1
