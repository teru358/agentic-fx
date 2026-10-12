"""改善 mission の CLI 隔離の配線 (admission → WorkerRunner → mission worker → launcher)。"""
from __future__ import annotations

import ast
import dataclasses
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "agentic_fx"
EXAMPLE = REPO / "config" / "settings.yaml.example"


def _settings():
    from agentic_fx.config import load_settings

    return load_settings(EXAMPLE)


def _improve_loop(tmp_path: Path):
    from agentic_fx.core.contracts import FixedClock
    from agentic_fx.loops.improve_loop import ImproveLoop

    return ImproveLoop(
        root=tmp_path, settings=_settings(),
        clock=FixedClock(datetime(2026, 10, 11, tzinfo=timezone.utc)),
        db_write_conn_factory=lambda: None, db_readonly_conn_factory=lambda: None,
        activity=None, rag=None)


def _run_context(tmp_path: Path):
    from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
    from agentic_fx.loops.improve_run_context import ImproveRunContext

    staging = tmp_path / "staging" / "1"
    staging.mkdir(parents=True, mode=0o700)
    source = tmp_path / "source"
    source.mkdir()
    return ImproveRunContext(
        mission_id=1, run_id=1, staging_dir=staging, source_snapshot_dir=source,
        allowed_backlog_ids=None, slot_key=None,
        ledger=ImproveRpcLedger(rpc_timeout_sec_by_kind={}),
        rpc_handlers={"run_backtest": lambda a: {}, "analyze_corr": lambda a: {}})


# --- 既定では隔離付き runner を組む --------------------------------------------------

def test_isolation_enabled_builds_isolated_runner(tmp_path, monkeypatch):
    from agentic_fx.core import improve_admission as IA
    from agentic_fx.core import improve_fingerprint as F
    from agentic_fx.core import improve_isolation as I
    from agentic_fx.core import mission_cleanup as MC
    from agentic_fx.runners.worker_runner import WorkerRunner

    # 有効化は code の定数で、settings の key ではない (隔離を緩める key を作らない)
    assert IA.ISOLATION_ENABLED is True
    tree = ast.parse((SRC / "core" / "improve_admission.py").read_text())
    assigned = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) == "ISOLATION_ENABLED" for t in n.targets)]
    assert len(assigned) == 1 and isinstance(assigned[0].value, ast.Constant)
    assert assigned[0].value.value is True
    example = EXAMPLE.read_text()
    assert re.search(r"isolat", example, re.IGNORECASE) is None
    config_src = (SRC / "config.py").read_text()
    assert "ISOLATION_ENABLED" not in config_src and "improve_admission" not in config_src

    # admission はここでは走らせず、admitted choice を注入して runner の配線だけを見る。
    artifact = F.ArtifactIdentity("a" * 64, 1, 2, 3, 4)
    decision = IA.AdmissionDecision(True, None, "codex", fingerprint=SimpleNamespace(
        aggregate="b" * 64), artifact=artifact)
    options = IA.worker_runner_options(decision, SimpleNamespace(
        cleanup_mission=lambda *_: None, report_worker_not_reaped=lambda: None))
    monkeypatch.setattr(IA, "choose_for_mission",
                        lambda *_args, **_kwargs: IA.MissionChoice(worker_options=options,
                                                                     decision=decision))

    loop = _improve_loop(tmp_path)
    runner = loop._build_worker_runner(_run_context(tmp_path))
    assert isinstance(runner, WorkerRunner)
    assert isinstance(runner._cleanup, MC.CleanupProtocol)
    assert isinstance(runner._isolation, dict)
    assert I.parse_isolation_field(runner._isolation) == I.IsolationSpec(
        "mission", "codex", artifact, "b" * 64)


def test_operator_shell_verify_runner_has_no_isolation_fields(tmp_path, monkeypatch):
    from agentic_fx.core.contracts import FixedClock
    from agentic_fx.loops import verify_backend as VB
    from agentic_fx.runners.base import MissionResult

    captured: dict = {}

    class Runner:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self._ready = kwargs["on_ready"]
            self._context = kwargs["run_context"]

        def run(self, mission):
            self._ready({"run_context": {
                "mission_id": str(self._context.mission_id),
                "staging_dir": str(self._context.staging_dir),
                "source_snapshot_dir": "/worker/source"}})
            nonce = mission.output_schema["properties"]["echo"]["const"]
            return MissionResult("completed", {"echo": nonce}, [])

    monkeypatch.setattr(VB, "WorkerRunner", Runner)
    result = VB.verify_backend(
        tmp_path, _settings(), backend="local", provider=None,
        clock=FixedClock(datetime(2026, 10, 11, tzinfo=timezone.utc)),
        isolation_options=VB.OPERATOR_SHELL_NO_ISOLATION)

    assert result.ok is True
    assert "cleanup_protocol" not in captured
    assert "isolation" not in captured


