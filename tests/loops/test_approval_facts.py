from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone

import pytest

from agentic_fx.loops import approval_facts
from agentic_fx.loops.approval_facts import FactsError, build_facts_payload
from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
from agentic_fx.loops.improve_run_context import ImproveRunContext
from agentic_fx.store import backlog as backlog_store
from agentic_fx.store import improve_runs as improve_runs_store
from agentic_fx.store import missions as missions_store

NOW = datetime(2026, 10, 2, tzinfo=timezone.utc)
SUBMITTED = "1a2b3c4d" + "0" * 56


def _bt(content_hash=SUBMITTED, *, trades=100, pf=1.2, avg_r=0.1,
        max_drawdown=0.05, error=None):
    summary = ({"error": error} if error else {
        "scope": "in_sample", "content_hash": content_hash,
        "metrics": {"trades": trades, "pf": pf, "avg_r": avg_r,
                    "max_drawdown": max_drawdown}})
    return {"opaque_ref": "bt", "kind": "run_backtest",
            "params": {"name": "x", "pair": "USDJPY"},
            "result_summary": summary, "trial_count": 1}


def _ac(opaque="ac"):
    return {"opaque_ref": opaque, "kind": "analyze_corr", "params": {},
            "result_summary": {"params": {}, "trial_count": 1,
                               "source": "improve_agent"},
            "trial_count": 1}


def _accepted(entries):
    return [e for e in entries if "error" not in e["result_summary"]]


def _setup(conn, clock, *, source="agent", idea="課題の文面"):
    missions_store.start(conn, "improve", "codex", "m", clock.now(),
                         commit=False)
    mission_id = missions_store.start(conn, "improve", "codex", "m",
                                      clock.now(), commit=False)
    backlog_id = backlog_store.add(conn, idea, source, clock.now())
    run_id = improve_runs_store.start(conn, backlog_id, clock.now(),
                                      mission_id=mission_id, commit=False)
    conn.execute("UPDATE improvement_backlog SET status='selected', "
                 "attempts=1 WHERE id=?", (backlog_id,))
    conn.commit()
    return mission_id, run_id, backlog_id


def _build(conn, run_id, backlog_id, entries, **kw):
    kw.setdefault("selection_rationale", "意図")
    kw.setdefault("summary", "概要")
    return build_facts_payload(
        conn, run_id=run_id, backlog_id=backlog_id,
        accepted_entries=_accepted(entries),
        submitted_content_hash=SUBMITTED, **kw)


def _finished_run(conn, backlog_id, result, approval_id=None, finished=True,
                  mission_status=None, report_state="none"):
    mission_id = None
    if mission_status is not None:
        mission_id = missions_store.start(conn, "improve", "codex", "m", NOW,
                                          commit=False)
        if mission_status != "running":
            missions_store.finish(conn, mission_id, mission_status, None, [],
                                  NOW, commit=False)
    run_id = improve_runs_store.start(conn, backlog_id, NOW,
                                      mission_id=mission_id, commit=False)
    if finished:
        improve_runs_store.finish(conn, run_id, result=result, now=NOW,
                                  approval_id=approval_id,
                                  report_state=report_state, commit=False)
    conn.commit()
    return run_id


def test_trials_are_numbered_in_accepted_order_and_exclude_errors(
        conn, clock):
    _, run_id, backlog_id = _setup(conn, clock)
    entries = [_bt("a" * 64, trades=1), _bt(error="timeout"),
               _bt("b" * 64, trades=2), _bt(SUBMITTED, trades=3)]
    facts = _build(conn, run_id, backlog_id, entries)["parent_facts"]
    assert [(t["n"], t["content_hash8"], t["trades"],
             t["same_hash_as_submitted"]) for t in facts["trials"]] == [
        (1, "aaaaaaaa", 1, False), (2, "bbbbbbbb", 2, False),
        (3, "1a2b3c4d", 3, True)]
    assert facts["trials_omitted"] == 0


def test_more_than_twenty_trials_keep_first_twenty_and_count_omitted(
        conn, clock):
    _, run_id, backlog_id = _setup(conn, clock)
    entries = [_bt(trades=i) for i in range(1, 24)]
    facts = _build(conn, run_id, backlog_id, entries)["parent_facts"]
    assert [t["trades"] for t in facts["trials"]] == list(range(1, 21))
    assert facts["trials_omitted"] == 3


