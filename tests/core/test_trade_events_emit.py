"""遷移 → 取引 event の写像と、observer / sink の変異で注文処理が止まらないこと
(AC-4〜9、AC-10b、AC-11〜14、AC-16、AC-19)。"""
from __future__ import annotations

import ast
import fcntl
import inspect
import json
import textwrap
import time
from datetime import timedelta

import pytest

from agentic_fx.core import transitions
from agentic_fx.core.contracts import Bar, Origin, TradeIntent
from agentic_fx.core.contracts import OrderStatus as S
from agentic_fx.ops import trade_event_sink as sink_mod
from agentic_fx.ops.trade_event_sink import TradeEventEmitter, TradeEventSink
from agentic_fx.store import missions, orders
from agentic_fx.store.db import connect
from agentic_fx.store.state import StateStore

from .test_executor import StubBroker
from .test_scheduler import Env, WED


class Rig:
    def __init__(self, tmp_path, *, state_mode="learning", sink=None, observer=True,
                 start=False):
        # worker は events() まで起こさない。主 thread の書き込みと競合して busy で捨てる
        # 揺れを避け、写像だけを決定的に確かめる (競合時の挙動は sink の test が持つ)。
        self.env = Env(tmp_path)
        self.db = tmp_path / "t.db"
        self.sink = sink or TradeEventSink(self.db, wall_clock=lambda: WED)
        if start:
            self.sink.start()
        self.token = None
        if observer:
            self.emitter = TradeEventEmitter(self.env.conn, self.sink,
                                             state_mode=state_mode, ccy="JPY")
            self.token = transitions.observe(self.env.conn, self.emitter)

    def mission(self, trigger="cron"):
        return missions.start(self.env.conn, "trade", "local", "m", WED, trigger)

    def open_intent(self, **over):
        d = {"action": "open", "pair": "USDJPY", "direction": "long",
             "entry_type": "limit", "horizon": "day", "limit_price": 148.20,
             "expires_in": "4h", "stop_loss": 147.80, "take_profit": 149.00,
             "reasoning": "t"}
        d.update(over)
        return TradeIntent.from_llm_dict(d, origin=Origin.SCHEDULER)

    def market(self, trigger="cron", **over):
        d = dict(entry_type="market", limit_price=None, expires_in=None,
                 stop_loss=148.00, take_profit=149.60)
        d.update(over)
        return self.env.executor.handle_intent(self.open_intent(**d), self.mission(trigger))

    def limit(self, trigger="cron", **over):
        return self.env.executor.handle_intent(self.open_intent(**over), self.mission(trigger))

    def events(self):
        if not self.sink.worker_alive():
            self.sink.start()
        self.sink.stop()
        if self.token is not None:
            transitions.unobserve(self.token)
            self.token = None
        conn = connect(self.db)
        try:
            return [{"id": r["id"], "code": r["code"], "ref": r["ref"],
                     "fields": json.loads(r["fields_json"])}
                    for r in conn.execute(
                        "SELECT * FROM ops_events WHERE code IN "
                        "('order_state_changed','position_closed') ORDER BY id")]
        finally:
            conn.close()

    def close(self):
        if self.sink.worker_alive():
            self.sink.stop()
        if self.token is not None:
            transitions.unobserve(self.token)
            self.token = None


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


def _states(events):
    return [(e["code"], e["fields"].get("state") or e["fields"].get("reason_code"))
            for e in events]


def test_ac4_market_open_then_sl_emits_filled_once_and_closed_once(rig):
    out = rig.market()
    oid = out["order_id"]
    rig.env.bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.20, 148.25, 147.90, 148.0, 100)
    rig.env.sched.tick(WED + timedelta(minutes=1))
    assert orders.get(rig.env.conn, oid)["close_reason"] == "sl"
    events = rig.events()
    assert _states(events) == [("order_state_changed", "filled"), ("position_closed", "sl")]
    assert [e["ref"] for e in events] == [f"order:{oid}"] * 2
    assert events[0]["id"] < events[1]["id"]
    filled, closed = events[0]["fields"], events[1]["fields"]
    row = orders.get(rig.env.conn, oid)
    assert filled["price"] == f"{row['avg_fill_price']:.8f}".rstrip("0").rstrip(".")
    assert filled["origin_code"] == "cron" and filled["mode"] == "paper"
    assert closed["ccy"] == "JPY" and closed["pnl"] is not None and closed["pnl"].startswith("-")
    assert closed["pnl_r"] is not None and closed["held_sec"] >= 0
    assert not any(e["fields"].get("state") == "placed" for e in events)


