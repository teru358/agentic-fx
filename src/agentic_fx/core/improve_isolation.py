"""改善 mission の worker 側で CLI 隔離を組み立てる (afx が handshake で隔離を指示したときだけ)。

順序 (仕様 §2):

1. outer Landlock の前 (`WorkerIsolation.prepare`): worker 自身の source と kernel・userspace で
   fingerprint を計算し直して afx が admission で判定した値と照合し (違えば
   `fingerprint_mismatch` で何も作らずに止める)、inheritable capability を空にし、mission
   cgroup `m-<nonce>/` を作って interface fd を保持し、CLI の実行 artifact を
   `O_RDONLY|O_CLOEXEC` で開いて ELF として検査し、(dev, ino, size, mtime) を afx が admission で
   見た値と照合し、proxy の listener と NETLINK_SOCK_DIAG を作る (opencode は proxy を持たない)。
   spool root は mission worker の既存の手順で作る。worker は artifact の bytes を読まない。
2. outer Landlock の後 (`WorkerIsolation.start`): 同期 handoff の用意、connect supervisor、
   resolver と CONNECT proxy の順に起こす。supervisor と proxy の障害は mission を止める契機にする。
3. CLI の起動 (`IsolatedLaunch`): 1 mission に 1 回だけ。開いた fd の複製と admission の
   digest を `launch_cli` に渡す (path は開き直さない)。launcher は mission cgroup に入った後で
   その bytes を封印した memfd に写し (page は mission cgroup に課金される)、digest と照合して
   memfd を exec する。終わり (完走・timeout・abort・障害) は
   cleanup 要求 → afx の deadline の受領 → ACK → 仕様 §4.2.3 の系列で片付ける。

CLI は worker と同じ process group に居る (launcher は session を作らない) ので、worker の
pgid に signal を送る経路を使わない。CLI の部分木は mission cgroup の `cgroup.kill` で止める。
"""
from __future__ import annotations

import io
import logging
import os
import queue
import resource
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from agentic_fx.core import cli_launch as L
from agentic_fx.core import cli_launch_protocol as P
from agentic_fx.core import improve_fingerprint as F
from agentic_fx.core import mission_cleanup as MC
from agentic_fx.core import mission_protocol as MP

_log = logging.getLogger("agentic_fx.core.improve_isolation")

#: handshake の field 名。afx が admission を通したときだけ載せる
HANDSHAKE_FIELD = "improve_isolation"

KIND_MISSION = "mission"
KIND_CANARY = "canary"
KINDS = (KIND_MISSION, KIND_CANARY)

#: launcher の setup (HELLO〜exec の確認) の上限
SETUP_DEADLINE_SEC = 30.0
#: cleanup 要求の後、afx の deadline を待つ上限 (届かなければ何もせず afx の kill に委ねる)
DEADLINE_WAIT_SEC = MC.CLEANUP_BUDGET_SEC
#: proxy の accept thread の死を見る間隔
PROXY_WATCH_SEC = 0.2

SANDBOX_UNAVAILABLE = "sandbox_unavailable"
FAILED = "failed"

#: worker 側の隔離の障害 (mission を止める固定 reason)
REASON_SUPERVISOR_DIED = "supervisor_died"
REASON_PROXY_DIED = "proxy_died"
REASON_CLEANUP_DEADLINE = "cleanup_deadline_received"
REASON_PARENT_GONE = "parent_gone"
REASON_LAUNCH_ONCE = "isolated_launch_once"
#: artifact を写す memfd が mission cgroup の memory.max に収まらない (launcher を起こさない)
REASON_ARTIFACT_EXCEEDS_MEMORY_MAX = "artifact_exceeds_memory_max"

#: worker が自分の source で計算し直した fingerprint が afx の admission の値と違う
REASON_FINGERPRINT_MISMATCH = "fingerprint_mismatch"

