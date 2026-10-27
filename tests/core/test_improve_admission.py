"""改善 mission の CLI 隔離の admission (判定の順序と固定 reason、cache と再試行、空の対応集合)。"""
from __future__ import annotations

import dataclasses
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_fx.core import improve_admission as IA
from agentic_fx.core import improve_fingerprint as F

PY = os.path.realpath(sys.executable)
KERNEL = F.KernelCapabilities(machine="x86_64", landlock_abi=8, seccomp_missing=(),
                              supervisor_missing=(), yama="1", inet_diag="ok")


class Host:
    """`MissionCgroupHost` の gate だけを持つ代役 (cgroup に触れない)。"""

    def __init__(self, reasons: tuple[str, ...] = ()) -> None:
        self.reasons = list(reasons)

    def mission_allowed(self) -> bool:
        return not self.reasons

    def blocking_reasons(self) -> tuple[str, ...]:
        return tuple(self.reasons)


class Probe:
    def __init__(self, *outcomes: IA.ProbeOutcome) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self, ctx: IA.AdmissionContext) -> IA.ProbeOutcome:
        self.calls += 1
        return self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]


OK = IA.ProbeOutcome(True)


def _admission(canary=None, selftest=None, kernel=KERNEL, **kw) -> IA.ImproveAdmission:
    return IA.ImproveAdmission(canary=canary or Probe(OK), selftest=selftest or Probe(OK),
                               kernel_probe=lambda: kernel, **kw)


def _elf(tmp_path: Path, name: str = "cli", extra: bytes = b"") -> Path:
    package = tmp_path / "package"
    bindir = package / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    p = bindir / name
    data = Path(PY).read_bytes()
    p.write_bytes(data + extra)
    p.chmod(0o755)
    host = bindir / "codex-code-mode-host"
    host.write_bytes(Path(PY).read_bytes())
    host.chmod(0o755)
    (package / "codex-package.json").write_text('{"layoutVersion":1}')
    return p


# --- 判定の順序と固定 reason -----------------------------------------------------------

ORDER_CASES = [
    # (kernel の差し替え, host の reason, 期待する reason)
    ({"machine": "aarch64", "landlock_abi": 6}, ("cgroup_delegation_missing",),
     "arch_unsupported"),
    ({"landlock_abi": 7, "seccomp_missing": ("seccomp_user_notif_unavailable",)}, (),
     "landlock_abi_too_old"),
    ({"seccomp_missing": ("seccomp_wait_killable_unavailable",),
      "supervisor_missing": ("pidfd_thread_unavailable",)}, (),
     "seccomp_wait_killable_unavailable"),
    ({"supervisor_missing": ("pidfd_thread_unavailable", "ptrace_scope_unsupported"),
      "inet_diag": "unavailable: x"}, (), "pidfd_thread_unavailable"),
    ({"supervisor_missing": ("ptrace_scope_unsupported",)}, (), "ptrace_scope_unsupported"),
    ({"inet_diag": "unavailable: x"}, ("cgroup_delegation_missing",), "proxy_start_failed"),
    ({}, ("cgroup_delegation_missing",), "cgroup_delegation_missing"),
    ({}, ("unit_stop_policy_unsupported",), "unit_stop_policy_unsupported"),
    ({}, ("worker_not_reaped",), "worker_not_reaped"),
]


@pytest.mark.parametrize("kernel_update,host_reasons,expected", ORDER_CASES)
def test_admission_order_and_fixed_reasons(kernel_update, host_reasons, expected, tmp_path):
    canary, selftest = Probe(OK), Probe(OK)
    adm = _admission(canary, selftest, kernel=dataclasses.replace(KERNEL, **kernel_update))
    d = adm.admit(backend="codex", artifact_path=str(_elf(tmp_path)), host=Host(host_reasons))
    assert (d.admitted, d.reason) == (False, expected)
    assert canary.calls == 0 and selftest.calls == 0


def test_missing_host_is_topology_unsupported(tmp_path):
    d = _admission().admit(backend="codex", artifact_path=str(_elf(tmp_path)), host=None)
    assert d.reason == "cgroup_topology_unsupported"


