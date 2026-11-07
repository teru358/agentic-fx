"""承認カード (Discord) の「土台」の警告行と、無効になった依頼の終端カード。"""
from __future__ import annotations

import pytest

from afx_ops import base_text
from afx_ops.gate import render
from afx_ops.gate.render import BODY_CHARS, render_approval, render_terminal
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
MISMATCH = {"state": "mismatch", "base": "a" * 12, "live": "b" * 12}
NONE_BUT_LIVE = {"state": "none_but_live", "base": "none", "live": "c" * 12}
UNREADABLE = {"state": "unreadable", "base": "d" * 12, "live": "unreadable"}


def _detail(base_check=None, status="pending", **fields):
    data = {"kind": "plugin", "status": status, "change": dict(CHANGE),
            "expires_at": "2026-10-26T00:00:00+00:00", "base_check": base_check}
    data.update(fields)
    return data


def _sections(content):
    return content.split("\n\n")


def _plain(text):
    return text.replace("\\", "")


# 受入 14 (Discord): 警告が出る 3 状態

@pytest.mark.parametrize("view,expected", [
    (MISMATCH, "警告: この候補の土台は配備中の版と違います (土台 " + "a" * 12 + " / 配備中 "
               + "b" * 12 + ")。このままでは配備できません"),
    ({**MISMATCH, "live": "none"}, "配備中 無し)"),
    ({**MISMATCH, "live": "plain"}, "配備中 plain)"),
    ({**MISMATCH, "base": "unknown"}, "土台 不明 /"),
    (NONE_BUT_LIVE, "警告: この候補は同名の配備中の版を見ずに作られました。"
                    "このままでは配備できません"),
    (UNREADABLE, "警告: 配備中の版を確かめられません"),
])
def test_ac14_the_card_shows_the_base_warning(view, expected):
    content = _plain(render_approval(RECORD, _detail(view)))
    assert expected in content


def test_ac14_the_warning_is_its_own_paragraph_before_the_change():
    sections = _sections(_plain(render_approval(RECORD, _detail(NONE_BUT_LIVE))))
    assert sections[0].startswith("承認 #7")
    assert sections[1].startswith("警告: この候補は同名")
    assert sections[2].startswith("変更:")


# 受入 14: 一致・旧形式・bless・無い・壊れている

@pytest.mark.parametrize("view", [
    {"state": "match", "base": "a" * 12, "live": "a" * 12},
    {"state": "legacy", "base": None, "live": None},
    {"state": "unchecked", "base": None, "live": None},
    None, "mismatch", 5, [], {}, {"state": 5}, {"state": ["mismatch"]}, {"state": "nonsense"},
])
def test_ac14_the_card_shows_no_warning_otherwise(view):
    content = render_approval(RECORD, _detail(view))
    assert "警告" not in content
    assert content == render_approval(RECORD, {k: v for k, v in _detail(view).items()
                                              if k != "base_check"})


@pytest.mark.parametrize("view", [
    {"state": "mismatch", "base": {"x": 1}, "live": ["y"]},
    {"state": "mismatch"},
    {"state": "mismatch", "base": "<@123>\n@everyone", "live": "x" * 500},
])
def test_ac14_a_malformed_value_in_the_view_never_raises_and_is_never_echoed(view):
    content = render_approval(RECORD, _detail(view))
    assert "警告: この候補の土台は配備中の版と違います (土台 不明 / 配備中 不明)" in _plain(content)
    assert "everyone" not in content and "<@" not in content and "x" * 20 not in content


def test_ac14_a_terminal_card_never_shows_the_warning():
    content = render_terminal(RECORD, _detail(MISMATCH, status="approved",
                                              decided_at="2026-10-19T14:16:00+00:00"))
    assert "警告" not in content


# 受入 14: 文字数で省くとき、警告は比較成績より先に残る

