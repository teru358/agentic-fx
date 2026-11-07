from datetime import datetime, timezone

import pytest

from agentic_fx.analytics.econ_reactions import WindowValue
from agentic_fx.store import econ_reaction_values as rv
from agentic_fx.store.db import connect, init_db


def _db(tmp_path):
    conn = connect(tmp_path / "reactions.db")
    init_db(conn)
    now = "2026-11-09T12:00:00+00:00"
    event = conn.execute("INSERT INTO econ_events(ts,country,name,importance) VALUES(?,?,?,?)",
                         (now, "JPY", "Rate", 3)).lastrowid
    set_id = conn.execute("INSERT INTO ohlcv_slice_sets(event_id,pair,range_start,range_end,expected_rows,state,created_at,completed_at,rule_version) VALUES(?,?,?,?,?,'complete',?,?,1)",
                          (event, "USDJPY", "2026-11-09T11:45:00+00:00", "2026-11-09T16:00:00+00:00", 255, now, now)).lastrowid
    conn.commit()
    return conn, event, set_id


def test_write_snapshot_and_key_once_and_load_aggregate(tmp_path):
    conn, event, set_id = _db(tmp_path)
    vals = tuple(WindowValue(w, "resolved", 10.0, -2.0, 2.0, 1.0) for w in (15, 60, 240))
    now = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
    assert rv.write_set_tx(conn, set_id=set_id, values=vals, keys=[(event, "JPY")], now=now).committed
    # Repeated write preserves the original snapshot and key association.
    assert rv.write_set_tx(conn, set_id=set_id, values=(), keys=[(event, "JPY")], now=now).committed
    assert conn.execute("SELECT COUNT(*) FROM econ_reaction_values WHERE set_id=?", (set_id,)).fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM econ_reaction_keys WHERE event_id=?", (event,)).fetchone()[0] == 1
    assert len(rv.load_samples(conn)) == 3
    assert {r[0] for r in conn.execute("SELECT computed_at FROM econ_reaction_values") } == {now.isoformat()}


def test_values_and_keys_rollback_together_on_checkpoint(tmp_path):
    conn, event, set_id = _db(tmp_path)
    def fail(): raise RuntimeError("injected")
    vals = tuple(WindowValue(w, "unresolvable", None, None, None, None) for w in (15, 60, 240))
    with pytest.raises(RuntimeError):
        rv.write_set_tx(conn, set_id=set_id, values=vals, keys=[(event, "JPY")],
                        now=datetime.now(timezone.utc), checkpoint=fail)
    assert conn.execute("SELECT COUNT(*) FROM econ_reaction_values").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM econ_reaction_keys").fetchone()[0] == 0


PAIRS = {"EURUSD": ("EUR", "USD"), "USDJPY": ("USD", "JPY")}


def _vals(status="resolved"):
    if status == "resolved":
        return tuple(WindowValue(w, "resolved", 10.0, -2.0, 2.0, 1.0) for w in (15, 60, 240))
    return tuple(WindowValue(w, "unresolvable", None, None, None, None) for w in (15, 60, 240))


def _more_set(conn, country, name, pair, *, state="complete", superseded_by=None):
    ts = "2026-11-10T12:00:00+00:00"
    event = conn.execute(
        "INSERT INTO econ_events(ts,country,name,importance,superseded_by) VALUES(?,?,?,?,?)",
        (ts, country, name, 3, superseded_by)).lastrowid
    set_id = conn.execute(
        "INSERT INTO ohlcv_slice_sets(event_id,pair,range_start,range_end,expected_rows,state,"
        "created_at,completed_at,rule_version) VALUES(?,?,?,?,?,?,?,?,1)",
        (event, pair, "2026-11-10T11:45:00+00:00", "2026-11-10T16:00:00+00:00", 255, state,
         ts, None if state in ("awaiting", "staging") else ts)).lastrowid
    conn.commit()
    return event, set_id


NOW = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)


