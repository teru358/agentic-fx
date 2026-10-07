"""AC-24: 別 process の plugin flock 待ちと SQLite 競合の下での応答。AC-34: 有界 tail。"""
from __future__ import annotations

import os
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agentic_fx.ops import ErrorCode, OpsError
from agentic_fx.ops.tail import MAX_SCAN_BYTES, tail_lines
from agentic_fx.store import backlog

from .conftest import APPROVER, OPERATOR, digest_of, wait_job

CHILD = Path(__file__).with_name("_child.py")


def _spawn(*args):
    return subprocess.Popen([sys.executable, str(CHILD), *map(str, args)],
                            stdin=subprocess.DEVNULL)


def _wait_file(path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def _quantiles(samples):
    ordered = sorted(samples)
    return (ordered[int(len(ordered) * 0.95) - 1], ordered[int(len(ordered) * 0.99) - 1],
            ordered[-1])


def test_ac24_ops_lane_stays_responsive_while_a_decision_waits_for_external_flock(ops_env):
    service = ops_env.service()
    approval_id = ops_env.make_plugin_approval()
    ops_env.state.update(kill_switch_latched=True)
    generation = ops_env.state.load().kill_switch_generation
    backlog_id = backlog.add(ops_env.conn, "seed", "user", ops_env.wall())
    ready = ops_env.root / "plugin-ready"
    child = _spawn("hold_plugin_lock", ops_env.plugins_root, "sma", ready, 4)
    try:
        _wait_file(ready)
        reply = service.approve(APPROVER, approval_id, digest_of(ops_env, approval_id))
        timings = {"status": [], "backlog": [], "reset": []}
        for n in range(50):
            started = time.monotonic()
            service.status(OPERATOR)
            timings["status"].append(time.monotonic() - started)
            started = time.monotonic()
            service.transition_backlog(OPERATOR, backlog_id, "note" if n % 2 == 0 else "reopen")
            timings["backlog"].append(time.monotonic() - started)
            started = time.monotonic()
            with pytest.raises(OpsError):
                service.reset_kill_switch(APPROVER, generation + 1)
            timings["reset"].append(time.monotonic() - started)
        assert service.get_job(APPROVER, reply["job_id"])["state"] == "running"
    finally:
        child.wait(30)
    for name, samples in timings.items():
        p95, p99, worst = _quantiles(samples)
        assert p95 <= 0.1 and p99 <= 0.3 and worst <= 1.0, (name, p95, p99, worst)
    assert wait_job(service, reply["job_id"], timeout=30)["state"] == "done"


def test_ac24_decisions_and_core_writes_do_not_hit_sqlite_busy(ops_env):
    service = ops_env.service()
    ids = [ops_env.make_plugin_approval(name=f"p{n}", mission_id=n + 1) for n in range(20)]
    out = ops_env.root / "core-writer"
    child = _spawn("core_writer", ops_env.db_path, 40, 0.1, out)
    replies = []
    for approval_id in ids:
        if approval_id % 2:
            replies.append(service.reject(APPROVER, approval_id, "x"))
        else:
            replies.append(service.approve(APPROVER, approval_id,
                                           digest_of(ops_env, approval_id)))
        time.sleep(0.05)
    results = [wait_job(service, r["job_id"], timeout=30) for r in replies]
    assert child.wait(60) == 0
    busy, worst = out.read_text().split()
    assert int(busy) == 0 and float(worst) <= 1.0
    assert [r["result_code"] for r in results if r["state"] != "done"] == []
    assert all(r["result_code"] != "database_busy" for r in results)


def test_ac24_api_side_busy_becomes_503_database_busy_within_deadline(ops_env):
    service = ops_env.service()
    holder = sqlite3.connect(ops_env.db_path, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        with pytest.raises(OpsError) as raised:
            service.add_backlog(OPERATOR, "k", "idea", deadline=ops_env.mono() + 0.5)
        elapsed = time.monotonic() - started
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert raised.value.code is ErrorCode.DATABASE_BUSY
    assert elapsed <= 1.0



def _hold_exclusive(db_path: Path) -> sqlite3.Connection:
    holder = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN EXCLUSIVE")
    return holder


def test_ac24_stop_during_sqlite_busy_returns_unavailable_quickly(ops_env):
    service = ops_env.service()
    holder = _hold_exclusive(ops_env.db_path)
    outcome = {}

    def call():
        started = time.monotonic()
        try:
            service.add_backlog(OPERATOR, "k-stop", "idea", deadline=ops_env.mono() + 5.0)
        except OpsError as exc:
            outcome["code"] = exc.code
        outcome["elapsed"] = time.monotonic() - started

    worker = threading.Thread(target=call)
    try:
        worker.start()
        time.sleep(0.1)
        stopped = time.monotonic()
        service.stop_event.set()
        worker.join(3.0)
        after_stop = time.monotonic() - stopped
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert not worker.is_alive()
    assert outcome.get("code") is ErrorCode.UNAVAILABLE
    assert after_stop <= 0.5, after_stop
    assert ops_env.conn.execute(
        "SELECT COUNT(*) AS n FROM improvement_backlog").fetchone()["n"] == 0


def test_ac24_short_sqlite_busy_is_waited_out_within_the_deadline(ops_env):
    service = ops_env.service()
    holder = _hold_exclusive(ops_env.db_path)
    release = threading.Timer(0.4, lambda: (holder.execute("ROLLBACK"), holder.close()))
    release.start()
    try:
        reply = service.add_backlog(OPERATOR, "k-wait", "idea", deadline=ops_env.mono() + 5.0)
    finally:
        release.join(5.0)
    assert reply["id"] >= 1
    assert ops_env.conn.execute(
        "SELECT COUNT(*) AS n FROM improvement_backlog").fetchone()["n"] == 1


def test_ac24_decision_lane_stops_during_sqlite_busy(ops_env):
    service = ops_env.service()
    approval_id = ops_env.make_plugin_approval()
    # worker を起動させず、決定 1 件をこのテストの thread で回す。
    service.start_decision_worker = lambda: None
    reply = service.reject(APPROVER, approval_id, "x")
    holder = _hold_exclusive(ops_env.db_path)
    done = {}

    def run():
        done["job"] = service.run_next_decision()

    worker = threading.Thread(target=run)
    try:
        worker.start()
        time.sleep(0.1)
        stopped = time.monotonic()
        service.stop_event.set()
        worker.join(3.0)
        after_stop = time.monotonic() - stopped
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert not worker.is_alive()
    assert after_stop <= 0.5, after_stop
    job = service.jobs.lookup(reply["job_id"])
    assert job.state.value == "shutdown" and job.error_code == "unavailable"
    assert ops_env.approval(approval_id)["status"] == "pending"


# ------------------------------------------------------------------ AC-34


def _big_file(path: Path, size: int, *, giant_last_line: bool = False) -> None:
    line = ("2026-10-05T00:00:00+00:00\tSYSTEM\tevent\t" + "x" * 100 + "\t-\n").encode()
    with path.open("wb") as file:
        chunk = line * (1024 * 1024 // len(line))
        written = 0
        while written < size:
            file.write(chunk)
            written += len(chunk)
        if giant_last_line:
            file.write(b"y" * (8 * 1024 * 1024) + b"\n")


class _CountingRead:
    def __init__(self, monkeypatch):
        self.total = 0
        original = os.pread

        def pread(fd, n, offset):
            data = original(fd, n, offset)
            self.total += len(data)
            return data
        monkeypatch.setattr(os, "pread", pread)


def test_ac34_hundred_mib_log_is_tailed_quickly_with_bounded_reads(ops_env, monkeypatch):
    path = ops_env.root / "big.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    _big_file(path, 100 * 1024 * 1024)
    counter = _CountingRead(monkeypatch)
    service = ops_env.service(log_path=path, activity_path=path)
    started = time.monotonic()
    result = service.log(OPERATOR, 500)
    activity = service.activity(OPERATOR, 500, "TRADE")
    assert time.monotonic() - started <= 1.0
    assert len(result["lines"]) == 500 and result["truncated"] is False
    assert activity["lines"] == [] and activity["truncated"] is True
    assert counter.total <= 2 * MAX_SCAN_BYTES


def test_ac34_giant_line_is_cut_at_2000_characters(ops_env):
    path = ops_env.root / "giant.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    _big_file(path, 1024 * 1024, giant_last_line=True)
    result = tail_lines(path, 5)
    assert all(len(line) <= 2000 for line in result.lines)
    assert result.bytes_read <= MAX_SCAN_BYTES
    assert result.lines and set(result.lines[-1]) == {"y"}


def test_ac34_activity_category_is_enumerated(ops_env):
    service = ops_env.service()
    with pytest.raises(OpsError) as raised:
        service.activity(OPERATOR, 10, "../../etc")
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT
    for limit in (0, 501):
        with pytest.raises(OpsError):
            service.log(OPERATOR, limit)


def test_ac34_long_line_inside_the_scan_is_cut_at_2000_characters(ops_env):
    path = ops_env.root / "long.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("head\n" + "z" * 3000 + "\n" + "tail\n", encoding="utf-8")
    result = tail_lines(path, 3)
    assert [len(line) for line in result.lines] == [4, 2000, 4]


def test_ac34_activity_category_keeps_exactly_the_matching_lines(ops_env):
    from agentic_fx.activity import Category
    path = ops_env.root / "mixed.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    written = [f"2026-10-05T00:00:0{n}+00:00\t{category.value}\tevent\tsummary {n}"
               for n, category in enumerate(Category)]
    path.write_text("".join(f"{line}\n" for line in written), encoding="utf-8")
    service = ops_env.service(activity_path=path)
    for category in Category:
        result = service.activity(OPERATOR, 10, category.value)
        assert result["lines"] == [line for line in written
                                   if line.split("\t")[1] == category.value], category


def _busy_after_deadline(ops_env, service, hold_seconds: float, holders: list):
    """副作用の直後に要求 deadline を過ぎさせ、DB を ``hold_seconds`` だけ占有する。"""
    real = service._regenerate_policy

    def wrapped():
        real()
        ops_env.mono.advance(60.0)
        holders.append(_hold_exclusive(ops_env.db_path))
        timer = threading.Timer(hold_seconds, lambda: (holders[0].execute("ROLLBACK"),
                                                       holders[0].close()))
        timer.start()
        holders.append(timer)

    service._regenerate_policy = wrapped


def test_ac24_terminal_row_is_written_even_when_busy_outlasts_the_request_deadline(ops_env):
    service = ops_env.service()
    holders: list = []
    _busy_after_deadline(ops_env, service, 0.3, holders)
    try:
        reply = service.add_policy(OPERATOR, "k-term", "risk small",
                                   deadline=ops_env.mono() + 0.5)
    finally:
        holders[-1].join(5.0)
    assert "id" in reply
    rows = ops_env.conn.execute(
        "SELECT phase FROM ops_requests WHERE endpoint='POST /v1/policy' "
        "AND phase!='accepted'").fetchall()
    assert [row["phase"] for row in rows] == ["succeeded"]
    assert ops_env.conn.execute(
        "SELECT state FROM ops_idempotency").fetchone()["state"] == "succeeded"


def test_ac24_terminal_write_budget_is_bounded(ops_env):
    from agentic_fx.ops.service import OpsLimits
    service = ops_env.service(limits=OpsLimits(terminal_write_seconds=0.3))
    holders: list = []
    _busy_after_deadline(ops_env, service, 3.0, holders)
    started = time.monotonic()
    try:
        with pytest.raises(Exception):
            service.add_policy(OPERATOR, "k-bound", "risk small",
                               deadline=ops_env.mono() + 0.5)
        elapsed = time.monotonic() - started
    finally:
        holders[-1].join(5.0)
    assert elapsed <= 1.5, elapsed
