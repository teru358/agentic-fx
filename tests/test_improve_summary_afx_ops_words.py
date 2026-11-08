"""afx-ops の要約の描画が使う語と、daemon の要約の形の一致。

afx-ops は import せず、`ast` でリテラルを読むだけにする。"""
from __future__ import annotations

import ast
from pathlib import Path

from agentic_fx.loops import improve_run_summary as summary

RENDER = (Path(__file__).resolve().parents[1] / "afx-ops" / "afx_ops" / "gate"
          / "improve_render.py")
TREE = ast.parse(RENDER.read_text(encoding="utf-8"))


def _assigned(name: str) -> ast.expr:
    for node in TREE.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return node.value
    raise AssertionError(f"{name} is not assigned in improve_render.py")


def _literal(name: str):
    return ast.literal_eval(_assigned(name))


def test_the_comparison_reason_labels_cover_exactly_the_daemons_words():
    assert set(_literal("COMPARISON_REASON_LABELS")) == set(summary.COMPARISON_REASONS)


def test_the_kind_origin_and_stage_words_match_the_daemons_schema():
    tried = summary.ITEMS_SCHEMA["properties"]["tried"]["properties"]
    assert set(_literal("SUMMARY_KINDS")) == {k for k in tried["kind"]["enum"] if k}
    assert set(_literal("SUMMARY_ORIGINS")) == {o for o in tried["origin"]["enum"] if o}
    assert set(_literal("SUMMARY_STAGE_STATES")) == set(summary.STAGE_STATES)
    assert set(_literal("KIND_ORIGIN_LABELS")) == set(_literal("SUMMARY_ORIGINS"))


def test_the_discord_allowlist_names_only_items_of_the_schema_and_not_the_agents_claims():
    allow = _assigned("SUMMARY_ITEMS")
    assert isinstance(allow, ast.Dict)
    names = [ast.literal_eval(key) for key in allow.keys]
    assert set(names) <= set(summary.ITEMS_SCHEMA["properties"])
    assert not {"hypothesis", "explanation"} & set(names)


def test_the_item_and_list_limits_match_the_daemons():
    assert _literal("SUMMARY_LIST_MAX") == summary.PAIR_ROWS_MAX
    assert _literal("SUMMARY_INDICATORS_MAX") == summary.INDICATOR_NAMES_MAX
    assert _literal("SUMMARY_VERSION") == 1
    assert _literal("SUMMARY_OBSERVATION_MAX") >= summary.TEXT_LIMIT + len(
        summary.TRUNCATION_SUFFIX)
