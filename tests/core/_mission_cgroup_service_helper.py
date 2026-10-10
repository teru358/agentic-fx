"""使い捨ての user service の main として走る helper (mission cgroup と cleanup 系列の観測)。

usage: python _mission_cgroup_service_helper.py <scenario> <out.json>

起動直後に afx 本体の役で topology を作る (実 `MissionCgroupHost`、admission だけ注入)。
その後は scenario ごとに、自分 (または supervisor/ に居る子 process) が mission worker の役で
`m-<nonce>/` を作り、実 launcher・実 `ConnectSupervisor` で CLI 役を起こし、§4.2.3 の系列を
走らせて観測を JSON に書く。触るのは自分の unit の cgroup と out.json だけ。

worker 役の子 process (`<scenario> <out.json>` の代わりに `--worker <kind> <out.json>`) は、
outer Landlock や WorkerRunner の reap のように afx 本体と別 process が要る場面で使う。
"""
from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import os
import resource
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

from agentic_fx.core import cgroup_host as ch  # noqa: E402
from agentic_fx.core import cli_launch as L  # noqa: E402
from agentic_fx.core import cli_launch_protocol as CLP  # noqa: E402
from agentic_fx.core import connect_supervisor as CS  # noqa: E402
from agentic_fx.core import cli_launcher  # noqa: E402
from agentic_fx.core import mission_cgroup as MCG  # noqa: E402
from agentic_fx.core import mission_cleanup as MC  # noqa: E402
from agentic_fx.core import mission_protocol as MP  # noqa: E402
from agentic_fx.core import unit_admission as ua  # noqa: E402
from agentic_fx.tools import mcp_spool  # noqa: E402


def _cap_inh() -> str:
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("CapInh:"):
                return line.split(":", 1)[1].strip()
    raise RuntimeError("CapInh が無い")


# user service の process は systemd から CapInh に CAP_WAKE_ALARM を受け継ぐ。launcher の資格検査
# (effective / permitted / inheritable が全部 0) を通すため、worker が起動直後に空にする。
CAPS_BEFORE = (_cap_inh(), cli_launcher._capabilities())
cli_launcher.clear_inheritable_caps()
CAPS_AFTER = (_cap_inh(), cli_launcher._capabilities())

HERE = Path(__file__).resolve()
PEER = HERE.with_name("_cli_launch_peer.py")
REPO = HERE.parents[2]
PY = os.path.realpath(sys.executable)
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C"}
MOUNT = "/sys/fs/cgroup"


# --- 共通 ------------------------------------------------------------------------

def save(out: Path, r: dict) -> None:
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(r, ensure_ascii=False, indent=1, default=str))
    os.replace(tmp, out)


def self_rel() -> str:
    with open("/proc/self/cgroup") as f:
        return f.read().strip().split("::", 1)[1]


def start_host() -> ch.MissionCgroupHost:
    unit = self_rel().rsplit("/", 1)[1]
    host = ch.MissionCgroupHost()
    host.start(deadline=time.monotonic() + 10, admit=lambda: ua.UnitAdmission(unit=unit))
    if not host.mission_allowed():
        raise RuntimeError(f"topology を作れない: {host.blocking_reasons()}")
    return host


def new_nonce() -> MP.MissionNonce:
    return MP.MissionNonce(os.urandom(16).hex())


def cg_path(cg: MCG.MissionCgroup) -> Path:
    return Path(MOUNT + cg.rel)


def procs_of(path: Path) -> list[int]:
    try:
        return [int(x) for x in (path / "cgroup.procs").read_text().split()]
    except OSError:
        return []


def fd_table(pid: int | str = "self") -> dict[str, str]:
    out = {}
    base = f"/proc/{pid}/fd"
    for name in os.listdir(base):
        try:
            out[name] = os.readlink(f"{base}/{name}")
        except OSError:
            pass
    return out


def acked() -> MC.AckedDeadline:
    """この process は afx と worker を兼ねる場面がある。WorkerRunner の発行の代わりに同じ
    規則 (今 + CLEANUP_BUDGET_SEC) で deadline を作る。"""
    return MC.AckedDeadline(time.monotonic() + MC.CLEANUP_BUDGET_SEC)


def report_dict(rep: MC.CleanupReport) -> dict:
    t0 = rep.steps[0]["t"] if rep.steps else 0.0
    return {"series": rep.series, "names": rep.names(), "reason": rep.reason,
            "supervisor_state": rep.supervisor_state,
            "supervisor_failure": rep.supervisor_failure,
            "launcher_reaped": rep.launcher_reaped,
            "steps": [{**s, "ms": round((s["t"] - t0) * 1e3, 1)} for s in rep.steps],
            "deadline_left_ms": round((rep.deadline - time.monotonic()) * 1e3, 1)}


def read_json_lines(fd: int, want: int, timeout: float) -> list[dict]:
    end = time.monotonic() + timeout
    buf = b""
    out: list[dict] = []
    while len(out) < want and time.monotonic() < end:
        r, _, _ = select.select([fd], [], [], max(0.0, end - time.monotonic()))
        if not r:
            break
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if line.strip():
                out.append(json.loads(line))
    return out


