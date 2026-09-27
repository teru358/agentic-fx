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


def test_startup_rejects_lease_not_longer_than_worker_ceiling(tmp_path):
    """signal_lease_min * 60 が Cw (worker 上限) + commit-pre 余白
    (`LEASE_COMMIT_MARGIN_SEC`) 以下だと、mission 実行中に lease が切れて
    reclaim_expired が claim 済み signal を pending に戻し得る。cron の
    consume CAS が失敗して判断が捨てられるので起動を拒否する。既定
    (local backend, Cw 405 秒 + 余白 60 秒 = 465 秒) で
    signal_lease_min=5 (300 秒)。"""
    activity = ActivityLog(tmp_path / "a.log")
    settings = EXAMPLE.model_copy(update={"plugin": EXAMPLE.plugin.model_copy(
        update={"signal_lease_min": 5})})
    with pytest.raises(RuntimeError) as exc:
        _check_mission_ceilings(settings, activity)
    msg = str(exc.value)
    assert "signal_lease_min 5 分 (300 秒)" in msg
    assert "上限 405 秒" in msg
    assert "余白 60 秒" in msg
    assert "465 秒以下です" in msg
    assert "signal_lease_min を 8 分以上にしてください" in msg


def test_startup_rejects_lease_margin_not_just_worker_ceiling(tmp_path):
    """commit-pre のネットワーク呼出し分の余白
    (`LEASE_COMMIT_MARGIN_SEC` = 60 秒) を含めない旧判定
    (`lease_sec <= cw.total`) では、Cw (405 秒) より長いが Cw+余白 (465 秒)
    以下の signal_lease_min=7 (420 秒) は素通りしてしまっていた —
    余白込みでも拒否することをピンする。"""
    activity = ActivityLog(tmp_path / "a.log")
    settings = EXAMPLE.model_copy(update={"plugin": EXAMPLE.plugin.model_copy(
        update={"signal_lease_min": 7})})
    with pytest.raises(RuntimeError) as exc:
        _check_mission_ceilings(settings, activity)
    msg = str(exc.value)
    assert "signal_lease_min 7 分 (420 秒)" in msg
    assert "465 秒以下です" in msg


def test_startup_rejects_lease_equal_to_worker_ceiling_plus_margin(tmp_path):
    """境界: signal_lease_min * 60 == Cw + 余白 も拒否する (等しいは拒否)。
    llama_swap.timeout_sec=315 で Cw = 30+345+10+5+5+5+20 = 420 秒、
    余白 60 秒を足した 480 秒に signal_lease_min=8 (480 秒) をちょうど
    合わせる。"""
    activity = ActivityLog(tmp_path / "a.log")
    settings = _with(EXAMPLE, llama_swap_timeout_sec=315)
    settings = settings.model_copy(update={"plugin": settings.plugin.model_copy(
        update={"signal_lease_min": 8})})
    with pytest.raises(RuntimeError) as exc:
        _check_mission_ceilings(settings, activity)
    msg = str(exc.value)
    assert "signal_lease_min 8 分 (480 秒)" in msg
    assert "上限 420 秒" in msg
    assert "余白 60 秒" in msg
    assert "480 秒以下です" in msg


def test_startup_allows_lease_longer_than_worker_ceiling_plus_margin(tmp_path):
    """Cw 420 + 余白 60 = 480 秒に対し 9 分 (540 秒) は通る。
    llama_swap.timeout_sec=315 で Cw=420 秒。"""
    activity = ActivityLog(tmp_path / "a.log")
    settings = _with(EXAMPLE, llama_swap_timeout_sec=315)
    settings = settings.model_copy(update={"plugin": settings.plugin.model_copy(
        update={"signal_lease_min": 9})})
    _check_mission_ceilings(settings, activity)  # 例外を投げない


def test_startup_allows_default_lease_min(tmp_path):
    """既定値 (signal_lease_min 15 分 = 900 秒 > Cw 405 秒 + 余白 60 秒) は
    通る。"""
    activity = ActivityLog(tmp_path / "a.log")
    _check_mission_ceilings(EXAMPLE, activity)  # 例外を投げない


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
