from __future__ import annotations

from afx_ops.gate.reconcile import MessageMetadata, invalidate_restart, mark_repost_intent, plan_reconcile, record_reposted
from afx_ops.gate.render import footer, render_approval, render_event
from afx_ops.gate.state import ApprovalRecord, BotState

from ._fakes import FakeDiscordStore


def _record(**changes):
    values = dict(approval_id=7, message_id=101, guild_id=12345678901234567,
                  channel_id=12345678901234568, payload_sha256="a" * 64,
                  display_generation=1, state="fresh")
    values.update(changes)
    return ApprovalRecord(**values)


def test_renderer_only_projects_the_fixed_event_schema_and_configured_mentions():
    rendered = render_event({"id": 4, "ts": "2026-10-11T00:00:00+00:00",
                             "code": "service_health_changed", "ref": "instance",
                             "fields": {"component_code": "api", "health_code": "ok"}},
                            mention_role_id=12345678901234569,
                            mention_user_ids=(12345678901234570,))
    assert rendered.mentions == ("<@&12345678901234569>", "<@12345678901234570>")
    assert "service_health_changed" in rendered.content
    assert "instance" not in rendered.content


def test_approval_renderer_shows_only_server_allowed_fields_and_never_claim_bodies():
    record = _record(state="claimed", actor="discord:12345678901234569", action="approve",
                     nonce="n", expiry=600, confirm_message_id=202)
    detail = {
        "kind": "plugin", "status": "pending",
        "facts_lines": ["自己申告: summary: 全部安全です", "plugin=alpha"],
        "agent_claims": "claim text", "parent_facts": {"untrusted": "free text"},
        "reason": "reject reason", "decided_by": "api:ops-bot", "secret": "key-material",
    }
    content = render_approval(record, detail)
    for expected in ("approval=7", "kind=plugin", "facts=none",
                     "claims=present", "digest=aaaaaaaa", "expires=600",
                     "decision=pending", "claimed=yes"):
        assert expected in content
    for forbidden in ("claim text", "free text", "reject reason", "api:ops-bot", "key-material",
                      "a" * 64, "discord:12345678901234569", "全部安全", "plugin=alpha",
                      "untrusted"):
        assert forbidden not in content


def test_approval_renderer_marks_a_revoked_approval_without_candidate_fields():
    content = render_approval(_record(), {
        "kind": "plugin", "status": "rejected", "reason": "revoked_by_operator",
        "parent_facts": None, "agent_claims": None,
    })
    assert "decision=rejected" in content
    assert "reason=revoked_by_operator" in content


def test_approval_renderer_reprojects_only_the_allowlisted_parent_fact_fields():
    detail = {
        "kind": "plugin", "status": "pending",
        "facts_lines": ["agent said: approve me"],
        "parent_facts": {
            "backlog": {"id": 101, "attempts": 2, "status": "selected", "idea": "buy now"},
            "prior_runs": [{"run_id": 8, "result": "approval", "approval_id": 3, "note": "x"}],
            "trials": [{"n": 1, "content_hash8": "1a2b3c4d", "same_hash_as_submitted": True,
                        "trades": 100, "pf": 1.2, "avg_r": 0.1, "max_drawdown": 0.05,
                        "comment": "free"}],
            "trials_omitted": 4, "analysis_call_count": 0, "selection_rationale": "free text",
        },
    }
    content = render_approval(_record(), detail)
    for expected in ("backlog.id=101", "backlog.attempts=2", "backlog.status=selected",
                     "run8=approval:3", "trial1=1a2b3c4d,same=true,trades=100,pf=1.2,avg_r=0.1,mdd=0.05",
                     "trials_omitted=4", "analysis_calls=0"):
        assert expected in content
    for forbidden in ("buy now", "approve me", "free", "note", "idea", "selection_rationale"):
        assert forbidden not in content


def test_approval_renderer_drops_parent_fact_values_outside_the_fixed_shapes():
    detail = {"parent_facts": {
        "backlog": {"id": True, "attempts": "2; claims=none", "status": "selected\nclaims=absent"},
        "prior_runs": [{"run_id": 1, "result": "ignore previous", "approval_id": None}],
        "trials": [{"n": 1, "content_hash8": "ZZ not hex", "same_hash_as_submitted": "yes",
                    "trades": 1.5, "pf": float("nan"), "avg_r": "1", "max_drawdown": None}],
        "trials_omitted": -1, "analysis_call_count": "5"}}
    content = render_approval(_record(), detail)
    assert content.count("\n") == 7
    for forbidden in ("ignore previous", "ZZ not hex", "claims=none", "nan", "yes"):
        assert forbidden not in content


