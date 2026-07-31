import threading
from unittest.mock import MagicMock

from agentic_fx.shell import run_shell


def _run(lines):
    cmds = MagicMock()
    cmds.dispatch.return_value = "OK"
    stop = threading.Event()
    it = iter(lines)
    outputs = []

    def fake_input(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError

    run_shell(cmds, stop, input_fn=fake_input, print_fn=outputs.append)
    return cmds, stop, outputs


def test_stop_sets_event():
    cmds, stop, _ = _run(["stop"])
    assert stop.is_set()
    cmds.dispatch.assert_not_called()


def test_eof_sets_event():
    _, stop, _ = _run([])
    assert stop.is_set()


def test_commands_dispatched_and_printed():
    cmds, _, outputs = _run(["status", "stop"])
    cmds.dispatch.assert_called_once_with("status")
    assert "OK" in outputs


def test_empty_line_ignored():
    cmds, _, _ = _run(["", "  ", "stop"])
    cmds.dispatch.assert_not_called()
