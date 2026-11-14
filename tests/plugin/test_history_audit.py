"""非配備の旧承認版を、配備せずに現在の条件で評価する入口。

実 sqlite・実 plugins dir・実の共有 gate を使い、gate の隔離実行 (pytest・replay) だけを
差し替える。成績は DB に書かず report だけに残り、warn の完走は合格にならない。"""
from __future__ import annotations

import json
import os

import pytest

from agentic_fx.backtest import history_health as hh
from agentic_fx.backtest.history_migration import UNEVALUABLE_REASONS_V2
from agentic_fx.plugin import history_audit as audit
from agentic_fx.plugin import loader, switch
from agentic_fx.store import backtest_runs as backtest_runs_store
from tests.plugin.audit_world import NAME, TUESDAY, AuditWorld, Gate, Replay, never_block
from tests.plugin.test_strategy_gate import _metrics


pytestmark = pytest.mark.real_history_guard      # 履歴検査を差し替えず実 sqlite の履歴で通す


@pytest.fixture
def make(tmp_path, monkeypatch):
    def factory(**kwargs):
        return AuditWorld(tmp_path, monkeypatch, **kwargs)
    return factory


def _result(world, target, **kwargs):
    before = world.counts()
    result = world.run(target, **kwargs)
    assert world.counts() == before
    return result


# ---- 4 軸と総合結果 ----------------------------------------------------------------

def test_a_healthy_version_passes_with_every_scope_and_a_verified_ready_evidence(make):
    world = make()
    _version, target = world.stored()
    result = _result(world, target)
    report = result.report
    assert report["result"] == {"overall": "pass", "reason": None, "result_status": "evaluated"}
    assert report["axes"]["execution"]["status"] == "completed"
    assert report["axes"]["floor"]["in_sample"]["result"] == "pass"
    assert report["axes"]["floor"]["holdout"]["result"] == "pass"
    assert report["axes"]["history"]["result"] == "ready_verified"
    assert report["axes"]["history"]["evidence"]["verdict"] == "ready"
    assert report["axes"]["unexecuted"] == []
    assert world.gate.calls == 1
    assert sorted(world.replay.calls) == [("holdout_gate", "USDJPY"), ("in_sample", "USDJPY")]
    assert report["evaluation"]["mode"] == {"floor_mode": "warn", "compare_mode": "off"}
    assert [r["scope"] for r in report["rows"]] == ["in_sample", "holdout"]
    assert all(r["db_run_id"] is None for r in report["rows"])
    on_disk = json.loads(result.report_path.read_text())
    assert on_disk == report


def test_a_completed_warn_with_an_in_sample_floor_failure_is_a_fail_not_a_pass(make):
    world = make(replay=Replay(in_sample=lambda pair: _metrics(trades=40, pf=0.5, avg_r=-0.1)))
    _version, target = world.stored()
    report = _result(world, target).report
    assert report["result"]["overall"] == "fail" and report["result"]["reason"] == "floor"
    assert report["axes"]["execution"]["status"] == "completed"
    floor = report["axes"]["floor"]
    assert floor["in_sample"]["result"] == "fail"
    assert floor["in_sample"]["pairs"]["USDJPY"]["reasons"] == ["pf"]
    # in_sample の不合格でも holdout まで進み、その成績と合否も残る
    assert floor["holdout"]["result"] == "pass"
    assert ("holdout_gate", "USDJPY") in world.replay.calls
    assert floor["holdout"]["pairs"]["USDJPY"]["metrics"]["pf"] == 1.5


def test_a_holdout_floor_failure_and_both_failures_are_each_recorded(make):
    bad = lambda pair: _metrics(trades=40, pf=0.5, avg_r=-0.1)  # noqa: E731
    holdout_only = make(replay=Replay(holdout=bad))
    _v, target = holdout_only.stored()
    report = holdout_only.run(target, clock=lambda: TUESDAY).report
    assert report["result"]["overall"] == "fail"
    assert report["axes"]["floor"]["in_sample"]["result"] == "pass"
    assert report["axes"]["floor"]["holdout"]["result"] == "fail"


def test_both_scope_failures_are_recorded_separately(make):
    bad = lambda pair: _metrics(trades=40, pf=0.5, avg_r=-0.1)  # noqa: E731
    world = make(replay=Replay(in_sample=bad, holdout=bad))
    _v, target = world.stored()
    floor = world.run(target, clock=lambda: TUESDAY).report["axes"]["floor"]
    assert (floor["in_sample"]["result"], floor["holdout"]["result"]) == ("fail", "fail")