def test_ac5_limit_placed_filled_closed(rig):
    oid = rig.limit()["order_id"]
    rig.env.bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.30, 148.32, 148.18, 148.25, 100)
    rig.env.sched.tick(WED + timedelta(minutes=1))
    rig.env.bars["USDJPY"] = Bar("USDJPY", "1m", WED + timedelta(minutes=1),
                                 148.90, 149.10, 148.85, 149.05, 100)
    rig.env.sched.tick(WED + timedelta(minutes=2))
    assert orders.get(rig.env.conn, oid)["close_reason"] == "tp"
    events = rig.events()
    assert _states(events) == [("order_state_changed", "placed"),
                               ("order_state_changed", "filled"), ("position_closed", "tp")]
    placed = events[0]["fields"]
    assert placed["entry_type"] == "limit" and placed["price"] == "148.2"
    assert placed["sl"] == "147.8" and placed["tp"] == "149"
    assert placed["reason_code"] is None


def test_ac5_limit_cancel_emits_cancelled_with_reason(rig):
    oid = rig.limit()["order_id"]
    rig.env.executor.cancel_order(orders.get(rig.env.conn, oid), reason="llm_cancel")
    events = rig.events()
    assert _states(events) == [("order_state_changed", "placed"),
                               ("order_state_changed", "cancelled")]
    assert events[1]["fields"]["reason_code"] == "llm_cancel"


def test_ac5_limit_expiry_emits_expired(rig):
    oid = rig.limit(expires_in="1h")["order_id"]
    rig.env.sched.tick(WED + timedelta(hours=2))
    assert orders.get(rig.env.conn, oid)["status"] == "expired"
    events = rig.events()
    assert _states(events) == [("order_state_changed", "placed"),
                               ("order_state_changed", "expired")]
    assert events[1]["fields"]["reason_code"] is None


def _closing_order(env, **over):
    base = dict(pair="USDJPY", direction="long", entry_type="market", horizon="day",
                status="closing", now=WED, quantity=0.1, requested_price=148.2,
                stop_loss=147.8, avg_fill_price=148.2, filled_at=WED.isoformat(),
                close_reason="llm_close")
    base.update(over)
    return orders.insert(env.conn, **base)


def test_ac6_retry_close_and_finish_close_both_emit_position_closed(rig):
    # _retry_close 経由 (CLOSING のまま滞留していたものを次 tick の再試行で確定)
    retry_id = _closing_order(rig.env)
    rig.env.sched._retry_close(orders.get(rig.env.conn, retry_id), WED + timedelta(minutes=5))
    assert orders.get(rig.env.conn, retry_id)["status"] == "closed"
    # _finish_close 経由 (LLM の close intent)
    open_id = rig.market()["order_id"]
    close = TradeIntent.from_llm_dict({"action": "close", "order_id": open_id},
                                      origin=Origin.SCHEDULER)
    assert rig.env.executor.handle_intent(close, rig.mission())["result"] == "closed"
    events = rig.events()
    closed = [e for e in events if e["code"] == "position_closed"]
    assert sorted(e["ref"] for e in closed) == sorted([f"order:{retry_id}", f"order:{open_id}"])
    by_ref = {e["ref"]: e["fields"] for e in closed}
    assert by_ref[f"order:{open_id}"]["reason_code"] == "llm_close"
    assert by_ref[f"order:{retry_id}"]["held_sec"] == 300
    assert by_ref[f"order:{retry_id}"]["origin_code"] is None  # intent が無い


