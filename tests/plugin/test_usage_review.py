"""配備中の指標の棚卸し: 4 条件、使用中の優先、読めない行の扱い、出所と停止の提案、読む時刻。"""
from __future__ import annotations

import contextlib
import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.ops.plugins_view import PluginViewCache
from agentic_fx.plugin import read_model, read_model_inputs, usage_review
from agentic_fx.plugin.logic_identity import (
    ParsedDependency, family_key_from_parts, reference_family_key)
from agentic_fx.plugin.read_model import LiveEntry, build_read_model, model_to_dto
from agentic_fx.plugin.usage_review import (
    COND_CANDIDATE, COND_DEPLOY, COND_LIVE, COND_PENDING, K_WEEKS, RESULT_CLEAR,
    RESULT_HOLDS, RESULT_UNKNOWN, STATE_INDETERMINATE, STATE_UNUSED, STATE_USED, UsageFacts,
    UsageScan)
from tests.backtest.factories import SETTINGS
from tests.plugin.read_model_world import World

UTC = timezone.utc
NOW = datetime(2027, 1, 31, 12, 0, tzinfo=UTC)
WEEK = timedelta(weeks=1)
OLD = NOW - 20 * WEEK


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.conn.close()


def dependency_pin(plugin, pin, alias="a"):
    return f"indicators:\n  {alias}: {{plugin: {plugin}, pin: '{pin}'}}\n"


def put_indicator(world, name, *, deployed=OLD, origin="staging", plain=False):
    """実際の配置と、配備を決めた承認・切替の記録を持つ indicator。"""
    meta = world.deploy(name, plain=plain)
    approval = world.request(meta, status="approved", origin=origin)
    if deployed is not None and not plain:
        world.journal(name, approval, new_target=f".versions/{name}/{meta.artifact_hash}",
                      at=deployed)
    return meta


def model_of(world, now=NOW, **replacements):
    inputs = read_model_inputs.collect_inputs(world.plugins, world.conn)
    inputs = dataclasses.replace(inputs, **replacements)
    return build_read_model(inputs, settings=SETTINGS)


def listing(world, now=NOW, **replacements):
    return model_to_dto(model_of(world, **replacements), now=now)


def review(world, name, now=NOW, **replacements):
    dto = listing(world, now, **replacements)
    row = next(row for row in dto["plugins"] if row["name"] == name)
    return row["usage_review"]


def results(usage):
    return {item["id"]: (item["result"], item["reason"]) for item in usage["conditions"]}


def legacy_key(names, pairs=("USDJPY",), timeframe="1h"):
    return family_key_from_parts("strategy", "", timeframe, pairs, names)


def v2_key(plugin, pairs=("USDJPY",), alias="a", timeframe="1h"):
    return reference_family_key(
        timeframe, pairs, [ParsedDependency(alias, plugin, "self", None, timeframe)])


# ---------------------------------------------------------------- 4 条件

def test_an_indicator_that_nothing_uses_is_unused_and_proposable(world):
    put_indicator(world, "rsi")
    usage = review(world, "rsi")
    assert usage["state"] == STATE_UNUSED
    assert all(result == RESULT_CLEAR for result, _ in results(usage).values())
    assert (usage["origin"], usage["proposable"], usage["not_proposable"]) == (
        "staging", True, None)
    assert usage["k_weeks"] == K_WEEKS == 8


def test_a_live_strategy_that_references_it_makes_it_used(world):
    meta = put_indicator(world, "rsi")
    world.deploy("follow", "strategy", dependency_pin("rsi", meta.content_hash))
    usage = review(world, "rsi")
    assert usage["state"] == STATE_USED
    assert results(usage)[COND_LIVE] == (RESULT_HOLDS, None)
    assert usage["proposable"] is False


def test_a_pending_approval_that_references_it_makes_it_used(world):
    meta = put_indicator(world, "rsi")
    world.raw_request({"name": "next", "kind": "strategy", "content_hash": "a" * 64,
                       "indicator_deps": {"x": {"plugin": "rsi",
                                                "content_hash": meta.content_hash}}})
    usage = review(world, "rsi")
    assert usage["state"] == STATE_USED
    assert results(usage)[COND_PENDING] == (RESULT_HOLDS, None)


def test_a_decided_approval_does_not_count_as_a_pending_reference(world):
    meta = put_indicator(world, "rsi")
    world.raw_request({"name": "old", "kind": "strategy", "content_hash": "a" * 64,
                       "indicator_deps": {"x": {"plugin": "rsi",
                                                "content_hash": meta.content_hash}}},
                      status="rejected")
    assert review(world, "rsi")["state"] == STATE_UNUSED


