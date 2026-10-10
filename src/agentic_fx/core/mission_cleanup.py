"""改善 mission の後始末: 単一の absolute cleanup deadline、WorkerRunner 側の direct-parent
reap、worker 側の deadline 受領と watchdog。

deadline は WorkerRunner だけが `monotonic_now + CLEANUP_BUDGET_SEC` で 1 回だけ発行し、
cleanup の最初の操作より前に worker へ送る。契機は worker の cleanup 要求、worker exit /
result EOF / 無応答、afx 本体の mission deadline、afx 本体の停止開始の早いもの。

- WorkerRunner は ACK や完了通知を待たずに `worker_proc.wait(deadline - 2s - now)`。timeout
  なら worker を SIGKILL し、必ず `worker_proc.wait(deadline - now)` をもう一度行う。それでも
  reap できなければ `worker_not_reaped` (rmdir と次 mission を禁止する)。
- worker は deadline を受けて ACK を返すまで停止と cgroup 操作を始めない。ACK の後、
  deadline の 3 秒前に `os._exit` する独立の watchdog を起こす。watchdog は thread なので、
  GIL を握ったままの停止には効かない (best-effort。その場合は WorkerRunner の SIGKILL が止める)。

EOF・ACK・`populated 0` は worker exit の証拠にしない。worker の exit の証拠は wait の結果だけ。

注入点 (`CleanupProtocol.inject_rewait_timeout`) は constructor 引数だけで渡せる。環境変数や
settings からは入れられない。
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from agentic_fx.core import cgroup_host as ch
from agentic_fx.core import mission_protocol as MP

# --- profile 定数 (settings から変えられない) -----------------------------------
CLEANUP_BUDGET_SEC = 10.0
#: WorkerRunner が worker を SIGKILL する時刻 (deadline の何秒前か)
WORKER_KILL_LEAD_SEC = 2.0
#: worker の watchdog が os._exit する時刻 (deadline の何秒前か)
WATCHDOG_LEAD_SEC = 3.0
WATCHDOG_EXIT_CODE = 86
#: worker が受け取る deadline の上限 (受領時刻からの秒)。正しい発行は必ず予算の内側に
#: あるので、1 秒の余裕は境界での取りこぼしを避けるためだけのもの
DEADLINE_MAX_AHEAD_SEC = CLEANUP_BUDGET_SEC + 1.0
_WATCHDOG_TICK_SEC = 0.05

# --- 固定 reason ---------------------------------------------------------------
REASON_CLEANUP_DEADLINE_EXCEEDED = "cleanup_deadline_exceeded"
REASON_WORKER_NOT_REAPED = ch.REASON_WORKER_NOT_REAPED
REASON_CGROUP_CLEANUP_TIMEOUT = ch.REASON_CLEANUP_TIMEOUT
SANDBOX_UNAVAILABLE = "sandbox_unavailable"

# --- 発行の契機 -----------------------------------------------------------------
TRIGGER_WORKER_REQUEST = "worker_request"
TRIGGER_WORKER_EXIT = "worker_exit"          # result / EOF / protocol 違反
TRIGGER_NO_RESPONSE = "no_response"          # ready が来ない
TRIGGER_MISSION_DEADLINE = "mission_deadline"
TRIGGER_STOP = "stop_event"
TRIGGER_ABORT = "afx_abort"                  # afx 側が mission を始めないと決めた (on_ready の失敗)


class DeadlineIssuer:
    """1 mission に 1 個。最初の `issue` だけが deadline を決め、以後は同じ値を返す。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self.deadline: float | None = None
        self.trigger: str | None = None
        self.issued_at: float | None = None

    def issue(self, trigger: str) -> tuple[float, bool]:
        """(deadline, この呼び出しで発行したか) を返す。"""
        with self._lock:
            if self.deadline is not None:
                return self.deadline, False
            now = self._clock()
            self.issued_at = now
            self.deadline = now + CLEANUP_BUDGET_SEC
            self.trigger = trigger
            return self.deadline, True


@dataclass
class CleanupProtocol:
    """WorkerRunner の cleanup protocol を有効にする constructor 引数。

    - `on_reaped(cgroup 名, reaped, deadline)`: worker の reap の後 (または reap できなかった
      とき) に 1 回呼ぶ。afx 本体はここで `MissionCgroupHost.cleanup_mission(name, deadline)`
      か `report_worker_not_reaped()` を呼ぶ。
    - `inject_rewait_timeout`: 試験用。SIGKILL 後の再 wait の結果を timeout と判定させる。
    - `events`: 発行・送信・wait・SIGKILL・再 wait・reap の記録 (時刻は monotonic)。"""

    on_reaped: Callable[[str, bool, float], None] | None = None
    inject_rewait_timeout: bool = False
    events: list[dict] = field(default_factory=list)

    def record(self, event: str, **kw) -> None:
        self.events.append({"event": event, "t": time.monotonic(), **kw})


