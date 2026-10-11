from __future__ import annotations

import os
import threading
from dataclasses import replace

from afx_ops.api_client import Actor, Decision, DecisionJob, Reply
from afx_ops.gate.service import GateService
from afx_ops.gate.state import ApprovalRecord, BotState
from afx_ops.gate.types import Interaction, encode_custom_id

from ._fakes import FakeBotClient


GUILD = 12345678901234567
CHANNEL = 12345678901234568
USER = 12345678901234569


def _record(**changes):
    values = dict(approval_id=7, message_id=101, guild_id=GUILD, channel_id=CHANNEL,
                  payload_sha256="a" * 64, display_generation=1, state="fresh")
    values.update(changes)
    return ApprovalRecord(**values)


def _interaction(custom_id, *, user=USER, message=101, generation=1):
    return Interaction(custom_id=custom_id, guild_id=GUILD, channel_id=CHANNEL, user_id=user,
                       message_id=message, display_generation=generation)


def _service(record, *, users=(USER,), client=None, state_dir_fd=None):
    return GateService(BotState.empty().with_record(record), guild_id=GUILD, channel_id=CHANNEL,
                       allowlist_user_ids=users, client=client, state_dir_fd=state_dir_fd,
                       nonce_factory=lambda: "n" * 32)


def test_malformed_and_unknown_ids_do_not_change_state_or_make_a_plan():
    service = _service(_record())
    before = service.state
    assert service.handle(_interaction("bad"), now=0).decision is None
    assert service.handle(_interaction(encode_custom_id("claim", 99)), now=0).decision is None
    assert service.state == before


def test_claim_cancel_then_confirm_transitions_and_binds_the_actor():
    service = _service(_record())
    claim = service.handle(_interaction(encode_custom_id("claim", 7)), now=10)
    claimed = service.state.approvals[0]
    assert (claimed.state, claimed.action, claimed.actor, claimed.expiry) == (
        "claimed", "approve", f"discord:{USER}", 610)
    assert claim.confirm_view is not None
    assert service.bind_confirm(7, 201)
    cancelled = service.handle(_interaction(encode_custom_id("cancel", 7), message=201), now=11)
    assert cancelled.decision is None and service.state.approvals[0].state == "fresh"
    service.handle(_interaction(encode_custom_id("reject", 7)), now=12)
    assert service.bind_confirm(7, 202)
    confirmed = service.handle(_interaction(encode_custom_id("confirm", 7), message=202), now=13)
    assert service.state.approvals[0].state == "in_flight"
    assert confirmed.decision is not None
    assert confirmed.decision.action == "reject"
    assert confirmed.decision.actor == f"discord:{USER}"


def test_disallowed_or_other_user_claim_does_not_change_a_fresh_or_claimed_record():
    service = _service(_record(), users=())
    assert service.handle(_interaction(encode_custom_id("claim", 7)), now=0).decision is None
    assert service.state.approvals[0].state == "fresh"
    service = _service(_record())
    service.handle(_interaction(encode_custom_id("claim", 7)), now=0)
    assert service.handle(_interaction(encode_custom_id("claim", 7), user=USER + 1), now=1).decision is None
    assert service.state.approvals[0].state == "claimed"


def _assert_refreshed(service, generation=2):
    record = service.state.approvals[0]
    assert (record.state, record.display_generation) == ("fresh", generation)
    assert (record.actor, record.action, record.nonce, record.expiry,
            record.confirm_message_id, record.job_id) == (None,) * 6


def test_a_stale_confirm_is_ignored_and_expiry_refreshes_without_a_decision():
    service = _service(_record())
    service.handle(_interaction(encode_custom_id("claim", 7)), now=0)
    service.bind_confirm(7, 201)
    before = service.state
    outcome = service.handle(_interaction(encode_custom_id("confirm", 7), message=201, generation=2), now=1)
    assert outcome.decision is None and service.state == before
    service = _service(_record())
    service.handle(_interaction(encode_custom_id("claim", 7)), now=0)
    service.bind_confirm(7, 201)
    outcome = service.handle(_interaction(encode_custom_id("confirm", 7), message=201), now=601)
    assert outcome.decision is None and outcome.text == "This confirmation has expired."
    _assert_refreshed(service)