class ImmediateAck:
    """proxy の同期 handoff の代役: pending を受けて即 ack する (proxy 役の listener は実 socket)。"""

    def __init__(self) -> None:
        self.acked: list[tuple] = []

    def add_pending(self, dup_fd: int) -> object:
        return object()

    def wait_ack(self, entry: object, client: tuple, deadline: float) -> bool:
        self.acked.append(client)
        return True

    def remove_pending(self, entry: object) -> None:
        pass


def blackhole() -> tuple[socket.socket, list[socket.socket]]:
    bl = socket.socket()
    bl.bind(("127.0.0.1", 0))
    bl.listen(0)
    fillers = []
    for _ in range(4):
        f = socket.socket()
        f.setblocking(False)
        f.connect_ex(bl.getsockname())
        fillers.append(f)
    time.sleep(0.2)
    return bl, fillers


def launch(cg: MCG.MissionCgroup, sup: CS.ConnectSupervisor | None, code: str, *args: str,
           nonce: MP.MissionNonce, timeout: float = 10.0, join=None, launcher_cmd=None):
    """実 launcher で CLI 役 (`python -I -S -c code`) を起こす。例外 2 形も結果として返す。"""
    try:
        return L.launch_cli(
            cli_fd=L.open_cli_binary(PY), argv=[PY, "-I", "-S", "-c", code, *args],
            env=ENV, backend="claude", join=join or cg.join,
            handoff=sup.handoff if sup is not None else os.close,
            deadline=time.monotonic() + timeout, kill=MCG.launcher_kill(cg), nonce=nonce.raw,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, launcher_cmd=launcher_cmd)
    except (L.LaunchError, L.LaunchInterrupted) as e:
        return e


def outcome_dict(o) -> dict:
    if isinstance(o, L.LaunchResult):
        return {"kind": "result", "started": o.started, "reason": o.reason, "series": o.series,
                "reaped": o.reaped, "detail": o.detail,
                "launcher_pid": o.launcher_proc.pid if o.launcher_proc else None,
                "returncode": o.launcher_proc.returncode if o.launcher_proc else None}
    return {"kind": type(o).__name__, "series": o.series, "launcher_pid": o.launcher_proc.pid}


def spool_dispatcher(nonce: MP.MissionNonce) -> mcp_spool.SpoolDispatcher:
    root = mcp_spool.SpoolRoot.create(Path.cwd(), nonce.spool_name)
    d = mcp_spool.SpoolDispatcher(root, lambda req, publish: None)
    d.start()
    return d


def finish_host(host: ch.MissionCgroupHost, cg: MCG.MissionCgroup, r: dict) -> None:
    """afx 本体の役: reap 後の保険 kill → populated 0 → 空 → rmdir。"""
    path = cg_path(cg)
    r["procs_before_rmdir"] = procs_of(path)
    cg.close()
    t0 = time.monotonic()
    r["cleanup_mission"] = host.cleanup_mission(cg.name, time.monotonic() + 5)
    r["cleanup_mission_ms"] = round((time.monotonic() - t0) * 1e3, 1)
    r["cgroup_exists_after"] = path.exists()
    r["blocking_reasons"] = list(host.blocking_reasons())


# --- CLI 役のコード (標準ライブラリだけ、-I -S) ------------------------------------

ORPHANS_CODE = r"""
import json, os, sys, time
def emit(**kw):
    os.write(1, (json.dumps(kw) + "\n").encode())
emit(role="cli", pid=os.getpid())
gate_r, gate_w = os.pipe()
for kind in ("plain", "setsid"):
    mid = os.fork()
    if mid == 0:
        if os.fork() == 0:
            if kind == "setsid":
                os.setsid()
            os.read(gate_r, 1)
            emit(role="orphan", kind=kind, pid=os.getpid(), ppid=os.getppid(), sid=os.getsid(0))
            time.sleep(120)
            os._exit(0)
        os._exit(0)
    os.waitpid(mid, 0)
os.write(gate_w, b"xx")
time.sleep(120)
"""

ESCAPE_CODE = r"""
import errno, json, os, time
with open("/proc/self/cgroup") as f:
    rel = f.read().strip().split("::", 1)[1]
own = "/sys/fs/cgroup" + rel
root = os.path.dirname(own)
res = {"role": "probe", "pid": os.getpid(), "cgroup_before": rel}
for name, path, value in (("parent_procs", root + "/supervisor/cgroup.procs", str(os.getpid())),
                          ("root_procs", root + "/cgroup.procs", str(os.getpid())),
                          ("own_procs", own + "/cgroup.procs", str(os.getpid())),
                          ("subtree_control", own + "/cgroup.subtree_control", "+pids"),
                          ("root_subtree_control", root + "/cgroup.subtree_control", "-memory"),
                          ("own_kill", own + "/cgroup.kill", "1"),
                          ("parent_kill", root + "/supervisor/cgroup.kill", "1")):
    try:
        fd = os.open(path, os.O_WRONLY)
        try:
            os.write(fd, value.encode())
            res[name] = "written"
        finally:
            os.close(fd)
    except OSError as e:
        res[name] = errno.errorcode.get(e.errno, str(e.errno))
with open("/proc/self/cgroup") as f:
    res["cgroup_after"] = f.read().strip().split("::", 1)[1]
fds = {}
for n in os.listdir("/proc/self/fd"):
    try:
        fds[n] = os.readlink("/proc/self/fd/" + n)
    except OSError:
        pass
res["fds"] = fds
os.write(1, (json.dumps(res) + "\n").encode())
time.sleep(120)
"""

