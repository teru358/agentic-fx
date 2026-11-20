"""前方成績の test が使う、signal・採否・shadow の組み立て。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from agentic_fx.store import signal_decisions as sd
from agentic_fx.store import signal_forward as sf
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.signal_shadow import ShadowSpread

UTC = timezone.utc
NOW = datetime(2027, 1, 31, 12, 0, tzinfo=UTC)
WEEK = timedelta(weeks=1)
SPREAD = ShadowSpread(pip_size=0.01, spread_pips=1.0, source="assumed")
HASH = "h" * 16


def open_db(path):
    c = connect(path)
    init_db(c)
    return c


def target(started=None, *, name="strat", content_hash=HASH, reason=None, generation=(0, 1)):
    started = NOW - 40 * WEEK if started is None else started
    if reason is not None:
        return sf.ForwardTarget(name, content_hash, None, None, reason)
    return sf.ForwardTarget(name, content_hash, started, generation, None)


class Maker:
    def __init__(self, conn):
        self.conn = conn
        self.n = 0
        self.mission = self.new_mission()

    def new_mission(self):
        mission = self.conn.execute(
            "INSERT INTO missions (loop,runner,model,status,started_at) "
            "VALUES ('trade','local','m','running',?)", (NOW.isoformat(),)).lastrowid
        self.conn.commit()
        return mission

    def signal(self, *, at, plugin="strat", content_hash=HASH, status="claimed",
               reason=None, stop_loss=149.0, payload=None, created_text=None,
               contract="executable_v1"):
        self.n += 1
        body = payload if payload is not None else json.dumps(
            {"action": "open", "direction": "long", "entry_type": "market",
             "limit_price": None, "stop_loss": stop_loss, "take_profit": 152.0})
        abandoned_at = (at.isoformat() if status == "abandoned"
                        and reason != "legacy_unknown" else None)
        return self.conn.execute(
            "INSERT INTO signals (plugin,content_hash,pair,timeframe,bar_ts,kind,payload_json,"
            "status,claimed_by_mission_id,claimed_at,created_at,contract_version,"
            "abandon_reason,abandoned_at) VALUES (?,?,'USDJPY','1h',?,'strategy',?,?,?,?,?,"
            "?,?,?)",
            (plugin, content_hash, f"2027-01-01T00:{self.n:02d}:00+00:00", body, status,
             self.mission if status == "claimed" else None,
             at.isoformat() if status == "claimed" else None,
             created_text or at.isoformat(), contract, reason, abandoned_at)).lastrowid

    def decide(self, *, at, accept=True, plugin="strat", content_hash=HASH, result="tp",
               pnl=50.0, entry=150.0, stop_loss=149.0, gate="accepted", payload=None,
               signal_at=None, signal_created_text=None, state=None):
        self.mission = self.new_mission()
        sid = self.signal(at=signal_at or at, plugin=plugin, content_hash=content_hash,
                          stop_loss=stop_loss, payload=payload,
                          created_text=signal_created_text)
        self.conn.commit()
        if accept:
            done = sd.commit_accept(self.conn, mission_id=self.mission, signal_id=sid,
                                    intent_payload={}, spread=SPREAD, output=None,
                                    transcript=[], now=at)
            self.conn.execute(
                "UPDATE trade_intents SET gate_result=?, reject_category=? WHERE id=?",
                (gate, "risk_gate" if gate == "rejected" else None, done.trade_intent_id))
        else:
            done = sd.commit_reject(self.conn, mission_id=self.mission, signal_id=sid,
                                    actor="system", reject_code="econ_window", spread=SPREAD,
                                    output=None, transcript=[], now=at)
        shadow_id = done.shadow_id
        self.finish(shadow_id, result=result, pnl=pnl, entry=entry, at=at, state=state)
        self.conn.commit()
        return shadow_id

    def finish(self, shadow_id, *, result, pnl, entry, at, state=None):
        stamp = (at + timedelta(hours=1)).isoformat()
        if result in ("sl", "tp", "day_close"):
            self.conn.execute(
                "UPDATE signal_shadow SET state='resolved', result_code=?, entry_price=?, "
                "entry_at=?, exit_price=?, pnl_pips=?, resolved_at=? WHERE id=?",
                (result, entry, stamp, entry + pnl * 0.01, pnl, stamp, shadow_id))
        elif result == "data_insufficient":
            self.conn.execute("UPDATE signal_shadow SET state='resolved', result_code=? "
                              "WHERE id=?", (result, shadow_id))
        elif result == "no_fill":
            self.conn.execute("UPDATE signal_shadow SET state='expired', result_code=? "
                              "WHERE id=?", (result, shadow_id))
        elif result == "filled":
            self.conn.execute("UPDATE signal_shadow SET state='filled', entry_price=?, "
                              "entry_at=? WHERE id=?", (entry, stamp, shadow_id))
