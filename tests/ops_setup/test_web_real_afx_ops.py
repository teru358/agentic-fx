"""実 afx-ops (afx-ops/.venv の実体) を相手にした web の統合試験。

本体の writer が書いた設定と password を、実 afx-ops の probe・interfaces・ops-web が
そのまま読めること、Basic 認証が loopback の実接続で働くこと、非対話の enable が実 probe を
通って状態機械へ進むことを観測する。実 home・本番 unit には触れない (XDG は一時領域)。
"""
from __future__ import annotations

import base64
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agentic_fx.ops.contracts import Principal
from agentic_fx.ops.keys import enroll, initialize, key_dir
from agentic_fx.ops_setup import interfaces as interface_listing
from agentic_fx.ops_setup.feature_config import web_access_reason
from agentic_fx.ops_setup.interactive import interactive_enable
from agentic_fx.ops_setup.stages import OpsSetup, read_stage
from agentic_fx.ops_setup.units import UnitPaths, resolve_ops_exec
from agentic_fx.ops_setup.web_setup import SecretPlan, WebOptions, apply_target
from web_support import FakeSystemd, ScriptIO

HERE = Path(__file__).resolve().parent
OPS_BIN = HERE.parents[1] / "afx-ops" / ".venv" / "bin"
AFX_OPS = OPS_BIN / "afx-ops"
OPS_PYTHON = OPS_BIN / "python"
AFX_PROCESS = HERE / "_ac12_afx_process.py"
WEB_LAUNCHER = HERE / "_ac12_web_launcher.py"

PASSWORD = "Wizard:written-pass-0042"
WRONG = "definitely-wrong-password-1"


