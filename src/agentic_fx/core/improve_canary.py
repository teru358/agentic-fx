"""改善 mission の CLI 隔離の安全性 canary と互換性自己試験 (集合外 fingerprint の exec 前に通す)。

安全性 canary は、改善 mission と同じ経路 (afx の WorkerRunner → mission worker → 同じ launch と
cleanup の系列) で、CLI の代わりに `sys.executable` の ELF で試験 script (`CANARY_PROBE_SRC`) を
起こす。試験 script (CLI 役) と worker 役が各項目を試し、afx 側が固定の期待値と照合する。
実 launcher・実 user service (afx の unit)・実 mission cgroup・実 supervisor・実 proxy の下で動く。

| 項目 | 誰が試すか | 期待値 |
|---|---|---|
| af_unix_denied | CLI 役: `socket(AF_UNIX)` | EACCES |
| disallowed_port | CLI 役: 部分木の外の listener へ connect | ECONNREFUSED |
| listen_ipv4_eperm / listen_ipv6_eperm | CLI 役: bind 無しの `listen` | EPERM |
| clone_parent_eperm | CLI 役: `clone(CLONE_PARENT)` | EPERM |
| external_signal | CLI 役: worker への signal 0 | EPERM |
| dev_tty | CLI 役: `/dev/tty` の open | 開けない |
| cgroup_file | CLI 役: mission cgroup の `cgroup.procs` の open | EACCES |
| shared_ro_fd_metadata | CLI 役: 共有 ro file の fd への `fchmod` / `futimens` | EPERM |
| subreaper | CLI 役: `PR_GET_CHILD_SUBREAPER` | 1 |
| proxy_external_connect | worker 役: supervisor を経ない proxy への接続 | 受理されない |
| port_reuse_forward | CLI 役の受理済み接続の client port を worker 役が再利用して接続 | 受理されない |
| port_reuse_reverse | CLI 役の接続の待ちの間に worker 役が並行に接続 | CLI 役だけが受理 |
| bucket_collision | CLI 役の並行 connect | 各 1 回だけ受理 |
| receiver_close_race | CLI 役の connect の連打中に cleanup の系列 | supervisor が CLOSED |

opencode は proxy を持たないので proxy の 4 項目は `n/a`、receiver の項目は不許可 port への connect
の連打で試す。互換性自己試験は実 CLI を同じ経路で 1 mission 走らせる (verify-backend と同じ
1 ターンの接続性 probe)。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

_log = logging.getLogger("agentic_fx.core.improve_canary")

#: 安全性 canary の項目 (仕様 §8 手順 1 の列挙の順)
CANARY_ITEMS: tuple[str, ...] = (
    "af_unix_denied",
    "disallowed_port",
    "listen_ipv4_eperm",
    "listen_ipv6_eperm",
    "clone_parent_eperm",
    "external_signal",
    "dev_tty",
    "cgroup_file",
    "shared_ro_fd_metadata",
    "subreaper",
    "proxy_external_connect",
    "port_reuse_forward",
    "port_reuse_reverse",
    "bucket_collision",
    "receiver_close_race",
)

#: proxy を持たない backend では試せない項目
PROXY_ITEMS = ("proxy_external_connect", "port_reuse_forward", "port_reuse_reverse",
               "bucket_collision")

#: 各項目の期待値 (観測を正規化した固定語)
EXPECTED: dict[str, str] = {
    "af_unix_denied": "EACCES",
    "disallowed_port": "ECONNREFUSED",
    "listen_ipv4_eperm": "EPERM",
    "listen_ipv6_eperm": "EPERM",
    "clone_parent_eperm": "EPERM",
    "external_signal": "EPERM",
    "dev_tty": "denied",
    "cgroup_file": "EACCES",
    "shared_ro_fd_metadata": "EPERM",
    "subreaper": "1",
    "proxy_external_connect": "rejected",
    "port_reuse_forward": "rejected",
    "port_reuse_reverse": "tracee_only",
    "bucket_collision": "each_once",
    "receiver_close_race": "closed",
}

#: 並行 connect の本数 (CONNECT_SLOTS より少なく、tunnel 上限より少ない)
BURST = 6
#: client port の再利用 (TIME_WAIT の組) を試し直す上限
REUSE_WAIT_SEC = 1.5
#: client port の再利用を別の port でやり直す回数
REUSE_TRIES = 5
#: 1 段の待ちの上限
STEP_TIMEOUT_SEC = 20.0
#: canary mission 全体の上限
CANARY_TIMEOUT_SEC = 90

# CLI 役の試験 script。標準ライブラリだけを使う (隔離の下で exec される)
CANARY_PROBE_SRC = r'''
import ctypes, errno, json, os, socket, sys, threading, time
cfg = json.loads(sys.argv[1])
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()
def wait_next():
    line = sys.stdin.readline()
    if line.strip() != "next":
        os._exit(3)
def code(e):
    return errno.errorcode.get(e.errno, str(e.errno))
def trial(fn):
    try:
        v = fn()
        return "ok" if v is None else str(v)
    except OSError as e:
        return code(e)
r = {}
r["af_unix_denied"] = trial(lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).close())
def con(port, payload=b""):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5)
    try:
        s.connect(("127.0.0.1", port))
        if payload:
            s.sendall(payload)
        return s
    except BaseException:
        s.close()
        raise
r["disallowed_port"] = trial(lambda: con(cfg["outside_port"]).close())
def lst(fam):
    s = socket.socket(fam, socket.SOCK_STREAM)
    try:
        s.listen(1)
    finally:
        s.close()
r["listen_ipv4_eperm"] = trial(lambda: lst(socket.AF_INET))
r["listen_ipv6_eperm"] = trial(lambda: lst(socket.AF_INET6))
def clone_parent():
    rc = libc.syscall(56, 0x00008000 | 17, 0, 0, 0, 0)
    if rc == 0:
        os._exit(0)
    if rc < 0:
        raise OSError(ctypes.get_errno(), "clone")
    os.waitpid(rc, 0)
r["clone_parent_eperm"] = trial(clone_parent)
r["external_signal"] = trial(lambda: os.kill(cfg["worker_pid"], 0))
def tty():
    os.close(os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY))
v = trial(tty)
r["dev_tty"] = "denied" if v in ("ENXIO", "EACCES", "EPERM", "ENOENT") else v
r["cgroup_file"] = trial(lambda: os.close(os.open(cfg["cgroup_procs"], os.O_WRONLY)))
def meta():
    fd = os.open(cfg["shared_ro"], os.O_RDONLY)
    try:
        st = os.fstat(fd)
        a = trial(lambda: os.fchmod(fd, st.st_mode & 0o7777))
        b = trial(lambda: os.utime(fd, ns=(st.st_atime_ns, st.st_mtime_ns)))
        return a if a == b else a + "/" + b
    finally:
        os.close(fd)
r["shared_ro_fd_metadata"] = meta()
r["subreaper"] = str(libc.prctl(37, ctypes.byref(sub := ctypes.c_int(-1)), 0, 0, 0) or sub.value)
out({"phase": "local", "items": r})
proxy = cfg.get("proxy_port")
def broken(port):
    s = con(port, b"BROKEN\r\n\r\n")
    try:
        cport = s.getsockname()[1]
        s.settimeout(5)
        while s.recv(4096):
            pass
        return cport
    finally:
        s.close()
def quick(port):
    # RST で閉じて TIME_WAIT を残さない (同じ client port を外から使えるようにする)
    import struct
    s = con(port)
    try:
        return s.getsockname()[1]
    finally:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        s.close()
if proxy:
    wait_next()
    while True:
        out({"phase": "a", "client_port": quick(proxy)})
        line = sys.stdin.readline().strip()
        if line == "again":
            continue
        if line != "next":
            os._exit(3)
        break
    res = []
    def one():
        try:
            res.append(broken(proxy))
        except OSError as e:
            res.append(code(e))
    ts = [threading.Thread(target=one) for _ in range(cfg["burst"])]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    out({"phase": "burst", "results": res})
    wait_next()
race_port = proxy or cfg["outside_port"]
def loop():
    while True:
        try:
            con(race_port).close()
        except OSError:
            pass
for _ in range(4):
    threading.Thread(target=loop, daemon=True).start()
out({"phase": "race"})
time.sleep(600)
'''


def _errname(e: OSError) -> str:
    import errno as _errno

    return _errno.errorcode.get(e.errno or 0, str(e.errno))


class _Lines:
    """CLI 役の stdout を 1 行ずつ読む (期限つき)。"""

    def __init__(self, stream) -> None:
        self._q: "queue.Queue[str | None]" = queue.Queue()
        self._t = threading.Thread(target=self._run, args=(stream,), daemon=True,
                                   name="afx-canary-reader")
        self._t.start()

    def _run(self, stream) -> None:
        try:
            for line in stream:
                self._q.put(line)
        except (OSError, ValueError):
            pass
        self._q.put(None)

    def next_json(self, timeout: float) -> dict:
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError("canary probe did not answer")
            line = self._q.get(timeout=left)
            if line is None:
                raise EOFError("canary probe exited")
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and "phase" in obj:
                return obj


def _outside_connect(port: int, *, bind_port: int | None = None) -> str:
    """部分木の外 (worker) から proxy へ接続して閉じられるまで待つ。受理されたかは proxy の判定の
    件数で見る (要求行は不正にして、受理されても名前解決や外部接続をさせない)。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if bind_port is not None:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", bind_port))
        s.settimeout(5)
        s.connect(("127.0.0.1", port))
    except OSError as e:
        s.close()
        return f"connect:{_errname(e)}"
    try:
        # 判定で拒否された接続は要求を読まれずに閉じられる (未読のデータがあれば RST)
        s.sendall(b"BROKEN\r\n\r\n")
        while s.recv(4096):
            pass
        return "closed"
    except (ConnectionResetError, BrokenPipeError):
        return "closed"
    except OSError as e:
        return _errname(e)
    finally:
        s.close()