def test_ac14_the_warning_outlives_the_performance_when_the_card_is_too_long(monkeypatch):
    change = dict(CHANGE, summary="申" * 300)
    change["params"] = {"items": [{"key": f"params.k{i}", "old": "o" * 30, "new": "n" * 30}
                                  for i in range(10)], "omitted": 0, "invalid_keys": 0,
                        "flatten_omitted": 0, "reason": None}
    full = render_approval(RECORD, _detail(None, change=change))
    assert "成績 (in-sample)" in full and "agent の申告" in full
    # 申告を省けばちょうど収まる上限。警告を足すと成績も省かないと収まらない
    budget = len(render_approval(RECORD, _detail(None, change=dict(change, summary=None)))) + 1
    monkeypatch.setattr(render, "BODY_CHARS", budget)
    shown = render_approval(RECORD, _detail(UNREADABLE, change=change))
    assert len(shown) <= budget
    plain = _plain(shown)
    assert "警告: 配備中の版を確かめられません" in plain
    assert "成績 (in-sample)" not in plain and "agent の申告" not in plain
    assert "変更:" in plain


def test_ac14_the_warning_survives_an_overlong_card_at_the_real_limit():
    change = dict(CHANGE, summary="申" * 300)
    change["params"] = {"items": [{"key": f"params.k{i}", "old": "o" * 40, "new": "n" * 40}
                                  for i in range(10)], "omitted": 0, "invalid_keys": 0,
                        "flatten_omitted": 0, "reason": None}
    change["performance"] = {"in_sample": [{"pair": f"P{i}", "trades": 9, "pf": 1.1, "avg_r": 0.1}
                                           for i in range(8)], "holdout": []}
    content = render_approval(RECORD, _detail(MISMATCH, change=change))
    assert len(content) <= BODY_CHARS
    assert "警告: この候補の土台は配備中の版と違います" in _plain(content)


# 受入 13 (Discord): invalidated の終端カード

def _terminal(reason, status="invalidated"):
    return _detail(None, status=status, decided_at="2026-10-19T14:16:00+00:00", reason=reason)


def test_ac13_status_labels_has_invalidated():
    assert render.STATUS_LABELS["invalidated"] == "無効"


def test_ac13_base_changed_adds_the_fixed_paragraph():
    content = render_terminal(RECORD, _terminal("base_changed"))
    sections = _sections(content)
    assert "状態: 無効 (2026-10-19 14:16 +00:00)" in sections
    assert base_text.BASE_CHANGED_TEXT in sections
    assert sections[0] == "承認 #7 / 戦略 sma\\_cross (更新)"
    assert any(s.startswith("変更:") for s in sections)
    assert content.count(base_text.BASE_CHANGED_TEXT) == 1


@pytest.mark.parametrize("reason", [None, "", "reevaluation_stale", "base_changed2", 5,
                                    "REJECT_REASON <@1>"])
def test_ac13_an_invalidated_card_without_that_reason_has_no_fixed_paragraph(reason):
    content = render_terminal(RECORD, _terminal(reason))
    assert "状態: 無効 (2026-10-19 14:16 +00:00)" in content
    assert base_text.BASE_CHANGED_TEXT not in content
    assert "REJECT_REASON" not in content and "reevaluation_stale" not in content


def test_ac13_the_superseded_invalidated_card_is_a_plain_invalid_card():
    content = render_terminal(RECORD, _terminal(None))
    sections = _sections(content)
    assert len(sections) == 6
    assert sections[:3] == ["承認 #7 / 戦略 sma\\_cross (更新)",
                            "状態: 無効 (2026-10-19 14:16 +00:00)",
                            "変更:\nparams.period: 14 → 10"]
    assert sections[3].startswith("成績 (in-sample):")
    assert sections[4:] == ["agent の申告 (未検証):\n> 短い申告です", render._guide(7)]


def test_ac13_a_pending_card_ignores_a_stale_base_changed_reason():
    content = render_approval(RECORD, _detail(None, reason="base_changed"))
    assert base_text.BASE_CHANGED_TEXT not in content


def test_ac13_a_legacy_invalidated_card_keeps_the_fixed_sentence_path():
    detail = {"status": "invalidated", "change": None, "reason": "base_changed",
              "decided_at": "2026-10-19T14:16:00+00:00"}
    content = render_terminal(RECORD, detail)
    assert content.startswith("承認 #7\n\n状態: 無効")
    assert "旧形式のため要約なし" in content
