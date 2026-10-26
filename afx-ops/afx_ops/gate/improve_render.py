"""改善 mission の開始・終了通知の文面と、詳細 (GET の応答) の検査。

見出しは `render.IMPROVE_HEADINGS` を `(result_code, reason_code)` と `report_state` だけで引く。
変数部分 (観察文・reason・pair・backend / model・plugin 名・kind) は全て escape し、固定語と
数字は escape しない。詳細が無い (取得できなかった・検査に反した) ときは event の固定 field
だけから作る。設定済みの mention は付けない (呼び側は allowed_mentions を none にする)。
"""
from __future__ import annotations

import math
from collections.abc import Mapping

from afx_ops import improve_events

from .render import (IMPROVE_BASE_WORDS, IMPROVE_HEADINGS, IMPROVE_OBSERVATION_CHARS,
                     IMPROVE_REPORT_FAILED_HEADING, IMPROVE_START_HEADING,
                     IMPROVE_UNKNOWN_HEADING, cut_escaped, escape_discord, plugin_name_or_none)

_DTO_KEYS = frozenset({"id", "backend", "model", "elapsed_sec", "snapshot", "report_state",
                      "approval", "floor"})
REPORT_STATES = frozenset({"none", "prepared", "published", "failed"})
STAGES = ("in_sample", "holdout")
STAGE_LABELS = {"in_sample": "in-sample", "holdout": "holdout"}
TRIGGER_LABELS = {"manual": "手動", "scheduled": "週次"}
MAX_PAIR_LINES = 3
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
    return {"pair": value["pair"], "trades": value["trades"], "pf": pf, "avg_r": avg_r,
            "evaluable": value["evaluable"]}


def _floor(value: object) -> dict | None | bool:
    if value is None:
        return None
    if type(value) is not dict or type(value.get("stages")) is not list or not value["stages"]:
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
    if len({stage["stage"] for stage in stages}) != len(stages):
        return False
    return {"stages": stages}


def validate_improve_run(data: object, mission_id: int) -> dict | None:
    """応答 DTO が契約の型・null 条件に合えば、使うキーだけの写しを返す。反すれば None。"""
    if type(data) is not dict or not _DTO_KEYS <= set(data):
        return None
    if (not _int(data.get("id"), low=0) or data["id"] != mission_id
            or type(data.get("backend")) is not str or type(data.get("model")) is not str
            or not (data.get("elapsed_sec") is None or _int(data["elapsed_sec"]))
            or data.get("report_state") not in REPORT_STATES):
        return None
    snapshot, approval, floor = (_snapshot(data.get("snapshot")), _approval(data.get("approval")),
                                 _floor(data.get("floor")))
    if snapshot is False or approval is False or floor is False:
        return None
    return {"id": data["id"], "backend": data["backend"], "model": data["model"],
            "elapsed_sec": data["elapsed_sec"], "snapshot": snapshot,
            "report_state": data["report_state"], "approval": approval, "floor": floor}


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
            line = (f"{label}: {escape_discord(row['pair'])} pf {_pf(row['pf'])} "
                    f"avg_r {_avg_r(row['avg_r'])} 取引 {row['trades']}")
            lines.append(line + ("" if row["evaluable"] else " (評価不能)"))
        if len(pairs) > MAX_PAIR_LINES:
            lines.append(f"ほか {len(pairs) - MAX_PAIR_LINES} 本")
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
            head = f"plugin {escape_discord(plugin)}"
            if kind:
                head += f" ({escape_discord(kind)})"
            head += " / "
        return [f"{head}承認 #{approval['id']}"]
    if key == ("observation", None):
        note = cut_escaped(snapshot["observation_text"], IMPROVE_OBSERVATION_CHARS)
        return [f"agent の申告: {note or NO_NOTE}"]
    if key == ("observation", "not_submitted"):
        return [_NOT_SUBMITTED_LINE]
    if key == ("observation", "demoted_recovered"):
        return [_DEMOTED_LINE]
    if key == ("gate_failed", "unprofitable") and detail["floor"] is not None:
        return _floor_lines(detail["floor"])
    return []


def render_improve_start(payload: Mapping[str, object], detail: dict | None) -> str:
    """開始通知。detail (検査済みの応答) があれば backend / model の行を添える。"""
    head = IMPROVE_START_HEADING.format(id=mission_id_of(payload),
                                        trigger=TRIGGER_LABELS[payload["trigger"]])
    if detail is None:
        return head
    return f"{head}\nbackend {escape_discord(detail['backend'])} / {escape_discord(detail['model'])}"


def render_improve_finish(payload: Mapping[str, object], detail: dict | None) -> str:
    """終了通知。detail が None (取得失敗・検査違反・snapshot なし) なら event の固定 field だけ。"""
    mission_id = mission_id_of(payload)
    snapshot = None if detail is None else detail["snapshot"]
    if snapshot is None:
        lines = [_heading(mission_id, payload["result_code"], payload["reason_code"])]
        if payload["tool_calls"] is not None:
            lines.append(f"tool {payload['tool_calls']} 回")
        lines.append(UNAVAILABLE_LINE)
        return "\n".join(lines)
    lines = [_heading(mission_id, snapshot["result_code"], snapshot["reason_code"],
                      detail["report_state"])]
    lines.extend(_body_lines(snapshot, detail))
    lines.append(_last_line(detail["elapsed_sec"], snapshot["tool_calls"]))
    return "\n".join(lines)
