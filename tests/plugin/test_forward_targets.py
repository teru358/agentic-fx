"""前方成績の対象: 配備中の戦略ごとの (名前, 内容 hash, 配備世代)。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.plugin import read_model, read_model_inputs, usage_review
from agentic_fx.plugin.read_model import SOURCE_UNAVAILABLE
from agentic_fx.store import signal_forward as sf
from tests.plugin.read_model_world import World

UTC = timezone.utc
NOW = datetime(2027, 1, 31, 12, 0, tzinfo=UTC)
WEEK = timedelta(weeks=1)


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.conn.close()


def targets(world, **replacements):
    import dataclasses
    inputs = read_model_inputs.collect_inputs(world.plugins, world.conn)
    inputs = dataclasses.replace(inputs, **replacements)
    return {t.name: t for t in read_model.forward_targets(inputs)}


def put_strategy(world, name="strat", at=NOW - 5 * WEEK):
    meta = world.deploy(name, kind="strategy")
    approval = world.request(meta, status="approved")
    op = world.journal(name, approval, new_target=f".versions/{name}/{meta.artifact_hash}", at=at)
    return meta, approval, op


def test_a_deployed_strategy_has_the_hash_and_the_generation_of_its_last_switch(world):
    meta, _approval, op = put_strategy(world)
    target = targets(world)["strat"]
    assert target == sf.ForwardTarget("strat", meta.content_hash, NOW - 5 * WEEK,
                                      (usage_review.SOURCE_JOURNAL, op), None)


def test_an_indicator_and_a_missing_plugin_are_not_targets(world):
    world.deploy("rsi")
    assert targets(world) == {}


def test_a_return_to_an_earlier_version_starts_a_new_generation(world):
    meta, approval, _op = put_strategy(world, at=NOW - 30 * WEEK)
    world.journal("strat", approval, new_target=f".versions/strat/{'b' * 64}", at=NOW - 20 * WEEK)
    last = world.journal("strat", approval, new_target=f".versions/strat/{meta.artifact_hash}",
                         at=NOW - 2 * WEEK)
    target = targets(world)["strat"]
    assert (target.started_at, target.generation) == (NOW - 2 * WEEK, (0, last))


def test_a_bless_of_the_same_version_keeps_the_generation(world):
    meta, approval, op = put_strategy(world)
    world.journal("strat", approval, kind="bless", switch_required=0,
                  new_target=f".versions/strat/{meta.artifact_hash}", at=NOW - WEEK)
    target = targets(world)["strat"]
    assert (target.started_at, target.generation) == (NOW - 5 * WEEK, (0, op))


@pytest.mark.parametrize("phase", ["preparing", "recorded", "reverted"])
def test_an_unfinished_or_reverted_switch_keeps_the_generation(world, phase):
    meta, approval, op = put_strategy(world)
    world.journal("strat", approval, phase=phase, new_target=f".versions/strat/{'d' * 64}",
                  at=NOW - WEEK)
    assert targets(world)["strat"].generation == (0, op)


def test_a_finished_rollback_into_the_live_version_is_a_generation_of_the_other_source(world):
    meta, _approval, _op = put_strategy(world)
    rollback = world.deploy_op("strat", op_kind="rollback_symlink",
                               target=meta.artifact_hash, at=NOW - WEEK)
    target = targets(world)["strat"]
    assert (target.started_at, target.generation) == (NOW - WEEK, (1, rollback))


def test_a_rollback_that_did_not_finish_does_not_start_a_generation(world):
    meta, _approval, op = put_strategy(world)
    world.deploy_op("strat", op_kind="rollback_symlink", target=meta.artifact_hash,
                    phase="needs_retry", fail_reason="syscall_failed", at=NOW - WEEK)
    assert targets(world)["strat"].generation == (0, op)


def test_without_any_deployment_record_the_start_is_unknown(world):
    world.deploy("strat", kind="strategy", plain=True)
    target = targets(world)["strat"]
    assert (target.started_at, target.generation) == (None, None)
    assert target.reason == usage_review.REASON_NO_GENERATION


def test_a_last_switch_that_points_elsewhere_is_unknown(world):
    meta, approval, _op = put_strategy(world)
    world.journal("strat", approval, new_target=f".versions/strat/{'e' * 64}", at=NOW - WEEK)
    assert targets(world)["strat"].reason == usage_review.REASON_GENERATION_MISMATCH


def test_an_unreadable_switch_time_makes_the_start_unknown(world):
    meta, approval, _op = put_strategy(world)
    world.journal("strat", approval, new_target=f".versions/strat/{meta.artifact_hash}",
                  at="not a time")
    assert targets(world)["strat"].reason == usage_review.REASON_TIME_UNREADABLE


def test_an_unreadable_journal_makes_every_start_unknown_instead_of_zero(world):
    put_strategy(world)
    target = targets(world, journal_status=SOURCE_UNAVAILABLE, journals=())["strat"]
    assert target.started_at is None and target.reason == usage_review.REASON_JOURNAL_SOURCE


def test_the_indicator_inventory_and_the_forward_target_agree_on_the_generation_start(world):
    meta = world.deploy("rsi")
    approval = world.request(meta, status="approved")
    world.journal("rsi", approval, new_target=f".versions/rsi/{meta.artifact_hash}",
                  at=NOW - 3 * WEEK)
    inputs = read_model_inputs.collect_inputs(world.plugins, world.conn)
    transitions = read_model.transitions_by_name(inputs)["rsi"]
    start, order, reason = usage_review.generation_of(transitions, meta.artifact_hash)
    assert usage_review.generation_start(transitions, meta.artifact_hash) == (start, reason)
    assert start == NOW - 3 * WEEK and reason is None and order[0] == usage_review.SOURCE_JOURNAL


def test_the_reader_scans_the_plugins_and_reads_the_journal_through_the_given_connection(world):
    meta, _approval, op = put_strategy(world)
    found = read_model_inputs.forward_targets_reader(world.plugins)(world.conn)
    assert [(t.name, t.content_hash, t.generation) for t in found] == [
        ("strat", meta.content_hash, (0, op))]
