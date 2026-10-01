"""state が restricted の間の scheduler gate。

新規リスクを増やす経路 (cron / signal mission、指値の約定、signal の生成) は止まり、
既存建玉の保護 (SL/TP の判定) と news / econ は止まらない。
"""
from datetime import timedelta

from agentic_fx.core.contracts import Bar
from agentic_fx.store import orders

from tests.core.test_scheduler import FRI, WED, Env, _seed_decision_bar


def test_restricted_blocks_cron_mission_and_does_not_advance_the_cursor(tmp_path):
    state = {"v": "restricted"}
    env = Env(tmp_path, seed_cron_bar=False, state_fn=lambda: state["v"])
    _seed_decision_bar(env, WED - timedelta(hours=1))
    env.sched.tick(WED + timedelta(seconds=30))

    assert env.trade_calls == 0
    assert ("USDJPY", "1h") not in env.sched._cron_watermarks
    # 解除後の最初の tick で、止まっていた判断足の mission が 1 回だけ起動する
    state["v"] = "ready"
    env.sched.tick(WED + timedelta(minutes=1, seconds=30))
    assert env.trade_reasons == ["cron"]


def test_restricted_blocks_signal_mission(tmp_path):
    state = {"v": "restricted"}
    env = Env(tmp_path, seed_cron_bar=False, state_fn=lambda: state["v"],
              signal_due_fn=lambda now: True)
    env.sched.tick(WED)

    assert env.trade_calls == 0
    state["v"] = "ready"
    env.sched.tick(WED + timedelta(minutes=1))
    assert env.trade_reasons == ["signal"]


def test_restricted_blocks_limit_fill_but_not_the_stop_loss_of_an_open_position(tmp_path):
    state = {"v": "ready"}
    env = Env(tmp_path, seed_cron_bar=False, state_fn=lambda: state["v"])
    pending_oid = env.place_limit(price=148.20, sl=147.80, tp=149.00)
    open_oid = orders.insert(
        env.conn, pair="USDJPY", direction="long", entry_type="market",
        horizon="day", status="open", now=WED, quantity=0.1,
        avg_fill_price=148.20, stop_loss=147.80, take_profit=149.00,
        filled_at=WED.isoformat())
    state["v"] = "restricted"
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED + timedelta(minutes=1),
                             148.30, 148.35, 147.75, 147.90, 100)
    env.sched.tick(WED + timedelta(minutes=1))

    assert orders.get(env.conn, pending_oid)["status"] == "pending_fill"
    assert orders.get(env.conn, open_oid)["status"] == "closed"
    assert orders.get(env.conn, open_oid)["close_reason"] == "sl"


def test_restricted_blocks_signal_maintenance_but_not_news_and_econ(tmp_path):
    state = {"v": "restricted"}
    env = Env(tmp_path, seed_cron_bar=False, state_fn=lambda: state["v"],
              on_signal_maintenance=lambda now: None)
    env.sched.tick(WED)

    assert env.signal_maintenance_calls == 0
    assert env.news_calls == 1
    assert env.econ_calls == 1
    state["v"] = "ready"
    env.sched.tick(WED + timedelta(minutes=1))
    assert env.signal_maintenance_calls == 1


def test_restricted_keeps_the_cron_cursor_unadvanced_across_a_market_close(tmp_path):
    state = {"v": "restricted"}
    closed = FRI.replace(hour=22)
    env = Env(tmp_path, base=FRI, seed_cron_bar=False, state_fn=lambda: state["v"])
    _seed_decision_bar(env, FRI - timedelta(hours=1, seconds=30))

    env.sched.tick(closed)

    assert env.trade_calls == 0
    assert ("USDJPY", "1h") not in env.sched._cron_watermarks
