"""実際に隔離した afx-ops の web (書込み有効・loopback・Basic) から、実 UDS を通して実 daemon へ
承認・却下・改善の開始・job の閲覧と取消・応答を失った後の回復ができる。

- web は afx-ops の venv の python で、隔離 (Landlock + seccomp) を通した実 process として回す
  (afx-ops/tests/isolation の canary の web_runtime)。鍵は隔離の前に `ops-web-write` の 1 本だけを
  読み、隔離の後は他の鍵を読めない。
- daemon は `afx --daemon` 相当の実 process (tests/ops/_daemon.py)。
- 通常の操作 (承認・却下・改善・job の閲覧と取消) は web が daemon の実 socket・lock へ直接つなぎ、
  daemon が peer を検証する。応答を失う状況だけは、実 daemon の手前の中継 (_uds_relay.py) が、
  改善の開始の応答を client へ返さずに閉じることで作る (daemon は受け付け済み)。
"""
from __future__ import annotations

import base64
import json
import os
import queue
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlencode

import pytest

from agentic_fx.ops import keys
from agentic_fx.ops.contracts import Principal

from ._daemon import Daemon, base_env, init_root

REPO = Path(__file__).resolve().parents[2]
AFX_OPS = REPO / "afx-ops"
AFX_PY = AFX_OPS / ".venv" / "bin" / "python"
CANARY = AFX_OPS / "tests" / "isolation" / "afx_isolation_canary.py"
RELAY = Path(__file__).with_name("_uds_relay.py")
PASSWORD = b"e2e-web-pass-0042"
ACTOR = "web:afx"


def _basic(user: str) -> str:
    return "Basic " + base64.b64encode(user.encode("ascii") + b":" + PASSWORD).decode("ascii")


BASIC = _basic("afx")


def _landlock_ok() -> bool:
    if sys.platform != "linux" or not AFX_PY.exists():
        return False
    done = subprocess.run(
        [str(AFX_PY), "-I", "-c",
         "from afx_ops.isolation import landlock; print(landlock.abi_version() >= landlock.REQUIRED_ABI)"],
        capture_output=True, text=True, cwd="/", timeout=60)
    return done.stdout.strip() == "True"


pytestmark = pytest.mark.skipif(not _landlock_ok(),
                                reason="afx-ops の venv と Landlock の実機が要る")


def _no_core() -> None:
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Child:
    """標準出力の行を queue に集める子 process。"""

    def __init__(self, argv: list[str], env: dict[str, str]) -> None:
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, env=env, cwd="/",
                                     preexec_fn=_no_core, start_new_session=True)
        self.lines: "queue.Queue[str]" = queue.Queue()
        self.seen: list[str] = []
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        for line in self.proc.stdout:
            self.lines.put(line.rstrip("\n"))

    def wait_line(self, prefix: str, timeout: float = 60.0) -> str:
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise AssertionError(f"no {prefix!r}: {self.seen} {self._stderr()}")
            try:
                line = self.lines.get(timeout=left)
            except queue.Empty:
                continue
            self.seen.append(line)
            if line.startswith(prefix):
                return line

    def _stderr(self) -> str:
        if self.proc.poll() is None:
            return ""
        return self.proc.stderr.read()

    def send(self, text: str) -> None:
        self.proc.stdin.write(text + "\n")
        self.proc.stdin.flush()

    def stop(self) -> str:
        """止めて stderr を返す。"""
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except OSError:
                pass
        self.proc.wait(30)
        err = self.proc.stderr.read()
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except OSError:
                pass
        return err


def _afx_ops_env(home: Path) -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", ""), "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config")}


def _make_web_root(base: Path) -> Path:
    root = base / "web-root"
    for feature in ("web",):
        for name, mode in (("config", 0o755), ("state", 0o700), ("secret", 0o700)):
            path = root / feature / name
            path.mkdir(parents=True)
            path.chmod(mode)
        (root / feature / "config" / "settings.txt").write_text("web\n")
    password = root / "web" / "secret" / "web.password"
    password.write_bytes(PASSWORD)
    password.chmod(0o600)
    return root