SLEEP_CODE = "import time; time.sleep(120)"
EXIT_CODE = "print('done')"

STUCK_CODE = r"""
import json, os, socket, sys, time
port = int(sys.argv[1])
def emit(**kw):
    os.write(1, (json.dumps(kw) + "\n").encode())
def connect(tag):
    s = socket.socket()
    emit(role=tag, event="start", pid=os.getpid())
    rc = s.connect_ex(("127.0.0.1", port))
    emit(role=tag, event="done", rc=rc)
emit(role="cli", pid=os.getpid())
if os.fork() == 0:
    connect("A"); time.sleep(120); os._exit(0)
time.sleep(float(sys.argv[2]))
if os.fork() == 0:
    connect("B"); time.sleep(120); os._exit(0)
time.sleep(120)
"""

CONNECT_ONCE_CODE = r"""
import json, os, socket, sys, time
s = socket.socket()
rc = s.connect_ex(("127.0.0.1", int(sys.argv[1])))
os.write(1, (json.dumps({"role": "cli", "rc": rc}) + "\n").encode())
time.sleep(120)
"""


# --- scenario: cgroup ----------------------------------------------------------------

def sc_orphans(r: dict) -> None:
    host = start_host()
    nonce = new_nonce()
    cg = MCG.MissionCgroup.create(nonce.hex)
    r["cgroup"] = cg.name
    r["limits"] = {k: (cg_path(cg) / k).read_text().strip()
                   for k in ("pids.max", "memory.max", "memory.swap.max")}
    r["held_cloexec"] = [bool(fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC)
                         for fd in cg.fds()]
    sup = CS.ConnectSupervisor(CS.Destinations(None))
    o = launch(cg, sup, ORPHANS_CODE, nonce=nonce)
    r["launch"] = outcome_dict(o)
    assert isinstance(o, L.LaunchResult) and o.started, r["launch"]
    lines = read_json_lines(o.launcher_proc.stdout.fileno(), 3, 10)
    r["lines"] = lines
    r["procs_before_kill"] = procs_of(cg_path(cg))
    rep = MC.run_cleanup_series(deadline=acked(), series=MC.AFTER_HANDOFF, cgroup=cg,
                                launcher_proc=o.launcher_proc, supervisor=sup)
    r["series"] = report_dict(rep)
    r["procs_after"] = procs_of(cg_path(cg))
    r["orphans_alive_after"] = [ln["pid"] for ln in lines if ln.get("role") == "orphan"
                                and os.path.exists(f"/proc/{ln['pid']}")
                                and not _is_zombie(ln["pid"])]
    finish_host(host, cg, r)


def _is_zombie(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] == "Z"
    except OSError:
        return True


def sc_escape(r: dict) -> None:
    host = start_host()
    out = Path(sys.argv[2]).with_suffix(".worker.json")
    p = subprocess.run([sys.executable, str(HERE), "--worker", "escape", str(out)],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, timeout=60)
    r["worker_rc"] = p.returncode
    r["worker_stderr"] = p.stderr.decode(errors="replace")[-2000:]
    w = json.loads(out.read_text())
    r["worker"] = w
    path = Path(MOUNT + w["rel"])
    r["procs_before_rmdir"] = procs_of(path)
    r["cleanup_mission"] = host.cleanup_mission(w["name"], time.monotonic() + 5)
    r["cgroup_exists_after"] = path.exists()


def worker_escape(out: Path) -> None:
    """outer Landlock を掛けた worker: path の cgroup 操作は EACCES、保持 fd では動く。"""
    from agentic_fx.core import landlock

    w: dict = {}
    try:
        nonce = new_nonce()
        cg = MCG.MissionCgroup.create(nonce.hex)
        w.update(name=cg.name, rel=cg.rel, held_fds=list(cg.fds()))
        sup = CS.ConnectSupervisor(CS.Destinations(None))
        ro = [Path(p) for p in ("/usr", "/lib", "/lib64", "/etc", "/proc", sys.prefix,
                                sys.base_prefix, str(REPO / "src"), str(HERE.parent))
              if Path(p).exists()]
        landlock.restrict_to(read_only_paths=ro,
                             read_write_paths=[out.parent, Path("/dev")],
                             execute_paths=[Path(sys.base_prefix)],
                             execute_file_paths=landlock.interpreter_files_for([Path(PY)]))
        path = Path(MOUNT + cg.rel)
        for key, flags in (("cgroup.procs", os.O_WRONLY), ("cgroup.kill", os.O_WRONLY),
                           ("cgroup.events", os.O_RDONLY)):
            try:
                os.close(os.open(path / key, flags))
                w[f"path_{key}"] = "opened"
            except OSError as e:
                w[f"path_{key}"] = errno.errorcode.get(e.errno, str(e.errno))
        w["held_populated_before"] = cg.populated()
        o = launch(cg, sup, ESCAPE_CODE, nonce=nonce)
        w["launch"] = outcome_dict(o)
        assert isinstance(o, L.LaunchResult) and o.started, w["launch"]
        lines = read_json_lines(o.launcher_proc.stdout.fileno(), 1, 10)
        w["probe"] = lines[0] if lines else None
        w["held_populated_running"] = cg.populated()
        rep = MC.run_cleanup_series(deadline=acked(), series=MC.AFTER_HANDOFF, cgroup=cg,
                                    launcher_proc=o.launcher_proc, supervisor=sup)
        w["series"] = report_dict(rep)
        w["held_populated_after"] = cg.populated()
    except BaseException as e:  # noqa: BLE001
        w["exception"] = repr(e)
        w["traceback"] = traceback.format_exc()
    out.write_text(json.dumps(w, default=str))


