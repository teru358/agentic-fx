"""承認 #N の変更内容 (概要・params の差分・成績・申告・変更 diff) と byte 上限。"""
from __future__ import annotations

import html
import re
from pathlib import Path

import pytest

from afx_ops.api_client import Reply
from afx_ops.web import render
from afx_ops.web.render import (DIFF_BUDGET_BYTES, FILE_QUOTA_BYTES, OTHER_BUDGET_BYTES,
                                PAGE_LIMIT_BYTES, VALUE_CHARS, plan_diff)
from afx_ops.web.request import Request, handle_request

DETAIL = {"id": 7, "kind": "plugin", "status": "pending", "payload_sha256": "cd" * 32,
          "in_sample": {"USDJPY": {"trades": 40}}, "holdout": {"USDJPY": {"trades": 20}}}
H_CAND, H_BASE = "a1" * 32, "b2" * 32


def make_change(**over):
    change = {"name": "sma_cross", "kind": "strategy", "classification": "updated",
              "candidate_hash": H_CAND, "baseline_hash": H_BASE,
              "computed_at": "2026-10-19T01:02:03+00:00", "reason": None,
              "params": {"items": [{"key": "params.period", "old": "14", "new": "10"}],
                         "omitted": 0, "invalid_keys": 0, "flatten_omitted": 0, "reason": None},
              "performance": {"in_sample": [{"pair": "USDJPY", "trades": 40, "pf": 1.31,
                                              "avg_r": 0.12}],
                              "holdout": [{"pair": "USDJPY", "trades": 20, "pf": 1.18,
                                           "avg_r": 0.08}]},
              "summary": "要約"}
    change.update(over)
    return change


def file_entry(path, lines, status="changed", omitted=0, header="@@ -1 +1 @@"):
    return {"path": path, "status": status, "reason": None, "omitted_lines": omitted,
            "hunks": [{"header": header, "lines": lines}] if lines is not None else []}


def make_change_data(files=None, change=None, claims=None):
    files = files if files is not None else [file_entry("plugin.py", ["+x = 1"]),
                                             file_entry("config.yaml", [" a"], "unchanged"),
                                             file_entry("test_plugin.py", ["-y"], "changed")]
    return {"id": 7, "change": change or make_change(),
            "claims": claims or {"summary": "S", "selection_rationale": "R"},
            "diff": {"files": files, "omitted_lines": 0}}


class Client:
    def __init__(self, detail=None, change_data=None, fail_change=False):
        self.detail = detail if detail is not None else dict(DETAIL, change=make_change())
        self.change_data = change_data
        self.fail_change = fail_change
        self.calls = []

    def approval(self, approval_id):
        self.calls.append(("approval", approval_id))
        return Reply(200, self.detail, None)

    def approval_change(self, approval_id):
        self.calls.append(("change", approval_id))
        if self.fail_change:
            raise OSError("down")
        return Reply(200, self.change_data, None) if self.change_data is not None \
            else Reply(404, None, "not_found")


def page_of(client, path="/approvals/7"):
    response = handle_request(Request("GET", path, {}), client)
    assert response.status == 200
    return response.body


def compose(change_data, detail=None):
    detail = detail if detail is not None else dict(DETAIL)
    return render.compose_approval_page(detail, change_data["change"], change_data)


def sections(body: bytes) -> list[str]:
    return re.findall(r"<h2>(.*?)</h2>", body.decode())


# ---------------------------------------------------------------- AC-12: 5 節

def test_ac12_page_has_the_five_sections_holdout_and_web_only_rationale():
    body = page_of(Client(change_data=make_change_data()))
    assert sections(body) == ["概要", "params の差分", "成績", "申告 (agent の申告・未検証)",
                              "変更 diff"]
    text = body.decode()
    assert "holdout USDJPY" in text and "trades=20 PF=1.18 avg_r=0.08" in text
    assert "<h3>selection_rationale</h3><pre>R</pre>" in text
    assert "<h3>summary</h3><pre>S</pre>" in text
    assert "<dt>params.period</dt><dd>14 → 10</dd>" in text
    assert "<h3>plugin.py</h3><pre>@@ -1 +1 @@\n+x = 1\n</pre>" in text
    assert "<h3>config.yaml</h3><p>差分なし</p>" in text


