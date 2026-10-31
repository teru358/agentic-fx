"""indicator_pipeline の整形・分類・再判定の細部。sandbox は起動せず、fixture 実行
だけを差し替える (分類・比較・cache は本物)。"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from agentic_fx import improve_notice_codes as codes
from agentic_fx.config import load_settings
from agentic_fx.plugin import indicator_fixture_cache as cache
from agentic_fx.plugin import indicator_gate as gate
from agentic_fx.plugin import indicator_pipeline as pipeline
from tests.fixtures import indicator_gate_fakes as fakes

_EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"
SETTINGS = load_settings(_EXAMPLE)
VOCAB = sorted(codes.INDICATOR_PAYLOAD_WARNING_CODES)


def _row(name, kind, column="value"):
    return {"name": name, "column": column, "kind": kind}


def _meta(tmp_path, name, tag, **kw):
    return fakes.meta_for(name, tmp_path / "plugins" / name,
                          content_hash=tag * 64, **kw)


def _with_improve(settings, **update):
    return settings.model_copy(update={
        "improve": settings.improve.model_copy(update=update)})


def stage(tmp_path, monkeypatch, outputs, *, name="cand", deployed=(),
          pending=(), mode="warn", lane="loop", settings=None, probe_kw=None,
          tag="c"):
    fakes.FakeProbe(outputs, **(probe_kw or {})).install(monkeypatch)
    return pipeline.run_fixture_stage(
        _meta(tmp_path, name, tag), settings=settings or SETTINGS,
        data_dir=tmp_path / "data", deployed_metas=list(deployed),
        pending=list(pending), mode=mode, lane=lane)


# --- warning / similar_to の整形 -------------------------------------------------

def test_clean_warnings_caps_at_eight_dedupes_and_drops_foreign_words():
    assert len(VOCAB) >= 9
    out = pipeline._clean_warnings(["bogus", VOCAB[0], VOCAB[0], *VOCAB[1:9], "x"])
    assert out == tuple(VOCAB[:8])
    assert pipeline._clean_warnings(VOCAB[:7]) == tuple(VOCAB[:7])
    assert pipeline._clean_warnings(VOCAB[:8]) == tuple(VOCAB[:8])
    assert pipeline.MAX_WARNINGS == 8 and pipeline.MAX_SIMILAR == 3


def test_payload_fields_have_exactly_the_documented_keys_and_copy_rows():
    rows = tuple(_row(f"n{i}", "similar") for i in range(5))
    report = pipeline.IndicatorReport(
        warnings=("bogus", "scale_dependent"), similar_to=rows,
        fixture_data_hash="h" * 64, fixture_timeframe="1h")
    fields = pipeline.payload_fields(report)
    assert fields == {
        "gate_warnings": ["scale_dependent"],
        "similar_to": [dict(r) for r in rows[:3]],
        "fixture_data_hash": "h" * 64, "fixture_timeframe": "1h"}
    assert fields["similar_to"][0] is not rows[0]


def test_merge_warnings_keeps_old_first_then_new_in_the_closed_vocabulary():
    out = pipeline.merge_warnings(
        ["scale_dependent", 3, "foreign", None], ["duplicate_at_approve", "scale_dependent"])
    assert out == ["scale_dependent", "duplicate_at_approve"]


def test_merge_warnings_drops_old_items_that_are_not_hashable_words():
    assert pipeline.merge_warnings(
        ["scale_dependent", ["x"], {"b": 1}], []) == ["scale_dependent"]


@pytest.mark.parametrize("old", [None, "scale_dependent", {"a": 1}, ("scale_dependent",)])
def test_merge_warnings_ignores_an_old_value_that_is_not_a_list(old):
    assert pipeline.merge_warnings(old, ["sparse_output"]) == ["sparse_output"]


def test_merge_warnings_caps_at_eight_keeping_the_old_ones():
    out = pipeline.merge_warnings(VOCAB[:6], VOCAB[6:10])
    assert out == VOCAB[:8]


def test_merge_similar_orders_by_kind_strength_and_keeps_three():
    new = [_row("a", "similar"), _row("b", "overlap")]
    old = [_row("c", "monotone"), _row("d", "duplicate")]
    out = pipeline.merge_similar(old, new)
    assert [r["kind"] for r in out] == ["duplicate", "monotone", "overlap"]
    for weak, strong in (("similar", "overlap"), ("overlap", "monotone"),
                         ("monotone", "duplicate")):
        pair = pipeline.merge_similar([], [_row("w", weak), _row("s", strong)])
        assert [r["kind"] for r in pair] == [strong, weak]


def test_merge_similar_puts_new_rows_before_old_ones_of_the_same_kind():
    out = pipeline.merge_similar([_row("old", "similar")], [_row("new", "similar")])
    assert [r["name"] for r in out] == ["new", "old"]


def test_merge_similar_drops_duplicates_and_malformed_rows():
    good = _row("a", "duplicate")
    bad = [
        {"name": "x", "column": "v", "kind": "duplicate", "extra": "1"},
        {"name": "x", "column": "v"},
        {"name": "x", "column": "v", "kind": "bogus"},
        {"name": "x", "column": 3, "kind": "duplicate"},
        {"name": 3, "column": "v", "kind": "duplicate"},
        "row", None, 3]
    out = pipeline.merge_similar([good, *bad], [good])
    assert out == [good]
    assert out[0] is not good


@pytest.mark.parametrize("old", [None, "x", {"a": 1}, 5])
def test_merge_similar_ignores_an_old_value_that_is_not_a_list(old):
    assert pipeline.merge_similar(old, [_row("a", "similar")]) == [_row("a", "similar")]


def test_effective_mode_only_softens_bless():
    assert pipeline.effective_mode("bless", "enforce") == "warn"
    assert pipeline.effective_mode("submit", "enforce") == "enforce"
    assert pipeline.effective_mode("loop", "enforce") == "enforce"
    assert pipeline.effective_mode("loop", "warn") == "warn"


# --- 静的検査 -------------------------------------------------------------------

def _write_identity(directory: Path, *, plugin_py="def compute(df, params):\n    return {}\n",
                    config=None):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plugin.py").write_text(plugin_py)
    (directory / "config.yaml").write_text(yaml.safe_dump(
        config or {"kind": "indicator", "outputs": ["value"], "max_bars": 200}))


def _static(tmp_path, name, *, params=None, deployed=None, lane="loop"):
    candidate = tmp_path / "cand_dir" / name
    _write_identity(candidate)
    meta = dataclasses.replace(
        _meta(tmp_path, name, "c"), path=candidate, params=params or {})
    return pipeline.check_static(
        meta, candidate, settings=SETTINGS, deployed=deployed, lane=lane)


def _legacy(tmp_path, name):
    old = tmp_path / "plugins" / name
    _write_identity(old)
    return fakes.meta_for(name, old, content_hash="a" * 64)


def test_static_reject_keeps_the_warnings_found_so_far(tmp_path):
    report = _static(tmp_path, "usdjpy_ratio", params={"pip_size": 0.01},
                     deployed=_legacy(tmp_path, "usdjpy_ratio"))
    assert (report.ok, report.code) == (False, "param_price_unit")
    assert report.warnings == ("legacy_symbol_name",)
    assert report.detail == "param_key:pip_size"


def test_static_without_a_deployed_version_grants_no_exemption(tmp_path):
    report = _static(tmp_path, "usdjpy_ratio")
    assert (report.ok, report.code) == (False, "symbol_in_name")
    assert report.detail.startswith("name:usdjpy")


def test_static_with_an_unreadable_deployed_version_grants_no_exemption(tmp_path):
    ghost = fakes.meta_for("usdjpy_ratio", tmp_path / "plugins" / "ghost",
                           content_hash="a" * 64)
    assert _static(tmp_path, "usdjpy_ratio", deployed=ghost).code == "symbol_in_name"


def test_static_with_a_deployed_version_that_does_not_parse_grants_no_exemption(
        tmp_path):
    old = tmp_path / "plugins" / "usdjpy_ratio"
    _write_identity(old, plugin_py="def (:\n")
    deployed = fakes.meta_for("usdjpy_ratio", old, content_hash="a" * 64)
    assert _static(tmp_path, "usdjpy_ratio", deployed=deployed).code == "symbol_in_name"


def test_static_grants_the_exemption_when_the_deployed_version_has_the_same_hits(
        tmp_path):
    report = _static(tmp_path, "usdjpy_ratio",
                     deployed=_legacy(tmp_path, "usdjpy_ratio"))
    assert report.ok and report.warnings == ("legacy_symbol_name",)


def test_static_bless_turns_both_findings_into_warnings(tmp_path):
    report = _static(tmp_path, "usdjpy_ratio", params={"pip_size": 0.01},
                     lane="bless")
    assert report.ok
    assert report.warnings == ("symbol_in_name", "param_price_unit")


def test_static_of_an_unreadable_candidate_fails_closed(tmp_path):
    meta = _meta(tmp_path, "cand", "c")
    report = pipeline.check_static(
        meta, tmp_path / "nowhere", settings=SETTINGS, deployed=None, lane="bless")
    assert (report.ok, report.code) == (False, "fixture_run_failed")


# --- fixture 段: 契約 run と probe の警告 ------------------------------------------

def test_a_constant_in_the_probe_only_is_suspect_not_a_rejection(tmp_path, monkeypatch):
    report = stage(tmp_path, monkeypatch, {"cand": fakes.constant_in_probe_only})
    assert report.ok and "output_constant_suspect" in report.warnings


def test_a_constant_in_both_runs_rejects_with_the_column(tmp_path, monkeypatch):
    report = stage(tmp_path, monkeypatch, {"cand": fakes.constant})
    assert (report.ok, report.code, report.detail) == (
        False, "output_constant", "value")


def test_sparseness_is_read_from_the_contract_run(tmp_path, monkeypatch):
    assert "sparse_output" in stage(
        tmp_path, monkeypatch,
        {"cand": fakes.sparse_in_contract_only}).warnings
    assert "sparse_output" not in stage(
        tmp_path, monkeypatch,
        {"cand2": fakes.sparse_in_probe_only}, name="cand2").warnings


def test_the_first_decision_timeframe_picks_the_fixture(tmp_path, monkeypatch):
    datafeed = SETTINGS.datafeed.model_copy(
        update={"decision_timeframes": ["4h", "1h"]})
    settings = SETTINGS.model_copy(update={"datafeed": datafeed})
    report = stage(tmp_path, monkeypatch, {"cand": fakes.ratio}, settings=settings)
    assert report.fixture_timeframe == "4h"
    assert report.fixture_data_hash == pipeline.canonical_bars("4h")[1]


def test_a_rejection_still_carries_the_fixture_identity_and_build_count(
        tmp_path, monkeypatch):
    report = stage(tmp_path, monkeypatch, {"cand": fakes.constant})
    assert report.fixture_timeframe == "1h"
    assert len(report.fixture_data_hash) == 64 and report.built == 0


# --- cache の失敗の分類 ---------------------------------------------------------

def _fail_get_or_build(monkeypatch, exc, *, only_hash=None):
    real = cache.get_or_build

    def wrapper(data_dir, key, builder, **kw):
        if only_hash is None or key.content_hash == only_hash:
            raise exc
        return real(data_dir, key, builder, **kw)

    monkeypatch.setattr(pipeline.cache, "get_or_build", wrapper)


@pytest.mark.parametrize("exc", [
    cache.IndicatorCacheLockTimeout("t"), cache.IndicatorCacheError("e"),
    OSError("o"), ValueError("v")])
def test_a_deployed_cache_failure_is_retryable_in_the_loop_and_a_warning_in_bless(
        tmp_path, monkeypatch, exc):
    deployed = [_meta(tmp_path, "rsi", "a")]
    outputs = {"cand": fakes.ratio, "rsi": fakes.price_diff}
    fakes.FakeProbe(outputs).install(monkeypatch)
    _fail_get_or_build(monkeypatch, exc, only_hash="a" * 64)
    kwargs = dict(settings=SETTINGS, data_dir=tmp_path / "data",
                  deployed_metas=deployed, pending=[], mode="warn")
    with pytest.raises(pipeline.IndicatorGateUnavailable) as raised:
        pipeline.run_fixture_stage(_meta(tmp_path, "cand", "c"), lane="loop", **kwargs)
    assert str(raised.value) == "fixture_cache_unavailable"
    report = pipeline.run_fixture_stage(
        _meta(tmp_path, "cand", "c"), lane="bless", **kwargs)
    assert report.ok and "duplicate_unavailable" in report.warnings


def test_a_foreign_exception_while_building_a_deployed_cache_propagates(
        tmp_path, monkeypatch):
    fakes.FakeProbe({"cand": fakes.ratio, "rsi": fakes.price_diff}).install(monkeypatch)
    _fail_get_or_build(monkeypatch, RuntimeError("boom"), only_hash="a" * 64)
    with pytest.raises(RuntimeError):
        pipeline.run_fixture_stage(
            _meta(tmp_path, "cand", "c"), settings=SETTINGS,
            data_dir=tmp_path / "data", deployed_metas=[_meta(tmp_path, "rsi", "a")],
            pending=[], mode="warn", lane="loop")


@pytest.mark.parametrize("exc", [cache.IndicatorCacheError("e"),
                                 cache.IndicatorCacheLockTimeout("t"), OSError("o")])
def test_the_candidates_own_cache_failure_is_a_retryable_environment_failure(
        tmp_path, monkeypatch, exc):
    fakes.FakeProbe({"cand": fakes.ratio}).install(monkeypatch)
    _fail_get_or_build(monkeypatch, exc)
    with pytest.raises(pipeline.IndicatorGateUnavailable) as raised:
        pipeline.run_fixture_stage(
            _meta(tmp_path, "cand", "c"), settings=SETTINGS,
            data_dir=tmp_path / "data", deployed_metas=[], pending=[],
            mode="warn", lane="bless")
    assert str(raised.value) == "fixture_cache_unavailable"


def test_a_sandbox_failure_names_the_environment_reason(tmp_path, monkeypatch):
    with pytest.raises(pipeline.IndicatorGateUnavailable) as raised:
        stage(tmp_path, monkeypatch, {"cand": fakes.ratio},
              probe_kw=dict(env_error=True))
    assert str(raised.value) == "sandbox_unavailable"


@pytest.mark.parametrize("exc", [cache.IndicatorCacheError("e"), OSError("o")])
def test_an_unreadable_pending_cache_is_skipped_not_fatal(tmp_path, monkeypatch, exc):
    fakes.FakeProbe({"cand": fakes.ratio}).install(monkeypatch)

    def broken_read(data_dir, key):
        raise exc

    monkeypatch.setattr(pipeline.cache, "read", broken_read)
    report = pipeline.run_fixture_stage(
        _meta(tmp_path, "cand", "c"), settings=SETTINGS, data_dir=tmp_path / "data",
        deployed_metas=[], pending=[("pending-1", "p" * 64, "queued")],
        mode="warn", lane="loop")
    assert report.ok and report.similar_to == ()


# --- 閾値と引数の受け渡し ---------------------------------------------------------

def _spy_compare(monkeypatch):
    seen = []
    real = gate.compare_outputs

    def spy(candidate, deployed, **kwargs):
        seen.append(kwargs)
        return real(candidate, deployed, **kwargs)

    monkeypatch.setattr(pipeline.gate, "compare_outputs", spy)
    return seen


def test_the_similarity_threshold_and_classes_reach_the_comparison(
        tmp_path, monkeypatch):
    seen = _spy_compare(monkeypatch)
    settings = _with_improve(SETTINGS, indicator_similar_corr=0.61)
    stage(tmp_path, monkeypatch, {"cand": fakes.ratio, "rsi": fakes.price_diff},
          deployed=[_meta(tmp_path, "rsi", "a")], settings=settings,
          mode="enforce")
    assert len(seen) == 1
    assert seen[0]["similar_corr"] == 0.61 and seen[0]["mode"] == "enforce"
    assert seen[0]["classifications"] == {"value": ("invariant", "invariant")} or \
        set(seen[0]["classifications"]) == {"value"}


def test_the_similarity_threshold_decides_the_similar_warning(tmp_path, monkeypatch):
    outputs = {"cand": fakes.ratio, "dep": fakes.ratio_with_wobble}
    deployed = [_meta(tmp_path, "dep", "a")]
    loose = stage(tmp_path, monkeypatch, outputs, deployed=deployed,
                  settings=_with_improve(SETTINGS, indicator_similar_corr=0.5))
    assert "indicator_similar" in loose.warnings
    assert loose.similar_to == ({"name": "dep", "column": "value", "kind": "similar"},)
    strict = stage(tmp_path / "other", monkeypatch, outputs, deployed=deployed,
                   settings=_with_improve(SETTINGS, indicator_similar_corr=0.99999))
    assert "indicator_similar" not in strict.warnings and strict.similar_to == ()


def test_a_passing_report_still_carries_the_relation_detail(tmp_path, monkeypatch):
    report = stage(
        tmp_path, monkeypatch, {"cand": fakes.ratio, "dep": fakes.ratio_with_wobble},
        deployed=[_meta(tmp_path, "dep", "a")],
        settings=_with_improve(SETTINGS, indicator_similar_corr=0.5))
    assert report.ok
    assert report.detail.startswith("- similar: dep.value (column=value diff_pearson=0.")


# --- GC -----------------------------------------------------------------------

def test_gc_counts_reach_the_report(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.cache, "gc_stale",
                        lambda data_dir, keep: cache.GcResult(["a", "b"], ["c"]))
    report = stage(tmp_path, monkeypatch, {"cand": fakes.ratio})
    assert (report.gc_removed, report.gc_skipped) == (2, 1)


@pytest.mark.parametrize("exc", [cache.IndicatorCacheError("e"), OSError("o")])
def test_a_failing_gc_is_counted_as_one_skipped_and_does_not_fail_the_check(
        tmp_path, monkeypatch, exc):
    def broken(data_dir, keep):
        raise exc

    monkeypatch.setattr(pipeline.cache, "gc_stale", broken)
    report = stage(tmp_path, monkeypatch, {"cand": fakes.ratio})
    assert report.ok and (report.gc_removed, report.gc_skipped) == (0, 1)


def test_gc_removes_entries_of_an_older_fixture_generation(tmp_path, monkeypatch):
    bars, data_hash = pipeline.canonical_bars("1h")
    stale = cache.CacheKey("9" * 64, "8" * 64, "1h")
    index = pd.date_range("2026-01-01", periods=3, freq="h", tz="UTC")
    cache.write(tmp_path / "data", stale, cache.CacheEntry(
        outputs={"value": pd.Series([1.0, 2.0, 3.0], index=index)}))
    report = stage(tmp_path, monkeypatch, {"cand": fakes.ratio})
    assert report.gc_removed >= 1 and report.gc_skipped == 0
    assert cache.read(tmp_path / "data", stale) is None


# --- 同名更新の再定義 -----------------------------------------------------------

def _entry(**series):
    return cache.CacheEntry(outputs=dict(series))


def _correlated(rho, n=120, seed=3):
    rng = np.random.default_rng(seed)
    a = rng.normal(size=n)
    a = (a - a.mean()) / a.std()
    e = rng.normal(size=n)
    e = e - e.mean()
    e = e - (e @ a) / (a @ a) * a
    e = e / e.std()
    index = pd.RangeIndex(n)
    return pd.Series(a, index=index), pd.Series(rho * a + np.sqrt(1 - rho ** 2) * e,
                                                index=index)


def _redefined(new_outputs, old_outputs, *, meta_outputs=("value",),
               old_meta_outputs=("value",)):
    meta = fakes.meta_for("cand", Path("."), content_hash="c" * 64,
                          outputs=meta_outputs)
    old = fakes.meta_for("cand", Path("."), content_hash="a" * 64,
                         outputs=old_meta_outputs)
    old_entry = None if old_outputs is None else _entry(**old_outputs)
    return pipeline._redefined(meta, old, _entry(**new_outputs), old_entry)


def test_a_changed_outputs_declaration_is_a_redefinition_even_without_a_cache():
    assert _redefined({}, None, meta_outputs=("a",), old_meta_outputs=("b",))
    assert not _redefined({}, None)


@pytest.mark.parametrize("rho,expected", [(0.45, True), (0.55, False),
                                          (-0.9, False), (0.9, False), (0.05, True)])
def test_redefinition_is_low_absolute_correlation_against_one_half(rho, expected):
    a, b = _correlated(rho)
    assert _redefined({"value": a}, {"value": b}) is expected


def test_redefinition_needs_every_comparable_column_to_be_dissimilar():
    a1, b1 = _correlated(0.05, seed=1)
    a2, b2 = _correlated(0.95, seed=2)
    assert not _redefined({"p": a1, "q": a2}, {"p": b1, "q": b2})
    assert _redefined({"p": a1, "q": a1}, {"p": b1, "q": b1})


def test_no_comparable_column_is_not_a_redefinition():
    a, b = _correlated(0.05)
    short_a, short_b = a.iloc[:10], b.iloc[:10]
    assert not _redefined({"value": short_a}, {"value": short_b})
    assert not _redefined({"value": 1.0}, {"value": 2.0})
    shifted = pd.Series(b.to_numpy(), index=b.index + 10_000)
    assert not _redefined({"value": a}, {"value": shifted})
    assert not _redefined({"value": a}, {"other": b})


def test_a_same_name_update_with_an_unrelated_series_is_flagged_end_to_end(
        tmp_path, monkeypatch):
    old = _meta(tmp_path, "cand", "a")
    outputs = {("cand", "a" * 64): fakes.ratio, "cand": fakes.noise}
    report = stage(tmp_path, monkeypatch, outputs, deployed=[old])
    assert "indicator_redefined" in report.warnings
    same = stage(tmp_path / "s", monkeypatch,
                 {("cand", "a" * 64): fakes.ratio, "cand": fakes.ratio},
                 deployed=[old])
    assert "indicator_redefined" not in same.warnings
    flipped = stage(tmp_path / "f", monkeypatch,
                    {("cand", "a" * 64): fakes.ratio, "cand": fakes.negated_ratio},
                    deployed=[old])
    assert "indicator_redefined" not in flipped.warnings


def test_a_same_name_update_whose_old_cache_cannot_be_built_is_not_flagged(
        tmp_path, monkeypatch):
    old = _meta(tmp_path, "cand", "a")
    report = stage(tmp_path, monkeypatch,
                   {("cand", "a" * 64): fakes.ratio, "cand": fakes.noise},
                   deployed=[old], lane="bless",
                   probe_kw=dict(fail_names=frozenset({"a" * 64})))
    assert report.ok and "indicator_redefined" not in report.warnings


# --- 承認時の再判定 ---------------------------------------------------------------

def _payload_after_submit(tmp_path, monkeypatch, outputs, candidate="cand"):
    report = stage(tmp_path, monkeypatch, outputs, name=candidate)
    assert report.ok, report
    return {"name": candidate, "content_hash": "c" * 64,
            "fixture_data_hash": report.fixture_data_hash,
            "fixture_timeframe": report.fixture_timeframe}


def _rejudge(tmp_path, payload, deployed, *, mode, settings=None):
    return pipeline.rejudge_at_approval(
        payload, settings=settings or SETTINGS, data_dir=tmp_path / "data",
        deployed_metas=deployed, mode=mode)


def _twin_setup(tmp_path, monkeypatch, twin_fn, candidate_fn=fakes.ratio):
    payload = _payload_after_submit(tmp_path, monkeypatch, {"cand": candidate_fn})
    fakes.FakeProbe({"cand": candidate_fn, "twin": twin_fn}).install(monkeypatch)
    return payload, [_meta(tmp_path, "twin", "b")]


def test_rejudge_enforce_refuses_an_exact_twin(tmp_path, monkeypatch):
    payload, deployed = _twin_setup(tmp_path, monkeypatch, fakes.ratio)
    result = _rejudge(tmp_path, payload, deployed, mode="enforce")
    assert result.rejected_code == "duplicate_at_approve" and result.changed
    assert result.similar_to == (_row("twin", "duplicate"),)
    assert result.warnings == ("duplicate_at_approve",)


def test_rejudge_enforce_refuses_a_monotone_twin_on_its_own(tmp_path, monkeypatch):
    payload, deployed = _twin_setup(tmp_path, monkeypatch, fakes.affine_of_ratio)
    result = _rejudge(tmp_path, payload, deployed, mode="enforce")
    assert result.rejected_code == "duplicate_at_approve"
    assert result.similar_to == (_row("twin", "monotone"),)


def test_rejudge_enforce_lets_a_partial_overlap_through(tmp_path, monkeypatch):
    payload, deployed = _twin_setup(
        tmp_path, monkeypatch, fakes.two_columns, candidate_fn=fakes.one_shared_column)
    result = _rejudge(tmp_path, payload, deployed, mode="enforce")
    assert result == pipeline.Rejudgement()


def test_rejudge_enforce_lets_a_merely_similar_twin_through(tmp_path, monkeypatch):
    payload, deployed = _twin_setup(tmp_path, monkeypatch, fakes.ratio_with_wobble)
    result = _rejudge(tmp_path, payload, deployed, mode="enforce",
                      settings=_with_improve(SETTINGS, indicator_similar_corr=0.5))
    assert result == pipeline.Rejudgement()


def test_rejudge_warn_updates_instead_of_refusing(tmp_path, monkeypatch):
    payload, deployed = _twin_setup(tmp_path, monkeypatch, fakes.ratio)
    result = _rejudge(tmp_path, payload, deployed, mode="warn")
    assert result.rejected_code == "" and result.changed is True
    assert result.warnings == ("duplicate_at_approve",)
    assert result.similar_to == (_row("twin", "duplicate"),)


def test_rejudge_warn_marks_a_monotone_twin_with_both_words(tmp_path, monkeypatch):
    payload, deployed = _twin_setup(tmp_path, monkeypatch, fakes.affine_of_ratio)
    result = _rejudge(tmp_path, payload, deployed, mode="warn")
    assert result.rejected_code == ""
    assert result.warnings == ("duplicate_at_approve", "indicator_monotone_duplicate")
    assert result.similar_to == (_row("twin", "monotone"),)


def test_rejudge_warn_keeps_only_the_two_rejudged_findings(tmp_path, monkeypatch):
    payload, deployed = _twin_setup(
        tmp_path, monkeypatch, fakes.two_columns, candidate_fn=fakes.one_shared_column)
    result = _rejudge(tmp_path, payload, deployed, mode="warn")
    assert result == pipeline.Rejudgement()
    assert result.changed is False


@pytest.mark.parametrize("field", ["fixture_data_hash", "fixture_timeframe",
                                   "content_hash", "name"])
@pytest.mark.parametrize("bad", [None, 5, ["x"]])
def test_rejudge_ignores_a_payload_with_mistyped_references(
        tmp_path, monkeypatch, field, bad):
    payload, deployed = _twin_setup(tmp_path, monkeypatch, fakes.ratio)
    payload[field] = bad
    assert _rejudge(tmp_path, payload, deployed, mode="enforce") is None


def test_rejudge_ignores_a_payload_missing_a_reference(tmp_path, monkeypatch):
    payload, deployed = _twin_setup(tmp_path, monkeypatch, fakes.ratio)
    del payload["name"]
    assert _rejudge(tmp_path, payload, deployed, mode="enforce") is None


@pytest.mark.parametrize("exc", [cache.IndicatorCacheError("e"), OSError("o"),
                                 ValueError("v")])
def test_rejudge_ignores_a_candidate_whose_cache_cannot_be_read(
        tmp_path, monkeypatch, exc):
    payload, deployed = _twin_setup(tmp_path, monkeypatch, fakes.ratio)

    def broken(data_dir, key):
        raise exc

    monkeypatch.setattr(pipeline.cache, "read", broken)
    assert _rejudge(tmp_path, payload, deployed, mode="enforce") is None


@pytest.mark.parametrize("exc", [ValueError("v"), KeyError("k")])
def test_rejudge_ignores_a_payload_whose_fixture_cannot_be_rebuilt(
        tmp_path, monkeypatch, exc):
    payload, deployed = _twin_setup(tmp_path, monkeypatch, fakes.ratio)

    def broken(timeframe):
        raise exc

    monkeypatch.setattr(pipeline, "canonical_bars", broken)
    assert _rejudge(tmp_path, payload, deployed, mode="enforce") is None


@pytest.mark.parametrize("mode", ["warn", "enforce"])
def test_rejudge_fails_retryably_when_the_sandbox_cannot_run_a_deployed_one(
        tmp_path, monkeypatch, mode):
    payload = _payload_after_submit(tmp_path, monkeypatch, {"cand": fakes.ratio})
    fakes.FakeProbe({"cand": fakes.ratio, "twin": fakes.ratio}).install(monkeypatch)
    real = pipeline.probe_mod.open_session

    def flaky(meta, plugin_settings):
        if meta.name == "twin":
            raise pipeline.probe_mod.IndicatorEnvironmentError("sandbox_unavailable")
        return real(meta, plugin_settings)

    monkeypatch.setattr(pipeline.probe_mod, "open_session", flaky)
    with pytest.raises(pipeline.IndicatorGateUnavailable) as raised:
        _rejudge(tmp_path, payload, [_meta(tmp_path, "twin", "b")], mode=mode)
    assert str(raised.value) == "fixture_cache_unavailable"


@pytest.mark.parametrize("mode", ["warn", "enforce"])
def test_rejudge_drops_a_deployed_one_whose_own_run_failed_and_warns(
        tmp_path, monkeypatch, mode):
    payload = _payload_after_submit(tmp_path, monkeypatch, {"cand": fakes.ratio})
    fakes.FakeProbe({"cand": fakes.ratio, "twin": fakes.ratio},
                    fail_names=frozenset({"twin"})).install(monkeypatch)
    result = _rejudge(tmp_path, payload, [_meta(tmp_path, "twin", "b")], mode=mode)
    assert result.warnings == ("duplicate_unavailable",)
    assert result.rejected_code == "" and result.changed
    assert result.similar_to == ()


@pytest.mark.parametrize("exc", [cache.IndicatorCacheLockTimeout("t"),
                                 cache.IndicatorCacheError("e"), OSError("o"),
                                 ValueError("v")])
def test_rejudge_fails_retryably_on_any_cache_error_while_building(
        tmp_path, monkeypatch, exc):
    payload = _payload_after_submit(tmp_path, monkeypatch, {"cand": fakes.ratio})
    fakes.FakeProbe({"cand": fakes.ratio, "twin": fakes.ratio}).install(monkeypatch)
    real = cache.get_or_build

    def wrapper(data_dir, key, builder, **kw):
        if key.content_hash == "b" * 64:
            raise exc
        return real(data_dir, key, builder, **kw)

    monkeypatch.setattr(pipeline.cache, "get_or_build", wrapper)
    with pytest.raises(pipeline.IndicatorGateUnavailable):
        _rejudge(tmp_path, payload, [_meta(tmp_path, "twin", "b")], mode="warn")


def test_rejudge_passes_the_threshold_mode_and_classes_to_the_comparison(
        tmp_path, monkeypatch):
    payload, deployed = _twin_setup(tmp_path, monkeypatch, fakes.price_diff)
    seen = _spy_compare(monkeypatch)
    _rejudge(tmp_path, payload, deployed, mode="enforce",
             settings=_with_improve(SETTINGS, indicator_similar_corr=0.77))
    assert len(seen) == 1
    assert seen[0]["similar_corr"] == 0.77 and seen[0]["mode"] == "enforce"
    assert set(seen[0]["classifications"]) == {"value"}


def test_rejudge_excludes_the_candidates_own_name_from_the_partners(
        tmp_path, monkeypatch):
    payload = _payload_after_submit(tmp_path, monkeypatch, {"cand": fakes.ratio})
    fakes.FakeProbe({"cand": fakes.ratio}).install(monkeypatch)
    result = _rejudge(tmp_path, payload, [_meta(tmp_path, "cand", "d")], mode="enforce")
    assert result == pipeline.Rejudgement()


def test_rejudge_only_compares_against_deployed_indicators_it_was_given(
        tmp_path, monkeypatch):
    payload = _payload_after_submit(tmp_path, monkeypatch, {"cand": fakes.ratio})
    fakes.FakeProbe({"cand": fakes.ratio, "strat": fakes.ratio}).install(monkeypatch)
    strategy = _meta(tmp_path, "strat", "b", kind="strategy")
    assert _rejudge(tmp_path, payload, [strategy], mode="enforce") == pipeline.Rejudgement()


# --- 比較相手の選び方 --------------------------------------------------------------

def test_a_deployed_strategy_is_not_built_or_compared(tmp_path, monkeypatch):
    strategy = _meta(tmp_path, "strat", "9", kind="strategy")
    report = stage(tmp_path, monkeypatch, {"cand": fakes.ratio}, deployed=[strategy])
    assert report.ok and report.similar_to == () and report.built == 1


def test_only_the_very_same_submission_is_skipped_among_pending(tmp_path, monkeypatch):
    own = ("pending-1", "c" * 64, "cand")
    assert stage(tmp_path, monkeypatch, {"cand": fakes.ratio}, pending=[own]).ok
    renamed_copy = ("pending-2", "c" * 64, "other")
    report = stage(tmp_path / "r", monkeypatch, {"cand": fakes.ratio},
                   pending=[renamed_copy])
    assert (report.ok, report.code) == (False, "indicator_duplicate")


# --- 親専有の数値 ------------------------------------------------------------------

def _compare(candidate_fn, other_fn, *, mode="warn", corr=0.5):
    bars, _ = pipeline.canonical_bars("1h")
    tail = bars.tail(400)
    classes = {"value": ("invariant", "invariant")}
    return gate.compare_outputs(
        candidate_fn(tail), {"dep": other_fn(tail)}, mode=mode, similar_corr=corr,
        classifications=classes)


def test_every_relation_kind_carries_its_numbers_in_the_same_order_as_similar_to():
    exact = _compare(fakes.ratio, fakes.ratio)
    assert exact.measures == ("exact column=value",)
    mono = _compare(fakes.ratio, fakes.affine_of_ratio)
    assert len(mono.measures) == 1
    assert mono.measures == ("column=value level_spearman=1.0000 diff_spearman=1.0000",)
    cubic = _compare(fakes.ratio, fakes.cubic_of_ratio)
    assert cubic.similar_to[0]["kind"] == "monotone"
    assert cubic.measures == ("column=value level_spearman=1.0000 diff_spearman=0.9954",)
    similar = _compare(fakes.ratio, fakes.ratio_with_wobble)
    assert similar.similar_to[0]["kind"] == "similar"
    assert similar.measures[0].startswith("column=value diff_pearson=0.")
    nothing = _compare(fakes.ratio, fakes.noise, corr=0.99)
    assert nothing.similar_to == () and nothing.measures == ()


def test_the_overlap_numbers_name_the_exact_column():
    result = _compare(fakes.one_shared_column, fakes.two_columns)
    assert [r["kind"] for r in result.similar_to] == ["overlap"]
    assert result.measures == ("exact column=value",)


def test_the_detail_lines_pair_each_relation_with_its_numbers():
    result = gate.CompareResult(
        similar_to=(_row("a", "duplicate"), _row("b", "similar")),
        measures=("exact column=value", "column=value diff_pearson=0.9876"))
    assert pipeline._relation_detail(result) == (
        "- duplicate: a.value (exact column=value)\n"
        "- similar: b.value (column=value diff_pearson=0.9876)")
    bare = gate.CompareResult(similar_to=(_row("a", "duplicate"),))
    assert pipeline._relation_detail(bare) == "- duplicate: a.value"
    assert pipeline._relation_detail(gate.CompareResult()) == ""


# --- 想定外例外の分類・世代違い・候補起因の配備済み失敗 -------------------------------

@pytest.mark.parametrize("exc", [ValueError("v"), KeyError("k"), OSError("o")])
def test_a_failure_building_the_canonical_fixture_is_retryable(
        tmp_path, monkeypatch, exc):
    fakes.FakeProbe({"cand": fakes.ratio}).install(monkeypatch)

    def broken(timeframe):
        raise exc

    monkeypatch.setattr(pipeline, "canonical_bars", broken)
    with pytest.raises(pipeline.IndicatorGateUnavailable):
        pipeline.run_fixture_stage(
            _meta(tmp_path, "cand", "c"), settings=SETTINGS,
            data_dir=tmp_path / "data", deployed_metas=[], pending=[],
            mode="warn", lane="loop")


@pytest.mark.parametrize("exc", [ValueError("v"), KeyError("k")])
def test_a_foreign_error_in_the_candidates_own_cache_is_retryable(
        tmp_path, monkeypatch, exc):
    fakes.FakeProbe({"cand": fakes.ratio}).install(monkeypatch)
    _fail_get_or_build(monkeypatch, exc)
    with pytest.raises(pipeline.IndicatorGateUnavailable):
        pipeline.run_fixture_stage(
            _meta(tmp_path, "cand", "c"), settings=SETTINGS,
            data_dir=tmp_path / "data", deployed_metas=[], pending=[],
            mode="warn", lane="loop")


@pytest.mark.parametrize("lane", ["loop", "submit", "bless"])
def test_a_deployed_indicator_that_itself_fails_is_dropped_with_a_warning(
        tmp_path, monkeypatch, lane):
    report = stage(
        tmp_path, monkeypatch, {"cand": fakes.ratio, "rsi": fakes.ratio,
                                "ok": fakes.noise},
        deployed=[_meta(tmp_path, "rsi", "a"), _meta(tmp_path, "ok", "b")],
        lane=lane, probe_kw=dict(fail_names=frozenset({"rsi"})))
    assert report.ok and "duplicate_unavailable" in report.warnings
    assert report.unavailable == (("rsi", "probe_exception"),)
    assert report.similar_to == ()


def test_a_dropped_partner_is_not_compared_so_the_candidate_passes_its_twin(
        tmp_path, monkeypatch):
    report = stage(
        tmp_path, monkeypatch, {"cand": fakes.ratio, "rsi": fakes.ratio},
        deployed=[_meta(tmp_path, "rsi", "a")], lane="loop", mode="enforce",
        probe_kw=dict(fail_names=frozenset({"rsi"})))
    assert report.ok and report.code == ""


def test_an_environment_failure_of_a_deployed_indicator_stays_retryable_in_the_loop(
        tmp_path, monkeypatch):
    deployed = [_meta(tmp_path, "rsi", "a")]
    fakes.FakeProbe({"cand": fakes.ratio, "rsi": fakes.ratio}).install(monkeypatch)
    real = pipeline.probe_mod.open_session

    def flaky(meta, plugin_settings):
        if meta.name == "rsi":
            raise pipeline.probe_mod.IndicatorEnvironmentError("sandbox_unavailable")
        return real(meta, plugin_settings)

    monkeypatch.setattr(pipeline.probe_mod, "open_session", flaky)
    with pytest.raises(pipeline.IndicatorGateUnavailable):
        pipeline.run_fixture_stage(
            _meta(tmp_path, "cand", "c"), settings=SETTINGS,
            data_dir=tmp_path / "data", deployed_metas=deployed, pending=[],
            mode="warn", lane="loop")


def test_rejudge_skips_a_payload_of_another_fixture_generation(tmp_path, monkeypatch):
    payload, deployed = _twin_setup(tmp_path, monkeypatch, fakes.ratio)
    old_key = cache.CacheKey("c" * 64, payload["fixture_data_hash"], "1h")
    entry = cache.read(tmp_path / "data", old_key)
    assert entry is not None
    cache.get_or_build(tmp_path / "data",
                       cache.CacheKey("c" * 64, "e" * 64, "1h"), lambda: entry)
    payload = {**payload, "fixture_data_hash": "e" * 64}
    built = []
    real = cache.get_or_build

    def spy(*a, **k):
        built.append(a)
        return real(*a, **k)

    monkeypatch.setattr(pipeline.cache, "get_or_build", spy)
    assert _rejudge(tmp_path, payload, deployed, mode="enforce") is None
    assert built == []
