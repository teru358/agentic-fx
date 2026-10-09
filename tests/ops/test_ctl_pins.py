"""`afx ctl` の境界: 接続先検査の細部、鍵の読込の委譲、rc、端末、job poll、event、表示の無害化。"""
from __future__ import annotations

import errno
import io
import os
import socket
import stat
import subprocess
import sys

import httpx
import pytest

from agentic_fx.ops import client, keys
from agentic_fx.ops.contracts import Principal

from ._ctl import FakeServer, ctl_api, run_ctl, run_pty  # noqa: F401

ESC = "\x1b[31m"


class _Tty(io.StringIO):
    """isatty が真を返す、読むと EOF の stream。"""

    def isatty(self) -> bool:
        return True


class _StubApi:
    """Runner に渡す偽の ApiClient。送った要求を記録し、用意した応答を順に返す。"""

    def __init__(self, root, replies, principal=Principal.OPERATOR):
        self.root = root
        self.principal = principal
        self.replies = list(replies)
        self.sent = []

    def request(self, method, path, *, body=None, params=None, idempotent=False):
        self.sent.append((method, path, body, idempotent))
        reply = self.replies.pop(0)
        return reply(method, path) if callable(reply) else reply


class _Os:
    """client だけに見せる os。指定した関数だけ差し替え、残りは本物へ渡す。"""

    def __init__(self, **overrides):
        self.__dict__.update(overrides)

    def __getattr__(self, name):
        return getattr(os, name)


def _ok(data):
    return client.Reply(200, data, None)


JOB = "a" * 32


# ---------------------------------------------------------------- 接続先の検査

def _fake_env(ops_env):
    keys.ensure_initialized(keys.key_dir(ops_env.root), root=ops_env.root)
    return ops_env.root / "data" / "run" / "api.sock"


def test_socket_dir_with_group_access_is_refused(ops_env, tmp_path):
    sock_path = _fake_env(ops_env)
    server = FakeServer(sock_path, tmp_path / "rec", hold_lock_dir=ops_env.root / "data")
    try:
        os.chmod(sock_path.parent, 0o750)
        rc, _, err = run_ctl(ops_env.root, ["whoami"])
        assert rc == 3
        assert "鍵を送りませんでした (socket_dir)" in err
        assert server.connections(expected=1, timeout=0.5) == []
    finally:
        server.stop()


def test_symlinked_socket_dir_is_refused(ops_env, tmp_path):
    _fake_env(ops_env)
    real_dir = tmp_path / "real-run"
    real_dir.mkdir(mode=0o700)
    server = FakeServer(real_dir / "api.sock", tmp_path / "rec",
                        hold_lock_dir=ops_env.root / "data")
    try:
        (ops_env.root / "data" / "run").symlink_to(real_dir)
        rc, _, err = run_ctl(ops_env.root, ["whoami"])
        assert rc == 3
        assert "鍵を送りませんでした (socket_dir)" in err
        assert server.connections(expected=1, timeout=0.5) == []
    finally:
        server.stop()


def test_more_than_one_lock_holder_is_refused(tmp_path):
    lock_path = tmp_path / "instance.lock"
    lock_path.touch()
    st = os.stat(lock_path)
    dev = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
    proc = tmp_path / "locks"
    proc.write_text(f"1: FLOCK  ADVISORY  WRITE {os.getpid()} {dev} 0 EOF\n"
                    f"2: FLOCK  ADVISORY  WRITE {os.getppid()} {dev} 0 EOF\n")
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        with pytest.raises(client.ServerAuthError) as caught:
            client.verify_peer(left, uid=os.getuid(), lock_path=lock_path, proc_locks=proc)
        assert caught.value.reason == "not_lock_holder"
        proc.write_text(f"1: FLOCK  ADVISORY  WRITE {os.getpid()} {dev} 0 EOF\n")
        assert client.verify_peer(left, uid=os.getuid(), lock_path=lock_path,
                                  proc_locks=proc) == os.getpid()
    finally:
        left.close()
        right.close()


def test_lock_holders_reads_hex_device_numbers(tmp_path, monkeypatch):
    lock_path = tmp_path / "instance.lock"
    lock_path.touch()
    real = os.stat(lock_path)
    fake_dev = os.makedev(0x1a, 0x2b)

    class _St:
        st_dev = fake_dev
        st_ino = real.st_ino

    monkeypatch.setattr(client, "os", _Os(stat=lambda _path: _St()))
    proc = tmp_path / "locks"
    proc.write_text(f"1: FLOCK  ADVISORY  WRITE 100 1a:2b:{real.st_ino} 0 EOF\n"
                    f"2: FLOCK  ADVISORY  WRITE 200 26:43:{real.st_ino} 0 EOF\n"
                    f"3: FLOCK  ADVISORY  WRITE 300 1a:2c:{real.st_ino} 0 EOF\n")
    assert client.lock_holders(lock_path, proc_locks=proc) == {100}


class _Lstat:
    def __init__(self, mode, uid):
        self.st_mode, self.st_uid = mode, uid


def _lstat_table(table):
    def fake(path):
        key = os.fspath(path)
        if key in table:
            return table[key]
        raise FileNotFoundError(key)
    return fake


