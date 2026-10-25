"""Offline status: instance lock probe, locked output and the state reader."""
from __future__ import annotations

import builtins
import fcntl
import os
import threading

import pytest

from agentic_fx import status
from agentic_fx.ops.contracts import Principal
from agentic_fx.ops.keys import key_dir
from agentic_fx.store.instance_lock import acquire_instance_lock
from tests.status_support import (
    NOTE, UNKNOWN_HOLDER, held_by_other_process, make_db, make_run, manifest, state_json, touch_lock,
    write_activity, write_enroll_marker, write_key, write_settings, write_setup, write_setup_marker,
    write_state,
)

JOURNAL = "journalctl --user -u afx.service (operator の鍵が無いため afx ctl status は使えません)"
READERS = ("read_state", "connect_observer_readonly", "tail_lines", "collect_status", "read_setup_record",
           "count_open_readonly", "collect_pending_facts", "pending_steps", "load_settings_text")


@pytest.fixture(autouse=True)
def unit_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(status, "default_unit_dir", lambda: tmp_path / "unit")


def _forbid_readers(monkeypatch):
    calls = []

    def forbid(name):
        def spy(*_args, **_kwargs):
            calls.append(name)
            raise AssertionError(name)
        return spy

    for name in READERS:
        monkeypatch.setattr(status, name, forbid(name))
    return calls


def _root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    return root


# locked output

HOLDERS = {
    "setup": dict(setup=True, enroll=None, locked_by="setup",
                  next="afx init の完了を待ってから afx status"),
    "enroll-bot": dict(setup=False, enroll='{"feature":"bot","started_at":"2026-10-25T00:00:00+00:00"}',
                       locked_by="enroll:bot", next="afx ops bot status"),
    "enroll-web": dict(setup=False, enroll='{"feature":"web","started_at":"2026-10-25T00:00:00+00:00"}',
                       locked_by="enroll:web", next="afx ops web status"),
    "enroll-notify": dict(setup=False, enroll='{"feature":"notify","started_at":"2026-10-25T00:00:00+00:00"}',
                          locked_by="enroll:notify", next="afx ops notify status"),
    "both": dict(setup=True, enroll='{"feature":"web","started_at":"2026-10-25T00:00:00+00:00"}',
                 locked_by="enroll:web+setup", next="afx ops web status"),
    "enroll-unreadable": dict(setup=False, enroll="not json", locked_by="enroll:unknown",
                              next="afx ops の有効化の完了を待ってから afx status"),
    "enroll-unreadable-and-setup": dict(setup=True, enroll="not json", locked_by="enroll:unknown+setup",
                                        next="afx ops の有効化の完了を待ってから afx status"),
}


def _expected_locked(root, locked_by, next_step):
    return "\n".join(("agentic-fx offline status", f"root: {root}", "instance: locked",
                      f"locked-by: {locked_by}", NOTE, f"next: {next_step}")) + "\n"


@pytest.mark.parametrize("keys", ["operator", "approver", "both", "none"])
@pytest.mark.parametrize("holder", sorted(HOLDERS))
def test_locked_output_is_six_exact_lines_for_every_holder(tmp_path, monkeypatch, capsys, holder, keys):
    spec = HOLDERS[holder]
    root = _root(tmp_path)
    lock = touch_lock(root)
    if spec["setup"]:
        write_setup_marker(root)
    if spec["enroll"] is not None:
        write_enroll_marker(root, spec["enroll"])
    if keys in ("operator", "both"):
        write_key(root, Principal.OPERATOR)
    if keys in ("approver", "both"):
        write_key(root, Principal.APPROVER)
    before, before_keys = manifest(root), (manifest(key_dir(root)) if key_dir(root).exists() else {})
    readers = _forbid_readers(monkeypatch)

    with held_by_other_process(lock):
        rc = status.main(root, run=lambda *a, **k: pytest.fail("systemctl must not run when locked"))

    out = capsys.readouterr()
    assert rc == 0
    assert out.err == ""
    assert readers == []
    assert out.out == _expected_locked(root, spec["locked_by"], spec["next"])
    assert manifest(root) == before
    assert (manifest(key_dir(root)) if key_dir(root).exists() else {}) == before_keys


@pytest.mark.parametrize("keys,expected", [("operator", "afx ctl status"), ("approver", "afx ctl status"),
                                           ("none", JOURNAL)])