class CanaryRunner:
    """worker 側の canary mission。CLI 役を `IsolatedLaunch` で 1 回起こし、項目を試す。"""

    def __init__(self, launch, isolation, workdir: Path) -> None:
        self._launch = launch
        self._iso = isolation
        self._workdir = workdir
        self.mcp_spool_dir = None

    def close(self) -> None:
        """資源は isolation が持つ。"""

    def run(self, mission):
        from agentic_fx.core.improve_isolation import CliLaunchRefused
        from agentic_fx.runners.base import MissionResult

        iso = self._iso
        obs: dict[str, str] = {}
        error = None
        # listener と pipe は作った直後から finally で閉じる (起動の拒否や例外でも残さない)
        outside = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        r_fd = w_fd = None
        try:
            outside.bind(("127.0.0.1", 0))
            outside.listen(16)
            proxy_port = iso.proxy.port if iso.proxy is not None else None
            cfg = {"worker_pid": os.getpid(), "outside_port": outside.getsockname()[1],
                   "cgroup_procs": f"/sys/fs/cgroup{iso.cgroup.rel}/cgroup.procs",
                   "shared_ro": os.path.realpath(sys.executable), "proxy_port": proxy_port,
                   "burst": BURST}
            env = {"PATH": "/usr/bin:/bin", "HOME": str(self._workdir / "home"),
                   "TMPDIR": str(self._workdir / "tmp")}
            argv = [sys.executable, "-I", "-c", CANARY_PROBE_SRC, json.dumps(cfg)]
            r_fd, w_fd = os.pipe()
            try:
                handle = self._launch.start(argv, env, cwd=str(self._workdir), stdin=r_fd)
            except CliLaunchRefused as e:
                return MissionResult("failed", None, [], reason=e.reason)
            finally:
                os.close(r_fd)
                r_fd = None
            lines = _Lines(handle.proc.stdout)
            try:
                obs.update(lines.next_json(STEP_TIMEOUT_SEC)["items"])
                if proxy_port is not None:
                    self._proxy_items(obs, lines, w_fd, proxy_port)
                else:
                    for item in PROXY_ITEMS:
                        obs[item] = "n/a"
                lines.next_json(STEP_TIMEOUT_SEC)  # race の connect の連打が始まった
                time.sleep(0.3)
            except (TimeoutError, EOFError, OSError, KeyError) as e:
                error = f"{type(e).__name__}: {e}"
            finally:
                # 連打の最中に cleanup の系列 (supervisor の STOPPING → drain → CLOSED → kill)
                handle.terminate()
                # CLI 役が stdin を待ったまま残っていても EOF で終わるように、stderr を読む前に閉じる
                os.close(w_fd)
                w_fd = None
                try:
                    handle.proc.stderr.read()
                except (OSError, ValueError):
                    pass
        finally:
            if w_fd is not None:
                os.close(w_fd)
            outside.close()
        cl = iso.report.cleanup or {}
        obs["receiver_close_race"] = (
            "closed" if cl.get("supervisor_state") == "CLOSED" and cl.get("reason") is None
            else f"{cl.get('supervisor_state')}:{cl.get('reason')}")
        output = {"canary": obs, "error": error, "isolation": iso.report.as_dict()}
        return MissionResult("completed", output, [])

    def _proxy_items(self, obs: dict, lines: _Lines, w_fd: int, port: int) -> None:
        iso = self._iso

        def counts() -> dict:
            return dict(iso.handoff.verdict_counts())

        def outside(**kw) -> str:
            """接続できて proxy に閉じられ、受理の件数が増えなければ rejected。"""
            before = counts().get("accept", 0)
            got = _outside_connect(port, **kw)
            end = time.monotonic() + REUSE_WAIT_SEC
            while got == "connect:EADDRINUSE" and time.monotonic() < end:
                # 同じ組が TIME_WAIT の間は接続できない。loopback の再利用は timestamp が
                # 1 秒進んでから許される
                time.sleep(0.1)
                got = _outside_connect(port, **kw)
            if counts().get("accept", 0) != before:
                return "accepted"
            return "rejected" if got == "closed" else f"unreached:{got}"

        obs["proxy_external_connect"] = outside()
        c0 = counts()
        os.write(w_fd, b"next\n")
        # CLI 役の受理済み接続と同じ client port から外部接続する。その port を host の別の
        # 接続 (宛先違いで同じ ephemeral port を共有する) が使っていると bind できないので、
        # CLI 役に接続をやり直させて別の port で試す
        for tries in range(1, REUSE_TRIES + 1):
            a = lines.next_json(STEP_TIMEOUT_SEC)
            obs["port_reuse_forward"] = outside(bind_port=int(a["client_port"]))
            if not obs["port_reuse_forward"].startswith("unreached:") or tries == REUSE_TRIES:
                break
            os.write(w_fd, b"again\n")
        accepted_a = counts().get("accept", 0) - c0.get("accept", 0) - tries + 1
        c1 = counts()
        # CLI 役の並行 connect の待ちの間、worker 役も並行に接続する
        outsiders: list[str] = []
        stop = threading.Event()

        def hammer() -> None:
            while not stop.is_set():
                outsiders.append(_outside_connect(port))

        hs = [threading.Thread(target=hammer, daemon=True) for _ in range(2)]
        for h in hs:
            h.start()
        os.write(w_fd, b"next\n")
        burst = lines.next_json(STEP_TIMEOUT_SEC)["results"]
        stop.set()
        for h in hs:
            h.join(10)
        c2 = counts()
        tracee_ok = all(isinstance(x, int) for x in burst) and len(burst) == BURST
        accepted_burst = c2.get("accept", 0) - c1.get("accept", 0)
        # 受理の件数が CLI 役の本数と一致する = 並行の外部接続は 1 本も受理されていない
        obs["port_reuse_reverse"] = (
            "tracee_only" if tracee_ok and outsiders and accepted_burst == BURST
            else f"burst={burst} accepted={accepted_burst} outsiders={len(outsiders)}")
        obs["bucket_collision"] = (
            "each_once" if accepted_a == 1 and accepted_burst == BURST
            and c2.get("multiple_match", 0) == 0 else f"counts={c2}")
        os.write(w_fd, b"next\n")


