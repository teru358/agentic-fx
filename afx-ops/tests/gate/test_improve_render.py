"""改善通知の文面 (見出しの表・本文行・所要・床数値・取得失敗の全文) と詳細 DTO の検査。"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from afx_ops.gate import render
from afx_ops.gate.render import escape_discord
from afx_ops.gate.improve_render import (format_elapsed, render_improve_finish,
                                         render_improve_start, validate_improve_run)

TS = "2026-10-25T00:00:00+00:00"


def _mentions(text: str) -> bool:
    """Discord が mention と読む形 (@everyone / @here / <@...>) が残っているか。"""
    return re.search(r"@everyone|@here|<@[!&]?[0-9]", text) is not None


def finished(mission_id=2040, result="observation", reason=None, tool=11, trigger="manual"):
    return {"event_id": 5, "ts": TS, "code": "improve_run_changed",
            "ref": f"improve:{mission_id}", "state": "finished", "trigger": trigger,
            "result_code": result, "reason_code": reason, "tool_calls": tool}


def started(mission_id=2043, trigger="manual"):
    return {"event_id": 5, "ts": TS, "code": "improve_run_changed",
            "ref": f"improve:{mission_id}", "state": "started", "trigger": trigger,
            "result_code": None, "reason_code": None, "tool_calls": None}


def pair(name="USDJPY", trades=13, pf=1.8458377992140615, avg_r=0.3029234868346611,
         evaluable=False):
    return {"pair": name, "trades": trades, "pf": pf, "avg_r": avg_r, "evaluable": evaluable}


def dto(mission_id=2040, *, result="observation", reason=None, tool=11, elapsed=289,
        text=None, report_state="none", approval=None, floor=None, trigger="manual",
        backend="codex", model="gpt-5.6-luna", comparison=None):
    return {"id": mission_id, "backend": backend, "model": model, "elapsed_sec": elapsed,
            "snapshot": {"trigger": trigger, "result_code": result, "reason_code": reason,
                         "tool_calls": tool, "observation_text": text},
            "report_state": report_state, "approval": approval, "floor": floor,
            "comparison": comparison}


def finish(result="observation", reason=None, *, tool=11, mission_id=2040, **kw):
    detail = validate_improve_run(dto(mission_id, result=result, reason=reason, tool=tool, **kw),
                                  mission_id)
    assert detail is not None
    return render_improve_finish(finished(mission_id, result, reason, tool), detail)


# ------------------------------------------------------------ 表

PAIRS_WITH_HEADING = {
    ("submitted", None), ("observation", None), ("observation", "not_submitted"),
    ("observation", "demoted_recovered"), ("observation", "risk_gate_unsupported"),
    ("observation", "duplicate_logic"), ("observation", "duplicate_metrics"),
    ("observation", "family_redirect"), ("gate_failed", "unprofitable"),
    ("gate_failed", "not_improved"), ("gate_failed", "same_name_contract_changed"),
    ("report", None), ("report", "duplicate_skipped"),
}


def test_the_heading_table_has_exactly_the_dedicated_pairs():
    assert set(render.IMPROVE_HEADINGS) == PAIRS_WITH_HEADING
    assert all("{id}" in text for text in render.IMPROVE_HEADINGS.values())


def test_the_heading_table_is_a_plain_dict_literal_the_daemon_side_can_read_by_ast():
    tree = ast.parse(Path(render.__file__).read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == "IMPROVE_HEADINGS" for t in n.targets)]
    assert len(nodes) == 1 and isinstance(nodes[0].value, ast.Dict)
    keys = [ast.literal_eval(key) for key in nodes[0].value.keys]
    assert len(keys) == len(set(keys)) == 13


@pytest.mark.parametrize("result,reason,heading", [
    ("submitted", None, "[改善] 候補を提出 #9"),
    ("observation", None, "[改善] 提出なし (agent の申告) #9"),
    ("observation", "not_submitted", "[改善] 提出なし #9"),
    ("observation", "demoted_recovered", "[改善] 提出なし (降格) #9"),
    ("observation", "risk_gate_unsupported", "[改善] 提出なし (risk_gate は未対応) #9"),
    ("observation", "duplicate_logic", "[改善] 見送り (既存候補と同じロジック) #9"),
    ("observation", "duplicate_metrics", "[改善] 見送り (既存候補と同じ成績) #9"),
    ("observation", "family_redirect", "[改善] 見送り (既存系統への誘導) #9"),
    ("gate_failed", "unprofitable", "[改善] 床未達で見送り #9 (unprofitable)"),
    ("gate_failed", "not_improved", "[改善] 配備中の版を上回らず見送り #9"),
    ("gate_failed", "same_name_contract_changed", "[改善] 見送り #9 (同名更新の条件変更)"),
    ("report", None, "[改善] レポートを公開 #9"),
    ("report", "duplicate_skipped", "[改善] レポートを公開 #9 (duplicate_skipped)"),
])
def test_every_dedicated_heading_is_the_first_line(result, reason, heading):
    assert finish(result, reason, mission_id=9).splitlines()[0] == heading


@pytest.mark.parametrize("result,reason,heading", [
    ("submitted", "x_y", r"[改善] 候補を提出 #9 (x\_y)"),
    ("observation", "other_word", r"[改善] 提出なし #9 (other\_word)"),
    ("gate_failed", "insufficient_trades", r"[改善] 見送り #9 (insufficient\_trades)"),
    ("gate_failed", None, "[改善] 見送り #9"),
    ("failed", "commit_crashed", r"[改善] 失敗 #9 (commit\_crashed)"),
    ("failed", None, "[改善] 失敗 #9"),
    ("report", "new_word", r"[改善] レポートを公開 #9 (new\_word)"),
])
def test_a_known_result_without_a_dedicated_row_uses_the_base_word_and_a_parenthesised_reason(
        result, reason, heading):
    assert finish(result, reason, mission_id=9).splitlines()[0] == heading


def test_an_unknown_reason_is_escaped_inside_the_parentheses():
    assert finish("failed", "zzz_new_word", mission_id=9).splitlines()[0] == \
        r"[改善] 失敗 #9 (zzz\_new\_word)"


def test_an_unknown_result_code_is_a_plain_finished_heading_without_a_body():
    payload = finished(9, "failed", None)
    detail = dto(9, result="failed")
    detail["snapshot"]["result_code"] = "brand_new"
    # DTO 検査は 5 値だけを通すので、描画側の防御は検査済みの写しを直接渡して確かめる
    checked = {**validate_improve_run(dto(9), 9),
               "snapshot": {**validate_improve_run(dto(9), 9)["snapshot"],
                            "result_code": "brand_new", "reason_code": "x"}}
    assert render_improve_finish(payload, checked).splitlines() == \
        ["[改善] 終了 #9", "所要 4分49秒 / tool 11 回"]


def test_a_failed_report_publication_has_its_own_heading_for_result_report_only():
    text = finish("report", None, mission_id=9, report_state="failed")
    assert text.splitlines()[0] == "[改善] レポートの公開に失敗 #9"
    text = finish("report", "duplicate_skipped", mission_id=9, report_state="failed")
    assert text.splitlines()[0] == "[改善] レポートの公開に失敗 #9"
    # 他の結果は report_state=failed でも見出しを変えない
    assert finish("failed", "x", mission_id=9, report_state="failed").splitlines()[0] == \
        "[改善] 失敗 #9 (x)"


# ------------------------------------------------------------ 開始

def test_start_shows_the_trigger_and_the_escaped_backend_line():
    detail = validate_improve_run(dto(2043), 2043)
    assert render_improve_start(started(2043, "manual"), detail) == \
        "[改善] 開始 #2043 (手動)\nbackend codex / gpt-5.6-luna"
    assert render_improve_start(started(2043, "scheduled"), detail).splitlines()[0] == \
        "[改善] 開始 #2043 (週次)"


def test_start_without_detail_is_the_first_line_only():
    assert render_improve_start(started(2043, "scheduled"), None) == "[改善] 開始 #2043 (週次)"


def test_detail_repeat_is_ignored_and_an_unknown_shape_does_not_hide_the_backend():
    payload = started(2043)
    payload.update({"repeat_run": 2, "repeat_max_runs": 10,
                    "repeat_elapsed_sec": 305, "repeat_duration_sec": 1800})
    data = dto(2043)
    data["repeat"] = {"run": 1, "max_runs": 1, "elapsed_sec": 0, "duration_sec": 60}
    detail = validate_improve_run(data, 2043)

    assert detail is not None
    assert render_improve_start(payload, detail).splitlines() == [
        "[改善] 開始 #2043 (手動)", "backend codex / gpt-5.6-luna",
        "繰り返し 2/10 回 · 5m/30m"]
    data["repeat"] = {"different": "future_daemon_shape"}
    assert validate_improve_run(data, 2043) is not None


def test_start_escapes_backend_and_model():
    detail = validate_improve_run(dto(2043, backend="@everyone", model="<@&1>_x*"), 2043)
    lines = render_improve_start(started(2043), detail).splitlines()
    assert lines[1] == (f"backend {escape_discord('@everyone')} / "
                        f"{escape_discord('<@&1>_x*')}")
    assert not _mentions(lines[1])


# ------------------------------------------------------------ 所要

@pytest.mark.parametrize("seconds,text", [
    (0, "0分00秒"), (1, "0分01秒"), (59, "0分59秒"), (60, "1分00秒"), (289, "4分49秒"),
    (3599, "59分59秒"), (3600, "1時間00分00秒"), (3661, "1時間01分01秒"),
    (36000 + 61, "10時間01分01秒"),
])
def test_elapsed_is_minutes_and_seconds_then_hours(seconds, text):
    assert format_elapsed(seconds) == text


@pytest.mark.parametrize("elapsed,tool,last", [
    (289, 11, "所要 4分49秒 / tool 11 回"),
    (289, None, "所要 4分49秒 / tool -"),
    (None, 11, "tool 11 回"),
    (None, None, "所要 -"),
    (0, 0, "所要 0分00秒 / tool 0 回"),
])
def test_the_last_line_omits_what_is_missing(elapsed, tool, last):
    assert finish("failed", "interrupted", tool=tool, elapsed=elapsed).splitlines()[-1] == last


def test_tool_count_comes_from_the_snapshot_not_the_event():
    detail = validate_improve_run(dto(2040, tool=3), 2040)
    assert render_improve_finish(finished(2040, tool=99), detail).splitlines()[-1] == \
        "所要 4分49秒 / tool 3 回"


# ------------------------------------------------------------ submitted

def test_submitted_shows_the_plugin_kind_and_approval():
    text = finish("submitted", None, mission_id=2050, elapsed=330, tool=14,
                  approval={"id": 26, "plugin": "sma_cross", "kind": "strategy"})
    assert text == ("[改善] 候補を提出 #2050\nplugin sma\\_cross (strategy) / 承認 #26\n"
                    "所要 5分30秒 / tool 14 回")


def test_submitted_without_approval_info_omits_the_line():
    assert finish("submitted", None, mission_id=2050, approval=None).splitlines() == \
        ["[改善] 候補を提出 #2050", "所要 4分49秒 / tool 11 回"]


def test_submitted_with_an_unsafe_plugin_name_shows_only_the_approval():
    text = finish("submitted", None, mission_id=2050,
                  approval={"id": 26, "plugin": "@everyone", "kind": "strategy"})
    assert text.splitlines()[1] == "承認 #26"


def test_submitted_escapes_a_hostile_kind():
    text = finish("submitted", None, mission_id=2050,
                  approval={"id": 26, "plugin": "p", "kind": "@everyone <@&1>"})
    line = text.splitlines()[1]
    assert line == f"plugin p ({escape_discord('@everyone <@&1>')}) / 承認 #26"
    assert not _mentions(line)


# ------------------------------------------------------------ observation

def test_observation_note_is_the_escaped_one_line_text():
    text = finish("observation", None, text="a **b** @everyone <@123>\nline2 ```x``` https://e.co/x")
    note = text.splitlines()[1]
    assert note.startswith("agent の申告: ")
    for banned in ("@everyone", "<@123>", "```", "https://", "**b**"):
        assert banned not in note
    assert len(text.splitlines()) == 3


@pytest.mark.parametrize("value", [None, "", "   ", "\n\t \r\n", "​\x00"])
def test_a_missing_or_blank_note_is_marked_as_not_written(value):
    assert finish("observation", None, text=value).splitlines()[1] == "agent の申告: (記載なし)"


def test_a_note_of_exactly_three_hundred_characters_is_not_cut():
    note = finish("observation", None, text="あ" * 300).splitlines()[1]
    assert note == "agent の申告: " + "あ" * 300


def test_a_longer_note_is_cut_to_three_hundred_escaped_characters_with_a_mark():
    body = finish("observation", None, text="あ" * 301).splitlines()[1][len("agent の申告: "):]
    assert len(body) == 300 and body.endswith("…") and set(body[:-1]) == {"あ"}


def test_the_cut_never_splits_an_escape_unit_and_counts_escaped_length():
    body = finish("observation", None, text="*" * 400).splitlines()[1][len("agent の申告: "):]
    assert len(body) <= 300 and body.endswith("…")
    assert body[:-1] == "\\*" * ((len(body) - 1) // 2)


def test_a_long_underscore_run_stays_within_the_limit():
    body = finish("observation", None, text="_" * 1000).splitlines()[1][len("agent の申告: "):]
    assert len(body) <= 300 and body.endswith("…")


def test_not_submitted_never_shows_the_agents_note():
    text = finish("observation", "not_submitted", text="SECRET NOTE")
    assert text.splitlines() == ["[改善] 提出なし #2040",
                                 "agent の申告なし (重複または系統の見送り)",
                                 "所要 4分49秒 / tool 11 回"]


def test_demoted_recovered_has_its_fixed_body():
    assert finish("observation", "demoted_recovered", text="SECRET").splitlines()[1] == \
        "resume 回収経由のため report は公開せず降格しました"


@pytest.mark.parametrize("reason", ["risk_gate_unsupported", "duplicate_logic",
                                    "duplicate_metrics", "family_redirect"])
def test_observation_rows_without_a_body_show_no_note(reason):
    lines = finish("observation", reason, text="SECRET").splitlines()
    assert len(lines) == 2 and "SECRET" not in "\n".join(lines)


def test_the_whole_text_fits_the_discord_limit_even_for_the_longest_inputs():
    text = finish("observation", None, text="@" * 5000)
    assert len(text) <= 2000
    stages = [{"stage": "in_sample", "pairs": [pair(f"P{i}") for i in range(30)]},
              {"stage": "holdout", "pairs": [pair(f"Q{i}") for i in range(30)]}]
    text = finish("gate_failed", "unprofitable", floor={"stages": stages})
    assert len(text) <= 2000


# ------------------------------------------------------------ 床数値

def _floor(*stages):
    return {"stages": [{"stage": name, "pairs": list(pairs)} for name, pairs in stages]}


def test_a_holdout_floor_failure_shows_the_stage_and_the_unevaluable_pair():
    text = finish("gate_failed", "unprofitable", mission_id=2043, elapsed=261, tool=10,
                  report_state="published", floor=_floor(("holdout", [pair()])))
    assert text == ("[改善] 床未達で見送り #2043 (unprofitable)\n落ちた段: holdout\n"
                    "holdout: USDJPY pf 1.85 avg_r +0.30 取引 13 (評価不能)\n"
                    "所要 4分21秒 / tool 10 回")


def test_an_in_sample_failure_is_labelled_in_sample_and_an_evaluable_pair_has_no_suffix():
    row = pair(trades=40, pf=0.80, avg_r=-0.10, evaluable=True)
    text = finish("gate_failed", "unprofitable", mission_id=2051, elapsed=185, tool=12,
                  report_state="published", floor=_floor(("in_sample", [row])))
    assert text == ("[改善] 床未達で見送り #2051 (unprofitable)\n落ちた段: in-sample\n"
                    "in-sample: USDJPY pf 0.80 avg_r -0.10 取引 40\n所要 3分05秒 / tool 12 回")


def test_two_failed_stages_are_listed_in_order_with_their_own_rows():
    text = finish("gate_failed", "unprofitable", report_state="published",
                  floor=_floor(("in_sample", [pair("EURUSD", evaluable=True)]),
                               ("holdout", [pair("USDJPY")])))
    assert text.splitlines()[1:4] == [
        "落ちた段: in-sample, holdout",
        "in-sample: EURUSD pf 1.85 avg_r +0.30 取引 13",
        "holdout: USDJPY pf 1.85 avg_r +0.30 取引 13 (評価不能)"]


def test_more_than_three_pairs_in_a_stage_are_cut_with_a_remainder_line():
    rows = [pair(f"P{i}AAA") for i in range(4)]
    lines = finish("gate_failed", "unprofitable", report_state="published",
                   floor=_floor(("holdout", rows))).splitlines()
    assert [line for line in lines if line.startswith("holdout: ")].__len__() == 3
    assert lines[5] == "ほか 1 本"


def test_each_stage_is_cut_separately():
    rows = [pair(f"P{i}AAA", evaluable=True) for i in range(5)]
    lines = finish("gate_failed", "unprofitable", report_state="published",
                   floor=_floor(("in_sample", rows), ("holdout", rows))).splitlines()
    assert lines.count("ほか 2 本") == 2
    assert sum(1 for line in lines if line.startswith(("in-sample: ", "holdout: "))) == 6


def test_a_null_floor_shows_neither_the_stage_nor_numbers():
    lines = finish("gate_failed", "unprofitable", floor=None).splitlines()
    assert lines == ["[改善] 床未達で見送り #2040 (unprofitable)", "所要 4分49秒 / tool 11 回"]


@pytest.mark.parametrize("pf,avg_r,expected", [
    (-0.001, -0.001, "pf 0.00 avg_r +0.00"),
    (0.001, 0.001, "pf 0.00 avg_r +0.00"),
    (-0.004999, -0.004999, "pf 0.00 avg_r +0.00"),
    (0.005, 0.005, f"pf {0.005:.2f} avg_r {0.005:+.2f}"),
    (0.015, 0.015, f"pf {0.015:.2f} avg_r {0.015:+.2f}"),
    (2.675, 2.675, f"pf {2.675:.2f} avg_r {2.675:+.2f}"),
    (-2.675, -2.675, f"pf {-2.675:.2f} avg_r {-2.675:+.2f}"),
    (1, 0, "pf 1.00 avg_r +0.00"),
    (None, 0.5, "pf - avg_r +0.50"),
    (0.5, None, "pf 0.50 avg_r -"),
    (None, None, "pf - avg_r -"),
])
def test_numbers_use_format_and_normalise_negative_zero(pf, avg_r, expected):
    text = finish("gate_failed", "unprofitable", report_state="published",
                  floor=_floor(("holdout", [pair(pf=pf, avg_r=avg_r)])))
    assert f"USDJPY {expected} 取引 13" in text


def test_the_known_rounding_values_differ_from_a_naive_round_half_up():
    assert f"{2.675:.2f}" == "2.67" and f"{0.015:.2f}" == "0.01"
    text = finish("gate_failed", "unprofitable", report_state="published",
                  floor=_floor(("holdout", [pair(pf=2.675, avg_r=0.015)])))
    assert "pf 2.67 avg_r +0.01" in text


def test_a_pair_name_outside_the_grammar_drops_only_the_floor():
    checked = validate_improve_run(dto(2040, result="gate_failed", reason="unprofitable",
                                       report_state="published",
                                       floor=_floor(("holdout", [pair("@everyone<@&1>")]))), 2040)
    assert checked is not None and checked["floor"] is None
    text = render_improve_finish(finished(2040, "gate_failed", "unprofitable"), checked)
    assert text.splitlines() == ["[改善] 床未達で見送り #2040 (unprofitable)",
                                 "所要 4分49秒 / tool 11 回"]


def test_a_pair_name_that_reaches_the_renderer_is_still_escaped():
    checked = validate_improve_run(dto(2040, result="gate_failed", reason="unprofitable",
                                       report_state="published",
                                       floor=_floor(("holdout", [pair()]))), 2040)
    checked["floor"]["stages"][0]["pairs"][0]["pair"] = "@everyone<@&1>"
    text = render_improve_finish(finished(2040, "gate_failed", "unprofitable"), checked)
    assert escape_discord("@everyone<@&1>") in text and not _mentions(text)


# ------------------------------------------------------------ 本文の長さ

LONG = "ab_*" * 100


def test_long_backend_model_kind_and_plugin_are_cut_to_sixty_four_characters():
    start = validate_improve_run(dto(2043, backend=LONG, model=LONG), 2043)
    text = render_improve_start(started(2043), start)
    backend_line = text.splitlines()[1]
    assert len(backend_line) <= len("backend  / ") + 2 * 64 and backend_line.count("…") == 2
    approval = {"id": 3, "plugin": "p" * 40, "kind": LONG}
    done = validate_improve_run(dto(2040, result="submitted", approval=approval), 2040)
    line = render_improve_finish(finished(2040, "submitted", None), done).splitlines()[1]
    assert len(line) <= len("plugin  () / 承認 #3") + 64 + 64 and "…" in line


def test_a_pair_name_is_cut_even_when_it_reaches_the_renderer():
    checked = validate_improve_run(dto(2040, result="gate_failed", reason="unprofitable",
                                       report_state="published",
                                       floor=_floor(("holdout", [pair()]))), 2040)
    checked["floor"]["stages"][0]["pairs"][0]["pair"] = LONG
    text = render_improve_finish(finished(2040, "gate_failed", "unprofitable"), checked)
    assert "…" in text and len(text) < 400


def test_the_longest_accepted_values_stay_inside_the_discord_limit():
    pairs = [pair("A" * 16, evaluable=True) for _ in range(5)]
    text = finish("gate_failed", "unprofitable", report_state="published", backend=LONG,
                  model=LONG, floor=_floor(("in_sample", pairs), ("holdout", pairs)))
    assert len(text) <= 2000 and "本文を省略" not in text


def test_a_body_over_the_discord_limit_falls_back_to_the_heading_and_a_fixed_line(monkeypatch):
    from afx_ops.gate import improve_render
    monkeypatch.setattr(improve_render, "IMPROVE_BODY_LIMIT", 30)
    text = finish("observation", None, text="あ" * 200)
    assert text == "[改善] 提出なし (agent の申告) #2040\n(本文を省略)"
    start = render_improve_start(started(2043), validate_improve_run(dto(2043), 2043))
    assert start == "[改善] 開始 #2043 (手動)\n(本文を省略)"


def test_the_fit_helper_keeps_a_short_text_and_cuts_a_long_one_to_the_first_line():
    from afx_ops.gate.improve_render import _fit
    assert _fit(["a", "b"]) == "a\nb"
    assert _fit(["head", "x" * 3000]) == "head\n(本文を省略)"


# ------------------------------------------------------------ 床の条件と形

def _floor_text(**kw):
    detail = validate_improve_run(dto(2040, **kw), 2040)
    assert detail is not None
    return detail


@pytest.mark.parametrize("kw", [
    dict(result="gate_failed", reason="unprofitable", report_state="failed"),
    dict(result="gate_failed", reason="unprofitable", report_state="prepared"),
    dict(result="gate_failed", reason="unprofitable", report_state="none"),
    dict(result="gate_failed", reason="other_reason", report_state="published"),
    dict(result="failed", reason="unprofitable", report_state="published"),
    dict(result="observation", reason=None, report_state="published"),
])
def test_the_floor_is_used_only_for_an_unprofitable_gate_failure_with_a_published_report(kw):
    detail = _floor_text(floor=_floor(("holdout", [pair()])), **kw)
    assert detail["floor"] is None and detail["snapshot"]["result_code"] == kw["result"]
    text = render_improve_finish(finished(2040, kw["result"], kw["reason"]), detail)
    assert "pf " not in text and "落ちた段" not in text


def test_a_floor_without_a_terminal_snapshot_is_dropped():
    detail = validate_improve_run({**dto(2040, report_state="published",
                                         floor=_floor(("holdout", [pair()]))),
                                   "snapshot": None}, 2040)
    assert detail is not None and detail["floor"] is None


def test_the_floor_is_kept_for_an_unprofitable_gate_failure_with_a_published_report():
    detail = _floor_text(result="gate_failed", reason="unprofitable", report_state="published",
                         floor=_floor(("holdout", [pair()])))
    assert detail["floor"]["stages"][0]["stage"] == "holdout"


def test_empty_stages_mean_no_numbers_and_the_rest_of_the_detail_is_kept():
    detail = _floor_text(result="gate_failed", reason="unprofitable", report_state="published",
                         floor={"stages": []})
    assert detail["floor"] is None and detail["elapsed_sec"] == 289
    text = render_improve_finish(finished(2040, "gate_failed", "unprofitable"), detail)
    assert text == "[改善] 床未達で見送り #2040 (unprofitable)\n所要 4分49秒 / tool 11 回"


@pytest.mark.parametrize("name,floor", [
    ("empty pairs", _floor(("holdout", []))),
    ("empty pairs in the first of two", _floor(("in_sample", []), ("holdout", [pair()]))),
    ("reversed order", _floor(("holdout", [pair()]), ("in_sample", [pair()]))),
    ("duplicated stage", _floor(("holdout", [pair()]), ("holdout", [pair()]))),
    ("lowercase pair", _floor(("holdout", [pair("usdjpy")]))),
    ("slash in pair", _floor(("holdout", [pair("bad/name")]))),
    ("short pair", _floor(("holdout", [pair("US")]))),
    ("long pair", _floor(("holdout", [pair("A" * 17)]))),
    ("second row bad", _floor(("holdout", [pair(), pair("a b")]))),
])
def test_a_floor_that_breaks_the_stage_contract_is_dropped_but_the_detail_is_kept(name, floor):
    detail = _floor_text(result="gate_failed", reason="unprofitable", report_state="published",
                         floor=floor)
    assert detail["floor"] is None and detail["id"] == 2040
    assert render_improve_finish(finished(2040, "gate_failed", "unprofitable"),
                                 detail).splitlines()[-1] == "所要 4分49秒 / tool 11 回"


# ------------------------------------------------------------ 取得失敗の全文

@pytest.mark.parametrize("result,reason,tool,expected", [
    ("observation", None, 11, "[改善] 提出なし (agent の申告) #2040\ntool 11 回\n"
                              "(詳細を取得できませんでした)"),
    ("gate_failed", "unprofitable", 10,
     "[改善] 床未達で見送り #2040 (unprofitable)\ntool 10 回\n(詳細を取得できませんでした)"),
    ("failed", "commit_crashed", None,
     "[改善] 失敗 #2040 (commit\\_crashed)\n(詳細を取得できませんでした)"),
    ("submitted", None, 14, "[改善] 候補を提出 #2040\ntool 14 回\n(詳細を取得できませんでした)"),
    ("report", None, 0, "[改善] レポートを公開 #2040\ntool 0 回\n(詳細を取得できませんでした)"),
])
def test_finish_without_detail_is_built_from_the_event_only(result, reason, tool, expected):
    assert render_improve_finish(finished(2040, result, reason, tool), None) == expected


def test_finish_without_detail_for_an_unknown_result_is_the_plain_heading():
    payload = finished(2040, "failed", None, None)
    payload["result_code"] = "brand_new"
    assert render_improve_finish(payload, None) == "[改善] 終了 #2040\n(詳細を取得できませんでした)"


def test_a_finished_detail_without_a_snapshot_falls_back_to_the_event_only_text():
    detail = validate_improve_run({**dto(2040), "snapshot": None}, 2040)
    assert render_improve_finish(finished(2040), detail) == \
        "[改善] 提出なし (agent の申告) #2040\ntool 11 回\n(詳細を取得できませんでした)"


# ------------------------------------------------------------ DTO の検査

def test_a_valid_dto_is_returned_with_only_the_used_keys():
    data = dto(2043, result="gate_failed", reason="unprofitable", report_state="published",
               floor=_floor(("holdout", [pair()])))
    data["extra"] = {"path": "/secret"}
    checked = validate_improve_run(data, 2043)
    assert checked is not None and "extra" not in checked
    assert checked["floor"]["stages"][0]["pairs"][0]["pf"] == 1.8458377992140615


def test_improved_submission_renders_only_the_four_holdout_metrics_per_version():
    comparison = {"base_content_hash": "b" * 64, "comparison": {
        "verdict": "improved", "stages": {"holdout": {"USDJPY": {
            "candidate": {"pf": 1.2, "avg_r": 0.1, "trades": 40, "evaluable": True,
                          "max_drawdown": 0.9},
            "deployed": {"pf": 1.1, "avg_r": 0.05, "trades": 38, "evaluable": True,
                         "max_drawdown": 0.8}}}}}}
    text = finish("submitted", approval={"id": 7, "plugin": "s", "kind": "strategy"},
                  comparison=comparison)
    assert "配備中の版を上回りました" in text
    assert "USDJPY 候補: pf 1.20 avg_r +0.10 取引 40 評価可能 はい" in text
    assert "USDJPY 配備中の版: pf 1.10 avg_r +0.05 取引 38 評価可能 はい" in text
    assert "max_drawdown" not in text and "in-sample" not in text


@pytest.mark.parametrize("result,reason,verdict", [
    ("gate_failed", "not_improved", "not_improved"),
    ("submitted", None, "unavailable"),
])
def test_non_improved_comparisons_never_render_numbers(result, reason, verdict):
    comparison = {"base_content_hash": "b" * 64, "comparison": {
        "verdict": verdict, "stages": {"holdout": {"USDJPY": {
            "candidate": {"pf": 9.9, "avg_r": 9.9, "trades": 999, "evaluable": True},
            "deployed": {"pf": 1.0, "avg_r": 0.0, "trades": 1, "evaluable": False}}}}}}
    text = finish(result, reason, comparison=comparison)
    assert "9.9" not in text and "999" not in text


def test_a_pre_terminal_dto_with_null_snapshot_and_elapsed_is_valid():
    checked = validate_improve_run({**dto(2043), "snapshot": None, "elapsed_sec": None}, 2043)
    assert checked is not None and checked["snapshot"] is None


def test_the_plugin_name_is_dropped_unless_it_passes_the_name_grammar():
    ok = validate_improve_run(dto(approval={"id": 1, "plugin": "sma_cross", "kind": None}), 2040)
    bad = validate_improve_run(dto(approval={"id": 1, "plugin": "Bad Name", "kind": "x"}), 2040)
    assert ok["approval"]["plugin"] == "sma_cross" and bad["approval"]["plugin"] is None


def _bad(mutate):
    data = dto(2040, result="gate_failed", reason="unprofitable", report_state="published",
               floor=_floor(("holdout", [pair()])), approval={"id": 3, "plugin": "p", "kind": "k"})
    mutate(data)
    return data


@pytest.mark.parametrize("name,mutate", [
    ("id mismatch", lambda d: d.update(id=2041)),
    ("id bool", lambda d: d.update(id=True)),
    ("id str", lambda d: d.update(id="2040")),
    ("id missing", lambda d: d.pop("id")),
    ("backend missing", lambda d: d.pop("backend")),
    ("backend int", lambda d: d.update(backend=1)),
    ("model none", lambda d: d.update(model=None)),
    ("elapsed negative", lambda d: d.update(elapsed_sec=-1)),
    ("elapsed float", lambda d: d.update(elapsed_sec=1.5)),
    ("elapsed bool", lambda d: d.update(elapsed_sec=True)),
    ("elapsed missing", lambda d: d.pop("elapsed_sec")),
    ("report_state unknown", lambda d: d.update(report_state="done")),
    ("report_state missing", lambda d: d.pop("report_state")),
    ("snapshot list", lambda d: d.update(snapshot=[])),
    ("snapshot missing", lambda d: d.pop("snapshot")),
    ("snapshot result unknown", lambda d: d["snapshot"].update(result_code="new")),
    ("snapshot trigger unknown", lambda d: d["snapshot"].update(trigger="auto")),
    ("snapshot reason bad", lambda d: d["snapshot"].update(reason_code="Bad:1")),
    ("snapshot tool bool", lambda d: d["snapshot"].update(tool_calls=True)),
    ("snapshot tool negative", lambda d: d["snapshot"].update(tool_calls=-1)),
    ("snapshot tool float", lambda d: d["snapshot"].update(tool_calls=1.5)),
    ("snapshot text int", lambda d: d["snapshot"].update(observation_text=1)),
    ("snapshot key missing", lambda d: d["snapshot"].pop("observation_text")),
    ("approval list", lambda d: d.update(approval=[])),
    ("approval id str", lambda d: d["approval"].update(id="3")),
    ("approval id bool", lambda d: d["approval"].update(id=True)),
    ("approval plugin int", lambda d: d["approval"].update(plugin=3)),
    ("approval kind int", lambda d: d["approval"].update(kind=3)),
    ("approval key missing", lambda d: d["approval"].pop("kind")),
    ("floor list", lambda d: d.update(floor=[])),
    ("floor no stages", lambda d: d.update(floor={})),
    ("floor stage unknown", lambda d: d["floor"]["stages"][0].update(stage="train")),
    ("floor stage name not a string", lambda d: d["floor"]["stages"][0].update(stage=["holdout"])),
    ("floor stages not a list", lambda d: d["floor"].update(stages="holdout")),
    ("floor pairs missing", lambda d: d["floor"]["stages"][0].pop("pairs")),
    ("pair not dict", lambda d: d["floor"]["stages"][0]["pairs"].append("USDJPY")),
    ("pair name int", lambda d: d["floor"]["stages"][0]["pairs"][0].update(pair=1)),
    ("pair trades negative", lambda d: d["floor"]["stages"][0]["pairs"][0].update(trades=-1)),
    ("pair trades bool", lambda d: d["floor"]["stages"][0]["pairs"][0].update(trades=True)),
    ("pair trades float", lambda d: d["floor"]["stages"][0]["pairs"][0].update(trades=1.0)),
    ("pair pf nan", lambda d: d["floor"]["stages"][0]["pairs"][0].update(pf=float("nan"))),
    ("pair pf inf", lambda d: d["floor"]["stages"][0]["pairs"][0].update(pf=float("inf"))),
    ("pair avg_r -inf", lambda d: d["floor"]["stages"][0]["pairs"][0].update(
        avg_r=float("-inf"))),
    ("pair pf str", lambda d: d["floor"]["stages"][0]["pairs"][0].update(pf="1.0")),
    ("pair pf bool", lambda d: d["floor"]["stages"][0]["pairs"][0].update(pf=True)),
    ("pair pf huge int", lambda d: d["floor"]["stages"][0]["pairs"][0].update(pf=10 ** 400)),
    ("pair evaluable int", lambda d: d["floor"]["stages"][0]["pairs"][0].update(evaluable=1)),
    ("pair key missing", lambda d: d["floor"]["stages"][0]["pairs"][0].pop("evaluable")),
])
def test_a_dto_that_breaks_the_contract_is_rejected(name, mutate):
    assert validate_improve_run(_bad(mutate), 2040) is None


@pytest.mark.parametrize("value", [None, [], "x", 3])
def test_a_non_object_response_is_rejected(value):
    assert validate_improve_run(value, 2040) is None


def test_the_default_dto_used_by_the_negative_cases_is_itself_valid():
    assert validate_improve_run(_bad(lambda d: None), 2040) is not None
