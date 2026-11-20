"""戦略別の前方成績 (機械採点) の集計: R・穴・採否・到達率・集中度・窓・世代。"""
from __future__ import annotations

from datetime import timedelta, timezone

import pytest

from agentic_fx.store import signal_forward as sf
from agentic_fx.store import signal_shadow as ss
from tests.store.forward_world import (
    NOW, WEEK, Maker, open_db, target)


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "f.db")
    yield c
    c.close()


@pytest.fixture
def make(conn):
    return Maker(conn)


def summary(conn, **kw):
    started = kw.pop("started", None)
    return sf.forward_summary(conn, target(started, **kw), now=NOW)


# ---------------------------------------------------------------- R

def test_r_is_pips_divided_by_the_risk_width_in_pips():
    assert sf.r_multiple(50.0, entry=150.0, stop_loss=149.0, pip_size=0.01) == pytest.approx(0.5)
    assert sf.r_multiple(-100.0, entry=150.0, stop_loss=149.0, pip_size=0.01) == pytest.approx(-1.0)


@pytest.mark.parametrize("entry, stop_loss, pip", [
    (150.0, 150.0, 0.01), (150.0, None, 0.01), (150.0, float("nan"), 0.01),
    (150.0, "149", 0.01), (150.0, True, 0.01), (150.0, 149.0, 0.0), (None, 149.0, 0.01)])
def test_r_is_unknown_when_the_risk_width_cannot_be_read(entry, stop_loss, pip):
    assert sf.r_multiple(50.0, entry=entry, stop_loss=stop_loss, pip_size=pip) is None


# ---------------------------------------------------------------- 表示の値

def test_the_values_count_resolved_rows_of_accepts_and_rejects(conn, make):
    for i, (acc, pnl) in enumerate([(True, 100.0), (True, -100.0), (False, 100.0),
                                    (False, 50.0), (True, 0.0)]):
        make.decide(at=NOW - timedelta(days=i + 1), accept=acc, pnl=pnl,
                    result="tp" if pnl > 0 else "sl")
    out = summary(conn)
    assert out["state"] == "ok"
    allv, acc, rej = (out["series"][k] for k in ("all", "accept", "reject"))
    assert (allv["n"], allv["wins"], allv["losses"], allv["flats"]) == (5, 3, 1, 1)
    assert allv["avg_r"] == pytest.approx((1.0 - 1.0 + 1.0 + 0.5 + 0.0) / 5)
    assert allv["pf"] == pytest.approx(2.5 / 1.0) and allv["pf_state"] == "computed"
    assert (acc["n"], rej["n"]) == (3, 2)
    assert rej["pf_state"] == "no_losses" and rej["pf"] is None
    assert out["decisions"] == {"total": 5, "accept": 3, "reject": 2, "gate_passed": 3}


def test_the_level_follows_the_minimum_and_reference_counts(conn, make):
    for i in range(4):
        make.decide(at=NOW - timedelta(hours=i + 1))
    assert summary(conn)["series"]["all"]["level"] == "none"
    make.decide(at=NOW - timedelta(hours=9))
    assert summary(conn)["series"]["all"]["level"] == "reference"
    for i in range(25):
        make.decide(at=NOW - timedelta(hours=20 + i))
    assert summary(conn)["series"]["all"]["level"] == "normal"


def test_an_unknown_risk_width_is_left_out_of_the_values_and_counted(conn, make):
    make.decide(at=NOW - timedelta(days=1))
    make.decide(at=NOW - timedelta(days=2), stop_loss=150.0)
    make.decide(at=NOW - timedelta(days=3), payload="{}")
    out = summary(conn)
    assert out["series"]["all"]["n"] == 1
    assert out["series"]["all"]["r_unknown"] == 2
    assert out["holes"]["r_unknown"] == 2


def test_gate_passed_counts_only_accepts_the_gate_let_through(conn, make):
    make.decide(at=NOW - timedelta(days=1), gate="accepted")
    make.decide(at=NOW - timedelta(days=2), gate="rejected")
    make.decide(at=NOW - timedelta(days=3), accept=False)
    assert summary(conn)["decisions"]["gate_passed"] == 1


# ---------------------------------------------------------------- 穴

def test_the_holes_are_counted_by_cause(conn, make):
    make.decide(at=NOW - timedelta(days=1), result="no_fill")
    make.decide(at=NOW - timedelta(days=2), result="data_insufficient")
    make.decide(at=NOW - timedelta(days=3), result="filled")
    make.decide(at=NOW - timedelta(days=5), result="filled")
    for reason in ("freshness_expired", "freshness_expired", "retry_exhausted"):
        make.signal(at=NOW - timedelta(days=2), status="abandoned", reason=reason)
    holes = summary(conn)["holes"]
    assert holes["abandoned"] == {"freshness_expired": 2, "retry_exhausted": 1}
    assert (holes["no_fill"], holes["data_insufficient"], holes["unterminated"]) == (1, 1, 2)
    assert holes["oldest_unterminated_sec"] == pytest.approx(5 * 86400)