def sc_create_fail(r: dict, kind: str) -> None:
    host = start_host()
    root = host.root
    assert root is not None
    nonce = new_nonce()
    made: list[Path] = []
    try:
        if kind == "eexist":
            pre = root / MCG.cgroup_name(nonce.hex)
            pre.mkdir()
            made.append(pre)
            try:
                MCG.MissionCgroup.create(nonce.hex)
                r["created"] = True
            except MCG.MissionCgroupError as e:
                r.update(reason=e.reason, detail=e.detail, classification=e.classification)
            r["m_dirs"] = sorted(p.name for p in root.iterdir() if p.name.startswith("m-"))
            return
        if kind == "not_supervisor":
            target = root / f"other-{nonce.hex[:8]}"
            target.mkdir()
            made.append(target)
        else:
            nest = root / f"nest-{nonce.hex[:8]}"
            nest.mkdir()
            made.append(nest)
            # pids だけを配り memory を配らない: memory.max が無い mission cgroup ができる
            (nest / "cgroup.subtree_control").write_text("+pids")
            target = nest / "supervisor"
            target.mkdir()
            made.append(target)
        out = Path(sys.argv[2]).with_suffix(".worker.json")
        p = subprocess.run([sys.executable, str(HERE), "--worker", "create_in", str(out),
                            str(target), nonce.hex],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, timeout=60)
        r["worker_rc"] = p.returncode
        r["worker_stderr"] = p.stderr.decode(errors="replace")[-2000:]
        r.update(json.loads(out.read_text()))
        r["m_dirs"] = sorted(p.name for p in target.parent.iterdir() if p.name.startswith("m-"))
    finally:
        for p in reversed(made):
            try:
                p.rmdir()
            except OSError:
                pass


def worker_create_in(out: Path, target: str, nonce_hex: str) -> None:
    w: dict = {}
    (Path(target) / "cgroup.procs").write_text(str(os.getpid()))
    w["self_cgroup"] = self_rel()
    try:
        cg = MCG.MissionCgroup.create(nonce_hex)
        w["created"] = True
        cg.close()
        os.rmdir(MOUNT + cg.rel)
    except MCG.MissionCgroupError as e:
        w.update(reason=e.reason, detail=e.detail, classification=e.classification)
    out.write_text(json.dumps(w))


def sc_join_fail(r: dict, kind: str) -> None:
    host = start_host()
    nonce = new_nonce()
    cg = MCG.MissionCgroup.create(nonce.hex)
    sup = CS.ConnectSupervisor(CS.Destinations(None))
    seen: dict = {}

    if kind == "esrch":
        def join(pid: int) -> None:
            os.kill(pid, signal.SIGKILL)
            end = time.monotonic() + 5
            while not _is_zombie(pid) and time.monotonic() < end:
                time.sleep(0.01)
            seen["zombie"] = _is_zombie(pid)
            try:
                cg.join(pid)
            except MCG.MissionCgroupError as e:
                seen["detail"] = e.detail
                raise
    else:
        # 書き込みは mission cgroup、読み戻しは別の (空の) cgroup の fd: pid が見えない
        other = MCG.MissionCgroup.create(new_nonce().hex)
        seen["other_fds"] = other.fds()
        wrong = MCG.MissionCgroup(cg.name, cg.rel, *cg.fds()[:3], other.fds()[3])

        def join(pid: int) -> None:
            try:
                wrong.join(pid)
            except MCG.MissionCgroupError as e:
                seen["detail"] = e.detail
                seen["procs_after_write"] = procs_of(cg_path(cg))
                raise
    o = launch(cg, sup, SLEEP_CODE, nonce=nonce, join=join)
    r["launch"] = outcome_dict(o)
    r["join"] = seen
    rep = MC.cleanup_after_launch(o, acquire=acked, cgroup=cg, supervisor=sup)
    r["series"] = report_dict(rep)
    if kind == "mismatch":
        other_path = cg_path(other)
        other.close()
        os.rmdir(other_path)
    finish_host(host, cg, r)


def sc_launch_after_clear(r: dict) -> None:
    """CapInh を空にした後の launch_cli (launcher の資格検査) が通り、後始末まで進む。"""
    host = start_host()
    nonce = new_nonce()
    cg = MCG.MissionCgroup.create(nonce.hex)
    sup = CS.ConnectSupervisor(CS.Destinations(None))
    o = launch(cg, sup, SLEEP_CODE, nonce=nonce)
    r["launch"] = outcome_dict(o)
    assert isinstance(o, L.LaunchResult) and o.started, r["launch"]
    rep = MC.run_cleanup_series(deadline=acked(), series=MC.AFTER_HANDOFF, cgroup=cg,
                                launcher_proc=o.launcher_proc, supervisor=sup)
    r["series"] = report_dict(rep)
    finish_host(host, cg, r)