def test_another_first_button_on_a_claimed_record_keeps_the_claim():
    service = _service(_record())
    service.handle(_interaction(encode_custom_id("claim", 7)), now=0)
    before = service.state
    outcome = service.handle(_interaction(encode_custom_id("retry", 7)), now=1)
    assert outcome.decision is None
    assert outcome.text == "This approval is awaiting confirmation."
    assert service.state == before


def test_confirm_without_a_claim_on_the_primary_message_leaves_a_fresh_record_as_it_is():
    service = _service(_record())
    before = service.state
    result = service.handle(_interaction(encode_custom_id("confirm", 7)), now=1)
    assert result.decision is None
    assert result.text == "This confirmation is no longer valid."
    assert service.state == before


def test_perform_rereads_detail_and_refuses_digest_or_terminal_state_without_deciding():
    changed = FakeBotClient(Reply(200, {"status": "pending", "payload_sha256": "b" * 64}, None))
    service = _service(_record())
    service.client = changed
    service.handle(_interaction(encode_custom_id("claim", 7)), now=0)
    service.bind_confirm(7, 201)
    plan = service.handle(_interaction(encode_custom_id("confirm", 7), message=201), now=1).decision
    assert service.perform(plan).text == "This confirmation is no longer valid."
    assert changed.decide_calls == []
    _assert_refreshed(service)
    restricted = FakeBotClient(Reply(409, {"status": "pending", "payload_sha256": "a" * 64},
                                    "autopilot_restricted"))
    service = _service(_record())
    service.client = restricted
    service.handle(_interaction(encode_custom_id("claim", 7)), now=0)
    service.bind_confirm(7, 201)
    plan = service.handle(_interaction(encode_custom_id("confirm", 7), message=201), now=1).decision
    service.perform(plan)
    assert restricted.decide_calls == []
    _assert_refreshed(service)


def test_perform_accepts_the_daemon_detail_status_and_sends_approve():
    client = FakeBotClient(Reply(200, {"status": "pending", "payload_sha256": "a" * 64}, None))
    service = _service(_record(), client=client)
    service.handle(_interaction(encode_custom_id("approve", 7)), now=0)
    assert service.bind_confirm(7, 201)
    plan = service.handle(_interaction(encode_custom_id("confirm", 7), message=201), now=1).decision
    assert service.perform(plan).text == "Decision completed."
    assert client.decide_calls == [("approve", 7, f"discord:{USER}")]


def test_response_loss_never_reports_success_or_marks_done():
    service = _service(_record())
    service.client = FakeBotClient(RuntimeError("lost"))
    service.handle(_interaction(encode_custom_id("claim", 7)), now=0)
    service.bind_confirm(7, 201)
    plan = service.handle(_interaction(encode_custom_id("confirm", 7), message=201), now=1).decision
    result = service.perform(plan)
    assert "checked again" in result.text
    assert service.state.approvals[0].state == "in_flight"


def test_event_is_committed_only_after_the_adapter_reports_success():
    service = _service(_record())
    event = {"id": 4, "ts": "2026-10-11T00:00:00+00:00", "code": "service_health_changed",
             "ref": "instance", "fields": {"component_code": "api", "health_code": "ok"}}
    plan = service.plan_event(event)
    assert plan is not None and service.state.cursor == 0
    service.commit_event(plan.event_id, high_watermark=4)
    assert service.state.cursor == 4
    assert service.plan_event(event) is None


def test_old_ephemeral_confirmation_after_cancel_is_an_invalid_interaction_without_mutation():
    service = _service(_record())
    service.handle(_interaction(encode_custom_id("approve", 7)), now=0)
    assert service.bind_confirm(7, 201)
    service.handle(_interaction(encode_custom_id("cancel", 7), message=201), now=1)
    service.handle(_interaction(encode_custom_id("reject", 7)), now=2)
    assert service.bind_confirm(7, 202)
    before = service.state
    result = service.handle(_interaction(encode_custom_id("confirm", 7), message=201), now=3)
    assert result.text == "This interaction is invalid."
    assert service.state == before


def test_ac8_old_ephemeral_confirmation_is_invalid_after_the_new_one_is_in_flight():
    service = _service(_record())
    service.handle(_interaction(encode_custom_id("approve", 7)), now=0)
    assert service.bind_confirm(7, 201)
    service.handle(_interaction(encode_custom_id("cancel", 7), message=201), now=1)
    service.handle(_interaction(encode_custom_id("reject", 7)), now=2)
    assert service.bind_confirm(7, 202)
    service.handle(_interaction(encode_custom_id("confirm", 7), message=202), now=3)
    before = service.state
    result = service.handle(_interaction(encode_custom_id("confirm", 7), message=201), now=4)
    assert result.text == "This interaction is invalid."
    assert service.state == before