def test_a_signal_still_waiting_for_a_decision_is_not_a_hole_but_lowers_the_rate(conn, make):
    make.decide(at=NOW - timedelta(days=1))
    make.signal(at=NOW - timedelta(days=1), status="pending")
    out = summary(conn)
    assert out["holes"]["abandoned"] == {}
    assert out["reach"] == {"signals": 2, "decided": 1, "rate": 0.5}


def test_cleanup_and_legacy_abandoned_rows_are_not_signals_of_the_strategy(conn, make):
    make.decide(at=NOW - timedelta(days=1))
    make.signal(at=NOW - timedelta(days=1), status="abandoned", reason="ineligible_cleanup",
                contract=None)
    make.signal(at=NOW - timedelta(days=1), status="abandoned", reason="legacy_unknown")
    make.signal(at=NOW - timedelta(days=1), status="abandoned", reason="normalization_failed")
    out = summary(conn)
    assert out["holes"]["abandoned"] == {"normalization_failed": 1}
    assert out["reach"]["signals"] == 2


# ---------------------------------------------------------------- 窓と世代

def test_only_the_live_version_of_the_strategy_is_counted(conn, make):
    make.decide(at=NOW - timedelta(days=1))
    make.decide(at=NOW - timedelta(days=1), plugin="other")
    make.decide(at=NOW - timedelta(days=1), content_hash="z" * 16)
    assert summary(conn)["decisions"]["total"] == 1


def test_the_window_stops_twelve_weeks_back_even_in_an_older_generation(conn, make):
    make.decide(at=NOW - 12 * WEEK - timedelta(minutes=1))
    make.decide(at=NOW - 12 * WEEK + timedelta(minutes=1))
    out = summary(conn)
    assert out["decisions"]["total"] == 1
    assert out["window"]["start"] == (NOW - 12 * WEEK).isoformat()


def test_a_decision_before_the_generation_start_is_left_out(conn, make):
    start = NOW - 3 * WEEK
    make.decide(at=start - timedelta(hours=1))
    make.decide(at=start + timedelta(hours=1))
    out = summary(conn, started=start)
    assert out["decisions"]["total"] == 1
    assert out["window"]["start"] == start.isoformat()
    assert out["generation"] == {"started_at": start.isoformat(), "source": "journal", "op_id": 1}


def test_a_signal_of_an_earlier_period_of_the_same_hash_is_not_counted(conn, make):
    """A → B → A で、最初の A の間に出た signal は、後に採否が付いても今の世代に数えない。"""
    start = NOW - 3 * WEEK
    make.decide(at=start + timedelta(days=1), signal_at=start - timedelta(days=1))
    make.decide(at=start + timedelta(days=2))
    out = summary(conn, started=start)
    assert out["decisions"]["total"] == 1
    assert out["reach"]["signals"] == 1


def test_a_signal_created_with_a_non_utc_offset_is_compared_as_an_instant(conn, make):
    start = NOW - 3 * WEEK
    jst = timezone(timedelta(hours=9))
    inside = (start + timedelta(minutes=30)).astimezone(jst).isoformat()
    before = (start - timedelta(minutes=30)).astimezone(jst).isoformat()
    make.signal(at=start, created_text=inside)
    make.signal(at=start, created_text=before)
    assert summary(conn, started=start)["reach"]["signals"] == 1


def test_a_target_without_a_generation_start_is_undetermined_and_has_no_numbers(conn, make):
    make.decide(at=NOW - timedelta(days=1))
    out = summary(conn, reason="no_generation_record")
    assert out["state"] == "undetermined" and out["reason"] == "no_generation_record"
    assert "series" not in out and "decisions" not in out


# ---------------------------------------------------------------- 到達率の比較と集中度

def test_recent_and_previous_four_weeks_are_compared_by_reach_rate(conn, make):
    make.decide(at=NOW - 1 * WEEK)
    make.signal(at=NOW - 2 * WEEK, status="abandoned", reason="freshness_expired")
    make.decide(at=NOW - 5 * WEEK)
    make.signal(at=NOW - 6 * WEEK, status="abandoned", reason="freshness_expired")
    make.signal(at=NOW - 7 * WEEK, status="abandoned", reason="freshness_expired")
    comp = summary(conn)["comparison"]
    assert comp["recent"] == {"signals": 2, "decided": 1, "rate": 0.5}
    assert comp["previous"] == {"signals": 3, "decided": 1, "rate": pytest.approx(1 / 3)}
    assert comp["comparable"] is True


