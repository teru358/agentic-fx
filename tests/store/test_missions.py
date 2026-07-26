import json
from datetime import datetime, timezone

from agentic_fx.store import missions
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


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
