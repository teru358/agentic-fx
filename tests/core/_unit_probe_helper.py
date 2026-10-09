"""使い捨ての user service の main として走る helper。

usage: python _unit_probe_helper.py <mode> <out.json>

admission・topology・mission cgroup の回収を自分の unit の cgroup の中だけで実行し、観測を JSON に
書く。書くのは out.json (テストの tmp_path) と自分の unit の cgroup だけ。変更は戻さない
(unit が消えれば cgroup も消え、それをテスト側で確かめる)。
"""
from __future__ import annotations

import json
import os
import resource
import signal
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path

resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

from agentic_fx.core import cgroup_host as ch  # noqa: E402
from agentic_fx.core import unit_admission as ua  # noqa: E402

MODE, OUT = sys.argv[1], Path(sys.argv[2])
R: dict = {"mode": MODE, "pid": os.getpid()}


def save() -> None:
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(R, ensure_ascii=False, indent=1))
    os.replace(tmp, OUT)


def rd(p: Path) -> str:
    return p.read_text().strip()


def self_cgroup() -> str:
    return rd(Path("/proc/self/cgroup")).split("::", 1)[1]


def root_now() -> Path:
    rel = self_cgroup()
    if rel.endswith("/supervisor"):
        rel = rel.rsplit("/", 1)[0]
    return Path("/sys/fs/cgroup" + rel)


def admission_record():
    try:
        adm = ua.admit_unit()
    except ua.AdmissionError as e:
        return None, {"ok": False, "reason": e.reason, "detail": e.detail,
                      "properties": e.properties}
    return adm, {"ok": True, "unit": adm.unit, "properties": adm.properties}


def spawn_sleeper_into(cg: Path) -> subprocess.Popen:
    p = subprocess.Popen(["/bin/sleep", "300"], stdin=subprocess.DEVNULL)
    (cg / "cgroup.procs").write_text(str(p.pid))
    return p


def sleeper_signal(p: subprocess.Popen) -> int | None:
    try:
        rc = p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
        return None
    return -rc if rc < 0 else None


def started_host(adm=None) -> ch.MissionCgroupHost:
    host = ch.MissionCgroupHost()
    if adm is None:
        host.start(deadline=time.monotonic() + 10)
    else:
        host.start(deadline=time.monotonic() + 10, admit=lambda: adm)
    return host


def mode_admit_only() -> None:
    _, R["admission"] = admission_record()


def mode_admit_and_move() -> None:
    root = root_now()
    before = self_cgroup()
    procs_before = rd(root / "cgroup.procs").split()
    adm, R["admission"] = admission_record()
    if adm is None:
        raw: dict = {"self_cgroup_before": before, "root_procs": procs_before}
        try:
            unit = ua.identify_user_unit()
            ch.adopt_topology(unit)
            raw["reason"] = None
        except (ua.AdmissionError, ch.CgroupHostError) as e:
            raw["reason"] = e.reason
        raw["supervisor_exists"] = (root / "supervisor").exists()
        raw["self_cgroup_after"] = self_cgroup()
        R["topology_raw"] = raw
        return
    host = started_host(adm)
    R["topology"] = {
        "root_procs_before": procs_before,
        "self_cgroup_after": self_cgroup(),
        "root_procs_after": rd(root / "cgroup.procs").split(),
        "root_subtree_control_after": rd(root / "cgroup.subtree_control"),
        "reasons": list(host.blocking_reasons()),
    }
    m = root / "m-probe"
    m.mkdir()
    (m / "pids.max").write_text("64")
    (m / "memory.max").write_text(str(256 << 20))
    child = {"pids.max": rd(m / "pids.max"), "memory.max": rd(m / "memory.max")}
    p = spawn_sleeper_into(m)
    child["cleanup_reason"] = host.cleanup_mission("m-probe", time.monotonic() + 5)
    child["sleeper_signal"] = sleeper_signal(p)
    child["exists_after"] = m.exists()
    R["mission_cgroup"] = child
    R["gate"] = {"mission_allowed_after_cleanup": host.mission_allowed()}
    try:
        again = ch.adopt_topology(adm.unit)
        R["topology_again"] = {"ok": True, "root": str(again)}
    except ch.CgroupHostError as e:
        R["topology_again"] = {"ok": False, "reason": e.reason, "detail": e.detail}