def test_a_stale_history_is_unevaluable_and_leaves_holdout_unexecuted(make):
    world = make(history="stale")
    _v, target = world.stored()
    report = _result(world, target).report
    assert report["result"]["overall"] == "unevaluable"
    assert report["result"]["reason"] == hh.HISTORY_STALE
    assert report["axes"]["history"]["result"] == "failed"
    assert report["axes"]["history"]["evidence"] is None
    assert report["axes"]["floor"]["in_sample"]["result"] == "pass"
    assert report["axes"]["floor"]["holdout"]["result"] == "not_run"
    assert {"item": "holdout:USDJPY", "reason": hh.HISTORY_STALE} in report["axes"]["unexecuted"]
    assert not any(c[0] == "holdout_gate" for c in world.replay.calls)


def test_insufficient_trades_is_unevaluable(make):
    world = make(replay=Replay(in_sample=lambda pair: _metrics(trades=2, pf=1.5)))
    _v, target = world.stored()
    report = _result(world, target).report
    assert (report["result"]["overall"], report["result"]["reason"]) == (
        "unevaluable", "insufficient_trades")
    assert report["axes"]["floor"]["holdout"]["result"] == "not_run"


def test_a_failing_stored_test_is_a_normal_fail_and_floor_and_history_stay_unexecuted(make):
    world = make(gate=Gate(passed=False, returncode=1))
    _v, target = world.stored()
    report = _result(world, target).report
    assert report["result"] == {"overall": "fail", "reason": "pytest",
                                "result_status": "evaluated"}
    assert report["axes"]["execution"] == {"status": "completed", "stage": "pytest"}
    assert report["axes"]["history"]["result"] == "not_run"
    assert report["axes"]["floor"]["in_sample"]["result"] == "not_run"
    assert {"item": "floor_and_history", "reason": "pytest_stopped"} in report["axes"]["unexecuted"]
    assert world.replay.calls == []


@pytest.mark.parametrize("returncode,reason,status", [
    (-9, "resource_unavailable", "interrupted"),      # SIGKILL
    (-24, "resource_unavailable", "interrupted"),     # a negative code from a resource limit
    (2, "sandbox_unavailable", "interrupted"),        # pytest interrupted
    (3, "sandbox_unavailable", "interrupted"),        # pytest internal error
    (4, "sandbox_unavailable", "interrupted"),        # usage error / launcher failure
    (5, "sandbox_unavailable", "interrupted"),        # nothing collected
    (0, "changed_during_evaluation", "completed"),    # not passed with a clean exit: hash change
])
def test_a_pytest_stage_failure_that_is_not_an_ordinary_test_failure_is_unevaluable(
        make, returncode, reason, status):
    world = make(gate=Gate(passed=False, returncode=returncode))
    _v, target = world.stored()
    report = _result(world, target).report
    assert report["result"] == {"overall": "unevaluable", "reason": reason,
                                "result_status": "unevaluable"}
    assert report["axes"]["execution"]["status"] == status


def test_a_stored_version_changed_by_its_own_test_run_is_unevaluable_not_a_fail(make):
    class Mutating(Gate):
        def __call__(self, plugin_dir, *, settings):
            (plugin_dir / "plugin.py").write_bytes(
                (plugin_dir / "plugin.py").read_bytes() + b"\n# edited\n")
            return super().__call__(plugin_dir, settings=settings)

    world = make(gate=Mutating(passed=False, returncode=1))
    _v, target = world.stored()
    report = _result(world, target).report
    assert (report["result"]["overall"], report["result"]["reason"]) == (
        "unevaluable", "changed_during_evaluation")


def test_a_snapshot_error_in_the_pytest_stage_is_invalid_snapshot_and_completed(make):
    from agentic_fx.plugin.gate_pytest import CandidateSnapshotError
    world = make()
    _v, target = world.stored()

    def raising(plugin_dir, *, settings):
        raise CandidateSnapshotError("extra entry")

    import agentic_fx.plugin.switch as sw
    sw_run = sw.run_gate_pytest
    try:
        sw.run_gate_pytest = raising
        report = _result(world, target).report
    finally:
        sw.run_gate_pytest = sw_run
    assert (report["result"]["overall"], report["result"]["reason"]) == (
        "unevaluable", "invalid_snapshot")
    assert report["axes"]["execution"]["status"] == "completed"


