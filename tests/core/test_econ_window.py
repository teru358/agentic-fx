"""指標回避帯の純粋判定: inclusive 境界・通貨・All・連結成分・次 event・agent 表現。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.config import EconWindowSettings
from agentic_fx.core import econ_window as ew
from agentic_fx.core.contracts import InstrumentSpec
from agentic_fx.core.econ_window import (
    EconEvent, WindowRule, WindowSettings)

UTC = timezone.utc
TS = datetime(2026, 10, 30, 12, 30, tzinfo=UTC)   # 金曜。米指標の典型時刻
US = timedelta(microseconds=1)
SETTINGS = WindowSettings(importance_min=3, before_min=30, after_min=15)
USDJPY = WindowRule.for_currencies(SETTINGS, ("USD", "JPY"))
EURUSD = WindowRule.for_currencies(SETTINGS, ("EUR", "USD"))


def ev(id=1, ts=TS, country="USD", name="CPI", importance=3, superseded_by=None):
    return EconEvent(id=id, ts=ts, country=country, name=name,
                     importance=importance, superseded_by=superseded_by)


# --- WindowSettings / 通貨の導出 ----------------------------------------------

def test_settings_defaults_and_ranges():
    s = WindowSettings()
    assert (s.importance_min, s.before_min, s.after_min) == (3, 30, 15)
    assert s.before == timedelta(minutes=30) and s.after == timedelta(minutes=15)
    WindowSettings(importance_min=1, before_min=0, after_min=0)
    for bad in ({"importance_min": 0}, {"importance_min": 4},
                {"importance_min": True}, {"importance_min": 2.0},
                {"before_min": -1}, {"after_min": -1}, {"before_min": 1.5},
                {"after_min": False}):
        with pytest.raises(ValueError):
            WindowSettings(**bad)


def test_from_config_reads_the_three_keys():
    cfg = EconWindowSettings(importance_min=2, before_min=45, after_min=5)
    assert WindowSettings.from_config(cfg) == WindowSettings(2, 45, 5)
    assert WindowSettings.from_config(EconWindowSettings()) == WindowSettings()


def test_currencies_come_from_the_instrument_definition_not_the_symbol():
    spec = InstrumentSpec("XAUUSD", 0.01, 0.01, 10.0, 0.01, 100,
                          base_currency="XAU", quote_currency="USD")
    assert ew.pair_currencies(spec) == ("XAU", "USD")
    odd = InstrumentSpec("ABCDEF", 0.01, 0.01, 10.0, 0.01, 100,
                         base_currency="USD", quote_currency="JPY")
    assert ew.pair_currencies(odd) == ("USD", "JPY")      # symbol[:3] / [3:] ではない


def test_currencies_by_pair_keeps_the_settings_order_and_lets_unknown_raise():
    specs = {
        "EURUSD": InstrumentSpec("EURUSD", 1e-4, .01, 10, .01, 1e5, "EUR", "USD"),
        "USDJPY": InstrumentSpec("USDJPY", .01, .01, 10, .01, 1e5, "USD", "JPY")}
    out = ew.currencies_by_pair(["USDJPY", "EURUSD"], specs.__getitem__)
    assert list(out) == ["USDJPY", "EURUSD"]
    assert out == {"USDJPY": ("USD", "JPY"), "EURUSD": ("EUR", "USD")}
    with pytest.raises(KeyError):
        ew.currencies_by_pair(["GBPJPY"], specs.__getitem__)


# --- 境界表 -----------------------------------------------------------------

@pytest.mark.parametrize("now, expected", [
    (TS - timedelta(minutes=30), True),               # 前端ちょうど
    (TS - timedelta(minutes=30) - US, False),         # 前端の 1µs 外
    (TS - timedelta(minutes=30) + US, True),
    (TS, True),
    (TS + timedelta(minutes=15), True),               # 後端ちょうど
    (TS + timedelta(minutes=15) + US, False),         # 後端の 1µs 外
    (TS + timedelta(minutes=15) - US, True),
    (TS + timedelta(minutes=10), True),               # 発表後の窓
    (TS - timedelta(hours=2), False),
    (TS + timedelta(hours=2), False),
])
def test_in_window_boundaries_are_inclusive(now, expected):
    assert ew.in_window(now, [ev()], USDJPY) is expected


def test_window_of_is_the_closed_interval():
    assert ew.window_of(ev(), SETTINGS) == (TS - timedelta(minutes=30),
                                           TS + timedelta(minutes=15))
    zero = WindowSettings(3, 0, 0)
    assert ew.window_of(ev(), zero) == (TS, TS)
    assert ew.in_window(TS, [ev()], WindowRule.for_currencies(zero, ("USD",)))
    assert not ew.in_window(TS + US, [ev()],
                            WindowRule.for_currencies(zero, ("USD",)))


def test_unrelated_currency_does_not_make_a_window():
    cad = ev(country="CAD")
    assert not ew.in_window(TS, [cad], USDJPY)
    assert not ew.bars_overlap(TS, TS + timedelta(minutes=1), [cad], USDJPY)
    assert not ew.is_target(cad, USDJPY)
    assert ew.is_target(cad, WindowRule.for_currencies(SETTINGS, ("CAD", "JPY")))


def test_base_and_quote_are_both_targets():
    assert ew.in_window(TS, [ev(country="JPY")], USDJPY)       # quote
    assert ew.in_window(TS, [ev(country="USD")], USDJPY)       # base
    assert not ew.in_window(TS, [ev(country="EUR")], USDJPY)
    assert ew.in_window(TS, [ev(country="EUR")], EURUSD)


def test_all_is_always_a_target_even_when_no_currency_matches():
    all_ev = ev(country="All")
    assert ew.in_window(TS, [all_ev], USDJPY)
    assert ew.in_window(TS, [all_ev], WindowRule.for_currencies(SETTINGS, ("GBP",)))
    assert ew.in_window(TS, [all_ev], WindowRule.for_currencies(SETTINGS, ()))
    assert not ew.in_window(TS, [ev(country="all")], USDJPY)   # 綴りの一致だけ


def test_importance_below_the_minimum_is_ignored_and_equal_counts():
    assert not ew.in_window(TS, [ev(importance=2)], USDJPY)
    assert not ew.in_window(TS, [ev(importance=0)], USDJPY)
    low = WindowRule.for_currencies(WindowSettings(2, 30, 15), ("USD",))
    assert ew.in_window(TS, [ev(importance=2)], low)
    assert not ew.in_window(TS, [ev(importance=1)], low)


def test_superseded_events_never_make_a_window():
    assert not ew.in_window(TS, [ev(superseded_by=9)], USDJPY)
    assert ew.in_window(TS, [ev(superseded_by=9), ev(id=2)], USDJPY)


def test_naive_datetimes_are_refused_and_other_offsets_are_normalised():
    with pytest.raises(ValueError):
        ew.in_window(datetime(2026, 10, 30, 12, 30), [ev()], USDJPY)
    with pytest.raises(ValueError):
        EconEvent(1, datetime(2026, 10, 30, 12, 30), "USD", "x", 3)
    jst = timezone(timedelta(hours=9))
    assert ew.in_window(TS.astimezone(jst), [ev(ts=TS.astimezone(jst))], USDJPY)
    assert ev(ts=TS.astimezone(jst)).ts.utcoffset() == timedelta(0)
    with pytest.raises(ValueError):
        ew.overlapping_events(TS, TS - US, [ev()], USDJPY)


def test_active_events_are_sorted_by_time_then_id():
    a = ev(id=5, ts=TS + timedelta(minutes=5), name="b")
    b = ev(id=7, ts=TS, name="a")
    c = ev(id=3, ts=TS, name="c")
    far = ev(id=1, ts=TS + timedelta(hours=3), name="far")
    assert [e.id for e in ew.active_events(TS, [a, b, far, c], USDJPY)] == [3, 7, 5]


# --- 足区間 -----------------------------------------------------------------

def test_bar_overlap_is_a_closed_interval_test_on_both_sides():
    start, end = TS - timedelta(minutes=30), TS + timedelta(minutes=15)
    m = timedelta(minutes=1)
    assert ew.bars_overlap(start - m, start, [ev()], USDJPY)         # 前端を跨ぐ・接する
    assert not ew.bars_overlap(start - m - US, start - US, [ev()], USDJPY)
    assert ew.bars_overlap(end, end + m, [ev()], USDJPY)             # 後端に接する
    assert not ew.bars_overlap(end + US, end + m + US, [ev()], USDJPY)
    assert ew.bars_overlap(TS, TS + m, [ev()], USDJPY)               # 帯の中
    assert ew.bars_overlap(start - m, end + m, [ev()], USDJPY)       # 帯を覆う


def test_a_bar_processed_late_still_overlaps_the_back_edge_without_now():
    # 足 [b, b+1m] を now=b+5m に処理。event ts=b-15m の帯の後端は b。
    b = datetime(2026, 10, 30, 13, 0, tzinfo=UTC)
    event = ev(ts=b - timedelta(minutes=15))
    now = b + timedelta(minutes=5)
    assert not ew.in_window(now, [event], USDJPY)                   # now では帯の外
    assert ew.bars_overlap(b, b + timedelta(minutes=1), [event], USDJPY)
    bar_after = b + timedelta(minutes=1)
    assert not ew.bars_overlap(bar_after, bar_after + timedelta(minutes=1),
                               [event], USDJPY)


def test_bar_overlap_applies_currency_importance_and_supersede_filters_too():
    m = timedelta(minutes=1)
    for bad in (ev(country="CAD"), ev(importance=2), ev(superseded_by=2)):
        assert not ew.bars_overlap(TS, TS + m, [bad], USDJPY)
    assert ew.bars_overlap(TS, TS + m, [ev(country="All")], USDJPY)


# --- 実時刻: 週末境界・日跨ぎ -----------------------------------------------

FRI_CLOSE = datetime(2026, 10, 30, 21, 0, tzinfo=UTC)
SUN_OPEN = datetime(2026, 11, 1, 21, 0, tzinfo=UTC)


def test_weekend_close_a_friday_2100_event_window_ends_at_2115_and_not_on_sunday():
    event = ev(ts=FRI_CLOSE, name="late")
    assert ew.in_window(FRI_CLOSE - timedelta(minutes=30), [event], USDJPY)
    assert ew.in_window(FRI_CLOSE + timedelta(minutes=15), [event], USDJPY)
    assert not ew.in_window(FRI_CLOSE + timedelta(minutes=15) + US, [event], USDJPY)
    assert not ew.bars_overlap(SUN_OPEN, SUN_OPEN + timedelta(minutes=1),
                               [event], USDJPY)
    last_bar = FRI_CLOSE - timedelta(minutes=1)
    assert ew.bars_overlap(last_bar, FRI_CLOSE, [event], USDJPY)


def test_weekend_open_a_sunday_2130_event_window_starts_at_the_open_minute():
    event = ev(ts=SUN_OPEN + timedelta(minutes=30), name="gap")
    assert ew.in_window(SUN_OPEN, [event], USDJPY)                    # 再開の 1 本目
    assert not ew.in_window(SUN_OPEN - US, [event], USDJPY)
    assert ew.bars_overlap(SUN_OPEN - timedelta(minutes=1), SUN_OPEN,
                           [event], USDJPY)                           # 閉場側の端が接する
    assert not ew.bars_overlap(SUN_OPEN - timedelta(minutes=1) - US,
                               SUN_OPEN - US, [event], USDJPY)


def test_window_crosses_midnight_utc():
    event = ev(ts=datetime(2026, 11, 1, 0, 10, tzinfo=UTC), country="JPY")
    assert ew.in_window(datetime(2026, 10, 31, 23, 40, tzinfo=UTC), [event], USDJPY)
    assert not ew.in_window(datetime(2026, 10, 31, 23, 40, tzinfo=UTC) - US,
                            [event], USDJPY)


# --- 連結成分 ---------------------------------------------------------------

def test_overlapping_windows_form_one_component_with_the_smallest_id():
    # A と B、B と C が重なり、A と C は重ならない。
    a = ev(id=8, ts=TS, name="a")
    b = ev(id=3, ts=TS + timedelta(minutes=40), name="b")      # a の後端 +15 と b の前端 +10
    c = ev(id=5, ts=TS + timedelta(minutes=80), name="c")
    assert ew.window_of(a, SETTINGS)[1] >= ew.window_of(b, SETTINGS)[0]
    assert ew.window_of(c, SETTINGS)[0] <= ew.window_of(b, SETTINGS)[1]
    assert ew.window_of(a, SETTINGS)[1] < ew.window_of(c, SETTINGS)[0]
    comps = ew.components([c, a, b], USDJPY)
    assert len(comps) == 1
    comp = comps[0]
    assert comp.id == 3
    assert [e.id for e in comp.events] == [8, 3, 5]
    assert comp.start == TS - timedelta(minutes=30)
    assert comp.end == TS + timedelta(minutes=80) + timedelta(minutes=15)


def test_windows_that_only_touch_at_an_endpoint_are_one_component():
    a = ev(id=1, ts=TS)
    b = ev(id=2, ts=TS + timedelta(minutes=45))        # a.end == b.start
    assert len(ew.components([a, b], USDJPY)) == 1
    far = ev(id=3, ts=TS + timedelta(minutes=45) + US)  # 1µs 離れる
    assert [c.id for c in ew.components([a, far], USDJPY)] == [1, 3]


def test_separate_windows_are_separate_components_in_start_order():
    first = ev(id=9, ts=TS)
    second = ev(id=2, ts=TS + timedelta(hours=5))
    comps = ew.components([second, first], USDJPY)
    assert [c.id for c in comps] == [9, 2]
    assert comps[0].start < comps[1].start


def test_components_ignore_non_targets_and_do_not_depend_on_input_order():
    a = ev(id=4, ts=TS)
    noise = ev(id=1, ts=TS + timedelta(minutes=5), country="CAD")
    gone = ev(id=2, ts=TS + timedelta(minutes=5), superseded_by=4)
    comps = ew.components([noise, gone, a], USDJPY)
    assert [(c.id, len(c.events)) for c in comps] == [(4, 1)]
    assert ew.components([], USDJPY) == ()


def test_active_and_bar_components_select_by_now_and_by_bar():
    a = ev(id=1, ts=TS)
    z = ev(id=2, ts=TS + timedelta(hours=5))
    assert [c.id for c in ew.active_components(TS, [a, z], USDJPY)] == [1]
    assert ew.active_components(TS + timedelta(hours=2), [a, z], USDJPY) == ()
    m = timedelta(minutes=1)
    zs = ew.window_of(z, SETTINGS)[0]
    assert [c.id for c in ew.bar_components(zs - m, zs, [a, z], USDJPY)] == [2]
    assert ew.bar_components(zs - m - US, zs - US, [a, z], USDJPY) == ()


# --- 次 event ---------------------------------------------------------------

def test_next_event_at_seven_days_is_returned_and_one_microsecond_later_is_not():
    now = datetime(2026, 10, 30, 0, 0, tzinfo=UTC)
    edge = ev(ts=now + timedelta(days=7))
    assert ew.next_event(now, [edge], USDJPY) is edge
    late = ev(ts=now + timedelta(days=7) + US)
    assert ew.next_event(now, [late], USDJPY) is None
    assert ew.next_event(now, [late], USDJPY, horizon=timedelta(days=8)) is late


def test_next_event_lower_edge_is_inclusive_and_the_past_is_not_next():
    now = TS
    assert ew.next_event(now, [ev(ts=now)], USDJPY) is not None
    assert ew.next_event(now, [ev(ts=now - US)], USDJPY) is None


def test_next_event_skips_non_targets_and_picks_the_earliest_then_lowest_id():
    now = datetime(2026, 10, 30, 0, 0, tzinfo=UTC)
    soon = now + timedelta(hours=1)
    junk = [ev(id=1, ts=soon, country="CAD"), ev(id=2, ts=soon, importance=2),
            ev(id=3, ts=soon, superseded_by=9)]
    real_late = ev(id=10, ts=soon + timedelta(hours=1))
    assert ew.next_event(now, junk, USDJPY) is None
    assert ew.next_event(now, [*junk, real_late], USDJPY) is real_late
    tie_hi, tie_lo = ev(id=8, ts=soon), ev(id=6, ts=soon)
    assert ew.next_event(now, [real_late, tie_hi, tie_lo], USDJPY) is tie_lo
    assert ew.next_event(now, [ev(country="All", id=4, ts=soon)], USDJPY).id == 4


# --- agent 表現 -------------------------------------------------------------

def _saved():
    return [ev(id=1, country="CAD", name="BoC Rate Statement", importance=3),
            ev(id=2, country="USD", name="Retail Sales", importance=2),
            ev(id=3, country="USD", name="Core CPI", importance=3)]


def test_view_for_usdjpy_shows_only_the_usd_importance_3_event():
    view = ew.build_view("USDJPY", TS, USDJPY, candidates=_saved(),
                         upcoming=_saved(), freshness="fresh")
    assert view.in_window is True
    assert [(e.country, e.name, e.importance) for e in view.active] == [
        ("USD", "Core CPI", 3)]
    assert view.next_event.name == "Core CPI"
    payload = json.dumps(view.to_dict(), ensure_ascii=False)
    assert "BoC Rate Statement" not in payload
    assert "Retail Sales" not in payload
    assert "CAD" not in payload
    assert "Core CPI" in payload
    assert repr(view).count("BoC") == 0 and repr(view).count("Retail") == 0


def test_view_carries_only_the_agreed_fields():
    view = ew.build_view("USDJPY", TS, USDJPY, candidates=_saved(),
                         upcoming=_saved(), freshness="stale")
    d = view.to_dict()
    assert set(d) == {"pair", "currencies", "in_window", "active_events",
                      "next_event", "freshness"}
    assert d["pair"] == "USDJPY" and d["currencies"] == ["JPY", "USD"]
    assert d["freshness"] == "stale"
    assert set(d["active_events"][0]) == {"country", "name", "importance", "ts"}
    assert set(d["next_event"]) == {"country", "name", "importance", "ts"}
    assert d["active_events"][0]["ts"] == "2026-10-30T12:30:00+00:00"


def test_view_outside_the_window_has_no_active_events_but_a_next_event():
    now = TS - timedelta(hours=3)
    view = ew.build_view("USDJPY", now, USDJPY, candidates=[], upcoming=_saved(),
                         freshness="unknown")
    assert view.in_window is False and view.active == ()
    assert view.next_event.name == "Core CPI"
    none = ew.build_view("USDJPY", now, USDJPY, candidates=[], upcoming=[],
                         freshness="unknown")
    assert none.next_event is None and none.to_dict()["next_event"] is None


def test_view_rejects_an_unknown_freshness_word():
    with pytest.raises(ValueError):
        ew.build_view("USDJPY", TS, USDJPY, candidates=[], upcoming=[],
                      freshness="old")


def test_view_rechecks_candidates_that_a_loose_query_returned():
    # query が絞りきれず別通貨・低重要度・期限切れを返しても、view は信じない
    loose = _saved() + [ev(id=9, ts=TS - timedelta(hours=5), name="Old CPI")]
    view = ew.build_view("USDJPY", TS, USDJPY, candidates=loose, upcoming=loose,
                         freshness="fresh")
    assert [e.name for e in view.active] == ["Core CPI"]
    assert "Old CPI" not in json.dumps(view.to_dict())
