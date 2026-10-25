"""Offline status reads local state without changing the instance."""
from __future__ import annotations

import fcntl
import os
from types import SimpleNamespace

from agentic_fx import status


def test_probe_instance_lock_does_not_create_a_missing_lock_file(tmp_path):
    assert status.probe_instance_lock(tmp_path) == "unknown"
    assert not (tmp_path / "data" / "instance.lock").exists()


def test_probe_instance_lock_reports_a_lock_held_by_another_fd(tmp_path):
    lock_path = tmp_path / "data" / "instance.lock"
    lock_path.parent.mkdir()
    lock_path.touch()
    fd = os.open(lock_path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert status.probe_instance_lock(tmp_path) == "locked"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_read_state_without_lock_file_does_not_create_one(tmp_path):
    state_path = tmp_path / "data" / "state" / "app_state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text('{"initialized":true,"mode":"paper","autopilot":false,"kill_switch_latched":false}', encoding="utf-8")

    assert status.read_state(tmp_path) == ("paper", "clear")
    assert not state_path.with_name("app_state.json.lock").exists()


def test_read_state_marker_latches_the_kill_switch(tmp_path):
    state_path = tmp_path / "data" / "state" / "app_state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text('{"initialized":true,"mode":"live","autopilot":false,"kill_switch_latched":false}', encoding="utf-8")
    state_path.with_name("app_state.json.reset-in-progress").write_text("{}", encoding="utf-8")

    assert status.read_state(tmp_path) == ("live", "latched")


def test_render_locked_status_is_exact_five_lines():
    assert status.render_locked("setup") == "\n".join((
        "agentic-fx offline status",
        "instance: locked",
        "locked-by: setup",
        "note: この表示は観測時点の情報です。複数データ源の原子的 snapshot ではありません。",
        "next: afx init の完了を待ってから afx status",
    )) + "\n"


def test_locked_by_prefers_enroll_and_uses_unknown_for_an_invalid_marker(tmp_path):
    enroll = tmp_path / "data" / "ops" / "enroll_in_progress"
    enroll.parent.mkdir(parents=True)

    assert status._locked_by(tmp_path) == "unknown (daemon 稼働中とみられる)"
    (tmp_path / "data" / "state").mkdir()
    (tmp_path / "data" / "state" / "setup_in_progress").touch()
    assert status._locked_by(tmp_path) == "setup"
    enroll.write_text('{"feature":"web","started_at":"2026-10-25T00:00:00+00:00"}', encoding="utf-8")
    assert status._locked_by(tmp_path) == "enroll:web+setup"
    enroll.write_text("not json", encoding="utf-8")
    assert status._locked_by(tmp_path) == "enroll:unknown+setup"


def test_locked_next_uses_keys_only_for_an_unidentified_holder(tmp_path, monkeypatch):
    keys = tmp_path / "keys"
    monkeypatch.setattr(status, "key_dir", lambda _root: keys)

    unknown = "unknown (daemon 稼働中とみられる)"
    assert "journalctl --user -u afx.service" in status._locked_next(tmp_path, unknown)
    keys.mkdir()
    (keys / "operator.token").touch()
    assert status._locked_next(tmp_path, unknown) == "afx ctl status"
    assert status._locked_next(tmp_path, "enroll:notify") == "afx ops notify status"


def test_entry_status_routes_to_offline_status(tmp_path, monkeypatch):
    from agentic_fx import entry

    called = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("agentic_fx.status.main", lambda root: called.append(root) or 17)

    assert entry.main(["status"]) == 17
    assert called == [tmp_path]


def test_unknown_instance_does_not_call_the_state_reader(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(status, "probe_instance_lock", lambda _root: "unknown")
    monkeypatch.setattr(status, "read_state", lambda _root: (_ for _ in ()).throw(AssertionError("state")))
    monkeypatch.setattr(status, "default_unit_dir", lambda: tmp_path / "unit")

    rc = status.main(tmp_path, run=lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=""))

    assert rc == 0
    assert "state: mode=unavailable kill_switch=unavailable" in capsys.readouterr().out


def test_source_error_is_unavailable_but_other_status_lines_are_rendered(tmp_path, monkeypatch, capsys):
    lock = tmp_path / "data" / "instance.lock"
    lock.parent.mkdir()
    lock.touch()
    monkeypatch.setattr(status, "default_unit_dir", lambda: tmp_path / "unit")
    monkeypatch.setattr(status, "tail_lines", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("bad log")))

    assert status.main(tmp_path, run=lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="")) == 1
    output = capsys.readouterr().out
    assert "recent activity: unavailable" in output
    assert "setup: missing" in output


def test_main_probes_the_instance_lock_only_once(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(status, "probe_instance_lock", lambda _root: calls.append(1) or "free")
    monkeypatch.setattr(status, "default_unit_dir", lambda: tmp_path / "unit")

    status.main(tmp_path, run=lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=""))

    assert calls == [1]
    assert "instance: free" in capsys.readouterr().out
