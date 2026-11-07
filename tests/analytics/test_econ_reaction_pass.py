from datetime import datetime, timedelta, timezone

from agentic_fx.analytics.econ_reaction_pass import run_pass
from agentic_fx.store.db import connect, init_db


def test_pass_computes_values_and_canonical_key_for_terminal_event(tmp_path):
    conn = connect(tmp_path / "pass.db")
    init_db(conn)
    ts = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
    event = conn.execute("INSERT INTO econ_events(ts,country,name,importance) VALUES(?,?,?,?)",
                         (ts.isoformat(), "JPY", "Rate", 3)).lastrowid
    set_id = conn.execute("INSERT INTO ohlcv_slice_sets(event_id,pair,range_start,range_end,expected_rows,state,spread_pips,spread_source,created_at,completed_at,rule_version) VALUES(?,?,?,?,?,'complete',1,'assumed',?,?,1)",
        (event, "USDJPY", (ts-timedelta(minutes=15)).isoformat(), (ts+timedelta(hours=4)).isoformat(), 255, ts.isoformat(), ts.isoformat())).lastrowid
    for i in range(-1, 240):
        at = ts + timedelta(minutes=i)
        price = 150 + max(i, 0) * .01
        conn.execute("INSERT INTO ohlcv_slices(set_id,bar_time,open,high,low,close,volume,source,quality) VALUES(?,?,?,?,?,?,?,?,?)",
            (set_id, at.isoformat(), price, price+.01, price-.01, price, 1, "feed", "observed"))
    conn.commit()
    pair_map = {"USDJPY": ("USD", "JPY")}
    def read(fn): return fn(conn)
    def begin(c):
        c.execute("BEGIN IMMEDIATE")
        return True
    def write(op): return op(conn, begin, lambda: None)
    run_pass(read=read, write=write, stopped=lambda: False, pair_currencies=pair_map,
             pip_size_fn=lambda pair: .01, now=ts)
    assert conn.execute("SELECT COUNT(*) FROM econ_reaction_values WHERE set_id=?", (set_id,)).fetchone()[0] == 3
    assert conn.execute("SELECT currency FROM econ_reaction_keys WHERE event_id=?", (event,)).fetchone()[0] == "JPY"
    assert conn.execute("SELECT COUNT(*) FROM econ_reaction_values WHERE set_id=? AND status='resolved'", (set_id,)).fetchone()[0] == 3
