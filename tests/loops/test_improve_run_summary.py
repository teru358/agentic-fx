"""要約の組み立て・形の検査・上限の単体。"""
from __future__ import annotations

import json
import math
from pathlib import Path

import jsonschema
import pytest

from agentic_fx.config import load_settings
from agentic_fx.loops import improve_run_summary as rs
from agentic_fx.store import improve_run_summaries as store

_SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")
PAIRS = ["USDJPY", "EURUSD"]


def _output(**overrides):
    out = {"discoveries": [],
           "selected": {"backlog_id": 7, "idea": "try a pullback"},
           "artifact": {"type": "plugin", "name": "cand", "kind": "strategy",
                        "self_test": "passed", "summary": "a summary"},
           "selection_rationale": "because"}
    out.update(overrides)
    return out


def _metrics(pf=1.2, avg_r=0.1, trades=40, **extra):
    return {"trades": trades, "pf": pf, "avg_r": avg_r, "evaluable": True,
            "max_drawdown": 0.05, "eval_bars": 1000, **extra}


def _prepare(material, plan, pairs=PAIRS):
    return rs.prepare(material, plan, allowed_pairs=pairs)


# --- 計画ごとの項目 ------------------------------------------------------------

def test_plan_none_takes_nothing_even_with_every_material():
    material = rs.SummaryMaterial(
        output=_output(), plugin_name="cand",
        floor=rs.floor_material(_SETTINGS, in_sample={"USDJPY": _metrics()},
                                holdout=None),
        observation_text="obs", parent_reason="duplicate_logic")
    prepared = _prepare(material, rs.PLAN_NONE)
    assert prepared.items == {} and not prepared.failed
    assert rs.serialize(prepared).state == store.STATE_EMPTY


def test_hypothesis_plan_has_no_explanation():
    material = rs.SummaryMaterial(output=_output())
    items = _prepare(material, rs.PLAN_LOSER).items
    assert set(items) == {"hypothesis"}
    assert items["hypothesis"] == {"idea": "try a pullback", "backlog_id": 7}


def test_claims_need_a_schema_valid_output():
    for bad in (None, {}, {"selected": {"idea": "x"}}, "text",
                _output(extra="nope")):
        assert _prepare(rs.SummaryMaterial(output=bad),
                        rs.PLAN_CLAIMS).items == {}


def test_name_only_plan_drops_the_parents_meta():
    meta = rs.CandidateMeta("strategy", True, ("rsi",))
    material = rs.SummaryMaterial(plugin_name="cand", candidate=meta)
    name_only = _prepare(material, rs.PLAN_COMMIT_CRASH).items["tried"]
    assert name_only == {"plugin": "cand", "kind": None, "origin": None,
                         "indicators": []}
    full = _prepare(material, rs.PLAN_SUBMITTED).items["tried"]
    assert full == {"plugin": "cand", "kind": "strategy", "origin": "new",
                    "indicators": ["rsi"]}


def test_no_tried_without_a_checked_name():
    material = rs.SummaryMaterial(
        candidate=rs.CandidateMeta("strategy", False, ()))
    assert "tried" not in _prepare(material, rs.PLAN_SUBMITTED).items


def test_parent_reason_must_be_a_fixed_word():
    ok = rs.SummaryMaterial(parent_reason="family_redirect")
    assert _prepare(ok, rs.PLAN_DEMOTED).items["parent_reason"] \
        == "family_redirect"
    bad = rs.SummaryMaterial(parent_reason="free text from somewhere")
    assert "parent_reason" not in _prepare(bad, rs.PLAN_DEMOTED).items
    assert "parent_reason" not in _prepare(ok, rs.PLAN_REPORT).items


def test_candidate_meta_collects_unique_sorted_indicator_names_for_strategy():
    class Ref:
        def __init__(self, plugin):
            self.plugin = plugin

    class Meta:
        name, kind = "cand", "strategy"
        indicators = (Ref("rsi"), Ref("atr"), Ref("rsi"), Ref("Bad Name"))

    meta = rs.candidate_meta(Meta, deployed_names=["cand"])
    assert (meta.kind, meta.is_new, meta.indicators) == (
        "strategy", False, ("atr", "rsi"))

    class Indicator(Meta):
        kind = "indicator"

    assert rs.candidate_meta(Indicator, deployed_names=[]).indicators == ()