def mode_host_start() -> None:
    host = started_host()
    R["mission_allowed"] = host.mission_allowed()
    R["reasons"] = list(host.blocking_reasons())
    R["root"] = str(host.root) if host.root is not None else None


def mode_host_stale() -> None:
    root = root_now()
    m = root / "m-stale"
    m.mkdir()
    p = spawn_sleeper_into(m)
    R["stale_before"] = {"joined": str(p.pid) in rd(m / "cgroup.procs").split(),
                         "populated": [line for line in rd(m / "cgroup.events").splitlines()
                                       if line.startswith("populated")][0]}
    host = started_host()
    R["reasons"] = list(host.blocking_reasons())
    R["mission_allowed"] = host.mission_allowed()
    R["reclaimed"] = list(host.reclaimed)
    R["sleeper_signal"] = sleeper_signal(p)
    R["stale_exists_after"] = m.exists()


def mode_host_busy() -> None:
    root = root_now()
    m = root / "m-busy"
    (m / "inner").mkdir(parents=True)
    host = started_host()
    R["reasons"] = list(host.blocking_reasons())
    R["mission_allowed"] = host.mission_allowed()
    R["busy_exists_after"] = m.exists()
    time.sleep(0.2)
    R["mission_allowed_later"] = host.mission_allowed()
    try:
        host.start(deadline=time.monotonic() + 5)
        R["restart_in_process"] = "allowed"
    except RuntimeError:
        R["restart_in_process"] = "RuntimeError"
    # supervisor/ への自己移動までは済んでいるので、mission 後の経路でも同じ判定になる
    m2 = root / "m-busy2"
    (m2 / "inner").mkdir(parents=True)
    R["cleanup_after_start"] = host.cleanup_mission("m-busy2", time.monotonic() + 5)


def mode_host_deadline_passed() -> None:
    host = started_host()
    R["allowed_before"] = host.mission_allowed()
    m = host.root / "m-late"
    m.mkdir()
    p = spawn_sleeper_into(m)
    t0 = time.monotonic()
    R["cleanup_reason"] = host.cleanup_mission("m-late", time.monotonic() - 0.01)
    R["cleanup_elapsed"] = time.monotonic() - t0
    R["mission_allowed"] = host.mission_allowed()
    R["reasons"] = list(host.blocking_reasons())
    R["sleeper_signal"] = sleeper_signal(p)


def mode_host_worker_not_reaped() -> None:
    host = started_host()
    R["allowed_before"] = host.mission_allowed()
    host.report_worker_not_reaped()
    R["mission_allowed"] = host.mission_allowed()
    R["reasons"] = list(host.blocking_reasons())
    m = host.root / "m-after"
    m.mkdir()
    R["after_cleanup_reason"] = host.cleanup_mission("m-after", time.monotonic() + 5)
    R["allowed_after_successful_cleanup"] = host.mission_allowed()


def mode_host_bad_name() -> None:
    host = started_host()
    R["cleanup_reason"] = host.cleanup_mission("../supervisor", time.monotonic() + 5)
    R["mission_allowed"] = host.mission_allowed()
    R["supervisor_exists"] = (host.root / "supervisor").exists()


def _leftover_supervisor(occupied: bool) -> None:
    root = root_now()
    sup = root / "supervisor"
    sup.mkdir()
    p = spawn_sleeper_into(sup) if occupied else None
    host = started_host()
    R["mission_allowed"] = host.mission_allowed()
    R["reasons"] = list(host.blocking_reasons())
    R["self_cgroup_after"] = self_cgroup()
    if p is not None:
        p.kill()
        p.wait()


def mode_host_leftover_supervisor_empty() -> None:
    _leftover_supervisor(occupied=False)


def mode_host_leftover_supervisor_occupied() -> None:
    _leftover_supervisor(occupied=True)


def mode_stop_ignorer() -> None:
    """main が SIGTERM を無視して自分を SIGSTOP し、子と孫も SIGTERM を無視する。"""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    c = os.fork()
    if c == 0:
        g = os.fork()
        if g == 0:
            while True:
                time.sleep(1)
        OUT.with_suffix(".g").write_text(str(g))
        while True:
            time.sleep(1)
    gfile = OUT.with_suffix(".g")
    deadline = time.monotonic() + 10
    while not gfile.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    g = gfile.read_text().strip()
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(f"{os.getpid()} {c} {g}\n")
    os.replace(tmp, OUT)
    time.sleep(0.2)
    os.kill(os.getpid(), signal.SIGSTOP)
    while True:
        time.sleep(1)


