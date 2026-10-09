"""実 daemon process (``afx --daemon`` 相当) に同梱 CLI ``afx ctl`` を通す統合試験。

- whoami / status / approve を pty で通し、instance lock の保持者が daemon の実 pid である
- daemon 自身の子 process から正しい鍵で送った要求は 403 ``peer_rejected``
- SIGTERM で停止すると listener と socket が消え、``afx ctl`` は rc=3 の固定文面
- 停止中の rotate / revoke の後、旧鍵は実 HTTP で 401、新鍵の ``afx ctl`` は通る
- 稼働中の鍵 command は rc=1 で鍵 dir を変えない
- AC-41: secret env allowlist が listener を開く daemon で効く
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from agentic_fx.ops import client as ctl_client
from agentic_fx.ops import keys
from agentic_fx.ops.contracts import Principal

from ._ctl import run_pty
from ._daemon import Daemon, activity, base_env, init_root, set_settings, socket_path

_ENTRY = ("import sys; from agentic_fx.entry import main; "
          "raise SystemExit(main(sys.argv[1:]))")


def afx(root: Path, *argv: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """``afx <argv>`` を端末なしの子 process で実行する (cwd = root)。"""
    return subprocess.run([sys.executable, "-c", _ENTRY, *argv], cwd=str(root),
                          env=env or base_env(), capture_output=True, text=True,
                          timeout=120, stdin=subprocess.DEVNULL)


def _seed_plugin_approval(root: Path) -> int:
    """fake gate で実 submit_candidate を通した承認依頼を daemon の DB に作る。"""
    from agentic_fx.config import load_settings
    from agentic_fx.plugin import switch
    from agentic_fx.plugin.gate_pytest import GateResult
    from agentic_fx.store import db
    staging = root / "plugins" / "_staging" / "1"
    candidate = staging / "sma"
    candidate.mkdir(parents=True)
    (candidate / "plugin.py").write_text("def compute(df, params):\n    return {'v': 1.0}\n")
    (candidate / "config.yaml").write_text("kind: indicator\noutputs: [v]\n")
    (candidate / "test_plugin.py").write_text("def test_x():\n    pass\n")
    conn = db.connect(root / "data" / "agentic.db")
    original = switch.run_gate_pytest
    switch.run_gate_pytest = lambda plugin_dir, *, settings: GateResult(
        passed=True, returncode=0, stdout_tail="ok", duration_sec=0.1)
    try:
        return switch.submit_candidate(
            conn, name="sma", staging_dir=staging, candidate_origin="staging",
            mission_id=1, backlog_id=None,
            settings=load_settings(root / "config" / "settings.yaml"),
            now=datetime.now(timezone.utc))
    finally:
        switch.run_gate_pytest = original
        conn.close()


def _approval(root: Path, approval_id: int) -> tuple:
    from agentic_fx.store import db
    conn = db.connect(root / "data" / "agentic.db")
    try:
        row = conn.execute("SELECT status,decided_by FROM approval_requests WHERE id=?",
                           (approval_id,)).fetchone()
        return tuple(row)
    finally:
        conn.close()


def _whoami_status(root: Path, token: str) -> int:
    with httpx.Client(transport=httpx.HTTPTransport(uds=str(socket_path(root))),
                      base_url="http://afx", timeout=15,
                      headers={"Authorization": f"Bearer {token}",
                               "Content-Length": "0"}) as client:
        return client.get("/v1/whoami").status_code


@pytest.fixture
def daemons():
    started: list[Daemon] = []
    yield started
    for daemon in started:
        daemon.kill()  # 失敗した試験でも子を残さない


def test_ctl_against_a_real_daemon_whoami_status_approve_peer_and_sigterm(tmp_path, daemons):
    root = init_root(tmp_path / "root")
    approval_id = _seed_plugin_approval(root)
    probe = tmp_path / "peer.json"
    daemon = Daemon(root, peer_probe=probe)
    daemons.append(daemon)
    daemon.wait_listening()

    # instance lock の保持者は daemon の実 pid (client はそれを照合してから鍵を送る)
    assert ctl_client.lock_holders(root / "data" / "instance.lock") == {daemon.pid}

    who = run_pty(root, ["ctl", "whoami"], [])
    assert who.rc == 0, who.output
    assert "principal: operator" in who.output
    status = run_pty(root, ["ctl", "status"], [])
    assert status.rc == 0, status.output
    assert "mode: learning / autopilot: off / kill switch: ok" in status.output

    approve = run_pty(root, ["ctl", "--as", "approver", "approve", str(approval_id)],
                      [("もう一度入力して", str(approval_id))])
    assert approve.rc == 0, approve.output
    assert f"approval #{approval_id} approved (outcome=deployed" in approve.output
    assert _approval(root, approval_id) == ("approved", "api:approver")
    assert (root / "plugins" / "sma").is_symlink()

    # daemon 自身の子 process は正しい鍵でも 403 (本文を読む前に拒否)
    end = time.monotonic() + 30
    while not probe.exists():
        assert time.monotonic() < end, "peer probe did not report"
        time.sleep(0.05)
    assert json.loads(probe.read_text()) == {"status": 403, "code": "peer_rejected"}
    assert "peer_rejected" in activity(root)

    # SIGTERM: listener を閉じて socket を消し、正常終了する
    assert daemon.stop() == 0
    assert not socket_path(root).exists()
    assert "\tservice_stopped\tgraceful" in activity(root)
    after = afx(root, "ctl", "whoami")
    assert after.returncode == 3
    assert after.stderr.strip() == "afx ctl: " + ctl_client.MESSAGES["no_socket"].format(
        path=socket_path(root))


def test_key_rotate_and_revoke_while_stopped_then_old_keys_are_401_on_restart(
        tmp_path, daemons):
    root = init_root(tmp_path / "root")
    first = Daemon(root)
    daemons.append(first)
    first.wait_listening()
    directory = keys.key_dir(root)
    old_approver = keys.read_token(directory, Principal.APPROVER)
    old_operator = keys.read_token(directory, Principal.OPERATOR)
    before = {p.name: p.read_bytes() for p in directory.iterdir()}

    # 稼働中の key command は lock を取れず rc=1、鍵 dir は無変更
    running = afx(root, "keys", "rotate", "approver")
    assert running.returncode == 1, running.stderr
    assert "稼働中" in running.stderr
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before
    assert _whoami_status(root, old_approver) == 200

    # 漏洩手順: 停止 → rotate → 起動 → 旧鍵が 401 → 同梱 CLI が新鍵で通る
    assert first.stop() == 0
    rotated = afx(root, "keys", "rotate", "approver")
    assert rotated.returncode == 0, rotated.stderr
    second = Daemon(root)
    daemons.append(second)
    second.wait_listening()
    assert _whoami_status(root, old_approver) == 401
    assert _whoami_status(root, keys.read_token(directory, Principal.APPROVER)) == 200
    who = afx(root, "ctl", "--as", "approver", "whoami")
    assert who.returncode == 0, who.stderr
    assert "principal: approver" in who.stdout
    assert _whoami_status(root, old_operator) == 200  # 他 principal は影響を受けない

    # revoke: 当該 principal は常に 401、CLI は鍵を読めず送らない
    assert second.stop() == 0
    revoked = afx(root, "keys", "revoke", "operator")
    assert revoked.returncode == 0, revoked.stderr
    third = Daemon(root)
    daemons.append(third)
    third.wait_listening()
    assert _whoami_status(root, old_operator) == 401
    who = afx(root, "ctl", "whoami")
    assert who.returncode == 3
    assert "鍵を読めません" in who.stderr
    assert _whoami_status(root, keys.read_token(directory, Principal.APPROVER)) == 200
    assert third.stop() == 0


# ------------------------------------------------------------ AC-41

_ALLOWED_NAME = "CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS"


def _claude_trade_root(tmp_path: Path) -> Path:
    root = init_root(tmp_path / "root")
    creds = tmp_path / ".credentials.json"
    creds.write_text('{"token":"x"}')
    creds.chmod(0o600)
    set_settings(root, runner={"trade": {"backend": "claude", "model": "m"},
                               "claude": {"bin": sys.executable,
                                          "credentials_file": str(creds)}},
                 service={"secret_env_allowlist": [_ALLOWED_NAME]})
    return root


def test_ac41_exact_allowlisted_secret_like_env_reaches_an_api_daemon(tmp_path, daemons):
    root = _claude_trade_root(tmp_path)
    daemon = Daemon(root, extra_env={_ALLOWED_NAME: "not-a-secret"})
    daemons.append(daemon)
    daemon.wait_listening()
    # 名前は実 daemon の初期 env にある (seam ではなく /proc/<pid>/environ で確かめる)
    environ = Path(f"/proc/{daemon.pid}/environ").read_bytes()
    assert f"{_ALLOWED_NAME}=".encode() in environ
    token = keys.read_token(keys.key_dir(root), Principal.OPERATOR)
    with httpx.Client(transport=httpx.HTTPTransport(uds=str(socket_path(root))),
                      base_url="http://afx", timeout=15,
                      headers={"Authorization": f"Bearer {token}"}) as client:
        assert client.post("/v1/policy", json={"text": "allowlist pin"},
                           headers={"Idempotency-Key": "a1"}).status_code == 200
    assert daemon.stop() == 0
    directives = (root / "policy" / "directives.md").read_text(encoding="utf-8")
    assert directives.count("- allowlist pin") == 1
    log = (root / "logs" / "agentic.log").read_text(encoding="utf-8")
    assert "secret_env_allowlist" in log and _ALLOWED_NAME in log


@pytest.mark.parametrize("leaked", [_ALLOWED_NAME + "_V2", "X" + _ALLOWED_NAME,
                                    _ALLOWED_NAME.lower() + "_v2"])
def test_ac41_near_miss_secret_like_env_refuses_the_daemon_before_the_listener(
        tmp_path, daemons, leaked):
    root = _claude_trade_root(tmp_path)
    daemon = Daemon(root, extra_env={leaked: "x"})
    daemons.append(daemon)
    end = time.monotonic() + 60
    while daemon.proc.poll() is None:
        # 起動してしまった (listener まで進んだ) なら待たずに落とす
        assert "\tapi_started\t" not in activity(root), "daemon opened the listener"
        assert time.monotonic() < end, "daemon neither exited nor opened the listener"
        time.sleep(0.05)
    assert daemon.proc.returncode != 0
    assert "secret" in daemon.text()
    assert "\tapi_started\t" not in activity(root)
    assert not socket_path(root).exists()
    assert not keys.key_dir(root).exists()
