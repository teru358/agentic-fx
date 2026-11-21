"""`data resume` の受付 event は新規に記録した要求にだけ残す。"""
from __future__ import annotations

import pytest

from .conftest import APPROVER


@pytest.mark.parametrize(("outcome", "count"), [("new", 1), ("existing", 0), ("replaced", 0)])
def test_only_a_newly_recorded_request_leaves_a_receipt_event(ops_env, outcome, count):
    service = ops_env.service(data_resume=lambda ack: {
        "requested": True, "acknowledge": ack, "request_outcome": outcome, "epoch": 3})
    reply = service.resume_data(APPROVER, acknowledge=True)
    assert reply["request_outcome"] == outcome
    rows = ops_env.conn.execute(
        "SELECT COUNT(*) AS n FROM ops_events WHERE code='guard_state_changed'").fetchone()["n"]
    assert rows == count


def test_a_response_without_an_outcome_keeps_the_receipt_event(ops_env):
    service = ops_env.service(data_resume=lambda ack: {"requested": True})
    service.resume_data(APPROVER, acknowledge=False)
    assert ops_env.conn.execute(
        "SELECT COUNT(*) AS n FROM ops_events WHERE code='guard_state_changed'").fetchone()["n"] == 1