def test_the_submitted_hash_row_is_kept_even_when_it_is_past_the_twentieth(
        conn, clock):
    _, run_id, backlog_id = _setup(conn, clock)
    entries = [_bt("a" * 64, trades=i) for i in range(1, 24)]
    entries[21] = _bt(SUBMITTED, trades=999)
    facts = _build(conn, run_id, backlog_id, entries)["parent_facts"]
    trials = facts["trials"]
    assert len(trials) == 20
    assert [t["n"] for t in trials] == sorted(t["n"] for t in trials)
    assert [t["n"] for t in trials if t["same_hash_as_submitted"]] == [22]
    assert [t["n"] for t in trials if not t["same_hash_as_submitted"]] == \
        list(range(1, 20))
    assert facts["trials_omitted"] == 3


def test_more_than_twenty_submitted_hash_rows_are_cut_at_twenty(conn, clock):
    _, run_id, backlog_id = _setup(conn, clock)
    entries = [_bt(SUBMITTED, trades=i) for i in range(1, 26)]
    facts = _build(conn, run_id, backlog_id, entries)["parent_facts"]
    assert [t["n"] for t in facts["trials"]] == list(range(1, 21))
    assert facts["trials_omitted"] == 5


def test_analysis_call_count_counts_accepted_analyses_only(conn, clock):
    _, run_id, backlog_id = _setup(conn, clock)
    entries = [_ac("a"), _ac("b"), {**_ac("c"),
                                    "result_summary": {"error": "x"}}]
    facts = _build(conn, run_id, backlog_id, entries)["parent_facts"]
    assert facts["analysis_call_count"] == 2


def test_parent_facts_key_sets_match_the_fixed_schema(conn, clock):
    _, run_id, backlog_id = _setup(conn, clock)
    _finished_run(conn, backlog_id, None)
    payload = _build(conn, run_id, backlog_id, [_bt()])
    facts = payload["parent_facts"]
    assert payload["facts_version"] == 1
    assert set(payload) == {"facts_version", "parent_facts", "agent_claims"}
    assert set(facts) == {"backlog", "prior_runs", "trials",
                          "trials_omitted", "analysis_call_count"}
    assert set(facts["backlog"]) == {"id", "attempts", "status"}
    assert set(facts["trials"][0]) == {
        "n", "content_hash8", "same_hash_as_submitted", "trades", "pf",
        "avg_r", "max_drawdown"}
    assert set(facts["prior_runs"][0]) == {"run_id", "result", "approval_id"}
    assert facts["backlog"] == {"id": backlog_id, "attempts": 1,
                                "status": "selected"}


def _valid_facts(conn, clock):
    _, run_id, backlog_id = _setup(conn, clock)
    _finished_run(conn, backlog_id, None)
    return _build(conn, run_id, backlog_id, [_bt()])["parent_facts"]


def test_validator_rejects_extra_or_missing_keys_at_every_level(conn, clock):
    base = _valid_facts(conn, clock)

    def mutated(path, fn):
        facts = json.loads(json.dumps(base))
        fn(facts)
        with pytest.raises(FactsError):
            approval_facts.validate_parent_facts(facts)

    mutated("root extra", lambda f: f.update(extra=1))
    mutated("root missing", lambda f: f.pop("trials"))
    mutated("backlog extra", lambda f: f["backlog"].update(idea="x"))
    mutated("trial extra", lambda f: f["trials"][0].update(note="x"))
    mutated("trial missing", lambda f: f["trials"][0].pop("pf"))
    mutated("run extra", lambda f: f["prior_runs"][0].update(reason="x"))
    mutated("run missing", lambda f: f["prior_runs"][0].pop("result"))


def test_prior_runs_only_same_backlog_finished_other_runs(conn, clock):
    _, run_id, backlog_id = _setup(conn, clock)
    other = backlog_store.add(conn, "別課題", "agent", clock.now())
    approval_run = _finished_run(conn, backlog_id, "approval", approval_id=7)
    report_run = _finished_run(conn, backlog_id, "report")
    null_run = _finished_run(conn, backlog_id, None)
    _finished_run(conn, backlog_id, None, finished=False)
    _finished_run(conn, other, None)
    facts = _build(conn, run_id, backlog_id, [])["parent_facts"]
    assert facts["prior_runs"] == [
        {"run_id": null_run, "result": None, "approval_id": None},
        {"run_id": report_run, "result": "report", "approval_id": None},
        {"run_id": approval_run, "result": "approval", "approval_id": 7}]