def test_operator_shell_verify_cli_passes_no_isolation_constant(
        tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    from agentic_fx.backtest import cli
    from agentic_fx.loops import verify_backend as VB

    captured: dict = {}

    def fake_verify(*_args, **kwargs):
        captured.update(kwargs)
        return VB.VerifyBackendResult(True, "local", None, "a" * 64, "ok")

    monkeypatch.setattr(VB, "verify_backend", fake_verify)
    rc = cli._improve_verify_backend(
        None, _settings(), SimpleNamespace(backend="local", provider=None), tmp_path)

    assert rc == 0
    assert captured["isolation_options"] is VB.OPERATOR_SHELL_NO_ISOLATION
    assert "隔離適合の合格ではない" in capsys.readouterr().out


# --- disposable service の中の結合 (afx 役 → WorkerRunner → mission worker → launcher → probe) ---

SERVICE_HELPER = REPO / "tests" / "core" / "_improve_isolation_service_helper.py"


@pytest.fixture
def isolation_units(tmp_path):
    from agentic_fx.core import landlock_nested
    from tests.core import _disposable_unit as du

    reason = du.environment_skip_reason()
    if reason is not None:
        pytest.skip(reason)
    if landlock_nested.probe_abi() < landlock_nested.MIN_ABI:
        pytest.skip("Landlock ABI < 8")
    units = du.DisposableUnits(tmp_path)
    try:
        yield units
    finally:
        units.cleanup()
    assert units.listed() == [], "使い捨て unit が残った"


def run_service(units, case: str, scenario: str, timeout: float = 150) -> dict:
    import json
    import sys

    from tests.core import _disposable_unit as du

    out = units.tmp_path / f"{case}.json"
    command = [sys.executable, str(SERVICE_HELPER), scenario, str(out)]
    units.started.append(units.unit_name(case))
    r = du._run(units._argv(case, {"RuntimeMaxSec": "140"}, command, wait=True),
                timeout=timeout)
    if not out.exists():
        raise AssertionError(f"helper が結果を書かなかった (rc={r.returncode}): "
                             f"{r.stdout!r} {r.stderr!r}")
    res = json.loads(out.read_text())
    assert "error" not in res, res["error"]
    return res


def _assert_clean_mission(m: dict) -> None:
    assert m["hook"] and m["hook"][0]["reaped"] is True, m
    assert m["hook"][0]["exists_after"] is False, m
    assert m["m_dirs_left"] == [], m
    assert m["mission_allowed"] is True and m["blocking_reasons"] == [], m
    # worker は自分で正常に終わる (stdin の読み手の thread が残っていても abort しない)
    assert m["worker_returncode"] == 0, m["worker_stderr"]
    events = [e["event"] for e in m["events"]]
    assert "reaped" in events and "worker_sigkill" not in events, events


def test_isolated_improve_mission_end_to_end(isolation_units):
    res = run_service(isolation_units, "e2e", "e2e:codex")
    m = res["mission"]
    assert m["result"]["status"] == "completed", m
    probe = m["result"]["output"]["probe"]
    rep = m["report"]
    assert rep is not None, m
    # spool tool の往復
    assert probe["spool"] == {"init": True, "call": True, "call_error": None}, probe
    # proxy 経由の connect が同期 handoff で受理された
    assert probe["proxy"]["connect"] == "ok", probe
    assert rep["handoff_verdicts"] == {"accept": 1}, rep
    # CLI は m-<nonce> に居た (handshake の nonce から導いた名前)
    cg = rep["cgroup"]
    assert cg.startswith("m-") and probe["pid"] in m["cgroups_seen"][cg], m["cgroups_seen"]
    assert rep["cli_pid"] == probe["pid"]
    # supervisor を組んでから proxy が accept を始める
    assert rep["start_order"] == ["handoff", "supervisor", "proxy"], rep
    # worker 自身の RLIMIT_FSIZE は launcher を fork した後 (8 MiB, 8 MiB)
    assert rep["worker_fsize"] == [8 * 1024 * 1024] * 2, rep
    # worker は自分の source で fingerprint を計算し直し、admission の aggregate と一致した
    agg = res["decision"]["aggregate"]
    assert rep["fingerprint_check"] == {"expected": agg, "computed": agg, "match": True}, rep
    # 正常系の系列: supervisor → cgroup.kill → populated 0 → launcher reap → dispatcher
    cl = rep["cleanup"]
    assert cl["series"] == "after_handoff" and cl["reason"] is None, cl
    assert cl["steps"][:4] == ["supervisor_stop", "cgroup_kill", "populated_zero",
                               "launcher_reap"], cl
    assert "dispatcher_stop" in cl["steps"] and cl["supervisor_state"] == "CLOSED", cl
    assert cl["launcher_reaped"] is True
    assert rep["proxy_stopped"] is True
    # afx 側: worker を reap してから cgroup を rmdir、gate は開いたまま
    _assert_clean_mission(m)
    events = [e["event"] for e in m["events"]]
    assert events.index("issued") < events.index("reaped") < events.index("hook")


def test_hashed_fd_is_the_executed_fd(isolation_units):
    """worker が開いた fd を launcher が検査して、その fd を exec する。admission の後に path を
    別 file へ差し替えても、exec されるのは検査した (admission が見た) inode で、差し替えた
    file ではない。fingerprint は差し替え前のまま。"""
    res = run_service(isolation_units, "renamed", "renamed")
    m = res["mission"]
    assert m["result"]["status"] == "completed", m
    probe = m["result"]["output"]["probe"]
    assert res["swapped_ino"] == res["other_ino"] != res["original_ino"]
    exe = m["report"]["exec_artifact"]
    assert probe["exe"] == [exe["dev"], exe["ino"]], (probe, exe)
    assert exe["ino"] == res["original_ino"] != res["other_ino"]
    assert m["report"]["artifact"] == res["admitted_artifact"]
    assert res["admitted_artifact"]["ino"] == res["original_ino"]
    _assert_clean_mission(m)


def test_in_place_overwrite_after_hash_is_not_executed(isolation_units):
    """admission の後に artifact を同じ inode のまま書き換えると、launcher が exec 直前に行う
    (dev, ino, size, mtime) の再検査が admission の値と合わず、CLI を exec しない。"""
    res = run_service(isolation_units, "in_place", "in_place")
    m = res["mission"]
    # 同じ inode の内容が変わった
    assert res["after_ino"] == res["original_ino"]
    assert res["after_sha256"] != res["admitted_artifact"]["sha256"]
    assert m["result"]["status"] == "failed", m
    assert m["result"]["reason"].startswith(
        "sandbox_unavailable: artifact_changed"), m["result"]
    rep = m["report"]
    assert rep["launched"] is True and rep["launch_reason"] == "artifact_changed", rep
    assert rep["cli_pid"] is None and rep["exec_artifact"] is None, rep
    assert rep["cleanup"]["series"] == "before_handoff", rep
    assert m["report"]["artifact"] == res["admitted_artifact"]
    _assert_clean_mission(m)


def test_artifact_copy_is_charged_to_the_mission_cgroup(isolation_units):
    """launcher は mission cgroup に入った後で artifact を memfd に写すので、memfd の page
    (shmem) は mission cgroup に課金され memory.max の内側に入る (artifact は 150 MiB 超)。
    memfd に写すのは単体の ELF を exec する backend (claude) で、codex は path_fd で写さない。"""
    res = run_service(isolation_units, "memory", "memory_memfd:")
    m = res["mission"]
    assert m["result"]["status"] == "completed", m["result"]
    size = res["artifact_size"]
    assert size >= 150 * 1024 * 1024
    mem = m["cgroup_memory"][m["report"]["cgroup"]]
    assert mem["shmem_max"] >= size, (mem, size)
    assert mem["current_max"] >= size, (mem, size)
    # launcher は artifact の大きさの hard を受け継いで写せた。worker 自身の hard は 8 MiB に戻った
    assert m["report"]["worker_fsize"] == [8 * 1024 * 1024] * 2, m["report"]
    _assert_clean_mission(m)


#: launcher の copy 中の mission cgroup の使用量 (memfd を除く) の実測の最大 (bytes)。
#: 2026-10-11、memory scenario 3 回の `memory.peak - shmem の最大` (6856704 / 7225344 / 7237632)
MEASURED_COPY_PEAK = 7237632


def test_artifact_memory_headroom_is_twice_the_measured_copy_peak():
    from agentic_fx.core import improve_isolation as I

    assert I.ARTIFACT_COPY_PEAK_MEASURED == MEASURED_COPY_PEAK
    assert I.ARTIFACT_MEMORY_HEADROOM >= 2 * MEASURED_COPY_PEAK


def test_artifact_at_the_memory_max_boundary_is_copied_without_oom(isolation_units):
    """artifact の大きさと launcher の見積りの和が memory.max にちょうど等しい (判定を通る最大)
    ときも、launcher は OOM kill されずに写して CLI を起こす。"""
    res = run_service(isolation_units, "memory_boundary", "memory:boundary")
    m = res["mission"]
    assert m["result"]["status"] == "completed", m["result"]
    mem = m["cgroup_memory"][m["report"]["cgroup"]]
    assert mem["events"]["oom_kill"] == 0, mem
    assert mem["peak"] <= res["memory_max"], (mem, res["memory_max"])
    _assert_clean_mission(m)


def test_artifact_larger_than_memory_max_is_refused_before_the_launcher(isolation_units):
    """artifact (と launcher の分) が mission cgroup の memory.max に収まらなければ、launcher を
    起こさず固定 reason で終わる (起こすと copy の途中で OOM kill され launcher_died になる)。"""
    res = run_service(isolation_units, "memory_low", f"memory:{64 * 1024 * 1024}")
    m = res["mission"]
    assert m["result"]["status"] == "failed"
    assert "sandbox_unavailable: artifact_exceeds_memory_max" in m["result"]["reason"], (
        m["result"])
    assert m["report"] is None  # CLI の起動に進んでいない
    assert all(pids == [] for pids in m["cgroups_seen"].values()), m["cgroups_seen"]
    assert all(v.get("events", {}).get("oom_kill", 0) == 0
               for v in m["cgroup_memory"].values()), m["cgroup_memory"]
    _assert_clean_mission(m)


def test_memory_max_check_runs_before_any_resource(monkeypatch, codex_cli_path):
    """memory.max の見積りは mission cgroup を作る前に行う (作った資源を片付ける経路を持たない)。
    収まる大きさなら次の段 (cgroup の作成) へ進む。"""
    from agentic_fx.core import cli_launcher
    from agentic_fx.core import improve_isolation as I
    from agentic_fx.core import mission_cgroup as MCG

    created: list[str] = []

    def create(cls, nonce_hex, **kw):
        created.append(nonce_hex)
        raise MCG.MissionCgroupError("cgroup_create_failed", "injected")

    monkeypatch.setattr(cli_launcher, "clear_inheritable_caps", lambda: None)
    monkeypatch.setattr(MCG.MissionCgroup, "create", classmethod(create))
    spec = I.parse_isolation_field(_spec_field())
    nonce = SimpleNamespace(hex="ab" * 16)
    limit = MCG.MEMORY_MAX - I.ARTIFACT_MEMORY_HEADROOM

    def with_size(size: int) -> I.IsolationSpec:
        # fingerprint は worker が計算し直す値と一致させる (照合の次の段を見る)
        artifact = dataclasses.replace(spec.artifact, size=size)
        return I.IsolationSpec(spec.kind, spec.backend, artifact, _admitted_fingerprint(
            spec.backend, artifact, codex_cli_path))

    with pytest.raises(I.IsolationSetupError) as ei:
        I.WorkerIsolation.prepare(with_size(limit + 1), nonce, codex_cli_path)
    assert ei.value.reason == "artifact_exceeds_memory_max" and created == []
    with pytest.raises(I.IsolationSetupError) as ei:
        I.WorkerIsolation.prepare(with_size(limit), nonce, codex_cli_path)
    assert ei.value.reason == "cgroup_create_failed" and created == ["ab" * 16]


@pytest.fixture
def codex_cli_path():
    """codex の package 配置 (host と manifest つき) の `bin/codex`。"""
    import shutil

    from tests.core._codex_package import make_codex_package

    cli = make_codex_package()
    yield str(cli)
    shutil.rmtree(cli.parent.parent, ignore_errors=True)


def _admitted_fingerprint(backend: str, artifact, artifact_path: str | None = None) -> str:
    from agentic_fx.core import improve_fingerprint as F

    return F.compute_fingerprint(backend, artifact, F.probe_kernel(),
                                 artifact_path=artifact_path).aggregate


def _flip(hex64: str) -> str:
    return ("0" if hex64[0] != "0" else "1") + hex64[1:]


def test_worker_recomputes_the_fingerprint_before_any_resource(monkeypatch, codex_cli_path):
    """worker は自分の source で fingerprint を計算し直し、handshake の値 (admission の aggregate)
    と 1 文字でも違えば `fingerprint_mismatch` で止める。capability・cgroup・launcher には進まない。
    一致すれば次の段 (capability の整理と cgroup の作成) へ進む。"""
    import subprocess

    from agentic_fx.core import cli_launcher
    from agentic_fx.core import improve_isolation as I
    from agentic_fx.core import mission_cgroup as MCG

    steps: list[str] = []

    def create(cls, nonce_hex, **kw):
        steps.append("cgroup")
        raise MCG.MissionCgroupError("cgroup_create_failed", "injected")

    def no_popen(*a, **kw):
        raise AssertionError("no process may be started")

    monkeypatch.setattr(cli_launcher, "clear_inheritable_caps", lambda: steps.append("caps"))
    monkeypatch.setattr(MCG.MissionCgroup, "create", classmethod(create))
    monkeypatch.setattr(subprocess, "Popen", no_popen)
    spec = I.parse_isolation_field(_spec_field())
    good = dataclasses.replace(spec, fingerprint=_admitted_fingerprint(
        spec.backend, spec.artifact, codex_cli_path))
    bad = dataclasses.replace(good, fingerprint=_flip(good.fingerprint))
    nonce = SimpleNamespace(hex="ab" * 16)

    with pytest.raises(I.IsolationSetupError) as ei:
        I.WorkerIsolation.prepare(bad, nonce, codex_cli_path)
    assert ei.value.reason == "fingerprint_mismatch" and steps == []
    assert str(ei.value).startswith("sandbox_unavailable: fingerprint_mismatch")
    assert I.verify_fingerprint(good, artifact_path=codex_cli_path) == good.fingerprint
    with pytest.raises(I.IsolationSetupError) as ei:
        I.WorkerIsolation.prepare(good, nonce, codex_cli_path)
    assert ei.value.reason == "cgroup_create_failed" and steps == ["caps", "cgroup"]


def test_fingerprint_that_cannot_be_computed_is_a_mismatch(monkeypatch):
    """worker が fingerprint を計算できなければ照合できたとはみなさない (fail closed)。"""
    from agentic_fx.core import improve_fingerprint as F
    from agentic_fx.core import improve_isolation as I

    def broken():
        raise OSError("injected")

    monkeypatch.setattr(F, "probe_kernel", broken)
    spec = I.parse_isolation_field(_spec_field())
    with pytest.raises(I.IsolationSetupError) as ei:
        I.verify_fingerprint(spec)
    assert ei.value.reason == "fingerprint_mismatch" and "OSError" in ei.value.detail


def test_artifact_changed_after_admission_is_not_executed(isolation_units):
    """admission が見た artifact と worker が開いた fd の hash が違えば、launcher を起こさない。"""
    res = run_service(isolation_units, "changed", "changed")
    m = res["mission"]
    assert m["result"]["status"] == "failed"
    assert "cli_artifact_unsupported" in m["result"]["reason"], m["result"]
    assert m["report"] is None  # CLI の起動に進んでいない
    assert all(pids == [] for pids in m["cgroups_seen"].values()), m["cgroups_seen"]
    _assert_clean_mission(m)


def test_supervisor_fault_stops_cli_and_fails_mission(isolation_units):
    res = run_service(isolation_units, "fault", "fault")
    m = res["mission"]
    assert (m["result"]["status"], m["result"]["reason"]) == (
        "failed", "sandbox_unavailable: supervisor_died"), m["result"]
    assert m["elapsed"] < 15, m["elapsed"]  # probe の 20 秒の hold を待たずに止めた
    rep = m["report"]
    assert rep["fault"] == "supervisor_died"
    assert rep["cleanup"]["series"] == "after_handoff" and rep["cleanup"]["reason"] is None
    _assert_clean_mission(m)


#: CLI を起こさなかった mission の後始末 (§4.2.3 の handoff 前系列、未移譲 fd は無い)
UNLAUNCHED_STEPS = ["close_untransferred", "cgroup_kill", "populated_zero", "launcher_reap",
                    "dispatcher_stop", "supervisor_stop"]


def _assert_unlaunched_series(m: dict) -> None:
    rep = m["report"]
    assert rep is not None and rep["launched"] is False, m
    cl = rep["cleanup"]
    assert cl is not None, rep
    assert cl["series"] == "before_handoff" and cl["reason"] is None, cl
    assert cl["steps"][:len(UNLAUNCHED_STEPS)] == UNLAUNCHED_STEPS, cl
    events = [e["event"] for e in m["events"]]
    # deadline を受けて ACK してから始めた
    assert "issued" in events and "ack" in events, events


def test_unlaunched_mission_runs_the_before_handoff_series(isolation_units):
    """CLI を起こす前に障害が起きた mission も、cgroup.kill → populated 0 の後に dispatcher を
    止める。dispatcher の停止は系列の 1 回だけ (mission の終わりで重ねて止めない)。"""
    res = run_service(isolation_units, "early-fault", "early_fault")
    m = res["mission"]
    assert (m["result"]["status"], m["result"]["reason"]) == (
        "failed", "sandbox_unavailable: supervisor_died"), m["result"]
    _assert_unlaunched_series(m)
    assert m["report"]["cli_pid"] is None
    assert m["dispatcher_stops"] == 1, m
    _assert_clean_mission(m)


def test_mission_abandoned_before_go_runs_the_series(isolation_units):
    """afx が go を送らずに mission をやめても、worker は deadline を受けて待つのをやめ、
    handoff 前系列で片付けて自分で終わる (WorkerRunner の SIGKILL を待たない)。"""
    res = run_service(isolation_units, "abort", "abort")
    m = res["mission"]
    assert m["result"]["status"] == "failed", m["result"]
    assert m["result"]["reason"].startswith("on_ready failed"), m["result"]
    events = [e["event"] for e in m["events"]]
    assert "ack" in events and "worker_sigkill" not in events, events
    assert m["dispatcher_stops"] == 1, m
    _assert_clean_mission(m)


def test_worker_exit_code_follows_the_escaping_exception(isolation_units):
    """router を起こした worker でも、main を抜ける SystemExit の code が終了 code になる
    (0 に潰さない)。mission は result 無しで失敗し、mission cgroup は片付く。"""
    res = run_service(isolation_units, "sysexit", "system_exit")
    m = res["mission"]
    assert m["worker_returncode"] == 3, m["worker_stderr"]
    assert m["result"]["status"] == "failed", m["result"]
    assert m["m_dirs_left"] == [] and m["mission_allowed"] is True, m
    assert m["hook"] and m["hook"][0]["reaped"] is True, m


def test_exit_code_for_the_escaping_exception(capsys):
    from agentic_fx import mission_worker as MW

    assert MW._exit_code_for(SystemExit(3)) == 3
    assert MW._exit_code_for(SystemExit()) == 0
    assert MW._exit_code_for(SystemExit("bye")) == 1
    assert MW._exit_code_for(KeyboardInterrupt()) == 130
    assert MW._exit_code_for(RuntimeError("x")) == 1
    err = capsys.readouterr().err
    assert "bye" in err and "RuntimeError: x" in err


def test_isolation_close_failure_is_reported(capsys, monkeypatch):
    from agentic_fx import mission_worker as MW

    class Iso:
        def close(self):
            raise OSError("injected")

    monkeypatch.setattr(MW, "_isolation_close_failed", False)
    assert MW._close_isolation(Iso()) is False
    assert MW._isolation_close_failed is True
    assert capsys.readouterr().err == "mission_worker: isolation_close_failed: OSError\n"
    assert MW._close_isolation(None) is True


def test_isolated_mission_stops_the_dispatcher_once(isolation_units):
    """CLI を起こした mission も dispatcher の停止は系列の 1 回だけ。"""
    res = run_service(isolation_units, "e2e-stops", "e2e:codex")
    m = res["mission"]
    assert m["result"]["status"] == "completed", m
    assert m["dispatcher_stops"] == 1, m
    _assert_clean_mission(m)


def test_gate_closed_blocks_next_mission(isolation_units):
    res = run_service(isolation_units, "gate", "gate")
    first = res["first"]
    assert first["result"]["status"] == "completed", first
    # reap できなかった扱い: rmdir はせず、gate を閉じる
    assert first["hook"][0]["exists_after"] is True
    assert first["blocking_reasons"] == ["worker_not_reaped"]
    assert first["mission_allowed"] is False
    assert res["second_refusal"] == "worker_not_reaped"
    assert res["second_options"] == []
    assert res["second_result"] == {"status": "failed",
                                    "reason": "sandbox_unavailable: worker_not_reaped"}
    assert res["children_after_refusal"] == []


def test_opencode_has_no_proxy(isolation_units):
    res = run_service(isolation_units, "oc", "e2e:opencode")
    m = res["mission"]
    assert m["result"]["status"] == "completed", m
    probe = m["result"]["output"]["probe"]
    rep = m["report"]
    assert rep["proxy_port"] is None and rep["handoff_verdicts"] == {}
    assert rep["start_order"] == ["supervisor"], rep
    assert probe["env"] == {}
    # 許可宛先は 8080 だけ (別の loopback port は supervisor が拒否する)
    assert rep["destinations"] == {"proxy_port": None, "direct_port": 8080}
    assert probe["direct"]["connect"] == "ECONNREFUSED", probe
    _assert_clean_mission(m)


@pytest.mark.parametrize("backend", ["claude", "codex"])
def test_proxy_env_for_claude_and_codex(isolation_units, backend):
    res = run_service(isolation_units, f"env-{backend}", f"e2e:{backend}")
    m = res["mission"]
    assert m["result"]["status"] == "completed", m
    probe = m["result"]["output"]["probe"]
    port = m["report"]["proxy_port"]
    url = f"http://127.0.0.1:{port}"
    assert probe["env"] == {"HTTPS_PROXY": url, "https_proxy": url, "HTTP_PROXY": url,
                            "http_proxy": url, "NO_PROXY": "localhost,127.0.0.1",
                            "no_proxy": "localhost,127.0.0.1"}
    assert probe["managed_env"] == []
    assert m["report"]["destinations"] == {"proxy_port": port, "direct_port": None}
    _assert_clean_mission(m)


# --- admission を通らない mission は CLI も launcher も起こさない ---------------------------

def _fake_host(reasons=()):
    return SimpleNamespace(mission_allowed=lambda: not reasons,
                           blocking_reasons=lambda: tuple(reasons))


def _unit_admission_denied_host():
    """実 `MissionCgroupHost` で unit admission が通らない (自己移動の前に止まる)。"""
    import time

    from agentic_fx.core import cgroup_host as ch
    from agentic_fx.core import unit_admission as ua

    def deny():
        raise ua.AdmissionError("cgroup_delegation_missing", "Delegate=no")

    host = ch.MissionCgroupHost()
    host.start(deadline=time.monotonic() + 5, admit=deny)
    return host


FAIL_CASES = {
    "unit_admission": (_unit_admission_denied_host, {}, "cgroup_delegation_missing"),
    "landlock_abi": (lambda: _fake_host(), {"landlock_abi": 7}, "landlock_abi_too_old"),
    "inet_diag": (lambda: _fake_host(), {"inet_diag": "unavailable: EPERM"},
                  "proxy_start_failed"),
}


@pytest.mark.parametrize("case", sorted(FAIL_CASES))
def test_admission_failure_fails_closed(case, tmp_path, monkeypatch):
    import dataclasses
    import os
    import subprocess
    import sys

    from agentic_fx.core import improve_admission as IA
    from agentic_fx.core import improve_fingerprint as F
    from agentic_fx.runners import worker_runner as WR
    from agentic_fx.runners.base import Mission

    make_host, kernel_update, expected = FAIL_CASES[case]
    host = make_host()
    kernel = dataclasses.replace(
        F.KernelCapabilities(machine="x86_64", landlock_abi=8, seccomp_missing=(),
                             supervisor_missing=(), yama="1", inet_diag="ok"),
        **kernel_update)
    probes = []
    adm = IA.ImproveAdmission(canary=lambda c: probes.append("canary") or IA.ProbeOutcome(True),
                              selftest=lambda c: probes.append("self") or IA.ProbeOutcome(True),
                              kernel_probe=lambda: kernel)
    real_choose = IA.choose_for_mission
    monkeypatch.setattr(IA, "choose_for_mission", lambda settings, root, **kw: real_choose(
        settings, root, enabled=True, host=host, admission=adm))
    spawned = []

    def no_popen(*a, **kw):
        spawned.append(a)
        raise AssertionError("no process may be started")

    monkeypatch.setattr(WR.subprocess, "Popen", no_popen)
    monkeypatch.setattr(subprocess, "Popen", no_popen)

    loop = _improve_loop(tmp_path)
    s = loop._settings
    loop._settings = s.model_copy(update={"runner": s.runner.model_copy(update={
        "improve": s.runner.improve.model_copy(update={"backend": "codex"}),
        "codex": s.runner.codex.model_copy(update={"bin": os.path.realpath(sys.executable)})})})
    runner = loop._build_worker_runner(_run_context(tmp_path))
    assert type(runner) is IA.RefusedRunner
    res = runner.run(Mission(prompt="p", tools=[], output_schema={}, max_turns=1,
                             timeout_sec=5))
    assert (res.status, res.reason) == ("failed", f"sandbox_unavailable: {expected}")
    assert spawned == [] and probes == []


def test_service_startup_keeps_afx_cgroup_while_disabled(tmp_path, monkeypatch, caplog):
    """明示的に無効化した診断は host を起こさず、1 行だけ技術ログに出す。"""
    import logging

    from agentic_fx import service
    from agentic_fx.core import cgroup_host as ch
    from agentic_fx.core import improve_admission as IA
    from agentic_fx.core import unit_admission as ua

    monkeypatch.setattr(IA, "ISOLATION_ENABLED", False)
    monkeypatch.setattr(IA, "_HOST", None)

    def forbidden(*a, **kw):
        raise AssertionError("must not touch the afx cgroup while disabled")

    monkeypatch.setattr(ch.MissionCgroupHost, "start", forbidden)
    monkeypatch.setattr(ch, "adopt_topology", forbidden)

    def deny():
        raise ua.AdmissionError("cgroup_delegation_missing", "screen wrapper")

    monkeypatch.setattr(ua, "admit_unit", deny)
    before = Path("/proc/self/cgroup").read_text()
    with caplog.at_level(logging.INFO, logger="agentic_fx.service"):
        service._log_improve_isolation_diagnostics(_settings(), tmp_path)
    assert Path("/proc/self/cgroup").read_text() == before
    assert IA.current_host() is None
    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("improve isolation:")]
    assert len(lines) == 1 and "disabled" in lines[0]
    assert "unit_admission=cgroup_delegation_missing" in lines[0]
    src = (SRC / "service.py").read_text()
    assert "target=_log_improve_isolation_diagnostics" in src


