"""API 鍵は隔離前に manifest と digest を照合して読み、READY=1 は server 認証の後に送る。"""
from __future__ import annotations

import errno
import inspect
import json
import os
import socket
from pathlib import Path

import pytest

from _afx_isolation_support import (
    FEATURES,
    PRINCIPAL,
    SECRET_TEXT,
    Interactive,
    layout_of,
    make_root,
    needs_landlock,
    needs_x86_64,
    run_canary,
)
from _keydir import digest, token_for, write_ready
from _ops_harness import daemon, start, stop, token_in
from afx_ops import api_client, keys, server_auth
from afx_ops.api_client import ApiError, BotClient, NotifyClient, WebClient
from afx_ops.isolation import startup

@pytest.fixture
def root(tmp_path):
    return make_root(tmp_path)


def _spec(root, feature, op="noop", **extra):
    return {"feature": feature, "root": str(root), "op": op, "params": {}, **extra}


class _FakeManager:
    """systemd の通知受け口の代わり (実 AF_UNIX DGRAM)。"""

    def __init__(self, tmp_path) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.env_value = str(tmp_path / "notify.sock")
        self.sock.bind(self.env_value)

    def pending(self) -> list[bytes]:
        got = []
        self.sock.setblocking(False)
        while True:
            try:
                got.append(self.sock.recv(4096))
            except BlockingIOError:
                return got

    def wait(self, timeout: float = 10) -> bytes:
        self.sock.setblocking(True)
        self.sock.settimeout(timeout)
        return self.sock.recv(4096)

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def manager(tmp_path):
    fake = _FakeManager(tmp_path)
    yield fake
    fake.close()


# --- 鍵の受け渡し ----------------------------------------------------------------

def test_principal_is_fixed_per_feature():
    assert dict(startup.PRINCIPALS) == {"bot": "ops-bot", "web": "ops-web",
                                        "notify": "ops-notify"}
    with pytest.raises(TypeError):
        startup.PRINCIPALS["bot"] = "ops-web"  # type: ignore[index]


def test_clients_expose_a_production_token_factory_and_a_test_key_file_seam():
    for cls in (BotClient, WebClient, NotifyClient):
        assert list(inspect.signature(cls).parameters) == ["endpoint", "token"]
        assert list(inspect.signature(cls.from_token).parameters) == ["endpoint", "token"]
        seam = inspect.signature(cls.from_key_files).parameters["_for_tests"]
        assert seam.kind is inspect.Parameter.KEYWORD_ONLY and seam.default is True


def test_every_key_code_maps_to_a_registered_reason():
    mapped = {code: startup.key_reason(code) for code in keys.CODES}
    assert mapped == {
        "not_initialized": "key_not_initialized", "key_dir_invalid": "key_dir_invalid",
        "ready_invalid": "key_ready_invalid", "not_enrolled": "key_not_enrolled",
        "ready_not_committed": "key_ready_not_committed", "revoked": "key_revoked",
        "key_missing": "key_missing", "key_file_invalid": "key_file_invalid",
        "digest_mismatch": "key_digest_mismatch", "key_path_mismatch": "key_path_mismatch"}
    assert set(mapped.values()) <= set(startup.REASONS)


def test_endpoint_is_derived_from_the_instance_root():
    endpoint = startup.endpoint_for(Path("/srv/afx"))
    assert endpoint.socket_path == Path("/srv/afx/data/run/api.sock")
    assert endpoint.lock_path == Path("/srv/afx/data/instance.lock")


@needs_x86_64
@needs_landlock
@pytest.mark.parametrize("feature", FEATURES)
def test_api_token_is_read_after_the_digest_check_and_handed_over_as_bytes(feature, root,
                                                                           child_env):
    got = run_canary(_spec(root, feature, "api_token"), child_env)
    assert got.rc == 0, got.stderr[-2000:]
    assert got.result == ["bytes", digest(token_for(PRINCIPAL[feature]))]


@needs_x86_64
@needs_landlock
@pytest.mark.parametrize("feature", FEATURES)
def test_body_sees_the_config_endpoint_and_state_dir(feature, root, child_env):
    got = run_canary(_spec(root, feature, "config_view"), child_env)
    assert got.rc == 0, got.stderr[-2000:]
    instance = root / "root"
    assert got.result == {
        "instance_root": str(instance), "key_dir": str(root / "keys"),
        "socket_path": str(instance / "data" / "run" / "api.sock"),
        "lock_path": str(instance / "data" / "instance.lock"),
        "state_dir": str(root / feature / "state"),
        "config_dir_fd": None,
    }