def sc_join_no_proc(r: dict) -> None:
    host = start_host()
    out = Path(sys.argv[2]).with_suffix(".worker.json")
    p = subprocess.run([sys.executable, str(HERE), "--worker", "join_no_proc", str(out)],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, timeout=60)
    r["worker_rc"] = p.returncode
    r["worker_stderr"] = p.stderr.decode(errors="replace")[-2000:]
    w = json.loads(out.read_text())
    r["worker"] = w
    path = Path(MOUNT + w["rel"])
    r["cleanup_mission"] = host.cleanup_mission(w["name"], time.monotonic() + 5)
    r["cgroup_exists_after"] = path.exists()


def worker_join_no_proc(out: Path) -> None:
    """/proc を許可しない outer Landlock の下でも、保持 fd の読み戻しで join が成立する。"""
    from agentic_fx.core import landlock

    w: dict = {}
    child = None
    try:
        nonce = new_nonce()
        cg = MCG.MissionCgroup.create(nonce.hex)
        w.update(name=cg.name, rel=cg.rel)
        child = subprocess.Popen([PY, "-c", "import time; time.sleep(60)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        ro = [Path(p) for p in ("/usr", "/lib", "/lib64", "/etc", sys.prefix,
                                sys.base_prefix, str(REPO / "src"), str(HERE.parent))
              if Path(p).exists()]
        landlock.restrict_to(read_only_paths=ro,
                             read_write_paths=[out.parent, Path("/dev")],
                             execute_paths=[Path(sys.base_prefix)],
                             execute_file_paths=landlock.interpreter_files_for([Path(PY)]))
        try:
            open(f"/proc/{child.pid}/cgroup").close()
            w["proc_read"] = "opened"
        except OSError as e:
            w["proc_read"] = errno.errorcode.get(e.errno, str(e.errno))
        cg.join(child.pid)
        w["joined"] = True
        cg.kill()
        w["unpopulated"] = cg.wait_unpopulated(time.monotonic() + 5)
        child.wait(10)
        cg.close()
    except BaseException as e:  # noqa: BLE001
        w["exception"] = repr(e)
        w["traceback"] = traceback.format_exc()
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(10)
    out.write_text(json.dumps(w, default=str))


# --- scenario: cleanup 系列 ----------------------------------------------------------

def sc_series(r: dict, row: str) -> None:
    host = start_host()
    nonce = new_nonce()
    cg = MCG.MissionCgroup.create(nonce.hex)
    dispatcher = spool_dispatcher(nonce)
    holders: list = []
    if row == "receiver_anomaly":
        def at(point: str, nid: int) -> None:
            if point == "receiver_loop":
                raise RuntimeError("injected receiver failure")
        sup = CS.ConnectSupervisor(CS.Destinations(None), faults=CS.Faults(at=at))
    elif row == "stuck":
        bl, fillers = blackhole()
        holders += [bl, fillers]
        never = threading.Event()

        def hold(point: str, nid: int) -> None:
            if point == "after_commit":
                never.wait(60)
        sup = CS.ConnectSupervisor(CS.Destinations(None, bl.getsockname()[1]),
                                   faults=CS.Faults(at=hold))
    else:
        sup = CS.ConnectSupervisor(CS.Destinations(None))

    if row == "normal":
        o = launch(cg, sup, EXIT_CODE, nonce=nonce)
        assert isinstance(o, L.LaunchResult) and o.started, outcome_dict(o)
        # CLI の正常終了は呼び出し側 (CliRunner) が wait で検出する
        o.launcher_proc.wait(10)
    elif row == "receiver_anomaly":
        o = launch(cg, sup, SLEEP_CODE, nonce=nonce)
        r["terminal_before_cleanup"] = sup.wait_terminal(5)
    elif row == "stuck":
        o = launch(cg, sup, STUCK_CODE, str(holders[0].getsockname()[1]), "60", nonce=nonce)
        r["terminal_before_cleanup"] = sup.wait_terminal(10)
        r["state_before_cleanup"] = sup.state
    elif row == "before_handoff":
        o = launch(cg, sup, SLEEP_CODE, nonce=nonce, timeout=2.0,
                   launcher_cmd=[sys.executable, str(PEER), "no_prepared"])
    elif row == "after_handoff_failure":
        o = launch(cg, sup, SLEEP_CODE, nonce=nonce, timeout=2.0,
                   launcher_cmd=[sys.executable, str(PEER), "exec_timeout"])
    else:
        raise ValueError(row)
    r["launch"] = outcome_dict(o)
    r["procs_before_cleanup"] = procs_of(cg_path(cg))
    listener = sup._listener
    rep = MC.cleanup_after_launch(o, acquire=acked, cgroup=cg, supervisor=sup,
                                  dispatcher=dispatcher)
    r["series"] = report_dict(rep)
    r["efd_after"] = sup._efd
    r["listener_fd"] = listener
    r["listener_open_after"] = (listener >= 0 and
                                fd_table().get(str(listener)) == L.SECCOMP_NOTIFY_LINK)
    r["dispatcher_stopped"] = dispatcher.stopped
    finish_host(host, cg, r)


