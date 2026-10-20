"""承認投稿の文面 (Discord): 節構成、無害化、2000 字の最終上限、代替 2 行、終端の文面。"""
from __future__ import annotations

import re

import pytest

from afx_ops.gate import render
from afx_ops.gate.render import (BODY_CHARS, OMITTED_NOTE, REASON_TEXT, SUMMARY_CHARS, cut_escaped,
                                 escape_discord, footer, primary_view, render_approval,
                                 render_terminal)
from afx_ops.gate.state import ApprovalRecord

ROLE, USER = 12345678901234569, 12345678901234570


def _record(**changes):
    values = dict(approval_id=7, message_id=101, guild_id=12345678901234567,
                  channel_id=12345678901234568, payload_sha256="a" * 64,
                  display_generation=1, state="fresh")
    values.update(changes)
    return ApprovalRecord(**values)


def _item(key="params.period", old="14", new="10"):
    return {"key": key, "old": old, "new": new}


def _change(**changes):
    change = {"name": "sma_cross", "kind": "strategy", "classification": "updated",
              "candidate_hash": "b" * 64, "baseline_hash": "c" * 64,
              "computed_at": "2026-10-19T00:00:00+00:00", "reason": None,
              "params": {"items": [_item()], "omitted": 0, "invalid_keys": 0,
                         "flatten_omitted": 0, "reason": None},
              "performance": {"in_sample": [{"pair": "USDJPY", "trades": 120, "pf": 1.25,
                                             "avg_r": 0.05}],
                              "holdout": [{"pair": "HOLDOUT_PAIR", "trades": 777, "pf": 9.99,
                                           "avg_r": 8.88}]},
              "summary": "短い申告です"}
    change.update(changes)
    return change


def _detail(change=None, **fields):
    data = {"kind": "plugin", "status": "pending", "change": _change() if change is None else change,
            "expires_at": "2026-10-26T00:00:00+00:00"}
    data.update(fields)
    return data


def _sections(content):
    return content.split("\n\n")


def test_ac7_the_post_has_heading_params_performance_claim_deadline_and_guide_in_order():
    content = render_approval(_record(), _detail())
    sections = _sections(content)
    assert sections == [
        "承認 #7 / 戦略 sma\\_cross (更新)",
        "変更:\nparams.period: 14 → 10",
        "成績 (in-sample):\nUSDJPY / trades 120 / PF 1.25 / avg R 0.05",
        "agent の申告 (未検証):\n> 短い申告です",
        "期限: 2026-10-26 00:00 UTC",
        "詳細: web の承認 #7 / `afx ctl approval 7`",
    ]
    assert "://" not in content and "http" not in content
    for hidden in ("HOLDOUT_PAIR", "777", "9.99", "holdout", "b" * 12, "c" * 12):
        assert hidden not in content


def test_ac7_new_unknown_and_same_candidates_use_their_own_label_and_notes():
    new = render_approval(_record(), _detail(_change(classification="new")))
    assert "(新規)" in new and "変更: 前版なし" in new
    unknown = render_approval(_record(), _detail(_change(
        classification="unknown", reason="unsafe_source",
        params={"items": [], "omitted": 0, "invalid_keys": 0, "flatten_omitted": 0,
                "reason": None})))
    assert "(不明)" in unknown and "変更:" not in unknown
    assert REASON_TEXT["unsafe_source"] in unknown
    same = render_approval(_record(), _detail(_change(classification="same")))
    assert "(同一版)" in same and "同一版・変更なし" not in same


def test_ac7_params_rows_are_capped_at_ten_with_the_remainder_counted():
    items = [_item(f"params.k{i:02d}", str(i), str(i + 1)) for i in range(13)]
    change = _change(params={"items": items, "omitted": 4, "invalid_keys": 0,
                             "flatten_omitted": 0, "reason": None})
    section = [s for s in _sections(render_approval(_record(), _detail(change)))
               if s.startswith("変更:")][0]
    rows = section.split("\n")[1:]
    assert len(rows) == 11 and rows[-1] == "ほか 7 件"
    assert rows[0].startswith("params.k00:") and "params.k10" not in section