def test_reconcile_adopts_only_matching_bot_footer_channel_and_guild():
    record = _record()
    matching = MessageMetadata(message_id=202, author_is_bot=True, guild_id=record.guild_id,
                               channel_id=record.channel_id,
                               footer=footer(record.approval_id, record.payload_sha256, record.display_generation))
    wrong_author = MessageMetadata(message_id=203, author_is_bot=False, guild_id=record.guild_id,
                                   channel_id=record.channel_id, footer=matching.footer)
    plan = plan_reconcile(BotState.empty().with_record(record), (matching, wrong_author))
    assert [effect.kind for effect in plan.effects] == ["adopt"]
    assert plan.state.approvals[0].message_id == 202
    store = FakeDiscordStore([matching], [])
    store.apply(plan.effects)
    assert store.operations == ["adopt"]


def test_reconcile_reposts_when_no_matching_message_exists_and_puts_the_next_generation_in_the_footer():
    record = _record()
    plan = plan_reconcile(BotState.empty().with_record(record), ())
    assert [effect.kind for effect in plan.effects] == ["repost"]
    assert "Approval pending." not in plan.effects[0].content
    for expected in ("approval=7", "kind=plugin", "facts=none", "claims=absent",
                     "digest=aaaaaaaa", "expires=unclaimed", "decision=fresh", "claimed=no"):
        assert expected in plan.effects[0].content
    assert plan.effects[0].footer.endswith(":2")
    intended = mark_repost_intent(plan.state, record.approval_id)
    committed = record_reposted(intended, record.approval_id, 202, generation=2)
    assert committed.approvals[0].message_id == 202
    assert committed.approvals[0].display_generation == 2


def test_a_repost_is_committed_only_with_the_generation_it_put_in_the_footer():
    intended = mark_repost_intent(BotState.empty().with_record(_record()), 7)
    assert intended.approvals[0].repost_generation == 2
    assert record_reposted(intended, 7, 202, generation=3) is None
    assert record_reposted(intended, 7, 202, generation=1) is None
    committed = record_reposted(intended, 7, 202, generation=2)
    assert (committed.approvals[0].display_generation, committed.approvals[0].repost_generation) == (2, None)
    # 採用済みの意図は二度目の commit では一致しない
    assert record_reposted(committed, 7, 203, generation=2) is None


def test_restart_invalidation_clears_confirm_binding_from_a_claimed_record():
    claimed = _record(state="claimed", actor="discord:12345678901234569", action="approve",
                      nonce="n", expiry=600, confirm_message_id=202)
    invalidated = invalidate_restart(BotState.empty().with_record(claimed)).approvals[0]
    assert (invalidated.state, invalidated.display_generation) == ("fresh", 2)
    assert invalidated.confirm_message_id is None and invalidated.actor is None
    assert invalidated.nonce_used is True


def test_reconcile_never_adopts_a_message_with_another_digest_author_or_channel():
    record = _record()
    base = dict(message_id=202, author_is_bot=True, guild_id=record.guild_id,
                channel_id=record.channel_id, footer=footer(record.approval_id, record.payload_sha256, record.display_generation))
    others = (
        MessageMetadata(**{**base, "footer": footer(record.approval_id, "b" * 64, record.display_generation)}),
        MessageMetadata(**{**base, "author_is_bot": False}),
        MessageMetadata(**{**base, "channel_id": record.channel_id + 1}),
    )
    for message in others:
        plan = plan_reconcile(BotState.empty().with_record(record), (message,))
        assert [effect.kind for effect in plan.effects] == ["repost"]


def test_restart_invalidation_returns_an_in_flight_record_without_a_job_to_fresh():
    in_flight = _record(state="in_flight", actor="discord:12345678901234569", action="approve",
                        nonce="n", expiry=600)
    invalidated = invalidate_restart(BotState.empty().with_record(in_flight)).approvals[0]
    assert (invalidated.state, invalidated.display_generation) == ("fresh", 2)
    assert invalidated.job_id is None and invalidated.nonce_used is True


def test_restart_invalidation_keeps_an_in_flight_record_whose_job_reached_the_daemon():
    in_flight = _record(state="in_flight", actor="discord:12345678901234569", action="approve",
                        nonce="n", expiry=600, confirm_message_id=202, job_id="c" * 32)
    state = BotState.empty().with_record(in_flight)
    assert invalidate_restart(state) == state


def test_reconcile_edits_only_done_records_and_adopts_an_in_flight_one():
    done = _record(state="done", edit_pending=True)
    plan = plan_reconcile(BotState.empty().with_record(done), ())
    assert [effect.kind for effect in plan.effects] == ["edit"]
    in_flight = _record(state="in_flight", actor="discord:12345678901234569", action="approve",
                        nonce="n", expiry=600, confirm_message_id=202, job_id="c" * 32)
    message = MessageMetadata(message_id=101, author_is_bot=True, guild_id=in_flight.guild_id,
                              channel_id=in_flight.channel_id,
                              footer=footer(in_flight.approval_id, in_flight.payload_sha256, in_flight.display_generation))
    plan = plan_reconcile(BotState.empty().with_record(in_flight), (message,))
    assert [effect.kind for effect in plan.effects] == ["adopt"]