def test_prior_runs_without_a_result_are_told_apart_by_mission_and_report_state(
        conn, clock):
    _, run_id, backlog_id = _setup(conn, clock)
    observed = _finished_run(conn, backlog_id, None,
                             mission_status="completed")
    failed = _finished_run(conn, backlog_id, None, mission_status="failed")
    interrupted = _finished_run(conn, backlog_id, None,
                                mission_status="interrupted")
    report_failed = _finished_run(conn, backlog_id, None,
                                  mission_status="completed",
                                  report_state="failed")
    unknown = _finished_run(conn, backlog_id, None)
    facts = _build(conn, run_id, backlog_id, [])["parent_facts"]
    assert {r["run_id"]: r["result"] for r in facts["prior_runs"]} == {
        observed: "observation", failed: "failed",
        interrupted: "interrupted", report_failed: "report_failed",
        unknown: None}
    approval_facts.validate_parent_facts(facts)


def test_prior_runs_exclude_current_run_even_when_finished(conn, clock):
    _, run_id, backlog_id = _setup(conn, clock)
    improve_runs_store.finish(conn, run_id, result=None, now=NOW)
    facts = _build(conn, run_id, backlog_id, [])["parent_facts"]
    assert facts["prior_runs"] == []


def test_prior_runs_keep_newest_ten(conn, clock):
    _, run_id, backlog_id = _setup(conn, clock)
    ids = [_finished_run(conn, backlog_id, None) for _ in range(12)]
    facts = _build(conn, run_id, backlog_id, [])["parent_facts"]
    assert [r["run_id"] for r in facts["prior_runs"]] == ids[::-1][:10]


@pytest.mark.parametrize("run", [
    {"run_id": 1, "result": "bogus", "approval_id": None},
    {"run_id": 1, "result": "approval", "approval_id": None},
    {"run_id": 1, "result": "approval", "approval_id": 0},
    {"run_id": 1, "result": "approval", "approval_id": True},
    {"run_id": 1, "result": "approval", "approval_id": "7"},
    {"run_id": 1, "result": "report", "approval_id": 7},
    {"run_id": 1, "result": None, "approval_id": 7},
    {"run_id": True, "result": None, "approval_id": None},
    {"run_id": "1", "result": None, "approval_id": None},
])
def test_validator_rejects_inconsistent_prior_run(conn, clock, run):
    facts = _valid_facts(conn, clock)
    facts["prior_runs"] = [run]
    with pytest.raises(FactsError):
        approval_facts.validate_parent_facts(facts)


@pytest.mark.parametrize("field,value", [
    ("trades", True), ("trades", 1.5), ("trades", -1),
    ("pf", True), ("pf", float("nan")), ("pf", float("inf")), ("pf", "1.2"),
    ("avg_r", float("-inf")), ("max_drawdown", False),
    ("content_hash8", "ZZZZZZZZ"), ("content_hash8", "1a2b"),
    ("same_hash_as_submitted", 1), ("n", 0),
])
def test_validator_rejects_bad_trial_values(conn, clock, field, value):
    facts = _valid_facts(conn, clock)
    facts["trials"][0][field] = value
    with pytest.raises(FactsError):
        approval_facts.validate_parent_facts(facts)


@pytest.mark.parametrize("path,value", [
    (("backlog", "status"), "weird"), (("backlog", "id"), True),
    (("backlog", "attempts"), -1), (("trials_omitted"), True),
    (("analysis_call_count"), 1.0), (("prior_runs"), {}),
    (("trials"), "x"),
])
def test_validator_rejects_bad_root_values(conn, clock, path, value):
    facts = _valid_facts(conn, clock)
    if isinstance(path, tuple):
        facts[path[0]][path[1]] = value
    else:
        facts[path] = value
    with pytest.raises(FactsError):
        approval_facts.validate_parent_facts(facts)