def test_indicator_names_are_capped_at_twenty():
    names = tuple(f"ind{i:02d}" for i in range(30))
    material = rs.SummaryMaterial(
        plugin_name="cand", candidate=rs.CandidateMeta("strategy", True, names))
    tried = _prepare(material, rs.PLAN_SUBMITTED).items["tried"]
    assert len(tried["indicators"]) == rs.INDICATOR_NAMES_MAX


# --- 床 ---------------------------------------------------------------------

def test_floor_from_verdict_dicts_marks_failed_pairs_and_conditions():
    in_sample = {"USDJPY": _metrics(pf=0.4, avg_r=-0.2),
                 "EURUSD": _metrics(pf=1.4, max_drawdown=0.9)}
    floor = rs.floor_material(_SETTINGS, in_sample=in_sample, holdout=None)
    item = _prepare(rs.SummaryMaterial(floor=floor), rs.PLAN_GATE_FAILED
                    ).items["floor"]
    assert item["failed_stage"] == "in_sample"
    assert item["failed_conditions"] == ["pf", "drawdown_too_deep"]
    assert item["failed_pair_count"] == 2
    first, second = item["stages"][0]["pairs"]
    assert (first["failed"], second["failed"]) == (["pf"], ["drawdown_too_deep"])
    assert item["stages"][1] == {"stage": "holdout", "state": "not_run",
                                 "pairs": []}


def test_floor_failed_items_agree_with_the_gates_own_judgement():
    from agentic_fx.plugin.strategy_gate import floor_fail_items
    in_sample = {"USDJPY": _metrics(pf=0.4), "EURUSD": _metrics(trades=0)}
    expected = {(i.pair, i.kind) for i in floor_fail_items(
        in_sample, settings=_SETTINGS, scope="in_sample")}
    floor = rs.floor_material(_SETTINGS, in_sample=in_sample, holdout={})
    item = _prepare(rs.SummaryMaterial(floor=floor), rs.PLAN_GATE_FAILED
                    ).items["floor"]
    actual = {(p["pair"], k) for p in item["stages"][0]["pairs"]
              for k in p["failed"]}
    assert actual == expected


def test_floor_from_rows_uses_the_candidates_rows_as_reference_only():
    rows = [
        {"scope": "in_sample", "pair": "USDJPY", "variant": "candidate",
         "metrics": _metrics(pf=0.8, trades=12)},
        {"scope": "in_sample", "pair": "USDJPY", "variant": "no_strategy",
         "metrics": _metrics(pf=None, trades=0)},
        {"scope": "in_sample", "pair": "USDJPY", "variant": "baseline",
         "metrics": _metrics(pf=9.9, trades=999)},
        {"scope": "in_sample", "pair": "EURUSD",
         "metrics": _metrics(pf=0.7, trades=9)},
    ]
    floor = rs.floor_material(_SETTINGS, in_sample=None, holdout=None,
                              rows=rows)
    item = _prepare(rs.SummaryMaterial(floor=floor), rs.PLAN_GATE_FAILED
                    ).items["floor"]
    in_sample, holdout = item["stages"]
    assert in_sample["state"] == "reference" and holdout["state"] == "not_run"
    assert [(p["pair"], p["pf"], p["trades"]) for p in in_sample["pairs"]] == [
        ("USDJPY", 0.8, 12), ("EURUSD", 0.7, 9)]
    assert item["failed_stage"] is None and item["failed_conditions"] == []


def test_floor_from_no_usable_rows_is_none():
    assert rs.floor_material(_SETTINGS, in_sample=None, holdout=None,
                             rows=[]) is None
    only_baseline = [{"scope": "in_sample", "pair": "USDJPY",
                      "variant": "baseline", "metrics": _metrics()}]
    assert rs.floor_material(_SETTINGS, in_sample=None, holdout=None,
                             rows=only_baseline) is None


def test_floor_values_are_numbers_or_null_and_pairs_are_the_configured_ones():
    in_sample = {"USDJPY": _metrics(pf=None, avg_r=math.nan, trades=True),
                 "EVILPAIR": _metrics()}
    floor = rs.floor_material(_SETTINGS, in_sample=in_sample, holdout=None)
    pairs = _prepare(rs.SummaryMaterial(floor=floor), rs.PLAN_GATE_FAILED
                     ).items["floor"]["stages"][0]["pairs"]
    assert [p["pair"] for p in pairs] == ["USDJPY"]
    assert (pairs[0]["pf"], pairs[0]["avg_r"], pairs[0]["trades"]) == (
        None, None, None)