def test_wrapper_artifact_is_refused_at_admission(tmp_path):
    """codex の npm wrapper (shebang) は artifact の段で落ち、canary も自己試験も launcher も通らない。"""
    wrapper = tmp_path / "codex"
    wrapper.write_text("#!/usr/bin/env node\nrequire('./bin/codex.js')\n")
    wrapper.chmod(0o755)
    canary, selftest = Probe(OK), Probe(OK)
    d = _admission(canary, selftest).admit(backend="codex", artifact_path=str(wrapper),
                                           host=Host())
    assert (d.admitted, d.reason) == (False, "cli_artifact_unsupported")
    assert "not ELF" in d.detail
    assert canary.calls == 0 and selftest.calls == 0
    missing = _admission().admit(backend="codex", artifact_path=str(tmp_path / "nope"),
                                 host=Host())
    assert missing.reason == "cli_artifact_unsupported"


def test_codex_package_host_is_required_at_admission(tmp_path):
    path = _elf(tmp_path)
    (path.parent / "codex-code-mode-host").unlink()
    d = _admission().admit(backend="codex", artifact_path=str(path), host=Host())
    assert (d.admitted, d.reason) == (False, "cli_artifact_unsupported")
    assert "host open" in d.detail


def test_codex_package_manifest_is_required_at_admission(tmp_path):
    path = _elf(tmp_path)
    (path.parent.parent / "codex-package.json").unlink()
    canary, selftest = Probe(OK), Probe(OK)
    d = _admission(canary, selftest).admit(backend="codex", artifact_path=str(path), host=Host())
    assert (d.admitted, d.reason) == (False, "cli_artifact_unsupported")
    assert "package manifest missing" in d.detail
    assert canary.calls == 0 and selftest.calls == 0


def test_codex_package_host_must_be_an_elf(tmp_path):
    path = _elf(tmp_path)
    host = path.parent / "codex-code-mode-host"
    host.unlink()
    host.write_text("#!/bin/sh\nexit 0\n")
    host.chmod(0o755)
    canary, selftest = Probe(OK), Probe(OK)
    d = _admission(canary, selftest).admit(backend="codex", artifact_path=str(path), host=Host())
    assert (d.admitted, d.reason) == (False, "cli_artifact_unsupported")
    assert "host" in d.detail and "ELF" in d.detail
    assert canary.calls == 0 and selftest.calls == 0


def test_codex_package_identity_is_taken_from_the_inspected_host(tmp_path, monkeypatch):
    """検査した後に host が別 inode に差し替わっても、aggregate は検査した host の値のまま。"""
    path = _elf(tmp_path)
    host = path.parent / "codex-code-mode-host"
    artifact = IA.inspect_artifact(str(path))
    before = F.compute_fingerprint("codex", artifact, KERNEL, artifact_path=str(path)).aggregate
    real = IA.inspect_codex_package

    def inspect_then_swap(p):
        got = real(p)
        replacement = host.with_name("replacement")
        replacement.write_bytes(Path(PY).read_bytes() + b"\0swapped")
        replacement.chmod(0o755)
        os.rename(replacement, host)
        return got

    monkeypatch.setattr(IA, "inspect_codex_package", inspect_then_swap)
    d = _admission().admit(backend="codex", artifact_path=str(path), host=Host())
    after = F.compute_fingerprint("codex", artifact, KERNEL, artifact_path=str(path)).aggregate
    assert after != before
    assert d.admitted and d.fingerprint.aggregate == before


def test_out_of_set_runs_canary_then_selftest_and_admits(tmp_path):
    canary, selftest = Probe(OK), Probe(OK)
    adm = _admission(canary, selftest)
    path = str(_elf(tmp_path))
    d = adm.admit(backend="codex", artifact_path=path, host=Host())
    assert d.admitted and d.reason is None and d.supported is False
    assert d.fingerprint is not None and d.artifact is not None
    assert (canary.calls, selftest.calls) == (1, 1)
    # 成功は process 内で覚える
    again = adm.admit(backend="codex", artifact_path=path, host=Host())
    assert again is d and (canary.calls, selftest.calls) == (1, 1)


def test_canary_failure_blocks_exec(tmp_path):
    canary, selftest = Probe(IA.ProbeOutcome(False, detail="listen_ipv4_eperm")), Probe(OK)
    adm = _admission(canary, selftest)
    d = adm.admit(backend="codex", artifact_path=str(_elf(tmp_path)), host=Host())
    assert (d.admitted, d.reason) == (False, "safety_canary_failed")
    assert selftest.calls == 0  # canary を通らなければ実 CLI も起こさない
    choice = IA.choose_for_mission(_settings("codex", _elf(tmp_path, "c2")), tmp_path,
                                   enabled=True, host=Host(), admission=_admission(canary))
    assert choice.refusal == "safety_canary_failed" and choice.worker_options == {}
    res = IA.RefusedRunner(choice.refusal).run(None)
    assert (res.status, res.reason) == ("failed", "sandbox_unavailable: safety_canary_failed")