def test_trial_with_no_trades_keeps_none_metrics(conn, clock):
    _, run_id, backlog_id = _setup(conn, clock)
    entry = _bt(trades=0, pf=None, avg_r=None)
    trial = _build(conn, run_id, backlog_id, [entry]
                   )["parent_facts"]["trials"][0]
    assert (trial["trades"], trial["pf"], trial["avg_r"]) == (0, None, None)


def test_malformed_ledger_entry_raises_facts_error(conn, clock):
    _, run_id, backlog_id = _setup(conn, clock)
    entry = _bt()
    entry["result_summary"]["metrics"] = None
    with pytest.raises(FactsError):
        _build(conn, run_id, backlog_id, [entry])


def test_agent_free_text_never_enters_parent_facts(conn, clock):
    mission_id, run_id, backlog_id = _setup(
        conn, clock, source="agent", idea="MARK_IDEA")
    conn.execute("UPDATE improvement_backlog SET last_result=? WHERE id=?",
                 ("MARK_LAST", backlog_id))
    other = _finished_run(conn, backlog_id, "report")
    conn.execute("UPDATE improvement_runs SET report_path='MARK_PATH' "
                 "WHERE id=?", (other,))
    conn.commit()
    entry = _bt()
    entry["params"] = {"name": "MARK_NAME", "pair": "MARK_PAIR"}
    entry["result_summary"]["plugin_ref"] = "MARK_REF"
    payload = _build(conn, run_id, backlog_id, [entry],
                     selection_rationale="MARK_RATIONALE",
                     summary="MARK_SUMMARY")
    dumped = json.dumps(payload["parent_facts"], ensure_ascii=False)
    for mark in ("MARK_IDEA", "MARK_LAST", "MARK_PATH", "MARK_NAME",
                 "MARK_PAIR", "MARK_REF", "MARK_RATIONALE", "MARK_SUMMARY"):
        assert mark not in dumped
    assert payload["agent_claims"] == {
        "selection_rationale": "MARK_RATIONALE", "summary": "MARK_SUMMARY",
        "selected_backlog_idea": "MARK_IDEA"}


@pytest.mark.parametrize("source,quoted", [
    ("agent", True), ("research", True), ("user", False), ("system", False)])
def test_selected_idea_is_quoted_only_for_agent_or_research_origin(
        conn, clock, source, quoted):
    _, run_id, backlog_id = _setup(conn, clock, source=source)
    claims = _build(conn, run_id, backlog_id, [])["agent_claims"]
    assert ("selected_backlog_idea" in claims) is quoted
    assert "source" not in json.dumps(claims)


def test_long_claims_are_truncated_on_save(conn, clock):
    _, run_id, backlog_id = _setup(conn, clock)
    claims = _build(conn, run_id, backlog_id, [],
                    selection_rationale="あ" * 5000)["agent_claims"]
    assert len(claims["selection_rationale"]) == (
        2000 + len(approval_facts.TRUNCATION_SUFFIX))
    assert claims["selection_rationale"].endswith(
        approval_facts.TRUNCATION_SUFFIX)


def test_non_string_claims_are_not_stored(conn, clock):
    _, run_id, backlog_id = _setup(conn, clock)
    claims = _build(conn, run_id, backlog_id, [], selection_rationale=["x"],
                    summary=None)["agent_claims"]
    assert "selection_rationale" not in claims and "summary" not in claims


def test_missing_backlog_gives_null_backlog_and_no_prior_runs(conn, clock):
    _, run_id, _ = _setup(conn, clock)
    payload = _build(conn, run_id, None, [])
    assert payload["parent_facts"]["backlog"] is None
    assert payload["parent_facts"]["prior_runs"] == []
    assert "selected_backlog_idea" not in payload["agent_claims"]


# ---- Tx-2 への配線 (approval 行に保存される) ----

def _ctx(tmp_path, mission_id, run_id, entries):
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={})
    for entry in entries:
        ledger.record(**entry)
    ledger.freeze()
    staging = tmp_path / "plugins" / "_staging" / str(mission_id)
    staging.mkdir(parents=True, exist_ok=True)
    return ImproveRunContext(
        mission_id=mission_id, run_id=run_id, staging_dir=staging,
        source_snapshot_dir=staging / "source", allowed_backlog_ids=None,
        slot_key=None, ledger=ledger, rpc_handlers={})


