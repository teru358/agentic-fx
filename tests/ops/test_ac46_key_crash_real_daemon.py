"""AC-46: rotate / revoke の各点で process ごと落とし (os._exit)、再起動した実 daemon に
実 HTTP で確かめる。

- ``.ready`` の commit 前に落ちた残骸では daemon は listener を開かない (旧鍵も新鍵も通らない)
- 同じ key command の再実行で完了させた後、旧鍵は 401、新鍵は 200 (revoke は当該 principal が 401)
- ``.ready`` の準備段の一時 file だけが残った点 (preparing:written) では旧鍵集合のまま開く
"""
from __future__ import annotations

import resource
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from agentic_fx.ops import keys
from agentic_fx.ops.contracts import Principal

from ._daemon import Daemon, activity, init_root, socket_path
from .test_real_daemon_integration import afx

_CRASH = ("import os, sys\n"
          "from pathlib import Path\n"
          "from agentic_fx.ops import keys\n"
          "from agentic_fx.ops.contracts import Principal\n"
          "action, principal, wanted = sys.argv[2], Principal(sys.argv[3]), sys.argv[4]\n"
          "def hook(step):\n"
          "    if step == wanted:\n"
          "        os._exit(17)\n"
          "getattr(keys, action)(Path(sys.argv[1]), principal, hook=hook)\n")

ROTATE_STEPS = ["preparing:written", "preparing:renamed", "preparing:dir_synced",
                "key:written", "key:renamed", "key:dir_synced",
                "ready:written", "ready:renamed", "ready:dir_synced"]
REVOKE_STEPS = ["preparing:written", "preparing:renamed", "preparing:dir_synced",
                "tombstone:written", "tombstone:renamed", "tombstone:dir_synced",
                "ready:written", "ready:renamed", "ready:dir_synced"]
# .ready がまだ旧世代のまま (準備段の一時 file だけ書いた) の点
_OLD_SET_INTACT = {"preparing:written"}
# 新しい .ready が rename 済み (commit 済み) の点
_COMMITTED = {"ready:renamed", "ready:dir_synced"}


def _crash(directory: Path, action: str, principal: Principal, step: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _CRASH, str(directory), action, principal.value, step],
        capture_output=True, text=True, timeout=60,
        preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CORE, (0, 0)))
    assert result.returncode == 17, result.stderr


def _status(root: Path, token: str) -> int:
    with httpx.Client(transport=httpx.HTTPTransport(uds=str(socket_path(root))),
                      base_url="http://afx", timeout=15,
                      headers={"Authorization": f"Bearer {token}",
                               "Content-Length": "0"}) as client:
        return client.get("/v1/whoami").status_code


def _token_or_none(directory: Path, principal: Principal) -> str | None:
    try:
        return keys.read_token(directory, principal)
    except keys.KeySetError:
        return None


@pytest.fixture
def daemons():
    started: list[Daemon] = []
    yield started
    for daemon in started:
        daemon.kill()


def _start(root: Path, daemons: list) -> Daemon:
    daemon = Daemon(root)
    daemons.append(daemon)
    return daemon


def _expect_refused(root: Path, daemon: Daemon) -> None:
    # listener を開いてしまった場合も待たずに失敗させる
    assert daemon.wait_first("api_start_failed", "api_started") == "api_start_failed"
    assert "ready_not_committed" in activity(root)
    assert not socket_path(root).exists()
    assert daemon.stop() == 0


def _expect_only_new(root: Path, directory: Path, action: str, principal: Principal,
                     old: str, other_token: str) -> None:
    new = _token_or_none(directory, principal)
    assert _status(root, old) == 401
    if action == "rotate":
        assert new is not None and new != old
        assert _status(root, new) == 200
    else:
        assert new is None  # tombstone は鍵として読めない
    assert _status(root, other_token) == 200


@pytest.mark.parametrize("action,step",
                         [("rotate", s) for s in ROTATE_STEPS]
                         + [("revoke", s) for s in REVOKE_STEPS])
def test_ac46_crash_at_each_step_then_real_daemon_accepts_only_the_committed_set(
        tmp_path, daemons, action, step):
    root = init_root(tmp_path / "root")
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    principal = Principal.APPROVER if action == "revoke" else Principal.OPERATOR
    other = Principal.OPERATOR if principal is Principal.APPROVER else Principal.APPROVER
    old = keys.read_token(directory, principal)
    other_token = keys.read_token(directory, other)

    _crash(directory, action, principal, step)

    daemon = _start(root, daemons)
    if step in _COMMITTED:
        daemon.wait_listening()
        _expect_only_new(root, directory, action, principal, old, other_token)
        assert daemon.stop() == 0
        return
    if step in _OLD_SET_INTACT:
        daemon.wait_listening()
        assert _status(root, old) == 200  # 旧世代の集合がまだ commit 済み
        assert daemon.stop() == 0
    else:
        _expect_refused(root, daemon)
    # 同じ key command の再実行で完了させる (自動補完も旧鍵への巻き戻しもしない)
    finished = afx(root, "keys", action, principal.value)
    assert finished.returncode == 0, finished.stderr
    again = _start(root, daemons)
    again.wait_listening()
    _expect_only_new(root, directory, action, principal, old, other_token)
    assert again.stop() == 0