@needs_x86_64
@needs_landlock
@pytest.mark.parametrize("feature", ["bot", "notify"])
def test_repr_and_error_text_never_contain_the_token_or_secret(feature, root, child_env):
    got = run_canary(_spec(root, feature, "repr_isolated"), child_env)
    assert got.rc == 0, got.stderr[-2000:]
    text = got.stdout + got.stderr
    assert token_for(PRINCIPAL[feature]) not in text
    assert SECRET_TEXT[feature] not in text
    # body は token と secret を持っている (repr から外れているだけ)
    assert got.result[2:] == [True, True]


@needs_x86_64
@needs_landlock
@pytest.mark.parametrize("feature", FEATURES)
def test_key_dir_is_unreachable_after_isolation(feature, root, child_env):
    keys_dir = root / "keys"
    own = keys_dir / keys.key_file_name(PRINCIPAL[feature])
    for op, params in (("open", {"path": str(keys_dir)}), ("listdir", {"path": str(keys_dir)}),
                       ("open", {"path": str(own)}),
                       ("open", {"path": str(keys_dir / keys.READY_NAME)})):
        got = run_canary({**_spec(root, feature, op), "params": params}, child_env)
        assert got.rc == 0, got.stderr[-2000:]
        assert got.result == [-1, errno.EACCES], (op, params)


@needs_x86_64
@needs_landlock
@pytest.mark.parametrize(("where", "suffix"), [
    ("config", "keys"),
    ("state", "."),
    ("secret", "keys"),
])
def test_key_dir_inside_a_feature_directory_stops_before_reading_a_key(where, suffix, root,
                                                                        child_env):
    key_dir = root / "bot" / where / suffix
    got = run_canary({**_spec(root, "bot"), "key_dir": str(key_dir),
                      "trace": ["read_api_token"]}, child_env)

    assert got.rc == startup.RC_ISOLATION_FAILED
    assert got.stderr.strip().splitlines()[-1] == "afx-ops: config_invalid"
    assert not got.isolated
    assert [line for line in got.stdout.splitlines() if line.startswith("CALL ")] == []


def _entry(directory: Path, principal: str, entry: dict | None) -> None:
    ready = json.loads((directory / ".ready").read_text())
    if entry is None:
        del ready["principals"][principal]
    else:
        ready["principals"][principal] = entry
    write_ready(directory, ready["principals"], version=ready["version"])


def _break(state: str, directory: Path, principal: str) -> None:
    key = directory / keys.key_file_name(principal)
    if state == "not_initialized":
        (directory / ".ready").unlink()
    elif state == "key_dir_invalid":
        os.chmod(directory, 0o755)
    elif state == "ready_invalid":
        (directory / ".ready").write_text("{")
    elif state == "not_enrolled":
        _entry(directory, principal, None)
    elif state == "ready_not_committed":
        _entry(directory, principal, {"generation": 2, "state": "preparing", "op": "rotate"})
    elif state == "revoked":
        _entry(directory, principal, {"generation": 2, "state": "revoked", "sha256": None})
    elif state == "key_missing":
        key.unlink()
    elif state == "key_file_invalid":
        os.chmod(key, 0o644)
    elif state == "digest_mismatch":
        _entry(directory, principal, {"generation": 1, "state": "active",
                                      "sha256": digest(token_for("operator"))})
    else:
        raise AssertionError(state)


_KEY_STATES = {
    "not_initialized": "key_not_initialized", "key_dir_invalid": "key_dir_invalid",
    "ready_invalid": "key_ready_invalid", "not_enrolled": "key_not_enrolled",
    "ready_not_committed": "key_ready_not_committed", "revoked": "key_revoked",
    "key_missing": "key_missing", "key_file_invalid": "key_file_invalid",
    "digest_mismatch": "key_digest_mismatch",
}
_TRACED = ["read_secrets", "prebind_listener", "apply_landlock", "apply_seccomp"]


@needs_x86_64
@needs_landlock
@pytest.mark.parametrize("feature", ["bot", "web"])
@pytest.mark.parametrize("state", sorted(_KEY_STATES))
def test_key_failure_stops_before_isolation_with_a_key_reason(state, feature, root, child_env):
    _break(state, root / "keys", PRINCIPAL[feature])
    got = run_canary({**_spec(root, feature), "trace": _TRACED}, child_env)
    assert got.rc == startup.RC_ISOLATION_FAILED
    assert got.stderr.strip().splitlines()[-1] == f"afx-ops: {_KEY_STATES[state]}"
    assert not got.isolated
    # 鍵の段で止まり、secret も listener も Landlock も seccomp も手前で呼ばれていない
    assert [line for line in got.stdout.splitlines() if line.startswith("CALL ")] == []