def test_a_pytest_timeout_is_unevaluable_and_counts_as_an_aborted_run(make):
    world = make(gate=Gate(passed=False, returncode=-1))
    _v, target = world.stored()
    report = _result(world, target).report
    assert (report["result"]["overall"], report["result"]["reason"]) == ("unevaluable", "timeout")
    assert report["axes"]["execution"]["status"] == "interrupted"


def test_an_unusable_sandbox_is_unevaluable(make, monkeypatch):
    world = make()
    _v, target = world.stored()
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest",
                        lambda d, *, settings: (_ for _ in ()).throw(RuntimeError("no landlock")))
    report = _result(world, target).report
    assert report["result"]["reason"] == "sandbox_unavailable"
    assert report["axes"]["execution"]["status"] == "interrupted"


def test_an_unexpected_exception_is_unevaluable_failed_and_nothing_else(make):
    world = make()
    _v, target = world.stored()

    def broken(*a, **kw):
        raise ZeroDivisionError("secret detail")

    report = _result(world, target, gate_fn=broken).report
    assert (report["result"]["overall"], report["result"]["reason"]) == ("unevaluable", "failed")
    assert "secret detail" not in json.dumps(report)


def test_a_holdout_that_is_not_measured_never_becomes_a_pass(make, monkeypatch):
    """gate の成績の行が欠けた (測定できなかった) scope は、成功に数えない。"""
    world = make()
    _v, target = world.stored()
    real = switch._run_full_gate

    def drop_holdout(*a, **kw):
        meta, content, artifact, outcome = real(*a, **kw)
        outcome.gate_rows[:] = [r for r in outcome.gate_rows if r["scope"] == "in_sample"]
        return meta, content, artifact, outcome

    report = _result(world, target, gate_fn=drop_holdout).report
    assert report["result"]["overall"] == "unevaluable"
    assert report["axes"]["floor"]["holdout"]["result"] == "not_run"


@pytest.mark.parametrize("strict", [False, True], ids=["default", "strict"])
@pytest.mark.parametrize("in_sample_ok", [True, False], ids=["in-sample-ok", "in-sample-floor-fail"])
def test_a_short_holdout_sample_is_unevaluable_never_a_pass_or_a_floor_fail(
        make, strict, in_sample_ok):
    """in_sample は十分で holdout の標本が足りない版は、既定でも strict でも評価不能。"""
    in_sample = ((lambda pair: _metrics(trades=40, pf=1.5)) if in_sample_ok
                 else (lambda pair: _metrics(trades=40, pf=0.5, avg_r=-0.1)))
    world = make(replay=Replay(
        in_sample=in_sample,
        holdout=lambda pair: _metrics(trades=3, pf=1.5, evaluable=False)))
    world.settings.improve.gate.require_holdout_evaluable = strict
    _v, target = world.stored()
    report = _result(world, target).report
    assert report["result"] == {"overall": "unevaluable", "reason": "insufficient_trades",
                                "result_status": "unevaluable"}
    assert report["axes"]["execution"]["status"] == "completed"
    # 部分成績は残る
    assert report["axes"]["floor"]["in_sample"]["pairs"]["USDJPY"]["metrics"]["trades"] == 40
    assert report["axes"]["floor"]["holdout"]["pairs"]["USDJPY"]["metrics"]["trades"] == 3
    assert [r["scope"] for r in report["rows"]] == ["in_sample", "holdout"]


def test_a_short_in_sample_sample_is_unevaluable_too(make):
    world = make(replay=Replay(
        in_sample=lambda pair: _metrics(trades=3, pf=1.5, evaluable=False)))
    _v, target = world.stored()
    report = _result(world, target).report
    assert report["result"]["overall"] == "unevaluable"


def test_every_reason_the_audit_can_report_is_in_the_fixed_set():
    from agentic_fx.plugin.switch import GateFailure
    seen = set()
    for stage in ("snapshot", "source", "discovery", "contract", "indicator_static", "indicator_fixture",
                  "pytest", "hash", "kind_gate", "verdict", "unexpected"):
        for detail in ("", "pytest", "timeout", "crashed", "sandbox_unavailable", "db_busy",
                       "resource", "no_history"):
            for kind in (None, "indicator_unresolved", "insufficient_trades",
                         "reference_history_missing", "history_source_unconfigured",
                         hh.HISTORY_STALE, hh.HISTORY_GAP, hh.HISTORY_UNAVAILABLE, "floor"):
                for error_type, rc in (("ValueError", None), ("SandboxError", None),
                                       ("RuntimeError", 1), ("OSError", -1),
                                       ("CandidateSnapshotError", None)):
                    overall, reason = audit.classify_failure(GateFailure(
                        stage=stage, error_type=error_type, detail=detail, verdict_kind=kind,
                        returncode=rc))
                    seen.add((overall, reason))
    for overall, reason in seen:
        assert overall in ("fail", "unevaluable")
        if overall == "unevaluable":
            assert reason in UNEVALUABLE_REASONS_V2