# --- worker 側の部品 (handshake の field、stdin の読み手、CliRunner の差し込み) -----------------

def _spec_field(**over):
    base = {"kind": "mission", "backend": "codex",
            "artifact": {"sha256": "a" * 64, "dev": 1, "ino": 2, "size": 3, "mtime_ns": 4},
            "fingerprint": "b" * 64}
    base.update(over)
    return base


def test_isolation_field_is_validated():
    from agentic_fx.core import improve_isolation as I
    from agentic_fx.core.mission_protocol import ProtocolError

    spec = I.parse_isolation_field(_spec_field())
    assert I.parse_isolation_field(spec.as_field()) == spec
    for bad in (_spec_field(kind="other"), _spec_field(backend="local"),
                _spec_field(fingerprint="B" * 64), _spec_field(artifact={"sha256": "a"}),
                {**_spec_field(), "extra": 1}, "x", None):
        with pytest.raises(ProtocolError):
            I.parse_isolation_field(bad)


def test_worker_runner_isolation_requires_cleanup_protocol(tmp_path):
    from agentic_fx.core import mission_cleanup as MC
    from agentic_fx.core.contracts import FixedClock
    from agentic_fx.runners.worker_runner import WorkerRunner

    kw = dict(root=tmp_path, settings=_settings(), rag=None,
              clock=FixedClock(datetime(2026, 10, 11, tzinfo=timezone.utc)))
    with pytest.raises(ValueError):
        WorkerRunner(worker_profile="improve", isolation=_spec_field(), **kw)
    with pytest.raises(ValueError):
        WorkerRunner(worker_profile="trade", isolation=_spec_field(), **kw)
    r = WorkerRunner(worker_profile="improve", isolation=_spec_field(),
                     cleanup_protocol=MC.CleanupProtocol(), **kw)
    assert r._isolation == _spec_field()


