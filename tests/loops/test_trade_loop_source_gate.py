"""Claim 済み signal を使う OPEN の source gate 回帰テスト。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch

from agentic_fx.core.contracts import FixedClock
from agentic_fx.core.executor import snapshot_age_exceeded
from agentic_fx.plugin import switch
from agentic_fx.runners.base import MissionResult
from agentic_fx.store import signals

from tests.loops.test_trade_loop import NOW, SETTINGS, _loop


OPEN = {
    "action": "open", "pair": "USDJPY", "direction": "long",
    "entry_type": "market", "horizon": "day", "stop_loss": 147.0,
    "take_profit": 149.0, "reasoning": "test",
}


def _add(conn, *, bar_ts=NOW):
    return signals.add(
        conn, plugin="strategy", content_hash="a" * 64, pair="USDJPY",
        timeframe="1h", bar_ts=bar_ts.isoformat(), kind="strategy",
        payload={"direction": "long"}, now=NOW)


def _last_intent(conn):
    return conn.execute(
        "SELECT gate_result,reject_reason,reject_category FROM trade_intents "
        "ORDER BY id DESC LIMIT 1").fetchone()


def test_open_abandons_claim_when_source_becomes_inactive(tmp_path, monkeypatch):
    """source 再検証を外す変異は、無効化済み signal で OPEN してしまう。"""
    conn, loop, _, _ = _loop(tmp_path, [MissionResult("completed", OPEN, [])])
    _add(conn)
    loop._plugins_root = tmp_path / "plugins"
    monkeypatch.setattr(
        "agentic_fx.loops.trade_loop.signal_source_active", lambda *_args: False)

    out = loop.run_once("signal")

    assert out == {"result": "rejected", "order_id": None,
                   "reasons": ["signal_source_inactive"]}
    assert conn.execute("SELECT status FROM signals").fetchone()[0] == "abandoned"
    assert dict(_last_intent(conn)) == {
        "gate_result": "rejected", "reject_reason": "signal_source_inactive",
        "reject_category": "signal",
    }


def test_open_requeues_claim_when_source_name_lock_times_out(tmp_path, monkeypatch):
    """source lock timeout は consume せず、既存の requeue 経路へ渡す。"""
    conn, loop, _, _ = _loop(tmp_path, [MissionResult("completed", OPEN, [])])
    sid = _add(conn)
    loop._plugins_root = tmp_path / "plugins"

    @contextmanager
    def busy(*_args, **_kwargs):
        raise switch.PluginBusyError("held")
        yield

    monkeypatch.setattr(switch, "_plugin_locks", busy)
    out = loop.run_once("signal")

    assert out["reasons"] == ["signal_source_lock_timeout"]
    row = conn.execute(
        "SELECT status,requeue_count FROM signals WHERE id=?", (sid,)).fetchone()
    assert dict(row) == {"status": "pending", "requeue_count": 1}


def test_signal_staleness_wins_over_snapshot_staleness(tmp_path):
    """同時 stale 時は signal を abandoned にし、snapshot 側へは進まない。"""
    conn, loop, _, _ = _loop(tmp_path, [])
    sid = _add(conn)

    class AdvancingRunner:
        def run(self, _mission):
            late = NOW + timedelta(hours=3)
            loop.clock = FixedClock(late)
            loop.executor.clock = FixedClock(late)
            return MissionResult("completed", OPEN, [])

    loop.runner = AdvancingRunner()
    out = loop.run_once("signal")

    assert out["reasons"] == ["signal stale"]
    assert conn.execute("SELECT status FROM signals WHERE id=?", (sid,)).fetchone()[0] == "abandoned"
    assert _last_intent(conn)["reject_category"] == "signal"


def test_both_staleness_predicates_are_evaluated_then_signal_wins(tmp_path):
    """両方 stale のとき両述語を評価し、優先順位は signal stale が先。"""
    conn, loop, _, _ = _loop(tmp_path, [MissionResult("completed", OPEN, [])])
    sid = _add(conn)
    real_gather = loop.executor.gather_open_snapshot

    def gather_then_advance(*args, **kwargs):
        snapshot = real_gather(*args, **kwargs)
        late = NOW + timedelta(hours=3)
        loop.clock = FixedClock(late)
        loop.executor.clock = FixedClock(late)
        return snapshot

    loop.executor.gather_open_snapshot = gather_then_advance
    with patch("agentic_fx.loops.trade_loop.snapshot_age_exceeded",
               wraps=snapshot_age_exceeded) as spy:
        out = loop.run_once("signal")

    assert spy.call_count == 1
    assert out["reasons"] == ["signal stale"]
    row = conn.execute(
        "SELECT status,requeue_count FROM signals WHERE id=?", (sid,)).fetchone()
    assert dict(row) == {"status": "abandoned", "requeue_count": 0}


def _plain_indicator_world(tmp_path):
    """plain の indicator ディレクトリだけが live にある plugins root。"""
    from types import SimpleNamespace
    root = tmp_path / "plugins"
    (root / "legacy_ind").mkdir(parents=True)
    resolved = SimpleNamespace(items=(SimpleNamespace(
        plugin_name="legacy_ind", content_hash="f" * 64),))
    return root, resolved


def _approve_strategy(conn):
    import json
    conn.execute(
        "INSERT INTO approval_requests (kind,status,payload_json,decided_at,"
        "created_at) VALUES ('plugin','approved',?,?,?)",
        (json.dumps({"name": "strategy", "content_hash": "a" * 64}),
         NOW.isoformat(), NOW.isoformat()))
    conn.commit()


def test_open_gate_accepts_strategy_depending_on_plain_indicator(tmp_path):
    """plain の indicator は存在だけを見る (hash / 最新決定は照合しない)。"""
    from agentic_fx.loops.trade_loop import signal_source_active
    from agentic_fx.plugin import signal_producer
    conn, _loop_obj, _, _ = _loop(tmp_path, [])
    root, resolved = _plain_indicator_world(tmp_path)
    live = type("Id", (), {"kind": "plain", "content_hash": "0" * 64,
                           "artifact_hash": None})
    strategy = type("Id", (), {"kind": "plain", "content_hash": "a" * 64,
                               "artifact_hash": None})
    _approve_strategy(conn)

    def identity(_root, name):
        return strategy if name == "strategy" else live

    with patch.object(signal_producer.plugin_deploy_ops, "live_identity", identity):
        assert signal_source_active(
            conn, root, "strategy", "a" * 64, resolved) is True
        assert signal_producer._live_source_reason(
            conn, root, type("M", (), {"name": "strategy",
                                       "content_hash": "a" * 64,
                                       "artifact_hash": None})(),
            resolved, {"strategy", "legacy_ind"}) is None


def test_symlink_indicator_hash_mismatch_is_not_live(tmp_path):
    from agentic_fx.loops.trade_loop import signal_source_active
    from agentic_fx.plugin import signal_producer
    conn, _loop_obj, _, _ = _loop(tmp_path, [])
    root, resolved = _plain_indicator_world(tmp_path)
    strategy = type("Id", (), {"kind": "plain", "content_hash": "a" * 64,
                               "artifact_hash": None})
    linked = type("Id", (), {"kind": "symlink", "content_hash": "0" * 64,
                             "artifact_hash": "x"})
    _approve_strategy(conn)

    def identity(_root, name):
        return strategy if name == "strategy" else linked

    with patch.object(signal_producer.plugin_deploy_ops, "live_identity", identity):
        assert signal_source_active(
            conn, root, "strategy", "a" * 64, resolved) is False


def test_snapshot_age_helper_has_shared_strict_boundary(tmp_path):
    """OPEN gate と executor が同じ helper を使い、等号は許可する。"""
    conn, loop, _, _ = _loop(tmp_path, [])
    snapshot = loop.executor.gather_open_snapshot(
        type("Intent", (), {"pair": "USDJPY"})(), exposure_pairs=[])
    assert snapshot_age_exceeded(snapshot, NOW + timedelta(seconds=10), 10) is None
    assert "execution snapshot is stale" in snapshot_age_exceeded(
        snapshot, NOW + timedelta(seconds=10, microseconds=1), 10)


def test_snapshot_staleness_requeues_before_consume(tmp_path):
    """snapshot helper を consume の後ろへ移す変異は signal を消費してしまう。"""
    conn, loop, _, _ = _loop(tmp_path, [MissionResult("completed", OPEN, [])])
    sid = _add(conn)
    real_gather = loop.executor.gather_open_snapshot

    def gather_then_advance(*args, **kwargs):
        snapshot = real_gather(*args, **kwargs)
        late = NOW + timedelta(seconds=31)
        loop.clock = FixedClock(late)
        loop.executor.clock = FixedClock(late)
        return snapshot

    loop.executor.gather_open_snapshot = gather_then_advance
    out = loop.run_once("signal")

    assert "execution snapshot is stale" in out["reasons"][0]
    row = conn.execute(
        "SELECT status,requeue_count FROM signals WHERE id=?", (sid,)).fetchone()
    assert dict(row) == {"status": "pending", "requeue_count": 1}


def test_hold_never_acquires_plugin_name_lock(tmp_path, monkeypatch):
    """HOLD は source gate の対象外で、plugin lock を取らない。"""
    conn, loop, _, _ = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "wait"}, [])])
    _add(conn)
    loop._plugins_root = tmp_path / "plugins"

    with patch("agentic_fx.loops.trade_loop.switch._plugin_locks") as locks:
        out = loop.run_once("signal")

    assert out["result"] == "hold"
    locks.assert_not_called()
