"""systemd 上の ops unit の契約を、実 user manager の使い捨て unit で観測する。

偽 afx は instance flock と tick 計数 (file) を持ち、指定があれば遅れて UDS を開く。
偽 ops は Type=notify の readiness と意図的な crash を持つ。名前に一時 id を持つ unit だけを
作り、終了時に必ず stop・削除する。実 afx.service や実 home の unit には触れない。
"""
from __future__ import annotations

import json
import os
import resource
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_systemd_real import Env, _ctl, _lock_free, _manager_available, _show, _wait

from agentic_fx.ops.keys import initialize, key_dir
from agentic_fx.ops_setup.stages import read_stage
from agentic_fx.ops_setup.systemd import SystemdError

pytestmark = pytest.mark.skipif(
    not (_manager_available() and os.environ.get("XDG_RUNTIME_DIR")),
    reason="user manager か XDG_RUNTIME_DIR が無い")

_FAKE_OPS = """#!{python}
import json, os, socket, sys, time
from pathlib import Path
spec = json.loads(Path(__file__ + ".json").read_text())
root = Path(spec["root"])
with open(root / "ops_starts", "a") as handle:
    handle.write(f"{{time.time()}}\\n")
mode = spec["mode"]
if mode == "serve":
    deadline = time.monotonic() + spec["deadline"]
    while True:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.connect(spec["sock"])
        except OSError:
            client.close()
            if time.monotonic() >= deadline:
                (root / "ops_exit").write_text("3")
                sys.exit(3)
            time.sleep(0.1)
            continue
        client.sendall(b"T" * 16)
        client.close()
        break
addr = os.environ["NOTIFY_SOCKET"]
if addr.startswith("@"):
    addr = "\\0" + addr[1:]
notify = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
notify.connect(addr)
notify.sendall(b"READY=1")
notify.close()
if mode == "readycrash":
    time.sleep(spec["crash_after"])
    sys.exit(1)
time.sleep(600)
"""


_HERE = Path(__file__).resolve().parent
_AFX_PROCESS = _HERE / "_ac12_afx_process.py"
_WEB_LAUNCHER = _HERE / "_ac12_web_launcher.py"
_AFX_OPS_PYTHON = _HERE.parents[1] / "afx-ops" / ".venv" / "bin" / "python"


class Ac12Env(Env):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        self.fake_ops = Path(self.paths.ops_exec)
        self.fake_afx = tmp_path / "fake-afx"
        self.fake_afx.write_text(f"#!{sys.executable}\n" + _AFX_PROCESS.read_text())
        self.fake_ops.write_text(_FAKE_OPS.format(python=sys.executable))
        for path in (self.fake_afx, self.fake_ops):
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        (self.root / "data").mkdir(exist_ok=True)
        self.sock = self.root / "srv.sock"
        assert len(str(self.sock)) < 100
        self.ops_mode(mode="ready")

    def ops_mode(self, **spec) -> None:
        spec = {"root": str(self.root), "sock": str(self.sock), **spec}
        Path(str(self.fake_ops) + ".json").write_text(json.dumps(spec))

    def tick(self) -> tuple[int, int]:
        pid, count = (self.root / "tick").read_text().split()
        return int(pid), int(count)

    def ops_starts(self) -> list[float]:
        path = self.root / "ops_starts"
        return [float(line) for line in path.read_text().split()] if path.exists() else []

    def token_bytes(self) -> int:
        path = self.root / "token_bytes"
        return sum(int(line) for line in path.read_text().split()) if path.exists() else 0

    def restart_afx_with(self, uds_delay: float | None, *, mode: str = "stub", **extra) -> None:
        """afx の代わりの process を、指定の mode で作り直す (uds_delay が None なら UDS を出さない)。"""
        spec = {"mode": "none" if uds_delay is None else mode, **extra}
        if uds_delay is not None:
            spec["delay"] = uds_delay
        (self.root / "afx_mode.json").write_text(json.dumps(spec))
        assert _ctl("restart", self.afx).returncode == 0
        assert _wait(lambda: not _lock_free(self.root))


