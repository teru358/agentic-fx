"""回避帯の候補 query・次 event の 7 日検索・時刻変更の置換 (supersede)。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.core.econ_window import WindowSettings
from agentic_fx.store import econ_events
from agentic_fx.store.db import connect, init_db

UTC = timezone.utc
TS = datetime(2026, 10, 30, 12, 30, tzinfo=UTC)
US = timedelta(microseconds=1)
S = WindowSettings(3, 30, 15)
USDJPY = ("USD", "JPY")


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "e.db")
    init_db(c)
    return c


def add(conn, ts=TS, country="USD", name="CPI", importance=3):
    return econ_events.upsert(conn, ts=ts, country=country, name=name,
                              importance=importance).id


def ids(events):
    return [e.id for e in events]


# --- upsert の戻り値 ----------------------------------------------------------

def test_upsert_reports_the_row_id_and_whether_it_is_new(conn):
    first = econ_events.upsert(conn, ts=TS, country="USD", name="CPI", importance=3)
    assert first.inserted and not first.revived
    assert first.id == conn.execute("SELECT id FROM econ_events").fetchone()[0]
    again = econ_events.upsert(conn, ts=TS, country="USD", name="CPI",
                               importance=3, forecast="3%")
    assert again == (first.id, False, False)
    other = econ_events.upsert(conn, ts=TS, country="USD", name="PPI", importance=3)
    assert other.inserted and other.id != first.id


def test_upsert_of_a_superseded_row_revives_it(conn):
    old = add(conn, ts=TS)
    new = add(conn, ts=TS + timedelta(hours=1))
    conn.execute("UPDATE econ_events SET superseded_by=? WHERE id=?", (new, old))
    conn.commit()
    back = econ_events.upsert(conn, ts=TS, country="USD", name="CPI", importance=3)
    assert back == (old, False, True)
    assert conn.execute("SELECT superseded_by FROM econ_events WHERE id=?",
                        (old,)).fetchone()[0] is None


# --- 候補 query -------------------------------------------------------------

def test_candidate_range_is_now_minus_after_to_now_plus_before_inclusive(conn):
    inside_lo = add(conn, ts=TS - timedelta(minutes=15), name="lo")
    below_lo = add(conn, ts=TS - timedelta(minutes=15) - US, name="below")
    inside_hi = add(conn, ts=TS + timedelta(minutes=30), name="hi")
    above_hi = add(conn, ts=TS + timedelta(minutes=30) + US, name="above")
    got = econ_events.window_candidates(conn, TS, settings=S, currencies=USDJPY)
    assert ids(got) == [inside_lo, inside_hi]
    assert below_lo not in ids(got) and above_hi not in ids(got)


def test_a_released_event_still_inside_the_after_window_is_a_candidate(conn):
    released = add(conn, ts=TS)
    now = TS + timedelta(minutes=10)
    assert ids(econ_events.window_candidates(
        conn, now, settings=S, currencies=USDJPY)) == [released]
    assert econ_events.window_candidates(
        conn, TS + timedelta(minutes=30) + US, settings=S, currencies=USDJPY) == []


def test_candidates_filter_currency_importance_and_superseded(conn):
    keep_usd = add(conn, country="USD", name="a")
    keep_jpy = add(conn, country="JPY", name="b")
    keep_all = add(conn, country="All", name="c")
    add(conn, country="CAD", name="d")
    add(conn, country="USD", name="e", importance=2)
    gone = add(conn, country="USD", name="f")
    conn.execute("UPDATE econ_events SET superseded_by=? WHERE id=?",
                 (keep_usd, gone))
    conn.commit()
    got = econ_events.window_candidates(conn, TS, settings=S, currencies=USDJPY)
    assert ids(got) == [keep_usd, keep_jpy, keep_all]
    assert [e.country for e in got] == ["USD", "JPY", "All"]


def test_importance_min_applies_to_every_row_including_all(conn):
    add(conn, country="All", importance=2)
    got = econ_events.window_candidates(
        conn, TS, settings=WindowSettings(3, 30, 15), currencies=USDJPY)
    assert got == []
    got = econ_events.window_candidates(
        conn, TS, settings=WindowSettings(2, 30, 15), currencies=USDJPY)
    assert len(got) == 1


def test_candidates_without_currencies_still_return_all(conn):
    add(conn, country="All")
    add(conn, country="USD", name="x")
    assert len(econ_events.window_candidates(
        conn, TS, settings=S, currencies=())) == 1


def test_microsecond_precision_holds_even_if_the_sql_cannot_tell(conn):
    edge = add(conn, ts=TS + timedelta(minutes=30), name="edge")
    over = add(conn, ts=TS + timedelta(minutes=30) + US, name="over")
    got = ids(econ_events.window_candidates(conn, TS, settings=S, currencies=USDJPY))
    assert got == [edge]
    got = ids(econ_events.window_candidates(conn, TS + US, settings=S,
                                            currencies=USDJPY))
    assert got == [edge, over]


def test_rows_stored_with_another_offset_are_found_by_instant(conn):
    jst = timezone(timedelta(hours=9))
    eid = add(conn, ts=TS.astimezone(jst), name="jst")
    got = econ_events.window_candidates(conn, TS, settings=S, currencies=USDJPY)
    assert ids(got) == [eid]
    assert got[0].ts == TS and got[0].ts.utcoffset() == timedelta(0)
    # 端も offset に依らず同じ瞬間で判定する
    assert ids(econ_events.window_candidates(
        conn, TS - timedelta(minutes=30), settings=S, currencies=USDJPY)) == [eid]
    assert econ_events.window_candidates(
        conn, TS - timedelta(minutes=30) - US, settings=S,
        currencies=USDJPY) == []
    assert ids(econ_events.window_candidates(
        conn, TS + timedelta(minutes=15), settings=S, currencies=USDJPY)) == [eid]
    assert econ_events.window_candidates(
        conn, TS + timedelta(minutes=15) + US, settings=S,
        currencies=USDJPY) == []


def test_query_naive_now_is_refused_and_other_offsets_are_normalised(conn):
    eid = add(conn)
    with pytest.raises(ValueError):
        econ_events.window_candidates(conn, datetime(2026, 10, 30, 12, 30),
                                      settings=S, currencies=USDJPY)
    jst = timezone(timedelta(hours=9))
    assert ids(econ_events.window_candidates(
        conn, TS.astimezone(jst), settings=S, currencies=USDJPY)) == [eid]


def test_a_row_with_a_naive_ts_is_skipped_with_a_warning(conn, caplog):
    conn.execute("INSERT INTO econ_events (ts,country,name,importance) "
                 "VALUES ('2026-10-30T12:30:00','USD','naive',3)")
    conn.commit()
    ok = add(conn, name="ok")
    with caplog.at_level("WARNING", logger="agentic_fx.econ"):
        got = econ_events.window_candidates(conn, TS, settings=S,
                                            currencies=USDJPY)
    assert ids(got) == [ok]
    assert any("unreadable ts" in r.message for r in caplog.records)


def test_bar_candidates_use_the_bar_interval_and_not_now(conn):
    b = datetime(2026, 10, 30, 13, 0, tzinfo=UTC)
    back_edge = add(conn, ts=b - timedelta(minutes=15), name="back")   # 帯の後端 == b
    miss_back = add(conn, ts=b - timedelta(minutes=15) - US, name="miss")
    front_edge = add(conn, ts=b + timedelta(minutes=1) + timedelta(minutes=30),
                     name="front")
    miss_front = add(conn, ts=b + timedelta(minutes=31) + US, name="miss2")
    got = econ_events.bar_candidates(conn, b, b + timedelta(minutes=1),
                                     settings=S, currencies=USDJPY)
    assert ids(got) == [back_edge, front_edge]
    assert miss_back not in ids(got) and miss_front not in ids(got)
    # now=b+5m の候補には後端の event は入らない
    assert back_edge not in ids(econ_events.window_candidates(
        conn, b + timedelta(minutes=5), settings=S, currencies=USDJPY))


def test_weekend_rows_use_real_utc_times(conn):
    fri_close = datetime(2026, 10, 30, 21, 0, tzinfo=UTC)
    sun_open = datetime(2026, 11, 1, 21, 0, tzinfo=UTC)
    fri = add(conn, ts=fri_close, name="fri")
    sun = add(conn, ts=sun_open + timedelta(minutes=30), name="sun")
    assert ids(econ_events.window_candidates(
        conn, fri_close + timedelta(minutes=15), settings=S,
        currencies=USDJPY)) == [fri]
    assert ids(econ_events.window_candidates(
        conn, sun_open, settings=S, currencies=USDJPY)) == [sun]
    assert econ_events.window_candidates(
        conn, sun_open - US, settings=S, currencies=USDJPY) == []


# --- 7 日先の検索 -----------------------------------------------------------

def test_upcoming_targets_end_at_seven_days_inclusive(conn):
    now = datetime(2026, 10, 30, 0, 0, tzinfo=UTC)
    edge = add(conn, ts=now + timedelta(days=7), name="edge")
    add(conn, ts=now + timedelta(days=7) + US, name="over")
    at_now = add(conn, ts=now, name="now")
    add(conn, ts=now - US, name="past")
    got = econ_events.upcoming_targets(conn, now, settings=S, currencies=USDJPY)
    assert ids(got) == [at_now, edge]


def test_upcoming_targets_skip_non_targets(conn):
    now = datetime(2026, 10, 30, 0, 0, tzinfo=UTC)
    t = now + timedelta(days=2)
    add(conn, ts=t, country="CAD", name="a")
    add(conn, ts=t, importance=2, name="b")
    sup = add(conn, ts=t, name="c")
    conn.execute("UPDATE econ_events SET superseded_by=? WHERE id=?",
                 (add(conn, ts=t + timedelta(hours=1), name="c2"), sup))
    conn.commit()
    keep_all = add(conn, ts=t, country="All", name="d")
    got = econ_events.upcoming_targets(conn, now, settings=S, currencies=USDJPY)
    assert sup not in ids(got)
    assert {e.name for e in got} == {"d", "c2"}
    assert keep_all in ids(got)


def test_upcoming_horizon_is_adjustable(conn):
    now = datetime(2026, 10, 30, 0, 0, tzinfo=UTC)
    add(conn, ts=now + timedelta(days=3))
    assert econ_events.upcoming_targets(
        conn, now, settings=S, currencies=USDJPY, horizon=timedelta(days=2)) == []


# --- supersede -------------------------------------------------------------

NOW = datetime(2026, 10, 28, 0, 0, tzinfo=UTC)


def _move(conn, *, old_ts, new_ts, country="USD", name="CPI"):
    old = add(conn, ts=old_ts, country=country, name=name)
    new = add(conn, ts=new_ts, country=country, name=name)
    return old, new


def _run(conn, new_ids, fetched, now=NOW):
    out = econ_events.supersede_tx(conn, new_ids=new_ids, fetched_keys=fetched,
                                   now=now)
    assert out.committed
    return out.value


def _sup(conn, row_id):
    return conn.execute("SELECT superseded_by FROM econ_events WHERE id=?",
                        (row_id,)).fetchone()[0]


def _fetched(*ts_list, country="USD", name="CPI"):
    return [(t, country, name) for t in ts_list]


def test_an_unreleased_row_with_a_changed_time_is_superseded(conn):
    old_ts = NOW + timedelta(days=1)
    new_ts = old_ts + timedelta(hours=2)
    old, new = _move(conn, old_ts=old_ts, new_ts=new_ts)
    assert _run(conn, [new], _fetched(new_ts)) == [(old, new)]
    assert _sup(conn, old) == new and _sup(conn, new) is None
    assert conn.execute("SELECT COUNT(*) FROM econ_events").fetchone()[0] == 2
    assert econ_events.window_candidates(
        conn, old_ts, settings=S, currencies=USDJPY) == []
    assert ids(econ_events.window_candidates(
        conn, new_ts, settings=S, currencies=USDJPY)) == [new]


def test_the_second_run_does_nothing(conn):
    old_ts = NOW + timedelta(days=1)
    old, new = _move(conn, old_ts=old_ts, new_ts=old_ts + timedelta(hours=1))
    fetched = _fetched(old_ts + timedelta(hours=1))
    assert _run(conn, [new], fetched) == [(old, new)]
    assert _run(conn, [new], fetched) == []
    assert _sup(conn, old) == new


def test_a_released_row_is_not_superseded_and_now_itself_is_released(conn):
    # ts > now だけ。ts == now は発表済み (置換しない)、1µs 後は未発表。
    on_now, new1 = _move(conn, old_ts=NOW, new_ts=NOW + timedelta(hours=3),
                         name="A")
    after, new2 = _move(conn, old_ts=NOW + US, new_ts=NOW + timedelta(hours=3),
                        name="B")
    past, new3 = _move(conn, old_ts=NOW - timedelta(days=1),
                       new_ts=NOW + timedelta(hours=3), name="C")
    got = _run(conn, [new1, new2, new3],
               [(NOW + timedelta(hours=3), "USD", n) for n in "ABC"])
    assert got == [(after, new2)]
    assert _sup(conn, on_now) is None and _sup(conn, past) is None


def test_seven_days_apart_is_superseded_and_one_microsecond_more_is_not(conn):
    base = NOW + timedelta(days=1)
    near, new1 = _move(conn, old_ts=base, new_ts=base + timedelta(days=7),
                       name="A")
    far, new2 = _move(conn, old_ts=base, new_ts=base + timedelta(days=7) + US,
                      name="B")
    got = _run(conn, [new1, new2],
               [(base + timedelta(days=7), "USD", "A"),
                (base + timedelta(days=7) + US, "USD", "B")])
    assert got == [(near, new1)]
    assert _sup(conn, far) is None


def test_the_same_name_and_country_are_both_required(conn):
    base = NOW + timedelta(days=1)
    other_country = add(conn, ts=base, country="EUR", name="CPI")
    other_name = add(conn, ts=base, country="USD", name="PPI")
    new = add(conn, ts=base + timedelta(hours=1), country="USD", name="CPI")
    assert _run(conn, [new], _fetched(base + timedelta(hours=1))) == []
    assert _sup(conn, other_country) is None and _sup(conn, other_name) is None


def test_a_row_still_in_the_fetch_result_is_another_instance(conn):
    a_ts, b_ts = NOW + timedelta(days=1), NOW + timedelta(days=2)
    a = add(conn, ts=a_ts)
    b = add(conn, ts=b_ts)                           # 新しく入ったが a も取得結果に並ぶ
    assert _run(conn, [b], _fetched(a_ts, b_ts)) == []
    assert _sup(conn, a) is None


def test_an_already_superseded_row_is_not_taken_again(conn):
    base = NOW + timedelta(days=1)
    first = add(conn, ts=base)
    mid = add(conn, ts=base + timedelta(hours=1))
    conn.execute("UPDATE econ_events SET superseded_by=? WHERE id=?", (mid, first))
    conn.commit()
    newer = add(conn, ts=base + timedelta(hours=2))
    assert _run(conn, [newer], _fetched(base + timedelta(hours=2))) == [(mid, newer)]
    assert _sup(conn, first) == mid


def test_only_rows_new_in_this_refresh_replace_anything(conn):
    base = NOW + timedelta(days=1)
    old = add(conn, ts=base)
    stable = add(conn, ts=base + timedelta(hours=1))   # 以前からある (新 row ではない)
    assert _run(conn, [], _fetched(base + timedelta(hours=1))) == []
    assert _sup(conn, old) is None and _sup(conn, stable) is None


def test_the_nearest_old_row_wins_and_ties_go_to_the_lowest_id(conn):
    base = NOW + timedelta(days=2)
    far = add(conn, ts=base - timedelta(days=3), name="CPI")
    near = add(conn, ts=base + timedelta(hours=5), name="CPI")
    new = add(conn, ts=base, name="CPI")
    assert _run(conn, [new], _fetched(base)) == [(near, new)]
    assert _sup(conn, far) is None

    t2 = NOW + timedelta(days=4)
    lo = add(conn, ts=t2 - timedelta(hours=1), name="PPI")
    hi = add(conn, ts=t2 + timedelta(hours=1), name="PPI")
    new2 = add(conn, ts=t2, name="PPI")
    assert _run(conn, [new2], _fetched(t2, name="PPI")) == [(lo, new2)]
    assert _sup(conn, hi) is None


def test_one_new_row_replaces_at_most_one_and_one_old_row_has_one_successor(conn):
    base = NOW + timedelta(days=2)
    o1 = add(conn, ts=base - timedelta(hours=1))
    o2 = add(conn, ts=base - timedelta(hours=2))
    n1 = add(conn, ts=base)
    assert _run(conn, [n1], _fetched(base)) == [(o1, n1)]
    assert _sup(conn, o2) is None

    # 新 row が 2 行、旧 row が 1 行: 近い方の新 row だけが結ばれる
    t = NOW + timedelta(days=5)
    old = add(conn, ts=t, name="PPI")
    far_new = add(conn, ts=t + timedelta(hours=3), name="PPI")
    near_new = add(conn, ts=t + timedelta(hours=1), name="PPI")
    got = _run(conn, [far_new, near_new],
               _fetched(t + timedelta(hours=3), t + timedelta(hours=1),
                        name="PPI"))
    assert got == [(old, near_new)]
    assert _sup(conn, far_new) is None


def test_two_new_rows_each_take_their_own_old_row(conn):
    t = NOW + timedelta(days=2)
    o1 = add(conn, ts=t)
    o2 = add(conn, ts=t + timedelta(days=1))
    n1 = add(conn, ts=t + timedelta(hours=1))
    n2 = add(conn, ts=t + timedelta(days=1, hours=1))
    got = _run(conn, [n1, n2], _fetched(t + timedelta(hours=1),
                                        t + timedelta(days=1, hours=1)))
    assert sorted(got) == [(o1, n1), (o2, n2)]


def test_supersede_with_no_new_rows_is_a_no_op_and_naive_now_is_refused(conn):
    assert econ_events.supersede_tx(
        conn, new_ids=[], fetched_keys=[], now=NOW).value == []
    with pytest.raises(ValueError):
        econ_events.supersede_in_tx(conn, new_ids=[1], fetched_keys=[],
                                    now=datetime(2026, 10, 28))


def test_supersede_tx_refuses_an_open_transaction_and_honours_begin(conn):
    old_ts = NOW + timedelta(days=1)
    old, new = _move(conn, old_ts=old_ts, new_ts=old_ts + timedelta(hours=1))
    conn.execute("BEGIN")
    with pytest.raises(RuntimeError):
        econ_events.supersede_tx(conn, new_ids=[new], fetched_keys=[], now=NOW)
    conn.rollback()
    out = econ_events.supersede_tx(conn, new_ids=[new], fetched_keys=[], now=NOW,
                                   begin=lambda c: False)
    assert out.status == "yielded" and _sup(conn, old) is None


def test_a_failing_supersede_rolls_back_every_update(conn):
    t = NOW + timedelta(days=2)
    o1 = add(conn, ts=t, name="A")
    n1 = add(conn, ts=t + timedelta(hours=1), name="A")
    o2 = add(conn, ts=t, name="B")
    n2 = add(conn, ts=t + timedelta(hours=1), name="B")
    conn.execute("CREATE TEMP TRIGGER boom BEFORE UPDATE ON econ_events "
                 "WHEN OLD.id=%d BEGIN SELECT RAISE(ABORT,'boom'); END" % o2)
    with pytest.raises(Exception, match="boom"):
        econ_events.supersede_tx(
            conn, new_ids=[n1, n2], now=NOW,
            fetched_keys=[(t + timedelta(hours=1), "USD", "A"),
                          (t + timedelta(hours=1), "USD", "B")])
    assert _sup(conn, o1) is None and _sup(conn, o2) is None


# --- 連結閉包の成分 ---------------------------------------------------------

def _chain(conn):
    """TS-100m (id 低) → TS-50m → TS: 隣同士の帯が端で重なる 1 成分。"""
    far = add(conn, ts=TS - timedelta(minutes=90), name="far")
    mid = add(conn, ts=TS - timedelta(minutes=45), name="mid")
    near = add(conn, ts=TS, name="near")
    return far, mid, near


def test_active_components_keep_the_minimum_id_of_the_whole_component(conn):
    near = add(conn, ts=TS, name="near")                  # 先に入れるので id 小
    far = add(conn, ts=TS - timedelta(minutes=90), name="far")
    mid = add(conn, ts=TS - timedelta(minutes=45), name="mid")
    assert near < far < mid
    # now は near の帯だけに掛かり、候補 (now ± after/before) に far は入らない
    now = TS + timedelta(minutes=10)
    assert far not in ids(econ_events.window_candidates(
        conn, now, settings=S, currencies=USDJPY))
    comps = econ_events.active_components(conn, now, settings=S,
                                          currencies=USDJPY)
    assert len(comps) == 1
    assert comps[0].id == near
    assert sorted(ids(comps[0].events)) == sorted([far, mid, near])


def test_the_component_id_is_the_older_events_when_it_has_the_minimum(conn):
    far, mid, near = _chain(conn)
    comps = econ_events.active_components(
        conn, TS + timedelta(minutes=10), settings=S, currencies=USDJPY)
    assert [c.id for c in comps] == [far]
    assert sorted(ids(comps[0].events)) == [far, mid, near]


def test_the_closure_stops_at_a_gap_and_ignores_other_currencies(conn):
    far, mid, near = _chain(conn)
    add(conn, ts=TS - timedelta(minutes=150) - US, name="gap")      # 1µs 離れる
    add(conn, ts=TS - timedelta(minutes=135), country="CAD", name="cad")
    comps = econ_events.active_components(
        conn, TS, settings=S, currencies=USDJPY)
    assert sorted(ids(comps[0].events)) == [far, mid, near]


def test_bar_components_use_the_closure_and_the_bar_interval(conn):
    far, mid, near = _chain(conn)
    comps = econ_events.bar_components(
        conn, TS + timedelta(minutes=5), TS + timedelta(minutes=6),
        settings=S, currencies=USDJPY)
    assert [c.id for c in comps] == [far]
    assert econ_events.bar_components(
        conn, TS + timedelta(hours=3), TS + timedelta(hours=4),
        settings=S, currencies=USDJPY) == ()
    with pytest.raises(ValueError):
        econ_events.bar_components(conn, TS, TS - US, settings=S,
                                   currencies=USDJPY)
    with pytest.raises(ValueError):
        econ_events.active_components(conn, datetime(2026, 1, 1), settings=S,
                                      currencies=USDJPY)


def test_a_new_row_missing_from_the_fetch_or_already_superseded_replaces_nothing(conn):
    old = add(conn, ts=TS + timedelta(days=1))
    new = add(conn, ts=TS + timedelta(days=1, hours=2))
    now = TS
    assert econ_events.supersede_in_tx(
        conn, new_ids=[new], fetched_keys=[], now=now) == []
    other = add(conn, ts=TS + timedelta(days=3), name="other")
    conn.execute("UPDATE econ_events SET superseded_by=? WHERE id=?", (other, new))
    key = (TS + timedelta(days=1, hours=2), "USD", "CPI")
    assert econ_events.supersede_in_tx(
        conn, new_ids=[new], fetched_keys=[key], now=now) == []
    assert conn.execute("SELECT superseded_by FROM econ_events WHERE id=?",
                        (old,)).fetchone()[0] is None