def test_stdin_router_routes_frames_in_order():
    import io
    import time

    from agentic_fx.core import improve_isolation as I
    from agentic_fx.core import mission_protocol as MP

    frames = [{"type": "go", "seq": 2},
              {"type": "tool_rpc_result", "seq": 3, "rpc_id": "1", "ok": True, "result": 1},
              {"type": "cleanup_deadline", "seq": 4, "deadline": time.monotonic() + 5}]
    stream = io.BytesIO(b"".join(MP.encode_frame(f) for f in frames))
    seq = MP.SeqTracker()
    seq.check(1)  # handshake
    delivered, stops = [], []
    router = I.StdinRouter(stream, seq, deliver=delivered.append, on_stop=stops.append)
    router.start()
    assert router.wait_go(5)
    assert router.read_rpc()["rpc_id"] == "1"
    assert router.read_rpc() is None  # EOF
    assert [f["type"] for f in delivered] == ["cleanup_deadline"]
    assert stops == ["cleanup_deadline_received", "parent_gone"]

    bad = io.BytesIO(MP.encode_frame({"type": "go", "seq": 3}))
    seq2 = MP.SeqTracker()
    seq2.check(1)
    stops2 = []
    r2 = I.StdinRouter(bad, seq2, deliver=lambda f: None, on_stop=stops2.append)
    r2.start()
    assert r2.wait_go(2) is False and r2.error is not None
    assert stops2 == ["parent_gone"]


