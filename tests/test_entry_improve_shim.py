"""`afx improve …` は 1 リリースだけ残る廃止名: 案内を 1 回出して rc 2、何にも触れない。"""
from __future__ import annotations

import importlib.metadata

import pytest

from agentic_fx.entry import main
from tests.fixtures.fs_tree import tree_snapshot

MESSAGE = ("afx improve verify-backend は廃止されました。afx check backend --backend <name> "
           "[--provider chatgpt] を使ってください。")

IMPROVE_SHAPES = [
    ["improve"],
    ["improve", "--help"],
    ["improve", "verify-backend"],
    ["improve", "verify-backend", "--backend", "local"],
    ["improve", "verify-backend", "--backend", "codex", "--provider", "chatgpt"],
    ["improve", "bogus", "--x"],
]


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text("schema_version: 1\n")
    return tmp_path


def _forbid_everything(monkeypatch):
    touched: list[str] = []

    def spy(name):
        def call(*_args, **_kwargs):
            touched.append(name)
            raise AssertionError(name)
        return call

    monkeypatch.setattr("agentic_fx.loops.verify_backend.verify_backend", spy("backend"))
    monkeypatch.setattr("agentic_fx.backtest.cli.verify_backend_command", spy("check_backend"))
    monkeypatch.setattr("agentic_fx.backtest.cli.dispatch", spy("dispatch"))
    monkeypatch.setattr("agentic_fx.service.run_service", spy("service"))
    monkeypatch.setattr("agentic_fx.service.run_init", spy("init"))
    monkeypatch.setattr("agentic_fx.store.db.connect", spy("db"))
    return touched


@pytest.mark.parametrize("daemon", [0, 1, 2])
@pytest.mark.parametrize("argv", IMPROVE_SHAPES, ids=lambda argv: " ".join(argv))
def test_every_improve_shape_prints_the_notice_once_and_touches_nothing(
        root, monkeypatch, capsys, argv, daemon):
    touched = _forbid_everything(monkeypatch)
    before = tree_snapshot(root)

    assert main(["--daemon"] * daemon + argv) == 2

    captured = capsys.readouterr()
    assert captured.err == MESSAGE + "\n"
    assert captured.out == ""
    assert touched == []
    assert tree_snapshot(root) == before
    assert not (root / "data").exists()


def test_the_improve_notice_ends_with_the_removal_release(monkeypatch, root, capsys):
    monkeypatch.setattr("agentic_fx.entry.importlib.metadata.version", lambda _name: "0.2.9")
    assert main(["improve"]) == 2
    assert capsys.readouterr().err == MESSAGE + "\n"


@pytest.mark.parametrize("argv", [["improve"], ["improve", "verify-backend", "--backend", "local"]])
def test_after_the_removal_release_improve_is_a_plain_argparse_error(root, monkeypatch, capsys, argv):
    monkeypatch.setattr("agentic_fx.entry.importlib.metadata.version", lambda _name: "0.3.0")
    touched = _forbid_everything(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    assert touched == []
    err = capsys.readouterr().err
    assert "廃止されました" not in err and "invalid choice: 'improve'" in err


def test_an_uninstalled_package_cannot_prove_expiry_so_the_notice_stays(root, monkeypatch, capsys):
    def missing(_name):
        raise importlib.metadata.PackageNotFoundError("agentic-fx")

    monkeypatch.setattr("agentic_fx.entry.importlib.metadata.version", missing)
    touched = _forbid_everything(monkeypatch)
    assert main(["improve", "verify-backend", "--backend", "local"]) == 2
    assert capsys.readouterr().err == MESSAGE + "\n"
    assert touched == []


def test_an_uninstalled_package_does_not_break_commands_unrelated_to_improve(root, monkeypatch, capsys):
    def missing(_name):
        raise importlib.metadata.PackageNotFoundError("agentic-fx")

    monkeypatch.setattr("agentic_fx.entry.importlib.metadata.version", missing)
    with pytest.raises(SystemExit) as exc:
        main(["check", "backend"])
    assert exc.value.code == 2
    assert "--backend" in capsys.readouterr().err