#: launcher が artifact を memfd に写す間の mission cgroup の使用量のうち memfd (shmem) を除いた
#: 分の実測の最大 (2026-10-11、kernel 7.0.0-38-generic、CPython 3.13.14、backend codex、CLI 役は
#: python の ELF に 160 MiB を足した 199321480 bytes の artifact、使い捨て unit の memory scenario
#: を 3 回 (`tests/core/_improve_isolation_service_helper.py memory <out>` を使い捨て unit で)。
#: `memory.peak - memory.stat の shmem の最大` が 6856704 / 7225344 / 7237632 bytes、内訳は
#: anon 最大 5947392・kernel 最大 626688・file 4096)。CLI 役が exec した後の分も含む
ARTIFACT_COPY_PEAK_MEASURED = 7237632
#: memory.max の見積りで artifact に足す launcher 自身の分。実測の最大の 2 倍 (14475264) を
#: 2 の冪の MiB に切り上げた値。memory.high は設定しない (high は超えた分を reclaim と throttle で
#: 抑えるだけで kill しない。copy が遅くなるだけで、収まらない artifact を止める判定にならない)
ARTIFACT_MEMORY_HEADROOM = 16 * 1024 * 1024


def classified(reason: str) -> str:
    """mission の reason の形 `<分類>: <固定 reason>`。"""
    cls = FAILED if reason in (P.CLI_EXEC_UNPROVEN, "spool_flood") else SANDBOX_UNAVAILABLE
    return f"{cls}: {reason}"


def pin_worker_fsize() -> tuple[int, int]:
    """worker 自身の RLIMIT_FSIZE の hard を soft (8 MiB) まで下げ、下げた後の値を返す。

    launcher は mission cgroup の中で artifact を memfd に写すので RLIMIT_FSIZE の hard を
    artifact の大きさ以上で受け継ぐ必要があり、特権の無い process は fork した子の側で hard を
    上げられない。そのため worker は hard を残したまま launcher を fork し、fork の直後にここで
    下げる (下げた hard は戻せない)。launcher を起こさなかった経路も mission の終わりに下げる。"""
    soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    if hard != soft:
        resource.setrlimit(resource.RLIMIT_FSIZE, (soft, soft))
    return resource.getrlimit(resource.RLIMIT_FSIZE)


# --- handshake の field -------------------------------------------------------------

@dataclass(frozen=True)
class IsolationSpec:
    """afx が admission で見た値 (worker はこれと自分の fd を照合する)。"""

    kind: str
    backend: str
    artifact: F.ArtifactIdentity
    fingerprint: str

    def as_field(self) -> dict:
        return {"kind": self.kind, "backend": self.backend,
                "artifact": self.artifact.as_dict(), "fingerprint": self.fingerprint}


def parse_isolation_field(value: object) -> IsolationSpec:
    """handshake の field を検査する。形式違反は `ProtocolError`。"""
    if not isinstance(value, dict) or set(value) != {"kind", "backend", "artifact",
                                                      "fingerprint"}:
        raise MP.ProtocolError(f"{HANDSHAKE_FIELD} must have kind/backend/artifact/fingerprint")
    kind, backend, fp = value["kind"], value["backend"], value["fingerprint"]
    if kind not in KINDS:
        raise MP.ProtocolError(f"{HANDSHAKE_FIELD}.kind {kind!r}")
    if backend not in F.BACKENDS:
        raise MP.ProtocolError(f"{HANDSHAKE_FIELD}.backend {backend!r}")
    if not isinstance(fp, str) or len(fp) != 64 or any(c not in "0123456789abcdef" for c in fp):
        raise MP.ProtocolError(f"{HANDSHAKE_FIELD}.fingerprint must be 64 lowercase hex")
    try:
        artifact = F.ArtifactIdentity.from_dict(value["artifact"])
    except ValueError as e:
        raise MP.ProtocolError(f"{HANDSHAKE_FIELD}.artifact: {e}") from None
    return IsolationSpec(kind, backend, artifact, fp)