def canary_runner_factory(isolation):
    """mission worker の `build_runner` の代わり (canary の handshake のときだけ)。"""

    def build(profile, settings, registry, *, workdir, launch=None, **_kw):
        if launch is None:
            raise RuntimeError("canary runs only on the isolated launch")
        return CanaryRunner(launch, isolation, Path(workdir))

    return build


def evaluate(observed: dict, backend: str,
             expected: dict[str, str] | None = None) -> list[str]:
    """期待値と違う項目の名前 (空なら合格)。"""
    exp = dict(EXPECTED if expected is None else expected)
    if backend == "opencode":
        for item in PROXY_ITEMS:
            exp[item] = "n/a"
    return [item for item in CANARY_ITEMS if observed.get(item) != exp[item]]


# --- afx 側: canary と自己試験の mission ------------------------------------------------

def _scratch_context(root: Path):
    from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
    from agentic_fx.loops.improve_run_context import ImproveRunContext

    scratch = Path(tempfile.mkdtemp(prefix="afx-improve-canary-"))
    staging = scratch / "staging" / "-1"
    staging.mkdir(parents=True, mode=0o700)
    source = scratch / "source_snapshot"
    source.mkdir(mode=0o500)

    def reject(args: dict) -> dict:
        raise RuntimeError("rpc is not used by the canary")

    ctx = ImproveRunContext(
        mission_id=-1, run_id=-1, staging_dir=staging, source_snapshot_dir=source,
        allowed_backlog_ids=None, slot_key=None,
        ledger=ImproveRpcLedger(rpc_timeout_sec_by_kind={}),
        rpc_handlers={"run_backtest": reject, "analyze_corr": reject})
    return scratch, ctx