@pytest.mark.parametrize("parent,sock,reason", [
    (_Lstat(stat.S_IFREG | 0o700, None), None, "socket_dir"),
    (_Lstat(stat.S_IFDIR | 0o700, "other"), _Lstat(stat.S_IFSOCK | 0o600, None), "socket_dir"),
    (_Lstat(stat.S_IFDIR | 0o700, None), _Lstat(stat.S_IFSOCK | 0o600, "other"), "socket_file"),
    (_Lstat(stat.S_IFDIR | 0o700, None), _Lstat(stat.S_IFREG | 0o600, None), "socket_file"),
])
def test_socket_path_checks_each_property(tmp_path, monkeypatch, parent, sock, reason):
    uid = os.getuid()
    table = {}
    for name, entry in ((str(tmp_path), parent), (str(tmp_path / "api.sock"), sock)):
        if entry is not None:
            table[name] = _Lstat(entry.st_mode, uid + 1 if entry.st_uid == "other" else uid)
    monkeypatch.setattr(client, "os", _Os(lstat=_lstat_table(table)))
    with pytest.raises(client.ServerAuthError) as caught:
        client.check_socket_path(tmp_path / "api.sock", uid=uid)
    assert caught.value.reason == reason


class _PeerSock:
    """SO_PEERCRED / SO_PEERPIDFD の答えを決めた偽 socket。"""

    def __init__(self, pid, *, pidfd_error=None, pidfd=None):
        self.pid, self.pidfd_error, self.pidfd = pid, pidfd_error, pidfd

    def getsockopt(self, level, option, size=None):
        if option == socket.SO_PEERCRED:
            return client._UCRED.pack(self.pid, os.getuid(), os.getgid())
        if self.pidfd_error is not None:
            raise OSError(self.pidfd_error, os.strerror(self.pidfd_error))
        return self.pidfd


def _held_by(tmp_path, pid):
    lock_path = tmp_path / "instance.lock"
    lock_path.touch()
    st = os.stat(lock_path)
    proc = tmp_path / "locks"
    proc.write_text(f"1: FLOCK  ADVISORY  WRITE {pid} "
                    f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino} 0 EOF\n")
    return lock_path, proc


def test_peer_gone_before_pidfd_is_refused(tmp_path):
    lock_path, proc = _held_by(tmp_path, os.getpid())
    with pytest.raises(client.ServerAuthError) as caught:
        client.verify_peer(_PeerSock(os.getpid(), pidfd_error=errno.ESRCH), uid=os.getuid(),
                           lock_path=lock_path, proc_locks=proc)
    assert caught.value.reason == "peer_gone"


def test_pidfd_naming_another_process_is_refused(tmp_path, monkeypatch):
    lock_path, proc = _held_by(tmp_path, os.getpid())
    pidfd = os.open(lock_path, os.O_RDONLY | os.O_CLOEXEC)
    monkeypatch.setattr(client, "_pidfd_pid", lambda _fd: os.getpid() + 1)
    with pytest.raises(client.ServerAuthError) as caught:
        client.verify_peer(_PeerSock(os.getpid(), pidfd=pidfd), uid=os.getuid(),
                           lock_path=lock_path, proc_locks=proc)
    assert caught.value.reason == "peer_gone"