# 本番の unit は NoNewPrivileges=yes で起動する。実 afx-ops の起動検査 (`unit_no_new_privs`) を
# 通すため、同じ属性を立ててから launcher を exec する。
_NO_NEW_PRIVS_EXEC = ("import ctypes, os, sys; ctypes.CDLL(None, use_errno=True).prctl(38, 1, 0, 0, 0); "
                      "os.execv(sys.argv[1], sys.argv[1:])")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class World:
    """一時領域に XDG・instance・鍵を置いた実行環境。"""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.root = tmp_path / "i"
        (self.root / "data").mkdir(parents=True)
        self.home = tmp_path / "h"
        self.home.mkdir()
        self.xdg_config = tmp_path / "xc"
        self.xdg_state = tmp_path / "xs"
        for directory in (self.xdg_config, self.xdg_state):
            directory.mkdir(mode=0o700)
        self.web = self.xdg_config / "agentic-fx" / "ops" / "web"
        self.env = {"PATH": os.defpath, "HOME": str(self.home),
                    "XDG_CONFIG_HOME": str(self.xdg_config), "XDG_STATE_HOME": str(self.xdg_state)}
        assert AFX_OPS.exists(), "afx-ops/.venv が無い (uv sync --project afx-ops)"
        unit_paths = UnitPaths(unit_dir=tmp_path / "units", afx_unit="afx-test.service",
                               ops_exec=str(AFX_OPS))
        self.paths = resolve_ops_exec(unit_paths)
        self.ops_exec = str(self.paths.ops_exec)

    def ops(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run([*shlex.split(self.ops_exec), *args], env=self.env,
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=60)

    def probe(self) -> tuple[int, str]:
        done = self.ops("probe", "web")
        assert done.stdout == b""
        return done.returncode, done.stderr.decode()

    def password_path(self) -> Path:
        return self.web / "secret" / "web.password"


@pytest.fixture
def world(tmp_path, monkeypatch) -> World:
    built = World(tmp_path)
    monkeypatch.setenv("HOME", str(built.home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(built.xdg_config))
    monkeypatch.setenv("XDG_STATE_HOME", str(built.xdg_state))
    return built


def _write_basic(world: World, port: int) -> None:
    apply_target(world.root, {"port": port, "auth": "basic"}, SecretPlan("install", PASSWORD))


# --- (a) probe --------------------------------------------------------------------------------

def test_probe_accepts_what_the_wizard_wrote_for_basic_and_agrees_with_the_body_reader(world):
    _write_basic(world, _free_port())
    assert world.probe() == (0, "")
    assert web_access_reason(world.root) is None
    assert world.password_path().read_bytes() == PASSWORD.encode()
    assert oct(world.password_path().stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize(("mutate", "reason"), [
    (lambda path: path.write_bytes(b"short"), "secret_invalid"),
    (lambda path: path.unlink(), "secret_missing"),
])
def test_probe_and_body_reader_give_the_same_reason_for_a_broken_password(world, mutate, reason):
    _write_basic(world, _free_port())
    mutate(world.password_path())
    if reason == "secret_invalid":
        world.password_path().chmod(0o600)
    assert world.probe() == (71, f"afx-ops: {reason}\n")
    assert web_access_reason(world.root) == reason


def test_probe_rejects_a_password_left_behind_when_auth_is_none(world):
    _write_basic(world, _free_port())
    apply_target(world.root, {"port": _free_port()}, SecretPlan("keep"))
    assert world.probe() == (71, "afx-ops: secret_unexpected\n")
    assert web_access_reason(world.root) == "secret_unexpected"


# --- (b) interfaces ---------------------------------------------------------------------------

def test_interfaces_contract_is_accepted_by_the_body_parser(world):
    done = world.ops("interfaces")
    assert done.returncode == 0 and done.stderr == b""
    assert done.stdout.endswith(b"\n") and done.stdout.count(b"\n") == 1
    document = json.loads(done.stdout)
    assert isinstance(document, dict) and "version" in document
    parsed = interface_listing.parse(done.stdout)
    assert tuple(parsed) == tuple(interface_listing.list_interfaces(world.ops_exec))
    for candidate in parsed:
        assert candidate.scope in ("global", "link")


# --- (c) ops-web を実 process で起動して Basic 認証を通す ----------------------------------------

def _request(port: int, token: str | None) -> tuple[int, dict[str, str], bytes]:
    auth = "" if token is None else f"Authorization: Basic {token}\r\n"
    raw = (f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n{auth}Connection: close\r\n\r\n").encode()
    with socket.create_connection(("127.0.0.1", port), timeout=10) as client:
        client.sendall(raw)
        data = b""
        while chunk := client.recv(65536):
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = {name.strip().lower(): value.strip()
               for name, _, value in (line.partition(":") for line in lines[1:])}
    return int(lines[0].split(" ")[1]), headers, body


def _token(password: str) -> str:
    return base64.b64encode(f"afx:{password}".encode()).decode()


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=20)


def _wait_for(predicate, *, seconds: float = 60.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_real_ops_web_serves_basic_auth_over_loopback_with_ready_and_the_sixth_failure_is_429(
        world, tmp_path):
    port = _free_port()
    keys = key_dir(world.root)
    initialize(keys, root=world.root)
    enroll(keys, Principal.OPS_WEB)
    _write_basic(world, port)
    assert world.probe() == (0, "")

    (world.root / "afx_mode.json").write_text(json.dumps(
        {"mode": "api", "delay": 0.0, "key_dir": str(keys)}))
    notify_path = tmp_path / "n.sock"
    work = tmp_path / "w"
    work.mkdir()
    launcher = tmp_path / "launch.py"
    launcher.write_text(WEB_LAUNCHER.read_text())
    Path(str(launcher) + ".json").write_text(json.dumps({
        "work": str(work), "xdg_config": str(world.xdg_config), "xdg_state": str(world.xdg_state),
        "limit": 30.0}))
    assert OPS_PYTHON.exists()

    notify = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    notify.bind(str(notify_path))
    notify.settimeout(0.2)
    afx = web = None
    try:
        afx = subprocess.Popen([sys.executable, str(AFX_PROCESS), str(world.root)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, cwd=tmp_path)
        assert _wait_for(lambda: (world.root / "data" / "run" / "api.sock").exists())
        web = subprocess.Popen([str(OPS_PYTHON), "-c", _NO_NEW_PRIVS_EXEC, str(OPS_PYTHON),
                                str(launcher), "web"],
                               env={**world.env, "NOTIFY_SOCKET": str(notify_path)},
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, cwd=tmp_path)

        ready = False
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not ready:
            try:
                ready = b"READY=1" in notify.recv(4096)
            except socket.timeout:
                assert web.poll() is None, "ops-web が READY の前に終了した"
        assert ready, "READY=1 が届かない"

        good = _token(PASSWORD)
        wrong = _token(WRONG)
        status, _headers, body = _request(port, good)
        assert status == 200 and PASSWORD.encode() not in body
        status, headers, _ = _request(port, None)
        assert status == 401 and headers["www-authenticate"] == 'Basic realm="afx"'
        assert [_request(port, wrong)[0] for _ in range(5)] == [401] * 5
        status, headers, _ = _request(port, wrong)
        assert status == 429 and 1 <= int(headers["retry-after"]) <= 60
        assert _request(port, good)[0] == 429
    finally:
        notify.close()
        for process in (web, afx):
            if process is not None:
                _stop(process)
    stderr_text = (work / "ops_stderr").read_text() if (work / "ops_stderr").exists() else ""
    assert PASSWORD not in stderr_text and _token(PASSWORD) not in stderr_text
    assert WRONG not in stderr_text and _token(WRONG) not in stderr_text
    # 数えるのは誤った資格情報の 5 本と、制限中の 2 本だけ。資格情報なしの要求は数えない
    assert stderr_text.splitlines() == (["afx-ops: web_auth_failed"] * 5
                                        + ["afx-ops: web_auth_limited"] * 2)


# --- (d) afx ops web enable の非対話を実 probe で通す -----------------------------------------------

def test_non_interactive_enable_runs_the_real_probe_before_leaving_staged(world, tmp_path):
    initialize(key_dir(world.root), root=world.root)
    port = _free_port()
    config_file = tmp_path / "target.toml"
    config_file.write_text(f'port = {port}\nauth = "basic"\n')
    config_file.chmod(0o600)
    password_file = tmp_path / "pw"
    password_file.write_text(PASSWORD)
    password_file.chmod(0o600)

    probes: list[tuple[int, str]] = []
    at_stopped: list[tuple[str, int]] = []

    def real_probe(_ops_exec: str, feature: str) -> tuple[int, str]:
        assert feature == "web"
        result = world.probe()
        probes.append(result)
        return (0, "") if result[0] == 0 else (result[0], result[1].strip().removeprefix("afx-ops: "))

    def hook(name: str) -> None:
        if name == "before_stage_stopped":
            saved = read_stage(world.root, "web")
            at_stopped.append((saved["stage"], len(probes)))

    systemd_paths = replace(world.paths, unit_dir=tmp_path / "units", afx_unit="afx-test.service")
    systemd = FakeSystemd(systemd_paths)
    systemd.active.add(systemd_paths.afx_unit)

    def factory(root: Path) -> OpsSetup:
        return OpsSetup(root, systemd=systemd, paths=systemd_paths, ready=lambda _f: True,
                        hook=hook, probe=real_probe)

    io = ScriptIO(tty=False)
    options = WebOptions(non_interactive=True, config_file=str(config_file),
                         password_file=str(password_file), secret_action="install")
    result = interactive_enable(
        world.root, "web", io=io, probe_runner=lambda feature: real_probe(world.ops_exec, feature),
        setup_factory=factory, options=options,
        candidates=interface_listing.provider(lambda: world.ops_exec))
    assert (result.rc, result.stage, result.reason_code) == (0, "enabled", "ok")
    # 入力の前検査と OpsSetup の staged で、実 afx-ops の probe が 2 回とも 0 を返す。
    # staged の記録が残っている間 (stopped へ進む直前) に、後者は済んでいる
    assert probes == [(0, ""), (0, "")]
    assert at_stopped == [("staged", 2)]
    assert world.password_path().read_bytes() == PASSWORD.encode()
    assert (world.web / "config" / "ops.toml").read_text().count('auth = "basic"') == 1
    assert PASSWORD not in io.text()
    assert web_access_reason(world.root) is None