def _wait(proc: subprocess.Popen, timeout: float) -> bool:
    try:
        proc.wait(timeout=max(0.0, timeout))
    except subprocess.TimeoutExpired:
        return False
    return True


def reap_worker(proc: subprocess.Popen, deadline: float,
                protocol: CleanupProtocol | None = None) -> bool:
    """ACK を待たずに `deadline - 2s` まで wait、timeout なら SIGKILL と再 wait。reap できたか。

    SIGKILL の宛先は worker の pid だけ (CLI 部分木は mission cgroup の `cgroup.kill` が受け持つ)。
    未 reap の pid は再利用されないので、pid 宛の signal はこの process に届く。"""
    record = protocol.record if protocol is not None else (lambda *a, **k: None)
    record("worker_wait", timeout=deadline - WORKER_KILL_LEAD_SEC - time.monotonic())
    if _wait(proc, deadline - WORKER_KILL_LEAD_SEC - time.monotonic()):
        record("reaped", returncode=proc.returncode, by="wait")
        return True
    record("worker_sigkill")
    try:
        os.kill(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    record("worker_rewait", timeout=deadline - time.monotonic())
    if protocol is not None and protocol.inject_rewait_timeout:
        record("not_reaped", injected=True)
        return False
    if _wait(proc, deadline - time.monotonic()):
        record("reaped", returncode=proc.returncode, by="rewait")
        return True
    record("not_reaped")
    return False


# --- worker 側 ------------------------------------------------------------------

@dataclass(frozen=True)
class AckedDeadline:
    """ACK を送り終えた deadline。cleanup の系列はこれを受けてからでないと始められない。"""

    value: float


class WorkerCleanupChannel:
    """worker 側の deadline の受け手。

    - `request(reason)`: `cleanup_request` を 1 回だけ送る。
    - `deliver(frame)`: stdin の読み手が `cleanup_deadline` を渡す。2 回目は `ProtocolError`。
    - `wait_deadline()`: deadline を待ち、`cleanup_ack` を送り、watchdog を起こしてから
      `AckedDeadline` を返す。要求を出していなくても (WorkerRunner 側の契機で) 届いた deadline
      をそのまま受ける。"""

    def __init__(self, send: Callable[[dict], None], *,
                 exit_fn: Callable[[int], None] = os._exit,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._send = send
        self._exit = exit_fn
        self._clock = clock
        self._lock = threading.Lock()
        self._ack_cond = threading.Condition(self._lock)
        self._got = threading.Event()
        self._deadline: float | None = None
        self._requested = False
        self._acked: AckedDeadline | None = None
        self._ack_reserved = False
        self.watchdog: threading.Thread | None = None

    def request(self, reason: str) -> None:
        with self._lock:
            if self._requested:
                return
            self._requested = True
        self._send({"type": MP.CLEANUP_REQUEST, "reason": reason})

    def deliver(self, frame: dict) -> None:
        deadline = MP.parse_cleanup_deadline(frame, now=self._clock(),
                                             max_ahead=DEADLINE_MAX_AHEAD_SEC)
        with self._lock:
            if self._deadline is not None:
                raise MP.ProtocolError("cleanup_deadline is issued once")
            self._deadline = deadline
        self._got.set()

    def wait_deadline(self, timeout: float | None = None) -> AckedDeadline | None:
        """ACK は state lock 下で 1 回だけ予約する。予約していない呼び出しは ACK を送らず、
        送信が終わるのを待って同じ deadline を返す (ACK 前に cleanup を始めさせない)。

        watchdog は ACK の送信より前に起こす (親へ書けなくても worker は自分で終わる)。
        送信が失敗したら予約を解いて例外を伝える。呼び直せば再送する。"""
        end = None if timeout is None else time.monotonic() + timeout
        if not self._got.wait(timeout):
            return None
        with self._ack_cond:
            while self._acked is None and self._ack_reserved:
                left = None if end is None else end - time.monotonic()
                if left is not None and left <= 0:
                    return None
                self._ack_cond.wait(left)
            if self._acked is not None:
                return self._acked
            self._ack_reserved = True
            assert self._deadline is not None
            deadline = self._deadline
            if self.watchdog is None:
                self._start_watchdog(deadline)
        try:
            self._send({"type": MP.CLEANUP_ACK, "deadline": deadline})
        except BaseException:
            with self._ack_cond:
                self._ack_reserved = False
                self._ack_cond.notify_all()
            raise
        with self._ack_cond:
            self._acked = AckedDeadline(deadline)
            self._ack_cond.notify_all()
            return self._acked

    def acquire(self, reason: str, timeout: float | None = None) -> AckedDeadline | None:
        self.request(reason)
        return self.wait_deadline(timeout)

    def _start_watchdog(self, deadline: float) -> None:
        at = deadline - WATCHDOG_LEAD_SEC

        def run() -> None:
            while True:
                left = at - self._clock()
                if left <= 0:
                    self._exit(WATCHDOG_EXIT_CODE)
                    return
                time.sleep(min(left, _WATCHDOG_TICK_SEC))

        t = threading.Thread(target=run, name="afx-cleanup-watchdog", daemon=True)
        t.start()
        self.watchdog = t


# --- worker 側の cleanup 系列 (§4.2.3 の正本) ---------------------------------------

BEFORE_HANDOFF = "before_handoff"
AFTER_HANDOFF = "after_handoff"


@dataclass
class CleanupReport:
    """系列の各段 (`steps`、時刻は monotonic) と結果。`reason` は固定 reason か None。"""

    series: str
    deadline: float
    steps: list[dict] = field(default_factory=list)
    supervisor_state: str | None = None
    supervisor_failure: str | None = None
    launcher_reaped: bool | None = None
    reason: str | None = None

    def step(self, name: str, **kw) -> None:
        self.steps.append({"step": name, "t": time.monotonic(), **kw})

    def names(self) -> list[str]:
        return [s["step"] for s in self.steps]


def _close_fd(fd: int) -> bool:
    try:
        os.close(fd)
    except OSError:
        return False
    return True


def run_cleanup_series(*, deadline: AckedDeadline, series: str, cgroup,
                       launcher_proc: subprocess.Popen | None = None,
                       supervisor=None, dispatcher=None,
                       untransferred_fds: tuple[int, ...] | list[int] = ()) -> CleanupReport:
    """§4.2.3 の系列を発行済み deadline の下で 1 回走らせる。

    - handoff 後 (正常・receiver 異常・STUCK・setup / runtime 失敗): supervisor の stop
      (RUNNING → STOPPING の線形化、drain → CLOSED / STUCK) → `cgroup.kill` → `populated 0` →
      `launcher_proc` の reap → dispatcher の stop → (CLOSED のときだけ) join → release。
      STUCK では listener を閉じず release も呼ばない (worker の exit で消える)。
    - handoff 前: 渡された `untransferred_fds` だけの close → `cgroup.kill` → `populated 0` →
      `launcher_proc` の reap (JOINED より前の launcher は cgroup に居ないので pid 宛 SIGKILL も)
      → dispatcher の stop → (listener を受け取っていない) supervisor の stop / join / release。

    handoff 前の listener と status 親端を閉じる所有者は `launch_cli` で、その結果の
    `untransferred_fds` は空。この関数は渡された番号以外を閉じない (閉じ済みの番号を
    もう一度閉じると、再利用された別の fd を閉じる)。handoff 後の listener を閉じるのは
    receiver だけで、この関数は閉じない。`launcher_proc` が既に reap
    済み (CLI の正常終了を呼び出し側が wait で検出した) なら reap の段は記録だけの no-op。

    worker 自身の作業の上限は `deadline - WATCHDOG_LEAD_SEC` (watchdog が worker を終わらせる
    時刻)。drain の待ちは handler の期限 (`HANDLER_DEADLINE_SEC`) を越えて延ばさない
    (それを越えて残る handler は止まっているので STUCK とする)。"""
    from agentic_fx.core import connect_supervisor as CS
    from agentic_fx.core.cli_launch import reap_launcher

    if not isinstance(deadline, AckedDeadline):
        raise TypeError("cleanup は ACK 済みの deadline を受けてから始める")
    if series not in (BEFORE_HANDOFF, AFTER_HANDOFF):
        raise ValueError(f"unknown series {series!r}")
    limit = deadline.value - WATCHDOG_LEAD_SEC
    report = CleanupReport(series, deadline.value)

    def stop_supervisor() -> None:
        drain = min(limit, time.monotonic() + CS.HANDLER_DEADLINE_SEC)
        state = supervisor.stop(drain)
        failure = supervisor.failure
        report.supervisor_state = state
        report.supervisor_failure = failure.reason if failure is not None else None
        report.step("supervisor_stop", state=state, failure=report.supervisor_failure)

    def finish_supervisor() -> None:
        if report.supervisor_state == CS.CLOSED:
            joined = supervisor.join(max(0.0, limit - time.monotonic()))
            report.step("supervisor_join", joined=joined)
            supervisor.release()
            report.step("supervisor_release")
        else:
            report.step("release_skipped", state=report.supervisor_state)

    if series == BEFORE_HANDOFF:
        closed = [fd for fd in untransferred_fds if _close_fd(fd)]
        report.step("close_untransferred", fds=closed)
    elif supervisor is not None:
        stop_supervisor()

    killed = cgroup.kill_quietly()
    report.step("cgroup_kill", ok=killed)
    empty = cgroup.wait_unpopulated(limit)
    report.step("populated_zero", ok=empty)
    if not empty:
        report.reason = REASON_CGROUP_CLEANUP_TIMEOUT

    if launcher_proc is None:
        report.launcher_reaped = True
        report.step("launcher_reap", result="no_launcher")
    elif launcher_proc.returncode is not None:
        report.launcher_reaped = True
        report.step("launcher_reap", result="already_reaped", returncode=launcher_proc.returncode)
    else:
        reaped = reap_launcher(launcher_proc, deadline=limit, kill=cgroup.kill_quietly)
        report.launcher_reaped = reaped
        report.step("launcher_reap", result="reaped" if reaped else "not_reaped",
                    returncode=launcher_proc.returncode)
        if not reaped and report.reason is None:
            report.reason = REASON_CLEANUP_DEADLINE_EXCEEDED

    if dispatcher is not None:
        stopped = dispatcher.stop(deadline=limit)
        report.step("dispatcher_stop", stopped=stopped)

    if supervisor is not None:
        if series == BEFORE_HANDOFF:
            stop_supervisor()
        finish_supervisor()

    if report.reason is None and time.monotonic() > limit:
        report.reason = REASON_CLEANUP_DEADLINE_EXCEEDED
    return report


def cleanup_after_launch(outcome, *, acquire: Callable[[], AckedDeadline | None], cgroup,
                         supervisor=None, dispatcher=None) -> CleanupReport:
    """`launch_cli` の結果 3 形 (`LaunchResult`、`LaunchError`、`LaunchInterrupted`) を系列に
    振り分けて後始末する。`LaunchInterrupted` は後始末の後に再送出する。閉じる未移譲 fd は
    結果が持つ `untransferred_fds` だけ (呼び出し側からは渡さない)。

    `acquire` は cleanup 要求 → deadline の受領 → ACK までを行い `AckedDeadline` を返す
    (`WorkerCleanupChannel.acquire`)。受けられなければ何も始めず
    `cleanup_deadline_exceeded` を返す (WorkerRunner の SIGKILL と afx の保険 kill に委ねる)。"""
    from agentic_fx.core import cli_launch as L

    if isinstance(outcome, L.LaunchResult):
        series = AFTER_HANDOFF if outcome.started else outcome.series
        proc = outcome.launcher_proc
    elif isinstance(outcome, (L.LaunchError, L.LaunchInterrupted)):
        series = outcome.series
        proc = outcome.launcher_proc
    else:
        raise TypeError(f"launch_cli の結果ではない: {type(outcome).__name__}")
    if series not in (BEFORE_HANDOFF, AFTER_HANDOFF):
        raise ValueError(f"unknown series {series!r}")
    deadline = acquire()
    if deadline is None:
        report = CleanupReport(series, float("nan"), reason=REASON_CLEANUP_DEADLINE_EXCEEDED)
    else:
        report = run_cleanup_series(deadline=deadline, series=series, cgroup=cgroup,
                                    launcher_proc=proc, supervisor=supervisor,
                                    dispatcher=dispatcher,
                                    untransferred_fds=outcome.untransferred_fds)
    if isinstance(outcome, L.LaunchInterrupted):
        raise outcome
    return report


__all__ = [
    "AFTER_HANDOFF", "BEFORE_HANDOFF", "CleanupReport", "cleanup_after_launch",
    "run_cleanup_series",
    "AckedDeadline", "CLEANUP_BUDGET_SEC", "DEADLINE_MAX_AHEAD_SEC", "CleanupProtocol", "DeadlineIssuer",
    "REASON_CGROUP_CLEANUP_TIMEOUT", "REASON_CLEANUP_DEADLINE_EXCEEDED",
    "REASON_WORKER_NOT_REAPED", "WATCHDOG_EXIT_CODE", "WATCHDOG_LEAD_SEC",
    "WORKER_KILL_LEAD_SEC", "WorkerCleanupChannel", "reap_worker",
]