def test_an_empty_period_cannot_be_compared(conn, make):
    make.decide(at=NOW - 1 * WEEK)
    comp = summary(conn)["comparison"]
    assert comp["previous"] == {"signals": 0, "decided": 0, "rate": None}
    assert comp["comparable"] is False


def test_the_previous_period_is_clipped_to_the_generation_start(conn, make):
    start = NOW - 5 * WEEK
    make.decide(at=NOW - 1 * WEEK)
    make.signal(at=NOW - 6 * WEEK, status="abandoned", reason="freshness_expired")
    comp = summary(conn, started=start)["comparison"]
    assert comp["previous"]["signals"] == 0


def test_concentration_is_the_number_of_scored_days_and_the_largest_day_share(conn, make):
    day = NOW - timedelta(days=3)
    for hours in (0, 1, 2):
        make.decide(at=day + timedelta(minutes=hours))
    make.decide(at=NOW - timedelta(days=1))
    make.decide(at=NOW - timedelta(days=2), result="no_fill")
    out = summary(conn)["concentration"]
    assert out["days"] == 2 and out["max_day_count"] == 3
    assert out["max_day_share"] == pytest.approx(0.75)


def test_without_scored_rows_the_concentration_is_empty(conn, make):
    make.decide(at=NOW - timedelta(days=2), result="no_fill")
    assert summary(conn)["concentration"] == {"days": 0, "max_day_count": 0,
                                              "max_day_share": None}


def test_no_decisions_give_zero_counts_and_unknown_values(conn):
    out = summary(conn)
    assert out["state"] == "ok"
    assert out["series"]["all"]["n"] == 0 and out["series"]["all"]["avg_r"] is None
    assert out["series"]["all"]["pf_state"] == "unknown"
    assert out["reach"] == {"signals": 0, "decided": 0, "rate": None}


def test_the_summaries_are_returned_in_the_order_of_the_targets(conn, make):
    make.decide(at=NOW - timedelta(days=1))
    listed = [target(name="strat"), target(reason="no_generation_record", name="zeta")]
    out = [sf.summary_or_failed(conn, item, now=NOW) for item in listed]
    assert [row["name"] for row in out] == ["strat", "zeta"]
    assert not hasattr(sf, "forward_summaries")


