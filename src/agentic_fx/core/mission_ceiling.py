"""判断 mission がスロットを占有し得る上限。

worker 1 回の上限 Cw は、子の起動待ち・実行 deadline・SIGTERM 猶予・
SIGKILL 後の wait (finally での再実行分を含む)・reader/dispatcher の join・
(backend=claude なら) CLI の回収猶予を直列に足した保守的な上界。
dispatch 全体の上限 Cd は trade 1 回と reflection 最大 3 回を同じ Cw で
見積もり、lock/SQL 等の未計測分に固定の余白を足す。watchdog と停止時の
join 予算はこの Cd に検出遅延の余裕を足した 1 本の値を共有する。
終了を保証するのは worker の deadline と SIGKILL であり、tool の予算ではない。
"""
from __future__ import annotations

from dataclasses import dataclass

# WorkerRunner の打ち切り経路の固定待ち (runners/worker_runner.py が参照する)。
KILL_WAIT_SEC = 5.0
READER_JOIN_SEC = 5.0
DISPATCHER_JOIN_MARGIN_SEC = 5.0
# MissionSupervisor._dispatch は trade の後に ReflectionCycle.run_pending()
# (既定 max_items=3) を同じ dispatch 内で呼ぶ。
REFLECTIONS_PER_DISPATCH = 3
DISPATCH_MARGIN_SEC = 10.0
WATCHDOG_MARGIN_SEC = 60.0


@dataclass(frozen=True)
class WorkerCeiling:
    startup: float
    deadline: float
    terminate: float
    kill_wait: float
    kill_wait_retry: float
    reader_join: float
    dispatcher_join: float
    cli_terminate: float

    @property
    def total(self) -> float:
        return (self.startup + self.deadline + self.terminate + self.kill_wait
                + self.kill_wait_retry + self.reader_join
                + self.dispatcher_join + self.cli_terminate)

    def breakdown_text(self) -> str:
        return (f"起動待ち {self.startup:g} + 実行 deadline {self.deadline:g}"
                f" + SIGTERM 猶予 {self.terminate:g}"
                f" + SIGKILL 後 wait {self.kill_wait:g}"
                f" + wait 再実行 {self.kill_wait_retry:g}"
                f" + reader.join {self.reader_join:g}"
                f" + dispatcher.join {self.dispatcher_join:g}"
                f" + CLI 回収 {self.cli_terminate:g}")


def worker_ceiling(settings) -> WorkerCeiling:
    w = settings.worker
    cli = (settings.runner.cli_terminate_grace_sec
           if settings.runner.trade.backend == "claude" else 0.0)
    return WorkerCeiling(
        startup=float(w.worker_startup_timeout_sec),
        deadline=float(settings.llama_swap.timeout_sec + w.worker_grace_sec),
        terminate=float(w.worker_terminate_grace_sec),
        kill_wait=KILL_WAIT_SEC,
        kill_wait_retry=KILL_WAIT_SEC,
        reader_join=READER_JOIN_SEC,
        dispatcher_join=float(w.rpc_timeout_sec + DISPATCHER_JOIN_MARGIN_SEC),
        cli_terminate=float(cli))


def dispatch_ceiling_sec(settings) -> float:
    cw = worker_ceiling(settings).total
    return cw * (1 + REFLECTIONS_PER_DISPATCH) + DISPATCH_MARGIN_SEC


def watchdog_ceiling_sec(settings) -> float:
    explicit = settings.worker.dispatch_ceiling_sec
    if explicit is not None:
        return float(explicit)
    return dispatch_ceiling_sec(settings) + WATCHDOG_MARGIN_SEC