def test_thresholds_are_the_gate_snapshot_at_record_time():
    floor = rs.floor_material(_SETTINGS, in_sample={"USDJPY": _metrics()},
                              holdout=None)
    assert floor.thresholds == _SETTINGS.improve.gate.snapshot()
    item = _prepare(rs.SummaryMaterial(floor=floor), rs.PLAN_SUBMITTED
                    ).items["floor"]
    assert item["thresholds"] == _SETTINGS.improve.gate.snapshot()


def test_holdout_measured_when_present():
    floor = rs.floor_material(
        _SETTINGS, in_sample={"USDJPY": _metrics()},
        holdout={"USDJPY": _metrics(pf=0.5)})
    item = _prepare(rs.SummaryMaterial(floor=floor), rs.PLAN_SUBMITTED
                    ).items["floor"]
    assert item["failed_stage"] == "holdout"
    assert item["stages"][1]["state"] == "measured"


# --- 比較 -------------------------------------------------------------------

def _comparison(verdict="not_improved", reason="holdout_not_exceeds"):
    side = {"trades": 40, "pf": 1.1, "avg_r": 0.05, "win_rate": 0.5, "evaluable": True}
    return {"mode": "enforce", "verdict": verdict, "reason": reason,
            "stages": {"holdout": {"USDJPY": {
                "candidate": side, "deployed": {**side, "pf": 1.3},
                "verdict": "not_improved"}}, "in_sample": {}}}


def test_comparison_item_keeps_fixed_words_and_the_compared_metrics_only():
    item = _prepare(rs.SummaryMaterial(comparison=_comparison()),
                    rs.PLAN_GATE_FAILED).items["comparison"]
    assert item["verdict"] == "not_improved" and item["metrics"] == ["pf", "avg_r"]
    assert item["pairs"] == [{
        "pair": "USDJPY", "verdict": "not_improved",
        "candidate": {"trades": 40, "pf": 1.1, "avg_r": 0.05, "evaluable": True},
        "deployed": {"trades": 40, "pf": 1.3, "avg_r": 0.05, "evaluable": True}}]


def test_comparison_with_unknown_words_is_left_out():
    bad = _comparison(reason="free text")
    assert "comparison" not in _prepare(
        rs.SummaryMaterial(comparison=bad), rs.PLAN_GATE_FAILED).items


def test_comparison_is_not_part_of_a_report_plan():
    assert "comparison" not in _prepare(
        rs.SummaryMaterial(comparison=_comparison()), rs.PLAN_DEMOTED).items


# --- agent の文章 -----------------------------------------------------------

def test_agent_text_sanitizes_and_cuts():
    text = rs.agent_text("a\nb\t c https://x.example/p?apikey=SECRET\x07 d")
    assert text == "a b c <url> d"
    long = rs.agent_text("あ" * 900)
    assert long.startswith("あ" * rs.TEXT_LIMIT)
    assert long.endswith(rs.TRUNCATION_SUFFIX)
    assert rs.agent_text("   ") is None and rs.agent_text(None) is None
    assert rs.agent_text(12) is None


# --- 形・上限 ---------------------------------------------------------------

def test_items_schema_rejects_unknown_keys_and_over_long_values():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"surprise": 1}, rs.ITEMS_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"observation": "x" * 2000}, rs.ITEMS_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {"tried": {"plugin": "a", "kind": None, "origin": None,
                       "indicators": ["x"] * 21}}, rs.ITEMS_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"parent_reason": "made up"}, rs.ITEMS_SCHEMA)


