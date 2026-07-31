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
    assert outputs == ["OK"]


def test_empty_line_ignored():
    cmds, _, _ = _run(["", "  ", "stop"])
    cmds.dispatch.assert_not_called()


def test_dispatch_keyboard_interrupt_sets_event():
    """dispatch 実行中の Ctrl-C は stop_event.set() して例外を吸収"""
    cmds = MagicMock()
    cmds.dispatch.side_effect = KeyboardInterrupt()
    stop = threading.Event()
    outputs = []

    def fake_input(prompt=""):
        return "ask something"

    run_shell(cmds, stop, input_fn=fake_input, print_fn=outputs.append)
    assert stop.is_set()
    cmds.dispatch.assert_called_once_with("ask something")


def test_external_stop_event_prevents_dispatch():
    """input_fn から返った後、外部で stop_event.set() されていたら dispatch しない"""
    cmds = MagicMock()
    cmds.dispatch.return_value = "OK"
    stop = threading.Event()
    outputs = []
    call_count = [0]

    def fake_input(prompt=""):
        if call_count[0] == 0:
            call_count[0] += 1
            return "first"
        # 2 番目の input で stop_event.set() (外部割り込み想定)
        stop.set()
        raise EOFError

    run_shell(cmds, stop, input_fn=fake_input, print_fn=outputs.append)
    assert stop.is_set()
    # dispatch は 1 回だけ呼ばれるべき (2 番目の input の直後に stop が効く)
    cmds.dispatch.assert_called_once_with("first")