@pytest.mark.parametrize("make_key", [
    lambda name: legacy_key([name]), lambda name: v2_key(name)])
def test_a_recent_strategy_candidate_makes_it_used(world, make_key):
    put_indicator(world, "rsi")
    world.candidate(make_key("rsi"), NOW - 2 * WEEK)
    usage = review(world, "rsi")
    assert usage["state"] == STATE_USED
    assert results(usage)[COND_CANDIDATE] == (RESULT_HOLDS, None)


@pytest.mark.parametrize("make_key", [
    lambda name: legacy_key([name]), lambda name: v2_key(name)])
def test_a_candidate_from_before_the_window_does_not_count(world, make_key):
    put_indicator(world, "rsi")
    world.candidate(make_key("rsi"), NOW - 9 * WEEK)
    assert review(world, "rsi")["state"] == STATE_UNUSED


def test_the_candidate_window_boundary_is_measured_from_the_given_time(world):
    put_indicator(world, "rsi")
    world.candidate(legacy_key(["rsi"]), NOW - K_WEEKS * WEEK)
    assert review(world, "rsi", NOW)["state"] == STATE_USED
    assert review(world, "rsi", NOW + timedelta(seconds=1))["state"] == STATE_UNUSED


def test_the_candidate_condition_is_read_from_the_key_only_for_the_dependency_names(world):
    put_indicator(world, "rsi")
    # pair は設定の外・字句の文法の外でも、依存名は読める
    world.candidate("strategy:rsi@1h@NOTAPAIR,zz", NOW - WEEK)
    assert results(review(world, "rsi"))[COND_CANDIDATE] == (RESULT_HOLDS, None)
    world.candidate(v2_key("ema", pairs=("QQQQQQ",)), NOW - WEEK)
    put_indicator(world, "ema")
    assert results(review(world, "ema"))[COND_CANDIDATE] == (RESULT_HOLDS, None)


def test_a_substring_of_a_dependency_name_is_not_a_use(world):
    put_indicator(world, "rsi")
    world.candidate(legacy_key(["rsi_fast"]), NOW - WEEK)
    world.candidate(v2_key("xrsi"), NOW - WEEK)
    assert review(world, "rsi")["state"] == STATE_UNUSED


def test_an_indicator_candidate_is_not_a_use_and_is_not_an_excluded_row(world):
    put_indicator(world, "rsi")
    world.candidate(family_key_from_parts("indicator", "rsi", "1h", (), ()), NOW - WEEK)
    dto = listing(world)
    assert next(row for row in dto["plugins"]
                if row["name"] == "rsi")["usage_review"]["state"] == STATE_UNUSED
    assert dto["usage_inventory"]["excluded"] == {
        "null_key": 0, "unreadable_key": 0, "unreadable_time": 0}
    assert dto["usage_inventory"]["window_candidates"] == 0


def test_an_indicator_candidate_is_left_out_of_the_strategy_candidate_count(world):
    put_indicator(world, "rsi")
    world.candidate(family_key_from_parts("indicator", "rsi", "1h", (), ()), NOW - WEEK)
    world.candidate(legacy_key(["x1"]), NOW - WEEK)
    world.candidate("garbage", NOW - WEEK)
    summary = listing(world)["usage_inventory"]
    assert summary["window_candidates"] == 1
    assert summary["excluded"]["unreadable_key"] == 1


def test_a_recent_deployment_keeps_an_indicator_in_use_until_k_weeks_pass(world):
    put_indicator(world, "rsi", deployed=NOW - K_WEEKS * WEEK + timedelta(seconds=1))
    usage = review(world, "rsi")
    assert usage["state"] == STATE_USED
    assert results(usage)[COND_DEPLOY] == (RESULT_HOLDS, None)
    assert review(world, "rsi", NOW + timedelta(seconds=1))["state"] == STATE_UNUSED


def test_exactly_k_weeks_after_the_deployment_the_trial_is_over(world):
    put_indicator(world, "rsi", deployed=NOW - K_WEEKS * WEEK)
    assert results(review(world, "rsi"))[COND_DEPLOY] == (RESULT_CLEAR, None)


# ---------------------------------------------------------------- 配備世代

def test_the_generation_start_is_the_last_transition_into_the_live_version(world):
    meta = world.deploy("rsi")
    other = "b" * 64
    first = world.request(meta, status="approved")
    world.journal("rsi", first, new_target=f".versions/rsi/{meta.artifact_hash}",
                  at=NOW - 30 * WEEK)
    world.journal("rsi", first, new_target=f".versions/rsi/{other}", at=NOW - 20 * WEEK)
    world.journal("rsi", first, new_target=f".versions/rsi/{meta.artifact_hash}",
                  at=NOW - 2 * WEEK)
    usage = review(world, "rsi")
    assert results(usage)[COND_DEPLOY] == (RESULT_HOLDS, None)        # A → B → A は再開始


