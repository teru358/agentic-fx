"""起動時の enroll marker 契約: marker があれば manifest を読む前に固定 reason と専用終了コードで止まる。"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from agentic_fx.ops.keys import initialize, key_dir
from agentic_fx.service import build_app, run_init, run_service
from agentic_fx.store.enroll_marker import (
    EXIT_ENROLL_IN_PROGRESS, EnrollInProgress, enroll_marker_path,
)
from agentic_fx.store.instance_lock import InstanceAlreadyRunning, acquire_instance_lock

_REPO = Path(__file__).resolve().parents[1]


def _init(root: Path) -> None:
    (root / "config").mkdir(parents=True)
    example = (_REPO / "config" / "settings.yaml.example").read_text(encoding="utf-8")
    (root / "config" / "settings.yaml.example").write_text(example)
    with patch("agentic_fx.service.PriceProvider") as provider, \
         patch("agentic_fx.service._check_llama_swap"):
        provider.return_value.healthcheck.return_value = "yfinance"
        assert run_init(root) == 0
    # 判定が抜けて daemon が起動しても実 llama-swap / bridge に届かないよう、空き loopback port へ向ける。
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    settings = root / "config" / "settings.yaml"
    text = settings.read_text(encoding="utf-8")
    for real in ("http://localhost:8080", "http://localhost:8812"):
        assert real in text
        text = text.replace(real, f"http://127.0.0.1:{port}")
    settings.write_text(text, encoding="utf-8")


def _place_marker(root: Path) -> Path:
    path = enroll_marker_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"feature": "bot", "started_at": "2026-10-10T00:00:00Z"}))
    return path


def _sealed_key_dir(root: Path, home: Path, monkeypatch) -> Path:
    # 鍵 dir を誰も読めない権限にする。起動経路が manifest に触れれば例外になる。
    monkeypatch.setenv("HOME", str(home))
    directory = key_dir(root)
    initialize(directory, root=root)
    directory.chmod(0o000)
    return directory


def test_exit_code_is_distinct_from_existing_service_codes():
    # 既存の終了コードは 0 / 1 / 2 (未初期化)。systemd の RestartPreventExitStatus で区別できる値にする。
    assert EXIT_ENROLL_IN_PROGRESS == 75
    assert EXIT_ENROLL_IN_PROGRESS not in {0, 1, 2}


def test_marker_path_sits_next_to_ops_stage_files(tmp_path):
    assert enroll_marker_path(tmp_path) == tmp_path / "data" / "ops" / "enroll_in_progress"


def test_build_app_with_marker_refuses_before_touching_lock_or_keys(tmp_path, monkeypatch):
    root = tmp_path / "instance"
    _init(root)
    directory = _sealed_key_dir(root, tmp_path / "home", monkeypatch)
    _place_marker(root)
    try:
        with patch("agentic_fx.service.acquire_instance_lock") as lock:
            with pytest.raises(EnrollInProgress):
                build_app(root)
        lock.assert_not_called()
    finally:
        directory.chmod(0o700)


def test_build_app_with_marker_and_held_flock_refuses_as_enroll_in_progress(tmp_path, monkeypatch):
    root = tmp_path / "instance"
    _init(root)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _place_marker(root)
    held = acquire_instance_lock(root / "data")
    try:
        with pytest.raises(EnrollInProgress):
            build_app(root)
    finally:
        held.close()


def test_marker_placed_while_flock_is_held_turns_lock_failure_into_enroll_in_progress(tmp_path, monkeypatch):
    # marker の確認と flock 取得の間に enable が marker を置いて flock を握った場合。
    root = tmp_path / "instance"
    _init(root)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    held = acquire_instance_lock(root / "data")
    import agentic_fx.service as service_module
    real = service_module.acquire_instance_lock

    def place_then_try(data_dir):
        _place_marker(root)
        return real(data_dir)

    try:
        with patch("agentic_fx.service.acquire_instance_lock", side_effect=place_then_try):
            with pytest.raises(EnrollInProgress):
                build_app(root)
    finally:
        held.close()


def test_marker_placed_right_after_lock_is_acquired_releases_lock_and_refuses(tmp_path, monkeypatch):
    root = tmp_path / "instance"
    _init(root)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    import agentic_fx.service as service_module
    real = service_module.acquire_instance_lock

    def lock_then_place(data_dir):
        handle = real(data_dir)
        _place_marker(root)
        return handle

    with patch("agentic_fx.service.acquire_instance_lock", side_effect=lock_then_place):
        with pytest.raises(EnrollInProgress):
            build_app(root)
    # 拒否した側は flock を手放している (enable 側が取得できる)。
    acquire_instance_lock(root / "data").close()


def test_without_marker_held_flock_keeps_the_existing_failure(tmp_path, monkeypatch):
    root = tmp_path / "instance"
    _init(root)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    held = acquire_instance_lock(root / "data")
    try:
        with pytest.raises(InstanceAlreadyRunning):
            build_app(root)
    finally:
        held.close()


def test_run_service_with_marker_returns_75_and_writes_one_reason_line(tmp_path, monkeypatch, capsys):
    root = tmp_path / "instance"
    _init(root)
    directory = _sealed_key_dir(root, tmp_path / "home", monkeypatch)
    _place_marker(root)
    capsys.readouterr()
    try:
        # 判定が抜けた場合に daemon が待ち続けないよう、停止済みの event を渡す。
        stopped = threading.Event()
        stopped.set()
        assert run_service(root, daemon=True, _stop_event=stopped) == EXIT_ENROLL_IN_PROGRESS
    finally:
        directory.chmod(0o700)
    err = capsys.readouterr().err
    assert err.splitlines() == ["afx: enroll_in_progress"]


def test_real_afx_process_with_marker_exits_75_with_one_stderr_line(tmp_path):
    root = tmp_path / "instance"
    _init(root)
    home = tmp_path / "home"
    home.mkdir()
    with patch.dict(os.environ, {"HOME": str(home)}):
        directory = key_dir(root)
        initialize(directory, root=root)
    directory.chmod(0o000)
    _place_marker(root)
    try:
        child = subprocess.run(
            [sys.executable, "-c",
             "import resource, sys; resource.setrlimit(resource.RLIMIT_CORE, (0, 0));"
             "from agentic_fx.entry import main; sys.exit(main(['--daemon']))"],
            cwd=root, capture_output=True, text=True, timeout=60,
            env={**os.environ, "HOME": str(home)})
    finally:
        directory.chmod(0o700)
    assert child.returncode == 75, child.stderr
    assert child.stderr.splitlines() == ["afx: enroll_in_progress"]


def test_refusal_after_lock_releases_lock_while_the_error_is_still_held(tmp_path, monkeypatch):
    root = tmp_path / "instance"
    _init(root)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    import agentic_fx.service as service_module
    real = service_module.acquire_instance_lock

    def lock_then_place(data_dir):
        handle = real(data_dir)
        _place_marker(root)
        return handle

    with patch("agentic_fx.service.acquire_instance_lock", side_effect=lock_then_place):
        with pytest.raises(EnrollInProgress) as caught:
            build_app(root)
    # 例外 (traceback が frame を掴む) が生きている間も flock は閉じられている。
    assert caught.value is not None
    acquire_instance_lock(root / "data").close()


@pytest.mark.parametrize("kind", ["empty", "garbage", "dangling_symlink", "directory"])
def test_marker_in_any_form_refuses_start(tmp_path, monkeypatch, kind):
    from agentic_fx.store.enroll_marker import enroll_marker_present

    root = tmp_path / "instance"
    path = enroll_marker_path(root)
    path.parent.mkdir(parents=True)
    if kind == "empty":
        path.write_bytes(b"")
    elif kind == "garbage":
        path.write_bytes(b"\x00not json")
    elif kind == "dangling_symlink":
        path.symlink_to(tmp_path / "missing")
    else:
        path.mkdir()

    assert enroll_marker_present(root)
