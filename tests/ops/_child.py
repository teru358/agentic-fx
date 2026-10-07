"""ops テストの子 process。第 1 引数で役割を選ぶ。

core を残さないよう、最初に RLIMIT_CORE を 0 にする。
"""
from __future__ import annotations

import fcntl
import os
import resource
import sys
import time
from pathlib import Path

resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def latch_loop(state_path: str, count: str) -> None:
    from agentic_fx.store.state import StateStore
    store = StateStore(Path(state_path))
    for _ in range(int(count)):
        store.update(kill_switch_latched=True)


def hold_state_lock(state_path: str, ready: str, seconds: str) -> None:
    fd = os.open(state_path + ".lock", os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    Path(ready).write_text("ready")
    time.sleep(float(seconds))


def hold_state_lock_shared(state_path: str, ready: str, seconds: str) -> None:
    """読者として共有 lock を持ち続ける (読みは通り、書込みだけが待たされる)。"""
    fd = os.open(state_path + ".lock", os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_SH)
    Path(ready).write_text("ready")
    time.sleep(float(seconds))


def hold_plugin_lock(plugins_root: str, name: str, ready: str, seconds: str) -> None:
    from agentic_fx.plugin.switch import _plugin_lock
    with _plugin_lock(Path(plugins_root), name):
        Path(ready).write_text("ready")
        time.sleep(float(seconds))


def core_writer(db_path: str, count: str, interval: str, out: str) -> None:
    """取引側の書込みを模した、100 ms 間隔の短い書込み。busy と所要時間を記録する。"""
    from agentic_fx.store import db
    conn = db.connect(Path(db_path))
    busy, worst = 0, 0.0
    for n in range(int(count)):
        started = time.monotonic()
        try:
            conn.execute("INSERT INTO cron_cursor(pair,interval,bar_time,updated_at) "
                         "VALUES (?,?,?,?) ON CONFLICT(pair,interval) DO UPDATE SET "
                         "bar_time=excluded.bar_time, updated_at=excluded.updated_at",
                         (f"CORE{n % 3}", "1h", str(n), str(n)))
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            if "locked" in str(exc) or "busy" in str(exc):
                busy += 1
            else:
                raise
        worst = max(worst, time.monotonic() - started)
        time.sleep(float(interval))
    Path(out).write_text(f"{busy} {worst}")


if __name__ == "__main__":
    globals()[sys.argv[1]](*sys.argv[2:])
