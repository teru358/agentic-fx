import json

import pytest

from agentic_fx.core.contracts import Mode
from agentic_fx.store.state import AppState, StateError, StateStore


def test_defaults_when_missing(tmp_path):
    s = StateStore(tmp_path / "state" / "app_state.json").load()
    assert s == AppState(initialized=False, mode=Mode.LEARNING,
                         autopilot=False, kill_switch_latched=False)


def test_update_roundtrip(tmp_path):
    store = StateStore(tmp_path / "app_state.json")
    store.update(initialized=True)
    s2 = StateStore(tmp_path / "app_state.json").load()
    assert s2.initialized is True
    assert s2.mode is Mode.LEARNING


def test_atomic_write_no_partial_file(tmp_path):
    path = tmp_path / "app_state.json"
    store = StateStore(path)
    store.update(initialized=True, autopilot=True)
    data = json.loads(path.read_text())
    assert data["autopilot"] is True
    assert not list(tmp_path.glob("*.tmp"))


def test_unknown_field_rejected(tmp_path):
    store = StateStore(tmp_path / "app_state.json")
    try:
        store.update(no_such_field=1)
        assert False, "should raise"
    except TypeError:
        pass


def test_load_rejects_string_false_autopilot(tmp_path):
    path = tmp_path / "app_state.json"
    path.write_text(json.dumps({
        "initialized": True, "mode": "learning",
        "autopilot": "false", "kill_switch_latched": False,
    }))
    with pytest.raises(StateError):
        StateStore(path).load()


def test_load_rejects_string_false_initialized(tmp_path):
    path = tmp_path / "app_state.json"
    path.write_text(json.dumps({
        "initialized": "false", "mode": "learning",
        "autopilot": False, "kill_switch_latched": False,
    }))
    with pytest.raises(StateError):
        StateStore(path).load()


def test_load_rejects_bogus_mode(tmp_path):
    path = tmp_path / "app_state.json"
    path.write_text(json.dumps({
        "initialized": True, "mode": "bogus",
        "autopilot": False, "kill_switch_latched": False,
    }))
    with pytest.raises(StateError):
        StateStore(path).load()


def test_load_rejects_missing_key(tmp_path):
    path = tmp_path / "app_state.json"
    path.write_text(json.dumps({
        "initialized": True, "mode": "learning", "autopilot": False,
    }))
    with pytest.raises(StateError):
        StateStore(path).load()


def test_load_rejects_int_for_bool_field(tmp_path):
    path = tmp_path / "app_state.json"
    path.write_text(json.dumps({
        "initialized": 1, "mode": "learning",
        "autopilot": False, "kill_switch_latched": False,
    }))
    with pytest.raises(StateError):
        StateStore(path).load()


# --- 排他・耐久性・世代付きラッチ -------------------------------------------
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone

from agentic_fx.store.state import GenerationMismatch, NotLatched

LATCH_TIME = datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc)


class _FixedClock:
    def __init__(self, t):
        self.t = t

    def now(self):
        return self.t