# ---- 入力の同一性 ---------------------------------------------------------------------

def test_the_stored_test_of_the_named_artifact_runs_even_when_a_live_plugin_has_another_version(
        make):
    world = make()
    old, target = world.stored("a")
    newer, _ = world.stored("b", py=old.joinpath("plugin.py").read_text() + "\n# newer\n")
    world.link(NAME, newer)
    report = _result(world, target).report
    assert report["artifact_hash"] == old.name
    assert report["result"]["overall"] == "pass"
    assert os.readlink(world.root / NAME) == f".versions/{NAME}/{newer.name}"


@pytest.mark.parametrize("field,value,reason", [
    ("content_hash", "d" * 64, "hash_mismatch"),
    ("artifact_hash", "d" * 64, "hash_mismatch"),
    ("name", "other", "hash_mismatch"),
])
def test_an_input_that_differs_from_the_old_approval_is_refused_before_the_gate(
        make, field, value, reason):
    world = make()
    _v, target = world.stored()
    bad = audit.AuditTarget(**{**target.__dict__, field: value})
    report = _result(world, bad).report
    assert (report["result"]["overall"], report["result"]["reason"]) == ("unevaluable", reason)
    assert world.gate.calls == 0 and not world.work_root.exists() or not list(
        world.work_root.iterdir())


def test_a_version_missing_from_the_store_is_unevaluable_without_a_fallback(make):
    world = make()
    version, target = world.stored()
    for entry in version.iterdir():
        entry.unlink()
    version.rmdir()
    report = _result(world, target).report
    assert (report["result"]["reason"], report["input_checks"]["stored_version"]) == (
        "artifact_missing", "artifact_missing")
    assert world.gate.calls == 0


def test_a_stored_version_whose_bytes_changed_is_a_hash_mismatch(make):
    world = make()
    version, target = world.stored()
    os.chmod(version, 0o700)
    (version / "plugin.py").write_text((version / "plugin.py").read_text() + "# tampered\n")
    report = _result(world, target).report
    assert report["result"]["reason"] == "hash_mismatch"
    assert world.gate.calls == 0


def test_the_copy_area_is_cleaned_after_every_outcome(make):
    world = make(gate=Gate(passed=False, returncode=1))
    _v, target = world.stored()
    world.run(target, clock=lambda: TUESDAY)
    assert list(world.work_root.iterdir()) == []


def test_the_latest_decision_is_recorded_without_changing_it(make):
    from agentic_fx.store import approvals as approvals_store
    from tests.plugin.deploy_world import at
    world = make()
    version, target = world.stored()
    rejected = approvals_store.create(
        world.conn, "plugin", {"name": NAME, "kind": "strategy",
                               "content_hash": target.content_hash,
                               "artifact_hash": target.artifact_hash}, at(5))
    approvals_store.apply_decision(world.conn, rejected, "rejected", decided_by="human",
                                   now=at(6), reason="superseded")
    report = _result(world, target).report
    assert report["input_checks"]["latest_decision"] == {"id": rejected, "status": "rejected"}
    assert report["result"]["overall"] == "pass"   # a recorded evaluation, not a revival
    assert world.conn.execute(
        "SELECT status FROM approval_requests WHERE id=?", (rejected,)).fetchone()[0] == "rejected"


# ---- 依存 (現在の承認済み inventory) --------------------------------------------------

def _with_indicator(world):
    ind = world.version("ind", "i", kind="indicator")
    world.deploy("ind", ind, None, 1)
    pin = loader.content_hash(ind)
    version = world.version(NAME, "s", kind="strategy", indicator="ind", pin=pin)
    old_id = world.approve(NAME, version)
    pins = {"fast": {"plugin": "ind", "content_hash": pin, "params": {}}}
    world.conn.execute(
        "UPDATE approval_requests SET payload_json=json_set(payload_json,'$.indicator_deps',"
        "json(?)) WHERE id=?", (json.dumps(pins), old_id))
    world.conn.commit()
    return ind, audit.AuditTarget(old_id, NAME, version.name, loader.content_hash(version), pins)


