"""indicator gate の周辺: 族表の relation の整形、last_result の語彙、台帳の移行、
settings.yaml.example の値、prompt の文言、通知語彙。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from agentic_fx import improve_notice_codes as codes
from agentic_fx.loops import improve_context
from agentic_fx.store import candidate_archives
from agentic_fx.store import db as db_mod
from agentic_fx.store.db import connect, init_db

_ROOT = Path(__file__).resolve().parents[2]


# --- relation の投影 ----------------------------------------------------------------

def _raw(*rows):
    return json.dumps([{"kind": k, "name": n} for k, n in rows])


@pytest.mark.parametrize("name,kept", [
    ("rsi", True), ("a" * 64, True), ("a" * 65, False), ("Rsi", False),
    ("1rsi", False), ("rsi-x", False), ("rsi x", False), ("", False),
    ("rsi\n", False)])
def test_a_relation_name_must_be_a_plain_plugin_name(name, kept):
    deployed = {name, "rsi"}
    found = improve_context.project_relations(_raw(("duplicate", name)), deployed)
    assert found == ([("duplicate", name)] if kept else [])


@pytest.mark.parametrize("raw", [None, 5, b"[]", [{"kind": "duplicate", "name": "rsi"}],
                                 {"kind": "duplicate", "name": "rsi"}])
def test_a_relation_value_that_is_not_a_json_string_projects_to_nothing(raw):
    assert improve_context.project_relations(raw, {"rsi"}) == []


@pytest.mark.parametrize("raw", ['{"kind": "duplicate", "name": "rsi"}', '"rsi"', "3",
                                 "null", "true"])
def test_a_json_value_that_is_not_a_list_projects_to_nothing(raw):
    assert improve_context.project_relations(raw, {"rsi"}) == []


def test_good_rows_survive_next_to_bad_ones_in_order():
    raw = json.dumps([3, "x", None, {"kind": "duplicate"}, {"name": "rsi"},
                      {"kind": "similar", "name": "rsi"},
                      {"kind": "weird", "name": "rsi"},
                      {"kind": "duplicate", "name": 3},
                      {"kind": ["duplicate"], "name": "rsi"},
                      {"kind": "overlap", "name": "ema"}])
    assert improve_context.project_relations(raw, {"rsi", "ema"}) == [
        ("similar", "rsi"), ("overlap", "ema")]


def test_relation_labels_take_the_strongest_kind_then_the_larger_count_then_the_name():
    labels = improve_context._relation_labels({
        ("similar", "b"): 1, ("similar", "a"): 1, ("similar", "c"): 5,
        ("duplicate", "z"): 1})
    assert labels == ["同等 z", "類似 c", "類似 a"]
    assert improve_context._relation_labels({
        ("overlap", "m"): 2, ("monotone", "m"): 1, ("duplicate", "m"): 1}
        ) == ["同等 m", "単調変換 m", "一部重複 m"]
    assert improve_context._relation_labels(
        {("similar", n): 1 for n in "abc"}) == ["類似 a", "類似 b", "類似 c"]
    assert improve_context._relation_labels({}) == []
    assert improve_context.FAMILY_RELATION_MAX == 3


# --- last_result の語彙 ---------------------------------------------------------------

def test_the_last_result_vocabulary_is_exactly_the_reject_and_mode_words():
    assert codes.INDICATOR_LAST_RESULT_CODES == {
        "symbol_in_name", "param_price_unit", "fixture_run_failed",
        "output_not_finite", "output_constant", "indicator_duplicate",
        "indicator_monotone_duplicate"}


@pytest.mark.parametrize("word", sorted(
    codes.INDICATOR_PAYLOAD_WARNING_CODES - codes.INDICATOR_LAST_RESULT_CODES))
def test_a_warning_word_never_passes_into_last_result(word):
    assert improve_context.project_last_result(
        f"observation:gate_failed:{word}") == "observation:gate_failed"


# --- 警告語の閉語彙 (承認 payload) ---------------------------------------------------------

def test_the_payload_warning_vocabulary_is_the_documented_one():
    assert codes.INDICATOR_PAYLOAD_WARNING_CODES == {
        "scale_dependent", "indicator_monotone_duplicate", "indicator_similar",
        "indicator_output_overlap", "sparse_output", "output_constant_suspect",
        "legacy_symbol_name", "indicator_redefined", "duplicate_unavailable",
        "symbol_in_name", "param_price_unit", "duplicate_at_approve"}
    assert "duplicate_at_approve" not in codes.INDICATOR_GATE_WARNING_CODES
    assert "duplicate_at_approve" not in codes.INDICATOR_LAST_RESULT_CODES
    assert codes.INDICATOR_GATE_WARNING_CODES <= codes.INDICATOR_PAYLOAD_WARNING_CODES


# --- 台帳の移行 ------------------------------------------------------------------------

def test_a_rebuild_keeps_the_relation_of_existing_rows(tmp_path):
    path = tmp_path / "rel.db"
    conn = connect(path)
    init_db(conn)
    conn.execute("DROP TABLE candidate_archives")
    conn.executescript(db_mod._CANDIDATE_ARCHIVES_DDL.replace(
        "  pair TEXT,\n", "  pair TEXT NOT NULL,\n"))
    relation = json.dumps([{"kind": "duplicate", "name": "rsi"}])
    conn.execute(
        "INSERT INTO candidate_archives (mission_id,name,content_hash,"
        "artifact_hash,archive_path,pair,metrics_json,created_at,relation) VALUES "
        "(1,'n','c','a',NULL,'USDJPY','{}','2026-10-01T00:00:00+00:00',?)",
        (relation,))
    conn.commit()
    assert not db_mod._candidate_archives_is_canonical(conn)
    conn.close()
    migrated = connect(path)
    init_db(migrated)
    assert db_mod._candidate_archives_is_canonical(migrated)
    assert [tuple(r) for r in migrated.execute(
        "SELECT relation FROM candidate_archives")] == [(relation,)]


# --- settings.yaml.example / prompt -------------------------------------------------------

def test_the_example_settings_state_the_three_indicator_keys_explicitly():
    improve = yaml.safe_load((_ROOT / "config" / "settings.yaml.example"
                              ).read_text())["improve"]
    assert improve["indicator_duplicate_mode"] == "warn"
    assert improve["indicator_similar_corr"] == 0.98
    assert improve["indicator_fixture_wall_clock_max_sec"] == 60


def test_the_prompt_keeps_the_deployed_family_instruction():
    text = improve_context.mission_template()
    assert ("`deployed=yes` の族は別名の新規候補にせず、表示された配備名の更新として"
            "扱ってください。") in text


def test_the_family_table_header_gains_a_relation_column_only_with_relations():
    row = {"family_key": "indicator (未配備) @1h", "attempts": 1,
           "outcomes": {"gate_failed": 1}, "last_attempt_month": "2026-10",
           "deployed": "no", "deployed_names": [], "relations": ["同等 rsi"]}
    section = {"rows": [row], "unkeyed_deployed": [], "entries_omitted": 0,
               "invalid_name_count": 0}
    rendered = improve_context.render_family_attempts(section)
    assert ("| family_key | attempts | outcomes | last_attempt_month | deployed |"
            " relation |\n|---|---:|---|---|---|---|") in rendered
    assert "| no | 同等 rsi |" in rendered
    plain = improve_context.render_family_attempts(
        {**section, "rows": [{**row, "relations": []}]})
    assert "relation" not in plain
    assert "| no |" in plain and "| no | -" not in plain
    dashed = improve_context.render_family_attempts(
        {**section, "rows": [row, {**row, "family_key": "x", "relations": []}]})
    assert "| no | - |" in dashed