def sc_before_handoff(r: dict, when: str) -> None:
    host = start_host()
    nonce = new_nonce()
    cg = MCG.MissionCgroup.create(nonce.hex)
    sup = CS.ConnectSupervisor(CS.Destinations(None))
    peer = {"pre_joined_stop": "silent", "pre_joined_death": "die_before_hello",
            "after_joined": "no_prepared"}[when]
    before = fd_table()
    t0 = time.monotonic()
    o = launch(cg, sup, SLEEP_CODE, nonce=nonce, timeout=1.5,
               launcher_cmd=[sys.executable, str(PEER), peer])
    r["launch_ms"] = round((time.monotonic() - t0) * 1e3, 1)
    r["launch"] = outcome_dict(o)
    rep = MC.cleanup_after_launch(o, acquire=acked, cgroup=cg, supervisor=sup)
    r["series"] = report_dict(rep)
    # CLI の stdout pipe は呼び出し側 (CliRunner) の持ち物。比較の対象外にする
    if o.launcher_proc is not None and o.launcher_proc.stdout is not None:
        o.launcher_proc.stdout.close()
    after = fd_table()
    r["new_fds"] = {k: v for k, v in after.items() if k not in before}
    finish_host(host, cg, r)


def sc_launch_exc(r: dict, kind: str, when: str) -> None:
    host = start_host()
    nonce = new_nonce()
    cg = MCG.MissionCgroup.create(nonce.hex)
    sup = CS.ConnectSupervisor(CS.Destinations(None))
    exc = KeyboardInterrupt if kind == "LaunchInterrupted" else RuntimeError
    join = None
    if when == "before":
        if kind == "LaunchInterrupted":
            def join(pid: int) -> None:
                cg.join(pid)
                raise KeyboardInterrupt
        else:
            def boom(*a, **k):
                raise ValueError("injected")
            CLP.encode_joined = boom
    else:
        def boom2(pid):
            raise exc("injected")
        L._exe_identity = boom2
    before = fd_table()
    o = launch(cg, sup, SLEEP_CODE, nonce=nonce, join=join)
    r["launch"] = outcome_dict(o)
    r["procs_before_cleanup"] = procs_of(cg_path(cg))
    try:
        rep = MC.cleanup_after_launch(o, acquire=acked, cgroup=cg, supervisor=sup)
        r["reraised"] = None
    except L.LaunchInterrupted as e:
        r["reraised"] = type(e).__name__
        rep = None
    if rep is not None:
        r["series"] = report_dict(rep)
    proc = o.launcher_proc
    r["launcher_returncode"] = proc.returncode
    r["procs_after"] = procs_of(cg_path(cg))
    if proc.stdout is not None:
        proc.stdout.close()
    r["new_fds"] = {k: v for k, v in fd_table().items() if k not in before}
    finish_host(host, cg, r)


def sc_stop_join_release(r: dict, state: str) -> None:
    host = start_host()
    out = Path(sys.argv[2]).with_suffix(".worker.json")
    p = subprocess.Popen([sys.executable, str(HERE), "--worker", f"series_{state}", str(out)],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL)
    lines = read_json_lines(p.stdout.fileno(), 1, 60)
    w = lines[0] if lines else {}
    r["worker"] = w
    lfd = w.get("listener_fd", -1)
    try:
        r["listener_in_worker_before_exit"] = os.readlink(f"/proc/{p.pid}/fd/{lfd}")
    except OSError as e:
        r["listener_in_worker_before_exit"] = errno.errorcode.get(e.errno)
    p.stdin.write(b"exit\n")
    p.stdin.flush()
    r["worker_rc"] = p.wait(30)
    r["worker_fd_dir_after_exit"] = os.path.exists(f"/proc/{p.pid}/fd")
    path = Path(MOUNT + w["rel"])
    r["cleanup_mission"] = host.cleanup_mission(w["name"], time.monotonic() + 5)
    r["cgroup_exists_after"] = path.exists()


def worker_series(state: str) -> None:
    w: dict = {}
    nonce = new_nonce()
    cg = MCG.MissionCgroup.create(nonce.hex)
    w.update(name=cg.name, rel=cg.rel)
    if state == "stuck":
        bl, fillers = blackhole()
        never = threading.Event()

        def hold(point: str, nid: int) -> None:
            if point == "after_commit":
                never.wait(60)
        sup = CS.ConnectSupervisor(CS.Destinations(None, bl.getsockname()[1]),
                                   faults=CS.Faults(at=hold))
        o = launch(cg, sup, STUCK_CODE, str(bl.getsockname()[1]), "60", nonce=nonce)
        w["terminal"] = sup.wait_terminal(10)
    else:
        sup = CS.ConnectSupervisor(CS.Destinations(None))
        o = launch(cg, sup, SLEEP_CODE, nonce=nonce)
    efd = sup._efd
    w["listener_fd"] = sup._listener
    released: list[bool] = []
    orig_release = sup.release
    sup.release = lambda: (released.append(True), orig_release())[1]  # type: ignore[method-assign]
    rep = MC.cleanup_after_launch(o, acquire=acked, cgroup=cg, supervisor=sup)
    w["series"] = report_dict(rep)
    w["release_called"] = bool(released)
    w["efd_after"] = sup._efd
    w["efd_fd_open"] = str(efd) in fd_table()
    w["listener_open_after_series"] = fd_table().get(str(w["listener_fd"])) == L.SECCOMP_NOTIFY_LINK
    cg.close()
    sys.stdout.write(json.dumps(w, default=str) + "\n")
    sys.stdout.flush()
    sys.stdin.readline()
    os._exit(0)