def test_a_bless_of_the_same_version_does_not_restart_the_generation(world):
    meta = world.deploy("rsi")
    approval = world.request(meta, status="approved")
    world.journal("rsi", approval, new_target=f".versions/rsi/{meta.artifact_hash}", at=OLD)
    world.journal("rsi", approval, kind="bless", switch_required=0,
                  new_target=f".versions/rsi/{meta.artifact_hash}", at=NOW - WEEK)
    assert results(review(world, "rsi"))[COND_DEPLOY] == (RESULT_CLEAR, None)


@pytest.mark.parametrize("phase", ["preparing", "versioned", "recorded", "switched", "reverted"])
def test_an_unfinished_or_reverted_switch_does_not_restart_the_generation(world, phase):
    meta = world.deploy("rsi")
    approval = world.request(meta, status="approved")
    world.journal("rsi", approval, new_target=f".versions/rsi/{meta.artifact_hash}", at=OLD)
    world.journal("rsi", approval, phase=phase, new_target=f".versions/rsi/{'d' * 64}",
                  at=NOW - WEEK)
    assert results(review(world, "rsi"))[COND_DEPLOY] == (RESULT_CLEAR, None)


def test_a_finished_rollback_into_the_live_version_restarts_the_generation(world):
    meta = world.deploy("rsi")
    approval = world.request(meta, status="approved")
    world.journal("rsi", approval, new_target=f".versions/rsi/{meta.artifact_hash}", at=OLD)
    world.deploy_op("rsi", op_kind="rollback_symlink", target=meta.artifact_hash,
                    at=NOW - WEEK)
    assert results(review(world, "rsi"))[COND_DEPLOY] == (RESULT_HOLDS, None)


@pytest.mark.parametrize("phase, fail_reason", [
    ("intent", None), ("needs_retry", "syscall_failed"), ("foreign", None)])
def test_a_rollback_that_did_not_finish_does_not_restart_the_generation(
        world, phase, fail_reason):
    meta = world.deploy("rsi")
    approval = world.request(meta, status="approved")
    world.journal("rsi", approval, new_target=f".versions/rsi/{meta.artifact_hash}", at=OLD)
    world.deploy_op("rsi", op_kind="rollback_symlink", target=meta.artifact_hash,
                    phase=phase, fail_reason=fail_reason, at=NOW - WEEK)
    assert results(review(world, "rsi"))[COND_DEPLOY] == (RESULT_CLEAR, None)


def test_a_plugin_without_any_deployment_record_is_indeterminate_for_that_row(world):
    put_indicator(world, "rsi", plain=True)
    usage = review(world, "rsi")
    assert usage["state"] == STATE_INDETERMINATE
    assert results(usage)[COND_DEPLOY] == (RESULT_UNKNOWN, "no_generation_record")


def test_a_last_transition_that_points_elsewhere_is_indeterminate(world):
    meta = world.deploy("rsi")
    approval = world.request(meta, status="approved")
    world.journal("rsi", approval, new_target=f".versions/rsi/{meta.artifact_hash}", at=OLD)
    world.journal("rsi", approval, new_target=f".versions/rsi/{'e' * 64}", at=NOW - WEEK)
    assert results(review(world, "rsi"))[COND_DEPLOY] == (RESULT_UNKNOWN, "generation_mismatch")


def test_an_unreadable_transition_time_is_indeterminate_for_that_row_only(world):
    meta = put_indicator(world, "rsi")
    world.journal("rsi", 1, new_target=f".versions/rsi/{meta.artifact_hash}", at="not a time")
    put_indicator(world, "ema")
    assert results(review(world, "rsi"))[COND_DEPLOY] == (RESULT_UNKNOWN, "time_unreadable")
    assert review(world, "ema")["state"] == STATE_UNUSED


def test_a_time_without_an_offset_is_not_read(world):
    put_indicator(world, "rsi", deployed="2026-01-01T00:00:00")
    assert results(review(world, "rsi"))[COND_DEPLOY] == (RESULT_UNKNOWN, "time_unreadable")


# ---------------------------------------------------------------- 読めない行