def test_a_dependency_that_is_still_live_and_approved_is_evaluated(make):
    world = make()
    _ind, target = _with_indicator(world)
    report = _result(world, target).report
    assert report["input_checks"]["dependencies"] == "ok"
    assert report["result"]["overall"] == "pass"
    assert [d["alias"] for d in report["dependencies"]] == ["fast"]


def test_a_live_dependency_that_moved_on_is_dependency_changed_with_no_pin_update(make):
    world = make()
    ind, target = _with_indicator(world)
    newer = world.version("ind", "j", kind="indicator")
    world.deploy("ind", newer, ind, 2)
    report = _result(world, target).report
    assert (report["result"]["overall"], report["result"]["reason"]) == (
        "unevaluable", "dependency_changed")
    assert world.gate.calls == 0
    assert report["input_checks"]["dependencies"] != "ok"


def test_pins_that_differ_from_the_old_approval_are_dependency_changed(make):
    world = make()
    _ind, target = _with_indicator(world)
    other = audit.AuditTarget(**{**target.__dict__, "dependency_pins": {}})
    report = _result(world, other).report
    assert report["result"]["reason"] == "dependency_changed" and world.gate.calls == 0


def test_a_dependency_that_changes_after_the_gate_is_caught_by_the_final_recheck(make):
    world = make()
    ind, target = _with_indicator(world)
    newer = world.version("ind", "j", kind="indicator")
    real = switch._run_full_gate

    def replace_the_dependency_afterwards(*args, **kwargs):
        result = real(*args, **kwargs)
        world.deploy("ind", newer, ind, 3)
        return result

    report = world.run(target, clock=lambda: TUESDAY,
                       gate_fn=replace_the_dependency_afterwards).report
    assert report["result"]["overall"] == "unevaluable"
    assert report["result"]["reason"] == "changed_during_evaluation"
    assert "dependencies" in report["changed_during_evaluation"]
    assert report["axes"]["floor"]["in_sample"]["result"] == "pass"     # partial results are kept


def test_a_dependency_that_moves_inside_the_gate_is_dependency_changed(make, monkeypatch):
    world = make()
    ind, target = _with_indicator(world)
    newer = world.version("ind", "j", kind="indicator")
    original = world.gate

    def replace_the_dependency(plugin_dir, *, settings):
        world.deploy("ind", newer, ind, 3)
        return original(plugin_dir, settings=settings)

    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", replace_the_dependency)
    report = world.run(target, clock=lambda: TUESDAY).report
    assert (report["result"]["overall"], report["result"]["reason"]) == (
        "unevaluable", "dependency_changed")


def test_a_history_epoch_change_during_the_evaluation_is_not_a_pass(make, monkeypatch):
    world = make()
    _v, target = world.stored()
    original = world.gate.__call__

    def bump(plugin_dir, *, settings):
        world.conn.execute("INSERT INTO data_migrations(name, applied_at) VALUES (?, ?)",
                           ("time_repair_x", TUESDAY.isoformat()))
        world.conn.commit()
        return original(plugin_dir, settings=settings)

    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", bump)
    before_changes = world.conn.total_changes
    result = world.run(target, clock=lambda: TUESDAY)
    report = result.report
    assert report["result"]["overall"] == "unevaluable"
    assert report["result"]["reason"] == "changed_during_evaluation"
    assert "history_epoch" in report["changed_during_evaluation"]
    assert world.conn.total_changes == before_changes + 1   # only the test's own insert


# ---- 記録先と副作用 ------------------------------------------------------------------

def test_the_audit_calls_the_shared_gate_once_with_warn_off_and_an_explicit_sink(make):
    world = make()
    _v, target = world.stored()
    seen = []
    real = switch._run_full_gate

    def spy(*a, **kw):
        seen.append(kw)
        return real(*a, **kw)

    _result(world, target, gate_fn=spy)
    (kw,) = seen
    assert kw["floor_mode"] == "warn" and kw["activity"] is None
    assert isinstance(kw["failure_sink"], audit.FailureCollector)
    assert kw["now"] == TUESDAY