def test_ac12_page_calls_the_detail_then_the_change_endpoint_once_each():
    client = Client(change_data=make_change_data())
    page_of(client)
    assert client.calls == [("approval", 7), ("change", 7)]


def test_ac12_change_failures_still_render_the_page_with_a_fixed_text():
    for client in (Client(), Client(fail_change=True)):
        text = page_of(client).decode()
        assert "概要" in text and "変更 diff" in text
        assert render.REASON_TEXT["unavailable"] in text


def test_ac12_page_without_any_change_keeps_the_legacy_detail_and_fixed_text():
    class Old:
        def approval(self, approval_id):
            return Reply(200, dict(DETAIL), None)
    text = page_of(Old()).decode()
    assert "payload_sha256" in text and render.REASON_TEXT["unavailable"] in text


# ---------------------------------------------------------------- AC-12 (a)-(e): 48 KiB の配分

def lines_of(total_bytes: int, header="@@ -1 +1 @@"):
    """escape 後の表示 bytes (各行 + 改行) がちょうど total_bytes になる hunk の行。"""
    per = 200
    out, used = [], len(header) + 1
    while total_bytes - used > per + 2:
        out.append("+" + "a" * per)
        used += per + 2
    rest = total_bytes - used
    assert rest >= 2, rest
    out.append("+" + "a" * (rest - 2))
    return out


def entry_with_need(path, need, status="changed"):
    """FilePlan.need が need になる file 入力。"""
    probe = render.FilePlan(file_entry(path, ["+x"]))
    fixed = len(probe.head.encode()) + len(probe.tail.encode())
    entry = file_entry(path, lines_of(need - fixed), status)
    assert render.FilePlan(entry).need == need
    return entry


def plans(*entries):
    return plan_diff({"files": list(entries), "omitted_lines": 0})


KIB = 1024


def test_ac12_a_all_files_within_the_quota_are_shown_in_full():
    result = plans(entry_with_need("plugin.py", 8 * KIB), entry_with_need("config.yaml", 4 * KIB),
                   entry_with_need("test_plugin.py", 100))
    assert [p.alloc for p in result] == [8 * KIB, 4 * KIB, 100]
    assert all(p.omitted == 0 and p.shown == len(p.lines) for p in result)


def test_ac12_b_one_large_file_takes_the_unused_quota():
    result = plans(entry_with_need("plugin.py", 30 * KIB), entry_with_need("config.yaml", 1 * KIB),
                   entry_with_need("test_plugin.py", 1 * KIB))
    assert result[0].alloc == 30 * KIB and result[0].omitted == 0
    assert result[1].alloc == 1 * KIB and result[2].alloc == 1 * KIB


def test_ac12_c_several_large_files_are_served_in_file_order():
    result = plans(entry_with_need("plugin.py", 40 * KIB), entry_with_need("config.yaml", 40 * KIB),
                   entry_with_need("test_plugin.py", 10 * KIB))
    # 各 8 KiB の quota の後の残余 24 KiB は plugin.py に回り、続く二つは quota のまま
    assert [p.alloc for p in result] == [8 * KIB + 24 * KIB, 8 * KIB, 8 * KIB]
    assert [p.omitted > 0 for p in result] == [True, True, True]
    assert sum(len(p.html.encode()) for p in result) <= DIFF_BUDGET_BYTES