def _transient(status: str, reason: str | None) -> bool:
    text = reason or ""
    return (status == "timeout" or "never reached ready" in text or "worker eof" in text
            or "usage limit" in text or "try again at" in text)


def run_canary(ctx, *, settings: Any, root: Path, expected: dict[str, str] | None = None,
               on_result=None):
    """安全性 canary の mission を 1 回走らせ、期待値と照合した `ProbeOutcome` を返す。

    `expected` は期待値の差し替え、`on_result` は mission の結果の受け手 (どちらも試験用)。"""
    from agentic_fx.core import improve_admission as IA
    from agentic_fx.core import improve_fingerprint as F
    from agentic_fx.core import improve_isolation as I
    from agentic_fx.core.contracts import SystemClock
    from agentic_fx.runners.base import Mission
    from agentic_fx.runners.worker_runner import WorkerRunner

    host = ctx.host
    try:
        python = IA.inspect_artifact(os.path.realpath(sys.executable))
    except ValueError as e:
        return IA.ProbeOutcome(False, False, f"canary artifact: {e}")
    # worker は handshake の artifact (canary では Python の ELF) で fingerprint を計算し直して
    # 照合するので、artifact の要素だけを Python の ELF に替えた fingerprint を渡す
    decision = IA.AdmissionDecision(True, None, ctx.backend,
                                    F.with_artifact(ctx.fingerprint, python,
                                                    clear_codex_package=True), python)
    options = IA.worker_runner_options(decision, host, kind=I.KIND_CANARY)
    scratch, rctx = _scratch_context(root)
    try:
        runner = WorkerRunner(root=root, settings=settings, clock=SystemClock(), rag=None,
                              worker_profile="improve", run_context=rctx,
                              rpc_handlers=rctx.rpc_handlers, **options)
        mission = Mission(prompt="improve isolation safety canary", tools=[],
                          output_schema={"type": "object", "required": ["canary"]},
                          max_turns=1, timeout_sec=CANARY_TIMEOUT_SEC)
        result = runner.run(mission)
    except Exception as e:  # noqa: BLE001  起動できない環境は一時要因として試し直す
        return IA.ProbeOutcome(False, True, f"canary spawn: {type(e).__name__}")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if on_result is not None:
        on_result(result)
    if result.status != "completed" or not isinstance(result.output, dict):
        return IA.ProbeOutcome(False, _transient(result.status, result.reason),
                               f"canary mission {result.status}: {result.reason}")
    failed = evaluate(result.output.get("canary") or {}, ctx.backend, expected)
    if result.output.get("error"):
        failed = failed or ["probe_protocol"]
    if not host.mission_allowed():
        failed = failed + ["cgroup_gate"]
    if failed:
        return IA.ProbeOutcome(False, False, ",".join(failed))
    return IA.ProbeOutcome(True, False, "")


