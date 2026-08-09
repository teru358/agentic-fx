import threading
import time

from agentic_fx.service import (
    _busy_resources_after_join, _check_watchdog_health,
    _default_dispatch_ceiling_sec, _exit_code, _record_fatal, _watchdog_check,
)


class _Activity:
    def __init__(self): self.events = []
    def write(self, *args): self.events.append(args)


class _Notifier:
    def send(self, message): pass


def _minimal_app():
    class Worker: worker_grace_sec = 3.0; worker_terminate_grace_sec = 2.0
    class Llama: timeout_sec = 42.0
    class Settings: worker = Worker(); llama_swap = Llama()
    class Supervisor:
        heartbeat = time.monotonic()
        busy_since = None
        def is_alive(self): return True
        def fail_pending(self, *, exc): self.failed = exc
    app = type("AppStub", (), {})()
    app.settings = Settings()
    app.supervisor = Supervisor()
    app.activity = _Activity()
    app.notifier = _Notifier()
    app.fatal_reason = None
    app.watchdog_heartbeat = time.monotonic()
    app.mission_watch = type("Watch", (), {"breached": lambda self, grace_sec: None})()
    return app


def test_record_fatal_preserves_first_reason_and_sets_stop():
    app = _minimal_app(); stop = threading.Event()
    _record_fatal(app, stop, "first")
    _record_fatal(app, stop, "second")
    assert app.fatal_reason == "first"
    assert stop.is_set()


def test_dispatch_ceiling_and_join_budget_share_same_lower_bound():
    app = _minimal_app()
    ceiling = _default_dispatch_ceiling_sec(app)
    join_budget = _default_dispatch_ceiling_sec(app)
    assert join_budget >= ceiling
    assert ceiling == (42.0 + 3.0 + 2.0) * 4 + 60.0


def test_watchdog_detects_dead_supervisor_and_fails_pending():
    app = _minimal_app(); stop = threading.Event()
    app.supervisor.is_alive = lambda: False
    alive = type("Thread", (), {"is_alive": lambda self: True})()
    _watchdog_check(app, alive, stop)
    assert stop.is_set()
    assert isinstance(app.supervisor.failed, RuntimeError)


def test_watchdog_detects_stale_busy_dispatch_with_fresh_heartbeat():
    app = _minimal_app(); stop = threading.Event()
    app.supervisor.busy_since = time.monotonic() - 100.0
    alive = type("Thread", (), {"is_alive": lambda self: True})()
    _watchdog_check(app, alive, stop, dispatch_ceiling_sec=10.0)
    assert stop.is_set()
    assert "dispatch exceeded" in app.fatal_reason


def test_scheduler_side_detects_dead_watchdog():
    app = _minimal_app(); stop = threading.Event()
    dead = type("Thread", (), {"is_alive": lambda self: False})()
    _check_watchdog_health(app, dead, stop)
    assert stop.is_set()
    assert app.fatal_reason == "watchdog thread is dead"


def test_busy_resource_mapping_and_exit_code():
    assert _busy_resources_after_join(False, True) == frozenset(
        {"conn_core", "conn_supervisor"})
    app = _minimal_app(); app.fatal_reason = "fatal"
    assert _exit_code(app, False, False) == 1
