"""WorkerRunner の cleanup protocol 経路: deadline の 1 回発行、ACK 非依存の wait、SIGKILL 後の
再 wait、reap 不可の gate、mission nonce の受け渡し、既定経路の不変。

worker 役は `tests/core/_cleanup_worker_helper.py` を実 process として起こし、製品の
`WorkerRunner._run_with_child` (cleanup protocol 有効) に渡す。停止点は GIL を握る停止
(`ctypes.PyDLL(None).sleep`) と blocking syscall の停止で作る。wait / SIGKILL はモックしない。
"""
from __future__ import annotations

import io
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pytest

from agentic_fx.config import load_settings
from agentic_fx.core import cgroup_host as ch
from agentic_fx.core import mission_cleanup as MC
from agentic_fx.core import mission_protocol as MP
from agentic_fx.core.contracts import FixedClock
from agentic_fx.runners.base import Mission
from agentic_fx.runners.worker_runner import WorkerRunner

REPO = Path(__file__).resolve().parents[2]
SETTINGS = load_settings(REPO / "config" / "settings.yaml.example")
HELPER = REPO / "tests" / "core" / "_cleanup_worker_helper.py"
NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
HEX32 = re.compile(r"[0-9a-f]{32}")

# mission の期限 (mission.timeout_sec + worker_grace_sec) を短くして F1 を作る
MISSION_TIMEOUT_SEC = 1.0
W = SETTINGS.worker.model_copy(update={"worker_grace_sec": 0.5,
                                       "worker_startup_timeout_sec": 20})


def _preexec() -> None:
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