def _seed_approval(root: Path, name: str, value: float, mission: int) -> int:
    """fake gate で実 submit_candidate を通した plugin の承認依頼を daemon の DB に作る。"""
    from datetime import datetime, timezone

    from agentic_fx.config import load_settings
    from agentic_fx.plugin import switch
    from agentic_fx.plugin.gate_pytest import GateResult
    from agentic_fx.store import db
    staging = root / "plugins" / "_staging" / str(mission)
    candidate = staging / name
    candidate.mkdir(parents=True)
    (candidate / "plugin.py").write_text(f"def compute(df, params):\n    return {{'v': {value}}}\n")
    (candidate / "config.yaml").write_text("kind: indicator\noutputs: [v]\n")
    (candidate / "test_plugin.py").write_text("def test_x():\n    pass\n")
    conn = db.connect(root / "data" / "agentic.db")
    original = switch.run_gate_pytest
    switch.run_gate_pytest = lambda plugin_dir, *, settings: GateResult(
        passed=True, returncode=0, stdout_tail="ok", duration_sec=0.1)
    try:
        return switch.submit_candidate(
            conn, name=name, staging_dir=staging, candidate_origin="staging",
            mission_id=mission, backlog_id=None,
            settings=load_settings(root / "config" / "settings.yaml"),
            now=datetime.now(timezone.utc))
    finally:
        switch.run_gate_pytest = original
        conn.close()


def _rows(root: Path, sql: str, *args) -> list[tuple]:
    conn = sqlite3.connect(f"file:{root / 'data' / 'agentic.db'}?mode=ro", uri=True, timeout=30)
    try:
        return [tuple(row) for row in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _wait_for(predicate, what: str, limit: float = 60.0):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"timed out: {what}")


class Stack:
    """実 daemon + 隔離した書込みの web。`via_relay` なら web は中継を通して daemon につなぐ。

    起動した process は作った直後に `ExitStack` へ停止処理を登録するので、setup が途中で
    失敗しても残らない。
    """

    def __init__(self, tmp_path: Path, *, via_relay: bool = False) -> None:
        self.tmp = tmp_path
        self.exit = ExitStack()
        self.errors: list[str] = []
        self.children: list[Child] = []
        self.web: Child | None = None
        try:
            self._build(via_relay)
        except BaseException:
            self.exit.close()
            raise

    def _spawn(self, argv: list[str], env: dict[str, str]) -> Child:
        child = Child(argv, env)
        self.children.append(child)
        self.exit.callback(lambda: self.errors.append(child.stop()))
        return child

    def _build(self, via_relay: bool) -> None:
        self.root = init_root(self.tmp / "root")
        directory = keys.key_dir(self.root)
        keys.ensure_initialized(directory, root=self.root)
        for principal in (Principal.OPS_WEB, Principal.OPS_WEB_WRITE):
            keys.enroll(directory, principal)
        self.key_dir = directory
        self.approve_id = _seed_approval(self.root, "sma", 1.0, 1)
        self.reject_id = _seed_approval(self.root, "ema", 2.0, 2)
        # 改善の開始は実 daemon が実 mission を始めるので、transcript は一時領域へ向ける
        self.daemon = Daemon(self.root, extra_env={
            "AFX_TEST_TRANSCRIPTS_DIR": str(self.tmp / "transcripts")})

        def stop_daemon() -> None:
            try:
                self.daemon.stop()
            except Exception:  # noqa: BLE001 - 後始末
                self.daemon.kill()
        self.exit.callback(stop_daemon)
        self.daemon.wait_listening()
        self.drop_flag = self.tmp / "drop-improve-response"
        self.instance_root = self.root
        if via_relay:
            self.instance_root = self.tmp / "relay-root"
            self.instance_root.mkdir()
            relay = self._spawn(
                [sys.executable, str(RELAY), str(self.instance_root),
                 str(self.root / "data" / "run" / "api.sock"), str(self.drop_flag)], base_env())
            relay.wait_line("ready")
        self.home = self.tmp / "web-home"
        self.home.mkdir()
        self.web_root = _make_web_root(self.tmp)
        self.port = _free_port()

    def spec(self, op: str, *, port: int | None = None, user: str | None = None,
             **params) -> dict:
        spec = {"feature": "web", "root": str(self.web_root),
                "web_port": self.port if port is None else port,
                "web_auth": "basic", "web_write": True, "op": op, "params": params,
                "key_dir": str(self.key_dir), "instance_root": str(self.instance_root),
                "wait": {"interval": 0.1, "limit": 30}}
        if user is not None:
            spec["web_user"] = user
        return spec

    def start_web(self, *, port: int | None = None, user: str | None = None) -> Child:
        web = self._spawn([str(AFX_PY), "-I", str(CANARY),
                           json.dumps(self.spec("web_runtime", port=port, user=user,
                                                requests=10_000))],
                          _afx_ops_env(self.home))
        web.wait_line("SERVING")
        if self.web is None:
            self.web = web
        return web

    def ask(self, method: str, path: str, form: dict | None = None, *,
            authorization: str | None = BASIC, origin: bool = True,
            port: int | None = None) -> "Response":
        port = self.port if port is None else port
        host = f"127.0.0.1:{port}"
        body = urlencode(form).encode("ascii") if form is not None else b""
        lines = [f"{method} {path} HTTP/1.1", f"Host: {host}", "Connection: close"]
        if authorization is not None:
            lines.append(f"Authorization: {authorization}")
        if origin:
            lines.append(f"Origin: http://{host}")
        if form is not None:
            lines += ["Content-Type: application/x-www-form-urlencoded",
                      f"Content-Length: {len(body)}"]
        raw = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body
        with socket.create_connection(("127.0.0.1", port), timeout=30) as sock:
            sock.sendall(raw)
            data = b""
            while chunk := sock.recv(65536):
                data += chunk
        return Response(data)

    def close(self) -> list[str]:
        self.exit.close()
        return self.errors