@pytest.fixture
def ac(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    e = Ac12Env(tmp_path)
    try:
        initialize(key_dir(e.root), root=e.root)
        _ctl("reset-failed", e.ops("bot"))
        assert _ctl("start", e.afx).returncode == 0
        assert _wait(lambda: not _lock_free(e.root))
        assert _wait(lambda: (e.root / "tick").exists())
        yield e
    finally:
        problems = e.cleanup()
        if problems:
            import warnings
            warnings.warn("disposable unit の後始末で失敗: " + "; ".join(problems), stacklevel=1)
        assert e.leftovers() == []


def _ticks_advance(e: Ac12Env, pid: int) -> bool:
    """同じ afx process の tick が、少し待って増えている。"""
    first_pid, first = e.tick()
    time.sleep(0.6)
    second_pid, second = e.tick()
    return first_pid == second_pid == pid and second >= first + 3


def test_one_enabled_feature_has_one_ops_unit_and_expected_properties(ac):
    assert ac.setup().enable("bot").stage == "enabled"

    props = _show(ac.ops("bot"), "Type", "Requisite", "PartOf", "After", "Restart", "RestartUSec",
                  "StartLimitIntervalUSec", "StartLimitBurst", "NoNewPrivileges", "KillMode",
                  "ExecStart", "BindsTo", "ActiveState")
    assert props["Type"] == "notify"
    assert ac.afx in props["Requisite"].split()
    assert ac.afx in props["PartOf"].split()
    assert ac.afx in props["After"].split()
    assert props["BindsTo"] == ""
    assert props["Restart"] == "on-failure"
    assert props["RestartUSec"] == "5s"
    assert props["StartLimitIntervalUSec"] == "1min"
    assert props["StartLimitBurst"] == "3"
    assert props["NoNewPrivileges"] == "yes"
    assert props["KillMode"] == "control-group"
    assert f"path={ac.fake_ops} ;" in props["ExecStart"]
    assert f"argv[]={ac.fake_ops} bot ;" in props["ExecStart"]
    assert props["ActiveState"] == "active"
    listed = _ctl("list-units", "--user", "--type=service", "--all", "--no-legend", "--plain",
                  f"{ac.prefix}@*").stdout.split()
    assert [name for name in listed if name.endswith(".service")] == [ac.ops("bot")]


def test_stopping_or_crashing_ops_does_not_stop_afx_tick(ac):
    ac.setup().enable("bot")
    pid, _ = ac.tick()
    assert _ticks_advance(ac, pid)

    assert _ctl("stop", ac.ops("bot")).returncode == 0
    assert _show(ac.ops("bot"), "ActiveState")["ActiveState"] == "inactive"
    assert _ticks_advance(ac, pid)

    assert _ctl("start", ac.ops("bot")).returncode == 0
    ops_pid = int(_show(ac.ops("bot"), "MainPID")["MainPID"])
    os.kill(ops_pid, signal.SIGKILL)
    assert _wait(lambda: (lambda p: p["ActiveState"] == "active" and p["MainPID"] not in (str(ops_pid), "0"))(
        _show(ac.ops("bot"), "ActiveState", "MainPID")), seconds=20)
    assert _ticks_advance(ac, pid)
    assert _show(ac.afx, "ActiveState", "MainPID") == {"ActiveState": "active", "MainPID": str(pid)}


def _ops_replaced(ac, before: dict[str, str]) -> bool:
    now = _show(ac.ops("bot"), "ActiveState", "MainPID", "InvocationID")
    return (now["ActiveState"] == "active" and now["MainPID"] not in (before["MainPID"], "0")
            and now["InvocationID"] != before["InvocationID"])


def test_afx_stop_restart_and_crash_restarts_ops(ac):
    ac.setup().enable("bot")
    observed: dict[str, object] = {}

    def snapshot() -> dict[str, str]:
        return _show(ac.ops("bot"), "ActiveState", "MainPID", "InvocationID", "NRestarts")

    # stop: ops も止まり、afx を start し直すと Wants で新しい MainPID が上がる
    before = snapshot()
    assert _ctl("stop", ac.afx).returncode == 0
    assert _wait(lambda: _show(ac.ops("bot"), "ActiveState")["ActiveState"] == "inactive")
    assert _ctl("start", ac.afx).returncode == 0
    assert _wait(lambda: _ops_replaced(ac, before), seconds=20)
    observed["stop"] = snapshot()
    assert observed["stop"]["NRestarts"] == "0"

    # explicit restart
    before = snapshot()
    assert _ctl("restart", ac.afx).returncode == 0
    assert _wait(lambda: _ops_replaced(ac, before), seconds=20)
    observed["restart"] = snapshot()
    assert observed["restart"]["NRestarts"] == "0"

    # crash: afx が落ちている窓で ops が一時 active なのを観測し、afx 復帰後に入れ替わる
    before = snapshot()
    afx_restarts = _show(ac.afx, "NRestarts")["NRestarts"]
    assert _ctl("kill", "--kill-whom=main", "-s", "KILL", ac.afx).returncode == 0
    up_while_afx_down = False
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        afx_now = _show(ac.afx, "ActiveState", "NRestarts")
        if afx_now["ActiveState"] != "active":
            if _show(ac.ops("bot"), "ActiveState", "InvocationID") == {
                    "ActiveState": "active", "InvocationID": before["InvocationID"]}:
                up_while_afx_down = True
        elif afx_now["NRestarts"] != afx_restarts:
            break
        time.sleep(0.05)
    observed["crash_window_ops_active"] = up_while_afx_down
    assert _wait(lambda: _ops_replaced(ac, before), seconds=20)
    observed["crash"] = snapshot()
    assert observed["crash"]["NRestarts"] == "0"
    print("AC12 afx stop/restart/crash observed:", json.dumps(observed, sort_keys=True))
    assert up_while_afx_down


def test_ops_restart_delay_and_start_limit(ac):
    ac.setup().enable("bot")
    pid, _ = ac.tick()
    # 以降の起動は ready の後 0.5 秒で自分で落ちる。最初の 1 回は外から殺す
    ac.ops_mode(mode="readycrash", crash_after=0.5)
    before_count = len(ac.ops_starts())
    ops_pid = int(_show(ac.ops("bot"), "MainPID")["MainPID"])
    killed_at = time.time()
    os.kill(ops_pid, signal.SIGKILL)

    # enable 時の start (1 回目) + 殺した後 2 回の再起動で 60 秒窓の burst 3 に達し、次は拒否される
    assert _wait(lambda: _show(ac.ops("bot"), "ActiveState")["ActiveState"] == "failed", seconds=40)
    props = _show(ac.ops("bot"), "ActiveState", "SubState", "Result", "NRestarts")
    starts = ac.ops_starts()
    restarts = starts[before_count:]
    assert len(restarts) == 2, starts
    gaps = [restarts[0] - killed_at, restarts[1] - restarts[0]]
    print("AC12 restart gaps:", gaps, "props:", props)
    assert all(3 <= gap <= 10 for gap in gaps), gaps
    # 3 回目の再起動は start-limit で拒否され failed に留まる (Result はこの systemd では最後の exit-code)
    assert props["SubState"] == "failed" and props["NRestarts"] == "3"
    assert props["Result"] in {"exit-code", "start-limit-hit"}
    time.sleep(6)
    assert ac.ops_starts() == starts
    assert _show(ac.ops("bot"), "ActiveState")["ActiveState"] == "failed"
    assert _ticks_advance(ac, pid)
    assert _show(ac.afx, "ActiveState")["ActiveState"] == "active"


def test_unit_ready_gating_with_stand_in_ops_and_stub_socket(ac):
    """unit の ready 待ちの契約だけを見る (代役の ops と認証しない socket)。認証は実 ops の試験が担う。"""
    # 期限内に afx が UDS を出せば、ops は token を送って ready になる
    ac.ops_mode(mode="serve", deadline=15)
    ac.restart_afx_with(1.0)
    begun = time.monotonic()

    assert ac.setup().enable("bot").stage == "enabled"

    elapsed = time.monotonic() - begun
    print("AC12 ready elapsed:", elapsed)
    assert elapsed < 15
    assert _show(ac.ops("bot"), "ActiveState")["ActiveState"] == "active"
    assert _wait(lambda: ac.token_bytes() == 16)

    # UDS が期限まで出ない: ops は ready にならず failed、token は 1 byte も送られない
    assert ac.setup().disable("bot").stage == "disabled"
    (ac.root / "token_bytes").unlink()
    ac.ops_mode(mode="serve", deadline=3)
    ac.restart_afx_with(600)

    with pytest.raises(SystemdError) as raised:
        ac.setup().enable("bot")

    # 失敗後の巻き戻しで unit は stop 済みなので、偽 ops が自分で記録した終了値を見る
    props = _show(ac.ops("bot"), "ActiveState")
    print("AC12 not-ready:", raised.value.reason, props, (ac.root / "ops_exit").read_text())
    assert raised.value.reason == "start"
    assert read_stage(ac.root, "bot")["stage"] == "installed"
    assert props["ActiveState"] != "active"
    assert (ac.root / "ops_exit").read_text() == "3"
    assert ac.token_bytes() == 0


# --------------------------------------------------------------------------
# 実 afx-ops (web) と実 ApiServer による ready 試験

def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class RealWeb:
    """実 ``afx_ops`` の web を unit の ExecStart に置く。鍵 dir・設定・状態は一時領域に置く。"""

    def __init__(self, ac: Ac12Env, tmp_path: Path, *, limit: float) -> None:
        from agentic_fx.ops.contracts import Principal
        from agentic_fx.ops.keys import enroll

        self.ac = ac
        self.work = tmp_path / "web-work"
        self.work.mkdir()
        self.keys = tmp_path / "web-keys"
        initialize(self.keys, root=ac.root)
        enroll(self.keys, Principal.OPS_WEB)
        self.token = (self.keys / "ops-web.token").read_text().strip()
        xdg = tmp_path / "xdg"
        config_dir = xdg / "config" / "agentic-fx" / "ops" / "web" / "config"
        config_dir.mkdir(parents=True, mode=0o700)
        for directory in (xdg, xdg / "config", xdg / "state"):
            directory.mkdir(exist_ok=True, mode=0o700)
        self.state = xdg / "state" / "agentic-fx" / "ops" / "web"
        self.port = _free_port()
        (config_dir / "ops.toml").write_text(
            f'instance_root = "{ac.root}"\nkey_dir = "{self.keys}"\nport = {self.port}\n')
        (config_dir / "ops.toml").chmod(0o600)
        assert _AFX_OPS_PYTHON.exists(), "afx-ops/.venv が無い (uv sync --project afx-ops)"
        launcher = ac.fake_ops
        launcher.write_text(f"#!{_AFX_OPS_PYTHON}\n" + _WEB_LAUNCHER.read_text())
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
        Path(str(launcher) + ".json").write_text(json.dumps({
            "work": str(self.work), "xdg_config": str(xdg / "config"),
            "xdg_state": str(xdg / "state"), "limit": limit}))
        assert len(str(ac.root / "data" / "run" / "api.sock")) < 100

    def stderr(self) -> str:
        path = self.work / "ops_stderr"
        return path.read_text() if path.exists() else ""

    def trace(self) -> dict:
        return json.loads((self.state / "ops_trace.json").read_text())

    def exit_status(self) -> str | None:
        path = self.state / "ops_exit"
        return path.read_text() if path.exists() else None


@pytest.fixture
def real_web(ac, tmp_path):
    def make(limit: float) -> RealWeb:
        return RealWeb(ac, tmp_path, limit=limit)
    return make


def _get(port: int, path: str = "/") -> tuple[int, str]:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as client:
        client.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode())
        data = b""
        while chunk := client.recv(65536):
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split()[1]), body.decode("utf-8", "replace")