def test_dead_lock_holder_is_refused(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(10)
    lock_path, proc = _held_by(tmp_path, child.pid)
    assert not os.path.isdir(f"/proc/{child.pid}")
    with pytest.raises(client.ServerAuthError) as caught:
        client.verify_peer(_PeerSock(child.pid, pidfd_error=errno.ENOPROTOOPT),
                           uid=os.getuid(), lock_path=lock_path, proc_locks=proc)
    assert caught.value.reason == "holder_gone"


def test_every_request_is_checked_again(ctl_api):
    env = ctl_api.env
    api = client.ApiClient(env.root, Principal.OPERATOR)
    assert api.request("GET", "/v1/whoami").status == 200
    rows = len(env.ops_rows())
    os.chmod(env.root / "data" / "run", 0o755)
    try:
        with pytest.raises(client.ClientError) as caught:
            api.request("GET", "/v1/whoami")
    finally:
        os.chmod(env.root / "data" / "run", 0o700)
    assert caught.value.rc == 3
    assert len(env.ops_rows()) == rows


def test_peer_is_verified_on_every_connection(ctl_api, monkeypatch):
    env = ctl_api.env
    api = client.ApiClient(env.root, Principal.OPERATOR)
    assert api.request("GET", "/v1/whoami").status == 200
    calls = []

    def refuse(sock, **_kwargs):
        calls.append(sock)
        raise client.ServerAuthError("not_lock_holder")

    monkeypatch.setattr(client, "verify_peer", refuse)
    with pytest.raises(client.ClientError) as caught:
        api.request("GET", "/v1/whoami")
    assert caught.value.rc == 3 and "not_lock_holder" in caught.value.message
    assert len(calls) == 1


# ---------------------------------------------------------------- 鍵の読込

def test_symlinked_key_file_is_not_read(ctl_api, tmp_path):
    env = ctl_api.env
    directory = keys.key_dir(env.root)
    name = keys.key_file_name(Principal.OPERATOR)
    moved = tmp_path / "moved.token"
    os.replace(directory / name, moved)
    (directory / name).symlink_to(moved)
    rc, out, err = run_ctl(env.root, ["whoami"])
    assert (rc, out) == (3, "")
    assert "鍵を読めません (key_file_invalid)" in err
    assert env.ops_rows() == []


# ---------------------------------------------------------------- rc

def _response(status, payload):
    return httpx.Response(status, json=payload)


def test_internal_error_is_rc1_with_the_incident():
    with pytest.raises(client.ClientError) as caught:
        client._reply(_response(500, {"ok": False, "error": {"code": "internal",
                                                             "incident": "0123abcd"}}))
    assert caught.value.rc == 1
    assert "(incident 0123abcd)" in caught.value.message


def test_forbidden_403_is_rc3():
    with pytest.raises(client.ClientError) as caught:
        client._reply(_response(403, {"ok": False, "error": {"code": "forbidden"}}))
    assert caught.value.rc == 3
    assert "権限がありません" in caught.value.message


def test_unparsable_reply_is_rc1():
    with pytest.raises(client.ClientError) as caught:
        client._reply(httpx.Response(200, text="not json"))
    assert caught.value.rc == 1


def test_broken_settings_is_rc3(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text("api: [unclosed\n")
    rc, out, err = run_ctl(tmp_path, ["status"])
    assert (rc, out) == (3, "")
    assert "設定を読めません" in err


@pytest.mark.parametrize("argv", [["approve", "abc"], ["log", "-1"], ["jobs", "get", "x"],
                                  ["events", "--limit", "0"], ["killswitch", "explode"]])
def test_malformed_arguments_are_usage_rc2(tmp_path, argv):
    rc, out, err = run_ctl(tmp_path, argv)
    assert (rc, out) == (2, "")
    assert "usage: afx ctl" in err


def test_business_refusal_code_is_rc1(tmp_path):
    api = _StubApi(tmp_path, [client.Reply(409, None, "idempotency_mismatch")])
    runner = client.Runner(api, stdin=io.StringIO(""), stdout=io.StringIO())
    with pytest.raises(client.ClientError) as caught:
        runner.run(client.parse(["policy", "add", "x"]))
    assert caught.value.rc == 1
    assert "別の内容" in caught.value.message


# ---------------------------------------------------------------- operator の事前検査

@pytest.mark.parametrize("argv", [
    ["approve", "{id}"], ["reject", "{id}"], ["approval", "retry", "{id}"],
    ["killswitch", "reset", "1"], ["killswitch", "reconcile"], ["data", "resume"]])
def test_operator_is_refused_every_approver_operation_without_connecting(ctl_api, argv):
    env = ctl_api.env
    approval_id = env.raw_approval()
    rc, out, err = run_ctl(env.root, [a.format(id=approval_id) for a in argv])
    assert (rc, out) == (3, "")
    assert "approver の権限が要ります" in err
    assert env.ops_rows() == []


# ---------------------------------------------------------------- 端末

def test_terminal_requires_dev_tty_even_when_stdin_is_a_tty(monkeypatch):
    real_open = os.open

    def no_tty(path, *args, **kwargs):
        if path == "/dev/tty":
            raise OSError(6, "No such device or address")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(client, "os", _Os(open=no_tty))
    read_end, write_end = os.pipe()
    try:
        stdin = _Tty()
        stdin.fileno = lambda: read_end
        assert client.Terminal.open(stdin) is None
    finally:
        os.close(read_end)
        os.close(write_end)


def test_terminal_requires_dev_tty_to_be_a_terminal(monkeypatch, tmp_path):
    plain = tmp_path / "not-a-tty"
    plain.write_text("")
    real_open = os.open
    opened = []

    def plain_file(path, flags, *args, **kwargs):
        if path == "/dev/tty":
            fd = real_open(plain, os.O_RDWR | os.O_CLOEXEC)
            opened.append(fd)
            return fd
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(client, "os", _Os(open=plain_file))
    assert client.Terminal.open(_Tty()) is None
    assert opened
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_confirmed_operation_without_terminal_does_not_connect(ops_env, tmp_path):
    sock_path = _fake_env(ops_env)
    server = FakeServer(sock_path, tmp_path / "rec", hold_lock_dir=ops_env.root / "data")
    try:
        rc, _, err = run_ctl(ops_env.root, ["--as", "approver", "reject", "1"])
        assert rc == 2
        assert "端末からの確認が要ります" in err
        assert server.connections(expected=1, timeout=0.5) == []
    finally:
        server.stop()


@pytest.mark.parametrize("argv", [["--as", "approver", "data", "resume"],
                                  ["--as", "approver", "approval", "retry", "{id}"],
                                  ["jobs", "cancel", "0" * 32]])
def test_declining_on_the_terminal_sends_nothing(ctl_api, argv):
    env = ctl_api.env
    approval_id = env.make_plugin_approval()
    result = run_pty(env.root, ["ctl"] + [a.format(id=approval_id) for a in argv],
                     [("[y/N]", "n")], timeout=20.0)
    assert result.rc == 1, result.output
    assert "取り消しました" in result.output
    assert [row for row in env.ops_rows() if row["endpoint"].startswith("POST")] == []


def test_killswitch_reset_when_not_latched_asks_nothing(ctl_api):
    env = ctl_api.env
    result = run_pty(env.root, ["ctl", "--as", "approver", "killswitch", "reset", "1"], [],
                     timeout=20.0)
    assert result.rc == 1, result.output
    assert "ラッチされていません" in result.output
    assert "reset と入力" not in result.output
    assert env.ops_rows() == []


# ---------------------------------------------------------------- 入口の対話判定

def test_no_operation_with_only_stdin_on_a_terminal_is_usage_rc2(tmp_path):
    out, err = io.StringIO(), io.StringIO()
    rc = client.main([], root=tmp_path, stdin=_Tty(), stdout=out, stderr=err)
    assert rc == 2
    assert "usage: afx ctl" in err.getvalue()


def test_no_operation_with_only_stdout_on_a_terminal_is_usage_rc2(tmp_path):
    out, err = _Tty(), io.StringIO()
    rc = client.main([], root=tmp_path, stdin=io.StringIO(""), stdout=out, stderr=err)
    assert rc == 2
    assert "usage: afx ctl" in err.getvalue()


# ---------------------------------------------------------------- job poll

class _Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _accepted_then_running():
    replies = [_ok({"job_id": JOB, "state": "queued"})]
    return replies + [_ok({"id": JOB, "kind": "ask", "state": "running"})] * 5000


@pytest.mark.parametrize("op,limit", [(["ask", "q"], 905.0), (["improve"], 45.0)])
def test_poll_waits_at_the_interval_until_the_limit(tmp_path, op, limit):
    clock = _Clock()
    api = _StubApi(tmp_path, _accepted_then_running())
    out = io.StringIO()
    runner = client.Runner(api, stdin=io.StringIO(""), stdout=out, sleep=clock.sleep,
                           monotonic=clock.monotonic)
    assert runner.run(client.parse(op)) == 1
    assert set(clock.sleeps) == {0.25}
    assert limit <= clock.now < limit + 0.5
    assert "待ち時間の上限に達しました" in out.getvalue()


def test_cancelled_job_ends_the_poll(tmp_path):
    clock = _Clock()
    api = _StubApi(tmp_path, [_ok({"job_id": JOB, "state": "queued"}),
                              _ok({"id": JOB, "state": "cancelled", "result_code": "cancelled"})])
    runner = client.Runner(api, stdin=io.StringIO(""), stdout=io.StringIO(),
                           sleep=clock.sleep, monotonic=clock.monotonic)
    with pytest.raises(client.ClientError) as caught:
        runner.run(client.parse(["ask", "q"]))
    assert caught.value.rc == 1
    assert clock.sleeps == []


def test_failed_job_is_rc1(tmp_path):
    api = _StubApi(tmp_path, [_ok({"job_id": JOB, "state": "queued"}),
                              _ok({"id": JOB, "state": "failed", "result_code": "mission_busy"})])
    runner = client.Runner(api, stdin=io.StringIO(""), stdout=io.StringIO())
    with pytest.raises(client.ClientError) as caught:
        runner.run(client.parse(["improve"]))
    assert caught.value.rc == 1
    assert "mission が実行中" in caught.value.message


def test_ctrl_c_while_polling_sends_no_cancel(tmp_path):
    def interrupt(_seconds):
        raise KeyboardInterrupt

    api = _StubApi(tmp_path, _accepted_then_running())
    out = io.StringIO()
    runner = client.Runner(api, stdin=io.StringIO(""), stdout=out, sleep=interrupt)
    with pytest.raises(KeyboardInterrupt):
        runner.run(client.parse(["ask", "q"]))
    assert [path for _, path, _, _ in api.sent if "cancel" in path] == []
    assert [method for method, *_ in api.sent].count("POST") == 1


@pytest.mark.parametrize("op", [["ask", "q"], ["improve"], ["improve", "add", "x"],
                                ["policy", "add", "x"]])
def test_creating_operations_carry_an_idempotency_key(tmp_path, op):
    api = _StubApi(tmp_path, [_ok({"job_id": JOB, "state": "queued", "id": 1}),
                              _ok({"id": JOB, "state": "done", "result": {}})])
    runner = client.Runner(api, stdin=io.StringIO(""), stdout=io.StringIO())
    runner.run(client.parse(op))
    assert api.sent[0][0] == "POST" and api.sent[0][3] is True


# ---------------------------------------------------------------- event

def test_events_follow_ends_with_rc130_on_ctrl_c(ctl_api):
    def interrupt(_seconds):
        raise KeyboardInterrupt

    rc, _, _ = run_ctl(ctl_api.env.root, ["events", "--follow"], sleep=interrupt)
    assert rc == 130


def test_events_high_watermark_going_back_keeps_the_cursor(tmp_path):
    page = {"events": [{"id": 5, "code": "x"}], "high_watermark": 5}
    back = {"events": [], "high_watermark": 3}
    api = _StubApi(tmp_path, [_ok(page), _ok(back)])
    out = io.StringIO()
    runner = client.Runner(api, stdin=io.StringIO(""), stdout=out, sleep=lambda _s: None)
    with pytest.raises(client.ClientError) as caught:
        runner.run(client.parse(["events", "--follow", "--limit", "1"]))
    assert caught.value.rc == 1
    assert client._load_cursor(client._events_cursor_path(tmp_path)) == 5


# ---------------------------------------------------------------- 表示の無害化

def test_status_fields_are_sanitized():
    text = client.format_status({"mode": f"{ESC}live", "kill_switch": {
        "latched": True, "generation": f"{ESC}1", "latched_at": f"{ESC}t"},
        "mission": {"loop": ESC, "status": ESC, "started_at": ESC},
        "data": {"state": ESC}})
    assert "\x1b" not in text


def test_event_fields_are_sanitized():
    text = client.format_event({"id": f"{ESC}1", "ts": ESC, "code": f"{ESC}c", "ref": ESC,
                                "fields": {f"{ESC}k": f"{ESC}v"}})
    assert "\x1b" not in text


def test_multiline_answer_is_sanitized_per_line():
    assert "\x1b" not in client._multiline(f"a{ESC}\nb{ESC}\n")


def test_unknown_error_code_is_sanitized():
    assert "\x1b" not in client.error_text(f"weird{ESC}")


def test_approval_detail_is_sanitized():
    text = client.format_approval_detail({"id": 1, "kind": ESC, "status": ESC,
                                          "facts_lines": [f"{ESC}fact"], "reason": ESC,
                                          "decided_by": ESC, "decided_at": ESC,
                                          "payload_sha256": ESC})
    assert "\x1b" not in text


def test_job_line_is_sanitized():
    text = client.Runner._job_line({"id": ESC, "kind": ESC, "state": ESC,
                                    "result_code": ESC,
                                    "result": {f"{ESC}k": f"{ESC}v", "answer": f"{ESC}a"}})
    assert "\x1b" not in text


def test_lines_are_sanitized():
    assert "\x1b" not in client._lines({"lines": [f"{ESC}x"]})


def test_backlog_idea_echo_is_sanitized(tmp_path):
    api = _StubApi(tmp_path, [_ok({"id": 3})])
    out = io.StringIO()
    client.Runner(api, stdin=io.StringIO(""), stdout=out).run(
        client.parse(["improve", "add", f"x{ESC}y"]))
    assert "\x1b" not in out.getvalue()


@pytest.mark.parametrize("kind,job", [
    ("approve", {"id": JOB, "state": "failed", "result_code": "plugin_busy"}),
    ("approve", {"id": JOB, "state": "failed", "result": {"status": "pending",
                                                          "outcome": "gate_failed"}}),
    ("reject", {"id": JOB, "state": "failed", "result_code": "already_decided"}),
])
def test_decision_job_that_did_not_finish_is_rc1(tmp_path, kind, job):
    api = _StubApi(tmp_path, [_ok({"job_id": JOB, "state": "queued"}), _ok(job)],
                   principal=Principal.APPROVER)
    out = io.StringIO()
    runner = client.Runner(api, stdin=io.StringIO(""), stdout=out)
    with pytest.raises(client.ClientError) as caught:
        runner._decide(kind, 7, {})
    assert caught.value.rc == 1
    assert "approved" not in out.getvalue() and "rejected" not in out.getvalue()


class _Yes:
    def yes(self, _prompt):
        return True


def test_retry_sends_the_digest_of_the_shown_payload(tmp_path):
    api = _StubApi(tmp_path, [_ok({"id": 7, "kind": "plugin", "status": "failed",
                                   "payload_sha256": "ab" * 32}),
                              _ok({"job_id": JOB, "state": "queued"}),
                              _ok({"id": JOB, "state": "done",
                                   "result": {"status": "approved", "outcome": "switched"}})],
                   principal=Principal.APPROVER)
    runner = client.Runner(api, stdin=io.StringIO(""), stdout=io.StringIO())
    assert runner._op_retry(7, terminal=_Yes()) == 0
    posts = [(path, body) for method, path, body, _ in api.sent if method == "POST"]
    assert posts == [("/v1/approvals/7/retry", {"payload_sha256": "ab" * 32})]


@pytest.mark.parametrize("argv,principal", [(["--as", "operator"], "operator"),
                                            (["--as=approver"], "approver"),
                                            ([], "operator")])
def test_as_selects_the_principal(ctl_api, argv, principal):
    rc, out, err = run_ctl(ctl_api.env.root, argv + ["whoami"])
    assert rc == 0, err
    assert f"principal: {principal}" in out


# ---------------------------------------------------------------- 引数と principal の指定

@pytest.mark.parametrize("argv", [["--as", "bogus", "whoami"], ["--as=", "whoami"],
                                  ["--asx", "whoami"], ["events", "--limit", "501"],
                                  ["log", "1" * 19]])
def test_unknown_principal_or_out_of_range_argument_is_usage_rc2(tmp_path, argv):
    rc, out, err = run_ctl(tmp_path, argv)
    assert (rc, out) == (2, "")
    assert "usage: afx ctl" in err


def test_socket_missing_from_an_existing_dir_is_reported_as_no_socket(ops_env):
    sock = _fake_env(ops_env)
    sock.parent.mkdir(mode=0o700, parents=True)
    rc, out, err = run_ctl(ops_env.root, ["status"])
    assert (rc, out) == (3, "")
    assert "socket がありません" in err


# ---------------------------------------------------------------- 端末で確認した後の送信

def test_data_resume_confirmed_on_the_terminal_is_sent(tmp_path):
    api = _StubApi(tmp_path, [_ok({"unprocessed_positions": 0})], principal=Principal.APPROVER)
    runner = client.Runner(api, stdin=io.StringIO(""), stdout=io.StringIO())
    assert runner._op_data_resume(True, terminal=_Yes()) == 0
    assert [(m, p, b) for m, p, b, _ in api.sent] == [
        ("POST", "/v1/data/resume", {"acknowledge": True})]


def test_job_cancel_confirmed_on_the_terminal_is_sent(tmp_path):
    api = _StubApi(tmp_path, [_ok({"id": JOB, "state": "cancelled"})])
    out = io.StringIO()
    runner = client.Runner(api, stdin=io.StringIO(""), stdout=out)
    assert runner._op_job_cancel(JOB, terminal=_Yes()) == 0
    assert [(m, p) for m, p, _, _ in api.sent] == [("POST", f"/v1/jobs/{JOB}/cancel")]
    assert "cancelled" in out.getvalue()


class _EofTerminal(client.Terminal):
    """入力の途中で端末が閉じた (EOF) 確認口。"""

    def __init__(self):
        super().__init__(-1)

    def ask(self, prompt):
        return None


def _latched_status(generation=3):
    return _ok({"kill_switch": {"latched": True, "generation": generation,
                                "latched_at": "t"}, "equity": 1000})


@pytest.mark.parametrize("call", [
    lambda runner: runner._op_approve(7, terminal=_EofTerminal()),
    lambda runner: runner._op_reject(7, None, terminal=_EofTerminal()),
    lambda runner: runner._op_killswitch_reset(3, terminal=_EofTerminal()),
], ids=["approve", "reject", "killswitch_reset"])
def test_eof_at_the_confirmation_sends_nothing(tmp_path, call):
    detail = _ok({"id": 7, "kind": "plugin", "status": "pending", "payload_sha256": "ab" * 32})
    api = _StubApi(tmp_path, [lambda method, path: (_latched_status() if path == "/v1/status"
                                                    else detail)],
                   principal=Principal.APPROVER)
    runner = client.Runner(api, stdin=io.StringIO(""), stdout=io.StringIO())
    with pytest.raises(client.ClientError) as caught:
        call(runner)
    assert caught.value.rc == 1
    assert "取り消しました" in caught.value.message
    assert [m for m, *_ in api.sent if m == "POST"] == []


# ---------------------------------------------------------------- 拒否された変更は成功と表示しない

class _Typed:
    def __init__(self, word):
        self.word = word

    def ask(self, _prompt):
        return self.word

    def yes(self, _prompt):
        return True


@pytest.mark.parametrize("replies,call,text", [
    ([_latched_status(), client.Reply(409, None, "generation_mismatch")],
     lambda runner: runner._op_killswitch_reset(3, terminal=_Typed("reset")),
     "ラッチが更新されたので"),
    ([_ok({"kill_switch": {"reconcile_required": True}}), client.Reply(409, None, "invalid_state")],
     lambda runner: runner._op_killswitch_reconcile(terminal=_Typed("y")),
     "現在の状態では"),
    ([client.Reply(409, None, "invalid_state")],
     lambda runner: runner._op_data_resume(False, terminal=_Typed("y")),
     "現在の状態では"),
    ([client.Reply(409, None, "job_running")],
     lambda runner: runner._op_job_cancel(JOB, terminal=_Typed("y")),
     "取り消せません"),
    ([_ok({"status": "closed", "reflected": False, "attempts": 2, "last_attempt_at": "t"}),
      client.Reply(409, None, "attempt_changed")],
     lambda runner: runner._op_reflection_retry(9, terminal=None),
     "試行記録が更新された"),
], ids=["killswitch_reset", "killswitch_reconcile", "data_resume", "job_cancel", "reflect_retry"])
def test_refused_change_is_rc1_and_not_reported_as_done(tmp_path, replies, call, text):
    api = _StubApi(tmp_path, replies, principal=Principal.APPROVER)
    out = io.StringIO()
    runner = client.Runner(api, stdin=io.StringIO(""), stdout=out)
    with pytest.raises(client.ClientError) as caught:
        call(runner)
    assert caught.value.rc == 1
    assert text in caught.value.message
    for done in ("解除しました", "確定しました", "記録しました", "戻しました"):
        assert done not in out.getvalue()


# ---------------------------------------------------------------- 表示と event cursor

def test_interrupt_notice_state_is_sanitized():
    assert "\x1b" not in client.interrupt_notice(JOB, f"{ESC}running")


def test_backlog_idea_echo_counts_the_removed_characters(tmp_path):
    api = _StubApi(tmp_path, [_ok({"id": 3})])
    out = io.StringIO()
    client.Runner(api, stdin=io.StringIO(""), stdout=out).run(
        client.parse(["improve", "add", f"x{ESC}y"]))
    assert "表示できない文字を 1 個含みます" in out.getvalue()


class _BrokenOut(io.StringIO):
    def write(self, text):
        raise BrokenPipeError(errno.EPIPE, "closed")


def test_events_cursor_is_not_saved_when_the_display_fails(tmp_path):
    page = {"events": [{"id": 1, "code": "a"}, {"id": 2, "code": "b"}], "high_watermark": 2}
    api = _StubApi(tmp_path, [_ok(page)])
    runner = client.Runner(api, stdin=io.StringIO(""), stdout=_BrokenOut())
    with pytest.raises(BrokenPipeError):
        runner.run(client.parse(["events"]))
    assert client._load_cursor(client._events_cursor_path(tmp_path)) == 0


# ---------------------------------------------------------------- 対話モードの終了

class _EofTty(io.StringIO):
    """端末に見え、最初の readline で EOF を返す。EOF の後にもう一度読むと失敗する。"""

    def __init__(self):
        super().__init__("")
        self.reads = 0

    def isatty(self):
        return True

    def readline(self, *args):
        self.reads += 1
        assert self.reads == 1, "EOF の後にも入力を読んだ"
        return ""


def test_interactive_mode_ends_at_eof(tmp_path):
    stdin = _EofTty()
    rc = client.main([], root=tmp_path, stdin=stdin, stdout=_Tty(), stderr=io.StringIO())
    assert rc == 0
    assert stdin.reads == 1


# ---------------------------------------------------------------- kill switch の応答の形

@pytest.mark.parametrize("kill", ["latched", ["latched"], 1])
@pytest.mark.parametrize("call,text", [
    (lambda runner: runner._op_killswitch_show(terminal=None), "ラッチされていません"),
    (lambda runner: runner._op_killswitch_reconcile(terminal=_Typed("y")),
     "確認が必要な状態はありません"),
], ids=["show", "reconcile"])
def test_kill_switch_that_is_not_an_object_is_shown_as_not_latched(tmp_path, kill, call, text):
    api = _StubApi(tmp_path, [_ok({"kill_switch": kill})], principal=Principal.APPROVER)
    out = io.StringIO()
    runner = client.Runner(api, stdin=io.StringIO(""), stdout=out)
    assert call(runner) == 0
    assert text in out.getvalue()
    assert [m for m, *_ in api.sent if m == "POST"] == []


# ---------------------------------------------------------------- 決定の前の表示と決定後の文言

def test_reject_shows_the_approval_before_asking(tmp_path):
    detail = {"id": 7, "kind": "plugin", "status": "pending", "title": "候補 seven",
              "payload_sha256": "ab" * 32}
    api = _StubApi(tmp_path, [_ok(detail), _ok({"job_id": JOB, "state": "queued"}),
                              _ok({"id": JOB, "state": "done",
                                   "result": {"status": "rejected", "outcome": "rejected"}})],
                   principal=Principal.APPROVER)
    out = io.StringIO()
    runner = client.Runner(api, stdin=io.StringIO(""), stdout=out)
    assert runner._op_reject(7, "理由", terminal=_Yes()) == 0
    assert client.format_approval_detail(detail) in out.getvalue()
    assert [(m, p) for m, p, _, _ in api.sent[:2]] == [
        ("GET", "/v1/approvals/7"), ("POST", "/v1/approvals/7/reject")]
    assert api.sent[1][2] == {"reason": "理由"}


def test_retry_that_did_not_finish_says_the_retry_did_not_complete(tmp_path):
    job = {"id": JOB, "state": "failed", "result_code": "invalid_state",
           "result": {"status": "failed", "outcome": "gate_failed"}}
    api = _StubApi(tmp_path, [_ok({"job_id": JOB, "state": "queued"}), _ok(job)],
                   principal=Principal.APPROVER)
    runner = client.Runner(api, stdin=io.StringIO(""), stdout=io.StringIO())
    with pytest.raises(client.ClientError) as caught:
        runner._decide("retry", 7, {})
    assert caught.value.rc == 1
    assert "再試行は完了しませんでした (status=failed outcome=gate_failed)" in caught.value.message
    assert "承認され" not in caught.value.message


@pytest.mark.parametrize("outcome,approved", [("deployed", True),
                                              ("deployed_after_rollback", True),
                                              ("invalidated", False)])
def test_finished_approve_says_approved_only_for_an_approving_outcome(tmp_path, outcome,
                                                                      approved):
    job = {"id": JOB, "state": "done", "result": {"status": "x", "outcome": outcome}}
    api = _StubApi(tmp_path, [_ok({"job_id": JOB, "state": "queued"}), _ok(job)],
                   principal=Principal.APPROVER)
    out = io.StringIO()
    runner = client.Runner(api, stdin=io.StringIO(""), stdout=out)
    assert runner._decide("approve", 7, {}) == 0
    text = out.getvalue()
    if approved:
        assert f"approval #7 approved (outcome={outcome})" in text
    else:
        assert "approved" not in text
        assert f"approval #7 の処理が完了しました (outcome={outcome})" in text


# ---------------------------------------------------------------- event cursor の保存

def test_events_with_explicit_after_does_not_move_the_saved_cursor(tmp_path):
    path = client._events_cursor_path(tmp_path)
    client._save_cursor(path, 500, 600)
    page = {"events": [{"id": i, "code": "x"} for i in range(101, 111)], "high_watermark": 600}
    api = _StubApi(tmp_path, [_ok(page)])
    out = io.StringIO()
    runner = client.Runner(api, stdin=io.StringIO(""), stdout=out)
    assert runner.run(client.parse(["events", "--after", "100", "--limit", "10"])) == 0
    assert "#110" in out.getvalue()
    assert client._load_cursor_state(path) == (500, 600)


def test_events_watermark_going_back_names_the_cursor_file_to_delete(tmp_path):
    path = client._events_cursor_path(tmp_path)
    client._save_cursor(path, 5, 9)
    api = _StubApi(tmp_path, [_ok({"events": [], "high_watermark": 3})])
    runner = client.Runner(api, stdin=io.StringIO(""), stdout=io.StringIO())
    with pytest.raises(client.ClientError) as caught:
        runner.run(client.parse(["events"]))
    assert "cursor file" in caught.value.message and str(path) in caught.value.message
    assert "削除" in caught.value.message


def test_events_follow_does_not_rewrite_an_unchanged_cursor(tmp_path):
    path = client._events_cursor_path(tmp_path)
    client._save_cursor(path, 5, 5)
    before = os.stat(path)
    empty = _ok({"events": [], "high_watermark": 5})
    api = _StubApi(tmp_path, [empty, empty, empty])
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise KeyboardInterrupt

    runner = client.Runner(api, stdin=io.StringIO(""), stdout=io.StringIO(), sleep=sleep)
    with pytest.raises(KeyboardInterrupt):
        runner.run(client.parse(["events", "--follow"]))
    after = os.stat(path)
    assert len(api.sent) == 3
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert client._load_cursor_state(path) == (5, 5)


# ---------------------------------------------------------------- transport

def test_verifying_transport_does_not_build_on_the_httpx_pool_internals():
    assert issubclass(client._VerifyingTransport, httpx.BaseTransport)
    assert not issubclass(client._VerifyingTransport, httpx.HTTPTransport)


# ---------------------------------------------------------------- 接続先を調べられない

def test_unreadable_pidfd_info_is_a_server_auth_failure(ctl_api, monkeypatch):
    def unreadable(_pidfd):
        raise PermissionError(errno.EACCES, "fdinfo")

    monkeypatch.setattr(client, "_pidfd_pid", unreadable)
    rc, out, err = run_ctl(ctl_api.env.root, ["whoami"])
    assert (rc, out) == (3, "")
    assert client.MESSAGES["server_auth"].format(reason="peer_check") in err
    assert "接続を拒否されました" not in err


class _NoCredSock:
    def getsockopt(self, level, option, size=None):
        raise OSError(errno.EOPNOTSUPP, "no peer credentials")


def test_peer_credentials_that_cannot_be_read_are_a_peer_check_failure(tmp_path):
    lock_path, proc = _held_by(tmp_path, os.getpid())
    with pytest.raises(client.ServerAuthError) as caught:
        client.verify_peer(_NoCredSock(), uid=os.getuid(), lock_path=lock_path,
                           proc_locks=proc)
    assert caught.value.reason == "peer_check"


# ---------------------------------------------------------------- 装置番号が stat と食い違う fs

_HOLD_LOCK = ("import fcntl, sys, time\n"
              "f = open(sys.argv[1], 'a')\n"
              "fcntl.flock(f, fcntl.LOCK_EX)\n"
              "print('ready', flush=True)\n"
              "time.sleep(60)\n")


def _no_core():
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _spawn(code, *args):
    child = subprocess.Popen([sys.executable, "-c", code, *args], stdout=subprocess.PIPE,
                             text=True, preexec_fn=_no_core)
    assert child.stdout.readline().strip() == "ready"
    return child


def _stop(child):
    child.kill()
    child.wait(5)
    child.stdout.close()


def test_lock_holder_on_a_device_number_that_differs_from_stat_is_found_by_its_fd(tmp_path):
    lock_path = tmp_path / "instance.lock"
    lock_path.touch()
    st = os.stat(lock_path)
    holder = _spawn(_HOLD_LOCK, str(lock_path))
    bystander = _spawn("import time; print('ready', flush=True); time.sleep(60)")
    try:
        wrong = f"{os.major(st.st_dev) + 7:x}:{os.minor(st.st_dev) + 9:x}"
        proc = tmp_path / "locks"
        proc.write_text(f"1: FLOCK  ADVISORY  WRITE {holder.pid} {wrong}:{st.st_ino} 0 EOF\n")
        assert client.lock_holders(lock_path, proc_locks=proc) == {holder.pid}
        # inode が違う行は、同じ pid が file を開いていても数えない。
        proc.write_text(f"1: FLOCK  ADVISORY  WRITE {holder.pid} {wrong}:{st.st_ino + 1} 0 EOF\n")
        assert client.lock_holders(lock_path, proc_locks=proc) == set()
        # lock file を開いていない pid は、装置番号が食い違うときには数えない。
        proc.write_text(f"1: FLOCK  ADVISORY  WRITE {bystander.pid} {wrong}:{st.st_ino} 0 EOF\n")
        assert client.lock_holders(lock_path, proc_locks=proc) == set()
    finally:
        _stop(holder)
        _stop(bystander)
