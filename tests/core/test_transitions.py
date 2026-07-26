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


@pytest.mark.parametrize("terminal_status", [S.CLOSED, S.CANCELLED, S.REJECTED, S.EXPIRED, S.INVALIDATED])
def test_terminal_states_are_final(tmp_path, terminal_status):
    """All 5 terminal states block transitions to any of 16 destinations."""
    c, oid = _order(tmp_path, terminal_status)
    for to in S:
        with pytest.raises(IllegalTransition):
            transition(c, oid, to, NOW)


def test_submitted_reject_not_allowed(tmp_path):
    """SUBMITTED cannot transition to REJECTED (only SUBMITTING can)."""
    c, oid = _order(tmp_path, S.SUBMITTED)
    with pytest.raises(IllegalTransition):
        transition(c, oid, S.REJECTED, NOW)


def test_cancel_fill_race_allowed(tmp_path):
    # 取消と約定の競合: cancelling → protection_pending (設計書 §5)
    c, oid = _order(tmp_path, S.CANCELLING)
    row = transition(c, oid, S.PROTECTION_PENDING, NOW)
    assert row["status"] == "protection_pending"


def test_close_unknown_db_state_verified(tmp_path):
    """CLOSE_UNKNOWN is persisted to DB and is not CLOSED."""
    c, oid = _order(tmp_path, S.CLOSING)
    row = transition(c, oid, S.CLOSE_UNKNOWN, NOW)
    assert row["status"] == "close_unknown", "transition() return value has close_unknown"

    # Verify DB persistence
    db_row = orders.get(c, oid)
    assert db_row["status"] == "close_unknown", "DB has close_unknown, not closed"


def test_close_unknown_allowed_destinations(tmp_path):
    """CLOSE_UNKNOWN can transition to CLOSED or CLOSING only."""
    c, oid = _order(tmp_path, S.CLOSING)
    transition(c, oid, S.CLOSE_UNKNOWN, NOW)

    # Test closing (retry)
    row = transition(c, oid, S.CLOSING, NOW)
    assert row["status"] == "closing"

    # Transition back to close_unknown for the closed test
    transition(c, oid, S.CLOSE_UNKNOWN, NOW)

    # Test closed
    row = transition(c, oid, S.CLOSED, NOW)
    assert row["status"] == "closed"


def test_close_unknown_rejects_invalid_transitions(tmp_path):
    """CLOSE_UNKNOWN cannot transition to OPEN or other illegal destinations."""
    c, oid = _order(tmp_path, S.CLOSING)
    transition(c, oid, S.CLOSE_UNKNOWN, NOW)

    # Try illegal transitions
    with pytest.raises(IllegalTransition):
        transition(c, oid, S.OPEN, NOW)
    with pytest.raises(IllegalTransition):
        transition(c, oid, S.PENDING_FILL, NOW)


def test_fields_persisted_with_transition(tmp_path):
    c, oid = _order(tmp_path, S.OPEN)
    row = transition(c, oid, S.CLOSING, NOW, close_reason="sl")
    assert row["close_reason"] == "sl"


def test_all_statuses_covered():
    assert set(ALLOWED) | TERMINAL == set(S)
