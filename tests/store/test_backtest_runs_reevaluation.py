"""How the readers of ``backtest_runs`` treat the rows a re-evaluation writes.

A re-evaluation is not an improvement attempt, so the improvement loop's view leaves its rows
out. The numbers a trade decision sees follow the approval that is currently in force, and
the duplicate check keeps seeing every approved-quality row.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import backtest_runs as runs
from agentic_fx.store.db import connect, init_db

T = datetime(2026, 11, 8, 3, 0, tzinfo=timezone.utc)
NAME = "sma"
CONTENT = "c" * 64
SOURCE = "dukascopy"
BASE = "5m"


@pytest.fixture
def conn(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def _run(conn, *, scope="in_sample", outcome=None, metrics=None, pair="USDJPY",
         content=CONTENT, variant="candidate") -> int:
    return runs.save_harness_run(
        conn, scope=scope, plugin_ref=f"plugins/{NAME}", content_hash=content,
        kind="strategy", pair=pair, timeframe="1h", source=SOURCE, base_interval=BASE,
        period=(T, T), metrics=metrics or {"trades": 40, "pf": 1.3, "avg_r": 0.2},
        settings_hash="s", core_commit="c", initial_balance=1e6, now=T,
        mission_outcome=outcome, variant=variant)


def _approval(conn, payload: dict, *, status="approved", minute: int = 0) -> int:
    when = T.replace(minute=minute)
    approval_id = approvals_store.create(
        conn, "plugin", {"name": NAME, "content_hash": CONTENT, **payload}, when)
    if status != "pending":
        approvals_store.apply_decision(conn, approval_id, status, decided_by="human", now=when)
    return approval_id


def _reevaluation(conn, run_id: int, *, status="approved", minute: int = 5,
                  pair: str = "USDJPY") -> int:
    return _approval(conn, {"candidate_origin": "reevaluation",
                            "run_ids": {"in_sample": {pair: run_id}, "holdout": {}}},
                     status=status, minute=minute)


def _latest(conn, **kwargs):
    return runs.latest_in_sample_metrics(
        conn, CONTENT, pair="USDJPY", variant="candidate", source=SOURCE,
        base_interval=BASE, **kwargs)


# ------------------------------------------------------------------ in_sample_view

def test_the_improvement_view_leaves_re_evaluation_rows_out_and_keeps_null_rows(conn):
    null_row = _run(conn, outcome=None)
    approval_row = _run(conn, outcome="approval")
    _run(conn, outcome="reevaluation")
    other = _run(conn, outcome="gate_failed")

    assert [row["id"] for row in runs.in_sample_view(conn)] == [null_row, approval_row, other]
    assert [row["id"] for row in runs.in_sample_view(conn, pair="USDJPY")] == [
        null_row, approval_row, other]


# ------------------------------------------------------------------ latest_in_sample_metrics

def test_without_a_name_the_old_numbers_are_kept(conn):
    _run(conn, outcome="approval", metrics={"trades": 40, "pf": 1.3})
    new = _run(conn, outcome="reevaluation", metrics={"trades": 41, "pf": 0.9})
    _reevaluation(conn, new)

    assert _latest(conn) == {"trades": 40, "pf": 1.3}


def test_an_approved_re_evaluation_replaces_the_numbers_for_that_name(conn):
    _run(conn, outcome="approval", metrics={"trades": 40, "pf": 1.3})
    new = _run(conn, outcome="reevaluation", metrics={"trades": 41, "pf": 0.9})
    _reevaluation(conn, new)

    assert _latest(conn, name=NAME) == {"trades": 41, "pf": 0.9}


def test_another_name_does_not_get_the_numbers(conn):
    _run(conn, outcome="approval", metrics={"trades": 40, "pf": 1.3})
    new = _run(conn, outcome="reevaluation", metrics={"trades": 41, "pf": 0.9})
    _reevaluation(conn, new)

    assert _latest(conn, name="other") == {"trades": 40, "pf": 1.3}


@pytest.mark.parametrize("status", ["pending", "rejected", "invalidated"])
def test_a_re_evaluation_that_is_not_in_force_does_not_replace_the_numbers(conn, status):
    _run(conn, outcome="approval", metrics={"trades": 40, "pf": 1.3})
    new = _run(conn, outcome="reevaluation", metrics={"trades": 41, "pf": 0.9})
    _reevaluation(conn, new, status=status)

    assert _latest(conn, name=NAME) == {"trades": 40, "pf": 1.3}


def test_a_newer_ordinary_approval_takes_over_from_the_re_evaluation(conn):
    _run(conn, outcome="approval", metrics={"trades": 40, "pf": 1.3})
    new = _run(conn, outcome="reevaluation", metrics={"trades": 41, "pf": 0.9})
    _reevaluation(conn, new, minute=5)
    _approval(conn, {"candidate_origin": "human"}, minute=9)

    assert _latest(conn, name=NAME) == {"trades": 40, "pf": 1.3}


def test_run_ids_in_an_ordinary_approval_are_not_followed(conn):
    _run(conn, outcome="approval", metrics={"trades": 40, "pf": 1.3})
    new = _run(conn, outcome="reevaluation", metrics={"trades": 41, "pf": 0.9})
    _approval(conn, {"candidate_origin": "human",
                     "run_ids": {"in_sample": {"USDJPY": new}, "holdout": {}}})

    assert _latest(conn, name=NAME) == {"trades": 40, "pf": 1.3}


def test_a_pair_the_re_evaluation_did_not_run_gets_no_numbers(conn):
    _run(conn, outcome="approval", metrics={"trades": 40, "pf": 1.3})
    new = _run(conn, outcome="reevaluation", pair="EURUSD", metrics={"trades": 7})
    _reevaluation(conn, new, pair="EURUSD")

    assert _latest(conn, name=NAME) is None


def test_a_run_id_that_names_another_identity_is_not_followed_and_gives_no_numbers(conn):
    _run(conn, outcome="approval", metrics={"trades": 40, "pf": 1.3})
    foreign = _run(conn, outcome="reevaluation", content="d" * 64, metrics={"trades": 99})
    _reevaluation(conn, foreign)

    assert _latest(conn, name=NAME) is None


# ------------------------------------------------------------------ find_matching_approved_metrics

def _match(conn, *, trades=41, pf=0.9, avg_r=0.2, **kwargs):
    return runs.find_matching_approved_metrics(
        conn, pair="USDJPY", variant="candidate", source=SOURCE, base_interval=BASE,
        trades=trades, pf=pf, avg_r=avg_r, **kwargs)


def test_the_duplicate_check_sees_re_evaluation_rows(conn):
    new = _run(conn, outcome="reevaluation", metrics={"trades": 41, "pf": 0.9, "avg_r": 0.2})
    _reevaluation(conn, new, status="pending")

    assert _match(conn) == CONTENT
    assert _match(conn, exclude_name="other") == CONTENT
    assert _match(conn, exclude_name=NAME) is None


def test_the_duplicate_check_still_ignores_rows_of_other_outcomes(conn):
    _run(conn, outcome=None, metrics={"trades": 41, "pf": 0.9, "avg_r": 0.2})
    _run(conn, outcome="gate_failed", metrics={"trades": 41, "pf": 0.9, "avg_r": 0.2})
    _approval(conn, {})

    assert _match(conn) is None
