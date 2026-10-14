from __future__ import annotations

import pytest

from agentic_fx import entry, service
from agentic_fx.setup import wizard

from tests.setup.wizard_support import make_root, snapshot


def test_init_without_the_flag_runs_the_wizard_on_the_working_directory(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    seen = []
    monkeypatch.setattr(wizard, "run_wizard", lambda path, io, *args, **kwargs: seen.append((path, io)) or 7)
    assert entry.main(["init"]) == 7
    assert seen[0][0].resolve() == root.resolve()
    assert isinstance(seen[0][1], wizard.ConsoleWizardIO)


def test_init_without_the_flag_and_without_a_terminal_writes_nothing(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path)
    before = snapshot(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(wizard, "run_wizard", lambda *a, **k: pytest.fail("must not start"))
    assert entry.main(["init"]) == 2
    err = capsys.readouterr().err
    assert wizard.NON_TTY_MESSAGE in err and "--non-interactive" in err
    assert snapshot(root) == before


def test_config_is_only_accepted_together_with_the_non_interactive_flag(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path)
    before = snapshot(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(wizard, "run_wizard", lambda *a, **k: pytest.fail("must not start"))
    assert entry.main(["init", "--config", str(tmp_path / "x.yaml")]) == 2
    assert "--non-interactive" in capsys.readouterr().err
    assert snapshot(root) == before


def test_the_non_interactive_branch_still_reaches_the_plain_init(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    monkeypatch.chdir(root)
    seen = []
    monkeypatch.setattr(service, "run_init", lambda path, config_file=None: seen.append((path, config_file)) or 0)
    monkeypatch.setattr(wizard, "run_wizard", lambda *a, **k: pytest.fail("must not start"))
    assert entry.main(["init", "--non-interactive"]) == 0
    assert [config for _, config in seen] == [None]
    assert seen[0][0].resolve() == root.resolve()


def test_the_help_describes_the_interactive_default(capsys):
    with pytest.raises(SystemExit):
        entry.main(["--help"])
    assert "初期設定 (対話。`--non-interactive` で従来の非対話)" in capsys.readouterr().out