def test_gate_rows_never_reach_the_results_tables_or_their_readers(make):
    world = make(history="stale")     # rows exist (in_sample) and the gate then fails
    _v, target = world.stored()
    result = _result(world, target)
    assert result.report["axes"]["floor"]["in_sample"]["pairs"]["USDJPY"]["metrics"]["trades"] == 40
    assert backtest_runs_store.in_sample_view(world.conn) == []
    assert backtest_runs_store.latest_in_sample_metrics(
        world.conn, target.content_hash, pair="USDJPY", variant="candidate",
        source="dukascopy", base_interval="5m", name=NAME) is None
    assert world.conn.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()[0] == 0


def test_a_sink_that_fails_never_falls_back_to_the_database(make, monkeypatch):
    world = make(history="stale")
    _v, target = world.stored()

    def broken(self, failure):
        raise OSError("sink is down")

    monkeypatch.setattr(audit.FailureCollector, "__call__", broken)
    saved = []
    monkeypatch.setattr(switch, "_persist_human_gate_rows", lambda *a, **k: saved.append(1))
    report = _result(world, target).report
    assert saved == []
    assert report["result"]["overall"] == "unevaluable"
    assert world.conn.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()[0] == 0


def test_the_deployment_state_is_untouched(make):
    world = make()
    version, target = world.stored()
    world.link(NAME, version)
    live_before = os.readlink(world.root / NAME)
    entries_before = sorted(p.name for p in world.root.iterdir())
    _result(world, target)
    assert os.readlink(world.root / NAME) == live_before
    entries_after = sorted(p.name for p in world.root.iterdir() if p.name != ".locks")
    assert entries_after == [e for e in entries_before if e != ".locks"]


# ---- report の内容 -------------------------------------------------------------------

_FORBIDDEN = ("initial_balance", "123456", "total_pnl", "settings_hash", "Traceback")


def test_the_report_holds_only_allowed_fields(make):
    world = make()
    _v, target = world.stored()
    text = _result(world, target).report_path.read_text()
    for needle in _FORBIDDEN:
        assert needle not in text
    report = json.loads(text)
    assert set(report) == {
        "report_format", "trial_id", "old_approval_id", "name", "artifact_hash", "content_hash",
        "dependencies", "input_checks", "evaluation", "axes", "changed_during_evaluation",
        "result", "rows"}
    assert set(report["rows"][0]) == {
        "local_row_id", "db_run_id", "scope", "pair", "trades", "pf", "win_rate", "avg_r",
        "max_drawdown", "evaluable", "eval_bars", "period"}


def test_a_report_is_published_once_and_never_overwritten(make, tmp_path):
    world = make()
    _v, target = world.stored()
    first = world.run(target, clock=lambda: TUESDAY)
    with pytest.raises(FileExistsError):
        audit._write_report(world.report_dir, first.report["trial_id"], first.report)
    assert sorted(p.name for p in world.report_dir.iterdir()) == [first.report_path.name]


def test_two_trials_of_the_same_version_keep_separate_reports(make):
    world = make()
    _v, target = world.stored()
    one = world.run(target, clock=lambda: TUESDAY)
    two = world.run(target, clock=lambda: TUESDAY)
    assert one.report_path != two.report_path
    assert one.report_path.exists() and two.report_path.exists()


def test_a_failed_report_write_is_reported_and_is_not_a_pass_publication(make, monkeypatch):
    world = make()
    _v, target = world.stored()

    def broken(report_dir, trial_id, report):
        raise OSError("disk full")

    monkeypatch.setattr(audit, "_write_report", broken)
    result = world.run(target, clock=lambda: TUESDAY)
    assert result.report_path is None and result.write_error == "OSError"
    assert not world.report_dir.exists() or list(world.report_dir.iterdir()) == []


def test_an_interrupt_leaves_an_interrupted_report_and_reraises(make):
    world = make()
    _v, target = world.stored()

    def interrupted(*a, **kw):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        world.run(target, gate_fn=interrupted, clock=lambda: TUESDAY)
    (path,) = list(world.report_dir.iterdir())
    report = json.loads(path.read_text())
    assert report["axes"]["execution"]["status"] == "interrupted"
    assert (report["result"]["overall"], report["result"]["reason"]) == (
        "unevaluable", "interrupted")
    assert list(world.work_root.iterdir()) == []


def test_the_conditions_digest_changes_with_the_evaluation_conditions(make):
    world = make()
    _v, target = world.stored()
    one = world.run(target, clock=lambda: TUESDAY).report["evaluation"]["conditions_digest"]
    world.settings.improve.gate.min_pf = 1.2
    two = world.run(target, clock=lambda: TUESDAY).report["evaluation"]["conditions_digest"]
    assert one != two