class Response:
    def __init__(self, raw: bytes) -> None:
        head, _, self.body = raw.partition(b"\r\n\r\n")
        self.status = int(head.split(b" ", 2)[1])
        self.head = head
        self.text = self.body.decode("utf-8", "replace")

    @property
    def token(self) -> str:
        found = re.findall(r'name="token" value="([A-Za-z0-9_-]{43})"', self.text)
        assert len(found) == 1, self.text
        return found[0]


def _finish(built: Stack) -> None:
    try:
        secret = keys.read_token(built.key_dir, Principal.OPS_WEB_WRITE)
    finally:
        errors = built.close()
    output = "\n".join(errors + [line for child in built.children for line in child.seen])
    # 鍵・password・token の値は web の出力のどこにも出ない
    assert secret not in output and PASSWORD.decode() not in output
    assert "Traceback" not in output


@pytest.fixture
def stack(tmp_path):
    """web は daemon の実 socket・lock へ直接つなぐ。"""
    built = Stack(tmp_path)
    try:
        built.start_web()
    except BaseException:
        built.close()
        raise
    yield built
    _finish(built)


@pytest.fixture
def relay_stack(tmp_path):
    """web は応答を失わせる中継を通して daemon につなぐ。"""
    built = Stack(tmp_path, via_relay=True)
    try:
        built.start_web()
    except BaseException:
        built.close()
        raise
    yield built
    _finish(built)


def _digest_of(stack: Stack, approval_id: int) -> str:
    page = stack.ask("GET", f"/approvals/{approval_id}")
    assert page.status == 200, page.text
    found = re.findall(r'name="payload_sha256" value="([0-9a-f]{64})"', page.text)
    assert found, page.text
    return found[0]


def _approval(stack: Stack, approval_id: int) -> tuple:
    return _rows(stack.root, "SELECT status, decided_by FROM approval_requests WHERE id=?",
                 approval_id)[0]


def _ops_rows(stack: Stack, endpoint_like: str) -> list[tuple]:
    return _rows(stack.root,
                 "SELECT endpoint, authenticated_principal, asserted_actor, phase, job_id "
                 "FROM ops_requests WHERE endpoint LIKE ? ORDER BY id", endpoint_like)


