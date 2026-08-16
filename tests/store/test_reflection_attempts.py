from datetime import datetime, timezone

from agentic_fx.store import reflection_attempts
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


def _conn(tmp_path):
    c = connect(tmp_path / "a.db")
    init_db(c)
    return c


def test_bump_returns_incremented_attempt_count(tmp_path):
    c = _conn(tmp_path)
    c.execute("INSERT INTO orders (pair,direction,entry_type,horizon,status,"
              "created_at,updated_at) VALUES ('USDJPY','long','market','day',"
              "'closed',?,?)", (NOW.isoformat(), NOW.isoformat()))
    oid = c.execute("SELECT id FROM orders").fetchone()[0]
    assert reflection_attempts.bump(c, oid, now=NOW, reason="boom") == 1
    assert reflection_attempts.bump(c, oid, now=NOW, reason="boom2") == 2


def test_clear_deletes_attempt_row(tmp_path):
    c = _conn(tmp_path)
    c.execute("INSERT INTO orders (pair,direction,entry_type,horizon,status,"
              "created_at,updated_at) VALUES ('USDJPY','long','market','day',"
              "'closed',?,?)", (NOW.isoformat(), NOW.isoformat()))
    oid = c.execute("SELECT id FROM orders").fetchone()[0]
    reflection_attempts.bump(c, oid, now=NOW, reason=None)
    reflection_attempts.clear(c, oid)
    assert reflection_attempts.attempts_of(c, oid) == 0