def sc_stop_after_commit(r: dict) -> None:
    host = start_host()
    nonce = new_nonce()
    cg = MCG.MissionCgroup.create(nonce.hex)
    proxy = socket.socket()
    proxy.bind(("127.0.0.1", 0))
    proxy.listen(8)
    proxy.setblocking(False)
    reached = threading.Event()
    release = threading.Event()

    def at(point: str, nid: int) -> None:
        if point == "after_commit":
            reached.set()
            release.wait(10)
    ack = ImmediateAck()
    sup = CS.ConnectSupervisor(CS.Destinations(proxy.getsockname()[1]), proxy_handoff=ack,
                               faults=CS.Faults(at=at))
    o = launch(cg, sup, CONNECT_ONCE_CODE, str(proxy.getsockname()[1]), nonce=nonce)
    r["launch"] = outcome_dict(o)
    r["reached"] = reached.wait(10)
    stop_result: list[str] = []
    stopper = threading.Thread(target=lambda: stop_result.append(
        sup.stop(time.monotonic() + 5)))
    stopper.start()
    end = time.monotonic() + 2
    while sup.state == CS.RUNNING and time.monotonic() < end:
        time.sleep(0.005)
    r["state_when_released"] = sup.state
    release.set()
    lines = read_json_lines(o.launcher_proc.stdout.fileno(), 1, 10)
    r["cli"] = lines[0] if lines else None
    stopper.join(10)
    r["stop_result"] = stop_result
    accepted = 0
    while True:
        try:
            c, _ = proxy.accept()
            c.close()
            accepted += 1
        except BlockingIOError:
            break
    r["proxy_accepted"] = accepted
    r["acked"] = len(ack.acked)
    rep = MC.cleanup_after_launch(o, acquire=acked, cgroup=cg, supervisor=sup)
    r["series"] = report_dict(rep)
    finish_host(host, cg, r)


# --- scenario: WorkerRunner と worker process (受入 43) ----------------------------------