def test_pending_sets_lists_terminal_sets_without_values_or_without_a_canonical_key(tmp_path):
    conn, event, set_id = _db(tmp_path)
    _, awaiting = _more_set(conn, "JPY", "A", "USDJPY", state="awaiting")
    _, lost = _more_set(conn, "JPY", "B", "USDJPY", state="unrecoverable")
    new = conn.execute("INSERT INTO econ_events(ts,country,name,importance) VALUES(?,?,?,?)",
                       ("2026-11-10T13:00:00+00:00", "JPY", "Rate", 3)).lastrowid
    _, old_set = _more_set(conn, "JPY", "C", "USDJPY", superseded_by=new)
    got = rv.pending_sets(conn, pair_currencies=PAIRS, limit=10)
    assert [r["id"] for r in got] == [set_id, lost]
    assert all("missing_keys" not in r for r in got)
    assert [r["id"] for r in rv.pending_sets(conn, pair_currencies=PAIRS, limit=1)] == [set_id]
    assert [r["id"] for r in rv.pending_sets(conn, pair_currencies=PAIRS, limit=10,
                                             after=set_id)] == [lost]
    assert rv.pending_sets(conn, pair_currencies=PAIRS, limit=0) == []
    assert rv.write_set_tx(conn, set_id=set_id, values=_vals(), keys=[(event, "JPY")], now=NOW).committed
    assert [r["id"] for r in rv.pending_sets(conn, pair_currencies=PAIRS, limit=10)] == [lost]
    del awaiting, old_set


def test_pending_sets_reports_a_missing_canonical_key_on_a_set_that_already_has_values(tmp_path):
    conn, _, _ = _db(tmp_path)
    usd, usdjpy = _more_set(conn, "USD", "CPI", "USDJPY")
    assert rv.write_set_tx(conn, set_id=usdjpy, values=_vals(), keys=[], now=NOW).committed
    # EURUSD is first in settings, so USD is not canonical on USDJPY yet.
    assert [r["id"] for r in rv.pending_sets(conn, pair_currencies=PAIRS, limit=10)
            if r["event_id"] == usd] == []
    only_jpy = {"USDJPY": ("USD", "JPY")}
    got = [r for r in rv.pending_sets(conn, pair_currencies=only_jpy, limit=10) if r["event_id"] == usd]
    assert [(r["id"], r["missing_keys"]) for r in got] == [(usdjpy, ["USD"])]
    assert rv.write_set_tx(conn, set_id=usdjpy, values=(), keys=[(usd, "USD")], now=NOW).committed
    assert [r for r in rv.pending_sets(conn, pair_currencies=only_jpy, limit=10)
            if r["event_id"] == usd] == []


def test_a_set_with_one_or_two_value_rows_is_a_conflict_and_is_left_alone(tmp_path):
    conn, event, set_id = _db(tmp_path)
    conn.execute("INSERT INTO econ_reaction_values(set_id,window_min,status,rule_version,computed_at) "
                 "VALUES(?,15,'unresolvable',1,?)", (set_id, NOW.isoformat()))
    conn.commit()
    result = rv.write_set_tx(conn, set_id=set_id, values=_vals(), keys=[(event, "JPY")], now=NOW)
    assert not result.committed and result.status == "conflict"
    assert conn.execute("SELECT COUNT(*) FROM econ_reaction_values").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM econ_reaction_keys").fetchone()[0] == 0
    assert rv.pending_sets(conn, pair_currencies=PAIRS, limit=10) == []