def test_probe_exception_is_a_failure(tmp_path):
    def boom(ctx):
        raise RuntimeError("x")

    d = _admission(boom).admit(backend="codex", artifact_path=str(_elf(tmp_path)), host=Host())
    assert (d.admitted, d.reason, d.retryable) == (False, "safety_canary_failed", False)


def test_temporary_selftest_failure_is_retried(tmp_path):
    now = [1000.0]
    selftest = Probe(IA.ProbeOutcome(False, transient=True, detail="timeout"), OK)
    adm = _admission(selftest=selftest, clock=lambda: now[0])
    path = str(_elf(tmp_path))
    d1 = adm.admit(backend="codex", artifact_path=path, host=Host())
    assert (d1.admitted, d1.reason, d1.retryable) == (False, "runtime_fingerprint_selftest_failed",
                                                      True)
    # 間隔の内側は cache を返す (試し直さない)
    now[0] += IA.SELFTEST_RETRY_INTERVAL_SEC - 1
    assert adm.admit(backend="codex", artifact_path=path, host=Host()) is d1
    assert selftest.calls == 1
    # 間隔の後に試し直す
    now[0] += 2
    d2 = adm.admit(backend="codex", artifact_path=path, host=Host())
    assert d2.admitted and selftest.calls == 2


def test_permanent_failure_is_cached(tmp_path):
    now = [0.0]
    selftest = Probe(IA.ProbeOutcome(False, detail="schema"), OK)
    adm = _admission(selftest=selftest, clock=lambda: now[0])
    path = str(_elf(tmp_path))
    d1 = adm.admit(backend="codex", artifact_path=path, host=Host())
    now[0] += 10 * IA.SELFTEST_RETRY_INTERVAL_SEC
    assert adm.admit(backend="codex", artifact_path=path, host=Host()) is d1
    assert selftest.calls == 1 and d1.retryable is False


def test_artifact_mutation_reevaluates(tmp_path):
    """artifact の内容か mtime が変われば fingerprint が変わり、cache を使わずに判定し直す。"""
    canary = Probe(OK)
    adm = _admission(canary)
    p = _elf(tmp_path)
    d1 = adm.admit(backend="codex", artifact_path=str(p), host=Host())
    st = os.stat(p)
    with open(p, "r+b") as f:
        f.seek(-1, os.SEEK_END)
        last = f.read(1)
        f.seek(-1, os.SEEK_END)
        f.write(bytes([last[0] ^ 1]))
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    d2 = adm.admit(backend="codex", artifact_path=str(p), host=Host())
    assert d2.fingerprint.aggregate != d1.fingerprint.aggregate and canary.calls == 2
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5000))
    d3 = adm.admit(backend="codex", artifact_path=str(p), host=Host())
    assert d3.fingerprint.aggregate != d2.fingerprint.aggregate and canary.calls == 3
    assert adm.evaluations == {d.fingerprint.aggregate: 1 for d in (d1, d2, d3)}


def test_failed_fingerprint_is_not_registered(tmp_path):
    """canary / 自己試験の失敗は対応集合に入らず、許可としても覚えない。"""
    for out in (IA.ProbeOutcome(False, detail="EAGAIN"), IA.ProbeOutcome(False, detail="503")):
        adm = _admission(selftest=Probe(out))
        path = str(_elf(tmp_path))
        d = adm.admit(backend="codex", artifact_path=path, host=Host())
        assert not d.admitted
        assert not adm.admit(backend="codex", artifact_path=path, host=Host()).admitted
        assert d.fingerprint.aggregate not in F.SUPPORTED_FINGERPRINTS
        assert not F.is_supported(d.fingerprint)


def test_supported_fingerprint_skips_probes(tmp_path):
    canary = Probe(OK)
    path = str(_elf(tmp_path))
    first = _admission().admit(backend="codex", artifact_path=path, host=Host())
    adm = _admission(canary, supported=lambda: frozenset({first.fingerprint.aggregate}))
    d = adm.admit(backend="codex", artifact_path=path, host=Host())
    assert d.admitted and d.supported and canary.calls == 0


