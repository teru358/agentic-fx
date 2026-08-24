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


def test_out_of_partition_selection_is_not_rejected_only_logged(loop_and_ctx):
    """ヒント集合外の selected.backlog_id は拒否しない — activity に
    out_of_partition を記録して続行する (CAS が正)。"""
    loop, ctx, conn = loop_and_ctx  # ctx.allowed_backlog_ids = frozenset({1,2}) 前提の fixture
    output = {"artifact": {"type": "observation", "reason": "x"},
              "selected": {"backlog_id": 999, "idea": "x"},
              "discoveries": [], "selection_rationale": "x"}
    verdict = loop._inspect_output(output, ctx, conn=conn)
    assert verdict.ok is True
    assert verdict.out_of_partition is True


def test_selected_backlog_id_must_exist_in_db(loop_and_ctx):
    loop, ctx, conn = loop_and_ctx
    output = {"artifact": {"type": "observation", "reason": "x"},
              "selected": {"backlog_id": 424242, "idea": "x"},
              "discoveries": [], "selection_rationale": "x"}
    verdict = loop._inspect_output(output, ctx, conn=conn)
    assert verdict.ok is False
