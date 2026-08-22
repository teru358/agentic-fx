"""IMPROVE_OUTPUT_SCHEMA (設計書 §3.5)。"""
from __future__ import annotations

import jsonschema
import pytest

from agentic_fx.loops.summary import IMPROVE_OUTPUT_SCHEMA


def _valid_plugin_output():
    return {
        "discoveries": [{"idea": "x", "source": "agent", "evidence": "y"}],
        "selected": {"backlog_id": None, "idea": "x"},
        "artifact": {"type": "plugin", "name": "rsi_v2", "kind": "indicator",
                    "self_test": "passed", "summary": "s"},
        "selection_rationale": "because",
    }


def test_valid_plugin_output_passes():
    jsonschema.validate(_valid_plugin_output(), IMPROVE_OUTPUT_SCHEMA)


def test_valid_report_output_passes():
    out = _valid_plugin_output()
    out["artifact"] = {"type": "report", "proposal_kind": "core",
                       "title": "t", "body_md": "b"}
    jsonschema.validate(out, IMPROVE_OUTPUT_SCHEMA)


def test_valid_observation_output_passes():
    out = _valid_plugin_output()
    out["artifact"] = {"type": "observation", "reason": "no data"}
    jsonschema.validate(out, IMPROVE_OUTPUT_SCHEMA)


@pytest.mark.parametrize("missing_key", [
    "discoveries", "selected", "artifact", "selection_rationale"])
def test_missing_required_top_level_key_fails(missing_key):
    out = _valid_plugin_output()
    del out[missing_key]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(out, IMPROVE_OUTPUT_SCHEMA)


def test_schema_has_no_analysis_run_ids_or_trial_count_property():
    """§3.5: analysis_run_ids/trial_count は出力 schema に無い (agent に
    数えさせない — 親が RPC 台帳から作る)。"""
    top_props = IMPROVE_OUTPUT_SCHEMA.get("properties", {})
    assert "analysis_run_ids" not in top_props
    assert "trial_count" not in top_props


@pytest.mark.parametrize("bad_name", [
    "../evil", "a/b", "Rsi_V2", "_leading_underscore", "with space", ""])
def test_artifact_plugin_name_rejects_non_canonical_form(bad_name):
    """申し送り⑩の解決 (M4 pin): `artifact.name` の `pattern` が
    `^[a-z][a-z0-9_]{0,63}$` から外れる非正規形 (パストラバーサル・区切り
    文字混入・大文字・先頭 `_`・空白・空文字) を拒否することを schema
    単体で確認する。§4.2-1 の親側検査と二重防御になる箇所であり、
    `pattern` が削除される変異 (M4) を schema 単体で確実に殺す。"""
    out = _valid_plugin_output()
    out["artifact"]["name"] = bad_name
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(out, IMPROVE_OUTPUT_SCHEMA)