def test_a_null_key_is_excluded_and_counted_without_changing_any_verdict(world):
    put_indicator(world, "rsi")
    world.candidate(None, NOW - WEEK)
    dto = listing(world)
    assert next(row for row in dto["plugins"]
                if row["name"] == "rsi")["usage_review"]["state"] == STATE_UNUSED
    assert dto["usage_inventory"]["excluded"]["null_key"] == 1
    assert dto["usage_inventory"]["status"] == "ok"


@pytest.mark.parametrize("key", [
    "strategy:rsi@1h", "strategy-v2:1h@USDJPY@a;rsi;self", "strategy:Bad-Name@1h@USDJPY",
    "strategy-v2:1h@USDJPY@a;%ZZ;self;-;1h", "garbage", "", "strategy:@1h@USDJPY"])
def test_an_unreadable_key_that_does_not_name_a_live_indicator_is_only_counted(world, key):
    put_indicator(world, "macd")
    world.candidate(key, NOW - WEEK)
    dto = listing(world)
    assert next(row for row in dto["plugins"]
                if row["name"] == "macd")["usage_review"]["state"] == STATE_UNUSED
    assert dto["usage_inventory"]["excluded"]["unreadable_key"] == 1


def test_an_unreadable_key_that_contains_a_live_name_makes_only_that_row_indeterminate(world):
    put_indicator(world, "rsi")
    put_indicator(world, "ema")
    world.candidate("strategy:rsi@1h", NOW - WEEK)
    usage = review(world, "rsi")
    assert usage["state"] == STATE_INDETERMINATE
    assert results(usage)[COND_CANDIDATE] == (RESULT_UNKNOWN, "key_mentioned")
    assert review(world, "ema")["state"] == STATE_UNUSED


def test_a_live_name_inside_a_longer_word_of_an_unreadable_key_is_not_a_mention(world):
    put_indicator(world, "rsi")
    world.candidate("strategy:rsi_fast@1h", NOW - WEEK)
    world.candidate("garbage-xrsi", NOW - WEEK)
    assert review(world, "rsi")["state"] == STATE_UNUSED


def test_a_mention_in_an_unreadable_key_older_than_the_window_does_not_matter(world):
    put_indicator(world, "rsi")
    world.candidate("strategy:rsi@1h", NOW - 9 * WEEK)
    dto = listing(world)
    assert next(row for row in dto["plugins"]
                if row["name"] == "rsi")["usage_review"]["state"] == STATE_UNUSED
    assert dto["usage_inventory"]["excluded"]["unreadable_key"] == 0


def test_an_unreadable_creation_time_is_counted_and_its_names_are_indeterminate(world):
    put_indicator(world, "rsi")
    put_indicator(world, "ema")
    world.candidate(legacy_key(["rsi"]), "yesterday")
    dto = listing(world)
    rows = {row["name"]: row["usage_review"] for row in dto["plugins"]}
    assert rows["rsi"]["state"] == STATE_INDETERMINATE
    assert results(rows["rsi"])[COND_CANDIDATE] == (RESULT_UNKNOWN, "time_unreadable")
    assert rows["ema"]["state"] == STATE_UNUSED
    assert dto["usage_inventory"]["excluded"]["unreadable_time"] == 1


def test_a_pending_request_with_no_readable_dependency_record_is_indeterminate(world):
    put_indicator(world, "rsi")
    world.raw_request({"name": "old", "kind": "strategy", "content_hash": "a" * 64})
    usage = review(world, "rsi")
    assert usage["state"] == STATE_INDETERMINATE
    assert results(usage)[COND_PENDING] == (RESULT_UNKNOWN, "pending_request_unreadable")


def test_a_pending_indicator_request_without_dependencies_is_not_a_problem(world):
    put_indicator(world, "rsi")
    world.raw_request({"name": "new", "kind": "indicator", "content_hash": "a" * 64})
    assert review(world, "rsi")["state"] == STATE_UNUSED


def test_a_pending_request_with_a_broken_dependency_entry_is_indeterminate(world):
    put_indicator(world, "rsi")
    world.raw_request({"name": "new", "kind": "strategy", "content_hash": "a" * 64,
                       "indicator_deps": {"a": {"plugin": None}}})
    usage = review(world, "rsi")
    assert usage["state"] == STATE_INDETERMINATE
    assert results(usage)[COND_PENDING] == (RESULT_UNKNOWN, "pending_request_unreadable")


@pytest.mark.parametrize("broken", ["text", None, {"plugin": 3}, {}])
def test_readable_references_hold_while_a_broken_one_leaves_the_rest_indeterminate(
        world, broken):
    put_indicator(world, "rsi")
    put_indicator(world, "ema")
    world.raw_request({"name": "new", "kind": "strategy", "content_hash": "a" * 64,
                       "indicator_deps": {"a": {"plugin": "rsi"}, "b": broken}})
    assert review(world, "rsi")["state"] == STATE_USED
    ema = review(world, "ema")
    assert ema["state"] == STATE_INDETERMINATE
    assert results(ema)[COND_PENDING] == (RESULT_UNKNOWN, "pending_request_unreadable")