def test_ac7_cancel_fill_race_emits_a_single_filled(rig):
    oid = rig.limit()["order_id"]
    rig.env.executor.broker = StubBroker(cancel_status="rejected")
    rig.env.executor.cancel_order(orders.get(rig.env.conn, oid), reason="llm_cancel")
    assert orders.get(rig.env.conn, oid)["status"] == "protection_pending"
    rig.env.conn.execute("UPDATE orders SET avg_fill_price=148.2, filled_at=? WHERE id=?",
                         (WED.isoformat(), oid))
    rig.env.conn.commit()
    transitions.transition(rig.env.conn, oid, S.OPEN, WED)
    events = rig.events()
    assert _states(events) == [("order_state_changed", "placed"),
                               ("order_state_changed", "filled")]


def test_ac7_reconcile_resolutions_emit_rejected_and_cancelled(rig):
    base = dict(pair="USDJPY", direction="long", entry_type="limit", horizon="day", now=WED,
                quantity=0.1, requested_price=148.2, stop_loss=147.8)
    submit_unknown = orders.insert(rig.env.conn, status="submit_unknown", **base)
    cancel_unknown = orders.insert(rig.env.conn, status="cancel_unknown", **base)
    rig.env.sched.tick(WED)
    assert orders.get(rig.env.conn, submit_unknown)["status"] == "rejected"
    assert orders.get(rig.env.conn, cancel_unknown)["status"] == "cancelled"
    events = rig.events()
    got = {e["ref"]: e["fields"]["state"] for e in events}
    assert got == {f"order:{submit_unknown}": "rejected", f"order:{cancel_unknown}": "cancelled"}


def test_ac8_null_realized_pnl_still_emits_with_pnl_none(rig):
    oid = _closing_order(rig.env)
    transitions.transition(rig.env.conn, oid, S.CLOSED, WED + timedelta(seconds=90),
                           close_price=148.5, realized_pnl=None, close_reason="tp",
                           closed_at=(WED + timedelta(seconds=90)).isoformat())
    (event,) = rig.events()
    assert event["code"] == "position_closed" and event["fields"]["pnl"] is None
    assert event["fields"]["held_sec"] == 90 and event["fields"]["pnl_r"] == "0.75"


def test_r_denominator_zero_gives_none(rig):
    oid = _closing_order(rig.env, stop_loss=148.2)
    transitions.transition(rig.env.conn, oid, S.CLOSED, WED, close_price=148.5,
                           realized_pnl=10.0, close_reason="tp", closed_at=WED.isoformat())
    (event,) = rig.events()
    assert event["fields"]["pnl_r"] is None and event["fields"]["pnl"] == "10.00"


def test_negative_hold_time_is_zero(rig):
    oid = _closing_order(rig.env, filled_at=(WED + timedelta(hours=1)).isoformat())
    transitions.transition(rig.env.conn, oid, S.CLOSED, WED, close_price=148.5,
                           realized_pnl=-0.001, close_reason="sl", closed_at=WED.isoformat())
    (event,) = rig.events()
    assert event["fields"]["held_sec"] == 0 and event["fields"]["pnl"] == "0.00"


def test_ac9_gate_reject_hold_and_illegal_transition_emit_nothing(rig):
    out = rig.env.executor.handle_intent(
        rig.open_intent(stop_loss=148.19, limit_price=148.20), rig.mission())
    hold = TradeIntent.from_llm_dict({"action": "hold", "reasoning": "x"},
                                     origin=Origin.SCHEDULER)
    rig.env.executor.handle_intent(hold, rig.mission())
    oid = _closing_order(rig.env)
    transitions.transition(rig.env.conn, oid, S.CLOSED, WED, close_price=148.5,
                           realized_pnl=1.0, close_reason="tp", closed_at=WED.isoformat())
    with pytest.raises(transitions.IllegalTransition):
        transitions.transition(rig.env.conn, oid, S.CLOSED, WED)
    events = rig.events()
    assert out["order_id"] is None
    assert [e["code"] for e in events] == ["position_closed"]


