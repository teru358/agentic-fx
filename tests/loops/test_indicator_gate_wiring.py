"""improve_loop の plugin gate に足した indicator 段の配線 (段の順序、固定語、
warning の payload、環境起因の retryable、mode の snapshot、last_result、族表の
relation、prompt、settings)。sandbox は起動せず、fixture 実行だけを差し替える。"""
from __future__ import annotations

import ast
import inspect
import json
import textwrap
from pathlib import Path

import pytest
import yaml

from agentic_fx import improve_notice_codes as codes
from agentic_fx.config import ImproveSettings
from agentic_fx.loops import improve_context, improve_loop
from agentic_fx.loops.improve_run_context import ImproveRunContext
from agentic_fx.plugin import indicator_pipeline
from agentic_fx.plugin.gate_pytest import GateResult
from agentic_fx.plugin.resolve import ApprovedInventory, InventoryBuildResult
from agentic_fx.store import candidate_archives as archives
from tests.fixtures import indicator_gate_fakes as fakes

_TESTS = (b"def test_a(): pass\ndef test_b(): pass\ndef test_c(): pass\n")
_PLUGIN = ("def compute(df, params):\n"
           "    return {'value': df['close'].pct_change()}\n")


def _candidate(root: Path, name: str, *, params: dict | None = None,
               plugin_py: str = _PLUGIN, outputs=("value",)) -> Path:
    d = root / "staging" / name
    d.mkdir(parents=True)
    (d / "plugin.py").write_text(plugin_py)
    config = {"kind": "indicator", "outputs": list(outputs), "max_bars": 200,
              "params": params or {}}
    (d / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    (d / "test_plugin.py").write_bytes(_TESTS)
    return d


def _inventory(root: Path, *deployed) -> InventoryBuildResult:
    return InventoryBuildResult(
        inventory=ApprovedInventory(root=root, metas=tuple(deployed)),
        phase1_metas=(), resolved={}, rejected_strategies=())


def _deployed(root: Path, name: str, tag: str):
    return fakes.meta_for(name, root / "plugins" / name, content_hash=tag * 64)


@pytest.fixture
def gate_env(tmp_path, loop_min, monkeypatch):
    """pytest 段を通し、fixture 実行を差し替えた gate を返す。"""
    order: list[str] = []

    def _pytest(plugin_dir, *, settings):
        order.append("pytest")
        return GateResult(passed=True, returncode=0,
                          stdout_tail="3 passed in 0.1s", duration_sec=0.1)

    monkeypatch.setattr("agentic_fx.loops.improve_loop.run_gate_pytest", _pytest)
    source = tmp_path / "source"
    source.mkdir()

    class Env:
        loop = loop_min
        events = order

        def run(self, name, *, probe, deployed=(), pending=(), mode=None,
                params=None, plugin_py=_PLUGIN, conn=None):
            d = _candidate(tmp_path, name, params=params, plugin_py=plugin_py)
            fake = probe.install(monkeypatch)
            fake.events = order
            return loop_min._run_plugin_gate(
                d, name=name, source_snapshot_dir=source,
                inventory=_inventory(tmp_path, *deployed), conn=conn,
                indicator_duplicate_mode=mode)

    return Env()


def _probe(**extra):
    return fakes.FakeProbe({"cand": fakes.ratio, **extra})


def test_stage_order_is_static_then_pytest_then_contract_then_probe(gate_env):
    verdict = gate_env.run("cand", probe=_probe())
    assert verdict.passed
    assert gate_env.events == [
        "pytest", "open:cand", "contract:cand", "probe:cand"]


def test_symbol_in_name_rejects_before_pytest_and_any_run(gate_env):
    verdict = gate_env.run("usdjpy_ratio", probe=fakes.FakeProbe(
        {"usdjpy_ratio": fakes.ratio}))
    assert (verdict.passed, verdict.code) == (False, "symbol_in_name")
    assert gate_env.events == []
    assert "name:usdjpy" in verdict.report_detail


def test_param_price_unit_rejects_before_pytest(gate_env):
    verdict = gate_env.run("cand", probe=_probe(), params={"pip_size": 0.01})
    assert (verdict.passed, verdict.code) == (False, "param_price_unit")
    assert gate_env.events == []
    assert "pip_size" in verdict.report_detail


@pytest.mark.parametrize("probe,code", [
    (fakes.FakeProbe({"cand": fakes.ratio}, contract_failure="exception"),
     "fixture_run_failed"),
    (fakes.FakeProbe({"cand": fakes.ratio}, probe_failure="probe_timeout"),
     "fixture_run_failed"),
    (fakes.FakeProbe({"cand": fakes.all_nan}), "output_not_finite"),
    (fakes.FakeProbe({"cand": fakes.constant}), "output_constant"),
])
def test_structural_failures_carry_their_fixed_word(gate_env, probe, code):
    verdict = gate_env.run("cand", probe=probe)
    assert (verdict.passed, verdict.code) == (False, code)
    assert verdict.failure_kind == "gate_failed"
    assert code in codes.PLUGIN_GATE_CODES
    assert verdict.indicator_report is not None or code == "fixture_run_failed"


def test_exact_duplicate_of_a_deployed_indicator_rejects(tmp_path, gate_env):
    verdict = gate_env.run(
        "cand", probe=_probe(rsi=fakes.ratio),
        deployed=[_deployed(tmp_path, "rsi", "a")])
    assert (verdict.passed, verdict.code) == (False, "indicator_duplicate")
    assert verdict.report_detail == "- duplicate: rsi.value"


def test_monotone_duplicate_is_a_warning_in_warn_mode(tmp_path, gate_env):
    verdict = gate_env.run(
        "cand", probe=_probe(rsi=fakes.affine_of_ratio),
        deployed=[_deployed(tmp_path, "rsi", "a")], mode="warn")
    assert verdict.passed
    assert "indicator_monotone_duplicate" in verdict.warnings
    assert verdict.similar_to[0] == {
        "name": "rsi", "column": "value", "kind": "monotone"}


def test_monotone_duplicate_rejects_in_enforce_mode(tmp_path, gate_env):
    verdict = gate_env.run(
        "cand", probe=_probe(rsi=fakes.affine_of_ratio),
        deployed=[_deployed(tmp_path, "rsi", "a")], mode="enforce")
    assert (verdict.passed, verdict.code) == (False, "indicator_monotone_duplicate")


def test_the_mode_argument_beats_the_live_setting(tmp_path, gate_env, monkeypatch):
    settings = gate_env.loop._settings
    enforce = settings.model_copy(update={"improve": settings.improve.model_copy(
        update={"indicator_duplicate_mode": "enforce"})})
    monkeypatch.setattr(gate_env.loop, "_settings", enforce)
    verdict = gate_env.run(
        "cand", probe=_probe(rsi=fakes.affine_of_ratio),
        deployed=[_deployed(tmp_path, "rsi", "a")], mode="warn")
    assert verdict.passed and "indicator_monotone_duplicate" in verdict.warnings


def test_scale_dependent_output_passes_with_a_warning(gate_env):
    verdict = gate_env.run(
        "cand", probe=fakes.FakeProbe({"cand": fakes.absolute_threshold}))
    assert verdict.passed
    assert "scale_dependent" in verdict.warnings


def test_scale_invariant_and_proportional_outputs_carry_no_warning(gate_env):
    assert gate_env.run("cand", probe=_probe()).warnings == ()
    assert gate_env.run("cand2", probe=fakes.FakeProbe(
        {"cand2": fakes.price_diff})).warnings == ()


def test_same_name_update_is_not_compared_with_the_deployed_version(
        tmp_path, gate_env):
    verdict = gate_env.run(
        "cand", probe=_probe(), deployed=[_deployed(tmp_path, "cand", "a")])
    assert verdict.passed and verdict.similar_to == ()
    # 自分の配備中の版は比較相手でなく、redefined の比較にだけ使われる
    assert "indicator_redefined" not in verdict.warnings


def test_pending_candidate_is_a_comparison_partner(tmp_path, gate_env, conn):
    from agentic_fx.plugin import indicator_fixture_cache as cache
    from agentic_fx.plugin.indicator_pipeline import canonical_bars
    from agentic_fx.store import approvals as approvals_store
    from datetime import datetime, timezone
    bars, data_hash = canonical_bars("1h")
    pending_hash = "p" * 64
    approvals_store.create(conn, "plugin", {
        "name": "queued", "kind": "indicator", "content_hash": pending_hash},
        datetime(2026, 8, 22, tzinfo=timezone.utc))
    cache.write(tmp_path / "data", cache.CacheKey(pending_hash, data_hash, "1h"),
                indicator_pipeline.probe_mod.fixture_cache_entry(
                    _fake_result(fakes.ratio, bars)))
    verdict = gate_env.run("cand", probe=_probe(), conn=conn)
    assert (verdict.passed, verdict.code) == (False, "indicator_duplicate")
    # 承認待ちの相手は候補名でなく承認番号で表す
    assert "pending-1" in verdict.report_detail
    assert "queued" not in verdict.report_detail


def test_an_earlier_pending_submission_of_the_same_name_is_not_a_partner(
        tmp_path, gate_env, conn):
    from agentic_fx.plugin import indicator_fixture_cache as cache
    from agentic_fx.plugin.indicator_pipeline import canonical_bars
    from agentic_fx.store import approvals as approvals_store
    from datetime import datetime, timezone
    bars, data_hash = canonical_bars("1h")
    approvals_store.create(conn, "plugin", {
        "name": "cand", "kind": "indicator", "content_hash": "q" * 64},
        datetime(2026, 8, 22, tzinfo=timezone.utc))
    cache.write(tmp_path / "data", cache.CacheKey("q" * 64, data_hash, "1h"),
                indicator_pipeline.probe_mod.fixture_cache_entry(
                    _fake_result(fakes.ratio, bars)))
    verdict = gate_env.run("cand", probe=_probe(), conn=conn)
    assert verdict.passed and verdict.similar_to == ()


def _fake_result(fn, bars):
    from agentic_fx.plugin import indicator_probe as probe
    tail = bars.tail(probe.PROBE_BARS)
    runs = {label: fn(probe._scaled(tail, price, volume))
            for label, price, volume in probe.PROBE_RUNS}
    return probe.ProbeResult(runs=runs)


def test_constant_in_only_one_run_is_a_warning_not_a_rejection(gate_env):
    verdict = gate_env.run("cand", probe=fakes.FakeProbe(
        {"cand": fakes.constant_in_contract_only}))
    assert verdict.passed and "output_constant_suspect" in verdict.warnings


def test_sparse_output_passes_with_a_warning(gate_env):
    verdict = gate_env.run("cand", probe=fakes.FakeProbe({"cand": fakes.sparse}))
    assert verdict.passed and "sparse_output" in verdict.warnings


def test_a_symbol_name_in_a_deployed_version_is_grandfathered_for_a_same_name_update(
        tmp_path, gate_env):
    old = tmp_path / "plugins" / "usdjpy_ratio"
    old.mkdir(parents=True)
    (old / "plugin.py").write_text(_PLUGIN)
    (old / "config.yaml").write_text(yaml.safe_dump(
        {"kind": "indicator", "outputs": ["value"], "max_bars": 200}))
    deployed = fakes.meta_for("usdjpy_ratio", old, content_hash="a" * 64)
    verdict = gate_env.run(
        "usdjpy_ratio", probe=fakes.FakeProbe({"usdjpy_ratio": fakes.ratio}),
        deployed=[deployed])
    assert verdict.passed and "legacy_symbol_name" in verdict.warnings


def test_a_new_symbol_reference_in_a_legacy_update_is_still_rejected(
        tmp_path, gate_env):
    old = tmp_path / "plugins" / "usdjpy_ratio"
    old.mkdir(parents=True)
    (old / "plugin.py").write_text(_PLUGIN)
    (old / "config.yaml").write_text(yaml.safe_dump(
        {"kind": "indicator", "outputs": ["value"], "max_bars": 200}))
    deployed = fakes.meta_for("usdjpy_ratio", old, content_hash="a" * 64)
    verdict = gate_env.run(
        "usdjpy_ratio", probe=fakes.FakeProbe({"usdjpy_ratio": fakes.ratio}),
        deployed=[deployed], params={"jpy_window": 5})
    assert (verdict.passed, verdict.code) == (False, "symbol_in_name")


def test_a_same_name_update_with_other_outputs_is_flagged_as_redefined(
        tmp_path, gate_env):
    old = fakes.meta_for("cand", tmp_path / "plugins" / "cand",
                         content_hash="a" * 64, outputs=("other",))
    verdict = gate_env.run("cand", probe=_probe(), deployed=[old])
    assert verdict.passed and "indicator_redefined" in verdict.warnings


def test_a_same_name_update_with_the_same_outputs_is_not_flagged(
        tmp_path, gate_env):
    old = fakes.meta_for("cand", tmp_path / "plugins" / "cand",
                         content_hash="a" * 64)
    verdict = gate_env.run("cand", probe=fakes.FakeProbe(
        {"cand": fakes.ratio}), deployed=[old])
    assert verdict.passed and "indicator_redefined" not in verdict.warnings


def test_environment_failure_is_retryable_not_a_candidate_failure(gate_env):
    with pytest.raises(indicator_pipeline.IndicatorGateUnavailable):
        gate_env.run("cand", probe=fakes.FakeProbe(
            {"cand": fakes.ratio}, env_error=True))


def test_deployed_cache_build_failure_is_retryable_for_the_loop(tmp_path, gate_env):
    with pytest.raises(indicator_pipeline.IndicatorGateUnavailable):
        gate_env.run(
            "cand", probe=fakes.FakeProbe(
                {"cand": fakes.ratio, "rsi": fakes.ratio}, fail_names={"rsi"}),
            deployed=[_deployed(tmp_path, "rsi", "a")])


def test_second_mission_builds_no_cache_for_deployed_indicators(
        tmp_path, gate_env):
    deployed = [_deployed(tmp_path, "rsi", "a")]
    first = gate_env.run("cand", probe=_probe(rsi=fakes.price_diff),
                         deployed=deployed)
    assert first.indicator_report.built == 2     # 候補と配備済み 1 本
    gate_env.events.clear()
    second = gate_env.run("cand2", probe=fakes.FakeProbe(
        {"cand2": fakes.ratio, "rsi": fakes.price_diff}), deployed=deployed,
        plugin_py=_PLUGIN + "# second\n")
    assert second.indicator_report.built == 1    # 候補だけ
    assert "probe:rsi" not in gate_env.events


def test_contract_violation_skips_the_fixture_run(tmp_path, loop_min,
                                                  monkeypatch):
    monkeypatch.setattr(
        "agentic_fx.loops.improve_loop.run_gate_pytest",
        lambda plugin_dir, *, settings: GateResult(
            passed=True, returncode=0, stdout_tail="3 passed in 0.1s",
            duration_sec=0.1))
    d = _candidate(tmp_path, "cand")
    (d / "config.yaml").write_text(
        yaml.safe_dump({"kind": "indicator", "max_bars": 200}))
    fake = _probe().install(monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    verdict = loop_min._run_plugin_gate(
        d, name="cand", source_snapshot_dir=source)
    assert verdict.passed and fake.events == []


# --- commit 全体: 終端 ---------------------------------------------------------

def _gate_call_sources() -> str:
    return inspect.getsource(improve_loop.ImproveLoop.commit)


def test_commit_routes_environment_failure_to_the_sandbox_unavailable_end():
    tree = ast.parse(textwrap.dedent(_gate_call_sources()))
    handlers = [h for n in ast.walk(tree) if isinstance(n, ast.Try)
                for h in n.handlers
                if "IndicatorGateUnavailable" in ast.unparse(h.type or ast.Constant(0))]
    assert len(handlers) == 1
    assert "_finalize_gate_sandbox_unavailable" in ast.unparse(handlers[0])


def test_run_context_snapshots_the_duplicate_mode_at_prepare():
    assert ImproveRunContext.__dataclass_fields__[
        "indicator_duplicate_mode"].default == "warn"
    source = inspect.getsource(improve_loop.ImproveLoop)
    assert "indicator_duplicate_mode=ctx.indicator_duplicate_mode" in source
    assert "indicator_duplicate_mode=(\n                    self._settings.improve.indicator_duplicate_mode)" in source


# --- last_result ---------------------------------------------------------------

@pytest.mark.parametrize("code", sorted(codes.INDICATOR_LAST_RESULT_CODES))
def test_last_result_passes_each_indicator_word_verbatim(code):
    value = f"observation:gate_failed:{code}"
    assert improve_context.project_last_result(value) == value


@pytest.mark.parametrize("value,folded", [
    ("observation:gate_failed:pytest_failed", "observation:gate_failed"),
    ("observation:gate_failed:rsi", "observation:gate_failed"),
    ("observation:gate_failed:", "observation:gate_failed"),
    ("observation:gate_failed:indicator_duplicate:rsi", "observation:gate_failed"),
])
def test_last_result_folds_everything_else(value, folded):
    assert improve_context.project_last_result(value) == folded


# --- 族表の relation -----------------------------------------------------------

NOW = None


def _plant(conn, *, key, relation, n):
    from datetime import datetime, timezone
    from agentic_fx.store import missions as missions_store
    now = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
    mission = missions_store.start(conn, "improve", "codex", "gpt-5", now=now,
                                   commit=True)
    row = archives.insert(
        conn, mission_id=mission, name=f"cand{n}", content_hash=f"c{n}",
        artifact_hash=f"a{n}", archive_path=None, pair=None, metrics={},
        now=now, family_key=key, relation=relation, commit=True)
    archives.settle(conn, row, "gate_failed")
    conn.commit()


def _family_section(conn, tmp_path, deployed):
    from datetime import datetime, timezone
    from agentic_fx.config import load_settings
    from agentic_fx.loops.improve_context import build_improve_context
    from agentic_fx.plugin.loader import PluginMeta
    settings = load_settings(
        Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")
    metas = [PluginMeta(name=n, kind="indicator", path=Path("."), params={},
                        timeframe="1h", pairs=(), max_bars=100,
                        content_hash="c" * 64, artifact_hash="a" * 64,
                        outputs=("v",)) for n in deployed]
    data = build_improve_context(
        conn, settings=settings,
        now=datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc), root=tmp_path,
        allowed_backlog_ids=None, inventory_metas=metas)
    return data["improvement_history"]["family_attempts"]


def test_family_table_projects_relations_through_the_deployed_allowlist(
        conn, tmp_path):
    _plant(conn, key="indicator:ghost@1h", n=1, relation=[
        {"kind": "similar", "name": "ema"},
        {"kind": "duplicate", "name": "rsi"},
        {"kind": "monotone", "name": "unlisted_pending"},
        {"kind": "overlap", "name": "bollinger"},
        {"kind": "weird", "name": "sma"}])
    section = _family_section(conn, tmp_path, ["rsi", "ema", "bollinger", "sma"])
    row = next(r for r in section["rows"] if r["family_key"].startswith(
        "indicator (未配備)"))
    assert row["relations"] == ["同等 rsi", "一部重複 bollinger", "類似 ema"]
    assert "unlisted_pending" not in json.dumps(section)
    rendered = improve_context.render_family_attempts(section)
    assert "relation |" in rendered and "同等 rsi, 一部重複 bollinger, 類似 ema" in rendered


def test_family_table_keeps_at_most_three_relations_strongest_first(
        conn, tmp_path):
    names = ["a1", "a2", "a3", "a4"]
    _plant(conn, key="indicator:ghost@1h", n=1, relation=[
        {"kind": "similar", "name": "a1"}, {"kind": "overlap", "name": "a2"},
        {"kind": "monotone", "name": "a3"}, {"kind": "duplicate", "name": "a4"}])
    section = _family_section(conn, tmp_path, names)
    row = next(r for r in section["rows"] if "未配備" in r["family_key"])
    assert row["relations"] == ["同等 a4", "単調変換 a3", "一部重複 a2"]


def test_family_table_has_no_relation_column_without_relations(conn, tmp_path):
    _plant(conn, key="indicator:ghost@1h", n=1, relation=[])
    section = _family_section(conn, tmp_path, ["rsi"])
    assert "relation" not in improve_context.render_family_attempts(section)


@pytest.mark.parametrize("raw", ["not json", "{}", "[1]", '[{"kind":"duplicate"}]',
                                 '[{"kind":"duplicate","name":3}]'])
def test_malformed_relation_rows_project_to_nothing(raw):
    assert improve_context.project_relations(raw, {"rsi"}) == []


def test_candidate_relation_never_holds_scores_or_candidate_names():
    source = inspect.getsource(improve_loop.ImproveLoop._with_relation)
    assert 'relation.append({"kind": row["kind"], "name": row["name"]})' in source


# --- prompt / settings ---------------------------------------------------------

def test_prompt_carries_the_indicator_sentence_verbatim():
    text = improve_context.mission_template()
    assert ("indicator は銘柄を持たないので、名前・param・説明に銘柄名を書かず、"
            "価格の単位 (pips など) を param にしないでください。") in text


def test_settings_defaults_for_the_indicator_gate():
    s = ImproveSettings()
    assert (s.indicator_duplicate_mode, s.indicator_similar_corr,
            s.indicator_fixture_wall_clock_max_sec) == ("warn", 0.98, 60)
    with pytest.raises(Exception):
        ImproveSettings(indicator_duplicate_mode="off")


# --- commit 全体 (実 DB・実 report・実 activity) --------------------------------

from types import SimpleNamespace  # noqa: E402
from unittest.mock import patch  # noqa: E402

from tests.loops.test_improve_candidate_ledger import (  # noqa: E402
    _expect_rows, _indicator, _payload, _plugin_artifact, _run,
)
from tests.loops.test_improve_e2e import (  # noqa: E402,F401
    _PASSING_INDICATOR_CONFIG, _PASSING_INDICATOR_PY, _PASSING_INDICATOR_TEST,
    improve_env,
)
from tests.loops.test_improve_notice_paths import _pair  # noqa: E402


def _files_with_params(params: str):
    return ("cand", _PASSING_INDICATOR_PY,
            _PASSING_INDICATOR_CONFIG + f"params: {params}\n"
            if "params:" not in _PASSING_INDICATOR_CONFIG
            else _PASSING_INDICATOR_CONFIG.replace("params: {}", f"params: {params}"),
            _PASSING_INDICATOR_TEST)


def _report_text(root) -> str:
    reports = sorted((root / "data" / "improve_reports").glob("improve-*.md"))
    assert len(reports) == 1
    return reports[0].read_text()


def test_commit_gate_failure_uses_the_fixed_word_everywhere(
        improve_env, monkeypatch):
    app, root = improve_env
    fakes.FakeProbe({"cand": fakes.constant}).install(monkeypatch)
    loop, ctx, _ = _run(app, root, _plugin_artifact("cand"), files=_indicator())
    conn = app.conn_core
    assert _pair(conn, ctx.mission_id) == ("gate_failed", "output_constant")
    assert conn.execute("SELECT status FROM missions WHERE id=?",
                        (ctx.mission_id,)).fetchone()[0] == "completed"
    last = conn.execute("SELECT last_result, status FROM improvement_backlog "
                        "WHERE id IN (SELECT backlog_id FROM improvement_runs "
                        "WHERE mission_id=?)", (ctx.mission_id,)).fetchone()
    assert (last["last_result"], last["status"]) == (
        "observation:gate_failed:output_constant", "observation")
    activity = (root / "logs" / "activity.log").read_text()
    assert (f"IMPROVE\tgate_failed\tmission={ctx.mission_id} "
            "reason=output_constant") in activity
    report = _report_text(root)
    assert "## Indicator gate detail" in report and "value" in report
    assert conn.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0] == 0


def test_commit_param_price_unit_is_rejected_before_the_self_test(
        improve_env, monkeypatch):
    app, root = improve_env
    probe = fakes.FakeProbe({"cand": fakes.ratio}).install(monkeypatch)
    pytest_gate = patch("agentic_fx.loops.improve_loop.run_gate_pytest")
    with pytest_gate as spy:
        loop, ctx, _ = _run(app, root, _plugin_artifact("cand"),
                            files=_files_with_params("{pip_size: 0.01}"))
    assert _pair(app.conn_core, ctx.mission_id) == ("gate_failed", "param_price_unit")
    spy.assert_not_called()
    assert probe.events == []
    assert "param_key:pip_size" in _report_text(root)


def test_commit_environment_failure_is_retryable_and_keeps_the_backlog_open(
        improve_env, monkeypatch):
    app, root = improve_env
    fakes.FakeProbe({"cand": fakes.ratio}, env_error=True).install(monkeypatch)
    loop, ctx, _ = _run(app, root, _plugin_artifact("cand"), files=_indicator())
    conn = app.conn_core
    assert _pair(conn, ctx.mission_id) == ("failed", "sandbox_unavailable")
    assert conn.execute("SELECT status FROM missions WHERE id=?",
                        (ctx.mission_id,)).fetchone()[0] == "failed"
    status = conn.execute(
        "SELECT status, last_result FROM improvement_backlog WHERE id IN "
        "(SELECT backlog_id FROM improvement_runs WHERE mission_id=?)",
        (ctx.mission_id,)).fetchone()
    assert tuple(status) == ("open", None)
    assert conn.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0] == 0
    assert (ctx.staging_dir / "cand").exists()


