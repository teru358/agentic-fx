"""``afx plugin reevaluate [--all-deployed | <name>]``: one result row per version, an exit
code that says whether everything was filed, and one shared evaluation time.

The shared gate is replaced by ``FakeGate`` (see ``tests/plugin/reevaluation_world.py``); the
rest is the real command: argument parsing, the confirmation, the locks, the database.
"""
from __future__ import annotations

import argparse

import pytest

from agentic_fx.backtest import cli as backtest_cli
from agentic_fx.backtest import plugin_deploy_cli
from agentic_fx.entry import main
from agentic_fx.plugin import switch
from tests.backtest.test_plugin_deploy_cli import CliWorld
from tests.plugin.reevaluation_world import FakeGate


@pytest.fixture
def world(tmp_path, monkeypatch):
    built = CliWorld(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(plugin_deploy_cli, "_isatty", lambda: False)
    for name, minute in (("alpha", 2), ("sma", 3)):
        version = built.version(name, "a", kind="strategy")
        built.deploy(name, version, None, minute)
    indicator = built.version("ind", "i", kind="indicator")
    built.deploy("ind", indicator, None, 1)
    yield built
    built.conn.close()


def _gate(monkeypatch, gate=None) -> FakeGate:
    gate = gate or FakeGate()
    monkeypatch.setattr(switch, "_evaluate_deployed", gate)
    return gate


def _run(capsys, *argv):
    rc = main(["plugin", "reevaluate", *argv])
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _pending(world) -> list[tuple]:
    return [tuple(r) for r in world.conn.execute(
        "SELECT id, json_extract(payload_json,'$.name') FROM approval_requests "
        "WHERE status='pending' AND json_extract(payload_json,'$.candidate_origin')="
        "'reevaluation' ORDER BY id")]


def test_all_deployed_prints_one_row_per_version_in_name_order_and_exits_zero(
        world, monkeypatch, capsys):
    gate = _gate(monkeypatch)

    rc, out, err = _run(capsys, "--all-deployed", "--yes")

    lines = out.strip().splitlines()
    assert rc == 0, (out, err)
    assert [line.split("  ")[0] for line in lines] == ["alpha", "ind", "sma"]
    assert lines[0].startswith("alpha  created  #")
    assert lines[1].startswith("ind  skipped  (not_strategy)")
    assert lines[2].startswith("sma  created  #")
    assert [call["name"] for call in gate.calls] == ["alpha", "sma"]
    assert [name for _id, name in _pending(world)] == ["alpha", "sma"]


def test_every_strategy_is_evaluated_with_the_same_time(world, monkeypatch, capsys):
    gate = _gate(monkeypatch)

    _run(capsys, "--all-deployed", "--yes")

    nows = {call["now"] for call in gate.calls}
    assert len(nows) == 1 and next(iter(nows)).tzinfo is not None


def test_a_second_run_evaluates_nothing_and_still_exits_zero(world, monkeypatch, capsys):
    _gate(monkeypatch)
    _run(capsys, "--all-deployed", "--yes")
    gate = _gate(monkeypatch)

    rc, out, _err = _run(capsys, "--all-deployed", "--yes")

    assert rc == 0
    assert [line.split("  ")[:2] for line in out.strip().splitlines()] == [
        ["alpha", "existing"], ["ind", "skipped"], ["sma", "existing"]]
    assert gate.calls == []
    assert len(_pending(world)) == 2


def test_one_failure_does_not_stop_the_others_and_makes_the_exit_non_zero(
        world, monkeypatch, capsys):
    class FailsForAlpha(FakeGate):
        def __call__(self, conn, snapshot, **kwargs):
            if snapshot.meta.name == "alpha":
                raise RuntimeError("worker died")
            return super().__call__(conn, snapshot, **kwargs)

    _gate(monkeypatch, FailsForAlpha())

    rc, out, _err = _run(capsys, "--all-deployed", "--yes")

    assert rc == 1
    assert [line.split("  ")[:2] for line in out.strip().splitlines()] == [
        ["alpha", "failed"], ["ind", "skipped"], ["sma", "created"]]
    assert [name for _id, name in _pending(world)] == ["sma"]


def test_an_unevaluable_row_gives_the_reason_and_the_command_to_stop_it(
        world, monkeypatch, capsys):
    _gate(monkeypatch, FakeGate(evaluable=False, reason="insufficient_trades:3"))

    rc, out, _err = _run(capsys, "sma", "--yes")

    assert rc == 1
    (line,) = out.strip().splitlines()
    assert line.startswith("sma  unevaluable  (insufficient_trades:3)")
    assert line.endswith("afx plugin disable sma")
    assert _pending(world) == []


def test_a_named_indicator_is_a_failure_because_the_request_could_not_be_filed(
        world, monkeypatch, capsys):
    gate = _gate(monkeypatch)

    rc, out, _err = _run(capsys, "ind", "--yes")

    assert rc == 1
    assert out.strip().startswith("ind  skipped  (not_strategy)")
    assert gate.calls == []


def test_a_change_during_the_evaluation_is_reported_and_nothing_is_filed(
        world, monkeypatch, capsys):
    def edit_the_version():
        plugin = world.root / ".versions" / "sma"
        target = next(plugin.iterdir()) / "plugin.py"
        target.write_text(target.read_text() + "# edited\n")

    _gate(monkeypatch, FakeGate(during=edit_the_version))

    rc, out, _err = _run(capsys, "sma", "--yes")

    assert rc == 1
    assert out.strip().startswith("sma  changed_during_evaluation")
    assert _pending(world) == []


def test_without_yes_a_non_terminal_run_asks_for_confirmation_and_changes_nothing(
        world, monkeypatch, capsys):
    gate = _gate(monkeypatch)

    rc, out, err = _run(capsys, "--all-deployed")

    assert rc == 1
    assert "confirmation_required" in err
    assert "対象: alpha, ind, sma" in out
    assert gate.calls == [] and _pending(world) == []


@pytest.mark.parametrize(("answer", "evaluated"), [("y", 2), ("n", 0)])
def test_on_a_terminal_the_answer_decides(world, monkeypatch, capsys, answer, evaluated):
    gate = _gate(monkeypatch)
    monkeypatch.setattr(plugin_deploy_cli, "_isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt="": answer)

    rc, out, err = _run(capsys, "--all-deployed")

    assert len(gate.calls) == evaluated
    assert rc == (0 if evaluated else 1)
    if not evaluated:
        assert "aborted" in err


@pytest.mark.parametrize("argv", [[], ["sma", "--all-deployed"]])
def test_exactly_one_of_a_name_and_all_deployed_is_required(world, monkeypatch, capsys, argv):
    gate = _gate(monkeypatch)

    rc, _out, err = _run(capsys, *argv, "--yes")

    assert rc == 2
    assert "invalid_request" in err
    assert gate.calls == []


def test_the_parser_takes_a_name_or_the_all_deployed_flag():
    root = argparse.ArgumentParser()
    backtest_cli.register_subparsers(root.add_subparsers(dest="command"))

    named = root.parse_args(["plugin", "reevaluate", "sma"])
    every = root.parse_args(["plugin", "reevaluate", "--all-deployed", "--yes"])

    assert (named.plugin_command, named.name, named.all_deployed, named.yes) == (
        "reevaluate", "sma", False, False)
    assert (every.name, every.all_deployed, every.yes) == (None, True, True)