@needs_x86_64
@needs_landlock
def test_digest_mismatch_is_caught_although_the_key_file_is_readable(root, child_env):
    directory = root / "keys"
    _break("digest_mismatch", directory, "ops-bot")
    # 鍵 file 自体は通常 file・0600・64 hex で、secret の reader なら読めてしまう
    plan = startup.StartupPlan.for_feature("bot")
    import dataclasses
    loose = dataclasses.replace(plan, secret_dir=directory, secret_files=("ops-bot.token",))
    assert startup.read_secrets(loose)["ops-bot.token"].strip() == token_for("ops-bot").encode()
    got = run_canary(_spec(root, "bot"), child_env)
    assert got.rc == startup.RC_ISOLATION_FAILED
    assert got.stderr.strip().splitlines()[-1] == "afx-ops: key_digest_mismatch"


@needs_x86_64
@needs_landlock
@pytest.mark.parametrize("feature", FEATURES)
def test_other_principals_keys_are_never_opened(feature, root, child_env):
    for principal in keys.KNOWN_PRINCIPALS - {PRINCIPAL[feature]}:
        os.chmod(root / "keys" / keys.key_file_name(principal), 0)
    got = run_canary(_spec(root, feature, "api_token"), child_env)
    assert got.rc == 0, got.stderr[-2000:]
    assert got.result == ["bytes", digest(token_for(PRINCIPAL[feature]))]


def _fake_isolated(feature: str) -> startup.Isolated:
    return startup.Isolated(
        feature=feature, pid=1, proc_dir="/proc/1", secrets={}, listener=None,
        kept_fds=frozenset({0, 1, 2}), config=object(),
        endpoint=startup.endpoint_for(Path("/srv/afx")),
        api_token=token_for(PRINCIPAL[feature]).encode(), state_dir=Path("/srv/state"))


@pytest.mark.parametrize(("feature", "own"), [("bot", BotClient), ("web", WebClient),
                                              ("notify", NotifyClient)])
def test_client_for_binds_the_own_principal_only(feature, own):
    isolated = _fake_isolated(feature)
    client = startup.client_for(isolated, own)
    assert isinstance(client, own)
    assert token_for(PRINCIPAL[feature]) not in repr(client)
    assert token_for(PRINCIPAL[feature]) not in repr(isolated)
    for other in {BotClient, WebClient, NotifyClient} - {own}:
        with pytest.raises(ValueError):
            startup.client_for(isolated, other)


@pytest.mark.parametrize("stage", ["feature_filter", "read_api_token", "read_secrets",
                                    "apply_landlock"])
def test_isolation_failure_closes_notify_socket_and_prebound_listener(monkeypatch, tmp_path, stage):
    notify, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    notify_fd = notify.fileno()
    listener = None
    plan = startup.StartupPlan("web", tmp_path / "config", tmp_path / "secret", (),
                               tmp_path / "state", 0, (), False, False)
    from afx_ops.config import OpsConfig
    config = OpsConfig(tmp_path / "root", tmp_path / "keys", None)
    dirs = startup.PreparedDirs(-1, -1)

    class _Filter:
        compiled = object()

    def boom(*args, **kwargs):
        raise startup.StartupError("landlock_failed")

    monkeypatch.setattr(startup, "connect_notify", lambda *args, **kwargs: notify)
    monkeypatch.setattr(startup, "check_preconditions", lambda *args: None)
    monkeypatch.setattr(startup, "prepare_dirs", lambda *args, **kwargs: dirs)
    monkeypatch.setattr(startup, "close_unlisted_fds", lambda *args: None)
    monkeypatch.setattr(startup, "filter_count", lambda *args: 0)
    monkeypatch.setattr(startup, "feature_filter", lambda *args: _Filter())
    monkeypatch.setattr(startup, "read_api_token", lambda *args: b"token")
    monkeypatch.setattr(startup, "read_secrets", lambda *args: {})
    monkeypatch.setattr(startup, "apply_landlock", lambda *args: None)
    monkeypatch.setattr(startup, "prebind_listener", lambda *args: _capture_listener())

    captured = []
    def _capture_listener():
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        captured.append(sock)
        return sock

    if stage == "feature_filter":
        monkeypatch.setattr(startup, "feature_filter", boom)
    elif stage == "read_api_token":
        monkeypatch.setattr(startup, "read_api_token", boom)
    elif stage == "read_secrets":
        monkeypatch.setattr(startup, "read_secrets", boom)
    else:
        monkeypatch.setattr(startup, "apply_landlock", boom)
    try:
        with pytest.raises(startup.StartupError):
            startup.isolate(plan, config, plan_check=lambda value: None)
        assert notify.fileno() == -1
        with pytest.raises(OSError):
            os.fstat(notify_fd)
        assert notify_fd not in startup.open_fds(f"/proc/{os.getpid()}")
        for sock in captured:
            assert sock.fileno() == -1
    finally:
        peer.close()