# ---------------------------------------------------------------- 全体の状態

def test_a_period_without_candidates_is_shown_as_zero(world):
    put_indicator(world, "rsi")
    summary = listing(world)["usage_inventory"]
    assert summary == {"status": "ok", "reason": None, "k_weeks": 8,
                       "candidates_readable": True, "window_candidates": 0,
                       "excluded": {"null_key": 0, "unreadable_key": 0,
                                    "unreadable_time": 0}}


def test_the_window_counts_only_candidates_inside_it(world):
    put_indicator(world, "rsi")
    world.candidate(legacy_key(["x1"]), NOW - WEEK)
    world.candidate(legacy_key(["x2"]), NOW - 12 * WEEK)
    assert listing(world)["usage_inventory"]["window_candidates"] == 1


def test_a_candidate_table_that_cannot_be_read_leaves_unused_rows_indeterminate(world):
    put_indicator(world, "rsi")
    usage = review(world, "rsi", candidates_status="unavailable")
    assert usage["state"] == STATE_INDETERMINATE
    assert results(usage)[COND_CANDIDATE] == (RESULT_UNKNOWN, "candidates_unreadable")


def test_a_journal_that_cannot_be_read_makes_the_inventory_indeterminate(world):
    put_indicator(world, "rsi")
    dto = listing(world, journal_status="unavailable")
    row = next(row for row in dto["plugins"] if row["name"] == "rsi")["usage_review"]
    assert dto["usage_inventory"]["status"] == "indeterminate"
    assert dto["usage_inventory"]["reason"] == "journal_unreadable"
    assert row["state"] == STATE_INDETERMINATE
    assert results(row)[COND_DEPLOY] == (RESULT_UNKNOWN, "journal_unreadable")


def test_a_scan_that_hit_an_io_error_makes_the_inventory_indeterminate(world):
    put_indicator(world, "rsi")
    inputs = read_model_inputs.collect_inputs(world.plugins, world.conn)
    broken = dataclasses.replace(inputs.live, entries=(
        *inputs.live.entries,
        LiveEntry("flaky", read_model.ENTITY_UNREADABLE, reason="io_error")))
    model = build_read_model(dataclasses.replace(inputs, live=broken), settings=SETTINGS)
    dto = model_to_dto(model, now=NOW)
    row = next(row for row in dto["plugins"] if row["name"] == "rsi")["usage_review"]
    assert dto["usage_inventory"]["status"] == "indeterminate"
    assert dto["usage_inventory"]["reason"] == "scan_failed"
    assert row["state"] == STATE_INDETERMINATE
    assert results(row)[COND_LIVE] == (RESULT_UNKNOWN, "scan_failed")


def test_a_broken_entry_that_is_not_an_io_error_does_not_make_the_inventory_indeterminate(world):
    put_indicator(world, "rsi")
    (world.plugins / "broken").mkdir()
    dto = listing(world)
    assert dto["usage_inventory"]["status"] == "ok"
    assert next(row for row in dto["plugins"]
                if row["name"] == "rsi")["usage_review"]["state"] == STATE_INDETERMINATE


# ---------------------------------------------------------------- 評価順 (使用中の優先)

def facts(**overrides):
    base = dict(name="rsi", live_strategy=False, pending="clear", pending_reason=None,
                deployed_at=OLD, deployed_reason=None, origin="staging", scan_failed=False)
    base.update(overrides)
    return UsageFacts(**base)


UNKNOWN_SOURCES = dict(
    live=dict(scan_failed=True),
    pending=dict(pending="unknown", pending_reason="pending_source_unreadable"),
    deploy=dict(deployed_at=None, deployed_reason="journal_unreadable"))
HOLDING = dict(
    live=dict(live_strategy=True),
    pending=dict(pending="holds"),
    deploy=dict(deployed_at=NOW - WEEK))


@pytest.mark.parametrize("holding", sorted(HOLDING))
@pytest.mark.parametrize("unreadable_candidates", [False, True])
def test_one_holding_condition_decides_use_whatever_else_is_unreadable(
        holding, unreadable_candidates):
    others = [key for key in UNKNOWN_SOURCES if key != holding]
    merged = dict(HOLDING[holding])
    for key in others:
        merged.update(UNKNOWN_SOURCES[key])
    scan = UsageScan(candidates_readable=not unreadable_candidates)
    assert usage_review.classify(facts(**merged), scan, NOW)["state"] == STATE_USED


