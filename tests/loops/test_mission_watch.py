"""Mission 実行の watchdog 計測点テスト."""
from agentic_fx.loops.mission_watch import MissionWatch


def test_before_timeout_no_breach():
    """timeout+grace 未満なら breached() は None."""
    times = [0.0]
    watch = MissionWatch(time_fn=lambda: times[0])
    watch.begin(123, "trade", timeout_sec=10.0)
    times[0] = 50.0  # 10 + 60 未満
    assert watch.breached() is None


def test_after_grace_breach():
    """timeout+grace 超過なら entry を返す (mission_id/loop 一致)."""
    times = [0.0]
    watch = MissionWatch(time_fn=lambda: times[0])
    watch.begin(456, "ask", timeout_sec=10.0)
    times[0] = 75.0  # 10 + 60 超過
    entry = watch.breached()
    assert entry is not None
    assert entry.mission_id == 456
    assert entry.loop == "ask"
    assert entry.notified is False


def test_mark_notified_prevents_breach():
    """mark_notified 後は breached() は None."""
    times = [0.0]
    watch = MissionWatch(time_fn=lambda: times[0])
    watch.begin(789, "trade", timeout_sec=10.0)
    times[0] = 75.0
    watch.mark_notified(789)
    assert watch.breached() is None


def test_end_clears_entry():
    """end() 後は breached() は None."""
    times = [0.0]
    watch = MissionWatch(time_fn=lambda: times[0])
    watch.begin(111, "trade", timeout_sec=10.0)
    watch.end(111)
    times[0] = 75.0
    assert watch.breached() is None


def test_begin_new_mission_replaces_entry():
    """新 Mission begin で前 Mission が上書きされ、notified リセット."""
    times = [0.0]
    watch = MissionWatch(time_fn=lambda: times[0])
    watch.begin(111, "trade", timeout_sec=10.0)
    times[0] = 50.0
    watch.mark_notified(111)
    # 新 Mission begin
    watch.begin(222, "ask", timeout_sec=10.0)
    times[0] = 130.0  # 10 + 60 + 60 超過
    entry = watch.breached()
    assert entry is not None
    assert entry.mission_id == 222
    assert entry.notified is False  # reset


def test_sequential_missions_new_started_time():
    """前 Mission 終了→次 Mission 開始のとき、経過は新 started 起点で判定."""
    times = [0.0]
    watch = MissionWatch(time_fn=lambda: times[0])

    watch.begin(111, "trade", timeout_sec=10.0)
    times[0] = 50.0
    watch.end(111)

    times[0] = 51.0
    watch.begin(222, "ask", timeout_sec=10.0)
    times[0] = 70.0  # started from 51, so elapsed = 19, which is < 70
    assert watch.breached() is None

    times[0] = 122.0  # now elapsed = 71, which is > 70
    entry = watch.breached()
    assert entry is not None
    assert entry.mission_id == 222
