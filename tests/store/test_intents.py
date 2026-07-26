from datetime import datetime, timezone

from agentic_fx.store import intents, missions
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def test_insert_and_gate_result(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    mid = missions.start(c, "trade", "local", "qwen3.6-35b", NOW)
    iid = intents.insert(c, mid, {"action": "hold", "reasoning": "様子見"}, NOW)
    intents.set_gate_result(c, iid, accepted=False, reject_reason="RR below 1.5")
    row = intents.get(c, iid)
    assert row["gate_result"] == "rejected"
    assert row["reject_reason"] == "RR below 1.5"
    assert '"hold"' in row["payload_json"]
