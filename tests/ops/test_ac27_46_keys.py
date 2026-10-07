"""鍵 lifecycle: init / rotate / revoke と `.ready` の commit marker。"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_fx.ops import keys
from agentic_fx.ops.contracts import Principal


def _root(tmp_path: Path, name: str = "root") -> Path:
    root = tmp_path / name
    (root / "data").mkdir(parents=True)
    return root


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _token(directory: Path, principal: Principal) -> str:
    return (directory / keys.key_file_name(principal)).read_text().strip()


class Crash(Exception):
    pass


def _crash_at(step: str):
    def hook(name: str) -> None:
        if name == step:
            raise Crash(step)
    return hook


# ---------------------------------------------------------------- AC-27

def test_ac27_first_init_creates_instance_dir_0700_and_two_keys_and_ready_0600(tmp_path):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    assert directory == Path.home() / ".config" / "agentic-fx" / "api" / keys.instance_id(root)
    created = keys.initialize(directory, root=root)
    assert set(created) == set(Principal)
    assert _mode(directory) == 0o700
    for principal in Principal:
        path = directory / keys.key_file_name(principal)
        assert _mode(path) == 0o600
        token = _token(directory, principal)
        assert len(token) == 64 and int(token, 16) >= 0  # 256 bit
    assert _mode(directory / keys.READY_NAME) == 0o600
    ready = json.loads((directory / keys.READY_NAME).read_text())
    for principal in Principal:
        entry = ready["principals"][principal.value]
        assert entry["state"] == "active" and entry["generation"] == 1
    loaded = keys.load_keyset(directory)
    for principal in Principal:
        assert loaded.authenticate(_token(directory, principal)) is principal
    assert loaded.authenticate("0" * 64) is None
    assert loaded.authenticate("") is None


def test_ac27_keyset_holds_digests_not_plaintext(tmp_path):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    loaded = keys.load_keyset(directory)
    blob = repr(loaded) + repr(vars(loaded) if hasattr(loaded, "__dict__") else "")
    for slot in getattr(type(loaded), "__slots__", ()):
        blob += repr(getattr(loaded, slot))
    for principal in Principal:
        assert _token(directory, principal) not in blob


def test_ac46_key_file_owned_by_another_uid_is_not_read(tmp_path, monkeypatch):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    fd = keys._open_dir(directory)
    try:
        actual = keys.os.fstat
        monkeypatch.setattr(
            keys.os, "fstat",
            lambda opened: SimpleNamespace(
                st_mode=actual(opened).st_mode,
                st_uid=os.getuid() + 1),
        )
        with pytest.raises(OSError, match="key file check failed"):
            keys._read_checked(fd, keys.key_file_name(Principal.OPERATOR))
    finally:
        os.close(fd)


def test_ac27_roots_get_separate_key_dirs(tmp_path):
    first, second = _root(tmp_path, "a"), _root(tmp_path, "b")
    assert keys.instance_id(first) != keys.instance_id(second)
    keys.initialize(keys.key_dir(first), root=first)
    keys.initialize(keys.key_dir(second), root=second)
    a = _token(keys.key_dir(first), Principal.APPROVER)
    b = _token(keys.key_dir(second), Principal.APPROVER)
    assert a != b
    assert keys.load_keyset(keys.key_dir(second)).authenticate(a) is None


@pytest.mark.parametrize("step", ["key_written:operator", "key_linked:operator",
                                  "key_written:approver", "keys_dir_synced",
                                  "ready_written", "ready_renamed"])
def test_ac27_crashed_first_init_completes_only_the_missing_parts(tmp_path, step):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    with pytest.raises(Crash):
        keys.initialize(directory, root=root, hook=_crash_at(step))
    before = {}
    for principal in Principal:
        path = directory / keys.key_file_name(principal)
        if path.exists():
            before[principal] = (path.read_bytes(), path.stat().st_mtime_ns)
    keys.initialize(directory, root=root)
    for principal, (content, mtime) in before.items():
        path = directory / keys.key_file_name(principal)
        assert path.read_bytes() == content
        assert path.stat().st_mtime_ns == mtime
    loaded = keys.load_keyset(directory)
    for principal in Principal:
        assert loaded.authenticate(_token(directory, principal)) is principal
    leftovers = [p.name for p in directory.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [] or all(name.startswith(".") for name in leftovers)


def test_ac27_missing_key_after_ready_is_not_completed_and_load_fails(tmp_path):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    (directory / keys.key_file_name(Principal.OPERATOR)).unlink()
    with pytest.raises(keys.KeySetError) as err:
        keys.load_keyset(directory)
    assert err.value.code == "key_missing"
    # init は .ready の後を補完しない
    with pytest.raises(keys.KeySetError):
        keys.initialize(directory, root=root)
    assert not (directory / keys.key_file_name(Principal.OPERATOR)).exists()


def test_ac27_digest_mismatch_after_ready_fails_closed(tmp_path):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    path = directory / keys.key_file_name(Principal.APPROVER)
    path.write_text("f" * 64 + "\n")
    with pytest.raises(keys.KeySetError) as err:
        keys.load_keyset(directory)
    assert err.value.code == "digest_mismatch"


def test_ac27_key_commands_refuse_while_instance_running(tmp_path, capsys):
    from agentic_fx.store.instance_lock import acquire_instance_lock
    root = _root(tmp_path)
    assert keys.run_keys_command(root, "init") == 0
    directory = keys.key_dir(root)
    snapshot = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in directory.iterdir()}
    held = acquire_instance_lock(root / "data")
    try:
        for action, principal in (("init", None), ("rotate", "operator"),
                                  ("revoke", "approver")):
            assert keys.run_keys_command(root, action, principal) == 1
    finally:
        held.close()
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
            for p in directory.iterdir()} == snapshot


@pytest.mark.parametrize("bad_home", ["/etc", "/proc", "venv"])
def test_ac27_ac5_invalid_token_dir_is_never_created(tmp_path, monkeypatch, bad_home):
    root = _root(tmp_path)
    home = sys.prefix if bad_home == "venv" else bad_home
    monkeypatch.setenv("HOME", home)
    directory = keys.key_dir(root)
    with pytest.raises(keys.KeySetError) as err:
        keys.check_token_dir(directory, root=root)
    assert err.value.code == "token_dir_in_sandbox"
    assert keys.run_keys_command(root, "init") == 1
    assert not directory.exists()


def test_ac5_token_dir_under_worker_allowlist_is_rejected(tmp_path):
    root = _root(tmp_path)
    import agentic_fx
    code_root = Path(agentic_fx.__file__).resolve().parent
    for inside in (code_root / "x", root / "plugins" / "k", Path("/usr/lib/afx-keys"),
                   Path("/dev/afx")):
        with pytest.raises(keys.KeySetError) as err:
            keys.check_token_dir(inside, root=root)
        assert err.value.code == "token_dir_in_sandbox"
    keys.check_token_dir(keys.key_dir(root), root=root)


def test_ac27_service_and_manual_init_share_one_generator(tmp_path, monkeypatch):
    calls = []
    original = keys.new_token

    def spy() -> str:
        calls.append(1)
        return original()

    monkeypatch.setattr(keys, "new_token", spy)
    root = _root(tmp_path)
    assert keys.run_keys_command(root, "init") == 0
    assert len(calls) == 2
    other = _root(tmp_path, "other")
    keys.ensure_initialized(keys.key_dir(other), root=other)
    assert len(calls) == 4


# ---------------------------------------------------------------- AC-6

def test_ac6_world_readable_key_disables_only_that_principal(tmp_path):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    os.chmod(directory / keys.key_file_name(Principal.OPERATOR), 0o644)
    loaded = keys.load_keyset(directory)
    assert loaded.authenticate(_token(directory, Principal.OPERATOR)) is None
    assert loaded.authenticate(_token(directory, Principal.APPROVER)) is Principal.APPROVER
    assert Principal.OPERATOR in loaded.disabled


def test_ac6_symlinked_key_is_not_followed(tmp_path):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    real = directory / keys.key_file_name(Principal.OPERATOR)
    token = real.read_text().strip()
    outside = tmp_path / "outside.token"
    outside.write_text(token + "\n")
    os.chmod(outside, 0o600)
    real.unlink()
    real.symlink_to(outside)
    loaded = keys.load_keyset(directory)
    assert loaded.authenticate(token) is None


def test_ac6_key_dir_with_wrong_mode_fails(tmp_path):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    os.chmod(directory, 0o755)
    with pytest.raises(keys.KeySetError) as err:
        keys.load_keyset(directory)
    assert err.value.code == "key_dir_invalid"


# ---------------------------------------------------------------- AC-46

_ROTATE_STEPS = ["preparing:written", "preparing:renamed", "preparing:dir_synced",
                 "key:written", "key:renamed", "key:dir_synced",
                 "ready:written", "ready:renamed", "ready:dir_synced"]


@pytest.mark.parametrize("step", _ROTATE_STEPS)
def test_ac46_rotate_crash_points_never_reenable_old_token(tmp_path, step):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    old = _token(directory, Principal.OPERATOR)
    with pytest.raises(Crash):
        keys.rotate(directory, Principal.OPERATOR, hook=_crash_at(step))
    try:
        loaded = keys.load_keyset(directory)
    except keys.KeySetError as err:
        assert err.code == "ready_not_committed"
    else:
        if step in ("ready:renamed", "ready:dir_synced"):
            # commit 済み (dir fsync の前後どちらで落ちても): 新世代だけが有効
            assert loaded.authenticate(old) is None
            assert loaded.authenticate(_token(directory, Principal.OPERATOR)) \
                is Principal.OPERATOR
            return
        # preparing の commit 前に落ちた: rotate は始まっておらず旧鍵のまま
        assert step == "preparing:written"
        assert loaded.authenticate(old) is Principal.OPERATOR
    # 同じ command の再実行で完了させる
    keys.rotate(directory, Principal.OPERATOR)
    loaded = keys.load_keyset(directory)
    new = _token(directory, Principal.OPERATOR)
    assert new != old
    assert loaded.authenticate(old) is None
    assert loaded.authenticate(new) is Principal.OPERATOR
    ready = json.loads((directory / keys.READY_NAME).read_text())
    assert ready["principals"]["operator"] == {
        "generation": 2, "state": "active",
        "sha256": ready["principals"]["operator"]["sha256"]}


@pytest.mark.parametrize("operation", ["rotate", "revoke"])
def test_ac46_commit_rename_is_followed_by_a_directory_fsync_before_the_hook(
        tmp_path, monkeypatch, operation):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    events: list[str] = []
    real_fsync = os.fsync

    def spy(fd):
        events.append("dir_fsync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file_fsync")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)
    getattr(keys, operation)(directory, Principal.OPERATOR, hook=events.append)
    renamed = events.index("ready:renamed")
    synced = events.index("ready:dir_synced")
    assert "dir_fsync" in events[renamed:synced]


def test_ac46_rotate_then_only_new_token_succeeds(tmp_path):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    old_approver = _token(directory, Principal.APPROVER)
    old_operator = _token(directory, Principal.OPERATOR)
    keys.rotate(directory, Principal.APPROVER)
    loaded = keys.load_keyset(directory)
    assert loaded.authenticate(old_approver) is None
    assert loaded.authenticate(_token(directory, Principal.APPROVER)) is Principal.APPROVER
    assert loaded.authenticate(old_operator) is Principal.OPERATOR


_REVOKE_STEPS = ["preparing:written", "preparing:renamed", "preparing:dir_synced",
                 "tombstone:written", "tombstone:renamed", "tombstone:dir_synced",
                 "ready:written", "ready:renamed", "ready:dir_synced"]


@pytest.mark.parametrize("step", _REVOKE_STEPS)
def test_ac46_revoke_crash_points_and_completion(tmp_path, step):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    old = _token(directory, Principal.APPROVER)
    with pytest.raises(Crash):
        keys.revoke(directory, Principal.APPROVER, hook=_crash_at(step))
    try:
        loaded = keys.load_keyset(directory)
    except keys.KeySetError as err:
        assert err.code == "ready_not_committed"
    else:
        assert step in ("preparing:written", "ready:renamed", "ready:dir_synced")
        assert (loaded.authenticate(old) is Principal.APPROVER) == (
            step == "preparing:written")
    keys.revoke(directory, Principal.APPROVER)
    loaded = keys.load_keyset(directory)
    assert loaded.authenticate(old) is None
    assert Principal.APPROVER in loaded.disabled
    assert old not in (directory / keys.key_file_name(Principal.APPROVER)).read_text()
    # 既に revoked の revoke は同じ状態を返す
    ready_before = (directory / keys.READY_NAME).read_bytes()
    keys.revoke(directory, Principal.APPROVER)
    assert (directory / keys.READY_NAME).read_bytes() == ready_before


def test_ac46_revoked_principal_can_be_reissued_by_rotate(tmp_path):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    keys.revoke(directory, Principal.OPERATOR)
    keys.rotate(directory, Principal.OPERATOR)
    loaded = keys.load_keyset(directory)
    assert loaded.authenticate(_token(directory, Principal.OPERATOR)) is Principal.OPERATOR


def test_ac46_owner_or_mode_violation_in_ready_is_not_accepted(tmp_path):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    os.chmod(directory / keys.READY_NAME, 0o644)
    with pytest.raises(keys.KeySetError) as err:
        keys.load_keyset(directory)
    assert err.value.code == "ready_invalid"


def test_ac46_symlinked_ready_is_not_followed(tmp_path):
    root = _root(tmp_path)
    directory = keys.key_dir(root)
    keys.initialize(directory, root=root)
    ready = directory / keys.READY_NAME
    copy = tmp_path / "ready.copy"
    copy.write_bytes(ready.read_bytes())
    os.chmod(copy, 0o600)
    ready.unlink()
    ready.symlink_to(copy)
    with pytest.raises(keys.KeySetError) as err:
        keys.load_keyset(directory)
    assert err.value.code == "ready_invalid"


def test_ac46_keys_cli_rotate_and_revoke_via_entry(tmp_path, monkeypatch, capsys):
    from agentic_fx.entry import main
    root = _root(tmp_path)
    monkeypatch.chdir(root)
    assert main(["keys", "init"]) == 0
    directory = keys.key_dir(root)
    old = _token(directory, Principal.OPERATOR)
    assert main(["keys", "rotate", "operator"]) == 0
    assert _token(directory, Principal.OPERATOR) != old
    assert main(["keys", "revoke", "approver"]) == 0
    loaded = keys.load_keyset(directory)
    assert Principal.APPROVER in loaded.disabled
    out = capsys.readouterr()
    assert old not in out.out + out.err
    assert _token(directory, Principal.OPERATOR) not in out.out + out.err
    with pytest.raises(SystemExit):
        main(["keys", "rotate", "nobody"])


def test_keys_cli_does_not_load_dotenv(tmp_path, monkeypatch):
    from agentic_fx import config as config_mod
    from agentic_fx.entry import main
    root = _root(tmp_path)
    (root / ".env").write_text("AFX_KEYS_PROBE=1\n")
    monkeypatch.chdir(root)
    called = []
    monkeypatch.setattr(config_mod, "load_env_file", lambda *a: called.append(a))
    monkeypatch.delenv("AFX_KEYS_PROBE", raising=False)
    assert main(["keys", "init"]) == 0
    assert called == [] and "AFX_KEYS_PROBE" not in os.environ
