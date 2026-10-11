"""使い捨ての user service の main として走る helper (改善 mission の CLI 隔離の結合)。

usage:
  python _improve_isolation_service_helper.py <scenario> <out.json>
  python _improve_isolation_service_helper.py --worker <backend> <probe.json>   (worker 役)

afx 本体の役: 実 `MissionCgroupHost` を起こし (unit 名だけを注入)、`improve_admission` で判定し、
製品の `WorkerRunner` (cleanup protocol と隔離の field つき) で worker 役を起こす。worker 役は
製品の `mission_worker.main()` で、runner の組み立てだけを probe の CLI 役に差し替える。CLI 役は
`sys.executable` の ELF を launcher が `execveat` し、`-c` で probe の source を走らせる。

触るのは自分の unit の cgroup と tmp の file だけ。
"""
from __future__ import annotations

import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

HERE = Path(__file__).resolve()
REPO = HERE.parents[2]
PROBE_SRC = HERE.with_name("_improve_probe_cli.py").read_text()
VENV_PY = sys.executable
BASE_PY = os.path.realpath(sys.executable)
MOUNT = "/sys/fs/cgroup"


# --- worker 役 -------------------------------------------------------------------

#: probe が受け取った argv / env などを書く file (worker 役の cwd = mission の workdir)
OBSERVE_FILE = "probe-observed.json"

#: worker 役が自分の process で計算し直した fingerprint (verify_fingerprint の計算を記録する)
OBSERVED: dict = {}


def _observe_fingerprint() -> None:
    from agentic_fx.core import improve_fingerprint as F

    real = F.compute_fingerprint

    def compute(backend, *a, **kw):
        fp = real(backend, *a, **kw)
        OBSERVED.update(fp_backend=backend, fp_aggregate=fp.aggregate,
                        fp_components=dict(fp.components))
        return fp
    F.compute_fingerprint = compute


def _probe_runner(runner, backend: str, probe_cfg: dict):
    """production の `runner_factory.build_runner` が返した runner の instance を probe 用にする。

    runner の種類・コンストラクタ引数・`_build_argv` / `_build_env` / MCP 設定 file は production の
    もののまま。差し替えるのは argv の先頭 (CLI の path) の後ろへ probe の source を挟む点と、出力の
    取り出しだけ。probe が受け取った argv の残り・env・設定 file を結果に書く。"""
    real_argv = runner._build_argv

    def build_argv(mission, *, mcp_spool):
        real = real_argv(mission, mcp_spool=mcp_spool)
        cfg = {**probe_cfg, "observe_to": str(Path.cwd() / OBSERVE_FILE),
               "spool": str(mcp_spool) if mcp_spool else None,
               "observed": {"runner_class": type(runner).__name__,
                            "backend": backend, **OBSERVED}}
        # argv[0] は venv の python (site-packages を見つけるため)。exec されるのは artifact の
        # fd (hash したもの)。本物の argv の先頭 (CLI の path) 以外はそのまま渡す
        return [VENV_PY, "-I", "-c", PROBE_SRC, json.dumps(cfg), *real[1:]]

    def extract_output(stdout_lines, workdir):
        for line in reversed(stdout_lines):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and "probe" in obj:
                return obj
        return None

    runner._build_argv = build_argv
    runner._extract_output = extract_output
    runner._recovery_reserve_sec = lambda: 0.0
    return runner