def test_real_web_becomes_ready_only_after_real_server_authentication(ac, real_web):
    web = real_web(30.0)
    delay = 3.0
    ac.restart_afx_with(delay, mode="api", key_dir=str(web.keys))
    begun = time.time()

    assert ac.setup().enable("web").stage == "enabled"

    trace = web.trace()
    ready_after = trace["wait_done"] - trace["started"]
    print("AC12 real ready after:", ready_after, "sleeps:", len(trace["sleeps"]),
          "since enable:", time.time() - begun)
    # 実装の既定どおりの 250 ms 間隔・30 秒上限
    assert trace["default_interval"] == 0.25 and trace["default_limit"] == 30.0
    assert set(trace["sleeps"]) <= {0.25}
    assert trace["outcome"] == "ok"
    # READY (= wait_ready の成功) は server が UDS を出した後で、期限 (30 秒) よりずっと早い。
    # 出る前から待っていたので、待ちの間隔が実際に使われている
    socket_made = (ac.root / "data" / "run" / "api.sock").stat().st_ctime
    assert trace["wait_done"] >= socket_made
    assert len(trace["sleeps"]) >= 1
    assert ready_after < 30
    assert _show(ac.ops("web"), "ActiveState")["ActiveState"] == "active"
    assert web.exit_status() is None
    # READY の後、実 web が実 ApiServer から status を取って 200 を返す (web の鍵も認証を通っている)
    status, body = _get(web.port)
    assert status == 200 and "paper" in body
    assert web.token not in body and web.token not in web.stderr()
    assert web.stderr() == ""


