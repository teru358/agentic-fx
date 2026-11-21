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


def _generations(conn):
    import json
    return [json.loads(r["fields_json"])["generation"] for r in conn.execute(
        "SELECT fields_json FROM ops_events WHERE code='guard_state_changed' ORDER BY id")]


def test_with_the_producer_a_new_request_is_received_with_the_epoch_it_was_recorded_in(ops_env):
    service = ops_env.service(datafeed_notices=True, data_resume=lambda ack: {
        "requested": True, "acknowledge": ack, "request_outcome": "new", "epoch": 66})
    service.resume_data(APPROVER, acknowledge=False)
    assert _generations(ops_env.conn) == [66]


def test_with_the_producer_an_epoch_of_zero_is_a_valid_generation(ops_env):
    service = ops_env.service(datafeed_notices=True, data_resume=lambda ack: {
        "requested": True, "request_outcome": "new", "epoch": 0})
    service.resume_data(APPROVER, acknowledge=False)
    assert _generations(ops_env.conn) == [0]


@pytest.mark.parametrize("epoch", [None, True, -1, "66", 1.5])
def test_with_the_producer_an_invalid_epoch_keeps_the_compatible_receipt(ops_env, epoch):
    service = ops_env.service(datafeed_notices=True, data_resume=lambda ack: {
        "requested": True, "request_outcome": "new", "epoch": epoch})
    service.resume_data(APPROVER, acknowledge=False)
    assert _generations(ops_env.conn) == [None]


def test_with_the_producer_a_response_without_an_epoch_keeps_the_compatible_receipt(ops_env):
    service = ops_env.service(datafeed_notices=True, data_resume=lambda ack: {"requested": True})
    service.resume_data(APPROVER, acknowledge=False)
    assert _generations(ops_env.conn) == [None]


def test_without_the_producer_a_new_request_keeps_the_compatible_receipt(ops_env):
    service = ops_env.service(data_resume=lambda ack: {
        "requested": True, "request_outcome": "new", "epoch": 66})
    service.resume_data(APPROVER, acknowledge=False)
    assert _generations(ops_env.conn) == [None]


@pytest.mark.parametrize("outcome", ["existing", "replaced"])
def test_with_the_producer_an_inquiry_or_a_replacement_still_leaves_no_receipt(ops_env, outcome):
    service = ops_env.service(datafeed_notices=True, data_resume=lambda ack: {
        "requested": True, "request_outcome": outcome, "epoch": 66})
    service.resume_data(APPROVER, acknowledge=True)
    assert _generations(ops_env.conn) == []
