"""AC-14: 稼働中の daemon の実 tick (``_scheduler_tick_once``: 実 core_lock・executor・
scheduler) の遅延が、API への slowloris 4 本で乱れない。

run_service を実際に回し (listener・scheduler thread・watchdog・decide worker が動いている
状態)、その中で service の tick 本体を 200 回ずつ測る。scheduler の実 loop は 60 秒間隔
なので 200 tick を loop の周期で待つことはせず、loop が呼ぶのと同じ関数を直接呼ぶ。
攻撃側は別 process。CPU 負荷の条件は CPU 数の半分の busy process を並走させる。
判定式: 同じ CPU 条件の基準に対し p95 +20 ms、p99 +50 ms 以内、最大 500 ms 以内。
"""
from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import yfinance

from agentic_fx import service as service_mod

from ._daemon import socket_path
from .test_service_api_wiring import _app, _run, _settings, _wait_for
from tests.test_service_app import _init

SLOWLORIS = Path(__file__).with_name("_slowloris_child.py")
TICKS = 200


def _no_core() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _offline(*_args, **_kwargs):
    raise ConnectionError("network is disabled in this test")


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))]


def _measure(app, n: int, gap: float = 0.03) -> list[float]:
    # 200 tick の測定窓を要求 deadline (5 秒) より長くし、slowloris が 408 で切られて
    # 張り直される区間も測定に入れる。
    out = []
    for _ in range(n):
        started = time.perf_counter()
        service_mod._scheduler_tick_once(app)
        out.append(time.perf_counter() - started)
        time.sleep(gap)
    return out


def _busy_processes(count: int) -> list[subprocess.Popen]:
    return [subprocess.Popen([sys.executable, "-c", "while True:\n    pass\n"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, preexec_fn=_no_core)
            for _ in range(count)]


def _stop(processes: list[subprocess.Popen]) -> None:
    for proc in processes:
        if proc.poll() is None:
            proc.kill()
    for proc in processes:
        proc.wait(10)


@pytest.mark.parametrize("cpu_load", [False, True], ids=["idle", "cpu_half_busy"])
def test_ac14_real_tick_latency_is_not_disturbed_by_slowloris(tmp_path, cpu_load):
    _init(tmp_path)
    (tmp_path / "plugins" / ".locks").mkdir(parents=True, exist_ok=True)
    _settings(tmp_path, enabled=True)
    app = _app(tmp_path)
    result: dict = {}
    busy: list[subprocess.Popen] = []

    def exercise() -> None:
        _wait_for(socket_path(tmp_path))
        _measure(app, 20, 0.002)  # 初回 tick (scheduler thread) と cache の立ち上がりを捨てる
        baseline = _measure(app, TICKS)
        stop = tmp_path / "stop-slowloris"
        attacker = subprocess.Popen(
            [sys.executable, str(SLOWLORIS), str(socket_path(tmp_path)), "4", str(stop)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            stdin=subprocess.DEVNULL, preexec_fn=_no_core)
        try:
            assert attacker.stdout.readline().strip() == "ready"
            time.sleep(0.2)
            loaded = _measure(app, TICKS)
            stop.write_text("stop")
            out, err = attacker.communicate(timeout=30)
            assert attacker.returncode == 0, err
        finally:
            if attacker.poll() is None:
                attacker.kill()
                attacker.wait(10)
        result.update(baseline=baseline, loaded=loaded, connections=int(out.split()[-1]))

    try:
        if cpu_load:
            busy = _busy_processes(max(1, (os.cpu_count() or 2) // 2))
        with patch.object(yfinance, "download", _offline), \
             patch.object(yfinance, "Ticker", _offline):
            assert _run(tmp_path, app, until=exercise) == 0
    finally:
        _stop(busy)

    baseline, loaded = result["baseline"], result["loaded"]
    summary = {
        "cpu_load": cpu_load, "busy_processes": len(busy),
        "baseline_ms": {"p50": _pct(baseline, 0.50) * 1e3, "p95": _pct(baseline, 0.95) * 1e3,
                        "p99": _pct(baseline, 0.99) * 1e3, "max": max(baseline) * 1e3},
        "loaded_ms": {"p50": _pct(loaded, 0.50) * 1e3, "p95": _pct(loaded, 0.95) * 1e3,
                      "p99": _pct(loaded, 0.99) * 1e3, "max": max(loaded) * 1e3},
        "slowloris_connections": result["connections"],
    }
    print("AC14 " + json.dumps(summary))
    # 測定中に 408 で切られて張り直した (攻撃が測定窓の全体に掛かっていた)
    assert result["connections"] > 4
    assert _pct(loaded, 0.95) - _pct(baseline, 0.95) <= 0.020, summary
    assert _pct(loaded, 0.99) - _pct(baseline, 0.99) <= 0.050, summary
    assert max(loaded) <= 0.5, summary