def test_the_floor_of_a_version_is_judged_by_the_shared_rule_per_scope(make):
    """報告の scope 別の合否は、gate が使う共有の床判定と同じ結論になる。"""
    world = make(replay=Replay(
        in_sample=lambda pair: _metrics(trades=40, pf=1.0, avg_r=0.0),
        holdout=lambda pair: _metrics(trades=40, pf=1.5)))
    _v, target = world.stored()
    report = _result(world, target).report
    from agentic_fx.plugin import strategy_gate
    expected = strategy_gate._check_profitability_floor(
        {"USDJPY": _metrics(trades=40, pf=1.0, avg_r=0.0)}, settings=world.settings,
        scope="in_sample")[0]
    assert (expected == "unprofitable") == (report["axes"]["floor"]["in_sample"]["result"] == "fail")


# ---- 並行運用 (lock は取得とコピー・終端照合だけ) ---------------------------------------

def test_a_held_plugin_lock_gives_a_bounded_wait_and_an_unevaluable_result(make):
    import time
    world = make()
    _v, target = world.stored()
    with switch._plugin_name_lock(world.root, NAME):
        started = time.monotonic()
        report = world.run(target, clock=lambda: TUESDAY, lock_wait_sec=0.3).report
        waited = time.monotonic() - started
    assert report["result"]["reason"] == "resource_unavailable"
    assert waited < 5 and world.gate.calls == 0


def test_no_plugin_lock_is_held_while_the_version_is_evaluated(make, monkeypatch):
    import time
    world = make()
    _v, target = world.stored()
    original = world.gate
    acquired = []

    def probe(plugin_dir, *, settings):
        # a daemon-side writer must be able to take the lock during the evaluation
        with switch._plugin_name_lock(world.root, NAME, deadline=time.monotonic() + 0.5):
            acquired.append(True)
        return original(plugin_dir, settings=settings)

    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", probe)
    assert world.run(target, clock=lambda: TUESDAY).report["result"]["overall"] == "pass"
    assert acquired == [True]


def test_a_swap_during_the_copy_is_unevaluable_and_cleans_up(make):
    world = make()
    version, target = world.stored()

    def swap():
        (version / "plugin.py").unlink()
        os.mkfifo(version / "plugin.py")

    with never_block():
        report = world.run(target, clock=lambda: TUESDAY,
                           between_checks_and_reads=swap).report
    assert report["result"]["reason"] == "changed_during_evaluation"
    assert world.gate.calls == 0 and list(world.work_root.iterdir()) == []


def test_the_old_approved_row_keeps_the_stored_version_as_a_gc_root(make):
    from agentic_fx.plugin import version_store
    world = make()
    version, target = world.stored()
    before = version_store.gc_roots(world.conn, plugins_root=world.root)
    world.run(target, clock=lambda: TUESDAY)
    assert version in before
    assert version_store.gc_roots(world.conn, plugins_root=world.root) == before


def test_an_evidence_that_no_longer_matches_at_the_end_is_not_a_pass(make):
    """no-bars 一覧が評価の途中で変わると、評価時の証跡は終了時の照合で落ち、pass を出さない。"""
    from tests.history_support import hole_day, write_no_bars
    world = make()
    _v, target = world.stored()
    real = switch._run_full_gate

    def amend_the_list_afterwards(*args, **kwargs):
        result = real(*args, **kwargs)
        day = hole_day(TUESDAY)[0].date()
        write_no_bars(world.root.parent, source="dukascopy", symbol="USDJPY", day=day)
        return result

    report = world.run(target, clock=lambda: TUESDAY, gate_fn=amend_the_list_afterwards).report
    assert (report["result"]["overall"], report["result"]["reason"]) == (
        "unevaluable", "changed_during_evaluation")
    assert report["axes"]["history"]["result"] == "failed"
    assert report["axes"]["history"]["evidence_check"] == "invalid"


def test_a_gate_result_that_is_not_evaluable_is_never_a_pass(make):
    import dataclasses
    world = make()
    _v, target = world.stored()
    real = switch._run_full_gate

    def not_evaluable(*args, **kwargs):
        meta, content, artifact, outcome = real(*args, **kwargs)
        return meta, content, artifact, dataclasses.replace(outcome, evaluable=False)

    report = world.run(target, clock=lambda: TUESDAY, gate_fn=not_evaluable).report
    assert report["result"]["overall"] == "unevaluable"


