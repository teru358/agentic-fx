import pytest
from datetime import datetime, timezone
from agentic_fx.store import alert_state
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 8, 11, tzinfo=timezone.utc)


def test_alert_state_roundtrip_single_known_key(tmp_path):
    c = connect(tmp_path / "a.db"); init_db(c)
    assert alert_state.get(c, alert_state.GATE_REJECT_STREAK_KEY) is None
    alert_state.set(c, alert_state.GATE_REJECT_STREAK_KEY, "7", now=NOW)
    assert alert_state.get(c, alert_state.GATE_REJECT_STREAK_KEY) == "7"


def test_alert_state_rejects_unknown_key(tmp_path):
    c = connect(tmp_path / "a.db"); init_db(c)
    with pytest.raises(ValueError, match="unknown alert_state key"):
        alert_state.set(c, "anything", "1", now=NOW)