def test_real_web_fails_closed_when_server_socket_never_appears(ac, real_web):
    web = real_web(3.0)
    ac.restart_afx_with(None)
    assert not (ac.root / "data" / "run" / "api.sock").exists()
    begun = time.monotonic()

    with pytest.raises(SystemdError) as raised:
        ac.setup().enable("web")

    elapsed = time.monotonic() - begun
    trace = web.trace()
    print("AC12 real not-ready:", raised.value.reason, elapsed, trace["outcome"],
          web.exit_status(), repr(web.stderr()), "sleeps:", len(trace["sleeps"]))
    assert raised.value.reason == "start"
    assert read_stage(ac.root, "web")["stage"] == "installed"
    assert _show(ac.ops("web"), "ActiveState")["ActiveState"] != "active"
    # 期限切れの固定 reason と終了値。READY は送られていない
    assert trace["outcome"] == "readiness_timeout"
    assert trace["wait_done"] - trace["started"] >= 2.5
    assert set(trace["sleeps"]) <= {0.25} and len(trace["sleeps"]) >= 8
    assert web.exit_status() == "3"
    assert web.stderr().splitlines()[0] == "afx-ops: readiness_timeout"
    # 接続自体が無いので鍵はどこにも渡っていない
    assert web.token not in web.stderr()
    assert (_get_listener_closed(web.port))