#: 自己試験の mission の上限 (秒)。unit の RuntimeMaxSec (900) に切られる前に自分で止まる値
SELFTEST_MISSION_TIMEOUT_SEC = 600


def run_selftest(ctx, *, settings: Any, root: Path):
    """互換性自己試験: 実 CLI を隔離の下で 1 mission (verify-backend と同じ 1 ターンの接続性
    probe) 走らせる。schema の往復、終了後の cgroup の片付け、gate が開いたままを確かめる。"""
    from agentic_fx.core import improve_admission as IA
    from agentic_fx.core.contracts import SystemClock
    from agentic_fx.loops.verify_backend import verify_backend

    host = ctx.host
    improve = settings.improve
    if improve.mission_timeout_sec > SELFTEST_MISSION_TIMEOUT_SEC:
        settings = settings.model_copy(update={"improve": improve.model_copy(
            update={"mission_timeout_sec": SELFTEST_MISSION_TIMEOUT_SEC})})
    decision = IA.AdmissionDecision(True, None, ctx.backend, ctx.fingerprint, ctx.artifact)
    options = IA.worker_runner_options(decision, host)
    try:
        res = verify_backend(root, settings, backend=ctx.backend, provider=None,
                             clock=SystemClock(), isolation_options=options,
                             stop_on_echo=True)
    except Exception as e:  # noqa: BLE001
        return IA.ProbeOutcome(False, True, f"selftest spawn: {type(e).__name__}")
    if not res.ok:
        return IA.ProbeOutcome(False, _transient("failed", res.detail), res.detail[:200])
    if not host.mission_allowed():
        return IA.ProbeOutcome(False, False, "cgroup_gate: " + ",".join(host.blocking_reasons()))
    return IA.ProbeOutcome(True, False, "")


__all__ = [
    "BURST", "CANARY_ITEMS", "CANARY_PROBE_SRC", "EXPECTED", "PROXY_ITEMS",
    "SELFTEST_MISSION_TIMEOUT_SEC", "CanaryRunner",
    "canary_runner_factory", "evaluate", "run_canary", "run_selftest",
]
