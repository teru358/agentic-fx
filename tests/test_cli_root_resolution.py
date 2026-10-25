"""`afx` resolves one root and hands the same value to every subcommand."""
from __future__ import annotations

from pathlib import Path

import pytest

from agentic_fx import entry, status
from tests.status_support import make_run, manifest

def not_a_directory_option(path):
    return f"afx: --root {path} は directory ではありません\n"


def not_a_directory_env(path):
    return f"afx: AFX_ROOT={path} は directory ではありません\n"


def _make_root(path: Path) -> Path:
    (path / "config").mkdir(parents=True)
    (path / "config" / "settings.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("AFX_ROOT", raising=False)
    monkeypatch.setattr(status, "default_unit_dir", lambda: tmp_path / "unit")
    real_main = status.main
    monkeypatch.setattr("agentic_fx.status.main", lambda root: real_main(root, run=make_run()))


# --- resolve_root: the four routes in order ---

def test_the_root_option_wins_over_the_environment_and_the_search(tmp_path):
    outer = _make_root(tmp_path / "outer")
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    env_root = tmp_path / "env"
    env_root.mkdir()
    found = entry.resolve_root(["--root", str(chosen), "status"], {"AFX_ROOT": str(env_root)}, outer)
    assert found == chosen


def test_the_environment_wins_over_the_upward_search(tmp_path):
    outer = _make_root(tmp_path / "outer")
    env_root = tmp_path / "env"
    env_root.mkdir()
    assert entry.resolve_root(["status"], {"AFX_ROOT": str(env_root)}, outer) == env_root


def test_the_upward_search_takes_the_nearest_directory_with_settings(tmp_path):
    outer = _make_root(tmp_path / "outer")
    inner = _make_root(outer / "inner")
    deep = inner / "a" / "b"
    deep.mkdir(parents=True)
    assert entry.resolve_root(["status"], {}, deep) == inner
    sibling = outer / "other"
    sibling.mkdir()
    assert entry.resolve_root(["status"], {}, sibling) == outer


def test_without_any_settings_the_root_is_the_working_directory(tmp_path):
    work = tmp_path / "work" / "sub"
    work.mkdir(parents=True)
    assert entry.resolve_root(["init"], {}, work) == work


def test_the_search_walks_lexical_parents_and_accepts_symlinked_markers(tmp_path):
    real = _make_root(tmp_path / "real")
    work = tmp_path / "work"
    (work / "child").mkdir(parents=True)
    (work / "config").symlink_to(real / "config", target_is_directory=True)
    assert entry.resolve_root(["init"], {}, work / "child") == work

    other = tmp_path / "other"
    (other / "config").mkdir(parents=True)
    (other / "config" / "settings.yaml").symlink_to(real / "config" / "settings.yaml")
    (other / "child").mkdir()
    assert entry.resolve_root(["init"], {}, other / "child") == other

    dangling = tmp_path / "dangling"
    (dangling / "config").mkdir(parents=True)
    (dangling / "config" / "settings.yaml").symlink_to(tmp_path / "nowhere")
    (dangling / "child").mkdir()
    assert entry.resolve_root(["init"], {}, dangling / "child") == dangling / "child"


def test_an_empty_environment_value_is_treated_as_unset(tmp_path):
    outer = _make_root(tmp_path / "outer")
    assert entry.resolve_root(["status"], {"AFX_ROOT": ""}, outer) == outer


def test_a_relative_root_is_resolved_against_the_working_directory(tmp_path):
    work = tmp_path / "work"
    (work / "target").mkdir(parents=True)
    assert entry.resolve_root(["--root", "target", "status"], {}, work) == work / "target"
    assert entry.resolve_root(["--root=target", "status"], {}, work) == work / "target"


@pytest.mark.parametrize("argv", [["--root", "X", "status"], ["--daemon", "--root", "X", "status"],
                                  ["--root", "X", "--daemon", "status"]])
def test_the_root_option_is_read_in_either_order_with_the_daemon_flag(tmp_path, argv):
    (tmp_path / "X").mkdir()
    argv = [str(tmp_path / "X") if token == "X" else token for token in argv]
    assert entry.resolve_root(argv, {}, tmp_path) == tmp_path / "X"


def test_a_root_option_after_the_subcommand_is_not_a_prefix(tmp_path):
    (tmp_path / "X").mkdir()
    assert entry._split_prefix(["status", "--root", str(tmp_path / "X")]) == (0, None)


# --- invalid specifications stop with rc 2, fixed text and no writes ---

@pytest.mark.parametrize("kind", ["missing", "file"])
def test_an_invalid_root_option_stops_with_a_fixed_sentence_and_writes_nothing(
        tmp_path, monkeypatch, capsys, kind):
    work = _make_root(tmp_path / "work")
    bad = work / "nothing" if kind == "missing" else work / "config" / "settings.yaml"
    monkeypatch.chdir(work)
    before = manifest(tmp_path)
    assert entry.main(["--root", str(bad), "status"]) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == not_a_directory_option(bad)
    assert manifest(tmp_path) == before


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_an_invalid_environment_root_stops_with_a_fixed_sentence_and_writes_nothing(
        tmp_path, monkeypatch, capsys, kind):
    work = _make_root(tmp_path / "work")
    bad = work / "nothing" if kind == "missing" else work / "config" / "settings.yaml"
    monkeypatch.chdir(work)
    monkeypatch.setenv("AFX_ROOT", str(bad))
    before = manifest(tmp_path)
    assert entry.main(["status"]) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == not_a_directory_env(bad)
    assert manifest(tmp_path) == before


def test_an_invalid_root_also_stops_the_ctl_route_before_the_client_runs(tmp_path, monkeypatch, capsys):
    called = []
    monkeypatch.setattr("agentic_fx.ops.client.main", lambda *a, **k: called.append(1) or 0)
    monkeypatch.chdir(tmp_path)
    assert entry.main(["--root", str(tmp_path / "nothing"), "ctl", "status"]) == 2
    assert called == []
    assert capsys.readouterr().err == not_a_directory_option(tmp_path / "nothing")


# --- every subcommand receives the resolved root ---

def test_status_prints_the_resolved_root_right_after_the_title(tmp_path, monkeypatch, capsys):
    root = _make_root(tmp_path / "root")
    sub = root / "deep" / "er"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    before = manifest(tmp_path)
    assert entry.main(["status"]) in (0, 1)
    lines = capsys.readouterr().out.splitlines()
    assert lines[:2] == ["agentic-fx offline status", f"root: {root}"]
    assert lines[2].startswith("instance: ")
    assert manifest(tmp_path) == before


def test_status_with_the_root_option_prints_that_root(tmp_path, monkeypatch, capsys):
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.chdir(tmp_path)
    assert entry.main(["--root", "target", "status"]) in (0, 1)
    assert capsys.readouterr().out.splitlines()[1] == f"root: {target}"


def test_only_status_prints_the_root(tmp_path, monkeypatch, capsys):
    called = []
    monkeypatch.setattr("agentic_fx.setup.preflight.main", lambda root, **k: called.append(root) or 0)
    monkeypatch.chdir(tmp_path)
    assert entry.main(["check"]) == 0
    assert "root:" not in capsys.readouterr().out
    assert called == [tmp_path]


def test_check_and_ctl_from_a_subdirectory_use_the_parent_root(tmp_path, monkeypatch):
    root = _make_root(tmp_path / "root")
    sub = root / "x"
    sub.mkdir()
    seen = {}
    monkeypatch.setattr("agentic_fx.setup.preflight.main", lambda root, **k: seen.setdefault("check", root) and 0)
    monkeypatch.setattr("agentic_fx.ops.client.main",
                        lambda argv, *, root, **k: seen.update(ctl=(list(argv), root)) or 0)
    monkeypatch.chdir(sub)
    assert entry.main(["check"]) == 0
    assert entry.main(["ctl", "status"]) == 0
    assert seen == {"check": root, "ctl": (["status"], root)}


@pytest.mark.parametrize("prefix", [["--daemon", "--root", "{r}"], ["--root", "{r}", "--daemon"],
                                    ["--root", "{r}"], ["--daemon", "--daemon", "--root={r}"]])
def test_the_ctl_route_gets_the_same_head_and_root_for_every_prefix_order(tmp_path, monkeypatch, prefix):
    target = tmp_path / "target"
    target.mkdir()
    seen = []
    monkeypatch.setattr("agentic_fx.ops.client.main",
                        lambda argv, *, root, **k: seen.append((list(argv), root)) or 0)
    monkeypatch.chdir(tmp_path)
    argv = [token.format(r=target) for token in prefix] + ["ctl", "--as", "approver", "status"]
    assert entry.main(argv) == 0
    assert seen == [(["--as", "approver", "status"], target)]


@pytest.mark.parametrize("prefix", [["--daemon", "--root", "R"], ["--root", "R", "--daemon"]])
def test_the_compatibility_shim_counts_the_root_option_as_a_prefix(prefix, capsys):
    head, _ = entry._split_prefix([*prefix, "ops", "enable", "web"])
    assert head == 3
    normalized, rc = entry._shim_argv([*prefix, "ops", "enable", "web"], head)
    assert rc is None
    assert normalized == [*prefix, "ops", "web", "enable"]
    capsys.readouterr()
    assert entry._shim_argv([*prefix, "improve"], head)[1] == 2


def test_the_interactive_ops_probe_uses_the_given_root(tmp_path, monkeypatch):
    from agentic_fx.ops_setup import interactive

    seen = {}

    class Paths:
        ops_exec = Path("/nonexistent/ops-exec")

    class FakeSetup:
        def __init__(self, root):
            seen["root"] = root
            self.paths = Paths()

    monkeypatch.setattr(interactive, "OpsSetup", FakeSetup)
    monkeypatch.setattr(interactive, "resolve_ops_exec", lambda paths: paths)
    monkeypatch.setattr(interactive, "run_probe", lambda path, feature: (0, ""))
    monkeypatch.chdir(tmp_path)
    assert interactive._default_probe("web", tmp_path / "elsewhere") == (0, "")
    assert seen["root"] == tmp_path / "elsewhere"