def test_a_recent_candidate_decides_use_whatever_else_is_unreadable():
    scan = UsageScan(latest_used={"rsi": NOW - WEEK})
    merged = {}
    for source in UNKNOWN_SOURCES.values():
        merged.update(source)
    assert usage_review.classify(facts(**merged), scan, NOW)["state"] == STATE_USED


@pytest.mark.parametrize("source", sorted(UNKNOWN_SOURCES))
def test_with_nothing_holding_an_unreadable_condition_is_indeterminate_not_unused(source):
    verdict = usage_review.classify(facts(**UNKNOWN_SOURCES[source]), UsageScan(), NOW)
    assert verdict["state"] == STATE_INDETERMINATE
    assert verdict["proposable"] is False and verdict["not_proposable"] is None


def test_the_unreadable_candidate_source_alone_is_indeterminate():
    verdict = usage_review.classify(facts(), UsageScan(candidates_readable=False), NOW)
    assert verdict["state"] == STATE_INDETERMINATE


# ---------------------------------------------------------------- 出所と停止の提案

@pytest.mark.parametrize("origin, proposable, why", [
    ("staging", True, None), ("human", False, "human_origin")])
def test_only_an_unused_indicator_with_a_confirmed_staging_origin_can_be_proposed(
        world, origin, proposable, why):
    put_indicator(world, "rsi", origin=origin)
    usage = review(world, "rsi")
    assert usage["state"] == STATE_UNUSED
    assert (usage["origin"], usage["proposable"], usage["not_proposable"]) == (
        origin, proposable, why)


def test_a_version_without_a_submission_request_has_an_unknown_origin(world):
    world.deploy("rsi")
    assert review(world, "rsi")["origin"] == "unknown"


def test_an_unknown_origin_is_not_proposable_even_when_unused(world):
    meta = world.deploy("rsi")
    world.journal("rsi", 1, new_target=f".versions/rsi/{meta.artifact_hash}", at=OLD)
    usage = review(world, "rsi")
    assert usage["state"] == STATE_UNUSED
    assert (usage["origin"], usage["proposable"], usage["not_proposable"]) == (
        "unknown", False, "unknown_origin")


def test_a_reevaluation_inherits_the_origin_of_the_submission_of_the_same_version(world):
    meta = put_indicator(world, "rsi", origin="staging")
    world.request(meta, status="approved", origin="reevaluation")
    usage = review(world, "rsi")
    assert (usage["origin"], usage["proposable"]) == ("staging", True)


def test_a_reevaluation_alone_leaves_the_origin_unknown(world):
    meta = world.deploy("rsi")
    approval = world.request(meta, status="approved", origin="reevaluation")
    world.journal("rsi", approval, new_target=f".versions/rsi/{meta.artifact_hash}", at=OLD)
    usage = review(world, "rsi")
    assert (usage["origin"], usage["proposable"], usage["not_proposable"]) == (
        "unknown", False, "unknown_origin")


def test_the_origin_is_judged_on_the_live_version_not_on_the_name(world):
    put_indicator(world, "rsi", origin="staging")
    world.undeploy("rsi")
    second = world.deploy("rsi", extra="# revised\n")
    approval = world.request(second, status="approved", origin="human")
    world.journal("rsi", approval, new_target=f".versions/rsi/{second.artifact_hash}",
                  at=OLD)
    usage = review(world, "rsi")
    assert (usage["origin"], usage["proposable"], usage["not_proposable"]) == (
        "human", False, "human_origin")


def test_a_rejected_or_pending_submission_does_not_confirm_the_origin(world):
    meta = world.deploy("rsi")
    world.request(meta, status="rejected", origin="staging")
    world.journal("rsi", 1, new_target=f".versions/rsi/{meta.artifact_hash}", at=OLD)
    assert review(world, "rsi")["origin"] == "unknown"


# ---------------------------------------------------------------- 出し方

def test_only_a_readable_live_indicator_has_a_review(world):
    put_indicator(world, "rsi")
    world.deploy("follow", "strategy")
    world.version_only("gone")
    world.request(world.deploy("sig", "signal"), status="approved")
    dto = listing(world)
    by_name = {row["name"]: row["usage_review"] for row in dto["plugins"]}
    assert by_name["rsi"] is not None
    assert by_name["follow"] is None and by_name["sig"] is None and by_name["gone"] is None