def sc_runner(r: dict, mode: str) -> None:
    """afx 本体の役: 製品の WorkerRunner (cleanup protocol 有効) で worker 役を起こし、
    reap 後の hook から実 MissionCgroupHost の後始末 (または gate) を呼ぶ。"""
    from datetime import datetime, timezone

    from agentic_fx.config import load_settings
    from agentic_fx.core.contracts import FixedClock
    from agentic_fx.runners.base import Mission
    from agentic_fx.runners.worker_runner import WorkerRunner

    host = start_host()
    settings = load_settings(REPO / "config" / "settings.yaml.example")
    w = settings.worker.model_copy(update={"worker_grace_sec": 60.0,
                                           "worker_startup_timeout_sec": 30})
    hook: list = []

    def on_reaped(name: str, reaped: bool, deadline: float) -> None:
        entry = {"name": name, "reaped": reaped, "left_ms": round(
            (deadline - time.monotonic()) * 1e3, 1)}
        path = host.root / name
        entry["exists_before"] = path.exists()
        entry["procs_before"] = procs_of(path)
        if reaped:
            t0 = time.monotonic()
            entry["cleanup_mission"] = host.cleanup_mission(name, deadline)
            entry["cleanup_ms"] = round((time.monotonic() - t0) * 1e3, 1)
        else:
            host.report_worker_not_reaped()
        entry["exists_after"] = path.exists()
        entry["left_after_ms"] = round((deadline - time.monotonic()) * 1e3, 1)
        hook.append(entry)

    protocol = MC.CleanupProtocol(on_reaped=on_reaped,
                                  inject_rewait_timeout=(mode == "hang_unreaped"))
    runner = WorkerRunner(root=Path.cwd(), settings=settings,
                          clock=FixedClock(datetime(2026, 8, 4, tzinfo=timezone.utc)),
                          rag=None, worker_profile="improve", cleanup_protocol=protocol)
    out = Path(sys.argv[2]).with_suffix(".worker.jsonl")
    worker_mode = "normal" if mode == "stuck" else "hang"
    proc = subprocess.Popen([sys.executable, str(HERE), "--worker", f"runner_{worker_mode}",
                             str(out)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    mission = Mission(prompt="p", tools=[], output_schema={}, max_turns=1, timeout_sec=60)
    try:
        res = runner._run_with_child(proc, mission, w)
        r["result"] = {"status": res.status, "reason": res.reason, "output": res.output}
    finally:
        if proc.returncode is None:
            proc.kill()
            proc.wait(10)
    r["worker_returncode"] = proc.returncode
    r["events"] = protocol.events
    r["hook"] = hook
    r["worker_notes"] = ([json.loads(x) for x in out.read_text().splitlines() if x]
                         if out.exists() else [])
    r["blocking_reasons"] = list(host.blocking_reasons())
    # 試験の後片付け (gate が閉じて rmdir されなかった cgroup も残さない)
    for e in hook:
        path = host.root / e["name"]
        if path.exists():
            try:
                ch.remove_mission_cgroup(path, time.monotonic() + 5)
            except ch.CgroupHostError as err:
                r["leftover_cleanup"] = err.reason
    r["m_dirs_left"] = sorted(p.name for p in host.root.iterdir() if p.name.startswith("m-"))


def worker_runner(mode: str, out: Path) -> None:
    """worker 役: handshake の nonce で mission cgroup を作り、STUCK を起こしてから
    cleanup を要求する。`hang` は ACK の後、系列の前に GIL を握って止まる。"""
    from agentic_fx import mission_worker as MW

    send_lock = threading.Lock()
    seq = [0]
    stdout = sys.stdout.buffer

    def note(event: str, **kw) -> None:
        with open(out, "a") as f:
            f.write(json.dumps({"event": event, "t": time.monotonic(), **kw}, default=str)
                    + "\n")

    def send(frame: dict) -> None:
        with send_lock:
            seq[0] += 1
            stdout.write(MP.encode_frame({**frame, "seq": seq[0]}))
            stdout.flush()

    handshake = MP.read_frame(sys.stdin.buffer)
    nonce = MW._accept_mission_nonce(handshake)
    cg = MCG.MissionCgroup.create(nonce.hex)
    note("cgroup", name=cg.name)
    bl, fillers = blackhole()
    port = bl.getsockname()[1]
    never = threading.Event()

    def hold(point: str, nid: int) -> None:
        if point == "after_commit":
            never.wait(120)
    sup = CS.ConnectSupervisor(CS.Destinations(None, port), faults=CS.Faults(at=hold))
    channel = MC.WorkerCleanupChannel(send)
    go = threading.Event()

    def reader() -> None:
        while True:
            frame = MP.read_frame(sys.stdin.buffer)
            if frame is None:
                return
            if frame.get("type") == "go":
                go.set()
            elif frame.get("type") == MP.CLEANUP_DEADLINE:
                note("deadline_frame", deadline=frame.get("deadline"))
                channel.deliver(frame)

    threading.Thread(target=reader, daemon=True).start()
    send({"type": "ready", "ok": True})
    go.wait(10)
    o = launch(cg, sup, STUCK_CODE, str(port), "4.5", nonce=nonce)
    note("launch", **outcome_dict(o))
    cli_fd = o.launcher_proc.stdout.fileno()
    note("stuck", terminal=sup.wait_terminal(10), state=sup.state,
         failure=sup.failure.reason if sup.failure else None)
    lines = read_json_lines(cli_fd, 3, 10)  # cli, A start, B start (A / B の done は来ない)
    time.sleep(0.5)
    lines += read_json_lines(cli_fd, 1, 0.3)
    note("cli_lines", lines=lines)
    lfd = sup._listener
    note("listener", fd=lfd, open=fd_table().get(str(lfd)) == L.SECCOMP_NOTIFY_LINK)
    deadline = channel.acquire("supervisor_stuck")
    note("acked", deadline=deadline.value)
    if mode == "hang":
        note("hang")
        ctypes.PyDLL(None).sleep(60)
    rep = MC.cleanup_after_launch(o, acquire=lambda: deadline, cgroup=cg, supervisor=sup)
    note("series", **report_dict(rep))
    note("listener_after", open=fd_table().get(str(lfd)) == L.SECCOMP_NOTIFY_LINK)
    send({"type": "result", "status": "failed", "output": None, "reason": "supervisor_stalled"})
    os._exit(0)


SCENARIOS = {
    "orphans": sc_orphans,
    "escape": sc_escape,
    "stop_after_commit": sc_stop_after_commit,
}


def main() -> None:
    if sys.argv[1] == "--worker":
        kind, out = sys.argv[2], Path(sys.argv[3])
        if kind == "escape":
            worker_escape(out)
        elif kind == "join_no_proc":
            worker_join_no_proc(out)
        elif kind == "create_in":
            worker_create_in(out, sys.argv[4], sys.argv[5])
        elif kind.startswith("series_"):
            worker_series(kind[len("series_"):])
        elif kind.startswith("runner_"):
            worker_runner(kind[len("runner_"):], out)
        else:
            raise SystemExit(f"unknown worker {kind}")
        return
    scenario, out = sys.argv[1], Path(sys.argv[2])
    r: dict = {"scenario": scenario, "pid": os.getpid(),
               "caps_before": CAPS_BEFORE, "caps_after": CAPS_AFTER}
    try:
        name, _, arg = scenario.partition(":")
        if name in SCENARIOS:
            SCENARIOS[name](r)
        elif name == "create_fail":
            sc_create_fail(r, arg)
        elif name == "launch_after_clear":
            sc_launch_after_clear(r)
        elif name == "join_no_proc":
            sc_join_no_proc(r)
        elif name == "join_fail":
            sc_join_fail(r, arg)
        elif name == "series":
            sc_series(r, arg)
        elif name == "before_handoff":
            sc_before_handoff(r, arg)
        elif name == "launch_exc":
            kind, _, when = arg.partition(":")
            sc_launch_exc(r, kind, when)
        elif name == "stop_join_release":
            sc_stop_join_release(r, arg)
        elif name == "runner":
            sc_runner(r, arg)
        else:
            raise SystemExit(f"unknown scenario {scenario}")
    except BaseException as e:  # noqa: BLE001
        r["exception"] = repr(e)
        r["traceback"] = traceback.format_exc()
    save(out, r)


if __name__ == "__main__":
    main()