def test_admission_has_no_registration_path():
    public = {n for n in dir(IA.ImproveAdmission) if not n.startswith("_")}
    assert public == {"admit", "kernel"}
    assert sorted(IA.__all__) == sorted([
        "AdmissionContext", "AdmissionDecision", "ISOLATION_ENABLED", "ImproveAdmission",
        "MissionChoice", "ProbeOutcome", "REASON_ABI", "REASON_ARCH", "REASON_ARTIFACT",
        "REASON_CANARY", "REASON_PROXY_START", "REASON_SELFTEST", "REASON_STOPPING",
        "REASON_TOPOLOGY",
        "RefusedRunner", "SELFTEST_RETRY_INTERVAL_SEC", "artifact_path", "choose_for_mission",
        "cleanup_hook", "current_host", "default_admission", "inspect_artifact",
        "preflight_reason", "startup", "worker_runner_options"])


# --- afx 側の配線 -------------------------------------------------------------------

def _settings(backend: str, artifact: Path):
    from agentic_fx.config import load_settings

    s = load_settings(Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")
    runner = s.runner.model_copy(update={
        "improve": s.runner.improve.model_copy(update={"backend": backend}),
        backend: getattr(s.runner, backend).model_copy(update={"bin": str(artifact)})})
    return s.model_copy(update={"runner": runner})


def test_choose_disabled_or_local_is_the_current_path(tmp_path):
    s = _settings("codex", _elf(tmp_path))
    boom = Probe()
    assert IA.choose_for_mission(s, tmp_path, enabled=False, host=Host(),
                                 admission=_admission(boom)) == IA.MissionChoice()
    local = s.model_copy(update={"runner": s.runner.model_copy(update={
        "improve": s.runner.improve.model_copy(update={"backend": "local"})})})
    assert IA.choose_for_mission(local, tmp_path, enabled=True, host=Host()) == IA.MissionChoice()
    assert boom.calls == 0


def test_choose_admitted_builds_worker_options(tmp_path):
    from agentic_fx.core import improve_isolation as I
    from agentic_fx.core import mission_cleanup as MC

    s = _settings("codex", _elf(tmp_path))
    choice = IA.choose_for_mission(s, tmp_path, enabled=True, host=Host(),
                                   admission=_admission())
    assert choice.refusal is None
    assert isinstance(choice.worker_options["cleanup_protocol"], MC.CleanupProtocol)
    spec = I.parse_isolation_field(choice.worker_options["isolation"])
    assert spec.kind == "mission" and spec.backend == "codex"
    assert spec.artifact == choice.decision.artifact
    assert spec.fingerprint == choice.decision.fingerprint.aggregate


def test_cleanup_hook_rmdirs_or_closes_gate():
    calls = []

    class H:
        def cleanup_mission(self, name, deadline):
            calls.append(("cleanup", name))
            return None

        def report_worker_not_reaped(self):
            calls.append(("gate",))

    hook = IA.cleanup_hook(H())
    hook("m-" + "a" * 32, True, 1.0)
    hook("m-" + "b" * 32, False, 1.0)
    assert calls == [("cleanup", "m-" + "a" * 32), ("gate",)]


def test_disabled_startup_reads_only(tmp_path):
    """明示的に無効化した起動時は host を起こさず、読むだけの診断を返す。"""
    from agentic_fx.core import unit_admission as ua

    before = Path("/proc/self/cgroup").read_text()

    def no_host():
        raise AssertionError("MissionCgroupHost must not be created while disabled")

    def deny():
        raise ua.AdmissionError("cgroup_delegation_missing", "screen wrapper")

    line = IA.startup(_settings("codex", _elf(tmp_path)), tmp_path, deadline=0.0,
                      enabled=False, host_factory=no_host, admit_unit=deny,
                      kernel_probe=lambda: KERNEL)
    assert line.startswith("improve isolation: disabled")
    assert "unit_admission=cgroup_delegation_missing" in line and "kernel=ok" in line
    assert Path("/proc/self/cgroup").read_text() == before
    assert IA.current_host() is None
    bad = IA.startup(_settings("codex", _elf(tmp_path)), tmp_path, deadline=0.0,
                     enabled=False, host_factory=no_host, admit_unit=lambda: None,
                     kernel_probe=lambda: dataclasses.replace(KERNEL, landlock_abi=6))
    assert "unit_admission=ok" in bad and "kernel=landlock_abi_too_old" in bad


@pytest.mark.parametrize(("choice", "expected"), [
    (IA.MissionChoice(decision=IA.AdmissionDecision(
        True, None, "codex", fingerprint=SimpleNamespace(aggregate="admitted-fingerprint"))),
     "improve isolation: enabled; backend=codex admitted=yes fingerprint=admitted-fingerprint"),
    (IA.MissionChoice(refusal="safety_canary_failed", decision=IA.AdmissionDecision(
        False, "safety_canary_failed", "codex",
        fingerprint=SimpleNamespace(aggregate="refused-fingerprint"))),
     "improve isolation: enabled; backend=codex admitted=no reason=safety_canary_failed "
     "fingerprint=refused-fingerprint"),
    (IA.MissionChoice(refusal="cgroup_delegation_missing"),
     "improve isolation: enabled; backend=codex admitted=no reason=cgroup_delegation_missing "
     "fingerprint=none"),
])
def test_enabled_startup_reports_the_decision_fingerprint(tmp_path, monkeypatch, choice, expected):
    """起動時の admission 行は判定済み aggregate、未作成なら none を末尾に出す。"""
    class StartupHost(Host):
        def start(self, *, deadline):
            assert deadline == 0.0

    monkeypatch.setattr(IA, "default_admission", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(IA, "choose_for_mission", lambda *_args, **_kwargs: choice)
    monkeypatch.setattr(IA, "_HOST", None)

    assert IA.startup(_settings("codex", _elf(tmp_path)), tmp_path, deadline=0.0,
                      enabled=True, host_factory=StartupHost) == expected


@pytest.mark.parametrize("retryable", [False, True])
def test_startup_emits_a_redacted_single_line_selftest_detail(tmp_path, monkeypatch, retryable):
    """自己試験の診断だけを技術ログの別行へ出し、admission ABI は変えない。"""
    class StartupHost(Host):
        def start(self, *, deadline):
            assert deadline == 0.0

    detail = "left\x01right\nstderr token=TOPSECRET9 " + "x" * 240
    choice = IA.MissionChoice(
        refusal=IA.REASON_SELFTEST,
        decision=IA.AdmissionDecision(
            False, IA.REASON_SELFTEST, "codex",
            fingerprint=SimpleNamespace(aggregate="refused-fingerprint"),
            retryable=retryable, detail=detail, selftest_detail=detail),
    )
    emitted: list[str] = []
    monkeypatch.setattr(IA, "default_admission", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(IA, "choose_for_mission", lambda *_args, **_kwargs: choice)
    monkeypatch.setattr(IA, "_HOST", None)

    line = IA.startup(_settings("codex", _elf(tmp_path)), tmp_path, deadline=0.0,
                      enabled=True, host_factory=StartupHost, technical_log=emitted.append)

    assert line == ("improve isolation: enabled; backend=codex admitted=no "
                    "reason=runtime_fingerprint_selftest_failed fingerprint=refused-fingerprint")
    assert len(emitted) == 1
    prefix = "improve isolation: selftest detail: "
    assert emitted[0].startswith(prefix)
    logged_detail = emitted[0][len(prefix):]
    assert len(logged_detail) == 200
    assert logged_detail.startswith("left right stderr token=[redacted] ")
    assert "TOPSECRET9" not in logged_detail and "[redacted]" in logged_detail
    assert all(not ch.isspace() or ch == " " for ch in logged_detail)
    assert all(ord(ch) >= 32 for ch in logged_detail)


def test_selftest_failure_keeps_its_detail_in_the_dedicated_field(tmp_path):
    detail = "mission did not complete: status=failed reason=timeout"
    decision = _admission(selftest=Probe(IA.ProbeOutcome(False, detail=detail))).admit(
        backend="codex", artifact_path=str(_elf(tmp_path)), host=Host())

    assert decision.detail == detail
    assert decision.selftest_detail == detail


def test_startup_does_not_emit_a_selftest_detail_when_admitted(tmp_path, monkeypatch):
    class StartupHost(Host):
        def start(self, *, deadline):
            assert deadline == 0.0

    choice = IA.MissionChoice(decision=IA.AdmissionDecision(
        True, None, "codex", fingerprint=SimpleNamespace(aggregate="admitted-fingerprint")))
    emitted: list[str] = []
    monkeypatch.setattr(IA, "default_admission", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(IA, "choose_for_mission", lambda *_args, **_kwargs: choice)
    monkeypatch.setattr(IA, "_HOST", None)

    line = IA.startup(_settings("codex", _elf(tmp_path)), tmp_path, deadline=0.0,
                      enabled=True, host_factory=StartupHost, technical_log=emitted.append)

    assert line == ("improve isolation: enabled; backend=codex admitted=yes "
                    "fingerprint=admitted-fingerprint")
    assert emitted == []


def test_service_writes_the_selftest_detail_only_to_technical_logs(tmp_path, monkeypatch, caplog):
    import logging

    from agentic_fx import service

    detail_line = "improve isolation: selftest detail: [redacted]"
    admission_line = ("improve isolation: enabled; backend=codex admitted=no "
                      "reason=runtime_fingerprint_selftest_failed fingerprint=fingerprint")

    def startup(*_args, technical_log, **_kwargs):
        technical_log(detail_line)
        return admission_line

    monkeypatch.setattr(IA, "startup", startup)
    with caplog.at_level(logging.INFO, logger="agentic_fx.service"):
        service._log_improve_isolation_diagnostics(object(), tmp_path)

    assert [record.getMessage() for record in caplog.records] == [detail_line, admission_line]
    assert not (tmp_path / "logs" / "activity.log").exists()


def test_selftest_exception_is_a_failure(tmp_path):
    """自己試験の例外は通さない (canary と同じく失敗として固定 reason に入る)。"""
    def boom(ctx):
        raise RuntimeError("x")

    d = _admission(selftest=boom).admit(backend="codex", artifact_path=str(_elf(tmp_path)),
                                        host=Host())
    assert (d.admitted, d.reason, d.retryable) == (False, "runtime_fingerprint_selftest_failed",
                                                   False)


def test_cache_key_separates_backends(tmp_path, monkeypatch):
    """aggregate が同じでも backend が違えば cache を共有せず、backend ごとに判定し直す。"""
    real = F.compute_fingerprint
    monkeypatch.setattr(F, "compute_fingerprint",
                        lambda backend, artifact, kernel, **kw: real("codex", artifact, kernel))
    selftest = Probe(IA.ProbeOutcome(False, detail="schema"), OK)
    adm = _admission(selftest=selftest)
    path = str(_elf(tmp_path))
    d1 = adm.admit(backend="codex", artifact_path=path, host=Host())
    d2 = adm.admit(backend="claude", artifact_path=path, host=Host())
    assert d1.fingerprint.aggregate == d2.fingerprint.aggregate
    assert d2.backend == "claude" and selftest.calls == 2


def test_closed_gate_never_consults_admission(tmp_path):
    """gate が閉じていれば admission (kernel の能力・canary・自己試験) を問わずに refusal する。"""
    class NoAdmission:
        def admit(self, **kw):
            raise AssertionError("gate 閉では admission を問わない")

    choice = IA.choose_for_mission(_settings("codex", _elf(tmp_path)), tmp_path, enabled=True,
                                   host=Host(("cgroup_delegation_missing",)),
                                   admission=NoAdmission())
    assert choice.refusal == "cgroup_delegation_missing" and choice.worker_options == {}


def test_canary_uses_the_settings_of_each_call(tmp_path, monkeypatch):
    """process に 1 個の admission でも、canary と自己試験は判定を求めた呼び出しの settings で組む
    (最初に作ったときの backend を閉じ込めない)。"""
    from agentic_fx.core import improve_canary

    seen: list[tuple[str, str]] = []

    def fake(kind):
        def run(ctx, *, settings, root):
            seen.append((kind, settings.runner.improve.backend, ctx.backend))
            return OK
        return run

    monkeypatch.setattr(improve_canary, "run_canary", fake("canary"))
    monkeypatch.setattr(improve_canary, "run_selftest", fake("selftest"))
    monkeypatch.setattr(IA, "_ADMISSION", None)
    adm = IA.default_admission(tmp_path)
    adm._kernel_probe = lambda: KERNEL
    cli = _elf(tmp_path)
    first = IA.choose_for_mission(_settings("claude", cli), tmp_path, enabled=True, host=Host())
    second = IA.choose_for_mission(_settings("codex", cli), tmp_path, enabled=True, host=Host())
    assert IA.default_admission(tmp_path) is adm
    assert first.refusal is None and second.refusal is None
    assert seen == [("canary", "claude", "claude"), ("selftest", "claude", "claude"),
                    ("canary", "codex", "codex"), ("selftest", "codex", "codex")]


class _Blocking:
    """`backend` の canary だけを `release` まで止める probe。"""

    def __init__(self, backend: str) -> None:
        import threading

        self.backend = backend
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def __call__(self, ctx):
        self.calls += 1
        if ctx.backend == self.backend:
            self.entered.set()
            assert self.release.wait(20)
        return OK


def test_canary_runs_outside_the_lock(tmp_path):
    """canary の間も kernel() と別 fingerprint の判定は待たされない。"""
    import threading
    import time

    canary = _Blocking("codex")
    adm = _admission(canary)
    path = str(_elf(tmp_path))
    out: list = []
    t = threading.Thread(target=lambda: out.append(
        adm.admit(backend="codex", artifact_path=path, host=Host())))
    t.start()
    try:
        assert canary.entered.wait(10)
        t0 = time.monotonic()
        assert adm.kernel() == KERNEL
        other = adm.admit(backend="claude", artifact_path=path, host=Host())
        assert time.monotonic() - t0 < 5
        assert other.admitted and other.backend == "claude"
        assert t.is_alive()
    finally:
        canary.release.set()
        t.join(10)
    assert out and out[0].admitted


def test_same_fingerprint_waits_and_stop_ends_the_wait(tmp_path):
    """同じ fingerprint の判定は進行中の判定を待ち (canary を二重に走らせない)、afx の停止で
    待ちをやめる。先行者も次の試験に進まない (停止の refusal は覚えない)。"""
    import threading
    import time

    canary = _Blocking("codex")
    stop = threading.Event()
    adm = _admission(canary, stop_event=stop)
    path = str(_elf(tmp_path))
    first: list = []
    waiter: list = []
    t1 = threading.Thread(target=lambda: first.append(
        adm.admit(backend="codex", artifact_path=path, host=Host())))
    t1.start()
    try:
        assert canary.entered.wait(10)
        t2 = threading.Thread(target=lambda: waiter.append(
            adm.admit(backend="codex", artifact_path=path, host=Host())))
        t2.start()
        time.sleep(0.5)
        assert t2.is_alive() and canary.calls == 1
        t0 = time.monotonic()
        stop.set()
        t2.join(5)
        assert not t2.is_alive() and time.monotonic() - t0 < 2
        stopped = waiter[0]
        assert (stopped.admitted, stopped.reason, stopped.retryable) == (
            False, "service_stopping", True)
    finally:
        canary.release.set()
        t1.join(10)
    # 先行者も停止を見て自己試験に進まず、停止の判定を返す (覚えない)
    assert (first[0].admitted, first[0].reason, first[0].retryable) == (
        False, "service_stopping", True)
    assert canary.calls == 1
    stop.clear()
    again = adm.admit(backend="codex", artifact_path=path, host=Host())
    assert again.admitted and canary.calls == 2


def test_same_fingerprint_waiter_reads_the_finished_decision(tmp_path):
    import threading

    canary = _Blocking("codex")
    adm = _admission(canary)
    path = str(_elf(tmp_path))
    out: list = []
    threads = [threading.Thread(target=lambda: out.append(
        adm.admit(backend="codex", artifact_path=path, host=Host()))) for _ in range(3)]
    for t in threads:
        t.start()
    assert canary.entered.wait(10)
    canary.release.set()
    for t in threads:
        t.join(10)
    assert len(out) == 3 and all(d is out[0] for d in out)
    assert canary.calls == 1 and list(adm.evaluations.values()) == [1]


def test_retryable_failure_is_cached_from_its_completion(tmp_path):
    """自己試験が再試行の間隔より長くかかって一時失敗しても、判定は終わった時刻から覚える
    (直後の admit は試し直さない)。"""
    now = [1000.0]

    def selftest(ctx):
        selftest.calls += 1
        now[0] += IA.SELFTEST_RETRY_INTERVAL_SEC + 30
        return IA.ProbeOutcome(False, transient=True, detail="timeout")

    selftest.calls = 0
    adm = _admission(selftest=selftest, clock=lambda: now[0])
    path = str(_elf(tmp_path))
    d1 = adm.admit(backend="codex", artifact_path=path, host=Host())
    assert (d1.reason, d1.retryable) == ("runtime_fingerprint_selftest_failed", True)
    now[0] += 1
    assert adm.admit(backend="codex", artifact_path=path, host=Host()) is d1
    assert selftest.calls == 1


def _stopping(d) -> bool:
    return (d.admitted, d.reason, d.retryable) == (False, "service_stopping", True)


def test_stop_wins_over_an_admitted_cache(tmp_path):
    """停止の後は覚えた admitted も返さず service_stopping (停止の判定は覚えない)。"""
    import threading

    stop = threading.Event()
    canary = Probe(OK)
    adm = _admission(canary, stop_event=stop)
    path = str(_elf(tmp_path))
    first = adm.admit(backend="codex", artifact_path=path, host=Host())
    assert first.admitted
    stop.set()
    assert _stopping(adm.admit(backend="codex", artifact_path=path, host=Host()))
    stop.clear()
    assert adm.admit(backend="codex", artifact_path=path, host=Host()) is first
    assert canary.calls == 1


def test_stop_before_leading_runs_no_probe(tmp_path):
    import threading

    stop = threading.Event()
    stop.set()
    canary, selftest = Probe(OK), Probe(OK)
    adm = _admission(canary, selftest, stop_event=stop)
    d = adm.admit(backend="codex", artifact_path=str(_elf(tmp_path)), host=Host())
    assert _stopping(d)
    assert (canary.calls, selftest.calls) == (0, 0)


def test_stop_during_canary_skips_the_selftest_and_is_not_cached(tmp_path):
    import threading

    stop = threading.Event()

    def canary(ctx):
        canary.calls += 1
        if canary.calls == 1:
            stop.set()
        return OK

    canary.calls = 0
    selftest = Probe(OK)
    adm = _admission(canary, selftest, stop_event=stop)
    path = str(_elf(tmp_path))
    assert _stopping(adm.admit(backend="codex", artifact_path=path, host=Host()))
    assert (canary.calls, selftest.calls) == (1, 0)
    # 停止が解けたら判定をやり直す (停止の結果は残っていない)
    stop.clear()
    d = adm.admit(backend="codex", artifact_path=path, host=Host())
    assert d.admitted and (canary.calls, selftest.calls) == (2, 1)


def test_decision_finished_after_stop_is_not_cached(tmp_path):
    """先行者の判定が停止の後に終わったら、その判定は返さず覚えない。"""
    import threading

    stop = threading.Event()

    def selftest(ctx):
        selftest.calls += 1
        if selftest.calls == 1:
            stop.set()
        return OK

    selftest.calls = 0
    canary = Probe(OK)
    adm = _admission(canary, selftest, stop_event=stop)
    path = str(_elf(tmp_path))
    assert _stopping(adm.admit(backend="codex", artifact_path=path, host=Host()))
    stop.clear()
    d = adm.admit(backend="codex", artifact_path=path, host=Host())
    assert d.admitted and (canary.calls, selftest.calls) == (2, 2)


def test_stop_check_and_cache_write_share_the_lock(tmp_path):
    """判定の後の停止の確認と cache の書込みは同じ lock の区間で行う。停止が lock を取る瞬間に
    届いても、その判定は返さず覚えない (確認と書込みの間に停止が入る隙間が無い)。"""
    import threading

    stop = threading.Event()

    class StopOnAcquire:
        """armed のとき、取った瞬間に停止を立てる lock。"""

        def __init__(self) -> None:
            self.inner = threading.Lock()
            self.armed = False

        def __enter__(self):
            self.inner.acquire()
            if self.armed:
                self.armed = False
                stop.set()
            return self

        def __exit__(self, *a):
            self.inner.release()

    lock = StopOnAcquire()

    def selftest(ctx):
        selftest.calls += 1
        if selftest.calls == 1:
            lock.armed = True  # 次に lock を取るのは判定の後の区間
        return OK

    selftest.calls = 0
    canary = Probe(OK)
    adm = _admission(canary, selftest, stop_event=stop)
    adm._lock = lock
    path = str(_elf(tmp_path))
    assert _stopping(adm.admit(backend="codex", artifact_path=path, host=Host()))
    assert adm._cache == {} and adm._running == {}
    stop.clear()
    d = adm.admit(backend="codex", artifact_path=path, host=Host())
    assert d.admitted and (canary.calls, selftest.calls) == (2, 2)


def test_running_event_is_released_when_the_cache_write_fails(tmp_path):
    """判定の後の区間で例外が出ても、進行中の合図は必ず立てて外す (待ち手を残さない)。"""
    adm = _admission()
    path = str(_elf(tmp_path))
    seen: list = []

    class BrokenCache(dict):
        def __setitem__(self, key, value):
            seen.append(adm._running.get(key))
            raise RuntimeError("injected")

    adm._cache = BrokenCache()
    with pytest.raises(RuntimeError):
        adm.admit(backend="codex", artifact_path=path, host=Host())
    assert len(seen) == 1 and seen[0] is not None
    assert seen[0].is_set() and adm._running == {}