def test_concurrent_updates_in_threads_lose_nothing(tmp_path):
    """別々のキーを同時に更新しても、どちらの更新も失われない。"""
    path = tmp_path / "app_state.json"
    store = StateStore(path)
    barrier = threading.Barrier(2)
    errors = []

    def a():
        try:
            for _ in range(40):
                barrier.wait()
                store.update(autopilot=True)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    def b():
        try:
            for _ in range(40):
                barrier.wait()
                store.update(kill_switch_latched=True)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    ts = [threading.Thread(target=a), threading.Thread(target=b)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors
    s = store.load()
    assert s.autopilot is True and s.kill_switch_latched is True


def test_update_holds_lock_across_read_modify_write(tmp_path):
    """load と save の間に別の書き手が割り込めない (lost update が無い)。"""
    path = tmp_path / "app_state.json"
    store = StateStore(path)
    other = StateStore(path)
    inside = threading.Event()
    release = threading.Event()
    real_load = store.load

    def slow_load():
        s = real_load()
        inside.set()
        release.wait(5)
        return s

    store.load = slow_load
    t = threading.Thread(target=lambda: store.update(autopilot=True))
    t.start()
    assert inside.wait(5)
    done = threading.Event()

    def writer():
        other.update(initialized=True)
        done.set()

    w = threading.Thread(target=writer)
    w.start()
    assert not done.wait(0.3)  # 先の更新が終わるまで待たされる
    release.set()
    t.join()
    w.join()
    s = other.load()
    assert s.autopilot is True and s.initialized is True


_CHILD = """
import sys
from agentic_fx.store.state import StateStore
from pathlib import Path
s = StateStore(Path(sys.argv[1]))
for _ in range(60):
    s.update(initialized=True)
    s.update(kill_switch_latched=True)
"""


def test_concurrent_update_from_another_process_loses_nothing(tmp_path):
    path = tmp_path / "app_state.json"
    store = StateStore(path)
    procs = [subprocess.Popen([sys.executable, "-c", _CHILD, str(path)])
             for _ in range(2)]
    for _ in range(60):
        store.update(autopilot=True)
        store.update(mode=Mode.LEARNING)
    assert [p.wait(60) for p in procs] == [0, 0]
    s = store.load()
    assert s.autopilot is True and s.initialized is True
    assert s.kill_switch_latched is True


def test_temp_names_differ_per_call_and_none_left(tmp_path, monkeypatch):
    path = tmp_path / "app_state.json"
    store = StateStore(path)
    names = []
    real_replace = os.replace

    def spy(src, dst):
        names.append(os.path.basename(src))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    store.update(initialized=True)
    store.update(autopilot=True)
    assert len(names) == 2 and names[0] != names[1]
    assert "app_state.tmp" not in names
    assert not list(tmp_path.glob("*.tmp"))


def test_failed_write_keeps_original_and_leaves_no_temp(tmp_path, monkeypatch):
    path = tmp_path / "app_state.json"
    store = StateStore(path)
    store.update(initialized=True)
    before = path.read_bytes()

    def boom(fd):
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError):
        store.update(autopilot=True)
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert not [p for p in tmp_path.iterdir()
                if p.name not in ("app_state.json", "app_state.json.lock")]


def test_parent_directory_is_fsynced_after_replace(tmp_path, monkeypatch):
    path = tmp_path / "app_state.json"
    store = StateStore(path)
    events = []
    real_replace, real_fsync = os.replace, os.fsync
    dir_fds = []
    real_open = os.open

    def spy_open(p, flags, *a, **k):
        fd = real_open(p, flags, *a, **k)
        if flags & getattr(os, "O_DIRECTORY", 0):
            dir_fds.append(fd)
        return fd

    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "replace",
                        lambda a, b: (events.append("replace"),
                                      real_replace(a, b))[1])
    monkeypatch.setattr(os, "fsync",
                        lambda fd: (events.append(
                            "dirsync" if fd in dir_fds else "fsync"),
                            real_fsync(fd))[1])
    store.update(initialized=True)
    assert events.index("replace") < len(events) - 1
    assert events[-1] == "dirsync"


def test_latch_increments_generation_and_stamps_time(tmp_path):
    store = StateStore(tmp_path / "app_state.json",
                       clock=_FixedClock(LATCH_TIME))
    s = store.update(kill_switch_latched=True)
    assert s.kill_switch_generation == 1
    assert s.kill_switch_latched_at == "2026-10-03T08:00:00+00:00"


def test_relatch_while_latched_keeps_generation_and_time(tmp_path):
    clock = _FixedClock(LATCH_TIME)
    store = StateStore(tmp_path / "app_state.json", clock=clock)
    store.update(kill_switch_latched=True)
    clock.t = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)
    s = store.update(kill_switch_latched=True)
    assert s.kill_switch_generation == 1
    assert s.kill_switch_latched_at == "2026-10-03T08:00:00+00:00"