def test_write_stamps_rule_version_and_times_and_never_rewrites_values_or_keys(tmp_path):
    conn, event, set_id = _db(tmp_path)
    _, other = _more_set(conn, "JPY", "Other", "USDJPY")
    assert rv.write_set_tx(conn, set_id=set_id, values=_vals(), keys=[(event, "JPY")], now=NOW).committed
    later = NOW.replace(day=20)
    assert rv.write_set_tx(conn, set_id=set_id, values=_vals("unresolvable"),
                           keys=[(event, "JPY")], now=later).committed
    assert rv.write_set_tx(conn, set_id=other, values=_vals(), keys=[(event, "JPY")],
                           now=later).committed
    rows = conn.execute("SELECT status,rule_version,computed_at FROM econ_reaction_values "
                        "WHERE set_id=?", (set_id,)).fetchall()
    assert {tuple(r) for r in rows} == {("resolved", 1, NOW.isoformat())}
    key = conn.execute("SELECT set_id,decided_at FROM econ_reaction_keys WHERE event_id=?",
                       (event,)).fetchall()
    assert [tuple(r) for r in key] == [(set_id, NOW.isoformat())]
    with pytest.raises(ValueError):
        rv.write_set_tx(conn, set_id=other + 100, values=_vals()[:2], keys=[], now=NOW)


def test_bundle_stats_skip_superseded_events_and_unkeyed_sets(tmp_path):
    conn, event, set_id = _db(tmp_path)
    assert rv.write_set_tx(conn, set_id=set_id, values=_vals(), keys=[(event, "JPY")], now=NOW).committed
    new = conn.execute("INSERT INTO econ_events(ts,country,name,importance) VALUES(?,?,?,?)",
                       ("2026-11-10T13:00:00+00:00", "JPY", "Rate", 3)).lastrowid
    conn.commit()
    n_before = sum(s.n for s in rv.bundle_stats(conn))
    assert n_before == 6
    conn.execute("UPDATE econ_events SET superseded_by=? WHERE id=?", (new, event))
    conn.commit()
    assert rv.bundle_stats(conn) == ()


def test_purge_after_values_keeps_values_keys_and_stats(tmp_path):
    from datetime import timedelta
    from agentic_fx.store import ohlcv_slices as os_
    conn, event, set_id = _db(tmp_path)
    conn.execute("INSERT INTO ohlcv_slices(set_id,bar_time,open,high,low,close,volume,source,quality) "
                 "VALUES(?,?,150,150.1,149.9,150,1,'feed','observed')",
                 (set_id, "2026-11-09T12:00:00+00:00"))
    conn.execute("UPDATE ohlcv_slice_sets SET created_at=? WHERE id=?",
                 ((NOW - timedelta(days=os_.RETENTION_DAYS + 1)).isoformat(), set_id))
    conn.commit()
    assert os_.purgeable_sets(conn, now=NOW, limit=10) == []
    assert os_.purge_step(conn, set_id=set_id, now=NOW).status == "conflict"
    assert rv.write_set_tx(conn, set_id=set_id, values=_vals(), keys=[(event, "JPY")], now=NOW).committed
    values = conn.execute("SELECT * FROM econ_reaction_values ORDER BY 1,2").fetchall()
    keys = conn.execute("SELECT * FROM econ_reaction_keys").fetchall()
    stats = rv.bundle_stats(conn)
    assert os_.purgeable_sets(conn, now=NOW, limit=10) == [set_id]
    assert os_.purge_step(conn, set_id=set_id, now=NOW).done
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_slices").fetchone()[0] == 0
    assert conn.execute("SELECT * FROM econ_reaction_values ORDER BY 1,2").fetchall() == values
    assert conn.execute("SELECT * FROM econ_reaction_keys").fetchall() == keys
    assert rv.bundle_stats(conn) == stats and sum(s.n for s in stats) == 6


def test_backlog_counts_count_sets_without_values_and_events_without_a_currency_key(tmp_path):
    conn, event, set_id = _db(tmp_path)
    _more_set(conn, "EUR", "GDP", "USDJPY")
    only_jpy = {"USDJPY": ("USD", "JPY")}
    assert rv.backlog_counts(conn, pair_currencies=only_jpy) == {
        "sets_without_values": 2, "events_without_currency_key": 1}
    assert rv.write_set_tx(conn, set_id=set_id, values=_vals(), keys=[(event, "JPY")], now=NOW).committed
    assert rv.backlog_counts(conn, pair_currencies=only_jpy) == {
        "sets_without_values": 1, "events_without_currency_key": 1}