def test_serialize_drops_explanation_then_handoff_then_pairs_to_fit():
    pairs = [f"P{i:05d}" for i in range(rs.PAIR_ROWS_MAX)]
    stage_pairs = {p: _metrics() for p in pairs}
    floor = rs.floor_material(
        _SETTINGS, in_sample=stage_pairs, holdout=stage_pairs)
    big = "z" * 10_000
    material = rs.SummaryMaterial(output=_output(selection_rationale=big,
                                                 selected={"backlog_id": None,
                                                           "idea": big}),
                                  plugin_name="cand", floor=floor)
    prepared = rs.with_handoff(
        rs.prepare(material, rs.PLAN_SUBMITTED, allowed_pairs=pairs),
        {"family_key": "indicator:rsi@1h", "attempts": 2,
         "outcomes": {"pending": 2}})
    record = rs.serialize(prepared)
    assert record.state == store.STATE_OK
    assert len(record.summary_json.encode("utf-8")) <= rs.SUMMARY_MAX_BYTES
    jsonschema.validate(json.loads(record.summary_json), rs.ITEMS_SCHEMA)


def _sized(n_pairs, *, explanation):
    pairs = [f"P{i:05d}" for i in range(n_pairs)]
    metrics = {p: _metrics() for p in pairs}
    floor = rs.floor_material(_SETTINGS, in_sample=metrics, holdout=metrics)
    output = _output(selection_rationale="r" * 5000,
                     selected={"backlog_id": None, "idea": "i" * 5000})
    plan = rs.PLAN_SUBMITTED if explanation else rs.SummaryPlan(
        claims="hypothesis", tried="full", floor=True, handoff=True)
    prepared = rs.with_handoff(
        rs.prepare(rs.SummaryMaterial(output=output, plugin_name="cand",
                                      floor=floor), plan, allowed_pairs=pairs),
        {"family_key": "indicator:rsi@1h", "attempts": 2,
         "outcomes": {"pending": 2}})
    return prepared


def _raw_size(prepared):
    return len(rs._dump(prepared.items).encode("utf-8"))


def test_serialize_drops_the_explanation_before_anything_else():
    n = next(n for n in range(1, rs.PAIR_ROWS_MAX + 1)
             if _raw_size(_sized(n, explanation=True)) > rs.SUMMARY_MAX_BYTES
             >= _raw_size(_sized(n, explanation=False)))
    items = json.loads(rs.serialize(_sized(n, explanation=True)).summary_json)
    assert "explanation" not in items
    assert "handoff" in items and items["floor"]["stages"][0]["pairs"]


def test_serialize_drops_the_handoff_after_the_explanation_and_before_pairs(
        monkeypatch):
    prepared = _sized(20, explanation=True)

    def size_without(*keys):
        items = {k: v for k, v in prepared.items.items() if k not in keys}
        return len(rs._dump(items).encode("utf-8"))

    # 説明だけ落としても入らず、申し送りまで落とせばちょうど入る上限
    cap = size_without("explanation", "handoff")
    assert size_without("explanation") > cap
    with monkeypatch.context() as mp:
        mp.setattr(rs, "SUMMARY_MAX_BYTES", cap)
        items = json.loads(rs.serialize(prepared).summary_json)
    assert "explanation" not in items and "handoff" not in items
    assert items["floor"]["stages"][0]["pairs"]


def test_serialize_drops_the_pair_rows_last():
    prepared = _sized(rs.PAIR_ROWS_MAX, explanation=True)
    items = json.loads(rs.serialize(prepared).summary_json)
    assert "explanation" not in items
    assert items["floor"]["stages"][0]["pairs"] == []


def test_serialize_failure_is_build_failed_and_nothing_is_stored():
    prepared = rs.PreparedSummary({"observation": 3})  # 形が合わない
    record = rs.serialize(prepared)
    assert (record.state, record.summary_json) == (
        store.STATE_BUILD_FAILED, "{}")
    assert rs.serialize(rs.PreparedSummary({}, failed=True)).state \
        == store.STATE_BUILD_FAILED


def test_output_for_storage_boundary():
    small = {"k": "v"}
    assert rs.output_for_storage(small) == small
    assert rs.output_for_storage(None) is None
    assert rs.output_for_storage("not a dict") is None
    pad = rs.OUTPUT_JSON_MAX_BYTES - len(json.dumps({"k": ""}))
    exact = {"k": "a" * pad}
    assert len(json.dumps(exact, ensure_ascii=False).encode()) \
        == rs.OUTPUT_JSON_MAX_BYTES
    assert rs.output_for_storage(exact) == exact
    over = {"k": "a" * (pad + 1)}
    assert rs.output_for_storage(over) == {
        "truncated": True, "bytes": rs.OUTPUT_JSON_MAX_BYTES + 1}