class IsolationSetupError(Exception):
    """隔離の資源を作れなかった (固定 reason)。mission は CLI を起こさずに終わる。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{classified(reason)} ({detail})" if detail else classified(reason))
        self.reason = reason
        self.detail = detail


def verify_fingerprint(spec: IsolationSpec, *, artifact_path: str | None = None) -> str:
    """worker 自身の source と kernel・userspace で fingerprint を計算し直し、handshake の値
    (afx が admission で判定した aggregate) と照合する。返り値は計算した aggregate。

    artifact の要素は handshake の artifact で組む (worker は artifact の bytes を読まない。
    metadata は開いた fd と、内容は launcher が写した memfd の digest と照合する)。違えば、
    計算できなければ `fingerprint_mismatch` (launcher も cgroup も起こさない)。"""
    try:
        computed = F.compute_fingerprint(spec.backend, spec.artifact, F.probe_kernel(),
                                         artifact_path=artifact_path).aggregate
    except Exception as e:  # noqa: BLE001  計算できなければ照合できたとはみなさない
        raise IsolationSetupError(REASON_FINGERPRINT_MISMATCH,
                                  f"compute: {type(e).__name__}") from None
    if computed != spec.fingerprint:
        raise IsolationSetupError(REASON_FINGERPRINT_MISMATCH,
                                  f"admitted={spec.fingerprint} computed={computed}")
    return computed


class CliLaunchRefused(Exception):
    """隔離の CLI 起動が成立しなかった。`reason` は `<分類>: <固定 reason>`。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason} ({detail})")
        self.reason = reason
        self.detail = detail


# --- stdin の読み手 (隔離の経路だけ) ------------------------------------------------

class StdinRouter:
    """afx からの frame を 1 本の thread で読み、種類ごとに配る。

    `go` は event、`tool_rpc_result` は rpc client の queue、`cleanup_deadline` は
    `WorkerCleanupChannel.deliver` へ。seq はここで全 frame を到着順に検査する。cleanup の
    deadline は rpc の往復の途中でも届くので、読み手を 1 本にする。

    afx の deadline か読み手の終わり (EOF・読めない frame) の後は、rpc の応答を待っている
    呼び出しも以後の呼び出しも、それより前に届いた応答を読み終えたらすぐ None を受け取る。"""

    def __init__(self, stream, in_seq: MP.SeqTracker, *,
                 deliver: Callable[[dict], None],
                 on_stop: Callable[[str], None]) -> None:
        self._stream = stream
        self._in_seq = in_seq
        self._deliver = deliver
        self._on_stop = on_stop
        self.go = threading.Event()
        self.closed = threading.Event()
        #: afx が cleanup の deadline を送った (mission は終わる。go はもう待たない)
        self.deadline_received = threading.Event()
        self.error: str | None = None
        #: go と mission の終わり (deadline・読み手の終わり) のどちらが先に届いたか。
        #: 最初の 1 回だけ決め、`_decided` で待ち手に知らせる (判定と記録は同じ lock の中)
        self._order_lock = threading.Lock()
        self._go_first: bool | None = None
        self._decided = threading.Event()
        self._rpc: "queue.Queue[dict | None]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="afx-stdin-router",
                                        daemon=True)

    def start(self) -> None:
        self._thread.start()

    def wait_go(self, timeout: float) -> bool:
        """go が deadline・読み手の終わりより先に届いていれば True。後に届いた go は数えない。"""
        self._decided.wait(max(0.0, timeout))
        with self._order_lock:
            return self._go_first is True

    def _arrived(self, event: threading.Event, *, is_go: bool) -> None:
        with self._order_lock:
            if self._go_first is None:
                self._go_first = is_go
            event.set()
        self._decided.set()

    def read_rpc(self) -> dict | None:
        """`tool_rpc_result` を 1 件 (seq 検査済み)。deadline の受領後か読み手が終わっていれば
        None (終わりの印は queue に戻すので、何度呼んでもすぐ None)。"""
        item = self._rpc.get()
        if item is None:
            self._rpc.put(None)
        return item

    def _close(self, why: str) -> None:
        self._arrived(self.closed, is_go=False)
        self._rpc.put(None)
        self._on_stop(why)

    def _run(self) -> None:
        try:
            while True:
                frame = MP.read_frame(self._stream)
                if frame is None:
                    self._close(REASON_PARENT_GONE)
                    return
                self._in_seq.check(frame.get("seq"))
                kind = frame.get("type")
                if kind == "go":
                    self._arrived(self.go, is_go=True)
                elif kind == "tool_rpc_result":
                    self._rpc.put(frame)
                elif kind == MP.CLEANUP_DEADLINE:
                    self._deliver(frame)
                    self._arrived(self.deadline_received, is_go=False)
                    # rpc の応答を待つ呼び出しを起こす (mission は終わるので応答はもう待たない)
                    self._rpc.put(None)
                    self._on_stop(REASON_CLEANUP_DEADLINE)
                else:
                    raise MP.ProtocolError(f"unexpected frame {kind!r}")
        except Exception as e:  # noqa: BLE001  読めなくなったら mission を止める
            self.error = f"{type(e).__name__}: {e}"
            self._close(REASON_PARENT_GONE)


