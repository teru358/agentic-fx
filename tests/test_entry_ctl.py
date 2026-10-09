"""`afx ctl` の入口: 非端末の引数なしは usage + rc=2、stop は送らない、.env を読まない。"""
from __future__ import annotations

import os
import resource
import subprocess
import sys
from pathlib import Path

import pytest

from agentic_fx.entry import main as entry_main

_CHILD = ("import sys; from agentic_fx.entry import main; "
          "raise SystemExit(main(sys.argv[1:]))")


def _home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "root"
    root.mkdir()
    return root


def test_ctl_without_operation_on_non_terminal_prints_usage_rc2(tmp_path, monkeypatch):
    root = _home(tmp_path, monkeypatch)
    result = subprocess.run([sys.executable, "-c", _CHILD, "ctl"], cwd=root,
                            stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            timeout=60,
                            preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CORE, (0, 0)))
    assert result.returncode == 2
    assert "usage: afx ctl" in result.stderr
    # daemon へ落ちない (サービスを起動しない)
    assert not (root / "data").exists()


def test_ctl_stop_is_not_sent_and_points_to_systemd(tmp_path, monkeypatch, capsys):
    root = _home(tmp_path, monkeypatch)
    monkeypatch.chdir(root)
    assert entry_main(["ctl", "stop"]) == 2
    err = capsys.readouterr().err
    assert "systemctl --user stop" in err and "SIGTERM" in err


def test_ctl_does_not_load_dotenv(tmp_path, monkeypatch, capsys):
    root = _home(tmp_path, monkeypatch)
    (root / ".env").write_text("AFX_CTL_DOTENV_PROBE=leaked\n")
    monkeypatch.chdir(root)
    before = dict(os.environ)
    assert entry_main(["ctl", "status"]) == 3
    assert "AFX_CTL_DOTENV_PROBE" not in os.environ
    assert dict(os.environ) == before


def test_ctl_help_lists_operations(tmp_path, monkeypatch, capsys):
    root = _home(tmp_path, monkeypatch)
    monkeypatch.chdir(root)
    assert entry_main(["ctl", "help"]) == 0
    out = capsys.readouterr().out
    for word in ("approve <id>", "killswitch reconcile", "jobs cancel", "events"):
        assert word in out


def test_ctl_unknown_operation_is_usage_rc2(tmp_path, monkeypatch, capsys):
    root = _home(tmp_path, monkeypatch)
    monkeypatch.chdir(root)
    assert entry_main(["ctl", "mode", "live"]) == 2
    assert "usage: afx ctl" in capsys.readouterr().err


def test_ctl_after_global_flag_never_starts_the_service(tmp_path, monkeypatch, capsys):
    root = _home(tmp_path, monkeypatch)
    monkeypatch.chdir(root)

    def refuse(*_args, **_kwargs):
        raise AssertionError("ctl must not start the service")

    monkeypatch.setattr("agentic_fx.service.run_service", refuse)
    assert entry_main(["--daemon", "ctl", "stop"]) == 2
    assert "systemctl --user stop" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ["--daemon", "ctl", "--as", "approver", "whoami"],
    ["--daemon", "ctl", "--as=approver", "whoami"],
    ["--daemon", "ctl", "status", "--as", "approver"],
])
def test_ctl_after_global_flag_passes_its_options_to_the_client(argv, tmp_path, monkeypatch):
    root = _home(tmp_path, monkeypatch)
    monkeypatch.chdir(root)

    def refuse(*_args, **_kwargs):
        raise AssertionError("ctl must not start the service")

    received = []

    def client_main(args, *, root):
        received.append(list(args))
        return 0

    monkeypatch.setattr("agentic_fx.service.run_service", refuse)
    monkeypatch.setattr("agentic_fx.ops.client.main", client_main)
    assert entry_main(argv) == 0
    assert received == [argv[2:]]


def test_ctl_after_global_flag_reaches_the_client_whoami_path(tmp_path, monkeypatch, capsys):
    root = _home(tmp_path, monkeypatch)
    monkeypatch.chdir(root)

    def refuse(*_args, **_kwargs):
        raise AssertionError("ctl must not start the service")

    monkeypatch.setattr("agentic_fx.service.run_service", refuse)
    # 鍵が無いので approver の鍵を読む段で rc=3 (client が --as approver を読んだ)。
    assert entry_main(["--daemon", "ctl", "--as", "approver", "whoami"]) == 3
    err = capsys.readouterr().err
    assert "afx ctl: 鍵を読めません" in err
    assert not (root / "data").exists()


def test_abbreviated_global_flag_before_ctl_is_usage_rc2(tmp_path, monkeypatch, capsys):
    # 全体 flag の略記は受け付けない (ctl の前置きは完全一致の --daemon だけ)。
    root = _home(tmp_path, monkeypatch)
    monkeypatch.chdir(root)

    def refuse(*_args, **_kwargs):
        raise AssertionError("must not run")

    monkeypatch.setattr("agentic_fx.service.run_service", refuse)
    monkeypatch.setattr("agentic_fx.ops.client.main", refuse)
    with pytest.raises(SystemExit) as caught:
        entry_main(["--dae", "ctl", "status"])
    assert caught.value.code == 2
    assert "usage: afx" in capsys.readouterr().err
