import json
from datetime import datetime, timezone
from pathlib import Path

from agentic_fx.config import load_settings
from agentic_fx.store import missions
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


def test_start_finish_recent(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    mid = missions.start(c, "trade", "local", "qwen3.6-35b", NOW)
    missions.finish(c, mid, "completed", {"action": "hold"},
                    [{"role": "assistant", "content": "..."}], NOW)
    rows = missions.recent(c, 5)
    assert len(rows) == 1
    assert rows[0]["status"] == "completed"
    assert json.loads(rows[0]["output_json"]) == {"action": "hold"}
    assert json.loads(rows[0]["transcript_json"])[0]["role"] == "assistant"


def test_system_filtered_signal_mission_does_not_consume_rate_limit(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    mid = missions.start(c, "trade", "local", "m", NOW, trigger="signal:p")
    missions.finish_system_filtered_in_tx(c, mid, None, [], NOW)
    c.commit()
    assert missions.signals_rate_ok(c, NOW, SETTINGS)


def test_system_filtered_is_written_only_by_its_own_terminal_helper(tmp_path):
    import pytest
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    now = datetime(2026, 10, 30, 13, 0, tzinfo=timezone.utc)
    mid = missions.start(conn, "trade", "local", "m", now, "signal:p")
    for fn in (
            lambda: missions.finish(conn, mid, "system_filtered", None, [], now),
            lambda: missions.finish_in_tx(
                conn, mid, "system_filtered", None, [], now)):
        with pytest.raises(ValueError):
            fn()
    assert conn.execute("SELECT status FROM missions WHERE id=?", (mid,)
                        ).fetchone()["status"] == "running"
    assert missions.finish_system_filtered_in_tx(conn, mid, None, [], now) is True
    conn.rollback()
    assert conn.execute("SELECT status FROM missions WHERE id=?", (mid,)
                        ).fetchone()["status"] == "running"
    assert missions.finish_system_filtered_in_tx(conn, mid, None, [], now) is True
    conn.commit()
    row = conn.execute("SELECT status, finished_at FROM missions WHERE id=?",
                       (mid,)).fetchone()
    assert (row["status"], row["finished_at"]) == ("system_filtered", now.isoformat())
    assert missions.finish_system_filtered_in_tx(conn, mid, None, [], now) is False