def test_intermediate_states_emit_nothing(rig):
    oid = orders.insert(rig.env.conn, pair="USDJPY", direction="long", entry_type="limit",
                        horizon="day", status=S.SUBMITTING, now=WED, quantity=0.1,
                        requested_price=148.2, stop_loss=147.8)
    for to in (S.SUBMITTED, S.PROTECTION_PENDING):
        transitions.transition(rig.env.conn, oid, to, WED)
    assert rig.events() == []


def test_transition_table_every_destination_is_pinned():
    expected = {S.PENDING_FILL: "placed", S.OPEN: "filled", S.REJECTED: "rejected",
                S.CANCELLED: "cancelled", S.EXPIRED: "expired", S.CLOSED: "closed"}
    from agentic_fx.ops import trade_events as te
    for status in S:
        assert te.has_event(status.value) == (status in expected), status
    # transitions.ALLOWED の遷移先のうち event を持つものは上の 6 つだけ
    reachable = {to for tos in transitions.ALLOWED.values() for to in tos}
    assert {s for s in reachable if te.has_event(s.value)} == set(expected)


def test_ac10b_state_mode_follows_the_value_the_service_read(tmp_path):
    for mode in ("learning", "trading", None):
        sub = tmp_path / str(mode)
        sub.mkdir()
        rig = Rig(sub, state_mode=mode)
        try:
            rig.limit()
            oid = rig.market()["order_id"]
            rig.env.executor.close_order(orders.get(rig.env.conn, oid), 148.5, reason="llm_close")
            events = rig.events()
        finally:
            rig.close()
        assert {e["fields"]["state_mode"] for e in events} == {mode}
        assert len(events) == 3


def test_ac10b_observer_does_no_file_io_or_state_reads(rig, monkeypatch):
    oid = _closing_order(rig.env)
    state_path = rig.env.tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    holder = open(state_path, "rb")
    fcntl.flock(holder, fcntl.LOCK_EX)  # state の writer が lock を持ち続けている状態
    try:
        def forbid(*a, **k):
            raise AssertionError("observer touched the filesystem or state")

        monkeypatch.setattr(StateStore, "load", forbid)
        monkeypatch.setattr(fcntl, "flock", forbid)
        monkeypatch.setattr("builtins.open", forbid)
        started = time.monotonic()
        transitions.transition(rig.env.conn, oid, S.CLOSED, WED, close_price=148.5,
                               realized_pnl=1.0, close_reason="tp", closed_at=WED.isoformat())
        assert time.monotonic() - started < 2.0
        monkeypatch.undo()
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()
    assert [e["code"] for e in rig.events()] == ["position_closed"]


def test_ac10b_emitter_source_has_no_state_or_file_calls():
    source = textwrap.dedent(inspect.getsource(TradeEventEmitter))
    names = {n.attr for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Attribute)}
    names |= {n.id for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Name)}
    assert not names & {"load", "open", "flock", "StateStore", "read_text", "write_text", "Path"}


def test_ac11_failing_builder_counts_observer_error_and_order_proceeds(rig, monkeypatch):
    from agentic_fx.ops import trade_events as te

    def boom(*a, **k):
        raise RuntimeError("builder exploded")

    monkeypatch.setattr(te, "build_event", boom)
    out = rig.market()
    assert out["result"] == "opened"
    rig.env.bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.20, 148.25, 147.90, 148.0, 100)
    rig.env.sched.tick(WED + timedelta(minutes=1))
    assert orders.get(rig.env.conn, out["order_id"])["status"] == "closed"
    assert rig.sink.counters()["observer_error"] == 2
    assert rig.events() == []


def test_ac11_raising_registered_observer_does_not_change_sl_monitoring(tmp_path):
    rig = Rig(tmp_path, observer=False)
    token = transitions.observe(rig.env.conn, lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    try:
        out = rig.market()
        rig.env.bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.20, 148.25, 147.90, 148.0, 100)
        rig.env.sched.tick(WED + timedelta(minutes=1))
        assert orders.get(rig.env.conn, out["order_id"])["status"] == "closed"
    finally:
        transitions.unobserve(token)
        rig.close()