def _config_test_heads():
    heads = 0
    for path in ("config.yaml", "test_plugin.py"):
        heads += render.FilePlan(file_entry(path, [" a"], "unchanged")).need
    return heads


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_ac12_d_total_budget_boundary_omits_only_past_48_kib(delta):
    heads = _config_test_heads()
    need = DIFF_BUDGET_BYTES - heads + delta
    result = plans(entry_with_need("plugin.py", need),
                   file_entry("config.yaml", [" a"], "unchanged"),
                   file_entry("test_plugin.py", [" a"], "unchanged"))
    total = sum(len(p.html.encode()) for p in result)
    assert total <= DIFF_BUDGET_BYTES
    if delta <= 0:
        assert result[0].omitted == 0 and "行省略" not in result[0].html
    else:
        assert result[0].omitted >= 1 and "行省略" in result[0].html


def test_ac12_e_a_single_line_larger_than_the_allocation_is_replaced_by_the_mark():
    huge = file_entry("plugin.py", ["+" + "a" * (60 * KIB)])
    result = plans(huge)
    only = result[0]
    # 割当に入る行は hunk の header だけで、本文は 0 行 + 省略印
    assert only.shown == 1 and only.omitted == 1
    assert "@@ -1 +1 @@\n…(1 行省略)\n" in only.html and "aaaa" not in only.html
    assert len(only.html.encode()) <= only.alloc


def test_ac12_omission_mark_is_reserved_inside_the_allocation_for_every_file():
    result = plans(entry_with_need("plugin.py", 60 * KIB), entry_with_need("config.yaml", 20 * KIB),
                   entry_with_need("test_plugin.py", 20 * KIB))
    for plan in result:
        assert len(plan.html.encode()) <= plan.alloc
        assert plan.omitted > 0 and "行省略)" in plan.html


def test_ac12_api_side_omission_is_added_to_the_mark():
    result = plans(file_entry("plugin.py", ["+x"], omitted=41))
    assert result[0].omitted == 41 and "…(41 行省略)" in result[0].html
    assert result[0].need == len(result[0].head.encode()) + len(result[0].tail.encode()) \
        + len("@@ -1 +1 @@\n") + len("+x\n") + len("…(41 行省略)\n".encode())


def test_ac12_markup_heavy_input_keeps_the_complete_page_under_64_kib():
    bomb = "&<>\"'" * 400
    files = [file_entry(p, ["+" + bomb] * 80) for p in ("plugin.py", "config.yaml", "test_plugin.py")]
    items = [{"key": f"params.k{i}", "old": bomb, "new": bomb + "x"} for i in range(64)]
    change = make_change(params={"items": items, "omitted": 3, "invalid_keys": 2,
                                 "flatten_omitted": 1, "reason": None})
    data = make_change_data(files, change, {"summary": bomb * 5, "selection_rationale": bomb * 5})
    built = compose(data)
    assert len(built.page) < PAGE_LIMIT_BYTES
    assert b"Output clipped" not in built.page
    assert built.diff_bytes <= DIFF_BUDGET_BYTES
    assert built.other_bytes <= OTHER_BUDGET_BYTES
    assert b"holdout" in built.page
    for text in re.findall(rb"<pre>(.*?)</pre>", built.page, re.S):
        assert b"<" not in text.replace(b"&lt;", b"")


def test_ac12_other_sections_shrink_params_first_then_claims():
    items = [{"key": f"params.k{i:02d}", "old": "o" * 80, "new": "n" * 80} for i in range(64)]
    change = make_change(params={"items": items, "omitted": 0, "invalid_keys": 0,
                                 "flatten_omitted": 0, "reason": None})
    data = make_change_data(change=change, claims={"summary": "s" * 2000,
                                                    "selection_rationale": "r" * 2000})
    built = compose(data)
    text = built.page.decode()
    assert built.other_bytes <= OTHER_BUDGET_BYTES
    assert "件省略)" in text  # params が先に削られる
    # 申告は params を削って収まる限り、切らずに残る
    assert "s" * 2000 in text and "r" * 2000 in text
    assert 0 < text.count("<dt>params.k") < 64


def test_ac12_page_is_under_64_kib_even_with_maximal_everything():
    entries = [entry_with_need(p, 200 * KIB) for p in ("plugin.py", "config.yaml", "test_plugin.py")]
    built = compose(make_change_data(entries))
    assert len(built.page) < PAGE_LIMIT_BYTES and built.diff_bytes <= DIFF_BUDGET_BYTES


