"""起動時検査: Cw >= 判断足幅は拒否、Cd >= 判断足幅は WARNING 1 回。"""
from pathlib import Path
from unittest.mock import patch

import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.config import RunnerChoice, load_settings
from agentic_fx.core.contracts import FixedClock
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.service import _check_mission_ceilings, build_app
from tests.test_service_app import NOW, _init

EXAMPLE = load_settings(
    Path(__file__).resolve().parents[1] / "config" / "settings.yaml.example")


def _with(settings, *, decision=None, backend=None, dispatch_ceiling_sec=None,
         llama_swap_timeout_sec=None):
    s = settings
    if decision is not None:
        s = s.model_copy(update={"datafeed": s.datafeed.model_copy(update={
            "decision_timeframes": [decision], "primary_intervals": [decision]})})
    if backend is not None:
        s = s.model_copy(update={"runner": s.runner.model_copy(update={
            "trade": RunnerChoice(backend=backend, model="m")})})
    if dispatch_ceiling_sec is not None:
        s = s.model_copy(update={"worker": s.worker.model_copy(update={
            "dispatch_ceiling_sec": dispatch_ceiling_sec})})
    if llama_swap_timeout_sec is not None:
        s = s.model_copy(update={"llama_swap": s.llama_swap.model_copy(update={
            "timeout_sec": llama_swap_timeout_sec})})
    return s


def _events(path):
    if not path.exists():
        return []
    return [line.split("\t") for line in
            path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize(("backend", "cw"), [("local", 405), ("claude", 415)])
def test_startup_rejects_decision_timeframe_not_longer_than_worker_ceiling(
        tmp_path, backend, cw):
    activity = ActivityLog(tmp_path / "a.log")
    with pytest.raises(RuntimeError) as exc:
        _check_mission_ceilings(_with(EXAMPLE, decision="5m", backend=backend),
                                activity)
    msg = str(exc.value)
    assert "判断足 5m (300 秒)" in msg
    assert f"Cw {cw} 秒" in msg
    assert "1 確定足に 1 回の判断が成立しません" in msg
    assert "llama_swap.timeout_sec" in msg


def test_startup_rejects_when_deadline_alone_is_below_width_but_total_is_not(
        tmp_path):
    """Important 1 (Ta1-M4): `cw.total >= width_sec` を `cw.deadline >=
    width_sec` に潰す変異は、既定値のままでは (deadline も total もどちらも
    足幅以上のため) 生存する。`llama_swap.timeout_sec=250` にすると
    `deadline` (250+worker_grace_sec 30=280) は判断足 5m の幅 (300 秒)
    未満になるが、`total` (355 秒、起動待ち・SIGTERM 猶予・SIGKILL 後 wait
    ×2・reader/dispatcher の join を含む) は幅以上のまま — 変異後は
    `deadline < width_sec` で起動を許してしまうので拒否されなくなる。"""
    activity = ActivityLog(tmp_path / "a.log")
    settings = _with(EXAMPLE, decision="5m", backend="local",
                     llama_swap_timeout_sec=250)
    with pytest.raises(RuntimeError) as exc:
        _check_mission_ceilings(settings, activity)
    msg = str(exc.value)
    assert "判断足 5m (300 秒)" in msg
    assert "Cw 355 秒" in msg
    assert "1 確定足に 1 回の判断が成立しません" in msg


@pytest.mark.parametrize(("backend", "cd"), [("local", 1630), ("claude", 1670)])
def test_startup_allows_15m_and_writes_one_dispatch_warning(tmp_path, backend, cd):
    path = tmp_path / "a.log"
    _check_mission_ceilings(_with(EXAMPLE, decision="15m", backend=backend),
                            ActivityLog(path))
    events = _events(path)
    assert [e[2] for e in events] == ["mission_ceiling_dispatch_warning"]
    assert "判断足 15m (900 秒)" in events[0][3]
    assert f"Cd {cd} 秒" in events[0][3]


def test_startup_1h_writes_no_dispatch_warning(tmp_path):
    path = tmp_path / "a.log"
    _check_mission_ceilings(EXAMPLE, ActivityLog(path))
    assert _events(path) == []


def test_startup_rejects_explicit_dispatch_ceiling_below_cd(tmp_path):
    activity = ActivityLog(tmp_path / "a.log")
    with pytest.raises(RuntimeError) as exc:
        _check_mission_ceilings(_with(EXAMPLE, dispatch_ceiling_sec=1629.0),
                                activity)
    assert "worker.dispatch_ceiling_sec=1629" in str(exc.value)
    assert "Cd 1630 秒" in str(exc.value)
    _check_mission_ceilings(_with(EXAMPLE, dispatch_ceiling_sec=1630.0), activity)


def test_build_app_runs_mission_ceiling_check(tmp_path):
    import agentic_fx.service as service_mod

    _init(tmp_path)
    seen = []
    real = service_mod._check_mission_ceilings

    def spy(settings, activity):
        seen.append(settings.datafeed.decision_timeframe)
        return real(settings, activity)

    with patch("agentic_fx.service._check_mission_ceilings", spy):
        app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
    try:
        assert seen == ["1h"]
    finally:
        app.close()


def test_startup_rejects_when_worker_ceiling_equals_width(tmp_path):
    """Cw == 判断足幅 (15m = 900 秒) も拒否する (境界は「以上」)。
    llama_swap.timeout_sec=795 で Cw = 30+825+10+5+5+5+20 = 900。"""
    activity = ActivityLog(tmp_path / "a.log")
    settings = _with(EXAMPLE, decision="15m", backend="local",
                     llama_swap_timeout_sec=795)
    with pytest.raises(RuntimeError) as exc:
        _check_mission_ceilings(settings, activity)
    assert "判断足 15m (900 秒)" in str(exc.value)
    assert "Cw 900 秒" in str(exc.value)


def test_startup_warns_when_dispatch_ceiling_equals_width(tmp_path):
    """Cd == 判断足幅でも WARNING を書く (境界は「以上」)。
    llama_swap.timeout_sec=117.5 で Cw = 222.5、Cd = 222.5×4+10 = 900。"""
    path = tmp_path / "a.log"
    settings = _with(EXAMPLE, decision="15m", backend="local",
                     llama_swap_timeout_sec=117.5)
    _check_mission_ceilings(settings, ActivityLog(path))
    events = _events(path)
    assert [e[2] for e in events] == ["mission_ceiling_dispatch_warning"]
    assert "Cd 900 秒" in events[0][3]