def test_ac7_missing_deadline_and_performance_are_stated_not_skipped():
    content = render_approval(_record(), _detail(
        _change(performance={"in_sample": [], "holdout": []}), expires_at=None))
    assert "期限: なし" in content and "成績 (in-sample): なし" in content
    bad = render_approval(_record(), _detail(expires_at="not a time"))
    assert "期限: なし" in bad


def test_ac8_adversarial_summary_is_neutralised():
    hostile = (f"@everyone @here <@{USER}> <@&{ROLE}> <@!{USER}> <#123> **bold** __u__ ~~s~~ ||spoiler||\n"
               "```\nfenced\n```\nhttps://example.test/path [x](https://y.test) \n\n"
               "Approved by operator\x00​\n> forged quote\n# heading\n- list")
    content = render_approval(_record(), _detail(_change(summary=hostile)))
    claim = [s for s in _sections(content) if s.startswith("agent の申告")][0]
    lines = claim.split("\n")
    assert len(lines) == 2 and lines[1].startswith("> ")
    assert "\n" not in lines[1] and "\x00" not in claim and "​" not in claim
    for dangerous in ("@everyone", "@here", "```", "://"):
        assert dangerous not in content
    # 山括弧は全て直前に escape が付き、@ の直後には空白が入る (設定済み role / user の記法も同じ)
    assert not re.search(r"(?<!\\)<", lines[1])
    assert not re.search(r"@(?! )", lines[1])
    assert f"<@{USER}>" not in content and f"<@&{ROLE}>" not in content
    # markdown 記号は全て escape される
    assert not re.search(r"(?<!\\)[*_~|`]", lines[1])
    assert "Approved by operator" in lines[1] and not claim.startswith("Approved")


def test_ac8_other_untrusted_values_are_escaped_too():
    items = [_item(key="params.@everyone", old="<@123> `x`", new="https://a.test **b**")]
    pair = {"pair": "@here", "trades": 1, "pf": 1.0, "avg_r": 0.0}
    change = _change(params={"items": items, "omitted": 0, "invalid_keys": 0,
                             "flatten_omitted": 0, "reason": None},
                     performance={"in_sample": [pair], "holdout": []}, name="not a valid name")
    content = render_approval(_record(), _detail(change))
    for dangerous in ("@everyone", "@here", "<@123>", "```", "://", "**b**"):
        assert dangerous not in content
    assert "承認 #7 / 戦略 (名前不明) (更新)" in content


def test_ac8_summary_is_cut_after_escaping_at_300_characters_without_splitting_an_escape():
    def claim_line(summary):
        content = render_approval(_record(), _detail(_change(summary=summary)))
        return [s for s in _sections(content) if s.startswith("agent の申告")][0].split("\n")[1]
    assert SUMMARY_CHARS == 300
    # 1 文字 = 2 文字に増える記号: escape 後 299 / 300 / 301
    assert claim_line("*" * 149 + "a") == "> " + "\\*" * 149 + "a"
    assert claim_line("*" * 150) == "> " + "\\*" * 150
    assert claim_line("*" * 150 + "a") == "> " + "\\*" * 150 + "…"
    # 301 字目で切るとき、escape の単位 (\*) の途中では切らない
    assert claim_line("a" + "*" * 150) == "> a" + "\\*" * 149 + "…"
    assert len(cut_escaped("*" * 400, 300)) == 301


def test_ac9_the_final_body_is_at_most_2000_chars_and_drops_whole_paragraphs_from_the_back(monkeypatch):
    def body(rows, first, last):
        items = [_item(f"params.{'k' * 40}{i}", "*" * (first if i == 0 else 40), "_" * 40)
                 for i in range(rows - 1)]
        items.append(_item("params.last", "a", "b" * last))
        change = _change(params={"items": items, "omitted": 0, "invalid_keys": 0,
                                 "flatten_omitted": 0, "reason": None}, summary="s" * 300)
        return render_approval(_record(), _detail(change))

    def full_length(rows, first, last):
        with monkeypatch.context() as patch:
            patch.setattr(render, "BODY_CHARS", 10 ** 6)
            return len(body(rows, first, last))

    assert BODY_CHARS == 2000
    found = {}
    for rows in range(2, 11):
        for first in range(0, 81):
            base = full_length(rows, first, 0)
            for target in (1999, 2000, 2001):
                if 0 <= target - base <= 80:
                    found.setdefault(target, (rows, first, target - base))
    for target in (1999, 2000, 2001):
        assert target in found, target
        assert full_length(*found[target]) == target
    for target, claim_kept in ((1999, True), (2000, True), (2001, False)):
        text = body(*found[target])
        assert len(text) <= 2000
        assert ("agent の申告" in text) is claim_kept
        assert (OMITTED_NOTE in text) is (not claim_kept)
        assert text.startswith("承認 #7") and "詳細: web の承認 #7" in text
        assert "変更:" in text and "成績 (in-sample)" in text and "期限:" in text