def test_stdin_router_stops_waiting_for_go_on_cleanup_deadline():
    """go の前に afx の deadline が届けば、pipe が開いたままでも go を待つのをやめる。"""
    import os
    import time

    from agentic_fx.core import improve_isolation as I
    from agentic_fx.core import mission_protocol as MP

    r_fd, w_fd = os.pipe()
    stream = os.fdopen(r_fd, "rb")
    seq = MP.SeqTracker()
    seq.check(1)  # handshake
    delivered, stops = [], []
    router = I.StdinRouter(stream, seq, deliver=delivered.append, on_stop=stops.append)
    router.start()
    try:
        os.write(w_fd, MP.encode_frame({"type": "cleanup_deadline", "seq": 2,
                                        "deadline": time.monotonic() + 5}))
        t0 = time.monotonic()
        assert router.wait_go(20) is False
        assert time.monotonic() - t0 < 2
        assert router.closed.is_set() is False  # pipe は開いたまま
        assert [f["type"] for f in delivered] == ["cleanup_deadline"]
        assert stops == ["cleanup_deadline_received"]
    finally:
        os.close(w_fd)
        router._thread.join(5)
        stream.close()


def test_wait_go_is_false_when_the_deadline_arrived_before_go():
    """afx の deadline が go より先に届いていれば、その後に go が来ても wait_go は False。
    (読み手が両方を読み終えてから問う順序に固定する)"""
    import io
    import time

    from agentic_fx.core import improve_isolation as I
    from agentic_fx.core import mission_protocol as MP

    frames = [{"type": "cleanup_deadline", "seq": 2, "deadline": time.monotonic() + 5},
              {"type": "go", "seq": 3}]
    stream = io.BytesIO(b"".join(MP.encode_frame(f) for f in frames))
    seq = MP.SeqTracker()
    seq.check(1)  # handshake
    router = I.StdinRouter(stream, seq, deliver=lambda f: None, on_stop=lambda why: None)
    router.start()
    router._thread.join(5)
    assert router.go.is_set() and router.deadline_received.is_set()
    assert router.wait_go(1) is False