def _inject(kind: str) -> None:
    """worker 役の process の中だけで飽和を作る (supervisor の slot / proxy の tunnel 上限)。"""
    if kind == "503":
        from agentic_fx.egress import proxy as PX
        PX.MAX_TUNNELS = 1
    elif kind == "eagain":
        from agentic_fx.core import connect_supervisor as CS
        real_init = CS.ConnectSupervisor.__init__

        def init(self, *a, **kw):
            def slow(point: str, nid: int) -> None:
                if point == "handler":
                    time.sleep(1.0)
            kw["faults"] = CS.Faults(at=slow)
            real_init(self, *a, **kw)
            self._slots = threading.BoundedSemaphore(1)
        CS.ConnectSupervisor.__init__ = init
    elif kind == "fault":
        from agentic_fx.core import connect_supervisor as CS
        real_init = CS.ConnectSupervisor.__init__

        def init(self, *a, **kw):
            real_init(self, *a, **kw)
            hook = kw.get("on_fault")
            # CLI の実行中に supervisor の障害を 1 回知らせる
            threading.Timer(0.5, lambda: hook(CS.SupervisorFailure(
                CS.SUPERVISOR_DIED, "injected"))).start()
        CS.ConnectSupervisor.__init__ = init
    elif kind == "fault_early":
        from agentic_fx.core import connect_supervisor as CS
        real_init = CS.ConnectSupervisor.__init__

        def init(self, *a, **kw):
            real_init(self, *a, **kw)
            # CLI を起こす前に supervisor の障害を知らせる (CLI は起動しない)
            kw.get("on_fault")(CS.SupervisorFailure(CS.SUPERVISOR_DIED, "injected"))
        CS.ConnectSupervisor.__init__ = init
    elif kind == "system_exit":
        pass  # worker_main が runner.run を差し替える
    elif kind.startswith("memory_max:"):
        # mission cgroup の memory.max を小さくする (artifact の copy が収まらない場合を作る)
        from agentic_fx.core import mission_cgroup as MCG
        MCG.MEMORY_MAX = int(kind.split(":", 1)[1])
    elif kind:
        raise SystemExit(f"unknown inject {kind}")


#: worker 役の cwd (mission の workdir、Landlock で書ける) に置く dispatcher.stop の呼出し記録
STOP_LOG = "dispatcher-stops.log"


def _record_dispatcher_stops() -> None:
    from agentic_fx.tools import mcp_spool

    real_stop = mcp_spool.SpoolDispatcher.stop
    log = Path.cwd() / STOP_LOG

    def stop(self, *a, **kw):
        with open(log, "a") as f:
            f.write("stop\n")
        return real_stop(self, *a, **kw)
    mcp_spool.SpoolDispatcher.stop = stop


def worker_main(backend: str, probe_json: str) -> None:
    from agentic_fx import mission_worker as MW
    from agentic_fx.runners import factory as runner_factory

    probe_cfg = json.loads(probe_json)
    _observe_fingerprint()
    _inject(probe_cfg.get("inject", ""))
    _record_dispatcher_stops()

    def build_runner(profile, settings, registry, **kw):
        # backend の選択・引数は production の factory (model だけ probe 名にする)
        improve = settings.runner.improve.model_copy(update={"model": "probe"})
        settings = settings.model_copy(update={
            "runner": settings.runner.model_copy(update={"improve": improve})})
        runner = runner_factory.build_runner(profile, settings, registry, **kw)
        return _probe_runner(runner, backend, probe_cfg)

    if probe_cfg.get("inject") == "system_exit":
        real_build = build_runner

        def build_runner(*a, **kw):  # noqa: F811
            runner = real_build(*a, **kw)

            def run(mission):
                # router を起こした後に SystemExit が main を抜ける
                raise SystemExit(3)
            runner.run = run
            return runner

    MW.main(build_runner=build_runner)


# --- afx 本体の役 ------------------------------------------------------------------

def save(out: Path, r: dict) -> None:
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(r, ensure_ascii=False, indent=1, default=str))
    os.replace(tmp, out)


def self_rel() -> str:
    with open("/proc/self/cgroup") as f:
        return f.read().strip().split("::", 1)[1]


def start_host():
    from agentic_fx.core import cgroup_host as ch
    from agentic_fx.core import unit_admission as ua

    unit = self_rel().rsplit("/", 1)[1]
    host = ch.MissionCgroupHost()
    host.start(deadline=time.monotonic() + 10, admit=lambda: ua.UnitAdmission(unit=unit))
    if not host.mission_allowed():
        raise RuntimeError(f"topology を作れない: {host.blocking_reasons()}")
    return host


