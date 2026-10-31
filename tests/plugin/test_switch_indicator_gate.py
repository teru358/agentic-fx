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


def test_submit_lane_fails_retryably_when_a_deployed_cache_fails(env):
    env.deploy("rsi", "a")
    env.install({"cand": fakes.ratio, "rsi": fakes.ratio}, fail_names=frozenset({"rsi"}))
    with pytest.raises(ValueError, match="indicator_gate_unavailable"):
        env.gate("cand", floor_mode="enforce")


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
    assert "indicator_output_overlap" in updated["gate_warnings"]
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