def _router_on_pipe():
    import os

    from agentic_fx.core import improve_isolation as I
    from agentic_fx.core import mission_protocol as MP

    r_fd, w_fd = os.pipe()
    stream = os.fdopen(r_fd, "rb")
    seq = MP.SeqTracker()
    seq.check(1)  # handshake
    stops: list[str] = []
    router = I.StdinRouter(stream, seq, deliver=lambda f: None, on_stop=stops.append)
    router.start()
    return router, stream, w_fd, stops


def test_rpc_wait_ends_on_cleanup_deadline():
    """tool_rpc の応答待ちの最中に afx の deadline が届けば、待っている dispatcher の呼び出しは
    すぐ戻る (pipe は開いたまま)。以後の呼び出しも待たない。"""
    import io
    import os
    import time

    from agentic_fx import mission_worker as MW
    from agentic_fx.core import mission_protocol as MP

    router, stream, w_fd, stops = _router_on_pipe()
    call = MW._make_rpc_client(io.BytesIO(), MP.SeqTracker(), None, read_fn=router.read_rpc)
    errors: list[BaseException] = []

    def dispatcher() -> None:
        try:
            call("run_backtest", {})
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=dispatcher, daemon=True)
    try:
        t.start()
        time.sleep(0.3)
        assert t.is_alive()
        os.write(w_fd, MP.encode_frame({"type": "cleanup_deadline", "seq": 2,
                                        "deadline": time.monotonic() + 5}))
        t0 = time.monotonic()
        t.join(3)
        assert not t.is_alive() and time.monotonic() - t0 < 2
        assert len(errors) == 1 and isinstance(errors[0], RuntimeError)
        assert router.closed.is_set() is False
        assert stops == ["cleanup_deadline_received"]
        t0 = time.monotonic()
        assert router.read_rpc() is None and router.read_rpc() is None
        assert time.monotonic() - t0 < 1
    finally:
        os.close(w_fd)
        router._thread.join(5)
        stream.close()


def test_rpc_reads_after_parent_eof_all_return_none():
    import os
    import time

    router, stream, w_fd, stops = _router_on_pipe()
    try:
        os.close(w_fd)
        router._thread.join(5)
        out: list = []
        t = threading.Thread(target=lambda: out.extend(router.read_rpc() for _ in range(3)),
                             daemon=True)
        t0 = time.monotonic()
        t.start()
        t.join(3)
        assert not t.is_alive() and out == [None, None, None]
        assert time.monotonic() - t0 < 2
        assert stops == ["parent_gone"]
    finally:
        stream.close()


def test_router_stop_reason_is_classified():
    """stdin の読み手の停止で CLI を止めたときの reason も `<分類>: <固定 reason>` の形。"""
    from agentic_fx import mission_worker as MW

    iso = SimpleNamespace(fault=None)
    assert MW._isolation_fault_reason(None, {"reason": "parent_gone"}) is None
    assert MW._isolation_fault_reason(iso, {}) is None
    assert (MW._isolation_fault_reason(iso, {"reason": "parent_gone"})
            == "sandbox_unavailable: parent_gone")
    assert (MW._isolation_fault_reason(iso, {"reason": "cleanup_deadline_received"})
            == "sandbox_unavailable: cleanup_deadline_received")
    faulted = SimpleNamespace(fault="supervisor_died")
    assert (MW._isolation_fault_reason(faulted, {"reason": "parent_gone"})
            == "sandbox_unavailable: supervisor_died")