def test_a_failing_aggregate_becomes_a_failed_row_with_the_thresholds(conn, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(sf, "forward_summary", broken)
    row = sf.summary_or_failed(conn, target(), now=NOW)
    assert row["state"] == sf.STATE_FAILED and row["reason"] == "summary_failed"
    assert row["name"] == "strat" and row["thresholds"]["min_count"] == sf.MIN_COUNT


def test_the_caller_can_turn_a_failure_into_a_propagation(conn, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    def stop(exc):
        raise ValueError("stop") from exc

    monkeypatch.setattr(sf, "forward_summary", broken)
    with pytest.raises(ValueError):
        sf.summary_or_failed(conn, target(), now=NOW, on_error=stop)


# ---------------------------------------------------------------- 旧 period_summary の検査の移行

def test_counts_by_strategy_and_outcome_survive_the_move_from_period_summary(conn, make):
    """旧集計が数えていた状態別の件数・数値終端の数・pips の和・勝ちの数を、新しい集計も返す。"""
    p_tp = make.decide(at=NOW - timedelta(hours=1), pnl=20.0, result="tp")
    p_sl = make.decide(at=NOW - timedelta(hours=2), pnl=-10.0, result="sl")
    make.decide(at=NOW - timedelta(hours=3), result="filled")
    make.decide(at=NOW - timedelta(hours=4), plugin="q", result="filled")
    del p_tp, p_sl
    p = summary(conn)
    assert p["outcomes"] == {"tp": 1, "sl": 1, "day_close": 0, "data_insufficient": 0,
                             "no_fill": 0, "unterminated": 1}
    assert p["series"]["all"]["scored"] == 2 and p["series"]["all"]["pips_sum"] == 10.0
    assert p["series"]["all"]["wins"] == 1
    q = sf.forward_summary(conn, target(name="q"), now=NOW)
    assert q["outcomes"]["unterminated"] == 1 and q["series"]["all"]["scored"] == 0


def test_period_summary_is_gone_so_no_second_aggregate_can_drift():
    assert not hasattr(ss, "period_summary")


# ---------------------------------------------------------------- 秒未満の境界

SUB = timedelta(milliseconds=800)


def test_a_signal_of_an_earlier_generation_within_the_same_second_is_not_counted(conn, make):
    start = (NOW - 3 * WEEK).replace(microsecond=900_000)
    make.decide(at=start + timedelta(hours=1), signal_at=start - SUB)
    make.decide(at=start + timedelta(hours=2), signal_at=start + timedelta(milliseconds=50))
    out = summary(conn, started=start)
    assert out["decisions"]["total"] == 1
    assert out["reach"]["signals"] == 1


def test_a_decision_within_the_same_second_outside_the_window_is_not_counted(conn, make):
    end = NOW.replace(microsecond=100_000)
    start = end - 12 * WEEK
    make.decide(at=start - timedelta(milliseconds=50), signal_at=start + timedelta(hours=1))
    make.decide(at=end + timedelta(milliseconds=50), signal_at=start + timedelta(hours=1))
    make.decide(at=end - timedelta(milliseconds=50), signal_at=start + timedelta(hours=1))
    assert sf.forward_summary(conn, target(), now=end)["decisions"]["total"] == 1


# ---------------------------------------------------------------- 評価時刻より後に起きたこと

def test_a_decision_made_after_the_evaluation_time_is_not_yet_a_reach(conn, make):
    make.decide(at=NOW + timedelta(hours=1), signal_at=NOW - timedelta(hours=1))
    out = summary(conn)
    assert out["decisions"]["total"] == 0
    assert out["reach"] == {"signals": 1, "decided": 0, "rate": 0.0}


def test_an_abandon_after_the_evaluation_time_is_not_a_hole_yet(conn, make):
    make.signal(at=NOW + timedelta(hours=1), status="abandoned", reason="freshness_expired",
                created_text=(NOW - timedelta(hours=1)).isoformat())
    out = summary(conn)
    assert out["holes"]["abandoned"] == {}
    assert out["reach"]["signals"] == 1


@pytest.mark.parametrize("result", ["tp", "no_fill", "data_insufficient"])
def test_a_result_that_came_after_the_evaluation_time_is_still_unterminated(conn, make, result):
    make.decide(at=NOW - timedelta(minutes=30), result=result)
    out = summary(conn)
    assert out["series"]["all"]["n"] == 0 and out["series"]["all"]["scored"] == 0
    assert out[ "outcomes"][result] == 0
    assert out["holes"]["unterminated"] == 1
    assert out["holes"]["as_of_unknown"] == 0


def test_a_result_before_the_evaluation_time_is_kept(conn, make):
    make.decide(at=NOW - timedelta(hours=3), result="tp")
    assert summary(conn)["series"]["all"]["n"] == 1


def test_the_same_data_gives_an_earlier_picture_for_an_earlier_evaluation_time(conn, make):
    make.decide(at=NOW - timedelta(hours=3), result="tp")
    later = NOW - timedelta(hours=2)
    early = sf.forward_summary(conn, target(), now=later - timedelta(hours=5))
    assert early["decisions"]["total"] == 0
    assert early["reach"]["decided"] == 0
    assert sf.forward_summary(conn, target(), now=NOW)["series"]["all"]["n"] == 1


def test_a_result_whose_time_cannot_be_read_is_unknown_not_a_win(conn, make):
    shadow = make.decide(at=NOW - timedelta(hours=3), result="tp")
    conn.execute("UPDATE signal_shadow SET resolved_at='garbage', updated_at='garbage' "
                 "WHERE id=?", (shadow,))
    conn.commit()
    out = summary(conn)
    assert out["series"]["all"]["n"] == 0
    assert out["holes"]["as_of_unknown"] == 1 and out["holes"]["unterminated"] == 0


# ---------------------------------------------------------------- 時刻を読めない行

def test_a_decision_whose_time_cannot_be_read_is_counted_as_a_hole(conn, make):
    make.decide(at=NOW - timedelta(days=1))
    make.decide(at=NOW - timedelta(days=2))
    conn.execute("UPDATE signal_decisions SET decided_at='garbage' "
                 "WHERE id=(SELECT MAX(id) FROM signal_decisions)")
    conn.commit()
    out = summary(conn)
    assert out["holes"]["time_unreadable"] == 1
    assert out["decisions"]["total"] == 1


def test_a_decision_time_without_a_zone_is_unreadable_not_silently_dropped(conn, make):
    make.decide(at=NOW - timedelta(days=1))
    conn.execute("UPDATE signal_decisions SET decided_at='2027-01-30 10:00:00'")
    conn.commit()
    out = summary(conn)
    assert out["holes"]["time_unreadable"] == 1 and out["decisions"]["total"] == 0


def test_a_signal_whose_creation_time_cannot_be_read_is_counted_once(conn, make):
    make.decide(at=NOW - timedelta(days=1))
    make.signal(at=NOW - timedelta(days=2))
    conn.execute("UPDATE signals SET created_at='garbage'")
    conn.commit()
    out = summary(conn)
    assert out["holes"]["time_unreadable"] == 2          # 採否のある 1 件 + 採否のない 1 件
    assert out["decisions"]["total"] == 0 and out["reach"]["signals"] == 0


def test_readable_data_has_no_unreadable_time_hole(conn, make):
    make.decide(at=NOW - timedelta(days=1))
    assert summary(conn)["holes"]["time_unreadable"] == 0