def test_expiry_is_applied_before_every_known_interaction():
    service = _service(_record())
    service.handle(_interaction(encode_custom_id("approve", 7)), now=0)
    result = service.handle(_interaction(encode_custom_id("approve", 7), user=USER + 1), now=600)
    assert result.text == "This confirmation has expired."
    _assert_refreshed(service)


def test_ac8_another_allowlisted_user_can_claim_after_expiry():
    next_user = USER + 1
    service = _service(_record(), users=(USER, next_user))
    service.handle(_interaction(encode_custom_id("approve", 7)), now=0)
    service.handle(_interaction(encode_custom_id("confirm", 7), message=201), now=600)
    claimed = service.handle(_interaction(encode_custom_id("reject", 7), user=next_user,
                                          generation=2), now=601)
    assert claimed.confirm_view is not None
    assert service.state.approvals[0].state == "claimed"
    assert service.state.approvals[0].actor == f"discord:{next_user}"


def test_two_simultaneous_confirms_make_one_plan_and_one_persisted_in_flight_record(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        service = _service(_record(), state_dir_fd=fd)
        service.handle(_interaction(encode_custom_id("approve", 7)), now=0)
        service.bind_confirm(7, 201)
        barrier = threading.Barrier(3)
        results = []
        def confirm():
            barrier.wait()
            results.append(service.handle(_interaction(encode_custom_id("confirm", 7), message=201), now=1))
        first = threading.Thread(target=confirm)
        second = threading.Thread(target=confirm)
        first.start(); second.start(); barrier.wait(); first.join(); second.join()
        assert sum(result.decision is not None for result in results) == 1
        assert sum(result.text == "Decision is already in progress." for result in results) == 1
        assert service.state.approvals[0].state == "in_flight"
        assert service._read_state().approvals[0].state == "in_flight"
    finally:
        os.close(fd)


def test_perform_rechecks_current_allowlist_immediately_before_deciding():
    client = FakeBotClient(Reply(200, {"status": "pending", "payload_sha256": "a" * 64}, None))
    service = _service(_record(), client=client)
    service.handle(_interaction(encode_custom_id("approve", 7)), now=0)
    service.bind_confirm(7, 201)
    plan = service.handle(_interaction(encode_custom_id("confirm", 7), message=201), now=1).decision
    service.allowlist_user_ids = frozenset()
    result = service.perform(plan)
    assert result.text == "This confirmation is no longer valid."
    assert client.decide_calls == []
    _assert_refreshed(service)


def test_reconcile_keeps_a_running_job_and_finishes_a_done_job():
    client = FakeBotClient(Reply(200, {"status": "pending", "payload_sha256": "a" * 64}, None),
                           Decision(Reply(202, {}, None),
                                    DecisionJob("f" * 32, Actor(f"discord:{USER}"))))
    client.poll_reply = Reply(200, {"state": "running"}, None)
    service = _service(_record(), client=client)
    service.handle(_interaction(encode_custom_id("approve", 7)), now=0)
    service.bind_confirm(7, 201)
    plan = service.handle(_interaction(encode_custom_id("confirm", 7), message=201), now=1).decision
    assert service.perform(plan).text == "The decision is still in progress."
    assert service.state.approvals[0].state == "in_flight"
    client.poll_reply = Reply(200, {"state": "done"}, None)
    assert service.reconcile_in_flight(7).text == "Decision completed."
    assert service.state.approvals[0].state == "done"


def test_perform_polls_queued_running_then_done_before_completing():
    client = FakeBotClient(
        Reply(200, {"status": "pending", "payload_sha256": "a" * 64}, None),
        Decision(Reply(202, {}, None), DecisionJob("f" * 32, Actor(f"discord:{USER}"))),
    )
    client.poll_replies = [
        Reply(200, {"state": "queued"}, None),
        Reply(200, {"state": "running"}, None),
        Reply(200, {"state": "done"}, None),
    ]
    service = _service(_record(), client=client)
    service.handle(_interaction(encode_custom_id("approve", 7)), now=0)
    assert service.bind_confirm(7, 201)
    plan = service.handle(_interaction(encode_custom_id("confirm", 7), message=201), now=1).decision
    assert service.perform(plan).text == "Decision completed."
    assert service.state.approvals[0].state == "done"


def test_poll_limit_leaves_a_running_job_in_flight_for_reconciliation():
    client = FakeBotClient(
        Reply(200, {"status": "pending", "payload_sha256": "a" * 64}, None),
        Decision(Reply(202, {}, None), DecisionJob("f" * 32, Actor(f"discord:{USER}"))),
    )
    client.poll_replies = [Reply(200, {"state": "running"}, None)] * 3
    service = _service(_record(), client=client)
    service.handle(_interaction(encode_custom_id("approve", 7)), now=0)
    assert service.bind_confirm(7, 201)
    plan = service.handle(_interaction(encode_custom_id("confirm", 7), message=201), now=1).decision
    assert service.perform(plan).text == "The decision is still in progress."
    assert client.poll_calls == 3
    assert service.state.approvals[0].state == "in_flight"


class _CountingClient(FakeBotClient):
    detail_calls = 0

    def approval(self, approval_id: int) -> Reply:
        self.detail_calls += 1
        return super().approval(approval_id)


def _confirming(service: GateService, approval_id: int = 7, *, custom="approve") -> object:
    service.handle(_interaction(encode_custom_id(custom, approval_id)), now=0)
    assert service.bind_confirm(approval_id, 201)
    return service.handle(_interaction(encode_custom_id("confirm", approval_id), message=201), now=1).decision


def test_decision_post_loss_keeps_the_record_in_flight_and_never_reports_done():
    client = FakeBotClient(Reply(200, {"status": "pending", "payload_sha256": "a" * 64}, None),
                           decision=RuntimeError("lost"))
    service = _service(_record(), client=client)
    plan = _confirming(service)
    assert "checked again" in service.perform(plan).text
    assert client.decide_calls == [("approve", 7, f"discord:{USER}")]
    assert service.state.approvals[0].state == "in_flight"


def test_a_daemon_status_other_than_pending_before_the_decision_is_terminal_and_never_copied():
    for reply in (Reply(200, {"status": "approved", "payload_sha256": "a" * 64}, None),
                  Reply(200, {"status": "expired", "payload_sha256": "a" * 64}, None),
                  Reply(404, None, "not_found")):
        client = FakeBotClient(reply)
        service = _service(_record(), client=client)
        plan = _confirming(service)
        assert service.perform(plan).text == "The authoritative approval is no longer pending."
        assert service.state.approvals[0].state == "done"
        assert client.decide_calls == []


def test_a_plan_whose_record_is_no_longer_in_flight_makes_no_detail_call():
    client = _CountingClient(Reply(200, {"status": "pending", "payload_sha256": "a" * 64}, None))
    service = _service(_record(), client=client)
    plan = _confirming(service)
    service.state = service.state.replace_record(service.state.approvals[0].fresh())
    assert service.perform(plan).text == "This confirmation is no longer valid."
    assert client.detail_calls == 0 and client.decide_calls == []


def test_a_plan_whose_digest_no_longer_matches_the_record_is_refused_before_the_detail_call():
    client = _CountingClient(Reply(200, {"status": "pending", "payload_sha256": "a" * 64}, None))
    service = _service(_record(), client=client)
    plan = _confirming(service)
    current = service.state.approvals[0]
    service.state = service.state.replace_record(replace(current, payload_sha256="b" * 64))
    assert service.perform(plan).text == "This confirmation is no longer valid."
    assert client.detail_calls == 0 and client.decide_calls == []


def test_reconcile_of_a_pending_in_flight_record_without_a_job_polls_nothing():
    client = FakeBotClient(Reply(200, {"status": "pending", "payload_sha256": "a" * 64}, None))
    service = _service(_record(), client=client)
    _confirming(service)
    assert service.state.approvals[0].job_id is None
    assert service.reconcile_in_flight(7).text == "The decision did not complete."
    assert client.poll_calls == 0


def test_a_claim_without_a_bound_confirm_message_refuses_a_confirm_from_no_message():
    service = _service(_record())
    service.handle(_interaction(encode_custom_id("claim", 7)), now=0)
    before = service.state
    result = service.handle(_interaction(encode_custom_id("confirm", 7), message=None), now=1)
    assert result.decision is None and service.state == before


def test_a_claimed_record_whose_nonce_is_already_used_refuses_confirm():
    record = _record(state="claimed", actor=f"discord:{USER}", action="approve", nonce="n" * 32,
                     expiry=600, confirm_message_id=201, nonce_used=True)
    service = _service(record)
    before = service.state
    result = service.handle(_interaction(encode_custom_id("confirm", 7), message=201), now=1)
    assert result.decision is None and service.state == before


class _Crash(BaseException):
    """poll の途中で process が落ちたことを表す。"""


def test_the_job_id_is_persisted_before_the_first_poll_and_a_restart_resumes_polling():
    client = FakeBotClient(Reply(200, {"status": "pending", "payload_sha256": "a" * 64}, None),
                           decision=Decision(Reply(202, {"job_id": "c" * 32}, None),
                                             DecisionJob("c" * 32, Actor(f"discord:{USER}"))))
    client.poll = lambda job: (_ for _ in ()).throw(_Crash())
    service = _service(_record(), client=client)
    persisted = []
    service.persist = lambda: persisted.append(service.state.approvals[0])
    plan = _confirming(service)
    try:
        service.perform(plan)
    except _Crash:
        pass
    assert persisted and persisted[-1].state == "in_flight" and persisted[-1].job_id == "c" * 32
    restarted = GateService(BotState.empty().with_record(persisted[-1]), guild_id=GUILD,
                            channel_id=CHANNEL, allowlist_user_ids=(USER,),
                            client=FakeBotClient(Reply(200, {"status": "pending",
                                                             "payload_sha256": "a" * 64}, None)))
    assert restarted.reconcile_in_flight(7).text == "Decision completed."
    assert restarted.client.poll_calls == 1
    assert restarted.state.approvals[0].state == "done"


def test_a_known_daemon_refusal_returns_the_record_to_fresh_with_a_fixed_text():
    for status, code in ((403, "autopilot_restricted"), (409, "stale_payload")):
        client = FakeBotClient(Reply(200, {"status": "pending", "payload_sha256": "a" * 64}, None),
                               decision=Decision(Reply(status, {"error": "free text"}, code), None))
        service = _service(_record(), client=client)
        plan = _confirming(service)
        result = service.perform(plan)
        assert result.text == "The decision was refused."
        assert "free text" not in result.text and code not in result.text
        _assert_refreshed(service)
        assert service.reconcile_in_flight(7).text == "This confirmation is no longer valid."


def test_a_server_error_or_rate_limit_decision_stays_in_flight_for_reconciliation():
    for status in (429, 500, 503):
        client = FakeBotClient(Reply(200, {"status": "pending", "payload_sha256": "a" * 64}, None),
                               decision=Decision(Reply(status, None, None), None))
        service = _service(_record(), client=client)
        plan = _confirming(service)
        assert "checked again" in service.perform(plan).text
        assert service.state.approvals[0].state == "in_flight"


def test_every_gate_transition_goes_through_the_persist_hook_when_there_is_no_state_dir():
    client = FakeBotClient(Reply(200, {"status": "pending", "payload_sha256": "a" * 64}, None),
                           Decision(Reply(202, {}, None),
                                    DecisionJob("f" * 32, Actor(f"discord:{USER}"))))
    client.poll_reply = Reply(200, {"state": "running"}, None)
    service = _service(_record(), client=client)
    seen = []
    service.persist = lambda: seen.append(service.state)
    service.handle(_interaction(encode_custom_id("approve", 7)), now=0)
    assert seen[-1].approvals[0].state == "claimed"
    service.bind_confirm(7, 201)
    assert seen[-1].approvals[0].confirm_message_id == 201
    plan = service.handle(_interaction(encode_custom_id("confirm", 7), message=201), now=1).decision
    assert seen[-1].approvals[0].state == "in_flight"
    service.perform(plan)
    assert seen[-1].approvals[0].job_id == "f" * 32
    client.poll_reply = Reply(200, {"state": "done"}, None)
    count = len(seen)
    service.reconcile_in_flight(7)
    assert len(seen) == count + 1 and seen[-1].approvals[0].state == "done"
    assert seen[-1] == service.state


def test_a_double_click_on_the_first_button_keeps_the_claim_and_its_nonce():
    service = _service(_record())
    service.handle(_interaction(encode_custom_id("approve", 7)), now=0)
    assert service.bind_confirm(7, 201)
    before = service.state
    second = service.handle(_interaction(encode_custom_id("approve", 7)), now=1)
    assert second.text == "This approval is awaiting confirmation."
    assert second.decision is None and second.confirm_view is None
    assert service.state == before
    confirmed = service.handle(_interaction(encode_custom_id("confirm", 7), message=201), now=2)
    assert confirmed.decision is not None


def _in_flight_with_job(**changes):
    return _record(state="in_flight", actor=f"discord:{USER}", action="approve", nonce="n" * 32,
                   expiry=600, confirm_message_id=201, nonce_used=True, job_id="c" * 32, **changes)


def test_clicks_and_cancel_while_in_flight_change_nothing_and_keep_the_job_id():
    service = _service(_in_flight_with_job())
    before = service.state
    for custom, message, generation, user in (
            ("approve", 101, 1, USER), ("reject", 101, 1, USER), ("retry", 101, 1, USER + 1),
            ("approve", 101, 9, USER), ("approve", 999, 1, USER),
            ("cancel", 201, 1, USER), ("cancel", 201, 1, USER + 1), ("confirm", 201, 1, USER),
            ("cancel", 101, 1, USER), ("confirm", 101, 1, USER)):
        result = service.handle(_interaction(encode_custom_id(custom, 7), message=message,
                                             generation=generation, user=user), now=5)
        assert result.decision is None and result.confirm_view is None
        assert service.state == before, (custom, message)
    assert service.state.approvals[0].job_id == "c" * 32
    first = service.handle(_interaction(encode_custom_id("approve", 7)), now=5)
    assert first.text == "Decision is already in progress."
    cancel = service.handle(_interaction(encode_custom_id("cancel", 7), message=201), now=5)
    assert cancel.text == "Decision is already in progress."


class _SequencedClient(FakeBotClient):
    """approval detail を呼ばれた順に返す (最後の応答はその後も返し続ける)。"""

    def __init__(self, details, decision=None):
        super().__init__(details[0], decision)
        self.details = list(details)
        self.detail_calls = 0

    def approval(self, approval_id: int) -> Reply:
        self.detail_calls += 1
        self.detail = self.details.pop(0) if len(self.details) > 1 else self.details[0]
        return super().approval(approval_id)


def test_an_approved_status_after_the_decision_does_not_stop_the_job_poll():
    pending = Reply(200, {"status": "pending", "payload_sha256": "a" * 64}, None)
    approved = Reply(200, {"status": "approved", "payload_sha256": "a" * 64}, None)
    client = _SequencedClient([pending, approved],
                              Decision(Reply(202, {}, None),
                                       DecisionJob("f" * 32, Actor(f"discord:{USER}"))))
    client.poll_replies = [Reply(200, {"state": "running"}, None)] * 3
    service = _service(_record(), client=client)
    plan = _confirming(service)
    assert service.perform(plan).text == "The decision is still in progress."
    assert client.poll_calls == 3
    record = service.state.approvals[0]
    assert (record.state, record.job_id) == ("in_flight", "f" * 32)
    client.poll_replies = [Reply(200, {"state": "done"}, None)]
    assert service.reconcile_in_flight(7).text == "Decision completed."
    assert service.state.approvals[0].state == "done"
    # job ID があれば approval の status を読まずに job を見る (decision 前の 1 回だけ)
    assert client.detail_calls == 1


def test_an_in_flight_record_without_a_job_reads_the_approval_status():
    in_flight = _record(state="in_flight", actor=f"discord:{USER}", action="approve",
                        nonce="n" * 32, expiry=600, confirm_message_id=201, nonce_used=True)
    for reply, state, text in (
            (Reply(200, {"status": "approved", "payload_sha256": "a" * 64}, None), "done",
             "The authoritative approval is no longer pending."),
            (Reply(200, {"status": "rejected", "payload_sha256": "a" * 64}, None), "done",
             "The authoritative approval is no longer pending."),
            (Reply(404, None, "not_found"), "done",
             "The authoritative approval is no longer pending."),
            (Reply(200, {"status": "pending", "payload_sha256": "a" * 64}, None), "fresh",
             "The decision did not complete."),
            (Reply(503, None, None), "in_flight", "The authoritative result must be checked again."),
            (RuntimeError("lost"), "in_flight", "The authoritative result must be checked again.")):
        client = FakeBotClient(reply)
        service = _service(in_flight, client=client)
        assert service.reconcile_in_flight(7).text == text
        assert service.state.approvals[0].state == state
        assert client.poll_calls == 0


def test_after_expiry_the_next_primary_click_on_the_new_generation_claims_again():
    service = _service(_record())
    service.handle(_interaction(encode_custom_id("approve", 7)), now=0)
    assert service.bind_confirm(7, 201)
    expired = service.handle(_interaction(encode_custom_id("approve", 7)), now=600)
    assert expired.text == "This confirmation has expired."
    _assert_refreshed(service)
    again = service.handle(_interaction(encode_custom_id("reject", 7), generation=2), now=601)
    assert again.confirm_view is not None
    record = service.state.approvals[0]
    assert (record.state, record.action, record.display_generation) == ("claimed", "reject", 2)
    assert service.bind_confirm(7, 202)
    plan = service.handle(_interaction(encode_custom_id("confirm", 7), message=202, generation=2),
                          now=602).decision
    assert plan is not None and plan.action == "reject"


def test_a_done_record_ignores_every_interaction():
    service = _service(_record(state="done"))
    before = service.state
    for custom, message in (("approve", 101), ("confirm", 101), ("cancel", 201)):
        result = service.handle(_interaction(encode_custom_id(custom, 7), message=message), now=1)
        assert result.text == "This approval is no longer active." and result.decision is None
    assert service.state == before


def test_a_failed_job_returns_the_record_to_fresh_for_another_decision(monkeypatch):
    from afx_ops.gate import service as gate_service
    monkeypatch.setattr(gate_service, "JOB_POLL_INTERVAL_SECONDS", 0)
    client = FakeBotClient(Reply(200, {"status": "pending", "payload_sha256": "a" * 64}, None),
                           Decision(Reply(202, {}, None),
                                    DecisionJob("f" * 32, Actor(f"discord:{USER}"))))
    client.poll_reply = Reply(200, {"state": "failed"}, None)
    service = _service(_record(), client=client)
    plan = _confirming(service)
    assert service.perform(plan).text == "The decision did not complete."
    _assert_refreshed(service)


def test_settle_ends_fresh_and_claimed_records_but_leaves_in_flight_to_the_job():
    service = _service(_record())
    assert service.settle(7) and service.state.approvals[0].state == "done"
    assert not service.settle(7)
    service = _service(_in_flight_with_job())
    assert not service.settle(7) and service.state.approvals[0].state == "in_flight"


def test_a_refresh_or_a_terminal_marks_the_display_edit_and_only_the_same_display_clears_it():
    service = _service(_record())
    service.handle(_interaction(encode_custom_id("approve", 7)), now=0)
    assert service.state.approvals[0].edit_pending is False
    service.handle(_interaction(encode_custom_id("approve", 7)), now=600)
    record = service.state.approvals[0]
    assert (record.state, record.display_generation, record.edit_pending) == ("fresh", 2, True)
    # 古い世代の edit の成功では解かない
    assert not service.edit_done(7, generation=1, state="fresh")
    assert service.state.approvals[0].edit_pending is True
    assert service.edit_done(7, generation=2, state="fresh")
    assert service.state.approvals[0].edit_pending is False
    assert service.settle(7)
    assert service.state.approvals[0].edit_pending is True
    assert not service.edit_done(7, generation=2, state="fresh")
    assert service.state.approvals[0].edit_pending is True


def test_a_done_record_is_dropped_once_its_terminal_display_is_edited():
    service = _service(_record())
    seen = []
    service.persist = lambda: seen.append(service.state)
    assert service.settle(7)
    assert service.edit_done(7, generation=1, state="done")
    assert service.state.approvals == () and seen[-1].approvals == ()
    assert service.handle(_interaction(encode_custom_id("approve", 7)), now=0).text == (
        "This approval is no longer available.")


def test_a_restart_plans_a_terminal_edit_only_for_a_record_still_waiting_for_it():
    from afx_ops.gate.reconcile import plan_reconcile
    waiting = _record(state="done", edit_pending=True)
    assert [e.kind for e in plan_reconcile(BotState.empty().with_record(waiting), ()).effects] == [
        "edit"]
    edited = _record(state="done")
    assert plan_reconcile(BotState.empty().with_record(edited), ()).effects == ()


def _claimed_with_confirm():
    service = _service(_record(), users=(USER, USER + 1))
    service.handle(_interaction(encode_custom_id("claim", 7)), now=0)
    assert service.bind_confirm(7, 201)
    return service


def test_another_users_confirm_leaves_the_claim_and_generation_untouched():
    service = _claimed_with_confirm()
    before = service.state
    result = service.handle(_interaction(encode_custom_id("confirm", 7), message=201,
                                         user=USER + 1), now=1)
    assert result.decision is None and result.text == "This confirmation is no longer valid."
    assert service.state == before
    assert service.handle(_interaction(encode_custom_id("confirm", 7), message=201),
                          now=2).decision is not None


def test_another_users_cancel_leaves_the_claim_and_generation_untouched():
    service = _claimed_with_confirm()
    before = service.state
    result = service.handle(_interaction(encode_custom_id("cancel", 7), message=201,
                                         user=USER + 1), now=1)
    assert result.decision is None and result.text == "This confirmation is no longer valid."
    assert service.state == before
    assert service.handle(_interaction(encode_custom_id("cancel", 7), message=201),
                          now=2).text == "Decision cancelled."
    assert service.state.approvals[0].state == "fresh"


def test_a_stale_primary_click_leaves_the_claim_and_generation_untouched():
    service = _claimed_with_confirm()
    before = service.state
    for kwargs in ({"generation": 0}, {"generation": None}, {"message": 999}):
        result = service.handle(_interaction(encode_custom_id("approve", 7), user=USER + 1,
                                             **kwargs), now=1)
        assert result.decision is None and result.text == "This confirmation is no longer valid."
        assert service.state == before


def _lost_in_flight():
    return _record(state="in_flight", actor=f"discord:{USER}", action="approve",
                   nonce="n" * 32, expiry=600, confirm_message_id=201, nonce_used=True)


def test_an_in_flight_record_without_a_job_and_a_pending_daemon_returns_to_fresh():
    client = FakeBotClient(Reply(200, {"status": "pending", "payload_sha256": "a" * 64}, None))
    service = _service(_lost_in_flight(), client=client)
    assert service.reconcile_in_flight(7).text == "The decision did not complete."
    _assert_refreshed(service)
    assert service.state.approvals[0].edit_pending is True
    assert client.decide_calls == [] and client.poll_calls == 0


def test_an_in_flight_record_with_a_running_job_stays_in_flight():
    client = FakeBotClient(Reply(200, {"status": "pending", "payload_sha256": "a" * 64}, None))
    client.poll_reply = Reply(200, {"state": "running"}, None)
    service = _service(_in_flight_with_job(), client=client)
    assert service.reconcile_in_flight(7).text == "The decision is still in progress."
    record = service.state.approvals[0]
    assert (record.state, record.job_id, record.display_generation) == ("in_flight", "c" * 32, 1)


def test_an_in_flight_record_without_a_job_and_a_non_pending_daemon_is_done():
    client = FakeBotClient(Reply(200, {"status": "approved", "payload_sha256": "a" * 64}, None))
    service = _service(_lost_in_flight(), client=client)
    assert service.reconcile_in_flight(7).text == "The authoritative approval is no longer pending."
    assert service.state.approvals[0].state == "done"


def test_a_transient_or_malformed_detail_before_the_decision_changes_nothing():
    for reply in (Reply(500, None, None), Reply(503, {"status": "pending"}, None),
                  Reply(429, None, None), Reply(200, {"payload_sha256": "a" * 64}, None),
                  Reply(200, "text", None), Reply(200, {"status": 3}, None), None):
        client = FakeBotClient(reply)
        service = _service(_record(), client=client)
        plan = _confirming(service)
        before = service.state
        assert service.perform(plan).text == "The authoritative result must be checked again."
        assert service.state == before and client.decide_calls == []


def test_a_job_poll_404_ends_the_record_and_other_non_2xx_replies_leave_it_in_flight():
    for status in (500, 503, 429, 403, 400):
        client = FakeBotClient(Reply(200, {"status": "pending", "payload_sha256": "a" * 64}, None))
        client.poll_reply = Reply(status, None, None)
        service = _service(_in_flight_with_job(), client=client)
        before = service.state
        assert service.reconcile_in_flight(7).text == "The authoritative result must be checked again."
        assert service.state == before
    client.poll_reply = Reply(404, None, "not_found")
    service = _service(_in_flight_with_job(), client=client)
    assert service.reconcile_in_flight(7).text == "Decision completed."
    assert service.state.approvals[0].state == "done"
    assert client.poll_calls == 2