def test_close_does_not_stop_the_egress_twice():
    """起動した経路で cleanup の系列が proxy を止めた後、close() は止め直さない (ACK 済みの
    deadline を超えて待たず、report の観測を上書きしない)。"""
    import os

    from agentic_fx.core import improve_isolation as I

    calls: list[float] = []

    class Proxy:
        failure = None

        def stop(self, deadline):
            calls.append(deadline)
            return len(calls) > 1

    fd = os.open("/dev/null", os.O_RDONLY | os.O_CLOEXEC)
    spec = I.parse_isolation_field(_spec_field())
    iso = I.WorkerIsolation(spec, None, SimpleNamespace(name="m-x", close=lambda: None), fd,
                            None, None)
    iso.proxy = Proxy()
    iso.report.launched = True
    iso._stop_egress(123.0)  # cleanup の系列の終わり (IsolatedLaunch._finish) と同じ呼び出し
    iso.close()
    assert calls == [123.0]
    assert iso.report.proxy_stopped is False


def test_isolated_launch_lowers_the_worker_fsize_hard_after_the_fork(monkeypatch):
    """worker は launcher を fork した直後 (`after_spawn`) に自分の RLIMIT_FSIZE の hard を下げ、
    launch_cli がどう終わっても戻る前にもう一度下げて report に残す。"""
    import os

    from agentic_fx.core import cli_launch as L
    from agentic_fx.core import improve_isolation as I

    events: list[str] = []
    monkeypatch.setattr(I, "pin_worker_fsize", lambda: events.append("pin") or (1, 1))

    def fake_launch_cli(**kw):
        events.append("fork")
        kw["after_spawn"]()
        os.close(kw["cli_fd"])
        return SimpleNamespace(started=False, reason="launcher_died", detail="",
                               classification="sandbox_unavailable", exec_identity=None)

    monkeypatch.setattr(L, "launch_cli", fake_launch_cli)
    monkeypatch.setattr(I.IsolatedLaunch, "_finish", lambda self, outcome, why: None)
    fd = os.open("/dev/null", os.O_RDONLY | os.O_CLOEXEC)
    spec = I.parse_isolation_field(_spec_field(backend="opencode"))
    iso = I.WorkerIsolation(spec, SimpleNamespace(raw=b"n" * 16),
                            SimpleNamespace(name="m-x", close=lambda: None, join=None), fd,
                            None, None)
    iso.supervisor = SimpleNamespace(handoff=None)
    try:
        with pytest.raises(I.CliLaunchRefused):
            iso.launcher(None).start(["x"], {}, cwd="/", stdin=None)
    finally:
        os.close(fd)
    assert events == ["fork", "pin", "pin"]
    assert iso.report.worker_fsize == [1, 1]


def test_pin_worker_fsize_lowers_the_hard_limit_for_good():
    """hard を soft まで下げ、以後は上げられない (別 process で確かめる)。"""
    import json
    import resource
    import subprocess
    import sys

    code = (
        "import json, resource\n"
        "from agentic_fx.core import improve_isolation as I\n"
        "M = 1 << 20\n"
        "resource.setrlimit(resource.RLIMIT_FSIZE, (8 * M, 300 * M))\n"
        "after = I.pin_worker_fsize()\n"
        "try:\n"
        "    resource.setrlimit(resource.RLIMIT_FSIZE, (8 * M, 300 * M))\n"
        "    raised = True\n"
        "except ValueError:\n"
        "    raised = False\n"
        "print(json.dumps([list(after), raised]))\n")

    def no_core():
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       timeout=60, preexec_fn=no_core, check=True)
    mib = 1 << 20
    assert json.loads(r.stdout) == [[8 * mib, 8 * mib], False]


def test_isolated_launch_runs_once():
    from agentic_fx.core import improve_isolation as I

    launch = I.IsolatedLaunch(SimpleNamespace(fault=None), channel=None)
    launch._used = True
    with pytest.raises(I.CliLaunchRefused) as e:
        launch.start(["x"], {}, cwd="/", stdin=None)
    assert e.value.reason == "sandbox_unavailable: isolated_launch_once"
    faulted = I.IsolatedLaunch(SimpleNamespace(fault="supervisor_died"), channel=None)
    with pytest.raises(I.CliLaunchRefused) as e2:
        faulted.start(["x"], {}, cwd="/", stdin=None)
    assert e2.value.reason == "sandbox_unavailable: supervisor_died"


def _start_with_recorders(monkeypatch, proxy_start_fails=False):
    """`WorkerIsolation.start` の部品を記録係に替え、起動順を返す (kernel の応答は使わない)。"""
    import os
    import socket

    from agentic_fx.core import connect_supervisor as CS
    from agentic_fx.core import improve_isolation as I
    from agentic_fx.egress import handoff as HO
    from agentic_fx.egress import inet_diag
    from agentic_fx.egress import proxy as PX
    from agentic_fx.egress import resolver

    order: list[str] = []

    class Handoff:
        def __init__(self, diag):
            order.append("handoff")
            self.admit = self.begin_stop = lambda *a, **k: None

    class Supervisor:
        def __init__(self, dest, *, proxy_handoff, on_fault):
            order.append(f"supervisor:{dest.proxy_port}")
            self.handoff = proxy_handoff

        def stop(self, deadline):
            order.append("supervisor_stop")
            return "CLOSED"

        def join(self, timeout):
            order.append("supervisor_join")
            return True

        def release(self):
            order.append("supervisor_release")

    class Proxy:
        def __init__(self, listener, resolver_, *, admit, on_stopping):
            order.append("proxy")
            self.port = listener.getsockname()[1]
            self.failure = None

        def start(self):
            if proxy_start_fails:
                raise OSError("injected")
            order.append("proxy_start")

    monkeypatch.setattr(HO, "SyncHandoff", Handoff)
    monkeypatch.setattr(inet_diag, "InetDiag", lambda sock: None)
    monkeypatch.setattr(resolver, "MissionResolver", lambda backend: None)
    monkeypatch.setattr(CS, "ConnectSupervisor", Supervisor)
    monkeypatch.setattr(PX, "ConnectProxy", Proxy)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    fd = os.open("/dev/null", os.O_RDONLY | os.O_CLOEXEC)
    spec = I.parse_isolation_field(_spec_field())
    iso = I.WorkerIsolation(spec, None, SimpleNamespace(name="m-x", close=lambda: None), fd,
                            listener, None)
    port = listener.getsockname()[1]
    try:
        iso.start()
        err = None
    except I.IsolationSetupError as e:
        err = e
    finally:
        iso._stop_watch.set()
        listener.close()
        os.close(fd)
    return order, port, iso, err


def test_supervisor_is_built_before_the_proxy_listens(monkeypatch):
    """起動順は handoff の用意 → supervisor → proxy (accept の開始は supervisor の後)。"""
    order, port, iso, err = _start_with_recorders(monkeypatch)
    assert err is None
    assert order == ["handoff", f"supervisor:{port}", "proxy", "proxy_start"]
    assert iso.report.start_order == ["handoff", "supervisor", "proxy"]


def test_proxy_start_failure_stops_the_supervisor(monkeypatch):
    order, port, iso, err = _start_with_recorders(monkeypatch, proxy_start_fails=True)
    assert err is not None and err.reason == "proxy_start_failed"
    assert order == ["handoff", f"supervisor:{port}", "proxy", "supervisor_stop",
                     "supervisor_join", "supervisor_release"]
    assert iso.report.start_order == ["handoff", "supervisor"] and iso.supervisor is None