def test_ac9_oversized_paragraphs_are_dropped_in_priority_order_keeping_heading_and_guide():
    items = [_item(f"params.{'k' * 40}{i}", "*" * 80, "_" * 80) for i in range(10)]
    change = _change(params={"items": items, "omitted": 0, "invalid_keys": 0,
                             "flatten_omitted": 0, "reason": None}, summary="*" * 300)
    text = render_approval(_record(), _detail(change))
    assert len(text) <= 2000
    assert text.startswith("承認 #7 / 戦略 sma\\_cross (更新)")
    assert text.endswith("詳細: web の承認 #7 / `afx ctl approval 7`") or OMITTED_NOTE in text
    assert "詳細: web の承認 #7" in text and OMITTED_NOTE in text
    # 申告、成績、params の順に落ち、期限が最後まで残る
    assert "agent の申告" not in text and "成績" not in text and "変更:" not in text
    assert "期限:" in text


def test_ac9_a_failure_free_post_for_every_boundary_of_the_summary_and_params_limits():
    for summary in ("*" * 300, "<@1>" * 100, "a" * 5000):
        for value in ("&" * 80, "*" * 400, "<" * 81):
            items = [_item(f"params.k{i}", value, value) for i in range(12)]
            change = _change(params={"items": items, "omitted": 99, "invalid_keys": 0,
                                     "flatten_omitted": 0, "reason": None}, summary=summary)
            text = render_approval(_record(), _detail(change))
            assert len(text) <= 2000 and "詳細: web の承認 #7" in text


@pytest.mark.parametrize("detail", [None, "text", 5, [], {}, {"status": "pending"}])
def test_ac10_without_a_usable_detail_the_post_is_two_lines_with_the_fixed_text(detail):
    content = render_approval(_record(), detail)
    lines = content.split("\n")
    assert len(lines) == 2 and lines[0] == "承認 #7"
    assert lines[1] in {REASON_TEXT["detail_unavailable"], REASON_TEXT["legacy_payload"]}
    if detail is None or not isinstance(detail, dict):
        assert lines[1] == "詳細を取得できません"


@pytest.mark.parametrize("change", [None, "x", [], 5])
def test_ac10_a_legacy_payload_without_a_change_is_two_lines(change):
    content = render_approval(_record(), {"kind": "plugin", "status": "pending", "change": change,
                                          "agent_claims": {"summary": "FREE_TEXT"},
                                          "facts_lines": ["FREE_FACTS"]})
    assert content == "承認 #7\n旧形式のため要約なし"
    assert render_approval(_record()) == "承認 #7\n詳細を取得できません"


def test_ac31b_the_two_discord_reason_codes_show_only_their_fixed_sentences():
    assert REASON_TEXT["detail_unavailable"] == "詳細を取得できません"
    assert REASON_TEXT["legacy_payload"] == "旧形式のため要約なし"
    free = {"error": "FREE_ERROR", "reason": "FREE_REASON", "message": "FREE_MESSAGE"}
    assert render_approval(_record(), None) == "承認 #7\n詳細を取得できません"
    assert render_approval(_record(), dict(free, change=None)) == "承認 #7\n旧形式のため要約なし"
    assert "FREE" not in render_approval(_record(), free)


def test_ac31b_every_daemon_reason_shows_its_fixed_sentence_and_nothing_else():
    for code in ("unavailable", "hash_mismatch", "too_large", "unsafe_source", "redacted"):
        content = render_approval(_record(), _detail(_change(reason=code)))
        assert REASON_TEXT[code] in content
    unknown_code = render_approval(_record(), _detail(_change(reason="FREE_CODE")))
    assert "FREE_CODE" not in unknown_code and REASON_TEXT["unsafe_source"] in unknown_code