def test_the_web_reads_only_the_write_key_and_the_isolated_process_is_a_real_one(stack):
    # 実 daemon の詳細が読め (読取りにも書込みの鍵を使う)、承認詳細に決定の form が出る
    page = stack.ask("GET", f"/approvals/{stack.approve_id}")
    assert page.status == 200
    assert f'action="/approvals/{stack.approve_id}/approve"' in page.text
    # 隔離された実 process の seccomp / Landlock が掛かっている
    pid = stack.web.proc.pid
    status = Path(f"/proc/{pid}/status").read_text()
    assert re.search(r"^Seccomp:\s*2$", status, re.M)
    assert re.search(r"^NoNewPrivs:\s*1$", status, re.M)
    # 認証の無い・誤った要求は書込みの口に届かない
    assert stack.ask("POST", f"/approvals/{stack.approve_id}/approve",
                     {"payload_sha256": "0" * 64}, authorization=None).status == 401
    assert _approval(stack, stack.approve_id)[0] == "pending"


def test_approve_through_the_isolated_web_reaches_the_real_daemon_with_the_web_actor(stack):
    digest = _digest_of(stack, stack.approve_id)
    first = stack.ask("POST", f"/approvals/{stack.approve_id}/approve",
                      {"payload_sha256": digest})
    assert first.status == 200
    # 1 回目は確認画面を返すだけで、daemon はまだ何も決めていない
    assert _approval(stack, stack.approve_id)[0] == "pending"
    done = stack.ask("POST", "/confirm", {"token": first.token})
    assert done.status == 200, done.text
    _wait_for(lambda: _approval(stack, stack.approve_id)[0] == "approved", "approval decided")
    assert _approval(stack, stack.approve_id) == ("approved", "api:ops-web-write")
    # 監査には principal と web が付けた actor が残り、受付 → 成功の 2 行が同じ job に結ばれる
    rows = _ops_rows(stack, "%approve%")
    assert [(r[1], r[2], r[3]) for r in rows] == [("ops-web-write", ACTOR, "accepted"),
                                                  ("ops-web-write", ACTOR, "succeeded")]
    assert len({r[4] for r in rows}) == 1 and rows[0][4]
    # 同じ token は 2 回目に通らない
    again = stack.ask("POST", "/confirm", {"token": first.token})
    assert again.status == 409


def test_reject_through_the_isolated_web_records_the_reason_and_the_actor(stack):
    digest = _digest_of(stack, stack.reject_id)
    first = stack.ask("POST", f"/approvals/{stack.reject_id}/reject",
                      {"payload_sha256": digest, "reason": "e2e reject"})
    assert first.status == 200
    done = stack.ask("POST", "/confirm", {"token": first.token})
    assert done.status == 200, done.text
    _wait_for(lambda: _approval(stack, stack.reject_id)[0] == "rejected", "approval rejected")
    assert _approval(stack, stack.reject_id) == ("rejected", "api:ops-web-write")
    assert {(r[1], r[2]) for r in _ops_rows(stack, "%reject%")} == {("ops-web-write", ACTOR)}


def test_a_stale_digest_is_not_rejected_and_an_origin_less_post_never_reaches_the_daemon(stack):
    first = stack.ask("POST", f"/approvals/{stack.reject_id}/reject",
                      {"payload_sha256": "f" * 64, "reason": "stale"})
    assert first.status == 200
    done = stack.ask("POST", "/confirm", {"token": first.token})
    assert _approval(stack, stack.reject_id)[0] == "pending", done.text
    digest = _digest_of(stack, stack.approve_id)
    assert stack.ask("POST", f"/approvals/{stack.approve_id}/approve",
                     {"payload_sha256": digest}, origin=False).status == 403
    assert _approval(stack, stack.approve_id)[0] == "pending"


def _job_id(response: Response) -> str:
    found = re.findall(r"\b([0-9a-f]{32})\b", response.text)
    assert found, response.text
    return found[0]


def _start_improve(stack: Stack) -> str:
    first = stack.ask("POST", "/improve/runs", {"duration_sec": "60", "max_runs": "1"})
    assert first.status == 200, first.text
    done = stack.ask("POST", "/confirm", {"token": first.token})
    assert done.status == 200, done.text
    job_id = _job_id(done)
    # 結果の画面から job の画面へ辿れる
    assert f'href="/jobs/{job_id}"' in done.text
    return job_id