@needs_x86_64
@needs_landlock
def test_token_authenticates_under_isolation_and_zero_bytes_reach_a_fake_server(root,
                                                                                child_env):
    layout = layout_of(root)
    with daemon(layout):
        got = run_canary(_spec(root, "web", "client_status"), child_env)
        received = layout.received_at_least(1)
    assert got.rc == 0, got.stderr[-2000:]
    assert got.result == [200, 0]
    assert sum(token_in([chunk], "ops-web") for chunk in received) == 1

    # lock を持たない偽の daemon には、認証待ちの段で止まり 1 byte も送らない
    stale = make_root(root.parent / "second")
    layout = layout_of(stale)
    layout.lock_path.write_text("")
    with daemon(layout, lock=False):
        got = run_canary({**_spec(stale, "web", "client_status"),
                          "wait": {"interval": 0.05, "limit": 1.0}}, child_env)
        received = layout.received(expect_any=False)
    assert got.rc == api_client.RC_UNREACHABLE
    assert got.stderr.strip().splitlines()[-1] == "afx-ops: not_lock_holder"
    assert not got.isolated
    assert sum(map(len, received)) == 0


# --- READY の時機 ----------------------------------------------------------------

@needs_x86_64
@needs_landlock
@pytest.mark.parametrize("feature", FEATURES)
def test_ready_waits_for_server_authentication(feature, root, child_env, manager):
    layout = layout_of(root)
    env = {**child_env, "NOTIFY_SOCKET": manager.env_value}
    child = Interactive({**_spec(root, feature), "require_notify": True,
                         "wait": {"interval": 0.05, "limit": 20.0}}, env)
    started = None
    try:
        child.wait_line("WAITING")
        # 自己検査は通ったが、server 認証の前なので READY=1 は届いていない
        assert "CHECKED-NOHOLD" in child.lines
        assert manager.pending() == []
        started = start(layout)
        assert manager.wait() == b"READY=1"
        done = child.finish()
        received = layout.received(expect_any=False)
    finally:
        child.kill()
        stop(started)
    assert done.rc == 0, done.stderr[-2000:]
    assert done.isolated
    # 待ちは鍵を送らない
    assert sum(map(len, received)) == 0


_AUTH_FAILURES = {"no_socket": "readiness_timeout", "no_lock_holder": "not_lock_holder"}


@needs_x86_64
@needs_landlock
@pytest.mark.parametrize("case", sorted(_AUTH_FAILURES))
def test_authentication_failure_sends_no_ready_and_exits_3(case, root, child_env, manager):
    layout = layout_of(root)
    env = {**child_env, "NOTIFY_SOCKET": manager.env_value}
    spec = {**_spec(root, "bot"), "require_notify": True,
            "wait": {"interval": 0.05, "limit": 1.0}}
    if case == "no_socket":
        got = run_canary(spec, env)
        received: list[bytes] = []
    else:
        layout.lock_path.write_text("")
        with daemon(layout, lock=False):
            got = run_canary(spec, env)
            received = layout.received(expect_any=False)
    assert got.rc == api_client.RC_UNREACHABLE
    reason = _AUTH_FAILURES[case]
    assert reason in api_client.CLIENT_REASONS + server_auth.REASONS
    assert got.stderr.strip().splitlines()[-1] == f"afx-ops: {reason}"
    assert manager.pending() == []
    assert not got.isolated
    assert sum(map(len, received)) == 0


def _dgram_pair():
    return socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)


class _Boom(Exception):
    pass


