"""indicator gate を実 sandbox (本番の PluginSession) で通す結合試験。

fixture 実行を差し替えず、契約 run -> 解析 probe -> cache `.npz` -> 2 回目は配備済み分の
sandbox を起動しない -> 承認時の再判定、までを実 worker の応答で確かめる。load の前に
失敗する環境起因と、load の後に失敗する候補起因の分類も実 worker の応答で見る。

sandbox が動かない環境では skip する。配備済み indicator の複製を使う 2 本
(`AFX_INDICATOR_CORPUS`) は、複製の置き場を環境変数で渡したときだけ走り、実
`plugins/` や実 DB・実 cache には触れない。"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from agentic_fx.config import load_settings
from agentic_fx.plugin import indicator_fixture_cache as cache
from agentic_fx.plugin import indicator_gate as gate
from agentic_fx.plugin import indicator_pipeline as pipeline
from agentic_fx.plugin import indicator_probe as probe_mod
from agentic_fx.plugin import loader, sandbox, switch
from agentic_fx.plugin.resolve import ApprovedInventory, InventoryBuildResult
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import db as db_store

_reason = sandbox.host_preflight()[0] or sandbox.inherited_filter_preflight()
needs_sandbox = pytest.mark.skipif(
    _reason is not None, reason=f"requires a runnable PluginSession sandbox ({_reason})")

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"
NOW = datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(EXAMPLE)

RATIO = '    return {"value": df["close"].pct_change()}\n'
AFFINE = '    return {"value": 2.0 * df["close"].pct_change() + 1.0}\n'
WOBBLE = ('    base = df["close"].pct_change()\n'
          '    return {"value": base * base.rank(pct=True)}\n')
ABSOLUTE = ('    gap = ((df["close"] - df["open"]) > 0.05).astype(float)\n'
            '    return {"gap": gap + 0.001 * df["close"].pct_change().fillna(0.0)}\n')
LOG_OFFSET = '    import numpy as np\n    return {"value": np.log(df["close"] - 100.0)}\n'
ALL_NAN_AT_MAX_BARS = '    return {"value": df["close"].rolling(300).mean()}\n'
MISMATCH = '    return {"other": df["close"].pct_change()}\n'
RAISE_ON_LONG = ('    if len(df) > 1000:\n'
                 '        raise ValueError("boom on the long input")\n'
                 '    return {"value": df["close"].pct_change()}\n')


def write_plugin(directory: Path, body: str, *, outputs=("value",), at_import: str = "",
                 max_bars: int = 200) -> loader.PluginMeta:
    directory.mkdir(parents=True)
    (directory / "plugin.py").write_text(
        f"# {directory.name}\n{at_import}\ndef compute(df, params):\n{body}")
    (directory / "config.yaml").write_text(yaml.safe_dump(
        {"kind": "indicator", "outputs": list(outputs), "max_bars": max_bars,
         "params": {}}, sort_keys=False))
    (directory / "test_plugin.py").write_text("def test_x():\n    pass\n")
    meta, reason = loader.discover_one_with_reason(directory, directory.name)
    assert meta is not None, reason
    return meta


def stage(tmp_path, meta, *, deployed=(), pending=(), mode="warn", lane="loop",
          settings=SETTINGS):
    return pipeline.run_fixture_stage(
        meta, settings=settings, data_dir=tmp_path / "data",
        deployed_metas=list(deployed), pending=list(pending), mode=mode, lane=lane)


@pytest.fixture
def opened(monkeypatch):
    """Names of the plugins a session was opened for, in order."""
    names: list[str] = []
    real = probe_mod.open_session

    @contextmanager
    def spy(meta, settings):
        names.append(meta.name)
        with real(meta, settings) as session:
            yield session

    monkeypatch.setattr(probe_mod, "open_session", spy)
    return names


def cache_files(tmp_path) -> list[Path]:
    return sorted(cache.cache_dir(tmp_path / "data").rglob("*.npz"))


# --- contract run -> probe -> cache; the second run starts no deployed sandbox -----

@needs_sandbox
def test_a_real_run_writes_a_cache_entry_for_the_candidate_and_each_deployed_indicator(
        tmp_path, opened):
    deployed = write_plugin(tmp_path / "plugins" / "deployed_ratio", WOBBLE)
    cand = write_plugin(tmp_path / "plugins" / "cand_a", RATIO)
    report = stage(tmp_path, cand, deployed=[deployed])
    assert report.ok and report.code == ""
    assert sorted(opened) == ["cand_a", "deployed_ratio"]
    assert report.built == 2
    assert len(cache_files(tmp_path)) == 2
    assert report.fixture_timeframe == "1h" and len(report.fixture_data_hash) == 64


@needs_sandbox
def test_the_second_run_starts_no_sandbox_for_the_deployed_indicator(tmp_path, opened):
    deployed = write_plugin(tmp_path / "plugins" / "deployed_ratio", WOBBLE)
    first = write_plugin(tmp_path / "plugins" / "cand_a", RATIO)
    stage(tmp_path, first, deployed=[deployed])
    assert opened.count("deployed_ratio") == 1
    opened.clear()
    second = write_plugin(tmp_path / "plugins" / "cand_b", AFFINE.replace("2.0", "3.0"))
    report = stage(tmp_path, second, deployed=[deployed])
    assert opened == ["cand_b"]
    assert report.built == 1 and report.ok
    assert len(cache_files(tmp_path)) == 3


# --- findings the real worker's outputs produce -------------------------------------

@needs_sandbox
def test_an_exact_twin_of_a_deployed_indicator_is_rejected(tmp_path):
    deployed = write_plugin(tmp_path / "plugins" / "rsi_like", RATIO)
    twin = write_plugin(tmp_path / "plugins" / "renamed", RATIO)
    report = stage(tmp_path, twin, deployed=[deployed])
    assert (report.ok, report.code) == (False, "indicator_duplicate")
    assert report.similar_to[0] == {"name": "rsi_like", "column": "value", "kind": "duplicate"}


@needs_sandbox
@pytest.mark.parametrize("mode,verdict", [("warn", "warning"), ("enforce", "reject")])
def test_a_monotone_twin_warns_or_rejects_by_mode(tmp_path, mode, verdict):
    deployed = write_plugin(tmp_path / "plugins" / "base_ratio", RATIO)
    twin = write_plugin(tmp_path / "plugins" / "stretched", AFFINE)
    report = stage(tmp_path, twin, deployed=[deployed], mode=mode)
    if verdict == "warning":
        assert report.ok and "indicator_monotone_duplicate" in report.warnings
    else:
        assert (report.ok, report.code) == (False, "indicator_monotone_duplicate")
    assert report.similar_to[0]["kind"] == "monotone"


@needs_sandbox
def test_an_absolute_threshold_is_scale_dependent_and_names_its_column(tmp_path):
    cand = write_plugin(tmp_path / "plugins" / "gap_flag", ABSOLUTE, outputs=("gap",))
    report = stage(tmp_path, cand)
    assert report.ok and "scale_dependent" in report.warnings
    assert report.scale_columns == ("gap",)
    fields = pipeline.payload_fields(report)
    assert fields["scale_columns"] == ["gap"]


@needs_sandbox
def test_a_scale_invariant_indicator_carries_no_scale_warning(tmp_path):
    cand = write_plugin(tmp_path / "plugins" / "cand_ratio", RATIO)
    report = stage(tmp_path, cand)
    assert "scale_dependent" not in report.warnings and report.scale_columns == ()


@needs_sandbox
@pytest.mark.parametrize("body,detail", [
    (MISMATCH, "outputs_mismatch"),
])
def test_declared_outputs_that_differ_from_the_returned_keys_fail_the_contract_run(
        tmp_path, body, detail):
    cand = write_plugin(tmp_path / "plugins" / "bad_keys", body)
    report = stage(tmp_path, cand)
    assert (report.ok, report.code, report.detail) == (False, "fixture_run_failed", detail)


@needs_sandbox
def test_a_log_of_a_shifted_price_is_all_nan_at_the_small_scale_and_not_finite(tmp_path):
    cand = write_plugin(tmp_path / "plugins" / "log_offset", LOG_OFFSET)
    report = stage(tmp_path, cand)
    assert (report.ok, report.code) == (False, "output_not_finite")


@needs_sandbox
def test_all_nan_over_max_bars_is_not_finite_even_though_the_long_input_works(tmp_path):
    cand = write_plugin(tmp_path / "plugins" / "slow_window", ALL_NAN_AT_MAX_BARS)
    report = stage(tmp_path, cand)
    assert (report.ok, report.code, report.detail) == (
        False, "output_not_finite", "contract run")


# --- environment cause vs candidate cause, from real worker responses ---------------

@needs_sandbox
def test_a_failure_while_loading_in_the_worker_is_the_candidates_not_the_environment(
        tmp_path):
    cand = write_plugin(tmp_path / "plugins" / "raises_at_import", RATIO,
                        at_import='raise RuntimeError("fails at import")')
    report = stage(tmp_path, cand)
    assert (report.ok, report.code) == (False, "fixture_run_failed")
    assert report.detail == "exception"


@needs_sandbox
def test_a_worker_that_is_not_ready_before_the_load_is_an_environment_failure(
        tmp_path, monkeypatch):
    cand = write_plugin(tmp_path / "plugins" / "fine", RATIO)
    monkeypatch.setattr(sandbox, "_STARTUP_TIMEOUT_SEC", 0.001)
    with pytest.raises(pipeline.IndicatorGateUnavailable) as err:
        stage(tmp_path, cand)
    assert str(err.value) == "sandbox_unavailable"
    assert cache_files(tmp_path) == []


@pytest.fixture
def env_failure_for(monkeypatch):
    """Make the worker of the named plugin miss its start-up deadline (a real worker
    is spawned and killed before it reports ready); other plugins run normally."""
    targets: set[str] = set()
    real = probe_mod.open_session

    @contextmanager
    def maybe_late(meta, settings):
        if meta.name in targets:
            with monkeypatch.context() as patch:
                patch.setattr(sandbox, "_STARTUP_TIMEOUT_SEC", 0.001)
                with real(meta, settings) as session:
                    yield session
        else:
            with real(meta, settings) as session:
                yield session

    monkeypatch.setattr(probe_mod, "open_session", maybe_late)
    return targets


@needs_sandbox
def test_a_deployed_indicator_that_fails_its_own_run_is_dropped_with_a_warning(tmp_path):
    broken = write_plugin(tmp_path / "plugins" / "broken_partner", RAISE_ON_LONG)
    cand = write_plugin(tmp_path / "plugins" / "cand_a", RATIO)
    report = stage(tmp_path, cand, deployed=[broken])
    assert report.ok and "duplicate_unavailable" in report.warnings
    assert [name for name, _ in report.unavailable] == ["broken_partner"]


@needs_sandbox
def test_an_environment_failure_on_a_deployed_indicator_stops_the_loop_lane(
        tmp_path, env_failure_for):
    partner = write_plugin(tmp_path / "plugins" / "partner", WOBBLE)
    cand = write_plugin(tmp_path / "plugins" / "cand_a", RATIO)
    env_failure_for.add("partner")
    with pytest.raises(pipeline.IndicatorGateUnavailable) as err:
        stage(tmp_path, cand, deployed=[partner])
    assert str(err.value) == "fixture_cache_unavailable"


@needs_sandbox
def test_the_bless_lane_goes_on_with_a_warning_when_only_a_deployed_run_cannot_start(
        tmp_path, env_failure_for):
    partner = write_plugin(tmp_path / "plugins" / "partner", WOBBLE)
    cand = write_plugin(tmp_path / "plugins" / "cand_a", RATIO)
    env_failure_for.add("partner")
    report = stage(tmp_path, cand, deployed=[partner], lane="bless")
    assert report.ok and "duplicate_unavailable" in report.warnings


@needs_sandbox
def test_a_candidate_that_cannot_start_is_unavailable_in_every_lane(
        tmp_path, env_failure_for):
    cand = write_plugin(tmp_path / "plugins" / "cand_a", RATIO)
    env_failure_for.add("cand_a")
    for lane in ("loop", "submit", "bless"):
        with pytest.raises(pipeline.IndicatorGateUnavailable):
            stage(tmp_path, cand, lane=lane)


# --- submit -> a twin is deployed -> approve re-judges, on real runs -----------------

@pytest.fixture
def human(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / ".locks").mkdir()
    conn = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(conn)
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _pytest_ok)
    deployed: list[loader.PluginMeta] = []

    def inventory(*_a, **_k):
        return InventoryBuildResult(
            inventory=ApprovedInventory(root=plugins_dir, metas=tuple(deployed)),
            phase1_metas=(), resolved={}, rejected_strategies=())

    monkeypatch.setattr("agentic_fx.plugin.switch.tools_plugin_loader.approved_plugins",
                        inventory)

    def submit(name, body, *, settings=SETTINGS):
        write_plugin(plugins_dir / "_staging" / "1" / name, body)
        return switch.submit_candidate(
            conn, name=name, staging_dir=plugins_dir / "_staging" / "1",
            candidate_origin="staging", mission_id=1, backlog_id=None,
            settings=settings, now=NOW)

    def deploy(name, body):
        deployed.append(write_plugin(plugins_dir / name, body))

    yield SimpleNamespace(root=tmp_path, plugins=plugins_dir, conn=conn,
                          submit=submit, deploy=deploy)
    conn.close()


def _pytest_ok(plugin_dir, *, settings):
    from agentic_fx.plugin.gate_pytest import GateResult
    return GateResult(passed=True, returncode=0, stdout_tail="1 passed", duration_sec=0.1)


def _payload(conn, approval_id):
    row = conn.execute("SELECT payload_json FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    return json.loads(row["payload_json"])


def _with_mode(mode):
    return SETTINGS.model_copy(update={"improve": SETTINGS.improve.model_copy(
        update={"indicator_duplicate_mode": mode})})


@needs_sandbox
def test_approve_in_warn_mode_adds_the_duplicate_warning_after_a_twin_was_deployed(human):
    approval_id = human.submit("cand", RATIO)
    payload = _payload(human.conn, approval_id)
    assert payload["gate_warnings"] == [] and payload["fixture_timeframe"] == "1h"
    human.deploy("twin", RATIO)
    updated = switch._rejudge_indicator_at_approval(
        human.conn, approval_id, payload, settings=_with_mode("warn"),
        plugins_root=human.plugins)
    assert "duplicate_at_approve" in updated["gate_warnings"]
    assert updated["similar_to"][0] == {"name": "twin", "column": "value", "kind": "duplicate"}
    assert _payload(human.conn, approval_id) == updated
    from agentic_fx.loops import approval_facts
    assert "警告: 配備済み `twin` と同等 (列: value)" in approval_facts.gate_lines(updated)


@needs_sandbox
def test_approve_in_enforce_mode_refuses_a_twin_deployed_after_submission(human):
    approval_id = human.submit("cand", RATIO, settings=_with_mode("enforce"))
    human.deploy("twin", RATIO)
    with pytest.raises(ValueError, match="duplicate_at_approve"):
        switch.approve_candidate(
            human.conn, approval_id, decided_by="alice", now=NOW,
            plugins_root=human.plugins, settings=_with_mode("enforce"))
    row = human.conn.execute("SELECT status FROM approval_requests WHERE id=?",
                             (approval_id,)).fetchone()
    assert row["status"] == "pending"
    assert not (human.plugins / ".versions").exists()


@needs_sandbox
def test_approve_in_enforce_mode_also_refuses_a_monotone_twin(human):
    approval_id = human.submit("cand", RATIO, settings=_with_mode("enforce"))
    human.deploy("stretched", AFFINE)
    with pytest.raises(ValueError, match="duplicate_at_approve"):
        switch.approve_candidate(
            human.conn, approval_id, decided_by="alice", now=NOW,
            plugins_root=human.plugins, settings=_with_mode("enforce"))


@needs_sandbox
def test_approve_without_a_new_twin_leaves_the_payload_and_builds_nothing_twice(
        human, opened):
    approval_id = human.submit("cand", RATIO)
    human.deploy("unrelated", WOBBLE)
    payload = _payload(human.conn, approval_id)
    opened.clear()
    again = switch._rejudge_indicator_at_approval(
        human.conn, approval_id, payload, settings=_with_mode("warn"),
        plugins_root=human.plugins)
    assert again == payload
    assert opened == ["unrelated"]
    opened.clear()
    switch._rejudge_indicator_at_approval(
        human.conn, approval_id, payload, settings=_with_mode("warn"),
        plugins_root=human.plugins)
    assert opened == []


@needs_sandbox
def test_a_submission_of_a_twin_is_refused_with_no_request_and_the_cache_holds_both(human):
    human.deploy("rsi_like", RATIO)
    with pytest.raises(ValueError, match="indicator_duplicate"):
        human.submit("renamed", RATIO)
    assert approvals_store.pending(human.conn, kind="plugin") == []
    assert len(cache_files(human.root)) == 2


# --- the deployed corpus (a copy; set AFX_INDICATOR_CORPUS to its directory) ---------

CORPUS = os.environ.get("AFX_INDICATOR_CORPUS")
needs_corpus = pytest.mark.skipif(
    not CORPUS, reason="AFX_INDICATOR_CORPUS (a copy of the deployed indicators) is not set")
# The six symbols a production settings file lists: a fixed set, not read from any file.
SYMBOLS = ["XAUUSD", "USDJPY", "EURUSD", "US500", "USOIL", "JP225"]


def corpus_metas() -> list[loader.PluginMeta]:
    metas = [m for m in loader.discover(Path(CORPUS)) if m.kind == "indicator"]
    assert len(metas) >= 50
    return metas


@needs_corpus
def test_only_the_four_symbol_named_deployed_indicators_hit_the_name_check():
    settings = SimpleNamespace(pairs=SYMBOLS[:3], datafeed=SimpleNamespace(
        watch_symbols=SYMBOLS[3:]))
    hit = sorted(
        meta.name for meta in corpus_metas()
        if not pipeline.check_static(meta, Path(meta.path), settings=settings,
                                     deployed=None, lane="submit").ok)
    assert hit == ["us500_hourly_drift", "usoil_utc_day_open_return",
                   "xau_hourly_volatility_surprise", "xau_utc_open_gap_reversal"]
    assert pipeline.check_static(
        corpus_metas()[0], Path(corpus_metas()[0].path), settings=settings,
        deployed=None, lane="submit").ok
    symbols = gate.symbol_tokens(SYMBOLS)
    for name in ("volume_per_bar", "copper_x"):
        verdict = gate.check_symbol_in_name(
            gate.candidate_tokens({"name": name, "params": {}}, "", ""), symbols)
        assert verdict.ok, name


@needs_sandbox
@needs_corpus
def test_every_deployed_indicator_passes_the_contract_run_on_the_fixture(tmp_path):
    bars, _ = pipeline.canonical_bars("1h")
    failed = {}
    for meta in corpus_metas():
        with probe_mod.open_session(meta, SETTINGS.plugin) as session:
            result = probe_mod.run_contract(session, meta, bars)
        if result.failure or result.all_nan:
            failed[meta.name] = result.failure or "all_nan"
    assert failed == {}