# ---------------------------------------------------------------- AC-5 / 23a (web)

def test_ac5_web_values_are_cut_at_80_escaped_characters_without_splitting_entities():
    def shown(value):
        items = [{"key": "k", "old": "a", "new": value}]
        change = make_change(params={"items": items, "omitted": 0, "invalid_keys": 0,
                                     "flatten_omitted": 0, "reason": None})
        built = compose(make_change_data(change=change))
        return re.search(r"<dt>k</dt><dd>a → (.*?)</dd>", built.page.decode()).group(1)

    for length, cut in ((VALUE_CHARS - 1, False), (VALUE_CHARS, False), (VALUE_CHARS + 1, True)):
        value = "v" * length
        assert (shown(value) == "v" * VALUE_CHARS + "…") is cut
    # `&` は escape 後に 5 文字。16 個で 80 文字ちょうど、17 個目は入らず切る
    out = shown("&" * 40)
    assert out == "&amp;" * 16 + "…" and out.count("&amp;") == 16
    assert "&amp" not in out.replace("&amp;", "")
    # 80 文字ちょうどに収まる escape 済みの値は切らない
    assert shown("&" * 16) == "&amp;" * 16


def test_ac5_web_cut_counts_the_escaped_form_of_the_next_character():
    items = [{"key": "k", "old": "a", "new": "v" * (VALUE_CHARS - 2) + "&"}]
    change = make_change(params={"items": items, "omitted": 0, "invalid_keys": 0,
                                 "flatten_omitted": 0, "reason": None})
    out = re.search(r"<dt>k</dt><dd>a → (.*?)</dd>",
                    compose(make_change_data(change=change)).page.decode()).group(1)
    assert out == "v" * (VALUE_CHARS - 2) + "…"


def test_ac23a_web_orders_removal_escape_and_cut_so_markup_cannot_exceed_the_limit():
    value = "<" * 19 + "z"  # escape 後は 19 * 4 + 1 = 77 文字
    items = [{"key": "k", "old": "a", "new": value}]
    change = make_change(params={"items": items, "omitted": 0, "invalid_keys": 0,
                                 "flatten_omitted": 0, "reason": None})
    out = re.search(r"<dt>k</dt><dd>a → (.*?)</dd>",
                    compose(make_change_data(change=change)).page.decode()).group(1)
    assert out == "&lt;" * 19 + "z" and len(out) <= VALUE_CHARS
    assert html.unescape(out) == value


def test_ac23a_web_diff_budget_counts_escaped_bytes_not_raw_characters():
    raw = "+" + "&" * 1000
    entry = file_entry("plugin.py", [raw] * 30)
    plan = plans(entry)[0]
    assert plan.need > 5 * 30 * 1000 and len(raw) * 30 < FILE_QUOTA_BYTES * 4
    assert plan.omitted == 0 or len(plan.html.encode()) <= plan.alloc


def test_web_strips_control_format_and_surrogate_characters_but_keeps_newlines():
    nasty = "a\x07b‮c\ud800d\x1b[31me"
    data = make_change_data(claims={"summary": nasty + "\nline2", "selection_rationale": "-"},
                            files=[file_entry("plugin.py", ["+" + nasty])])
    text = compose(data).page.decode()
    assert "\x07" not in text and "‮" not in text and "\x1b" not in text
    assert "abc" in text and "\nline2" in text


# ---------------------------------------------------------------- AC-25 / 26 / 31a / 13

def test_ac25_page_shows_twelve_hash_digits_and_the_computed_time():
    text = page_of(Client(change_data=make_change_data())).decode()
    assert f"<dt>候補</dt><dd>{H_CAND[:12]}</dd>" in text
    assert f"<dt>比較対象</dt><dd>{H_BASE[:12]}</dd>" in text
    assert H_CAND[:13] not in text
    assert "<dt>計算時刻</dt><dd>2026-10-19T01:02:03+00:00</dd>" in text