# --- worker 側の資源 ----------------------------------------------------------------

@dataclass
class IsolationReport:
    """mission の終わりに transcript へ 1 行で残す観測 (固定語と数だけ)。"""

    kind: str
    backend: str
    cgroup: str
    artifact: dict
    proxy_port: int | None
    destinations: dict | None = None
    launched: bool = False
    launch_reason: str | None = None
    cli_pid: int | None = None
    fault: str | None = None
    cleanup: dict | None = None
    handoff_verdicts: dict = field(default_factory=dict)
    supervisor_state: str | None = None
    proxy_stopped: bool | None = None
    exec_artifact: dict | None = None
    start_order: list = field(default_factory=list)
    #: worker が計算し直した fingerprint と handshake の値の照合 (照合を通った mission だけが残る)
    fingerprint_check: dict | None = None
    #: launcher を fork した後の worker 自身の RLIMIT_FSIZE (soft, hard)
    worker_fsize: list | None = None

    def as_dict(self) -> dict:
        return dict(self.__dict__)


class WorkerIsolation:
    """1 mission 分の隔離の資源 (worker が mission の終わりまで保持する)。"""

    def __init__(self, spec: IsolationSpec, nonce: MP.MissionNonce, cgroup, artifact_fd: int,
                 listener, diag_sock) -> None:
        self.spec = spec
        self.nonce = nonce
        self.cgroup = cgroup
        self.artifact_fd = artifact_fd
        self._listener = listener
        self._diag_sock = diag_sock
        self.proxy = None
        self.handoff = None
        self.supervisor = None
        self.dispatcher = None
        self.launch: IsolatedLaunch | None = None
        self._fault_lock = threading.Lock()
        self.fault: str | None = None
        self._fault_hooks: list[Callable[[str], None]] = []
        self._stop_watch = threading.Event()
        self._closed = False
        #: proxy の停止と判定の件数の記録は 1 回だけ (起動した経路では cleanup の系列の後に済む)
        self._egress_stopped = False
        self.report = IsolationReport(spec.kind, spec.backend, cgroup.name,
                                      spec.artifact.as_dict(),
                                      listener.getsockname()[1] if listener else None)

    # 1. outer Landlock の前 ---------------------------------------------------------
    @classmethod
    def prepare(cls, spec: IsolationSpec, nonce: MP.MissionNonce,
                bin_path: str) -> "WorkerIsolation":
        from agentic_fx.core import cli_launcher
        from agentic_fx.core import mission_cgroup as MCG
        from agentic_fx.egress import inet_diag
        from agentic_fx.egress import proxy as PX

        computed = verify_fingerprint(
            spec, artifact_path=(None if spec.kind == KIND_CANARY else bin_path))
        if spec.artifact.size + ARTIFACT_MEMORY_HEADROOM > MCG.MEMORY_MAX:
            # launcher は mission cgroup の中で artifact を memfd に写す (swap は 0)。収まらなければ
            # copy の途中で launcher が OOM kill されるので、起こす前に固定 reason で止める
            raise IsolationSetupError(REASON_ARTIFACT_EXCEEDS_MEMORY_MAX,
                                      f"size={spec.artifact.size} memory.max={MCG.MEMORY_MAX}")
        try:
            # user service の process は CapInh を受け継ぐ。launcher は全部 0 を要求する
            cli_launcher.clear_inheritable_caps()
        except OSError as e:
            raise IsolationSetupError("credentials_unexpected", str(e)) from None
        try:
            cgroup = MCG.MissionCgroup.create(nonce.hex)
        except MCG.MissionCgroupError as e:
            raise IsolationSetupError(e.reason, getattr(e, "detail", str(e))) from None
        held: list = []
        try:
            try:
                fd = L.open_cli_binary(bin_path)
            except OSError as e:
                raise IsolationSetupError(P.CLI_ARTIFACT_UNSUPPORTED, f"open: {e}") from None
            held.append(fd)
            identity, why = L.inspect_cli_artifact(fd)
            if identity is None:
                raise IsolationSetupError(P.CLI_ARTIFACT_UNSUPPORTED, why)
            st = os.fstat(fd)
            adm = spec.artifact
            if (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns) != (
                    adm.dev, adm.ino, adm.size, adm.mtime_ns):
                # admission の後で artifact が変わった。見ていない artifact は exec しない
                # (内容の照合は launcher が mission cgroup の中で写した bytes で行う)
                raise IsolationSetupError(P.CLI_ARTIFACT_UNSUPPORTED,
                                          "artifact differs from the admitted one")
            listener = diag = None
            if spec.backend != "opencode":
                try:
                    listener = PX.create_listener()
                    held.append(listener)
                    diag = inet_diag.open_diag_socket()
                    held.append(diag)
                except (PX.ProxyStartFailed, OSError) as e:
                    raise IsolationSetupError(PX.PROXY_START_FAILED, str(e)) from None
        except BaseException:
            for h in held:
                try:
                    h.close() if hasattr(h, "close") else os.close(h)
                except OSError:
                    pass
            cgroup.close()
            raise
        iso = cls(spec, nonce, cgroup, fd, listener, diag)
        iso.report.fingerprint_check = {"expected": spec.fingerprint, "computed": computed,
                                        "match": True}
        return iso

    # 2. outer Landlock の後 ---------------------------------------------------------
    def start(self) -> None:
        """handoff の用意 → supervisor → proxy の順に起こす。proxy が accept を始める時点で、
        connect を代行する supervisor と同期 handoff はもう在る (port は prepare 済みの listener
        から取る)。"""
        from agentic_fx.core import connect_supervisor as CS
        from agentic_fx.egress import handoff as HO
        from agentic_fx.egress import inet_diag
        from agentic_fx.egress import proxy as PX
        from agentic_fx.egress.resolver import MissionResolver

        port = None
        if self._listener is not None:
            try:
                self.handoff = HO.SyncHandoff(inet_diag.InetDiag(self._diag_sock))
            except Exception as e:  # noqa: BLE001
                raise IsolationSetupError(PX.PROXY_START_FAILED, repr(e)) from None
            self.report.start_order.append("handoff")
            port = self._listener.getsockname()[1]
        dest = CS.destinations_for(self.spec.backend, port)
        self.report.destinations = {"proxy_port": dest.proxy_port,
                                    "direct_port": dest.direct_port}
        try:
            self.supervisor = CS.ConnectSupervisor(
                dest, proxy_handoff=self.handoff,
                on_fault=lambda f: self._set_fault(f.reason))
        except CS.SupervisorUnavailable as e:
            raise IsolationSetupError(e.reason, e.detail) from None
        self.report.start_order.append("supervisor")
        if self._listener is None:
            return
        try:
            self.proxy = PX.ConnectProxy(self._listener, MissionResolver(self.spec.backend),
                                         admit=self.handoff.admit,
                                         on_stopping=self.handoff.begin_stop)
            self.proxy.start()
        except Exception as e:  # noqa: BLE001
            self.proxy = None
            # 起こした supervisor は listener を受け取っていない。ここで止めて閉じる
            limit = time.monotonic() + 1.0
            self.supervisor.stop(limit)
            self.supervisor.join(max(0.0, limit - time.monotonic()))
            self.supervisor.release()
            self.supervisor = None
            raise IsolationSetupError(PX.PROXY_START_FAILED, repr(e)) from None
        self.report.start_order.append("proxy")
        threading.Thread(target=self._watch_proxy, name="afx-proxy-watch",
                         daemon=True).start()

    def on_fault(self, hook: Callable[[str], None]) -> None:
        self._fault_hooks.append(hook)

    def _set_fault(self, reason: str) -> None:
        with self._fault_lock:
            if self.fault is not None:
                return
            self.fault = reason
            self.report.fault = reason
            hooks = list(self._fault_hooks)
        for hook in hooks:
            try:
                hook(reason)
            except Exception:  # noqa: BLE001
                _log.exception("isolation fault hook failed")

    def _watch_proxy(self) -> None:
        while not self._stop_watch.wait(PROXY_WATCH_SEC):
            if self.proxy is not None and self.proxy.failure is not None:
                self._set_fault(REASON_PROXY_DIED)
                return

    def launcher(self, channel: MC.WorkerCleanupChannel) -> "IsolatedLaunch":
        self.launch = IsolatedLaunch(self, channel)
        return self.launch

    def _stop_egress(self, deadline: float) -> None:
        if self._egress_stopped:
            return
        self._egress_stopped = True
        self._stop_watch.set()
        if self.proxy is not None:
            self.report.proxy_stopped = self.proxy.stop(deadline)
        if self.handoff is not None:
            self.report.handoff_verdicts = dict(self.handoff.verdict_counts())

    def close(self) -> None:
        """mission の終わり。CLI を起こさなかった経路も、afx の deadline を受けて ACK してから
        §4.2.3 の handoff 前系列 (未移譲 fd の close → `cgroup.kill` → `populated 0` → dispatcher
        の停止 → supervisor の停止) を通す。起こした経路では系列が済んでいるので残りを閉じるだけ。"""
        if self._closed:
            return
        self._closed = True
        try:
            # launcher を起こさなかった経路でも worker の RLIMIT_FSIZE の hard を下げる
            pin_worker_fsize()
        except (OSError, ValueError):
            _log.exception("could not lower the worker RLIMIT_FSIZE hard limit")
        if self.launch is not None:
            # 起動した CLI の系列がまだなら (CliRunner の途中の例外など)、ここで走らせる
            self.launch.finish_pending("worker_closing")
        if not self.report.launched:
            self._stop_egress(self._cleanup_unlaunched())
        elif not self._egress_stopped:
            # 起動したが系列を通らなかった (launch_cli の想定外の例外など)
            self._stop_egress(time.monotonic() + MC.CLEANUP_BUDGET_SEC - MC.WATCHDOG_LEAD_SEC)
        if self.proxy is None and self._listener is not None:
            self._listener.close()
        if self._diag_sock is not None:
            self._diag_sock.close()
        try:
            os.close(self.artifact_fd)
        except OSError:
            pass
        self.cgroup.close()

    def _cleanup_unlaunched(self) -> float:
        """CLI を起こさなかった mission の handoff 前系列。deadline を受けられなければ何も始めず
        `cleanup_deadline_exceeded` を残す (WorkerRunner の SIGKILL と afx の保険 kill に委ねる)。
        返り値は残りの停止 (proxy) に使う上限。"""
        channel = self.launch.channel if self.launch is not None else None
        deadline = (channel.acquire("worker_closing", timeout=DEADLINE_WAIT_SEC)
                    if channel is not None else None)
        if deadline is None:
            report = MC.CleanupReport(MC.BEFORE_HANDOFF, float("nan"),
                                      reason=MC.REASON_CLEANUP_DEADLINE_EXCEEDED)
            limit = time.monotonic() + 1.0
        else:
            # 未移譲の listener と status 親端は launch_cli が持つもので、起動していないので無い
            report = MC.run_cleanup_series(
                deadline=deadline, series=MC.BEFORE_HANDOFF, cgroup=self.cgroup,
                launcher_proc=None, supervisor=self.supervisor, dispatcher=self.dispatcher,
                untransferred_fds=())
            limit = deadline.value - MC.WATCHDOG_LEAD_SEC
        self.report.cleanup = {"series": report.series, "steps": report.names(),
                               "reason": report.reason,
                               "launcher_reaped": report.launcher_reaped,
                               "supervisor_state": report.supervisor_state}
        self.report.supervisor_state = report.supervisor_state
        return limit