def test_reset_succeeds_only_for_matching_generation(tmp_path):
    store = StateStore(tmp_path / "app_state.json",
                       clock=_FixedClock(LATCH_TIME))
    store.update(kill_switch_latched=True)
    s = store.reset_kill_switch(1)
    assert s.kill_switch_latched is False
    assert s.kill_switch_generation == 1
    assert s.kill_switch_latched_at == "2026-10-03T08:00:00+00:00"
    assert store.load() == s
    # 解除後に再ラッチすると世代が進む
    assert store.update(kill_switch_latched=True).kill_switch_generation == 2


def test_reset_with_stale_generation_writes_nothing(tmp_path):
    path = tmp_path / "app_state.json"
    store = StateStore(path, clock=_FixedClock(LATCH_TIME))
    store.update(kill_switch_latched=True)
    store.reset_kill_switch(1)
    store.update(kill_switch_latched=True)  # 新しいラッチ (generation 2)
    before = path.read_bytes()
    with pytest.raises(GenerationMismatch) as ei:
        store.reset_kill_switch(1)
    assert ei.value.current == 2
    assert ei.value.latched_at == "2026-10-03T08:00:00+00:00"
    assert path.read_bytes() == before
    assert store.load().kill_switch_latched is True


def test_reset_when_not_latched_raises_and_writes_nothing(tmp_path):
    path = tmp_path / "app_state.json"
    store = StateStore(path)
    with pytest.raises(NotLatched):
        store.reset_kill_switch(0)
    assert not path.exists()


def test_legacy_state_file_without_generation_keys_loads(tmp_path):
    path = tmp_path / "app_state.json"
    path.write_text(json.dumps({"initialized": True, "mode": "learning",
                                "autopilot": False,
                                "kill_switch_latched": True}))
    s = StateStore(path).load()
    assert s.kill_switch_generation == 0
    assert s.kill_switch_latched_at is None
    assert s.kill_switch_latched is True


def test_generation_keys_roundtrip_through_file(tmp_path):
    path = tmp_path / "app_state.json"
    StateStore(path, clock=_FixedClock(LATCH_TIME)).update(
        kill_switch_latched=True)
    raw = json.loads(path.read_text())
    assert raw["kill_switch_generation"] == 1
    assert raw["kill_switch_latched_at"] == "2026-10-03T08:00:00+00:00"
    s = StateStore(path).load()
    assert s.kill_switch_generation == 1
    assert s.kill_switch_latched_at == "2026-10-03T08:00:00+00:00"


@pytest.mark.parametrize("key,bad", [
    ("kill_switch_generation", "1"), ("kill_switch_generation", True),
    ("kill_switch_generation", 1.5), ("kill_switch_latched_at", 5),
])
def test_wrong_type_generation_keys_fail_closed(tmp_path, key, bad):
    path = tmp_path / "app_state.json"
    raw = {"initialized": True, "mode": "learning", "autopilot": False,
           "kill_switch_latched": True, key: bad}
    path.write_text(json.dumps(raw))
    with pytest.raises(StateError):
        StateStore(path).load()


def test_reset_holds_lock_across_read_modify_write(tmp_path):
    """解除の load と save の間に、別インスタンスの更新は割り込めない。"""
    path = tmp_path / "app_state.json"
    store = StateStore(path, clock=_FixedClock(LATCH_TIME))
    other = StateStore(path, clock=_FixedClock(LATCH_TIME))
    store.update(kill_switch_latched=True)
    inside = threading.Event()
    release = threading.Event()
    real_load = store.load

    def slow_load():
        s = real_load()
        inside.set()
        release.wait(5)
        return s

    store.load = slow_load
    t = threading.Thread(target=lambda: store.reset_kill_switch(1))
    t.start()
    assert inside.wait(5)
    done = threading.Event()

    def writer():
        other.update(autopilot=True)
        done.set()

    w = threading.Thread(target=writer)
    w.start()
    assert not done.wait(0.3)
    release.set()
    t.join()
    w.join()
    s = other.load()
    assert s.autopilot is True and s.kill_switch_latched is False


