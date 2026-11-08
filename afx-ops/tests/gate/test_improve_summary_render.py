"""改善の終了通知を要約 (`summary`) から組む本文と、要約の検査。

DTO は daemon が実際に作った `GET /v1/improve/runs/{id}` の応答の写し
(`tests/fixtures/improve_run_summary_dtos.json`、写しが実経路と一致することは本体側の
test が確かめる)。形を変えて確かめるものは、その写しの値だけを書き換えて作る。
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from afx_ops.gate import improve_render
from afx_ops.gate.improve_render import (SUMMARY_ITEMS, render_improve_finish,
                                         validate_improve_run)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "improve_run_summary_dtos.json"
RECORDED = json.loads(FIXTURE.read_text(encoding="utf-8"))
CANARY_METRICS = RECORDED.pop("_canary_metrics")
DTOS = RECORDED
# `canary_*` は holdout の値を判別できる値にした実経路の応答 (下の canary の test 用)。
NAMES = sorted(name for name in DTOS if not name.startswith("canary_"))
HEAD = "[改善] "


def dto_of(name: str, mutate=None) -> dict:
    data = copy.deepcopy(DTOS[name])
    if mutate is not None:
        mutate(data)
    return data


def payload_of(data: dict) -> dict:
    snap = data["snapshot"]
    return {"ref": f"improve:{data['id']}", "trigger": snap["trigger"],
            "result_code": snap["result_code"], "reason_code": snap["reason_code"],
            "tool_calls": snap["tool_calls"]}


def render(data: dict) -> str:
    detail = validate_improve_run(data, data["id"])
    assert detail is not None
    return render_improve_finish(payload_of(data), detail)


def render_name(name: str, mutate=None) -> str:
    return render(dto_of(name, mutate))


def without_summary(data: dict) -> dict:
    data.pop("summary", None)
    return data


def items_of(data: dict) -> dict:
    return data["summary"]["items"]


def mentions(text: str) -> bool:
    return re.search(r"@everyone|@here|<@[!&]?[0-9]", text) is not None


# ------------------------------------------------------------ 実経路の写しの前提

@pytest.mark.parametrize("name", NAMES)
def test_every_real_dto_carries_a_valid_ok_summary(name):
    data = dto_of(name)
    assert data["summary"]["version"] == 1 and data["summary"]["state"] == "ok"
    detail = validate_improve_run(data, data["id"])
    assert detail is not None and detail["summary"] is not None


def test_the_fixture_covers_every_ending_the_notice_distinguishes():
    endings = {(d["snapshot"]["result_code"], d["snapshot"]["reason_code"])
               for d in DTOS.values()}
    assert endings >= {("observation", None), ("observation", "not_submitted"),
                       ("observation", "duplicate_logic"), ("submitted", None),
                       ("gate_failed", "not_improved"), ("gate_failed", "unprofitable"),
                       ("gate_failed", "insufficient_trades")}
    stages = {(s["stage"], s["state"], bool(p["failed"]))
              for d in DTOS.values() if "floor" in items_of(d)
              for s in items_of(d)["floor"]["stages"] for p in s["pairs"] or [{"failed": []}]}
    assert ("in_sample", "reference", False) in stages
    assert ("holdout", "measured", True) in stages


# ------------------------------------------------------------ allowlist (1 か所)

def test_the_discord_allowlist_is_exactly_the_parent_made_items_and_the_observation():
    assert list(SUMMARY_ITEMS) == ["tried", "floor", "comparison", "observation",
                                   "parent_reason", "handoff"]
    assert "hypothesis" not in SUMMARY_ITEMS and "explanation" not in SUMMARY_ITEMS
    for item in SUMMARY_ITEMS.values():
        assert callable(item.validate) and callable(item.render)


@pytest.mark.parametrize("name", NAMES)
def test_the_hypothesis_and_the_explanation_never_reach_the_validated_summary_or_the_text(name):
    data = dto_of(name)
    items = items_of(data)
    idea = items["hypothesis"]["idea"]
    rationale = items["explanation"]["rationale"]
    artifact = items["explanation"]["artifact_summary"]
    items["hypothesis"]["idea"] = "HYPOTHESIS-CANARY-" + idea
    items["explanation"]["rationale"] = "RATIONALE-CANARY"
    items["explanation"]["artifact_summary"] = "ARTIFACT-CANARY"
    detail = validate_improve_run(data, data["id"])
    assert set(detail["summary"]["items"]) <= set(SUMMARY_ITEMS)
    text = render_improve_finish(payload_of(data), detail)
    originals = [value for value in (idea, rationale, artifact)
                 if isinstance(value, str) and len(value) >= 6]
    assert originals
    for canary in ("HYPOTHESIS-CANARY", "RATIONALE-CANARY", "ARTIFACT-CANARY", *originals):
        assert canary not in text
    assert "hypothesis" not in json.dumps(detail["summary"])


def test_a_broken_hypothesis_or_an_unknown_item_does_not_invalidate_the_summary():
    data = dto_of("unprofitable_holdout")
    items_of(data)["hypothesis"] = 5
    items_of(data)["future_item"] = {"x": 1}
    detail = validate_improve_run(data, data["id"])
    assert detail["summary"] is not None
    assert "future_item" not in detail["summary"]["items"]
    assert render(data) == render_name("unprofitable_holdout")


# ------------------------------------------------------------ 本文の見本 (結末ごと)

TAIL = "所要 0分00秒 / tool -"
FAMILY = r"族 strategy\:未配備指標@ 1h@ USDJPY"

EXPECTED = {
    "observation": (
        "[改善] 提出なし (agent の申告) #{id}\n"
        "agent の申告: session\\_ranges で \\*ロンドン\\* 開始足の逆行を検証した。"
        "\\[確認\\]\\(\\<url\\>\\) @ everyone \\<@ 123456\\> \\`code\\` を含む申告。"
        "次回は別の仮説を優先する。\n" + TAIL),
    "not_submitted": (
        "[改善] 提出なし #{id}\n"
        "agent の申告なし (重複または系統の見送り)\n" + TAIL),
    "quality_demotion": (
        "[改善] 見送り (既存候補と同じロジック) #{id}\n"
        "試したもの: strategy cand (新設)\n" + TAIL),
    "submitted": (
        "[改善] 候補を提出 #{id}\n"
        "plugin cand (plugin) / 承認 #1\n"
        "試したもの: 新設\n"
        f"申し送り (終端時点): {FAMILY} — 試行 1 (pending 1)\n" + TAIL),
    "submitted_improved": (
        "[改善] 候補を提出 #{id}\n"
        "plugin cand (plugin) / 承認 #1\n"
        "試したもの: 新設\n"
        "配備中の版を上回りました\n"
        "USDJPY 候補: pf 1.40 avg_r +0.15 取引 40 評価可能 はい\n"
        "USDJPY 配備中の版: pf 1.10 avg_r +0.05 取引 40 評価可能 はい\n" + TAIL),
    "not_improved": (
        "[改善] 配備中の版を上回らず見送り #{id}\n"
        "配備中の版を上回らなかったため見送ります\n"
        "試したもの: strategy cand (新設)\n"
        "比較結果: holdout で上回らなかった\n" + TAIL),
    "unprofitable_in_sample": (
        "[改善] 床未達で見送り #{id} (unprofitable)\n"
        "試したもの: strategy cand (新設)\n"
        "落ちた段: in-sample\n"
        "in-sample: USDJPY pf 0.30 avg_r -0.10 取引 60\n"
        "holdout: 未実施\n"
        f"申し送り (終端時点): {FAMILY} — 試行 1 (not\\_submitted 1)\n" + TAIL),
    "unprofitable_drawdown": (
        "[改善] 床未達で見送り #{id} (unprofitable)\n"
        "試したもの: strategy cand (新設)\n"
        "落ちた段: in-sample\n"
        "in-sample: USDJPY pf 1.50 avg_r +0.19 取引 194 (DD が深すぎる)\n"
        "holdout: 未実施\n"
        f"申し送り (終端時点): {FAMILY} — 試行 1 (not\\_submitted 1)\n" + TAIL),
    "unprofitable_holdout": (
        "[改善] 床未達で見送り #{id} (unprofitable)\n"
        "試したもの: strategy cand (新設)\n"
        "落ちた段: holdout\n"
        "holdout: USDJPY pf 0.62 avg_r -0.04 取引 53\n"
        f"申し送り (終端時点): {FAMILY} — 試行 1 (not\\_submitted 1)\n" + TAIL),
    "unprofitable_holdout_report_failed": (
        "[改善] 床未達で見送り #{id} (unprofitable)\n"
        "試したもの: strategy cand (新設)\n"
        f"申し送り (終端時点): {FAMILY} — 試行 1 (not\\_submitted 1)\n" + TAIL),
    "long_family_submitted": (
        "[改善] 候補を提出 #{id}\n"
        "plugin cand (plugin) / 承認 #1\n"
        "試したもの: 新設\n"
        f"申し送り (終端時点): 族 strategy\\:{'x' * 60}@ 1h@ USDJPY — 試行 1 (pending 1)\n" + TAIL),
    "unprofitable_holdout_thin": (
        "[改善] 床未達で見送り #{id} (unprofitable)\n"
        "試したもの: strategy cand (新設)\n"
        "落ちた段: holdout\n"
        "holdout: USDJPY pf 1.85 avg_r +0.30 取引 13 (評価不能)\n"
        f"申し送り (終端時点): {FAMILY} — 試行 1 (not\\_submitted 1)\n" + TAIL),
    "insufficient_trades": (
        "[改善] 見送り #{id} (insufficient\\_trades)\n"
        "試したもの: strategy cand (新設)\n"
        "in-sample 参考値: USDJPY pf 1.50 avg_r +0.19 取引 16 (評価不能)\n"
        "holdout: 未実施\n"
        f"申し送り (終端時点): {FAMILY} — 試行 1 (not\\_submitted 1)\n" + TAIL),
}


def test_the_expected_texts_cover_every_fixture_ending():
    assert set(EXPECTED) == set(NAMES)


@pytest.mark.parametrize("name", NAMES)
def test_the_body_is_built_from_the_summary(name):
    data = dto_of(name)
    assert render(data) == EXPECTED[name].format(id=data["id"])


def test_the_elapsed_time_the_tool_count_and_the_repeat_line_still_come_from_the_notice():
    data = dto_of("unprofitable_holdout")
    data["elapsed_sec"] = 372
    data["snapshot"]["tool_calls"] = 41
    detail = validate_improve_run(data, data["id"])
    payload = {**payload_of(data), "tool_calls": 41, "repeat_run": 2, "repeat_max_runs": 5,
               "repeat_elapsed_sec": 600, "repeat_duration_sec": 7200}
    lines = render_improve_finish(payload, detail).splitlines()
    assert lines[-2:] == ["繰り返し 2/5 回 · 10m/2h00m", "所要 6分12秒 / tool 41 回"]


# ------------------------------------------------------------ 参考値と未実施

def test_a_reference_stage_is_labelled_and_a_stage_that_was_not_run_says_so():
    text = render_name("insufficient_trades")
    assert "in-sample 参考値: USDJPY pf 1.50" in text
    assert "holdout: 未実施" in text
    # 参考値は床の判定ではないので「落ちた段」とは言わない。
    assert "落ちた段" not in text


def test_a_holdout_reference_stage_is_never_printed():
    def make_holdout_reference(data):
        stage = items_of(data)["floor"]["stages"][1]
        stage["state"] = "reference"
        stage["pairs"] = [{"pair": "USDJPY", "trades": 4242, "pf": 7.77, "avg_r": 0.4321,
                           "max_drawdown": 0.5151, "trades_per_eval_bar": None,
                           "evaluable": False, "failed": []}]

    text = render_name("insufficient_trades", make_holdout_reference)
    for canary in ("4242", "7.77", "+0.43", "0.5151", "0.52"):
        assert canary not in text


def test_a_holdout_reference_row_that_failed_is_printed_with_the_reference_label():
    def make(data):
        floor = items_of(data)["floor"]
        floor["failed_stage"] = "holdout"
        stage = floor["stages"][1]
        stage["state"] = "reference"
        stage["pairs"] = [{"pair": "USDJPY", "trades": 13, "pf": 1.2, "avg_r": 0.1,
                           "max_drawdown": 0.1, "trades_per_eval_bar": None,
                           "evaluable": False, "failed": ["holdout_not_evaluable"]}]

    text = render_name("unprofitable_in_sample", make)
    assert "holdout 参考値: USDJPY pf 1.20 avg_r +0.10 取引 13 (評価不能)" in text


def test_both_stages_failing_lists_both_and_each_stages_own_failed_pairs():
    def make(data):
        floor = items_of(data)["floor"]
        holdout = floor["stages"][1]
        holdout["state"] = "measured"
        holdout["pairs"] = [{"pair": "EURUSD", "trades": 31, "pf": 0.7, "avg_r": -0.2,
                             "max_drawdown": 0.1, "trades_per_eval_bar": 0.3,
                             "evaluable": True, "failed": ["pf", "too_frequent"]}]

    lines = render_name("unprofitable_in_sample", make).splitlines()
    assert "落ちた段: in-sample, holdout" in lines
    assert "holdout: EURUSD pf 0.70 avg_r -0.20 取引 31 (取引頻度が高すぎる)" in lines
    assert "holdout: 未実施" not in lines


def test_a_pair_that_passed_in_a_failing_stage_is_not_printed():
    def make(data):
        stage = items_of(data)["floor"]["stages"][0]
        stage["pairs"].append({"pair": "EURUSD", "trades": 9999, "pf": 5.55, "avg_r": 0.9,
                               "max_drawdown": 0.01, "trades_per_eval_bar": None,
                               "evaluable": True, "failed": []})

    text = render_name("unprofitable_in_sample", make)
    assert "EURUSD" not in text and "9999" not in text and "5.55" not in text


def test_more_failed_pairs_than_three_are_summarised():
    def make(data):
        stage = items_of(data)["floor"]["stages"][0]
        row = stage["pairs"][0]
        stage["pairs"] = [{**row, "pair": f"PAIR{i}"} for i in range(5)]

    lines = render_name("unprofitable_in_sample", make).splitlines()
    assert [ln.split(" ")[1] for ln in lines if ln.startswith("in-sample: ")] == [
        "PAIR0", "PAIR1", "PAIR2"]
    assert "ほか 2 本" in lines


# ------------------------------------------------------------ 保持 (今の本文が出していた情報)

OLD_CONTENT_ENDINGS = ["unprofitable_in_sample", "unprofitable_holdout",
                       "unprofitable_holdout_thin", "unprofitable_drawdown", "submitted",
                       "observation", "not_submitted", "not_improved", "submitted_improved"]


@pytest.mark.parametrize("name", OLD_CONTENT_ENDINGS)
def test_every_content_line_of_the_old_body_is_still_present(name):
    old_lines = render(without_summary(dto_of(name))).splitlines()
    new_lines = render_name(name).splitlines()
    for line in old_lines:
        if "評価可能" in line:
            # 比較値の行は、評価可否の欄だけを要約が持たない (下の test)。
            line = line.rsplit(" 評価可能", 1)[0]
            assert any(new.startswith(line) for new in new_lines), line
        else:
            assert line in new_lines, line


def test_the_submitted_comparison_values_are_kept():
    old = render(without_summary(dto_of("submitted_improved"))).splitlines()
    new = render_name("submitted_improved").splitlines()
    assert any("評価可能" in line for line in old)
    assert "USDJPY 候補: pf 1.40 avg_r +0.15 取引 40 評価可能 はい" in new
    assert "USDJPY 配備中の版: pf 1.10 avg_r +0.05 取引 40 評価可能 はい" in new
    assert [line for line in old if "評価可能" in line] \
        == [line for line in new if "評価可能" in line]


def test_a_floor_is_not_printed_unless_the_report_is_published():
    published = render_name("unprofitable_holdout")
    assert "取引 53" in published
    for state in ("failed", "none", "prepared"):
        text = render_name("unprofitable_holdout",
                           lambda d, state=state: d.update(report_state=state))
        assert "0.62" not in text and "取引 53" not in text and "落ちた段" not in text
        assert "試したもの: strategy cand (新設)" in text
    failed = render_name("unprofitable_holdout_report_failed")
    assert "0.62" not in failed and "取引 53" not in failed and "落ちた段" not in failed
    assert "床未達で見送り" in failed


def test_a_family_key_past_a_plugin_name_is_kept_and_cut_after_escaping():
    text = render_name("long_family_submitted")
    assert "申し送り (終端時点): 族 strategy" + "\\:" + "x" * 60 in text

    def long_key(data):
        items_of(data)["handoff"]["family"] = "*" * 512
    cut = [line for line in render_name("long_family_submitted", long_key).splitlines()
           if line.startswith("申し送り")][0]
    assert cut.count("…") == 1 and len(cut) < 260


def test_the_approval_number_is_kept_with_a_summary():
    assert "plugin cand (plugin) / 承認 #1" in render_name("submitted").splitlines()


def test_a_failed_pair_with_its_condition_and_the_not_evaluable_mark_are_kept():
    assert "(DD が深すぎる)" in render_name("unprofitable_drawdown")
    assert "(評価不能)" in render_name("unprofitable_holdout_thin")


def test_not_improved_keeps_the_fixed_guidance_and_adds_the_reason_without_numbers():
    lines = render_name("not_improved").splitlines()
    assert "配備中の版を上回らなかったため見送ります" in lines
    assert "比較結果: holdout で上回らなかった" in lines
    assert not any("pf" in line for line in lines)


def test_nothing_is_printed_twice():
    for name in NAMES:
        lines = render_name(name).splitlines()
        assert len(lines) == len(set(lines)), (name, lines)
    submitted = render_name("submitted")
    assert submitted.count("cand") == 1
    observation = render_name("observation")
    assert observation.count("session") == 1
    # 見出しが言っている固定語を、親の判定の行でもう一度言わない。
    assert "親の判定" not in render_name("quality_demotion")


def test_a_parent_reason_the_heading_does_not_state_is_added_once():
    def make(data):
        items_of(data)["parent_reason"] = "family_redirect"

    lines = render_name("unprofitable_in_sample", make).splitlines()
    assert lines.count("親の判定: family\\_redirect") == 1


def test_the_observation_is_printed_only_for_an_agent_declared_observation():
    def make(data):
        items_of(data)["observation"] = "OBSERVATION-CANARY"

    for name in ("not_submitted", "quality_demotion", "unprofitable_holdout", "submitted"):
        assert "OBSERVATION-CANARY" not in render_name(name, make)
    assert "OBSERVATION-CANARY" in render_name("observation", make)


def test_an_observation_without_a_text_says_so():
    def make(data):
        items_of(data).pop("observation")

    assert "agent の申告: (記載なし)" in render_name("observation", make)


def test_the_observation_is_cut_after_escaping_to_300_characters():
    def make(data):
        items_of(data)["observation"] = "*" * 400

    line = [ln for ln in render_name("observation", make).splitlines()
            if ln.startswith("agent の申告: ")][0]
    body = line.removeprefix("agent の申告: ")
    assert len(body) <= 300 and body.endswith("…")


# ------------------------------------------------------------ 要約が使えないときは今の通知

def _set(path, value):
    def apply(data):
        target = data
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = value
    return apply


def _drop(path):
    def apply(data):
        target = data
        for part in path[:-1]:
            target = target[part]
        del target[path[-1]]
    return apply


def _stage(index, key, value):
    def apply(data):
        items_of(data)["floor"]["stages"][index][key] = value
    return apply


def _row(key, value):
    def apply(data):
        items_of(data)["floor"]["stages"][0]["pairs"][0][key] = value
    return apply


BAD_SUMMARIES = {
    "unknown_version": _set(("summary", "version"), 2),
    "version_zero": _set(("summary", "version"), 0),
    "version_is_a_string": _set(("summary", "version"), "1"),
    "version_is_a_bool": _set(("summary", "version"), True),
    "build_failed": _set(("summary", "state"), "build_failed"),
    "empty": _set(("summary", "state"), "empty"),
    "unknown_state": _set(("summary", "state"), "later"),
    "items_missing": _drop(("summary", "items")),
    "items_not_a_dict": _set(("summary", "items"), []),
    "not_a_dict": _set(("summary",), "ok"),
    "version_missing": _drop(("summary", "version")),
    "tried_bad_plugin": lambda d: items_of(d)["tried"].update(plugin="Bad Name"),
    "tried_bad_kind": lambda d: items_of(d)["tried"].update(kind="mystery"),
    "tried_bad_origin": lambda d: items_of(d)["tried"].update(origin="both"),
    "tried_too_many_indicators": lambda d: items_of(d)["tried"].update(
        indicators=[f"ind{i}" for i in range(21)]),
    "tried_bad_indicator": lambda d: items_of(d)["tried"].update(indicators=["Bad Name"]),
    "tried_key_missing": lambda d: items_of(d)["tried"].pop("origin"),
    "floor_not_a_dict": _set(("summary", "items", "floor"), []),
    "floor_unknown_stage": _stage(0, "stage", "oos"),
    "floor_unknown_state": _stage(0, "state", "guess"),
    "floor_stage_order": lambda d: items_of(d)["floor"]["stages"].reverse(),
    "floor_pair_name": _row("pair", "usdjpy"),
    "floor_pf_nan": _row("pf", float("nan")),
    "floor_pf_bool": _row("pf", True),
    "floor_pf_string": _row("pf", "1.2"),
    "floor_trades_negative": _row("trades", -1),
    "floor_trades_bool": _row("trades", True),
    "floor_evaluable_not_bool": _row("evaluable", 1),
    "floor_failed_not_list": _row("failed", "pf"),
    "floor_failed_bad_word": _row("failed", ["PF!"]),
    "floor_too_many_pairs": lambda d: _stage(0, "pairs", items_of(d)["floor"]["stages"][0][
        "pairs"] * 65)(d),
    "comparison_bad_pair": lambda d: items_of(d)["comparison"]["pairs"][0].update(pair="x"),
    "comparison_side_missing": lambda d: items_of(d)["comparison"]["pairs"][0].pop("deployed"),
    "comparison_pf_inf": lambda d: items_of(d)["comparison"]["pairs"][0]["candidate"].update(
        pf=float("inf")),
    "comparison_bad_reason": lambda d: items_of(d)["comparison"].update(reason="Bad Reason"),
    "handoff_empty_family": lambda d: items_of(d)["handoff"].update(family=""),
    "handoff_long_family": lambda d: items_of(d)["handoff"].update(family="f" * 513),
    "handoff_negative_attempts": lambda d: items_of(d)["handoff"].update(attempts=-1),
    "handoff_zero_count": lambda d: items_of(d)["handoff"].update(outcomes={"pending": 0}),
    "handoff_bad_word": lambda d: items_of(d)["handoff"].update(outcomes={"Pending!": 1}),
    "observation_empty": lambda d: items_of(d).update(observation=""),
    "observation_not_text": lambda d: items_of(d).update(observation=5),
    "observation_too_long": lambda d: items_of(d).update(observation="x" * 506),
    "parent_reason_bad": lambda d: items_of(d).update(parent_reason="Bad Reason"),
}

BAD_TARGETS = {
    "tried": "unprofitable_holdout", "floor": "unprofitable_holdout",
    "comparison": "not_improved", "handoff": "unprofitable_holdout",
    "observation": "observation", "parent_reason": "quality_demotion",
}


def _target_for(label: str) -> str:
    for prefix, name in BAD_TARGETS.items():
        if label.startswith(prefix):
            return name
    return "unprofitable_holdout"


@pytest.mark.parametrize("label", sorted(BAD_SUMMARIES))
def test_an_unusable_summary_is_dropped_alone_and_the_current_notice_is_posted(label):
    name = _target_for(label)
    broken = dto_of(name, BAD_SUMMARIES[label])
    detail = validate_improve_run(broken, broken["id"])
    assert detail is not None, "the rest of the DTO must stay usable"
    assert detail["summary"] is None
    assert detail["snapshot"] is not None
    assert render(broken) == render(without_summary(dto_of(name)))


@pytest.mark.parametrize("name", NAMES)
def test_a_daemon_without_the_summary_key_gets_the_current_notice(name):
    data = without_summary(dto_of(name))
    detail = validate_improve_run(data, data["id"])
    assert detail is not None and detail["summary"] is None
    assert "summary" not in data
    assert render(data) == render_improve_finish(payload_of(data), detail)


def test_a_null_summary_is_the_current_notice():
    data = dto_of("unprofitable_holdout", _set(("summary",), None))
    assert render(data) == render(without_summary(dto_of("unprofitable_holdout")))


def test_the_current_notice_for_an_unprofitable_ending_is_kept_byte_for_byte():
    text = render(without_summary(dto_of("unprofitable_holdout_thin")))
    assert text == ("[改善] 床未達で見送り #{id} (unprofitable)\n"
                    "落ちた段: holdout\n"
                    "holdout: USDJPY pf 1.85 avg_r +0.30 取引 13 (評価不能)\n"
                    "所要 0分00秒 / tool -").format(id=DTOS["unprofitable_holdout_thin"]["id"])


# ------------------------------------------------------------ holdout の値

def _canary_floor(data):
    """holdout 段・通った段・比較・閾値の数値を、他に現れない値に置き換える。"""
    items = items_of(data)
    for stage in items.get("floor", {}).get("stages", []):
        if (stage["stage"], stage["state"]) == ("in_sample", "reference"):
            continue  # 評価不能の in-sample の参考値は出す
        for pair in stage["pairs"]:
            if not pair["failed"]:
                pair.update(trades=4242, pf=7.7777, avg_r=0.4321, max_drawdown=0.5151,
                            trades_per_eval_bar=0.7373)
    for pair in items.get("comparison", {}).get("pairs", []):
        for side in ("candidate", "deployed"):
            pair[side].update(trades=5252, pf=8.8888, avg_r=0.6543)
    if "floor" in items:
        items["floor"]["thresholds"].update(min_pf=3.3333, max_drawdown=0.6161)


CANARIES = ("4242", "7.78", "7.7777", "+0.43", "0.4321", "0.5151", "0.52", "0.7373",
            "5252", "8.89", "8.8888", "+0.65", "0.6543", "3.33", "0.6161", "0.62 ")


@pytest.mark.parametrize("name", [n for n in NAMES if n != "unprofitable_holdout"])
def test_no_holdout_or_passing_stage_number_is_printed_outside_the_allowed_rows(name):
    text = render_name(name, _canary_floor)
    for canary in CANARIES:
        if name == "submitted_improved" and canary in ("5252", "8.89", "8.8888", "+0.65",
                                                       "0.6543"):
            continue  # 提出したときの holdout の比較値は出してよい
        assert canary not in text, (name, canary)


def test_an_improved_submission_prints_only_pf_avg_r_and_trades_of_the_holdout_comparison():
    text = render_name("submitted_improved", _canary_floor)
    assert "USDJPY 候補: pf 8.89 avg_r +0.65 取引 5252" in text
    assert "USDJPY 配備中の版: pf 8.89 avg_r +0.65 取引 5252" in text
    for canary in ("4242", "7.78", "+0.43", "0.5151", "0.7373", "3.33", "0.6161", "win_rate",
                   "max_drawdown", "total_pnl"):
        assert canary not in text


def test_a_not_improved_ending_prints_no_comparison_value():
    text = render_name("not_improved", _canary_floor)
    for canary in CANARIES:
        assert canary not in text


def test_a_failed_holdout_prints_that_rows_pf_avg_r_and_trades_but_not_the_in_sample_pass():
    text = render_name("unprofitable_holdout", _canary_floor)
    assert "holdout: USDJPY pf 0.62 avg_r -0.04 取引 53" in text
    for canary in CANARIES:
        if canary != "0.62 ":
            assert canary not in text


def test_only_the_three_floor_values_the_ruling_allows_are_printed_for_a_failed_pair():
    def make(data):
        pair = items_of(data)["floor"]["stages"][1]["pairs"][0]
        pair.update(max_drawdown=0.5151, trades_per_eval_bar=0.7373)
        items_of(data)["floor"]["thresholds"].update(min_pf=3.3333, max_drawdown=0.6161)

    text = render_name("unprofitable_holdout", make)
    for canary in ("0.5151", "0.52", "0.7373", "3.33", "0.6161"):
        assert canary not in text


def _metric_strings(names) -> set[str]:
    out = set()
    for name in names:
        for key in ("trades", "pf", "win_rate", "avg_r", "max_drawdown", "total_pnl"):
            value = CANARY_METRICS[name][key]
            out.add(str(value))
            if isinstance(value, float):
                out.update({f"{value:.2f}", f"{value:.3f}"})
    return out


def test_the_real_canary_run_never_prints_a_value_outside_the_submission_comparison():
    """daemon が実際に作った、値を判別できる応答から組んだ本文。提出したときの holdout の
    比較値 (pf・avg_r・取引数) 以外の指標値は、in-sample も含めて出ない。"""
    text = render(dto_of("canary_improved"))
    shown = ("cand_ho", "dep_ho_worse")
    allowed = set()
    for name in shown:
        for key in ("pf", "avg_r", "trades"):
            value = CANARY_METRICS[name][key]
            allowed.update({str(value), f"{value:.2f}"} if isinstance(value, float)
                           else {str(value)})
    assert "USDJPY 候補: pf 2.95 avg_r +0.53 取引 602222" in text
    assert "USDJPY 配備中の版: pf 1.73 avg_r +0.32 取引 604444" in text
    every = _metric_strings(CANARY_METRICS)
    assert len(every) > 70
    for canary in every - allowed:
        assert canary not in text, canary


def test_the_real_canary_not_improved_run_prints_no_metric_value_at_all():
    text = render(dto_of("canary_not_improved"))
    assert "比較結果: holdout で上回らなかった" in text
    for canary in _metric_strings(CANARY_METRICS):
        assert canary not in text, canary


# ------------------------------------------------------------ 長さと削る順

def _long_indicator(i: int) -> str:
    return ("a_" * 32)[:63] + str(i % 10)


def _fat(data):
    items_of(data)["tried"]["indicators"] = [_long_indicator(i) for i in range(20)]
    items_of(data)["handoff"]["outcomes"] = {"pending": 3, "not_submitted": 2, "approved": 1}
    stage = items_of(data)["floor"]["stages"][0]
    row = stage["pairs"][0]
    stage["pairs"] = [{**row, "pair": f"PAIR{i}"} for i in range(6)]


def test_the_length_is_counted_after_escaping(monkeypatch):
    """上限ちょうどの文字数は収まり、escape の 1 文字ぶん超えたら削る。素の文字数で数えると
    この境目が動く。"""
    full, no_handoff, _rows, _names = _levels("unprofitable_in_sample", _fat)
    text = "\n".join(full)
    assert text.count("\\") >= 3
    monkeypatch.setattr(improve_render, "IMPROVE_BODY_LIMIT", len(text))
    assert render(dto_of("unprofitable_in_sample", _fat)) == text
    monkeypatch.setattr(improve_render, "IMPROVE_BODY_LIMIT", len(text) - 1)
    assert render(dto_of("unprofitable_in_sample", _fat)) == "\n".join(no_handoff)


def test_a_realistic_oversized_body_is_cut_under_the_real_limit():
    def make(data):
        _fat(data)
        items_of(data)["handoff"]["outcomes"] = {f"word_{'x_' * 15}{i}": 5 for i in range(30)}

    data = dto_of("unprofitable_in_sample", make)
    text = render(data)
    assert len(text) <= improve_render.IMPROVE_BODY_LIMIT == 2000
    assert "申し送り" not in text
    assert "落ちた段: in-sample" in text


def _levels(name: str, mutate):
    """各段で削った期待の本文 (本文の行から作る)。"""
    lines = render(dto_of(name, mutate)).splitlines()
    full = lines
    no_handoff = [ln for ln in full if not ln.startswith("申し送り")]
    no_rows = [ln for ln in no_handoff
               if not (ln.startswith("in-sample: ") or ln.startswith("ほか "))]
    no_names = [("試したもの: strategy cand (新設)" if ln.startswith("試したもの") else ln)
                for ln in no_rows]
    return full, no_handoff, no_rows, no_names


def test_the_drop_order_is_the_handoff_then_the_pair_rows_then_the_indicator_names(monkeypatch):
    full, no_handoff, no_rows, no_names = _levels("unprofitable_in_sample", _fat)
    assert any(ln.startswith("申し送り") for ln in full)
    assert any(ln.startswith("in-sample: PAIR0") for ln in full)
    assert any("indicator:" in ln for ln in full)
    sizes = [len("\n".join(level)) for level in (full, no_handoff, no_rows, no_names)]
    assert sizes == sorted(sizes, reverse=True) and len(set(sizes)) == 4
    for level, size in zip((full, no_handoff, no_rows, no_names), sizes):
        monkeypatch.setattr(improve_render, "IMPROVE_BODY_LIMIT", size)
        assert render(dto_of("unprofitable_in_sample", _fat)) == "\n".join(level)


def test_a_body_that_still_does_not_fit_falls_back_to_the_heading_and_the_omission_line(
        monkeypatch):
    monkeypatch.setattr(improve_render, "IMPROVE_BODY_LIMIT", 10)
    data = dto_of("unprofitable_in_sample", _fat)
    text = render(data)
    assert text == f"[改善] 床未達で見送り #{data['id']} (unprofitable)\n(本文を省略)"


def test_the_default_limit_is_two_thousand_and_every_rendering_respects_it():
    assert improve_render.IMPROVE_BODY_LIMIT == 2000
    for name in NAMES:
        full = {"tried", "floor", "handoff"} <= set(items_of(DTOS[name]))
        assert len(render_name(name, _fat if full else None)) <= 2000


# ------------------------------------------------------------ 無害化

HOSTILE = "@everyone <@123> <@&456> **x** ```y``` https://evil.example/a_b [l](http://e.x)"


def test_agent_and_name_strings_are_escaped_and_carry_no_mention():
    def make(data):
        items_of(data)["observation"] = HOSTILE
        items_of(data)["handoff"] = {"family": "@everyone", "attempts": 2,
                                     "outcomes": {"pending": 2}}
        items_of(data)["tried"] = {"plugin": "cand", "kind": "strategy", "origin": "new",
                                   "indicators": ["a_b"]}

    text = render_name("observation", make)
    assert not mentions(text)
    for banned in ("```", "https://", "**x**", "[l](http"):
        assert banned not in text
    text = render_name("submitted", make)
    assert not mentions(text)
