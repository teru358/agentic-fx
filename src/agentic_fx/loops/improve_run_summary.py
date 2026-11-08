"""改善 mission 1 回ごとの要約: 材料の入れ物・組み立て・形の検査。

終端の各経路が commit の途中で `SummaryMaterial` に材料を書き足し、
終端の直前に `prepare` が経路ごとの `SummaryPlan` に従って項目へ整える。
申し送りだけは終端 tx の中で数えるので `close` が足す。保存する中身は
数値・固定語・長さを切った引用文字列だけで、agent の文章は
`safe_text` → `display_text` → 上限を通す。

出力する `items` の形は `ITEMS_SCHEMA` が正本で、組み立て側と API 側が
同じ検査を通る。要約も `output_json` も、改善 mission の prompt には
入らない (この module は prompt を作る側から import されない)。"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

import jsonschema

from agentic_fx._safe_error import safe_text
from agentic_fx import improve_notice_codes as notice_codes
from agentic_fx.loops.approval_facts import TRUNCATION_SUFFIX, display_text
from agentic_fx.loops.summary import IMPROVE_OUTPUT_SCHEMA
from agentic_fx.plugin.logic_identity import FAMILY_KEY_MAX_BYTES
from agentic_fx.plugin.strategy_gate import _floor_fail_items, trades_per_eval_bar
from agentic_fx.store import improve_run_summaries as summaries_store
from agentic_fx.store.improve_run_summaries import (
    STATE_BUILD_FAILED, STATE_EMPTY, STATE_OK, SummaryRecord)

_log = logging.getLogger("agentic_fx.loops.improve_run_summary")

TEXT_LIMIT = 500
INDICATOR_NAMES_MAX = 20
NAME_LIMIT = 64
PAIR_ROWS_MAX = 64
SUMMARY_MAX_BYTES = 16 * 1024
OUTPUT_JSON_MAX_BYTES = 64 * 1024

_TEXT_MAX_LENGTH = TEXT_LIMIT + len(TRUNCATION_SUFFIX)
FAIL_KINDS = ("pf", "avg_r", "holdout_not_evaluable", "drawdown_too_deep",
              "too_frequent")
COMPARISON_VERDICTS = ("improved", "not_improved", "unavailable")
COMPARISON_REASONS = (
    "holdout_exceeds", "holdout_not_exceeds", "baseline_not_evaluable",
    "candidate_not_evaluable", "no_pairs", "gate_timeout", "baseline_unhealthy")
COMPARISON_MODES = ("shadow", "enforce")
COMPARED_METRICS = ("pf", "avg_r")
PARENT_REASONS = frozenset(
    reason for _result, reason in notice_codes.ALLOWED_PAIRS
    if reason is not None)
STAGE_STATES = ("measured", "reference", "not_run")


def _nullable(kind: str) -> dict:
    return {"type": [kind, "null"]}


_NUMBER = {"type": ["number", "null"]}
_TEXT = {"type": "string", "minLength": 1, "maxLength": _TEXT_MAX_LENGTH}
_NAME = {"type": "string", "minLength": 1, "maxLength": NAME_LIMIT}
_FAMILY = {"type": "string", "minLength": 1, "maxLength": FAMILY_KEY_MAX_BYTES}


def _object(properties: dict, required: Iterable[str] | None = None) -> dict:
    return {"type": "object", "additionalProperties": False,
            "required": list(properties if required is None else required),
            "properties": properties}


_PAIR_ROW = _object({
    "pair": _NAME,
    "trades": {"type": ["integer", "null"], "minimum": 0},
    "pf": _NUMBER, "avg_r": _NUMBER, "max_drawdown": _NUMBER,
    "trades_per_eval_bar": _NUMBER,
    "evaluable": _nullable("boolean"),
    "failed": {"type": "array", "maxItems": len(FAIL_KINDS),
               "items": {"enum": list(FAIL_KINDS)}}})
_STAGE = _object({
    "stage": {"enum": ["in_sample", "holdout"]},
    "state": {"enum": list(STAGE_STATES)},
    "pairs": {"type": "array", "maxItems": PAIR_ROWS_MAX, "items": _PAIR_ROW}})
_SIDE = _object({
    "trades": {"type": ["integer", "null"], "minimum": 0},
    "pf": _NUMBER, "avg_r": _NUMBER, "evaluable": _nullable("boolean")})
_COMPARISON_PAIR = _object({
    "pair": _NAME, "verdict": {"enum": list(COMPARISON_VERDICTS)},
    "candidate": _SIDE, "deployed": _SIDE})

ITEMS_SCHEMA: dict = _object({
    "hypothesis": _object({
        "idea": _TEXT, "backlog_id": {"type": ["integer", "null"]}}),
    "explanation": _object({
        "rationale": {"anyOf": [_TEXT, {"type": "null"}]},
        "artifact_summary": {"anyOf": [_TEXT, {"type": "null"}]}}),
    "observation": _TEXT,
    "parent_reason": {"enum": sorted(PARENT_REASONS)},
    "tried": _object({
        "plugin": _NAME,
        "kind": {"enum": ["indicator", "signal", "strategy", None]},
        "origin": {"enum": ["new", "update", None]},
        "indicators": {"type": "array", "maxItems": INDICATOR_NAMES_MAX,
                       "items": _NAME}}),
    "floor": _object({
        "thresholds": _object({
            "min_pf": _NUMBER,
            "require_positive_avg_r": _nullable("boolean"),
            "require_holdout_evaluable": _nullable("boolean"),
            "max_drawdown": _NUMBER, "max_trades_per_eval_bar": _NUMBER}),
        "failed_stage": {"enum": ["in_sample", "holdout", None]},
        "failed_conditions": {"type": "array", "maxItems": len(FAIL_KINDS),
                              "items": {"enum": list(FAIL_KINDS)}},
        "failed_pair_count": {"type": "integer", "minimum": 0},
        "stages": {"type": "array", "maxItems": 2, "items": _STAGE}}),
    "comparison": _object({
        "mode": {"enum": list(COMPARISON_MODES)},
        "verdict": {"enum": list(COMPARISON_VERDICTS)},
        "reason": {"enum": list(COMPARISON_REASONS)},
        "stage": {"enum": ["holdout"]},
        "metrics": {"type": "array", "maxItems": len(COMPARED_METRICS),
                    "items": {"enum": list(COMPARED_METRICS)}},
        "pairs": {"type": "array", "maxItems": PAIR_ROWS_MAX,
                  "items": _COMPARISON_PAIR}}),
    "handoff": _object({
        "family": _FAMILY,
        "attempts": {"type": "integer", "minimum": 0},
        "outcomes": {"type": "object", "additionalProperties": False,
                     "properties": {}, "patternProperties": {
                         "^[a-z_]{1,32}$": {"type": "integer", "minimum": 1}}}}),
}, required=())

# 項目を落とす順 (16 KiB を超えたとき、先に落とすものから)。
_DROP_ORDER = ("explanation", "handoff", "floor.pairs", "tried.indicators")


# ---------------------------------------------------------------- 材料

@dataclass
class StageMaterial:
    stage: str
    state: str
    pairs: dict[str, dict]
    failed: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class FloorMaterial:
    thresholds: dict
    stages: list[StageMaterial]


@dataclass
class CandidateMeta:
    kind: str | None
    is_new: bool | None
    indicators: tuple[str, ...]


@dataclass
class SummaryMaterial:
    """mission 1 回ぶんの材料入れ。`ImproveRunContext` が mission ごとに
    1 つ持ち、commit の各段が書き足す。値は表示用に整える前の生の値で、
    `prepare` が経路ごとの `SummaryPlan` に従って項目にする。"""
    output: dict | None = None
    plugin_name: str | None = None
    candidate: CandidateMeta | None = None
    floor: FloorMaterial | None = None
    comparison: dict | None = None
    observation_text: str | None = None
    parent_reason: str | None = None


@dataclass(frozen=True)
class SummaryPlan:
    """終端経路ごとに、要約が持ってよい項目。"""
    claims: Literal["none", "hypothesis", "full"] = "none"
    tried: Literal["none", "name", "full"] = "none"
    floor: bool = False
    comparison: bool = False
    observation: bool = False
    parent_reason: bool = False
    handoff: bool = False


PLAN_NONE = SummaryPlan()
PLAN_COMMIT_CRASH = SummaryPlan(claims="full", tried="name")
PLAN_SUBMITTED = SummaryPlan(
    claims="full", tried="full", floor=True, comparison=True, handoff=True)
PLAN_REPORT = SummaryPlan(claims="full", observation=True)
PLAN_DEMOTED = SummaryPlan(
    claims="full", tried="full", floor=True, observation=True,
    parent_reason=True)
PLAN_CLAIMS = SummaryPlan(claims="full")
PLAN_LOSER = SummaryPlan(claims="hypothesis")
PLAN_GATE_FAILED = SummaryPlan(
    claims="full", tried="full", floor=True, comparison=True, handoff=True)


# ---------------------------------------------------------------- 整形

def agent_text(value: object) -> str | None:
    """agent の文章を保存してよい形にする。`safe_text` で URL と秘密の
    パラメータを落とし、`display_text` で 1 行に畳んで上限で切る。
    文字列でない・空は None。"""
    if not isinstance(value, str):
        return None
    text = display_text(safe_text(value), limit=TEXT_LIMIT)
    return None if text == "-" else text


def _number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float):
        return round(value, 6) if math.isfinite(value) else None
    return value


def _count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _flag(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _name(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value if len(value) <= NAME_LIMIT else None


def _family_name(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value if len(value.encode("utf-8")) <= FAMILY_KEY_MAX_BYTES else None


def _plugin_name(value: object) -> str | None:
    from agentic_fx.plugin import loader as plugin_loader
    if (isinstance(value, str)
            and plugin_loader._PLUGIN_NAME_RE.fullmatch(value) is not None):
        return value
    return None


def output_for_storage(output: object) -> dict | None:
    """`missions.output_json` に入れる agent の元の出力。辞書でなければ
    (出力が無い経路) None。直列化した大きさが 64 KiB を超えるときは、
    本文を保存せず大きさだけを記す。"""
    if not isinstance(output, dict):
        return None
    try:
        size = len(json.dumps(output, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError, RecursionError):
        _log.warning("improve output is not serializable", exc_info=True)
        return None
    if size > OUTPUT_JSON_MAX_BYTES:
        return {"truncated": True, "bytes": size}
    return output


# ---------------------------------------------------------------- 材料の作り方

def candidate_meta(meta: Any, *, deployed_names: Iterable[str]
                   ) -> CandidateMeta:
    """`plugin gate` を通ったあとに loader が返した meta から、試したものの
    材料を作る。新設か更新かは mission 開始時の配備の集合で決める。
    使った indicator の名前は strategy の依存だけ。"""
    kind = getattr(meta, "kind", None)
    indicators: list[str] = []
    if kind == "strategy":
        for ref in getattr(meta, "indicators", ()) or ():
            plugin = _plugin_name(getattr(ref, "plugin", None))
            if plugin is not None and plugin not in indicators:
                indicators.append(plugin)
    name = getattr(meta, "name", None)
    return CandidateMeta(
        kind=kind if kind in ("indicator", "signal", "strategy") else None,
        is_new=(name not in set(deployed_names)
                if isinstance(name, str) else None),
        indicators=tuple(sorted(indicators)))


def floor_material(settings: Any, *, in_sample: dict | None,
                   holdout: dict | None,
                   rows: Iterable[dict] | None = None) -> FloorMaterial | None:
    """床との差の材料。`in_sample` / `holdout` は gate が判定に使った
    pair→指標 (候補の値だけ)。評価不能で verdict が成績を持たないときは、
    候補の計測行 `rows` (variant が candidate のものだけ) を「参考値」に
    する。holdout を回していなければ「未実施」。"""
    thresholds = settings.improve.gate.snapshot()
    stages: list[StageMaterial] = []
    if in_sample:
        stages.append(_measured_stage(settings, "in_sample", in_sample))
        if holdout is None:
            stages.append(StageMaterial("holdout", "not_run", {}))
        else:
            stages.append(_measured_stage(settings, "holdout", holdout))
        return FloorMaterial(thresholds, stages)
    reference: dict[str, dict[str, dict]] = {"in_sample": {}, "holdout": {}}
    for row in rows or ():
        if row.get("variant", "candidate") != "candidate":
            continue
        scope = {"in_sample": "in_sample",
                 "holdout_gate": "holdout"}.get(row.get("scope"))
        metrics = row.get("metrics")
        if scope is None or not isinstance(metrics, dict) \
                or not isinstance(row.get("pair"), str):
            continue
        reference[scope][row["pair"]] = metrics
    for scope in ("in_sample", "holdout"):
        if reference[scope]:
            stages.append(StageMaterial(scope, "reference", reference[scope]))
        else:
            stages.append(StageMaterial(scope, "not_run", {}))
    if all(stage.state == "not_run" for stage in stages):
        return None
    return FloorMaterial(thresholds, stages)


def _measured_stage(settings: Any, scope: str, per_pair: dict
                    ) -> StageMaterial:
    failed: dict[str, list[str]] = {}
    for item in _floor_fail_items(per_pair, settings=settings, scope=scope):
        failed.setdefault(item.pair, []).append(item.kind)
    return StageMaterial(scope, "measured", dict(per_pair), failed)


def comparison_material(comparison: object) -> dict | None:
    """配備中の版との比較の材料 (`StrategyGateVerdict.comparison`)。"""
    return comparison if isinstance(comparison, dict) else None


# ---------------------------------------------------------------- 組み立て

@dataclass
class PreparedSummary:
    """tx の前に整えた項目。整形に失敗したときは `failed`。"""
    items: dict
    failed: bool = False
    output: dict | None = None


def _claims(output: object, level: str) -> dict:
    if level == "none" or not isinstance(output, dict):
        return {}
    try:
        jsonschema.validate(output, IMPROVE_OUTPUT_SCHEMA)
    except jsonschema.ValidationError:
        return {}
    selected = output["selected"]
    idea = agent_text(selected.get("idea"))
    items: dict = {}
    if idea is not None:
        backlog_id = selected.get("backlog_id")
        items["hypothesis"] = {
            "idea": idea,
            "backlog_id": backlog_id if type(backlog_id) is int else None}
    if level == "full":
        artifact = output["artifact"]
        rationale = agent_text(output.get("selection_rationale"))
        artifact_summary = (agent_text(artifact.get("summary"))
                            if artifact.get("type") == "plugin" else None)
        if rationale is not None or artifact_summary is not None:
            items["explanation"] = {
                "rationale": rationale, "artifact_summary": artifact_summary}
    return items


def _pair_row(pair: str, metrics: dict, failed: list[str]) -> dict:
    trades = _count(metrics.get("trades"))
    return {
        "pair": pair, "trades": trades, "pf": _number(metrics.get("pf")),
        "avg_r": _number(metrics.get("avg_r")),
        "max_drawdown": _number(metrics.get("max_drawdown")),
        "trades_per_eval_bar": _number(
            trades_per_eval_bar(metrics.get("trades"), metrics.get("eval_bars"))),
        "evaluable": _flag(metrics.get("evaluable")),
        "failed": [kind for kind in FAIL_KINDS if kind in failed]}


def _floor_item(material: FloorMaterial, allowed_pairs: list[str]) -> dict:
    limit = min(len(allowed_pairs), PAIR_ROWS_MAX)
    thresholds = material.thresholds
    stages = []
    failed_stage = None
    failed_conditions: list[str] = []
    failed_pair_count = 0
    for stage in material.stages:
        rows = [_pair_row(pair, metrics, stage.failed.get(pair, []))
                for pair, metrics in stage.pairs.items()
                if pair in allowed_pairs][:limit]
        stages.append({"stage": stage.stage, "state": stage.state,
                       "pairs": rows})
        if failed_stage is None and stage.failed:
            failed_stage = stage.stage
            kinds = {kind for kinds in stage.failed.values() for kind in kinds}
            failed_conditions = [k for k in FAIL_KINDS if k in kinds]
            failed_pair_count = len(
                [pair for pair in stage.failed if pair in allowed_pairs])
    return {
        "thresholds": {
            "min_pf": _number(thresholds.get("min_pf")),
            "require_positive_avg_r": _flag(
                thresholds.get("require_positive_avg_r")),
            "require_holdout_evaluable": _flag(
                thresholds.get("require_holdout_evaluable")),
            "max_drawdown": _number(thresholds.get("max_drawdown")),
            "max_trades_per_eval_bar": _number(
                thresholds.get("max_trades_per_eval_bar"))},
        "failed_stage": failed_stage, "failed_conditions": failed_conditions,
        "failed_pair_count": failed_pair_count, "stages": stages}


def _side(metrics: object) -> dict:
    metrics = metrics if isinstance(metrics, dict) else {}
    return {"trades": _count(metrics.get("trades")),
            "pf": _number(metrics.get("pf")),
            "avg_r": _number(metrics.get("avg_r")),
            "evaluable": _flag(metrics.get("evaluable"))}


def _comparison_item(comparison: dict, allowed_pairs: list[str]
                     ) -> dict | None:
    mode, verdict, reason = (comparison.get("mode"), comparison.get("verdict"),
                             comparison.get("reason"))
    if (mode not in COMPARISON_MODES or verdict not in COMPARISON_VERDICTS
            or reason not in COMPARISON_REASONS):
        return None
    holdout = (comparison.get("stages") or {}).get("holdout") or {}
    pairs = []
    for pair, entry in holdout.items():
        if pair not in allowed_pairs or not isinstance(entry, dict) \
                or entry.get("verdict") not in COMPARISON_VERDICTS:
            continue
        pairs.append({"pair": pair, "verdict": entry["verdict"],
                      "candidate": _side(entry.get("candidate")),
                      "deployed": _side(entry.get("deployed"))})
    return {"mode": mode, "verdict": verdict, "reason": reason,
            "stage": "holdout", "metrics": list(COMPARED_METRICS),
            "pairs": pairs[:min(len(allowed_pairs), PAIR_ROWS_MAX)]}


def prepare(material: SummaryMaterial, plan: SummaryPlan, *,
            allowed_pairs: Iterable[str]) -> PreparedSummary:
    """経路ごとの `plan` が許す項目だけを、材料から整える。tx の前に呼ぶ。
    整形の途中で例外が出たら `failed` で返す (終端は止めない)。"""
    output = output_for_storage(material.output)
    try:
        pairs = list(allowed_pairs)
        items: dict = _claims(material.output, plan.claims)
        if plan.tried != "none" and material.plugin_name is not None:
            tried: dict = {"plugin": material.plugin_name, "kind": None,
                           "origin": None, "indicators": []}
            meta = material.candidate if plan.tried == "full" else None
            if meta is not None:
                tried["kind"] = meta.kind
                tried["origin"] = (None if meta.is_new is None
                                   else "new" if meta.is_new else "update")
                tried["indicators"] = list(
                    meta.indicators[:INDICATOR_NAMES_MAX])
            items["tried"] = tried
        if plan.floor and material.floor is not None:
            items["floor"] = _floor_item(material.floor, pairs)
        if plan.comparison and material.comparison is not None:
            comparison = _comparison_item(material.comparison, pairs)
            if comparison is not None:
                items["comparison"] = comparison
        if plan.observation:
            observation = agent_text(material.observation_text)
            if observation is not None:
                items["observation"] = observation
        if plan.parent_reason and material.parent_reason in PARENT_REASONS:
            items["parent_reason"] = material.parent_reason
        return PreparedSummary(items, output=output)
    except Exception:  # noqa: BLE001 — 要約の失敗で終端を止めない
        _log.warning("improve run summary preparation failed", exc_info=True)
        return PreparedSummary({}, failed=True, output=output)


def with_handoff(prepared: PreparedSummary, tally: dict | None
                 ) -> PreparedSummary:
    """申し送り (`improve_context.family_tally` の結果) を足した写し。"""
    if tally is None or prepared.failed:
        return prepared
    family = _family_name(tally.get("family_key"))
    attempts = _count(tally.get("attempts"))
    outcomes = tally.get("outcomes")
    if family is None or attempts is None or not isinstance(outcomes, dict):
        return prepared
    items = dict(prepared.items)
    items["handoff"] = {
        "family": family, "attempts": attempts,
        "outcomes": {word: count for word, count in outcomes.items()
                     if isinstance(word, str) and type(count) is int
                     and count >= 1}}
    return PreparedSummary(items, output=prepared.output)


def _drop(items: dict, what: str) -> bool:
    if what == "floor.pairs":
        floor = items.get("floor")
        if not floor or not any(s["pairs"] for s in floor["stages"]):
            return False
        for stage in floor["stages"]:
            stage["pairs"] = []
        return True
    if what == "tried.indicators":
        tried = items.get("tried")
        if not tried or not tried["indicators"]:
            return False
        tried["indicators"] = []
        return True
    return items.pop(what, None) is not None


def _dump(items: dict) -> str:
    return json.dumps(items, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def serialize(prepared: PreparedSummary) -> SummaryRecord:
    """最終の直列化。形を検査し、16 KiB を超えるときは説明 → 申し送り →
    pair の順に落とす。検査や直列化に失敗したら `build_failed`。材料が
    1 つも無ければ `empty`。"""
    if prepared.failed:
        return SummaryRecord(STATE_BUILD_FAILED, "{}")
    try:
        items = json.loads(_dump(prepared.items))
        jsonschema.validate(items, ITEMS_SCHEMA)
        if not items:
            return SummaryRecord(STATE_EMPTY, "{}")
        text = _dump(items)
        for what in _DROP_ORDER:
            if len(text.encode("utf-8")) <= SUMMARY_MAX_BYTES:
                break
            if _drop(items, what):
                text = _dump(items)
        if len(text.encode("utf-8")) > SUMMARY_MAX_BYTES:
            return SummaryRecord(STATE_BUILD_FAILED, "{}")
        return SummaryRecord(STATE_OK, text)
    except Exception:  # noqa: BLE001
        _log.warning("improve run summary serialization failed", exc_info=True)
        return SummaryRecord(STATE_BUILD_FAILED, "{}")


# ---------------------------------------------------------------- API の形

def dto_of(row: Any) -> dict | None:
    """保存した 1 行を API の `summary` にする。行が無ければ None。
    `{"version", "state", "items"}`。`ok` の中身が形の検査を通らない
    (壊れた・将来の版の) 行は、中身を出さず `build_failed` に倒す。"""
    if row is None:
        return None
    version = row["summary_version"]
    state = row["state"]
    items: dict = {}
    if state == STATE_OK and version == summaries_store.SUMMARY_VERSION:
        try:
            parsed = json.loads(row["summary_json"])
            jsonschema.validate(parsed, ITEMS_SCHEMA)
            items = parsed
        except (ValueError, RecursionError, jsonschema.ValidationError):
            state = STATE_BUILD_FAILED
    elif state == STATE_OK:
        state = STATE_BUILD_FAILED
    return {"version": version, "state": state, "items": items}