class Run:
    def __init__(self, tmp_path: Path, mode: str, *, protocol: MC.CleanupProtocol | None = None,
                 stop_event: threading.Event | None = None,
                 worker_profile: str = "improve", **runner_kw) -> None:
        self.out = tmp_path / f"{mode}.jsonl"
        self.hooks: list[tuple[str, bool, float]] = []
        if protocol is None:
            protocol = MC.CleanupProtocol(on_reaped=lambda *a: self.hooks.append(a))
        self.protocol = protocol
        self.runner = WorkerRunner(root=tmp_path, settings=SETTINGS, clock=FixedClock(NOW),
                                   rag=None, worker_profile=worker_profile,
                                   stop_event=stop_event, cleanup_protocol=protocol,
                                   **runner_kw)
        self.proc = subprocess.Popen(
            [sys.executable, str(HELPER), mode, str(self.out)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True, preexec_fn=_preexec)
        self.result = None
        self.elapsed = None

    def run(self):
        mission = Mission(prompt="p", tools=[], output_schema={}, max_turns=1,
                          timeout_sec=MISSION_TIMEOUT_SEC)
        t0 = time.monotonic()
        try:
            self.result = self.runner._run_with_child(self.proc, mission, W)
        finally:
            self.elapsed = time.monotonic() - t0
            if self.proc.returncode is None:
                self.proc.kill()
                self.proc.wait(10)
        return self.result

    def notes(self) -> list[dict]:
        if not self.out.exists():
            return []
        return [json.loads(line) for line in self.out.read_text().splitlines() if line]

    def events(self, name: str | None = None) -> list[dict]:
        return [e for e in self.protocol.events if name is None or e["event"] == name]


def _names(protocol: MC.CleanupProtocol) -> list[str]:
    return [e["event"] for e in protocol.events]


def _deadline_frames(notes: list[dict]) -> list[dict]:
    return [n for n in notes if n["event"] == "received" and n["type"] == MP.CLEANUP_DEADLINE]


# --- 発行の契機と 1 回発行 ---------------------------------------------------------

@pytest.mark.parametrize("trigger", ["worker_request", "worker_exit", "mission_deadline",
                                     "stop_event"])
def test_deadline_issued_once_before_first_cleanup_op(tmp_path, trigger):
    stop = threading.Event()
    mode = {"worker_request": "request", "worker_exit": "exit",
            "mission_deadline": "silent", "stop_event": "silent"}[trigger]
    r = Run(tmp_path, mode, stop_event=stop)
    if trigger == "stop_event":
        # worker が go を受けて mission に入ってから止める (起動の速さに依らない)
        def stop_after_go() -> None:
            end = time.monotonic() + 30
            while time.monotonic() < end:
                try:
                    if any(n["event"] == "go" for n in r.notes()):
                        break
                except ValueError:  # 書きかけの行
                    pass
                time.sleep(0.02)
            stop.set()
        threading.Thread(target=stop_after_go, daemon=True).start()
    result = r.run()

    issued = r.events("issued")
    assert len(issued) == 1, r.protocol.events
    assert issued[0]["trigger"] == trigger
    names = _names(r.protocol)
    # 親の cleanup の最初の操作 (worker の wait) より前に発行と送信が済んでいる
    assert names.index("issued") < names.index("deadline_sent") < names.index("worker_wait")
    deadline = issued[0]["deadline"]
    assert deadline == pytest.approx(issued[0]["t"] + MC.CLEANUP_BUDGET_SEC)
    notes = r.notes()
    frames = _deadline_frames(notes)
    if trigger == "worker_exit":
        # 送信先は既に居ない。frame は届かないが、発行は 1 回
        assert frames == []
    else:
        assert len(frames) == 1 and frames[0]["deadline"] == deadline
        started = [n for n in notes if n["event"] == "cleanup_started"]
        assert len(started) == 1
        ack = [n for n in notes if n["event"] == "sent" and n["type"] == MP.CLEANUP_ACK]
        # worker の cleanup は ACK を送った後に始まる
        assert ack and ack[0]["t"] <= started[0]["t"]
        assert frames[0]["t"] <= started[0]["t"]
    assert r.proc.returncode is not None
    assert r.hooks and r.hooks[0][1] is True and r.hooks[0][2] == deadline
    if trigger == "worker_request":
        assert result.status == "completed"
    if trigger in ("mission_deadline", "stop_event"):
        assert result.status == "timeout"


# --- 停止点 (F1〜F3b) --------------------------------------------------------------

def test_worker_stops_before_request(tmp_path):
    r = Run(tmp_path, "F1")
    r.run()
    issued = r.events("issued")
    assert [e["trigger"] for e in issued] == ["mission_deadline"]
    names = _names(r.protocol)
    assert "ack" not in names
    assert names.index("worker_wait") < names.index("worker_sigkill") < names.index("worker_rewait")
    deadline = issued[0]["deadline"]
    kill = r.events("worker_sigkill")[0]
    assert kill["t"] == pytest.approx(deadline - MC.WORKER_KILL_LEAD_SEC, abs=0.3)
    reaped = r.events("reaped")[0]
    assert reaped["t"] < deadline
    assert r.proc.returncode == -signal.SIGKILL
    assert r.hooks == [(r.hooks[0][0], True, deadline)]


def test_worker_stops_before_deadline_receipt(tmp_path):
    r = Run(tmp_path, "F2")
    r.run()
    issued = r.events("issued")
    assert [e["trigger"] for e in issued] == ["worker_request"]
    notes = r.notes()
    # ACK を送っていないので、worker は cleanup を始めていない
    assert not [n for n in notes if n["event"] == "cleanup_started"]
    assert not [n for n in notes if n["event"] == "sent" and n["type"] == MP.CLEANUP_ACK]
    assert "worker_sigkill" in _names(r.protocol)
    assert r.events("reaped")[0]["t"] < issued[0]["deadline"]
    assert r.proc.returncode == -signal.SIGKILL


@pytest.mark.parametrize("mode", ["F3", "F3b"])
def test_worker_stops_after_ack(tmp_path, mode):
    r = Run(tmp_path, mode)
    r.run()
    issued = r.events("issued")
    deadline = issued[0]["deadline"]
    notes = r.notes()
    assert [n for n in notes if n["event"] == "cleanup_started"]
    assert r.events("ack"), "ACK を受けている (ただし待ちには使わない)"
    reaped = r.events("reaped")[0]
    assert reaped["t"] < deadline
    if mode == "F3b":
        # blocking syscall では watchdog が deadline - 3s で os._exit する
        assert r.proc.returncode == MC.WATCHDOG_EXIT_CODE
        assert "worker_sigkill" not in _names(r.protocol)
        assert reaped["t"] == pytest.approx(deadline - MC.WATCHDOG_LEAD_SEC, abs=0.3)
    else:
        # GIL を握られると watchdog は動けない (best-effort)。WorkerRunner の SIGKILL で止まる
        assert r.proc.returncode == -signal.SIGKILL
        assert r.events("worker_sigkill")[0]["t"] == pytest.approx(
            deadline - MC.WORKER_KILL_LEAD_SEC, abs=0.3)


@pytest.mark.parametrize("mode, why", [
    ("ack_dup", "duplicate"), ("ack_mismatch", "mismatch"), ("ack_early", "before_issue")])
def test_cleanup_ack_is_accepted_once_for_the_issued_deadline(tmp_path, mode, why):
    """ACK は発行済みの deadline と一致するものを 1 回だけ受理する。発行前・値の不一致・
    重複は拒否として記録し、受理した ACK の記録は 1 件のまま。"""
    r = Run(tmp_path, mode)
    r.run()
    deadline = r.events("issued")[0]["deadline"]
    accepted = r.events("ack")
    assert len(accepted) == 1 and accepted[0]["deadline"] == deadline, r.protocol.events
    rejected = r.events("ack_rejected")
    assert [e["why"] for e in rejected] == [why], r.protocol.events
    assert r.hooks and r.hooks[0][1] is True


def test_unreapable_worker_closes_gate(tmp_path):
    """再 wait の結果を timeout と判定させる (constructor 引数だけの注入) と
    `worker_not_reaped` で gate が閉じ、`cleanup_mission` (rmdir) を呼ばない。"""
    host = ch.MissionCgroupHost()
    calls: list[tuple] = []
    cleaned: list[str] = []
    host.cleanup_mission = lambda name, deadline: cleaned.append(name)  # type: ignore[method-assign]

    def on_reaped(name: str, reaped: bool, deadline: float) -> None:
        calls.append((name, reaped, deadline))
        if reaped:
            host.cleanup_mission(name, deadline)
        else:
            host.report_worker_not_reaped()

    protocol = MC.CleanupProtocol(on_reaped=on_reaped, inject_rewait_timeout=True)
    r = Run(tmp_path, "F1", protocol=protocol)
    r.run()
    assert [c[1] for c in calls] == [False]
    names = _names(protocol)
    assert "worker_sigkill" in names and "not_reaped" in names and "reaped" not in names
    assert host.blocking_reasons() == (ch.REASON_WORKER_NOT_REAPED,)
    assert cleaned == []


# --- 親→子の書き手 ----------------------------------------------------------------

class _DelayDispatcherLock:
    """stdin への書き手が取る lock の代役。dispatcher thread の取得だけを、別の書き手が
    cleanup_deadline を送り終えるまで遅らせる (採番と書込みの間に他の書き手を割り込ませる)。"""

    def __init__(self, sent: Callable[[], bool]) -> None:
        self._lock = threading.Lock()
        self._sent = sent

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if threading.current_thread().name == "afx-worker-dispatcher":
            end = time.monotonic() + 5
            while not self._sent() and time.monotonic() < end:
                time.sleep(0.01)
        return self._lock.acquire(blocking, timeout)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def test_parent_seq_follows_write_order_across_writers(tmp_path, monkeypatch):
    """dispatcher (tool_rpc_result) と reader (cleanup_deadline) が並んでも、worker が受ける
    seq は書かれた順に 1 ずつ増える (採番は書込みと同じ lock の中)。"""
    from agentic_fx.runners import worker_runner as wr_mod

    holder: dict[str, Run] = {}
    real_threading = threading

    def deadline_sent() -> bool:
        return any(e["event"] == "deadline_sent" for e in holder["r"].protocol.events)

    class _Threading:
        def __getattr__(self, name):
            return getattr(real_threading, name)

        @staticmethod
        def Lock():  # noqa: N802
            return _DelayDispatcherLock(deadline_sent)

    monkeypatch.setattr(wr_mod, "threading", _Threading())
    r = holder["r"] = Run(tmp_path, "rpc_race", rpc_handlers={"probe": lambda args: {"ok": 1}})
    r.run()
    got = [n for n in r.notes() if n["event"] == "received"]
    types = [n["type"] for n in got]
    assert types.index(MP.CLEANUP_DEADLINE) < types.index("tool_rpc_result"), got
    seqs = [n["seq"] for n in got]
    assert seqs == list(range(2, 2 + len(seqs))), got


def test_deadline_issue_does_not_block_on_full_stdin(tmp_path):
    """worker が stdin を読まず、dispatcher が大きな応答の書込みで lock を持ったまま詰まって
    いても、発行は reap を妨げない。deadline を送れなかったことを記録し、SIGKILL と再 wait
    へ進んで deadline の内に reap する。"""
    r = Run(tmp_path, "stdin_full", rpc_handlers={"big": lambda args: {"blob": "x" * (1 << 21)}})
    t = threading.Thread(target=r.run, daemon=True)
    t.start()
    t.join(MISSION_TIMEOUT_SEC + 0.5 + MC.CLEANUP_BUDGET_SEC + 5)
    if t.is_alive():
        r.proc.kill()  # 詰まった書込みを解いて試験の thread を終わらせる
        t.join(30)
        pytest.fail(f"cleanup が stdin の書込みで止まった: {r.protocol.events}")
    issued = r.events("issued")
    assert [e["trigger"] for e in issued] == ["mission_deadline"]
    assert [e["ok"] for e in r.events("deadline_sent")] == [False]
    names = _names(r.protocol)
    assert names.index("deadline_sent") < names.index("worker_wait") < names.index("worker_sigkill")
    reaped = r.events("reaped")
    assert reaped and reaped[0]["t"] < issued[0]["deadline"]
    assert r.proc.returncode == -signal.SIGKILL
    assert r.hooks and r.hooks[0][1] is True


def test_cleanup_request_before_ready_wakes_startup_wait(tmp_path):
    """ready の前に届いた cleanup 要求でも起動待ちを打ち切り、worker_request を契機に後始末へ
    入る (起動の期限まで待たない)。"""
    r = Run(tmp_path, "request_before_ready")
    result = r.run()
    assert result.status == "failed"
    issued = r.events("issued")
    assert [e["trigger"] for e in issued] == ["worker_request"]
    assert [e["trigger"] for e in r.events("finish")] == ["worker_request"]
    assert r.elapsed < MC.CLEANUP_BUDGET_SEC, r.protocol.events
    wait = r.events("worker_wait")[0]
    assert wait["t"] - issued[0]["t"] < 1.0
    assert r.events("reaped")[0]["t"] < issued[0]["deadline"]
    assert r.hooks and r.hooks[0][1] is True


def test_request_then_ready_cleans_up_without_on_ready_or_go(tmp_path, monkeypatch):
    """cleanup 要求が ready より先に届いていたら、ready が queue にあっても wake を優先し、
    on_ready も go も通さず worker_request を契機に後始末へ入る。"""
    orig = WorkerRunner._wait_with_stop
    calls = {"n": 0}

    def wait_after_both(self, q, *, timeout, poll_interval=0.2, wake=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:  # ready の待ち: 要求と ready が両方そろってから判定に入る
            end = time.monotonic() + 10
            while not (wake is not None and wake.is_set() and q.qsize() > 0):
                assert time.monotonic() < end, "要求と ready がそろわない"
                time.sleep(0.01)
        return orig(self, q, timeout=timeout, poll_interval=poll_interval, wake=wake, **kw)

    monkeypatch.setattr(WorkerRunner, "_wait_with_stop", wait_after_both)
    readied: list[dict] = []
    r = Run(tmp_path, "request_then_ready", on_ready=readied.append)
    result = r.run()
    assert result.status == "failed"
    assert readied == []
    assert [e["trigger"] for e in r.events("finish")] == ["worker_request"]
    assert not any(n.get("event") == "go" for n in r.notes())
    assert not any(n.get("type") == "go" for n in r.notes())


def test_wait_prefers_queued_item_over_wake():
    """待ちの判定で queue に既に入っているものは wake より先に取る (wake は次の判定で)。"""
    runner = WorkerRunner(root=Path("/nonexistent"), settings=SETTINGS, clock=FixedClock(NOW),
                          rag=None, worker_profile="improve",
                          cleanup_protocol=MC.CleanupProtocol())
    q: queue.Queue = queue.Queue(maxsize=1)
    wake = threading.Event()
    wake.set()
    q.put(("result", {"status": "completed"}))
    assert runner._wait_with_stop(q, timeout=1, wake=wake,
                                  prefer_queued=True) == ("result", {"status": "completed"})
    from agentic_fx.runners import worker_runner as wr_mod
    with pytest.raises(wr_mod._CleanupWake):
        runner._wait_with_stop(q, timeout=1, wake=wake, prefer_queued=True)
    q.put(("ready", {"ok": True}))
    with pytest.raises(wr_mod._CleanupWake):
        runner._wait_with_stop(q, timeout=1, wake=wake)


def test_result_and_request_together_take_the_result(tmp_path, monkeypatch):
    """cleanup 要求と result が両方届いた状態で終端を待つと result を取り、後始末の契機は
    worker_request でなく worker の終了になる。"""
    orig = WorkerRunner._wait_with_stop
    calls = {"n": 0}

    def wait_after_both(self, q, *, timeout, poll_interval=0.2, wake=None, **kw):
        calls["n"] += 1
        if calls["n"] == 2:  # 終端の待ち: 要求と result が両方そろってから判定に入る
            end = time.monotonic() + 10
            while not (wake is not None and wake.is_set() and q.qsize() > 0):
                assert time.monotonic() < end, "要求と result がそろわない"
                time.sleep(0.01)
        return orig(self, q, timeout=timeout, poll_interval=poll_interval, wake=wake, **kw)

    monkeypatch.setattr(WorkerRunner, "_wait_with_stop", wait_after_both)
    r = Run(tmp_path, "request_then_result")
    result = r.run()
    assert result.status == "completed"
    assert [e["trigger"] for e in r.events("finish")] == [MC.TRIGGER_WORKER_EXIT]


# --- nonce ----------------------------------------------------------------------

def test_mission_nonce_flows_from_handshake(tmp_path):
    r = Run(tmp_path, "request")
    r.run()
    notes = r.notes()
    sent = [n for n in notes if n["event"] == "handshake"][0]["nonce"]
    assert HEX32.fullmatch(sent)
    derived = [n for n in notes if n["event"] == "nonce"][0]
    assert derived["spool"] == sent
    assert derived["cgroup"] == "m-" + sent
    assert derived["launch"] == sent
    assert r.hooks[0][0] == "m-" + sent

    # 別の mission では別の値
    (tmp_path / "b").mkdir()
    r2 = Run(tmp_path / "b", "request")
    r2.run()
    other = [n for n in r2.notes() if n["event"] == "handshake"][0]["nonce"]
    assert other != sent


@pytest.mark.parametrize("bad", ["AB" * 16, "ab" * 15, "zz" * 16, 12, None, "ab" * 17])
def test_malformed_nonce_is_ready_not_ok(monkeypatch, bad):
    from agentic_fx import mission_worker

    frame = json.dumps({"type": "handshake", "seq": 1, MP.HANDSHAKE_NONCE_FIELD: bad,
                        "expected_parent_pid": os.getppid()}).encode() + b"\n"
    monkeypatch.setattr(mission_worker.sys, "stdin",
                        type("S", (), {"buffer": io.BytesIO(frame)})())
    captured = io.BytesIO()
    monkeypatch.setattr(mission_worker, "_protect_protocol_stdout", lambda: captured)
    mission_worker.main()
    lines = captured.getvalue().splitlines()
    assert len(lines) == 1
    sent = json.loads(lines[0])
    assert sent["type"] == "ready" and sent["ok"] is False
    assert MP.HANDSHAKE_NONCE_FIELD in sent["error"]
    # main() の外には値を残さない
    assert mission_worker._mission_nonce is None


def test_mission_nonce_is_cleared_after_main(monkeypatch):
    """handshake で受けた nonce は main() の間だけ保持し、終わったら消す。"""
    from agentic_fx import mission_worker

    nonce = "ab" * 16
    seen: list = []
    accept = mission_worker._accept_mission_nonce

    def spy(handshake: dict):
        got = accept(handshake)
        seen.append(mission_worker._mission_nonce)
        return got

    frame = json.dumps({"type": "handshake", "seq": 1, MP.HANDSHAKE_NONCE_FIELD: nonce,
                        "expected_parent_pid": -1}).encode() + b"\n"
    monkeypatch.setattr(mission_worker.sys, "stdin",
                        type("S", (), {"buffer": io.BytesIO(frame)})())
    monkeypatch.setattr(mission_worker, "_protect_protocol_stdout", lambda: io.BytesIO())
    monkeypatch.setattr(mission_worker, "_accept_mission_nonce", spy)
    monkeypatch.setattr(mission_worker, "_set_pdeathsig", lambda sig: None)
    mission_worker.main()
    assert [n.hex for n in seen] == [nonce]
    assert mission_worker._mission_nonce is None


@pytest.mark.parametrize("profile", ["trade", "improve"])
def test_handshake_carries_nonce_for_every_profile(tmp_path, monkeypatch, profile):
    """既定の WorkerRunner (cleanup protocol なし) も全 profile で nonce を送る。"""
    seen: list[dict] = []
    r_fd, w_fd = os.pipe()
    r2_fd, w2_fd = os.pipe()

    def child() -> None:
        cin = os.fdopen(r2_fd, "rb")
        cout = os.fdopen(w_fd, "wb")
        seen.append(MP.read_frame(cin))
        MP.write_frame(cout, {"type": "ready", "seq": 1, "ok": False, "error": "x"})
        cout.close()
        # 親が stdin を閉じるまでに送った frame をすべて読む
        while (frame := MP.read_frame(cin)) is not None:
            seen.append(frame)

    class Proc:
        pid = 2 ** 22 + 12345  # 存在しない pid (kill は ESRCH)
        stdin = os.fdopen(w2_fd, "wb")
        stdout = os.fdopen(r_fd, "rb")
        returncode = 0

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    t = threading.Thread(target=child, daemon=True)
    t.start()
    runner = WorkerRunner(root=tmp_path, settings=SETTINGS, clock=FixedClock(NOW), rag=None,
                          worker_profile=profile)
    mission = Mission(prompt="p", tools=[], output_schema={}, max_turns=1, timeout_sec=1)
    runner._run_with_child(Proc(), mission, W)
    t.join(5)
    assert HEX32.fullmatch(seen[0][MP.HANDSHAKE_NONCE_FIELD])
    # cleanup protocol を有効にしない WorkerRunner は、どの profile でも deadline を送らない
    assert not t.is_alive()
    assert [f["type"] for f in seen if f["type"] == MP.CLEANUP_DEADLINE] == []


def test_cleanup_protocol_is_not_for_trade(tmp_path):
    with pytest.raises(ValueError):
        WorkerRunner(root=tmp_path, settings=SETTINGS, clock=FixedClock(NOW), rag=None,
                     worker_profile="trade", cleanup_protocol=MC.CleanupProtocol())


# --- 既定経路の不変 ----------------------------------------------------------------

def test_default_worker_runner_path_unchanged(tmp_path, monkeypatch):
    """cleanup protocol を有効にしない WorkerRunner は deadline frame を送らず、既存の
    `_ensure_dead` → `_kill` の終了経路のまま。cleanup 系の frame は未知の型として扱う。"""
    from agentic_fx.runners import worker_runner as wr_mod

    calls: list[str] = []
    orig_ensure, orig_kill = WorkerRunner._ensure_dead, WorkerRunner._kill
    monkeypatch.setattr(WorkerRunner, "_ensure_dead",
                        lambda self, p, w: (calls.append("ensure_dead"), orig_ensure(self, p, w)))
    monkeypatch.setattr(WorkerRunner, "_kill",
                        lambda self, p: (calls.append("kill"), orig_kill(self, p)))
    issued: list = []
    monkeypatch.setattr(MC.DeadlineIssuer, "issue",
                        lambda self, trigger: issued.append(trigger))
    out = tmp_path / "d.jsonl"
    proc = subprocess.Popen([sys.executable, str(HELPER), "request", str(out)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, start_new_session=True,
                            preexec_fn=_preexec)
    runner = WorkerRunner(root=tmp_path, settings=SETTINGS, clock=FixedClock(NOW), rag=None,
                          worker_profile="improve")
    mission = Mission(prompt="p", tools=[], output_schema={}, max_turns=1, timeout_sec=5)
    try:
        result = runner._run_with_child(proc, mission, W)
    finally:
        if proc.returncode is None:
            proc.kill()
            proc.wait(10)
    # 既定経路では cleanup_request は未知の frame (protocol_error) で、deadline は送らない
    assert result.status == "failed"
    assert result.reason == "worker protocol_error"
    assert issued == []
    notes = [json.loads(x) for x in out.read_text().splitlines()]
    assert not _deadline_frames(notes)
    assert calls[:2] == ["ensure_dead", "kill"]
    assert wr_mod.KILL_WAIT_SEC == 5.0
