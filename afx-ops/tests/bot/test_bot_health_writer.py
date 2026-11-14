"""bot の health snapshot の writer: 単一 worker、有限値だけ、書込み失敗で止まらない、終端の保存。"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from types import SimpleNamespace

import pytest

from afx_ops import health
from afx_ops.bot import runtime
from afx_ops.bot.adapter import BotFailure
from afx_ops.bot.progress import ProgressTracker
from afx_ops.health import HealthWriter, Publisher, SNAPSHOT_NAME
from test_bot_logsafe import _FakeWatchdog, _Out, _isolated


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _dir(tmp_path) -> int:
    return os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)


def _read(tmp_path) -> dict:
    return json.loads((tmp_path / SNAPSHOT_NAME).read_text())


def _publisher(tmp_path, mono=None, wall=None, say=None):
    mono = mono or Clock()
    wall = wall or Clock(1_800_000_000.0)
    tracker = ProgressTracker(mono)
    writer = HealthWriter(_dir(tmp_path), say=say)
    return Publisher(writer, tracker, wall=wall, mono=mono), tracker, mono, wall


def test_the_snapshot_has_exactly_the_fixed_fields_and_validates(tmp_path):
    pub, tracker, mono, wall = _publisher(tmp_path)
    tracker.gateway_connected()
    pub.publish(force=True)
    assert pub._writer.wait(1, 5)
    record = _read(tmp_path)
    assert tuple(sorted(record)) == tuple(sorted(health.FIELDS))
    assert health.parse((tmp_path / SNAPSHOT_NAME).read_bytes()) is not None
    assert record["gateway_state"] == "healthy" and record["terminal"] is None
    pub.close()


@pytest.mark.parametrize("latency", [float("nan"), float("inf"), float("-inf"), -1.0, 1e12, "x", True])
def test_a_non_finite_or_absurd_latency_is_written_as_null(tmp_path, latency):
    pub, tracker, *_ = _publisher(tmp_path)
    pub.bind_latency(lambda: latency)
    pub.publish(force=True)
    assert pub._writer.wait(1, 5)
    assert _read(tmp_path)["last_ack_latency_ms"] is None
    assert b"NaN" not in (tmp_path / SNAPSHOT_NAME).read_bytes()
    pub.close()


def test_a_finite_latency_is_written_in_milliseconds(tmp_path):
    pub, *_ = _publisher(tmp_path)
    pub.bind_latency(lambda: 0.0425)
    pub.publish(force=True)
    assert pub._writer.wait(1, 5)
    assert _read(tmp_path)["last_ack_latency_ms"] == pytest.approx(42.5)
    pub.close()


def test_the_writer_uses_one_thread_and_never_writes_concurrently(tmp_path, monkeypatch):
    active, peak, threads = [0], [0], set()
    real = health.write_snapshot

    def slow(dir_fd, name, obj):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        threads.add(threading.get_ident())
        time.sleep(0.01)
        real(dir_fd, name, obj)
        active[0] -= 1
    monkeypatch.setattr(health, "write_snapshot", slow)
    writer = HealthWriter(_dir(tmp_path))
    seqs = []

    def burst():
        for i in range(20):
            seqs.append(writer.submit({"i": i}))
    callers = [threading.Thread(target=burst) for _ in range(3)]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join()
    assert writer.wait(max(seqs), 10)
    assert peak[0] == 1 and len(threads) == 1
    assert threading.get_ident() not in threads
    writer.close()


def test_only_the_latest_pending_snapshot_is_written(tmp_path, monkeypatch):
    gate, entered = threading.Event(), threading.Event()
    written = []
    real = health.write_snapshot

    def hold(dir_fd, name, obj):
        written.append(obj["i"])
        entered.set()
        gate.wait(5)
        real(dir_fd, name, obj)
    monkeypatch.setattr(health, "write_snapshot", hold)
    writer = HealthWriter(_dir(tmp_path))
    writer.submit({"i": 0})
    assert entered.wait(5)
    for i in range(1, 6):
        last = writer.submit({"i": i})
    gate.set()
    assert writer.wait(last, 10)
    assert written == [0, 5]
    writer.close()


def test_a_failing_write_does_not_stop_the_writer_and_reports_once(tmp_path, monkeypatch):
    said = []
    real = health.write_snapshot
    state = {"fail": True}

    def flaky(dir_fd, name, obj):
        if state["fail"]:
            raise OSError("disk")
        real(dir_fd, name, obj)
    monkeypatch.setattr(health, "write_snapshot", flaky)
    writer = HealthWriter(_dir(tmp_path), say=said.append)
    for i in range(3):
        assert writer.wait(writer.submit({"version": 1, "i": i}), 5)
    assert said == ["health_snapshot_write_failed"]
    state["fail"] = False
    assert writer.wait(writer.submit({"version": 1, "ok": 1}), 5)
    assert json.loads((tmp_path / SNAPSHOT_NAME).read_text())["ok"] == 1
    # 回復後に再び失敗すれば、遷移として 1 回だけ出る
    state["fail"] = True
    assert writer.wait(writer.submit({"version": 1}), 5)
    assert said == ["health_snapshot_write_failed"] * 2
    writer.close()


def test_a_closed_directory_fd_is_a_write_failure_not_a_crash(tmp_path):
    said = []
    fd = _dir(tmp_path)
    os.close(fd)
    writer = HealthWriter(fd, say=said.append)
    assert writer.wait(writer.submit({"version": 1}), 5)
    assert said == ["health_snapshot_write_failed"]


def test_a_failing_say_does_not_break_the_writer(tmp_path, monkeypatch):
    def boom(_text):
        raise RuntimeError("x")
    monkeypatch.setattr(health, "write_snapshot", lambda *a: (_ for _ in ()).throw(OSError()))
    writer = HealthWriter(_dir(tmp_path), say=boom)
    assert writer.wait(writer.submit({}), 5)
    assert writer.wait(writer.submit({}), 5)
    writer.close()


def test_publish_writes_on_a_transition_and_every_interval_only(tmp_path):
    pub, tracker, mono, wall = _publisher(tmp_path)
    submitted = []
    real = pub._writer.submit
    pub._writer.submit = lambda snap: (submitted.append(snap), real(snap))[1]
    pub.publish()
    pub.publish()
    assert len(submitted) == 1
    mono.t += 30
    pub.publish()
    assert len(submitted) == 1
    tracker.gateway_connected()
    pub.publish()
    assert len(submitted) == 2 and submitted[-1]["gateway_state"] == "healthy"
    mono.t += 59
    pub.publish()
    assert len(submitted) == 2
    mono.t += 2
    pub.publish()
    assert len(submitted) == 3
    pub.close()


def test_delivery_stalls_only_after_the_threshold(tmp_path):
    pub, tracker, mono, wall = _publisher(tmp_path)
    tracker.delivery_begin(1)
    assert pub.snapshot()["delivery_state"] == "idle"
    mono.t += health.DELIVERY_STALL_SECONDS
    assert pub.snapshot()["delivery_state"] == "stalled"
    assert pub.snapshot()["delivery_stalled_since"] is not None
    pub.close()


def test_monotonic_times_are_written_as_wall_clock_utc(tmp_path):
    pub, tracker, mono, wall = _publisher(tmp_path)
    tracker.gateway_connected()
    mono.t += 90
    wall.t += 90
    snap = pub.snapshot()
    assert snap["observed_at"] == health.format_time(wall.t)
    assert snap["last_ready_at"] == health.format_time(wall.t - 90)
    pub.close()


# ---- 終端

def test_a_terminal_decision_is_written_and_waited_for(tmp_path):
    pub, tracker, *_ = _publisher(tmp_path)
    pub.publish(force=True)
    pub.finalize("token_invalid")
    record = _read(tmp_path)
    assert record["terminal"] == "permanent" and record["terminal_code"] == "token_invalid"
    pub.finalize("gateway_unhealthy")
    record = _read(tmp_path)
    assert record["terminal"] == "transient" and record["terminal_code"] == "gateway_unhealthy"
    pub.close()


def test_an_unknown_reason_is_not_stored_as_a_terminal(tmp_path):
    pub, *_ = _publisher(tmp_path)
    pub.publish(force=True)
    pub.finalize("Bot MTIzNDU2.secret")
    assert pub._writer.wait(1, 5)
    assert _read(tmp_path)["terminal"] is None
    pub.close()


def test_the_terminal_wait_is_bounded_and_the_exit_proceeds(tmp_path, monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(health, "write_snapshot", lambda *a: gate.wait(10))
    monkeypatch.setattr(health, "TERMINAL_WAIT", 0.2)
    pub, *_ = _publisher(tmp_path)
    start = time.monotonic()
    pub.finalize("token_invalid")
    assert time.monotonic() - start < 3
    gate.set()
    pub.close()


def test_a_failing_terminal_write_still_returns(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "write_snapshot", lambda *a: (_ for _ in ()).throw(OSError()))
    pub, *_ = _publisher(tmp_path)
    pub.finalize("token_invalid")
    pub.close()


@pytest.mark.parametrize("reason", sorted(health.TERMINAL_CODES))
def test_every_allowed_code_round_trips_through_the_reader(tmp_path, reason):
    pub, *_ = _publisher(tmp_path)
    pub.finalize(reason)
    record = health.parse((tmp_path / SNAPSHOT_NAME).read_bytes())
    assert record is not None and record["terminal_code"] == reason
    pub.close()


def test_the_terminal_allowlist_covers_every_bot_reason():
    from afx_ops.bot import exits
    assert set(exits.REASONS) <= health.TERMINAL_CODES
    assert health.PERMANENT_CODES == set(exits.PERMANENT_REASONS)
    assert health.terminal_for("nonsense") is None


# ---- 配線 (run)

def test_run_places_a_start_snapshot_with_no_terminal_and_a_permanent_stop_is_recorded(tmp_path, monkeypatch):
    isolated = _isolated(tmp_path)
    seen = {}

    async def main(*args, health=None, **kwargs):
        health._writer.wait(1, 5)
        seen["early"] = _read(isolated.state_dir)
        raise BotFailure("payload_invalid")
    monkeypatch.setattr(runtime, "_main", main)
    out = _Out()
    assert runtime.run(isolated, client=object(), stderr=out, watchdog=_FakeWatchdog()) == 78
    assert seen["early"]["terminal"] is None
    final = _read(isolated.state_dir)
    assert final["terminal"] == "permanent" and final["terminal_code"] == "payload_invalid"
    assert "payload_invalid" in out.text


def test_a_restart_replaces_the_previous_terminal_with_null(tmp_path, monkeypatch):
    isolated = _isolated(tmp_path)
    old = health.build(ProgressTracker(Clock()), wall_now=1_800_000_000.0, mono_now=1000.0,
                       terminal="permanent", terminal_code="token_invalid")
    (isolated.state_dir / SNAPSHOT_NAME).write_text(json.dumps(old))
    seen = {}

    async def main(*args, health=None, **kwargs):
        health._writer.wait(1, 5)
        seen["now"] = _read(isolated.state_dir)
    monkeypatch.setattr(runtime, "_main", main)
    runtime.run(isolated, client=object(), stderr=_Out(), watchdog=_FakeWatchdog())
    assert seen["now"]["terminal"] is None


def test_a_transient_internal_error_is_recorded(tmp_path, monkeypatch):
    isolated = _isolated(tmp_path)

    async def main(*args, **kwargs):
        raise RuntimeError("secret-token-value")
    monkeypatch.setattr(runtime, "_main", main)
    assert runtime.run(isolated, client=object(), stderr=_Out(), watchdog=_FakeWatchdog()) == 79
    record = _read(isolated.state_dir)
    assert record["terminal"] == "transient" and record["terminal_code"] == "bot_internal_error"
    assert b"secret-token-value" not in (isolated.state_dir / SNAPSHOT_NAME).read_bytes()


def test_a_snapshot_failure_does_not_stop_the_bot(tmp_path, monkeypatch):
    isolated = _isolated(tmp_path)
    monkeypatch.setattr(health, "write_snapshot", lambda *a: (_ for _ in ()).throw(OSError()))
    ran = []

    async def main(*args, **kwargs):
        ran.append(True)
    monkeypatch.setattr(runtime, "_main", main)
    out = _Out()
    assert runtime.run(isolated, client=object(), stderr=out, watchdog=_FakeWatchdog()) == 0
    assert ran and "health_snapshot_write_failed" in out.text


def test_a_writer_that_cannot_start_does_not_stop_the_bot(tmp_path, monkeypatch):
    isolated = _isolated(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("no thread")
    monkeypatch.setattr(runtime, "HealthWriter", boom)
    ran = []

    async def main(*args, **kwargs):
        ran.append(True)
    monkeypatch.setattr(runtime, "_main", main)
    assert runtime.run(isolated, client=object(), stderr=_Out(), watchdog=_FakeWatchdog()) == 0
    assert ran