def test_invalid_value_drops_the_event_and_counts_it(rig):
    oid = _closing_order(rig.env)
    transitions.transition(rig.env.conn, oid, S.CLOSED, WED, close_price=float("nan"),
                           realized_pnl=1.0, close_reason="tp", closed_at=WED.isoformat())
    assert rig.events() == []
    assert rig.sink.counters()["invalid_value_drop"] == 1


def test_ac12_broken_ops_events_does_not_stop_orders(tmp_path):
    rig = Rig(tmp_path, start=True)
    rig.env.conn.execute("DROP TABLE ops_events")
    rig.env.conn.commit()
    out = rig.market()
    assert out["result"] == "opened"
    deadline = time.monotonic() + 5
    while rig.sink.counters()["db_error_drop"] < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert rig.sink.counters()["db_error_drop"] == 1
    assert rig.sink.health() == "degraded"
    rig.close()


def test_ac13_overflow_does_not_block_transition(tmp_path):
    sink = TradeEventSink(tmp_path / "t.db", wall_clock=lambda: WED, queue_max=1)
    rig = Rig(tmp_path, sink=sink)  # worker 未起動なので queue は詰まったまま
    out = rig.market()  # filled の 1 件で queue が満杯になる
    assert out["result"] == "opened"
    rig.limit()  # placed は溢れる
    rig.env.sched.tick(WED + timedelta(minutes=1))
    assert sink.counters()["queue_drop"] >= 1
    assert len(rig.events()) == 1


def test_ac14_stalled_sink_does_not_stall_transition(tmp_path):
    import threading
    release = threading.Event()
    sink = TradeEventSink(tmp_path / "t.db", wall_clock=lambda: WED, queue_max=50)
    real = sink._insert
    sink._insert = lambda *a, **k: (release.wait(10), real(*a, **k))[1]
    rig = Rig(tmp_path, sink=sink, start=True)
    try:
        started = time.monotonic()
        rig.market()
        rig.limit()
        assert time.monotonic() - started < 3.0
    finally:
        release.set()
        rig.close()


def test_ac14_other_writer_holds_the_lock_so_events_drop_but_orders_continue(tmp_path):
    rig = Rig(tmp_path, start=True)
    holder = connect(tmp_path / "t.db")
    holder.execute("BEGIN IMMEDIATE")
    try:
        # core の接続も書けないので、遷移は別接続が lock を放した後。ここでは sink 単体の挙動を見る
        rig.sink.submit("order_state_changed", "order:1",
                        {"state": "placed", "pair": "USDJPY", "side": "long",
                         "entry_type": "limit", "qty": "0.1", "price": "150.123", "sl": "149.8",
                         "tp": None, "origin_code": None, "mode": "paper",
                         "state_mode": None, "reason_code": None})
        deadline = time.monotonic() + 5
        while rig.sink.counters()["db_busy_drop"] < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert rig.sink.counters()["db_busy_drop"] == 1
    finally:
        holder.rollback()
        holder.close()
        rig.close()


def test_ac16_canary_strings_never_reach_events(rig):
    canary = "sk-ant-api03-SECRETKEY https://hooks.example.com/x 1234567890123 host.internal"
    rig.limit(reasoning=canary)
    rig.market(reasoning=canary)
    events = rig.events()
    assert events
    text = json.dumps(events)
    for needle in ("sk-ant", "SECRETKEY", "hooks.example", "1234567890123", "host.internal"):
        assert needle not in text


@pytest.mark.parametrize("trigger,expected", [
    ("cron", "cron"), ("signal:rsi_div", "signal:rsi_div"), ("signal:", None),
    ("Signal:x", None), ("free text here", None), ("signal:" + "a" * 60, None),
    ("signal:" + "a" * 57, "signal:" + "a" * 57), (None, None)])
def test_origin_code_is_rechecked_at_the_boundary(tmp_path, trigger, expected):
    rig = Rig(tmp_path)
    try:
        rig.limit(trigger=trigger)
        (event,) = rig.events()
    finally:
        rig.close()
    assert event["fields"]["origin_code"] == expected


def _orders_dump(env):
    return [dict(r) for r in env.conn.execute("SELECT * FROM orders ORDER BY id")]


