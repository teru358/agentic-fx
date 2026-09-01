"""commit() 手順0-1: 台帳凍結 + artifact.type 別出力検査
(設計書 §4.2-0/1、プラン §8.1-42)。"""
from __future__ import annotations

import pytest


def test_step0_freezes_ledger_before_any_output_inspection(loop_and_ctx):
    loop, ctx, conn = loop_and_ctx
    assert ctx.ledger._state == "OPEN"
    loop._freeze_ledger(ctx)
    assert ctx.ledger._state == "FROZEN"


def test_plugin_artifact_requires_name_and_staging_checks(loop_and_ctx):
    loop, ctx, conn = loop_and_ctx
    output = {"artifact": {"type": "plugin", "name": "../evil", "kind": "indicator",
                           "self_test": "passed", "summary": "x"},
              "selected": {"backlog_id": None, "idea": "x"},
              "discoveries": [], "selection_rationale": "x"}
    verdict = loop._inspect_output(output, ctx, conn=conn)
    assert verdict.ok is False
    assert "name" in verdict.reason


def test_discovery_missing_kind_is_output_invalid(loop_and_ctx):
    loop, ctx, conn = loop_and_ctx
    output = {
        "artifact": {"type": "observation", "reason": "x"},
        "selected": {"backlog_id": None, "idea": "x"},
        "discoveries": [{"idea": "fact", "source": "agent", "evidence": "e"}],
        "selection_rationale": "x",
    }

    verdict = loop._inspect_output(output, ctx, conn=conn)

    assert verdict.ok is False
    assert "discoveries" in verdict.reason or "kind" in verdict.reason


def test_plugin_artifact_name_with_trailing_newline_is_rejected(loop_and_ctx):
    """round2 D3 是正 (検収 acceptance-round2.md D3): `_inspect_output`
    (improve_loop.py:689) の `artifact.name` 検査が `.fullmatch()` に
    揃っていることの pin。既存の `test_plugin_artifact_requires_name_
    and_staging_checks` は `"../evil"` (パス走査) しか通っておらず、
    `.fullmatch`→`.match` に戻す変異 (末尾改行1個を受理してしまう) を
    実測で殺せなかった (台帳の「同型なので機構的に同じ効果」は誤り —
    acceptance-round2.md D3 参照)。"""
    loop, ctx, conn = loop_and_ctx
    output = {"artifact": {"type": "plugin", "name": "sma\n", "kind": "indicator",
                           "self_test": "passed", "summary": "x"},
              "selected": {"backlog_id": None, "idea": "x"},
              "discoveries": [], "selection_rationale": "x"}
    verdict = loop._inspect_output(output, ctx, conn=conn)
    assert verdict.ok is False
    assert "canonical form" in verdict.reason


def test_noncanonical_artifact_name_is_capped_before_activity_echo(loop_and_ctx, tmp_path):
    """agent 由来 name は output-invalid activity を肥大化させない。"""
    loop, ctx, conn = loop_and_ctx
    huge_name = "x" * 200_001
    output = {"artifact": {"type": "plugin", "name": huge_name},
              "selected": {"backlog_id": None, "idea": "x"},
              "discoveries": [], "selection_rationale": "x"}
    verdict = loop._inspect_output(output, ctx, conn=conn)
    assert verdict.ok is False
    loop._activity.write(
        __import__("agentic_fx.activity", fromlist=["Category"]).Category.IMPROVE,
        "output_invalid", f"mission={ctx.mission_id} reason={verdict.reason}")

    line = next(line for line in (tmp_path / "activity.log").read_text().splitlines()
                if "output_invalid" in line)
    assert len(line) < 500
    assert "is not in canonical form" in line


def test_report_artifact_skips_plugin_specific_checks(loop_and_ctx):
    """report / observation artifact には name/path 検査を掛けない
    (設計書 §4.2-1「report / observation の artifact にはこれらの検査を
    掛けない」)。"""
    loop, ctx, conn = loop_and_ctx
    output = {"artifact": {"type": "report", "proposal_kind": "core",
                           "title": "t", "body_md": "../not/a/plugin/path"},
              "selected": {"backlog_id": None, "idea": "x"},
              "discoveries": [], "selection_rationale": "x"}
    verdict = loop._inspect_output(output, ctx, conn=conn)
    assert verdict.ok is True


def test_observation_artifact_skips_plugin_specific_checks(loop_and_ctx):
    loop, ctx, conn = loop_and_ctx
    output = {"artifact": {"type": "observation", "reason": "no idea"},
              "selected": {"backlog_id": None, "idea": "x"},
              "discoveries": [], "selection_rationale": "x"}
    verdict = loop._inspect_output(output, ctx, conn=conn)
    assert verdict.ok is True


def test_risk_gate_report_is_flagged_unsupported_in_plan10(loop_and_ctx):
    loop, ctx, conn = loop_and_ctx
    output = {"artifact": {"type": "report", "proposal_kind": "risk_gate",
                           "title": "t", "body_md": "x"},
              "selected": {"backlog_id": None, "idea": "x"},
              "discoveries": [], "selection_rationale": "x"}
    verdict = loop._inspect_output(output, ctx, conn=conn)
    assert verdict.ok is True
    assert verdict.risk_gate_unsupported is True


def test_out_of_partition_selection_is_not_rejected_only_logged(
        loop_and_ctx, tmp_path):
    """ヒント集合外の selected.backlog_id は拒否しない — activity に
    out_of_partition を記録して続行する (CAS が正)。

    A36 裁定 (2026-08-28、束D検収 verified-local-round1.md §7):
    `_InspectionVerdict.out_of_partition` field は撤去した (`commit()`
    が一度も読まない dead field だったため)。実質の産物である activity
    ログ 1 行が唯一の観測点になったので、これを直接 assert する
    (L-B28 が指摘していた「唯一の産物が無 pin」も併せて解消)。"""
    loop, ctx, conn = loop_and_ctx  # ctx.allowed_backlog_ids = frozenset({1,2}) 前提の fixture
    output = {"artifact": {"type": "observation", "reason": "x"},
              "selected": {"backlog_id": 999, "idea": "x"},
              "discoveries": [], "selection_rationale": "x"}
    verdict = loop._inspect_output(output, ctx, conn=conn)
    assert verdict.ok is True
    activity_text = (tmp_path / "activity.log").read_text()
    assert f"mission={ctx.mission_id} backlog_id=999" in activity_text
    assert "out_of_partition" in activity_text


def test_selected_backlog_id_must_exist_in_db(loop_and_ctx):
    loop, ctx, conn = loop_and_ctx
    output = {"artifact": {"type": "observation", "reason": "x"},
              "selected": {"backlog_id": 424242, "idea": "x"},
              "discoveries": [], "selection_rationale": "x"}
    verdict = loop._inspect_output(output, ctx, conn=conn)
    assert verdict.ok is False