class _FakeLaunch:
    """CliRunner に差し込む起動の方法の代役 (実 process を起こし、停止の回数を数える)。"""

    def __init__(self, script: str, refuse_after: int = 1) -> None:
        self.script = script
        self.refuse_after = refuse_after
        self.starts = 0
        self.terminated = 0
        self.argv = None
        self.env = None

    def start(self, argv, env, *, cwd, stdin):
        import subprocess
        import sys

        from agentic_fx.core.improve_isolation import CliLaunchRefused

        self.starts += 1
        if self.starts > self.refuse_after:
            raise CliLaunchRefused("sandbox_unavailable: isolated_launch_once")
        self.argv, self.env = argv, env
        proc = subprocess.Popen([sys.executable, "-c", self.script], cwd=cwd, stdin=stdin,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def terminate():
            self.terminated += 1
            proc.wait(10)

        return SimpleNamespace(proc=proc, pgid=None, terminate=terminate)


def _codex_runner(tmp_path, launch, **kw):
    from agentic_fx.runners.codex_runner import CodexRunner
    from agentic_fx.tools.registry import ToolRegistry

    r = CodexRunner(bin_path=Path("/nonexistent/codex"), model="m", workdir=tmp_path,
                    provider="chatgpt", cli_terminate_grace_sec=1.0, registry=ToolRegistry(),
                    launch=launch, **kw)
    r._transcript_dir = tmp_path / "tr"
    r.mcp_spool_dir = tmp_path / "spool"
    return r


PROMPT = "差し込みの試験の mission"


def test_cli_runner_uses_injected_launch_without_pgid(tmp_path):
    from agentic_fx.runners.base import Mission

    (tmp_path / "output.txt").write_text('{"ok": 1}')
    launch = _FakeLaunch("import sys; sys.stdout.write('line\\n')")
    started = []
    runner = _codex_runner(tmp_path, launch, cli_started_sink=started.append)
    res = runner.run(Mission(prompt=PROMPT, tools=[], output_schema={"type": "object"},
                             max_turns=1, timeout_sec=30))
    assert res.status == "completed" and res.output == {"ok": 1}
    assert launch.starts == 1 and launch.terminated == 1
    # CLI は worker の process group に居るので pgid を通知しない
    assert started == []
    assert launch.argv[0] == "/nonexistent/codex"


def test_cli_runner_launch_refused_fails_without_cli(tmp_path):
    from agentic_fx.runners.base import Mission

    launch = _FakeLaunch("", refuse_after=0)
    runner = _codex_runner(tmp_path, launch)
    res = runner.run(Mission(prompt=PROMPT, tools=[], output_schema={}, max_turns=1,
                             timeout_sec=30))
    assert (res.status, res.reason) == ("failed", "sandbox_unavailable: isolated_launch_once")
    assert launch.terminated == 0


def test_opencode_recovery_is_refused_on_the_isolated_launch(tmp_path):
    from agentic_fx.runners.base import Mission
    from agentic_fx.runners.opencode_runner import OpencodeRunner
    from agentic_fx.tools.registry import ToolRegistry

    script = "import json; print(json.dumps({'type': 'step_start', 'sessionID': 'ses_abc'}))"
    launch = _FakeLaunch(script)
    runner = OpencodeRunner(bin_path=Path("/nonexistent/opencode"), model="m", workdir=tmp_path,
                            llama_swap_base_url="http://127.0.0.1:8080/v1", context_limit=4096,
                            mcp_timeout_ms=1000, cli_terminate_grace_sec=1.0,
                            registry=ToolRegistry(), launch=launch)
    runner._transcript_dir = tmp_path / "tr"
    runner.mcp_spool_dir = tmp_path / "spool"
    res = runner.run(Mission(prompt="p", tools=[], output_schema={}, max_turns=1,
                             timeout_sec=600))
    assert res.status == "failed" and "no output recovered" in res.reason
    assert launch.starts == 2  # 追撃の起動は断られ、例外にはならない


def test_factory_passes_launch_and_literal_llama_url(tmp_path):
    from agentic_fx.egress.policy import LLAMA_SWAP_URL
    from agentic_fx.runners.factory import build_runner
    from agentic_fx.tools.registry import ToolRegistry

    s = _settings()
    sentinel = object()
    for backend in ("claude", "codex", "opencode"):
        runner_s = s.runner.model_copy(update={
            "improve": s.runner.improve.model_copy(update={"backend": backend}),
            "opencode": s.runner.opencode.model_copy(update={"context_limit": 4096})})
        settings = s.model_copy(update={"runner": runner_s})
        r = build_runner("improve", settings, ToolRegistry(), workdir=tmp_path, launch=sentinel)
        assert r._launch is sentinel
        if backend == "opencode":
            assert r._llama_swap_base_url == LLAMA_SWAP_URL
        plain = build_runner("improve", settings, ToolRegistry(), workdir=tmp_path)
        assert plain._launch is None
    with pytest.raises(ValueError):
        build_runner("trade", s, ToolRegistry(), workdir=tmp_path, launch=sentinel)


# --- 実 CLI の互換性自己試験 (`-m realbackend` のときだけ。課金枠・llama-swap を使う) -----------

def _real_artifact(backend: str) -> str | None:
    from tests.core import test_cli_subreaper_real_cli as RC

    finder = {"claude": RC._claude, "opencode": RC._opencode}.get(backend)
    if backend == "codex":
        from tests.core.test_cli_launch_pins import _codex_native_elf
        finder = _codex_native_elf
    return finder()


@pytest.mark.realbackend
@pytest.mark.parametrize("backend", ["claude", "codex", "opencode"])
def test_real_cli_selftest(isolation_units, backend):
    """実 CLI を隔離の下で 1 mission (SIGSYS で落ちず、schema の往復、終了後の cgroup の片付け)。"""
    import json
    import sys

    from tests.core import _disposable_unit as du

    artifact = _real_artifact(backend)
    if artifact is None:
        pytest.skip(f"{backend} の実行 artifact が見つからない")
    case = f"real-{backend}"
    out = isolation_units.tmp_path / f"{case}.json"
    command = [sys.executable, str(SERVICE_HELPER), f"real_selftest:{backend}@{artifact}",
               str(out)]
    argv = isolation_units._argv(case, {"RuntimeMaxSec": "900"}, command, wait=True)
    # 実 CLI は実 HOME の認証を WorkerRunner が写して使う
    argv[argv.index(command[0]):argv.index(command[0])] = [f"--setenv=HOME={Path.home()}"]
    isolation_units.started.append(isolation_units.unit_name(case))
    du._run(argv, timeout=960)
    res = json.loads(out.read_text())
    assert "error" not in res, res["error"]
    assert res["outcome"]["ok"] is True, res
    assert res["m_dirs_left"] == [] and res["mission_allowed"] is True
