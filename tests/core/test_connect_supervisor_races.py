"""stop の観測点と通知の処理が競る場面を、実 seccomp user-notif で確かめる。

stop の線形化点は lock 下の RUNNING → STOPPING。stop がどの観測点の後に入ったかで
tracee の受ける値が決まる:

| 観測点 | tracee の値 | proxy 役の accept |
|---|---|---|
| poll 復帰後、RECV 前 | ENOSYS (listener の close) | 0 |
| RECV・登録後、handler 起動前 | EPERM (receiver が応答) | 0 |
| commit 前 | EPERM (handler が応答) | 0 |
| commit 後 | connect の実値 | 1 |
"""
from __future__ import annotations

import collections
import errno
import os
import sys
import threading
import time

import pytest

from agentic_fx.core import connect_supervisor as CS
from tests.core._notif_tracee import Factory, Gate
from tests.core.test_connect_supervisor import (
    ImmediateAck, Listener, fd_table, leaked, require_kernel, special_fds)

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.uname().machine != "x86_64",
    reason="connect supervisor は x86_64 Linux 専用")

ENOSYS, EPERM = -errno.ENOSYS, -errno.EPERM

# (観測点, 期待値, 応答した者)
SEAMS = [
    ("after_poll", ENOSYS, None),
    ("after_register", EPERM, "receiver"),
    ("before_commit", EPERM, "handler"),
    ("after_commit", 0, "handler"),
]
# 4 種の結果。stop がその観測点か後で入ったときに起こりうるもの
OUTCOMES = ("ENOSYS", "EPERM_receiver", "EPERM_handler", "actual")
ALLOWED_AFTER = {
    "after_poll": set(OUTCOMES),
    "after_register": {"EPERM_receiver", "EPERM_handler", "actual"},
    "before_commit": {"EPERM_handler", "actual"},
    "after_commit": {"actual"},
}


@pytest.fixture(scope="module")
def factory():
    require_kernel()
    f = Factory()
    yield f
    f.close()


@pytest.fixture
def proxy():
    p = Listener()
    yield p
    p.close()


def _outcome(rc: int, records) -> str:
    if rc == ENOSYS:
        return "ENOSYS"
    if rc == EPERM:
        by = {r.get("by") for r in records if r.get("result") == EPERM}
        assert len(by) == 1, list(records)
        return f"EPERM_{by.pop()}"
    assert rc == 0, rc
    return "actual"


@pytest.mark.parametrize("seam, want, by", SEAMS + [("before_send", 0, "handler")],
                         ids=[s[0] for s in SEAMS] + ["before_send"])
def test_four_stop_seams(factory, proxy, seam, want, by):
    before = fd_table()
    gate = Gate(seam)
    t = factory.spawn()
    sup = CS.ConnectSupervisor(CS.Destinations(proxy.port), proxy_handoff=ImmediateAck(),
                               faults=CS.Faults(at=gate))
    try:
        sup.handoff(t.take_listener())
        cid = t.start("connect", port=proxy.port)
        assert gate.wait()
        box = {}
        th = threading.Thread(target=lambda: box.update(r=sup.stop(time.monotonic() + 5)))
        th.start()
        end = time.monotonic() + 2
        while sup.state == CS.RUNNING and time.monotonic() < end:
            time.sleep(0.001)
        assert sup.state == CS.STOPPING
        gate.release()
        th.join(6)
        assert box["r"] == CS.CLOSED
        assert sup.closed_at is not None and sup.closed_at - sup.stop_requested_at < 1.0
        r = t.result(cid)
        assert r["rc"] == want, (r, list(sup.records))
        if by is not None:
            assert {x.get("by") for x in sup.records if x.get("result") == want} == {by}
        assert proxy.drain() == (1 if want == 0 else 0)
        assert sup.failure is None
        # CLOSED の後の connect は ENOSYS
        assert t.call("connect", port=proxy.port)["rc"] == ENOSYS
        assert sup.join(5)
        sup.release()
    finally:
        t.close()
    assert not leaked(before, fd_table())
    assert special_fds() == []


ITERATIONS = 1000


@pytest.mark.slow
@pytest.mark.parametrize("seam", [s[0] for s in SEAMS])
def test_four_stop_seams_shifted_1000_times(factory, proxy, seam, record_property):
    """各観測点で stop の時刻を 1,000 通りずらす (観測点での保持 10 通り × 観測点到達から
    stop までの遅れ 100 通り)。結果は表の 4 種のどれかで、観測点より前の結果は出ず、
    accept は実値のときだけ 1 件、必ず CLOSED で終わる。"""
    before = fd_table()
    ack = ImmediateAck()
    dist: collections.Counter[str] = collections.Counter()
    t0 = time.monotonic()
    for i in range(ITERATIONS):
        hold = (i % 10) * 20e-6
        delay = (i // 10) * 10e-6
        reached = threading.Event()

        def at(point, nid, reached=reached, hold=hold):
            if point == seam and not reached.is_set():
                reached.set()
                if hold:
                    time.sleep(hold)

        t = factory.spawn()
        sup = CS.ConnectSupervisor(CS.Destinations(proxy.port), proxy_handoff=ack,
                                   faults=CS.Faults(at=at))
        try:
            sup.handoff(t.take_listener())
            accepted0 = proxy.accepted
            cid = t.start("connect", port=proxy.port)
            assert reached.wait(5), i
            if delay:
                time.sleep(delay)
            state = sup.stop(time.monotonic() + 5)
            assert state in (CS.CLOSED, CS.STUCK)
            assert state == CS.CLOSED, (i, list(sup.records))
            r = t.result(cid, 5)
            assert r is not None, i
            kind = _outcome(r["rc"], sup.records)
            assert kind in ALLOWED_AFTER[seam], (i, kind, list(sup.records))
            proxy.drain()
            assert proxy.accepted - accepted0 == (1 if kind == "actual" else 0), (i, kind)
            assert sup.failure is None
            dist[kind] += 1
            assert sup.join(5)
            sup.release()
        finally:
            t.close()
    elapsed = time.monotonic() - t0
    record_property("distribution", dict(dist))
    record_property("elapsed_sec", round(elapsed, 2))
    print(f"\n[{seam}] {dict(dist)} {elapsed:.1f}s")
    assert sum(dist.values()) == ITERATIONS
    assert not leaked(before, fd_table())
    assert special_fds() == []