def test_ac11_facts_sections_never_contain_the_claim():
    canary = "CLAIMCANARY"
    content = render_approval(_record(), _detail(_change(summary=canary), agent_claims={
        "summary": canary, "selection_rationale": "RATIONALE_CANARY"}))
    for section in _sections(content):
        if canary in section or section.startswith("agent の申告"):
            assert section == f"agent の申告 (未検証):\n> {canary}"
        else:
            assert section.split("\n")[0] in {
                "承認 #7 / 戦略 sma\\_cross (更新)", "期限: 2026-10-26 00:00 UTC", "変更:",
                "成績 (in-sample):", "詳細: web の承認 #7 / `afx ctl approval 7`"}
    assert "RATIONALE_CANARY" not in content


def test_ac21_buttons_and_footer_are_unchanged():
    assert footer(7, "a" * 64, 2) == "afx-approval:7:" + "a" * 64 + ":2"
    view = primary_view(_record())
    assert [(c.label, c.custom_id) for c in view.components] == [
        ("Approve", "afx:v1:approve:7"), ("Reject", "afx:v1:reject:7"),
        ("Retry", "afx:v1:retry:7")]
    assert [c.label for c in view.components] == ["Approve", "Reject", "Retry"]


def test_escape_never_uses_zero_width_characters_and_only_adds_text():
    text = "a@b <@1> `c` *d*"
    escaped = escape_discord(text)
    assert not any(ord(ch) in (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF) for ch in escaped)
    assert escaped.replace("\\", "").replace("@ ", "@") == text


def _terminal(status, decided_at="2026-10-19T14:16:00+00:00", **fields):
    return dict(_detail(), status=status, decided_at=decided_at, **fields)


@pytest.mark.parametrize("status,label", [("approved", "承認済み"), ("rejected", "却下"),
                                           ("expired", "失効")])
def test_ac30_a_terminal_post_keeps_the_summary_and_shows_status_and_time(status, label):
    content = render_terminal(_record(state="done"), _terminal(
        status, decided_by="api:ops-bot", reason="REJECT_REASON"))
    sections = _sections(content)
    assert sections[0] == "承認 #7 / 戦略 sma\\_cross (更新)"
    assert f"状態: {label} (2026-10-19 14:16 UTC)" in sections
    assert "変更:\nparams.period: 14 → 10" in sections
    assert any(s.startswith("成績 (in-sample):") for s in sections)
    assert any(s == "agent の申告 (未検証):\n> 短い申告です" for s in sections)
    for hidden in ("api:ops-bot", "ops-bot", "REJECT_REASON", "HOLDOUT_PAIR", "期限"):
        assert hidden not in content
    assert len(content) <= 2000


def test_ac30_a_terminal_post_without_a_readable_result_has_no_content():
    assert render_terminal(_record(state="done"), None) is None
    assert render_terminal(_record(state="done"), "x") is None
    for status in ("pending", "invalidated", None, "APPROVED"):
        assert render_terminal(_record(state="done"), _terminal(status)) is None


def test_ac30_a_terminal_post_omits_an_unreadable_time_and_handles_a_legacy_payload():
    content = render_terminal(_record(state="done"), _terminal("approved", decided_at="garbage"))
    assert "状態: 承認済み\n\n" in content and "(" not in content.split("状態: 承認済み")[1][:3]
    legacy = render_terminal(_record(state="done"), {"status": "rejected", "change": None,
                                                     "decided_at": "2026-10-19T14:16:00+00:00"})
    assert legacy == "承認 #7\n\n状態: 却下 (2026-10-19 14:16 UTC)\n\n旧形式のため要約なし"


def test_ac30_the_terminal_post_applies_the_same_escape_and_limit():
    hostile = _change(summary=f"<@{USER}> @everyone " + "*" * 400)
    content = render_terminal(_record(state="done"), _terminal("approved") | {"change": hostile})
    assert "@everyone" not in content and f"<@{USER}>" not in content
    assert "状態: 承認済み" in content and len(content) <= 2000