class CgroupWatch:
    """mission 中の `m-*` の cgroup.procs を記録する (CLI がどこに居たか)。memory.current と
    memory.stat の shmem の最大値、memory.events の最後の値も残す (memfd の課金先を見る)。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.seen: dict[str, set[int]] = {}
        self.memory: dict[str, dict] = {}
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            for d in self.root.glob("m-*"):
                try:
                    pids = {int(x) for x in (d / "cgroup.procs").read_text().split()}
                except (OSError, ValueError):
                    continue
                self.seen.setdefault(d.name, set()).update(pids)
                self._sample_memory(d)
            self._stop.wait(0.005)

    def _sample_memory(self, d: Path) -> None:
        try:
            current = int((d / "memory.current").read_text())
            stat = dict(line.split() for line in (d / "memory.stat").read_text().splitlines())
            events = dict(line.split() for line in
                          (d / "memory.events").read_text().splitlines())
        except (OSError, ValueError):
            return
        try:
            peak = int((d / "memory.peak").read_text())
        except (OSError, ValueError):
            peak = None
        m = self.memory.setdefault(d.name, {"current_max": 0, "shmem_max": 0,
                                            "non_shmem_max": 0, "anon_max": 0,
                                            "file_max": 0, "kernel_max": 0})
        shmem = int(stat.get("shmem", 0))
        m["current_max"] = max(m["current_max"], current)
        m["shmem_max"] = max(m["shmem_max"], shmem)
        # memfd (shmem) を除いた分: launcher と CLI 役の RSS・page cache・kernel の分
        m["non_shmem_max"] = max(m["non_shmem_max"], current - shmem)
        m["anon_max"] = max(m["anon_max"], int(stat.get("anon", 0)))
        m["file_max"] = max(m["file_max"], int(stat.get("file", 0)) - shmem)
        m["kernel_max"] = max(m["kernel_max"], int(stat.get("kernel", 0)))
        if peak is not None:
            m["peak"] = peak
        m["events"] = {k: int(v) for k, v in events.items()}

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        self._t.join(5)


#: backend ごとに実在する改善 mission の model (実 CLI の自己試験がそのまま使う。example の
#: improve.model は local の model なので claude / codex では通らない)。opencode は example のまま
REAL_MODELS = {"claude": "haiku", "codex": "gpt-5.6-luna"}
#: 実 CLI の自己試験の mission の上限。unit の RuntimeMaxSec (900) より十分内側に置き、mission が
#: 自分で終わらなくても結果の JSON と片付けの観測が残るようにする
REAL_SELFTEST_MISSION_TIMEOUT_SEC = 600


def make_settings(backend: str, artifact: str):
    from agentic_fx.config import load_settings

    s = load_settings(REPO / "config" / "settings.yaml.example")
    update = {"bin": artifact}
    if backend == "opencode":
        update["context_limit"] = 4096
    improve = {"backend": backend}
    if backend in REAL_MODELS:
        improve["model"] = REAL_MODELS[backend]
    runner = s.runner.model_copy(update={
        "improve": s.runner.improve.model_copy(update=improve),
        backend: getattr(s.runner, backend).model_copy(update=update)})
    w = s.worker.model_copy(update={"worker_grace_sec": 60.0, "worker_startup_timeout_sec": 30})
    return s.model_copy(update={"runner": runner, "worker": w})


def run_mission(r: dict, *, host, backend: str, artifact: str, probe_cfg: dict,
                options: dict, on_ready_extra=None, hook_override=None,
                timeout_sec: float = 60) -> dict:
    """1 mission を製品の WorkerRunner で走らせ、観測を返す。"""
    from agentic_fx.core.contracts import FixedClock
    from agentic_fx.runners.base import Mission
    from agentic_fx.runners.worker_runner import WorkerRunner, _mission_worker_env

    settings = make_settings(backend, artifact)
    obs: dict = {"hook": []}
    protocol = options["cleanup_protocol"]
    real_hook = protocol.on_reaped

    def on_reaped(name: str, reaped: bool, deadline: float) -> None:
        path = host.root / name
        entry = {"name": name, "reaped": reaped, "exists_before": path.exists()}
        (hook_override or real_hook)(name, reaped, deadline)
        entry["exists_after"] = path.exists()
        obs["hook"].append(entry)

    protocol.on_reaped = on_reaped

    def on_ready(frame: dict) -> None:
        obs["ready"] = frame.get("ok")
        if on_ready_extra is not None:
            on_ready_extra()

    base = Path(tempfile.mkdtemp(prefix="afx-mission-"))
    workdir = base
    for sub in ("home", "tmp", "cfg"):
        (workdir / sub).mkdir(mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix="afx-staging-")) / "7"
    staging.mkdir(mode=0o700)
    (workdir / "source").mkdir()
    transcripts = Path(tempfile.mkdtemp(prefix="afx-transcripts-"))
    env = dict(_mission_worker_env("improve"))
    env["AGENTIC_FX_MISSION_TRANSCRIPTS_DIR"] = str(transcripts)
    runner = WorkerRunner(
        root=Path(tempfile.mkdtemp(prefix="afx-root-")), settings=settings,
        clock=FixedClock(datetime(2026, 10, 11, tzinfo=timezone.utc)), rag=None,
        worker_profile="improve", on_ready=on_ready,
        rpc_handlers={"run_backtest": lambda a: {}, "analyze_corr": lambda a: {}},
        **options)
    proc = subprocess.Popen(
        [VENV_PY, str(HERE), "--worker", backend, json.dumps(probe_cfg)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(workdir), env=env, start_new_session=True)
    mission = Mission(prompt="隔離の結合の mission (CLI は probe)", tools=[], output_schema={
        "type": "object", "required": ["probe"]}, max_turns=1, timeout_sec=timeout_sec)
    fields = {"mission_id": "7", "staging_dir": str(staging),
              "source_snapshot_dir": str(workdir / "source"), "inventory_view": None}
    t0 = time.monotonic()
    try:
        with CgroupWatch(host.root) as watch:
            res = runner._run_with_child(proc, mission, settings.worker,
                                         run_context_fields=fields)
    finally:
        if proc.returncode is None:
            proc.kill()
            proc.wait(10)
    obs["elapsed"] = round(time.monotonic() - t0, 2)
    obs["result"] = {"status": res.status, "reason": res.reason, "output": res.output}
    obs["worker_returncode"] = proc.returncode
    try:
        obs["worker_stderr"] = proc.stderr.read().decode("utf-8", "replace")[-3000:]
    except (OSError, ValueError):
        obs["worker_stderr"] = None
    obs["report"] = None
    for msg in res.transcript:
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str) and content.startswith('{"_improve_isolation"'):
            obs["report"] = json.loads(content)["_improve_isolation"]
    obs["events"] = protocol.events
    obs["cgroups_seen"] = {k: sorted(v) for k, v in watch.seen.items()}
    obs["cgroup_memory"] = watch.memory
    obs["blocking_reasons"] = list(host.blocking_reasons())
    obs["mission_allowed"] = host.mission_allowed()
    obs["m_dirs_left"] = sorted(p.name for p in host.root.iterdir() if p.name.startswith("m-"))
    obs["handshake_field"] = options.get("isolation")
    observed = workdir / OBSERVE_FILE
    obs["probe_observed"] = json.loads(observed.read_text()) if observed.exists() else None
    stops = workdir / STOP_LOG
    obs["dispatcher_stops"] = (stops.read_text().count("stop\n") if stops.exists() else 0)
    for d in (base, staging.parent, transcripts):
        shutil.rmtree(d, ignore_errors=True)
    return obs


def admitted_options(host, backend: str, artifact: str):
    from agentic_fx.core import improve_admission as IA

    adm = IA.ImproveAdmission(canary=lambda c: IA.ProbeOutcome(True),
                              selftest=lambda c: IA.ProbeOutcome(True))
    choice = IA.choose_for_mission(make_settings(backend, artifact), REPO, enabled=True,
                                   host=host, admission=adm)
    if choice.refusal is not None:
        raise RuntimeError(f"admission refused: {choice.refusal} {choice.decision}")
    return choice


def leftover_cleanup(host, r: dict) -> None:
    from agentic_fx.core import cgroup_host as ch

    for p in host.root.iterdir():
        if p.name.startswith("m-"):
            try:
                ch.remove_mission_cgroup(p, time.monotonic() + 5)
            except ch.CgroupHostError as err:
                r.setdefault("leftover_cleanup", []).append(err.reason)


# --- scenario ---------------------------------------------------------------------

def sc_e2e(r: dict, backend: str) -> None:
    host = start_host()
    choice = admitted_options(host, backend, BASE_PY)
    # 部分木の外の listener (supervisor が通さなければ connect は成功する)
    import socket
    outside = socket.socket()
    outside.bind(("127.0.0.1", 0))
    outside.listen(8)
    cfg = {"connect": "proxy" if backend != "opencode" else outside.getsockname()[1],
           "hold": 0.3}
    try:
        r["mission"] = run_mission(r, host=host, backend=backend, artifact=BASE_PY,
                                   probe_cfg=cfg, options=choice.worker_options)
    finally:
        outside.close()
    r["decision"] = {"reason": choice.decision.reason, "detail": choice.decision.detail,
                     "aggregate": choice.decision.fingerprint.aggregate}
    leftover_cleanup(host, r)


def sc_renamed(r: dict) -> None:
    """hash した inode が exec される (ready の後・go の前に path を別 file へ差し替える)。"""
    host = start_host()
    d = Path(tempfile.mkdtemp(prefix="afx-artifact-"))
    art = d / "cli"
    shutil.copy2(BASE_PY, art)
    other = d / "other"
    shutil.copy2(BASE_PY, other)
    with open(other, "ab") as f:
        f.write(b"\0" * 16)  # 内容の違う別 inode
    r["original_ino"] = os.stat(art).st_ino
    r["other_ino"] = os.stat(other).st_ino
    choice = admitted_options(host, "codex", str(art))

    def swap() -> None:
        os.rename(other, art)
        r["swapped_ino"] = os.stat(art).st_ino

    r["mission"] = run_mission(r, host=host, backend="codex", artifact=str(art),
                               probe_cfg={"hold": 0.1}, options=choice.worker_options,
                               on_ready_extra=swap)
    r["admitted_artifact"] = choice.decision.artifact.as_dict()
    shutil.rmtree(d, ignore_errors=True)
    leftover_cleanup(host, r)


def sc_in_place(r: dict) -> None:
    """admission の後 (ready の後・go の前) に artifact を同じ inode のまま書き換えると、launcher が
    写した bytes が admission の digest と合わず exec しない。"""
    import hashlib

    host = start_host()
    d = Path(tempfile.mkdtemp(prefix="afx-artifact-"))
    art = d / "cli"
    shutil.copy2(BASE_PY, art)
    r["original_ino"] = os.stat(art).st_ino
    choice = admitted_options(host, "codex", str(art))

    def overwrite() -> None:
        size = os.stat(art).st_size
        with open(art, "r+b") as f:
            f.seek(size // 2)
            f.write(b"\xcc" * 4096)
        r["after_ino"] = os.stat(art).st_ino
        r["after_sha256"] = hashlib.sha256(art.read_bytes()).hexdigest()

    r["mission"] = run_mission(r, host=host, backend="codex", artifact=str(art),
                               probe_cfg={"hold": 0.1}, options=choice.worker_options,
                               on_ready_extra=overwrite)
    r["admitted_artifact"] = choice.decision.artifact.as_dict()
    shutil.rmtree(d, ignore_errors=True)
    leftover_cleanup(host, r)


def sc_changed(r: dict) -> None:
    """admission の後・worker の open の前に artifact が変わると、worker は exec しない。"""
    host = start_host()
    d = Path(tempfile.mkdtemp(prefix="afx-artifact-"))
    art = d / "cli"
    shutil.copy2(BASE_PY, art)
    choice = admitted_options(host, "codex", str(art))
    other = d / "other"
    shutil.copy2(BASE_PY, other)
    with open(other, "ab") as f:
        f.write(b"\0" * 16)
    os.rename(other, art)
    r["mission"] = run_mission(r, host=host, backend="codex", artifact=str(art),
                               probe_cfg={"hold": 0.1}, options=choice.worker_options)
    shutil.rmtree(d, ignore_errors=True)
    leftover_cleanup(host, r)


def sc_gate(r: dict) -> None:
    """worker が reap できなかった扱いで gate が閉じ、次の mission は起動しない。"""
    from agentic_fx.core import improve_admission as IA

    host = start_host()
    choice = admitted_options(host, "codex", BASE_PY)
    real = choice.worker_options["cleanup_protocol"].on_reaped
    r["first"] = run_mission(r, host=host, backend="codex", artifact=BASE_PY,
                             probe_cfg={"hold": 0.1}, options=choice.worker_options,
                             hook_override=lambda n, _reaped, dl: real(n, False, dl))
    adm = IA.ImproveAdmission(canary=lambda c: IA.ProbeOutcome(True),
                              selftest=lambda c: IA.ProbeOutcome(True))
    second = IA.choose_for_mission(make_settings("codex", BASE_PY), REPO, enabled=True,
                                   host=host, admission=adm)
    r["second_refusal"] = second.refusal
    r["second_options"] = sorted(second.worker_options)
    before = set(os.listdir("/proc"))
    res = IA.RefusedRunner(second.refusal).run(None) if second.refusal else None
    r["second_result"] = {"status": res.status, "reason": res.reason} if res else None
    r["children_after_refusal"] = sorted(
        p for p in set(os.listdir("/proc")) - before if p.isdigit()
        and _ppid(p) == os.getpid())
    leftover_cleanup(host, r)


def _ppid(pid: str) -> int:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return int(f.read().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return -1


def fake_credentials() -> None:
    """unit の HOME (tmp) に WorkerRunner が写す認証原本の代役を置く (CLI 役は読まない)。"""
    home = Path.home()
    for rel in (".claude/.credentials.json", ".codex/auth.json"):
        p = home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}")
        p.chmod(0o600)
    (home / ".config/opencode/node_modules").mkdir(parents=True, exist_ok=True)
    (home / ".config/opencode/package.json").write_text("{}")
    (home / ".cache/opencode").mkdir(parents=True, exist_ok=True)


def isolate_transcripts() -> None:
    """製品の WorkerRunner.run が起こす worker の transcript 先を tmp にする。"""
    from agentic_fx.runners import worker_runner as WR

    d = tempfile.mkdtemp(prefix="afx-transcripts-")
    real = WR._mission_worker_env
    WR._mission_worker_env = lambda profile: {**real(profile),
                                              "AGENTIC_FX_MISSION_TRANSCRIPTS_DIR": d}


def admission_context(host, backend: str, artifact: str):
    from agentic_fx.core import improve_admission as IA
    from agentic_fx.core import improve_fingerprint as F

    ident = IA.inspect_artifact(artifact)
    kernel = F.probe_kernel()
    fp = F.compute_fingerprint(backend, ident, kernel)
    return IA.AdmissionContext(backend, artifact, ident, fp, host)


def sc_canary(r: dict, backend: str, flip: str | None = None, mismatch: bool = False) -> None:
    """製品の canary (WorkerRunner.run → 実 mission worker → 同じ launch と cleanup)。"""
    from agentic_fx.core import improve_canary as C

    host = start_host()
    fake_credentials()
    isolate_transcripts()
    settings = make_settings(backend, BASE_PY)
    ctx = admission_context(host, backend, BASE_PY)
    if mismatch:
        # worker に渡す handshake の fingerprint を、別 backend の値 (正しい形で値だけ違う) にする
        other = "claude" if backend != "claude" else "codex"
        wrong = admission_context(host, other, BASE_PY).fingerprint
        assert wrong.aggregate != ctx.fingerprint.aggregate
        import dataclasses
        ctx = dataclasses.replace(ctx, fingerprint=wrong)
    seen: dict = {}

    def on_result(res) -> None:
        seen["status"], seen["reason"], seen["output"] = res.status, res.reason, res.output
        for msg in res.transcript:
            c = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(c, str) and c.startswith('{"_improve_isolation"'):
                seen["report"] = json.loads(c)["_improve_isolation"]

    expected = None
    if flip:
        expected = dict(C.EXPECTED)
        expected[flip] = "ok"
    t0 = time.monotonic()
    out = C.run_canary(ctx, settings=settings, root=REPO, expected=expected,
                       on_result=on_result)
    r["elapsed"] = round(time.monotonic() - t0, 2)
    r["outcome"] = {"ok": out.ok, "transient": out.transient, "detail": out.detail}
    r["mission"] = seen
    r["mission_allowed"] = host.mission_allowed()
    r["blocking_reasons"] = list(host.blocking_reasons())
    r["m_dirs_left"] = sorted(p.name for p in host.root.iterdir() if p.name.startswith("m-"))
    leftover_cleanup(host, r)


def sc_canary_blocks(r: dict, backend: str) -> None:
    """canary の 1 項目の期待値を反転すると admission は safety_canary_failed で、CLI を起こさない。"""
    from agentic_fx.core import improve_admission as IA
    from agentic_fx.core import improve_canary as C

    host = start_host()
    fake_credentials()
    isolate_transcripts()
    settings = make_settings(backend, BASE_PY)
    flipped = dict(C.EXPECTED)
    flipped["listen_ipv4_eperm"] = "ok"
    if backend == "opencode":
        flipped.update({k: "n/a" for k in C.PROXY_ITEMS})
    selftest_calls = []
    seen: dict = {}

    def on_result(res) -> None:
        for msg in res.transcript:
            c = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(c, str) and c.startswith('{"_improve_isolation"'):
                seen["report"] = json.loads(c)["_improve_isolation"]

    adm = IA.ImproveAdmission(
        canary=lambda c: C.run_canary(c, settings=settings, root=REPO, expected=flipped,
                                      on_result=on_result),
        selftest=lambda c: selftest_calls.append(1) or IA.ProbeOutcome(True))
    choice = IA.choose_for_mission(settings, REPO, enabled=True, host=host, admission=adm)
    r["refusal"] = choice.refusal
    r["detail"] = choice.decision.detail if choice.decision else None
    r["selftest_calls"] = len(selftest_calls)
    r["canary_report"] = seen.get("report")
    r["options"] = sorted(choice.worker_options)
    r["mission_allowed"] = host.mission_allowed()
    r["m_dirs_left"] = sorted(p.name for p in host.root.iterdir() if p.name.startswith("m-"))
    leftover_cleanup(host, r)


def sc_saturate(r: dict, kind: str, backend: str) -> None:
    """飽和の注入の下の自己試験 (probe CLI) が失敗すると admission は不可、許可として覚えない。"""
    from agentic_fx.core import improve_admission as IA
    from agentic_fx.core import improve_fingerprint as F

    host = start_host()
    missions: list = []
    if backend == "opencode":
        # proxy を持たない backend は、飽和させる slot も tunnel も無い (直接の宛先は 8080 だけ)
        choice = admitted_options(host, backend, BASE_PY)
        r["mission"] = run_mission(r, host=host, backend=backend, artifact=BASE_PY,
                                   probe_cfg={"inject": kind, "hold": 0.2},
                                   options=choice.worker_options)
        leftover_cleanup(host, r)
        return

    def selftest(ctx):
        options = IA.worker_runner_options(
            IA.AdmissionDecision(True, None, ctx.backend, ctx.fingerprint, ctx.artifact), host)
        m = run_mission(r, host=host, backend=backend, artifact=BASE_PY,
                        probe_cfg={"connect": "saturate", "inject": kind, "hold": 0.2},
                        options=options)
        missions.append(m)
        ok = m["result"]["status"] == "completed"
        return IA.ProbeOutcome(ok, False, "" if ok else str(m["result"]["reason"])[:200])

    adm = IA.ImproveAdmission(canary=lambda c: IA.ProbeOutcome(True), selftest=selftest)
    settings = make_settings(backend, BASE_PY)
    first = IA.choose_for_mission(settings, REPO, enabled=True, host=host, admission=adm)
    second = IA.choose_for_mission(settings, REPO, enabled=True, host=host, admission=adm)
    r["first"] = {"refusal": first.refusal, "detail": first.decision.detail}
    r["second"] = {"refusal": second.refusal, "same": second.decision is first.decision}
    r["selftest_runs"] = len(missions)
    r["mission"] = missions[0] if missions else None
    r["supported"] = sorted(F.SUPPORTED_FINGERPRINTS)
    leftover_cleanup(host, r)


def sc_real_selftest(r: dict, backend: str, artifact: str) -> None:
    """実 CLI の互換性自己試験 (課金枠・llama-swap を使う。`-m realbackend` のときだけ)。"""
    from agentic_fx.core import improve_canary as C

    host = start_host()
    isolate_transcripts()
    settings = make_settings(backend, artifact)
    settings = settings.model_copy(update={"improve": settings.improve.model_copy(update={
        "mission_timeout_sec": REAL_SELFTEST_MISSION_TIMEOUT_SEC})})
    ctx = admission_context(host, backend, artifact)
    t0 = time.monotonic()
    out = C.run_selftest(ctx, settings=settings, root=REPO)
    r["elapsed"] = round(time.monotonic() - t0, 2)
    r["outcome"] = {"ok": out.ok, "transient": out.transient, "detail": out.detail}
    r["mission_allowed"] = host.mission_allowed()
    r["m_dirs_left"] = sorted(p.name for p in host.root.iterdir() if p.name.startswith("m-"))
    leftover_cleanup(host, r)


def sc_early_fault(r: dict) -> None:
    """CLI を起こす前の障害: CLI 無しの mission も §4.2.3 の handoff 前系列で片付ける。"""
    host = start_host()
    choice = admitted_options(host, "codex", BASE_PY)
    r["mission"] = run_mission(r, host=host, backend="codex", artifact=BASE_PY,
                               probe_cfg={"inject": "fault_early", "hold": 0.1},
                               options=choice.worker_options)
    leftover_cleanup(host, r)


def sc_abort(r: dict) -> None:
    """afx が go を送らずに mission をやめる (on_ready の失敗)。worker は deadline を受けて
    go を待つのをやめ、CLI 無しの handoff 前系列で片付けてから終わる。"""
    host = start_host()
    choice = admitted_options(host, "codex", BASE_PY)

    def refuse() -> None:
        raise RuntimeError("injected on_ready failure")

    r["mission"] = run_mission(r, host=host, backend="codex", artifact=BASE_PY,
                               probe_cfg={"hold": 0.1}, options=choice.worker_options,
                               on_ready_extra=refuse)
    leftover_cleanup(host, r)


def sc_fault(r: dict) -> None:
    """supervisor の障害は CLI を止める契機で、mission は sandbox_unavailable で失敗する。"""
    host = start_host()
    choice = admitted_options(host, "codex", BASE_PY)
    r["mission"] = run_mission(r, host=host, backend="codex", artifact=BASE_PY,
                               probe_cfg={"inject": "fault", "hold": 20},
                               options=choice.worker_options)
    leftover_cleanup(host, r)


def sc_system_exit(r: dict) -> None:
    """router を起こした worker の main を SystemExit(3) が抜けると、worker の終了 code は 3。"""
    host = start_host()
    choice = admitted_options(host, "codex", BASE_PY)
    r["mission"] = run_mission(r, host=host, backend="codex", artifact=BASE_PY,
                               probe_cfg={"inject": "system_exit"},
                               options=choice.worker_options)
    leftover_cleanup(host, r)


#: memfd の課金を見る artifact の水増し (CLI の artifact の大きさに近づける)
PADDED_EXTRA_BYTES = 160 * 1024 * 1024


def sc_memory(r: dict, memory_max: str) -> None:
    """python の ELF に 160 MiB を足した artifact を CLI 役にして、mission cgroup の
    memory.current と shmem を観測する。`memory_max` を渡すと mission cgroup の memory.max を
    その値にする (`boundary` は artifact の大きさと launcher の見積りの和)。"""
    host = start_host()
    d = Path(tempfile.mkdtemp(prefix="afx-artifact-"))
    art = d / "cli"
    shutil.copy2(BASE_PY, art)
    with open(art, "ab") as f:
        f.truncate(os.stat(art).st_size + PADDED_EXTRA_BYTES)
    r["artifact_size"] = os.stat(art).st_size
    choice = admitted_options(host, "codex", str(art))
    cfg = {"hold": 1.0}
    if memory_max == "boundary":
        # artifact と launcher の見積りの和が memory.max にちょうど等しい (判定は通る最大)
        from agentic_fx.core import improve_isolation as I
        memory_max = str(r["artifact_size"] + I.ARTIFACT_MEMORY_HEADROOM)
        r["memory_max"] = int(memory_max)
    if memory_max:
        cfg["inject"] = f"memory_max:{int(memory_max)}"
    try:
        r["mission"] = run_mission(r, host=host, backend="codex", artifact=str(art),
                                   probe_cfg=cfg, options=choice.worker_options)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    leftover_cleanup(host, r)


SCENARIOS = {
    "memory": lambda r, arg: sc_memory(r, arg),
    "system_exit": lambda r, arg: sc_system_exit(r),
    "fault": lambda r, arg: sc_fault(r),
    "early_fault": lambda r, arg: sc_early_fault(r),
    "abort": lambda r, arg: sc_abort(r),
    "real_selftest": lambda r, arg: sc_real_selftest(r, *arg.split("@", 1)),
    "canary": lambda r, arg: sc_canary(r, arg or "codex"),
    "canary_mismatch": lambda r, arg: sc_canary(r, arg or "codex", mismatch=True),
    "canary_blocks": lambda r, arg: sc_canary_blocks(r, arg or "codex"),
    "saturate": lambda r, arg: sc_saturate(r, *arg.split("@", 1)),
    "e2e": lambda r, arg: sc_e2e(r, arg or "codex"),
    "renamed": lambda r, arg: sc_renamed(r),
    "in_place": lambda r, arg: sc_in_place(r),
    "changed": lambda r, arg: sc_changed(r),
    "gate": lambda r, arg: sc_gate(r),
}


def main() -> None:
    if sys.argv[1] == "--worker":
        worker_main(sys.argv[2], sys.argv[3])
        return
    scenario, out = sys.argv[1], Path(sys.argv[2])
    r: dict = {"scenario": scenario, "pid": os.getpid()}
    try:
        name, _, arg = scenario.partition(":")
        SCENARIOS[name](r, arg)
    except BaseException:  # noqa: BLE001
        r["error"] = traceback.format_exc()
    save(out, r)


if __name__ == "__main__":
    main()