def test_the_detail_response_carries_the_review(world):
    put_indicator(world, "rsi")
    detail = read_model.detail_to_dto(model_of(world), "rsi", now=NOW)
    assert detail["plugin"]["usage_review"]["state"] == STATE_UNUSED


def test_the_review_uses_only_fixed_words_and_numbers(world):
    put_indicator(world, "rsi")
    world.candidate("strategy:rsi@1h", NOW - WEEK)
    usage = review(world, "rsi")
    assert set(usage) == {"state", "conditions", "origin", "proposable", "not_proposable",
                          "k_weeks"}
    assert [item["id"] for item in usage["conditions"]] == [
        COND_LIVE, COND_PENDING, COND_CANDIDATE, COND_DEPLOY]


# ---------------------------------------------------------------- 読む時刻と memo

def test_the_candidate_generation_is_part_of_the_database_generation(world):
    before = read_model_inputs.db_generation(world.conn)
    world.candidate(legacy_key(["rsi"]), NOW)
    assert read_model_inputs.db_generation(world.conn) != before


def test_the_view_cache_judges_at_each_read_time_without_rebuilding(world):
    put_indicator(world, "rsi", deployed=NOW - K_WEEKS * WEEK + timedelta(hours=1))
    clock = {"now": NOW}
    cache = PluginViewCache(world.plugins, SETTINGS, clock=lambda: clock["now"])

    def state():
        view = cache.get(world.conn, contextlib.nullcontext, lambda lock: lock.acquire())
        return next(row for row in view.listing["plugins"]
                    if row["name"] == "rsi")["usage_review"]["state"]

    assert state() == STATE_USED
    clock["now"] = NOW + timedelta(hours=2)
    assert state() == STATE_UNUSED
    assert cache.builds == 1


def test_a_new_candidate_invalidates_the_cached_model(world):
    put_indicator(world, "rsi")
    cache = PluginViewCache(world.plugins, SETTINGS, clock=lambda: NOW)

    def state():
        view = cache.get(world.conn, contextlib.nullcontext, lambda lock: lock.acquire())
        return next(row for row in view.listing["plugins"]
                    if row["name"] == "rsi")["usage_review"]["state"]

    assert state() == STATE_UNUSED
    world.candidate(legacy_key(["rsi"]), NOW - WEEK)
    assert state() == STATE_USED
    assert cache.builds == 2


def test_a_family_key_filled_in_later_invalidates_the_cached_model(world):
    put_indicator(world, "rsi")
    row_id = world.candidate(None, NOW - WEEK)
    cache = PluginViewCache(world.plugins, SETTINGS, clock=lambda: NOW)

    def state():
        view = cache.get(world.conn, contextlib.nullcontext, lambda lock: lock.acquire())
        return next(row for row in view.listing["plugins"]
                    if row["name"] == "rsi")["usage_review"]["state"]

    assert state() == STATE_UNUSED
    world.conn.execute("UPDATE candidate_archives SET family_key=? WHERE id=?",
                       (legacy_key(["rsi"]), row_id))
    world.conn.commit()
    assert state() == STATE_USED
    assert cache.builds == 2


def test_replacing_a_family_key_with_another_of_the_same_length_changes_the_generation(world):
    row_id = world.candidate(legacy_key(["rsi"]), NOW - WEEK)
    before = read_model_inputs.db_generation(world.conn)
    world.conn.execute("UPDATE candidate_archives SET family_key=? WHERE id=?",
                       (legacy_key(["ema"]).replace("ema", "rsi"[::-1]), row_id))
    world.conn.commit()
    assert read_model_inputs.db_generation(world.conn) != before


# ---------------------------------------------------------------- 世代値の費用と範囲

def test_a_family_key_outside_the_window_does_not_change_the_generation(world):
    row_id = world.candidate(legacy_key(["rsi"]), NOW - 30 * WEEK)
    before = read_model_inputs.db_generation(world.conn, NOW)
    world.conn.execute("UPDATE candidate_archives SET family_key=? WHERE id=?",
                       (legacy_key(["ema"]), row_id))
    world.conn.commit()
    assert read_model_inputs.db_generation(world.conn, NOW) == before


def test_a_family_key_inside_the_window_changes_the_generation(world):
    row_id = world.candidate(legacy_key(["rsi"]), NOW - WEEK)
    before = read_model_inputs.db_generation(world.conn, NOW)
    world.conn.execute("UPDATE candidate_archives SET family_key=? WHERE id=?",
                       (legacy_key(["ema"]), row_id))
    world.conn.commit()
    assert read_model_inputs.db_generation(world.conn, NOW) != before