@pytest.mark.parametrize("cls,label", [("new", "新規"), ("updated", "更新"), ("same", "同一版"),
                                        ("unknown", "不明")])
def test_ac26_classification_has_exactly_one_label(cls, label):
    data = make_change_data(change=make_change(classification=cls))
    text = compose(data).page.decode()
    assert f"<dt>分類</dt><dd>{label}</dd>" in text
    assert "同一版・変更なし" not in text


@pytest.mark.parametrize("code", ["unavailable", "hash_mismatch", "too_large", "unsafe_source",
                                  "redacted"])
def test_ac31a_each_reason_code_prints_only_its_fixed_text(code):
    data = make_change_data(change=make_change(reason=code, classification="unknown"), files=[])
    text = compose(data).page.decode()
    assert render.REASON_TEXT[code] in text
    others = {t for c, t in render.REASON_TEXT.items() if t != render.REASON_TEXT[code]}
    assert not any(other in text for other in others)


def test_ac31a_unknown_reason_code_falls_back_to_a_fixed_text_without_echo():
    data = make_change_data(change=make_change(reason="free text /etc/passwd"), files=[])
    text = compose(data).page.decode()
    assert render.REASON_TEXT["unsafe_source"] in text and "passwd" not in text


def test_ac31a_redacted_file_is_hidden_alone_and_other_files_are_shown():
    files = [file_entry("plugin.py", None, "redacted"),
             file_entry("config.yaml", ["+k: v"]), file_entry("test_plugin.py", None, "unchanged")]
    text = compose(make_change_data(files)).page.decode()
    assert f"<h3>plugin.py</h3><p>{render.REASON_TEXT['redacted']}</p>" in text
    assert "<h3>config.yaml</h3><pre>@@ -1 +1 @@\n+k: v\n</pre>" in text


def test_ac31a_params_granularity_value_redaction_keeps_other_rows():
    items = [{"key": "params.a", "old": "[redacted]", "new": "[redacted]"},
             {"key": "params.b", "old": "1", "new": "2"}]
    change = make_change(params={"items": items, "omitted": 0, "invalid_keys": 1,
                                 "flatten_omitted": 0, "reason": None})
    text = compose(make_change_data(change=change)).page.decode()
    assert "<dt>params.a</dt><dd>[redacted] → [redacted]</dd>" in text
    assert "<dt>params.b</dt><dd>1 → 2</dd>" in text
    assert "形に合わない key 1 件" in text


def test_ac13_unknown_fields_never_reach_the_page():
    marker = "LEAK_MARKER_9f3"
    detail = dict(DETAIL, change=make_change(), agent_claims={"summary": marker}, reason=marker,
                  facts_lines=[marker], secret=marker)
    data = make_change_data()
    data["change"]["extra"] = marker
    data["extra"] = {"x": marker}
    data["claims"]["other"] = marker
    data["diff"]["files"][0]["debug"] = marker
    data["diff"]["files"][0]["hunks"][0]["extra"] = marker
    data["change"]["params"]["items"][0]["extra"] = marker
    data["change"]["performance"]["in_sample"][0]["extra"] = marker
    text = page_of(Client(detail, data)).decode()
    assert marker not in text


def test_ac13_routes_and_style_are_unchanged():
    from afx_ops.web.routes import ROUTE_TABLE, resolve_route
    assert [(r.path, r.client_method) for r in ROUTE_TABLE] == [
        ("/", "status"), ("/approvals", "approvals"), ("/approvals/{id}", "approval"),
        ("/logs", "log"), ("/activity", "activity")]
    assert resolve_route("/approvals/7/change") is None
    assert handle_request(Request("GET", "/approvals/7/change", {}), Client()).status == 404
    assert handle_request(Request("POST", "/approvals/7", {}), Client()).status == 405
    assert handle_request(Request("GET", "/approvals/7", {"x": "1"}), Client()).status == 400