@dataclass
class _Handle:
    proc: subprocess.Popen
    pgid: int | None
    terminate: Callable[[], None]


class IsolatedLaunch:
    """CliRunner に渡す起動の方法。1 mission に 1 回だけ CLI を起こす。"""

    def __init__(self, iso: WorkerIsolation, channel: MC.WorkerCleanupChannel) -> None:
        self._iso = iso
        self.channel = channel
        self._used = False
        self._finished = False
        self._outcome = None
        self._lock = threading.Lock()

    def start(self, argv: Sequence[str], env: Mapping[str, str], *, cwd: str,
              stdin) -> _Handle:
        iso = self._iso
        with self._lock:
            if self._used:
                # supervisor の handoff と cleanup の deadline は 1 mission に 1 回だけ
                raise CliLaunchRefused(classified(REASON_LAUNCH_ONCE))
            self._used = True
        if iso.fault is not None:
            raise CliLaunchRefused(classified(iso.fault))
        from agentic_fx.core import mission_cgroup as MCG

        port = iso.proxy.port if iso.proxy is not None else None
        cli_env = F.isolated_cli_env(iso.spec.backend, env, port)
        cli_fd = os.dup(iso.artifact_fd)  # admission と照合した fd の複製
        iso.report.launched = True
        try:
            try:
                outcome = L.launch_cli(
                    cli_fd=cli_fd, argv=list(argv), env=cli_env, backend=iso.spec.backend,
                    join=iso.cgroup.join, handoff=iso.supervisor.handoff,
                    deadline=time.monotonic() + SETUP_DEADLINE_SEC,
                    kill=MCG.launcher_kill(iso.cgroup), nonce=iso.nonce.raw, cwd=cwd,
                    artifact_sha256=iso.spec.artifact.sha256, stdin=stdin,
                    artifact_identity=iso.spec.artifact,
                    launch_kind=("path_fd" if iso.spec.backend == "codex" else "native_elf"),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    after_spawn=pin_worker_fsize)
            finally:
                # fork に失敗した経路でも hard を下げる (fork した経路では下げ済み)
                iso.report.worker_fsize = list(pin_worker_fsize())
        except (L.LaunchError, L.LaunchInterrupted) as e:
            self._outcome = e
            self._finish(e, "launch_error")
            raise CliLaunchRefused(classified("launcher_died"), repr(e.__cause__)) from None
        self._outcome = outcome
        if outcome.exec_identity is not None:
            # launcher が写して exec する封印済み memfd
            iso.report.exec_artifact = {"dev": outcome.exec_identity[0],
                                        "ino": outcome.exec_identity[1]}
        if not outcome.started:
            iso.report.launch_reason = outcome.reason
            self._finish(outcome, outcome.reason or "launch_failed")
            raise CliLaunchRefused(f"{outcome.classification}: {outcome.reason}",
                                   outcome.detail)
        proc = outcome.launcher_proc
        iso.report.cli_pid = proc.pid
        # launch_cli の pipe は bytes。CliRunner の読み手は行単位の str を読む
        proc.stdout = io.TextIOWrapper(proc.stdout, encoding="utf-8", errors="replace")
        proc.stderr = io.TextIOWrapper(proc.stderr, encoding="utf-8", errors="replace")
        return _Handle(proc=proc, pgid=None,
                       terminate=lambda: self._finish(outcome, "cli_finished"))

    def finish_pending(self, why: str) -> None:
        if self._outcome is not None:
            self._finish(self._outcome, why)

    def _finish(self, outcome, why: str) -> None:
        """cleanup 要求 → deadline の受領 → ACK → 仕様 §4.2.3 の系列 → proxy の停止。1 回だけ。"""
        iso = self._iso
        with self._lock:
            if self._finished:
                return
            self._finished = True
        report = MC.cleanup_after_launch(
            outcome, acquire=lambda: self.channel.acquire(why, timeout=DEADLINE_WAIT_SEC),
            cgroup=iso.cgroup, supervisor=iso.supervisor, dispatcher=iso.dispatcher)
        iso.report.cleanup = {"series": report.series, "steps": report.names(),
                              "reason": report.reason,
                              "launcher_reaped": report.launcher_reaped,
                              "supervisor_state": report.supervisor_state}
        iso.report.supervisor_state = report.supervisor_state
        limit = (report.deadline - MC.WATCHDOG_LEAD_SEC if report.deadline == report.deadline
                 else time.monotonic() + 1.0)
        iso._stop_egress(limit)


__all__ = [
    "CliLaunchRefused", "HANDSHAKE_FIELD", "IsolatedLaunch", "IsolationReport",
    "IsolationSetupError", "IsolationSpec", "KIND_CANARY", "KIND_MISSION", "KINDS",
    "SETUP_DEADLINE_SEC", "StdinRouter", "WorkerIsolation", "classified",
    "parse_isolation_field", "pin_worker_fsize", "verify_fingerprint",
]
