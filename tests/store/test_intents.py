from datetime import datetime, timezone

from agentic_fx.store import intents, missions
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def test_insert_and_gate_result(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    mid = missions.start(c, "trade", "local", "qwen3.6-35b", NOW)
    iid = intents.insert(c, mid, {"action": "hold", "reasoning": "様子見"},
                         NOW, action="hold")
    intents.set_gate_result(c, iid, accepted=False, reject_reason="RR below 1.5",
                            reject_category="mission")
    row = intents.get(c, iid)
    assert row["gate_result"] == "rejected"
    assert row["reject_reason"] == "RR below 1.5"
    assert '"hold"' in row["payload_json"]


def test_insert_requires_explicit_action_and_persists_it(tmp_path):
    c = connect(tmp_path / "t.db"); init_db(c)
    mid = missions.start(c, "trade", "local", "m", NOW)
    iid = intents.insert(c, mid, {"opaque": True}, NOW, action="open")
    assert intents.get(c, iid)["action"] == "open"


def test_set_gate_result_requires_category_and_persists_it(tmp_path):
    c = connect(tmp_path / "t.db"); init_db(c)
    mid = missions.start(c, "trade", "local", "m", NOW)
    iid = intents.insert(c, mid, {}, NOW, action="open")
    intents.set_gate_result(c, iid, accepted=False,
                            reject_reason="RR", reject_category="risk_gate")
    assert intents.get(c, iid)["reject_category"] == "risk_gate"