def _send_extend(path: str) -> str:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        s.sendto(b"EXTEND_TIMEOUT_USEC=60000000\nSTATUS=extend\n", path)
        return "sent"
    except OSError as e:
        return f"{type(e).__name__}: {e.strerror}"
    finally:
        s.close()


def mode_extend_timeout() -> None:
    """NotifyAccess=none の main が manager の notify socket へ直接 EXTEND_TIMEOUT を送る。"""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    env_sock = os.environ.get("NOTIFY_SOCKET")
    direct = f"/run/user/{os.getuid()}/systemd/notify"
    R["notify_socket_env"] = env_sock
    R["sent_direct"] = _send_extend(direct) if os.path.exists(direct) else "absent"

    def on_term(signum, frame):
        _send_extend(direct)

    signal.signal(signal.SIGTERM, on_term)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(R) + "\n")
    os.replace(tmp, OUT)
    while True:
        _send_extend(direct)
        time.sleep(0.2)


def mode_host_leftover_supervisor_with_child() -> None:
    root = root_now()
    (root / "supervisor" / "inner").mkdir(parents=True)
    host = started_host()
    R["mission_allowed"] = host.mission_allowed()
    R["reasons"] = list(host.blocking_reasons())
    R["self_cgroup_after"] = self_cgroup()


def mode_host_kill_unwritable() -> None:
    """mission cgroup の cgroup.kill を書けなくし、process が残ったまま deadline に達する。"""
    host = started_host()
    m = host.root / "m-stuck"
    m.mkdir()
    p = spawn_sleeper_into(m)
    os.chmod(m / "cgroup.kill", 0o444)
    R["kill_writable"] = os.access(m / "cgroup.kill", os.W_OK)
    t0 = time.monotonic()
    R["cleanup_reason"] = host.cleanup_mission("m-stuck", time.monotonic() + 0.5)
    R["cleanup_elapsed"] = time.monotonic() - t0
    R["mission_allowed"] = host.mission_allowed()
    R["reasons"] = list(host.blocking_reasons())
    p.kill()
    p.wait()


def mode_host_root_joined_during_move() -> None:
    """自己移動の直後、読み戻しの前に別の process が root に入る。"""
    root = root_now()
    original = ch._write
    joined: list[subprocess.Popen] = []

    def write(path, value):
        original(path, value)
        if path.name == "cgroup.procs" and path.parent.name == "supervisor" and not joined:
            joined.append(spawn_sleeper_into(root))

    ch._write = write
    try:
        host = started_host()
    finally:
        ch._write = original
    R["joined"] = bool(joined)
    R["mission_allowed"] = host.mission_allowed()
    R["reasons"] = list(host.blocking_reasons())
    for p in joined:
        p.kill()
        p.wait()


def mode_host_controllers_disabled_then_adopt_again() -> None:
    host = started_host()
    R["allowed_before"] = host.mission_allowed()
    (host.root / "cgroup.subtree_control").write_text("-pids -memory")
    R["disabled"] = rd(host.root / "cgroup.subtree_control")
    try:
        ch.adopt_topology(host.admission.unit)
        R["again"] = None
    except ch.CgroupHostError as e:
        R["again"] = e.reason
    R["subtree_control_after"] = rd(host.root / "cgroup.subtree_control")


def mode_host_deadline_passed_empty() -> None:
    host = started_host()
    m = host.root / "m-empty"
    m.mkdir()
    R["cleanup_reason"] = host.cleanup_mission("m-empty", time.monotonic() - 0.01)
    R["exists_after"] = m.exists()
    R["reasons"] = list(host.blocking_reasons())


def mode_host_subtree_control_unwritable() -> None:
    root = root_now()
    os.chmod(root / "cgroup.subtree_control", 0o444)
    host = started_host()
    R["mission_allowed"] = host.mission_allowed()
    R["reasons"] = list(host.blocking_reasons())


MODES = {name[len("mode_"):]: fn for name, fn in globals().items() if name.startswith("mode_")}

if __name__ == "__main__":
    if MODE in ("stop_ignorer", "extend_timeout"):
        MODES[MODE]()
    try:
        MODES[MODE]()
    except Exception:  # 観測結果を残して赤の理由を読めるようにする
        R["error"] = traceback.format_exc()
    save()
