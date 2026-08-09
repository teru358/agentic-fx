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


def test_dispatch_ceiling_formula():
    """裁定 B: ceiling は trade 1 回 + reflection 最大 3 回を包含する。

    (レビュー1周目 I-3) 旧 `test_dispatch_ceiling_and_join_budget_share_same_
    lower_bound` は `_default_dispatch_ceiling_sec` を 2 回呼んで比較する
    **完全な恒真テスト**だった (実装をどう壊しても検出しない)。順序関係の
    ピンは `test_watchdog_uses_a_ceiling_not_larger_than_the_join_budget`
    が担う。
    """
    app = _minimal_app()
    assert _default_dispatch_ceiling_sec(app) == (42.0 + 3.0 + 2.0) * 4 + 60.0


def test_record_fatal_sets_stop_even_if_activity_write_raises():
    """レビュー1周目 C-1: 記録の失敗で停止のトリガーを失わない。

    無防備だと、呼び出し元 (watchdog/scheduler) は `except Exception` で
    握って周期的に同じ経路を再実行し、毎回同じ行で落ちるため**停止が永久に
    トリガーされない** (fatal_reason だけが立って誰も止まらない)。
    """
    app = _minimal_app(); stop = threading.Event()

    def boom(*args):
        raise OSError("disk full")

    app.activity.write = boom
    _record_fatal(app, stop, "scheduler thread is dead")
    assert stop.is_set(), "activity.write の失敗で stop_event.set() に到達していない"
    assert app.fatal_reason == "scheduler thread is dead"


def test_record_fatal_sets_stop_even_if_notifier_raises():
    """同上の通知側 (適用範囲の両方に当てる — プラン規約)。"""
    app = _minimal_app(); stop = threading.Event()

    def boom(message):
        raise RuntimeError("webhook down")

    app.notifier.send = boom
    _record_fatal(app, stop, "supervisor thread is dead")
    assert stop.is_set()


def test_watchdog_detects_dead_scheduler_before_supervisor():
    """レビュー1周目 I-1: scheduler 死亡は最優先で検出される。

    scheduler が死ぬと以後 tick が回らず**資金保護 (SL/TP 監視) が止まる**。
    この分岐は本 task で最も基幹の検出経路だが、旧テストは全て
    `is_alive() -> True` のスタブしか渡しておらず無ピンだった (分岐を丸ごと
    削除しても全件 1709 passed を指揮者が実測)。
    """
    app = _minimal_app(); stop = threading.Event()
    dead = type("Thread", (), {"is_alive": lambda self: False})()

    _watchdog_check(app, dead, stop)

    assert stop.is_set()
    assert app.fatal_reason == "scheduler thread is dead"
    # supervisor 側の判定より**先**に落ちていること (supervisor は生きて
    # いるので、順序が逆なら fatal_reason は別の理由になる)。
    assert app.supervisor.is_alive() is True


def test_watchdog_detects_stale_supervisor_heartbeat():
    """レビュー1周目 I-1: heartbeat 鮮度の分岐 (is_alive は True のまま)。"""
    app = _minimal_app(); stop = threading.Event()
    app.supervisor.heartbeat = time.monotonic() - 999.0
    alive = type("Thread", (), {"is_alive": lambda self: True})()

    _watchdog_check(app, alive, stop)

    assert stop.is_set()
    assert app.fatal_reason == "supervisor heartbeat stale"


def test_scheduler_side_detects_stale_watchdog_heartbeat():
    """レビュー1周目 I-1: 相互監視の scheduler→watchdog 方向の鮮度分岐。

    watchdog スレッドが生きたまま (例: 例外を握るループの中で) 実質停止した
    場合、`is_alive()` は True のままなので鮮度でしか検出できない。
    """
    app = _minimal_app(); stop = threading.Event()
    app.watchdog_heartbeat = time.monotonic() - 999.0
    alive = type("Thread", (), {"is_alive": lambda self: True})()

    _check_watchdog_health(app, alive, stop)

    assert stop.is_set()
    assert app.fatal_reason == "watchdog heartbeat stale"


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
    # (レビュー1周目 I-4) scheduler 単独 busy のケース。旧テストは
    # (False, True) しか通しておらず、`if scheduler_still_busy or ...` の
    # 左辺を落としても全件緑だった (指揮者が実測) — scheduler が join
    # タイムアウトで still-busy なのに `conn_core` を close してしまう
    # fd-safety の退行を見逃す。
    assert _busy_resources_after_join(True, False) == frozenset({"conn_core"})
    assert _busy_resources_after_join(False, False) == frozenset()
    app = _minimal_app(); app.fatal_reason = "fatal"
    assert _exit_code(app, False, False) == 1


def test_watchdog_check_is_a_noop_once_stopping_has_begun():
    """停止シーケンス中のスレッド終了は fatal ではない。

    (レビュー1周目 I-3 に伴う追加) watchdog が「待ちより先にチェック」を
    するようになった結果、停止中に正常終了しつつあるスレッドを「死亡」と
    誤認しうる — graceful な停止が終了コード 1 になる (実測: 既存 2 テストが
    red になった)。停止の実行主体は常に main であり、停止中の監視は不要。
    """
    app = _minimal_app(); stop = threading.Event()
    stop.set()
    dead = type("Thread", (), {"is_alive": lambda self: False})()
    app.supervisor.is_alive = lambda: False

    _watchdog_check(app, dead, stop)

    assert app.fatal_reason is None, (
        "停止中のスレッド終了を fatal と誤認している")