def _get_listener_closed(port: int) -> bool:
    """失敗した web の port が開いたまま残っていない。"""
    with socket.socket() as probe:
        probe.settimeout(2)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def test_real_web_fails_closed_when_socket_holder_is_not_the_instance_lock_holder(ac, real_web):
    web = real_web(3.0)
    ac.restart_afx_with(None)
    rogue = subprocess.Popen([sys.executable, str(_AFX_PROCESS), str(ac.root), "rogue"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                             preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CORE, (0, 0)))
    try:
        assert _wait(lambda: (ac.root / "data" / "run" / "api.sock").exists())
        begun = time.monotonic()

        with pytest.raises(SystemdError) as raised:
            ac.setup().enable("web")

        elapsed = time.monotonic() - begun
        trace = web.trace()
        connections = ac.root / "rogue_connections"
        received = ac.root / "rogue_bytes"
        sent = sum(int(line) for line in received.read_text().split()) if received.exists() else None
        print("AC12 real auth-failure:", raised.value.reason, elapsed, trace["outcome"],
              web.exit_status(), repr(web.stderr()), "connections:",
              len(connections.read_text().split()) if connections.exists() else 0, "bytes:", sent)
    finally:
        os.kill(rogue.pid, signal.SIGKILL)
        rogue.wait(10)
        rogue.stderr.close()

    assert raised.value.reason == "start"
    assert _show(ac.ops("web"), "ActiveState")["ActiveState"] != "active"
    # 偽の server に実際に接続したうえで、待たずに固定 reason で止まった (期限切れではない)
    assert trace["outcome"] == "not_lock_holder"
    assert trace["sleeps"] == []
    assert web.exit_status() == "3"
    assert web.stderr().splitlines()[0] == "afx-ops: not_lock_holder"
    assert len(connections.read_text().split()) >= 1
    # 偽の server が受け取った鍵 bytes は 0。鍵は ops の出力にも出ていない
    assert sent == 0
    assert web.token not in web.stderr()
