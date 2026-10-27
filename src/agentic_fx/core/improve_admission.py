"""改善 mission の CLI backend を隔離付きで起動してよいかの判断 (admission) を一か所に集める。

隔離の経路に入るかは `ISOLATION_ENABLED` (code の定数) だけで決める。これは有効で、settings の
key にはしない (隔離を緩める設定を作らない)。

真のとき、判定は次の順で、最初の不足を固定 reason (分類は `sandbox_unavailable`) で返す。

| 段 | 不足の reason |
|---|---|
| arch | `arch_unsupported` |
| Landlock ABI ≥ 8 | `landlock_abi_too_old` |
| seccomp | `seccomp_user_notif_unavailable` / `seccomp_wait_killable_unavailable` / `seccomp_tsync_esrch_unavailable` |
| supervisor | `seccomp_user_notif_unavailable` / `pidfd_thread_unavailable` / `ptrace_scope_unsupported` |
| INET_DIAG | `proxy_start_failed` |
| unit / cgroup | `MissionCgroupHost` の gate の reason (`cgroup_*`、`unit_stop_policy_unsupported`、`worker_not_reaped`) |
| artifact | `cli_artifact_unsupported` |
| fingerprint が対応集合に無い → 安全性 canary | `safety_canary_failed` |
| → 互換性自己試験 | `runtime_fingerprint_selftest_failed` |

成功と恒久失敗は fingerprint (aggregate) ごとに process 内で覚える。一時失敗 (timeout・spawn・
provider) は覚えず、`SELFTEST_RETRY_INTERVAL_SEC` の後に試し直す。fingerprint が変われば別の
判定になる。canary や自己試験の結果から対応集合に足す経路は無い。

canary と自己試験は呼び出し時の settings (`admit` の引数) で組む。判定の cache と kernel の probe
だけを lock で守り、canary と自己試験はその外で走らせる。同じ (backend, fingerprint) の判定が
進行中なら終わるまで待ち (afx の停止で待ちをやめる)、別の fingerprint の判定は並行に進む。
afx の停止の後は cache も返さず、先行者も次の canary・自己試験に入らず、いずれも
`service_stopping` (再試行可、覚えない) を返す。停止の後に終わった判定も覚えない。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentic_fx.core import improve_fingerprint as F

_log = logging.getLogger("agentic_fx.core.improve_admission")

#: 隔離の経路は有効。settings から無効化できない。
ISOLATION_ENABLED = True

#: 一時失敗の再試行の間隔 (canary と自己試験は数秒〜数分かかるので、mission ごとには試さない)
SELFTEST_RETRY_INTERVAL_SEC = 60.0

SANDBOX_UNAVAILABLE = "sandbox_unavailable"
REASON_ARCH = "arch_unsupported"
REASON_ABI = "landlock_abi_too_old"
REASON_PROXY_START = "proxy_start_failed"
REASON_ARTIFACT = "cli_artifact_unsupported"
REASON_CANARY = "safety_canary_failed"
REASON_SELFTEST = "runtime_fingerprint_selftest_failed"
REASON_TOPOLOGY = "cgroup_topology_unsupported"
#: afx が停止を始めた (判定の前・進行中の判定を待つ間・canary と自己試験の前後。覚えない)
REASON_STOPPING = "service_stopping"

#: 進行中の判定を待つ間に停止の合図を見る間隔
WAIT_POLL_SEC = 0.2

MIN_LANDLOCK_ABI = 8
SUPPORTED_ARCH = "x86_64"


@dataclass(frozen=True)
class ProbeOutcome:
    """canary / 自己試験の結果。`transient` は一時要因 (覚えずに後で試し直す)。"""

    ok: bool
    transient: bool = False
    detail: str = ""


@dataclass(frozen=True)
class AdmissionContext:
    """canary / 自己試験に渡す、判定中の構成。"""

    backend: str
    artifact_path: str
    artifact: F.ArtifactIdentity
    fingerprint: F.ImproveFingerprint
    host: Any
    #: canary / 自己試験の WorkerRunner を組む settings (判定を求めた呼び出しのもの)
    settings: Any = None


@dataclass(frozen=True)
class AdmissionDecision:
    admitted: bool
    reason: str | None
    backend: str
    fingerprint: F.ImproveFingerprint | None = None
    artifact: F.ArtifactIdentity | None = None
    supported: bool = False
    retryable: bool = False
    detail: str = ""


def artifact_path(settings: Any, backend: str) -> str:
    """CLI の実行 artifact の path (worker と同じ値を開く)。"""
    if backend not in F.BACKENDS:
        raise ValueError(f"no CLI artifact for backend {backend!r}")
    return str(Path(getattr(settings.runner, backend).bin).expanduser())


def preflight_reason(kernel: F.KernelCapabilities, host: Any) -> str | None:
    """kernel と unit / cgroup の段 (artifact より前)。不足が無ければ None。"""
    if kernel.machine != SUPPORTED_ARCH:
        return REASON_ARCH
    if kernel.landlock_abi < MIN_LANDLOCK_ABI:
        return REASON_ABI
    if kernel.seccomp_missing:
        return kernel.seccomp_missing[0]
    if kernel.supervisor_missing:
        return kernel.supervisor_missing[0]
    if kernel.inet_diag != "ok":
        return REASON_PROXY_START
    if host is None:
        return REASON_TOPOLOGY
    if not host.mission_allowed():
        reasons = host.blocking_reasons()
        return reasons[0] if reasons else REASON_TOPOLOGY
    return None


def inspect_artifact(path: str) -> F.ArtifactIdentity:
    """artifact を開いて検査し、同じ fd を hash する。不可なら `ValueError`。"""
    from agentic_fx.core import cli_launch as L

    try:
        fd = L.open_cli_binary(path)
    except OSError as e:
        raise ValueError(f"open: {e.strerror}") from None
    try:
        ident, why = L.inspect_cli_artifact(fd)
        if ident is None:
            raise ValueError(why)
        return F.hash_artifact_fd(fd)
    except OSError as e:
        raise ValueError(f"hash: {e}") from None
    finally:
        os.close(fd)


def inspect_codex_package(path: str) -> F.CodexPackage:
    """codex の host と package manifest を検査し、検査した fd の identity と manifest の hash を返す。"""
    from agentic_fx.core import cli_launch as L
    binary = Path(path)
    host = binary.parent / "codex-code-mode-host"
    manifest = binary.parent.parent / "codex-package.json"
    try:
        fd = L.open_cli_binary(str(host))
    except OSError as e:
        raise ValueError(f"host open: {e.strerror}") from None
    try:
        ident, why = L.inspect_cli_artifact(fd)
        if ident is None:
            raise ValueError(f"host: {why}")
        host_identity = F.hash_artifact_fd(fd)
        try:
            manifest_bytes = manifest.read_bytes()
        except OSError:
            raise ValueError("package manifest missing") from None
        return F.CodexPackage(host_identity, F._sha(manifest_bytes))
    except OSError as e:
        raise ValueError(f"host hash: {e}") from None
    finally:
        os.close(fd)


class ImproveAdmission:
    """fingerprint ごとの判定を process 内で覚える。canary と自己試験は constructor で渡す。"""

    def __init__(self, *, canary: Callable[[AdmissionContext], ProbeOutcome],
                 selftest: Callable[[AdmissionContext], ProbeOutcome],
                 kernel_probe: Callable[[], F.KernelCapabilities] = F.probe_kernel,
                 supported: Callable[[], frozenset[str]] | None = None,
                 retry_interval_sec: float = SELFTEST_RETRY_INTERVAL_SEC,
                 clock: Callable[[], float] = time.monotonic,
                 stop_event: threading.Event | None = None,
                 technical_log: Callable[[str], None] | None = None) -> None:
        self._canary = canary
        self._selftest = selftest
        self._kernel_probe = kernel_probe
        self._supported = supported
        self._retry = retry_interval_sec
        self._clock = clock
        self._stop = stop_event
        self._technical_log = technical_log or (lambda line: _log.info("%s", line))
        #: kernel の probe と判定の cache だけを守る (canary と自己試験の間は握らない)
        self._lock = threading.Lock()
        self._kernel: F.KernelCapabilities | None = None
        self._cache: dict[tuple[str, str], tuple[AdmissionDecision, float]] = {}
        #: 進行中の判定 ((backend, aggregate) → 終わりの合図)
        self._running: dict[tuple[str, str], threading.Event] = {}
        #: canary / 自己試験を呼んだ回数 (fingerprint ごと)
        self.evaluations: dict[str, int] = {}

    def kernel(self) -> F.KernelCapabilities:
        # kernel の能力は process の間に変わらないので 1 回だけ問う
        with self._lock:
            if self._kernel is None:
                self._kernel = self._kernel_probe()
            return self._kernel

    def admit(self, *, backend: str, artifact_path: str, host: Any,
              settings: Any = None) -> AdmissionDecision:
        kernel = self.kernel()
        reason = preflight_reason(kernel, host)
        if reason is not None:
            return AdmissionDecision(False, reason, backend)
        try:
            artifact = inspect_artifact(artifact_path)
            package = inspect_codex_package(artifact_path) if backend == "codex" else None
        except ValueError as e:
            return AdmissionDecision(False, REASON_ARTIFACT, backend, detail=str(e))
        fp = F.compute_fingerprint(backend, artifact, kernel, codex_package=package)
        key = (backend, fp.aggregate)
        while True:
            # 停止の後は覚えた判定も返さず、canary・自己試験も始めない (停止の判定は覚えない)
            if self._stopping():
                return self._stopping_decision(backend, fp, artifact)
            with self._lock:
                now = self._clock()
                cached = self._cache.get(key)
                if cached is not None:
                    decision, ts = cached
                    if not decision.retryable or now - ts < self._retry:
                        return decision
                done = self._running.get(key)
                if done is None:
                    done = self._running[key] = threading.Event()
                    break
            # 同じ fingerprint の canary / 自己試験が進行中。終わったら cache を読み直す
            while not done.wait(WAIT_POLL_SEC):
                if self._stopping():
                    return self._stopping_decision(backend, fp, artifact)
        decision = None
        try:
            decision = self._decide(AdmissionContext(backend, artifact_path, artifact, fp, host,
                                                     settings))
        finally:
            with self._lock:
                try:
                    if decision is not None and decision.reason != REASON_STOPPING:
                        if self._stopping():
                            # 停止の後に終わった判定は返さず覚えない (確認と書込みは同じ区間)
                            decision = self._stopping_decision(backend, fp, artifact)
                        else:
                            # 再試行の間隔は判定が終わった時刻から数える (長い試験で即失効させない)
                            self._cache[key] = (decision, self._clock())
                finally:
                    self._running.pop(key, None)
                    done.set()
        self._emit_new_selftest_failure(decision)
        return decision

    def _emit_new_selftest_failure(self, decision: AdmissionDecision | None) -> None:
        if decision is None or decision.reason != REASON_SELFTEST:
            return
        from agentic_fx.loops.verify_backend import selftest_detail_or_default

        try:
            self._technical_log(
                f"improve isolation: selftest detail: {selftest_detail_or_default(decision.detail)}")
        except Exception:  # noqa: BLE001
            _log.exception("improve isolation selftest detail logging failed")

    def _stopping(self) -> bool:
        return self._stop is not None and self._stop.is_set()

    @staticmethod
    def _stopping_decision(backend: str, fp, artifact) -> AdmissionDecision:
        return AdmissionDecision(False, REASON_STOPPING, backend, fp, artifact, retryable=True)

    def _decide(self, ctx: AdmissionContext) -> AdmissionDecision:
        fp, art = ctx.fingerprint, ctx.artifact
        if F.is_supported(fp, self._supported):
            return AdmissionDecision(True, None, ctx.backend, fp, art, supported=True)
        with self._lock:
            self.evaluations[fp.aggregate] = self.evaluations.get(fp.aggregate, 0) + 1
        for reason, probe in ((REASON_CANARY, self._canary), (REASON_SELFTEST, self._selftest)):
            if self._stopping():
                return self._stopping_decision(ctx.backend, fp, art)
            try:
                out = probe(ctx)
            except Exception as e:  # noqa: BLE001  試験そのものの失敗は通さない
                _log.exception("improve isolation %s raised", reason)
                out = ProbeOutcome(False, False, f"{type(e).__name__}")
            if not isinstance(out, ProbeOutcome) or out.ok is not True:
                transient = isinstance(out, ProbeOutcome) and out.transient
                detail = out.detail if isinstance(out, ProbeOutcome) else "no outcome"
                return AdmissionDecision(False, reason, ctx.backend, fp, art,
                                         retryable=transient, detail=detail)
        return AdmissionDecision(True, None, ctx.backend, fp, art,
                                 detail="fingerprint is outside the supported set; "
                                        "canary and selftest passed in this process")


# --- afx 本体側の配線 -------------------------------------------------------------

class RefusedRunner:
    """隔離の判定を通らなかった mission。CLI も launcher も起こさず失敗を返す。

    隔離なしの経路には落とさない。結果は `refused` 付きで、supervisor は再試行せずに終端する。"""

    def __init__(self, reason: str) -> None:
        self.reason = f"{SANDBOX_UNAVAILABLE}: {reason}"

    def run(self, mission):
        from agentic_fx.runners.base import MissionResult

        return MissionResult("failed", None, [], reason=self.reason, refused=True)

    def close(self) -> None:
        """資源を持たない。"""


def cleanup_hook(host: Any) -> Callable[[str, bool, float], None]:
    """worker の reap の後に mission cgroup を片付ける (reap できなければ gate を閉じる)。"""

    def on_reaped(name: str, reaped: bool, deadline: float) -> None:
        if reaped:
            reason = host.cleanup_mission(name, deadline)
            if reason is not None:
                _log.warning("improve isolation: mission cgroup %s: %s", name, reason)
        else:
            host.report_worker_not_reaped()
            _log.warning("improve isolation: worker not reaped; next missions are blocked")

    return on_reaped


def worker_runner_options(decision: AdmissionDecision, host: Any, *,
                          kind: str | None = None) -> dict:
    """admission を通った mission の WorkerRunner の引数 (cleanup protocol と handshake の field)。"""
    from agentic_fx.core import improve_isolation as I
    from agentic_fx.core import mission_cleanup as MC

    if not decision.admitted or decision.fingerprint is None or decision.artifact is None:
        raise ValueError("isolation options need an admitted decision")
    spec = I.IsolationSpec(kind or I.KIND_MISSION, decision.backend, decision.artifact,
                           decision.fingerprint.aggregate)
    return {"cleanup_protocol": MC.CleanupProtocol(on_reaped=cleanup_hook(host)),
            "isolation": spec.as_field()}


@dataclass
class MissionChoice:
    """改善 mission の起動の仕方。`refusal` があれば CLI を起こさない。"""

    worker_options: dict = field(default_factory=dict)
    refusal: str | None = None
    decision: AdmissionDecision | None = None


_STATE_LOCK = threading.Lock()
_HOST: Any = None
_ADMISSION: ImproveAdmission | None = None


def current_host() -> Any:
    with _STATE_LOCK:
        return _HOST


def default_admission(root: Path, *, stop_event: threading.Event | None = None,
                      technical_log: Callable[[str], None] | None = None
                      ) -> ImproveAdmission:
    """canary と自己試験の本番の実装を持つ admission (process に 1 個)。

    settings は閉じ込めない。canary と自己試験は判定を求めた呼び出しの settings
    (`AdmissionContext.settings`) で WorkerRunner を組む。`stop_event` は最初に作るときだけ効く。"""
    global _ADMISSION
    with _STATE_LOCK:
        if _ADMISSION is None:
            from agentic_fx.core import improve_canary

            _ADMISSION = ImproveAdmission(
                canary=lambda ctx: improve_canary.run_canary(ctx, settings=ctx.settings,
                                                             root=root),
                selftest=lambda ctx: improve_canary.run_selftest(ctx, settings=ctx.settings,
                                                                 root=root),
                stop_event=stop_event, technical_log=technical_log)
        elif technical_log is not None:
            _ADMISSION._technical_log = technical_log
        return _ADMISSION


def choose_for_mission(settings: Any, root: Path, *, enabled: bool | None = None,
                       host: Any = None,
                       admission: ImproveAdmission | None = None) -> MissionChoice:
    """改善 mission を起こす直前の判定。無効なら今までどおり (空の引数)。"""
    if not (ISOLATION_ENABLED if enabled is None else enabled):
        return MissionChoice()
    backend = settings.runner.improve.backend
    if backend not in F.BACKENDS:
        # local は CLI を持たない (隔離の対象外)
        return MissionChoice()
    host = current_host() if host is None else host
    if host is None:
        return MissionChoice(refusal=REASON_TOPOLOGY)
    if not host.mission_allowed():
        reasons = host.blocking_reasons()
        return MissionChoice(refusal=reasons[0] if reasons else REASON_TOPOLOGY)
    adm = admission if admission is not None else default_admission(root)
    decision = adm.admit(backend=backend, artifact_path=artifact_path(settings, backend),
                         host=host, settings=settings)
    if not decision.admitted:
        return MissionChoice(refusal=decision.reason, decision=decision)
    return MissionChoice(worker_options=worker_runner_options(decision, host),
                         decision=decision)


def startup(settings: Any, root: Path, *, deadline: float, enabled: bool | None = None,
            host_factory: Callable[[], Any] | None = None,
            admit_unit: Callable[[], Any] | None = None,
            kernel_probe: Callable[[], F.KernelCapabilities] = F.probe_kernel,
            stop_event: threading.Event | None = None,
            technical_log: Callable[[str], None] | None = None) -> str:
    """afx の起動時に 1 回。admission の技術ログ 1 行を返す。`technical_log` があれば、
    自己試験の不合格 detail を別行で渡す。`stop_event` は afx の停止の合図で、同じ
    fingerprint の判定の終わりを待つ間に見る。

    有効なら mission cgroup host を起こし (unit の admission、afx の `supervisor/` への自己移動、
    前回の `m-*` の回収)、設定の backend を eager に判定する。無効なら読むだけの診断
    (unit admission と kernel の probe) で、afx の cgroup を動かさない。"""
    global _HOST
    if not (ISOLATION_ENABLED if enabled is None else enabled):
        from agentic_fx.core import unit_admission as ua

        try:
            (admit_unit or ua.admit_unit)()
            unit = "ok"
        except ua.AdmissionError as e:
            unit = e.reason
        except Exception as e:  # noqa: BLE001  診断の失敗で起動を止めない
            unit = f"error:{type(e).__name__}"
        k = kernel_probe()
        pre = preflight_reason(k, None)
        kernel_line = "ok" if pre in (None, REASON_TOPOLOGY) else pre
        return (f"improve isolation: disabled (code constant); unit_admission={unit} "
                f"kernel={kernel_line} landlock_abi={k.landlock_abi} yama={k.yama}")
    from agentic_fx.core import cgroup_host as ch

    host = (host_factory or ch.MissionCgroupHost)()
    host.start(deadline=deadline)
    with _STATE_LOCK:
        _HOST = host
    backend = settings.runner.improve.backend
    if backend not in F.BACKENDS:
        return (f"improve isolation: enabled; backend={backend} is not a CLI backend; "
                f"cgroup_gate={'open' if host.mission_allowed() else 'closed'}")
    adm = default_admission(root, stop_event=stop_event, technical_log=technical_log)
    choice = choose_for_mission(settings, root, enabled=True, host=host, admission=adm)
    fingerprint = (choice.decision.fingerprint.aggregate
                   if choice.decision is not None and choice.decision.fingerprint is not None
                   else "none")
    if choice.refusal is None:
        return f"improve isolation: enabled; backend={backend} admitted=yes fingerprint={fingerprint}"
    return (f"improve isolation: enabled; backend={backend} admitted=no "
            f"reason={choice.refusal} fingerprint={fingerprint}")


__all__ = [
    "AdmissionContext", "AdmissionDecision", "ISOLATION_ENABLED", "ImproveAdmission",
    "MissionChoice", "ProbeOutcome", "REASON_ABI", "REASON_ARCH", "REASON_ARTIFACT",
    "REASON_CANARY", "REASON_PROXY_START", "REASON_SELFTEST", "REASON_STOPPING",
    "REASON_TOPOLOGY",
    "RefusedRunner", "SELFTEST_RETRY_INTERVAL_SEC", "artifact_path", "choose_for_mission",
    "cleanup_hook", "current_host", "default_admission", "inspect_artifact",
    "preflight_reason", "startup", "worker_runner_options",
]