def test_locked_with_no_marker_depends_on_the_key_files(tmp_path, monkeypatch, capsys, keys, expected):
    root = _root(tmp_path)
    lock = touch_lock(root)
    if keys != "none":
        write_key(root, Principal.OPERATOR if keys == "operator" else Principal.APPROVER)
    _forbid_readers(monkeypatch)

    with held_by_other_process(lock):
        assert status.main(root) == 0

    assert capsys.readouterr().out == _expected_locked(root, UNKNOWN_HOLDER, expected)


def test_locked_next_is_chosen_without_reading_key_contents(tmp_path, monkeypatch, capsys):
    root = _root(tmp_path)
    lock = touch_lock(root)
    path = write_key(root, Principal.OPERATOR, mode=0o000)
    opened = []
    real_open, real_os_open = builtins.open, os.open

    def spy_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    def spy_os_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_os_open(file, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", spy_open)
        patch.setattr(os, "open", spy_os_open)
        with held_by_other_process(lock):
            assert status.main(root) == 0
    try:
        assert capsys.readouterr().out == _expected_locked(root, UNKNOWN_HOLDER, "afx ctl status")
        assert not [name for name in opened if name.endswith(".token")]
    finally:
        os.chmod(path, 0o600)


# state reader

def _state_root(tmp_path, text, **kwargs):
    root = _root(tmp_path)
    write_state(root, text, **kwargs)
    return root


@pytest.mark.parametrize("lock", [False, True])
@pytest.mark.parametrize("case,text,marker,expected", [
    ("clear", state_json("learning"), False, ("learning", "clear")),
    ("latched", state_json("trading", latched=True), False, ("trading", "latched")),
    ("reset marker", state_json("learning"), True, ("learning", "latched")),
    ("corrupt", "{broken", False, ("unavailable", "unavailable")),
])
def test_state_reader_values(tmp_path, lock, case, text, marker, expected):
    root = _state_root(tmp_path, text, lock=lock, reset_marker=marker)

    assert status.read_state(root) == expected


def test_state_reader_missing_state_reports_defaults_without_a_lock_file(tmp_path):
    root = _root(tmp_path)
    (root / "data" / "state").mkdir(parents=True)
    assert status.read_state(root) == ("learning", "clear")
    assert not list((root / "data" / "state").iterdir())
    assert status.read_state(tmp_path / "nowhere") == ("learning", "clear")
    assert not (tmp_path / "nowhere").exists()


def test_state_reader_corrupt_values_never_pass_through(tmp_path):
    for number, text in enumerate(('{"mode":"weird","kill_switch_latched":false}',
                                   '{"mode":"learning","kill_switch_latched":"no"}', "[]", "")):
        root = tmp_path / f"r{number}"
        write_state(root, text)
        assert status.read_state(root) == ("unavailable", "unavailable")


def test_state_reader_does_not_create_the_lock_file_and_leaves_the_tree_alone(tmp_path):
    root = _state_root(tmp_path, state_json())
    before = manifest(root)

    assert status.read_state(root) == ("learning", "clear")

    assert manifest(root) == before
    assert not (root / "data" / "state" / "app_state.json.lock").exists()


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def test_state_reader_waits_fifty_milliseconds_per_try_for_one_second_in_total(tmp_path):
    root = _state_root(tmp_path, state_json(), lock=True)
    lock = root / "data" / "state" / "app_state.json.lock"
    clock = _Clock()

    with held_by_other_process(lock, fcntl.LOCK_EX):
        result = status.read_state(root, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result == ("unavailable", "unavailable")
    assert clock.sleeps and set(clock.sleeps) == {0.05}
    assert 0.95 <= sum(clock.sleeps) <= 1.05
    assert 19 <= len(clock.sleeps) <= 21


def test_state_reader_gets_the_value_when_the_writer_releases_inside_the_window(tmp_path):
    root = _state_root(tmp_path, state_json("trading"), lock=True)
    lock = root / "data" / "state" / "app_state.json.lock"
    clock = _Clock()
    holder = held_by_other_process(lock, fcntl.LOCK_EX)
    process = holder.__enter__()
    released = []

    def sleep(seconds):
        clock.sleep(seconds)
        if len(clock.sleeps) == 3 and not released:
            released.append(True)
            holder.__exit__(None, None, None)

    try:
        assert status.read_state(root, sleep=sleep, monotonic=clock.monotonic) == ("trading", "clear")
    finally:
        if not released:
            holder.__exit__(None, None, None)
    assert process.returncode == 0
    assert len(clock.sleeps) >= 3


def test_main_continues_with_an_unavailable_state_when_the_state_lock_is_held(tmp_path, capsys):
    root = _state_root(tmp_path, state_json(), lock=True)
    touch_lock(root)
    write_settings(root, "ok")
    write_setup(root, "ok")
    lock = root / "data" / "state" / "app_state.json.lock"

    with held_by_other_process(lock, fcntl.LOCK_EX):
        rc = status.main(root, run=make_run())

    out = capsys.readouterr().out
    assert rc == 1
    assert "instance: free" in out
    assert "state: mode=unavailable kill_switch=unavailable" in out
    assert "setup: ok" in out and "settings: ok" in out
    assert out.rstrip("\n").splitlines()[-2] == NOTE


def test_state_store_load_and_shared_are_never_called(tmp_path, monkeypatch, capsys):
    from agentic_fx.store import state as state_module

    root = _state_root(tmp_path, state_json(), lock=True)
    touch_lock(root)
    calls = []
    for name in ("load", "_shared", "_exclusive", "save", "update"):
        if hasattr(state_module.StateStore, name):
            monkeypatch.setattr(state_module.StateStore, name,
                                lambda *a, _n=name, **k: calls.append(_n) or (_ for _ in ()).throw(AssertionError(_n)))

    assert status.main(root, run=make_run()) == 0
    assert calls == []
    assert "state: mode=learning kill_switch=clear" in capsys.readouterr().out


def _recording_open(monkeypatch_ctx):
    records = []
    real = os.open

    def spy(path, flags, *args, **kwargs):
        records.append((str(path), flags))
        return real(path, flags, *args, **kwargs)

    monkeypatch_ctx.setattr(os, "open", spy)
    return records


@pytest.mark.parametrize("with_state_lock", [False, True])
def test_neither_lock_file_is_opened_with_create(tmp_path, monkeypatch, capsys, with_state_lock):
    root = _state_root(tmp_path, state_json(), lock=with_state_lock)
    touch_lock(root)
    with monkeypatch.context() as patch:
        records = _recording_open(patch)
        assert status.main(root, run=make_run()) == 0
    capsys.readouterr()

    locks = [(path, flags) for path, flags in records if path.endswith(".lock")]
    instance = [flags for path, flags in locks if path.endswith("instance.lock")]
    state_locks = [flags for path, flags in locks if path.endswith("app_state.json.lock")]
    assert len(instance) == 1
    assert len(state_locks) == 1
    assert (root / "data" / "state" / "app_state.json.lock").exists() is with_state_lock
    for flags in instance + state_locks:
        assert flags & os.O_CREAT == 0
        assert flags & os.O_ACCMODE == os.O_RDONLY


def test_instance_shared_lock_is_released_before_the_state_is_read(tmp_path, monkeypatch, capsys):
    root = _state_root(tmp_path, state_json())
    touch_lock(root)
    observed = []
    real = status.read_state

    def read_state(*args, **kwargs):
        try:
            handle = acquire_instance_lock(root / "data")
        except Exception as error:  # noqa: BLE001
            observed.append(error)
        else:
            observed.append("acquired")
            handle.close()
        return real(*args, **kwargs)

    monkeypatch.setattr(status, "read_state", read_state)

    assert status.main(root, run=make_run()) == 0
    capsys.readouterr()
    assert observed == ["acquired"]


def test_a_second_process_can_take_the_exclusive_instance_lock_while_state_is_read(tmp_path, monkeypatch, capsys):
    root = _state_root(tmp_path, state_json())
    lock = touch_lock(root)
    outcome = []
    real = status.read_state

    def read_state(*args, **kwargs):
        fd = os.open(lock, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            outcome.append("held")
        except OSError:
            outcome.append("blocked")
        finally:
            os.close(fd)
        return real(*args, **kwargs)

    monkeypatch.setattr(status, "read_state", read_state)
    assert status.main(root, run=make_run()) == 0
    capsys.readouterr()
    assert outcome == ["held"]


# unknown instance

def _spy_state(monkeypatch):
    calls = []
    monkeypatch.setattr(status, "read_state", lambda *a, **k: calls.append(1) or ("learning", "clear"))
    return calls


def test_missing_instance_lock_is_unknown_and_the_state_reader_is_not_called(tmp_path, monkeypatch, capsys):
    root = _state_root(tmp_path, state_json())
    calls = _spy_state(monkeypatch)

    rc = status.main(root, run=make_run())

    out = capsys.readouterr().out
    assert calls == []
    assert rc == 0
    assert "instance: unknown" in out.splitlines()
    assert "state: mode=unavailable kill_switch=unavailable" in out
    assert NOTE in out.splitlines()
    assert not (root / "data" / "instance.lock").exists()


def test_unopenable_instance_lock_is_unknown(tmp_path, monkeypatch, capsys):
    if os.getuid() == 0:
        pytest.skip("mode 000 does not block root")
    root = _state_root(tmp_path, state_json())
    lock = touch_lock(root)
    os.chmod(lock, 0o000)
    calls = _spy_state(monkeypatch)
    try:
        status.main(root, run=make_run())
    finally:
        os.chmod(lock, 0o600)
    out = capsys.readouterr().out
    assert calls == []
    assert "instance: unknown" in out.splitlines()
    assert NOTE in out.splitlines()


def test_instance_lock_below_a_file_is_unknown(tmp_path, monkeypatch, capsys):
    root = _root(tmp_path)
    (root / "data").write_text("not a directory", encoding="utf-8")
    calls = _spy_state(monkeypatch)

    status.main(root, run=make_run())

    out = capsys.readouterr().out
    assert calls == []
    assert "instance: unknown" in out.splitlines()
    assert NOTE in out.splitlines()
    assert (root / "data").read_text(encoding="utf-8") == "not a directory"


def test_the_observation_sentence_is_printed_for_locked_free_and_unknown(tmp_path, capsys):
    root = _root(tmp_path)
    lock = touch_lock(root)
    write_state(root, state_json())

    status.main(root, run=make_run())
    free = capsys.readouterr().out
    with held_by_other_process(lock):
        status.main(root, run=make_run())
    locked = capsys.readouterr().out
    os.unlink(lock)
    status.main(root, run=make_run())
    unknown = capsys.readouterr().out

    for text, word in ((free, "free"), (locked, "locked"), (unknown, "unknown")):
        assert f"instance: {word}" in text.splitlines()
        assert text.splitlines().count(NOTE) == 1


# concurrency with the daemon lock

def _instance_flock_log(monkeypatch_ctx, lock_path):
    log = []
    real = fcntl.flock

    def spy(fd, operation):
        try:
            target = os.readlink(f"/proc/self/fd/{fd if isinstance(fd, int) else fd.fileno()}")
        except OSError:
            target = ""
        if target == str(lock_path.resolve()):
            log.append(operation)
        return real(fd, operation)

    monkeypatch_ctx.setattr(fcntl, "flock", spy)
    return log


def _concurrent_acquire(root):
    """Try the daemon's start-up lock from another thread; return whether it succeeded."""
    result = {}

    def work():
        try:
            handle = acquire_instance_lock(root / "data")
        except Exception as error:  # noqa: BLE001
            result["error"] = error
        else:
            result["ok"] = True
            handle.close()

    thread = threading.Thread(target=work)
    thread.start()
    thread.join(10)
    assert not thread.is_alive()
    return result


@pytest.mark.parametrize("reader", ["read_state", "connect_observer_readonly"])
def test_the_daemon_can_take_the_instance_lock_while_a_reader_is_waiting(tmp_path, monkeypatch, capsys, reader):
    root = _state_root(tmp_path, state_json())
    lock = touch_lock(root)
    make_db(root)
    results = []
    real = getattr(status, reader)

    def waiting(*args, **kwargs):
        results.append(_concurrent_acquire(root))
        return real(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(status, reader, waiting)
        log = _instance_flock_log(patch, lock)
        rc = status.main(root, run=make_run())

    capsys.readouterr()
    assert rc == 0
    assert results and results[0] == {"ok": True}
    shared = [operation for operation in log if operation & fcntl.LOCK_SH]
    assert shared == [fcntl.LOCK_SH | fcntl.LOCK_NB]


@pytest.mark.parametrize("instance", ["free", "unknown"])
def test_the_instance_lock_is_taken_shared_at_most_once_per_run(tmp_path, monkeypatch, capsys, instance):
    root = _state_root(tmp_path, state_json())
    lock = touch_lock(root)
    make_db(root)
    if instance == "unknown":
        os.unlink(lock)
        lock = root / "data" / "instance.lock"
    with monkeypatch.context() as patch:
        log = _instance_flock_log(patch, lock)
        status.main(root, run=make_run())
    capsys.readouterr()

    shared = [operation for operation in log if operation & fcntl.LOCK_SH]
    assert len(shared) == (1 if instance == "free" else 0)
    assert not [operation for operation in log if operation & fcntl.LOCK_EX]
