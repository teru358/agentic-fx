"""AC-17 / AC-18: 取引 event の導入が、LLM に渡る既存の入力を 1 byte も変えない。

`_performance_report` の canonical JSON、`build_state_summary`、reflection prompt を、固定 fixture・
固定時刻・固定 settings で golden (UTF-8 の byte) と比べる。golden は event の無い版で作ってあり、
observer と sink を有効にして遷移した後の入力も同じ byte になる。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.config import load_settings
from agentic_fx.core import transitions
from agentic_fx.core.contracts import FixedClock
from agentic_fx.core.contracts import OrderStatus as S
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.loops.improve_context import _performance_report
from agentic_fx.loops.reflection_cycle import ReflectionCycle
from agentic_fx.loops.summary import build_state_summary
from agentic_fx.runners.base import MissionResult
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.store import missions, orders
from agentic_fx.store.db import connect, init_db

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "fixtures" / "llm_inputs"
NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(ROOT / "config" / "settings.yaml.example")


def _build(tmp_path: Path, *, observed: bool):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    sink = token = None
    if observed:
        from agentic_fx.ops.trade_event_sink import TradeEventEmitter, TradeEventSink
        sink = TradeEventSink(tmp_path / "t.db", wall_clock=lambda: NOW)  # 書き込みは停止時にまとめて
        token = transitions.observe(conn, TradeEventEmitter(
            conn, sink, state_mode="learning", ccy="JPY"))
    base = dict(horizon="day", now=NOW)
    pending = orders.insert(conn, pair="USDJPY", direction="long", entry_type="limit",
                            status=S.SUBMITTED, quantity=0.1, requested_price=148.2,
                            stop_loss=147.8, take_profit=149.0, **{**base, "horizon": "swing"})
    transitions.transition(conn, pending, S.PENDING_FILL, NOW)
    rows = [("USDJPY", "long", 148.5, 149.0, 2500.0, "tp", 9),
            ("USDJPY", "short", 149.0, 149.4, -2000.0, "sl", 13),
            ("EURUSD", "long", 1.0850, 1.0830, -1800.5, "sl", 13),
            ("GBPJPY", "short", 190.12, 189.50, 3100.25, "llm_close", 21)]
    first_closed = None
    for i, (pair, side, entry, close, pnl, reason, hour) in enumerate(rows):
        created = NOW - timedelta(days=i + 1, hours=NOW.hour - hour)
        oid = orders.insert(conn, pair=pair, direction=side, entry_type="market",
                            horizon="day", status=S.CLOSING, now=created, quantity=0.1,
                            requested_price=entry, avg_fill_price=entry, stop_loss=entry - 0.5,
                            filled_at=created.isoformat(), close_reason=reason)
        transitions.transition(conn, oid, S.CLOSED, created + timedelta(minutes=30),
                               close_price=close, realized_pnl=pnl,
                               closed_at=(created + timedelta(minutes=30)).isoformat())
        first_closed = first_closed or oid
    mid = missions.start(conn, "trade", "local", "m", NOW, "cron")
    conn.execute(
        "INSERT INTO trade_intents(mission_id, payload_json, action, gate_result, "
        "reject_category, created_at) VALUES (?,'{}','hold',NULL,NULL,?)",
        (mid, (NOW - timedelta(days=1)).isoformat()))
    conn.commit()
    return conn, sink, token, first_closed


def _finish(sink, token):
    if token is not None:
        transitions.unobserve(token)
    if sink is not None:
        sink.start()
        sink.stop()


def _inputs(tmp_path: Path, *, observed: bool) -> dict[str, bytes]:
    conn, sink, token, closed_id = _build(tmp_path, observed=observed)
    try:
        report = json.dumps(_performance_report(conn, NOW), sort_keys=True,
                            ensure_ascii=False, separators=(",", ":"))
        econ = MagicMock()
        econ.upcoming.return_value = [{"ts": "2026-07-22T19:30:00+00:00", "country": "USD",
                                       "name": "CPI", "importance": 3}]
        summary = build_state_summary(conn, PaperBroker(conn, SETTINGS, FixedClock(NOW)), econ,
                                      FixedClock(NOW), SETTINGS.paper.starting_balance)
        runner = FakeRunner([MissionResult("completed", {"content": "x"}, [])])
        cycle = ReflectionCycle(conn=conn, runner=runner, rag=MagicMock(), settings=SETTINGS,
                                activity=ActivityLog(tmp_path / "a.log"), clock=FixedClock(NOW),
                                core_lock=threading.RLock())
        cycle.run_pending(max_items=1)
        prompt = runner.missions[0].prompt
    finally:
        _finish(sink, token)
        conn.close()
    return {"performance_report.json": report.encode("utf-8"),
            "state_summary.txt": summary.encode("utf-8"),
            "reflection_prompt.txt": prompt.encode("utf-8")}


@pytest.mark.parametrize("observed", [False, True])
@pytest.mark.parametrize("name", ["performance_report.json", "state_summary.txt",
                                  "reflection_prompt.txt"])
def test_ac18_llm_inputs_match_golden_bytes(tmp_path, name, observed):
    got = _inputs(tmp_path, observed=observed)[name]
    if os.environ.get("AFX_REGEN_GOLDEN") == "1" and not observed:
        GOLDEN.mkdir(parents=True, exist_ok=True)
        (GOLDEN / name).write_bytes(got)
    assert got == (GOLDEN / name).read_bytes()


def test_ac18_observed_run_really_wrote_events(tmp_path):
    conn, sink, token, _ = _build(tmp_path, observed=True)
    _finish(sink, token)
    codes = [r[0] for r in conn.execute("SELECT code FROM ops_events ORDER BY id")]
    conn.close()
    assert codes.count("position_closed") == 4 and codes.count("order_state_changed") == 1
