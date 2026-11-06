"""取引 event の origin は intent を作った mission の trigger から読む。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agentic_fx.ops import trade_events
from agentic_fx.ops.trade_event_sink import TradeEventEmitter
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 10, 30, 13, 0, tzinfo=timezone.utc)


class _Sink:
    def note_drop(self, *a, **k):
        pass


def _emitter(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn, TradeEventEmitter(conn, _Sink(), state_mode=None, ccy="JPY")


def _intent(conn, trigger):
    mid = conn.execute(
        "INSERT INTO missions (loop,runner,model,status,started_at,trigger) "
        "VALUES ('trade','local','m','completed',?,?)", (NOW.isoformat(), trigger)
    ).lastrowid
    iid = conn.execute(
        "INSERT INTO trade_intents (mission_id,payload_json,action,created_at) "
        "VALUES (?,'{}','open',?)", (mid, NOW.isoformat())).lastrowid
    conn.commit()
    return iid


@pytest.mark.parametrize("trigger,origin", [
    ("cron", "cron"), ("signal:rsi_div", "signal:rsi_div"), (None, None)])
def test_origin_follows_the_mission_trigger_of_the_intent(tmp_path, trigger, origin):
    conn, emitter = _emitter(tmp_path)
    row = {"intent_id": _intent(conn, trigger)}
    assert trade_events.origin_code(emitter._trigger(row)) == origin


def test_a_row_without_an_intent_has_no_origin(tmp_path):
    _, emitter = _emitter(tmp_path)
    assert emitter._trigger({"intent_id": None}) is None
