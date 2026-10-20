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


def _change(**changes):
    change = {"name": "sma_cross", "kind": "strategy", "classification": "updated",
              "candidate_hash": "b" * 64, "baseline_hash": "c" * 64,
              "computed_at": "2026-10-19T00:00:00+00:00", "reason": None,
              "params": {"items": [{"key": "params.period", "old": "14", "new": "10"}],
                         "omitted": 0, "invalid_keys": 0, "flatten_omitted": 0, "reason": None},
              "performance": {"in_sample": [{"pair": "USDJPY", "trades": 120, "pf": 1.25,
                                             "avg_r": 0.05}],
                              "holdout": [{"pair": "HOLDOUT_PAIR", "trades": 777, "pf": 9.99,
                                           "avg_r": 8.88}]},
              "summary": "短い申告です"}
    change.update(changes)
    return change


def test_ac11_approval_renderer_shows_the_summary_but_not_the_rationale_or_other_fields():
    record = _record(state="claimed", actor="discord:12345678901234569", action="approve",
                     nonce="n", expiry=600, confirm_message_id=202)
    detail = {
        "kind": "plugin", "status": "pending", "change": _change(),
        "facts_lines": ["自己申告: summary: 全部安全です", "plugin=alpha"],
        "agent_claims": {"summary": "RAW_SUMMARY", "selection_rationale": "RATIONALE_BODY"},
        "parent_facts": {"untrusted": "free text"}, "in_sample": {"RAW_IN": 1},
        "holdout": {"RAW_HOLDOUT": 2}, "reason": "reject reason", "decided_by": "api:ops-bot",
        "secret": "key-material",
    }
    content = render_approval(record, detail)
    assert "短い申告です" in content
    for forbidden in ("RAW_SUMMARY", "RATIONALE_BODY", "selection_rationale", "free text",
                      "reject reason", "api:ops-bot", "key-material", "a" * 64,
                      "discord:12345678901234569", "全部安全", "plugin=alpha", "untrusted",
                      "RAW_IN", "RAW_HOLDOUT", "HOLDOUT_PAIR", "777", "9.99", "b" * 64):
        assert forbidden not in content
    # 申告は専用の見出しの下にだけ出て、事実欄 (変更・成績) に混ざらない
    sections = content.split("\n\n")
    claim = [part for part in sections if "短い申告です" in part]
    assert len(claim) == 1 and claim[0].startswith("agent の申告 (未検証):\n> ")
    for part in sections:
        if part.startswith(("変更", "成績")):
            assert "申告" not in part


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
    assert plan.effects[0].content == "承認 #7\n詳細を取得できません"
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
