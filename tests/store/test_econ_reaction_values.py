from datetime import datetime, timezone

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
    import pytest
    with pytest.raises(RuntimeError):
        rv.write_set_tx(conn, set_id=set_id, values=vals, keys=[(event, "JPY")],
                        now=datetime.now(timezone.utc), checkpoint=fail)
    assert conn.execute("SELECT COUNT(*) FROM econ_reaction_values").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM econ_reaction_keys").fetchone()[0] == 0