def test_output_for_storage_counts_utf8_bytes():
    text = "あ" * (rs.OUTPUT_JSON_MAX_BYTES // 3)
    result = rs.output_for_storage({"k": text})
    assert result["truncated"] is True and result["bytes"] > rs.OUTPUT_JSON_MAX_BYTES


# --- API の形 ----------------------------------------------------------------

def _row(state="ok", version=1, items=None):
    return {"summary_version": version, "state": state,
            "summary_json": json.dumps(items if items is not None else {})}


def test_dto_of_shapes():
    assert rs.dto_of(None) is None
    ok = rs.dto_of(_row(items={"observation": "x"}))
    assert ok == {"version": 1, "state": "ok", "items": {"observation": "x"}}
    empty = rs.dto_of(_row("empty"))
    assert empty == {"version": 1, "state": "empty", "items": {}}
    failed = rs.dto_of(_row("build_failed"))
    assert failed == {"version": 1, "state": "build_failed", "items": {}}


def test_dto_of_never_returns_items_that_break_the_shape():
    broken = rs.dto_of(_row(items={"observation": 3}))
    assert broken == {"version": 1, "state": "build_failed", "items": {}}
    not_json = rs.dto_of({"summary_version": 1, "state": "ok",
                          "summary_json": "{not json"})
    assert not_json["state"] == "build_failed" and not_json["items"] == {}
    future = rs.dto_of(_row(version=2, items={"observation": "x"}))
    assert future["version"] == 2 and future["items"] == {}
    assert future["state"] == "build_failed"


# --- validator の使い回し ------------------------------------------------------

def test_shape_checks_do_not_rebuild_a_validator_per_call(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("jsonschema.validate rebuilds the validator")

    monkeypatch.setattr(jsonschema, "validate", boom)
    material = rs.SummaryMaterial(output=_output(), plugin_name="cand",
                                  observation_text="obs")
    prepared = _prepare(material, rs.PLAN_SUBMITTED)
    assert "hypothesis" in prepared.items
    record = rs.serialize(prepared)
    assert record.state == store.STATE_OK
    dto = rs.dto_of({"summary_version": 1, "state": "ok",
                     "summary_json": record.summary_json})
    assert dto["state"] == "ok" and "hypothesis" in dto["items"]
    broken = rs.dto_of(_row(items={"observation": 3}))
    assert broken["state"] == "build_failed"


# --- 16 KiB を超えるときの削り順 ------------------------------------------------

def _many_pairs(n=rs.PAIR_ROWS_MAX):
    pairs = [f"{'P' * 61}{i:03d}" for i in range(n)]
    entry = {"verdict": "improved",
             "candidate": _metrics(pf=1.123456, avg_r=0.123456),
             "deployed": _metrics(pf=1.234567, avg_r=0.234567)}
    comparison = {"mode": "enforce", "verdict": "improved",
                  "reason": "holdout_exceeds",
                  "stages": {"holdout": {p: dict(entry) for p in pairs}}}
    floor = rs.floor_material(_SETTINGS, in_sample={pairs[0]: _metrics()},
                              holdout=None)
    output = _output(selected={"backlog_id": 3, "idea": "あ" * 500},
                     selection_rationale="い" * 500)
    material = rs.SummaryMaterial(output=output, plugin_name="cand",
                                  floor=floor, comparison=comparison)
    prepared = rs.with_handoff(
        _prepare(material, rs.PLAN_SUBMITTED, pairs=pairs),
        {"family_key": "indicator:rsi@1h", "attempts": 2,
         "outcomes": {"pending": 2}})
    return pairs, prepared


def _fitted_size(prepared, *, pairs_kept, hypothesis):
    items = json.loads(rs._dump(prepared.items))
    for key in ("explanation", "handoff"):
        items.pop(key)
    for stage in items["floor"]["stages"]:
        stage["pairs"] = []
    items["tried"]["indicators"] = []
    items["comparison"]["pairs"] = items["comparison"]["pairs"][:pairs_kept]
    if not hypothesis:
        items.pop("hypothesis")
    return len(rs._dump(items).encode("utf-8"))


def test_many_comparison_pairs_do_not_turn_the_summary_into_build_failed():
    pairs, prepared = _many_pairs()
    assert _fitted_size(prepared, pairs_kept=64, hypothesis=True) \
        > rs.SUMMARY_MAX_BYTES
    record = rs.serialize(prepared)
    assert record.state == store.STATE_OK
    assert len(record.summary_json.encode("utf-8")) <= rs.SUMMARY_MAX_BYTES
    items = json.loads(record.summary_json)
    assert items["hypothesis"]["backlog_id"] == 3
    assert items["floor"]["thresholds"] and items["tried"]["plugin"] == "cand"
    kept = [row["pair"] for row in items["comparison"]["pairs"]]
    assert 0 < len(kept) < len(pairs) and kept == pairs[:len(kept)]
    jsonschema.validate(items, rs.ITEMS_SCHEMA)


def test_comparison_pairs_are_cut_from_the_back_only_as_far_as_needed(
        monkeypatch):
    pairs, prepared = _many_pairs()
    cap = _fitted_size(prepared, pairs_kept=10, hypothesis=True)
    assert _fitted_size(prepared, pairs_kept=11, hypothesis=True) > cap
    monkeypatch.setattr(rs, "SUMMARY_MAX_BYTES", cap)
    items = json.loads(rs.serialize(prepared).summary_json)
    assert [row["pair"] for row in items["comparison"]["pairs"]] == pairs[:10]
    assert "hypothesis" in items


def test_the_hypothesis_is_dropped_after_every_comparison_pair(monkeypatch):
    _pairs, prepared = _many_pairs()
    cap = _fitted_size(prepared, pairs_kept=0, hypothesis=True)
    assert _fitted_size(prepared, pairs_kept=0, hypothesis=False) < cap
    monkeypatch.setattr(rs, "SUMMARY_MAX_BYTES", cap)
    items = json.loads(rs.serialize(prepared).summary_json)
    assert items["comparison"]["pairs"] == [] and "hypothesis" in items
    monkeypatch.setattr(rs, "SUMMARY_MAX_BYTES", cap - 1)
    items = json.loads(rs.serialize(prepared).summary_json)
    assert "hypothesis" not in items and items["comparison"]["pairs"] == []
    assert items["floor"]["thresholds"]


# --- 保存する元の出力 ----------------------------------------------------------

def test_output_for_storage_sanitizes_every_string_leaf_and_keeps_the_shape():
    url = "https://leak.example/p?apikey=SECRET9"
    output = {"a": f"x {url}", "n": 3, "f": 1.5, "b": True, "z": None,
              "nested": {"list": [f"apikey=SECRET9 {url}", {"deep": url}, 2]}}
    stored = rs.output_for_storage(output)
    text = json.dumps(stored, ensure_ascii=False)
    assert "SECRET9" not in text and "leak.example" not in text
    assert stored["n"] == 3 and stored["f"] == 1.5 and stored["b"] is True
    assert stored["z"] is None and stored["nested"]["list"][2] == 2
    assert stored["a"] == "x <url>"
    assert output["a"] == f"x {url}"  # 引数は変えない


def test_output_for_storage_keeps_plain_text_and_whitespace_as_is():
    output = {"k": "line1\n  line2\ttab", "n": [1, "あ"]}
    assert rs.output_for_storage(output) == output


def test_output_for_storage_too_deep_is_not_stored():
    deep: dict = {}
    node = deep
    for _ in range(5000):
        node["k"] = {}
        node = node["k"]
    assert rs.output_for_storage(deep) is None


# --- 床の失敗は許す pair の集合から導く --------------------------------------------

def test_floor_failures_of_pairs_outside_the_settings_are_not_reported():
    bad = _metrics(pf=0.3)
    floor = rs.floor_material(_SETTINGS, in_sample={"GBPJPY": bad},
                              holdout=None)
    item = _prepare(rs.SummaryMaterial(floor=floor),
                    rs.PLAN_SUBMITTED).items["floor"]
    assert (item["failed_stage"], item["failed_conditions"],
            item["failed_pair_count"]) == (None, [], 0)


def test_floor_failure_items_come_from_the_same_pair_set():
    in_sample = {"GBPJPY": _metrics(pf=0.3),
                 "USDJPY": _metrics(pf=1.5, avg_r=-0.1)}
    floor = rs.floor_material(_SETTINGS, in_sample=in_sample, holdout=None)
    item = _prepare(rs.SummaryMaterial(floor=floor),
                    rs.PLAN_SUBMITTED).items["floor"]
    assert item["failed_stage"] == "in_sample"
    assert item["failed_conditions"] == ["avg_r"]
    assert item["failed_pair_count"] == 1
    # 外れた段の失敗だけが allowed の外なら、次の段の失敗を採る
    holdout = {"USDJPY": _metrics(pf=0.3, avg_r=-0.1)}
    floor = rs.floor_material(
        _SETTINGS, in_sample={"GBPJPY": _metrics(pf=0.3)}, holdout=holdout)
    item = _prepare(rs.SummaryMaterial(floor=floor),
                    rs.PLAN_SUBMITTED).items["floor"]
    assert (item["failed_stage"], item["failed_pair_count"]) == ("holdout", 1)
    assert item["failed_conditions"] == ["pf"]


# --- 名前の長さ ---------------------------------------------------------------

def test_over_long_pair_and_plugin_names_are_left_out_not_a_build_failure():
    long = "P" * (rs.NAME_LIMIT + 1)
    pairs = ["USDJPY", long]
    metrics = {"USDJPY": _metrics(), long: _metrics()}
    comparison = {"mode": "enforce", "verdict": "improved",
                  "reason": "holdout_exceeds",
                  "stages": {"holdout": {p: {
                      "verdict": "improved", "candidate": _metrics(),
                      "deployed": _metrics()} for p in pairs}}}
    material = rs.SummaryMaterial(
        output=_output(), plugin_name="x" * (rs.NAME_LIMIT + 1),
        floor=rs.floor_material(_SETTINGS, in_sample=metrics, holdout=metrics),
        comparison=comparison)
    prepared = _prepare(material, rs.PLAN_SUBMITTED, pairs=pairs)
    record = rs.serialize(prepared)
    assert record.state == store.STATE_OK
    items = json.loads(record.summary_json)
    assert "tried" not in items
    assert [r["pair"] for s in items["floor"]["stages"] for r in s["pairs"]] \
        == ["USDJPY", "USDJPY"]
    assert [r["pair"] for r in items["comparison"]["pairs"]] == ["USDJPY"]


# --- 他 module の private 名に依存しない --------------------------------------------

def test_the_summary_module_uses_only_public_names_of_other_modules():
    import ast
    tree = ast.parse(Path(rs.__file__).read_text(encoding="utf-8"))
    private = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            private += [a.name for a in node.names if a.name.startswith("_")]
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_") \
                and not node.attr.startswith("__") \
                and isinstance(node.value, ast.Name) \
                and node.value.id not in ("self", "cls"):
            private.append(f"{node.value.id}.{node.attr}")
    assert private == []


def test_floor_fail_items_is_a_public_name_of_the_gate():
    from agentic_fx.plugin import strategy_gate
    items = strategy_gate.floor_fail_items(
        {"USDJPY": _metrics(pf=0.3)}, settings=_SETTINGS, scope="in_sample")
    assert [(i.pair, i.kind) for i in items] == [("USDJPY", "pf")]


def test_output_for_storage_scrubs_keys_and_secret_named_fields():
    leaked = {"https://leak.example/?apikey=SECRET9": "x",
              "nested": {"api_key": "123456", "Token": 7, "client_secret": ["a"],
                         "password": {"v": 1}, "ordinary": "keep"},
              "list": [{"access_token": "zzz"}]}
    stored = rs.output_for_storage(leaked)
    text = json.dumps(stored, ensure_ascii=False)
    for secret in ("SECRET9", "leak.example", "123456", '"zzz"'):
        assert secret not in text
    assert stored["nested"]["ordinary"] == "keep"
    assert stored["nested"]["api_key"] == "***"
    assert stored["nested"]["Token"] == "***"
    assert stored["nested"]["client_secret"] == "***"
    assert stored["nested"]["password"] == "***"
    assert stored["list"][0]["access_token"] == "***"


def test_output_for_storage_keeps_count_like_names():
    stored = rs.output_for_storage({"tokens": 120, "token_count": 5, "max_tokens": 9,
                                    "apiKey": "k1", "accessToken": "t1", "password_hint": "h"})
    assert stored["tokens"] == 120 and stored["token_count"] == 5 and stored["max_tokens"] == 9
    assert stored["apiKey"] == "***" and stored["accessToken"] == "***"
    assert stored["password_hint"] == "***"
