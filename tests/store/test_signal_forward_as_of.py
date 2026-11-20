"""週次の確定が使う、前方成績の追加の面: 評価時刻と窓終端の分離・最初の採否・期限超え・直近の採否。"""
from __future__ import annotations

from datetime import timedelta

import pytest

from agentic_fx.store import signal_forward as sf
from tests.store.forward_world import NOW, WEEK, Maker, open_db, target


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "f.db")
    yield c
    c.close()


@pytest.fixture
def make(conn):
    return Maker(conn)


def summary(conn, *, now=NOW, evaluated_at=None, **kw):
    return sf.forward_summary(conn, target(kw.pop("started", None), **kw), now=now,
                              evaluated_at=evaluated_at)


def test_the_first_decision_time_of_the_window_is_reported(conn, make):
    make.decide(at=NOW - 3 * WEEK)
    make.decide(at=NOW - 2 * WEEK)
    assert summary(conn)["first_decided_at"] == (NOW - 3 * WEEK).isoformat()


def test_without_decisions_there_is_no_first_decision(conn):
    assert summary(conn)["first_decided_at"] is None


def test_the_r_profit_and_loss_sums_are_kept_for_the_all_series(conn, make):
    make.decide(at=NOW - 3 * WEEK, pnl=100.0)    # +1R
    make.decide(at=NOW - 2 * WEEK, pnl=50.0)     # +0.5R
    make.decide(at=NOW - WEEK, pnl=-100.0)       # -1R
    series = summary(conn)["series"]["all"]
    assert series["r_profit"] == pytest.approx(1.5)
    assert series["r_loss"] == pytest.approx(1.0)


def test_a_result_between_the_window_end_and_the_evaluation_time_is_scored(conn, make):
    end = NOW - 2 * WEEK
    sid = make.decide(at=end - timedelta(hours=30), result="filled", pnl=0.0)
    make.finish(sid, result="tp", pnl=50.0, entry=150.0, at=end + timedelta(hours=2))
    conn.commit()
    early = summary(conn, now=end)
    late = summary(conn, now=end, evaluated_at=end + timedelta(days=1))
    assert early["series"]["all"]["n"] == 0 and early["outcomes"]["unterminated"] == 1
    assert late["series"]["all"]["n"] == 1 and late["outcomes"]["unterminated"] == 0


def test_a_decision_after_the_window_end_is_not_counted_even_if_evaluated_later(conn, make):
    end = NOW - 2 * WEEK
    make.decide(at=end + timedelta(hours=3))
    late = summary(conn, now=end, evaluated_at=end + timedelta(days=3))
    assert late["decisions"]["total"] == 0


def test_the_evaluation_time_defaults_to_the_window_end(conn, make):
    make.decide(at=NOW - WEEK)
    assert summary(conn) == summary(conn, evaluated_at=NOW)


def test_decisions_in_the_last_two_weeks_are_counted(conn, make):
    make.decide(at=NOW - 3 * WEEK)
    make.decide(at=NOW - timedelta(days=10))
    assert summary(conn)["recent_decisions"] == 1
    make.decide(at=NOW - timedelta(days=1))
    assert summary(conn)["recent_decisions"] == 2


def test_an_unterminated_market_row_is_overdue_after_its_deadline_plus_grace(conn, make):
    at = NOW - 10 * WEEK
    make.decide(at=at, result="filled")
    out = summary(conn)
    assert out["holes"]["unterminated"] == 1 and out["holes"]["overdue_unterminated"] == 1


def test_a_recent_unterminated_row_is_not_overdue(conn, make):
    make.decide(at=NOW - timedelta(hours=2), result="filled")
    out = summary(conn)
    assert out["holes"]["unterminated"] == 1 and out["holes"]["overdue_unterminated"] == 0


def test_overdue_follows_the_evaluation_time_not_the_window_end(conn, make):
    end = NOW - 3 * WEEK
    make.decide(at=end - timedelta(hours=2), result="filled")
    fresh = summary(conn, now=end)
    stale = summary(conn, now=end, evaluated_at=end + timedelta(days=5))
    assert fresh["holes"]["overdue_unterminated"] == 0
    assert stale["holes"]["overdue_unterminated"] == 1


def test_a_filled_limit_row_uses_the_deadline_of_its_fill(conn, make):
    at = NOW - 10 * WEEK
    payload = ('{"action":"open","direction":"long","entry_type":"limit","limit_price":149.5,'
               '"stop_loss":149.0,"take_profit":152.0}')
    make.decide(at=at, result="filled", payload=payload)
    assert summary(conn)["holes"]["overdue_unterminated"] == 1


def test_the_short_term_view_counts_only_the_last_four_weeks(conn, make):
    make.decide(at=NOW - 6 * WEEK, pnl=100.0)
    make.decide(at=NOW - 2 * WEEK, pnl=50.0)
    make.decide(at=NOW - WEEK, pnl=-100.0)
    short = summary(conn)["short_term"]
    assert short["weeks"] == 4 and short["n"] == 2 and short["wins"] == 1 and short["losses"] == 1
    assert short["avg_r"] == pytest.approx(-0.25)


def test_an_empty_short_term_has_no_average(conn):
    short = summary(conn)["short_term"]
    assert short["n"] == 0 and short["avg_r"] is None