def test_commit_pass_puts_warnings_and_probe_reference_in_the_payload(
        improve_env, monkeypatch):
    app, root = improve_env
    fakes.FakeProbe({"cand": fakes.absolute_threshold}).install(monkeypatch)
    loop, ctx, _ = _run(app, root, _plugin_artifact("cand"), files=_indicator())
    payload = _payload(app.conn_core)
    assert payload["gate_warnings"] == ["scale_dependent"]
    assert payload["similar_to"] == []
    assert payload["fixture_timeframe"] == "1h"
    assert len(payload["fixture_data_hash"]) == 64
    activity = (root / "logs" / "activity.log").read_text()
    assert f"indicator_cache\tmission={ctx.mission_id} built=1 " in activity


def test_commit_pass_without_findings_has_empty_warning_lists(
        improve_env, monkeypatch):
    app, root = improve_env
    fakes.FakeProbe({"cand": fakes.ratio}).install(monkeypatch)
    _run(app, root, _plugin_artifact("cand"), files=_indicator())
    payload = _payload(app.conn_core)
    assert payload["gate_warnings"] == [] and payload["similar_to"] == []


def test_commit_uses_the_mode_taken_at_the_start_of_the_mission(
        improve_env, monkeypatch):
    app, root = improve_env
    fakes.FakeProbe({"cand": fakes.ratio}).install(monkeypatch)
    seen = []
    real = improve_loop.ImproveLoop._run_plugin_gate

    def spy(self, *a, **k):
        seen.append(k.get("indicator_duplicate_mode"))
        return real(self, *a, **k)

    def flip(loop, ctx):
        settings = loop._settings
        loop._settings = settings.model_copy(update={
            "improve": settings.improve.model_copy(
                update={"indicator_duplicate_mode": "enforce"})})

    _run(app, root, _plugin_artifact("cand"), files=_indicator(),
         before_commit=flip,
         extra=[patch.object(improve_loop.ImproveLoop, "_run_plugin_gate", spy)])
    assert seen == ["warn"]


def test_relation_keeps_only_deployed_names_in_the_ledger_row():
    candidate = improve_loop._FinalCandidate(
        name="c", content_hash="h", artifact_hash="a", pair=None,
        logic_hash=None, family_key=None, candidate_dir=None)
    verdict = SimpleNamespace(similar_to=(
        {"name": "rsi", "column": "v", "kind": "duplicate"},
        {"name": "rsi", "column": "w", "kind": "duplicate"},
        {"name": "pending-4", "column": "v", "kind": "similar"},
        {"name": "ema", "column": "v", "kind": "similar"}))
    ctx = SimpleNamespace(inventory=SimpleNamespace(inventory=SimpleNamespace(
        metas=(SimpleNamespace(name="rsi"), SimpleNamespace(name="ema")))))
    out = improve_loop.ImproveLoop._with_relation(candidate, verdict, ctx)
    assert out.relation == (
        {"kind": "duplicate", "name": "rsi"}, {"kind": "similar", "name": "ema"})
    assert improve_loop.ImproveLoop._with_relation(None, verdict, ctx) is None
