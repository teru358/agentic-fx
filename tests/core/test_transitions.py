from datetime import datetime, timezone

import pytest

from agentic_fx.core.contracts import OrderStatus as S
from agentic_fx.core.transitions import (
    ALLOWED, TERMINAL, IllegalTransition, transition,
)
from agentic_fx.store import orders
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def _order(tmp_path, status):
    c = connect(tmp_path / "t.db")
    init_db(c)
    oid = orders.insert(c, pair="USDJPY", direction="long", entry_type="limit",
                        horizon="day", status=status, now=NOW)
    return c, oid


def test_legal_paper_flow(tmp_path):
    c, oid = _order(tmp_path, S.SUBMITTING)
    for to in (S.SUBMITTED, S.PENDING_FILL, S.PROTECTION_PENDING, S.OPEN,
               S.CLOSING, S.CLOSED):
        row = transition(c, oid, to, NOW)
        assert row["status"] == to.value


def test_illegal_transition_raises(tmp_path):
    c, oid = _order(tmp_path, S.OPEN)
    with pytest.raises(IllegalTransition):
        transition(c, oid, S.PENDING_FILL, NOW)


def test_terminal_states_are_final(tmp_path):
    c, oid = _order(tmp_path, S.CLOSED)
    for to in S:
        with pytest.raises(IllegalTransition):
            transition(c, oid, to, NOW)


def test_cancel_fill_race_allowed(tmp_path):
    # 取消と約定の競合: cancelling → protection_pending (設計書 §5)
    c, oid = _order(tmp_path, S.CANCELLING)
    row = transition(c, oid, S.PROTECTION_PENDING, NOW)
    assert row["status"] == "protection_pending"


def test_close_unknown_not_closed(tmp_path):
    c, oid = _order(tmp_path, S.CLOSING)
    transition(c, oid, S.CLOSE_UNKNOWN, NOW)
    # close_unknown からは closed / closing (再試行) のみ
    assert ALLOWED[S.CLOSE_UNKNOWN] == frozenset({S.CLOSED, S.CLOSING})


def test_fields_persisted_with_transition(tmp_path):
    c, oid = _order(tmp_path, S.OPEN)
    row = transition(c, oid, S.CLOSING, NOW, close_reason="sl")
    assert row["close_reason"] == "sl"


def test_all_statuses_covered():
    assert set(ALLOWED) | TERMINAL == set(S)