def test_lock_is_released_after_failed_update_even_if_exception_is_kept(
        tmp_path, monkeypatch):
    """更新が例外で失敗しても、例外を保持している間に別インスタンスが更新できる。"""
    path = tmp_path / "app_state.json"
    store = StateStore(path)
    other = StateStore(path)

    def boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError) as ei:  # ei が traceback (= frame) を保持する
        store.update(autopilot=True)
    monkeypatch.undo()
    done = threading.Event()

    def writer():
        other.update(initialized=True)
        done.set()

    w = threading.Thread(target=writer, daemon=True)
    w.start()
    assert done.wait(2), "lock was not released after a failed update"
    assert ei.value is not None


def test_failed_replace_propagates_and_keeps_original(tmp_path, monkeypatch):
    path = tmp_path / "app_state.json"
    store = StateStore(path)
    store.update(initialized=True)
    before = path.read_bytes()

    def boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.update(autopilot=True)
    monkeypatch.undo()
    assert path.read_bytes() == before


def test_file_contents_are_flushed_before_fsync(tmp_path, monkeypatch):
    """fsync の時点で、書き込む内容が全てファイルに渡っている。"""
    path = tmp_path / "app_state.json"
    store = StateStore(path)
    sizes = []
    real_fsync = os.fsync

    def spy(fd):
        if not os.path.isdir(f"/proc/self/fd/{fd}"):
            sizes.append(os.fstat(fd).st_size)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)
    store.update(initialized=True)
    assert sizes and all(n > 0 for n in sizes)
    assert sizes[0] == len(path.read_bytes())


def test_latched_at_is_utc_even_if_clock_is_not(tmp_path):
    jst = timezone(timedelta(hours=9))
    store = StateStore(tmp_path / "app_state.json",
                       clock=_FixedClock(datetime(2026, 10, 3, 17, 0,
                                                  tzinfo=jst)))
    s = store.update(kill_switch_latched=True)
    assert s.kill_switch_latched_at == "2026-10-03T08:00:00+00:00"


def test_updates_not_latching_the_switch_leave_generation_alone(tmp_path):
    store = StateStore(tmp_path / "app_state.json",
                       clock=_FixedClock(LATCH_TIME))
    s = store.update(autopilot=True)
    assert (s.kill_switch_generation, s.kill_switch_latched_at) == (0, None)
    s = store.update(kill_switch_latched=False)  # ラッチしていない所への False
    assert (s.kill_switch_generation, s.kill_switch_latched_at) == (0, None)
    store.update(kill_switch_latched=True)
    s = store.update(autopilot=False)  # ラッチ中の無関係な更新
    assert (s.kill_switch_generation, s.kill_switch_latched_at) == (
        1, "2026-10-03T08:00:00+00:00")


def test_reset_with_future_generation_writes_nothing(tmp_path):
    path = tmp_path / "app_state.json"
    store = StateStore(path, clock=_FixedClock(LATCH_TIME))
    store.update(kill_switch_latched=True)
    before = path.read_bytes()
    with pytest.raises(GenerationMismatch):
        store.reset_kill_switch(2)
    assert path.read_bytes() == before


def test_reset_transitions_over_latch_cycles(tmp_path):
    """(ラッチ, 世代) の遷移: 古い世代の解除は、何回目のラッチでも新ラッチを消さない。"""
    store = StateStore(tmp_path / "app_state.json",
                       clock=_FixedClock(LATCH_TIME))
    for gen in (1, 2, 3):
        store.update(kill_switch_latched=True)
        assert store.load().kill_switch_generation == gen
        for stale in range(0, gen):
            with pytest.raises(GenerationMismatch):
                store.reset_kill_switch(stale)
            assert store.load().kill_switch_latched is True
        store.reset_kill_switch(gen)
        s = store.load()
        assert s.kill_switch_latched is False and s.kill_switch_generation == gen
        for g in range(0, gen + 2):
            with pytest.raises(NotLatched):
                store.reset_kill_switch(g)
