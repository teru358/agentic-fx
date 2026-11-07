from datetime import datetime, timedelta, timezone

from agentic_fx.analytics import econ_reaction_pass as reaction_pass
from agentic_fx.analytics.econ_reaction_pass import run_pass
from agentic_fx.store import econ_reaction_values as rv
from agentic_fx.store.db import connect, init_db

TS = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
SETTINGS = {"EURUSD": ("EUR", "USD"), "USDJPY": ("USD", "JPY")}


def _conn(tmp_path):
    conn = connect(tmp_path / "pass.db")
    init_db(conn)
    return conn


def _event(conn, country="JPY", name="Rate", ts=TS, importance=3, superseded_by=None):
    event = conn.execute(
        "INSERT INTO econ_events(ts,country,name,importance,superseded_by) VALUES(?,?,?,?,?)",
        (ts.isoformat(), country, name, importance, superseded_by)).lastrowid
    conn.commit()
    return event


def _set(conn, event, pair="USDJPY", *, state="complete", rows="observed", ts=TS, spread=1.0):
    """rows: observed (3 窓とも解決できる足) / marker (全部欠損の印) / none。"""
    set_id = conn.execute(
        "INSERT INTO ohlcv_slice_sets(event_id,pair,range_start,range_end,expected_rows,state,"
        "spread_pips,spread_source,created_at,completed_at,rule_version) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,1)",
        (event, pair, (ts - timedelta(minutes=15)).isoformat(),
         (ts + timedelta(hours=4)).isoformat(), 255, state,
         None if spread is None else spread, None if spread is None else "assumed",
         ts.isoformat(), ts.isoformat())).lastrowid
    for i in range(-1, 240) if rows != "none" else ():
        at = (ts + timedelta(minutes=i)).isoformat()
        if rows == "marker":
            conn.execute(
                "INSERT INTO ohlcv_slices(set_id,bar_time,source,quality) "
                "VALUES(?,?,'econ-normalizer','datafeed_outage_gap')", (set_id, at))
            continue
        price = 150 + max(i, 0) * .01
        conn.execute(
            "INSERT INTO ohlcv_slices(set_id,bar_time,open,high,low,close,volume,source,quality) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (set_id, at, price, price + .01, price - .01, price, 1, "feed", "observed"))
    conn.commit()
    return set_id


def _run(conn, pairs=SETTINGS, *, now=TS, stopped=lambda: False, write=None, cursor=None,
         pip_size_fn=lambda pair: .01):
    def begin(c):
        c.execute("BEGIN IMMEDIATE")
        return True

    def default_write(op):
        return op(conn, begin, lambda: None)

    run_pass(read=lambda fn: fn(conn), write=write or default_write, stopped=stopped,
             pair_currencies=pairs, pip_size_fn=pip_size_fn, now=now, cursor=cursor)


def _values(conn, set_id):
    return conn.execute(
        "SELECT window_min,status,range_pips,close_change_pips,abs_close_change_pips,"
        "net_abs_close_change_pips,computed_at FROM econ_reaction_values "
        "WHERE set_id=? ORDER BY window_min", (set_id,)).fetchall()


def _keys(conn):
    return conn.execute("SELECT event_id,currency,set_id FROM econ_reaction_keys "
                        "ORDER BY event_id,currency").fetchall()


def test_pass_computes_values_and_canonical_key_for_terminal_event(tmp_path):
    conn = _conn(tmp_path)
    event = _event(conn)
    set_id = _set(conn, event)
    _run(conn, {"USDJPY": ("USD", "JPY")})
    assert [r[1] for r in _values(conn, set_id)] == ["resolved"] * 3
    assert conn.execute("SELECT currency FROM econ_reaction_keys WHERE event_id=?",
                        (event,)).fetchone()[0] == "JPY"


def test_a_production_shaped_backlog_gets_three_rows_per_set_across_batches(tmp_path, monkeypatch):
    monkeypatch.setattr(reaction_pass, "REACTION_BATCH", 2)
    conn = _conn(tmp_path)
    good = [_set(conn, _event(conn, name=f"E{i}")) for i in range(3)]
    marker = _set(conn, _event(conn, name="Marker"), rows="marker")
    lost = _set(conn, _event(conn, name="Lost"), state="unrecoverable", rows="none", spread=None)
    all_sets = [*good, marker, lost]

    def done():
        return [conn.execute("SELECT COUNT(*) FROM econ_reaction_values WHERE set_id=?",
                             (s,)).fetchone()[0] for s in all_sets]

    _run(conn)
    assert done() == [3, 3, 0, 0, 0]
    _run(conn)
    assert done() == [3, 3, 3, 3, 0]
    _run(conn)
    assert done() == [3] * 5
    assert all(r[1] == "resolved" for s in good for r in _values(conn, s))
    for s in (marker, lost):
        assert [r[1] for r in _values(conn, s)] == ["unresolvable"] * 3
        assert all(v is None for r in _values(conn, s) for v in r[2:6])
    assert len(_keys(conn)) == 5


def test_a_second_pass_changes_nothing_and_pair_order_does_not_move_keys_or_stats(tmp_path):
    conn = _conn(tmp_path)
    usd = _event(conn, "USD", "CPI")
    _set(conn, usd, "EURUSD")
    _set(conn, usd, "USDJPY")
    _set(conn, _event(conn, "JPY", "Rate"), "USDJPY")
    _run(conn)
    values = conn.execute("SELECT * FROM econ_reaction_values ORDER BY 1,2").fetchall()
    keys = conn.execute("SELECT * FROM econ_reaction_keys ORDER BY 1,2").fetchall()
    stats = rv.bundle_stats(conn)
    assert len(values) == 9 and values and keys and stats
    later = TS + timedelta(days=3)
    _run(conn, now=later)
    assert conn.execute("SELECT * FROM econ_reaction_values ORDER BY 1,2").fetchall() == values
    assert conn.execute("SELECT * FROM econ_reaction_keys ORDER BY 1,2").fetchall() == keys
    _run(conn, {"USDJPY": ("USD", "JPY"), "EURUSD": ("EUR", "USD")}, now=later)
    assert conn.execute("SELECT * FROM econ_reaction_values ORDER BY 1,2").fetchall() == values
    assert conn.execute("SELECT * FROM econ_reaction_keys ORDER BY 1,2").fetchall() == keys
    assert rv.bundle_stats(conn) == stats


def test_a_currency_key_waits_for_its_canonical_pair_set(tmp_path):
    conn = _conn(tmp_path)
    event = _event(conn, "USD", "CPI")
    jpy_set = _set(conn, event, "USDJPY")
    _run(conn)
    assert len(_values(conn, jpy_set)) == 3
    assert _keys(conn) == []
    eur_set = _set(conn, event, "EURUSD")
    _run(conn)
    assert [tuple(r) for r in _keys(conn)] == [(event, "USD", eur_set)]
    assert len(_values(conn, eur_set)) == 3


def test_an_all_event_gets_one_key_per_currency_from_each_canonical_set(tmp_path):
    conn = _conn(tmp_path)
    event = _event(conn, "All", "Holiday")
    eur_set = _set(conn, event, "EURUSD")
    jpy_set = _set(conn, event, "USDJPY")
    _run(conn)
    assert [tuple(r) for r in _keys(conn)] == [
        (event, "EUR", eur_set), (event, "JPY", jpy_set), (event, "USD", eur_set)]


def test_dropping_a_pair_adds_the_missing_key_to_the_remaining_set_without_new_values(tmp_path):
    conn = _conn(tmp_path)
    event = _event(conn, "USD", "CPI")
    jpy_set = _set(conn, event, "USDJPY")
    _run(conn)
    values = _values(conn, jpy_set)
    assert _keys(conn) == []
    later = TS + timedelta(days=1)
    _run(conn, {"USDJPY": ("USD", "JPY")}, now=later)
    assert [tuple(r) for r in _keys(conn)] == [(event, "USD", jpy_set)]
    assert _values(conn, jpy_set) == values


def test_a_currency_no_longer_in_settings_gets_values_but_no_key(tmp_path):
    conn = _conn(tmp_path)
    event = _event(conn, "EUR", "GDP")
    set_id = _set(conn, event, "USDJPY")
    _run(conn)
    assert len(_values(conn, set_id)) == 3
    assert _keys(conn) == []
    assert rv.backlog_counts(conn, pair_currencies=SETTINGS) == {
        "sets_without_values": 0, "events_without_currency_key": 0}
    assert rv.backlog_counts(conn, pair_currencies={"USDJPY": ("USD", "JPY")}) == {
        "sets_without_values": 0, "events_without_currency_key": 1}


def test_a_superseded_event_makes_neither_values_nor_keys_nor_samples(tmp_path):
    conn = _conn(tmp_path)
    new = _event(conn, "JPY", "Rate", ts=TS + timedelta(hours=1))
    old = _event(conn, "JPY", "Rate", superseded_by=new)
    old_set = _set(conn, old)
    _run(conn)
    assert _values(conn, old_set) == []
    assert _keys(conn) == []
    assert rv.load_samples(conn) == ()


def test_a_failing_set_is_logged_and_the_rest_of_the_pass_continues(tmp_path, caplog):
    conn = _conn(tmp_path)
    first = _set(conn, _event(conn, name="A"))
    second = _set(conn, _event(conn, name="B"))

    def begin(c):
        c.execute("BEGIN IMMEDIATE")
        return True

    def write(op):
        if write.calls == 0:
            write.calls += 1
            raise RuntimeError("injected")
        write.calls += 1
        return op(conn, begin, lambda: None)

    write.calls = 0
    with caplog.at_level("ERROR"):
        _run(conn, write=write)
    assert _values(conn, first) == []
    assert len(_values(conn, second)) == 3
    assert any(f"set {first}" in r.getMessage() and "RuntimeError" in r.getMessage()
               for r in caplog.records)
    _run(conn)
    assert len(_values(conn, first)) == 3


def test_a_stop_request_ends_the_pass_before_the_next_set(tmp_path):
    conn = _conn(tmp_path)
    first = _set(conn, _event(conn, name="A"))
    second = _set(conn, _event(conn, name="B"))
    calls = []

    def stopped():
        return len(calls) >= 1

    def begin(c):
        c.execute("BEGIN IMMEDIATE")
        return True

    def write(op):
        calls.append(1)
        return op(conn, begin, lambda: None)

    _run(conn, stopped=stopped, write=write)
    assert len(_values(conn, first)) == 3
    assert _values(conn, second) == []


def test_a_write_that_the_worker_declines_ends_the_pass(tmp_path):
    conn = _conn(tmp_path)
    first = _set(conn, _event(conn, name="A"))
    second = _set(conn, _event(conn, name="B"))
    seen = []
    _run(conn, write=lambda op: seen.append(1))
    assert seen == [1]
    assert _values(conn, first) == _values(conn, second) == []


def test_complete_set_without_spread_is_unresolvable_and_logged(tmp_path, caplog):
    conn = _conn(tmp_path)
    set_id = _set(conn, _event(conn), spread=None)
    with caplog.at_level("ERROR"):
        _run(conn)
    assert [r[1] for r in _values(conn, set_id)] == ["unresolvable"] * 3
    assert any("no spread" in r.getMessage() for r in caplog.records)


def test_pass_does_not_touch_state_machine_module():
    import agentic_fx.analytics.econ_reaction_pass as mod
    src = open(mod.__file__).read()
    assert "signal_shadow" not in src


def _broken_pip(pair):
    if pair == "EURUSD":
        raise RuntimeError("pip size unavailable")
    return .01


def test_permanently_failing_sets_beyond_one_batch_do_not_starve_later_sets(tmp_path, monkeypatch):
    monkeypatch.setattr(reaction_pass, "REACTION_BATCH", 2)
    conn = _conn(tmp_path)
    bad = [_set(conn, _event(conn, country="EUR", name=f"Bad{i}"), "EURUSD") for i in range(3)]
    good = _set(conn, _event(conn, name="Good"))
    cursor = reaction_pass.ReactionCursor()
    for _ in range(3):
        _run(conn, cursor=cursor, pip_size_fn=_broken_pip)
    assert len(_values(conn, good)) == 3
    assert all(_values(conn, s) == [] for s in bad)


def test_without_a_cursor_the_pass_starts_from_the_first_set_every_time(tmp_path, monkeypatch):
    monkeypatch.setattr(reaction_pass, "REACTION_BATCH", 2)
    conn = _conn(tmp_path)
    for i in range(2):
        _set(conn, _event(conn, country="EUR", name=f"Bad{i}"), "EURUSD")
    good = _set(conn, _event(conn, name="Good"))
    for _ in range(3):
        _run(conn, pip_size_fn=_broken_pip)
    assert _values(conn, good) == []


def test_the_cursor_wraps_to_the_start_after_the_end_and_retries_earlier_sets(tmp_path, monkeypatch):
    monkeypatch.setattr(reaction_pass, "REACTION_BATCH", 2)
    conn = _conn(tmp_path)
    bad = [_set(conn, _event(conn, country="EUR", name=f"Bad{i}"), "EURUSD") for i in range(2)]
    good = _set(conn, _event(conn, name="Good"))
    cursor = reaction_pass.ReactionCursor()
    _run(conn, cursor=cursor, pip_size_fn=_broken_pip)
    assert cursor.after == bad[-1]
    _run(conn, cursor=cursor, pip_size_fn=_broken_pip)
    assert cursor.after is None and len(_values(conn, good)) == 3
    _run(conn, cursor=cursor)
    assert all(len(_values(conn, s)) == 3 for s in bad)


def test_a_stop_or_declined_write_does_not_move_the_cursor_past_the_unhandled_set(tmp_path, monkeypatch):
    monkeypatch.setattr(reaction_pass, "REACTION_BATCH", 2)
    conn = _conn(tmp_path)
    sets = [_set(conn, _event(conn, name=f"E{i}")) for i in range(4)]
    cursor = reaction_pass.ReactionCursor()
    _run(conn, cursor=cursor, write=lambda op: None)
    assert cursor.after is None
    _run(conn, cursor=cursor)
    assert cursor.after == sets[1]
    _run(conn, cursor=cursor, write=lambda op: None)
    assert cursor.after == sets[1]


def test_failure_logs_name_the_set_and_event_and_the_exception_type(tmp_path, caplog):
    conn = _conn(tmp_path)
    event = _event(conn, country="EUR", name="Bad")
    set_id = _set(conn, event, "EURUSD")
    with caplog.at_level("ERROR"):
        _run(conn, pip_size_fn=_broken_pip)
    text = " ".join(r.getMessage() for r in caplog.records)
    assert f"set {set_id} event {event}" in text
    assert "RuntimeError: pip size unavailable" in text

    caplog.clear()
    event2 = _event(conn, name="Write")
    set2 = _set(conn, event2)

    def write(op):
        raise OSError("disk https://secret.example/x?apikey=abc")

    with caplog.at_level("ERROR"):
        _run(conn, write=write)
    text = " ".join(r.getMessage() for r in caplog.records)
    assert f"set {set2} event {event2}" in text
    assert "OSError" in text and "secret.example" not in text and "abc" not in text
    # 整形済みの出力 (traceback を含む) にも元の例外の本文が残らない
    assert all(r.exc_info is None for r in caplog.records)
    assert "secret.example" not in caplog.text and "apikey" not in caplog.text