def _scenario(rig):
    rig.limit()
    out = rig.market()
    rig.env.bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.30, 148.35, 147.90, 148.25, 100)
    rig.env.sched.tick(WED + timedelta(minutes=1))
    rig.env.sched.tick(WED + timedelta(minutes=2))
    return out


def test_ac19_orders_columns_are_identical_with_and_without_observer(tmp_path):
    a = tmp_path / "with"
    b = tmp_path / "without"
    a.mkdir()
    b.mkdir()
    with_rig, without_rig = Rig(a), Rig(b, observer=False)
    try:
        _scenario(with_rig)
        _scenario(without_rig)
        assert _orders_dump(with_rig.env) == _orders_dump(without_rig.env)
    finally:
        with_rig.close()
        without_rig.close()
    tables = lambda env: {  # noqa: E731
        r[0] for r in env.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables(with_rig.env) == tables(without_rig.env)


def test_ac19_sink_only_writes_ops_events():
    tree = ast.parse(inspect.getsource(sink_mod))
    statements = [n.value for n in ast.walk(tree)
                  if isinstance(n, ast.Constant) and isinstance(n.value, str)
                  and n.value.lstrip().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "DROP"))]
    assert statements and all("ops_events" in text for text in statements), statements


def test_filled_price_is_the_fill_price_not_the_requested_price(rig):
    oid = rig.limit()["order_id"]
    rig.env.conn.execute("UPDATE orders SET requested_price=148.2 WHERE id=?", (oid,))
    transitions.transition(rig.env.conn, oid, S.PROTECTION_PENDING, WED,
                           avg_fill_price=148.15, filled_at=WED.isoformat())
    transitions.transition(rig.env.conn, oid, S.OPEN, WED)
    placed, filled = rig.events()
    assert placed["fields"]["price"] == "148.2" and filled["fields"]["price"] == "148.15"


def test_r_multiple_exact_values(rig):
    cases = [("long", 148.0, 147.8, 148.4, "2.00"), ("short", 148.0, 148.2, 147.6, "2.00"),
             ("short", 148.0, 148.2, 148.2, "-1.00"), ("long", 148.0, 147.5, 147.5, "-1.00")]
    for side, entry, sl, close, expected in cases:
        oid = _closing_order(rig.env, direction=side, avg_fill_price=entry, stop_loss=sl)
        transitions.transition(rig.env.conn, oid, S.CLOSED, WED, close_price=close,
                               realized_pnl=1.0, closed_at=WED.isoformat())
    got = [e["fields"]["pnl_r"] for e in rig.events()]
    assert got == [c[4] for c in cases]


def test_commit_stalled_sink_delays_but_does_not_fail_the_next_core_write(tmp_path):
    """sink が INSERT の後・commit の前で止まっている間、core の書き込みはその分だけ待つ
    (受容した残余リスク)。待ちが busy_timeout に達しなければ失敗せず、注文処理は完了する。"""
    import threading
    sink = TradeEventSink(tmp_path / "t.db", wall_clock=lambda: WED)
    stalled = threading.Event()
    release = threading.Event()

    def slow_insert(ts, code, ref, fields):
        import json as _json
        conn = sink.connection
        conn.execute("INSERT INTO ops_events(ts,code,ref,fields_json) VALUES (?,?,?,?)",
                     (ts, code, ref, _json.dumps(fields)))
        stalled.set()
        release.wait(10)
        conn.commit()

    sink._insert = slow_insert
    rig = Rig(tmp_path, sink=sink, start=True)
    oid = _closing_order(rig.env)
    transitions.transition(rig.env.conn, oid, S.CLOSED, WED, close_price=148.5,
                           realized_pnl=1.0, close_reason="tp", closed_at=WED.isoformat())
    assert stalled.wait(5)
    timer = threading.Timer(0.3, release.set)
    timer.start()
    started = time.monotonic()
    other = _closing_order(rig.env)  # sink が lock を持つ間の core の書き込み
    waited = time.monotonic() - started
    timer.join()
    assert orders.get(rig.env.conn, other)["status"] == "closing"
    assert 0.1 < waited < 4.0
    rig.close()
