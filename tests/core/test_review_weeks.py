"""週次の確定の暦: 週の最後の取引日の close・週の印・猶予・列挙。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from agentic_fx.core import review_weeks as rw

UTC = timezone.utc


def utc(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=UTC)


def test_a_winter_week_closes_on_friday_at_22_utc_and_is_labelled_by_that_friday():
    close = rw.close_of_week(date(2027, 1, 27))
    assert close == utc(2027, 1, 29, 22)
    assert rw.week_label(close) == date(2027, 1, 29)


def test_a_summer_week_closes_at_21_utc():
    assert rw.close_of_week(date(2027, 7, 1)) == utc(2027, 7, 2, 21)


def test_a_weekday_holiday_does_not_split_the_week_but_moves_its_last_day():
    # 2026-12-25 (金) は休場。最後の取引日は木曜 12-24。
    close = rw.close_of_week(date(2026, 12, 22))
    assert close == utc(2026, 12, 24, 22)
    assert rw.week_label(close) == date(2026, 12, 24)


def test_every_day_of_the_week_maps_to_the_same_close():
    expected = rw.close_of_week(date(2027, 1, 25))
    assert all(rw.close_of_week(date(2027, 1, 25) + timedelta(days=i)) == expected
               for i in range(5))


def test_a_label_that_is_not_the_last_trading_day_is_rejected():
    assert rw.week_close_for_label(date(2027, 1, 29)) == utc(2027, 1, 29, 22)
    assert rw.week_close_for_label(date(2027, 1, 28)) is None
    assert rw.week_close_for_label(date(2027, 1, 30)) is None     # 土曜


def test_finalization_starts_one_grace_after_the_close():
    close = utc(2027, 1, 29, 22)
    assert rw.finalize_at(close) == close + timedelta(days=1)
    assert rw.latest_due_week(close + timedelta(days=1) - timedelta(seconds=1)) == date(2027, 1, 22)
    assert rw.latest_due_week(close + timedelta(days=1)) == date(2027, 1, 29)


def test_due_weeks_are_listed_oldest_first_from_the_first_week():
    now = utc(2027, 2, 13, 23)
    labels = [label for label, _ in rw.due_weeks(now, first=date(2027, 1, 22))]
    assert labels == [date(2027, 1, 22), date(2027, 1, 29), date(2027, 2, 5), date(2027, 2, 12)]


def test_a_week_still_inside_its_grace_is_not_due():
    now = utc(2027, 2, 13, 5)
    labels = [label for label, _ in rw.due_weeks(now, first=date(2027, 1, 22))]
    assert labels == [date(2027, 1, 22), date(2027, 1, 29), date(2027, 2, 5)]


def test_a_new_year_holiday_on_friday_moves_the_label_to_thursday():
    # 2027-01-01 (金) は休場。前の週の最後の取引日は木曜 2026-12-31。
    close = rw.close_of_week(date(2026, 12, 28))
    assert close == utc(2026, 12, 31, 22)
    assert rw.week_label(close) == date(2026, 12, 31)


def test_the_dst_switch_week_closes_at_the_new_offset():
    # 2027-03-14 に米国が夏時間へ。その週の金曜は 21:00 UTC。
    assert rw.close_of_week(date(2027, 3, 15)) == utc(2027, 3, 19, 21)
    assert rw.close_of_week(date(2027, 3, 8)) == utc(2027, 3, 12, 22)


@pytest.mark.parametrize("text,ok", [("2027-01-29", True), ("2027-02-30", False),
                                     ("2027-1-29", False), ("x", False)])
def test_a_week_text_is_parsed_only_as_a_real_date(text, ok):
    assert (rw.parse_label(text) is not None) is ok