def test_improve_start_job_view_and_cancel_through_the_isolated_web(stack):
    job_id = _start_improve(stack)
    rows = _ops_rows(stack, "%improve%")
    assert [(r[1], r[2]) for r in rows] == [("ops-web-write", ACTOR)]
    assert rows[0][4] == job_id
    # web は daemon の socket・lock へ直接つなぐ (中継は居ない)
    assert stack.instance_root == stack.root
    assert not any(str(RELAY) in child.proc.args for child in stack.children)
    # 閲覧: web の GET が同じ actor で daemon の job を読む
    view = stack.ask("GET", f"/jobs/{job_id}")
    assert view.status == 200, view.text
    assert job_id in view.text and f'action="/jobs/{job_id}/cancel"' in view.text
    # 取消の 1 回目は確認画面だけで、daemon は何も変えない
    jobs_before = _ops_rows(stack, "%cancel%")
    first = stack.ask("POST", f"/jobs/{job_id}/cancel", {})
    assert first.status == 200 and job_id in first.text
    assert _ops_rows(stack, "%cancel%") == jobs_before == []
    # 2 回目 (token) で取消が daemon に届き、監査に web の actor が残る
    done = stack.ask("POST", "/confirm", {"token": first.token})
    assert done.status == 200, done.text
    cancel_rows = _ops_rows(stack, "%cancel%")
    assert cancel_rows and {(r[1], r[2]) for r in cancel_rows} == {("ops-web-write", ACTOR)}
    # 停止の要求が job の画面に出る
    after = stack.ask("GET", f"/jobs/{job_id}")
    assert after.status == 200 and "あり" in after.text
    # 同じ token は 2 回目に通らない
    assert stack.ask("POST", "/confirm", {"token": first.token}).status == 409


def test_a_job_of_another_user_is_refused_by_the_daemon_and_shown_as_a_fixed_screen(stack):
    job_id = _start_improve(stack)
    other_port = _free_port()
    stack.start_web(port=other_port, user="mallory")
    other = _basic("mallory")
    view = stack.ask("GET", f"/jobs/{job_id}", authorization=other, port=other_port)
    assert view.status == 403, view.text
    assert "主体" in view.text and "forbidden" not in view.text and "web:" not in view.text
    first = stack.ask("POST", f"/jobs/{job_id}/cancel", {}, authorization=other,
                      port=other_port)
    assert first.status == 200
    done = stack.ask("POST", "/confirm", {"token": first.token}, authorization=other,
                     port=other_port)
    assert done.status == 409 and "forbidden" in done.text
    # 持ち主の job は取り消されていない
    assert "あり" not in stack.ask("GET", f"/jobs/{job_id}").text
    # 存在しない job は見つからない画面
    missing = stack.ask("GET", "/jobs/" + "0" * 32)
    assert missing.status == 404 and "見つかりません" in missing.text


def test_a_lost_improve_response_is_recovered_with_the_same_job_through_check(relay_stack):
    stack = relay_stack
    assert stack.instance_root != stack.root
    stack.drop_flag.write_text("drop")
    first = stack.ask("POST", "/improve/runs", {"duration_sec": "60", "max_runs": "1"})
    assert first.status == 200
    token = first.token
    lost = stack.ask("POST", "/confirm", {"token": token})
    # 応答を失った: 結果不明の画面で、daemon は受け付け済み
    assert not stack.drop_flag.exists()
    assert lost.status == 502 and "結果" in lost.text
    accepted = _ops_rows(stack, "%improve%")
    assert [(r[1], r[2], r[3]) for r in accepted] == [("ops-web-write", ACTOR, "accepted")]
    job_id = accepted[0][4]
    assert job_id and job_id not in lost.text
    recovered = stack.ask("POST", "/confirm/check", {"token": token})
    assert recovered.status == 200, recovered.text
    # 同じ key の replay: 同じ job id が戻り、受付も job も増えない
    assert _job_id(recovered) == job_id
    assert _ops_rows(stack, "%improve%") == accepted