@pytest.mark.parametrize("outcome", ["ok", "api_error", "unexpected"])
def test_announce_ready_closes_the_notify_fd_on_success_and_on_failure(outcome):
    sock, peer = _dgram_pair()
    fd = sock.fileno()
    endpoint = startup.endpoint_for(Path("/srv/afx"))
    seen = []

    def wait(target):
        seen.append(target)
        if outcome == "api_error":
            raise ApiError("readiness_timeout")
        if outcome == "unexpected":
            raise _Boom()

    try:
        if outcome == "ok":
            startup.announce_ready(sock, endpoint, wait=wait)
        else:
            with pytest.raises((ApiError, _Boom)):
                startup.announce_ready(sock, endpoint, wait=wait)
        assert seen == [endpoint]
        assert sock.fileno() == -1
        with pytest.raises(OSError) as err:
            os.fstat(fd)
        assert err.value.errno == errno.EBADF
        peer.setblocking(False)
        got = []
        try:
            got.append(peer.recv(64))
        except BlockingIOError:
            pass
        assert got == ([b"READY=1"] if outcome == "ok" else [])
    finally:
        peer.close()


def test_announce_ready_without_a_notify_socket_still_waits():
    seen = []
    startup.announce_ready(None, startup.endpoint_for(Path("/srv/afx")), wait=seen.append)
    assert len(seen) == 1


def test_token_reaches_nothing_on_the_wire_before_body(tmp_path):
    # run の既定の待ちは鍵を持たない wait_ready (endpoint だけを受ける)
    import inspect
    param = inspect.signature(startup.run).parameters["wait"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is api_client.wait_ready
    assert list(inspect.signature(api_client.wait_ready).parameters)[0] == "endpoint"


@needs_x86_64
@needs_landlock
@pytest.mark.parametrize("feature", FEATURES)
def test_config_dir_fd_from_the_entry_is_used_and_closed_before_the_body(feature, root,
                                                                         child_env):
    # 起動口と同じく設定読込みで開いた config dir の fd を渡しても、body には漏れない
    got = run_canary({**_spec(root, feature, "body_fds"), "config_fd": True}, child_env)
    assert got.rc == 0, got.stderr[-2000:]
    open_fds, kept, _notify = got.result
    assert open_fds == kept
    assert len(kept) == (4 if feature == "web" else 3) and kept[:3] == [0, 1, 2]
    # 規則はその fd の dir に付いていて、読めるが書けない
    config = root / feature / "config"
    got = run_canary({**_spec(root, feature, "read_file"), "config_fd": True,
                      "params": {"path": str(config / "settings.txt")}}, child_env)
    assert got.rc == 0, got.stderr[-2000:]
    assert got.result == [len(feature) + 1, 0]
    got = run_canary({**_spec(root, feature, "write_state"), "config_fd": True,
                      "params": {"dir": str(config)}}, child_env)
    assert got.rc == 0, got.stderr[-2000:]
    assert got.result == [-1, errno.EACCES]


@needs_x86_64
@needs_landlock
@pytest.mark.parametrize("feature", FEATURES)
def test_body_config_hides_a_config_dir_fd_owned_by_startup(feature, root, child_env):
    got = run_canary({**_spec(root, feature, "config_view"), "config_fd": True}, child_env)

    assert got.rc == 0, got.stderr[-2000:]
    assert got.result["config_dir_fd"] is None


# --- 隔離下の runtime library と snapshot ---------------------------------------

_RUNTIME_MODULES = {"bot": ["discord"], "web": ["aiohttp.web"], "notify": ["aiohttp"]}


@needs_x86_64
@needs_landlock
@pytest.mark.parametrize("feature", FEATURES)
def test_runtime_libraries_import_under_isolation(feature, root, child_env):
    if feature == "web":
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("127.0.0.1", 0))
        except PermissionError:
            pytest.skip("この sandbox は loopback listener の作成・bind を許可しない")
    got = run_canary(_spec(root, feature, "import_runtime",
                           params={"modules": _RUNTIME_MODULES[feature]}), child_env)
    assert got.rc == 0, got.stderr[-2000:]
    assert got.result == [len(_RUNTIME_MODULES[feature]), 0]


@needs_x86_64
@needs_landlock
@pytest.mark.parametrize("feature", FEATURES)
def test_snapshot_round_trips_under_isolation(feature, root, child_env):
    got = run_canary(_spec(root, feature, "snapshot_rounds", params={"rounds": 200}), child_env)
    assert got.rc == 0, got.stderr[-2000:]
    # 一時 file を残さず、正本だけが state dir にある
    assert got.result == [1, ["snapshot.json"]]
