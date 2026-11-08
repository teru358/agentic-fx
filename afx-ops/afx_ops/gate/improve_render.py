"""改善 mission の開始・終了通知の文面と、詳細 (GET の応答) の検査。

見出しは `render.IMPROVE_HEADINGS` を `(result_code, reason_code)` と `report_state` だけで引く。
変数部分 (観察文・reason・pair・backend / model・plugin 名・kind) は全て escape し、固定語と
数字は escape しない。詳細が無い (取得できなかった・検査に反した) ときは event の固定 field
だけから作る。設定済みの mention は付けない (呼び側は allowed_mentions を none にする)。
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass

from afx_ops import improve_events

from .render import (IMPROVE_BASE_WORDS, IMPROVE_HEADINGS, IMPROVE_OBSERVATION_CHARS,
                     IMPROVE_REPORT_FAILED_HEADING, IMPROVE_START_HEADING,
                     IMPROVE_UNKNOWN_HEADING, cut_escaped, escape_discord, plugin_name_or_none)

_DTO_KEYS = frozenset({"id", "backend", "model", "elapsed_sec", "snapshot", "report_state",
                      "approval", "floor"})
REPORT_STATES = frozenset({"none", "prepared", "published", "failed"})
STAGES = ("in_sample", "holdout")
STAGE_LABELS = {"in_sample": "in-sample", "holdout": "holdout"}
CONDITION_LABELS = {
    "drawdown_too_deep": "DD が深すぎる",
    "too_frequent": "取引頻度が高すぎる",
}
FLOOR_CONDITION_KINDS = tuple(CONDITION_LABELS)
TRIGGER_LABELS = {"manual": "手動", "scheduled": "週次"}
MAX_PAIR_LINES = 3
IMPROVE_BODY_LIMIT = 2000
FIELD_CHARS = 64
FAMILY_KEY_CHARS = 512
FAMILY_DISPLAY_CHARS = 160
OMITTED_LINE = "(本文を省略)"
_PAIR_NAME = re.compile(r"[A-Z0-9_]{3,16}\Z")
UNAVAILABLE_LINE = "(詳細を取得できませんでした)"
NO_NOTE = "(記載なし)"

_NOT_SUBMITTED_LINE = "agent の申告なし (重複または系統の見送り)"
_DEMOTED_LINE = "resume 回収経由のため report は公開せず降格しました"
_ONE_HOUR = 3600


def mission_id_of(payload: Mapping[str, object]) -> int:
    """event の ref (`improve:<id>`) の mission id。"""
    return int(str(payload["ref"]).split(":", 1)[1])


# ------------------------------------------------------------ 詳細 (GET の応答) の検査

def _int(value: object, *, low: int = 0, high: int | None = None) -> bool:
    return (type(value) is int and value >= low and (high is None or value <= high))


def _text_or_none(value: object) -> bool:
    return value is None or type(value) is str


def _number_or_none(value: object) -> float | None | bool:
    """有限の数 (bool 不可) はそのまま、null は None。他は False (不正)。"""
    if value is None:
        return None
    if type(value) not in (int, float):
        return False
    try:
        return float(value) if math.isfinite(float(value)) else False
    except OverflowError:
        return False


def _snapshot(value: object) -> dict | None | bool:
    if value is None:
        return None
    if type(value) is not dict:
        return False
    keys = ("trigger", "result_code", "reason_code", "tool_calls", "observation_text")
    if any(key not in value for key in keys):
        return False
    trigger, result, reason = value["trigger"], value["result_code"], value["reason_code"]
    if (type(trigger) is not str or trigger not in improve_events.TRIGGERS
            or type(result) is not str or result not in improve_events.RESULT_CODES
            or not improve_events.valid_reason_code(reason)
            or not improve_events.valid_tool_calls(value["tool_calls"])
            or not _text_or_none(value["observation_text"])):
        return False
    return {key: value[key] for key in keys}


def _approval(value: object) -> dict | None | bool:
    if value is None:
        return None
    if (type(value) is not dict or not {"id", "plugin", "kind"} <= set(value)
            or not _int(value["id"]) or not _text_or_none(value["plugin"])
            or not _text_or_none(value["kind"])):
        return False
    return {"id": value["id"], "plugin": plugin_name_or_none(value["plugin"]),
            "kind": value["kind"]}


def _pair_row(value: object) -> dict | bool:
    if (type(value) is not dict or not {"pair", "trades", "pf", "avg_r", "evaluable"} <= set(value)
            or type(value["pair"]) is not str or not _int(value["trades"])
            or type(value["evaluable"]) is not bool):
        return False
    pf, avg_r = _number_or_none(value["pf"]), _number_or_none(value["avg_r"])
    if pf is False or avg_r is False:
        return False
    conditions = value.get("conditions", ())
    if (type(conditions) not in (list, tuple)
            or any(type(kind) is not str for kind in conditions)):
        return False
    if (len(set(conditions)) != len(conditions)
            or tuple(conditions) != tuple(
                kind for kind in FLOOR_CONDITION_KINDS if kind in conditions)):
        return False
    return {"pair": value["pair"], "trades": value["trades"], "pf": pf, "avg_r": avg_r,
            "evaluable": value["evaluable"], "conditions": list(conditions)}


class _DropFloor:
    """floor の中身が契約の形に反する印。DTO 全体は生かし、床の数値だけを使わない。"""


_DROP_FLOOR = _DropFloor()


def _floor(value: object) -> dict | None | bool | _DropFloor:
    """型の不正は False (DTO 全体を不正とする)。型は合うが中身が契約に反する (空の stages・
    段の順序・重複・空の pairs・pair 名の文法) ときは _DROP_FLOOR。"""
    if value is None:
        return None
    if type(value) is not dict or type(value.get("stages")) is not list:
        return False
    stages = []
    for stage in value["stages"]:
        if (type(stage) is not dict or stage.get("stage") not in STAGES
                or type(stage.get("pairs")) is not list):
            return False
        rows = [_pair_row(row) for row in stage["pairs"]]
        if any(row is False for row in rows):
            return False
        stages.append({"stage": stage["stage"], "pairs": rows})
    if not stages:
        return _DROP_FLOOR
    order = [STAGES.index(stage["stage"]) for stage in stages]
    if (order != sorted(set(order)) or any(not stage["pairs"] for stage in stages)
            or any(_PAIR_NAME.match(row["pair"]) is None
                   for stage in stages for row in stage["pairs"])):
        return _DROP_FLOOR
    return {"stages": stages}


def _floor_allowed(snapshot: dict | None, report_state: str) -> bool:
    """床の数値を出してよいのは、床未達で見送り、かつ report が公開済みのときだけ。"""
    return (snapshot is not None and snapshot["result_code"] == "gate_failed"
            and snapshot["reason_code"] == "unprofitable" and report_state == "published")


def _comparison(value: object) -> dict | None | bool:
    if value is None:
        return None
    if type(value) is not dict or type(value.get("holdout")) is not dict:
        return False
    return {"holdout": value["holdout"]}


# ------------------------------------------------------------ 要約 (summary)

SUMMARY_VERSION = 1
SUMMARY_KINDS = ("indicator", "signal", "strategy")
SUMMARY_ORIGINS = ("new", "update")
SUMMARY_STAGE_STATES = ("measured", "reference", "not_run")
SUMMARY_LIST_MAX = 64
SUMMARY_INDICATORS_MAX = 20
SUMMARY_OBSERVATION_MAX = 505
_WORD = re.compile(r"[a-z_]{1,48}\Z")


def _word(value: object) -> bool:
    return type(value) is str and _WORD.match(value) is not None


def _count_or_none(value: object) -> bool:
    return value is None or _int(value)


def _summary_tried(value: object) -> dict | bool:
    if (type(value) is not dict or not {"plugin", "kind", "origin", "indicators"} <= set(value)
            or plugin_name_or_none(value["plugin"]) is None
            or not (value["kind"] is None or value["kind"] in SUMMARY_KINDS)
            or not (value["origin"] is None or value["origin"] in SUMMARY_ORIGINS)
            or type(value["indicators"]) is not list
            or len(value["indicators"]) > SUMMARY_INDICATORS_MAX
            or any(plugin_name_or_none(name) is None for name in value["indicators"])):
        return False
    return {"plugin": value["plugin"], "kind": value["kind"], "origin": value["origin"],
            "indicators": list(value["indicators"])}


def _summary_pair_row(value: object) -> dict | bool:
    if (type(value) is not dict
            or not {"pair", "trades", "pf", "avg_r", "evaluable", "failed"} <= set(value)
            or type(value["pair"]) is not str or _PAIR_NAME.match(value["pair"]) is None
            or not _count_or_none(value["trades"])
            or not (value["evaluable"] is None or type(value["evaluable"]) is bool)
            or type(value["failed"]) is not list
            or any(not _word(word) for word in value["failed"])):
        return False
    pf, avg_r = _number_or_none(value["pf"]), _number_or_none(value["avg_r"])
    if pf is False or avg_r is False:
        return False
    return {"pair": value["pair"], "trades": value["trades"], "pf": pf, "avg_r": avg_r,
            "evaluable": value["evaluable"], "failed": list(value["failed"])}


def _summary_floor(value: object) -> dict | bool:
    if (type(value) is not dict or not {"failed_stage", "stages"} <= set(value)
            or not (value["failed_stage"] is None or value["failed_stage"] in STAGES)
            or type(value["stages"]) is not list or len(value["stages"]) > len(STAGES)):
        return False
    stages = []
    for stage in value["stages"]:
        if (type(stage) is not dict or stage.get("stage") not in STAGES
                or stage.get("state") not in SUMMARY_STAGE_STATES
                or type(stage.get("pairs")) is not list
                or len(stage["pairs"]) > SUMMARY_LIST_MAX):
            return False
        rows = [_summary_pair_row(row) for row in stage["pairs"]]
        if any(row is False for row in rows):
            return False
        stages.append({"stage": stage["stage"], "state": stage["state"], "pairs": rows})
    order = [STAGES.index(stage["stage"]) for stage in stages]
    if order != sorted(set(order)):
        return False
    return {"failed_stage": value["failed_stage"], "stages": stages}


def _summary_side(value: object) -> dict | bool:
    if (type(value) is not dict or not {"trades", "pf", "avg_r", "evaluable"} <= set(value)
            or not (value["evaluable"] is None or type(value["evaluable"]) is bool)):
        return False
    pf, avg_r = _number_or_none(value["pf"]), _number_or_none(value["avg_r"])
    if pf is False or avg_r is False or not _count_or_none(value["trades"]):
        return False
    return {"trades": value["trades"], "pf": pf, "avg_r": avg_r,
            "evaluable": value["evaluable"]}


def _summary_comparison(value: object) -> dict | bool:
    if (type(value) is not dict or not {"verdict", "reason", "pairs"} <= set(value)
            or not _word(value["verdict"]) or not _word(value["reason"])
            or type(value["pairs"]) is not list or len(value["pairs"]) > SUMMARY_LIST_MAX):
        return False
    pairs = []
    for entry in value["pairs"]:
        if (type(entry) is not dict or type(entry.get("pair")) is not str
                or _PAIR_NAME.match(entry["pair"]) is None):
            return False
        candidate, deployed = _summary_side(entry.get("candidate")), _summary_side(
            entry.get("deployed"))
        if candidate is False or deployed is False:
            return False
        pairs.append({"pair": entry["pair"], "candidate": candidate, "deployed": deployed})
    return {"verdict": value["verdict"], "reason": value["reason"], "pairs": pairs}


def _summary_handoff(value: object) -> dict | bool:
    if (type(value) is not dict or not {"family", "attempts", "outcomes"} <= set(value)
            or type(value["family"]) is not str
            or not 1 <= len(value["family"]) <= FAMILY_KEY_CHARS
            or not _int(value["attempts"]) or type(value["outcomes"]) is not dict
            or len(value["outcomes"]) > SUMMARY_LIST_MAX
            or any(not _word(word) or not _int(count, low=1)
                   for word, count in value["outcomes"].items())):
        return False
    return {"family": value["family"], "attempts": value["attempts"],
            "outcomes": dict(value["outcomes"])}


def _summary_observation(value: object) -> str | bool:
    if type(value) is not str or not 1 <= len(value) <= SUMMARY_OBSERVATION_MAX:
        return False
    return value


def _summary_parent_reason(value: object) -> str | bool:
    return value if _word(value) else False


def _summary(value: object) -> dict | None:
    """要約 (`summary`) の検査。使えるのは version 1・`state=ok`・形が正しいものだけで、
    Discord に出す項目 (`SUMMARY_ITEMS`) だけを写して返す。無い・古い daemon・形の不正・
    未知の version・ok でない要約は None (今の文面に倒す。DTO 全体は捨てない)。"""
    if type(value) is not dict or not {"version", "state", "items"} <= set(value):
        return None
    if (type(value["version"]) is not int or value["version"] != SUMMARY_VERSION
            or value["state"] != "ok" or type(value["items"]) is not dict):
        return None
    items = {}
    for name, item in SUMMARY_ITEMS.items():
        if name not in value["items"]:
            continue
        checked = item.validate(value["items"][name])
        if checked is False:
            return None
        items[name] = checked
    return {"items": items}


def validate_improve_run(data: object, mission_id: int) -> dict | None:
    """応答 DTO が契約の型・null 条件に合えば、使うキーだけの写しを返す。反すれば None。"""
    if type(data) is not dict or not _DTO_KEYS <= set(data):
        return None
    if (not _int(data.get("id"), low=0) or data["id"] != mission_id
            or type(data.get("backend")) is not str or type(data.get("model")) is not str
            or not (data.get("elapsed_sec") is None or _int(data["elapsed_sec"]))
            or data.get("report_state") not in REPORT_STATES):
        return None
    snapshot, approval, floor, comparison = (_snapshot(data.get("snapshot")), _approval(data.get("approval")),
                                              _floor(data.get("floor")), _comparison(data.get("comparison")))
    if snapshot is False or approval is False or floor is False or comparison is False:
        return None
    if floor is _DROP_FLOOR or not _floor_allowed(snapshot, data["report_state"]):
        floor = None
    return {"id": data["id"], "backend": data["backend"], "model": data["model"],
            "elapsed_sec": data["elapsed_sec"], "snapshot": snapshot,
            "report_state": data["report_state"], "approval": approval, "floor": floor,
            "comparison": comparison, "summary": _summary(data.get("summary"))}


# ------------------------------------------------------------ 文面

def _heading(mission_id: int, result_code: object, reason_code: object,
             report_state: object = "none") -> str:
    if type(result_code) is not str or result_code not in IMPROVE_BASE_WORDS:
        return IMPROVE_UNKNOWN_HEADING.format(id=mission_id)
    if result_code == "report" and report_state == "failed":
        return IMPROVE_REPORT_FAILED_HEADING.format(id=mission_id)
    known = IMPROVE_HEADINGS.get((result_code, reason_code))
    if known is not None:
        return known.format(id=mission_id)
    text = f"[改善] {IMPROVE_BASE_WORDS[result_code]} #{mission_id}"
    if reason_code is not None:
        text += f" ({escape_discord(reason_code)})"
    return text


def format_elapsed(seconds: int) -> str:
    """整数秒を `<m>分<ss>秒` に、60 分以上は `<h>時間<mm>分<ss>秒` にする。"""
    minutes, sec = divmod(seconds, 60)
    if seconds < _ONE_HOUR:
        return f"{minutes}分{sec:02d}秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}時間{minutes:02d}分{sec:02d}秒"


def _last_line(elapsed: int | None, tool_calls: int | None) -> str:
    tool = "tool -" if tool_calls is None else f"tool {tool_calls} 回"
    if elapsed is None:
        return "所要 -" if tool_calls is None else tool
    return f"所要 {format_elapsed(elapsed)} / {tool}"


def _repeat_line(repeat: object) -> str | None:
    if type(repeat) is not dict:
        return None
    run, maximum = repeat.get("run"), repeat.get("max_runs")
    elapsed, duration = repeat.get("elapsed_sec"), repeat.get("duration_sec")
    if (type(run) is not int or type(maximum) is not int or type(elapsed) is not int
            or type(duration) is not int or run < 1 or run > maximum or elapsed < 0
            or duration < 60):
        return None
    return f"繰り返し {run}/{maximum} 回 · {_repeat_span(elapsed // 60)}/{_repeat_span(duration // 60)}"


def _repeat_span(minutes: int) -> str:
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours > 0 else f"{minutes}m"


def _event_repeat(payload: Mapping[str, object]) -> dict | None:
    values = (payload.get("repeat_run"), payload.get("repeat_max_runs"),
              payload.get("repeat_elapsed_sec"), payload.get("repeat_duration_sec"))
    if any(type(value) is not int for value in values):
        return None
    return {"run": values[0], "max_runs": values[1], "elapsed_sec": values[2],
            "duration_sec": values[3]}


def _pf(value: float | None) -> str:
    text = "-" if value is None else f"{value:.2f}"
    return "0.00" if text == "-0.00" else text


def _avg_r(value: float | None) -> str:
    text = "-" if value is None else f"{value:+.2f}"
    return "+0.00" if text == "-0.00" else text


def _floor_lines(floor: dict) -> list[str]:
    stages = floor["stages"]
    lines = ["落ちた段: " + ", ".join(STAGE_LABELS[stage["stage"]] for stage in stages)]
    for stage in stages:
        label = STAGE_LABELS[stage["stage"]]
        pairs = stage["pairs"]
        for row in pairs[:MAX_PAIR_LINES]:
            line = (f"{label}: {cut_escaped(row['pair'], FIELD_CHARS)} pf {_pf(row['pf'])} "
                    f"avg_r {_avg_r(row['avg_r'])} 取引 {row['trades']}")
            conditions = row.get("conditions", ())
            suffix = "" if row["evaluable"] else " (評価不能)"
            rendered = [CONDITION_LABELS[kind] for kind in conditions if kind in CONDITION_LABELS]
            if rendered:
                suffix += " (" + "、".join(rendered) + ")"
            lines.append(line + suffix)
        if len(pairs) > MAX_PAIR_LINES:
            lines.append(f"ほか {len(pairs) - MAX_PAIR_LINES} 本")
    return lines


def _comparison_lines(comparison: dict) -> list[str]:
    holdout = comparison["holdout"]
    lines = ["配備中の版を上回りました"]
    for pair, entry in list(holdout.items())[:MAX_PAIR_LINES]:
        if type(pair) is not str or type(entry) is not dict:
            return []
        for label, key in (("候補", "candidate"), ("配備中の版", "deployed")):
            metrics = entry.get(key)
            if type(metrics) is not dict:
                return []
            pf, avg_r = _number_or_none(metrics.get("pf")), _number_or_none(metrics.get("avg_r"))
            trades, evaluable = metrics.get("trades"), metrics.get("evaluable")
            if pf is False or avg_r is False or not _int(trades) or type(evaluable) is not bool:
                return []
            lines.append(f"{cut_escaped(pair, FIELD_CHARS)} {label}: pf {_pf(pf)} "
                         f"avg_r {_avg_r(avg_r)} 取引 {trades} "
                         f"評価可能 {'はい' if evaluable else 'いいえ'}")
    if len(holdout) > MAX_PAIR_LINES:
        lines.append(f"ほか {len(holdout) - MAX_PAIR_LINES} 本")
    return lines

# ------------------------------------------------------------ 要約から組む内容行

KIND_ORIGIN_LABELS = {"new": "新設", "update": "更新"}
COMPARISON_REASON_LABELS = {
    "holdout_exceeds": "holdout で上回った",
    "holdout_not_exceeds": "holdout で上回らなかった",
    "baseline_not_evaluable": "配備中の版が評価不能",
    "candidate_not_evaluable": "候補が評価不能",
    "no_pairs": "比べられる pair がない",
    "gate_timeout": "比較が時間内に終わらなかった",
    "baseline_unhealthy": "配備中の版の状態が不良",
}
_NO_FLOOR_REASONS = frozenset({"not_improved", "same_name_contract_changed"})

# 削る段 (本文が上限を超えたとき、小さい番号から順に落とす)。
LEVEL_HANDOFF = 1
LEVEL_PAIR_ROWS = 2
LEVEL_INDICATORS = 3


@dataclass(frozen=True)
class _Ctx:
    key: tuple
    detail: dict
    level: int


def _tried_lines(tried: dict, ctx: _Ctx) -> list[str]:
    parts = []
    origin = KIND_ORIGIN_LABELS.get(tried["origin"])
    approval = ctx.detail["approval"]
    if ctx.key == ("submitted", None) and approval is not None:
        # plugin 名と kind は承認の行が出している。ここでは重ねない。
        if origin is not None:
            parts.append(origin)
    else:
        name = cut_escaped(tried["plugin"], FIELD_CHARS)
        head = name if tried["kind"] is None else f"{tried['kind']} {name}"
        parts.append(head if origin is None else f"{head} ({origin})")
    if tried["indicators"] and ctx.level < LEVEL_INDICATORS:
        names = ", ".join(cut_escaped(name, FIELD_CHARS) for name in tried["indicators"])
        parts.append(f"indicator: {names}")
    return ["試したもの: " + "、".join(parts)] if parts else []


def _summary_pair_line(label: str, row: dict) -> str:
    trades = "-" if row["trades"] is None else row["trades"]
    line = (f"{label}: {cut_escaped(row['pair'], FIELD_CHARS)} pf {_pf(row['pf'])} "
            f"avg_r {_avg_r(row['avg_r'])} 取引 {trades}")
    failed = row["failed"]
    if row["evaluable"] is False or "holdout_not_evaluable" in failed:
        line += " (評価不能)"
    rendered = [CONDITION_LABELS[kind] for kind in FLOOR_CONDITION_KINDS if kind in failed]
    if rendered:
        line += " (" + "、".join(rendered) + ")"
    return line


def _row_lines(label: str, rows: list[dict]) -> list[str]:
    lines = [_summary_pair_line(label, row) for row in rows[:MAX_PAIR_LINES]]
    if len(rows) > MAX_PAIR_LINES:
        lines.append(f"ほか {len(rows) - MAX_PAIR_LINES} 本")
    return lines


def _floor_item_lines(floor: dict, ctx: _Ctx) -> list[str]:
    """落ちた段の、落ちた pair の行だけを出す。通った段の値は出さない。holdout の行は
    holdout で落ちた pair のものだけ。評価不能で成績が無い段は in-sample の参考値と、
    実施していない段の「未実施」を添える。"""
    if (ctx.key[0] != "gate_failed" or ctx.key[1] in _NO_FLOOR_REASONS
            or ctx.detail["report_state"] != "published"):
        return []
    stages = floor["stages"]
    failing = [(stage, [row for row in stage["pairs"] if row["failed"]]) for stage in stages]
    failing = [(stage, rows) for stage, rows in failing if rows]
    lines = []
    if failing:
        lines.append("落ちた段: " + ", ".join(STAGE_LABELS[stage["stage"]]
                                              for stage, _rows in failing))
    if ctx.level < LEVEL_PAIR_ROWS:
        for stage, rows in failing:
            label = STAGE_LABELS[stage["stage"]]
            if stage["state"] == "reference":
                label += " 参考値"
            lines.extend(_row_lines(label, rows))
        if not failing:
            for stage in stages:
                if stage["stage"] == "in_sample" and stage["state"] == "reference":
                    lines.extend(_row_lines(STAGE_LABELS["in_sample"] + " 参考値",
                                            stage["pairs"]))
    for stage in stages:
        if stage["state"] == "not_run":
            lines.append(f"{STAGE_LABELS[stage['stage']]}: 未実施")
    return lines


def _comparison_item_lines(comparison: dict, ctx: _Ctx) -> list[str]:
    """提出したときの holdout の比較値と、見送ったときの理由 (固定語)。見送りの行には
    数値を出さない。"""
    if ctx.key == ("submitted", None):
        if comparison["verdict"] != "improved" or ctx.detail["approval"] is None:
            return []
        pairs = comparison["pairs"]
        lines = ["配備中の版を上回りました"]
        for entry in pairs[:MAX_PAIR_LINES]:
            for label, side in (("候補", entry["candidate"]), ("配備中の版", entry["deployed"])):
                trades = "-" if side["trades"] is None else side["trades"]
                suffix = ("" if side["evaluable"] is None
                          else f" 評価可能 {'はい' if side['evaluable'] else 'いいえ'}")
                lines.append(f"{cut_escaped(entry['pair'], FIELD_CHARS)} {label}: "
                             f"pf {_pf(side['pf'])} avg_r {_avg_r(side['avg_r'])} 取引 {trades}"
                             + suffix)
        if len(pairs) > MAX_PAIR_LINES:
            lines.append(f"ほか {len(pairs) - MAX_PAIR_LINES} 本")
        return lines
    if ctx.key == ("gate_failed", "not_improved"):
        reason = comparison["reason"]
        return ["比較結果: " + COMPARISON_REASON_LABELS.get(reason, cut_escaped(reason, 48))]
    return []


def _observation_item_lines(observation: str, ctx: _Ctx) -> list[str]:
    # 観察文は agent の申告があるときだけ。`not_submitted` などでは出さない。
    if ctx.key != ("observation", None):
        return []
    return [f"agent の申告: {cut_escaped(observation, IMPROVE_OBSERVATION_CHARS) or NO_NOTE}"]


def _parent_reason_lines(reason: str, ctx: _Ctx) -> list[str]:
    # 見出しが同じ固定語を出している結末では重ねない。
    if reason == ctx.key[1]:
        return []
    return [f"親の判定: {cut_escaped(reason, 48)}"]


def _handoff_lines(handoff: dict, ctx: _Ctx) -> list[str]:
    if ctx.level >= LEVEL_HANDOFF:
        return []
    outcomes = "、".join(f"{cut_escaped(word, 48)} {count}"
                         for word, count in handoff["outcomes"].items())
    tail = f" ({outcomes})" if outcomes else ""
    return [f"申し送り (終端時点): 族 {cut_escaped(handoff['family'], FAMILY_DISPLAY_CHARS)} — "
            f"試行 {handoff['attempts']}{tail}"]


@dataclass(frozen=True)
class SummaryItem:
    validate: object
    render: object


# Discord に出す要約の項目は、この表に載っているものだけ (表の順に出す)。仮説
# (`hypothesis`) と agent の説明 (`explanation`) は意図して載せていない。検査も描画も
# この表を引くので、足すときはここと通知の設計書を同時に変える。
SUMMARY_ITEMS = {
    "tried": SummaryItem(_summary_tried, _tried_lines),
    "floor": SummaryItem(_summary_floor, _floor_item_lines),
    "comparison": SummaryItem(_summary_comparison, _comparison_item_lines),
    "observation": SummaryItem(_summary_observation, _observation_item_lines),
    "parent_reason": SummaryItem(_summary_parent_reason, _parent_reason_lines),
    "handoff": SummaryItem(_summary_handoff, _handoff_lines),
}


def _lead_lines(snapshot: dict, detail: dict) -> list[str]:
    """要約で置き換えない行: 承認番号と固定の案内文。"""
    key = (snapshot["result_code"], snapshot["reason_code"])
    if key == ("submitted", None):
        approval = detail["approval"]
        if approval is None:
            return []
        plugin, kind = approval["plugin"], approval["kind"]
        head = ""
        if plugin is not None:
            head = f"plugin {cut_escaped(plugin, FIELD_CHARS)}"
            if kind:
                head += f" ({cut_escaped(kind, FIELD_CHARS)})"
            head += " / "
        return [f"{head}承認 #{approval['id']}"]
    return {
        ("observation", "not_submitted"): [_NOT_SUBMITTED_LINE],
        ("observation", "demoted_recovered"): [_DEMOTED_LINE],
        ("gate_failed", "not_improved"): ["配備中の版を上回らなかったため見送ります"],
        ("gate_failed", "same_name_contract_changed"): ["意味が変わる変更は新しい名前で提出する"],
    }.get(key, [])


def _summary_lines(snapshot: dict, detail: dict, summary: dict, level: int) -> list[str]:
    key = (snapshot["result_code"], snapshot["reason_code"])
    ctx = _Ctx(key, detail, level)
    lines = _lead_lines(snapshot, detail)
    items = summary["items"]
    for name, item in SUMMARY_ITEMS.items():
        if name in items:
            lines.extend(item.render(items[name], ctx))
    if key == ("observation", None) and "observation" not in items:
        lines.append(f"agent の申告: {NO_NOTE}")
    return lines


def _body_lines(snapshot: dict, detail: dict) -> list[str]:
    key = (snapshot["result_code"], snapshot["reason_code"])
    if key == ("submitted", None):
        approval = detail["approval"]
        if approval is None:
            return []
        plugin, kind = approval["plugin"], approval["kind"]
        head = ""
        if plugin is not None:
            head = f"plugin {cut_escaped(plugin, FIELD_CHARS)}"
            if kind:
                head += f" ({cut_escaped(kind, FIELD_CHARS)})"
            head += " / "
        lines = [f"{head}承認 #{approval['id']}"]
        if detail["comparison"] is not None:
            lines += _comparison_lines(detail["comparison"])
        return lines
    if key == ("observation", None):
        note = cut_escaped(snapshot["observation_text"], IMPROVE_OBSERVATION_CHARS)
        return [f"agent の申告: {note or NO_NOTE}"]
    if key == ("observation", "not_submitted"):
        return [_NOT_SUBMITTED_LINE]
    if key == ("observation", "demoted_recovered"):
        return [_DEMOTED_LINE]
    if key == ("gate_failed", "unprofitable") and detail["floor"] is not None:
        return _floor_lines(detail["floor"])
    if key == ("gate_failed", "not_improved"):
        return ["配備中の版を上回らなかったため見送ります"]
    if key == ("gate_failed", "same_name_contract_changed"):
        return ["意味が変わる変更は新しい名前で提出する"]
    return []


def _fit(lines: list[str]) -> str:
    """Discord の本文上限に収める。超えるときは見出し (1 行目) と固定の省略行だけにする。"""
    text = "\n".join(lines)
    if len(text) <= IMPROVE_BODY_LIMIT:
        return text
    return f"{lines[0]}\n{OMITTED_LINE}"


def render_improve_start(payload: Mapping[str, object], detail: dict | None) -> str:
    """開始通知。detail (検査済みの応答) があれば backend / model の行を添える。"""
    head = IMPROVE_START_HEADING.format(id=mission_id_of(payload),
                                        trigger=TRIGGER_LABELS[payload["trigger"]])
    if detail is None:
        return head
    backend = cut_escaped(detail["backend"], FIELD_CHARS)
    model = cut_escaped(detail["model"], FIELD_CHARS)
    lines = [head, f"backend {backend} / {model}"]
    repeat = _repeat_line(_event_repeat(payload))
    if repeat is not None:
        lines.append(repeat)
    return _fit(lines)


def render_improve_finish(payload: Mapping[str, object], detail: dict | None) -> str:
    """終了通知。detail が None (取得失敗・検査違反・snapshot なし) なら event の固定 field だけ。
    有効な要約があるときは内容行を要約から組む (今の内容行は使わない)。"""
    mission_id = mission_id_of(payload)
    snapshot = None if detail is None else detail["snapshot"]
    if snapshot is None:
        lines = [_heading(mission_id, payload["result_code"], payload["reason_code"])]
        if payload["tool_calls"] is not None:
            lines.append(f"tool {payload['tool_calls']} 回")
        lines.append(UNAVAILABLE_LINE)
        return "\n".join(lines)
    heading = _heading(mission_id, snapshot["result_code"], snapshot["reason_code"],
                       detail["report_state"])
    repeat = _repeat_line(_event_repeat(payload))
    tail = ([repeat] if repeat is not None else []) + [
        _last_line(detail["elapsed_sec"], snapshot["tool_calls"])]
    summary = detail.get("summary")
    if summary is None:
        return _fit([heading, *_body_lines(snapshot, detail), *tail])
    for level in range(LEVEL_INDICATORS + 1):
        lines = [heading, *_summary_lines(snapshot, detail, summary, level), *tail]
        text = "\n".join(lines)
        if len(text) <= IMPROVE_BODY_LIMIT:
            return text
    return f"{heading}\n{OMITTED_LINE}"
