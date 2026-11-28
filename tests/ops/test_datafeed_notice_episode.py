"""state machine が書いた記録を、同じ DB の ops sweep が順に event にする (配線の通し)。

machine と ops は別の接続。時刻は水曜の開場中の実時刻の例で、期待値は設計の表から手で書いた。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from agentic_fx.datafeed.outage import OutageStateMachine
from agentic_fx.ops.events import EventStore
from agentic_fx.store import alert_state

from tests.datafeed.test_outage_notices import KEY, WIDTHS, _open_position, _tick

UTC = timezone.utc
START = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)


def _machine(env, **kw):
    conn = env.connect()
    machine = OutageStateMachine(conn, hard_keys=frozenset({KEY}), interval_widths=WIDTHS,
                                 grace=timedelta(seconds=30), storage_source="mt5-live", **kw)
    return conn, machine


def _start_stop(conn, machine, now):
    """建玉ありの停止中 (degraded、epoch 1) を直接用意する。停止に至る観測の経路は遷移表の
    test が確かめるので、ここは記録から event までの配線に絞る。"""
    machine._ensure_row(now - timedelta(minutes=1))
    _tick(conn, machine, now - timedelta(minutes=3))
    conn.execute(
        "UPDATE datafeed_outage_state SET state='degraded', epoch=1, entered_degraded_at=?, "
        "pending_human_confirmation=1, updated_at=? WHERE id=1", (now.isoformat(), now.isoformat()))
    conn.execute(
        "INSERT INTO datafeed_outage_gap (pair, interval, epoch, gap_start, replay_through, "
        "source) VALUES ('USDJPY', '1m', 1, ?, NULL, 'mt5-live')",
        ((now - timedelta(minutes=2)).isoformat(),))
    conn.commit()
    machine._reset_ready_streak = False


def _guard_events(conn):
    out = []
    for row in conn.execute("SELECT fields_json FROM ops_events ORDER BY id"):
        fields = json.loads(row["fields_json"])
        out.append((fields["state"], fields["generation"]))
    return out


def test_an_episode_reaches_discord_as_stop_wait_rejection_acceptance_and_resumption(ops_env):
    ops_env.wall.now = START
    service = ops_env.service(datafeed_notices=True)
    service.emit_pending_notices(improve_enabled=False)   # 起動: 有効化の時刻が置かれる
    conn, machine = _machine(ops_env)
    ops_env.wall.now = START + timedelta(hours=1)
    t0 = START + timedelta(hours=1)
    _open_position(conn, t0 - timedelta(minutes=30))
    _start_stop(conn, machine, t0)                             # degraded (建玉あり)
    _tick(conn, machine, t0 + timedelta(minutes=1), failed=True)
    _tick(conn, machine, t0 + timedelta(minutes=2), failed=True)   # 2 連続: confirmed
    _tick(conn, machine, t0 + timedelta(minutes=3))               # 健全に戻る: 待ち
    machine.request_resume(t0 + timedelta(minutes=4), acknowledge=False)
    _tick(conn, machine, t0 + timedelta(minutes=5))               # 未処理の足があり拒否
    machine.request_resume(t0 + timedelta(minutes=6), acknowledge=True)
    _tick(conn, machine, t0 + timedelta(minutes=7))               # ack 付きで受理 → ready
    assert machine.state == "ready"
    ops_env.wall.now = t0 + timedelta(minutes=8)
    service.emit_pending_notices(improve_enabled=False)
    assert _guard_events(ops_env.conn) == [
        ("stopped", 1), ("awaiting_resume", 1), ("resume_rejected", 1),
        ("resume_accepted", 1), ("resumed", 1)]
    service.emit_pending_notices(improve_enabled=False)
    assert len(_guard_events(ops_env.conn)) == 5


def test_a_closed_episode_recorded_before_the_enabled_time_is_never_sent(ops_env):
    conn, machine = _machine(ops_env, flat_stall_max_sec=0)
    t0 = START - timedelta(hours=3)
    _tick(conn, machine, t0, failed=True)
    _tick(conn, machine, t0 + timedelta(minutes=1), failed=True)          # confirmed
    for minute in (2, 3, 4):
        _tick(conn, machine, t0 + timedelta(minutes=minute))               # 自動復帰
    assert machine.state == "ready"
    assert conn.execute("SELECT COUNT(*) FROM datafeed_outage_notices").fetchone()[0] == 2
    ops_env.wall.now = START
    service = ops_env.service(datafeed_notices=True)
    service.emit_pending_notices(improve_enabled=False)
    assert _guard_events(ops_env.conn) == []
    assert alert_state.get(ops_env.conn, "datafeed_notice.producer_enabled_at") == START.isoformat()


def test_the_open_episode_recorded_before_the_enabled_time_is_sent_once_by_the_startup_supplement(
        ops_env):
    conn, machine = _machine(ops_env)
    t0 = START - timedelta(hours=3)
    _open_position(conn, t0 - timedelta(minutes=30))
    _start_stop(conn, machine, t0)
    _tick(conn, machine, t0 + timedelta(minutes=1), failed=True)
    _tick(conn, machine, t0 + timedelta(minutes=2), failed=True)          # confirmed
    ops_env.wall.now = START
    service = ops_env.service(datafeed_notices=True)
    service.emit_pending_notices(improve_enabled=False)
    service.emit_pending_notices(improve_enabled=False)
    assert _guard_events(ops_env.conn) == [("stopped", 1)]


def test_a_notice_write_that_fails_on_the_machine_side_is_still_collected_after_a_retry(ops_env):
    ops_env.wall.now = START
    service = ops_env.service(datafeed_notices=True)
    service.emit_pending_notices(improve_enabled=False)
    conn, machine = _machine(ops_env)
    t0 = START + timedelta(hours=1)
    ops_env.wall.now = t0 + timedelta(minutes=5)
    _open_position(conn, t0 - timedelta(minutes=30))
    _start_stop(conn, machine, t0)
    _tick(conn, machine, t0 + timedelta(minutes=1), failed=True)
    # 最初の記録を失敗させる (次の tick の冒頭で再試行される)
    conn.execute("CREATE TRIGGER block BEFORE INSERT ON datafeed_outage_notices "
                 "BEGIN SELECT RAISE(ABORT,'blocked'); END")
    conn.commit()
    _tick(conn, machine, t0 + timedelta(minutes=2), failed=True)       # confirmed。記録は失敗する
    assert conn.execute("SELECT COUNT(*) FROM datafeed_outage_notices").fetchone()[0] == 0
    conn.execute("DROP TRIGGER block")
    conn.commit()
    _tick(conn, machine, t0 + timedelta(minutes=3), failed=True)
    service.emit_pending_notices(improve_enabled=False)
    assert _guard_events(ops_env.conn) == [("stopped", 1)]
