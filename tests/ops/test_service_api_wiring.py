"""daemon の配線: 起動順序、api.enabled、鍵の補完、起動失敗の記録、停止順序。

AC-5 / 15 / 27 (service 側) / 41 / 50 (起動順序)。実 build_app と run_service の
テスト用シーム (`_stop_event`) を使う。HOME は conftest が tmp_path に向けている。
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
import yaml

from agentic_fx import service as service_mod
from agentic_fx.ops import keys
from agentic_fx.ops.api_server import ApiServer
from agentic_fx.ops.contracts import Principal
from agentic_fx.ops.service import OpsService
from agentic_fx.plugin import switch
from agentic_fx.runners.fake_runner import FakeRunner
from tests.store.test_rag import FakeEmbedding
from tests.test_service_app import NOW, _init, _no_real_network


class _Notifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)


def _settings(root: Path, **api) -> None:
    path = root / "config" / "settings.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["api"].update(api)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def _app(root: Path):
    from agentic_fx.core.contracts import FixedClock
    app = service_mod.build_app(root, runner=FakeRunner([]), clock=FixedClock(NOW),
                                embedding_fn=FakeEmbedding())
    app.scheduler.on_improve_tick = None
    app.notifier = _Notifier()
    return app


def _run(root: Path, app, *, until=None) -> int:
    """run_service を daemon で回す。until があれば別 thread で実行して終わったら止める。"""
    stop = threading.Event()
    with _no_real_network(), \
         patch("agentic_fx.service.build_app", return_value=app), \
         patch("agentic_fx.service.signal.signal"):
        if until is None:
            stop.set()
            return service_mod.run_service(root, daemon=True, _stop_event=stop)
        box = {}
        thread = threading.Thread(target=lambda: box.setdefault(
            "rc", service_mod.run_service(root, daemon=True, _stop_event=stop)))
        thread.start()
        try:
            until()
        finally:
            stop.set()
            thread.join(120)
        assert not thread.is_alive()
        return box["rc"]


def _activity(root: Path) -> str:
    return (root / "logs" / "activity.log").read_text(encoding="utf-8")


def _socket(root: Path) -> Path:
    return root / "data" / "run" / "api.sock"


def _wait_for(path: Path, timeout: float = 30.0) -> None:
    end = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < end, f"{path} did not appear"
        time.sleep(0.05)


# ------------------------------------------------------------ AC-50 起動順序

def test_ac50_startup_order_reconcile_sweep_expire_recover_then_listen(tmp_path, monkeypatch):
    _init(tmp_path)
    _settings(tmp_path, enabled=True)
    order: list[str] = []

    def spy(name, original):
        def wrapper(*args, **kwargs):
            order.append(name)
            return original(*args, **kwargs)
        return wrapper

    monkeypatch.setattr(switch, "reconcile_switch_journals",
                        spy("reconcile", switch.reconcile_switch_journals))
    monkeypatch.setattr(switch, "sweep_orphans", spy("sweep", switch.sweep_orphans))
    monkeypatch.setattr(switch, "process_expired_approvals",
                        spy("expire", switch.process_expired_approvals))
    monkeypatch.setattr(OpsService, "recover_after_journal",
                        spy("recover", OpsService.recover_after_journal))
    monkeypatch.setattr(keys, "load_keyset", spy("keys", keys.load_keyset))
    monkeypatch.setattr(ApiServer, "start", spy("listen", ApiServer.start))
    app = _app(tmp_path)
    assert app.commands.ops_service is app.ops
    assert app.ops_recovered is True
    assert _run(tmp_path, app) == 0
    assert order == ["reconcile", "sweep", "expire", "recover", "keys", "listen"]
    assert "api_started" in _activity(tmp_path)


def test_ac50_unfinished_request_is_terminated_before_the_listener_opens(tmp_path):
    from agentic_fx.ops.audit import AuditStore
    from agentic_fx.store.db import connect, init_db
    from datetime import datetime, timezone
    _init(tmp_path)
    _settings(tmp_path, enabled=True)
    conn = connect(tmp_path / "data" / "agentic.db")
    init_db(conn)
    accepted = AuditStore(conn, wall_clock=lambda: datetime.now(timezone.utc)).accept(
        endpoint="POST /v1/backlog/{id}/note", principal=Principal.OPERATOR,
        asserted_actor=None, peer_pid=None, peer_exe=None, body={"id": 1},
        target_ref="backlog:1")
    conn.close()
    seen = {}
    original = ApiServer.start

    def start(self):
        probe = connect(tmp_path / "data" / "agentic.db")
        seen["terminal"] = probe.execute(
            "SELECT phase,result_code FROM ops_requests WHERE target_ref=?",
            (f"audit:{accepted.audit_id}",)).fetchone()
        probe.close()
        return original(self)

    with patch.object(ApiServer, "start", start):
        app = _app(tmp_path)
        assert _run(tmp_path, app) == 0
    assert tuple(seen["terminal"]) == ("outcome_unknown", "outcome_unknown")


def test_ac50_failed_recovery_keeps_listener_closed_and_daemon_running(tmp_path, monkeypatch):
    _init(tmp_path)
    _settings(tmp_path, enabled=True)

    def broken(self):
        raise RuntimeError("db gone")

    monkeypatch.setattr(OpsService, "recover_after_journal", broken)
    app = _app(tmp_path)
    assert app.ops_recovered is False
    assert _run(tmp_path, app) == 0
    assert not _socket(tmp_path).exists()
    assert "api_start_failed" in _activity(tmp_path)
    assert any("ops_recovery_failed" in reason for reason in app.health_latch.summary())


# ------------------------------------------------------------ api.enabled

def test_api_disabled_by_default_warns_and_opens_nothing(tmp_path):
    _init(tmp_path)
    app = _app(tmp_path)
    assert app.settings.api.enabled is False
    assert _run(tmp_path, app) == 0
    assert "api_disabled" in _activity(tmp_path)
    assert not _socket(tmp_path).exists()
    assert not keys.key_dir(tmp_path).exists()
    assert not app.health_latch.is_latched()


@pytest.mark.parametrize("value", [MagicMock(), 1, "true", "yes", None])
def test_api_enabled_must_be_the_bool_true_to_open_anything(tmp_path, value):
    _init(tmp_path)
    app = _app(tmp_path)
    app.settings = SimpleNamespace(api=SimpleNamespace(
        enabled=value, socket_path="data/run/api.sock"))
    service_mod.start_ops_api(app, tmp_path)
    assert not _socket(tmp_path).exists()
    assert not keys.key_dir(tmp_path).exists()
    assert "api_disabled" in _activity(tmp_path)


# ------------------------------------------------------------ build_app の失敗

def _is_closed(conn) -> bool:
    import sqlite3
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False




def test_build_app_failure_after_the_ops_service_closes_its_connections(tmp_path, monkeypatch):
    from agentic_fx.commands import Commands
    from agentic_fx.core.contracts import FixedClock
    _init(tmp_path)
    created = []
    original = OpsService.__init__

    def capture(self, *args, **kwargs):
        original(self, *args, **kwargs)
        created.append(self)

    def broken(self, ops_service):
        raise RuntimeError("wiring failed")

    monkeypatch.setattr(OpsService, "__init__", capture)
    monkeypatch.setattr(Commands, "use_ops_service", broken)
    with pytest.raises(RuntimeError, match="wiring failed"):
        service_mod.build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                              embedding_fn=FakeEmbedding())
    assert len(created) == 1
    ops = created[0]
    assert _is_closed(ops._conn.raw)
    assert _is_closed(ops._decide_conn.raw)


def test_build_app_failure_before_the_ops_service_closes_conn_ops(tmp_path, monkeypatch):
    from agentic_fx.core.contracts import FixedClock
    _init(tmp_path)
    opened = []
    real_connect = service_mod.connect

    def spy(path, *args, **kwargs):
        conn = real_connect(path, *args, **kwargs)
        opened.append(conn)
        return conn

    def broken(*args, **kwargs):
        raise RuntimeError("ops build failed")

    monkeypatch.setattr(service_mod, "connect", spy)
    monkeypatch.setattr(service_mod, "_build_ops_service", broken)
    with pytest.raises(RuntimeError, match="ops build failed"):
        service_mod.build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                              embedding_fn=FakeEmbedding())
    assert _is_closed(opened[-1])


# ------------------------------------------------------------ data resume の busy

def _hold_exclusive(root: Path):
    import sqlite3
    holder = sqlite3.connect(root / "data" / "agentic.db", isolation_level=None,
                             check_same_thread=False)
    holder.execute("BEGIN EXCLUSIVE")
    return holder


def _busy_after_accept(app, root: Path, holders: list, *, after=None) -> None:
    """accepted を書いた後 (= callback に入った時点) から DB を占有する。"""
    original = app.ops._data_resume

    def wrapped(acknowledge):
        holders.append(_hold_exclusive(root))
        if after is not None:
            after()
        return original(acknowledge)

    app.ops._data_resume = wrapped


def _release(holders: list) -> None:
    for holder in holders:
        holder.execute("ROLLBACK")
        holder.close()


def test_ac24_data_resume_busy_is_database_busy_within_the_request_deadline(tmp_path):
    from agentic_fx.ops import ErrorCode, OpsError
    from agentic_fx.ops.contracts import ShellCaller
    _init(tmp_path)
    app = _app(tmp_path)
    holders: list = []
    _busy_after_accept(app, tmp_path, holders)
    try:
        started = time.monotonic()
        with pytest.raises(OpsError) as raised:
            app.ops.resume_data(ShellCaller.SHELL, acknowledge=True,
                                deadline=time.monotonic() + 0.5)
        elapsed = time.monotonic() - started
    finally:
        _release(holders)
    assert raised.value.code is ErrorCode.DATABASE_BUSY
    # 要求 deadline (0.5 s) の後に、終端を書く独立の予算 (既定 2 s) だけ待つ。
    assert elapsed <= 4.0, elapsed


def test_ac24_data_resume_stop_during_busy_is_unavailable_quickly(tmp_path):
    from agentic_fx.ops import ErrorCode, OpsError
    from agentic_fx.ops.contracts import ShellCaller
    _init(tmp_path)
    app = _app(tmp_path)
    holders: list = []
    stopped_at = {}

    def stop_soon():
        stopped_at["t"] = time.monotonic()
        app.ops.stop_event.set()

    _busy_after_accept(app, tmp_path, holders, after=stop_soon)
    try:
        with pytest.raises(OpsError) as raised:
            app.ops.resume_data(ShellCaller.SHELL, acknowledge=True,
                                deadline=time.monotonic() + 20.0)
        after_stop = time.monotonic() - stopped_at["t"]
    finally:
        _release(holders)
    assert raised.value.code is ErrorCode.UNAVAILABLE
    assert after_stop <= 0.5, after_stop


def test_ac24_data_resume_short_busy_is_waited_out(tmp_path):
    from agentic_fx.ops.contracts import ShellCaller
    _init(tmp_path)
    app = _app(tmp_path)
    holders: list = []
    _busy_after_accept(app, tmp_path, holders,
                       after=lambda: threading.Timer(0.4, lambda: _release(holders)).start())
    reply = app.ops.resume_data(ShellCaller.SHELL, acknowledge=True,
                                deadline=time.monotonic() + 5.0)
    assert reply["requested"] is True


def test_ac24_data_resume_and_core_writes_do_not_hit_sqlite_busy(tmp_path):
    from agentic_fx.ops.contracts import ShellCaller
    from .test_ac24_34_contention_and_tail import _spawn
    _init(tmp_path)
    app = _app(tmp_path)
    out = tmp_path / "core-writer"
    child = _spawn("core_writer", tmp_path / "data" / "agentic.db", 40, 0.1, out)
    for _ in range(20):
        reply = app.ops.resume_data(ShellCaller.SHELL, acknowledge=True)
        assert reply["requested"] is True
        time.sleep(0.05)
    assert child.wait(60) == 0
    busy, worst = out.read_text().split()
    assert int(busy) == 0 and float(worst) <= 1.0


# ------------------------------------------------------------ AC-15 / AC-5 / AC-27

def test_ac15_regular_file_at_socket_path_records_failure_and_daemon_runs(tmp_path):
    _init(tmp_path)
    _settings(tmp_path, enabled=True)
    _socket(tmp_path).parent.mkdir(parents=True, mode=0o700)
    _socket(tmp_path).write_text("occupied")
    app = _app(tmp_path)
    ticks = []
    original_tick = service_mod._scheduler_tick_once

    def tick(app_):
        ticks.append(1)
        return original_tick(app_)

    with patch("agentic_fx.service._scheduler_tick_once", tick):
        rc = _run(tmp_path, app, until=lambda: _wait_until(lambda: ticks))
    assert rc == 0 and ticks
    assert "api_start_failed" in _activity(tmp_path)
    assert "socket_path_occupied" in _activity(tmp_path)
    assert any("api_start_failed" in r for r in app.health_latch.summary())
    assert any("socket_path_occupied" in t for t in app.notifier.sent)
    assert _socket(tmp_path).read_text() == "occupied"


def _wait_until(predicate, timeout: float = 30.0) -> None:
    end = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < end
        time.sleep(0.05)


@pytest.mark.parametrize("home", ["/etc", "/proc", "venv"])
def test_ac5_token_dir_in_sandbox_does_not_start_api(tmp_path, monkeypatch, home):
    import sys
    _init(tmp_path)
    _settings(tmp_path, enabled=True)
    app = _app(tmp_path)
    monkeypatch.setenv("HOME", sys.prefix if home == "venv" else home)
    assert _run(tmp_path, app) == 0
    assert "token_dir_in_sandbox" in _activity(tmp_path)
    assert not _socket(tmp_path).exists()
    assert not keys.key_dir(tmp_path).exists()


def test_ac27_service_completes_first_init_and_refuses_uncommitted_rotate(tmp_path):
    _init(tmp_path)
    _settings(tmp_path, enabled=True)
    app = _app(tmp_path)
    assert _run(tmp_path, app) == 0
    directory = keys.key_dir(tmp_path)
    assert (directory / keys.READY_NAME).exists()
    first = keys.read_token(directory, Principal.OPERATOR)

    class Crash(Exception):
        pass

    def crash(step):
        if step == "key:renamed":
            raise Crash

    with pytest.raises(Crash):
        keys.rotate(directory, Principal.OPERATOR, hook=crash)
    app = _app(tmp_path)
    assert _run(tmp_path, app) == 0
    assert "ready_not_committed" in _activity(tmp_path)
    assert not _socket(tmp_path).exists()
    # 自動補完も旧鍵への巻き戻しもしない
    assert (directory / keys.key_file_name(Principal.OPERATOR)).read_text().strip() != first


# ------------------------------------------------------------ AC-41 と停止順序

def test_ac41_api_policy_through_build_app_and_listener_then_clean_stop(tmp_path):
    _init(tmp_path)
    _settings(tmp_path, enabled=True)
    app = _app(tmp_path)
    results = {}

    def exercise():
        _wait_for(_socket(tmp_path))
        token = keys.read_token(keys.key_dir(tmp_path), Principal.OPERATOR)
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(_socket(tmp_path))),
                          base_url="http://afx", timeout=15,
                          headers={"Authorization": f"Bearer {token}"}) as client:
            results["status"] = client.get("/v1/status", headers={"Content-Length": "0"}).json()
            results["policy"] = client.post("/v1/policy", json={"text": "risk small"},
                                            headers={"Idempotency-Key": "p1"}).status_code

    assert _run(tmp_path, app, until=exercise) == 0
    assert results["policy"] == 200
    data = results["status"]["data"]
    assert {"mode", "autopilot", "kill_switch", "balance", "equity", "health",
            "data"} <= set(data)
    directives = (tmp_path / "policy" / "directives.md").read_text(encoding="utf-8")
    assert directives.count("- risk small") == 1
    assert not _socket(tmp_path).exists()  # 停止で listener を閉じた
    assert "service_stopped" in _activity(tmp_path)


def test_stop_order_closes_listener_before_ops_shutdown(tmp_path, monkeypatch):
    _init(tmp_path)
    _settings(tmp_path, enabled=True)
    app = _app(tmp_path)
    order: list[str] = []
    original_close = ApiServer.close_listener
    original_shutdown = OpsService.shutdown
    original_join = ApiServer.join

    def close_listener(self):
        order.append("close_listener")
        return original_close(self)

    def shutdown(self, **kwargs):
        order.append("ops_shutdown")
        return original_shutdown(self, **kwargs)

    def join(self, timeout):
        order.append("api_join")
        return original_join(self, timeout)

    monkeypatch.setattr(ApiServer, "close_listener", close_listener)
    monkeypatch.setattr(OpsService, "shutdown", shutdown)
    monkeypatch.setattr(ApiServer, "join", join)
    assert _run(tmp_path, app) == 0
    assert order[:3] == ["close_listener", "ops_shutdown", "api_join"]


# ------------------------------------------------------------ AC-22 周期 flush

def test_ac22_periodic_flush_timer_runs_while_listener_is_open(tmp_path, monkeypatch):
    _init(tmp_path)
    _settings(tmp_path, enabled=True)
    monkeypatch.setattr(service_mod, "_OPS_FLUSH_INTERVAL_SEC", 0.05)
    calls = []
    original = ApiServer.flush_rejections

    def flush(self):
        calls.append(time.monotonic())
        return original(self)

    monkeypatch.setattr(ApiServer, "flush_rejections", flush)
    app = _app(tmp_path)
    assert _run(tmp_path, app, until=lambda: _wait_until(lambda: len(calls) >= 3)) == 0
    stopped = len(calls)
    time.sleep(0.3)
    assert len(calls) == stopped  # 停止後は timer も止まっている
    assert service_mod._OPS_FLUSH_INTERVAL_SEC == 0.05


def test_ac22_default_flush_interval_is_30_seconds():
    assert service_mod._OPS_FLUSH_INTERVAL_SEC == 30.0