# ---- 指標の fixture 段 ----------------------------------------------------------------

def _indicator_target(world):
    version = world.version(NAME, "ind", kind="indicator")
    old_id = world.approve(NAME, version)
    return audit.AuditTarget(old_id, NAME, version.name, loader.content_hash(version), {})


@pytest.mark.parametrize("raises,code", [
    (lambda ip: ip.IndicatorGateUnavailable("busy"), "indicator_gate_unavailable"),
    (None, "duplicate_of_deployed"),
])
def test_an_indicator_fixture_rejection_reaches_the_sink_and_is_a_completed_unevaluable(
        make, monkeypatch, raises, code):
    from types import SimpleNamespace
    from agentic_fx.plugin import indicator_pipeline as ip
    world = make()
    target = _indicator_target(world)

    def fixture_stage(*a, **kw):
        if raises is not None:
            raise raises(ip)
        return SimpleNamespace(ok=False, code=code, warnings=(), similar_to=(), unavailable=())

    monkeypatch.setattr(ip, "run_fixture_stage", fixture_stage)
    report = _result(world, target).report
    assert (report["result"]["overall"], report["result"]["reason"]) == (
        "unevaluable", "unsupported_version")
    assert report["axes"]["execution"]["status"] == "completed"
    assert report["axes"]["execution"]["stage"] == "indicator_fixture"
    assert report["input_checks"]["indicator_fixture"] == code
    assert {"item": "history_check", "reason": f"indicator_fixture:{code}"} in (
        report["axes"]["unexecuted"])


# ---- 依存の確認 ------------------------------------------------------------------------

def test_a_version_without_dependencies_never_builds_the_inventory(make, monkeypatch):
    world = make()
    version, target = world.stored()
    meta = loader._discover_one(version, NAME)

    def forbidden(*a, **kw):
        raise AssertionError("the inventory must not be built")

    monkeypatch.setattr(audit.tools_plugin_loader, "approved_plugins", forbidden)
    assert audit._check_dependencies(world.conn, world.root, world.settings, meta, target) == (
        True, "")
    pinned = audit.AuditTarget(target.old_approval_id, NAME, target.artifact_hash,
                               target.content_hash, {"x": {"plugin": "p", "content_hash": "h"}})
    assert audit._check_dependencies(world.conn, world.root, world.settings, meta, pinned) == (
        False, "pins_differ")


# ---- report の作成と書込みの保護 -----------------------------------------------------------

def test_a_report_that_cannot_be_built_is_left_as_an_unevaluable_report(make, monkeypatch):
    world = make()
    _v, target = world.stored()

    def broken(*a, **kw):
        raise RuntimeError("settings unreadable")

    monkeypatch.setattr(audit, "_build_report", broken)
    result = world.run(target)
    (path,) = list(world.report_dir.iterdir())
    assert json.loads(path.read_text()) == result.report
    assert (result.report["result"]["overall"], result.report["result"]["reason"]) == (
        "unevaluable", "failed")
    assert result.report["axes"]["execution"]["status"] == "interrupted"
    assert list(world.work_root.iterdir()) == []


def test_an_interrupt_while_building_the_report_leaves_a_report_and_reraises(make, monkeypatch):
    world = make()
    _v, target = world.stored()

    def interrupted(*a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(audit, "_build_report", interrupted)
    with pytest.raises(KeyboardInterrupt):
        world.run(target)
    (path,) = list(world.report_dir.iterdir())
    report = json.loads(path.read_text())
    assert (report["result"]["overall"], report["result"]["reason"]) == (
        "unevaluable", "interrupted")
    assert list(world.work_root.iterdir()) == []


def test_an_interrupt_while_writing_the_report_is_retried_as_an_interrupted_report(
        make, monkeypatch):
    world = make()
    _v, target = world.stored()
    real = audit._write_report
    calls = []

    def once(report_dir, trial_id, report):
        calls.append(report["result"]["overall"])
        if len(calls) == 1:
            raise KeyboardInterrupt
        return real(report_dir, trial_id, report)

    monkeypatch.setattr(audit, "_write_report", once)
    with pytest.raises(KeyboardInterrupt):
        world.run(target)
    assert calls == ["pass", "unevaluable"]
    (path,) = list(world.report_dir.iterdir())
    assert json.loads(path.read_text())["result"]["reason"] == "interrupted"