def test_page_has_no_script_or_inline_style_with_hostile_values():
    hostile = "<script>alert(1)</script><img src=x onerror=y>"
    items = [{"key": "params.k", "old": hostile, "new": hostile}]
    change = make_change(name=hostile, params={"items": items, "omitted": 0, "invalid_keys": 0,
                                               "flatten_omitted": 0, "reason": None})
    data = make_change_data([file_entry("plugin.py", ["+" + hostile])], change,
                            {"summary": hostile, "selection_rationale": hostile})
    text = compose(data).page.decode()
    assert "<script" not in text.replace("<style>", "") and "<img" not in text
    assert " style=" not in text and " onerror" not in text.replace("onerror=y", "")


def test_the_displayed_constants_mirror_the_daemon_definitions():
    source = (Path(__file__).resolve().parents[3] / "src" / "agentic_fx" / "ops"
              / "change_limits.py").read_text(encoding="utf-8")

    def const(name):
        match = re.search(rf"^{name} = (.+)$", source, re.M)
        return eval(match.group(1), {"KIB": 1024})

    assert const("WEB_DIFF_BYTES") == DIFF_BUDGET_BYTES
    assert const("WEB_OTHER_BYTES") == OTHER_BUDGET_BYTES
    assert const("WEB_FILE_QUOTA_BYTES") == FILE_QUOTA_BYTES
    assert const("WEB_PAGE_BYTES") == PAGE_LIMIT_BYTES
    assert const("PARAM_VALUE_CHARS") == VALUE_CHARS
    assert const("CLAIM_WEB_CHARS") == render.CLAIM_CHARS
    assert const("PARAMS_DIFF_MAX") == render.PARAMS_MAX
    reasons = re.findall(r'^\s+(\w+): "(.+)",$', source.split("REASON_TEXT = {")[1], re.M)
    names = {"UNAVAILABLE": "unavailable", "HASH_MISMATCH": "hash_mismatch",
             "TOO_LARGE": "too_large", "UNSAFE_SOURCE": "unsafe_source",
             "REDACTED_CODE": "redacted"}
    for constant, text in reasons:
        if constant in names:
            assert render.REASON_TEXT[names[constant]] == text
    block = source.split("CLASS_LABELS = {")[1].split("}")[0]
    assert re.findall(r'"([^"]+)"', block) == [render.CLASS_LABELS[k] for k in
                                               ("new", "updated", "same", "unknown")]


# ---------------------------------------------------------------- 配分の一回化と型の防御

def test_page_plans_the_diff_exactly_once(monkeypatch):
    calls = []
    original = render.plan_diff

    def counting(diff):
        calls.append(1)
        return original(diff)

    monkeypatch.setattr(render, "plan_diff", counting)
    built = compose(make_change_data())
    assert len(calls) == 1
    assert len(built.plans) == 3


@pytest.mark.parametrize("field,value", [("classification", []), ("classification", {}),
                                         ("reason", {}), ("reason", []),
                                         ("params", {"items": "x"}), ("params", [])])
def test_malformed_change_fields_degrade_to_one_fixed_line(field, value):
    change = make_change(**{field: value})
    body = page_of(Client(change_data=make_change_data(change=change))).decode()
    assert render.INVALID_CHANGE_TEXT in body
    assert "変更 diff" not in body


def test_malformed_summary_in_detail_is_ignored_when_the_change_endpoint_is_valid():
    detail = dict(DETAIL, change={"classification": []})
    text = page_of(Client(detail=detail, change_data=make_change_data())).decode()
    assert render.INVALID_CHANGE_TEXT not in text and "変更 diff" in text


def test_a_degraded_diff_file_says_the_computation_was_skipped():
    entry = file_entry("plugin.py", ["-a", "+b"])
    entry["diff_degraded"] = "too_many_lines"
    text = render.compose_approval_page(DETAIL, make_change(), make_change_data([entry])).page.decode()
    assert render.DIFF_DEGRADED_TEXT in text
