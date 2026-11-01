"""improve_loop の indicator gate 配線の細部: 契約検査の位置、fixture 段への引数、
終端の固定語・activity・親専有レポート、台帳の relation。"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from agentic_fx import improve_notice_codes as codes
from agentic_fx.activity import Category
from agentic_fx.loops import improve_loop
from agentic_fx.plugin import indicator_pipeline
from agentic_fx.store import candidate_archives as archives
from tests.fixtures import indicator_gate_fakes as fakes
from tests.loops.test_improve_candidate_ledger import (  # noqa: F401
    _indicator, _plugin_artifact, _run,
)
from tests.loops.test_improve_e2e import (  # noqa: F401
    _PASSING_INDICATOR_CONFIG, _PASSING_INDICATOR_PY, _PASSING_INDICATOR_TEST,
    improve_env,
)
from tests.loops.test_improve_notice_paths import _pair
from tests.loops.test_indicator_gate_wiring import (  # noqa: F401
    _candidate, _deployed, _inventory, _probe, gate_env,
)


def _rewrite_config(**config):
    def apply(directory: Path):
        current = yaml.safe_load((directory / "config.yaml").read_text())
        current.update(config)
        (directory / "config.yaml").write_text(yaml.safe_dump(current, sort_keys=False))
    return apply


# --- 契約検査は loader の直後で、以後の段を走らせない ---------------------------------------

@pytest.mark.parametrize("change,code,reason_prefix", [
    ({"max_bars": 10 ** 9}, "max_bars_limit", "max_bars_limit:"),
    ({"outputs": None}, "outputs_required", "outputs_required"),
])
def test_a_contract_violation_stops_everything_after_the_loader(
        gate_env, change, code, reason_prefix):
    change = {k: v for k, v in change.items()}

    def apply(directory):
        config = yaml.safe_load((directory / "config.yaml").read_text())
        for key, value in change.items():
            if value is None:
                config.pop(key)
            else:
                config[key] = value
        (directory / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))

    verdict = gate_env.run("usdjpy_cand", probe=_probe(), on_candidate=apply)
    assert (verdict.passed, verdict.code) == (False, code)
    assert verdict.reason.startswith(reason_prefix)
    assert verdict.failure_kind == "gate_failed"
    assert verdict.indicator_report is None
    # 銘柄名の検査も、self-test も、fixture 実行も走っていない
    assert gate_env.events == []


def test_an_unsupported_kind_stops_before_pytest(gate_env):
    def make_signal(directory):
        (directory / "plugin.py").write_text("def detect(df, params):\n    return []\n")
        (directory / "config.yaml").write_text(yaml.safe_dump(
            {"kind": "signal", "pairs": ["USDJPY"], "timeframe": "1h",
             "max_bars": 200, "params": {}}))

    verdict = gate_env.run("cand", probe=_probe(), on_candidate=make_signal)
    assert (verdict.passed, verdict.code, verdict.reason) == (
        False, "kind_unsupported", "kind_unsupported:signal")
    assert gate_env.events == []


def test_the_contract_is_judged_before_the_name_even_when_the_name_is_bad(gate_env):
    verdict = gate_env.run("usdjpy_cand", probe=_probe(),
                           on_candidate=_rewrite_config(max_bars=10 ** 9))
    assert verdict.code == "max_bars_limit"


def test_a_candidate_the_loader_rejects_is_not_name_checked(gate_env):
    verdict = gate_env.run("usdjpy_cand", probe=_probe(),
                           on_candidate=_rewrite_config(kind="bogus"))
    assert (verdict.passed, verdict.code) == (False, "loader_rejected")
    assert gate_env.events == []


def test_the_contract_classifier_orders_max_bars_then_kind_then_outputs(loop_min):
    meta = fakes.meta_for("c", Path("."), content_hash="c" * 64, max_bars=10 ** 9,
                          kind="signal", outputs=None)
    assert loop_min._contract_violation(meta)[0] == "max_bars_limit"
    meta = dataclasses.replace(meta, max_bars=200)
    assert loop_min._contract_violation(meta) == ("kind_unsupported", "kind_unsupported:signal")
    meta = dataclasses.replace(meta, kind="indicator")
    assert loop_min._contract_violation(meta) == ("outputs_required", "outputs_required")
    meta = dataclasses.replace(meta, outputs=("value",))
    assert loop_min._contract_violation(meta) is None
    limit = loop_min._settings.plugin.max_bars_limit
    assert loop_min._contract_violation(dataclasses.replace(meta, max_bars=limit)) is None
    assert loop_min._contract_violation(
        dataclasses.replace(meta, max_bars=limit + 1))[0] == "max_bars_limit"
    strategy = dataclasses.replace(meta, kind="strategy", outputs=None)
    assert loop_min._contract_violation(strategy) is None


def test_the_terminal_reason_keeps_each_historic_shape():
    reason = improve_loop.ImproveLoop._gate_failed_reason
    contract = SimpleNamespace(code="outputs_required", reason="outputs_required")
    assert reason(contract, None) == "outputs_required"
    limit = SimpleNamespace(code="max_bars_limit", reason="max_bars_limit:boom")
    assert reason(limit, None) == "gate_failed:max_bars_limit:boom"
    assert reason(SimpleNamespace(code="output_constant", reason="output_constant"),
                  "output_constant") == "output_constant"


def test_commit_judges_the_contract_with_the_same_classifier():
    source = textwrap.dedent(inspect.getsource(improve_loop.ImproveLoop.commit))
    assert "self._contract_violation(candidate_meta)" in source
    assert "assert_max_bars_within_limit" not in source
    assert "outputs_required_violation" not in source
    gate = textwrap.dedent(inspect.getsource(improve_loop.ImproveLoop._run_plugin_gate))
    assert gate.index("self._contract_violation(meta)") < gate.index(
        "indicator_pipeline.check_static")


# --- commit 全体: 契約違反は self-test も走らせない ------------------------------------------

def _config_with(**change):
    config = yaml.safe_load(_PASSING_INDICATOR_CONFIG)
    for key, value in change.items():
        if value is None:
            config.pop(key, None)
        else:
            config[key] = value
    return yaml.safe_dump(config, sort_keys=False)


def _backlog_last_result(conn, mission_id):
    return conn.execute(
        "SELECT last_result, status FROM improvement_backlog WHERE id IN "
        "(SELECT backlog_id FROM improvement_runs WHERE mission_id=?)",
        (mission_id,)).fetchone()


def test_commit_max_bars_violation_keeps_its_words_and_skips_the_self_test(
        improve_env, monkeypatch):
    app, root = improve_env
    probe = fakes.FakeProbe({"cand": fakes.ratio}).install(monkeypatch)
    with patch("agentic_fx.loops.improve_loop.run_gate_pytest") as spy:
        loop, ctx, _ = _run(app, root, _plugin_artifact("cand"), files=(
            "cand", _PASSING_INDICATOR_PY, _config_with(max_bars=10 ** 9),
            _PASSING_INDICATOR_TEST))
    spy.assert_not_called()
    assert probe.events == []
    assert _pair(app.conn_core, ctx.mission_id) == ("gate_failed", "max_bars_limit")
    last = _backlog_last_result(app.conn_core, ctx.mission_id)
    assert last["last_result"].startswith("gate_failed:max_bars_limit:")
    assert last["status"] == "observation"
    assert app.conn_core.execute(
        "SELECT COUNT(*) FROM approval_requests").fetchone()[0] == 0


def test_commit_outputs_violation_keeps_its_words_and_skips_the_self_test(
        improve_env, monkeypatch):
    app, root = improve_env
    fakes.FakeProbe({"cand": fakes.ratio}).install(monkeypatch)
    with patch("agentic_fx.loops.improve_loop.run_gate_pytest") as spy:
        loop, ctx, _ = _run(app, root, _plugin_artifact("cand"), files=(
            "cand", _PASSING_INDICATOR_PY, _config_with(outputs=None),
            _PASSING_INDICATOR_TEST))
    spy.assert_not_called()
    assert _pair(app.conn_core, ctx.mission_id) == ("gate_failed", "outputs_required")
    assert _backlog_last_result(app.conn_core, ctx.mission_id)["last_result"] == (
        "outputs_required")


# --- fixture 段に渡るもの -------------------------------------------------------------

def test_the_loop_lane_passes_mode_deployed_indicators_pending_and_static_warnings(
        tmp_path, gate_env, conn, monkeypatch):
    from agentic_fx.store import approvals as approvals_store
    from datetime import datetime, timezone
    approval_id = approvals_store.create(conn, "plugin", {
        "name": "queued", "kind": "indicator", "content_hash": "p" * 64},
        datetime(2026, 8, 22, tzinfo=timezone.utc))
    seen = []
    real = indicator_pipeline.run_fixture_stage

    def spy(meta, **kw):
        seen.append(kw)
        return real(meta, **kw)

    monkeypatch.setattr(indicator_pipeline, "run_fixture_stage", spy)
    strategy = fakes.meta_for("strat", tmp_path / "plugins" / "strat",
                              content_hash="9" * 64, kind="strategy")
    legacy = tmp_path / "plugins" / "usdjpy_cand"
    legacy.mkdir(parents=True)
    (legacy / "plugin.py").write_text("def compute(df, params):\n    return {}\n")
    (legacy / "config.yaml").write_text(yaml.safe_dump(
        {"kind": "indicator", "outputs": ["value"], "max_bars": 200}))
    deployed = fakes.meta_for("usdjpy_cand", legacy, content_hash="a" * 64)
    verdict = gate_env.run(
        "usdjpy_cand", conn=conn, mode="enforce",
        probe=fakes.FakeProbe({"usdjpy_cand": fakes.ratio}),
        deployed=[strategy, deployed])
    assert verdict.passed
    (kw,) = seen
    assert kw["lane"] == "loop" and kw["mode"] == "enforce"
    assert [m.name for m in kw["deployed_metas"]] == ["usdjpy_cand"]
    assert kw["pending"] == [(f"pending-{approval_id}", "p" * 64, "queued")]
    assert kw["static_warnings"] == ("legacy_symbol_name",)
    assert verdict.warnings == ("legacy_symbol_name",)


def test_without_a_connection_nothing_is_pending(gate_env, monkeypatch):
    seen = []
    real = indicator_pipeline.run_fixture_stage
    monkeypatch.setattr(indicator_pipeline, "run_fixture_stage",
                        lambda meta, **kw: (seen.append(kw), real(meta, **kw))[1])
    gate_env.run("cand", probe=_probe())
    assert seen[0]["pending"] == [] and seen[0]["mode"] == "warn"


def test_a_strategy_candidate_never_reaches_the_indicator_checks(
        tmp_path, loop_min, monkeypatch):
    from tests.loops.test_improve_e2e import (
        _PASSING_STRATEGY_CONFIG, _PASSING_STRATEGY_PY, _PASSING_STRATEGY_TEST)
    from agentic_fx.plugin.gate_pytest import GateResult
    monkeypatch.setattr(
        "agentic_fx.loops.improve_loop.run_gate_pytest",
        lambda plugin_dir, *, settings: GateResult(
            passed=True, returncode=0, stdout_tail="3 passed in 0.1s",
            duration_sec=0.1))
    d = tmp_path / "staging" / "strat"
    d.mkdir(parents=True)
    (d / "plugin.py").write_text(_PASSING_STRATEGY_PY)
    (d / "config.yaml").write_text(_PASSING_STRATEGY_CONFIG)
    (d / "test_plugin.py").write_text(_PASSING_STRATEGY_TEST)
    calls = []
    for attr in ("check_static", "run_fixture_stage", "pending_indicator_refs"):
        monkeypatch.setattr(indicator_pipeline, attr,
                            lambda *a, _n=attr, **k: calls.append(_n))
    source = tmp_path / "source"
    source.mkdir()
    verdict = loop_min._run_plugin_gate(d, name="strat", source_snapshot_dir=source)
    assert verdict.passed and calls == []
    assert verdict.warnings == () and verdict.indicator_report is None


# --- 終端: 環境起因の語、語彙外の code、親専有レポート ---------------------------------------

def _activity(root):
    return (root / "logs" / "activity.log").read_text()


def test_commit_environment_failure_names_the_reason_in_the_technical_log_only(
        improve_env, monkeypatch):
    app, root = improve_env
    fakes.FakeProbe({"cand": fakes.ratio}, env_error=True).install(monkeypatch)
    loop, ctx, _ = _run(app, root, _plugin_artifact("cand"), files=_indicator())
    assert (f"mission={ctx.mission_id} status=failed "
            "reason=gate_worker_sandbox_unavailable "
            "sandbox_reason=sandbox_unavailable") in _activity(root)
    assert "sandbox_unavailable" not in str(
        _backlog_last_result(app.conn_core, ctx.mission_id)["last_result"])


def test_commit_cache_failure_names_the_cache_reason(improve_env, monkeypatch):
    from agentic_fx.plugin import indicator_fixture_cache as cache
    app, root = improve_env
    fakes.FakeProbe({"cand": fakes.ratio}).install(monkeypatch)

    def locked(*a, **k):
        raise cache.IndicatorCacheLockTimeout("held")

    monkeypatch.setattr(indicator_pipeline.cache, "get_or_build", locked)
    loop, ctx, _ = _run(app, root, _plugin_artifact("cand"), files=_indicator())
    assert "sandbox_reason=fixture_cache_unavailable" in _activity(root)
    assert _pair(app.conn_core, ctx.mission_id) == ("failed", "sandbox_unavailable")


def test_a_non_indicator_gate_failure_keeps_its_plain_last_result(
        improve_env, monkeypatch):
    app, root = improve_env
    fakes.FakeProbe({"cand": fakes.ratio}).install(monkeypatch)
    loop, ctx, _ = _run(app, root, _plugin_artifact("cand"), files=(
        "cand", _PASSING_INDICATOR_PY, _PASSING_INDICATOR_CONFIG, "x = 1\n"))
    assert _pair(app.conn_core, ctx.mission_id) == ("gate_failed", "self_test_missing")
    assert _backlog_last_result(app.conn_core, ctx.mission_id)["last_result"] == (
        "gate_failed:self_test_missing")
    reports = sorted((root / "data" / "improve_reports").glob("improve-*.md"))
    text = reports[0].read_text() if reports else ""
    assert "Indicator gate detail" not in text


def test_the_indicator_section_of_the_report_is_cut_at_the_limit(
        improve_env, monkeypatch):
    app, root = improve_env
    fakes.FakeProbe({"cand": fakes.ratio}).install(monkeypatch)
    monkeypatch.setattr(
        indicator_pipeline, "check_static",
        lambda *a, **k: indicator_pipeline.IndicatorReport(
            False, "symbol_in_name", detail="x" * 5000))
    _run(app, root, _plugin_artifact("cand"), files=_indicator())
    (report,) = (root / "data" / "improve_reports").glob("improve-*.md")
    text = report.read_text()
    assert "## Indicator gate detail" in text
    assert "x" * 2000 in text and "x" * 2001 not in text


# --- activity の生成本数 ----------------------------------------------------------------

class _Recorder:
    def __init__(self):
        self.rows = []

    def write(self, category, event, message):
        self.rows.append((category, event, message))


def _cache_activity(loop, report):
    recorder = _Recorder()
    loop._activity = recorder
    ctx = SimpleNamespace(mission_id=5)
    verdict = SimpleNamespace(indicator_report=report)
    loop._write_indicator_cache_activity(ctx, verdict)
    return recorder.rows


@pytest.mark.parametrize("fields", [
    dict(built=1), dict(gc_removed=2), dict(gc_skipped=3),
    dict(built=4, gc_removed=5, gc_skipped=6)])
def test_the_cache_activity_line_is_written_for_each_nonzero_count(loop_min, fields):
    report = indicator_pipeline.IndicatorReport(**fields)
    built, removed, skipped = (fields.get(k, 0) for k in (
        "built", "gc_removed", "gc_skipped"))
    assert _cache_activity(loop_min, report) == [(
        Category.IMPROVE, "indicator_cache",
        f"mission=5 built={built} gc_removed={removed} gc_skipped={skipped}")]


def test_the_cache_activity_line_is_silent_when_nothing_happened(loop_min):
    assert _cache_activity(loop_min, indicator_pipeline.IndicatorReport()) == []
    assert _cache_activity(loop_min, None) == []


def test_the_cache_activity_tolerates_a_loop_without_an_activity_log(loop_min):
    loop_min._activity = None
    loop_min._write_indicator_cache_activity(
        SimpleNamespace(mission_id=5),
        SimpleNamespace(indicator_report=indicator_pipeline.IndicatorReport(built=1)))


def test_commit_reports_the_stale_entries_it_collected(improve_env, monkeypatch):
    from agentic_fx.plugin import indicator_fixture_cache as cache
    app, root = improve_env
    fakes.FakeProbe({"cand": fakes.ratio}).install(monkeypatch)
    index = indicator_pipeline.pd.date_range("2026-01-01", periods=3, freq="h", tz="UTC")
    stale = cache.CacheKey("9" * 64, "8" * 64, "1h")
    cache.write(root / "data", stale, cache.CacheEntry(outputs={
        "v": indicator_pipeline.pd.Series([1.0, 2.0, 3.0], index=index)}))
    _run(app, root, _plugin_artifact("cand"), files=_indicator())
    (line,) = [l for l in _activity(root).splitlines() if "indicator_cache" in l]
    assert "built=1 gc_removed=" in line and "gc_removed=0" not in line
    assert cache.read(root / "data", stale) is None


# --- 台帳の relation -----------------------------------------------------------------------

def _with_deployed(ctx, root, *names):
    inventory = _inventory(root, *[_deployed(root, n, "a") for n in names])
    object.__setattr__(ctx, "inventory", inventory)


def _candidate_row(conn, mission_id, name="cand"):
    return next(r for r in archives.list_by_mission(conn, mission_id)
                if r["name"] == name)


def test_a_rejected_duplicate_leaves_the_deployed_partner_in_the_ledger_row(
        improve_env, monkeypatch):
    app, root = improve_env
    fakes.FakeProbe({"cand": fakes.ratio, "rsi": fakes.ratio}).install(monkeypatch)
    loop, ctx, _ = _run(
        app, root, _plugin_artifact("cand"), files=_indicator(),
        before_commit=lambda loop, ctx: _with_deployed(ctx, root, "rsi"))
    assert _pair(app.conn_core, ctx.mission_id) == ("gate_failed", "indicator_duplicate")
    row = _candidate_row(app.conn_core, ctx.mission_id)
    assert row["relation"] == [{"kind": "duplicate", "name": "rsi"}]
    assert row["outcome_class"] == "gate_failed"


def test_a_passing_candidate_with_a_monotone_twin_records_it_and_the_warning(
        improve_env, monkeypatch):
    from tests.loops.test_improve_candidate_ledger import _payload
    app, root = improve_env
    fakes.FakeProbe({"cand": fakes.ratio, "rsi": fakes.affine_of_ratio}
                    ).install(monkeypatch)
    loop, ctx, _ = _run(
        app, root, _plugin_artifact("cand"), files=_indicator(),
        before_commit=lambda loop, ctx: _with_deployed(ctx, root, "rsi"))
    row = _candidate_row(app.conn_core, ctx.mission_id)
    assert row["relation"] == [{"kind": "monotone", "name": "rsi"}]
    payload = _payload(app.conn_core)
    assert payload["gate_warnings"] == ["indicator_monotone_duplicate"]
    assert payload["similar_to"] == [
        {"name": "rsi", "column": "value", "kind": "monotone"}]


# --- 数値は親専有レポートにだけ ------------------------------------------------------------

def test_the_numbers_reach_the_parent_only_report_and_no_agent_surface(
        improve_env, monkeypatch):
    import json
    import re
    from datetime import datetime, timezone
    from agentic_fx.loops import improve_context
    app, root = improve_env
    fakes.FakeProbe({"cand": fakes.ratio, "rsi": fakes.affine_of_ratio}
                    ).install(monkeypatch)

    def setup(loop, ctx):
        _with_deployed(ctx, root, "rsi")
        object.__setattr__(ctx, "indicator_duplicate_mode", "enforce")

    loop, ctx, _ = _run(app, root, _plugin_artifact("cand"),
                        files=_indicator(), before_commit=setup)
    conn = app.conn_core
    assert _pair(conn, ctx.mission_id) == (
        "gate_failed", "indicator_monotone_duplicate")
    (report,) = (root / "data" / "improve_reports").glob("improve-*.md")
    found = re.search(
        r"- monotone: rsi\.value \(column=value level_spearman=(\d\.\d{4}) "
        r"diff_spearman=(\d\.\d{4})\)", report.read_text())
    assert found, report.read_text()
    numbers = set(found.groups())
    last = _backlog_last_result(conn, ctx.mission_id)["last_result"]
    data = improve_context.build_improve_context(
        conn, settings=app.settings,
        now=datetime(2026, 8, 22, 3, 0, tzinfo=timezone.utc), root=root,
        allowed_backlog_ids=None, inventory_metas=[_deployed(root, "rsi", "a")])
    section = data["improvement_history"]["family_attempts"]
    surfaces = [last, json.dumps(data, default=str),
                improve_context.render_family_attempts(section),
                json.dumps(_candidate_row(conn, ctx.mission_id)["relation"])]
    for text in surfaces:
        for number in numbers:
            assert number not in text
    assert last == "observation:gate_failed:indicator_monotone_duplicate" or \
        last == "indicator_monotone_duplicate"
