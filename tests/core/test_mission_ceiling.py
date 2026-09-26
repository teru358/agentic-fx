"""判断 mission の上限 Cw (worker 1 回) / Cd (dispatch 全体) の算出。"""
import inspect
from pathlib import Path

from agentic_fx.config import RunnerChoice, load_settings
from agentic_fx.core import mission_ceiling
from agentic_fx.loops.reflection_cycle import ReflectionCycle

EXAMPLE = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


def _with(settings, *, backend=None, dispatch_ceiling_sec=None):
    s = settings
    if backend is not None:
        s = s.model_copy(update={"runner": s.runner.model_copy(update={
            "trade": RunnerChoice(backend=backend, model="m")})})
    if dispatch_ceiling_sec is not None:
        s = s.model_copy(update={"worker": s.worker.model_copy(update={
            "dispatch_ceiling_sec": dispatch_ceiling_sec})})
    return s


def test_worker_ceiling_breakdown_matches_example_defaults():
    cw = mission_ceiling.worker_ceiling(EXAMPLE)
    assert (cw.startup, cw.deadline, cw.terminate, cw.kill_wait,
            cw.kill_wait_retry, cw.reader_join, cw.dispatcher_join,
            cw.cli_terminate) == (30.0, 330.0, 10.0, 5.0, 5.0, 5.0, 20.0, 0.0)
    assert cw.total == 405.0


def test_worker_ceiling_adds_cli_terminate_grace_only_for_claude():
    cw = mission_ceiling.worker_ceiling(_with(EXAMPLE, backend="claude"))
    assert cw.cli_terminate == 10.0
    assert cw.total == 415.0


def test_dispatch_and_watchdog_ceiling_derive_from_worker_ceiling():
    assert mission_ceiling.dispatch_ceiling_sec(EXAMPLE) == 1630.0
    assert mission_ceiling.watchdog_ceiling_sec(EXAMPLE) == 1690.0
    claude = _with(EXAMPLE, backend="claude")
    assert mission_ceiling.dispatch_ceiling_sec(claude) == 1670.0
    assert mission_ceiling.watchdog_ceiling_sec(claude) == 1730.0


def test_watchdog_ceiling_prefers_explicit_setting():
    s = _with(EXAMPLE, dispatch_ceiling_sec=2000.0)
    assert mission_ceiling.watchdog_ceiling_sec(s) == 2000.0
    assert mission_ceiling.dispatch_ceiling_sec(s) == 1630.0


def test_reflection_batch_size_matches_ceiling_constant():
    default = inspect.signature(ReflectionCycle.run_pending).parameters[
        "max_items"].default
    assert default == mission_ceiling.REFLECTIONS_PER_DISPATCH == 3


def test_worker_ceiling_breakdown_text_lists_every_term():
    assert mission_ceiling.worker_ceiling(
        _with(EXAMPLE, backend="claude")).breakdown_text() == (
        "起動待ち 30 + 実行 deadline 330 + SIGTERM 猶予 10"
        " + SIGKILL 後 wait 5 + wait 再実行 5 + reader.join 5"
        " + dispatcher.join 20 + CLI 回収 10")
