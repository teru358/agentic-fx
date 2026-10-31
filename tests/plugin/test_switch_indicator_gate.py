"""人間回廊 (submit / bless / approve) の indicator gate。

submit は改善ループと同じ reject、bless は政策検査を warning にして構造的失敗と
exact の重複だけ reject、approve は配備の直前に exact と単調変換を再判定する。
fixture 実行は差し替え、分類・比較・cache は本物を通す。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from agentic_fx.config import load_settings
from agentic_fx.plugin import indicator_fixture_cache as cache
from agentic_fx.plugin import indicator_pipeline, switch
from agentic_fx.plugin.resolve import ApprovedInventory, InventoryBuildResult
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import db as db_store
from tests.fixtures import indicator_gate_fakes as fakes

NOW = datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)
_EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"
_PLUGIN = "def compute(df, params):\n    return {'value': df['close'].pct_change()}\n"


def _fake_pytest_ok(plugin_dir, *, settings):
    from agentic_fx.plugin.gate_pytest import GateResult
    return GateResult(passed=True, returncode=0, stdout_tail="1 passed",
                      duration_sec=0.1)


def _write(dirpath: Path, *, params=None) -> None:
    dirpath.mkdir(parents=True)
    (dirpath / "plugin.py").write_text(_PLUGIN)
    (dirpath / "config.yaml").write_text(yaml.safe_dump(
        {"kind": "indicator", "outputs": ["value"], "max_bars": 200,
         "params": params or {}}, sort_keys=False))
    (dirpath / "test_plugin.py").write_text("def test_x():\n    pass\n")


def _with_mode(settings, mode):
    return settings.model_copy(update={"improve": settings.improve.model_copy(
        update={"indicator_duplicate_mode": mode})})


@pytest.fixture
def env(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / ".locks").mkdir()
    conn = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(conn)
    settings = load_settings(_EXAMPLE)
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)

    class Env:
        pass

    e = Env()
    e.root, e.plugins, e.conn, e.settings = tmp_path, plugins_dir, conn, settings
    e.deployed = []

    def install(outputs_by_name, **kw):
        fake = fakes.FakeProbe(outputs_by_name, **kw).install(monkeypatch)
        inventory = InventoryBuildResult(
            inventory=ApprovedInventory(root=plugins_dir, metas=tuple(e.deployed)),
            phase1_metas=(), resolved={}, rejected_strategies=())
        monkeypatch.setattr(
            "agentic_fx.plugin.switch.tools_plugin_loader.approved_plugins",
            lambda *a, **k: inventory)
        return fake

    e.install = install

    def deploy(name, tag):
        e.deployed.append(fakes.meta_for(
            name, plugins_dir / name, content_hash=tag * 64))

    e.deploy = deploy

    def submit(name, *, settings=None, params=None):
        _write(plugins_dir / "_staging" / "1" / name, params=params)
        return switch.submit_candidate(
            conn, name=name, staging_dir=plugins_dir / "_staging" / "1",
            candidate_origin="staging", mission_id=1, backlog_id=None,
            settings=settings or e.settings, now=NOW)

    e.submit = submit

    def gate(name, *, floor_mode, settings=None, params=None):
        d = plugins_dir / "_human" / name
        _write(d, params=params)
        return switch._run_full_gate(
            conn, d, name=name, settings=settings or e.settings, now=NOW,
            floor_mode=floor_mode, plugins_root=plugins_dir)

    e.gate = gate
    yield e
    conn.close()


def _payload(conn, approval_id):
    row = conn.execute("SELECT payload_json FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    return json.loads(row["payload_json"])


# --- submit: 改善ループと同じ reject --------------------------------------------

def test_submit_rejects_a_symbol_name_with_the_fixed_word(env):
    env.install({"usdjpy_x": fakes.ratio})
    with pytest.raises(ValueError, match="symbol_in_name"):
        env.submit("usdjpy_x")
    assert approvals_store.pending(env.conn, kind="plugin") == []


def test_submit_rejects_a_price_unit_param(env):
    env.install({"cand": fakes.ratio})
    with pytest.raises(ValueError, match="param_price_unit"):
        env.submit("cand", params={"pip_size": 0.01})


def test_submit_rejects_an_exact_duplicate_and_creates_no_request(env):
    env.deploy("rsi", "a")
    env.install({"cand": fakes.ratio, "rsi": fakes.ratio})
    with pytest.raises(ValueError, match="indicator_duplicate"):
        env.submit("cand")
    assert approvals_store.pending(env.conn, kind="plugin") == []


def test_submit_enforce_mode_rejects_a_monotone_duplicate(env):
    env.deploy("rsi", "a")
    env.install({"cand": fakes.ratio, "rsi": fakes.affine_of_ratio})
    with pytest.raises(ValueError, match="indicator_monotone_duplicate"):
        env.submit("cand", settings=_with_mode(env.settings, "enforce"))


def test_submit_warn_mode_carries_the_monotone_duplicate_as_a_warning(env):
    env.deploy("rsi", "a")
    env.install({"cand": fakes.ratio, "rsi": fakes.affine_of_ratio})
    payload = _payload(env.conn, env.submit("cand"))
    assert payload["gate_warnings"] == ["indicator_monotone_duplicate"]
    assert payload["similar_to"] == [
        {"name": "rsi", "column": "value", "kind": "monotone"}]


def test_submit_payload_holds_the_probe_reference_for_the_approve_time_check(env):
    env.install({"cand": fakes.ratio})
    payload = _payload(env.conn, env.submit("cand"))
    assert payload["gate_warnings"] == [] and payload["similar_to"] == []
    assert payload["fixture_timeframe"] == "1h"
    key = cache.CacheKey(payload["content_hash"], payload["fixture_data_hash"], "1h")
    assert cache.read(env.root / "data", key) is not None


def test_submit_environment_failure_is_a_fixed_word_and_creates_nothing(env):
    env.install({"cand": fakes.ratio}, env_error=True)
    with pytest.raises(ValueError, match="indicator_gate_unavailable"):
        env.submit("cand")
    assert approvals_store.pending(env.conn, kind="plugin") == []


# --- bless: 政策検査は warning、構造的失敗と exact は reject -----------------------

def test_bless_lane_turns_name_and_param_findings_into_warnings(env):
    env.install({"usdjpy_x": fakes.ratio})
    *_, outcome = env.gate("usdjpy_x", floor_mode="warn",
                           params={"pip_size": 0.01})
    assert outcome.gate_warnings == ("symbol_in_name", "param_price_unit")


def test_bless_lane_never_rejects_a_monotone_duplicate_even_if_enforce(env):
    env.deploy("rsi", "a")
    env.install({"cand": fakes.ratio, "rsi": fakes.affine_of_ratio})
    *_, outcome = env.gate("cand", floor_mode="warn",
                           settings=_with_mode(env.settings, "enforce"))
    assert "indicator_monotone_duplicate" in outcome.gate_warnings


@pytest.mark.parametrize("kwargs,word", [
    ({"contract_failure": "exception"}, "fixture_run_failed"),
])
def test_bless_lane_rejects_structural_failures(env, kwargs, word):
    env.install({"cand": fakes.ratio}, **kwargs)
    with pytest.raises(ValueError, match=word):
        env.gate("cand", floor_mode="warn")


@pytest.mark.parametrize("fn,word", [
    (fakes.all_nan, "output_not_finite"), (fakes.constant, "output_constant")])
def test_bless_lane_rejects_unusable_outputs(env, fn, word):
    env.install({"cand": fn})
    with pytest.raises(ValueError, match=word):
        env.gate("cand", floor_mode="warn")


def test_bless_lane_rejects_an_exact_duplicate(env):
    env.deploy("rsi", "a")
    env.install({"cand": fakes.ratio, "rsi": fakes.ratio})
    with pytest.raises(ValueError, match="indicator_duplicate"):
        env.gate("cand", floor_mode="warn")


def test_bless_lane_proceeds_with_duplicate_unavailable_when_only_a_deployed_cache_fails(
        env):
    env.deploy("rsi", "a")
    env.install({"cand": fakes.ratio, "rsi": fakes.ratio}, fail_names=frozenset({"rsi"}))
    *_, outcome = env.gate("cand", floor_mode="warn")
    assert "duplicate_unavailable" in outcome.gate_warnings


def _fail_cache_of(monkeypatch, content_hash):
    real = indicator_pipeline.cache.get_or_build

    def wrapper(data_dir, key, builder, **kw):
        if key.content_hash == content_hash:
            raise OSError("cache unavailable")
        return real(data_dir, key, builder, **kw)

    monkeypatch.setattr(indicator_pipeline.cache, "get_or_build", wrapper)


def test_submit_lane_fails_retryably_when_a_deployed_cache_cannot_be_built(
        env, monkeypatch):
    env.deploy("rsi", "a")
    env.install({"cand": fakes.ratio, "rsi": fakes.ratio})
    _fail_cache_of(monkeypatch, "a" * 64)
    with pytest.raises(ValueError, match="indicator_gate_unavailable"):
        env.gate("cand", floor_mode="enforce")


def test_submit_lane_passes_with_a_warning_when_a_deployed_indicator_itself_fails(env):
    env.deploy("rsi", "a")
    env.install({"cand": fakes.ratio, "rsi": fakes.ratio}, fail_names=frozenset({"rsi"}))
    *_, outcome = env.gate("cand", floor_mode="enforce")
    assert "duplicate_unavailable" in outcome.gate_warnings
    assert outcome.indicator_report.unavailable == (("rsi", "probe_exception"),)


def test_submit_writes_the_dropped_partner_and_its_detail_to_the_activity(env):
    env.deploy("rsi", "a")
    env.install({"cand": fakes.ratio, "rsi": fakes.ratio}, fail_names=frozenset({"rsi"}))
    written = []

    class Spy:
        def write(self, category, event, text, *a, **k):
            written.append((event, text))

    d = env.plugins / "_human" / "cand"
    _write(d)
    switch._run_full_gate(
        env.conn, d, name="cand", settings=env.settings, now=NOW,
        floor_mode="enforce", plugins_root=env.plugins, activity=Spy())
    assert ("indicator_partner_unavailable",
            "name=cand partner=rsi detail=probe_exception") in written


def test_the_inventory_is_built_once_for_an_indicator_gate(env, monkeypatch):
    env.install({"cand": fakes.ratio})
    calls = []
    real = switch.tools_plugin_loader.approved_plugins

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(switch.tools_plugin_loader, "approved_plugins", counting)
    env.gate("cand", floor_mode="enforce")
    assert len(calls) == 1


def test_bless_lane_fails_retryably_when_the_candidate_itself_cannot_run(env):
    env.install({"cand": fakes.ratio}, env_error=True)
    with pytest.raises(ValueError, match="indicator_gate_unavailable"):
        env.gate("cand", floor_mode="warn")


# --- 3 入口の payload は同じ形 ------------------------------------------------------

def test_the_payload_fields_have_one_shape_for_every_entry_point(env):
    env.install({"cand": fakes.absolute_threshold})
    *_, outcome = env.gate("cand", floor_mode="warn")
    assert set(indicator_pipeline.payload_fields(outcome.indicator_report)) == {
        "gate_warnings", "similar_to", "fixture_data_hash", "fixture_timeframe"}
    assert switch._indicator_payload_fields(outcome)["gate_warnings"] == [
        "scale_dependent"]


def test_payload_warnings_are_closed_vocabulary_deduplicated_and_capped():
    report = indicator_pipeline.IndicatorReport(
        warnings=("scale_dependent",) * 3 + ("made_up", "indicator_similar")
        + tuple(f"w{i}" for i in range(20)))
    fields = indicator_pipeline.payload_fields(report)
    assert fields["gate_warnings"] == ["scale_dependent", "indicator_similar"]
    from agentic_fx import improve_notice_codes as codes
    every = tuple(sorted(codes.INDICATOR_PAYLOAD_WARNING_CODES))
    assert len(every) > indicator_pipeline.MAX_WARNINGS
    capped = indicator_pipeline.payload_fields(
        indicator_pipeline.IndicatorReport(warnings=every))["gate_warnings"]
    assert capped == list(every[:indicator_pipeline.MAX_WARNINGS])
    wide = indicator_pipeline.IndicatorReport(similar_to=tuple(
        {"name": f"n{i}", "column": "v", "kind": "similar"} for i in range(5)))
    assert len(indicator_pipeline.payload_fields(wide)["similar_to"]) == 3


# --- approve: 配備の直前の再判定 (J6) -------------------------------------------------

def _submit_then_deploy_a_twin(env, mode):
    env.install({"cand": fakes.ratio, "twin": fakes.ratio})
    settings = _with_mode(env.settings, mode)
    approval_id = env.submit("cand", settings=settings)
    # 提出後に、同じ式の別名が配備された
    env.deploy("twin", "b")
    env.install({"cand": fakes.ratio, "twin": fakes.ratio})
    return approval_id, settings


def test_approve_enforce_refuses_a_twin_deployed_after_submission(env):
    approval_id, settings = _submit_then_deploy_a_twin(env, "enforce")
    with pytest.raises(ValueError, match="duplicate_at_approve"):
        switch.approve_candidate(
            env.conn, approval_id, decided_by="alice", now=NOW,
            plugins_root=env.plugins, settings=settings)
    row = env.conn.execute(
        "SELECT status FROM approval_requests WHERE id=?", (approval_id,)).fetchone()
    assert row["status"] == "pending"
    assert not (env.plugins / ".versions").exists()


def test_approve_warn_updates_the_warning_instead_of_refusing(env):
    approval_id, settings = _submit_then_deploy_a_twin(env, "warn")
    payload = _payload(env.conn, approval_id)
    updated = switch._rejudge_indicator_at_approval(
        env.conn, approval_id, payload, settings=settings, plugins_root=env.plugins)
    assert "duplicate_at_approve" in updated["gate_warnings"]
    assert "indicator_output_overlap" not in updated["gate_warnings"]
    assert updated["similar_to"][0] == {
        "name": "twin", "column": "value", "kind": "duplicate"}
    assert _payload(env.conn, approval_id) == updated


def test_approve_does_not_rejudge_a_payload_without_probe_output(env):
    approval_id, settings = _submit_then_deploy_a_twin(env, "enforce")
    payload = _payload(env.conn, approval_id)
    for key in ("fixture_data_hash", "fixture_timeframe"):
        payload.pop(key)
    assert switch._rejudge_indicator_at_approval(
        env.conn, approval_id, payload, settings=settings,
        plugins_root=env.plugins) == payload


def test_approve_does_not_rejudge_when_the_probe_output_is_gone(env):
    approval_id, settings = _submit_then_deploy_a_twin(env, "enforce")
    payload = _payload(env.conn, approval_id)
    payload["content_hash"] = "0" * 64
    assert switch._rejudge_indicator_at_approval(
        env.conn, approval_id, payload, settings=settings,
        plugins_root=env.plugins) == payload


def test_approve_rejudge_ignores_the_candidates_own_deployed_version(env):
    env.install({"cand": fakes.ratio})
    approval_id = env.submit("cand", settings=_with_mode(env.settings, "enforce"))
    env.deploy("cand", "c")
    env.install({"cand": fakes.ratio})
    payload = _payload(env.conn, approval_id)
    assert switch._rejudge_indicator_at_approval(
        env.conn, approval_id, payload,
        settings=_with_mode(env.settings, "enforce"),
        plugins_root=env.plugins) == payload


# --- 人間回廊の段の順序 (改善ループの gate と同じ) --------------------------------------

def _trace_stages(monkeypatch):
    events: list[str] = []

    def wrap(owner, attr, tag):
        real = getattr(owner, attr)

        def traced(*a, **k):
            events.append(tag)
            return real(*a, **k)

        monkeypatch.setattr(owner, attr, traced)

    wrap(switch, "check_candidate_snapshot", "snapshot")
    wrap(switch, "check_source", "source")
    wrap(switch.loader, "_discover_one", "loader")
    wrap(switch.approval, "outputs_required_violation", "outputs")
    wrap(switch.approval, "assert_max_bars_within_limit", "max_bars")
    wrap(switch.indicator_pipeline, "check_static", "static")
    wrap(switch, "run_gate_pytest", "pytest")
    return events


@pytest.mark.parametrize("floor_mode", ["enforce", "warn"])
def test_the_human_corridor_runs_cheap_checks_first(env, monkeypatch, floor_mode):
    env.install({"cand": fakes.ratio})
    events = _trace_stages(monkeypatch)
    env.gate("cand", floor_mode=floor_mode)
    assert events == ["snapshot", "source", "source", "loader", "outputs",
                      "max_bars", "static", "pytest"]


def _write_raw(dirpath: Path, *, plugin_py=_PLUGIN, config=None) -> None:
    dirpath.mkdir(parents=True)
    (dirpath / "plugin.py").write_text(plugin_py)
    (dirpath / "config.yaml").write_text(yaml.safe_dump(
        config or {"kind": "indicator", "outputs": ["value"], "max_bars": 200,
                   "params": {}}, sort_keys=False))
    (dirpath / "test_plugin.py").write_text("def test_x():\n    pass\n")


def _gate_raw(env, name, **kw):
    d = env.plugins / "_human" / name
    _write_raw(d, **kw)
    return switch._run_full_gate(
        env.conn, d, name=name, settings=env.settings, now=NOW,
        floor_mode="warn", plugins_root=env.plugins)


def test_a_rejected_source_stops_before_the_name_check_and_any_run(env, monkeypatch):
    env.install({"usdjpy_x": fakes.ratio})
    events = _trace_stages(monkeypatch)
    with pytest.raises(ValueError) as raised:
        _gate_raw(env, "usdjpy_x", plugin_py="import os\n" + _PLUGIN)
    assert "symbol_in_name" not in str(raised.value)
    assert "static" not in events and "pytest" not in events and "loader" not in events


def test_a_candidate_the_loader_rejects_is_not_statically_checked(env, monkeypatch):
    env.install({"usdjpy_x": fakes.ratio})
    events = _trace_stages(monkeypatch)
    with pytest.raises(ValueError, match="failed discovery"):
        _gate_raw(env, "usdjpy_x", config={"kind": "bogus_kind"})
    assert events[-1] == "loader"


def test_a_missing_outputs_declaration_stops_before_the_name_check(env, monkeypatch):
    env.install({"usdjpy_x": fakes.ratio})
    events = _trace_stages(monkeypatch)
    with pytest.raises(ValueError, match="^outputs_required$"):
        _gate_raw(env, "usdjpy_x", config={"kind": "indicator", "max_bars": 200})
    assert "static" not in events and "pytest" not in events


def test_an_oversized_max_bars_stops_before_the_name_check_and_pytest(env, monkeypatch):
    env.install({"usdjpy_x": fakes.ratio})
    events = _trace_stages(monkeypatch)
    limit = env.settings.plugin.max_bars_limit
    with pytest.raises(ValueError, match="max_bars_limit"):
        _gate_raw(env, "usdjpy_x", config={
            "kind": "indicator", "outputs": ["value"], "max_bars": limit + 1})
    assert "static" not in events and "pytest" not in events


# --- pending を比較相手にする (自分自身の再提出を除く) --------------------------------------

def _submit_in(env, name, mission, *, params=None):
    _write(env.plugins / "_staging" / str(mission) / name, params=params)
    return switch.submit_candidate(
        env.conn, name=name, staging_dir=env.plugins / "_staging" / str(mission),
        candidate_origin="staging", mission_id=mission, backlog_id=None,
        settings=env.settings, now=NOW)


def test_a_pending_of_the_same_name_and_other_content_is_a_comparison_partner(env):
    env.install({"cand": fakes.ratio})
    first = _submit_in(env, "cand", 1)
    with pytest.raises(ValueError, match="indicator_duplicate"):
        _submit_in(env, "cand", 2, params={"window": 5})
    assert [r["id"] for r in approvals_store.pending(env.conn, kind="plugin")] == [first]


def test_resubmitting_the_identical_candidate_is_not_compared_with_itself(env):
    env.install({"cand": fakes.ratio})
    first = _submit_in(env, "cand", 1)
    second = _submit_in(env, "cand", 2)
    assert second != first
    assert _payload(env.conn, second)["similar_to"] == []


def test_a_similar_pending_is_named_by_approval_id_in_the_payload(env):
    env.install({"cand": fakes.affine_of_ratio, "other": fakes.ratio})
    first = _submit_in(env, "other", 1)
    second = _submit_in(env, "cand", 2, params={"window": 5})
    assert _payload(env.conn, second)["similar_to"] == [
        {"name": f"pending-{first}", "column": "value", "kind": "monotone"}]


def test_the_fixture_stage_gets_lane_mode_deployed_indicators_and_pending(env, monkeypatch):
    env.deploy("rsi", "a")
    env.metas = env.deployed
    env.deployed.append(fakes.meta_for(
        "strat", env.plugins / "strat", content_hash="9" * 64, kind="strategy"))
    env.install({"cand": fakes.ratio, "cand2": fakes.ratio, "rsi": fakes.noise})
    seen = []
    real = indicator_pipeline.run_fixture_stage

    def spy(meta, **kw):
        seen.append(kw)
        return real(meta, **kw)

    monkeypatch.setattr(indicator_pipeline, "run_fixture_stage", spy)
    env.gate("cand", floor_mode="enforce", settings=_with_mode(env.settings, "enforce"))
    env.gate("cand2", floor_mode="warn")
    submit, bless = seen
    assert (submit["lane"], submit["mode"]) == ("submit", "enforce")
    assert (bless["lane"], bless["mode"]) == ("bless", "warn")
    assert [m.name for m in submit["deployed_metas"]] == ["rsi"]
    assert submit["pending"] == []
    assert submit["static_warnings"] == ()


def test_a_bless_static_warning_reaches_the_outcome_and_the_stage(env, monkeypatch):
    env.install({"usdjpy_x": fakes.ratio})
    seen = []
    real = indicator_pipeline.run_fixture_stage

    def spy(meta, **kw):
        seen.append(kw["static_warnings"])
        return real(meta, **kw)

    monkeypatch.setattr(indicator_pipeline, "run_fixture_stage", spy)
    *_, outcome = env.gate("usdjpy_x", floor_mode="warn")
    assert seen == [("symbol_in_name",)]
    assert outcome.gate_warnings == ("symbol_in_name",)


def test_the_outcome_carries_the_similar_rows_and_report(env):
    env.deploy("rsi", "a")
    env.install({"cand": fakes.ratio, "rsi": fakes.affine_of_ratio})
    *_, outcome = env.gate("cand", floor_mode="warn")
    assert outcome.similar_to == ({"name": "rsi", "column": "value", "kind": "monotone"},)
    assert outcome.indicator_report.similar_to == outcome.similar_to
    assert outcome.gate_warnings == ("indicator_monotone_duplicate",)


def test_a_deployed_strategy_is_not_a_comparison_partner(env):
    env.deployed.append(fakes.meta_for(
        "strat", env.plugins / "strat", content_hash="9" * 64, kind="strategy"))
    env.install({"cand": fakes.ratio})
    *_, outcome = env.gate("cand", floor_mode="enforce")
    assert outcome.similar_to == ()


# --- approve の再判定の配線 -------------------------------------------------------------

class _Reached(Exception):
    pass


def _reach_after_the_rejudge(env, monkeypatch, *, journal=None):
    """approve を、再判定の判断の直後 (候補ディレクトリの二度目の解決) で止める。"""
    calls = {"rejudge": [], "resolve": 0}

    def fake_rejudge(conn, approval_id, payload, **kw):
        calls["rejudge"].append(approval_id)
        return payload

    real_resolve = switch.resolve_candidate_dir

    def counting_resolve(*a, **k):
        calls["resolve"] += 1
        if calls["resolve"] >= 2:
            raise _Reached
        return real_resolve(*a, **k)

    monkeypatch.setattr(switch, "_rejudge_indicator_at_approval", fake_rejudge)
    monkeypatch.setattr(switch, "resolve_candidate_dir", counting_resolve)
    if journal is not None:
        monkeypatch.setattr(switch.journal_store, "get_open_by_name",
                            lambda conn, name: journal)
    return calls


def _approve(env, approval_id):
    return switch.approve_candidate(
        env.conn, approval_id, decided_by="alice", now=NOW,
        plugins_root=env.plugins, settings=env.settings)


def test_approve_rejudges_an_indicator_before_anything_is_deployed(env, monkeypatch):
    env.install({"cand": fakes.ratio})
    approval_id = env.submit("cand")
    calls = _reach_after_the_rejudge(env, monkeypatch)
    with pytest.raises(_Reached):
        _approve(env, approval_id)
    assert calls["rejudge"] == [approval_id]


def test_approve_does_not_rejudge_when_it_resumes_its_own_journal(env, monkeypatch):
    env.install({"cand": fakes.ratio})
    approval_id = env.submit("cand")
    journal = {"approval_id": approval_id, "op_id": 7, "phase": "recorded",
               "switch_required": 1, "old_kind": "absent", "old_target": None,
               "new_target": "x"}
    calls = _reach_after_the_rejudge(env, monkeypatch, journal=journal)
    with pytest.raises(_Reached):
        _approve(env, approval_id)
    assert calls["rejudge"] == []


def test_approve_does_not_rejudge_a_payload_that_is_not_an_indicator(env, monkeypatch):
    env.install({"cand": fakes.ratio})
    approval_id = env.submit("cand")
    payload = _payload(env.conn, approval_id)
    payload["kind"] = "strategy"
    env.conn.execute("UPDATE approval_requests SET payload_json=? WHERE id=?",
                     (json.dumps(payload), approval_id))
    env.conn.commit()
    calls = _reach_after_the_rejudge(env, monkeypatch)
    with pytest.raises(_Reached):
        _approve(env, approval_id)
    assert calls["rejudge"] == []


def _row_status(env, approval_id):
    return env.conn.execute("SELECT status FROM approval_requests WHERE id=?",
                            (approval_id,)).fetchone()["status"]


@pytest.mark.parametrize("mode", ["warn", "enforce"])
def test_approve_stays_pending_when_a_deployed_cache_cannot_be_built(
        env, mode, monkeypatch):
    env.install({"cand": fakes.ratio, "twin": fakes.ratio})
    settings = _with_mode(env.settings, mode)
    approval_id = env.submit("cand", settings=settings)
    env.deploy("twin", "b")
    env.install({"cand": fakes.ratio, "twin": fakes.ratio})
    _fail_cache_of(monkeypatch, "b" * 64)
    with pytest.raises(ValueError, match="^indicator_gate_unavailable$") as raised:
        switch.approve_candidate(
            env.conn, approval_id, decided_by="alice", now=NOW,
            plugins_root=env.plugins, settings=settings)
    assert isinstance(raised.value.__cause__, indicator_pipeline.IndicatorGateUnavailable)
    assert _row_status(env, approval_id) == "pending"
    assert not (env.plugins / ".versions").exists()


def test_approve_stays_pending_on_a_cache_lock_timeout_in_warn_mode(env, monkeypatch):
    approval_id, settings = _submit_then_deploy_a_twin(env, "warn")
    real = cache.get_or_build

    def wrapper(data_dir, key, builder, **kw):
        if key.content_hash == "b" * 64:
            raise cache.IndicatorCacheLockTimeout("held")
        return real(data_dir, key, builder, **kw)

    monkeypatch.setattr(indicator_pipeline.cache, "get_or_build", wrapper)
    with pytest.raises(ValueError, match="indicator_gate_unavailable"):
        switch.approve_candidate(
            env.conn, approval_id, decided_by="alice", now=NOW,
            plugins_root=env.plugins, settings=settings)
    assert _row_status(env, approval_id) == "pending"


def test_approve_warn_merges_the_new_findings_into_the_existing_payload(env):
    env.install({"cand": fakes.absolute_threshold, "twin": fakes.absolute_threshold})
    settings = _with_mode(env.settings, "warn")
    approval_id = env.submit("cand", settings=settings)
    payload = _payload(env.conn, approval_id)
    assert payload["gate_warnings"] == ["scale_dependent"]
    payload["similar_to"] = [{"name": "older", "column": "v", "kind": "similar"}]
    env.conn.execute("UPDATE approval_requests SET payload_json=? WHERE id=?",
                     (json.dumps(payload), approval_id))
    env.conn.commit()
    env.deploy("twin", "b")
    env.install({"cand": fakes.absolute_threshold, "twin": fakes.absolute_threshold})
    updated = switch._rejudge_indicator_at_approval(
        env.conn, approval_id, payload, settings=settings, plugins_root=env.plugins)
    assert updated["gate_warnings"] == ["scale_dependent", "duplicate_at_approve"]
    assert updated["similar_to"] == [
        {"name": "twin", "column": "value", "kind": "duplicate"},
        {"name": "older", "column": "v", "kind": "similar"}]
    assert _payload(env.conn, approval_id) == updated


def test_approve_warn_does_not_write_when_nothing_changed(env):
    env.install({"cand": fakes.ratio})
    approval_id = env.submit("cand")
    payload = _payload(env.conn, approval_id)
    before = env.conn.total_changes
    out = switch._rejudge_indicator_at_approval(
        env.conn, approval_id, payload, settings=_with_mode(env.settings, "warn"),
        plugins_root=env.plugins)
    assert out is payload and env.conn.total_changes == before


def test_the_rewritten_payload_only_lands_on_a_pending_row(env):
    approval_id, settings = _submit_then_deploy_a_twin(env, "warn")
    payload = _payload(env.conn, approval_id)
    env.conn.execute("UPDATE approval_requests SET status='rejected' WHERE id=?",
                     (approval_id,))
    env.conn.commit()
    switch._rejudge_indicator_at_approval(
        env.conn, approval_id, payload, settings=settings, plugins_root=env.plugins)
    assert _payload(env.conn, approval_id) == payload


def test_approve_enforce_refuses_a_monotone_twin_too(env):
    env.install({"cand": fakes.ratio, "twin": fakes.affine_of_ratio})
    settings = _with_mode(env.settings, "enforce")
    approval_id = env.submit("cand", settings=settings)
    env.deploy("twin", "b")
    env.install({"cand": fakes.ratio, "twin": fakes.affine_of_ratio})
    with pytest.raises(ValueError, match="^duplicate_at_approve$"):
        switch.approve_candidate(
            env.conn, approval_id, decided_by="alice", now=NOW,
            plugins_root=env.plugins, settings=settings)
    assert _row_status(env, approval_id) == "pending"


# --- approve 再判定: 世代違い・由来・書込みの確認 ---------------------------------------

def test_approve_does_not_rejudge_a_payload_of_another_fixture_generation(
        env, monkeypatch):
    approval_id, settings = _submit_then_deploy_a_twin(env, "enforce")
    payload = _payload(env.conn, approval_id)
    old_key = indicator_pipeline.cache.CacheKey(
        payload["content_hash"], payload["fixture_data_hash"],
        payload["fixture_timeframe"])
    entry = indicator_pipeline.cache.read(env.root / "data", old_key)
    assert entry is not None
    indicator_pipeline.cache.get_or_build(
        env.root / "data", indicator_pipeline.cache.CacheKey(
            payload["content_hash"], "f" * 64, payload["fixture_timeframe"]),
        lambda: entry)
    payload["fixture_data_hash"] = "f" * 64
    built = []
    real = indicator_pipeline.cache.get_or_build

    def spy(*a, **k):
        built.append(a)
        return real(*a, **k)

    monkeypatch.setattr(indicator_pipeline.cache, "get_or_build", spy)
    before = env.conn.total_changes
    assert switch._rejudge_indicator_at_approval(
        env.conn, approval_id, payload, settings=settings,
        plugins_root=env.plugins) == payload
    assert built == [] and env.conn.total_changes == before


def test_approve_of_a_blessed_candidate_warns_and_never_refuses(env):
    approval_id, settings = _submit_then_deploy_a_twin(env, "enforce")
    payload = _payload(env.conn, approval_id)
    payload["gate_lane"] = "bless"
    updated = switch._rejudge_indicator_at_approval(
        env.conn, approval_id, payload, settings=settings, plugins_root=env.plugins)
    assert "duplicate_at_approve" in updated["gate_warnings"]
    assert updated["similar_to"][0]["name"] == "twin"


def test_approve_of_a_submitted_candidate_still_refuses_under_enforce(env):
    approval_id, settings = _submit_then_deploy_a_twin(env, "enforce")
    payload = _payload(env.conn, approval_id)
    with pytest.raises(ValueError, match="duplicate_at_approve"):
        switch._rejudge_indicator_at_approval(
            env.conn, approval_id, payload, settings=settings,
            plugins_root=env.plugins)


def test_bless_stamps_its_lane_into_the_indicator_payload(env):
    plain = env.plugins / "cand"
    _write(plain)
    env.install({"cand": fakes.ratio})
    human = env.plugins / "_human" / "cand"
    _write(human)
    approval_id = switch.bless_candidate(
        env.conn, name="cand", human_dir=human, settings=env.settings,
        now=NOW, decided_by="alice")
    assert _payload(env.conn, approval_id)["gate_lane"] == "bless"


def test_a_submitted_payload_has_no_bless_lane(env):
    env.install({"cand": fakes.ratio})
    assert "gate_lane" not in _payload(env.conn, env.submit("cand"))


def test_a_rewrite_that_matches_no_pending_row_returns_the_stored_payload(env):
    approval_id, settings = _submit_then_deploy_a_twin(env, "warn")
    payload = _payload(env.conn, approval_id)
    env.conn.execute("UPDATE approval_requests SET status='rejected' WHERE id=?",
                     (approval_id,))
    env.conn.commit()
    assert switch._rejudge_indicator_at_approval(
        env.conn, approval_id, payload, settings=settings,
        plugins_root=env.plugins) == payload


@pytest.mark.parametrize("mode", ["warn", "enforce"])
def test_approve_goes_on_when_a_deployed_indicator_itself_fails(env, mode):
    env.install({"cand": fakes.ratio, "twin": fakes.ratio})
    settings = _with_mode(env.settings, mode)
    approval_id = env.submit("cand", settings=settings)
    env.deploy("twin", "b")
    env.install({"cand": fakes.ratio, "twin": fakes.ratio},
                fail_names=frozenset({"twin"}))
    updated = switch._rejudge_indicator_at_approval(
        env.conn, approval_id, _payload(env.conn, approval_id),
        settings=settings, plugins_root=env.plugins)
    assert "duplicate_unavailable" in updated["gate_warnings"]
    assert _payload(env.conn, approval_id) == updated