def _stored_payload(conn):
    row = conn.execute("SELECT payload_json FROM approval_requests "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(row["payload_json"])


def test_finalize_success_stores_facts_in_the_approval_payload(
        loop_min, conn, clock, tmp_path):
    mission_id, run_id, backlog_id = _setup(conn, clock)
    prior = _finished_run(conn, backlog_id, None)
    entries = [_ac(), _bt(error="timeout")]
    ctx = _ctx(tmp_path, mission_id, run_id, entries)
    loop_min._finalize_success(
        conn, mission_id=mission_id, run_id=run_id, backlog_id=backlog_id,
        slot_key=None,
        approval_payload={"name": "x", "kind": "indicator",
                          "content_hash": SUBMITTED,
                          "selection_rationale": "意図", "summary": "概要"},
        now=NOW, ledger_entries=tuple(ctx.ledger.entries()), ctx=ctx)
    payload = _stored_payload(conn)
    assert payload["facts_version"] == 1
    facts = payload["parent_facts"]
    assert facts["analysis_call_count"] == 1
    assert facts["trials"] == [] and facts["trials_omitted"] == 0
    assert facts["backlog"] == {"id": backlog_id, "attempts": 1,
                                "status": "selected"}
    assert facts["prior_runs"] == [
        {"run_id": prior, "result": None, "approval_id": None}]
    assert payload["agent_claims"]["selection_rationale"] == "意図"
    assert payload["selection_rationale"] == "意図"


def test_finalize_success_without_buildable_facts_still_creates_approval(
        loop_min, conn, clock, tmp_path):
    mission_id, run_id, backlog_id = _setup(conn, clock)
    broken = {
        "opaque_ref": "bt", "kind": "run_backtest",
        "params": {"name": "x", "pair": "USDJPY"}, "trial_count": 1,
        "result_summary": {
            "scope": "in_sample", "plugin_ref": "plugins/x",
            "content_hash": "content", "kind": "strategy",
            "pair": "USDJPY", "timeframe": "1h", "source": "test",
            "base_interval": "1m", "period": (NOW, NOW),
            "metrics": {"pf": 1.2}, "settings_hash": "settings",
            "core_commit": "core", "initial_balance": 10000.0, "now": NOW,
            "params": {}, "artifact_hash": "h",
            "archive_tmp": str(tmp_path / "gone")}}
    ctx = _ctx(tmp_path, mission_id, run_id, [broken])
    loop_min._finalize_success(
        conn, mission_id=mission_id, run_id=run_id, backlog_id=backlog_id,
        slot_key=None, approval_payload={"name": "x", "kind": "indicator"},
        now=NOW, ledger_entries=tuple(ctx.ledger.entries()), ctx=ctx)
    payload = _stored_payload(conn)
    assert payload["facts_version"] == 1 and payload["parent_facts"] is None
    assert payload["facts_error"] == "collection_failed"
    assert "agent_claims" not in payload
    assert payload["name"] == "x"


def test_finalize_success_stores_accepted_backtest_trials(
        loop_min, conn, clock, tmp_path):
    mission_id, run_id, backlog_id = _setup(conn, clock)
    entry = {
        "opaque_ref": "bt", "kind": "run_backtest",
        "params": {"name": "x", "pair": "USDJPY"}, "trial_count": 1,
        "result_summary": {
            "scope": "in_sample", "plugin_ref": "plugins/x",
            "content_hash": SUBMITTED, "kind": "strategy",
            "pair": "USDJPY", "timeframe": "1h", "source": "test",
            "base_interval": "1m", "period": (NOW, NOW),
            "metrics": {"trades": 100, "pf": 1.2, "avg_r": 0.1,
                        "max_drawdown": 0.05},
            "settings_hash": "settings", "core_commit": "core",
            "initial_balance": 10000.0, "now": NOW, "params": {},
            "artifact_hash": "h", "archive_tmp": str(tmp_path / "gone")}}
    ctx = _ctx(tmp_path, mission_id, run_id, [entry])
    loop_min._finalize_success(
        conn, mission_id=mission_id, run_id=run_id, backlog_id=backlog_id,
        slot_key=None,
        approval_payload={"name": "x", "kind": "strategy",
                          "content_hash": SUBMITTED},
        now=NOW, ledger_entries=tuple(ctx.ledger.entries()), ctx=ctx)
    assert _stored_payload(conn)["parent_facts"]["trials"] == [{
        "n": 1, "content_hash8": "1a2b3c4d", "same_hash_as_submitted": True,
        "trades": 100, "pf": 1.2, "avg_r": 0.1, "max_drawdown": 0.05}]


# ---- 無害化・保存・検証の境界 ----

def test_display_text_keeps_a_value_of_exactly_the_limit():
    limit = approval_facts.FIELD_DISPLAY_LIMIT
    assert approval_facts.display_text("a" * limit) == "a" * limit
    assert approval_facts.display_text("a" * (limit + 1)) == (
        "a" * limit + approval_facts.TRUNCATION_SUFFIX)


def test_display_text_removes_lone_surrogates():
    assert approval_facts.display_text("a\ud800b") == "ab"


def test_display_text_turns_cr_lf_tab_into_spaces():
    assert approval_facts.display_text("a\rb\nc\td") == "a b c d"


def test_display_text_collapses_whitespace_before_cutting():
    text = "x" * 190 + " " * 30 + "y" * 50
    assert approval_facts.display_text(text) == (
        "x" * 190 + " " + "y" * 9 + approval_facts.TRUNCATION_SUFFIX)


@pytest.mark.parametrize("value", ["", " ", " \t\r\n ", "\x1b‮"])
def test_display_text_shows_dash_for_empty_values(value):
    assert approval_facts.display_text(value) == "-"


def test_claim_of_exactly_the_store_limit_is_stored_whole(conn, clock):
    _, run_id, backlog_id = _setup(conn, clock)
    limit = approval_facts.CLAIM_STORE_LIMIT
    payload = _build(conn, run_id, backlog_id, [], summary="s" * limit)
    assert payload["agent_claims"]["summary"] == "s" * limit
    payload = _build(conn, run_id, backlog_id, [], summary="s" * (limit + 1))
    assert payload["agent_claims"]["summary"] == (
        "s" * limit + approval_facts.TRUNCATION_SUFFIX)


@pytest.mark.parametrize("hash8", ["1a2b3c4d9", "1A2B3C4D", " 1a2b3c4d"])
def test_validator_rejects_a_hash_that_is_not_exactly_eight_lowercase_hex(
        conn, clock, hash8):
    facts = _valid_facts(conn, clock)
    facts["trials"][0]["content_hash8"] = hash8
    with pytest.raises(FactsError):
        approval_facts.validate_parent_facts(facts)


@pytest.mark.parametrize("status", ["open", "observation", "done"])
def test_selected_idea_is_quoted_only_while_the_backlog_is_selected(
        conn, clock, status):
    _, run_id, backlog_id = _setup(conn, clock)
    conn.execute("UPDATE improvement_backlog SET status=? WHERE id=?",
                 (status, backlog_id))
    conn.commit()
    payload = _build(conn, run_id, backlog_id, [])
    assert "selected_backlog_idea" not in payload["agent_claims"]


# ---- 保存から表示まで通す ----

def _commands(tmp_path, conn):
    from unittest.mock import MagicMock

    from agentic_fx.activity import ActivityLog
    from agentic_fx.commands import Commands
    from agentic_fx.core.contracts import FixedClock
    from agentic_fx.core.health_latch import HealthLatch
    from agentic_fx.core.paper_broker import PaperBroker
    from agentic_fx.store.state import StateStore
    from tests.loops.conftest import SETTINGS

    (tmp_path / "logs").mkdir(exist_ok=True)
    return Commands(
        conn=conn, state_store=StateStore(tmp_path / "s.json"),
        broker=PaperBroker(conn, SETTINGS, FixedClock(NOW)),
        trade_loop=MagicMock(),
        activity=ActivityLog(tmp_path / "logs" / "activity.log"),
        log_dir=tmp_path / "logs", clock=FixedClock(NOW),
        health_latch=HealthLatch())


def _persistable_bt(tmp_path, content_hash=SUBMITTED, pf=1.2):
    return {
        "opaque_ref": "bt", "kind": "run_backtest",
        "params": {"name": "x", "pair": "USDJPY"}, "trial_count": 1,
        "result_summary": {
            "scope": "in_sample", "plugin_ref": "plugins/x",
            "content_hash": content_hash, "kind": "strategy",
            "pair": "USDJPY", "timeframe": "1h", "source": "test",
            "base_interval": "1m", "period": (NOW, NOW),
            "metrics": {"trades": 100, "pf": pf, "avg_r": 0.1,
                        "max_drawdown": 0.05},
            "settings_hash": "settings", "core_commit": "core",
            "initial_balance": 10000.0, "now": NOW, "params": {},
            "artifact_hash": "h", "archive_tmp": str(tmp_path / "gone")}}


def test_payload_saved_by_finalize_success_is_shown_by_the_approval_command(
        loop_min, conn, clock, tmp_path):
    mission_id, run_id, backlog_id = _setup(conn, clock)
    prior = _finished_run(conn, backlog_id, None, mission_status="completed")
    entries = [_persistable_bt(tmp_path),
               _persistable_bt(tmp_path, "ee" * 32, 0.8), _ac()]
    ctx = _ctx(tmp_path, mission_id, run_id, entries)
    loop_min._finalize_success(
        conn, mission_id=mission_id, run_id=run_id, backlog_id=backlog_id,
        slot_key=None,
        approval_payload={"name": "x", "kind": "strategy",
                          "content_hash": SUBMITTED, "eval_timeframe": "1h",
                          "selection_rationale": "意図です",
                          "summary": "概要です"},
        now=NOW, ledger_entries=tuple(ctx.ledger.entries()), ctx=ctx)
    approval_id = conn.execute(
        "SELECT id FROM approval_requests ORDER BY id DESC LIMIT 1"
    ).fetchone()["id"]
    out = _commands(tmp_path, conn).dispatch(f"approval {approval_id}")
    lines = out.split("\n")
    assert approval_facts.PARENT_HEADING in lines
    assert approval_facts.INVALID_FORMAT_TEXT not in out
    assert approval_facts.LEGACY_LINE not in out
    assert f"backlog #{backlog_id} (今回を含め attempts=1) status=selected" \
        in lines
    assert f"  run #{prior}: 観測のみ (承認依頼なし)" in lines
    assert ("mission 内の backtest 2 件 (in_sample の受理分) / 分析 1 件:"
            in lines)
    assert ("  1. hash=1a2b3c4d (提出と同一) trades=100 pf=1.2 avg_r=0.1 "
            "mdd=0.05") in lines
    assert ("  2. hash=eeeeeeee (提出と別) trades=100 pf=0.8 avg_r=0.1 "
            "mdd=0.05") in lines
    assert lines.index(approval_facts.PARENT_NOTE) < lines.index(
        approval_facts.CLAIMS_HEADING)
    assert "  自己申告: selection_rationale: 意図です" in lines
    assert "  自己申告: summary: 概要です" in lines
    assert "  引用: 選んだ課題の文面 (agent 起票): 課題の文面" in lines


_FINALIZE_PAYLOAD = {"name": "x", "kind": "strategy",
                     "content_hash": SUBMITTED}


def _finalize_with_ledger(loop_min, conn, clock, tmp_path, entries):
    mission_id, run_id, backlog_id = _setup(conn, clock)
    ctx = _ctx(tmp_path, mission_id, run_id, entries)
    loop_min._finalize_success(
        conn, mission_id=mission_id, run_id=run_id, backlog_id=backlog_id,
        slot_key=None,
        approval_payload={**_FINALIZE_PAYLOAD, "selection_rationale": "意図"},
        now=NOW, ledger_entries=tuple(ctx.ledger.entries()), ctx=ctx)
    return mission_id


def _bt_with(tmp_path, **summary):
    entry = _persistable_bt(tmp_path)
    entry["result_summary"].update(summary)
    return entry


@pytest.mark.parametrize("make_entry", [
    lambda tp: _bt_with(tp, metrics={"pf": 1.2}),
    lambda tp: _bt_with(tp, metrics=None),
    lambda tp: _bt_with(tp, metrics={"trades": "many"}),
    lambda tp: _bt_with(tp, metrics={"trades": 3, "pf": float("nan")}),
    lambda tp: _bt_with(tp, content_hash=12345),
], ids=["no-trades", "metrics-none", "trades-str", "pf-nan", "hash-not-str"])
def test_a_ledger_entry_shape_failure_creates_an_approval_marked_as_collection_failed(
        loop_min, conn, clock, tmp_path, caplog, make_entry):
    with caplog.at_level(logging.WARNING):
        mission_id = _finalize_with_ledger(
            loop_min, conn, clock, tmp_path, [make_entry(tmp_path)])
    payload = _stored_payload(conn)
    assert payload["facts_version"] == 1
    assert payload["parent_facts"] is None
    assert payload["facts_error"] == "collection_failed"
    assert "agent_claims" not in payload
    assert payload["selection_rationale"] == "意図"
    assert conn.execute("SELECT status FROM missions WHERE id=?",
                        (mission_id,)).fetchone()["status"] == "completed"
    assert any("approval facts not built" in r.getMessage()
               for r in caplog.records if r.levelno == logging.WARNING)


def _finalize_with_failing_collection(loop_min, conn, clock, tmp_path,
                                      monkeypatch, exc):
    mission_id, run_id, backlog_id = _setup(conn, clock)
    ctx = _ctx(tmp_path, mission_id, run_id, [_persistable_bt(tmp_path)])

    def boom(conn_, **kwargs):
        conn_.execute("INSERT INTO improvement_backlog(idea, source, created_at, "
                      "updated_at) VALUES ('leaked', 'user', 'x', 'x')")
        raise exc
    monkeypatch.setattr(approval_facts, "build_facts_payload", boom)
    loop_min._finalize_success(
        conn, mission_id=mission_id, run_id=run_id, backlog_id=backlog_id,
        slot_key=None, approval_payload=dict(_FINALIZE_PAYLOAD), now=NOW,
        ledger_entries=tuple(ctx.ledger.entries()), ctx=ctx)
    return mission_id


@pytest.mark.parametrize("exc", [
    sqlite3.OperationalError("disk I/O error"),
    sqlite3.DatabaseError("database disk image is malformed"),
    sqlite3.InterfaceError("Cannot operate on a closed database."),
    sqlite3.ProgrammingError("Cannot operate on a closed database."),
    RuntimeError("storage failure"),
    TypeError("unexpected"),
    KeyError("unexpected"),
    ValueError("unexpected"),
])
def test_any_collection_failure_other_than_a_facts_error_creates_no_approval_and_fails_the_mission(
        loop_min, conn, clock, tmp_path, monkeypatch, exc):
    mission_id = _finalize_with_failing_collection(
        loop_min, conn, clock, tmp_path, monkeypatch, exc)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM approval_requests").fetchone()["n"] == 0
    assert conn.execute("SELECT status FROM missions WHERE id=?",
                        (mission_id,)).fetchone()["status"] != "completed"
    assert conn.execute("SELECT COUNT(*) AS n FROM improvement_backlog "
                        "WHERE idea='leaked'").fetchone()["n"] == 0


def test_the_facts_of_the_current_run_exclude_that_run_and_count_its_backtests(
        loop_min, conn, clock, tmp_path):
    mission_id, run_id, backlog_id = _setup(conn, clock)
    older = _finished_run(conn, backlog_id, None)
    newer = _finished_run(conn, backlog_id, "report")
    # 現在の run の行も完了印が付いている状態にして、除外が run_id の一致
    # だけで成り立つようにする。
    improve_runs_store.finish(conn, run_id, result=None, now=NOW,
                              commit=False)
    conn.commit()
    entries = [_persistable_bt(tmp_path), _persistable_bt(tmp_path, "ee" * 32)]
    ctx = _ctx(tmp_path, mission_id, run_id, entries)
    loop_min._finalize_success(
        conn, mission_id=mission_id, run_id=run_id, backlog_id=backlog_id,
        slot_key=None, approval_payload=dict(_FINALIZE_PAYLOAD), now=NOW,
        ledger_entries=tuple(ctx.ledger.entries()), ctx=ctx)
    facts = _stored_payload(conn)["parent_facts"]
    ids = [r["run_id"] for r in facts["prior_runs"]]
    assert run_id not in ids
    assert ids == [newer, older]
    assert len(facts["trials"]) == 2 and facts["trials_omitted"] == 0
