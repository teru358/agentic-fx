"""IMPROVE_OUTPUT_SCHEMA (設計書 §3.5)。"""
from __future__ import annotations

import jsonschema
import pytest

from agentic_fx.loops.summary import IMPROVE_OUTPUT_SCHEMA


def _valid_plugin_output():
    return {
        "discoveries": [{"idea": "x", "source": "agent", "evidence": "y",
                         "kind": "task"}],
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


@pytest.mark.parametrize("missing_key", ["proposal_kind", "title", "body_md"])
def test_report_variant_missing_required_key_fails(missing_key):
    """C13 是正 (束D検収, verified-local-round1.md §11 #18):
    report variant の `required` から `body_md` を外しても実測 SURVIVED
    (全スイート 2924 passed) だった — `test_valid_report_output_passes`
    が唯一の report ケースで、`body_md` を常に含んでいたため missing-key
    の否定側が未踏だった。`proposal_kind`/`title`/`body_md` の 3 値
    parametrize で report variant の required 全項目を pin する。"""
    out = _valid_plugin_output()
    out["artifact"] = {"type": "report", "proposal_kind": "core",
                       "title": "t", "body_md": "b"}
    del out["artifact"][missing_key]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(out, IMPROVE_OUTPUT_SCHEMA)


def test_schema_has_no_analysis_run_ids_or_trial_count_property():
    """§3.5: analysis_run_ids/trial_count は出力 schema に無い (agent に
    数えさせない — 親が RPC 台帳から作る)。"""
    top_props = IMPROVE_OUTPUT_SCHEMA.get("properties", {})
    assert "analysis_run_ids" not in top_props
    assert "trial_count" not in top_props


@pytest.mark.parametrize("bad_name", [
    "../evil", "a/b", "Rsi_V2", "_leading_underscore", "with space", "",
    "a" * 65,  # L30: 名前長境界 (64 は許容、65 は拒否)
    "rsi.v2",  # L32: ドットは pattern から禁止されている
    "1rsi",  # A25 是正 (束D検収, verified-local-round1.md §11 #17):
             # 数字始まりは `^[a-z]...` (先頭は文字必須) で拒否される。
             # `pattern` を `^[a-z0-9]` へ緩める変異はこのケースが無いと
             # 実測 SURVIVED になる (全スイート 2924 passed だった)。
])
def test_artifact_plugin_name_rejects_non_canonical_form(bad_name):
    """申し送り⑩の解決 (M4 pin): `artifact.name` の `pattern` が
    `^[a-z][a-z0-9_]{0,63}$` から外れる非正規形 (パストラバーサル・区切り
    文字混入・大文字・先頭 `_`・空白・空文字・65 文字超・ドット) を拒否
    することを schema 単体で確認する。§4.2-1 の親側検査と二重防御になる
    箇所であり、`pattern` が削除される変異 (M4) を schema 単体で確実に
    殺す。"""
    out = _valid_plugin_output()
    out["artifact"]["name"] = bad_name
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(out, IMPROVE_OUTPUT_SCHEMA)


def test_artifact_plugin_name_length_64_is_accepted():
    """L30: 境界の反対側 — 64 文字ちょうどは許容する。"""
    out = _valid_plugin_output()
    out["artifact"]["name"] = "a" + "b" * 63
    jsonschema.validate(out, IMPROVE_OUTPUT_SCHEMA)


# --- I3 / L31: additionalProperties: false (未知キー拒否) ---

@pytest.mark.parametrize("mutate", [
    lambda d: d.update(analysis_run_ids=[999], trial_count=1),
    lambda d: d["discoveries"][0].__setitem__("analysis_run_ids", [1]),
    lambda d: d["selected"].__setitem__("trial_count", 7),
    lambda d: d["artifact"].__setitem__("trial_count", 7),
], ids=["top_level", "discoveries_item", "selected", "artifact_plugin"])
def test_unknown_keys_are_rejected(mutate):
    """codex I3 killer: `analysis_run_ids`/`trial_count` を含む任意の未知
    キーは、挿入位置 (top-level / discoveries[] / selected / artifact) を
    問わず schema が拒否すること (§3.5: agent に分析 ID・回数を申告させ
    ない設計意図の schema 境界強制)。"""
    out = _valid_plugin_output()
    mutate(out)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(out, IMPROVE_OUTPUT_SCHEMA)


def test_artifact_plugin_variant_rejects_report_only_key():
    """c08#4: `additionalProperties: false` により plugin variant に
    report 専用の `proposal_kind` を混入すると oneOf 全 variant が非適合
    になり拒否される (排他性の副次的強化)。"""
    out = _valid_plugin_output()
    out["artifact"]["proposal_kind"] = "core"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(out, IMPROVE_OUTPUT_SCHEMA)


# --- L05: サブオブジェクトの required / enum 否定テスト ---

def test_discoveries_item_missing_evidence_is_rejected():
    out = _valid_plugin_output()
    del out["discoveries"][0]["evidence"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(out, IMPROVE_OUTPUT_SCHEMA)


def test_discoveries_item_missing_kind_is_rejected():
    out = _valid_plugin_output()
    del out["discoveries"][0]["kind"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(out, IMPROVE_OUTPUT_SCHEMA)


def test_selected_missing_backlog_id_is_rejected():
    out = _valid_plugin_output()
    del out["selected"]["backlog_id"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(out, IMPROVE_OUTPUT_SCHEMA)


def test_selected_empty_idea_is_rejected_by_schema():
    """C14 裁定 (2026-08-28、束D検収 verified-local-round1.md §7):
    `selected.idea` に `minLength: 1` を足す — 空文字は schema 層でも
    拒否する (2 層防御。空白のみはコード層 D18 是正が引き続き担う)。"""
    out = _valid_plugin_output()
    out["selected"]["idea"] = ""
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(out, IMPROVE_OUTPUT_SCHEMA)


@pytest.mark.parametrize("field,value", [
    ("kind", "bogus"), ("self_test", "bogus")])
def test_artifact_plugin_enum_fields_reject_out_of_enum(field, value):
    out = _valid_plugin_output()
    out["artifact"][field] = value
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(out, IMPROVE_OUTPUT_SCHEMA)


def test_discoveries_item_source_enum_rejects_out_of_enum():
    out = _valid_plugin_output()
    out["discoveries"][0]["source"] = "bogus"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(out, IMPROVE_OUTPUT_SCHEMA)


def test_schema_is_chatgpt_structured_outputs_compatible():
    """実機 E2E 実測 (2026-08-30、runbook): ChatGPT structured outputs は
    (a) `oneOf` を不許可 (`'oneOf' is not permitted`)、(b) 全 property に
    "type" キーを要求する。artifact の 3 variant は const 判別で排他なので
    anyOf は oneOf と同値。退行すると codex+chatgpt E2E が 400 で構造的に
    落ちる。"""
    def walk(node):
        if isinstance(node, dict):
            assert "oneOf" not in node, "oneOf は ChatGPT で不許可"
            if "const" in node or "properties" in node or "enum" in node \
                    or "anyOf" in node:
                if "anyOf" not in node and "properties" not in node:
                    assert "type" in node, f"type キー欠落: {node}"
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(IMPROVE_OUTPUT_SCHEMA)