def test_the_generation_changes_when_the_window_start_moves_to_another_day(world):
    world.candidate(legacy_key(["rsi"]), NOW - WEEK)
    assert (read_model_inputs.db_generation(world.conn, NOW)
            != read_model_inputs.db_generation(world.conn, NOW + timedelta(days=2)))


def test_the_generation_cost_does_not_grow_with_old_candidate_keys(world):
    import time
    world.candidate(legacy_key(["rsi"]), NOW - WEEK)
    big = "strategy:" + ",".join(f"p{i}" for i in range(120)) + "@1h@USDJPY"
    world.conn.executemany(
        "INSERT INTO candidate_archives(mission_id,name,content_hash,artifact_hash,"
        "archive_path,pair,metrics_json,created_at,family_key) VALUES (1,'o',?,?,NULL,NULL,'{}',?,?)",
        [(f"{i + 10**6:064x}", f"{i + 10**6:064x}", (NOW - 30 * WEEK).isoformat(), big + str(i))
         for i in range(1, 20001)])
    world.conn.commit()

    def best(call):
        times = []
        for _ in range(5):
            start = time.perf_counter()
            call()
            times.append(time.perf_counter() - start)
        return min(times)

    scan = best(lambda: world.conn.execute(
        "SELECT COUNT(*), MAX(id), MAX(created_at) FROM candidate_archives").fetchone())
    generation = best(lambda: read_model_inputs.db_generation(world.conn, NOW))
    assert generation < 4 * scan + 0.002


# ---------------------------------------------------------------- 読めない strategy と lazy な一覧

def test_an_unreadable_deployed_entry_makes_the_live_condition_unknown(world):
    put_indicator(world, "rsi")
    (world.plugins / "follow").mkdir()
    usage = review(world, "rsi")
    assert usage["state"] == STATE_INDETERMINATE
    assert results(usage)[COND_LIVE] == (RESULT_UNKNOWN, "strategy_unreadable")
    assert usage["proposable"] is False


def test_a_non_canonical_entry_name_is_not_a_deployed_strategy(world):
    put_indicator(world, "rsi")
    (world.plugins / "Not A Plugin").mkdir()
    assert review(world, "rsi")["state"] == STATE_UNUSED


def test_a_live_reference_still_wins_over_an_unreadable_entry(world):
    meta = put_indicator(world, "rsi")
    world.deploy("follow", "strategy", dependency_pin("rsi", meta.content_hash))
    (world.plugins / "broken").mkdir()
    assert review(world, "rsi")["state"] == STATE_USED


def test_a_row_without_the_scan_is_never_judged_clear(world):
    put_indicator(world, "rsi")
    row = next(row for row in model_of(world).rows if row.name == "rsi")
    for dto in (read_model.row_to_dto(row, now=NOW),
                read_model.row_to_dto(row, detail=True, now=NOW)):
        assert dto["usage_review"]["state"] == STATE_INDETERMINATE
        assert dto["usage_review"]["proposable"] is False


def test_the_detail_view_does_not_build_the_listing(world, monkeypatch):
    put_indicator(world, "rsi")
    calls = []
    original = read_model.model_to_dto
    monkeypatch.setattr(read_model, "model_to_dto",
                        lambda *args, **kwargs: calls.append(1) or original(*args, **kwargs))
    cache = PluginViewCache(world.plugins, SETTINGS, clock=lambda: NOW)
    view = cache.get(world.conn, contextlib.nullcontext, lambda lock: lock.acquire())
    assert view.model is not None and calls == []
    assert view.listing["plugins"] and calls == [1]
    assert view.listing is view.listing and calls == [1]


def test_unreadable_and_null_key_rows_are_not_counted_as_strategy_candidates(world):
    put_indicator(world, "rsi")
    world.candidate(legacy_key(["x1"]), NOW - WEEK)
    world.candidate(None, NOW - WEEK)
    world.candidate("garbage", NOW - WEEK)
    summary = listing(world)["usage_inventory"]
    assert summary["window_candidates"] == 1
    assert summary["excluded"]["null_key"] == 1
    assert summary["excluded"]["unreadable_key"] == 1


@pytest.mark.parametrize("created_at", ["2020-bad", "2020-01-01T00:00:00", "2020-13-45"])
def test_a_key_change_on_a_row_whose_time_cannot_be_read_changes_the_generation(world, created_at):
    row_id = world.candidate(legacy_key(["rsi"]), created_at)
    before = read_model_inputs.db_generation(world.conn, NOW)
    world.conn.execute("UPDATE candidate_archives SET family_key=? WHERE id=?",
                       (legacy_key(["ema"]), row_id))
    world.conn.commit()
    assert read_model_inputs.db_generation(world.conn, NOW) != before
