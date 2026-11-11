"""履歴証跡で止まった依頼の固定 reason が、承認カードと承認画面に理由と次の操作で出る。"""
from __future__ import annotations

import pytest

from afx_ops import history_text
from afx_ops.gate.render import render_approval, render_terminal
from afx_ops.web import render as web_render
from afx_ops.gate.state import ApprovalRecord

RECORD = ApprovalRecord(approval_id=7, message_id=101, guild_id=12345678901234567,
                        channel_id=12345678901234568, payload_sha256="a" * 64,
                        display_generation=1, state="fresh")
CHANGE = {"name": "sma_cross", "kind": "strategy", "classification": "updated",
          "candidate_hash": "b" * 64, "baseline_hash": "c" * 64,
          "computed_at": "2026-10-19T00:00:00+00:00", "reason": None,
          "params": {"items": [{"key": "params.period", "old": "14", "new": "10"}],
                     "omitted": 0, "invalid_keys": 0, "flatten_omitted": 0, "reason": None},
          "performance": {"in_sample": [{"pair": "USDJPY", "trades": 120, "pf": 1.25,
                                         "avg_r": 0.05}], "holdout": []},
          "summary": "短い申告です"}


def _detail(base_check=None, status="pending", **fields):
    data = {"kind": "plugin", "status": status, "change": dict(CHANGE),
            "expires_at": "2026-10-26T00:00:00+00:00", "base_check": base_check}
    data.update(fields)
    return data


def _plain(text):
    return text.replace("\\", "")

REASONS = (history_text.EVIDENCE_INVALIDATED, history_text.EVIDENCE_REFRESH_REQUIRED,
           history_text.CHECK_UNAVAILABLE)


@pytest.mark.parametrize("reason", REASONS)
def test_a_pending_card_shows_the_reason_and_the_next_step(reason):
    content = _plain(render_approval(RECORD, _detail(None, reason=reason)))
    assert _plain(history_text.TEXT[reason]) in content


@pytest.mark.parametrize("reason", [None, "", "free text", 5, ["history_check_unavailable"]])
def test_an_unknown_or_malformed_reason_adds_nothing(reason):
    without = render_approval(RECORD, _detail(None))
    assert render_approval(RECORD, _detail(None, reason=reason)) == without


def test_an_invalidated_terminal_card_shows_the_invalidation_text():
    content = _plain(render_terminal(
        RECORD, _detail(None, status="invalidated", reason=history_text.EVIDENCE_INVALIDATED,
                        decided_at="2026-11-11T00:00:00+00:00")))
    assert _plain(history_text.TEXT[history_text.EVIDENCE_INVALIDATED]) in content
    assert "plugins/_human/<name>" in content


def test_only_an_invalidated_terminal_card_carries_the_invalidation_text():
    content = render_terminal(
        RECORD, _detail(None, status="rejected", reason=history_text.EVIDENCE_INVALIDATED,
                        decided_at="2026-11-11T00:00:00+00:00"))
    assert history_text.TEXT[history_text.EVIDENCE_INVALIDATED][:10] not in _plain(content)


@pytest.mark.parametrize("reason", REASONS)
def test_the_web_detail_has_a_history_section_with_the_fixed_text(reason):
    html = web_render._history_section({"reason": reason, "status": "pending"})
    assert "履歴証跡" in html and history_text.TEXT[reason].split("。")[0] in html


@pytest.mark.parametrize("reason", [None, "legacy_plain_present", "x", 3])
def test_the_web_detail_has_no_section_for_other_reasons(reason):
    assert web_render._history_section({"reason": reason}) == ""
    assert web_render._history_section({}) == ""


def test_the_section_is_part_of_the_detail_extras():
    sections = web_render._extra_sections({"reason": history_text.CHECK_UNAVAILABLE})
    assert any("履歴証跡" in section for section in sections)
